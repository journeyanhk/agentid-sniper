// Agent ID Sniper — W0 只读影子探针（无钱包、无私钥、不下单、不依赖 baw）
// 多源分块扫描：blockrazor(20) / 48.club(60) / 1rpc(20)，任一源失败自动切换并记录空洞。
import { appendFileSync, mkdirSync } from 'node:fs';
const DIR = process.env.SHADOW_DIR ?? new URL('.', import.meta.url).pathname;
mkdirSync(DIR, { recursive: true });
const SIG = DIR + '/signals.jsonl', MET = DIR + '/metrics.jsonl';
const REGISTRY = '60b9262456de011f7d15a54cdd8c48baaa8b022f';
const T_TRANSFER = '0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef';
const ZERO32 = '0x' + '0'.repeat(64);
// 分块扫描源（按速度排序，max = 该源允许的最大块区间）
const MINT_SRC = [
  { url: 'https://bsc.blockrazor.xyz', max: 20 },
  { url: 'https://rpc.48.club', max: 60 },
  { url: 'https://1rpc.io/bnb', max: 20 },
];
const RPC = ['https://rpc.48.club', 'https://bsc-dataseed.binance.org', 'https://bsc.blockrazor.xyz'];
let errCount = 0, calls = 0, gaps = 0;
const ts = () => new Date().toISOString();
const log = (...a) => console.log(...a);
async function rpcTo(url, method, params, timeout = 25000) {
  calls++;
  const r = await fetch(url, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ jsonrpc: '2.0', id: 1, method, params }), signal: AbortSignal.timeout(timeout) });
  const j = await r.json();
  if (j.error) throw new Error(method + ': ' + j.error.message);
  return j.result;
}
async function rpc(method, params, pool = RPC) {
  let last;
  for (const u of pool) { try { return await rpcTo(u, method, params); } catch (e) { last = e; } }
  errCount++; throw last;
}
const hex = (n) => '0x' + n.toString(16);
const wa = (a) => a.toLowerCase().replace(/^0x/, '').padStart(64, '0');
const big = (h) => (h && h !== '0x' ? BigInt(h.slice(0, 66)) : 0n);
const MINTS = new Map(), seen = new Set(), checked = new Map();
async function scanMints(from, to) {
  const out = [];
  let cur = from, localGap = 0;
  while (cur <= to) {
    let ok = false;
    for (const s of MINT_SRC) {
      const end = Math.min(to, cur + s.max - 1);
      try {
        const r = await rpcTo(s.url, 'eth_getLogs', [{ fromBlock: hex(cur), toBlock: hex(end), topics: [T_TRANSFER, ZERO32] }]);
        out.push(...r); cur = end + 1; ok = true; break;
      } catch (e) { /* 换下一个源 */ }
    }
    if (!ok) { localGap += 20; gaps++; cur += 20; }
  }
  const now = Date.now();
  let fresh = 0;
  for (const l of out) {
    const k = l.transactionHash + ':' + l.logIndex;
    if (seen.has(k)) continue;
    seen.add(k); fresh++;
    const tok = l.address.toLowerCase();
    let m = MINTS.get(tok);
    if (!m) { m = { at: now, to: new Set(), mints: 0 }; MINTS.set(tok, m); }
    m.to.add('0x' + l.topics[2].slice(26));
    m.mints++;
  }
  return { fresh, gap: localGap };
}
async function codeOf(a) { try { return ((await rpc('eth_getCode', [a, 'latest'])) || '').toLowerCase(); } catch { return ''; } }
function strAt(h) {
  try {
    const x = h.slice(2), len = Number(BigInt('0x' + x.slice(64, 128)));
    if (!len || len > 200) return '';
    return Buffer.from(x.slice(128, 128 + len * 2), 'hex').toString('utf8').replace(/[^\x20-\x7e\u4e00-\u9fa5]/g, '').slice(0, 40);
  } catch { return ''; }
}
async function meta(tok) {
  const code = await codeOf(tok);
  const name = strAt((await rpc('eth_call', [{ to: tok, data: '0x06fdde03' }, 'latest']).catch(() => '0x')) || '0x');
  const sym = strAt((await rpc('eth_call', [{ to: tok, data: '0x95d89b41' }, 'latest']).catch(() => '0x')) || '0x');
  let agentHolders = 0;
  const sample = [...(MINTS.get(tok)?.to || [])].slice(0, 7);
  for (const a of sample) {
    const r = await rpc('eth_call', [{ to: '0x' + REGISTRY, data: '0x70a08231' + wa(a) }, 'latest']).catch(() => null);
    if (r && big(r) > 0n) agentHolders++;
  }
  // 是否有 PancakeSwap 池子（退出通道是否存在）
  let pool = null, liq = 0;
  for (const q of ['0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c', '0x55d398326f99059ff775485246999027b3197955']) {
    const p = await rpc('eth_call', [{ to: '0xca143ce32fe78f1f7019d7d551a6402fc5350c73', data: '0xe6a43905' + wa(tok) + wa(q) }, 'latest']).catch(() => null);
    if (p && p.length > 66 && BigInt(p) > 0n) {
      const pair = '0x' + p.slice(-40);
      const rs = await rpc('eth_call', [{ to: pair, data: '0x0902f1ac' }, 'latest']).catch(() => null);
      if (rs && rs.length > 130) { const a = BigInt('0x' + rs.slice(2, 66)); if (a > 0n) { pool = pair; liq = Number(q.startsWith('0xbb4') ? BigInt('0x' + rs.slice(66, 130)) : a) / 1e18; } }
    }
  }
  return { name, sym, codeLen: Math.max(0, code.length / 2 - 1), hasRegistry: code.includes(REGISTRY), hasRewardsSel: code.includes('9f92d0dd'), agentHolders, sampleN: sample.length, pool, liqBnb: +liq.toFixed(4) };
}
async function main() {
  log('[' + ts() + '] shadow probe start (multi-source)');
  let last = 0;
  for (;;) {
    const t0 = Date.now();
    try {
      const head = parseInt(await rpc('eth_blockNumber', []), 16);
      if (!last) last = head - 60; else last = Math.max(last, head - 300);
      const to = Math.min(head, last + 60);
      const { fresh, gap } = await scanMints(last + 1, to);
      last = to;
      let hits = 0, checkedNow = 0, pooled = 0;
      for (const [tok, m] of MINTS) {
        if (Date.now() - m.at > 30 * 60e3) { MINTS.delete(tok); continue; }
        if (m.to.size < 6) continue;
        if (checked.has(tok) && m.to.size < checked.get(tok) * 2) continue;
        checked.set(tok, m.to.size); checkedNow++;
        if (checked.size > 5000) checked.delete(checked.keys().next().value);
        const mm = await meta(tok);
        const gated = mm.hasRegistry || mm.hasRewardsSel;
        if (gated) hits++;
        if (mm.pool) pooled++;
        appendFileSync(SIG, JSON.stringify({ t: ts(), tok, ...mm, wallets: m.to.size, mints: m.mints, ageMin: +((Date.now() - m.at) / 60e3).toFixed(1), gated }) + '\n');
        if (gated) log('  * GATED ' + (mm.sym || mm.name || tok) + ' ' + tok + ' wallets=' + m.to.size + ' agent=' + mm.agentHolders + '/' + mm.sampleN + ' pool=' + (mm.pool ? mm.pool.slice(0, 10) : '-'));
      }
      const ms = Date.now() - t0;
      appendFileSync(MET, JSON.stringify({ t: ts(), head, scannedTo: to, lagBlocks: head - to, fresh, gapBlocks: gap, tokens: MINTS.size, candidates: checkedNow, gatedHits: hits, pooled, ms, calls, err: errCount, gaps }) + '\n');
      log('[' + ts() + '] head=' + head + ' lag=' + (head - to) + ' fresh=' + fresh + ' gap=' + gap + ' mints30m=' + MINTS.size + ' cand=' + checkedNow + ' gated=' + hits + ' pooled=' + pooled + ' ' + ms + 'ms');
    } catch (e) { log('[' + ts() + '] ERR ' + e.message); }
    const used = Date.now() - t0;
    await new Promise((r) => setTimeout(r, Math.max(1500, 12000 - used)));
  }
}
main();

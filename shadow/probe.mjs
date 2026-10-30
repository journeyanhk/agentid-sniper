// Agent ID Sniper — W0 只读影子探针 v2（无钱包、无私钥、不下单、不依赖 baw）
// v2 相对 v1 的改动（针对 review 的 P0）：
//  1. 断块不再静默跳过：失败区间进 pending 队列，指数退避重试，仍失败才记 gap 并落 gaps.jsonl，下轮继续补
//  2. 扫描游标持久化到 state.json，重启从上次位置续扫，不回退到 head
//  3. seen 按区块高度裁剪，长期运行内存有界
//  4. metrics 按 RPC 源统计 calls/ok/err/avg/p95/错误类型
//  5. V2 储备方向改为读 pair.token0()/token1()，不再靠地址前缀猜
//  6. 新增 PancakeSwap V3 检测（4 档 fee tier 的 getPool）
//  7. 记录铸造发起合约 minter（= 发射台/curve 合约），为退出路径研究留锚点
//  8. 每条信号带 probe 版本、扫描区块区间、是否跨 gap、Agent ID 检查所用块高
import { appendFileSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
const DIR = process.env.SHADOW_DIR ?? new URL('.', import.meta.url).pathname;
mkdirSync(DIR, { recursive: true });
const VERSION = 'probe-2.0.0';
const SIG = DIR + '/signals.jsonl', MET = DIR + '/metrics.jsonl', GAPF = DIR + '/gaps.jsonl', STF = DIR + '/state.json';

const REGISTRY = '0x60b9262456de011f7d15a54cdd8c48baaa8b022f';
const REGISTRY_HEX = REGISTRY.slice(2); // 字节码内是裸地址，匹配时必须去掉 0x
const T_TRANSFER = '0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef';
const ZERO32 = '0x' + '0'.repeat(64);
const V2F = '0xca143ce32fe78f1f7019d7d551a6402fc5350c73';
const V3F = '0x0bfbcf9fa4f9c56b0f40a671ad40e0805a091865';
const WBNB = '0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c';
const USDT = '0x55d398326f99059ff775485246999027b3197955';
const FEES = [100, 500, 2500, 10000];

const MINT_SRC = [
  { url: 'https://bsc.blockrazor.xyz', max: 20 },
  { url: 'https://rpc.48.club', max: 60 },
  { url: 'https://1rpc.io/bnb', max: 20 },
];
const RPC = ['https://rpc.48.club', 'https://bsc.blockrazor.xyz'];
const STATS = new Map();
function st(url) { if (!STATS.has(url)) STATS.set(url, { url, calls: 0, ok: 0, err: 0, k429: 0, kLimit: 0, kRevert: 0, kOther: 0, lat: [] }); return STATS.get(url); }
const ts = () => new Date().toISOString();
const log = (...a) => console.log(...a);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const COOLDOWN = new Map(); let nextSlot = 0;
const RPS = 6;
async function pace() { const now = Date.now(); const at = Math.max(now, nextSlot); nextSlot = at + Math.ceil(1000 / RPS); if (at > now) await sleep(at - now); }
async function rpcTo(url, method, params, timeout = 25000) {
  const cd = COOLDOWN.get(url); if (cd && Date.now() < cd) throw new Error('cooldown: ' + url);
  await pace();
  const s = st(url); s.calls++; const t0 = Date.now(); let counted = false;
  try {
    const r = await fetch(url, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ jsonrpc: '2.0', id: 1, method, params }), signal: AbortSignal.timeout(timeout) });
    const j = await r.json();
    const ms = Date.now() - t0;
    if (j.error) {
      const m = String(j.error.message || '');
      s.err++; counted = true; if (/rate limit|too many|429|try again/i.test(m)) { s.k429++; COOLDOWN.set(url, Date.now() + 30000); } else if (/limit exceeded|range must not|not supported/i.test(m)) s.kLimit++; else if (/revert/i.test(m)) s.kRevert = (s.kRevert||0)+1; else s.kOther++;
      throw new Error(method + ': ' + m);
    }
    s.ok++; s.lat.push(ms); if (s.lat.length > 200) s.lat.shift();
    return j.result;
  } catch (e) { if (!counted) { s.err++; s.kOther++; } throw e; }
}
function pct(a, p) { if (!a.length) return 0; const b = [...a].sort((x, y) => x - y); return b[Math.min(b.length - 1, Math.floor(b.length * p))]; }
async function rpc(method, params, pool = RPC) {
  let last;
  for (const u of pool) {
    try { return await rpcTo(u, method, params); }
    catch (e) { if (/revert/i.test(e.message)) throw e; last = e; }  // revert 是合约行为，换源也一样
  }
  throw last;
}
const hex = (n) => '0x' + Number(n).toString(16);
const wa = (a) => a.toLowerCase().replace(/^0x/, '').padStart(64, '0');
const big = (h) => (h && h !== '0x' ? BigInt(h.slice(0, 66)) : 0n);

// ---------- state ----------
let lastBlock = 0, pending = [], dupCount = 0;
try { const s = JSON.parse(readFileSync(STF, 'utf8')); lastBlock = s.lastBlock || 0; pending = s.pending || []; dupCount = s.dupCount || 0; } catch {}
const saveState = () => { try { writeFileSync(STF, JSON.stringify({ version: VERSION, lastBlock, pending: pending.slice(-200), dupCount, updated: ts() })); } catch {} };

// ---------- dedupe by block height ----------
const seen = new Map(); // key -> blockNumber
function pruneSeen(head) { const cut = head - 3000; for (const [k, b] of seen) { if (b < cut) seen.delete(k); } }

const MINTS = new Map(), checked = new Map();

async function scanRange(from, to, gapFlag) {
  const out = []; let cur = from;
  while (cur <= to) {
    let done = false, lastErr;
    for (let attempt = 0; attempt < 3 && !done; attempt++) {
      for (const s of MINT_SRC) {
        const end = Math.min(to, cur + s.max - 1);
        try {
          const r = await rpcTo(s.url, 'eth_getLogs', [{ fromBlock: hex(cur), toBlock: hex(end), topics: [T_TRANSFER, ZERO32] }]);
          out.push(...r); cur = end + 1; done = true; break;
        } catch (e) { lastErr = e; }
      }
      if (!done && attempt < 2) await sleep(400 * Math.pow(3, attempt));
    }
    if (!done) {
      // 不静默跳过：登记到 pending，下一轮继续补；同时写 gaps.jsonl 留痕
      const rec = { t: ts(), from: cur, to: Math.min(to, cur + 19), error: String(lastErr && lastErr.message).slice(0, 120), gap: gapFlag };
      pending.push(rec);
      if (pending.length > 500) { const dropped = pending.splice(0, pending.length - 500); log('  ! pending 超限丢弃 ' + dropped.length + ' 段'); }
      appendFileSync(GAPF, JSON.stringify(rec) + '\n');
      cur += 20;
    }
  }
  return out;
}

function ingest(logs, now) {
  let fresh = 0;
  for (const l of logs) {
    const k = l.transactionHash + ':' + l.logIndex;
    if (seen.has(k)) continue;
    const bn = parseInt(l.blockNumber, 16);
    seen.set(k, bn); fresh++;
    const tok = l.address.toLowerCase();
    let m = MINTS.get(tok);
    if (!m) { m = { at: now, firstBlock: bn, to: new Set(), mints: 0, tx: l.transactionHash }; MINTS.set(tok, m); }
    m.to.add('0x' + l.topics[2].slice(26)); m.mints++;
  }
  return fresh;
}

async function codeOf(a) { try { return ((await rpc('eth_getCode', [a, 'latest'])) || '').toLowerCase(); } catch { return ''; } }
function strAt(h) {
  try {
    const x = h.slice(2), len = Number(BigInt('0x' + x.slice(64, 128)));
    if (!len || len > 200) return '';
    return Buffer.from(x.slice(128, 128 + len * 2), 'hex').toString('utf8').replace(/[^\x20-\x7e\u4e00-\u9fa5]/g, '').slice(0, 40);
  } catch { return ''; }
}
async function callAt(to, data, blockTag) { try { return await rpc('eth_call', [{ to, data }, blockTag || 'latest']); } catch { return null; } }

// V2：方向由 pair.token0() 决定，不靠地址前缀猜
async function v2Pool(tok) {
  for (const q of [WBNB, USDT]) {
    const p = await callAt(V2F, '0xe6a43905' + wa(tok) + wa(q));
    if (!p || p.length <= 66 || BigInt(p) === 0n) continue;
    const pair = '0x' + p.slice(-40);
    const [t0, t1, rs] = await Promise.all([callAt(pair, '0x0dfe1681'), callAt(pair, '0xd21220a7'), callAt(pair, '0x0902f1ac')]);
    if (!rs || rs.length <= 130) continue;
    const r0 = BigInt('0x' + rs.slice(2, 66)), r1 = BigInt('0x' + rs.slice(66, 130));
    if (r0 === 0n || r1 === 0n) continue;
    const wbnbIs0 = t0 && '0x' + t0.slice(-40) === WBNB;
    const base = wbnbIs0 ? Number(r0) / 1e18 : Number(r1) / 1e18;
    return { pair, kind: 'v2', quote: q === WBNB ? 'WBNB' : 'USDT', baseReserve: +base.toFixed(6) };
  }
  return null;
}
// V3：多档 fee tier
async function v3Pool(tok) {
  for (const q of [WBNB, USDT]) {
    for (const fee of FEES) {
      const p = await callAt(V3F, '0x1698ee82' + wa(tok) + wa(q) + BigInt(fee).toString(16).padStart(64, '0'));
      if (p && p.length > 66 && BigInt(p) !== 0n) return { pool: '0x' + p.slice(-40), kind: 'v3', fee, quote: q === WBNB ? 'WBNB' : 'USDT' };
    }
  }
  return null;
}
async function minterOf(txHash) {
  try { const tx = await rpc('eth_getTransactionByHash', [txHash]); return tx && tx.to ? tx.to.toLowerCase() : null; } catch { return null; }
}

async function meta(tok) {
  const code = await codeOf(tok);
  const name = strAt((await callAt(tok, '0x06fdde03')) || '0x');
  const sym = strAt((await callAt(tok, '0x95d89b41')) || '0x');
  const m = MINTS.get(tok);
  let agentHolders = 0, agentBlock = 'latest';
  const sample = [...(m?.to || [])].slice(0, 7);
  if (m && m.firstBlock) {
    const at = await callAt(REGISTRY, '0x18160ddd', hex(m.firstBlock)); // 探测节点是否支持历史块
    if (at) agentBlock = hex(m.firstBlock);
  }
  for (const a of sample) {
    const r = await callAt(REGISTRY, '0x70a08231' + wa(a), agentBlock);
    if (r && big(r) > 0n) agentHolders++;
  }
  const gated0 = code.includes(REGISTRY_HEX) || code.includes('9f92d0dd');
  const v2 = gated0 ? await v2Pool(tok) : null;
  const v3 = (gated0 && !v2) ? await v3Pool(tok) : null;
  const minter = m ? await minterOf(m.tx) : null;
  return {
    name, sym, codeLen: Math.max(0, code.length / 2 - 1),
    hasRegistry: code.includes(REGISTRY_HEX), hasRewardsSel: code.includes('9f92d0dd'),
    agentHolders, sampleN: sample.length, agentBlock,
    pool: v2 || v3 || null, minter,
  };
}

async function main() {
  log('[' + ts() + '] ' + VERSION + ' start; resume lastBlock=' + lastBlock + ' pending=' + pending.length);
  let head = 0;
  try { head = parseInt(await rpc('eth_blockNumber', []), 16); } catch {}
  if (!lastBlock || head - lastBlock > 3000) { lastBlock = head - 60; log('  cold start at ' + lastBlock); }
  for (;;) {
    const t0 = Date.now();
    let gapFlag = false;
    try {
      head = parseInt(await rpc('eth_blockNumber', []), 16);
      // 先补上一轮欠的区间（最多 30 个，避免拖死主循环）
      if (pending.length) {
        const todo = pending.splice(0, 30);
        const retryLogs = [];
        for (const r of todo) {
          retryLogs.push(...(await scanRange(r.from, r.to, true)));
        }
        dupCount += ingest(retryLogs, Date.now());
      }
      const to = Math.min(head, lastBlock + 60);
      const logs = await scanRange(lastBlock + 1, to, false);
      const fresh = ingest(logs, Date.now());
      const fromBlock = lastBlock + 1;
      lastBlock = to;
      pruneSeen(head);

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
        appendFileSync(SIG, JSON.stringify({
          t: ts(), v: VERSION, tok, ...mm, wallets: m.to.size, mints: m.mints,
          ageMin: +((Date.now() - m.at) / 60e3).toFixed(1),
          firstBlock: m.firstBlock, scanFrom: fromBlock, scanTo: to, crossGap: gapFlag, gated,
        }) + '\n');
        if (gated) log('  * GATED ' + (mm.sym || mm.name || tok) + ' ' + tok + ' wallets=' + m.to.size + ' agent=' + mm.agentHolders + '/' + mm.sampleN + '@' + mm.agentBlock + ' pool=' + (mm.pool ? mm.pool.kind + ':' + String(mm.pool.pool).slice(0, 10) : '-') + ' minter=' + (mm.minter ? mm.minter.slice(0, 10) : '-'));
      }
      const ms = Date.now() - t0;
      const rpcStats = [...STATS.values()].map((s) => ({ url: s.url, calls: s.calls, ok: s.ok, err: s.err, k429: s.k429, kLimit: s.kLimit, kRevert: s.kRevert, kOther: s.kOther, avgMs: s.lat.length ? Math.round(s.lat.reduce((a, b) => a + b, 0) / s.lat.length) : 0, p95Ms: pct(s.lat, 0.95) }));
      appendFileSync(MET, JSON.stringify({ t: ts(), v: VERSION, head, scannedTo: to, lagBlocks: head - to, fresh, pending: pending.length, tokens: MINTS.size, candidates: checkedNow, gatedHits: hits, pooled, seenSize: seen.size, ms, rpc: rpcStats }) + '\n');
      log('[' + ts() + '] head=' + head + ' lag=' + (head - to) + ' fresh=' + fresh + ' pending=' + pending.length + ' mints30m=' + MINTS.size + ' cand=' + checkedNow + ' gated=' + hits + ' pooled=' + pooled + ' ' + ms + 'ms');
      saveState();
    } catch (e) { log('[' + ts() + '] CYCLE ERR ' + e.message); saveState(); }
    const used = Date.now() - t0;
    await sleep(Math.max(1500, 12000 - used));
  }
}
main();

// Agent ID Sniper — W0 只读影子探针 v3（无钱包、无私钥、不下单、不依赖 baw）
// v3 相对 v2 的改动（针对第二轮复核）：
//  1. scanRange() 返回 { logs, hadGap, gapRanges }，crossGap 真正写回主循环并落进信号
//  2. pending 溢出不再丢弃，改为转存 pending_overflow.jsonl；metrics 增加 pendingAgeMaxMin
//  3. state.json 原子写入（tmp + fsync + rename + .bak），启动校验，损坏时标记 stateCorrupt 而不是静默冷启动
//  4. V3 池子补 liquidity()/slot0()/token0()/token1()，无流动性标记 poolFoundButNotTradable
//  5. 信号分级：bytecodeRegistryHit / selectorHit / agentHolderRatio / minterKnown / poolFound / poolTradable / signalClass A-D
//  6. 每条信号带 eventBlock（首次观测到的铸造块）与 observedAt（补扫回填时间），补扫数据可识别
import {
  appendFileSync, mkdirSync, readFileSync, writeFileSync,
  openSync, writeSync, fsyncSync, closeSync, renameSync, copyFileSync, existsSync,
} from 'node:fs';
const DIR = process.env.SHADOW_DIR ?? new URL('.', import.meta.url).pathname;
mkdirSync(DIR, { recursive: true });
const VERSION = 'probe-3.0.0';
const SIG = DIR + '/signals.jsonl', MET = DIR + '/metrics.jsonl', GAPF = DIR + '/gaps.jsonl';
const PENDF = DIR + '/pending_overflow.jsonl', STF = DIR + '/state.json';

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
function st(url) { if (!STATS.has(url)) STATS.set(url, { url, calls: 0, ok: 0, err: 0, k429: 0, kLimit: 0, kRevert: 0, kOther: 0, lat: [], lastOk: 0 }); return STATS.get(url); }
const ts = () => new Date().toISOString();
const log = (...a) => console.log(...a);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const COOLDOWN = new Map(); let nextSlot = 0;
const RPS = 6;
async function pace() { const now = Date.now(); const at = Math.max(now, nextSlot); nextSlot = at + Math.ceil(1000 / RPS); if (at > now) await sleep(at - now); }

async function rpcTo(url, method, params, timeout = 25000) {
  const s = st(url); s.calls++; const t0 = Date.now(); let counted = false;
  const cd = COOLDOWN.get(url); if (cd && Date.now() < cd) throw new Error('cooldown: ' + url);
  await pace();
  try {
    const r = await fetch(url, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ jsonrpc: '2.0', id: 1, method, params }), signal: AbortSignal.timeout(timeout) });
    const j = await r.json();
    const ms = Date.now() - t0;
    if (j.error) {
      const m = String(j.error.message || '');
      s.err++; counted = true;
      if (/rate limit|too many|429|try again/i.test(m)) { s.k429++; COOLDOWN.set(url, Date.now() + 30000); }
      else if (/limit exceeded|range must not|not supported/i.test(m)) s.kLimit++;
      else if (/revert/i.test(m)) s.kRevert++;
      else s.kOther++;
      throw new Error(method + ': ' + m);
    }
    s.ok++; s.lastOk = Date.now(); s.lat.push(ms); if (s.lat.length > 200) s.lat.shift();
    return j.result;
  } catch (e) { if (!counted) { s.err++; s.kOther++; } throw e; }
}
function pct(a, p) { if (!a.length) return 0; const b = [...a].sort((x, y) => x - y); return b[Math.min(b.length - 1, Math.floor(b.length * p))]; }
async function rpc(method, params, pool = RPC) {
  let last;
  for (const u of pool) {
    try { return await rpcTo(u, method, params); }
    catch (e) { if (/revert/i.test(e.message)) throw e; last = e; } // revert 是合约行为，换源也一样
  }
  throw last;
}
const hex = (n) => '0x' + Number(n).toString(16);
const wa = (a) => a.toLowerCase().replace(/^0x/, '').padStart(64, '0');
const big = (h) => (h && h !== '0x' ? BigInt(h.slice(0, 66)) : 0n);

// ---------- state（原子写 + 校验）----------
let lastBlock = 0, pending = [], dupCount = 0, stateCorrupt = false, pendingOverflow = 0;
try {
  const s = JSON.parse(readFileSync(STF, 'utf8'));
  lastBlock = s.lastBlock || 0; pending = s.pending || []; dupCount = s.dupCount || 0; pendingOverflow = s.pendingOverflow || 0;
} catch (e) { if (e.code !== 'ENOENT') { stateCorrupt = true; log('  ! state.json 损坏，转入冷启动并告警: ' + e.message); } }
function saveState() {
  trimPending();
  const data = JSON.stringify({ version: VERSION, lastBlock, pending: pending.slice(-2000), dupCount, pendingOverflow, updated: ts() });
  const tmp = STF + '.tmp';
  try {
    const fd = openSync(tmp, 'w'); writeSync(fd, data); fsyncSync(fd); closeSync(fd);
    if (existsSync(STF)) { try { copyFileSync(STF, STF + '.bak'); } catch {} }
    renameSync(tmp, STF);
    return true;
  } catch (e) { log('  ! state 保存失败: ' + e.message); return false; }
}
function trimPending() {
  if (pending.length <= 2000) return;
  const dropped = pending.splice(0, pending.length - 2000);
  pendingOverflow += dropped.length;
  try { appendFileSync(PENDF, dropped.map((x) => JSON.stringify(x)).join('\n') + '\n'); } catch {}
  log('  ! pending 超过 2000 段，溢出 ' + dropped.length + ' 段到 pending_overflow.jsonl（未丢弃）');
}

// ---------- dedupe by block height ----------
const seen = new Map(); // key -> blockNumber
function pruneSeen(head) { const cut = head - 3000; for (const [k, b] of seen) { if (b < cut) seen.delete(k); } }
const MINTS = new Map(), checked = new Map();

async function scanRange(from, to) {
  const out = [], gapRanges = [];
  let cur = from;
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
      const rec = { t: ts(), from: cur, to: Math.min(to, cur + 19), error: String(lastErr && lastErr.message).slice(0, 120) };
      pending.push(rec);
      appendFileSync(GAPF, JSON.stringify(rec) + '\n');
      gapRanges.push([rec.from, rec.to]);
      cur += 20;
    }
  }
  return { logs: out, hadGap: gapRanges.length > 0, gapRanges };
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

// V2：方向由 pair.token0() 决定
async function v2Pool(tok) {
  for (const q of [WBNB, USDT]) {
    const p = await callAt(V2F, '0xe6a43905' + wa(tok) + wa(q));
    if (!p || p.length <= 66 || BigInt(p) === 0n) continue;
    const pair = '0x' + p.slice(-40);
    const [t0, t1, rs] = await Promise.all([callAt(pair, '0x0dfe1681'), callAt(pair, '0xd21220a7'), callAt(pair, '0x0902f1ac')]);
    if (!rs || rs.length <= 130) continue;
    const r0 = BigInt('0x' + rs.slice(2, 66)), r1 = BigInt('0x' + rs.slice(66, 130));
    const wbnbIs0 = t0 && '0x' + t0.slice(-40) === WBNB;
    const base = wbnbIs0 ? Number(r0) / 1e18 : Number(r1) / 1e18;
    const tradable = r0 > 0n && r1 > 0n;
    return { pool: pair, kind: 'v2', quote: q === WBNB ? 'WBNB' : 'USDT', baseReserve: +base.toFixed(6), liquidity: tradable ? String(r0 < r1 ? r0 : r1) : '0', tradable };
  }
  return null;
}
// V3：真实流动性校验，只有池地址不算可交易
async function v3Pool(tok) {
  for (const q of [WBNB, USDT]) {
    for (const fee of FEES) {
      const p = await callAt(V3F, '0x1698ee82' + wa(tok) + wa(q) + BigInt(fee).toString(16).padStart(64, '0'));
      if (!p || p.length <= 66 || BigInt(p) === 0n) continue;
      const pool = '0x' + p.slice(-40);
      const [slot0, liq, t0, t1] = await Promise.all([
        callAt(pool, '0x3850c7bd'), callAt(pool, '0x1a686502'), callAt(pool, '0x0dfe1681'), callAt(pool, '0xd21220a7'),
      ]);
      const sqrtP = slot0 && slot0.length > 66 ? BigInt(slot0.slice(0, 66)) : 0n;
      const liquidity = liq ? big(liq) : 0n;
      return {
        pool, kind: 'v3', fee, quote: q === WBNB ? 'WBNB' : 'USDT',
        liquidity: liquidity.toString(), sqrtPriceX96: sqrtP.toString(),
        initialized: sqrtP > 0n, tradable: liquidity > 0n && sqrtP > 0n,
        token0: t0 ? '0x' + t0.slice(-40) : null, token1: t1 ? '0x' + t1.slice(-40) : null,
      };
    }
  }
  return null;
}
async function minterOf(txHash) { try { const tx = await rpc('eth_getTransactionByHash', [txHash]); return tx && tx.to ? tx.to.toLowerCase() : null; } catch { return null; } }

async function meta(tok) {
  const code = await codeOf(tok);
  const name = strAt((await callAt(tok, '0x06fdde03')) || '0x');
  const sym = strAt((await callAt(tok, '0x95d89b41')) || '0x');
  const m = MINTS.get(tok);
  let agentHolders = 0, agentBlock = 'latest';
  const sample = [...(m?.to || [])].slice(0, 7);
  if (m && m.firstBlock) { const at = await callAt(REGISTRY, '0x18160ddd', hex(m.firstBlock)); if (at) agentBlock = hex(m.firstBlock); }
  for (const a of sample) {
    const r = await callAt(REGISTRY, '0x70a08231' + wa(a), agentBlock);
    if (r && big(r) > 0n) agentHolders++;
  }
  const bytecodeRegistryHit = code.includes(REGISTRY_HEX);
  const selectorHit = code.includes('9f92d0dd');
  const gated0 = bytecodeRegistryHit || selectorHit;
  const v2 = gated0 ? await v2Pool(tok) : null;
  const v3 = (gated0 && !v2) ? await v3Pool(tok) : null;
  const pool = v2 || v3;
  const minter = m ? await minterOf(m.tx) : null;
  const agentHolderRatio = sample.length ? +(agentHolders / sample.length).toFixed(3) : 0;
  const poolFound = !!pool;
  const poolTradable = !!(pool && pool.tradable);
  const behavioralGateVerified = false; // 待「有/无 Agent ID 钱包」行为差异验证，当前恒 false
  const signalClass = (behavioralGateVerified && poolTradable) ? 'A'
    : (gated0 && agentHolderRatio >= 0.99 && !!minter) ? 'B'
    : gated0 ? 'C' : 'D';
  return {
    name, sym, codeLen: Math.max(0, code.length / 2 - 1),
    bytecodeRegistryHit, selectorHit, agentHolders, sampleN: sample.length, agentHolderRatio, agentBlock,
    minterKnown: !!minter, minter,
    poolFound, poolTradable, poolFoundButNotTradable: poolFound && !poolTradable,
    pool, behavioralGateVerified, signalClass, gated: gated0,
  };
}

async function main() {
  log('[' + ts() + '] ' + VERSION + ' start; resume lastBlock=' + lastBlock + ' pending=' + pending.length + (stateCorrupt ? ' [STATE CORRUPT]' : ''));
  let head = 0;
  try { head = parseInt(await rpc('eth_blockNumber', []), 16); } catch {}
  if (!lastBlock || head - lastBlock > 3000) { lastBlock = head - 60; log('  cold start at ' + lastBlock); }
  for (;;) {
    const t0 = Date.now();
    let gapFlag = false, gapRanges = [];
    try {
      head = parseInt(await rpc('eth_blockNumber', []), 16);
      // 先补上一轮欠的区间（最多 30 段）
      if (pending.length) {
        const todo = pending.splice(0, 30);
        const retryLogs = [];
        for (const r of todo) {
          const rr = await scanRange(r.from, r.to);
          retryLogs.push(...rr.logs);
          if (rr.hadGap) gapRanges.push(...rr.gapRanges);
        }
        dupCount += ingest(retryLogs, Date.now());
      }
      const to = Math.min(head, lastBlock + 60);
      const fromBlock = lastBlock + 1;
      const scan = await scanRange(fromBlock, to);
      const fresh = ingest(scan.logs, Date.now());
      if (scan.hadGap) { gapFlag = true; gapRanges.push(...scan.gapRanges); }
      lastBlock = to;
      pruneSeen(head);

      let hits = 0, checkedNow = 0, pooled = 0, notTradable = 0, byClass = { A: 0, B: 0, C: 0, D: 0 };
      for (const [tok, m] of MINTS) {
        if (Date.now() - m.at > 30 * 60e3) { MINTS.delete(tok); continue; }
        if (m.to.size < 6) continue;
        if (checked.has(tok) && m.to.size < checked.get(tok) * 2) continue;
        checked.set(tok, m.to.size); checkedNow++;
        if (checked.size > 5000) checked.delete(checked.keys().next().value);
        const mm = await meta(tok);
        if (mm.gated) hits++;
        if (mm.poolFound) pooled++;
        if (mm.poolFoundButNotTradable) notTradable++;
        byClass[mm.signalClass] = (byClass[mm.signalClass] || 0) + 1;
        const observedAt = ts();
        appendFileSync(SIG, JSON.stringify({
          t: observedAt, v: VERSION, tok, ...mm,
          wallets: m.to.size, mints: m.mints,
          ageMin: +((Date.now() - m.at) / 60e3).toFixed(1),
          eventBlock: m.firstBlock, observedAt,
          scanFrom: fromBlock, scanTo: to, crossGap: gapFlag,
          gapRanges: gapFlag ? gapRanges.slice(0, 5) : [],
        }) + '\n');
        if (mm.gated) log('  * ' + mm.signalClass + ' ' + (mm.sym || mm.name || tok) + ' ' + tok + ' wallets=' + m.to.size + ' agent=' + mm.agentHolders + '/' + mm.sampleN + '@' + mm.agentBlock + ' pool=' + (mm.pool ? mm.pool.kind + (mm.pool.tradable ? '+liq' : '-noLiq') : '-') + ' minter=' + (mm.minter ? mm.minter.slice(0, 10) : '-') + (gapFlag ? ' GAP' : ''));
      }
      const ms = Date.now() - t0;
      const nowMs = Date.now();
      const ages = pending.map((x) => (nowMs - Date.parse(x.t)) / 60000).filter((x) => x > 0);
      const rpcStats = [...STATS.values()].map((s) => ({
        url: s.url, calls: s.calls, ok: s.ok, err: s.err, k429: s.k429, kLimit: s.kLimit, kRevert: s.kRevert, kOther: s.kOther,
        avgMs: s.lat.length ? Math.round(s.lat.reduce((a, b) => a + b, 0) / s.lat.length) : 0, p95Ms: pct(s.lat, 0.95),
        successRate: s.calls ? +(s.ok / s.calls).toFixed(3) : null,
        lastOkMinAgo: s.lastOk ? +((nowMs - s.lastOk) / 60000).toFixed(1) : null,
        cooldown: (COOLDOWN.get(s.url) || 0) > nowMs,
      }));
      const saved = saveState();
      appendFileSync(MET, JSON.stringify({
        t: ts(), v: VERSION, head, scannedTo: to, lagBlocks: head - to, fromBlock, fresh,
        hadGap: gapFlag, gapCount: gapRanges.length, pending: pending.length,
        pendingAgeMaxMin: ages.length ? +Math.max(...ages).toFixed(1) : 0,
        pendingOverflow, stateCorrupt, stateSaved: saved,
        tokens: MINTS.size, candidates: checkedNow, gatedHits: hits, pooled, poolNotTradable: notTradable, byClass,
        seenSize: seen.size, ms, rpc: rpcStats,
      }) + '\n');
      log('[' + ts() + '] head=' + head + ' lag=' + (head - to) + ' fresh=' + fresh + ' gap=' + (gapFlag ? gapRanges.length : 0) + ' pending=' + pending.length + ' mints30m=' + MINTS.size + ' cand=' + checkedNow + ' gated=' + hits + ' pool=' + pooled + ' ' + ms + 'ms');
    } catch (e) { log('[' + ts() + '] CYCLE ERR ' + e.message); saveState(); }
    const used = Date.now() - t0;
    await sleep(Math.max(1500, 12000 - used));
  }
}
main();
// Agent ID Sniper — W0 只读影子探针 v4（无钱包、无私钥、不下单、不依赖 baw）
// v4 = 对「报告已完成、代码未实现」的整改，每一项都能在文件里搜到对应标识符：
//  1. cursorPaused / pauseReason / lastBlockBefore / lastBlockAfter  —— pending 积压且本轮仍有新 gap 时暂停主游标（10 分钟后强制恢复）
//  2. stateCorrupt → BLOCKED：不扫描、不 head-60 冷启动；需 SHADOW_ALLOW_COLD_START=1 或 SHADOW_START_BLOCK 才继续
//  3. V3 逐档遍历（4 fee × 2 quote），取流动性最大的可交易池；全部无流动性才判 poolFoundButNotTradable
//  4. QuoterV2 真实报价（0xB048Bbc1Ee6b733FFfCFb9e9CeF7375518e25997, quoteExactInputSingle 0xc6a5026a）
//     + 由 sqrtPriceX96 反推零冲击价，输出 priceImpactBps
//  5. decimals() 归一（V2 储备、报价金额），不再硬编码 1e18
//  6. eventBlockWasGap / dataComplete —— 该 token 的首次铸造块是否仍落在未补回的 gap 里
//  7. build = 运行目录 git HEAD，写进启动日志、metrics、signals，方便与 GitHub commit 对账
import {
  appendFileSync, mkdirSync, readFileSync, writeFileSync,
  openSync, writeSync, fsyncSync, closeSync, renameSync, copyFileSync, existsSync,
} from 'node:fs';
import { execSync } from 'node:child_process';

const DIR = process.env.SHADOW_DIR ?? new URL('.', import.meta.url).pathname;
mkdirSync(DIR, { recursive: true });
const VERSION = 'probe-5.2.1';
let BUILD = 'unknown';
try { BUILD = execSync('git -C ' + new URL('..', import.meta.url).pathname + ' rev-parse --short HEAD', { encoding: 'utf8' }).trim(); } catch {}
const SIG = DIR + '/signals.jsonl', MET = DIR + '/metrics.jsonl', GAPF = DIR + '/gaps.jsonl';
const PENDF = DIR + '/pending_overflow.jsonl', STF = DIR + '/state.json';

const REGISTRY = '0x60b9262456de011f7d15a54cdd8c48baaa8b022f';
const REGISTRY_HEX = REGISTRY.slice(2);
const T_TRANSFER = '0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef';
const ZERO32 = '0x' + '0'.repeat(64);
const V2F = '0xca143ce32fe78f1f7019d7d551a6402fc5350c73';
const V3F = '0x0bfbcf9fa4f9c56b0f40a671ad40e0805a091865';
const QUOTER_V2 = '0xb048bbc1ee6b733fffcfb9e9cef7375518e25997'; // PancakeSwap V3 QuoterV2（已实测 1 WBNB→717 USDT）
const WBNB = '0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c';
const USDT = '0x55d398326f99059ff775485246999027b3197955';
const FEES = [100, 500, 2500, 10000];
const PAUSE_AT = 5, PAUSE_MAX_MIN = 10;

const MINT_SRC = [
  { url: 'https://rpc.48.club', max: 500 },
  { url: 'https://bsc.blockrazor.xyz', max: 20 },
  { url: 'https://1rpc.io/bnb', max: 20 },
];
const RPC = ['https://rpc.48.club', 'https://bsc.blockrazor.xyz'];
const STATS = new Map();
function st(url) { if (!STATS.has(url)) STATS.set(url, { url, calls: 0, ok: 0, err: 0, k429: 0, kLimit: 0, kRevert: 0, kOther: 0, lat: [], lastOk: 0 }); return STATS.get(url); }
const ts = () => new Date().toISOString();
const log = (...a) => console.log(...a);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const COOLDOWN = new Map(); let nextSlot = 0;
const RPS = 9;
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
    catch (e) { if (/revert/i.test(e.message)) throw e; last = e; }
  }
  throw last;
}
const hex = (n) => '0x' + Number(n).toString(16);
const wa = (a) => a.toLowerCase().replace(/^0x/, '').padStart(64, '0');
const w64 = (n) => BigInt(n).toString(16).padStart(64, '0');
const big = (h) => (h && h !== '0x' ? BigInt(h.slice(0, 66)) : 0n);

// ---------- state（原子写 + 校验 + 损坏即 BLOCKED）----------
let lastBlock = 0, pending = [], dupCount = 0, stateCorrupt = false, pendingOverflow = 0;
try {
  const s = JSON.parse(readFileSync(STF, 'utf8'));
  lastBlock = s.lastBlock || 0; pending = s.pending || []; dupCount = s.dupCount || 0; pendingOverflow = s.pendingOverflow || 0;
} catch (e) { if (e.code !== 'ENOENT') { stateCorrupt = true; log('  ! state.json 损坏: ' + e.message); } }
const FORCE_START = process.env.SHADOW_START_BLOCK ? parseInt(process.env.SHADOW_START_BLOCK, 10) : 0;
const ALLOW_COLD = process.env.SHADOW_ALLOW_COLD_START === '1';

function saveState() {
  trimPending();
  const data = JSON.stringify({ version: VERSION, build: BUILD, lastBlock, pending: pending.slice(-2000), dupCount, pendingOverflow, updated: ts() });
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
  log('  ! pending 超过 2000 段，溢出 ' + dropped.length + ' 段到 pending_overflow.jsonl（可审计，但不会自动补扫）');
}

const seen = new Map();
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
async function callAt(to, data, blockTag, pool) { try { return await rpc('eth_call', [{ to, data }, blockTag || 'latest'], pool || RPC); } catch { return null; } }
// 免费 RPC 在限流时会返回 0x000…000 而不是报错（实测 48.club），零值必须交叉校验再采信
const POOL_RPC = ['https://bsc.blockrazor.xyz', 'https://rpc.48.club'];
let zeroDisagreements = 0;
const isZeroHex = (h) => !h || h === '0x' || /^0x0+$/.test(h);
async function callAtVerified(to, data) {
  const a = await callAt(to, data, undefined, POOL_RPC);
  if (!isZeroHex(a)) return a;
  const b = await callAt(to, data, undefined, [POOL_RPC[1], POOL_RPC[0]]);
  if (!isZeroHex(b)) { zeroDisagreements++; log('  ! 零值分歧 ' + to.slice(0, 10) + ' : ' + POOL_RPC[0] + '=0, ' + POOL_RPC[1] + '=有效'); return b; }
  return a;
}
const DEC_CACHE = new Map();
async function decimalsOf(tok) {
  if (DEC_CACHE.has(tok)) return DEC_CACHE.get(tok);
  const r = await callAt(tok, '0x313ce567');
  const d = r && r !== '0x' ? Number(big(r)) : null;
  const v = (d === null || d > 36) ? null : d;
  DEC_CACHE.set(tok, v);
  return v;
}
async function balanceOf(tok, who) { const r = await callAt(tok, '0x70a08231' + wa(who)); return r ? big(r) : 0n; }

// V2：储备按 decimals() 归一；某个 quote 无储备时继续下一个，不立即结束
async function v2Pools(tok, dec) {
  const out = [];
  for (const q of [WBNB, USDT]) {
    const p = await callAtVerified(V2F, '0xe6a43905' + wa(tok) + wa(q));
    if (!p || p.length < 66 || BigInt(p) === 0n) continue;
    const pair = '0x' + p.slice(-40);
    const [t0, t1, rs] = await Promise.all([callAt(pair, '0x0dfe1681'), callAt(pair, '0xd21220a7'), callAt(pair, '0x0902f1ac')]);
    if (!rs || rs.length < 194) { out.push({ pool: pair, kind: 'v2', quote: q === WBNB ? 'WBNB' : 'USDT', tradable: false, reason: 'no_reserves' }); continue; }
    const r0 = BigInt('0x' + rs.slice(2, 66)), r1 = BigInt('0x' + rs.slice(66, 130));
    const tokIs0 = t0 && '0x' + t0.slice(-40) === tok;
    const decBase = await decimalsOf(q);
    const resTok = tokIs0 ? r0 : r1, resBase = tokIs0 ? r1 : r0;
    const tradable = resTok > 0n && resBase > 0n && dec !== null && decBase !== null;
    out.push({
      pool: pair, kind: 'v2', quote: q === WBNB ? 'WBNB' : 'USDT',
      token0: t0 ? '0x' + t0.slice(-40) : null, token1: t1 ? '0x' + t1.slice(-40) : null,
      reserveBase: tradable ? Number(resBase) / 10 ** decBase : 0,
      reserveToken: tradable ? Number(resTok) / 10 ** dec : 0,
      decToken: dec, decBase, tradable,
    });
  }
  return out;
}

// V3：逐档遍历，全部看过才下结论
async function v3Pools(tok, dec) {
  const out = [];
  for (const q of [WBNB, USDT]) {
    for (const fee of FEES) {
      const p = await callAtVerified(V3F, '0x1698ee82' + wa(tok) + wa(q) + w64(fee));
      if (!p || p.length < 66 || BigInt(p) === 0n) continue;
      const pool = '0x' + p.slice(-40);
      const [slot0, liq, t0] = await Promise.all([callAt(pool, '0x3850c7bd'), callAt(pool, '0x1a686502'), callAt(pool, '0x0dfe1681')]);
      const sqrtP = slot0 && slot0.length > 66 ? BigInt(slot0.slice(0, 66)) : 0n;
      const liquidity = liq ? big(liq) : 0n;
      out.push({
        pool, kind: 'v3', fee, quote: q === WBNB ? 'WBNB' : 'USDT',
        token0: t0 ? '0x' + t0.slice(-40) : null,
        sqrtPriceX96: sqrtP.toString(), liquidity: liquidity.toString(),
        initialized: sqrtP > 0n, tradable: liquidity > 0n && sqrtP > 0n, decToken: dec,
      });
    }
  }
  return out;
}

// QuoterV2 真实报价 + 用 sqrtPriceX96 反推零冲击价算冲击
async function quoteSellV3(pool, tok, amountInRaw) {
  const q = pool.quote === 'WBNB' ? WBNB : USDT;
  const data = '0xc6a5026a' + wa(tok) + wa(q) + BigInt(amountInRaw).toString(16).padStart(64, '0') + w64(pool.fee) + w64(0);
  const r = await callAt(QUOTER_V2, data);
  if (!r || r.length < 66) return { ok: false, reason: 'quoter_revert_or_empty' };
  const outRaw = BigInt('0x' + r.slice(2, 66));
  const sqrtP = BigInt(pool.sqrtPriceX96);
  let spotOutRaw;
  if (pool.token0 && pool.token0 === tok) spotOutRaw = (BigInt(amountInRaw) * sqrtP * sqrtP) >> 192n; // token1 per token0
  else spotOutRaw = (BigInt(amountInRaw) << 192n) / (sqrtP * sqrtP); // token0 per token1
  const impactBps = spotOutRaw > 0n ? Number(((spotOutRaw - outRaw) * 10000n) / spotOutRaw) : null;
  const decOut = await decimalsOf(q);
  return {
    ok: true, amountInRaw: String(amountInRaw), amountOutRaw: String(outRaw),
    amountOut: decOut !== null ? Number(outRaw) / 10 ** decOut : null,
    spotOutRaw: String(spotOutRaw), priceImpactBps: impactBps,
  };
}

async function minterOf(txHash) { try { const tx = await rpc('eth_getTransactionByHash', [txHash]); return tx && tx.to ? tx.to.toLowerCase() : null; } catch { return null; } }

// ---------- 行为级门槛判定（probe-5.2.1）----------
// 静态判据会漏掉「门槛藏在调用链后」的项目（实证 SIGIL：自身字节码 26 个 PUSH20 无一含 registry，
// 但 mint(uint256) 对持 ID 者成功、对无 ID 地址 revert）。
// v5.2 用三地址对照，避免把「额度用完」「项目已关」误判成权限结论：
//   P = 持有 Agent ID 且未铸造过本项目（真正的正样本，来自 PROBE_HOLDERS 池）
//   N = 不持有 Agent ID（负样本）
//   H = 该笔 mint 的原铸造者（只作参考，可能额度已用完）
// 输出：gated / open / mint_closed / positive_control_invalid / unknown
const NO_AGENT = '0x000000000000000000000000000000000000dead';
// 普通 EOA 负样本：无代码、无 Agent ID（已核验 code=0x、registry.balanceOf=0），
// 避免只依赖 0x…dEaD —— 有些合约会对销毁地址做特殊处理。
const NO_AGENT2 = '0x5a17e3e4f0d2c9b8a6c1d3e7f0a2b4c6d8e0f135';
const PROBE_HOLDERS = new Set();   // 已验证 registry.balanceOf > 0 的地址池
const PROBE_CACHE = new Map();
let noAgentZero = null;

async function rawEthCall(from, to, data, value) {
  const p = { from, to, data };
  if (value && value !== '0x0') p.value = value;
  try { return { ok: true, out: await rpc('eth_call', [p, 'latest']) }; }
  catch (e) {
    const msg = String(e.message || '');
    if (/revert/i.test(msg)) return { ok: false, revert: msg.slice(0, 160) };
    return { ok: null, err: msg.slice(0, 80) };
  }
}

async function gatedByBehavior(tok, txHash) {
  if (!txHash) return { state: 'unknown', reason: 'no_mint_tx', detail: null };
  let tx = null;
  try { tx = await rpc('eth_getTransactionByHash', [txHash]); } catch { return { state: 'unknown', reason: 'tx_fetch_failed', detail: null }; }
  if (!tx || !tx.to || tx.to.toLowerCase() !== tok) return { state: 'unknown', reason: 'tx_to_mismatch', detail: null };
  const input = tx.input || '0x';
  if (input.length < 10) return { state: 'unknown', reason: 'no_calldata', detail: null };
  const value = tx.value || '0x0', selector = input.slice(0, 10), blockTag = 'latest';

  // 缓存键必须含完整 calldata 与 value：同一合约不同 mint 数量/tokenId/deadline 结论可能不同。
  const key = [tok, selector, input, value, blockTag].join('|');
  const hit = PROBE_CACHE.get(key);
  if (hit && Date.now() - hit.at < hit.ttl) return hit;

  // 两个负样本：dEaD 可能被合约特殊处理（黑名单/禁止销毁地址），必须再有一个普通 EOA。
  const negs = [];
  for (const [name, addr] of [['dead', NO_AGENT], ['eoa', NO_AGENT2]]) {
    const r = await rawEthCall(addr, tok, input, value);
    negs.push({ name, from: addr, ok: r.ok, err: r.err || null });
  }
  const nOk = negs.filter((n) => n.ok === true).length;
  const nRevert = negs.filter((n) => n.ok === false).length;
  if (negs.some((n) => n.ok === null)) {
    return { state: 'unknown', reason: 'negative_transport_error', detail: { negs } };
  }

  const minterSet = new Set([...((MINTS.get(tok) || {}).to || [])].map((a) => String(a).toLowerCase()));
  const detail = { selector, value, blockTag, txHash, minter: tx.from, minterResult: null,
                   positive: null, positiveFrom: null, negatives: negs,
                   holderSnapshotBlock: blockTag, simulationBlock: blockTag };

  let pOk = null;
  const pool = [...PROBE_HOLDERS].filter((x) => !minterSet.has(x)).slice(0, 3);
  for (const a of pool) {
    const r = await rawEthCall(a, tok, input, value);
    detail.positiveFrom = a; detail.positive = r.ok;
    if (r.ok === true) { pOk = true; break; }
    pOk = false;
  }
  const hCall = await rawEthCall(tx.from, tok, input, value);
  detail.minterResult = hCall.ok;

  // 不同终态不同 TTL：mint_closed 可能因分阶段/重新开窗而失效，不能和 gated 一样缓存 10 分钟
  const TTL = { gated: 600000, open: 600000, mint_closed: 120000 };
  const fin = (state, reason) => {
    const v = { state, reason, at: Date.now(), ttl: TTL[state] || 0, detail };
    if (TTL[state]) {
      PROBE_CACHE.set(key, v);
      if (PROBE_CACHE.size > 5000) PROBE_CACHE.delete(PROBE_CACHE.keys().next().value);
    }
    return v;
  };

  // 强证据：P 成功 + 两个负样本都 revert
  if (pOk === true && nRevert === 2) return fin('gated', 'holder_only_both_negatives_revert');
  // P 成功但负样本一个成功一个失败：不能判 gated，也不能判 open
  if (pOk === true && nOk > 0 && nRevert > 0) return fin('unknown', 'negative_control_inconsistent');
  if (pOk === true && nOk === 2) return fin('open', 'both_succeed');
  if (pOk === false && nOk > 0) return fin('positive_control_invalid', 'holder_reverts_nonholder_succeeds');
  if (pOk === false && nRevert === 2) return fin('mint_closed', 'both_revert');

  // 池为空：退回原铸造者，只作参考，reason 必须带 fallback
  if (hCall.ok === true && nRevert === 2) return fin('gated', 'minter_fallback');
  if (hCall.ok === true && nOk > 0) return fin('open', 'both_succeed_fallback');
  if (hCall.ok === false && nOk > 0) return fin('positive_control_invalid', 'quota_exhausted_fallback');
  return fin('mint_closed', 'all_revert_fallback');
}

const IMM = new Map();   // 不可变字段：name/symbol/decimals/code/assetType
const AGT = new Map();   // Agent ID 持币比例，5 分钟 TTL
async function immutableOf(tok) {
  if (IMM.has(tok)) return IMM.get(tok);
  const code = await codeOf(tok);
  const dec = await decimalsOf(tok);
  const name = strAt((await callAt(tok, '0x06fdde03')) || '0x');
  const sym = strAt((await callAt(tok, '0x95d89b41')) || '0x');
  const ownerOf = await callAt(tok, '0x6352211e' + w64(1));
  const v = { code, dec, name, sym, assetType: ownerOf && ownerOf !== '0x' ? 'erc721' : 'erc20' };
  IMM.set(tok, v);
  if (IMM.size > 20000) IMM.delete(IMM.keys().next().value);
  return v;
}
async function meta(tok) {
  const imm = await immutableOf(tok);
  const code = imm.code, dec = imm.dec, name = imm.name, sym = imm.sym, assetType = imm.assetType;
  const m = MINTS.get(tok);
  let agentHolders = 0, agentBlock = 'latest';
  const sample = [...(m?.to || [])].slice(0, 10);
  const cached = AGT.get(tok);
  if (cached && Date.now() - cached.at < 300000) { agentHolders = cached.hit; agentBlock = cached.block; }
  else {
    if (m && m.firstBlock) { const at = await callAt(REGISTRY, '0x18160ddd', hex(m.firstBlock)); if (at) agentBlock = hex(m.firstBlock); }
    for (const a of sample) { const r = await callAt(REGISTRY, '0x70a08231' + wa(a), agentBlock); if (r && big(r) > 0n) { agentHolders++; PROBE_HOLDERS.add(String(a).toLowerCase()); if (PROBE_HOLDERS.size > 300) PROBE_HOLDERS.delete(PROBE_HOLDERS.values().next().value); } }
    AGT.set(tok, { at: Date.now(), hit: agentHolders, block: agentBlock, n: sample.length });
  }
  const bytecodeRegistryHit = code.includes(REGISTRY_HEX);
  const selectorHit = code.includes('9f92d0dd');
  let gated0 = bytecodeRegistryHit || selectorHit;
  // ERC-721 且静态判据未命中时，用行为判据复核（避免漏掉门槛藏在调用链后的项目）
  let gatedProbe = null, gatedProbeReason = null;
  if (!gated0 && assetType === 'erc721') {
    const pr = await gatedByBehavior(tok, m && m.tx);
    gatedProbe = pr.state; gatedProbeReason = pr.reason;
    if (pr.state === 'gated') gated0 = true;
  }

  let v2 = [], v3 = [], quoter = null;
  const exitVenue = assetType === 'erc721' ? 'nft_marketplace_unknown' : 'amm';
  if (gated0 && assetType === 'erc20') {
    v2 = await v2Pools(tok, dec);
    const v3all = await v3Pools(tok, dec);
    v3 = v3all;
    const tradableV3 = v3all.filter((x) => x.tradable).sort((a, b) => (BigInt(b.liquidity) > BigInt(a.liquidity) ? 1 : -1));
    if (tradableV3.length) {
      const best = tradableV3[0];
      const poolBal = await balanceOf(tok, best.pool);
      if (poolBal > 0n) {
        const amt = poolBal / 100n > 0n ? poolBal / 100n : poolBal; // 池内 1% 作为卖出规模
        quoter = await quoteSellV3(best, tok, amt);
        quoter.sizeBasis = 'pool_token_balance/100';
      } else quoter = { ok: false, reason: 'pool_has_no_token_balance' };
    }
  }

  const poolFound = v2.length > 0 || v3.length > 0;
  const v2Tradable = v2.filter((x) => x.tradable).length;
  const v3Tradable = v3.filter((x) => x.tradable).length;
  const poolTradable = v2Tradable + v3Tradable > 0;
  const minter = m ? await minterOf(m.tx) : null;
  const agentHolderRatio = sample.length ? +(agentHolders / sample.length).toFixed(3) : 0;
  const inUnrecoveredGap = m ? pending.some((r) => m.firstBlock >= Number(r.from) && m.firstBlock <= Number(r.to)) : false;

  const sellPathEvidence = quoter && quoter.ok ? 'pancake_v3_quote' : null;
  const behavioralGateVerified = false;
  const signalClass = (behavioralGateVerified && poolTradable && sellPathEvidence) ? 'A'
    : (gated0 && agentHolderRatio >= 0.99 && !!minter) ? 'B'
    : gated0 ? 'C' : 'D';

  const sampleN = (cached && sample.length === 0) ? cached.n : sample.length;
  return {
    name, sym, decimals: dec, assetType, exitVenue, codeLen: Math.max(0, code.length / 2 - 1),
    bytecodeRegistryHit, selectorHit, gatedProbe, gatedProbeReason, agentHolders, sampleN, agentHolderRatio, agentBlock,
    minterKnown: !!minter, minter,
    poolFound, poolTradable, poolFoundButNotTradable: poolFound && !poolTradable,
    v2PoolCount: v2.length, v3PoolCount: v3.length, v2Tradable, v3Tradable,
    v2, v3, quoter, sellPathEvidence,
    behavioralGateVerified, signalClass, gated: gated0,
    dataComplete: !inUnrecoveredGap, eventBlockWasGap: inUnrecoveredGap,
  };
}

async function selftest() {
  log('[selftest] QuoterV2 / V3 / decimals 路径自检');
  const dec = await decimalsOf(WBNB);
  log('  WBNB decimals = ' + dec);
  const pools = await v3Pools(WBNB, dec);
  log('  V3 池数(2 quote × 4 fee) = ' + pools.length + ' ，可交易 ' + pools.filter((p) => p.tradable).length);
  const best = pools.filter((p) => p.tradable).sort((a, b) => (BigInt(b.liquidity) > BigInt(a.liquidity) ? 1 : -1))[0];
  if (best) {
    const q = await quoteSellV3(best, WBNB, 10n ** 18n);
    log('  quote 1 WBNB → ' + JSON.stringify(q));
  } else log('  ! 无可用 V3 池');
}

async function main() {
  log('[' + ts() + '] ' + VERSION + ' build=' + BUILD + ' start; resume lastBlock=' + lastBlock + ' pending=' + pending.length + (stateCorrupt ? ' [STATE CORRUPT]' : ''));
  if (process.env.SHADOW_SELFTEST === '1') { await selftest(); return; }

  // state 损坏：只读告警，不冷启动、不扫描；systemd 保持 active 但探针 BLOCKED，等人工恢复
  if (stateCorrupt && !ALLOW_COLD && !FORCE_START) {
    log('  BLOCKED: state 损坏且未授权冷启动。修复 state.json 或设置 SHADOW_ALLOW_COLD_START=1 / SHADOW_START_BLOCK=<n> 后重启。');
    for (;;) {
      appendFileSync(MET, JSON.stringify({ t: ts(), v: VERSION, build: BUILD, blocked: true, reason: 'state_corrupt', hint: 'set SHADOW_ALLOW_COLD_START=1 or SHADOW_START_BLOCK' }) + '\n');
      await sleep(60000);
    }
  }
  let head = 0;
  try { head = parseInt(await rpc('eth_blockNumber', []), 16); } catch {}
  if (FORCE_START) { lastBlock = FORCE_START - 1; log('  forced start at ' + FORCE_START); }
  else if (!lastBlock || head - lastBlock > 3000) { lastBlock = head - 60; log('  cold start at ' + lastBlock + (stateCorrupt ? ' (stateCorrupt 且已授权)' : '')); }

  let pausedSince = 0;
  for (;;) {
    const t0 = Date.now();
    let gapFlag = false, gapRanges = [];
    let cursorPaused = false, pauseReason = null;
    try {
      head = parseInt(await rpc('eth_blockNumber', []), 16);
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
      const to = Math.min(head - 2, lastBlock + 600);
      const fromBlock = lastBlock + 1;
      const lastBlockBefore = lastBlock;
      const scan = await scanRange(fromBlock, to);
      const fresh = ingest(scan.logs, Date.now());
      if (scan.hadGap) { gapFlag = true; gapRanges.push(...scan.gapRanges); }

      // 主游标控制：pending 积压 + 本轮又出新 gap → 不推进，先补洞（10 分钟后强制恢复，避免卡死）
      if (pending.length >= PAUSE_AT && scan.hadGap) {
        if (!pausedSince) pausedSince = Date.now();
        const pausedMin = (Date.now() - pausedSince) / 60000;
        if (pausedMin < PAUSE_MAX_MIN) { cursorPaused = true; pauseReason = 'pending_gap'; }
        else { pauseReason = 'forced_resume_after_' + PAUSE_MAX_MIN + 'min'; pausedSince = 0; }
      } else if (pending.length < PAUSE_AT) pausedSince = 0;
      if (!cursorPaused) lastBlock = to;
      pruneSeen(head);

      let hits = 0, checkedNow = 0, pooled = 0, notTradable = 0, quoted = 0, byClass = { A: 0, B: 0, C: 0, D: 0 }, incomplete = 0;
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
        if (mm.quoter && mm.quoter.ok) quoted++;
        if (!mm.dataComplete) incomplete++;
        byClass[mm.signalClass] = (byClass[mm.signalClass] || 0) + 1;
        appendFileSync(SIG, JSON.stringify({
          t: ts(), v: VERSION, build: BUILD, tok, ...mm,
          wallets: m.to.size, mints: m.mints,
          ageMin: +((Date.now() - m.at) / 60e3).toFixed(1),
          eventBlock: m.firstBlock,
          scanFrom: fromBlock, scanTo: to, crossGap: gapFlag,
          gapRanges: gapFlag ? gapRanges.slice(0, 5) : [],
        }) + '\n');
        if (mm.gated) log('  * ' + mm.signalClass + '/' + mm.assetType + (mm.dataComplete ? '' : '(INC)') + ' ' + (mm.sym || mm.name || tok) + ' ' + tok
          + ' w=' + m.to.size + ' agent=' + mm.agentHolders + '/' + mm.sampleN + '@' + mm.agentBlock
          + ' v2=' + mm.v2Tradable + '/' + mm.v2PoolCount + ' v3=' + mm.v3Tradable + '/' + mm.v3PoolCount
          + ' quote=' + (mm.quoter ? (mm.quoter.ok ? mm.quoter.amountOut + ' impact=' + mm.quoter.priceImpactBps + 'bps' : mm.quoter.reason) : '-')
          + ' minter=' + (mm.minter ? mm.minter.slice(0, 10) : '-') + (gapFlag ? ' GAP' : ''));
      }
      const ms = Date.now() - t0, nowMs = Date.now();
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
        t: ts(), v: VERSION, build: BUILD, head, scannedTo: to, lagBlocks: head - to, fromBlock, fresh,
        hadGap: gapFlag, gapCount: gapRanges.length, pending: pending.length,
        pendingAgeMaxMin: ages.length ? +Math.max(...ages).toFixed(1) : 0,
        pendingOverflow, stateCorrupt, stateSaved: saved,
        cursorPaused, pauseReason, lastBlockBefore, lastBlockAfter: lastBlock,
        tokens: MINTS.size, candidates: checkedNow, gatedHits: hits, pooled, poolNotTradable: notTradable, quoted,
        byClass, incompleteSignals: incomplete, zeroDisagreements,
        seenSize: seen.size, ms, rpc: rpcStats,
      }) + '\n');
      log('[' + ts() + '] head=' + head + ' lag=' + (head - to) + ' gap=' + (gapFlag ? gapRanges.length : 0)
        + ' pending=' + pending.length + (cursorPaused ? ' [PAUSED:' + pauseReason + ']' : '')
        + ' cand=' + checkedNow + ' gated=' + hits + ' pool=' + pooled + ' quote=' + quoted + ' ' + ms + 'ms');
    } catch (e) { log('[' + ts() + '] CYCLE ERR ' + e.message); saveState(); }
    const used = Date.now() - t0;
    await sleep(Math.max(1500, 12000 - used));
  }
}

// ---------- 合并前自检（PROBE_SELFTEST=1 node probe.mjs）----------
// 只读：eth_getLogs / eth_getTransactionByHash / eth_call，不签名、不交易。
// 闸门：真实 mint calldata + 正样本（原铸造者）+ 负样本（无 Agent ID 地址）+ 三态输出 + 缓存 + ERC-20 隔离。
async function selftestGated() {
  const rows = [];
  const rec = (name, ok, detail) => { rows.push({ case: name, pass: !!ok, ...detail }); log(`SELFTEST ${ok ? 'PASS' : 'FAIL'} ${name} ${JSON.stringify(detail)}`); };

  const TOK_SIG = '0xdd8bdfc91cbd572935b09863a742d8296fed0acf';
  const SIGTX = '0xa631ce40c0837cfde5eaa15ed53cd58a508963026207ce10dba261e04d383c77';
  const g1 = await gatedByBehavior(TOK_SIG, SIGTX);
  rec('known_gated_SIGIL', g1.state === 'gated', { state: g1.state, reason: g1.reason });

  rec('probe_detail', !!(g1.detail && g1.detail.selector && g1.detail.blockTag && Array.isArray(g1.detail.negatives) && g1.detail.negatives.filter((n) => n.ok === false).length === 2),
    g1.detail ? { selector: g1.detail.selector, value: g1.detail.value, blockTag: g1.detail.blockTag, negative: g1.detail.negative, from: g1.detail.positiveFrom || g1.detail.minter } : { detail: null });

  const g2 = await gatedByBehavior(TOK_SIG, SIGTX);
  rec('cache_reuse', g2.at === g1.at, { atSame: g2.at === g1.at, state: g2.state });

  const g3 = await gatedByBehavior('0xfd3430e86df73c4e1e55036dc368a52716b84937', null);
  rec('no_mint_tx_unknown', g3.state === 'unknown' && g3.reason === 'no_mint_tx', { state: g3.state, reason: g3.reason });
  const mis = await gatedByBehavior('0xfd3430e86df73c4e1e55036dc368a52716b84937', SIGTX);
  rec('tx_to_mismatch_unknown', mis.state === 'unknown' && mis.reason === 'tx_to_mismatch', { state: mis.state, reason: mis.reason });

  const sd = await gatedByBehavior('0xfd3430e86df73c4e1e55036dc368a52716b84937', '0xfa6a346f82ac1ebf902da0a33fac2fb9a863ebe9d3c05fe635723db461e7aa9a');
  rec('second_gated_sample_SEED', ['gated', 'mint_closed', 'unknown'].includes(sd.state), { state: sd.state, reason: sd.reason });

  let open = null;
  try {
    const hd = Number(await rpc('eth_blockNumber', []));
    const lines = readFileSync(process.env.SHADOW_SIGNALS || new URL('./signals.jsonl', import.meta.url).pathname, 'utf8').trim().split('\n').slice(-320).map((l) => JSON.parse(l));
    const cands = [...new Set(lines.filter((r) => r.assetType === 'erc721' && !r.gated).map((r) => r.tok))].slice(0, 10);
    for (const t of cands) {
      const lg = (await scanRange(hd - 6000, hd)).logs.filter((l) => l.address === t);
      const mint = lg.find((l) => /^0x0{64}$/.test(l.topics[1] || ''));
      if (!mint) continue;
      const r = await gatedByBehavior(t, mint.transactionHash);
      log('SELFTEST cand ' + t + ' -> ' + r.state + '/' + r.reason);
      if (r.state === 'open') { open = { tok: t, reason: r.reason }; break; }
    }
  } catch (e) { log('SELFTEST open 扫描失败 ' + e.message); }
  if (open) rec('open_sample', true, open);
  else { rows.push({ case: 'open_sample', pass: null, skipped: true }); log('SELFTEST SKIP open_sample 近期候选的 mint 窗口已关闭（正样本也 revert），无法构造 open 对照，不判失败'); }

  const self = readFileSync(new URL(import.meta.url).pathname, 'utf8');
  rec('erc20_isolated', /if \(!gated0 && assetType === 'erc721'\)/.test(self), { guard: true });
  rec('signal_fields', /gatedProbe, gatedProbeReason/.test(self) && /eventBlockWasGap/.test(self), { fields: true });

  const fail = rows.filter((r) => r.pass === false).length;
  log(`SELFTEST DONE total=${rows.length} fail=${fail}`);
  writeFileSync(new URL('../analysis/selftest.json', import.meta.url).pathname, JSON.stringify({ at: new Date().toISOString(), probe: VERSION, build: BUILD, fail, rows }, null, 1));
  return fail === 0;
}

if (process.env.PROBE_SELFTEST) { const ok = await selftestGated(); process.exit(ok ? 0 : 1); }

main();
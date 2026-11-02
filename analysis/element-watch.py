#!/usr/bin/env python3
"""Element(BSC) NFT 成交取证 v3 —— 只读，带可证明的窗口覆盖

相对 v2 的改动（2026-11-01 复核要求）：
  1. 显式窗口：--from-block/--to-block 或 --from-time/--to-time（自动解析成块高）；
  2. 覆盖证明：requestedRanges / scannedRanges / skippedRanges / retryCount /
     rpcSources / dataComplete，全部写进 coverage 段；
  3. 失败区间有限终止：分片减到 25 块仍失败 → 记 status=skipped + reason + attempts，
     前进到下一段，绝不死循环、也绝不把失败当「没有成交」；
  4. 去重：按 (txHash, logIndex) 去重，边界块不会重复计数；
  5. decoder 版本落盘，避免不同解码器结果被直接比较；
  6. 退出码：0=完整，3=有 skipped（此时 dataComplete=false）。

事件：Element Fill
  0x9c248aa1a265aa616f707b979d57f4529bb63a4fc34dc7fc61fdddc18410f74e
  w1 卖家 / w2 买家 / w4 支付币（0xeeee…=BNB）/ w5 价格 / w7 集合 / w8 tokenId
  w10 结算合约 / w11 手续费；12 或 14 words 两种布局按 len(words) 分流。

不做：签名、approve、mint、sell、baw、任何写操作。
"""
import calendar, json, os, sys, time, urllib.request
from collections import Counter, defaultdict

RPCS = ['https://rpc.48.club', 'https://bsc.blockrazor.xyz', 'https://1rpc.io/bnb']
FILL = '0x9c248aa1a265aa616f707b979d57f4529bb63a4fc34dc7fc61fdddc18410f74e'
NATIVE = 0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee
BLOCKS_PER_HOUR = 8000
DECODER_VERSION = 'element-fill-v3'
STEP, MINSTEP, MAXRETRY = 500, 25, 4


class Rpc:
    def __init__(self):
        self.i = 0; self.calls = 0; self.last = 0
        self.err = Counter(); self.ok = Counter(); self.used = Counter()

    def pace(self):
        dt = time.time() - self.last
        if dt < 0.17:
            time.sleep(0.17 - dt)
        self.last = time.time()

    def call(self, method, params, tries=3):
        for k in range(tries):
            url = RPCS[(self.i + k) % len(RPCS)]
            self.pace(); self.calls += 1; self.used[url] += 1
            body = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params}).encode()
            try:
                req = urllib.request.Request(url, body, {'content-type': 'application/json',
                                                         'user-agent': 'element-watch/3.0'})
                r = json.load(urllib.request.urlopen(req, timeout=25))
                if 'error' in r:
                    self.err[r['error'].get('message', '')[:40]] += 1
                    continue
                self.ok[url] += 1
                return r.get('result')
            except Exception as e:
                self.err[str(e)[:40]] += 1
        self.i = (self.i + 1) % len(RPCS)
        return None


R = Rpc()
u = lambda h: int(h, 16) if h and h != '0x' else 0


def block_ts(b, tries=4):
    for _ in range(tries):
        r = R.call('eth_getBlockByNumber', [hex(b), False])
        if r:
            return int(r['timestamp'], 16)
    return None


def block_at_time(target_ts):
    """把时间戳解析成块高：先按 20k 步长向后退到目标时间之前，再二分收敛。

    不能直接从 head-80天 二分：免费 RPC 对过老的块返回 null（实测 2,000,000 块前
    就取不到 timestamp），会让整个解析失败或退化。这里只触碰最近几十万块。
    """
    head = u(R.call('eth_blockNumber', []))
    if not head:
        return None
    hi, lo, step = head, None, 20000
    for _ in range(40):
        b = max(0, hi - step)
        ts = block_ts(b)
        if ts is None:
            return None
        if ts <= target_ts:
            lo = b
            break
        hi = b
        if hi == 0:
            lo = 0
            break
        step = min(step * 2, 400000)
    if lo is None:
        return None
    while lo < hi:
        mid = (lo + hi) // 2
        ts = block_ts(mid)
        if ts is None:
            return None
        if ts < target_ts:
            lo = mid + 1
        else:
            hi = mid
    return lo


def scan(frm, to, log_path=None):
    """扫描 [frm,to]，返回 (logs, coverage)。失败区间有限重试后记为 skipped 并前进。

    log_path 非空时逐 chunk 追加一行 JSON（fromBlock/toBlock/blocks/fills/status/retries），
    供 element-archive.py 生成可复跑的 chunk 清单。
    """
    out, cur, step, fails = [], frm, STEP, 0
    lg = open(log_path, 'a') if log_path else None

    def _chunk(rec):
        if lg:
            lg.write(json.dumps(rec, ensure_ascii=False) + '\n'); lg.flush()
    cov = {'requestedRanges': (to - frm) // STEP + 1, 'scannedRanges': 0,
           'retryCount': 0, 'skippedBlocks': 0, 'coveredBlocks': 0,
           'skippedRanges': []}
    while cur <= to:
        end = min(to, cur + step - 1)
        r = R.call('eth_getLogs', [{'fromBlock': hex(cur), 'toBlock': hex(end), 'topics': [FILL]}])
        if r is None:
            cov['retryCount'] += 1; fails += 1
            if step > MINSTEP:
                step = max(MINSTEP, step // 2); continue
            if fails >= MAXRETRY:
                cov['skippedRanges'].append({'fromBlock': cur, 'toBlock': end, 'status': 'skipped',
                                             'reason': 'all_rpc_failed', 'attempts': fails})
                cov['skippedBlocks'] += end - cur + 1
                _chunk({'fromBlock': cur, 'toBlock': end, 'blocks': end - cur + 1,
                        'fills': 0, 'status': 'skipped', 'reason': 'all_rpc_failed',
                        'retries': fails})
                cur = end + 1; fails = 0; continue
            time.sleep(0.4 * fails); continue
        out.extend(r)
        _chunk({'fromBlock': cur, 'toBlock': end, 'blocks': end - cur + 1,
                'fills': len(r), 'status': 'ok', 'stepUsed': step})
        cov['scannedRanges'] += 1; cov['coveredBlocks'] += end - cur + 1
        cur = end + 1; fails = 0
        if step < STEP: step = min(STEP, step * 2)
    cov['dataComplete'] = not cov['skippedRanges']
    cov['blocksRequested'] = to - frm + 1
    cov['coverageRatio'] = round((cov['coveredBlocks'] + cov['skippedBlocks']) / max(1, cov['blocksRequested']), 4)
    cov['rpcSources'] = sum(1 for v in R.ok.values() if v > 0)
    cov['rpcOk'] = dict(R.ok)
    return out, cov


def decode(l):
    d = l['data'][2:]
    w = [int(d[i:i + 64], 16) for i in range(0, len(d), 64)]
    if len(w) < 12:
        return None
    g = lambda i: w[i] if i < len(w) else 0
    return {
        'block': u(l['blockNumber']), 'tx': l['transactionHash'],
        'logIndex': u(l.get('logIndex', '0x0')), 'words': len(w),
        'seller': '0x%040x' % g(1), 'buyer': '0x%040x' % g(2),
        'tokenId': g(8), 'payToken': '0x%040x' % g(4), 'priceWei': g(5),
        'collection': '0x%040x' % g(7), 'market': '0x%040x' % g(10), 'feeWei': g(11),
    }


def arg(name, default=None):
    a = sys.argv
    return a[a.index(name) + 1] if name in a and a.index(name) + 1 < len(a) else default


def main():
    from_ts = arg('--from-time'); to_ts = arg('--to-time')
    frm, to = arg('--from-block'), arg('--to-block')
    outpath = arg('--output', os.path.expanduser('~/agentid-sniper/analysis/element-fills.json'))
    hours = float(arg('--hours', 12))

    if from_ts:
        frm = block_at_time(calendar.timegm(time.strptime(from_ts, '%Y-%m-%dT%H:%M:%SZ')))
        if frm is None:
            print('无法把 --from-time 解析成块高，终止（不猜窗口）'); sys.exit(2)
    if to_ts:
        to = block_at_time(calendar.timegm(time.strptime(to_ts, '%Y-%m-%dT%H:%M:%SZ')))
        if to is None:
            print('无法把 --to-time 解析成块高，终止（不猜窗口）'); sys.exit(2)
    head = u(R.call('eth_blockNumber', []))
    def _i(x):
        return int(x, 0) if isinstance(x, str) else int(x)
    to = _i(to) if to is not None else head
    frm = _i(frm) if frm is not None else to - int(hours * BLOCKS_PER_HOUR)
    if frm > to:
        print('窗口非法：fromBlock %s > toBlock %s' % (frm, to)); sys.exit(2)

    logs, cov = scan(frm, to, arg('--chunk-log', '') or None)

    # 去重：同 (tx, logIndex) 只算一次
    seen, uniq = set(), []
    for l in logs:
        k = (l['transactionHash'], l.get('logIndex', '0x0'))
        if k in seen:
            continue
        seen.add(k); uniq.append(l)

    fills = [x for x in (decode(l) for l in uniq) if x]
    native = [x for x in fills if int(x['payToken'], 16) == NATIVE]
    coll = defaultdict(lambda: {'sales': 0, 'vol': 0, 'min': None, 'max': None,
                                'sellers': set(), 'buyers': set(), 'last': 0})
    for x in native:
        c = coll[x['collection']]
        c['sales'] += 1; c['vol'] += x['priceWei']
        p = x['priceWei']
        c['min'] = p if c['min'] is None else min(c['min'], p)
        c['max'] = p if c['max'] is None else max(c['max'], p)
        c['sellers'].add(x['seller']); c['buyers'].add(x['buyer']); c['last'] = max(c['last'], x['block'])

    tracked = {}
    sig = os.path.expanduser('~/agentid-sniper/shadow/signals.jsonl')
    if os.path.exists(sig):
        for line in open(sig):
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get('gated') and r.get('assetType') == 'erc721':
                t = r['tok'].lower()
                e = tracked.setdefault(t, {'sym': r.get('sym'), 'mints': 0, 'lastBlock': 0})
                e['mints'] = max(e['mints'], r.get('mints') or 0)
                e['lastBlock'] = max(e['lastBlock'], r.get('eventBlock') or 0)

    rows = []
    for t, m in tracked.items():
        c = coll.get(t)
        rows.append({'collection': t, 'sym': m['sym'], 'mints': m['mints'],
                     'sales': c['sales'] if c else 0,
                     'volumeBnb': round(c['vol'] / 1e18, 6) if c else 0,
                     'uniqueBuyers': len(c['buyers']) if c else 0,
                     'uniqueSellers': len(c['sellers']) if c else 0,
                     'sellThrough': round(c['sales'] / m['mints'], 4) if c and m['mints'] else 0,
                     'lastSaleBlock': c['last'] if c else None})
    rows.sort(key=lambda r: -r['sales'])

    t0, t1 = block_ts(frm), block_ts(to)
    iso = lambda x: time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(x)) if x else None

    agg = {
        'generated': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'window': {'fromBlock': frm, 'toBlock': to, 'blocks': to - frm + 1,
                   'fromTimestamp': iso(t0), 'toTimestamp': iso(t1),
                   'windowMinutes': round((t1 - t0) / 60, 1) if t0 and t1 else None},
        'coverage': cov,
        'decoder': {'elementTopic': FILL, 'version': DECODER_VERSION,
                    'layouts': ['12words', '14words'], 'dedupe': 'txHash+logIndex'},
        'fills_total': len(fills), 'fills_native_bnb': len(native),
        'fills_erc20_pay': len(fills) - len(native),
        'collections_traded': len(coll),
        'volumeBnb': round(sum(c['vol'] for c in coll.values()) / 1e18, 6),
        'tracked_collections': len(tracked),
        'tracked_with_sales': sum(1 for r in rows if r['sales'] > 0),
        'tracked_sales': sum(r['sales'] for r in rows),
        'tracked_volumeBnb': round(sum(r['volumeBnb'] for r in rows), 6),
        'rpcCalls': R.calls, 'errors': dict(R.err.most_common(5)),
    }
    out = {'agg': agg, 'tracked': rows[:120],
           'top_collections': [{'collection': k, 'sales': v['sales'],
                                'volumeBnb': round(v['vol'] / 1e18, 6),
                                'buyers': len(v['buyers']), 'sellers': len(v['sellers'])}
                               for k, v in sorted(coll.items(), key=lambda kv: -kv[1]['vol'])[:10]]}
    tmp = outpath + '.tmp'
    json.dump(out, open(tmp, 'w'), ensure_ascii=False, indent=1)
    os.replace(tmp, outpath)

    print('窗口 %s..%s  %s → %s（%s 分钟）' % (frm, to, iso(t0), iso(t1), agg['window']['windowMinutes']))
    print('覆盖: requested=%s scanned=%s skipped=%s retry=%s dataComplete=%s ratio=%s' % (
        cov['requestedRanges'], cov['scannedRanges'], len(cov['skippedRanges']),
        cov['retryCount'], cov['dataComplete'], cov['coverageRatio']))
    print('成交: fills=%s native=%s collections=%s vol=%s BNB  tracked=%s withSales=%s sales=%s' % (
        agg['fills_total'], agg['fills_native_bnb'], agg['collections_traded'], agg['volumeBnb'],
        agg['tracked_collections'], agg['tracked_with_sales'], agg['tracked_sales']))
    print('已写 %s' % outpath)
    if not cov['dataComplete']:
        print('!! 有 %d 个区间被跳过，dataComplete=false，不可用于最终基线' % len(cov['skippedRanges']))
        sys.exit(3)


if __name__ == '__main__':
    main()
#!/usr/bin/env python3
"""cohort.py v2 —— mint 批次级成交核算（只读，不签名/不交易）

修正两个口径错误：
  1) 「重试后成功」不再算 gap；只有真正跳过的区块区间才记 skipped，dataComplete 由 skipped 决定
  2) 售出率不再用「同期成交量 / 同期 mint 量」，而是按 tokenId 回溯 mint 区块，算真实 time-to-sale

每次必须输出：fromBlock/toBlock/两端 timestamp/windowMinutes/dataComplete/skipped/retries
判据：只有 Element Fill 事件算成交；ERC-721 的 EOA↔EOA Transfer 一律不算。
"""
import json, os, sys, time, urllib.request
from collections import defaultdict, Counter

RPCS = ['https://bsc-rpc.publicnode.com', 'https://rpc.48.club']
FILL = '0x9c248aa1a265aa616f707b979d57f4529bb63a4fc34dc7fc61fdddc18410f74e'
XFER = '0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef'
ZERO = '0x' + '0' * 64
NATIVE = '0x' + 'e' * 40
BLOCKS_PER_HOUR = 8000
CHUNK = 5000
MIN_CHUNK = 250


class Rpc:
    def __init__(self):
        self.i = 0; self.calls = 0; self.last = 0; self.retries = 0; self.err = Counter()

    def pace(self):
        d = time.time() - self.last
        if d < 0.18: time.sleep(0.18 - d)
        self.last = time.time()

    def call(self, method, params, tries=4):
        for k in range(tries):
            url = RPCS[(self.i + k) % len(RPCS)]
            self.pace(); self.calls += 1
            body = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params}).encode()
            try:
                rq = urllib.request.Request(url, body, {'content-type': 'application/json', 'user-agent': 'cohort/2.0'})
                r = json.load(urllib.request.urlopen(rq, timeout=22))
                if 'error' in r:
                    self.err[r['error'].get('message', '')[:26]] += 1
                    continue
                return r.get('result')
            except Exception as e:
                self.err[str(e)[:26]] += 1
        self.retries += 1
        return None


R = Rpc()
def u(h): return int(h, 16) if h and h != '0x' else 0


def scan(frm, to, address=None, topics=None, label=''):
    """覆盖式扫描：失败就减半重试，只有降到 MIN_CHUNK 仍失败才跳过并计入 skipped。"""
    out, skipped, cur, step = [], [], frm, CHUNK
    while cur <= to:
        end = min(to, cur + step - 1)
        p = {'fromBlock': hex(cur), 'toBlock': hex(end)}
        if address: p['address'] = address
        if topics: p['topics'] = topics
        r = R.call('eth_getLogs', [p])
        if r is None:
            if step > MIN_CHUNK:
                step = max(MIN_CHUNK, step // 2); continue
            skipped.append([cur, end]); cur = end + 1; continue
        out.extend(r); cur = end + 1; step = CHUNK
    return out, skipped


def decode_fill(l):
    d = l['data'][2:]
    w = [int(d[i:i + 64], 16) for i in range(0, len(d), 64)]
    if len(w) < 12: return None
    g = lambda i: w[i] if i < len(w) else 0
    return {'block': u(l['blockNumber']), 'tx': l['transactionHash'],
            'seller': '0x%040x' % g(1), 'buyer': '0x%040x' % g(2),
            'payToken': '0x%040x' % g(4), 'priceWei': g(5),
            'collection': '0x%040x' % g(7), 'tokenId': str(g(8) if len(w) >= 9 else g(3)),
            'feeWei': g(11)}


def block_ts(b):
    r = R.call('eth_getBlockByNumber', [hex(b), False], tries=4)
    return u(r['timestamp']) if r and r.get('timestamp') else None


def main():
    hours = float(sys.argv[1]) if len(sys.argv) > 1 else 12.0
    offset = float(sys.argv[2]) if len(sys.argv) > 2 else 0.0
    base = os.path.expanduser('~/agentid-sniper')
    head = u(R.call('eth_blockNumber', []))
    to_blk = head - int(offset * BLOCKS_PER_HOUR)
    from_blk = to_blk - int(hours * BLOCKS_PER_HOUR)

    logs, skip_fill = scan(from_blk, to_blk, topics=[FILL])
    fills = [x for x in (decode_fill(l) for l in logs) if x]
    t0, t1 = block_ts(from_blk), block_ts(to_blk)
    window_min = round((t1 - t0) / 60, 1) if t0 and t1 else None

    tracked = {}
    for line in open(base + '/shadow/signals.jsonl'):
        if not line.strip(): continue
        r = json.loads(line)
        if r.get('gated') and r.get('assetType') == 'erc721':
            t = r['tok'].lower()
            d = tracked.setdefault(t, {'sym': r.get('sym'), 'mints': 0, 'launch': 10 ** 12})
            d['mints'] = max(d['mints'], r.get('mints') or 0)
            eb = r.get('eventBlock')
            if eb: d['launch'] = min(d['launch'], eb)

    native = [f for f in fills if f['payToken'] == NATIVE]
    bycol = defaultdict(list)
    for f in native: bycol[f['collection'].lower()].append(f)
    hits = {c: v for c, v in bycol.items() if c in tracked}
    print(f'窗口 {from_blk}..{to_blk} = {window_min} 分钟 | 全市场 fill {len(fills)} | 命中跟踪集合 {len(hits)}', flush=True)

    mint_map, skip_mint = {}, []
    for n, (col, fl) in enumerate(sorted(hits.items(), key=lambda kv: -len(kv[1])), 1):
        launch = tracked[col]['launch'] if tracked[col]['launch'] < 10 ** 12 else min(f['block'] for f in fl) - 20000
        end = min(head, launch + 80000)
        ml, sk = scan(max(0, launch), end, address=col, topics=[XFER, ZERO])
        skip_mint.extend(sk)
        for l in ml:
            if len(l['topics']) > 3 and l['topics'][1] == ZERO:
                mint_map[(col, str(u(l['topics'][3])))] = u(l['blockNumber'])
        print(f'  [{n}/{len(hits)}] {tracked[col]["sym"]}: mint logs={len(ml)} skipped={len(sk)} retries={R.retries}', flush=True)

    rows = []
    for col, fl in sorted(hits.items(), key=lambda kv: -len(kv[1])):
        prices, tts, pairs = [], [], Counter()
        buyers, sellers, tokens = set(), set(), set()
        self_trade = no_cohort = 0
        fees = []
        for f in fl:
            if f['seller'].lower() == f['buyer'].lower():
                self_trade += 1; continue
            px = f['priceWei'] / 1e18
            prices.append(px); buyers.add(f['buyer']); sellers.add(f['seller'])
            pairs[(f['seller'].lower(), f['buyer'].lower())] += 1
            tokens.add(f['tokenId'])
            if f['priceWei']: fees.append(f['feeWei'] / f['priceWei'])
            mb = mint_map.get((col, f['tokenId']))
            if mb is None: no_cohort += 1
            else: tts.append((f['block'] - mb) * 0.5 / 60)
        prices.sort()
        fee = sorted(fees)[len(fees) // 2] if fees else 0.02
        rows.append({
            'collection': col, 'sym': tracked[col]['sym'], 'mints': tracked[col]['mints'],
            'fills': len(fl), 'real_sales': len(prices), 'self_trade': self_trade,
            'unique_tokens': len(tokens), 'unique_buyers': len(buyers), 'unique_sellers': len(sellers),
            'repeat_pairs': sum(v - 1 for v in pairs.values() if v > 1),
            'max_pair_trades': max(pairs.values()) if pairs else 0,
            'top_buyer_share': round(max(Counter(f['buyer'] for f in fl).values()) / len(fl), 3),
            'volumeBnb': round(sum(prices), 6),
            'medianPriceBnb': prices[len(prices) // 2] if prices else 0,
            'minPriceBnb': prices[0] if prices else 0, 'maxPriceBnb': prices[-1] if prices else 0,
            'feeRatio': round(fee, 4),
            'netProceedsBnb': round(sum(p * (1 - fee) for p in prices), 6),
            'ttsMedianMin': round(sorted(tts)[len(tts) // 2], 1) if tts else None,
            'ttsMaxMin': round(max(tts), 1) if tts else None,
            'cohortMissing': no_cohort,
        })

    agg = {
        'generated': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'windowHours': hours, 'offsetHours': offset,
        'fromBlock': from_blk, 'toBlock': to_blk,
        'fromTimestamp': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(t0)) if t0 else None,
        'toTimestamp': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(t1)) if t1 else None,
        'windowMinutes': window_min,
        'skippedFill': skip_fill, 'skippedMint': skip_mint[:5],
        'dataComplete': len(skip_fill) == 0 and len(skip_mint) == 0,
        'retries': R.retries,
        'fillsTotal': len(fills), 'fillsNative': len(native),
        'collectionsTraded': len(bycol), 'trackedHits': len(hits),
        'trackedSales': sum(r['real_sales'] for r in rows),
        'trackedVolumeBnb': round(sum(r['volumeBnb'] for r in rows), 6),
        'rpcCalls': R.calls, 'errors': dict(R.err.most_common(3)),
    }
    tag = 'prev' if offset else 'latest'
    json.dump({'agg': agg, 'collections': rows}, open(base + f'/analysis/cohort-{tag}.json', 'w'), ensure_ascii=False, indent=1)
    print(json.dumps(agg, ensure_ascii=False, indent=1))
    print('%-8s %5s %5s %6s %5s %5s %8s %8s %8s %6s' % ('sym', 'fills', 'sales', 'tokens', 'buy', 'sell', 'volBNB', 'median$', 'tts中位m', '缺批'))
    for r in rows:
        print('%-8s %5d %5d %6d %5d %5d %8.4f %8.2f %8s %6d' % (
            r['sym'], r['fills'], r['real_sales'], r['unique_tokens'], r['unique_buyers'],
            r['unique_sellers'], r['volumeBnb'], r['medianPriceBnb'] * 700, r['ttsMedianMin'], r['cohortMissing']))


if __name__ == '__main__':
    main()
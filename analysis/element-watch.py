#!/usr/bin/env python3
"""Element(BSC) NFT 真实成交取证 v2 —— 只读

事件：Element Fill（匿名参数，14/12 words）
  0x9c248aa1a265aa616f707b979d57f4529bb63a4fc34dc7fc61fdddc18410f74e
字段布局（2026-10-31 实测校准，逐条验证过 ownerOf(1)）：
  w1 = 卖家地址      w2 = 买家地址
  w4 = 支付币（0xeeee...eeee = 原生 BNB，否则为 ERC20）
  w5 = 成交价(wei)   w7 = NFT 集合合约      w8 = tokenId（12 words 版本里 w3 不是 tokenId）
  w10 = 市场结算合约(0x67c0a1fc...)         w11 = 手续费(约 2%)
判据：只有本事件计入"成交"；EOA↔EOA 转账永不计数。

输出 analysis/element-fills.json + 终端摘要
不做：签名、approve、mint、sell、baw、任何写操作。
"""
import json, os, sys, time, urllib.request
from collections import Counter, defaultdict

RPCS = ['https://rpc.48.club', 'https://bsc.blockrazor.xyz', 'https://1rpc.io/bnb']
FILL = '0x9c248aa1a265aa616f707b979d57f4529bb63a4fc34dc7fc61fdddc18410f74e'
NATIVE = 0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee
BLOCKS_PER_HOUR = 8000

class Rpc:
    def __init__(self): self.i = 0; self.calls = 0; self.last = 0; self.err = Counter()
    def pace(self):
        dt = time.time() - self.last
        if dt < 0.17: time.sleep(0.17 - dt)
        self.last = time.time()
    def call(self, method, params, tries=3):
        for k in range(tries):
            url = RPCS[(self.i + k) % len(RPCS)]
            self.pace(); self.calls += 1
            body = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params}).encode()
            try:
                req = urllib.request.Request(url, body, {'content-type': 'application/json', 'user-agent': 'element-watch/2.0'})
                r = json.load(urllib.request.urlopen(req, timeout=25))
                if 'error' in r:
                    self.err[r['error'].get('message', '')[:30]] += 1
                    continue
                return r.get('result')
            except Exception as e:
                self.err[str(e)[:30]] += 1
        return None

R = Rpc()
def u(h): return int(h, 16) if h and h != '0x' else 0

def scan(frm, to):
    out, cur, step = [], frm, 500
    while cur <= to:
        end = min(to, cur + step - 1)
        r = R.call('eth_getLogs', [{'fromBlock': hex(cur), 'toBlock': hex(end), 'topics': [FILL]}])
        if r is None:
            step = max(25, step // 2); continue
        out.extend(r); cur = end + 1
    return out

def decode(l):
    d = l['data'][2:]
    w = [int(d[i:i + 64], 16) for i in range(0, len(d), 64)]
    if len(w) < 12: return None
    g = lambda i: w[i] if i < len(w) else 0
    return {
        'block': u(l['blockNumber']), 'tx': l['transactionHash'],
        'seller': '0x%040x' % g(1), 'buyer': '0x%040x' % g(2),
        'tokenId': g(8) if len(w) >= 9 else g(3), 'payToken': '0x%040x' % g(4), 'priceWei': g(5),
        'collection': '0x%040x' % g(7), 'market': '0x%040x' % g(10), 'feeWei': g(11),
    }

def main():
    hours = float(sys.argv[1]) if len(sys.argv) > 1 else 12
    head = u(R.call('eth_blockNumber', []))
    frm = head - int(hours * BLOCKS_PER_HOUR)
    logs = scan(frm, head)
    fills = [x for x in (decode(l) for l in logs) if x]
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

    # 我们的 gated 集合
    tracked = {}
    sig = os.path.expanduser('~/agentid-sniper/shadow/signals.jsonl')
    if os.path.exists(sig):
        for line in open(sig):
            try: r = json.loads(line)
            except: continue
            if r.get('gated') and r.get('assetType') == 'erc721':
                t = r['tok'].lower()
                tracked.setdefault(t, {'sym': r.get('sym'), 'mints': 0, 'eventBlock': r.get('eventBlock')})
                tracked[t]['mints'] = max(tracked[t]['mints'], r.get('mints') or 0)

    tracked_rows = []
    for t, m in tracked.items():
        c = coll.get(t)
        mint_gas = 0.00003 * m['mints']  # 单次 mint gas 约 3e-5 BNB（实测口径，量级参考）
        tracked_rows.append({
            'collection': t, 'sym': m['sym'], 'mints': m['mints'],
            'sales': c['sales'] if c else 0,
            'volumeBnb': (c['vol'] / 1e18) if c else 0,
            'minPriceBnb': (c['min'] / 1e18) if c and c['min'] is not None else None,
            'maxPriceBnb': (c['max'] / 1e18) if c and c['max'] is not None else None,
            'uniqueBuyers': len(c['buyers']) if c else 0,
            'uniqueSellers': len(c['sellers']) if c else 0,
            'sellThrough': (c['sales'] / m['mints']) if c and m['mints'] else 0,
            'lastSaleBlock': c['last'] if c else None,
            'mintGasBnbEst': round(mint_gas, 6),
        })
    tracked_rows.sort(key=lambda r: -r['sales'])

    top = sorted(coll.items(), key=lambda kv: -kv[1]['vol'])[:10]
    agg = {
        'generated': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'windowHours': hours, 'head': head, 'fromBlock': frm,
        'fills_total': len(fills), 'fills_native_bnb': len(native),
        'fills_erc20_pay': len(fills) - len(native),
        'collections_traded': len(coll),
        'volumeBnb': sum(c['vol'] for c in coll.values()) / 1e18,
        'tracked_collections': len(tracked),
        'tracked_with_sales': sum(1 for r in tracked_rows if r['sales'] > 0),
        'tracked_sales': sum(r['sales'] for r in tracked_rows),
        'tracked_volumeBnb': round(sum(r['volumeBnb'] for r in tracked_rows), 6),
        'rpcCalls': R.calls, 'errors': dict(R.err.most_common(4)),
    }
    out = {'agg': agg, 'tracked': tracked_rows[:80],
           'top_collections': [{'collection': k, 'sales': v['sales'],
                                'volumeBnb': round(v['vol'] / 1e18, 6),
                                'minBnb': (v['min'] or 0) / 1e18, 'maxBnb': (v['max'] or 0) / 1e18,
                                'buyers': len(v['buyers']), 'sellers': len(v['sellers'])}
                               for k, v in top]}
    dest = os.path.expanduser('~/agentid-sniper/analysis/element-fills.json')
    json.dump(out, open(dest, 'w'), ensure_ascii=False, indent=1)
    print(json.dumps(agg, ensure_ascii=False, indent=1))
    print('--- 全市场 Top5 系列（按成交量）')
    for t in out['top_collections'][:5]:
        print('  %s sales=%d vol=%.4fBNB 单价 %.5f~%.5f 买%d/卖%d' % (
            t['collection'], t['sales'], t['volumeBnb'], t['minBnb'], t['maxBnb'], t['buyers'], t['sellers']))
    hit = [r for r in tracked_rows if r['sales'] > 0]
    print('--- 我们跟踪的 gated 集合有成交的:', len(hit))
    for r in hit[:10]:
        print('  %s %s sales=%d vol=%.5f sell-through=%.4f' % (r['sym'], r['collection'], r['sales'], r['volumeBnb'], r['sellThrough']))

if __name__ == '__main__':
    main()
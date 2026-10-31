#!/usr/bin/env python3
"""按集合计算「mint 期望值」：把 12h Element 成交、mint gas 实付、mint 规模 join 起来
只读。不签名、不发交易。
输出 analysis/ev-report.json + 终端结论。
"""
import json, os, sys, time, urllib.request
from collections import defaultdict

RPCS = ['https://rpc.48.club', 'https://bsc.blockrazor.xyz']
T = '0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef'
ZERO = '0x' + '0' * 64

class R:
    def __init__(self): self.i = 0; self.calls = 0; self.last = 0; self.err = 0
    def pace(self):
        d = time.time() - self.last
        if d < 0.18: time.sleep(0.18 - d)
        self.last = time.time()
    def call(self, m, p, tries=3):
        for k in range(tries):
            u = RPCS[(self.i + k) % len(RPCS)]
            self.pace(); self.calls += 1
            try:
                rq = urllib.request.Request(u, json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': m, 'params': p}).encode(),
                                            {'content-type': 'application/json', 'user-agent': 'ev/1'})
                r = json.load(urllib.request.urlopen(rq, timeout=22))
                if 'error' in r: self.err += 1; continue
                return r.get('result')
            except Exception: self.err += 1
        return None
RR = R()
def u(h): return int(h, 16) if h and h != '0x' else 0

def mint_gas(tok, ev_block):
    """测该集合一笔真实 mint 的实付 gas（BNB）"""
    logs = RR.call('eth_getLogs', [{'address': tok, 'fromBlock': hex(ev_block), 'toBlock': hex(ev_block + 300), 'topics': [T, ZERO]}])
    if not logs: return None
    for l in logs[:3]:
        rc = RR.call('eth_getTransactionReceipt', [l['transactionHash']])
        if not rc or not rc.get('gasUsed'): continue
        gas = u(rc['gasUsed']); px = u(rc.get('effectiveGasPrice') or rc.get('gasPrice') or '0x0')
        if gas and px: return {'gasUsed': gas, 'gasPriceGwei': px / 1e9, 'bnb': gas * px / 1e18, 'tx': l['transactionHash']}
    return None

def main():
    base = os.path.expanduser('~/agentid-sniper')
    sig = [json.loads(l) for l in open(base + '/shadow/signals.jsonl') if l.strip()]
    coll = {}
    for r in sig:
        if r.get('gated') and r.get('assetType') == 'erc721':
            t = r['tok'].lower()
            c = coll.setdefault(t, {'sym': r.get('sym'), 'mints': 0, 'wallets': 0, 'ratio': 0, 'ev': r.get('eventBlock')})
            c['mints'] = max(c['mints'], r.get('mints') or 0)
            c['wallets'] = max(c['wallets'], r.get('wallets') or 0)
            c['ratio'] = max(c['ratio'], r.get('agentHolderRatio') or 0)
            c['ev'] = min(c['ev'] or 10**12, r.get('eventBlock') or 10**12)
    fills = json.load(open(base + '/analysis/element-fills.json'))
    fl = fills['fills'] if isinstance(fills, dict) else fills
    mkt = defaultdict(lambda: {'n': 0, 'vol': 0, 'prices': [], 'buyers': set(), 'sellers': set()})
    for f in fl:
        if f.get('payToken', '').lower() != '0x' + 'e' * 40: continue
        m = mkt[f['collection'].lower()]
        m['n'] += 1; m['vol'] += f['priceWei']; m['prices'].append(f['priceWei'])
        m['buyers'].add(f['buyer']); m['sellers'].add(f['seller'])

    sample = [t for t, c in sorted(coll.items(), key=lambda kv: -kv[1]['mints'])[:12]]
    gas = {}
    for t in sample:
        g = mint_gas(t, coll[t]['ev'])
        if g: gas[t] = g
    avg_gas = sum(g['bnb'] for g in gas.values()) / len(gas) if gas else None

    rows = []
    for t, c in coll.items():
        m = mkt.get(t)
        prices = sorted(m['prices']) if m else []
        med = prices[len(prices) // 2] / 1e18 if prices else 0
        rows.append({'collection': t, 'sym': c['sym'], 'mints': c['mints'], 'wallets': c['wallets'],
                     'agentRatio': c['ratio'], 'sales12h': m['n'] if m else 0,
                     'vol12h': (m['vol'] / 1e18) if m else 0, 'medianPrice': med,
                     'sellThrough': (m['n'] / c['mints']) if m and c['mints'] else 0,
                     'buyers': len(m['buyers']) if m else 0, 'sellers': len(m['sellers']) if m else 0})
    tracked_with_sales = [r for r in rows if r['sales12h'] > 0]
    pg = avg_gas or 0
    # 策略 A：所有 gated 集合各 mint 1 个
    a_cost = len(rows) * pg
    a_rev = sum(r['sellThrough'] * r['medianPrice'] for r in rows)
    # 策略 B：只 mint mint 数前 10% 的集合
    top = sorted(rows, key=lambda r: -r['mints'])[:max(1, len(rows) // 10)]
    b_cost = len(top) * pg
    b_rev = sum(r['sellThrough'] * r['medianPrice'] for r in top)
    out = {
        'generated': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'collections': len(rows), 'with_sales': len(tracked_with_sales),
        'gas_samples': {k: {kk: round(vv, 12) if isinstance(vv, float) else vv for kk, vv in v.items()} for k, v in list(gas.items())[:6]},
        'avg_mint_gas_bnb': pg,
        'strategy_all': {'mints': len(rows), 'costBnb': a_cost, 'revBnb': a_rev, 'netBnb': a_rev - a_cost},
        'strategy_top10pct': {'mints': len(top), 'costBnb': b_cost, 'revBnb': b_rev, 'netBnb': b_rev - b_cost},
        'top_by_sales': sorted(rows, key=lambda r: -r['sales12h'])[:10],
        'top_by_mints': sorted(rows, key=lambda r: -r['mints'])[:10],
    }
    json.dump({'summary': out, 'rows': rows}, open(base + '/analysis/ev-report.json', 'w'), ensure_ascii=False, indent=1)
    print(json.dumps({k: out[k] for k in ['collections', 'with_sales', 'avg_mint_gas_bnb', 'strategy_all', 'strategy_top10pct']}, ensure_ascii=False, indent=1))
    print('--- 有成交的集合:')
    for r in out['top_by_sales'][:8]:
        print('  %-10s sales=%-4d vol=%.4f 中位价=%.4f sellThrough=%.3f mints=%d buyers=%d sellers=%d' % (
            r['sym'], r['sales12h'], r['vol12h'], r['medianPrice'], r['sellThrough'], r['mints'], r['buyers'], r['sellers']))
    print('--- mint 数最多的集合（成交情况）:')
    for r in out['top_by_mints'][:8]:
        print('  %-10s mints=%-5d wallets=%-4d ratio=%.2f sales=%d' % (r['sym'], r['mints'], r['wallets'], r['agentRatio'], r['sales12h']))

if __name__ == '__main__':
    main()
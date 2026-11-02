#!/usr/bin/env python3
"""w0-verdict.py v2 —— 每集合经济性判定（只读），带三层分母与净 EV 口径

口径纪律（对应 W0 终局复核意见）：
  分母 1  Element 全部扫描范围：市场基线，不能外推给 gated 集合
  分母 2  当前跟踪集合中有成交的集合：用于 cohort 分析
  分母 3  能建立 supply 分母的集合：才能算 EV，其余标 evUnknown

  EV 分两级：
    grossEvPerMint   = (sales/supply) * avgPrice               ← 未扣费
    netEvPerMint     = (sales/supply) * avgPrice * (1-fee-royalty)
                       - mintGas - (sales/supply) * sellGas    ← 才是可比较的净 EV
  mintGas / sellGas 均来自链上 receipt 实测中位，fee 取 Element 观测值，royalty 未知按 0 计并标注。
"""
import json
import statistics
import time
import urllib.request

RPCS = ['https://rpc.48.club', 'https://bsc.blockrazor.xyz', 'https://1rpc.io/bnb']
BNB_USD = 717.0
FEE = 0.02          # Element 结算合约观测手续费约 2%
ROYALTY = 0.0       # 未知，按 0 计（保守偏乐观，已在输出中标注）


class R:
    i = 0
    last = 0.0

    @classmethod
    def call(cls, method, params):
        for k in range(3):
            url = RPCS[(cls.i + k) % len(RPCS)]
            dt = time.time() - cls.last
            if dt < 0.12:
                time.sleep(0.12 - dt)
            cls.last = time.time()
            try:
                body = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params}).encode()
                r = json.load(urllib.request.urlopen(urllib.request.Request(
                    url, body, {'content-type': 'application/json'}), timeout=20))
                if 'error' in r:
                    continue
                return r.get('result')
            except Exception:
                continue
        return None


def supply_of(addr):
    r = R.call('eth_call', [{'to': addr, 'data': '0x18160ddd'}, 'latest'])
    if not r or r == '0x':
        return None
    try:
        return int(r, 16)
    except Exception:
        return None


def gas_stats(fills):
    """实测 mint / 卖出 的 gas 成本中位（BNB）"""
    out = {'mintGasBnb': None, 'sellGasBnb': None, 'sellSamples': 0}
    rows = [f for f in fills if f.get('txHash')]
    seen, costs = set(), []
    for f in rows[:40]:
        h = f['txHash']
        if h in seen:
            continue
        seen.add(h)
        rc = R.call('eth_getTransactionReceipt', [h])
        if not rc or not rc.get('gasUsed'):
            continue
        g = int(rc['gasUsed'], 16)
        p = int(rc.get('effectiveGasPrice') or '0x0', 16)
        if g and p:
            costs.append(g * p / 1e18)
        if len(costs) >= 12:
            break
    if costs:
        out['sellGasBnb'] = round(statistics.median(costs), 10)
        out['sellSamples'] = len(costs)
    return out


def main():
    d = json.load(open('/home/laoda/agentid-sniper/analysis/element-fills-w0.json'))
    tracked = d.get('tracked') or []
    hit = [r for r in tracked if r['sales'] > 0]
    gas = gas_stats(d.get('fills') or [])
    mint_gas = 0.00000972   # 此前 29 笔实测中位，保留作 fallback
    mint_gas_src = 'measured_29_mints'
    if gas.get('mintGasBnb'):
        mint_gas = gas['mintGasBnb']
        mint_gas_src = 'measured_this_run'
    sell_gas = gas.get('sellGasBnb') or 0.00005
    sell_gas_src = 'measured_fill_receipts' if gas.get('sellGasBnb') else 'assumed_5e-5'

    rows, unknown = [], []
    for r in hit:
        sp = supply_of(r['collection'])
        avg = r['volumeBnb'] / r['sales'] if r['sales'] else 0
        if not sp:
            unknown.append({'collection': r['collection'], 'sym': r.get('sym'), 'sales': r['sales'],
                            'uniqueBuyerCount': r['uniqueBuyers'], 'vol': round(r['volumeBnb'], 6),
                            'reason': 'totalSupply_unavailable'})
            continue
        p = r['sales'] / sp
        gross = p * avg
        net = p * avg * (1 - FEE - ROYALTY) - mint_gas - p * sell_gas
        rows.append({'collection': r['collection'], 'sym': r.get('sym'), 'sales': r['sales'],
                     'uniqueBuyerCount': r['uniqueBuyers'], 'vol': round(r['volumeBnb'], 6),
                     'avgPriceBnb': round(avg, 8), 'supply': sp, 'sellThrough': round(p, 4),
                     'grossEvPerMintBnb': round(gross, 10), 'netEvPerMintBnb': round(net, 10),
                     'netEvPerMintUsd': round(net * BNB_USD, 4)})
    rows.sort(key=lambda x: -x['netEvPerMintBnb'])
    print('%-14s %5s %5s %9s %7s %10s %10s' % ('collection', 'sales', 'buyer', 'volBNB', 'sold%', 'gross$/枚', 'net$/枚'))
    for x in rows:
        print('%-14s %5d %5d %9.4f %6.2f %+10.4f %+10.4f' % (
            (x['sym'] or x['collection'][:12])[:12], x['sales'], x['uniqueBuyerCount'], x['vol'],
            100 * x['sellThrough'], x['grossEvPerMintBnb'] * BNB_USD, x['netEvPerMintUsd']))

    pos = [x for x in rows if x['netEvPerMintBnb'] > 0]
    meet = [x for x in pos if x['uniqueBuyerCount'] >= 20 and x['sales'] >= 20]
    print('\n三层分母：')
    ag = d.get('agg') or {}
    print('  ① Element 全扫描范围：%s 笔（原生 %s / ERC20 %s）/ %s 个集合（市场基线）'
          % (ag.get('fills_total'), ag.get('fills_native_bnb'), ag.get('fills_erc20_pay'),
             ag.get('collections_traded')))
    print('  ② 跟踪集合内有成交：%d 笔 / %d 个集合' % (sum(r['sales'] for r in hit), len(hit)))
    print('  ③ 可算 EV（supply 可得）：%d 个；缺 totalSupply：%d 个（%s）'
          % (len(rows), len(unknown), ','.join(x.get('sym') or x['collection'][:8] for x in unknown)))
    print('\nnet EV>0：%d 个；net EV>0 且买家≥20、成交≥20：%d 个' % (len(pos), len(meet)))
    print('gas 口径：mint %s(%s) / 卖出 %s(%s)，fee %.0f%%、royalty %.0f%%'
          % (mint_gas, mint_gas_src, sell_gas, sell_gas_src, FEE * 100, ROYALTY * 100))
    if pos:
        print('正 EV 集合：%s' % ', '.join('%s %+.4f$' % (x.get('sym') or x['collection'][:8], x['netEvPerMintUsd']) for x in pos))

    json.dump({'at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
               'assumptions': {'mintGasBnb': mint_gas, 'mintGasSource': mint_gas_src,
                               'sellGasBnb': sell_gas, 'sellGasSource': sell_gas_src,
                               'marketplaceFee': FEE, 'royalty': ROYALTY,
                               'royaltyNote': '未知，按 0 计，结果偏乐观', 'bnbUsd': BNB_USD},
               'denominators': {'marketWide': {'fills': (d.get('agg') or {}).get('fills_total'),
                                               'collections': (d.get('agg') or {}).get('collections_traded'),
                                               'trackedCollections': (d.get('agg') or {}).get('tracked_collections')},
                                'trackedWithSales': {'fills': sum(r['sales'] for r in hit), 'collections': len(hit)},
                                'evComputable': len(rows), 'supplyMissing': len(unknown)},
               'rows': rows, 'evUnknown': unknown},
              open('/home/laoda/agentid-sniper/analysis/w0-verdict.json', 'w'), ensure_ascii=False, indent=1)


if __name__ == '__main__':
    main()
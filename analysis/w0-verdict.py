#!/usr/bin/env python3
"""w0-verdict.py v3 —— 每集合经济性（只读）

命名纪律（本轮修正）：
  grossEvPerMint          卖出率 × 均价，未扣任何费用
  netKnownCostEvPerMint   扣 mintGas + 卖出率 × sellGas + 卖出率 × 均价 × marketplaceFee
                          royalty 未知 → 不能叫完全净 EV，字段名带 KnownCost
  royaltyUnknown          true 时同时给出 royalty 敏感性（0/2.5/5/7.5/10%）
  evLowerBound            用 --royalty-worst（默认 10%）算出的下界

覆盖度必须同时报告，避免把 12 个集合的经济性外推成 55 个：
  collectionCoverage / saleCoverage / volumeCoverage / evCoverage
"""
import json
import statistics
import time
import urllib.request

RPCS = ['https://rpc.48.club', 'https://bsc.blockrazor.xyz', 'https://1rpc.io/bnb']
BNB_USD = 717.0
FEE = 0.02
ROYALTY_STEPS = [0.0, 0.025, 0.05, 0.075, 0.10]
ROYALTY_WORST = 0.10


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


def main():
    d = json.load(open('/home/laoda/agentid-sniper/analysis/element-fills-w0.json'))
    ag = d.get('agg') or {}
    tracked = d.get('tracked') or []
    hit = [r for r in tracked if r['sales'] > 0]

    mint_gas = 0.00000972
    sell_gas = 0.00005
    mint_gas_src, sell_gas_src = 'measured_29_mints', 'assumed_5e-5'

    rows, unknown = [], []
    for r in hit:
        sp = supply_of(r['collection'])
        avg = r['volumeBnb'] / r['sales'] if r['sales'] else 0
        if not sp:
            unknown.append({'collection': r['collection'], 'sym': r.get('sym'), 'sales': r['sales'],
                            'uniqueBuyerCount': r['uniqueBuyers'], 'vol': round(r['volumeBnb'], 6),
                            'reason': 'totalSupply_unavailable', 'evUnknown': True})
            continue
        p = r['sales'] / sp
        gross = p * avg
        known = p * avg * (1 - FEE) - mint_gas - p * sell_gas
        sens = {('%.1f%%' % (x * 100)): round(p * avg * (1 - FEE - x) - mint_gas - p * sell_gas, 10)
                for x in ROYALTY_STEPS}
        lower = sens['%.1f%%' % (ROYALTY_WORST * 100)]
        rows.append({'collection': r['collection'], 'sym': r.get('sym'), 'sales': r['sales'],
                     'uniqueBuyerCount': r['uniqueBuyers'], 'vol': round(r['volumeBnb'], 6),
                     'avgPriceBnb': round(avg, 8), 'supply': sp, 'sellThrough': round(p, 4),
                     'grossEvPerMintBnb': round(gross, 10),
                     'netKnownCostEvPerMintBnb': round(known, 10),
                     'netKnownCostEvPerMintUsd': round(known * BNB_USD, 4),
                     'royaltyUnknown': True, 'royaltySensitivityBnb': sens,
                     'evLowerBoundUsd': round(lower * BNB_USD, 4)})
    rows.sort(key=lambda x: -x['netKnownCostEvPerMintBnb'])

    print('%-12s %5s %5s %8s %6s %9s %11s %9s' % (
        'collection', 'sales', 'buyer', 'volBNB', 'sold%', 'gross$', 'netKnownCost$', 'worst10%$'))
    for x in rows:
        print('%-12s %5d %5d %8.4f %5.2f%% %+9.4f %+11.4f %+9.4f' % (
            (x['sym'] or x['collection'][:12])[:12], x['sales'], x['uniqueBuyerCount'], x['vol'],
            100 * x['sellThrough'], x['grossEvPerMintBnb'] * BNB_USD,
            x['netKnownCostEvPerMintUsd'], x['evLowerBoundUsd']))

    pos_known = [x for x in rows if x['netKnownCostEvPerMintBnb'] > 0]
    pos_worst = [x for x in rows if x['evLowerBoundUsd'] > 0]
    meet = [x for x in pos_worst if x['uniqueBuyerCount'] >= 20 and x['sales'] >= 20]

    cov = {
        'collectionCoverage': '%d/%s' % (len(hit), ag.get('collections_traded')),
        'saleCoverage': '%d/%s' % (sum(r['sales'] for r in hit), ag.get('fills_total')),
        'volumeCoverage': '%.4f/%.4f' % (sum(r['volumeBnb'] for r in hit), ag.get('volumeBnb') or 0),
        'evCoverage': '%d/%d' % (len(rows), len(hit)),
    }
    print('\n三层分母：')
    print('  ① 全扫描 %s 笔 / %s 集合（原生 %s）' % (ag.get('fills_total'), ag.get('collections_traded'),
                                                ag.get('fills_native_bnb')))
    print('  ② 跟踪集合有成交 %d 笔 / %d 集合；③ 可算 EV %d 个，缺 supply %d 个（%s）'
          % (sum(r['sales'] for r in hit), len(hit), len(rows), len(unknown),
             ','.join(x.get('sym') or x['collection'][:8] for x in unknown)))
    print('  覆盖度：集合 %s、成交 %s、金额 %s、EV %s' % (
        cov['collectionCoverage'], cov['saleCoverage'], cov['volumeCoverage'], cov['evCoverage']))
    print('\nnetKnownCostEV>0：%d 个；最不利 royalty(10%%) 下仍>0：%d 个；'
          '后者且买家≥20、成交≥20：%d 个' % (len(pos_known), len(pos_worst), len(meet)))
    print('gas：mint %.8f(%s) / 卖出 %.8f(%s)，marketplaceFee %.0f%%，royalty 未知' % (
        mint_gas, mint_gas_src, sell_gas, sell_gas_src, FEE * 100))
    for x in rows[:3]:
        print('  敏感性 %s：%s' % (x.get('sym'), {k: round(v * BNB_USD, 4) for k, v in x['royaltySensitivityBnb'].items()}))

    json.dump({'at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
               'assumptions': {'mintGasBnb': mint_gas, 'mintGasSource': mint_gas_src,
                               'sellGasBnb': sell_gas, 'sellGasSource': sell_gas_src,
                               'marketplaceFee': FEE, 'royaltyUnknown': True,
                               'royaltyWorstCase': ROYALTY_WORST, 'bnbUsd': BNB_USD,
                               'note': 'netKnownCostEV 未含版税，不得称为完全净 EV；'
                                       'evLowerBound 取 royalty=10% 的最不利假设'},
               'coverage': cov,
               'denominators': {'marketWide': {'fills': ag.get('fills_total'),
                                               'collections': ag.get('collections_traded'),
                                               'volumeBnb': ag.get('volumeBnb')},
                                'trackedWithSales': {'fills': sum(r['sales'] for r in hit),
                                                     'collections': len(hit),
                                                     'volumeBnb': round(sum(r['volumeBnb'] for r in hit), 6)},
                                'evComputable': len(rows), 'supplyMissing': len(unknown)},
               'counts': {'positiveKnownCost': len(pos_known), 'positiveWorstCase': len(pos_worst),
                          'positiveWorstCaseAndLiquidEnough': len(meet)},
               'rows': rows, 'evUnknown': unknown},
              open('/home/laoda/agentid-sniper/analysis/w0-verdict.json', 'w'), ensure_ascii=False, indent=1)


if __name__ == '__main__':
    main()
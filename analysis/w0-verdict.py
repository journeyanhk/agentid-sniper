#!/usr/bin/env python3
"""w0-verdict.py —— 用 48h 快照算每集合 EV，输出 A/B/C 判定所需证据（只读）

EV 口径：
  supply  = totalSupply()（链上真值，不能用探针观测的 mint 数当分母）
  sold    = 本窗口该集合的 Element 成交笔数
  avg     = volume / sold
  gas     = 实测单次 mint 成本中位 0.00000972 BNB
  EV/枚   = (sold / supply) * avg - gas
"""
import json, sys, time, urllib.request
from collections import Counter

RPCS = ['https://rpc.48.club', 'https://bsc.blockrazor.xyz', 'https://1rpc.io/bnb']
GAS_BNB = 0.00000972
BNB_USD = 717.0


class R:
    i = 0
    last = 0

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
    rows = d.get('tracked') or []
    hit = [r for r in rows if r['sales'] > 0]
    hit.sort(key=lambda r: -r['volumeBnb'])
    out = []
    for r in hit[:20]:
        sp = supply_of(r['collection'])
        avg = r['volumeBnb'] / r['sales'] if r['sales'] else 0
        ev = ((r['sales'] / sp) * avg - GAS_BNB) if sp else None
        out.append({'collection': r['collection'], 'sym': r.get('sym'), 'sales': r['sales'],
                    'buyers': r['uniqueBuyers'], 'vol': round(r['volumeBnb'], 6),
                    'avgBnb': round(avg, 8), 'supply': sp,
                    'sellThrough': round(r['sales'] / sp, 4) if sp else None,
                    'evPerMintBnb': round(ev, 9) if ev is not None else None,
                    'evPerMintUsd': round(ev * BNB_USD, 4) if ev is not None else None})
    out.sort(key=lambda x: -(x['evPerMintBnb'] or -9))
    print('%-28s %5s %5s %9s %7s %9s %9s' % ('collection', 'sales', 'buyer', 'volBNB', 'sold%', 'EV/枚$', 'supply'))
    for x in out:
        print('%-28s %5d %5d %9.4f %6s %9s %9s' % (
            (x['sym'] or x['collection'][:12])[:26], x['sales'], x['buyers'], x['vol'],
            ('%.2f' % (100 * x['sellThrough'])) if x['sellThrough'] is not None else '-',
            ('%+.4f' % x['evPerMintUsd']) if x['evPerMintUsd'] is not None else '-',
            x['supply']))
    pos = [x for x in out if (x['evPerMintBnb'] or 0) > 0]
    strong = [x for x in pos if x['buyers'] >= 20 and x['sales'] >= 20]
    print('\n成交集合 %d 个（有 tracked 行的），其中 EV>0 %d 个，EV>0 且买家≥20 且成交≥20 的 %d 个'
          % (len(hit), len(pos), len(strong)))
    json.dump({'at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'gasBnb': GAS_BNB,
               'bnbUsd': BNB_USD, 'rows': out}, open(
        '/home/laoda/agentid-sniper/analysis/w0-verdict.json', 'w'), ensure_ascii=False, indent=1)


if __name__ == '__main__':
    main()
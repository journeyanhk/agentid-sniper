#!/usr/bin/env python3
"""aggregator-check.py —— 用第三方聚合器反向检验我们「无 AMM 出口」的结论（只读）

问题：我们的池子探测只查 PancakeSwap V2/V3 factory。如果项目上了别的 DEX
      （BakerySwap / Uniswap V3 fork / 发射台自带池），我们会误判成「没有出口」。

做法：抽一批 poolFound=false 的 gated 信号，逐个查 DexScreener
      https://api.dexscreener.com/latest/dex/tokens/<addr>
      统计有多少存在 pair、在哪家 DEX、liquidity 多少。

只读：纯 HTTP GET，不签名、不交易、不写链。
"""
import json, os, sys, time, urllib.request
from collections import Counter

def get(url):
    try:
        rq = urllib.request.Request(url, headers={'user-agent': 'aggcheck/1.0'})
        return json.load(urllib.request.urlopen(rq, timeout=20))
    except Exception as e:
        return {'_err': str(e)[:40]}

def main():
    base = os.path.expanduser('~/agentid-sniper')
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 40
    rows = [json.loads(l) for l in open(base + '/shadow/signals.jsonl') if l.strip()]
    seen, cand = set(), []
    for r in reversed(rows):
        if not r.get('gated') or r.get('poolFound'): continue
        t = r['tok'].lower()
        if t in seen: continue
        seen.add(t); cand.append(r)
        if len(cand) >= limit: break
    print(f'待检验：{len(cand)} 个「我们判定无池子」的 gated 项目', flush=True)

    stat = Counter(); found = []
    for n, r in enumerate(cand, 1):
        t = r['tok'].lower()
        d = get(f'https://api.dexscreener.com/latest/dex/tokens/{t}')
        if '_err' in d:
            stat['err'] += 1; continue
        pairs = d.get('pairs') or []
        stat['checked'] += 1
        if not pairs:
            stat['noPair'] += 1; continue
        stat['hasPair'] += 1
        best = max(pairs, key=lambda p: float((p.get('liquidity') or {}).get('usd') or 0))
        liq = float((best.get('liquidity') or {}).get('usd') or 0)
        found.append({'tok': t, 'sym': r.get('sym'), 'assetType': r.get('assetType'),
                      'dex': best.get('dexId'), 'pair': best.get('pairAddress'),
                      'liqUsd': liq, 'priceUsd': best.get('priceUsd')})
        if liq > 0: stat['withLiquidity'] += 1
        if n % 10 == 0: print(f'  {n}/{len(cand)} hasPair={stat["hasPair"]} liq>0={stat["withLiquidity"]}', flush=True)

    out = {'generated': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
           'sampled': len(cand), 'stats': dict(stat),
           'hasPairRate': round(stat['hasPair'] / max(1, stat['checked']), 3),
           'found': found[:40]}
    json.dump(out, open(base + '/analysis/aggregator-check.json', 'w'), ensure_ascii=False, indent=1)
    print(json.dumps({k: out[k] for k in ['sampled', 'stats', 'hasPairRate']}, ensure_ascii=False, indent=1))
    for f in found[:12]:
        print('   %-12s %-6s %-18s liq=$%-9.0f %s' % (f['sym'], f['assetType'], f['dex'], f['liqUsd'], f['tok']))

if __name__ == '__main__':
    main()
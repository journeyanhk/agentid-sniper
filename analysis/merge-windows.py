#!/usr/bin/env python3
"""merge-windows.py —— 把两个 12h 窗口的 cohort 结果合并成 24h 口径，并算集中度与净 EV。
只读，读 analysis/cohort-{latest,prev}.json。
"""
import json, os
from collections import defaultdict

base = os.path.expanduser('~/agentid-sniper/analysis')
L = json.load(open(base + '/cohort-latest.json'))
P = json.load(open(base + '/cohort-prev.json'))

def index(d):
    return {r['collection']: r for r in d['collections']}

il, ip = index(L), index(P)
cols = set(il) | set(ip)
rows = []
for c in cols:
    a, b = il.get(c), ip.get(c)
    sales = (a['real_sales'] if a else 0) + (b['real_sales'] if b else 0)
    if sales == 0: continue
    vol = (a['volumeBnb'] if a else 0) + (b['volumeBnb'] if b else 0)
    sym = (a or b)['sym']
    mints = max((a['mints'] if a else 0), (b['mints'] if b else 0))
    buyers = max((a['unique_buyers'] if a else 0), (b['unique_buyers'] if b else 0))
    sellers = max((a['unique_sellers'] if a else 0), (b['unique_sellers'] if b else 0))
    rep = max((a['repeat_pairs'] if a else 0), (b['repeat_pairs'] if b else 0))
    tts = [x for x in [(a or {}).get('ttsMedianMin'), (b or {}).get('ttsMedianMin')] if x]
    rows.append({'sym': sym, 'collection': c,
                 'sales24h': sales, 'volBnb24h': round(vol, 6),
                 'salesPrev': b['real_sales'] if b else 0, 'salesLatest': a['real_sales'] if a else 0,
                 'repeatable': bool(a and b and a['real_sales'] >= 5 and b['real_sales'] >= 5),
                 'mints': mints, 'uniqueBuyers': buyers, 'uniqueSellers': sellers,
                 'repeatPairs': rep,
                 'ttsMedianMin': round(sum(tts) / len(tts), 1) if tts else None,
                 'sellThrough24h': round(sales / mints, 5) if mints else None,
                 'avgPriceUsd': round((vol / sales) * 700, 3) if sales else 0,
                 'evPerMintUsd': round((sales / mints) * (vol / sales) * 0.98 * 700 - 0.0068, 4) if mints and sales else None,
                 })
rows.sort(key=lambda r: -r['volBnb24h'])

tot_sales = sum(r['sales24h'] for r in rows)
tot_vol = sum(r['volBnb24h'] for r in rows)
tot_mints = sum(r['mints'] for r in rows)
repeat = [r for r in rows if r['repeatable']]
summary = {
    'windows': {'latest': L['agg']['fromBlock'], 'prev': P['agg']['fromBlock']},
    'latestWindow': L['agg'], 'prevWindow': P['agg'],
    'collectionsWithSales': len(rows),
    'totalSales24h': tot_sales, 'totalVolumeBnb24h': round(tot_vol, 6),
    'totalVolumeUsd24h': round(tot_vol * 700, 1),
    'repeatableCollections': [r['sym'] for r in repeat],
    'mintsAcrossSoldCollections': tot_mints,
    'aggregateSellThrough': round(tot_sales / tot_mints, 5) if tot_mints else None,
    'avgPriceUsd': round(tot_vol / tot_sales * 700, 3) if tot_sales else None,
}
json.dump({'summary': summary, 'rows': rows}, open(base + '/cohort-24h.json', 'w'), ensure_ascii=False, indent=1)
print(json.dumps(summary, ensure_ascii=False, indent=1))
print('%-8s %6s %6s %6s %7s %7s %6s %6s %8s %9s %s' % ('sym', 'prev', 'latest', '24h', 'volBNB', 'buyers', 'sell', 'reppair', 'ttsMin', 'avg$', 'EV/mint$'))
for r in rows:
    print('%-8s %6d %6d %6d %7.4f %7d %6d %7d %8s %9.2f %s' % (
        r['sym'], r['salesPrev'], r['salesLatest'], r['sales24h'], r['volBnb24h'],
        r['uniqueBuyers'], r['uniqueSellers'], r['repeatPairs'], r['ttsMedianMin'],
        r['avgPriceUsd'], r['evPerMintUsd']))
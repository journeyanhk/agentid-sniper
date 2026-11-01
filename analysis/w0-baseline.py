#!/usr/bin/env python3
"""w0-baseline.py —— W0 窗口结束时一次性固化的完整基线（只读、可重复跑）

用途：48h 窗口（2026-10-31 09:30 → 2026-11-02 09:30 CST）结束时，把评审要求的
所有字段一次性写进 analysis/w0-baseline.json，之后合并 probe-5.x 再开新窗口，
新旧数据不混。

字段覆盖：窗口四要素、dataComplete、gaps、pending、zeroDisagreements、
collection 数量、有成交 collection、真实成交数、unique buyers/sellers、cohort EV。

只读：只解析本地已落盘的 metrics/signals/fills，不发任何链上请求。
"""
import json, os, sys, time, statistics as st
from collections import Counter, defaultdict

BASE = os.path.expanduser('~/agentid-sniper')
F = lambda *p: os.path.join(BASE, *p)


def jl(path):
    if not os.path.exists(path): return []
    out = []
    for line in open(path):
        line = line.strip()
        if not line: continue
        try: out.append(json.loads(line))
        except Exception: pass
    return out


def js(path):
    try: return json.load(open(path))
    except Exception: return None


def pct(v, q):
    if not v: return None
    v = sorted(v); i = min(len(v) - 1, int(len(v) * q)); return round(v[i], 1)


def main():
    hours = float(sys.argv[1]) if len(sys.argv) > 1 else 48.0
    metrics = jl(F('shadow', 'metrics.jsonl'))
    signals = jl(F('shadow', 'signals.jsonl'))
    gaps = jl(F('shadow', 'gaps.jsonl'))
    fills = js(F('analysis', 'element-fills.json'))
    cohort = js(F('analysis', 'cohort-latest.json'))

    # ---- 窗口四要素 ----
    cut = time.time() - hours * 3600
    m = [r for r in metrics if (r.get('t') or r.get('ts') or '') >= time.strftime('%Y-%m-%dT%H:%M', time.gmtime(cut))[:13]
         or True]
    m = metrics[-int(hours * 60 * 60 / 12):] if metrics else []   # 约 12s/轮
    first, last = (m[0], m[-1]) if m else ({}, {})
    win = {
        'fromBlock': first.get('fromBlock'), 'toBlock': last.get('lastBlockAfter') or last.get('head'),
        'fromTs': first.get('t') or first.get('ts'), 'toTs': last.get('t') or last.get('ts'),
        'windowHours': hours, 'rounds': len(m),
        'probeVersion': last.get('v'), 'build': last.get('build'),
    }
    # ---- 完整性 ----
    integ = {
        'hadGapRounds': sum(1 for r in m if r.get('hadGap')),
        'gapCountSum': sum(int(r.get('gapCount') or 0) for r in m),
        'gapFileEntries': len(gaps),
        'pendingMax': max([int(r.get('pending') or 0) for r in m] or [0]),
        'pendingOverflowMax': max([int(r.get('pendingOverflow') or 0) for r in m] or [0]),
        'stateCorrupt': any(r.get('stateCorrupt') for r in m),
        'cursorPausedRounds': sum(1 for r in m if r.get('cursorPaused')),
        'zeroDisagreements': last.get('zeroDisagreements'),
        'incompleteSignals': last.get('incompleteSignals'),
        'lagMax': max([int(r.get('lagBlocks') or 0) for r in m] or [0]),
        'lagLast': last.get('lagBlocks'),
        'msAvg': round(sum(r['ms'] for r in m) / len(m), 0) if m else None,
        'msP95': pct([r['ms'] for r in m], 0.95),
    }
    # ---- 集合与信号 ----
    cols = {r['tok'] for r in signals if r.get('assetType') == 'erc721' and r.get('gated')}
    sig = {
        'signals': len(signals),
        'assetType': dict(Counter(r.get('assetType') for r in signals)),
        'gatedTotal': sum(1 for r in signals if r.get('gated')),
        'gatedErc721': len(cols),
        'byClass': dict(Counter(r.get('signalClass') for r in signals)),
    }
    # ---- 成交 ----
    sales = {'source': 'element-fills.json', 'available': bool(fills)}
    if fills and isinstance(fills.get('agg'), dict):
        a = fills['agg']
        tr = fills.get('tracked')
        t = tr if isinstance(tr, dict) else {}
        tl = tr if isinstance(tr, list) else []
        sales.update({
            'generatedAt': a.get('generated'),
            'fills': a.get('fills_total'),
            'fillsNative': a.get('fills_native_bnb'),
            'collectionsTraded': a.get('collections_traded'),
            'volumeBnb': a.get('volumeBnb'),
            'trackedCollections': t.get('tracked_collections') or (len(tl) or None),
            'trackedWithSales': t.get('tracked_with_sales'),
            'trackedSales': t.get('tracked_sales'),
            'trackedVolumeBnb': t.get('trackedVolumeBnb') or t.get('tracked_volume_bnb'),
        })
    elif fills:
        rows = fills.get('fills') or fills.get('rows') or []
        if isinstance(rows, list) and rows:
            sales.update({
                'fills': len(rows),
                'collectionsTraded': len({r.get('collection') for r in rows if r.get('collection')}),
                'volumeBnb': round(sum(float(r.get('priceBnb') or 0) for r in rows), 6),
                'uniqueBuyers': len({r.get('buyer') for r in rows if r.get('buyer')}),
                'uniqueSellers': len({r.get('seller') for r in rows if r.get('seller')}),
            })
    # ---- cohort EV ----
    ev = None
    if cohort:
        ev = cohort.get('collections') or cohort.get('cohort') or cohort
    out = {'generated': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
           'window': win, 'integrity': integ, 'signals': sig, 'sales': sales, 'cohortEv': ev}
    # 成交快照必须新鲜：element-fills.json 过期就明确标注，不拿旧数据冒充当期基线
    g = sales.get('generatedAt')
    if g:
        try:
            import datetime
            age = (time.time() - datetime.datetime.strptime(g, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=datetime.timezone.utc).timestamp()) / 3600
            sales['snapshotAgeHours'] = round(age, 1)
            if age > 13: print('!! 成交快照已过期 %.1f 小时，先跑 analysis/element-watch.py 再固化基线' % age)
        except Exception: pass
    os.makedirs(F('analysis'), exist_ok=True)
    path = F('analysis', 'w0-baseline.json')
    json.dump(out, open(path, 'w'), ensure_ascii=False, indent=1)

    print('窗口 %s..%s  轮次 %s  版本 %s' % (win['fromBlock'], win['toBlock'], win['rounds'], win['probeVersion']))
    print('完整性: gap轮=%s gap数=%s pendingMax=%s overflow=%s zeroDis=%s lagMax=%s msAvg=%s' % (
        integ['hadGapRounds'], integ['gapCountSum'], integ['pendingMax'], integ['pendingOverflowMax'],
        integ['zeroDisagreements'], integ['lagMax'], integ['msAvg']))
    print('集合: gated ERC-721=%s | 信号=%s | class=%s' % (sig['gatedErc721'], sig['signals'], sig['byClass']))
    print('成交: %s' % json.dumps({k: v for k, v in sales.items() if k != 'source'}, ensure_ascii=False))
    print('已写入 %s' % path)


if __name__ == '__main__':
    main()
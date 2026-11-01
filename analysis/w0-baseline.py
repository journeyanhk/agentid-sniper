#!/usr/bin/env python3
"""w0-baseline.py —— W0 窗口基线的唯一生成入口（只读 + 严格闸门）

设计原则（2026-11-01 复核后重写）：
1. 所有数据源按**同一时间窗口**过滤，不按「最后 N 条」猜测；
2. 任何一项不满足就 BLOCKED，只写 <out>.invalid.json，绝不产出看似完整的基线；
3. 版本必须唯一（probe v / build / collector），混合即 BLOCKED；
4. 成交快照必须覆盖窗口 block 范围且足够新鲜；
5. 正式基线用 tmp + rename 原子落盘，并写 .sha256 供复核。

用法：
  python3 analysis/w0-baseline.py --from-ts 2026-10-31T01:30:00Z --to-ts 2026-11-02T01:30:00Z
  python3 analysis/w0-baseline.py --hours 48
  python3 analysis/w0-baseline.py --hours 0.5 --fills /tmp/ew-test.json --out /tmp/b.json
"""
import argparse, hashlib, json, os, sys, time
from collections import Counter
import datetime as dt

BASE = os.path.expanduser('~/agentid-sniper')
F = lambda *p: os.path.join(BASE, *p)


def jl(path):
    if not os.path.exists(path):
        return []
    out = []
    for line in open(path, encoding='utf-8'):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            pass
    return out


def js(path):
    try:
        return json.load(open(path, encoding='utf-8'))
    except Exception:
        return None


def parse_ts(v):
    if not v:
        return None
    s = str(v).replace('Z', '+00:00')
    try:
        d = dt.datetime.fromisoformat(s)
    except Exception:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d.timestamp()


def pct(v, q):
    if not v:
        return None
    v = sorted(v)
    i = min(len(v) - 1, int(len(v) * q))
    return round(v[i], 6)


def in_window(ts, a, b):
    t = parse_ts(ts)
    return t is not None and a <= t <= b


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--from-ts')
    ap.add_argument('--to-ts')
    ap.add_argument('--hours', type=float, default=48.0)
    ap.add_argument('--fills', default=F('analysis', 'element-fills.json'))
    ap.add_argument('--out', default=F('analysis', 'w0-baseline.json'))
    a = ap.parse_args()

    now = time.time()
    to_ts = parse_ts(a.to_ts) if a.to_ts else now
    from_ts = parse_ts(a.from_ts) if a.from_ts else to_ts - a.hours * 3600
    iso = lambda x: dt.datetime.fromtimestamp(x, dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

    blocked = []

    def block(reason, detail):
        blocked.append({'reason': reason, 'detail': detail})

    # ---------- 读取并按窗口过滤 ----------
    metrics_all = jl(F('shadow', 'metrics.jsonl'))
    signals_all = jl(F('shadow', 'signals.jsonl'))
    gaps_all = jl(F('shadow', 'gaps.jsonl'))

    m = [r for r in metrics_all if in_window(r.get('t') or r.get('ts'), from_ts, to_ts)]
    sg = [r for r in signals_all if in_window(r.get('t'), from_ts, to_ts)]
    gp = [r for r in gaps_all if in_window(r.get('t') or r.get('at'), from_ts, to_ts)]
    gp_undated = [r for r in gaps_all if not (r.get('t') or r.get('at'))]

    if not m:
        block('no_metrics_in_window', {'from': iso(from_ts), 'to': iso(to_ts), 'totalMetrics': len(metrics_all)})

    # ---------- 版本唯一性 ----------
    vers = sorted({r.get('v') for r in m if r.get('v')})
    builds = sorted({r.get('build') for r in m if r.get('build')})
    svers = sorted({r.get('v') for r in sg if r.get('v')})
    if len(vers) > 1:
        block('version_mix_metrics', {'versions': vers})
    if len(builds) > 1:
        block('build_mix_metrics', {'builds': builds})
    if len(svers) > 1:
        block('version_mix_signals', {'versions': svers})

    first, last = (m[0], m[-1]) if m else ({}, {})
    from_block = first.get('fromBlock')
    to_block = last.get('lastBlockAfter') or last.get('head')

    # ---------- 完整性 ----------
    integ = {
        'rounds': len(m),
        'hadGapRounds': sum(1 for r in m if r.get('hadGap')),
        'gapCountSum': sum(int(r.get('gapCount') or 0) for r in m),
        'gapsInWindow': len(gp),
        'gapsUndated': len(gp_undated),
        'pendingMax': max([int(r.get('pending') or 0) for r in m] or [0]),
        'pendingOverflowMax': max([int(r.get('pendingOverflow') or 0) for r in m] or [0]),
        'stateCorruptRounds': sum(1 for r in m if r.get('stateCorrupt')),
        'cursorPausedRounds': sum(1 for r in m if r.get('cursorPaused')),
        'zeroDisagreements': last.get('zeroDisagreements'),
        'incompleteSignals': last.get('incompleteSignals'),
        'lagMax': max([int(r.get('lagBlocks') or 0) for r in m] or [0]),
        'lagLast': last.get('lagBlocks'),
        'msAvg': round(sum(r['ms'] for r in m) / len(m), 0) if m else None,
        'msP95': pct([r.get('ms') for r in m if r.get('ms')], 0.95),
    }
    if integ['hadGapRounds'] or integ['gapCountSum'] or integ['gapsInWindow'] or gp_undated:
        block('data_incomplete', {k: integ[k] for k in ('hadGapRounds', 'gapCountSum', 'gapsInWindow', 'gapsUndated')})
    if integ['stateCorruptRounds']:
        block('state_corrupt_in_window', {'rounds': integ['stateCorruptRounds']})
    if integ['pendingMax'] or integ['pendingOverflowMax']:
        block('pending_not_drained', {'pendingMax': integ['pendingMax'], 'overflow': integ['pendingOverflowMax']})

    # ---------- 信号 ----------
    cols = {r['tok'] for r in sg if r.get('assetType') == 'erc721' and r.get('gated')}
    sig = {
        'signals': len(sg),
        'assetType': dict(Counter(r.get('assetType') for r in sg)),
        'gatedTotal': sum(1 for r in sg if r.get('gated')),
        'gatedErc721': len(cols),
        'byClass': {str(k): v for k, v in Counter(r.get('signalClass') for r in sg).items()},
    }

    # ---------- 成交快照 ----------
    fills = js(a.fills)
    sales = {'available': bool(fills), 'path': a.fills}
    if not fills:
        block('fills_missing', {'path': a.fills})
    else:
        w = fills.get('window') or ((fills.get('agg') or {}).get('window') or {})
        gen = fills.get('generated') or (fills.get('agg') or {}).get('generated')
        age_h = None
        if gen:
            t = parse_ts(gen)
            age_h = round((now - t) / 3600, 2) if t else None
        sales.update({
            'generatedAt': gen, 'snapshotAgeHours': age_h,
            'fillsBlockRange': [w.get('fromBlock'), w.get('toBlock')],
            'fillsTimestampRange': [w.get('fromTimestamp'), w.get('toTimestamp')],
            'fillsDataComplete': w.get('dataComplete'),
            'fillsSkippedBlocks': w.get('skippedBlocks'),
        })
        agg = fills.get('agg') or {}
        sales.update({
            'fills': agg.get('fills_total'), 'fillsNative': agg.get('fills_native_bnb'),
            'collectionsTraded': agg.get('collections_traded'), 'volumeBnb': agg.get('volumeBnb'),
        })
        if not w:
            block('fills_window_missing', {'note': '成交快照没有窗口四要素，无法证明覆盖基线窗口'})
        else:
            if age_h is None or age_h > 13:
                block('fills_snapshot_stale', {'ageHours': age_h})
            if w.get('dataComplete') is False:
                block('fills_incomplete', {'skippedBlocks': w.get('skippedBlocks')})
            fb, tb = w.get('fromBlock'), w.get('toBlock')
            if None in (fb, tb, from_block, to_block):
                block('fills_coverage_unknown', {'fills': [fb, tb], 'baseline': [from_block, to_block]})
            else:
                need = max(0, to_block - from_block + 1)
                have = max(0, min(tb, to_block) - max(fb, from_block) + 1)
                cov = round(have / need, 4) if need else None
                sales['coverageRatio'] = cov
                if cov is None or cov < 0.95:
                    block('fills_coverage_insufficient', {'coverage': cov, 'needBlocks': need})

    # ---------- 成交价分布（EV 的稳健性：中位/P25/P75，不只看均值）----------
    dist = {}
    tr = fills.get('tracked') if fills else None
    if isinstance(tr, list):
        for r in tr:
            if not isinstance(r, dict) or not r.get('sales'):
                continue
            px = r.get('prices') or []
            if isinstance(px, list) and px:
                dist[r.get('sym') or r.get('collection')] = {
                    'sales': r.get('sales'), 'medianBnb': pct(px, 0.5),
                    'p25': pct(px, 0.25), 'p75': pct(px, 0.75),
                }

    out = {
        'status': 'BLOCKED' if blocked else 'OK',
        'blocked': blocked,
        'generated': iso(now),
        'window': {
            'fromTimestamp': iso(from_ts), 'toTimestamp': iso(to_ts),
            'windowHours': round((to_ts - from_ts) / 3600, 2),
            'fromBlock': from_block, 'toBlock': to_block,
            'blocks': (to_block - from_block + 1) if None not in (from_block, to_block) else None,
        },
        'versions': {'probe': vers, 'build': builds, 'signals': svers},
        'sourceFilters': {'metrics': 't', 'signals': 't', 'gaps': 't', 'fills': 'window.fromBlock/toBlock'},
        'integrity': integ,
        'signals': sig,
        'sales': sales,
        'priceDistribution': dist,
    }

    tmp = a.out + '.tmp'
    if blocked:
        bad = a.out.replace('.json', '') + '.invalid.json'
        json.dump(out, open(bad, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
        print('STATUS=BLOCKED  原因:')
        for b in blocked:
            print('  - %s %s' % (b['reason'], json.dumps(b['detail'], ensure_ascii=False)))
        print('已写 %s（不可作为基线使用）' % bad)
        sys.exit(2)

    body = json.dumps(out, ensure_ascii=False, indent=1)
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(body); f.flush(); os.fsync(f.fileno())
    os.replace(tmp, a.out)
    sha = hashlib.sha256(body.encode('utf-8')).hexdigest()
    open(a.out + '.sha256', 'w').write(sha + '\n')

    w = out['window']
    print('STATUS=OK')
    print('窗口 %s → %s (%.2fh)  block %s..%s  rounds=%d  version=%s build=%s' % (
        w['fromTimestamp'], w['toTimestamp'], w['windowHours'], w['fromBlock'], w['toBlock'],
        integ['rounds'], vers, builds))
    print('完整性 gap=%s pendingMax=%s zeroDis=%s lagMax=%s msAvg=%s' % (
        integ['hadGapRounds'], integ['pendingMax'], integ['zeroDisagreements'], integ['lagMax'], integ['msAvg']))
    print('集合 gatedErc721=%s  signals=%s  class=%s' % (sig['gatedErc721'], sig['signals'], sig['byClass']))
    print('成交 fills=%s collections=%s vol=%s coverage=%s' % (
        sales.get('fills'), sales.get('collectionsTraded'), sales.get('volumeBnb'), sales.get('coverageRatio')))
    print('已写 %s  sha256=%s…' % (a.out, sha[:16]))


if __name__ == '__main__':
    main()
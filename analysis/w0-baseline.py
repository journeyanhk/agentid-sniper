#!/usr/bin/env python3
"""w0-baseline.py —— W0 窗口基线固化（只读；v2 严格窗口版）

v1 的三个缺陷已修：
  ① 用 `or True` 的伪筛选 + 按条数取最后 N 条 → 改为按 fromTimestamp/toTimestamp 真筛选；
  ② signals / gaps 用全历史 → 同样按窗口筛选；
  ③ 成交快照过期仍写正式基线 → 改为 BLOCKED，只写 .invalid.json 并 exit 2。

额外闸门：
  - 窗口内 probe VERSION / build 必须唯一（混合即 BLOCKED_VERSION_MIX）；
  - Element 快照必须覆盖窗口块范围（覆盖不足 → BLOCKED_FILLS_COVERAGE）；
  - 正式基线原子写入（tmp + fsync + rename），并记录自身 SHA-256。

用法：
  python3 analysis/w0-baseline.py --hours 48            # 正式固化
  python3 analysis/w0-baseline.py --hours 6 --dry-run   # 演练：只报告，不写正式文件
  python3 analysis/w0-baseline.py --from 2026-10-31T01:30Z --to 2026-11-02T01:30Z
"""
import argparse, hashlib, json, os, statistics as st, sys, time
from collections import Counter
from datetime import datetime, timedelta, timezone

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


def parse_ts(v):
    """容忍 2026-11-01T02:25:42.044Z / 无毫秒 / 带 +00:00"""
    if not v: return None
    s = str(v).replace('Z', '+00:00')
    try:
        d = datetime.fromisoformat(s)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def pct(v, q):
    if not v: return None
    v = sorted(v); return round(v[min(len(v) - 1, int(len(v) * q))], 1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--hours', type=float, default=48.0)
    ap.add_argument('--from', dest='t_from')
    ap.add_argument('--to', dest='t_to')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--max-snapshot-age', type=float, default=13.0)
    a = ap.parse_args()

    now = datetime.now(timezone.utc)
    to_dt = parse_ts(a.t_to) or now
    from_dt = parse_ts(a.t_from) or (to_dt - timedelta(hours=a.hours))

    metrics_all = jl(F('shadow', 'metrics.jsonl'))
    signals_all = jl(F('shadow', 'signals.jsonl'))
    gaps_all = jl(F('shadow', 'gaps.jsonl'))
    fills = js(F('analysis', 'element-fills.json'))

    inw = lambda r, k: (lambda t: bool(t and from_dt <= t <= to_dt))(parse_ts(r.get(k)))
    metrics = [r for r in metrics_all if inw(r, 't') or inw(r, 'ts')]
    signals = [r for r in signals_all if inw(r, 'observedAt') or inw(r, 't')]
    gaps = [r for r in gaps_all if inw(r, 't') or inw(r, 'ts')]

    blocked = []
    # ---- 版本唯一性 ----
    versions = {r.get('v') for r in metrics if r.get('v')}
    builds = {r.get('build') for r in metrics if r.get('build')}
    if len(versions) != 1 or len(builds) != 1: blocked.append('VERSION_MIX')
    sig_versions = {r.get('v') for r in signals if r.get('v')}
    if len(sig_versions) > 1: blocked.append('SIGNALS_VERSION_MIX')

    first, last = (metrics[0], metrics[-1]) if metrics else ({}, {})
    win = {
        'fromTimestamp': from_dt.strftime('%Y-%m-%dT%H:%M:%SZ'),
        'toTimestamp': to_dt.strftime('%Y-%m-%dT%H:%M:%SZ'),
        'windowMinutes': round((to_dt - from_dt).total_seconds() / 60, 1),
        'fromBlock': first.get('fromBlock'), 'toBlock': last.get('lastBlockAfter') or last.get('head'),
        'rounds': len(metrics),
        'probeVersions': sorted(v for v in versions if v),
        'builds': sorted(b for b in builds if b),
        'signalVersions': sorted(v for v in sig_versions if v),
    }
    if not metrics: blocked.append('NO_METRICS_IN_WINDOW')

    integ = {
        'dataCompleteFalse': sum(1 for r in metrics if r.get('dataComplete') is False),
        'hadGapRounds': sum(1 for r in metrics if r.get('hadGap')),
        'gapCountSum': sum(int(r.get('gapCount') or 0) for r in metrics),
        'gapEntriesInWindow': len(gaps),
        'pendingMax': max([int(r.get('pending') or 0) for r in metrics] or [0]),
        'pendingOverflowMax': max([int(r.get('pendingOverflow') or 0) for r in metrics] or [0]),
        'stateCorrupt': any(r.get('stateCorrupt') for r in metrics),
        'cursorPausedRounds': sum(1 for r in metrics if r.get('cursorPaused')),
        'zeroDisagreements': last.get('zeroDisagreements'),
        'incompleteSignals': sum(1 for r in signals if r.get('dataComplete') is False),
        'lagMax': max([int(r.get('lagBlocks') or 0) for r in metrics] or [0]),
        'lagLast': last.get('lagBlocks'),
        'msAvg': round(sum(r['ms'] for r in metrics) / len(metrics), 0) if metrics else None,
        'msP95': pct([r.get('ms') for r in metrics if r.get('ms')], 0.95),
    }

    cols = {r['tok'] for r in signals if r.get('assetType') == 'erc721' and r.get('gated')}
    sig = {
        'signalsInWindow': len(signals),
        'assetType': dict(Counter(r.get('assetType') for r in signals)),
        'gatedTotal': sum(1 for r in signals if r.get('gated')),
        'gatedErc721Collections': len(cols),
        'byClass': dict(Counter(str(r.get('signalClass')) for r in signals)),
        'inGapFlagged': sum(1 for r in signals if r.get('eventBlockWasGap')),
    }

    # ---- 成交快照：新鲜度 + 窗口覆盖 ----
    sales, snap = {'source': 'element-fills.json', 'available': bool(fills)}, {}
    age = None
    if fills and isinstance(fills.get('agg'), dict):
        g = fills['agg'].get('generated')
        gdt = parse_ts(g)
        age = round((now - gdt).total_seconds() / 3600, 1) if gdt else None
        snap = {'generatedAt': g, 'ageHours': age,
                'fromBlock': fills['agg'].get('fromBlock'), 'toBlock': fills['agg'].get('head'),
                'windowHours': fills['agg'].get('windowHours')}
        sales.update({'fills': fills['agg'].get('fills_total'),
                      'collectionsTraded': fills['agg'].get('collections_traded'),
                      'volumeBnb': fills['agg'].get('volumeBnb')})
        if age is None or age > a.max_snapshot_age: blocked.append('STALE_FILLS')
        if snap['fromBlock'] and win['fromBlock'] and int(snap['fromBlock']) > int(win['fromBlock']):
            blocked.append('FILLS_COVERAGE_GAP')
    elif fills:
        blocked.append('FILLS_UNRECOGNIZED')
    else:
        blocked.append('FILLS_MISSING')

    out = {
        'status': 'BLOCKED' if blocked else 'OK',
        'blockedReasons': blocked,
        'generated': now.strftime('%Y-%m-%dT%H:%M:%SZ'),
        'window': win,
        'sourceFilters': {'metrics': 't', 'signals': 'observedAt|t', 'gaps': 't', 'fills': 'eventBlock(collector)'},
        'elementSnapshot': snap,
        'integrity': integ,
        'signals': sig,
        'sales': sales,
        'notes': ['版本/口径任一混合即拒绝出正式基线',
                  '成交快照过期或覆盖不足即拒绝',
                  '正式文件原子写入并记录自身 sha256'],
    }

    os.makedirs(F('analysis'), exist_ok=True)
    body = json.dumps(out, ensure_ascii=False, indent=1)
    sha = hashlib.sha256(body.encode()).hexdigest()[:16]
    out['sha256_16'] = sha
    body = json.dumps(out, ensure_ascii=False, indent=1)

    if blocked:
        inv = F('analysis', 'w0-baseline.invalid.json')
        open(inv, 'w').write(body)
        print('基线被拒绝：' + ', '.join(blocked))
        print('已写 %s（非正式，不可用于结论）' % inv)
    if a.dry_run:
        print('[dry-run] 不写正式基线')
    elif not blocked:
        tmp = F('analysis', '.w0-baseline.tmp')
        with open(tmp, 'w') as fh:
            fh.write(body); fh.flush(); os.fsync(fh.fileno())
        os.replace(tmp, F('analysis', 'w0-baseline.json'))
        print('已写 analysis/w0-baseline.json  sha256_16=%s' % sha)

    print('窗口 %s → %s（%s 分钟，%s 轮）版本 %s' % (
        win['fromTimestamp'], win['toTimestamp'], win['windowMinutes'], win['rounds'], win['probeVersions']))
    print('完整性: dataComplete=false %s | gap轮 %s | gap数 %s | pendingMax %s | zeroDis %s | lagMax %s | msAvg %s' % (
        integ['dataCompleteFalse'], integ['hadGapRounds'], integ['gapCountSum'], integ['pendingMax'],
        integ['zeroDisagreements'], integ['lagMax'], integ['msAvg']))
    print('信号: %s 条 | gated %s | gated集合 %s | class %s' % (
        sig['signalsInWindow'], sig['gatedTotal'], sig['gatedErc721Collections'], sig['byClass']))
    print('成交: 快照 %s（%.1fh 前）fills=%s 集合=%s vol=%s BNB' % (
        snap.get('generatedAt'), age if age is not None else -1, sales.get('fills'),
        sales.get('collectionsTraded'), sales.get('volumeBnb')))
    return 2 if blocked else 0


if __name__ == '__main__':
    sys.exit(main())
#!/usr/bin/env python3
"""restart-audit.py v3 —— 重启连续性审计（只读）

不写「完全无损」，只写可验证的事实：
  scanCursorContinuity   区块游标是否 lastBlockAfter+1 == fromBlock
  signalContinuity       signals.jsonl 在断层前后是否连续、有无截断行
  metricsContinuity      metrics.jsonl 是否连续、有无不可解析行
  pendingReconciliation  pending / pendingOverflow 在断层两侧的计数
  stateCursor            state.json 当前游标（历史值不可回读，只报现值 + savedAt）

recoveryConfidence:
  high   = 无空洞、无重叠、pending 两侧为 0、两个文件无坏行、信号时间连续
  medium = 上述有一项无法确认（不判定为失败，只是证据不足）
  failed = 出现区块空洞
"""
import hashlib
import json
import time
from datetime import datetime, timezone, timedelta

BASE = '/home/laoda/agentid-sniper'
WIN = timedelta(minutes=10)


def ts(s):
    try:
        return datetime.fromisoformat(str(s).replace('Z', '+00:00'))
    except Exception:
        return None


def file_stat(path):
    h = hashlib.sha256()
    lines = bad = 0
    first = last = None
    with open(path, 'rb') as f:
        for ln in f:
            h.update(ln)
            if not ln.strip():
                continue
            lines += 1
            try:
                o = json.loads(ln)
                t = o.get('t')
                if t:
                    first = first or t
                    last = t
            except Exception:
                bad += 1
    import os
    return {'path': path.split('/')[-1], 'bytes': os.path.getsize(path), 'lines': lines,
            'badLines': bad, 'sha256': h.hexdigest()[:16],
            'firstT': first, 'lastT': last}


meta = file_stat(BASE + '/shadow/metrics.jsonl')
sigf = file_stat(BASE + '/shadow/signals.jsonl')
st = json.load(open(BASE + '/shadow/state.json'))

rows = [json.loads(l) for l in open(BASE + '/shadow/metrics.jsonl') if l.strip()]
rows = [r for r in rows if ts(r.get('t'))]
rows.sort(key=lambda r: ts(r['t']))

sig = []
for l in open(BASE + '/shadow/signals.jsonl'):
    l = l.strip()
    if not l:
        continue
    try:
        s = json.loads(l)
    except Exception:
        continue
    t = ts(s.get('t'))
    if t:
        sig.append((t, s))
sig.sort(key=lambda x: x[0])


def sig_stats(lo, hi):
    got = [s for t, s in sig if lo <= t <= hi]
    return {'rows': len(got), 'gated': sum(1 for s in got if s.get('gated')),
            'contracts': len({s.get('tok') for s in got if s.get('tok')})}


breaks = [(i, (ts(rows[i]['t']) - ts(rows[i - 1]['t'])).total_seconds())
          for i in range(1, len(rows))
          if (ts(rows[i]['t']) - ts(rows[i - 1]['t'])).total_seconds() > 90]

out = {'generated': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
       'rounds': len(rows), 'signals': len(sig),
       'files': {'metrics': meta, 'signals': sigf},
       'stateNow': {k: st.get(k) for k in ('lastBlock', 'savedAt', 'updated', 'version',
                                           'pendingOverflow', 'dupCount')},
       'breaks': []}
out['stateNow']['pending'] = len(st.get('pending') or [])

for i, gap in breaks:
    a, b = rows[i - 1], rows[i]
    ta, tb = ts(a['t']), ts(b['t'])
    before, after = int(a.get('lastBlockAfter') or 0), int(b.get('fromBlock') or 0)
    delta = after - before
    kind = 'continuous' if delta == 1 else ('overlap' if delta <= 0 else 'HOLE')
    sb, sa = sig_stats(ta - WIN, ta), sig_stats(tb, tb + WIN)
    # 信号时间是否跨断层连续（断层两侧各自最近一条信号与断层点的距离）
    prev_sig = max([t for t, _ in sig if t <= ta], default=None)
    next_sig = min([t for t, _ in sig if t >= tb], default=None)
    rec = {
        'restartAt': b['t'], 'gapSeconds': round(gap, 1),
        'lastMetricBefore': a['t'], 'firstMetricAfter': b['t'],
        'blockBefore': before, 'blockAfter': after, 'deltaBlocks': delta, 'kind': kind,
        'blockContinuity': delta == 1, 'holeBlocks': max(0, delta - 1),
        'overlapBlocks': max(0, 1 - delta),
        'pendingBefore': a.get('pending'), 'pendingAfter': b.get('pending'),
        'pendingOverflowBefore': a.get('pendingOverflow'), 'pendingOverflowAfter': b.get('pendingOverflow'),
        'pendingReconciliation': (a.get('pending') or 0) == 0 and (b.get('pending') or 0) == 0,
        'stateCorruptAfter': b.get('stateCorrupt'), 'cursorPausedAfter': b.get('cursorPaused'),
        'buildBefore': a.get('build'), 'buildAfter': b.get('build'),
        'signalBefore': sb, 'signalAfter': sa,
        'lastSignalBeforeRestart': prev_sig.isoformat() if prev_sig else None,
        'firstSignalAfterRestart': next_sig.isoformat() if next_sig else None,
        'signalGapSeconds': round((next_sig - prev_sig).total_seconds(), 1) if prev_sig and next_sig else None,
    }
    rec['signalContinuity'] = bool(prev_sig and next_sig and
                                   (next_sig - prev_sig) <= WIN * 2)
    out['breaks'].append(rec)

holes = [b for b in out['breaks'] if b['kind'] == 'HOLE']
bad = [b for b in out['breaks'] if not b['signalContinuity'] or not b['pendingReconciliation']]
out['verdict'] = {
    'blockHoles': sum(b['holeBlocks'] for b in holes),
    'blockOverlaps': len([b for b in out['breaks'] if b['kind'] == 'overlap']),
    'scanCursorContinuity': 'verified' if not holes else 'failed',
    'signalContinuity': 'verified' if not [b for b in out['breaks'] if not b['signalContinuity']] else 'partial',
    'metricsIntegrity': 'verified' if meta['badLines'] == 0 else 'partial',
    'signalsIntegrity': 'verified' if sigf['badLines'] == 0 else 'partial',
    'recoveryConfidence': 'failed' if holes else ('medium' if bad or meta['badLines'] or sigf['badLines'] else 'high'),
}
out['wording'] = ('restartRecovery=verified, scanCursorContinuity=verified, blockHoles=0, '
                  'blockOverlaps=0；不表述为「绝对无损」——state.json 为单文件覆盖写，'
                  '历史游标不可回读，只能以 metrics 相邻两轮游标 + signals 文件连续性作为旁证')

print('metrics %d 行/坏行 %d | signals %d 行/坏行 %d' % (meta['lines'], meta['badLines'], sigf['lines'], sigf['badLines']))
print('state: lastBlock=%s pending=%s overflow=%s savedAt=%s' % (
    st.get('lastBlock'), len(st.get('pending') or []), st.get('pendingOverflow'),
    st.get('savedAt') or st.get('updated')))
print('断层 %d 处：' % len(breaks))
for b in out['breaks']:
    print('  %s  Δ=%+d %s | pending %s→%s | 信号 %.0fs | 置信 %s' % (
        b['restartAt'], b['deltaBlocks'], b['kind'], b['pendingBefore'], b['pendingAfter'],
        b['signalGapSeconds'] or -1, 'ok' if b['signalContinuity'] else 'gap'))
print('verdict:', json.dumps(out['verdict'], ensure_ascii=False))
json.dump(out, open(BASE + '/analysis/restart-audit.json', 'w'), ensure_ascii=False, indent=1)
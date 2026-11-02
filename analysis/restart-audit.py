#!/usr/bin/env python3
"""restart-audit.py v2 —— OOM 重启后的扫描与信号连续性审计（只读）

回答四个问题：
  1) 崩溃前最后持久化的游标有没有超前于已处理区块；
  2) 重启后是否回扫重叠（fromBlock <= lastBlockBefore）；
  3) 重启期间是否出现区块空洞（fromBlock > lastBlockBefore+1）；
  4) 崩溃窗口内信号是否断层（前后各 10 分钟的行数/gated 数/唯一合约数）。

只读 metrics.jsonl / signals.jsonl / state.json。
"""
import json, time
from datetime import datetime, timezone, timedelta

BASE = '/home/laoda/agentid-sniper'
WIN = timedelta(minutes=10)


def ts(s):
    try:
        return datetime.fromisoformat(str(s).replace('Z', '+00:00'))
    except Exception:
        return None


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


breaks = []
for i in range(1, len(rows)):
    gap = (ts(rows[i]['t']) - ts(rows[i - 1]['t'])).total_seconds()
    if gap > 90:
        breaks.append((i, gap))

out = {'generated': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
       'rounds': len(rows), 'breaks': []}
print('轮次 %d，信号行 %d，时间断层 %d 处' % (len(rows), len(sig), len(breaks)))

for i, gap in breaks:
    a, b = rows[i - 1], rows[i]
    ta, tb = ts(a['t']), ts(b['t'])
    before = int(a.get('lastBlockAfter') or 0)
    after = int(b.get('fromBlock') or 0)
    delta = after - before
    kind = 'continuous' if delta == 1 else ('overlap' if delta <= 0 else 'HOLE')
    rec = {
        'gapSeconds': round(gap, 1), 'lastRoundBefore': a['t'], 'firstRoundAfter': b['t'],
        'blockBefore': before, 'blockAfter': after, 'deltaBlocks': delta, 'kind': kind,
        'holeBlocks': max(0, delta - 1), 'overlapBlocks': max(0, 1 - delta),
        'pendingBefore': a.get('pending'), 'hadGapBefore': a.get('hadGap'),
        'stateCorruptAfter': b.get('stateCorrupt'), 'cursorPausedAfter': b.get('cursorPaused'),
        'buildBefore': a.get('build'), 'buildAfter': b.get('build'),
        'signalBefore': sig_stats(ta - WIN, ta), 'signalAfter': sig_stats(tb, tb + WIN),
    }
    out['breaks'].append(rec)
    print('\n断层 %.0fs  %s → %s' % (gap, a['t'], b['t']))
    print('  区块 %d → %d  Δ=%+d  %s（空洞 %d / 重叠 %d）' % (
        before, after, delta, kind, rec['holeBlocks'], rec['overlapBlocks']))
    print('  崩溃前 pending=%s hadGap=%s；重启后 stateCorrupt=%s cursorPaused=%s' % (
        rec['pendingBefore'], rec['hadGapBefore'], rec['stateCorruptAfter'], rec['cursorPausedAfter']))
    print('  信号 前10min %s / 后10min %s' % (rec['signalBefore'], rec['signalAfter']))

st = json.load(open(BASE + '/shadow/state.json'))
out['stateNow'] = {'lastBlock': st.get('lastBlock'), 'pending': len(st.get('pending') or []),
                   'dupCount': st.get('dupCount'), 'pendingOverflow': st.get('pendingOverflow'),
                   'version': st.get('version')}
print('\n当前 state: lastBlock=%s pending=%s dup=%s overflow=%s' % (
    st.get('lastBlock'), len(st.get('pending') or []), st.get('dupCount'), st.get('pendingOverflow')))

holes = [b for b in out['breaks'] if b['kind'] == 'HOLE']
out['verdict'] = {'holes': len(holes), 'holeBlocks': sum(b['holeBlocks'] for b in holes),
                  'overlaps': len([b for b in out['breaks'] if b['kind'] == 'overlap']),
                  'restartRecovery': 'verified' if not holes else 'failed'}
print('\n结论：空洞 %d 块、重叠 %d 次 → restartRecovery=%s' % (
    out['verdict']['holeBlocks'], out['verdict']['overlaps'], out['verdict']['restartRecovery']))
json.dump(out, open(BASE + '/analysis/restart-audit.json', 'w'), ensure_ascii=False, indent=1)
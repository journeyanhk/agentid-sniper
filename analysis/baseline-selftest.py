#!/usr/bin/env python3
"""baseline-selftest.py —— 证明确实跑过拒绝测试（结果写 analysis/baseline-selftest.json）

用临时 fixture 跑 5 个场景，断言 w0-baseline.py 的行为：
  1. 版本混合            → BLOCKED + G3_VERSION_MIX
  2. 快照过期            → BLOCKED + G1_FILLS_STALE
  3. Element 有 skipped  → BLOCKED + G1_FILLS_INCOMPLETE
  4. 缺 window 字段      → BLOCKED + G1_NO_ELEMENT_WINDOW
  5. 单版本 + 完整覆盖    → READY_FOR_DECISION

只读写临时目录，不碰 shadow/ 真实数据。
"""
import json, os, shutil, subprocess, sys, tempfile, time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(HERE)
PY = sys.executable

FROM_BLOCK, TO_BLOCK = 130700000, 130892000
FROM_TS, TO_TS = '2026-10-31T01:30:00Z', '2026-11-01T01:30:00Z'


def iso_now(offset_h=0):
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(time.time() - offset_h * 3600))


def at(frac):
    """窗口内时间点（fixture 必须落在 [FROM_TS, TO_TS] 内，否则会被窗口过滤掉）"""
    a = datetime.strptime(FROM_TS, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc).timestamp()
    b = datetime.strptime(TO_TS, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc).timestamp()
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(a + (b - a) * frac))


def metrics_rows(v='probe-4.0.0', build='d0d27bc', n=3, lag=2, gap=0):
    rows = []
    for i in range(n):
        fb = FROM_BLOCK + int((TO_BLOCK - FROM_BLOCK) * i / max(1, n - 1))
        rows.append({'t': at(i / max(1, n - 1)),
                     'v': v, 'build': build, 'fromBlock': fb, 'lastBlockAfter': fb + 500,
                     'head': fb + 500, 'lagBlocks': lag, 'hadGap': gap > 0, 'gapCount': gap,
                     'pending': 0, 'pendingOverflow': 0, 'stateCorrupt': False, 'cursorPaused': False,
                     'dataComplete': gap == 0, 'ms': 600, 'zeroDisagreements': 0})
    rows[-1]['lastBlockAfter'] = TO_BLOCK
    rows[-1]['head'] = TO_BLOCK
    return rows


def budget(v='probe-4.0.0', generated_h_ago=0.2, complete=True, skipped=None, with_window=True):
    agg = {'generated': iso_now(generated_h_ago), 'fills_total': 10, 'fills_native_bnb': 9,
           'collections_traded': 2, 'volumeBnb': 0.5, 'tracked_collections': 5,
           'tracked_with_sales': 1, 'tracked_sales': 3, 'tracked_volumeBnb': 0.2,
           'coverage': {'dataComplete': complete, 'requestedRanges': 385, 'scannedRanges': 385 if complete else 380,
                        'skippedRanges': skipped or [], 'skippedBlocks': sum(
                            s['toBlock'] - s['fromBlock'] + 1 for s in (skipped or [])),
                        'retryCount': 2, 'coverageRatio': 1.0 if complete else 0.97, 'rpcSources': 2}}
    if with_window:
        agg['window'] = {'fromBlock': FROM_BLOCK, 'toBlock': TO_BLOCK, 'fromTimestamp': FROM_TS,
                         'toTimestamp': TO_TS, 'windowMinutes': 1440.0}
    return {'agg': agg, 'tracked': [], 'top_collections': []}


def build_case(root, metrics, fills):
    os.makedirs(os.path.join(root, 'shadow'), exist_ok=True)
    os.makedirs(os.path.join(root, 'analysis'), exist_ok=True)
    for name, rows in (('metrics.jsonl', metrics), ('signals.jsonl', []), ('gaps.jsonl', [])):
        with open(os.path.join(root, 'shadow', name), 'w') as f:
            for r in rows:
                f.write(json.dumps(r) + '\n')
    shutil.copy(os.path.join(BASE, 'shadow', 'probe.mjs'), os.path.join(root, 'shadow', 'probe.mjs'))
    p = os.path.join(root, 'analysis', 'element-fills.json')
    json.dump(fills, open(p, 'w'))
    return p


def run_case(name, metrics, fills, expect_code=None, expect_ready=False):
    root = tempfile.mkdtemp(prefix='w0sel-')
    try:
        fp = build_case(root, metrics, fills)
        r = subprocess.run([PY, os.path.join(BASE, 'analysis', 'w0-baseline.py'),
                            '--root', root, '--fills', fp, '--out', os.path.join(root, 'analysis', 'b.json')],
                           capture_output=True, text=True, timeout=120)
        out = os.path.join(root, 'analysis', 'b.json')
        inv = os.path.join(root, 'analysis', 'b.invalid.json')
        data = json.load(open(out)) if os.path.exists(out) else (json.load(open(inv)) if os.path.exists(inv) else {})
        codes = [b['code'] for b in data.get('blocked', [])]
        ok = (data.get('status') == 'READY_FOR_DECISION') if expect_ready else (expect_code in codes and r.returncode == 2)
        return {'case': name, 'pass': ok, 'exit': r.returncode, 'status': data.get('status'),
                'blocked': codes, 'expect': 'READY' if expect_ready else expect_code}
    finally:
        shutil.rmtree(root, ignore_errors=True)


def main():
    cases = []
    cases.append(run_case('01_version_mix', metrics_rows(v='probe-4.0.0') + metrics_rows(v='probe-3.0.0'),
                          budget(), 'G3_VERSION_MIX'))
    cases.append(run_case('02_fills_stale', metrics_rows(), budget(generated_h_ago=30), 'G1_FILLS_STALE'))
    cases.append(run_case('03_fills_incomplete', metrics_rows(),
                          budget(complete=False, skipped=[{'fromBlock': 130800000, 'toBlock': 130800024,
                                                           'status': 'skipped', 'reason': 'all_rpc_failed',
                                                           'attempts': 4}]), 'G1_FILLS_INCOMPLETE'))
    cases.append(run_case('04_no_window', metrics_rows(), budget(with_window=False), 'G1_NO_ELEMENT_WINDOW'))
    cases.append(run_case('05_ready', metrics_rows(), budget(), expect_ready=True))

    fail = [c for c in cases if not c['pass']]
    out = {'at': iso_now(), 'cases': cases, 'fail': len(fail)}
    dest = os.path.join(BASE, 'analysis', 'baseline-selftest.json')
    json.dump(out, open(dest, 'w'), ensure_ascii=False, indent=1)
    for c in cases:
        print('%s %s exit=%s status=%s blocked=%s' % ('PASS' if c['pass'] else 'FAIL', c['case'],
                                                      c['exit'], c['status'], ','.join(c['blocked'][:4])))
    print('已写 %s' % dest)
    return 1 if fail else 0


if __name__ == '__main__':
    sys.exit(main())
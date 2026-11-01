#!/usr/bin/env python3
"""w0-baseline.py v3 —— W0 基线的唯一生成入口（只读 + 全部闸门）

窗口只有一个来源：Element 成交快照自带的 window.fromBlock/toBlock（单一事实源），
metrics / signals / gaps 全部按这个窗口过滤，避免各数据源各算各的。

闸门（任一不满足即 BLOCKED，只写 *.invalid.json，exit 2）：
  G1  成交快照有 window 且 coverage.dataComplete=true、skippedRanges 为空
  G2  metrics 的块覆盖包含窗口两端
  G3  窗口内 probe version / build 唯一
  G4  dataComplete=false / pending / gaps / stateCorrupt 全为 0
  G5  probe 文件 SHA-256 与 git 提交可记录（来源标注 working-tree）
  G6  输入文件 hash 全部记录

统计口径：信号按行数、唯一合约、唯一事件三种分母分开输出，禁止混用。

用法：
  python3 analysis/w0-baseline.py --fills analysis/element-fills-w0.json
  python3 analysis/w0-baseline.py --hours 24 --dry-run
  python3 analysis/w0-baseline.py --root /tmp/fixture --fills ... (自检用)
"""
import argparse, hashlib, json, os, subprocess, sys, time
from collections import Counter
from datetime import datetime, timedelta, timezone

DEF_ROOT = os.path.expanduser('~/agentid-sniper')
TOL_BLOCKS = 50
LAG_WARN, LAG_DEGRADED, LAG_FAIL = 600, 1200, 2400
BLOCKS_PER_HOUR = 8000   # BSC 实测约 8000 块/小时


def parse_ts(v):
    if not v:
        return None
    s = str(v).replace('Z', '+00:00')
    try:
        d = datetime.fromisoformat(s)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def jl(path):
    if not os.path.exists(path):
        return []
    out = []
    for line in open(path, encoding='utf-8', errors='ignore'):
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


def file_sig(path):
    if not os.path.exists(path):
        return None
    h = hashlib.sha256()
    n = 0
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk); n += len(chunk)
    return {'sha256': h.hexdigest(), 'bytes': n}


def pct(v, q):
    if not v:
        return None
    v = sorted(v)
    return round(v[min(len(v) - 1, int(len(v) * q))], 1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', default=DEF_ROOT)
    ap.add_argument('--fills')
    ap.add_argument('--hours', type=float, default=48.0)
    ap.add_argument('--from', dest='t_from')
    ap.add_argument('--to', dest='t_to')
    ap.add_argument('--out', default=None)
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--max-snapshot-age', type=float, default=13.0)
    a = ap.parse_args()

    root = os.path.abspath(os.path.expanduser(a.root))
    P = lambda *p: os.path.join(root, *p)
    fills_path = a.fills or P('analysis', 'element-fills.json')
    out_path = a.out or P('analysis', 'w0-baseline.json')

    now = datetime.now(timezone.utc)
    fills = js(fills_path) or {}
    agg = fills.get('agg') or {}
    fwin = agg.get('window') or {}
    cov = agg.get('coverage') or {}

    blocked = []
    def blk(code, detail=None):
        blocked.append({'code': code, 'detail': detail or {}})

    # ---------- 窗口：优先 Element 快照 ----------
    if fwin.get('fromBlock') and fwin.get('toBlock'):
        from_block, to_block = int(fwin['fromBlock']), int(fwin['toBlock'])
        from_dt = parse_ts(fwin.get('fromTimestamp'))
        to_dt = parse_ts(fwin.get('toTimestamp'))
        window_source = 'element-fills'
    else:
        to_dt = parse_ts(a.t_to) or now
        from_dt = parse_ts(a.t_from) or (to_dt - timedelta(hours=a.hours))
        from_block = to_block = None
        window_source = 'args'
        blk('G1_NO_ELEMENT_WINDOW', {'fills': fills_path})

    # ---------- 读取并按窗口过滤 ----------
    metrics_all = jl(P('shadow', 'metrics.jsonl'))
    signals_all = jl(P('shadow', 'signals.jsonl'))
    gaps_all = jl(P('shadow', 'gaps.jsonl'))

    def m_in(r):
        t = parse_ts(r.get('t') or r.get('ts'))
        if not t or not (from_dt and to_dt and from_dt <= t <= to_dt):
            return False
        if from_block is not None:
            fb = r.get('fromBlock'); lb = r.get('lastBlockAfter') or r.get('head')
            if fb is None or lb is None:
                return False
            if int(lb) < from_block or int(fb) > to_block:
                return False
        return True

    def s_in(r):
        t = parse_ts(r.get('observedAt') or r.get('t'))
        if not t or not (from_dt and to_dt and from_dt <= t <= to_dt):
            return False
        eb = r.get('eventBlock')
        if from_block is not None and isinstance(eb, int):
            if eb < from_block - TOL_BLOCKS or eb > to_block + TOL_BLOCKS:
                return False
        return True

    metrics = [r for r in metrics_all if m_in(r)]
    signals = [r for r in signals_all if s_in(r)]
    gaps = [r for r in gaps_all if (lambda t: bool(t and from_dt and to_dt and from_dt <= t <= to_dt))(
        parse_ts(r.get('t') or r.get('at') or r.get('ts')))]

    if not metrics:
        blk('G2_NO_METRICS_IN_WINDOW')

    # ---------- G1：Element 覆盖 ----------
    if not fills:
        blk('G1_FILLS_MISSING', {'path': fills_path})
    else:
        age = None
        g = agg.get('generated')
        if g and parse_ts(g):
            age = round((now - parse_ts(g)).total_seconds() / 3600, 2)
        if age is None or age > a.max_snapshot_age:
            blk('G1_FILLS_STALE', {'ageHours': age})
        if not cov:
            blk('G1_NO_COVERAGE_BLOCK')
        else:
            if cov.get('dataComplete') is not True:
                blk('G1_FILLS_INCOMPLETE', {'skippedRanges': len(cov.get('skippedRanges') or []),
                                            'skippedBlocks': cov.get('skippedBlocks')})
            if cov.get('skippedRanges'):
                blk('G1_SKIPPED_RANGES', {'ranges': cov['skippedRanges'][:5]})
            if (cov.get('coverageRatio') or 0) < 0.999:
                blk('G1_COVERAGE_RATIO', {'ratio': cov.get('coverageRatio')})
        # 块覆盖必须严格等于窗口（窗口本来就取自快照）
        if from_block is not None and (fwin.get('fromBlock') != from_block or fwin.get('toBlock') != to_block):
            blk('G1_WINDOW_MISMATCH')

    # ---------- G2：metrics 块覆盖 ----------
    if metrics and from_block is not None:
        first_fb = min(int(r.get('fromBlock') or 0) for r in metrics)
        last_lb = max(int(r.get('lastBlockAfter') or r.get('head') or 0) for r in metrics)
        if first_fb > from_block + TOL_BLOCKS:
            blk('G2_METRICS_START_LATE', {'firstFromBlock': first_fb, 'windowFrom': from_block})
        if last_lb < to_block - TOL_BLOCKS:
            blk('G2_METRICS_END_EARLY', {'lastBlock': last_lb, 'windowTo': to_block})

    # ---------- G3：版本唯一 ----------
    versions = sorted({r.get('v') for r in metrics if r.get('v')})
    builds = sorted({r.get('build') for r in metrics if r.get('build')})
    schemas = sorted({r.get('schemaVersion') for r in metrics if r.get('schemaVersion')})
    collectors = sorted({r.get('collectorVersion') for r in metrics if r.get('collectorVersion')})
    probe_shas = sorted({r.get('probeSha256') for r in metrics if r.get('probeSha256')})
    if len(versions) != 1:
        blk('G3_VERSION_MIX', {'versions': versions})
    if len(builds) != 1:
        blk('G3_BUILD_MIX', {'builds': builds})
    if len(schemas) > 1 or len(collectors) > 1 or len(probe_shas) > 1:
        blk('G3_META_MIX', {'schema': schemas, 'collector': collectors, 'probeSha': [x[:12] for x in probe_shas]})

    # ---------- G4：完整性 ----------
    integ = {
        'rounds': len(metrics),
        'incompleteRounds': sum(1 for r in metrics if r.get('dataComplete') is False),
        'hadGapRounds': sum(1 for r in metrics if r.get('hadGap')),
        'gapCountSum': sum(int(r.get('gapCount') or 0) for r in metrics),
        'gapsInWindow': len(gaps),
        'pendingMax': max([int(r.get('pending') or 0) for r in metrics] or [0]),
        'pendingOverflowMax': max([int(r.get('pendingOverflow') or 0) for r in metrics] or [0]),
        'stateCorruptRounds': sum(1 for r in metrics if r.get('stateCorrupt')),
        'cursorPausedRounds': sum(1 for r in metrics if r.get('cursorPaused')),
        'zeroDisagreements': (metrics[-1].get('zeroDisagreements') if metrics else None),
        'msAvg': round(sum(r.get('ms') or 0 for r in metrics) / len(metrics), 0) if metrics else None,
        'msP95': pct([r.get('ms') for r in metrics if r.get('ms')], 0.95),
    }
    if integ['incompleteRounds'] or integ['hadGapRounds'] or integ['gapCountSum'] or integ['gapsInWindow']:
        blk('G4_DATA_INCOMPLETE', {k: integ[k] for k in ('incompleteRounds', 'hadGapRounds', 'gapCountSum', 'gapsInWindow')})
    if integ['pendingMax'] or integ['pendingOverflowMax']:
        blk('G4_PENDING', {'max': integ['pendingMax'], 'overflow': integ['pendingOverflowMax']})
    if integ['stateCorruptRounds']:
        blk('G4_STATE_CORRUPT', {'rounds': integ['stateCorruptRounds']})

    # ---------- dataFresh：lag 与完整性分开 ----------
    lags = [int(r.get('lagBlocks') or 0) for r in metrics] or [0]
    max_lag = max(lags)
    fresh = {
        'maxLagBlocks': max_lag,
        'maxLagMinutes': round(max_lag / BLOCKS_PER_HOUR * 60, 1) if max_lag else 0.0,
        'lagLast': lags[-1],
        'dataFresh': max_lag < LAG_DEGRADED,
        'degraded': max_lag >= LAG_WARN,
        'realtimeFailed': max_lag >= LAG_FAIL,
        'note': 'lag 高但已追平 = 数据完整、实时性降级；不等于扫描正常',
    }

    # ---------- 信号口径：行/唯一合约/唯一事件 ----------
    def sig_stats(rows):
        gated = [r for r in rows if r.get('gated')]
        g721 = [r for r in gated if r.get('assetType') == 'erc721']
        return {
            'signalRows': len(rows),
            'uniqueContracts': len({r.get('tok') for r in rows if r.get('tok')}),
            'uniqueEvents': len({(r.get('tok'), r.get('eventBlock')) for r in rows}),
            'duplicateRows': len(rows) - len({(r.get('tok'), r.get('eventBlock')) for r in rows}),
            'gatedRows': len(gated),
            'gatedContracts': len({r.get('tok') for r in gated if r.get('tok')}),
            'gatedErc721Rows': len(g721),
            'gatedErc721Contracts': len({r.get('tok') for r in g721 if r.get('tok')}),
            'byClass': dict(Counter(str(r.get('signalClass')) for r in rows)),
            'byAssetType': dict(Counter(str(r.get('assetType')) for r in rows)),
        }

    # ---------- G5：probe 溯源 ----------
    probe_path = P('shadow', 'probe.mjs')
    probe_sig = file_sig(probe_path)
    probe_commit = None
    try:
        probe_commit = subprocess.run(['git', '-C', root, 'log', '-1', '--format=%H', '--', 'shadow/probe.mjs'],
                                      capture_output=True, text=True, timeout=10).stdout.strip() or None
    except Exception:
        pass
    if not probe_sig:
        blk('G5_PROBE_MISSING')
    elif not probe_shas:
        # 当前探针不写 probeSha256（冻结期不能改）；只能记录工作树 hash 并标注来源
        pass

    # ---------- G6：输入 hash ----------
    inputs = {}
    for name, path in [('metrics.jsonl', P('shadow', 'metrics.jsonl')),
                       ('signals.jsonl', P('shadow', 'signals.jsonl')),
                       ('gaps.jsonl', P('shadow', 'gaps.jsonl')),
                       ('element-fills', fills_path)]:
        s = file_sig(path)
        if s:
            inputs[name] = s
    if len(inputs) < 3:
        blk('G6_INPUT_HASH_MISSING', {'have': sorted(inputs)})

    sales = {}
    if agg:
        sales = {'generatedAt': agg.get('generated'),
                 'fills': agg.get('fills_total'), 'fillsNative': agg.get('fills_native_bnb'),
                 'collectionsTraded': agg.get('collections_traded'), 'volumeBnb': agg.get('volumeBnb'),
                 'trackedCollections': agg.get('tracked_collections'),
                 'trackedWithSales': agg.get('tracked_with_sales'),
                 'trackedSales': agg.get('tracked_sales'),
                 'trackedVolumeBnb': agg.get('tracked_volumeBnb')}

    out = {
        'status': 'READY_FOR_DECISION' if not blocked else 'BLOCKED',
        'blocked': blocked,
        'generated': now.strftime('%Y-%m-%dT%H:%M:%SZ'),
        'window': {
            'source': window_source,
            'fromBlock': from_block, 'toBlock': to_block,
            'fromTimestamp': from_dt.strftime('%Y-%m-%dT%H:%M:%SZ') if from_dt else None,
            'toTimestamp': to_dt.strftime('%Y-%m-%dT%H:%M:%SZ') if to_dt else None,
            'windowMinutes': round((to_dt - from_dt).total_seconds() / 60, 1) if from_dt and to_dt else None,
        },
        'versions': {'probe': versions, 'build': builds, 'schemaVersion': schemas,
                     'collectorVersion': collectors, 'probeSha256Rounds': [x[:16] for x in probe_shas]},
        'provenance': {'probeFileSha256': (probe_sig or {}).get('sha256'),
                       'probeFileBytes': (probe_sig or {}).get('bytes'),
                       'probeGitCommit': probe_commit,
                       'probeSha256Source': 'working-tree-at-baseline-time',
                       'note': '冻结期不能在探针里加 probeSha256 字段，故按工作树 hash + git 提交溯源'},
        'integrity': integ,
        'dataFresh': fresh,
        'signals': sig_stats(signals),
        'sales': sales,
        'elementCoverage': {'dataComplete': cov.get('dataComplete'),
                            'requestedRanges': cov.get('requestedRanges'),
                            'scannedRanges': cov.get('scannedRanges'),
                            'skippedRanges': len(cov.get('skippedRanges') or []),
                            'retryCount': cov.get('retryCount'),
                            'coverageRatio': cov.get('coverageRatio'),
                            'rpcSources': cov.get('rpcSources')},
        'inputs': inputs,
        'thresholds': {'dataFreshLagWarn': LAG_WARN, 'dataFreshLagDegraded': LAG_DEGRADED,
                       'dataFreshLagFail': LAG_FAIL},
    }

    body = json.dumps(out, ensure_ascii=False, indent=1)
    if blocked:
        inv = out_path.replace('.json', '.invalid.json')
        open(inv, 'w', encoding='utf-8').write(body)
        print('STATUS=BLOCKED')
        for b in blocked:
            print('  - %s %s' % (b['code'], json.dumps(b['detail'], ensure_ascii=False)))
        print('已写 %s（不可用于 A/B/C 判定）' % inv)
        return 2
    if a.dry_run:
        print('STATUS=READY_FOR_DECISION (dry-run，未落盘)')
    else:
        tmp = out_path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            f.write(body); f.flush(); os.fsync(f.fileno())
        os.replace(tmp, out_path)
        print('已写 %s' % out_path)
    print('STATUS=%s 窗口 %s..%s（%s 分钟）' % (out['status'], from_block, to_block, out['window']['windowMinutes']))
    print('版本 %s / build %s / probe %s…' % (versions, builds, (probe_sig or {}).get('sha256', '')[:12]))
    print('完整性: 轮次=%s incomplete=%s gap=%s pending=%s zeroDis=%s' % (
        integ['rounds'], integ['incompleteRounds'], integ['gapCountSum'], integ['pendingMax'], integ['zeroDisagreements']))
    print('实时性: lagMax=%s 块（%s 分钟）dataFresh=%s' % (fresh['maxLagBlocks'], fresh['maxLagMinutes'], fresh['dataFresh']))
    print('信号: 行=%s 唯一合约=%s 唯一事件=%s 重复行=%s | gated 行=%s 合约=%s | gated721 行=%s 合约=%s' % (
        out['signals']['signalRows'], out['signals']['uniqueContracts'], out['signals']['uniqueEvents'],
        out['signals']['duplicateRows'], out['signals']['gatedRows'], out['signals']['gatedContracts'],
        out['signals']['gatedErc721Rows'], out['signals']['gatedErc721Contracts']))
    print('成交: fills=%s 集合=%s vol=%s trackedSales=%s' % (
        sales.get('fills'), sales.get('collectionsTraded'), sales.get('volumeBnb'), sales.get('trackedSales')))
    return 0


if __name__ == '__main__':
    sys.exit(main())
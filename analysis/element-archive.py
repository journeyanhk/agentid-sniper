#!/usr/bin/env python3
"""element-archive.py —— Element 证据归档清单（只读）

为一次成交扫描产出可复核的归档清单：
  输入 hash、输出 hash、窗口（块 + 时间）、chunk 清单、retry/skipped、
  decoder 版本、RPC 来源、每 chunk 的成交数与异常数（有 chunk 日志时）。

chunk 清单来源优先级：
  1) --chunk-log 指定的 jsonl（element-watch --chunk-log 产出，逐 chunk 实测）
  2) 从 coverage 推导（标记 derivedFromCoverage=true，说明无逐 chunk 实测）

用法：
  python3 analysis/element-archive.py --fills analysis/element-fills-w0.json \
      --output analysis/element-archive-w0.json [--chunk-log analysis/chunks.jsonl]
"""
import argparse
import hashlib
import json
import os
import time

STEP = 500


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for b in iter(lambda: f.read(1 << 20), b''):
            h.update(b)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--fills', required=True)
    ap.add_argument('--output', required=True)
    ap.add_argument('--chunk-log', default='')
    ap.add_argument('--script', default='analysis/element-watch.py')
    a = ap.parse_args()

    d = json.load(open(a.fills))
    ag = d.get('agg') or {}
    win = ag.get('window') or {}
    cov = ag.get('coverage') or {}

    chunks = []
    derived = True
    if a.chunk_log and os.path.exists(a.chunk_log):
        derived = False
        for line in open(a.chunk_log):
            line = line.strip()
            if line:
                chunks.append(json.loads(line))
    else:
        fb, tb = win.get('fromBlock'), win.get('toBlock')
        if fb is not None and tb is not None:
            b = fb
            while b <= tb:
                e = min(tb, b + STEP - 1)
                chunks.append({'fromBlock': b, 'toBlock': e, 'blocks': e - b + 1})
                b = e + 1

    summary = {
        'chunks': len(chunks),
        'chunksWithFills': sum(1 for c in chunks if c.get('fills')),
        'chunksWithError': sum(1 for c in chunks if c.get('error') or c.get('status') == 'skipped'),
        'fillsRecorded': sum(c.get('fills') or 0 for c in chunks),
    }
    if summary['fillsRecorded'] and ag.get('fills_total'):
        summary['fillsMatchTotal'] = summary['fillsRecorded'] == ag['fills_total']

    out = {
        'archivedAt': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'inputs': {a.fills: {'sha256': sha256(a.fills), 'bytes': os.path.getsize(a.fills)},
                   a.script: {'sha256': sha256(a.script), 'bytes': os.path.getsize(a.script)}},
        'window': {'fromBlock': win.get('fromBlock'), 'toBlock': win.get('toBlock'),
                   'blocks': win.get('blocks'), 'fromTimestamp': win.get('fromTimestamp'),
                   'toTimestamp': win.get('toTimestamp'), 'windowMinutes': win.get('windowMinutes')},
        'coverage': {'requestedRanges': cov.get('requestedRanges'), 'scannedRanges': cov.get('scannedRanges'),
                     'retryCount': cov.get('retryCount'), 'skippedRanges': cov.get('skippedRanges'),
                     'skippedBlocks': cov.get('skippedBlocks'), 'coverageRatio': cov.get('coverageRatio'),
                     'dataComplete': cov.get('dataComplete'), 'rpcSources': cov.get('rpcSources'),
                     'rpcOk': cov.get('rpcOk')},
        'decoder': ag.get('decoder'),
        'counts': {k: ag.get(k) for k in ('fills_total', 'fills_native_bnb', 'fills_erc20_pay',
                                          'collections_traded', 'volumeBnb', 'tracked_collections',
                                          'tracked_sales', 'tracked_volumeBnb', 'tracked_with_sales')},
        'chunkManifest': {'step': STEP, 'derivedFromCoverage': derived,
                          'chunkLog': a.chunk_log or None, 'summary': summary,
                          'chunks': chunks[:2000]},
        'reproduce': {
            'command': ('python3 analysis/element-watch.py --from-block %s --to-block %s '
                        '--output %s' % (win.get('fromBlock'), win.get('toBlock'), a.fills)),
            'note': '相同窗口重跑后比对 counts 与 output sha256；chunk 级重跑需 --chunk-log',
        },
        'self': {},
    }
    out['self'] = {'sha256': sha256(os.path.abspath(__file__)), 'path': os.path.basename(__file__)}
    json.dump(out, open(a.output, 'w'), ensure_ascii=False, indent=1)

    print('窗口 %s..%s（%.0f 分钟）chunk %d（推导=%s），有成交 %d、异常 %d'
          % (win.get('fromBlock'), win.get('toBlock'), win.get('windowMinutes') or 0,
             summary['chunks'], derived, summary['chunksWithFills'], summary['chunksWithError']))
    print('dataComplete=%s ratio=%s skipped=%s retry=%s decoder=%s'
          % (cov.get('dataComplete'), cov.get('coverageRatio'), cov.get('skippedRanges'),
             cov.get('retryCount'), (ag.get('decoder') or {}).get('version')))
    print('输入 sha256 %s…；清单 → %s' % (out['inputs'][a.fills]['sha256'][:16], a.output))


if __name__ == '__main__':
    main()
#!/usr/bin/env python3
"""check_w1_constants.py —— W1 判据漂移检查（只读，只报不改）

比对三处：
  1. analysis/w1-criteria.json          机器可读镜像
  2. analysis/CHAPTER-W1-DECISION.md    人读的判决章（必须有对应文字）
  3. analysis/w0-verdict.py             实际算 EV 的常量

任何不一致 exit 1，输出 expected / found / context，不修改任何文件。
"""
import json
import re
import sys
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CRITERIA = os.path.join(ROOT, 'analysis', 'w1-criteria.json')
CHAPTER = os.path.join(ROOT, 'analysis', 'CHAPTER-W1-DECISION.md')
VERDICT = os.path.join(ROOT, 'analysis', 'w0-verdict.py')

errors = []


def fail(where, expected, found, ctx=''):
    errors.append({'where': where, 'expected': expected, 'found': found, 'context': ctx})


def load_criteria():
    with open(CRITERIA, encoding='utf-8') as fh:
        d = json.load(fh)
    for k in ('window', 'criteria', 'assumptions'):
        if k not in d:
            fail('w1-criteria.json', f'含 {k}', '缺失')
    return d


def check_chapter(c):
    raw = open(CHAPTER, encoding='utf-8').read()
    w = c['window']
    need = [
        ('window.from (CST)', '2026-11-03 00:00', w['from']),
        ('window.to (CST)', '2026-11-16 23:59', w['to']),
        ('minCollections', '至少 3 个集合达到', str(c['criteria']['minCollections'])),
        ('minFills', '成交 ≥ 20 笔', str(c['criteria']['minFills'])),
        ('minBuyers', '独立买家 ≥ 20', str(c['criteria']['minBuyers'])),
        ('minNetKnownCostEVUsd', 'netKnownCostEVPerMint > 0', str(c['criteria']['minNetKnownCostEVUsd'])),
    ]
    for name, phrase, expected in need:
        if phrase not in raw:
            fail(f'CHAPTER:{name}', f'出现 "{phrase}"', '未找到',
                 '章节可能被改写，判据文字必须与 criteria 一致')
    # 反向检查：CHAPTER 里出现「至少 N 个集合」但 N 与 criteria 不同
    for m in re.finditer(r'至少\s*(\d+)\s*个集合达到', raw):
        if int(m.group(1)) != c['criteria']['minCollections']:
            line = raw[:m.start()].count('\n') + 1
            fail(f'CHAPTER:{line}', c['criteria']['minCollections'], m.group(1), m.group(0))
    for m in re.finditer(r'独立买家\s*≥\s*(\d+)', raw):
        if int(m.group(1)) != c['criteria']['minBuyers']:
            line = raw[:m.start()].count('\n') + 1
            fail(f'CHAPTER:{line}', c['criteria']['minBuyers'], m.group(1), m.group(0))
    return raw


def check_verdict(c):
    raw = open(VERDICT, encoding='utf-8').read()
    a = c['assumptions']
    patterns = {
        'bnbUsd': (r'^BNB_USD\s*=\s*([0-9.]+)', a['bnbUsd']),
        'marketplaceFee': (r'^FEE\s*=\s*([0-9.]+)', a['marketplaceFee']),
        'royaltyWorstCase': (r'^ROYALTY_WORST\s*=\s*([0-9.]+)', a['royaltyWorstCase']),
        'mintGasBnb': (r'mint_gas\s*=\s*([0-9.eE-]+)', a['mintGasBnb']),
        'sellGasBnb': (r'sell_gas\s*=\s*([0-9.eE-]+)', a['sellGasBnb']),
    }
    for name, (pat, expected) in patterns.items():
        m = re.search(pat, raw, re.M)
        if not m:
            fail(f'w0-verdict.py:{name}', expected, '未找到常量', pat)
            continue
        try:
            found = float(m.group(1))
        except ValueError:
            fail(f'w0-verdict.py:{name}', expected, m.group(1))
            continue
        if abs(found - float(expected)) > 1e-15:
            line = raw[:m.start()].count('\n') + 1
            fail(f'w0-verdict.py:{line}', expected, found, m.group(0))


def main():
    if not os.path.exists(CRITERIA):
        print('FAIL: 缺少 analysis/w1-criteria.json'); return 1
    c = load_criteria()
    check_chapter(c)
    check_verdict(c)
    if errors:
        for e in errors:
            print(f"FAIL {e['where']}: expected {e['expected']} / found {e['found']}"
                  + (f"  ({e['context']})" if e['context'] else ''))
        print(f'w1-constants: {len(errors)} drift(s)')
        return 1
    print('w1-constants: OK (window 2026-11-03 00:00 → 2026-11-16 23:59 CST; '
          'collect>=%d, fills>=%d, buyers>=%d, netEV>%s; verdict constants match)'
          % (c['criteria']['minCollections'], c['criteria']['minFills'],
             c['criteria']['minBuyers'], c['criteria']['minNetKnownCostEVUsd']))
    return 0


if __name__ == '__main__':
    sys.exit(main())
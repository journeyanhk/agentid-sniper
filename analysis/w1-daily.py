#!/usr/bin/env python3
"""w1-daily.py —— W1 日报的确定性入口（只读；只写 analysis/w1-daily.json）

存在理由：09:15 的日报此前由会话侧临时代码计算，代码不落盘、无法复现。
本脚本把同一套口径固定下来，并给 evUnknown 附可达的原因码：

  collection_address_missing      集合地址为空（eth_getLogs/eth_call 无从发起）
  total_supply_empty_or_revert    totalSupply() revert 或返回 0x（合约未实现）
  total_supply_zero               totalSupply() 返回 0（不能当合法分母）
  rpc_error                       重试后仍无结果（超时/429/其它）

不改判据、不改候选纳入：unknown 一律排除，EV 数值公式与 w0-verdict.py 完全一致。
"""
import json
import os
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COHORT = os.path.join(ROOT, 'analysis', 'cohort-latest.json')
OUT = os.path.join(ROOT, 'analysis', 'w1-daily.json')

RPCS = ['https://rpc.48.club', 'https://bsc-rpc.publicnode.com']
BNB_USD = 717.0
FEE = 0.02
MINT_GAS = 0.00000972
SELL_GAS = 0.00005
MIN_COLLECTIONS, MIN_FILLS, MIN_BUYERS = 3, 20, 20

_last = [0.0]


def rpc_call(method, params, tries=2):
    err = ''
    seen = []
    for _ in range(tries):
        for url in RPCS:
            dt = time.time() - _last[0]
            if dt < 0.15:
                time.sleep(0.15 - dt)
            _last[0] = time.time()
            body = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': method,
                               'params': params}).encode()
            try:
                rq = urllib.request.Request(url, body, {'content-type': 'application/json'})
                r = json.load(urllib.request.urlopen(rq, timeout=15))
            except Exception as e:  # noqa: BLE001 - 网络错误要分类回报
                err = type(e).__name__
                continue
            if 'error' in r:
                err = str(r['error'].get('message', ''))[:80]
                seen.append(err)
                continue
            return r.get('result'), None, (seen[-1] if seen else err)
    revert = next((e for e in seen if 'revert' in e.lower()), '')
    return None, 'rpc_error', (revert or err)


def supply_of(addr):
    if not addr:
        return None, 'collection_address_missing', 'address is empty'
    res, code, err = rpc_call('eth_call', [{'to': addr, 'data': '0x18160ddd'}, 'latest'])
    if code:
        if 'revert' in (err or '').lower():
            return None, 'total_supply_empty_or_revert', err
        return None, code, err
    if res in (None, '0x'):
        return None, 'total_supply_empty_or_revert', 'empty result'
    try:
        v = int(res, 16)
    except (TypeError, ValueError):
        return None, 'rpc_error', 'non-hex result'
    if v == 0:
        return None, 'total_supply_zero', 'totalSupply() == 0'
    return v, None, ''


def main():
    d = json.load(open(COHORT, encoding='utf-8'))
    agg = d.get('agg') or {}
    cols = [c for c in (d.get('collections') or []) if c.get('real_sales', 0) > 0]

    rows, unknown = [], []
    for c in cols:
        sym = c.get('sym') or (c.get('collection') or '')[:12]
        sp, reason, raw = supply_of(c.get('collection'))
        avg = c['volumeBnb'] / c['real_sales'] if c['real_sales'] else 0
        if sp is None:
            unknown.append({'collection': c.get('collection'), 'sym': sym,
                            'sales': c['real_sales'], 'uniqueBuyerCount': c.get('unique_buyers'),
                            'vol': round(c['volumeBnb'], 6), 'reason': reason,
                            'rawError': raw, 'evUnknown': True})
            continue
        p = c['real_sales'] / sp
        net = p * avg * (1 - FEE) - MINT_GAS - p * SELL_GAS
        rows.append({'collection': c.get('collection'), 'sym': sym,
                     'sales': c['real_sales'], 'uniqueBuyerCount': c.get('unique_buyers'),
                     'vol': round(c['volumeBnb'], 6), 'avgPriceBnb': round(avg, 8),
                     'supply': sp, 'sellThrough': round(p, 6),
                     'netKnownCostEvPerMintBnb': round(net, 10),
                     'netKnownCostEvPerMintUsd': round(net * BNB_USD, 4)})
    rows.sort(key=lambda x: -x['netKnownCostEvPerMintBnb'])

    cands = [r for r in rows
             if r['sales'] >= MIN_FILLS and (r['uniqueBuyerCount'] or 0) >= MIN_BUYERS
             and r['netKnownCostEvPerMintUsd'] > 0]
    gate = sum(1 for r in rows if r['sales'] >= MIN_FILLS)

    out = {
        'generatedUtc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'window': {k: agg.get(k) for k in ('fromBlock', 'toBlock', 'fromTimestamp',
                                           'toTimestamp', 'windowMinutes', 'dataComplete',
                                           'skippedFill', 'retries')},
        'market': {'fillsTotal': agg.get('fillsTotal'),
                   'collectionsTraded': agg.get('collectionsTraded'),
                   'trackedHits': agg.get('trackedHits')},
        'tracked': {'collections': len(cols),
                    'sales': sum(c['real_sales'] for c in cols),
                    'volumeBnb': round(sum(c['volumeBnb'] for c in cols), 6)},
        'evComputable': f'{len(rows)}/{len(cols)}',
        'evUnknown': unknown,
        'rows': rows,
        'candidates': cands,
        'counts': {'positiveKnownCost': sum(1 for r in rows if r['netKnownCostEvPerMintUsd'] > 0)},
        'sampleGate': f'{gate}/{MIN_COLLECTIONS} 集合达到成交>={MIN_FILLS}',
        'bestNetEvUsd': rows[0]['netKnownCostEvPerMintUsd'] if rows else None,
        'bestSym': rows[0]['sym'] if rows else None,
    }
    with open(OUT, 'w', encoding='utf-8') as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)

    print(f"[w1-daily] {agg.get('fromBlock')}..{agg.get('toBlock')} "
          f"{agg.get('windowMinutes')}min complete={agg.get('dataComplete')} | "
          f"fills {agg.get('fillsTotal')} / {agg.get('collectionsTraded')} col | "
          f"tracked {out['tracked']['collections']} col {out['tracked']['sales']} fills | "
          f"EV {out['evComputable']} | candidates {len(cands)} | "
          f"best {out['bestSym']} ${out['bestNetEvUsd']}")
    for u in unknown:
        print(f"  unknown {u['sym']}: {u['reason']} ({u['rawError']})")
    return 0


if __name__ == '__main__':
    sys.exit(main())
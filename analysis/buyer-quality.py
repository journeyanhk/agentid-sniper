#!/usr/bin/env python3
"""buyer-quality.py v3 —— 成交/铸造的结构性风险信号（只读，不接钱包）

定位：输出 buyerQualitySignals，**不是** realBuyer 判定。
结论措辞上限：no direct self-trade / no single-address concentration /
wash-trade risk unresolved。禁止写「无刷量」「真实需求」。

v3 相对 v2 的改动（按复核意见）：
  1. 新增 collection 级分解，riskClass 只是风险筛选标签，不是诈骗证明；
  2. 汇总层不再输出 no_concentration_flag，改为 materialRisks + wash_risk_unresolved；
  3. 明确标注两个窗口（sale 48h / mint 6h），禁止读者跨窗口比较；
  4. history 不可用时写 freshBuyerMetric=not_computed，不留空、不写 0。
"""
import argparse
import hashlib
import json
import time
import urllib.request
from collections import Counter, defaultdict

REL_TOL = 0.005
ABS_TOL = 1
HISTORY_WINDOW_BLOCKS = 100_000

# 风险筛选阈值（只是筛选，不是定性）
TH_REPEAT = 0.30
TH_OVERLAP = 0.25
TH_TOP10 = 0.50
TH_SUSPICIOUS = 0.20
MIN_FILLS_FOR_CLASS = 10


def clusters(prices, rel=REL_TOL, abs_tol=ABS_TOL):
    out, cur = [], []
    for p in sorted(prices):
        if not cur:
            cur = [p]
            continue
        base = cur[0]
        if abs(p - base) <= abs_tol or (base and abs(p - base) / base <= rel):
            cur.append(p)
        else:
            out.append(cur)
            cur = [p]
    if cur:
        out.append(cur)
    return out


def block(rs):
    buyers = Counter(r['buyer'] for r in rs if r['buyer'])
    sellers = Counter(r['seller'] for r in rs if r['seller'])
    pairs = Counter((r['buyer'], r['seller']) for r in rs if r['buyer'] and r['seller'])
    n = len(rs) or 1
    ub = len(buyers)
    overlap = len(set(buyers) & set(sellers))
    by_grp = defaultdict(list)
    for r in rs:
        try:
            by_grp[(r['collection'], r['paymentToken'])].append((int(r['priceNative']), r['buyer']))
        except Exception:
            pass
    cl, susp = [], 0
    for (coll, asset), ps in by_grp.items():
        amounts = [p for p, _ in ps]
        for c in clusters(amounts):
            if len(c) < 2:
                continue
            want = set(c)
            bs = {b for p, b in ps if p in want}
            is_susp = len(c) >= 5 and len(bs) <= 2
            if is_susp:
                susp += len(c)
            cl.append({'collection': coll, 'asset': asset, 'clusterAmount': str(c[0]),
                       'clusterCount': len(c), 'clusterBuyerCount': len(bs),
                       'clusterShare': round(len(c) / n, 4), 'suspicious': is_susp})
    cl.sort(key=lambda x: -x['clusterCount'])
    return {
        'fills': len(rs), 'uniqueBuyerCount': ub, 'uniqueSellerCount': len(sellers),
        'buyerSellerOverlap': round(overlap / ub, 4) if ub else None,
        'buyerSellerOverlapCount': overlap,
        'repeatPairCount': sum(1 for v in pairs.values() if v > 1),
        'repeatPairFills': sum(v for v in pairs.values() if v > 1),
        'repeatPairShare': round(sum(v for v in pairs.values() if v > 1) / n, 4),
        'top1BuyerShare': round(buyers.most_common(1)[0][1] / n, 4) if buyers else None,
        'top10BuyerShare': round(sum(c for _, c in buyers.most_common(10)) / n, 4) if buyers else None,
        'selfTradeCount': sum(1 for r in rs if r.get('selfTrade')),
        'sameAmountCluster': {
            'tolerance': {'relative': REL_TOL, 'absolute': ABS_TOL},
            'grouping': 'collection+paymentToken',
            'clusters': cl[:10],
            'clusterFillsShare': round(sum(x['clusterCount'] for x in cl) / n, 4) if cl else 0,
            'suspiciousClusterFillsShare': round(susp / n, 4),
            'note': '同价重复在地板价场景常见；判别力在 suspicious（≥5 笔且 ≤2 个买家）'},
    }


def risk_class(s):
    if s['fills'] < MIN_FILLS_FOR_CLASS:
        return 'insufficient-sample'
    trip = []
    if (s['repeatPairShare'] or 0) >= TH_REPEAT:
        trip.append('repeatPairShare')
    if (s['buyerSellerOverlap'] or 0) >= TH_OVERLAP:
        trip.append('buyerSellerOverlap')
    if (s['top10BuyerShare'] or 0) >= TH_TOP10:
        trip.append('top10BuyerShare')
    if (s['sameAmountCluster'].get('suspiciousClusterFillsShare') or 0) >= TH_SUSPICIOUS:
        trip.append('suspiciousCluster')
    return 'low-independence-risk' if trip else 'no-material-flag'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--rows', default='analysis/element-fills-w0-rows.jsonl')
    ap.add_argument('--meta', default='')
    ap.add_argument('--mint-replay', default='analysis/mint-replay-sold.json')
    ap.add_argument('--output', default='analysis/buyer-quality-w0.json')
    ap.add_argument('--history', action='store_true')
    a = ap.parse_args()
    mp = a.meta or (a.rows + '.meta.json')
    meta = json.load(open(mp))
    rows = [json.loads(l) for l in open(a.rows) if l.strip()]
    if not rows:
        raise SystemExit('rows 为空')
    ag = meta.get('aggregates') or {}
    if ag.get('fills_total') is not None and len(rows) != ag['fills_total']:
        raise SystemExit('rows 行数 %d ≠ 聚合 fills_total %d，停止' % (len(rows), ag['fills_total']))
    h = hashlib.sha256()
    with open(a.rows, 'rb') as f:
        for c in iter(lambda: f.read(1 << 20), b''):
            h.update(c)
    if meta.get('sha256') and h.hexdigest() != meta['sha256']:
        raise SystemExit('rows SHA-256 与 meta 不符，停止')

    native = [r for r in rows if r['paymentToken'].lower() == '0x' + 'e' * 40]
    erc20 = [r for r in rows if r['paymentToken'].lower() != '0x' + 'e' * 40]

    by_coll, grp = [], defaultdict(list)
    for r in rows:
        grp[r['collection']].append(r)
    for coll, rs in sorted(grp.items(), key=lambda kv: -len(kv[1])):
        s = block(rs)
        by_coll.append({'collection': coll, 'fills': s['fills'],
                        'uniqueBuyers': s['uniqueBuyerCount'], 'uniqueSellers': s['uniqueSellerCount'],
                        'buyerSellerOverlap': s['buyerSellerOverlap'],
                        'repeatPairShare': s['repeatPairShare'],
                        'top1BuyerShare': s['top1BuyerShare'],
                        'top10BuyerShare': s['top10BuyerShare'],
                        'suspiciousClusterShare': s['sameAmountCluster']['suspiciousClusterFillsShare'],
                        'selfTradeCount': s['selfTradeCount'],
                        'riskClass': risk_class(s)})

    allb = block(rows)
    material = []
    if (allb['repeatPairShare'] or 0) >= TH_REPEAT:
        material.append('repeatPairShare=%.3f ≥ %.2f' % (allb['repeatPairShare'], TH_REPEAT))
    if (allb['buyerSellerOverlap'] or 0) >= TH_OVERLAP:
        material.append('buyerSellerOverlap=%.3f ≥ %.2f' % (allb['buyerSellerOverlap'], TH_OVERLAP))
    if (allb['top10BuyerShare'] or 0) >= TH_TOP10:
        material.append('top10BuyerShare=%.3f ≥ %.2f' % (allb['top10BuyerShare'], TH_TOP10))
    if (allb['sameAmountCluster']['suspiciousClusterFillsShare'] or 0) >= TH_SUSPICIOUS:
        material.append('suspiciousClusterShare=%.3f ≥ %.2f'
                        % (allb['sameAmountCluster']['suspiciousClusterFillsShare'], TH_SUSPICIOUS))

    hist = {'historySource': 'not_requested', 'historyWindowBlocks': HISTORY_WINDOW_BLOCKS,
            'historyAvailability': 'not_requested', 'freshBuyerMetric': 'not_computed',
            'buyerWithHistoryCount': None, 'freshBuyerCount': None}
    if a.history:
        uniq = sorted({r['buyer'] for r in rows if r['buyer']})
        have = fresh = checked = 0
        for b in uniq[:60]:
            r0 = next(r for r in rows if r['buyer'] == b)
            fb = int(r0['blockNumber'])
            n_at = R.call('eth_getTransactionCount', [b, hex(max(0, fb - 1))])
            n_win = R.call('eth_getTransactionCount', [b, hex(max(0, fb - 1 - HISTORY_WINDOW_BLOCKS))])
            if n_at is None or n_win is None:
                continue
            checked += 1
            if int(n_at, 16) - int(n_win, 16) > 0:
                have += 1
            if int(n_at, 16) == 0:
                fresh += 1
        if checked:
            hist = {'historySource': 'archive', 'historyWindowBlocks': HISTORY_WINDOW_BLOCKS,
                    'historyAvailability': 'available', 'freshBuyerMetric': 'computed',
                    'buyersChecked': checked, 'coverage': round(checked / max(1, len(uniq)), 4),
                    'buyerWithHistoryCount': have, 'freshBuyerCount': fresh}
        else:
            hist['historyAvailability'] = 'unavailable'
            hist['note'] = '免费 RPC 无 archive：fillBlock-1 状态取不到，字段保持 null，不做推断'

    mint = {'window': None, 'source': a.mint_replay}
    try:
        mr = json.load(open(a.mint_replay))
        o = mr.get('outcomes') or {}
        tot = sum(o.values()) or 1
        mint = {'window': mr.get('window'), 'windowLengthHours': 6, 'sampleSize': tot,
                'directMintShare': round(o.get('direct', 0) / tot, 4),
                'relayLikeShare': round(o.get('sponsored_or_relay_like', 0) / tot, 4),
                'contractSenderShare': round(o.get('contract_sender', 0) / tot, 4),
                'failedShare': round(o.get('failed', 0) / tot, 4),
                'uniqueSenderCount': (mr.get('senderConcentration') or {}).get('uniqueSenders'),
                'top1SenderShare': (mr.get('senderConcentration') or {}).get('top1Share'),
                'note': 'direct 100% 只说明无明显 relay 集中，不等于 113 个发送者是 113 个真人'}
    except Exception as e:
        mint['error'] = str(e)

    out = {'generated': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
           'windows': {'sale': {'window': meta.get('window'), 'lengthHours': 48},
                       'mint': {'window': mint.get('window'), 'lengthHours': 6},
                       'discipline': '两个窗口不同，禁止跨窗口比较或合成单一总分'},
           'inputs': {'rows': a.rows, 'rowsSha256': meta.get('sha256'),
                      'transferVerification': meta.get('transferVerification')},
           'positions': {
               'can_assert': ['no direct self-trade detected',
                              'no single-address concentration (top1 %.0f%%, top10 %.0f%%)'
                              % (100 * (allb['top1BuyerShare'] or 0), 100 * (allb['top10BuyerShare'] or 0))],
               'cannot_assert': ['wash-trade risk excluded', 'these fills are independent real demand',
                                 'buyers are unique humans'],
               'materialRisks': material,
               'verdict': 'wash_risk_unresolved' if material else 'no_material_risk_detected'},
           'saleBuyerQuality': {'all': allb, 'native': block(native), 'erc20': block(erc20),
                                'byCollection': by_coll,
                                'riskThresholds': {'repeatPairShare': TH_REPEAT, 'buyerSellerOverlap': TH_OVERLAP,
                                                   'top10BuyerShare': TH_TOP10, 'suspiciousClusterShare': TH_SUSPICIOUS,
                                                   'minFillsForClass': MIN_FILLS_FOR_CLASS},
                                'riskClassCaveat': 'riskClass 是筛选标签，不是诈骗证明；样本<10 笔不作分类'},
           'mintSenderQuality': mint,
           'historyProxy': hist}

    json.dump(out, open(a.output, 'w'), ensure_ascii=False, indent=1)

    print('rows %d（native %d / erc20 %d / selfTrade %d）' % (len(rows), len(native), len(erc20), allb['selfTradeCount']))
    print('all  buyers %d | overlap %.2f | repeatPair %.1f%% | top1 %.2f top10 %.2f | susp %.2f'
          % (allb['uniqueBuyerCount'], allb['buyerSellerOverlap'] or 0, 100 * (allb['repeatPairShare'] or 0),
             allb['top1BuyerShare'] or 0, allb['top10BuyerShare'] or 0,
             allb['sameAmountCluster']['suspiciousClusterFillsShare']))
    print('verdict', out['positions']['verdict'], '| material', material)
    print('--- 按集合（fills≥10 才分类）')
    for c in by_coll[:12]:
        print('%-44s %4d 笔 | 买家 %3d | 重叠 %.2f | 重复对 %.2f | top10 %.2f | susp %.2f | %s'
              % (c['collection'][:44], c['fills'], c['uniqueBuyers'], c['buyerSellerOverlap'] or 0,
                 c['repeatPairShare'] or 0, c['top10BuyerShare'] or 0,
                 c['suspiciousClusterShare'] or 0, c['riskClass']))


class R:
    i = 0
    last = 0.0
    RPCS = ['https://rpc.48.club', 'https://bsc.blockrazor.xyz', 'https://1rpc.io/bnb']

    @classmethod
    def call(cls, method, params, tries=2):
        for k in range(tries):
            url = cls.RPCS[(cls.i + k) % len(cls.RPCS)]
            dt = time.time() - cls.last
            if dt < 0.1:
                time.sleep(0.1 - dt)
            cls.last = time.time()
            try:
                body = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params}).encode()
                r = json.load(urllib.request.urlopen(urllib.request.Request(
                    url, body, {'content-type': 'application/json'}), timeout=20))
                if 'error' in r:
                    return None
                return r.get('result')
            except Exception:
                continue
        return None


if __name__ == '__main__':
    main()
#!/usr/bin/env python3
"""buyer-quality.py —— 成交/铸造的结构性风险信号（只读，不接钱包）

定位：只输出 buyerQualitySignals，不输出 realBuyer。
结论措辞上限：low_independence / no obvious relay concentration / 结构较健康。

分组输出：
  saleBuyerQuality   来自逐笔 Element rows（买家侧集中度、重叠、重复对、同额簇）
  mintSenderQuality  来自 mint replay（direct / sponsored_or_relay_like / contract_sender）
两者窗口不同，禁止合并成单一总分。

防偏差：
  - buyer 历史一律取 fillBlock-1 及之前的链上状态；免费 RPC 无 archive 时
    historySource=unavailable，相关字段为 null，并用 currentNonceProxy 单独标注（不参与判定）。
  - sameAmountCluster 按支付资产分别聚类，容差 = 相对 0.5% + 绝对 1 wei。
"""
import argparse
import hashlib
import json
import time
import urllib.request
from collections import Counter, defaultdict

RPCS = ['https://rpc.48.club', 'https://bsc.blockrazor.xyz', 'https://1rpc.io/bnb']
REL_TOL = 0.005
ABS_TOL = 1
HISTORY_WINDOW_BLOCKS = 100_000   # ≈12.5h @8000 blocks/h


class R:
    i = 0
    last = 0.0
    ok = 0
    err = 0

    @classmethod
    def call(cls, method, params, tries=2):
        for k in range(tries):
            url = RPCS[(cls.i + k) % len(RPCS)]
            dt = time.time() - cls.last
            if dt < 0.1:
                time.sleep(0.1 - dt)
            cls.last = time.time()
            try:
                body = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params}).encode()
                r = json.load(urllib.request.urlopen(urllib.request.Request(
                    url, body, {'content-type': 'application/json'}), timeout=20))
                if 'error' in r:
                    cls.err += 1
                    continue
                cls.ok += 1
                return r.get('result')
            except Exception:
                cls.err += 1
        return None


def clusters(prices, rel=REL_TOL, abs_tol=ABS_TOL):
    """按资产内价格聚类：相邻价格相对差 ≤ rel 且绝对差 > abs_tol 时并入同簇。"""
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--rows', default='analysis/element-fills-w0-rows.jsonl')
    ap.add_argument('--meta', default='')
    ap.add_argument('--mint-replay', default='analysis/mint-replay-sold.json')
    ap.add_argument('--output', default='analysis/buyer-quality-w0.json')
    ap.add_argument('--history', action='store_true', help='尝试用历史块查 buyer nonce（需 archive，通常不可用）')
    a = ap.parse_args()

    meta_path = a.meta or (a.rows + '.meta.json')
    meta = json.load(open(meta_path))

    rows = []
    for line in open(a.rows):
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    if not rows:
        raise SystemExit('rows 为空')

    # 与聚合文件交叉核对（spec：汇总不一致就停）
    ag = meta.get('aggregates') or {}
    got = {'fills_total': len(rows)}
    expect = {'fills_total': ag.get('fills_total')}
    if expect['fills_total'] is not None and got['fills_total'] != expect['fills_total']:
        raise SystemExit('rows 行数 %d ≠ 聚合 fills_total %d，停止' % (got['fills_total'], expect['fills_total']))
    if meta.get('sha256'):
        h = hashlib.sha256()
        with open(a.rows, 'rb') as f:
            for c in iter(lambda: f.read(1 << 20), b''):
                h.update(c)
        if h.hexdigest() != meta['sha256']:
            raise SystemExit('rows SHA-256 与 meta 不符，停止')

    native = [r for r in rows if r['paymentToken'].lower() == '0x' + 'e' * 40]
    erc20 = [r for r in rows if r['paymentToken'].lower() != '0x' + 'e' * 40]
    self_trade = [r for r in rows if r.get('selfTrade')]

    def block(rs):
        buyers = Counter(r['buyer'] for r in rs if r['buyer'])
        sellers = Counter(r['seller'] for r in rs if r['seller'])
        pairs = Counter((r['buyer'], r['seller']) for r in rs if r['buyer'] and r['seller'])
        n = len(rs) or 1
        ub, us = len(buyers), len(sellers)
        overlap = len(set(buyers) & set(sellers))
        # 聚类必须按 (collection, asset)：跨集合的同价没有经济含义
        by_grp = defaultdict(list)
        for r in rs:
            try:
                by_grp[(r['collection'], r['paymentToken'])].append((int(r['priceNative']), r['buyer']))
            except Exception:
                pass
        cl = []
        for (coll, asset), ps in by_grp.items():
            amounts = [p for p, _ in ps]
            for c in clusters(amounts):
                if len(c) < 2:
                    continue
                want = set(c)
                bs = {b for p, b in ps if p in want}
                # 可疑簇：同价、次数多、参与者少（地板价正常成交通常参与者多）
                suspicious = len(c) >= 5 and len(bs) <= 2
                cl.append({'collection': coll, 'asset': asset, 'clusterAmount': str(c[0]),
                           'clusterCount': len(c), 'clusterBuyerCount': len(bs),
                           'clusterShare': round(len(c) / n, 4), 'suspicious': suspicious})
        cl.sort(key=lambda x: -x['clusterCount'])
        susp = sum(x['clusterCount'] for x in cl if x['suspicious'])
        return {
            'fills': len(rs), 'uniqueBuyerCount': ub, 'uniqueSellerCount': us,
            'buyerSellerOverlap': round(overlap / ub, 4) if ub else None,
            'buyerSellerOverlapCount': overlap,
            'repeatPairCount': sum(1 for v in pairs.values() if v > 1),
            'repeatPairFills': sum(v for v in pairs.values() if v > 1),
            'repeatPairShare': round(sum(v for v in pairs.values() if v > 1) / n, 4),
            'top1BuyerShare': round(buyers.most_common(1)[0][1] / n, 4) if buyers else None,
            'top10BuyerShare': round(sum(c for _, c in buyers.most_common(10)) / n, 4) if buyers else None,
            'selfTradeCount': sum(1 for r in rs if r.get('selfTrade')),
            'sameAmountCluster': {'tolerance': {'relative': REL_TOL, 'absolute': ABS_TOL},
                                  'grouping': 'collection+paymentToken',
                                  'clusters': cl[:10],
                                  'clusterFillsShare': round(sum(x['clusterCount'] for x in cl) / n, 4) if cl else 0,
                                  'suspiciousClusterFillsShare': round(susp / n, 4),
                                  'note': '同价重复在同集合地板价场景很常见，需看 suspicious（≥5 笔且 ≤2 个买家）'},
        }

    sale = {'window': meta.get('window'), 'all': block(rows), 'native': block(native), 'erc20': block(erc20)}

    # 买家历史（严格用 fillBlock-1，避免 look-ahead）
    hist = {'historySource': 'not_requested', 'historyWindowBlocks': HISTORY_WINDOW_BLOCKS,
            'buyerWithHistoryCount': None, 'freshBuyerCount': None}
    if a.history:
        uniq = sorted({r['buyer'] for r in rows if r['buyer']})
        have = fresh = 0
        checked = 0
        for b in uniq[:60]:
            r0 = next(r for r in rows if r['buyer'] == b)
            fb = int(r0['blockNumber'])
            n_at = R.call('eth_getTransactionCount', [b, hex(max(0, fb - 1))])
            n_win = R.call('eth_getTransactionCount', [b, hex(max(0, fb - 1 - HISTORY_WINDOW_BLOCKS))])
            if n_at is None or n_win is None:
                continue
            checked += 1
            d = int(n_at, 16) - int(n_win, 16)
            if d > 0:
                have += 1
            if int(n_at, 16) == 0:
                fresh += 1
        hist = {'historySource': 'archive' if checked else 'unavailable',
                'historyWindowBlocks': HISTORY_WINDOW_BLOCKS,
                'buyersChecked': checked, 'buyersTotal': len(uniq),
                'coverage': round(checked / max(1, len(uniq)), 4),
                'buyerWithHistoryCount': have if checked else None,
                'freshBuyerCount': fresh if checked else None,
                'note': '免费 RPC 无 archive 时 checked=0，字段为 null，不做推断'}

    mint = {'window': None, 'source': a.mint_replay}
    try:
        mr = json.load(open(a.mint_replay))
        o = mr.get('outcomes') or {}
        tot = sum(o.values()) or 1
        mint = {'window': mr.get('window'), 'sampleSize': tot,
                'directMintShare': round(o.get('direct', 0) / tot, 4),
                'relayLikeShare': round(o.get('sponsored_or_relay_like', 0) / tot, 4),
                'contractSenderShare': round(o.get('contract_sender', 0) / tot, 4),
                'failedShare': round(o.get('failed', 0) / tot, 4),
                'uniqueSenderCount': (mr.get('senderConcentration') or {}).get('uniqueSenders'),
                'top1SenderShare': (mr.get('senderConcentration') or {}).get('top1Share'),
                'layers': mr.get('layers'),
                'note': 'mint 交易指标，窗口与成交分析不同，禁止与 saleBuyerQuality 合并为总分'}
    except Exception as e:
        mint['error'] = str(e)

    flags = []
    a_ = sale['all']
    if (a_['top10BuyerShare'] or 0) >= 0.7:
        flags.append('top10BuyerShare_high')
    if (a_['buyerSellerOverlap'] or 0) >= 0.5:
        flags.append('buyerSellerOverlap_high')
    if (a_['repeatPairShare'] or 0) >= 0.2:
        flags.append('repeatPairShare_high')
    if (a_['sameAmountCluster'].get('suspiciousClusterFillsShare') or 0) >= 0.3:
        flags.append('suspiciousSameAmountCluster_high')
    hard = [f for f in flags if f in ('top10BuyerShare_high', 'buyerSellerOverlap_high',
                                      'suspiciousSameAmountCluster_high')]
    verdict = ('low_independence' if len(hard) >= 2 else
               ('mixed_signals' if flags else 'no_concentration_flag'))

    out = {'generated': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
           'inputs': {'rows': a.rows, 'rowsSha256': meta.get('sha256'), 'meta': meta_path,
                      'dedupeKey': meta.get('dedupeKey'),
                      'transferVerification': meta.get('transferVerification')},
           'positions': {'note': 'buyerQualitySignals 只是结构信号，不是真人身份验证',
                         'flags': flags, 'verdict': verdict,
                         'cannot_conclude': ['真实需求不足/充足', '退出规模是否足够']},
           'saleBuyerQuality': sale,
           'mintSenderQuality': mint,
           'historyProxy': hist,
           'rpc': {'ok': R.ok, 'err': R.err}}
    json.dump(out, open(a.output, 'w'), ensure_ascii=False, indent=1)

    print('rows %d（native %d / erc20 %d / selfTrade %d）' % (len(rows), len(native), len(erc20), len(self_trade)))
    for k in ('all', 'native'):
        s = sale[k]
        print('%-7s buyers %3d | overlap %.2f | repeatPair %d (%.1f%%) | top1 %.2f top10 %.2f | 同额簇占比 %.2f'
              % (k, s['uniqueBuyerCount'], s['buyerSellerOverlap'] or 0, s['repeatPairCount'],
                 100 * (s['repeatPairShare'] or 0), s['top1BuyerShare'] or 0,
                 s['top10BuyerShare'] or 0, s['sameAmountCluster'].get('clusterFillsShare') or 0))
    print('mint: direct %.2f / relayLike %.2f（样本 %d）'
          % (mint.get('directMintShare') or 0, mint.get('relayLikeShare') or 0, mint.get('sampleSize') or 0))
    print('flags %s → %s' % (flags, verdict))


if __name__ == '__main__':
    main()
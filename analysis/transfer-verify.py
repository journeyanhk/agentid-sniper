#!/usr/bin/env python3
"""transfer-verify.py —— 可复现的 Transfer 抽检（hash 抽样，固定 seed）

替换原来的 stride 抽样：stride 会与周期性成交别名（同一区块内多笔同价成交容易被系统跳过），
改用 sha256(seed + txHash + ':' + logIndex) 排序取前 N，任何人可用同一 seed 复跑同一批样本。

产出：
  - 重写 rows.jsonl 的 transferEvidence 列（未抽中的行保持 null，不伪造 true）
  - meta.transferVerification 记录 sampleMethod / sampleSeed / sampleSize / ok / failed / coverage
  - rows 重写后重新计算 meta.sha256
"""
import argparse
import hashlib
import json
import time
import urllib.request

RPCS = ['https://rpc.48.club', 'https://bsc.blockrazor.xyz', 'https://1rpc.io/bnb']
T721 = '0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef'


class R:
    i = 0
    last = 0.0
    ok = 0
    err = 0

    @classmethod
    def call(cls, method, params, tries=3):
        for k in range(tries):
            url = RPCS[(cls.i + k) % len(RPCS)]
            dt = time.time() - cls.last
            if dt < 0.08:
                time.sleep(0.08 - dt)
            cls.last = time.time()
            try:
                body = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params}).encode()
                r = json.load(urllib.request.urlopen(urllib.request.Request(
                    url, body, {'content-type': 'application/json'}), timeout=20))
                if 'error' in r:
                    cls.err += 1
                    cls.i += 1
                    continue
                cls.ok += 1
                return r.get('result')
            except Exception:
                cls.err += 1
                cls.i += 1
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--rows', default='analysis/element-fills-w0-rows.jsonl')
    ap.add_argument('--meta', default='')
    ap.add_argument('--n', type=int, default=200)
    ap.add_argument('--seed', default='w0-transfer-v1')
    a = ap.parse_args()
    mp = a.meta or (a.rows + '.meta.json')

    rows = [json.loads(l) for l in open(a.rows) if l.strip()]
    meta = json.load(open(mp))

    def key(i, r):
        return '%s:%s' % (r['txHash'].lower(), r['logIndex'])

    order = sorted(range(len(rows)), key=lambda i: hashlib.sha256(
        (a.seed + key(i, rows[i])).encode()).hexdigest())
    picked, seen_tx = [], set()
    for i in order:
        r = rows[i]
        if r['txHash'].lower() in seen_tx:
            continue
        seen_tx.add(r['txHash'].lower())
        picked.append(i)
        if len(picked) >= a.n:
            break

    receipt, verdict = {}, {}
    ok_fills = bad_fills = 0
    for i in picked:
        r = rows[i]
        tx = r['txHash'].lower()
        if tx not in receipt:
            receipt[tx] = R.call('eth_getTransactionReceipt', [tx])
        rc = receipt[tx]
        good = False
        if rc and rc.get('status') == '0x1':
            coll = r['collection'].lower()
            want_tid = format(int(r['tokenId']), '064x')
            want_to = '0' * 24 + r['buyer'].lower().replace('0x', '')
            for lg in rc.get('logs', []):
                if (lg.get('address', '').lower() != coll
                        or not lg.get('topics')
                        or lg['topics'][0].lower() != T721
                        or len(lg['topics']) < 4):
                    continue
                # topics: [sig, from, to, tokenId]
                if lg['topics'][2].lower().endswith(want_to[-40:]) and \
                        lg['topics'][3].lower().endswith(want_tid):
                    good = True
                    break
        if good:
            ok_fills += 1
        else:
            bad_fills += 1
        verdict[tx] = good
    covered = 0
    for r in rows:
        v = verdict.get(r['txHash'].lower())
        r['transferEvidence'] = v
        if v is not None:
            covered += 1

    with open(a.rows, 'w') as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + '\n')

    h = hashlib.sha256()
    with open(a.rows, 'rb') as f:
        for c in iter(lambda: f.read(1 << 20), b''):
            h.update(c)

    meta['sha256'] = h.hexdigest()
    meta['transferVerification'] = {
        'sampleMethod': 'deterministic_hash',
        'sampleSeed': a.seed,
        'sampleSize': a.n,
        'sampleTxs': len(picked),
        'sampleFills': covered,
        'transferOk': ok_fills,
        'transferFailed': bad_fills,
        'okRate': round(ok_fills / max(1, ok_fills + bad_fills), 4),
        'coverage': round(covered / len(rows), 4),
        'method': 'receipt 内 ERC-721 Transfer(collection, tokenId, to=buyer)',
        'reproduce': 'python3 analysis/transfer-verify.py --n %d --seed %s' % (a.n, a.seed),
        'scope': '抽检覆盖的行由同 tx 共享证据；未抽中行 transferEvidence=null，不外推',
    }
    json.dump(meta, open(mp, 'w'), ensure_ascii=False, indent=1)

    print('抽样 %d tx / 覆盖 %d 行 | ok %d / failed %d | rpc ok %d err %d'
          % (len(picked), covered, ok_fills, bad_fills, R.ok, R.err))
    print('新 sha256', h.hexdigest()[:16])


if __name__ == '__main__':
    main()
#!/usr/bin/env python3
"""verify-fill-decode.py —— 校验 Element Fill 解码：同一 tx 内所有原生 Fill 的价格之和 == tx.value

为什么不能逐笔比：一个 tx 常常包含多笔 Fill（批量结算/扫货），单笔 price != tx.value 是正常的。
正确口径是「按 tx 汇总」。按 words 数分组统计，能直接暴露 12/14 words 布局混排导致的字段错位。

只读：eth_getLogs + eth_getTransactionByHash。
"""
import json, sys, time, urllib.request
from collections import defaultdict

RPCS = ['https://rpc.48.club', 'https://bsc.blockrazor.xyz', 'https://1rpc.io/bnb']
FILL = '0x9c248aa1a265aa616f707b979d57f4529bb63a4fc34dc7fc61fdddc18410f74e'
NATIVE = 0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee


class R:
    i = 0
    last = 0

    @classmethod
    def call(cls, method, params):
        for k in range(3):
            url = RPCS[(cls.i + k) % len(RPCS)]
            dt = time.time() - cls.last
            if dt < 0.17:
                time.sleep(0.17 - dt)
            cls.last = time.time()
            try:
                body = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params}).encode()
                r = json.load(urllib.request.urlopen(urllib.request.Request(
                    url, body, {'content-type': 'application/json'}), timeout=25))
                if 'error' in r:
                    continue
                return r.get('result')
            except Exception:
                continue
        return None


def main():
    hours = float(sys.argv[1]) if len(sys.argv) > 1 else 3
    head = int(R.call('eth_blockNumber', []), 16)
    frm = head - int(hours * 8000)
    logs, cur = [], frm
    while cur <= head:
        end = min(head, cur + 499)
        r = R.call('eth_getLogs', [{'fromBlock': hex(cur), 'toBlock': hex(end), 'topics': [FILL]}])
        if r is None:
            print('scan fail at', cur)
            cur = end + 1
            continue
        logs.extend(r)
        cur = end + 1

    tx_price = defaultdict(lambda: {'sum': 0, 'n': 0, 'words': set()})
    for l in logs:
        d = l['data'][2:]
        w = [int(d[i:i + 64], 16) for i in range(0, len(d), 64)]
        if len(w) < 12 or w[4] != NATIVE:
            continue
        e = tx_price[l['transactionHash']]
        e['sum'] += w[5]; e['n'] += 1; e['words'].add(len(w))

    by_group = defaultdict(lambda: {'txs': 0, 'match': 0, 'mismatch': 0, 'fills': 0, 'samples': []})
    for tx, e in tx_price.items():
        r = R.call('eth_getTransactionByHash', [tx])
        paid = int((r or {}).get('value', '0x0'), 16)
        g = '+'.join(str(x) for x in sorted(e['words']))
        b = by_group[g]
        b['txs'] += 1; b['fills'] += e['n']
        if e['sum'] == paid:
            b['match'] += 1
        else:
            b['mismatch'] += 1
            if len(b['samples']) < 3:
                b['samples'].append({'sum': str(e['sum']), 'paid': str(paid), 'fills': e['n'], 'tx': tx})

    out = {'at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
           'fromBlock': frm, 'toBlock': head, 'logs': len(logs), 'byWords': by_group}
    json.dump(out, open('/home/laoda/agentid-sniper/analysis/fill-decode-check.json', 'w'),
              ensure_ascii=False, indent=1)
    for g, v in sorted(by_group.items()):
        print('words=%-6s txs=%4d fills=%4d 汇总匹配=%4d 不匹配=%3d (%.1f%%)' % (
            g, v['txs'], v['fills'], v['match'], v['mismatch'], 100.0 * v['mismatch'] / max(1, v['txs'])))
        for s in v['samples']:
            print('   sum=%s paid=%s fills=%s tx=%s' % (s['sum'][:20], s['paid'][:20], s['fills'], s['tx'][:18]))


if __name__ == '__main__':
    main()
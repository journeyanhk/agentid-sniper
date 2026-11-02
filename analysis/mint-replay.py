#!/usr/bin/env python3
"""mint-replay.py —— mint 历史交易回放审计（只读，不接钱包）

核心发现驱动的口径：BSC 上大量 mint 是「合约/中继代发」，tx.from 并不等于 NFT 收款人。
因此每笔 mint 同时记录 sender（tx.from）与 recipient（Transfer 的 to），并分类：

  direct     tx.from == recipient          自己付 gas 自己铸
  mediated   tx.from != recipient          合约/中继代发（批量铸造、机器人、平台代铸）
  failed     receipt.status != 1           交易失败
  reverted_no_token  status==1 但目标集合无 Transfer

对 cohort 的意义：mediated 占比高时，「独立钱包数」不等于「独立付款人」，
卖压可能来自同一批操作者的批量铸造。

用法：
  python3 analysis/mint-replay.py --hours 2 --collections 0xabc,0xdef --limit-tx 300
  python3 analysis/mint-replay.py --from-block 131149699 --to-block 131161699 --sold-only
"""
import argparse
import json
import time
import urllib.request
from collections import defaultdict, Counter

RPCS = ['https://rpc.48.club', 'https://bsc.blockrazor.xyz', 'https://1rpc.io/bnb', 'https://bsc-dataseed.binance.org']
T721 = '0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef'
Z64 = '0x' + '0' * 64
STEP, MAXRETRY, THROTTLE = 500, 4, 0.17


class Rpc:
    def __init__(self):
        self.i = 0
        self.last = 0.0
        self.calls = 0
        self.errors = 0
        self.sources = defaultdict(int)

    def call(self, method, params, tries=3):
        for k in range(tries):
            url = RPCS[(self.i + k) % len(RPCS)]
            dt = time.time() - self.last
            if dt < THROTTLE:
                time.sleep(THROTTLE - dt)
            self.last = time.time()
            self.calls += 1
            try:
                body = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params}).encode()
                r = json.load(urllib.request.urlopen(urllib.request.Request(
                    url, body, {'content-type': 'application/json'}), timeout=25))
                if 'error' in r:
                    self.errors += 1
                    continue
                self.sources[url] += 1
                return r.get('result')
            except Exception:
                self.errors += 1
        return None


def sold_collections(path='/home/laoda/agentid-sniper/analysis/element-fills-w0.json'):
    d = json.load(open(path))
    return {r['collection'].lower() for r in d.get('tracked') or [] if r.get('sales')}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--from-block', type=int)
    ap.add_argument('--to-block', type=int)
    ap.add_argument('--hours', type=float, default=2)
    ap.add_argument('--limit-tx', type=int, default=200)
    ap.add_argument('--collections', default='', help='逗号分隔的集合地址；留空=全市场')
    ap.add_argument('--sold-only', action='store_true', help='只看 W0 有成交的 21 个集合')
    ap.add_argument('--output', default='analysis/mint-replay.json')
    a = ap.parse_args()

    want = {c.strip().lower() for c in a.collections.split(',') if c.strip()}
    if a.sold_only:
        want |= sold_collections()
    if want:
        print('限定集合 %d 个' % len(want))

    R = Rpc()
    head = int(R.call('eth_blockNumber', []), 16)
    to_b = a.to_block or head
    from_b = a.from_block or max(0, to_b - int(a.hours * 8000))

    t0 = time.time()
    mints = defaultdict(list)
    chunks = skipped = total_logs = 0
    b = from_b
    while b <= to_b:
        e = min(to_b, b + STEP - 1)
        logs, step = None, STEP
        for _ in range(MAXRETRY):
            logs = R.call('eth_getLogs', [{'fromBlock': hex(b), 'toBlock': hex(e), 'topics': [T721, Z64]}])
            if logs is not None:
                break
            step = max(25, step // 2)
            e = min(to_b, b + step - 1)
        chunks += 1
        if logs is None:
            skipped += 1
            b = e + 1
            continue
        total_logs += len(logs)
        for lg in logs:
            c = lg['address'].lower()
            if want and c not in want:
                continue
            mints[lg['transactionHash']].append({
                'contract': c,
                'tokenId': int(lg['topics'][3], 16) if len(lg['topics']) > 3 else None,
                'recipient': '0x' + lg['topics'][2][-40:]})
        b = e + 1

    print('窗口 %d..%d（%.1f 分钟）分片 %d、skipped %d、全市场 mint 事件 %d、命中集合 mint 交易 %d 笔'
          % (from_b, to_b, (to_b - from_b) / 8000 * 60, chunks, skipped, total_logs, len(mints)))

    txs = list(mints.items())[:a.limit_tx]
    rows, tally, senders = [], Counter(), Counter()
    for h, evs in txs:
        rc = R.call('eth_getTransactionReceipt', [h])
        tx = R.call('eth_getTransactionByHash', [h])
        if not rc or not tx:
            tally['rpc_missing'] += 1
            continue
        sender = (tx.get('from') or '').lower()
        status = int(rc.get('status') or '0x0', 16)
        gas_used = int(rc.get('gasUsed') or '0x0', 16)
        gas_price = int(rc.get('effectiveGasPrice') or tx.get('gasPrice') or '0x0', 16)
        value = int(tx.get('value') or '0x0', 16)
        recipients = {e['recipient'] for e in evs}
        mediated = recipients != {sender}
        if status != 1:
            outcome = 'failed'
        elif not recipients:
            outcome = 'reverted_no_token'
        else:
            outcome = 'mediated' if mediated else 'direct'
        tally[outcome] += 1
        senders[sender] += 1
        rows.append({
            'txHash': h, 'contract': sorted({e['contract'] for e in evs})[0],
            'sender': sender, 'minter': sorted(recipients)[0] if len(recipients) == 1 else None,
            'recipients': sorted(recipients), 'recipientCount': len(recipients),
            'receiptStatus': status, 'mediated': mediated,
            'receivedTokenIds': [e['tokenId'] for e in evs if e['recipient'] == sender],
            'transferToMinter': bool([e for e in evs if e['recipient'] == sender]),
            'paymentValue': str(value), 'gasUsed': gas_used,
            'gasCostNative': str(gas_used * gas_price), 'mintOutcome': outcome})

    top = senders.most_common(5)
    res = {'generated': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
           'window': {'fromBlock': from_b, 'toBlock': to_b},
           'scope': {'collectionsRequested': sorted(want) if want else 'market-wide'},
           'coverage': {'chunks': chunks, 'skipped': skipped, 'dataComplete': skipped == 0},
           'counts': {'mintTxs': len(mints), 'replayed': len(rows), **tally},
           'senderConcentration': {'uniqueSenders': len(senders), 'top5': top,
                                   'top1Share': round(top[0][1] / len(rows), 4) if rows and top else None},
           'rpc': {'calls': R.calls, 'errors': R.errors, 'sources': dict(R.sources)},
           'rows': rows}
    json.dump(res, open(a.output, 'w'), ensure_ascii=False, indent=1)
    print('回放 %d 笔：%s' % (len(rows), dict(tally)))
    print('发送者 %d 个，top1 占比 %s，top5 %s' % (
        len(senders), res['senderConcentration']['top1Share'], top))
    print('耗时 %.1fs → %s' % (time.time() - t0, a.output))


if __name__ == '__main__':
    main()
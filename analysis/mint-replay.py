#!/usr/bin/env python3
"""mint-replay.py v3 —— mint 历史交易回放（只读）

命名纪律（本轮修正）：
  direct                  tx.from == NFT 收款人
  sponsored_or_relay_like tx.from != NFT 收款人（只说明「发送者与收款人不同」）
  contract_sender         且 eth_getCode(tx.from, 该 tx 区块) != 0x（有代码才敢叫合约发送者）
  unknown_code            取不到历史 code，不做定性

五层计数（禁止只报两个数）：
  rawEvents → uniqueTx → receiptSuccess → confirmedTransfer → cohortIncluded
  每层都带 excluded / reason，并显式记录窗口与 limit 截断。

用法：
  python3 analysis/mint-replay.py --hours 6 --sold-only --limit-tx 400 --output analysis/mint-replay-sold.json
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
    ap.add_argument('--hours', type=float, default=6)
    ap.add_argument('--limit-tx', type=int, default=400)
    ap.add_argument('--collections', default='')
    ap.add_argument('--sold-only', action='store_true')
    ap.add_argument('--output', default='analysis/mint-replay.json')
    a = ap.parse_args()

    want = {c.strip().lower() for c in a.collections.split(',') if c.strip()}
    if a.sold_only:
        want |= sold_collections()

    R = Rpc()
    head = int(R.call('eth_blockNumber', []), 16)
    to_b = a.to_block or head
    from_b = a.from_block or max(0, to_b - int(a.hours * 8000))

    t0 = time.time()
    events = defaultdict(list)
    raw_events = 0
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
        for lg in logs:
            total_logs += 1
            c = lg['address'].lower()
            if want and c not in want:
                continue
            raw_events += 1
            events[lg['transactionHash']].append({
                'contract': c,
                'tokenId': int(lg['topics'][3], 16) if len(lg['topics']) > 3 else None,
                'recipient': '0x' + lg['topics'][2][-40:]})
        b = e + 1

    unique_tx = len(events)
    txs = list(events.items())[:a.limit_tx]
    truncated = unique_tx - len(txs)

    rows, tally, senders, sender_kind = [], Counter(), Counter(), Counter()
    for h, evs in txs:
        rc = R.call('eth_getTransactionReceipt', [h])
        tx = R.call('eth_getTransactionByHash', [h])
        if not rc or not tx:
            tally['rpc_missing'] += 1
            continue
        sender = (tx.get('from') or '').lower()
        tx_to = (tx.get('to') or '').lower()
        status = int(rc.get('status') or '0x0', 16)
        gas_used = int(rc.get('gasUsed') or '0x0', 16)
        gas_price = int(rc.get('effectiveGasPrice') or tx.get('gasPrice') or '0x0', 16)
        value = int(tx.get('value') or '0x0', 16)
        blk = rc.get('blockNumber') or tx.get('blockNumber')
        recipients = sorted({e['recipient'] for e in evs})
        matches = sender in recipients

        code = None
        if not matches:
            code = R.call('eth_getCode', [sender, blk]) if blk else None
        if matches:
            skind, code_hex = 'eoa', None          # 自己付款铸造，发送者必然是 EOA（有代码无法发起 tx）
        elif code is None:
            skind, code_hex = 'unknown_code', None
        elif code in ('0x', '0x0', ''):
            skind, code_hex = 'eoa', code
        else:
            skind, code_hex = 'contract', code[:74]

        if status != 1:
            outcome = 'failed'
        elif matches:
            outcome = 'direct'
        elif skind == 'contract':
            outcome = 'contract_sender'
        else:
            outcome = 'sponsored_or_relay_like'
        tally[outcome] += 1
        senders[sender] += 1
        sender_kind[skind] += 1
        rows.append({
            'txHash': h, 'contract': sorted({e['contract'] for e in evs})[0],
            'txFrom': sender, 'recipient': recipients[0] if len(recipients) == 1 else None,
            'recipients': recipients, 'recipientCount': len(recipients),
            'txTo': tx_to, 'senderCodeAtBlock': code_hex, 'senderKind': skind,
            'senderMatchesRecipient': matches,
            'receiptStatus': status, 'blockNumber': int(blk, 16) if isinstance(blk, str) else blk,
            'receivedTokenIds': [e['tokenId'] for e in evs if e['recipient'] == sender],
            'transferToMinter': matches,
            'paymentValue': str(value), 'gasUsed': gas_used,
            'gasCostNative': str(gas_used * gas_price), 'mintOutcome': outcome})

    confirmed = [r for r in rows if r['receiptStatus'] == 1 and r['transferToMinter']]
    layers = {
        'rawEventsMarketWide': total_logs,
        'rawEvents': raw_events,
        'uniqueTx': unique_tx,
        'receiptSuccess': sum(1 for r in rows if r['receiptStatus'] == 1),
        'confirmedTransfer': len(confirmed),
        'cohortIncluded': len(confirmed),
        'excluded': {
            'notReplayed_dueToLimit': truncated,
            'rpcMissing': tally.get('rpc_missing', 0),
            'receiptFailed': tally.get('failed', 0),
            'noTransferToSender': len([r for r in rows if r['receiptStatus'] == 1 and not r['transferToMinter']]),
        },
    }
    top = senders.most_common(5)
    res = {'generated': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
           'window': {'fromBlock': from_b, 'toBlock': to_b,
                      'blocks': to_b - from_b + 1,
                      'approxMinutes': round((to_b - from_b) / 8000 * 60, 1)},
           'scope': {'collections': sorted(want) if want else 'market-wide',
                     'rawEventsMarketWide': total_logs,
                     'collectionCount': len(want) if want else None},
           'coverage': {'chunks': chunks, 'skipped': skipped, 'dataComplete': skipped == 0},
           'layers': layers, 'outcomes': dict(tally),
           'senderKind': dict(sender_kind),
           'senderConcentration': {'uniqueSenders': len(senders), 'top5': top,
                                   'top1Share': round(top[0][1] / len(rows), 4) if rows and top else None},
           'rpc': {'calls': R.calls, 'errors': R.errors, 'sources': dict(R.sources)},
           'rows': rows}
    json.dump(res, open(a.output, 'w'), ensure_ascii=False, indent=1)

    print('窗口 %d..%d（≈%.0f 分钟，%d 块）分片 %d skipped %d'
          % (from_b, to_b, res['window']['approxMinutes'], res['window']['blocks'], chunks, skipped))
    print('五层：rawEvents %d → uniqueTx %d → receiptSuccess %d → confirmedTransfer %d → cohortIncluded %d'
          % (layers['rawEvents'], layers['uniqueTx'], layers['receiptSuccess'],
             layers['confirmedTransfer'], layers['cohortIncluded']))
    print('排除：%s' % json.dumps(layers['excluded'], ensure_ascii=False))
    print('分类：%s；senderKind %s' % (dict(tally), dict(sender_kind)))
    print('发送者 %d 个，top1 %s，top5 %s' % (len(senders),
                                          res['senderConcentration']['top1Share'], top))
    print('耗时 %.0fs → %s' % (time.time() - t0, a.output))


if __name__ == '__main__':
    main()
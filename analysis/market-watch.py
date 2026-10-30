#!/usr/bin/env python3
"""market-watch：判定 ERC-721 是否出现可验证的 marketplace listing / sale（只读）

设计约束：
- 免费 RPC 无 archive（历史 state 只覆盖最近 ~50-200 块），因此**不能用余额差证明收款**；
- 也不依赖市场地址清单（会过时），改用链上行为判据。

判据（严格分级，绝不把钱包间转账算成交）：
- listing_like : token 被转入「有代码的合约」，且该合约被持有人 setApprovalForAll 授权过 → 挂牌/托管
- sale         : 同一 receipt 里出现已知结算事件（Seaport OrderFulfilled），且 NFT 由托管方交给另一个地址
- escrow_move  : 进/出合约，但没有结算事件 → 只算托管搬动，不算成交
- eoa_transfer : 纯 EOA↔EOA → 永不计入成交

输出：analysis/market-watch.jsonl（每次追加一条汇总）+ 终端摘要
不做：签名、approve、mint、sell、baw、任何写操作。
"""
import json, os, sys, time, urllib.request
from collections import Counter, defaultdict

RPCS = ['https://rpc.48.club', 'https://bsc.blockrazor.xyz', 'https://1rpc.io/bnb']
T = '0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef'
ZADDR = '0x' + '0' * 40
# Seaport OrderFulfilled（多家市场复用 Seaport 结算）
SETTLE_TOPICS = {
    '0x9d9af8e38d66c62e2c12f0225249fd9d721c54b83f48d9352c97c6cacdcb6f31': 'seaport_order_fulfilled',
}
# 已知市场/结算函数的 selector，用于给合约画像（不用于判定成交）
MKT_SELECTORS = {
    '0xfb0f3ee1': 'seaport_fulfill_basic_order',
    '0xb3a34c4c': 'seaport_fulfill_order',
    '0xed98a574': 'seaport_fulfill_advanced_order',
    '0x42842e0e': 'nft_safe_transfer_from',
}

class Rpc:
    def __init__(self): self.i = 0; self.calls = 0; self.err = Counter(); self.last = 0
    def pace(self):
        dt = time.time() - self.last
        if dt < 0.18: time.sleep(0.18 - dt)
        self.last = time.time()
    def call(self, method, params, tries=3):
        for k in range(tries):
            url = RPCS[(self.i + k) % len(RPCS)]
            self.pace(); self.calls += 1
            body = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params}).encode()
            try:
                req = urllib.request.Request(url, body, {'content-type': 'application/json', 'user-agent': 'market-watch/1.0'})
                r = json.load(urllib.request.urlopen(req, timeout=22))
                if 'error' in r:
                    self.err[r['error'].get('message', '')[:30]] += 1
                    continue
                return r.get('result')
            except Exception as e:
                self.err[str(e)[:30]] += 1
        return None

R = Rpc()
def u(h): return int(h, 16) if h and h != '0x' else 0
def topic_addr(t): return '0x' + t[-40:]

CODE = {}
def code_of(a):
    if a in CODE: return CODE[a]
    c = R.call('eth_getCode', [a, 'latest'])
    CODE[a] = c or '0x'
    return CODE[a]

def has_code(a): return len(code_of(a)) > 2

def contracts_in(code):
    hits = [name for sel, name in MKT_SELECTORS.items() if sel[2:] in code.lower()]
    return hits

def approved_for_all(nft, owner, operator):
    data = '0xe985e9c5' + owner[2:].rjust(64, '0') + operator[2:].rjust(64, '0')
    r = R.call('eth_call', [{'to': nft, 'data': data}, 'latest'])
    return bool(r) and r != '0x' and int(r, 16) == 1

def get_logs(addr, frm, to):
    out, cur = [], frm
    step = 500
    while cur <= to:
        end = min(to, cur + step - 1)
        r = R.call('eth_getLogs', [{'address': addr, 'fromBlock': hex(cur), 'toBlock': hex(end), 'topics': [T]}])
        if r is None:
            step = max(25, step // 2); continue
        out.extend(r); cur = end + 1
        if len(out) > 6000: break
    return out

def analyze(tok, meta, head, lookback):
    frm = max(int(meta.get('eventBlock') or 0) - 2, head - lookback)
    logs = get_logs(tok, frm, head)
    mints = [l for l in logs if topic_addr(l['topics'][1]) == ZADDR]
    moves = [l for l in logs if topic_addr(l['topics'][1]) != ZADDR]
    contract_moves, eoa_moves = [], 0
    mkt_seen = Counter()
    sales = []
    for l in moves:
        src, dst = topic_addr(l['topics'][1]), topic_addr(l['topics'][2])
        c_src, c_dst = has_code(src), has_code(dst)
        if not c_src and not c_dst:
            eoa_moves += 1
            continue
        rec = {'tx': l['transactionHash'], 'block': u(l['blockNumber']), 'from': src, 'to': dst,
               'from_is_contract': c_src, 'to_is_contract': c_dst}
        for a, tag in ((src, 'src'), (dst, 'dst')):
            if (tag == 'src' and c_src) or (tag == 'dst' and c_dst):
                hits = contracts_in(code_of(a))
                if hits: rec.setdefault('market_selectors', []).extend(hits); mkt_seen[a] = mkt_seen[a] + 1
        rcpt = R.call('eth_getTransactionReceipt', [l['transactionHash']])
        settle = None
        if rcpt:
            for lg in rcpt.get('logs', []):
                t0 = (lg.get('topics') or [''])[0]
                if t0 in SETTLE_TOPICS:
                    settle = SETTLE_TOPICS[t0]; break
        if settle:
            rec['settlement_event'] = settle
            sales.append(rec)
        contract_moves.append(rec)
    # 挂牌证据：持有人是否把某个合约设为 operator
    owners = [topic_addr(x['topics'][2]) for x in mints[-3:]]
    ops = list({r['to'] for r in contract_moves if r['to_is_contract']} | {r['from'] for r in contract_moves if r['from_is_contract']})
    approvals = []
    for o in ops[:4]:
        for ow in owners[:2]:
            if approved_for_all(tok, ow, o):
                approvals.append({'owner': ow, 'operator': o, 'selector_hits': contracts_in(code_of(o))})
                break
    return {
        'tok': tok, 'sym': meta.get('sym'), 'eventBlock': meta.get('eventBlock'),
        'transfers': len(logs), 'mints': len(mints), 'eoa_moves': eoa_moves,
        'contract_moves': len(contract_moves), 'contracts': dict(mkt_seen.most_common(5)),
        'market_selectors': dict(Counter(s for r in contract_moves for s in r.get('market_selectors', []))),
        'listing_like': len(approvals), 'approvals': approvals[:4],
        'sales': sales[:5], 'sale_count': len(sales),
        'first_move_block': moves[0]['blockNumber'] if moves else None,
    }

def main():
    lookback = int(sys.argv[1]) if len(sys.argv) > 1 else 30000
    src = os.path.expanduser('~/agentid-sniper/shadow/signals.jsonl')
    rows = [json.loads(l) for l in open(src) if l.strip()]
    last = {}
    for r in rows:
        if r.get('gated') and r.get('assetType') == 'erc721':
            last[r['tok']] = r
    picks = sorted(last.values(), key=lambda x: -(x.get('ageMin') or 0))[:60]
    head = u(R.call('eth_blockNumber', []))
    print(f'市场取证：{len(picks)} 个 gated ERC-721，head={head}，回看 {lookback} 块', flush=True)
    out = []
    for i, m in enumerate(picks, 1):
        try:
            rec = analyze(m['tok'], m, head, lookback)
        except Exception as e:
            rec = {'tok': m['tok'], 'error': str(e)[:80]}
        out.append(rec)
        if i % 10 == 0: print(f'  ...{i}/{len(picks)} calls={R.calls}', flush=True)
    agg = {
        'generated': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'head': head, 'collections': len(out),
        'with_eoa_moves': sum(1 for x in out if x.get('eoa_moves')),
        'with_contract_moves': sum(1 for x in out if x.get('contract_moves')),
        'with_market_selector': sum(1 for x in out if x.get('market_selectors')),
        'listing_like': sum(1 for x in out if x.get('listing_like')),
        'sale_count': sum(x.get('sale_count', 0) for x in out),
        'seaport_fulfilled': sum(1 for x in out for s in x.get('sales', [])),
        'rpc_calls': R.calls, 'errors': dict(R.err.most_common(5)),
    }
    dest = os.path.expanduser('~/agentid-sniper/analysis/market-watch.jsonl')
    with open(dest, 'a') as f: f.write(json.dumps({'agg': agg, 'tokens': out}, ensure_ascii=False) + '\n')
    print(json.dumps(agg, ensure_ascii=False, indent=1), flush=True)

if __name__ == '__main__':
    main()
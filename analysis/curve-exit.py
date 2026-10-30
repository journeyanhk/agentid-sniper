#!/usr/bin/env python3
"""Curve 退出路径取证（只读）
方法：不看函数名，只看真实链上行为——
  1) 取每个 gated 代币的全部 Transfer 事件（mint / 内部转手 / 卖回）
  2) 找出「从多个独立钱包持续收币、且自身有代码」的对手方 = 候选 curve/路由合约
  3) 拉这些对手方收到的真实交易的 tx.to / selector / value
  4) 用 eth_call 在该卖出交易的前一个区块、以原 sender 身份重放，看是否能成功、返回多少
不做：签名、approve、mint、sell、baw、任何写操作。
输出 analysis/curve-exit.json
"""
import json, sys, time, urllib.request, os
from collections import Counter, defaultdict

RPCS = ['https://rpc.48.club', 'https://bsc.blockrazor.xyz', 'https://1rpc.io/bnb']
T = '0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef'
ZERO = '0x' + '0' * 64
ZADDR = '0x' + '0' * 40

class Rpc:
    def __init__(self): self.i = 0; self.calls = 0; self.errs = Counter(); self.last = 0
    def pace(self):
        dt = time.time() - self.last
        if dt < 0.2: time.sleep(0.2 - dt)
        self.last = time.time()
    def call(self, method, params, tries=4, chunk_ok=True):
        for k in range(tries):
            url = RPCS[(self.i + k) % len(RPCS)]
            self.pace(); self.calls += 1
            body = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params}).encode()
            try:
                req = urllib.request.Request(url, body, {'content-type': 'application/json', 'user-agent': 'curve-probe/1.0'})
                r = json.load(urllib.request.urlopen(req, timeout=25))
                if 'error' in r:
                    m = r['error'].get('message', '')
                    self.errs[m[:34]] += 1
                    continue
                return r.get('result')
            except Exception as e:
                self.errs[str(e)[:34]] += 1
        return None

R = Rpc()
def u(h): return int(h, 16) if h and h != '0x' else 0
def addr_from_topic(t): return '0x' + t[-40:]

def get_logs_range(addr, topics, frm, to):
    out = []
    cur = frm
    step = 500
    while cur <= to:
        end = min(to, cur + step - 1)
        r = None
        for _ in range(3):
            r = R.call('eth_getLogs', [{'address': addr, 'fromBlock': hex(cur), 'toBlock': hex(end), 'topics': topics}], tries=2)
            if r is not None: break
            step = max(25, step // 2)
            end = min(to, cur + step - 1)
        if r is None:
            R.errs['gap:' + hex(cur)] += 1
            cur = end + 1
            continue
        out.extend(r)
        cur = end + 1
        if len(out) > 4000: break
    return out

def tx_of(h):
    return R.call('eth_getTransactionByHash', [h])

def has_code(a):
    c = R.call('eth_getCode', [a, 'latest'])
    return bool(c) and c != '0x'

def analyze(tok, meta, max_txs=6):
    head = u(R.call('eth_blockNumber', []))
    frm = max(int(meta.get('eventBlock') or 0) - 5, head - 2000)
    logs = get_logs_range(tok, [T], frm, head)
    mints, to_curve, sent_by = [], defaultdict(lambda: defaultdict(int)), defaultdict(set)
    for l in logs:
        src = addr_from_topic(l['topics'][1])
        dst = addr_from_topic(l['topics'][2])
        amt = u(l['data'])
        if src == ZADDR:
            mints.append({'tx': l['transactionHash'], 'block': u(l['blockNumber']), 'to': dst, 'amount': amt})
        else:
            to_curve[dst][l['transactionHash']] += 1
            sent_by[dst].add(src)
    # 候选 curve：收币来源 ≥3 个不同钱包，且自身有代码
    cands = []
    for a, senders in sent_by.items():
        if a == ZADDR or len(senders) < 3: continue
        if not has_code(a): continue
        cands.append({'addr': a, 'distinctSenders': len(senders)})
    cands.sort(key=lambda x: -x['distinctSenders'])
    detail = []
    for c in cands[:3]:
        txs, sels = [], Counter()
        seen_tx = set()
        for l in logs:
            if addr_from_topic(l['topics'][2]) != c['addr'] or l['transactionHash'] in seen_tx: continue
            seen_tx.add(l['transactionHash'])
            if len(txs) >= max_txs: break
            tx = tx_of(l['transactionHash'])
            if not tx: continue
            sel = (tx.get('input') or '0x')[:10]
            sels[sel] += 1
            txs.append({'hash': l['transactionHash'], 'block': u(tx['blockNumber']), 'from': tx['from'],
                        'to': tx['to'], 'selector': sel, 'value': u(tx['value']), 'inputLen': len(tx.get('input') or ''),
                        'input': tx.get('input')})
        replays = []
        for t in txs[:3]:
            call = {'from': t['from'], 'to': t['to'], 'data': t['input']}
            if t['value']: call['value'] = hex(t['value'])
            r = R.call('eth_call', [call, hex(max(0, t['block'] - 1))])
            replays.append({'hash': t['hash'], 'block': t['block'], 'ok': r is not None, 'result': (r or '')[:138]})
        detail.append({'contract': c['addr'], 'distinctSenders': c['distinctSenders'],
                       'selectors': dict(sels), 'txs': txs, 'replays': replays})
    return {'tok': tok, 'sym': meta.get('sym'), 'eventBlock': meta.get('eventBlock'),
            'transfers': len(logs), 'mints': len(mints),
            'mintTx': mints[0]['tx'] if mints else None,
            'candidates': cands[:3], 'detail': detail}

def main():
    src = os.path.expanduser('~/agentid-sniper/shadow/signals.jsonl')
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    rows = [json.loads(l) for l in open(src) if l.strip()]
    last = {}
    for r in rows:
        if r.get('gated') and (r.get('ageMin') or 0) >= 5:
            last[r['tok']] = r
    picks = sorted(last.values(), key=lambda x: -(x.get('mints') or 0))[:n]
    print(f'取证样本 {len(picks)} 个 gated 代币（按 mint 数排序）')
    out = []
    for i, m in enumerate(picks, 1):
        a = analyze(m['tok'], m)
        out.append(a)
        print(f"  {i}/{len(picks)} {a['sym'] or a['tok'][:10]} transfers={a['transfers']} mints={a['mints']} candidates={len(a['candidates'])}")
    summ = {
        'tokens': len(out),
        'with_candidates': len([x for x in out if x['candidates']]),
        'total_candidates': len({c['addr'] for x in out for c in x['candidates']}),
        'mint_selectors': Counter(
            (d['txs'][0]['selector'] if d['txs'] else None)
            for x in out for d in x['detail']).most_common(6),
        'sell_selectors': Counter(
            s for x in out for d in x['detail'] for s in d['selectors']).most_common(8),
        'replay_ok': sum(1 for x in out for d in x['detail'] for r in d['replays'] if r['ok']),
        'replay_total': sum(1 for x in out for d in x['detail'] for r in d['replays']),
        'rpc_calls': R.calls,
        'errors': dict(R.errs.most_common(6)),
    }
    dest = os.path.expanduser('~/agentid-sniper/analysis/curve-exit.json')
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    for x in out:
        for d in x['detail']:
            for t in d['txs']: t['input'] = (t['input'] or '')[:74] + '...'
    json.dump({'generated': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'summary': summ, 'tokens': out},
              open(dest, 'w'), ensure_ascii=False, indent=1)
    print(json.dumps(summ, ensure_ascii=False, indent=1, default=str))

if __name__ == '__main__':
    main()
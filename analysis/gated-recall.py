#!/usr/bin/env python3
"""gated-recall.py —— 量化 gated 分类器的漏判率（只读）

现状：gated 判据 = 合约自身字节码是否含 Agent ID registry 地址。
问题：通过发射台/工厂间接引用 registry 的项目，自身字节码里不含该地址，会被判成 not gated。
      已实证 SIGIL(9/9)、SEED(2/2)，两者铸造钱包 100% 持有 Agent ID。

做法：抽一批「被判为 not gated」的 ERC-721，不看字节码，直接看行为——
      取最近 N 块的 mint 事件，抽样铸造钱包，查 registry.balanceOf>0 的比例。
      ratio ≥ 0.8 且钱包数 ≥ 3 判为「实际受门槛限制」，记为分类器漏判。

只读：eth_getLogs / eth_call，不签名、不交易。
"""
import json, os, sys, time, urllib.request
from collections import Counter

RPC = 'https://bsc-rpc.publicnode.com'
REG = '0x60b9262456de011d8a8c8f8f8c0b0d6a0b4e6d0b'
REG = '0x60b9262456de011f7d15a54cdd8c48baaa8b022f'
XFER = '0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef'
ZERO = '0x' + '0' * 64
_last = [0.0]
_calls = [0]

def call(method, params, tries=3):
    for k in range(tries):
        d = time.time() - _last[0]
        if d < 0.2: time.sleep(0.2 - d)
        _last[0] = time.time(); _calls[0] += 1
        try:
            rq = urllib.request.Request(RPC, json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params}).encode(),
                                        {'content-type': 'application/json', 'user-agent': 'recall/1'})
            r = json.load(urllib.request.urlopen(rq, timeout=20))
            if 'error' in r: continue
            return r.get('result')
        except Exception:
            continue
    return None

def agent_balance(a):
    r = call('eth_call', [{'to': REG, 'data': '0x70a08231' + '0' * 24 + a[2:]}, 'latest'])
    return int(r, 16) if r and r != '0x' else 0

def main():
    base = os.path.expanduser('~/agentid-sniper')
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 40
    lookback = int(sys.argv[2]) if len(sys.argv) > 2 else 40000
    rows = [json.loads(l) for l in open(base + '/shadow/signals.jsonl') if l.strip()]
    seen, cand = set(), []
    for r in reversed(rows):
        if r.get('assetType') != 'erc721' or r.get('gated'): continue
        t = r['tok'].lower()
        if t in seen: continue
        seen.add(t); cand.append(r)
        if len(cand) >= limit: break
    head = int(call('eth_blockNumber', []), 16)
    print(f'抽样 {len(cand)} 个「not gated」ERC-721，回看 {lookback} 块', flush=True)

    stat = Counter(); misses = []
    for n, r in enumerate(cand, 1):
        t = r['tok'].lower()
        lg = call('eth_getLogs', [{'address': t, 'fromBlock': hex(head - lookback), 'toBlock': hex(head), 'topics': [XFER, ZERO]}]) or []
        wallets = []
        for l in lg:
            a = '0x' + l['topics'][2][-40:]
            if a not in wallets: wallets.append(a)
        if len(wallets) < 3:
            stat['tooFewMints'] += 1; continue
        sample = wallets[:8]
        hits = sum(1 for a in sample if agent_balance(a) > 0)
        ratio = hits / len(sample)
        stat['checked'] += 1
        if ratio >= 0.8:
            stat['missedGated'] += 1
            misses.append({'tok': t, 'sym': r.get('sym'), 'wallets': len(wallets),
                           'sample': len(sample), 'agentRatio': round(ratio, 2), 'mints': len(lg)})
        else:
            stat['trulyOpen'] += 1
        if n % 10 == 0:
            print(f'  {n}/{len(cand)} checked={stat["checked"]} 漏判={stat["missedGated"]} rpc={_calls[0]}', flush=True)

    out = {'generated': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
           'sampled': len(cand), 'lookbackBlocks': lookback, 'stats': dict(stat),
           'missRate': round(stat['missedGated'] / max(1, stat['checked']), 3),
           'misses': misses, 'rpcCalls': _calls[0]}
    json.dump(out, open(base + '/analysis/gated-recall.json', 'w'), ensure_ascii=False, indent=1)
    print(json.dumps({k: out[k] for k in ['sampled', 'stats', 'missRate', 'rpcCalls']}, ensure_ascii=False, indent=1))
    for m in misses[:15]:
        print('   漏判 %-12s %s mints=%-4d wallets=%-3d agentRatio=%.2f' % (m['sym'], m['tok'], m['mints'], m['wallets'], m['agentRatio']))

if __name__ == '__main__':
    main()
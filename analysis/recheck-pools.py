#!/usr/bin/env python3
"""重测历史 gated 代币是否真的没有 PancakeSwap 池子（v3 的 <=66 误判已修复后的独立复核）
只读：eth_call / eth_getLogs。不签名、不发交易、不接钱包。
输出 analysis/pool-recheck.json + 终端摘要。
"""
import json, sys, time, urllib.request, urllib.error, os
from collections import Counter

RPCS = ['https://bsc.blockrazor.xyz', 'https://rpc.48.club', 'https://1rpc.io/bnb']
V2F = '0xca143ce32fe78f1f7019d7d551a6402fc5350c73'
V3F = '0x0bfbcf9fa4f9c56b0f40a671ad40e0805a091865'
QUOTER = '0xb048bbc1ee6b733fffcfb9e9cef7375518e25997'
WBNB = '0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c'
USDT = '0x55d398326f99059ff775485246999027b3197955'
FEES = [100, 500, 2500, 10000]

def wa(a): return a.lower().replace('0x', '').rjust(64, '0')
def w64(n): return format(int(n), 'x').rjust(64, '0')

class Rpc:
    def __init__(self):
        self.i = 0; self.calls = 0; self.errs = Counter(); self.last = 0
    def pace(self):
        dt = time.time() - self.last
        if dt < 0.17: time.sleep(0.17 - dt)
        self.last = time.time()
    def call(self, method, params, tries=3):
        for k in range(tries):
            url = RPCS[(self.i + k) % len(RPCS)]
            self.pace(); self.calls += 1
            body = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params}).encode()
            try:
                req = urllib.request.Request(url, body, {'content-type': 'application/json', 'user-agent': 'recheck/1.0'})
                r = json.load(urllib.request.urlopen(req, timeout=20))
                if 'error' in r:
                    m = r['error'].get('message', '')
                    self.errs[(url.split('//')[1].split('.')[0], m[:28])] += 1
                    continue
                return r.get('result')
            except Exception as e:
                self.errs[(url.split('//')[1].split('.')[0], str(e)[:28])] += 1
        return None

R = Rpc()
ZERO = '0x' + '0' * 64

def eth_call(to, data, verify=True):
    r = R.call('eth_call', [{'to': to, 'data': data}, 'latest'])
    if verify and (r is None or r == '0x' or r == ZERO):
        r2 = R.call('eth_call', [{'to': to, 'data': data}, 'latest'])
        if r2 and r2 not in ('0x', ZERO): return r2
    return r

def u(h):
    return int(h, 16) if h and h not in ('0x',) else 0

def v3_pools(tok):
    out = []
    for q in (WBNB, USDT):
        for fee in FEES:
            p = eth_call(V3F, '0x1698ee82' + wa(tok) + wa(q) + w64(fee))
            if not p or len(p) < 66 or u(p) == 0: continue
            pool = '0x' + p[-40:]
            liq = eth_call(pool, '0x1a686502', verify=False)
            slot0 = eth_call(pool, '0x3850c7bd', verify=False)
            sq = u(slot0[2:66]) if slot0 and len(slot0) > 66 else 0
            lq = u(liq) if liq else 0
            out.append({'pool': pool, 'fee': fee, 'quote': 'WBNB' if q == WBNB else 'USDT',
                        'liquidity': lq, 'sqrtPriceX96': sq, 'tradable': lq > 0 and sq > 0})
    return out

def v2_pools(tok):
    out = []
    for q in (WBNB, USDT):
        p = eth_call(V2F, '0xe6a43905' + wa(tok) + wa(q))
        if not p or len(p) < 66 or u(p) == 0: continue
        pair = '0x' + p[-40:]
        rs = eth_call(pair, '0x0902f1ac', verify=False)
        r0 = u(rs[2:66]) if rs and len(rs) >= 194 else 0
        r1 = u(rs[66:130]) if rs and len(rs) >= 194 else 0
        out.append({'pool': pair, 'quote': 'WBNB' if q == WBNB else 'USDT',
                    'reserve0': r0, 'reserve1': r1, 'tradable': r0 > 0 and r1 > 0})
    return out

def quote_v3(pool, tok, amount_in, is_token0):
    q = WBNB if pool['quote'] == 'WBNB' else USDT
    data = '0xc6a5026a' + wa(tok) + wa(q) + w64(amount_in) + w64(pool['fee']) + w64(0)
    r = eth_call(QUOTER, data)
    if not r or len(r) < 66: return None
    return u(r[2:66])

def main():
    src = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser('~/agentid-sniper/shadow/signals.jsonl')
    maxn = int(sys.argv[2]) if len(sys.argv) > 2 else 120
    rows = [json.loads(l) for l in open(src) if l.strip()]
    last = {}
    for r in rows:
        if r.get('gated'):
            last[r['tok']] = r
    toks = sorted(last.values(), key=lambda x: -x.get('ageMin', 0))[:maxn]
    print(f'待复核 gated 代币 {len(toks)} 个（来自 {len(rows)} 条信号）')
    res = []
    for i, t in enumerate(toks, 1):
        tok = t['tok']
        v2 = v2_pools(tok); v3 = v3_pools(tok)
        tradable3 = [p for p in v3 if p['tradable']]
        quote = None
        if tradable3:
            best = max(tradable3, key=lambda p: p['liquidity'])
            bal = u(eth_call(tok, '0x70a08231' + wa(best['pool'])))
            if bal > 0:
                amt = bal // 100 if bal // 100 > 0 else bal
                o = quote_v3(best, tok, amt, True)
                quote = {'pool': best['pool'], 'fee': best['fee'], 'amountInRaw': amt, 'amountOutRaw': o}
        res.append({'tok': tok, 'sym': t.get('sym'), 'ageMin': t.get('ageMin'),
                    'mints': t.get('mints'), 'wallets': t.get('wallets'),
                    'agentHolderRatio': t.get('agentHolderRatio'),
                    'v2': v2, 'v3': v3, 'quote': quote})
        if i % 20 == 0: print(f'  ...{i}/{len(toks)}  calls={R.calls}')
        time.sleep(0.05)
    have_v2 = [r for r in res if r['v2']]
    have_v3 = [r for r in res if r['v3']]
    trad = [r for r in res if any(p['tradable'] for p in r['v2']) or any(p['tradable'] for p in r['v3'])]
    quoted = [r for r in res if r['quote'] and r['quote']['amountOutRaw']]
    summary = {'checked': len(res), 'with_v2': len(have_v2), 'with_v3': len(have_v3),
               'tradable': len(trad), 'quoted': len(quoted), 'rpc_calls': R.calls,
               'errors': {f'{k[0]}|{k[1]}': v for k, v in R.errs.most_common(8)}}
    out = {'generated': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'summary': summary, 'tokens': res}
    dest = os.path.expanduser('~/agentid-sniper/analysis/pool-recheck.json')
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    json.dump(out, open(dest, 'w'), ensure_ascii=False, indent=1)
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    if trad:
        for r in trad[:10]:
            print(' POOL', r['sym'], r['tok'], 'v2', len(r['v2']), 'v3', len(r['v3']), 'quote', bool(r['quote']))

if __name__ == '__main__':
    main()
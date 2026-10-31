#!/usr/bin/env python3
"""proxy-trace.py —— 代理合约穿透审计（只读）

问题：我们现在判定 gated 的方法是「合约自身字节码是否包含 Agent ID registry 地址」。
      但代理合约（EIP-1967 / Beacon / EIP-1167 最小代理 / 工厂建仓）自身字节码里没有业务逻辑，
      逻辑在 implementation 里 —— 于是这些项目会被误判成 not gated，直接漏掉。

做法：抽一批「未判定为 gated」的 ERC-721 合约，按 v2.4.2 的 fourCode 思路逐层穿透：
      ① EIP-1967 implementation 槽 0x360894a1…
      ② Beacon 槽 0xa3f0ad74… → implementation()
      ③ EIP-1167 最小代理（363d3d373d3d3d363d73<addr>5af43d82803e903d91602b57fd5bf3）
      ④ 字节码前 1200 字节里写死的 PUSH20 / PUSH32 地址
      然后对 implementation 再查 registry 子串。

只读：只做 eth_getCode / eth_getStorageAt / eth_call，不签名、不交易。
"""
import json, os, re, sys, time, urllib.request
from collections import Counter

RPCS = ['https://bsc-rpc.publicnode.com', 'https://rpc.48.club']
REGISTRY = '60b9262456de011f7d15a54cdd8c48baaa8b022f'
SLOT_IMPL = '0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc'
SLOT_BEACON = '0xa3f0ad74e5423aebfd80d3ef4346578335a9a72aeaee59ff6cb3582b35133d50'
SEL_IMPL = '0x5c60da1b'


class R:
    def __init__(self):
        self.i = 0; self.calls = 0; self.last = 0; self.err = Counter()
    def pace(self):
        d = time.time() - self.last
        if d < 0.2: time.sleep(0.2 - d)
        self.last = time.time()
    def call(self, m, p, tries=3):
        for k in range(tries):
            url = RPCS[(self.i + k) % len(RPCS)]
            self.pace(); self.calls += 1
            try:
                rq = urllib.request.Request(url, json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': m, 'params': p}).encode(),
                                            {'content-type': 'application/json', 'user-agent': 'proxytrace/1'})
                r = json.load(urllib.request.urlopen(rq, timeout=20))
                if 'error' in r: self.err[r['error'].get('message', '')[:24]] += 1; continue
                return r.get('result')
            except Exception as e:
                self.err[str(e)[:24]] += 1
        return None


RPC = R()


def code_of(addr):
    c = RPC.call('eth_getCode', [addr, 'latest'])
    return (c or '0x').lower()


def word_to_addr(w):
    if not w or w == '0x': return None
    v = int(w, 16) if len(w) <= 66 else int(w[:66], 16)
    if v == 0: return None
    return '0x' + w[-40:]


def resolve(code, addr):
    """返回 (implementation 地址列表, 用了哪种机制)"""
    impls, mech = [], []
    if code in ('0x', ''): return impls, mech
    # EIP-1967
    v = RPC.call('eth_getStorageAt', [addr, SLOT_IMPL, 'latest'])
    a = word_to_addr(v)
    if a:
        impls.append(a); mech.append('eip1967')
    # Beacon
    b = word_to_addr(RPC.call('eth_getStorageAt', [addr, SLOT_BEACON, 'latest']))
    if b:
        r = RPC.call('eth_call', [{'to': b, 'data': SEL_IMPL}, 'latest'])
        ia = word_to_addr(r)
        if ia:
            impls.append(ia); mech.append('beacon')
        else:
            impls.append(b); mech.append('beacon-self')
    # EIP-1167 最小代理
    m = re.search(r'363d3d373d3d3d363d73([0-9a-f]{40})5af43d82803e903d9160', code)
    if m:
        impls.append('0x' + m.group(1)); mech.append('eip1167')
    # 字节码里写死的地址（前 1200 字节）
    head = code[:2400]
    for pat in (r'7f000000000000000000000000([0-9a-f]{40})', r'73([0-9a-f]{40})'):
        for a in re.findall(pat, head):
            ca = '0x' + a
            if ca != addr and ca not in impls: impls.append(ca)
    return impls[:4], mech


def main():
    base = os.path.expanduser('~/agentid-sniper')
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 120
    rows = [json.loads(l) for l in open(base + '/shadow/signals.jsonl') if l.strip()]
    # 只要 ERC-721 且未判 gated 的，取最近的
    seen, cand = set(), []
    for r in reversed(rows):
        if r.get('assetType') != 'erc721' or r.get('gated'): continue
        t = r['tok'].lower()
        if t in seen: continue
        seen.add(t); cand.append(r)
        if len(cand) >= limit: break
    print(f'待审计的非 gated ERC-721：{len(cand)} 个', flush=True)

    stat = Counter()
    undercount, proxy_list, small = [], [], []
    for n, r in enumerate(cand, 1):
        t = r['tok'].lower()
        code = code_of(t)
        stat['total'] += 1
        if code in ('0x', ''):
            stat['nocode'] += 1; continue
        # 自身已含 registry 的说明我们本来就判到了，跳过
        if REGISTRY in code:
            stat['selfhit'] += 1; continue
        if len(code) < 4000: stat['small'] += 1; small.append((t, r.get('sym'), len(code)))
        impls, mech = resolve(code, t)
        if not impls: continue
        stat['proxy'] += 1
        hit = False
        for a in impls:
            ic = code_of(a)
            if REGISTRY in ic:
                hit = True; break
        if hit:
            stat['undercount'] += 1
            undercount.append({'tok': t, 'sym': r.get('sym'), 'mech': mech, 'impl': impls[0], 'codeLen': len(code)})
        elif mech:
            proxy_list.append({'tok': t, 'sym': r.get('sym'), 'mech': mech, 'codeLen': len(code)})
        if n % 20 == 0: print(f'  {n}/{len(cand)} proxy={stat["proxy"]} undercount={stat["undercount"]} rpc={RPC.calls}', flush=True)

    out = {'generated': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
           'sampled': len(cand), 'stats': dict(stat),
           'undercountRate': round(stat['undercount'] / max(1, stat['total']), 4),
           'proxyRate': round(stat['proxy'] / max(1, stat['total']), 4),
           'undercount': undercount, 'proxiesWithoutRegistry': proxy_list[:20],
           'rpcCalls': RPC.calls, 'errors': dict(RPC.err.most_common(3))}
    json.dump(out, open(base + '/analysis/proxy-trace.json', 'w'), ensure_ascii=False, indent=1)
    print(json.dumps({k: out[k] for k in ['sampled', 'stats', 'proxyRate', 'undercountRate', 'rpcCalls']}, ensure_ascii=False, indent=1))
    if undercount:
        print('--- 被漏判的代理项目（implementation 含 registry）:')
        for u in undercount[:12]:
            print('   %-12s %s  mech=%s codeLen=%d' % (u['sym'], u['tok'], ','.join(u['mech']), u['codeLen']))


if __name__ == '__main__':
    main()
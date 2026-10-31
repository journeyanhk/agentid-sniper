#!/usr/bin/env python3
"""mint 成本与期望值核算（只读）
1) 从 signals.jsonl 抽最近的 mint 交易，取 receipt 算 gas 成本
2) 结合 element-fills.json 的成交量/价，算每个集合的期望值
    EV/枚 = 售出率 × 均价 × (1−市场费) − mint 成本
"""
import json, os, sys, time, urllib.request
from collections import Counter

RPCS = ['https://rpc.48.club', 'https://bsc.blockrazor.xyz']
class Rpc:
    def __init__(self): self.i=0; self.calls=0; self.last=0; self.err=Counter()
    def pace(self):
        dt=time.time()-self.last
        if dt<0.17: time.sleep(0.17-dt)
        self.last=time.time()
    def call(self,m,p,tries=3):
        for k in range(tries):
            url=RPCS[(self.i+k)%len(RPCS)]; self.pace(); self.calls+=1
            b=json.dumps({'jsonrpc':'2.0','id':1,'method':m,'params':p}).encode()
            try:
                r=json.load(urllib.request.urlopen(urllib.request.Request(url,b,{'content-type':'application/json','user-agent':'ev/1'}),timeout=25))
                if 'error' in r: self.err[r['error'].get('message','')[:30]]+=1; continue
                return r.get('result')
            except Exception as e: self.err[str(e)[:30]]+=1
        return None
R=Rpc()
u=lambda h:int(h,16) if h and h!='0x' else 0

def main():
    sig=os.path.expanduser('~/agentid-sniper/shadow/signals.jsonl')
    ef=os.path.expanduser('~/agentid-sniper/analysis/element-fills.json')
    rows=[json.loads(l) for l in open(sig) if l.strip()]
    fills=json.load(open(ef)) if os.path.exists(ef) else {'tracked':[],'agg':{}}

    # 取每个集合的首次 mint 交易（signals 里存的是 token 地址，不是 mint tx；用 Transfer 里的）
    last={}
    for r in rows:
        if r.get('gated') and r.get('assetType')=='erc721':
            last[r['tok'].lower()]=r
    picks=list(last.values())[-60:]

    # 直接查每个集合最近 2000 块内的铸造交易
    head=u(R.call('eth_blockNumber',[]))
    gas=[]
    for m in picks[:40]:
        tok=m['tok']
        lg=R.call('eth_getLogs',[{'address':tok,'fromBlock':hex(head-2000),'toBlock':hex(head),
                                  'topics':['0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef','0x'+'0'*64]}])
        if not lg: continue
        rc=R.call('eth_getTransactionReceipt',[lg[0]['transactionHash']])
        if not rc: continue
        g=u(rc.get('gasUsed')); px=u(rc.get('effectiveGasPrice'))
        if g and px: gas.append({'tok':tok,'sym':m.get('sym'),'gasBnb':g*px/1e18,'gasUsed':g,'gwei':px/1e9})
    gas.sort(key=lambda x:x['gasBnb'])
    gs=[x['gasBnb'] for x in gas]
    med=gs[len(gs)//2] if gs else None

    out={'generated':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),
         'mint_gas_samples':len(gs),
         'mint_gas_median_bnb':round(med,8) if med else None,
         'mint_gas_min_bnb':round(min(gs),8) if gs else None,
         'mint_gas_max_bnb':round(max(gs),8) if gs else None,
         'mint_gas_median_gwei':round(sorted(x['gwei'] for x in gas)[len(gas)//2],4) if gas else None,
         'rpcCalls':R.calls,'errors':dict(R.err.most_common(3))}
    # 期望值：市场费按 2%（Fill 里 w11/fee 约为 price 的 2%）
    FEE=0.02
    ev=[]
    for t in fills.get('tracked',[]):
        if t['sales']<=0: continue
        avg=t['volumeBnb']/t['sales']
        st=t['sellThrough']
        net=avg*(1-FEE)*(1e9)  # 用小单位便于读
        gas_=med or 0.0003
        ev.append({'sym':t['sym'],'collection':t['collection'],'mints':t['mints'],'sales':t['sales'],
                   'sellThrough':round(st,4),'avgSaleBnb':round(avg,8),
                   'evPerMintBnb':round(st*avg*(1-FEE)-gas_,8),
                   'grossPerSaleUsd_at_700':round(avg*700,4)})
    ev.sort(key=lambda x:-x['evPerMintBnb'])
    out['ev']=ev
    dest=os.path.expanduser('~/agentid-sniper/analysis/mint-ev.json')
    json.dump(out,open(dest,'w'),ensure_ascii=False,indent=1)
    print(json.dumps({k:out[k] for k in out if k!='ev'},ensure_ascii=False,indent=1))
    print('--- 期望值（每枚，单位 BNB）')
    for e in ev[:10]:
        print('  %-8s mints=%4d sales=%3d 售出率 %5.1f%% 均价 %.6f BNB EV/枚 %+.8f'%(
            e['sym'],e['mints'],e['sales'],e['sellThrough']*100,e['avgSaleBnb'],e['evPerMintBnb']))

if __name__=='__main__': main()
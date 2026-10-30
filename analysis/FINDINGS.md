# agentid-sniper 退出路径取证（只读）

生成时间：2026-10-30（Asia/Shanghai）；代码 `a301ca5`；探针 `probe-4.0.0`
证据文件：`analysis/pool-recheck.json`、`analysis/curve-exit.json`
方法：只读 `eth_call` / `eth_getLogs` / `eth_getTransactionByHash`。**未连接钱包、未执行 baw、未签名、未 approve、未 mint、未 sell。**

## 1. 结论

| 问题 | 答案 |
|---|---|
| 这些「打新标的」是什么 | **ERC-721 NFT**（不是 ERC-20） |
| 有无 PancakeSwap V2/V3 池子 | V2 池 2 个但储备为 0；V3 池 **0 个**；可交易 **0/80** |
| mint 价格 | 抽样 29 笔全部 `value=0`（免费，只有 gas） |
| 二级转移 | 30 个样本里只有 6 个有非 mint 转移；接收方 **全部是 EOA（code=0）**，不是 marketplace 合约 |
| 原策略的「3x/5x 止盈」有无市场 | **没有 AMM 市场**；观测窗口内也没有 marketplace 交互 |

因此：**原代码里的 Curve/sell 退出假设在观测到的样本上不成立**——标的不是同质化代币，没有池子，也没有可被 `sell()` 卖进的曲线合约。想退出只能走 NFT 市场挂单，而我们在链上**尚未观测到任何挂单/成交证据**。

## 2. 关键证据

### 2.1 资产类型（决定性）

| 合约 | `decimals()` | `ownerOf(1)` | Transfer 日志 | 判定 |
|---|---|---|---|---|
| HIRE `0x6b1d77…9bea` | revert | 返回持有者 | 4 topics / data 0 字节 | ERC-721 |
| SPOKELY `0x3f5abf…0cbb` | revert | 返回持有者 | 4 topics / data 0 字节 | ERC-721 |
| SOLVD `0x59661d…4076` | revert | 返回持有者 | 4 topics / data 0 字节 | ERC-721 |

ERC-20 的 Transfer 是 3 topics + 32 字节 data；这些合约是 4 topics + 0 字节 data，即 `Transfer(from,to,tokenId)`。

### 2.2 池子重测（80 个历史 gated 合约）

```
checked 80 | with_v2 2 | with_v3 0 | tradable 0 | quoted 0 | rpc_calls 1600 | errors {}
```
两个 V2 交易对的储备均为 0（`getReserves` 返回 0/0），不存在可成交深度。

> 注意：这一结论此前是「靠运气」得到的。`probe-3.0.0` 里判断返回值用了 `p.length <= 66`，而 32 字节返回值长度恰好是 66，导致**所有池子查询都被当成空值跳过**。已修为 `< 66`。修复后重测，结论不变（0 可交易），但依据现在是真的。

### 2.3 二级市场（30 个样本）

```
有非 mint 转移的合约 6/30，转移总笔数 262
接收方抽样（HIRE）：0x0a65e9… x46 code=0、0xc30d41… x6 code=0 …
接收方抽样（SOLVD）：0xf44feb… x34 code=0、0x57125b… x32 code=0 …
```
接收方代码长度全部为 0 —— 没有 marketplace 合约参与，属于钱包间转移（搬砖/分发），不是成交。

## 3. 对策略的含义

1. **不要再找 `sell()`**。没有 curve，没有 AMM 池，退出的前提是 NFT 市场存在挂单与买盘，这一点目前没有证据。
2. **成本结构变了**：mint 免费，成本只有 gas；但「3x/5x 止盈」这类基于价格的规则在没有市场时无法执行。
3. **资格门槛是真的**：抽样铸造钱包持 Agent ID 比例 100%，`agentHolderRatio` 中位数 1.0。门槛真实，但有门槛 ≠ 有利润。
4. 若要继续，必须先回答：这些 NFT 在 PancakeSwap NFT / Element 等市场**是否有挂单价与成交量**。这需要另一套数据（市场 API 或事件日志），不再属于「追新 mint」。

## 4. 未验证 / 局限

- 样本窗口短（约 25–40 分钟、3000 块），不排除个别项目后续上架或建池。
- 未验证 NFT marketplace 的挂单簿（需要市场方 API 或额外事件源）。
- `agentHolders` 仍是**当前余额快照**（免费 RPC 无 archive，`agentBlock` 记 `latest`），不是铸造时点资格。
- 未做行为级门槛验证（有/无 Agent ID 钱包各模拟一次 mint 的行为差异），`behavioralGateVerified` 恒为 false，因此 A 级信号恒为 0。
# P0 报告：BSC PancakeSwap Infinity 只读观察（V4 类）

- **状态：closed（归档）**
- **结论码：`identity_link_not_established`**
- 范围：BSC PancakeSwap Infinity CLPoolManager `0xa0ffb9c1ce1fe56963b0321b32e7a0302114058b`，只读，未接 Robinhood。
- 窗口：`130769296..131153193`（2026-10-31T01:30Z → 2026-11-02T01:30Z，2880 分钟），即 W0 终局窗口。
- 未触碰：`shadow/probe.mjs`、W0 终局文件、现有 systemd 服务（全程未重启）。
- 代码：`analysis/market/{evidence.py, replay.py, v4_decode.py, v4_scan.py, v4_link.py, chain-config.json}`。

> Decision: closed · Reason: `identity_link_not_established` · No change to W0 verdict · No execution path enabled
> V4 数据是历史回放：`dataComplete=true`、`dataFresh=false`，不是实时监控。

## 结论（决策级）

**P0 没有带来可计量的退出增量。** 在当前检查范围和可验证字段内，V4/Infinity 与我们的 Agent ID NFT 退出研究之间**没有建立**可验证的同一性链接。

措辞是「未建立」而不是「不存在」：严格地址、池子 currency、符号、弱名称这四个范围内检查过，未发现链接；发射台登记表、Element 元数据、trace/同部署者、项目外部元数据等来源**没有检查**，因此不能据此断言链上或外部元数据中不存在任何间接关联。

结构性原因：V4 池子交易 ERC-20，Element 成交 ERC-721，两个场所不共享可识别对象。缺的是一条可靠链路：

```
V4 pool → ERC-20 token → Agent ID ERC-721 collection
```

| 检查 | 结果 |
|---|---|
| W0 窗口内新建 V4 池子 | 371 个（`dataComplete=true`，`skipped=0`） |
| 池子 token 地址 == Element collection 地址 | **0 / 371** |
| 符号完全相同（归一化后） | **0 / 371** |
| 弱名称包含（≥4 字符子串，`weak_candidate`） | 3 个，全部不达标（见下） |
| 近 ≈6 天全部 V4 池子（1,232 个 / 745 个不同 currency） | 与 55 个 collection 地址交集 **0** |

弱名称候选（标为 `weak_candidate`，**不是** `possible_same_collection`，不计入判据）：

| token 符号 | collection | collection fills | 独立买家 | 是否达标（fills≥20 且 buyers≥20） |
|---|---|---|---|---|
| FAGENT / BAGENT | AGENT `0x02a3…ceb4` | 1 | 1 | 否 |
| MARSI | MARSID `0xc405…91c8` | 42 | 11 | 否（买家 11 < 20） |

即使按最宽松的名称启发式，也没有任何一个 collection 同时满足「V4 先发现 + 后续 ≥20 笔 Element fills + ≥20 独立买家」。严格口径（地址同一）下，**合格集合数 = 0**。

## 边界：哪些来源没检查

以下路径**未**纳入本次判定，因此结论只能是「未建立」：

- 发射台官方登记表（token 与 collection 的项目级绑定）；
- Element collection 元数据/API 中的 token 关联字段；
- `same_deployer`（需要 trace 或创建交易，免费 RPC 不可得）；
- 合约内部 mapping、代理/工厂间接关系；
- 项目官网或社交资料中的 token 绑定声明。

要继续推进这条假设，必须先拿到上述任一类**可验证**来源；靠同名、相似 symbol、弱 ABI 关系继续猜，只会重新进入无法证伪的身份判断。

## 这为什么不是「再跑久一点」能解决的

- V4 的 `currency0/currency1` 是 ERC-20 合约地址（371 个池子、258 个不同 token，全部能取到 `decimals()`/`symbol()`）；
- Element 的 `collection` 是 ERC-721 合约（55 个，符号如 `SNAKEID`/`MARSID`/`SIGIL`/`VEthe`）；
- 两者既没有地址交集，也没有符号交集；`same_deployer` 需要 trace，免费 RPC 不可得，因此**不给**（不臆造）。

继续扩大扫描只会得到更多 `unlinked`；增量不来自数据量，而来自缺失的身份链路。

## 已交付并验证的工程

### 1. 最小 evidence envelope（`evidence.py`）

字段：`envelope/runId/chain/chainId/source{kind,endpoints,poolManager}/decoderVersion/window{fromBlock,toBlock,fromTimestamp,toTimestamp,blockMinutes}/chunks{requested,ok,retried,skipped[],minSize,maxSize,logsTotal,chunkHashes[],chunksHash}/dataComplete/dataFresh{headBlock,lagBlocks,lagMinutes,ok}/counts/hashes{input,output}/caveats[]`。

- `dataComplete` 只由分片台账推导（`ok == requested` 且 `skipped` 为空），不能由调用方声明；
- `dataFresh` 独立：本次 W0 窗口扫描 `dataComplete=true` 而 `dataFresh=false`（lag 257,090 块 / 1,928.7 分钟）——两个标志语义确实分离，历史回放不会被写成实时监控；
- 每片单独 `hash`（该片原始日志的 canonical sha256），另给 `chunksHash` 串联；`recordsHash` 逐行 canonical 串联，行序变化即变。

### 2. 固定回放测试（`replay.py`）

`--from-block 131402000 --to-block 131404000 --chunk 500 --runs 2`：

```
run1 outputHash f10b9f30… / run2 相同，chunksHash 53db632c… 相同
replay: PASS   （records 7,238、pools 189、dataComplete true、skipped 0）
```

该窗口 2,000 块（≈15 分钟）内 7,238 笔 Swap、189 个活跃池 → 该场所约 8 笔/秒，发现不是瓶颈，链接才是。

`replay.py --verify DIR` 另可校验落盘自洽：`recordsHash` 与 envelope 一致、文件 sha 与 `SHA256SUMS` 一致、`dataComplete` 与台账一致。

### 3. V4 解码（`v4_decode.py`，BigInt / Decimal 定点）

字段位置不是猜的，用真实日志的不变量验证：

| 事件 | 布局 | 验证 |
|---|---|---|
| Initialize | `topics[1]=poolId, topics[2]=currency0, topics[3]=currency1`；`data[0]=hooks`，`data[-2]=sqrtPriceX96`，`data[-1]=tick` | 101/101 条满足 `currency0 < currency1`；101/101 条 `tick` 与 `sqrtPriceX96` 反算一致 |
| Swap | `topics[1]=poolId, topics[2]=sender`；`data=[amount0, amount1, sqrtPriceX96, liquidity, tick, feeRaw, protocolFeeRaw]` | 27,976/27,976 条 tick 一致、liquidity < 2^128；27,973/27,976 条 amount0/amount1 符号相反 |

中间字段（`fee`/`tickSpacing`）**不猜语义**：原样存入 `unresolvedInitWords`，`fieldStatus` 标 `unresolved`。这不是遗漏，是拒绝编造。

实测抓到一个会静默错 5 个数量级的 bug：tick 换算写成 `ln(0.0001)` 而非 `ln(1.0001)`，把 179,883 算成 −2。修好后加了两道自检（`tickCheckedAtInit` / `tickMismatchAtInit`），本次 371/371 一致、0 不符。

### 4. 扫描器（`v4_scan.py`）

- 分片 + 自适应缩片（遇 range limit 自动减半，最低 200 块）+ 3 次重试 + RPC 轮换；
- 失败必留痕：`skipped[{fromBlock,toBlock,reason}]`，绝不静默丢段；
- `decimals` 走链上 `decimals()`，查不到即 `null`（不默认 18）；quote 资产（BNB/WBNB/USDT/USDC）取配置；
- 参与者：Swap 的 `sender` 实测 `== tx.to`、`!= tx.from`（抽样 5/5），是路由/池子调用者，因此 `participant=null`、`participantReason=sender_is_router_or_pool_manager_caller`；**unique buyers 保持 null**，不用 1 或 Router 地址填充；
- 五态分离：本次 `poolDiscovered=371`、`priceComputable=371`、`swapObserved=0`（只扫 Initialize）、`liquidityObserved=unknown`、`executionTradable=unknown`（`no_quote_or_simulation_adapter_in_p0`）。

**覆盖范围说明**：本次只扫了 W0 窗口的 `Initialize`。这足以完成**地址级身份筛选**（判断是否有新建池子与已知 55 个 ERC-721 collection 地址直接重合），但**不等价于完整的 V4 成交量回放**——不能据此描述这些池子在 W0 内的成交规模。鉴于地址交集为 0，继续扫全窗口 Swap 对当前身份结论不会产生实质增量。

### 5. 链接器（`v4_link.py`）

分级：`exact_address`（可算同一 collection）/ `same_deployer`（需 trace，P0 一律 null）/ `symbol_only`（弱，仅 `weak_candidate`）/ `unlinked`。只有第一级计入判据。

## 复现

```bash
cd analysis/market
python3 replay.py --from-block 131402000 --to-block 131404000 --chunk 500 --runs 2   # 回放测试
python3 v4_scan.py --events init --from-block 130769296 --to-block 131153193 \
        --chunk 2000 --retries 4 --out out/w0-init                                # W0 窗口池子
python3 v4_link.py --pools out/w0-init/pools.json --rows ../element-fills-w0-rows.jsonl \
        --out out/w0-init/links.json
```

本次产物 hash（可对照重跑）：`outputHash=f3ddf4d4…`、`chunksHash=83213ffc…`、`inputHash=80a36b61…`、`pools.json=d3c870bd…`。归档在 `analysis/market/artifacts/w0-init/`，`replay.py --verify` 可自校验。

## 限制（必须一起读）

1. W0 窗口只扫了 `Initialize`，没有全窗口 Swap 回放（见上「覆盖范围说明」）；
2. 窗口内活跃但创建早于窗口的池子，地址交集由「近 6 天 1,232 个池子」那次扫描覆盖，符号层面未覆盖；
3. `same_deployer` 未实现（免费 RPC 无 trace），「同一部署者同时发 NFT 与 ERC-20」这条路径**未被排除**；
4. 价格是池子中间价（`sqrtPriceX96`），不是可成交价，未换算 USD；
5. `fee/tickSpacing` 语义未断言；
6. 样本只覆盖 W0 那 55 个有成交的 collection（无成交的 collection 不满足 ≥20 笔判据，不影响结论）；
7. 上面「边界：哪些来源没检查」列出的五类来源均未纳入。

## 决定（已拍板）

1. **P0 关闭归档**：保留报告、解码器、replay 测试、evidence envelope 与 hash 归档，不接入 `shadow/probe`、W1 判决或自动交易。
2. **不启动 P1**（风险信号），**不接** Robinhood Chain、手机 Relay、AI 插件、交易执行。
3. **不追 token↔NFT 映射源**，**不转** token 级领先性研究——后者是另一个假设，需要重新预注册样本/窗口/EV/买家/退出定义。
4. **商业路线维持 C**：不接钱包、不执行 `baw`、不 mint/sell/approve、不买付费 RPC、不进入执行器。
5. W0 判决不变；本报告不改变任何执行路径。
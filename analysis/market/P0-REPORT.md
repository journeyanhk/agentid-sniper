# P0 报告：BSC PancakeSwap Infinity 只读观察（V4 类）

- 范围：BSC PancakeSwap Infinity CLPoolManager `0xa0ffb9c1ce1fe56963b0321b32e7a0302114058b`，只读，未接 Robinhood。
- 窗口：`130769296..131153193`（2026-10-31T01:30Z → 2026-11-02T01:30Z，2880 分钟），即 W0 终局窗口。
- 未触碰：`shadow/probe.mjs`、W0 终局文件、现有 systemd 服务（全程未重启）。
- 代码：`analysis/market/{evidence.py, replay.py, v4_decode.py, v4_scan.py, v4_link.py, chain-config.json}`。

## 结论（决策级）

**P0 没有带来可计量的退出增量。** 不是扫描失败，而是**同一性链接不存在（`identity_link_absent`）**：
BSC 上 V4 池子交易的是 ERC-20，Element 成交的是 ERC-721，两者之间没有可验证的同一性。

| 检查 | 结果 |
|---|---|
| W0 窗口内新建 V4 池子 | 371 个（`dataComplete=true`，`skipped=0`） |
| 池子 token 地址 == Element collection 地址 | **0 / 371** |
| 符号完全相同（归一化后） | **0 / 371** |
| 弱名称包含（≥4 字符子串） | 3 个候选，全部不达标（见下） |
| 近 ≈6 天全部 V4 池子（1,232 个 / 745 个不同 currency） | 与 55 个 collection 地址交集 **0** |

弱名称候选（**不构成同一性，不计入判据**）：

| token 符号 | collection | collection fills | 独立买家 | 是否达标（fills≥20 且 buyers≥20） |
|---|---|---|---|---|
| FAGENT / BAGENT | AGENT `0x02a3…ceb4` | 1 | 1 | 否 |
| MARSI | MARSID `0xc405…91c8` | 42 | 11 | 否（买家 11 < 20） |

即：即使按最宽松的名称启发式，也没有任何一个 collection 同时满足「V4 先发现 + 后续 ≥20 笔 Element fills + ≥20 独立买家」。
严格口径（地址同一）下，**合格集合数 = 0**。

## 这为什么不是「再跑久一点就会有的问题」

「V4 早于 Element 发现同一 collection」这句话预设了两个场所共享一个可识别的对象。实测不成立：

- V4 的 `currency0/currency1` 是 ERC-20 合约地址（371 个池子、258 个不同 token，全部能取到 `decimals()`/`symbol()`）；
- Element 的 `collection` 是 ERC-721 合约（55 个，符号如 `SNAKEID`/`MARSID`/`SIGIL`/`VEthe`）；
- 两者既没有地址交集，也没有符号交集；`same_deployer` 需要 trace/创建交易，免费 RPC 不可得，因此**不给**（不臆造）。

所以 P0 的成功判据在 BSC 这个组合上**不可判定**，而不是「暂时没达标」。继续跑只会得到更多 `unlinked`。

## 已交付并验证的工程

### 1. 最小 evidence envelope（`evidence.py`）

字段：`envelope/runId/chain/chainId/source{kind,endpoints,poolManager}/decoderVersion/window{fromBlock,toBlock,fromTimestamp,toTimestamp,blockMinutes}/chunks{requested,ok,retried,skipped[],minSize,maxSize,logsTotal,chunkHashes[],chunksHash}/dataComplete/dataFresh{headBlock,lagBlocks,lagMinutes,ok}/counts/hashes{input,output}/caveats[]`。

- `dataComplete` 只由分片台账推导（`ok == requested` 且 `skipped` 为空），不能由调用方声明；
- `dataFresh` 独立：本次 W0 窗口扫描 `dataComplete=true` 而 `dataFresh=false`（lag 257,090 块 / 1,928.7 分钟）——两个标志语义确实分离；
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
- 参与者：Swap 的 `sender` 实测 `== tx.to`、`!= tx.from`（抽样 5/5），是路由/池子调用者，因此 `participant=null`、`participantReason=sender_is_router_or_pool_manager_caller`，**不参与任何独立买家统计**；
- 五态分离：本次 `poolDiscovered=371`、`priceComputable=371`、`swapObserved=0`（只扫 Initialize）、`liquidityObserved=unknown`、`executionTradable=unknown`（`no_quote_or_simulation_adapter_in_p0`）。

### 5. 链接器（`v4_link.py`）

分级：`exact_address`（可算同一 collection）/ `same_deployer`（需 trace，P0 一律 null）/ `symbol_only`（弱，仅候选）/ `unlinked`。只有第一级计入判据。

## 复现

```bash
cd analysis/market
python3 replay.py --from-block 131402000 --to-block 131404000 --chunk 500 --runs 2   # 回放测试
python3 v4_scan.py --events init --from-block 130769296 --to-block 131153193 \
        --chunk 2000 --retries 4 --out out/w0-init                                # W0 窗口池子
python3 v4_link.py --pools out/w0-init/pools.json --rows ../element-fills-w0-rows.jsonl \
        --out out/w0-init/links.json
```

本次产物 hash（可对照重跑）：`outputHash=f3ddf4d4…`、`chunksHash=83213ffc…`、`inputHash=80a36b61…`、`pools.json=d3c870bd…`。

## 限制（必须一起读）

1. W0 窗口只扫了 `Initialize`，没扫全窗口 Swap（48h 约 130 万笔，P0 不需要）；
2. 窗口内活跃但创建早于窗口的池子，地址交集由「近 6 天 1,232 个池子」那次扫描覆盖，符号层面未覆盖；
3. `same_deployer` 未实现（免费 RPC 无 trace），所以「同一个部署者同时发 NFT 与 ERC-20」这条路径没被排除；
4. 价格是池子中间价（`sqrtPriceX96`），不是可成交价，未换算 USD；
5. `fee/tickSpacing` 语义未断言；
6. 样本只覆盖 W0 那 55 个有成交的 collection（无成交的 collection 不满足 ≥20 笔判据，不影响结论）。

## 建议

1. **停止在 P0 上继续加扫描量**——它不是数据不够，是对象不同一。
2. 若确实要追这个假设，只有两条路：
   - 拿到 **token↔NFT 映射源**（发射台登记表 / Element 元数据 API），把 `same_deployer`、`exact_address` 之外的层级补上；
   - 把判据改成**同层可比**：V4 池子创建 → 后续该 token 的流动性/成交是否放大（token 级领先性），这用现有工具就能测，但那是另一个假设，需要重新预注册。
3. P1（风险信号）、Robinhood Chain、手机 Relay、AI 插件、交易执行：按批准口径继续暂缓，不启动。
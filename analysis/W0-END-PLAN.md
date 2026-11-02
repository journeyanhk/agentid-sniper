# W0 收尾与 probe-5.x 合并闸门

决策（2026-11-01）：**A 方案**——当前 48h 窗口（2026-10-31 09:30 → 2026-11-02 09:30 CST）跑满为止，
`main` 的 `shadow/probe.mjs` 保持 probe-4.0.0 冻结，不合并 gated 行为补丁。
分支 `feat/gated-indirect` 可以继续只读验证，但不得影响在跑的窗口。

约束不变：不接钱包、不执行 baw、不 mint/sell/approve、不买付费 RPC、不启用自动铸造。

## 一、W0 结束时必须固化（一条命令）

```bash
cd ~/agentid-sniper && python3 analysis/w0-baseline.py 48
```

产出 `analysis/w0-baseline.json`，覆盖评审要求的全部字段：

- 窗口四要素：`fromBlock / toBlock / fromTs / toTs / windowHours / rounds / probeVersion / build`
- 完整性：`hadGapRounds / gapCountSum / gapFileEntries / pendingMax / pendingOverflowMax /
  stateCorrupt / cursorPausedRounds / zeroDisagreements / incompleteSignals / lagMax / msAvg / msP95`
- 规模：`signals / assetType / gatedTotal / gatedErc721 / byClass`
- 成交：`fills / collectionsTraded / volumeBnb / uniqueBuyers / uniqueSellers`
- `cohortEv`（若 `analysis/cohort-latest.json` 存在）

固化了就不要再覆盖：合并新版本后重新开一轮窗口，新旧不混。

## 二、合并闸门（probe-5.x → main）

1. `PROBE_SELFTEST=1 node shadow/probe.mjs` 全绿（或仅环境性 skip），`selftest.json` 留档；
2. 行为判据必须满足：真实 mint calldata、正样本（铸造者）、负样本（无 Agent ID 地址）、
   三态 `gated / open / unknown`、unknown 不猜；
3. holder ratio 只能作辅助证据（见 `analysis/gated-recall.py` 顶部警示：该脚本是相关性，不是证明）；
4. 合并后新开 48h 窗口，`v` 字段区分版本，跨版本不比较 gated 率。

## 三、probe-5.2 待办（已知缺陷）

- **正样本失效**：每钱包限额的项目里，原铸造者已用完额度会 revert，而空地址反而成功，
  当前会落 `unknown/nonholder_only`。自检已实测两例。下一版正样本改为
  「持有 Agent ID 但未铸造过本项目」的地址，从 registry 持仓里取，不依赖铸造者。
- **open 对照稀缺**：这类盘 mint 窗口常在数分钟内关闭，事后补测正样本也 revert，
  所以行为判据只对「mint 还开着」的候选有效——这恰好是真正要下手的时点，但意味着
  历史回填不可行，不能拿它重算旧窗口。

## 四、作者 v2.5.4 只借鉴三项（均属执行器/账本，当前不落地）

1. **进度异常值保护**：`sold < 0 → 0`、`cap <= 0 → unknown`、`sold > cap → progress=null,
   progressStatus=inconsistent`。不把异常压成 100%，异常项目只记录、不自动买。
2. **低频进度检查 + 快速队列**：未达门槛的免费项目降到 ~10 分钟一次，加 8–12 分钟随机抖动，
   避免整点齐发；接近门槛时切快速队列。
3. **missed 分类**：拆分 `missed_due_to_system / missed_due_to_strategy / skipped_by_risk_rule /
   unmintable / unknown`，不把风控主动跳过算成系统漏打。

暂不移植：自动更新（含手动 `/update`、UPDATE 文件触发）、30/40/60% 激进进场、`forceMint`、
replay 主路径、多钱包同步加仓。我们的 fork 一律 `UPD.on = false`、固定 commit、人工审计后部署。

## 五、当前证据状态（2026-11-01 02:30Z 前后）

- 市场冷却中：全市场 Element fill 877 → 420/12h，跟踪集合成交 341 → 120/12h；
- MORI 仍是唯一有正 EV 的系列，但买家 62 → 20、tts 175m → 777m；
- 尚未出现「≥2 集合 × ≥2 连续窗口 × 独立买家 ≥20 × 扣费后 EV 持续为正」的组合；
- 结论仍偏 **B（有成交、不足以证明可重复正收益）**，到点再判。

---

## 终点操作流程（2026-11-01 修订，必须按顺序执行）

终点时间：**2026-11-02 09:30 CST = 2026-11-02T01:30:00Z**，窗口起点固定
**2026-10-31T01:30:00Z**（该时刻起 probe 已全程为 `probe-4.0.0`，48h 内无版本混用）。

```bash
cd ~/agentid-sniper

# 1) 成交快照：显式窗口，必须 requested=scanned、skipped=0、ratio=1.0
python3 analysis/element-watch.py \
  --from-block <窗口起始块> --to-block <窗口结束块> \
  --output analysis/element-fills-w0.json

# 2) 基线：窗口单一来源取自上面这份快照
python3 analysis/w0-baseline.py --fills analysis/element-fills-w0.json \
  --out analysis/w0-baseline.json

# 3) 只有 STATUS=READY_FOR_DECISION 才允许给 A/B/C；否则先修数据，不许人工脑补
```

闸门（缺一即 BLOCKED，只写 `.invalid.json`）：
G1 快照新鲜 ≤13h + dataComplete + skippedRanges 为空；G2 metrics 块覆盖含两端；
G3 version/build 唯一；G4 incomplete/gap/pending/stateCorrupt 归零；
G5 probe 文件 SHA-256 与 git 提交可溯源；G6 四个输入文件 hash 齐全。

### 口径纪律（此前踩过）

- **信号按行数会虚高**：24h 窗口 rows 4202，但唯一事件 1700、重复行 2502（60%）。
  任何「gated 数量」必须写成 `gatedErc721Contracts`（唯一合约），不能报行数。
- 1128（旧报告）是行数，与 446（旧文档）不同分母，不得并列比较。
- `dataComplete` 与 `dataFresh` 分开：lagMax 1705 块（12.8 分钟）说明数据最终完整、
  但实时性降级；历史研究可用，执行器不可用。
- 解码正确性用「同一 tx 内原生 Fill 价格之和 == tx.value」校验（逐笔比会误判，
  一个 tx 常含多笔 Fill）：12 words 18/18、14 words 296/300。

### 仍未完成（不阻塞 A/B/C，但写进结论）

1. 探针冻结期内无法每轮写 `probeSha256`/`collectorVersion`/`schemaVersion`，
   当前基线用「工作树 SHA-256 + git 提交」溯源，来源已标注 `working-tree-at-baseline-time`；
   解冻后第一件事是给 metrics 加这四个字段。
2. 分支 `feat/gated-indirect`（probe-5.2.1）不合并，等 A/B/C 判定后单独评审。
---

## W0 终局归档口径（2026-11-02 定稿）

### 一、结论

```
商业路线：C —— 停止自动盈利开发，不接钱包，不做 W1 执行器
工程收尾：A —— 继续做纯只读工具（mint 回放 / 证据归档 / 买家质量 / manifest）
```

判 C 不等于「没有市场」，而是：

```
成交存在（48h 1350 笔 / 2.028 BNB / 55 个集合）
但集合集中、独立买家不足、正 EV 样本不足，退出仍是核心瓶颈
```

### 二、三层分母（禁止混用）

| 范围 | 成交 | 集合 | 用途 |
|---|---:|---:|---|
| ① Element 全扫描范围 | 1350（原生 1267 / ERC20 83） | 55 | 市场基线，**不可外推给任一 gated 集合** |
| ② 跟踪集合内有成交 | 242 | 21 | cohort 分析 |
| ③ 可算 EV（supply 可得） | — | 12 | 经济性判定 |
| ③′ 缺 totalSupply | — | 9（CZID/SNAKEID/RUNE/DWG/MUSE/GLAD/AGENT/FREAK/ENIG） | 标 `evUnknown`，不纳入正 EV |

### 三、EV 命名纪律

- `grossEvPerMint` = 卖出率 × 均价，**未扣费，不得称 net**。
- `netEvPerMint` = 卖出率 × 均价 × (1 − Element fee 2% − royalty) − mintGas − 卖出率 × sellGas。
- royalty 未知按 0 计 → 结果偏乐观，输出中必须带 `royaltyNote`。
- 48h 实测：MORI gross +$0.0715 → **net +$0.0626**；FARMER 由 gross +$0.0064 翻为 net −$0.0011。
- net EV>0 的集合：1 个（MORI，买家 12）；net EV>0 且买家≥20、成交≥20：**0 个**。

### 四、重启恢复证据（`analysis/restart-audit.json`）

5 处时间断层（两次 dev 期重启 + 三次含 11-01 05:04Z/06:24Z），**全部 Δ=+1 连续**：

```
空洞 0 块、回扫重叠 0 次、崩溃前 pending=0、重启后 stateCorrupt=false、signal 前后 10 分钟连续
→ restartRecovery = verified
```

归档措辞：`dataComplete=true, dataFresh=false, restartRecovery=verified`。

### 五、mint 回放口径（`analysis/mint-replay.py`）

- 判据：`receipt.status==1` ∧ 同 receipt 内 `Transfer(from=0x0, to=收款人, tokenId, 目标集合)`。
- **不能把 `tx.from` 当 minter**：全市场 90 分钟样本里 73/120 笔是合约/中继代发（`mediated`）；
  21 个有成交集合的近 6h 样本 150/150 全为 `direct`，141 个发送者、top1 仅 1.3%。
- 字段：`sender / recipients / mediated / receivedTokenIds / transferToMinter / paymentValue /
  gasUsed / gasCostNative / mintOutcome(direct|mediated|failed|reverted_no_token)`。

### 六、7 日复查触发条件（修订版）

必须**同时**满足，且「7 日」只是复查节奏，不替代窗口标准：

```
连续两个完整 12h/24h 窗口，dataComplete=true（dataFresh 降级需记录）
≥2 个 collection
每个 collection unique buyers ≥20 且有 ≥20 笔 marketplace fills
非自成交、repeat pair 占比 <20%、buyer/seller overlap 低
扣 Gas、平台费、版税后 net EV > 0
无严重数据缺口（gap/pending/skipped 全 0）
```

### 七、买家指标命名纪律

统一用 `buyerQualitySignals` 分组，**禁止**叫 `realBuyers`：

```
buyerCount / uniqueBuyerCount / buyerWithHistoryCount / buyerWithNonceAboveThreshold /
freshBuyerCount / repeatBuyerCount / buyerSellerOverlap / top1BuyerShare / top10BuyerShare /
repeatPairCount / selfTradeCount
```

真实含义是 heuristic classification，不是身份验证。---

## 补证复核后的口径修正（2026-11-02 第二轮）

### 1. 重启连续性：只写可验证项

```
restartRecovery      = verified
scanCursorContinuity = verified
blockHoles           = 0
blockOverlaps        = 0
signalContinuity     = verified
metricsIntegrity     = verified（20678 行，坏行 0）
signalsIntegrity     = verified（19652 行，坏行 0）
recoveryConfidence   = high
```

**不得写成「绝对无损」**：`state.json` 是单文件覆盖写，历史游标不可回读，
只能以「metrics 相邻两轮游标 + signals 文件连续性 + pending 两侧为 0」作为旁证。
每次断层的 `restartAt / lastMetricBefore / firstMetricAfter / blockBefore / blockAfter /
pendingBefore / pendingAfter / lastSignalBeforeRestart / firstSignalAfterRestart` 见
`analysis/restart-audit.json`。

### 2. mint replay 命名纪律

| 名字 | 判据 | 能说明什么 |
|---|---|---|
| `direct` | `tx.from == recipient` | 发送者自己付 gas 自己收款 |
| `sponsored_or_relay_like` | `tx.from != recipient` | **只说明发送者与收款人不同**，不证明是合约 |
| `contract_sender` | 且 `eth_getCode(tx.from, 该块) != 0x` | 有代码才敢叫合约发送者 |
| `unknown_code` | 取不到历史 code | 不做定性 |

行内字段：`txFrom / recipient / recipients / txTo / senderCodeAtBlock / senderKind /
senderMatchesRecipient / receiptStatus / blockNumber / receivedTokenIds / paymentValue /
gasUsed / gasCostNative / mintOutcome`。

五层计数（缺一层就会被误读）：

```
rawEventsMarketWide → rawEvents(限定集合) → uniqueTx → receiptSuccess
→ confirmedTransfer → cohortIncluded        （excluded 逐项列原因）
```

### 3. 174 与 150 的解释（历史记录，防止误读为数据矛盾）

- `174` = 窗口 131115477..131163477（6h）内命中 21 个集合的 unique mint 交易数；
- `150` = 当时命令行 `--limit-tx 150` 的**人为截断**，不是数据缺失；
- 被截断的 24 笔 `excluded.notReplayed_dueToLimit`，数据质量无问题；
- 修正后（limit 400、窗口 131122055..131170055）：
  `rawEvents 149 → uniqueTx 139 → receiptSuccess 139 → confirmedTransfer 139 → cohortIncluded 139`，
  排除项全 0，`direct 139 / senderKind eoa 139`，113 个发送者、top1 2.9%。

### 4. EV 命名

```
grossEvPerMint          未扣费
netKnownCostEvPerMint   扣 mintGas + 卖出 gas + Element 2% 手续费；royalty 未知
royaltyUnknown          = true
evLowerBound            取 royalty=10% 的最不利假设
```

**`netKnownCostEvPerMint` 不是完全净 EV**：MORI 在 royalty 0 / 2.5 / 5 / 7.5 / 10% 下
依次为 +$0.0626 / +0.0609 / +0.0591 / +0.0573 / +0.0555，始终为正，但这是**同一个 12 买家样本**的正，
不改变「买家 < 20」的结论。

覆盖度必须同时报告：

```
collectionCoverage 21/55    saleCoverage 242/1350
volumeCoverage 0.4598/2.0283  evCoverage 12/21
```

### 5. Element 证据归档（已完成）

`analysis/element-archive-w0.json` 内容：输入/输出 SHA-256、窗口块与时间、768 个 chunk 的
逐块记录（`fills`/`status`/`stepUsed`）、retry/skipped、`decoder=element-fill-v3`、RPC 来源、
可复跑命令。补跑一致性：同窗口重扫得到 `fills 1350 / native 1267 / collections 55 /
vol 2.028348 / trackedSales 242`，与归档输入**逐字段相同**。

### 6. buyer-quality 定位

只作为**风险信号**，不得称为 realBuyers，也不得把 `nonce >= 8` 当真人证明。
字段沿用 `buyerQualitySignals` 分组；结论措辞上限是 `no obvious relay concentration`。---

## buyer-quality 结果（2026-11-02，W0 全窗口）

### 数据准备

```
--dump-rows → analysis/element-fills-w0-rows.jsonl（1350 行，逐笔）
             analysis/element-fills-w0-rows.jsonl.meta.json（sha256 / 命令 / decoder / 抽检）
去重键      txHash + logIndex + collection + tokenId
复扫核对    fills 1350 / native 1267 / collections 55 / vol 2.028348 / trackedSales 242 —— 五项全一致
转账抽检    200 笔 receipt 内 ERC-721 Transfer(collection, tokenId, to=buyer)
```

### saleBuyerQuality（全窗口 1350 笔）

| 指标 | 值 |
|---|---:|
| uniqueBuyerCount | 265 |
| selfTradeCount | 0 |
| buyerSellerOverlap | 29%（77 个地址既买又卖） |
| repeatPairCount | 147 对，覆盖 515 笔（38.1%） |
| top1BuyerShare | 8% |
| top10BuyerShare | 33% |
| sameAmountCluster 原始占比 | 84.1% |
| suspiciousClusterFillsShare | **14.6%** |

同价簇前三名（按集合+资产聚类后）：

```
0.0004 BNB  ×35 笔，16 个买家
0.0015 BNB  ×29 笔，17 个买家
0.00013 BNB ×28 笔，7 个买家
```

即：**同价重复主要是地板价成交通常伴随的多买家行为，不是少数地址对刷**。

### 判定

```
flags = [repeatPairShare_high]
verdict = mixed_signals        （不是 low_independence）
```

`sameAmountCluster 84%` 不能直接当刷量证据：NFT 地板价天然重复，真正有判别力的是
`suspiciousClusterFillsShare`（≥5 笔且 ≤2 个买家），该值 14.6%，未达 30% 阈值。

结论边界：

```
能说：没有发现明显的地址集中/对刷结构（no obvious wash structure）
不能说：这批成交就是真实需求；也不能据此推翻 C
C 的依据是 net EV>0 且买家≥20 的集合为 0、独立买家不足、退出规模不足，不是「市场是假的」
```

### mintSenderQuality（另一样本，禁止与上面合并）

```
窗口 131122055..131170055（6h），21 个集合
rawEvents 149 → uniqueTx 139 → receiptSuccess 139 → confirmedTransfer 139 → cohortIncluded 139
direct 100%、relayLike 0%、uniqueSender 113、top1 2.9%
```

### 两段 mint 窗口必须分开写

```
window A: 131115477..131163477 → 174 unique tx（当时 --limit-tx 150 截断，被排除 24 笔）
window B: 131122055..131170055 → rawEvents 149 / uniqueTx 139 / confirmed 139
```

不同窗口的数字不得同行比较；`174` 与 `139` 的差异主要来自窗口不同与截断，不是数据缺失。

### 历史画像

免费 RPC 无 archive，`eth_getTransactionCount(buyer, fillBlock-1)` 不可用，
`historySource=unavailable`，`buyerWithHistoryCount/freshBuyerCount` 保持 null，
**不做推断**；`currentNonceProxy` 仅作标注，不参与任何判定。
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

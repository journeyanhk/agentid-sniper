# W0 终局报告（定稿）

**结论：商业路线 C（停止自动盈利开发）；工程路线 A（只做纯只读工具）。**
**buyer-quality 复核后结论不变，且证据更强：不是"没有明显刷量结构"，而是"洗量/关联交易风险未排除"。**

生成时间 2026-11-02。数据窗口与口径见各节；所有脚本可复跑，命令附在文末。

---

## 1. 窗口与完整性

| 项 | 值 |
|---|---|
| 窗口 | `130769296..131153193`，2026-10-31T01:30Z → 11-02T01:30Z，2880 分钟 |
| Element 分片 | 768 / 768，skipped 0，ratio 1.0，`dataComplete=true` |
| 采集轮次 | 14,241 轮；incomplete 0 / gap 0 / pending 0 / 零值分歧 0 |
| 实时性 | lagMax 1705 块（约 12.8 分钟）→ 该窗口 `dataFresh=false`（只影响实时性，不影响回溯完整性） |

**三层分母**（禁止混用）：

```
全市场       1350 笔 / 55 个集合 / 2.028348 BNB
跟踪集合      242 笔 / 21 个集合 / 0.459844 BNB   （覆盖 21/55 集合、242/1350 笔、22.7% 成交额）
可算 EV       12 / 21 个集合
```

9 个集合因 `totalSupply()` 取不到 → `evUnknown`：CZID / SNAKEID / RUNE / DWG / MUSE / GLAD / AGENT / FREAK / ENIG。

## 2. EV 结论

| 集合 | gross EV/枚 | netKnownCost EV/枚 | 买家数 | 判定 |
|---|---:|---:|---:|---|
| MORI | +$0.0715 | **+$0.0626** | 12 | 唯一 net > 0 |
| FARMER | +$0.0064 | −$0.0011 | — | gross 正、净负 |

MORI royalty 敏感性（0 / 2.5 / 5 / 7.5 / 10%）：
`+0.0626 / +0.0609 / +0.0591 / +0.0573 / +0.0555` —— 即使最不利假设仍为正，但**买家只有 12 个**。

**判定线（预先固定）：`netKnownCostEV > 0` 且 `独立买家 ≥ 20` 且 `成交 ≥ 20 笔` 的集合数 = 0。**
royalty 未知是结构性缺口，不是可忽略项；`royaltyUnknown` 与 `netKnownCost` 必须并列呈现。

## 3. buyer-quality：汇总与集合级（本节推翻上一版表述）

### 3.1 汇总层

| 指标 | 值 |
|---|---:|
| 成交笔数 | 1350（native 1267 / ERC20 83） |
| 唯一买家 | 265 |
| 自成交（buyer == seller） | **0** |
| 买卖重叠 | 29.1%（77 个地址） |
| 重复买卖对 | 147 对，覆盖 515 笔（38.2%） |
| top1 / top10 买家占比 | 8% / 33% |
| 同价簇占比（原始） | 84.1% |
| 可疑同价簇占比 | 14.6% |

### 3.2 集合级（真正有判别力的一层）

`top10 = 33%` 是**跨集合合并的假象**：55 个集合的买家彼此不重叠，合并后自然分散。
按集合拆开：

| 集合 | 笔数 | 买家 | 重叠 | 重复对 | top10 | 可疑簇 | 分类 |
|---|---:|---:|---:|---:|---:|---:|---|
| 0x3048…a18b | 489 | 66 | 0.36 | 0.48 | 0.67 | 0.06 | low-independence-risk |
| 0xb777…b256 | 177 | 31 | 0.32 | 0.09 | 0.75 | 0.03 | low-independence-risk |
| 0x6886…7572 | 80 | 19 | 0.16 | 0.68 | 0.89 | 0.00 | low-independence-risk |
| 0x0532…398a | 58 | **1** | 0.00 | 0.81 | 1.00 | 0.55 | low-independence-risk |
| MORI `0x5eb9…0f89` | 50 | 12 | 0.08 | 0.24 | 0.96 | 0.38 | low-independence-risk |
| 0xbc2d…2b78 | 45 | 16 | 0.19 | 0.71 | 0.87 | 0.24 | low-independence-risk |
| 0xc405…91c8 | 42 | 11 | 0.09 | 0.10 | 0.98 | 0.43 | low-independence-risk |
| TREE `0x2ed9…a0d0` | 31 | 9 | 0.00 | 0.68 | 1.00 | 0.52 | low-independence-risk |
| 0x4add…84a6 | 26 | **2** | 0.00 | 0.00 | 1.00 | 0.00 | low-independence-risk |
| 0xdaf3…dded | 25 | **2** | 0.00 | 0.48 | 1.00 | 0.48 | low-independence-risk |

分类统计：

```
low-independence-risk  24 个集合，覆盖 1259 / 1350 笔（93.3%）
insufficient-sample    31 个集合（< 10 笔，不作分类）
no-material-flag        0 个集合
```

阈值（筛选，非定性）：`repeatPairShare ≥ 30%` 或 `buyerSellerOverlap ≥ 25%` 或 `top10BuyerShare ≥ 50%` 或 `suspiciousClusterShare ≥ 20%`。

### 3.3 表述修正（自我更正）

上一版写的是 `mixed_signals`、"no obvious wash structure"。**这版作废。**

现在能写的：

```
no direct self-trade detected
no single-address concentration at aggregate level (top1 8%, top10 33%)
```

必须同时写的：

```
wash-trade risk unresolved
重复买卖对 38.2%、买卖重叠 29.1%、可疑同价簇 14.6%
集合级：24 个集合（93.3% 成交）触发集中度/重叠阈值；其中 3 个集合买家仅 1–2 个
```

不能写的：

```
无刷量 / 真实需求 / 独立买家 / 真人用户
```

**这条修正让 C 更稳，不是更弱**：独立需求比汇总层看起来的更少。

### 3.4 一个必须写进限制的发现

成交量最大的两个集合（`0x3048…a18b` 489 笔、`0xb777…b256` 177 笔，合计 49.3% 成交）
**不在跟踪的 21 个集合内**。本报告对「跟踪集合」的所有结论，不覆盖市场上最活跃的部分；
`21/55` 的覆盖度不只是数量问题，而是**抽样偏向外围集合**。任何「这个市场怎么样」的推断都必须
以此为限，不能把跟踪集的 EV 结论当作全市场结论。

## 4. mintSenderQuality（另一个窗口，禁止与成交指标合并）

```
窗口 131122055..131170055（6h，21 个集合）
rawEvents 149 → uniqueTx 139 → receiptSuccess 139 → confirmedTransfer 139 → cohortIncluded 139
direct 100% / relayLike 0% / 唯一发送者 113 / top1 2.9%
```

只能说 `no obvious relay concentration`；**不能**推出"113 个发送者 = 113 个真人"，也不能推出"mint 无女巫"。

窗口纪律：sale = 48h，mint = 6h，JSON 与正文都必须标，禁止跨窗口比较。

## 5. 可复现性与证据

| 项 | 值 |
|---|---|
| 逐笔 rows | 1350 行，`sha256=5a873d7e…`，去重键 `txHash+logIndex+collection+tokenId` |
| Transfer 抽检 | `deterministic_hash`，seed `w0-transfer-v1`，200 tx / 覆盖 548 行，`ok 200 / failed 0` |
| 抽检表述 | "200 个 tx（548 行）抽检全部通过"，**不外推 1350 笔** |
| 复跑命令 | `python3 analysis/transfer-verify.py --n 200 --seed w0-transfer-v1` |
| 归档 | `analysis/element-archive-w0.json`：768 分片逐块记录，337 个有成交，0 异常 |
| 重启审计 | 5 处断层全部 Δ=+1，`blockHoles=0`、`blockOverlaps=0`、`recoveryConfidence=high` |
| 历史画像 | `historyAvailability=unavailable`、`freshBuyerMetric=not_computed`（免费 RPC 无 archive，不做推断） |

## 6. 结论边界

能说：

```
当前市场（BSC / Element，48h）没有可重复的、扣费后仍为正的退出收益
买方结构不是集中到单一地址，但集合级重复买卖对与买卖重叠偏高
```

不能说：

```
这个赛道永远不会赚钱
这批成交是刷量
我们的数据有缺失
```

## 7. 不做什么（硬约束，未变）

不接钱包、不执行 `baw`、不 mint / sell / approve、不买付费 RPC、不启用自动铸造、不改自动交易配置、不进 W1 执行器。

## 8. 复跑命令

```bash
python3 analysis/element-watch.py --from-block 130769296 --to-block 131153193 \
  --output /tmp/agg.json --dump-rows analysis/element-fills-w0-rows.jsonl --verify-transfers 200
python3 analysis/transfer-verify.py --n 200 --seed w0-transfer-v1
python3 analysis/buyer-quality.py
python3 analysis/w0-baseline.py
python3 analysis/restart-audit.py
python3 analysis/w0-verdict.py
```

## 9. 发布完整性（工程 A 收尾）

```
闸门        systemd ExecStartPre=/usr/local/libexec/agentid-shadow/verify-release.sh
runtime     2 个文件（shadow/probe.mjs、deploy/systemd/agentid-shadow.service），变化即阻断启动
archive     analysis/*.py、analysis/*.md、deploy/*.sh，只用于归档复现，不阻断启动
可信输入    /etc/agentid-shadow/ 下 manifest-runtime.json、.sig、release-pubkey.pem（root:root 644）
私钥        /data/.secrets/agentid_release_key.pem（memoh 工作区，600），不在部署机、不在仓库
流程        部署机 hash → 本地签名 → 安装 sig → 部署机 verify
```

实测退出码：正常 0 / 改 probe 13 / 删 probe 12 / 改 unit 13 / 改已安装 unit 14 / 改签名 11；
只改 analysis 文档仍为 0（拆分生效）。2026-11-02 17:06:31 CST 受控重启，新 epoch 见
`analysis/EPOCH-post-release-verification.md`，W0 结论未改。

残留下限：部署机被完全控制时可连同 `/etc/agentid-shadow/` 的公钥一起替换（需 root），
本闸门防误改与带外修改，不防 root 沦陷。

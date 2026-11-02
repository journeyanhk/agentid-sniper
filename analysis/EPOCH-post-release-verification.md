# 观察 epoch：post-release-verification

**目的**：记录一次人为重启（安装发布闸门），与已完成的 W0 窗口严格分开。W0 终局结论不受影响、未修改。

```
releaseVerification = enabled
restartReason       = release_gate_install
restartAt           = 2026-11-02 17:06:31 CST（2026-11-02T09:06:31Z）
newObservationEpoch = post-release-verification
previousEpoch       = W0（2026-10-31T01:30Z → 11-02T01:30Z，已归档，不改）
```

## 闸门形态

```
runtime manifest   /etc/agentid-shadow/manifest-runtime.json（root:root 644）
runtime signature  /etc/agentid-shadow/manifest-runtime.sig
公钥               /etc/agentid-shadow/release-pubkey.pem
验证器             /usr/local/libexec/agentid-shadow/verify-runtime.mjs（root:root 755）
包装脚本           /usr/local/libexec/agentid-shadow/verify-release.sh（root:root 755）
unit               ExecStartPre=/usr/local/libexec/agentid-shadow/verify-release.sh
```

runtime 清单只含 2 个文件：`shadow/probe.mjs`、`deploy/systemd/agentid-shadow.service`。
analysis 下的脚本、报告、JSON 一律不进 runtime 清单；它们只进 archive 清单，**不影响启动**。

验证器行为：只读本地文件、不联网、不读私钥、不写任何文件、不自动修复。失败即 `exit` 非 0，systemd 拒绝启动。

## 退出码与实测

| 场景 | 退出码 | 实测 |
|---|---:|---|
| 正常 | 0 | ✅ `release-gate: OK (2 runtime files, gitHead 733134e)` |
| 改 `shadow/probe.mjs` | 13 | ✅ `file hash mismatch` |
| 删 `shadow/probe.mjs` | 12 | ✅ `missing file` |
| 改仓库 unit | 13 | ✅ `file hash mismatch` |
| 改已安装 unit | 14 | ✅ `unit drift` |
| 改签名 | 11 | ✅ `manifest signature invalid` |
| 只改 analysis 文档 | 0 | ✅ 不受影响（验证拆分生效） |
| 全部恢复 | 0 | ✅ |

## 重启过程

```
17:06:31 CST stop  → agentid-shadow Deactivated successfully
17:06:31 CST 单独跑 verify-release.sh → OK, exit 0
17:06:32 CST start → ExecStartPre 输出 release-gate: OK
17:06:32 CST node[3664526] probe-4.0.0 build=733134e start; resume lastBlock=131214039 pending=0
17:06:32 CST head=131214046 lag=2 gap=0 pending=0
```

| 项 | 重启前 | 重启后 |
|---|---|---|
| MainPID | 3601701 | 3664526 |
| NRestarts | 2（11-01 旧崩溃） | 0（手动 start 重置计数器） |
| ActiveEnterTimestamp | 2026-11-01 14:25:05 CST | 2026-11-02 17:06:32 CST |
| `build` 字段 | fa09631 | 733134e |
| `scannedTo` | 131214039 | 131214044（连续，gap=0） |
| pending | 0 | 0 |

**`build` 变化解释**：probe 读的是运行目录的 git HEAD，HEAD 因本轮的文档/脚本提交前进，
但 `shadow/probe.mjs` 的 SHA-256 未变且签名校验通过 —— 属于 HEAD 漂移，不是代码变更。
与 W0 里 `build 4353130 → fa09631` 的良性漂移同性质。

## 数据面检查

```
metrics.jsonl 22324 行，重启前后各一行均完整
新 epoch 首行 t=2026-11-02T09:06:32.520Z，fromBlock=131214040（紧接 lastBlock 131214039）
gap=0 / pending=0 / lagBlocks=2
state.json 可解析，lastBlock=131214044
```

## 边界

- 本 epoch 的数据**不并入** W0 窗口，也不用于重算 W0 的 EV / buyer-quality。
- 后续若更新 `analysis/*`：不触发重启，也不需要重签 runtime。
- 后续若更新 `shadow/probe.mjs` 或 unit：必须重新 `hash --scope runtime` → 离线签名 → 安装到
  `/etc/agentid-shadow/` → 人工重启 → 开新 epoch。
- 残留下限：部署机若被完全控制，可同时替换代码与 `/etc/agentid-shadow/` 下的公钥（需 root）；
  本闸门防的是误改、带外修改与降级启动，不是防 root 沦陷。---

## 第二次受控重启（build 口径修正）+ W1 交接

**时间**：2026-11-02 21:29:45 CST（2026-11-02T13:29:45Z）
**原因**：`build` 字段从 git HEAD 改为 `shadow/probe.mjs` 的 SHA-256 前 12 位。

| 项 | 值 |
|---|---|
| MainPID | 3674841（首次重启为 3664526） |
| `build` | `sha256:0b9c786f314a` |
| `gitHead`（新增，仅溯源） | `2c29d5a` |
| 续扫 | lastBlock 131249108 → scannedTo 131249130，`gap=0`，`pending=0`，`lag=2` |
| ExecStartPre | `release-gate: OK (2 runtime files)` |

**为什么改**：上一轮 HEAD 从 `fa09631` 变 `733134e` 只是文档提交，却改变了 epoch 主键；
若继续用 HEAD，复盘时会把"改了文档"误归因为"换了版本"。现在 `build` 只随代码变。

旧 epoch 的 `build` 仍保留 HEAD 语义（`fa09631` / `733134e`），跨 epoch 比较时不得直接对比字符串，
只能比 `sha256:` 前缀的那些行。

## 交接给 W1 判决章

```
章节        analysis/CHAPTER-W1-DECISION.md
观测窗口    2026-11-03 00:00 → 2026-11-16 23:59 CST
判决日      2026-11-16 23:59 CST（是 / 否 / 未能判决，三选一）
巡检        每小时静默检查 + T1–T4 触发器 + 每日 09:05 一行
本 epoch    不并入 W0；W0 结论未改
```
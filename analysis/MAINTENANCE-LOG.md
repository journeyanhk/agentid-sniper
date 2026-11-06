# 运维日志（只读探针）

本文件记录**与 W0 观察窗口无关**的运行环境变更。W0 终局数据与判决不因这些条目改变，也不计入 W0 的重启统计。

## 2026-11-04 13:10:34 CST — post-archive maintenance restart

**目的**：让 11-03 安装的 systemd drop-in `LimitCORE=0` 对运行中的进程真正生效（`daemon-reload` 只改配置，不改已运行进程的 rlimit）。

| 项 | 重启前 | 重启后 |
|---|---|---|
| MainPID | 3674841 | 3764054 |
| state.lastBlock | 131566484 | 从 131566484 续扫（resume 日志一致），随后推进到 131566526 |
| build | `sha256:0b9c786f314a` | `sha256:0b9c786f314a` |
| gitHead | 21b4ef3 | 21b4ef3 |
| probe sha256 | `0b9c786f314a2f10…` | 同一文件，未变 |
| `/proc/<pid>/limits` Max core file size | infinity | **0** |
| release-gate（ExecStartPre） | OK（2 runtime files） | OK（2 runtime files） |

**连续性核对**：

- 启动日志 `resume lastBlock=131566484 pending=0`，与重启前 `state.lastBlock` 完全一致 → 无回退、无跳段；
- 重启后首条 metrics：`lagBlocks=2, hadGap=false, gapCount=0, pending=0, stateCorrupt=false, cursorPaused=false`；
- `NRestarts` 仍为 0（`systemctl restart` 属人工操作，不计入自动重启计数）。

**边界**：未改 `shadow/probe.mjs`、未改 unit 文件、未改 W0 终局文件；W0 窗口（130769296..131153193）与判决均未触碰。

## 2026-11-03 — core dump 清理与 core 生成关闭

- 删除 `shadow/core`（1.0 GiB，sha256 `802c1380…`，11-01 14:24 崩溃遗留），元数据见 `analysis/market/CORE-CLEANUP.md`；
- `.gitignore` 增加 `core` / `core.*` / `*.core` / `shadow/core*` / `data/core*`；
- drop-in `/etc/systemd/system/agentid-shadow.service.d/limitcore.conf` 设 `LimitCORE=0`（不改签名 unit，闸门保持绿灯）。

## 2026-11-04 — 一致性检查扩展到 systemd 侧

`deploy/check-consistency.sh` 新增核对：`User`、`WorkingDirectory`、`ExecStart`、`Restart`、`LimitCORE`、`DropInPaths`、drop-in sha256；并把 build 比对从「git 短 HEAD」改成「probe sha256 前 12 位」（build 语义在 2026-11-02 已切换，旧比对会永久 WARN）。

## 2026-11-04 — W1 未跟踪文件移出工作树

`analysis/w1-bridge.py`、`analysis/w1-state-update.py`、`analysis/w1-verdict.py`（从未提交的一次性辅助脚本）移到仓库外隔离目录 `~/quarantine/agentid-w1-unused/`，附 `MANIFEST.md`（sha256 / 行数 / 用途 / 恢复方式）。未加 `.gitignore`，避免把未完成代码藏起来。
## 2026-11-04 — archive 收集范围补齐 analysis/market

`deploy/release-manifest.mjs` v3 把 archive 从「目录 + 单一扩展名」改为**显式 allowlist + 显式 exclude**：

- 纳入：`analysis/*.py|*.md`、`deploy/*.sh|*.md|*.mjs`、`analysis/market/*.py|*.md|*.mjs|*.sh|*.yaml|*.toml|*.json`（只收该目录第一层）；
- 排除：`*.jsonl`、`*.log`、`*.tmp`、`*.cache`、`core*`、`artifacts/`、`raw/`、`cache/`；
- 清单内记录 `rules` 与 `counts`（walked / hashed / excluded / skipped），`verify` 会比对规则：改了规则不重签 → 直接失败；
- 结果：archive 由 32 个文件扩到 **43 个**（新增 P0 的 5 个 py、2 个 md、1 个 chain-config.json，以及 deploy 的 2 个 mjs）。

边界不变：archive 变化**只影响归档校验**，不改 runtime 清单（仍 2 文件）、不触发 systemd 重启、不改 probe build、不碰 W0 结论。

配套测试 `deploy/test-archive-scope.sh`（6/6 通过）：新增 `analysis/market/*.py` → 未登记；新增 `*.jsonl`／`artifacts/*.json` → 排除；修改／删除已签名文件 → 被改／缺失。

## 重启计数口径

- `manualMaintenanceRestarts`：`analysis/MAINTENANCE-LOG.md` 里 `post-archive maintenance restart` 条目数（当前 1）；
- `automaticRestarts`：systemd `NRestarts`（只统计 systemd 自动拉起，当前 0）。

`deploy/check-consistency.sh` 同时打印两者，不把人工维护重启混进自动重启统计。

## 2026-11-04 — 收尾：first-level 说明、nested 可见性、父目录权限、数据 provenance

- **first-level only 写进规则**：manifest 的 `rules.note` 明确 `analysis/market is intentionally first-level only`，并说明子目录源码不进入签名、但 `verify` 会以「不纳入（first-level only）」信息行列出，便于区分「按规则排除」与「漏收」。
- **nested 非阻断测试**：`deploy/test-archive-scope.sh` 增加第 7 例——`analysis/market/nested/test.py` 存在时 verify 仍通过，但输出必须出现该路径与「不纳入（first-level only）」；现 7/7 通过。
- **父目录权限**：`deploy/check-consistency.sh` 逐级核对 `/usr/local`、`/usr/local/libexec`、`/usr/local/libexec/agentid-shadow`、`/etc`、`/etc/systemd`、`/etc/systemd/system`、`/etc/systemd/system/agentid-shadow.service.d`、`/etc/agentid-shadow` 的 owner 与 group/other 写位——文件不可写但父目录可写时，仍可被删除重建。
- **最小数据 provenance**：新增 `analysis/DATA-MANIFEST-W0.json`（9 个产物，记录 window / sha256 / size / mtime / decoderVersion / dataComplete / dataFresh / sourceCommit）。不重扫描、不进发布清单、不触发重启、不改变结论。
  - 交叉核对发现：P0 的 `SHA256SUMS` 只登记 3 个真实文件并含一条合成行 `recordsHash`，且未登记 `links.json`；本清单重算全部 9 个文件哈希，不依赖该辅助文件（差异已记录在 `checks` 字段，未改动原证据文件）。

## 2026-11-04 — 归档一致性小修（provenance 自签 + SHA256SUMS 归位）

- `analysis/DATA-MANIFEST-W0.json` 纳入 archive 签名：新增**显式单文件 allowlist**（`explicitFiles`），不放开 `analysis/` 根目录的整类 `*.json`（那里多是每次重跑都会变的动态产物）。
- `analysis/market/artifacts/w0-init/SHA256SUMS` 重建为 canonical：只列 4 个真实文件（`envelope.json`、`links.json`、`pools.json`、`records.jsonl`），4/4 与当前哈希一致；`links.json` 之前漏登记，已补。
- 原文件字节保留为 `SHA256SUMS.orig`；`recordsHash` 移入 `DERIVED-HASHES.json` 并注明是**派生哈希**（规范化序列化后与 `records.jsonl` 的 sha256 相同），不再伪装成文件校验行。
- `DATA-MANIFEST-W0.json` 升到 schemaVersion 2：11 个产物、`checks.sha256sumsMatched=4/4`、`sha256sumsMissing=[]`，并记录 `.orig` 的哈希与问题说明。
- verify 的 nested 提示改为醒目告警：`WARN: intentionally excluded nested source`；`check-consistency.sh` 增加 `nested sources` 计数与路径（当前 0）。
- 边界不变：archive 只重签、runtime 未动、未重启服务、未重跑 P0、未启动 P1。

## 2026-11-06 — 只读审查工具上线（guard + 判据漂移 + 日报确定性入口）

- 新增 `analysis/read_only_guard.py`：6 条硬规则（交易 / 签名 / 钱包 / 授权 / 动态执行 / 子进程）+ 未登记外连 + 私钥路径 + 依赖面审计（stdlib-only）。**不做验签**，发布信任根仍只有 `verify-release.sh`；例外 11 条，每条带 reason 与 maxHits，并禁止指向 guard 自身。
- 新增 `analysis/check_w1_constants.py` + `analysis/w1-criteria.json`：把 W1 窗口与门槛（3 集合 / 20 笔 / 20 买家 / netEV>0）和 EV 常量（gas、费率、BNB_USD、royalty 最坏 10%）做成机器可读镜像；只报漂移，不修改任何文件。
- 新增 `analysis/w1-daily.py`：日报的确定性入口（此前为会话侧临时代码，不落盘、无法复现）。读 `cohort-latest.json`，输出四类 evUnknown 原因码（`collection_address_missing` / `total_supply_empty_or_revert` / `total_supply_zero` / `rpc_error`）+ `rawError`；EV 公式、候选纳入、排序不变。
- 复算校验：用 11-06 09:15 的 cohort-latest.json 重算，176 笔 / 32 集合、跟踪 14 集合 / 40 笔、EV 8/14、candidates 0、best DYOR -$0.0069，与当日日报逐项一致；6 个 unknown 均为 `total_supply_empty_or_revert`（execution reverted: 0x）。
- `analysis/w1-criteria.json` 以 explicitFiles 进 archive 签名，不放开 analysis 根目录整类 `*.json`。
- 边界：runtime manifest 未动、probe 未改、服务未重启、W1 判据未改、未接任何执行路径。

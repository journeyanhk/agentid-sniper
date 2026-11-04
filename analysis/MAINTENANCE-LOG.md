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

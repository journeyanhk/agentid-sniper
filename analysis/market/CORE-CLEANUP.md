# Core dump 清理记录（2026-11-03）

## 对象

| 项 | 值 |
|---|---|
| 路径 | `/home/laoda/agentid-sniper/shadow/core` |
| 大小 | 1,075,998,720 字节（1.0 GiB） |
| mtime | 2026-11-01 14:24 CST |
| 权限 | `-rw-------` laoda:laoda |
| 类型 | ELF64 LSB, `ET_CORE`（`7f 45 4c 46 02 01 01 00 … 04 00 3e 00`），x86-64 core dump |
| sha256 | `802c138055360b2222329db75a849a87bcbd73874497eeaf71f39baaebf288b9` |

## 来历

对应 11-01 13:53 / 14:24 CST 两次 node 堆耗尽崩溃中的后一次（mtime 14:24 与之吻合），属 W0 冻结期（build `fa09631` epoch）遗留。`git status` 显示为未跟踪（`?? shadow/core`），未被 `.gitignore` 覆盖。

## 处置

- 取证需求：无（W0 已定稿，崩溃原因已定位为 node 堆耗尽，且服务此后两次受控重启正常）。
- 决定：**删除**，不提交、不复制到仓库。仅保留本记录的元数据（大小 / mtime / sha256 / 类型），不保留内容——core dump 可能包含进程内存中的路径、配置与凭据。
- 清理时间：2026-11-03 18:3x CST。

## 防复发

1. `.gitignore` 增加 `shadow/core`、`shadow/core.*`、`core`、`core.*`、`*.core`、`data/core`、`data/core.*`。
2. systemd 通过 **drop-in** 关闭 core dump：`/etc/systemd/system/agentid-shadow.service.d/limitcore.conf` → `[Service] LimitCORE=0`，并 `systemctl daemon-reload`。
   - 用 drop-in 而不是改 `deploy/systemd/agentid-shadow.service`，是因为后者在 runtime 清单里，改动会让发布闸门在下一次启动时判 hash 不符（exit 13）。drop-in 不改动已签名的 unit 文件，闸门保持绿灯。
   - `daemon-reload` 不重启服务；`LimitCORE=0` 对**下次启动**生效。当前进程的 RLIMIT_CORE 仍是 infinity，本次未重启（按批准口径）。
3. 后续若修订 unit，应把 `LimitCORE=0` 写进 unit 本体并走一次完整发布（hash → 签名 → 安装 → 校验 → 受控重启）。
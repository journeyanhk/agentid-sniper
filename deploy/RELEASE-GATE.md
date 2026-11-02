# 发布闸门（release gate）——威胁模型与更新流程

**一句话：这套闸门挡的是误改、回滚、非 root 篡改与供应链替换；挡不住拿到 root 的人，也挡不住宿主被完全控制。**

## 保护边界

| 威胁 | 是否挡住 | 机制 |
|---|---|---|
| 手滑改了 `shadow/probe.mjs` | ✅ | hash 不符 → exit 13 |
| `git checkout` 把未提交改动回滚 | ✅ | 回滚后 hash 与签名不符 → exit 13 |
| 非 root 用户替换 probe | ✅ | 部署用户无 `write` 权限路径 + `/etc/agentid-shadow` 644 root |
| 改了 `/etc/systemd/system` 里的 unit | ✅ | unit drift → exit 14 |
| 换掉清单让它匹配被改的代码 | ✅ | 没有私钥，签名对不上 → exit 11 |
| 降级到旧的「已签名」清单 | ⚠️ 部分 | 需 root 覆盖 `/etc/agentid-shadow`；无版本号递增校验 |
| 拿到 root 的人 | ❌ | 可同时改代码、验证器、公钥 |
| 宿主被完全控制 | ❌ | 同上，验证器本身可被替换 |

结论：这是**完整性校验**，不是**防入侵**。不要把它当安全边界用。

## 退出码

```
0   通过
10  清单/签名/公钥缺失，或清单格式、scope 不对
11  签名无效
12  运行文件缺失
13  运行文件 hash 不符
14  已安装 unit 与签名 unit 不一致
```

`ExecStartPre` 拿到非 0 即不启动服务（fail closed）。验证器只读本地文件：不联网、不读私钥、不写文件、不自动修复。

## 两个清单

```
runtime  shadow/probe.mjs、deploy/systemd/agentid-shadow.service        → 阻断启动
archive  analysis/*.py、analysis/*.md、deploy/*.sh、deploy/*.md         → 只用于归档复现
```

生成的 `*.json` / `*.jsonl` 不进任何清单：每次重跑都会变，纳入签名只会制造噪音。

## 更新 runtime 的唯一流程

```bash
# 1. 部署机产出未签名清单
ssh agentid 'cd ~/agentid-sniper && node deploy/release-manifest.mjs hash --scope runtime'
# 2. 取回并离线签名（私钥只在 memoh 工作区）
scp .../deploy/manifest-runtime.json /data/.secrets/
AGENTID_PRIVKEY=/data/.secrets/agentid_release_key.pem \
  node deploy/release-manifest.mjs signfile /data/.secrets/manifest-runtime.json /data/.secrets/manifest-runtime.sig
# 3. 回传并安装
scp /data/.secrets/manifest-runtime.sig .../deploy/
ssh agentid 'sudo install -o root -g root -m 644 deploy/manifest-runtime.json deploy/manifest-runtime.sig /etc/agentid-shadow/'
# 4. 重启并开新 epoch
```

**原子性要求**：`hash` 与 `sign` 之间不得再跑一次 `hash` —— 清单里的 `generated` 时间戳会变，签名立刻失效。
`deploy/publish-runtime.sh` 把 1–3 步包成一条命令，避免手工漏步。

## build 标识

`build` 字段取 `shadow/probe.mjs` 自身的 SHA-256 前 12 位（`sha256:xxxxxxxxxxxx`），
**不再取 git HEAD** —— 否则一次纯文档提交就会让 epoch 主键变化，复盘时会把「文档提交」误归因为「版本变化」。
git HEAD 另存 `gitHead` 字段，仅作溯源。
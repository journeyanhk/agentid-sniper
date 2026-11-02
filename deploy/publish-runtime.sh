#!/bin/sh
# publish-runtime.sh —— 原子发布 runtime 清单（hash → 离线签名 → 安装 → 校验）
#
# 在 memoh 工作区运行（私钥只在这里）。任何一步失败立即退出，不会留下"清单已换、签名没换"的中间态。
set -eu

HOST="${AGENTID_HOST:-laoda@38.125.160.61}"
PORT="${AGENTID_PORT:-20123}"
KEY="${AGENTID_SSH_KEY:-/data/.ssh/id_ed25519_arcus_jp}"
PRIV="${AGENTID_PRIVKEY:-/data/.secrets/agentid_release_key.pem}"
REPO="${AGENTID_REPO:-/home/laoda/agentid-sniper}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

SSH="ssh -i $KEY -p $PORT $HOST"
SCP="scp -i $KEY -P $PORT"

echo "[1/5] 部署机生成 runtime 清单"
$SSH "cd $REPO && node deploy/release-manifest.mjs hash --scope runtime"

echo "[2/5] 取回清单"
$SCP "$HOST:$REPO/deploy/manifest-runtime.json" "$WORK/manifest-runtime.json" >/dev/null

echo "[3/5] 本地签名"
AGENTID_PRIVKEY="$PRIV" node /data/scripts/release-manifest.mjs signfile \
  "$WORK/manifest-runtime.json" "$WORK/manifest-runtime.sig"

echo "[4/5] 回传 + 安装到 /etc/agentid-shadow"
$SCP "$WORK/manifest-runtime.sig" "$HOST:$REPO/deploy/manifest-runtime.sig" >/dev/null
$SSH "cd $REPO && sudo install -o root -g root -m 644 deploy/manifest-runtime.json deploy/manifest-runtime.sig /etc/agentid-shadow/"

echo "[5/5] 校验（签名 + hash + unit）"
$SSH "/usr/local/libexec/agentid-shadow/verify-release.sh"

echo "OK：runtime 清单已发布并校验通过。改完 probe/unit 后需人工重启服务并开新 epoch。"
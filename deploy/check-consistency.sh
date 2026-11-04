#!/usr/bin/env bash
# 核对「GitHub / 运行目录 / systemd / probe VERSION / 运行中进程」是否一致
# 用法: deploy/check-consistency.sh [repo_path] [service_name]
#
# 2026-11-04：加入 systemd 侧核对（User / WorkingDirectory / ExecStart / Restart /
# LimitCORE / drop-in 路径与 hash）。原因是 LimitCORE=0 走的是 drop-in
# （/etc/systemd/system/<svc>.service.d/limitcore.conf），不在签名清单里，
# 只靠 release-gate 看不出来它是否仍在位或被改写。
set -u
REPO=${1:-$HOME/agentid-sniper}
SVC=${2:-agentid-shadow}
EXPECT_USER=${EXPECT_USER:-laoda}
cd "$REPO" || exit 1

LOCAL=$(git rev-parse --short HEAD)
REMOTE=$(git ls-remote origin refs/heads/main 2>/dev/null | cut -c1-7)
FILE=$(grep -m1 '^const VERSION' shadow/probe.mjs | sed "s/.*'\(.*\)'.*/\1/")
SHA=$(sha256sum shadow/probe.mjs | cut -c1-16)
SHA12=$(printf '%s' "$SHA" | cut -c1-12)
PID=$(systemctl show -p MainPID --value "$SVC" 2>/dev/null)
RUNFILE=$(tr '\0' ' ' < "/proc/$PID/cmdline" 2>/dev/null | awk '{print $2}')
RUNDIR=$(readlink "/proc/$PID/cwd" 2>/dev/null)
BUILD=$(tail -20 shadow/metrics.jsonl 2>/dev/null | grep -o '"build":"[^"]*"' | tail -1 | cut -d'"' -f4)

SVC_USER=$(systemctl show -p User --value "$SVC" 2>/dev/null)
SVC_WD=$(systemctl show -p WorkingDirectory --value "$SVC" 2>/dev/null)
SVC_EXEC=$(systemctl show -p ExecStart --value "$SVC" 2>/dev/null | tr -s ' ' | sed 's/.*argv\[\]=\([^;]*\);.*/\1/' | sed 's/^ *//;s/ *$//')
SVC_RESTART=$(systemctl show -p Restart --value "$SVC" 2>/dev/null)
SVC_LIMITCORE=$(systemctl show -p LimitCORE --value "$SVC" 2>/dev/null)
DROPINS=$(systemctl show -p DropInPaths --value "$SVC" 2>/dev/null)
DROPIN_CORE="/etc/systemd/system/${SVC}.service.d/limitcore.conf"
DROPIN_SHA=""
[ -r "$DROPIN_CORE" ] && DROPIN_SHA=$(sha256sum "$DROPIN_CORE" | cut -c1-16)

echo "GitHub main       : ${REMOTE:-<unreachable>}"
echo "repo HEAD         : $LOCAL"
echo "repo status       : $(git status --porcelain | wc -l) 个未提交改动"
echo "probe VERSION     : $FILE"
echo "repo file sha256  : $SHA"
echo "service PID       : ${PID:-<none>} (active=$([ "$PID" != "0" ] && systemctl is-active "$SVC"))"
echo "running file      : ${RUNFILE:-<none>}"
echo "running cwd       : ${RUNDIR:-<none>}"
echo "metrics build     : ${BUILD:-<unknown>}"
echo "systemd User      : ${SVC_USER:-<none>}"
echo "systemd WorkingDir: ${SVC_WD:-<none>}"
echo "systemd ExecStart : ${SVC_EXEC:-<none>}"
echo "systemd Restart   : ${SVC_RESTART:-<none>}"
echo "systemd LimitCORE : ${SVC_LIMITCORE:-<none>}"
echo "drop-in path      : ${DROPINS:-<none>}"
echo "drop-in sha256    : ${DROPIN_SHA:-<missing>} (${DROPIN_CORE})"

rc=0
[ -n "${REMOTE:-}" ] && [ "$LOCAL" != "$REMOTE" ] && { echo "FAIL: 运行目录 HEAD 与 GitHub main 不一致"; rc=1; }
if [ -n "${RUNFILE:-}" ]; then
  RSH=$(sha256sum "$RUNFILE" 2>/dev/null | cut -c1-16)
  [ "$RSH" = "$SHA" ] || { echo "FAIL: 运行中的文件与仓库文件不一致（$RSH vs $SHA）"; rc=1; }
else
  echo "FAIL: 服务未运行"; rc=1
fi

# systemd 侧：这些是运行安全配置，必须与预期一致
[ "${SVC_USER:-}" = "$EXPECT_USER" ] || { echo "FAIL: systemd User=$SVC_USER，预期 $EXPECT_USER"; rc=1; }
[ "${SVC_WD:-}" = "$REPO/shadow" ] || { echo "FAIL: systemd WorkingDirectory=$SVC_WD，预期 $REPO/shadow"; rc=1; }
case "${SVC_EXEC:-}" in
  *"$REPO/shadow/probe.mjs"*) : ;;
  *) echo "FAIL: ExecStart 未指向 $REPO/shadow/probe.mjs（$SVC_EXEC）"; rc=1 ;;
esac
[ "${SVC_LIMITCORE:-}" = "0" ] || { echo "FAIL: LimitCORE=$SVC_LIMITCORE，预期 0（drop-in 缺失或被改写）"; rc=1; }
case "${DROPINS:-}" in
  *"$DROPIN_CORE"*) : ;;
  *) echo "FAIL: DropInPaths 未包含 $DROPIN_CORE"; rc=1 ;;
esac
[ -n "$DROPIN_SHA" ] || { echo "FAIL: drop-in 文件不可读：$DROPIN_CORE"; rc=1; }
case "${SVC_RESTART:-}" in
  always|on-failure) : ;;
  *) echo "WARN: Restart=$SVC_RESTART（历史值为 always）" ;;
esac

case "${BUILD:-}" in
  "sha256:$SHA12") : ;;
  *) echo "WARN: 运行 build=${BUILD:-<unknown>} 与 probe sha256 前缀 sha256:$SHA12 不同（提交后未重启？）" ;;
esac

[ $rc = 0 ] && echo "OK: 代码 / 部署 / 版本一致"
exit $rc
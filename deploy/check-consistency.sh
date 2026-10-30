#!/usr/bin/env bash
# 核对「GitHub / 运行目录 / systemd / probe VERSION / 运行中进程」是否一致
# 用法: deploy/check-consistency.sh [repo_path] [service_name]
set -u
REPO=${1:-$HOME/agentid-sniper}
SVC=${2:-agentid-shadow}
cd "$REPO" || exit 1

LOCAL=$(git rev-parse --short HEAD)
REMOTE=$(git ls-remote origin refs/heads/main 2>/dev/null | cut -c1-7)
FILE=$(grep -m1 '^const VERSION' shadow/probe.mjs | sed "s/.*'\(.*\)'.*/\1/")
SHA=$(sha256sum shadow/probe.mjs | cut -c1-16)
PID=$(systemctl show -p MainPID --value "$SVC" 2>/dev/null)
RUNFILE=$(tr '\0' ' ' < "/proc/$PID/cmdline" 2>/dev/null | awk '{print $2}')
RUNDIR=$(readlink "/proc/$PID/cwd" 2>/dev/null)
BUILD=$(tail -20 shadow/metrics.jsonl 2>/dev/null | grep -o '"build":"[^"]*"' | tail -1 | cut -d'"' -f4)

echo "GitHub main       : ${REMOTE:-<unreachable>}"
echo "repo HEAD         : $LOCAL"
echo "repo status       : $(git status --porcelain | wc -l) 个未提交改动"
echo "probe VERSION     : $FILE"
echo "repo file sha256  : $SHA"
echo "service PID       : ${PID:-<none>} (active=$([ "$PID" != "0" ] && systemctl is-active "$SVC"))"
echo "running file      : ${RUNFILE:-<none>}"
echo "running cwd       : ${RUNDIR:-<none>}"
echo "metrics build     : ${BUILD:-<unknown>}"
echo "ExecStart         : $(systemctl show -p ExecStart --value "$SVC" 2>/dev/null | tr -s ' ' | cut -c1-120)"

rc=0
[ -n "${REMOTE:-}" ] && [ "$LOCAL" != "$REMOTE" ] && { echo "FAIL: 运行目录 HEAD 与 GitHub main 不一致"; rc=1; }
if [ -n "${RUNFILE:-}" ]; then
  RSH=$(sha256sum "$RUNFILE" 2>/dev/null | cut -c1-16)
  [ "$RSH" = "$SHA" ] || { echo "FAIL: 运行中的文件与仓库文件不一致（$RSH vs $SHA）"; rc=1; }
else
  echo "FAIL: 服务未运行"; rc=1
fi
[ "${BUILD:-}" != "$LOCAL" ] && echo "WARN: 运行 build=$BUILD 与 HEAD=$LOCAL 不同（提交后未重启）"
[ $rc = 0 ] && echo "OK: 代码 / 部署 / 版本一致"
exit $rc
#!/usr/bin/env bash
# 核对「GitHub / 运行目录 / systemd / probe VERSION / 运行中进程」是否一致
# 用法: deploy/check-consistency.sh [repo_path] [service_name]
#
# 2026-11-04：加入 systemd 侧核对（User / WorkingDirectory / ExecStart / Restart /
# LimitCORE / drop-in 路径与 hash），以及关键路径的 owner / mode / 符号链接核对。
# 原因：LimitCORE=0 走的是 drop-in（不在签名清单里），release-gate 看不到它是否
# 仍在位；而 probe / 验证器 / drop-in 一旦变成符号链接或被运行用户改写，
# 闸门仍然会显示绿色。
set -u
REPO=${1:-$HOME/agentid-sniper}
SVC=${2:-agentid-shadow}
EXPECT_USER=${EXPECT_USER:-laoda}
VERIFIER_DIR=/usr/local/libexec/agentid-shadow
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
SVC_NRESTARTS=$(systemctl show -p NRestarts --value "$SVC" 2>/dev/null)
DROPINS=$(systemctl show -p DropInPaths --value "$SVC" 2>/dev/null)
DROPIN_CORE="/etc/systemd/system/${SVC}.service.d/limitcore.conf"
UNIT_FILE="/etc/systemd/system/${SVC}.service"
DROPIN_SHA=""
[ -r "$DROPIN_CORE" ] && DROPIN_SHA=$(sha256sum "$DROPIN_CORE" | cut -c1-16)
MANUAL_RESTARTS=$(grep -c '^## .*post-archive maintenance restart' analysis/MAINTENANCE-LOG.md 2>/dev/null || true)

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
NESTED_SRC=$(find analysis/market -mindepth 2 -type f \
  \( -name '*.py' -o -name '*.mjs' -o -name '*.sh' -o -name '*.yaml' -o -name '*.toml' \) \
  -not -path '*/artifacts/*' -not -path '*/raw/*' -not -path '*/cache/*' 2>/dev/null | sort)
NESTED_N=$(printf '%s' "${NESTED_SRC:-}" | grep -c . || true)
echo "nested sources    : ${NESTED_N} 个（analysis/market 子目录源码，按 first-level 规则不进入 archive 签名）"
[ "${NESTED_N:-0}" -gt 0 ] && printf '%s\n' "$NESTED_SRC" | sed 's/^/                    · /'

echo "restarts          : manual=${MANUAL_RESTARTS:-0} automatic=${SVC_NRESTARTS:-?} (NRestarts 只统计自动拉起)"

rc=0

# 关键路径：必须存在、绝对路径、非符号链接、owner/mode 符合预期、group/other 不可写
check_path() { # check_path <label> <path> <owner> <mode|-> <allow-symlink:0|1>
  local label=$1 p=$2 owner=$3 mode=$4 allow_link=${5:-0}
  if [ ! -e "$p" ]; then echo "FAIL: $label 不存在（$p）"; rc=1; return; fi
  if [ -L "$p" ]; then
    if [ "$allow_link" = "0" ]; then echo "FAIL: $label 是符号链接（$p → $(readlink "$p")）"; rc=1; fi
  fi
  local got go gmode oct
  got=$(stat -c '%U:%G %a' "$p" 2>/dev/null) || { echo "FAIL: $label 无法 stat（$p）"; rc=1; return; }
  go=${got% *}; gmode=${got##* }
  echo "$label : $got  ($p)"
  [ "$go" = "$owner" ] || { echo "FAIL: $label owner=$go，预期 $owner"; rc=1; }
  if [ "$mode" != "-" ] && [ "$gmode" != "$mode" ]; then
    echo "FAIL: $label mode=$gmode，预期 $mode"; rc=1
  fi
  oct=$(( 8#$gmode )) 2>/dev/null || oct=0
  if (( (oct & 8#022) != 0 )); then echo "FAIL: $label 允许 group/other 写入（mode $gmode）"; rc=1; fi
}

# 父目录：文件本身不可写还不够——父目录若对运行用户可写，可以删掉重建。
# 这里逐级核对 owner:group 与 group/other 写位（等价于 namei -l 的关键部分）。
check_dir() { # check_dir <path>
  local p=$1
  if [ ! -d "$p" ]; then echo "FAIL: 目录不存在（$p）"; rc=1; return; fi
  local got go gmode oct
  got=$(stat -c '%U:%G %a' "$p" 2>/dev/null) || { echo "FAIL: 无法 stat（$p）"; rc=1; return; }
  go=${got% *}; gmode=${got##* }
  echo "parent dir : $got  ($p)"
  [ "$go" = "root:root" ] || { echo "FAIL: $p owner=$go，预期 root:root"; rc=1; }
  oct=$(( 8#$gmode )) 2>/dev/null || oct=0
  if (( (oct & 8#022) != 0 )); then echo "FAIL: $p 允许 group/other 写入（mode $gmode）"; rc=1; fi
}

for d in /usr/local/libexec/agentid-shadow /usr/local/libexec /usr/local \
         /etc/agentid-shadow \
         /etc/systemd/system/agentid-shadow.service.d /etc/systemd/system /etc/systemd; do
  check_dir "$d"
done

check_path "probe file" "$REPO/shadow/probe.mjs" "$EXPECT_USER:$EXPECT_USER" "644" 0
check_path "verifier sh" "$VERIFIER_DIR/verify-release.sh" "root:root" "755" 0
check_path "verifier mjs" "$VERIFIER_DIR/verify-runtime.mjs" "root:root" "755" 0
check_path "unit file" "$UNIT_FILE" "root:root" "644" 0
check_path "drop-in file" "$DROPIN_CORE" "root:root" "644" 0
check_path "runtime manifest" "/etc/agentid-shadow/manifest-runtime.json" "root:root" "644" 0
check_path "runtime sig" "/etc/agentid-shadow/manifest-runtime.sig" "root:root" "644" 0

[ -n "${REMOTE:-}" ] && [ "$LOCAL" != "$REMOTE" ] && { echo "FAIL: 运行目录 HEAD 与 GitHub main 不一致"; rc=1; }
if [ -n "${RUNFILE:-}" ]; then
  RSH=$(sha256sum "$RUNFILE" 2>/dev/null | cut -c1-16)
  [ "$RSH" = "$SHA" ] || { echo "FAIL: 运行中的文件与仓库文件不一致（$RSH vs $SHA）"; rc=1; }
  [ "$(readlink -f "$RUNFILE")" = "$(readlink -f "$REPO/shadow/probe.mjs")" ] || { echo "FAIL: 运行中的 probe 不是仓库里的那个文件"; rc=1; }
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
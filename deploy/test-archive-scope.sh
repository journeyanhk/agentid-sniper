#!/usr/bin/env bash
# 验证 archive 规则「真的覆盖 analysis/market，且只覆盖该覆盖的」。
# 需要仓库里存在已签名的 deploy/manifest-archive.json + .sig。
#
# 用例：
#   1 基线：当前工作树应通过
#   2 新增 analysis/market/*.py        → 失败（未登记）
#   3 新增 analysis/market/artifacts/*.jsonl → 通过（排除）
#   4 新增 analysis/market/artifacts/*.json  → 通过（子目录不在 allowlist）
#   5 修改已签名文件 chain-config.json → 失败（被改）
#   6 删除已签名文件（移走再放回）      → 失败（缺失）
#
# 用法: deploy/test-archive-scope.sh [repo_path]
set -u
REPO=${1:-$(cd "$(dirname "$0")/.." && pwd)}
cd "$REPO" || exit 1

PASS=0
FAIL=0
TMP_NEW="analysis/market/_scope_test.py"
TMP_JSONL="analysis/market/artifacts/_scope_test.jsonl"
TMP_JSON="analysis/market/artifacts/_scope_test.json"
SIGNED="analysis/market/chain-config.json"
BACKUP=$(mktemp -d)
cleanup() {
  rm -f "$TMP_NEW" "$TMP_JSONL" "$TMP_JSON"
  [ -f "$BACKUP/chain-config.json" ] && cp "$BACKUP/chain-config.json" "$SIGNED"
  git checkout -- "$SIGNED" 2>/dev/null
  rm -rf "$BACKUP"
}
trap cleanup EXIT

cp "$SIGNED" "$BACKUP/chain-config.json"

run() { # run <expect_rc> <label> [needle]
  local expect=$1 label=$2 needle=${3:-}
  local out rc
  out=$(node deploy/release-manifest.mjs verify --scope archive 2>&1); rc=$?
  if [ "$rc" != "$expect" ]; then
    echo "FAIL [$label] 期望 rc=$expect 实际 rc=$rc"; echo "$out" | sed 's/^/     /'; FAIL=$((FAIL+1)); return
  fi
  if [ -n "$needle" ] && ! printf '%s' "$out" | grep -q -- "$needle"; then
    echo "FAIL [$label] 输出缺少「$needle」"; echo "$out" | sed 's/^/     /'; FAIL=$((FAIL+1)); return
  fi
  echo "PASS [$label] rc=$rc"; PASS=$((PASS+1))
}

echo "== archive scope tests =="
run 0 "基线：工作树与清单一致" "个文件一致"

echo "# scope probe" > "$TMP_NEW"
run 1 "新增 analysis/market/*.py 应未登记" "未登记 $TMP_NEW"
rm -f "$TMP_NEW"

: > "$TMP_JSONL"
run 0 "新增 *.jsonl 应被排除" "个文件一致"
rm -f "$TMP_JSONL"

echo '{"probe":1}' > "$TMP_JSON"
run 0 "artifacts/ 子目录 JSON 应被排除" "个文件一致"
rm -f "$TMP_JSON"

echo "" >> "$SIGNED"
run 1 "修改已签名文件应被检出" "被改 $SIGNED"
git checkout -- "$SIGNED"

mv "$SIGNED" "$BACKUP/moved.json"
run 1 "删除已签名文件应被检出" "缺失 $SIGNED"
mv "$BACKUP/moved.json" "$SIGNED"

echo "== $PASS passed, $FAIL failed =="
[ "$FAIL" = 0 ]
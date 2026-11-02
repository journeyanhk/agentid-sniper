#!/bin/sh
# 发布完整性闸门：签名或文件不符即 exit 1，调用方据此拒绝启动/归档
set -eu
cd "$(dirname "$0")/.."
exec node deploy/release-manifest.mjs verify

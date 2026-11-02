#!/bin/sh
# 固定路径闸门：只读本地文件、不联网、不访问私钥、不自动修复
exec /usr/bin/node /usr/local/libexec/agentid-shadow/verify-runtime.mjs

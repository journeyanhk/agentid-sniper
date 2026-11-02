#!/usr/bin/env node
/**
 * verify-runtime.mjs —— systemd ExecStartPre 闸门（只读、无网络、不碰私钥、不自动修复）
 *
 * 固定路径安装：/usr/local/libexec/agentid-shadow/verify-runtime.mjs（root:root 755）
 * 可信输入（root 拥有，部署用户不可写）：
 *   /etc/agentid-shadow/manifest-runtime.json
 *   /etc/agentid-shadow/manifest-runtime.sig
 *   /etc/agentid-shadow/release-pubkey.pem
 *
 * 退出码（全部 fail closed）：
 *   0  通过
 *   10 manifest/sig/公钥 缺失或不可解析
 *   11 签名无效
 *   12 运行文件缺失
 *   13 运行文件 hash 不符
 *   14 已安装 unit 与签名的 unit 不一致（drift）
 */
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';

const REPO = '/home/laoda/agentid-sniper';
const ETC = '/etc/agentid-shadow';
const MAN = path.join(ETC, 'manifest-runtime.json');
const SIG = path.join(ETC, 'manifest-runtime.sig');
const PUB = path.join(ETC, 'release-pubkey.pem');
const UNIT_REPO_REL = 'deploy/systemd/agentid-shadow.service';
const UNIT_INSTALLED = '/etc/systemd/system/agentid-shadow.service';

function die(code, msg) {
  console.error(`release-gate: ${msg}`);
  process.exit(code);
}

function sha256(file) {
  return crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');
}

for (const f of [MAN, SIG, PUB]) {
  if (!fs.existsSync(f)) die(10, `missing ${f}`);
}

let body, manifest;
try {
  body = fs.readFileSync(MAN);
  manifest = JSON.parse(body.toString());
} catch (e) {
  die(10, `manifest 不可解析: ${e.message}`);
}

let ok = false;
try {
  ok = crypto.verify(null, body, crypto.createPublicKey(fs.readFileSync(PUB)),
    Buffer.from(fs.readFileSync(SIG, 'utf8').trim(), 'base64'));
} catch (e) {
  die(11, `签名校验异常: ${e.message}`);
}
if (!ok) die(11, 'manifest signature invalid');
if (manifest.schema !== 'agentid-release/2' || manifest.scope !== 'runtime') {
  die(10, `wrong runtime manifest (schema=${manifest.schema} scope=${manifest.scope})`);
}

const files = manifest.files || {};
if (!Object.keys(files).length) die(10, 'manifest 为空');

for (const [rel, want] of Object.entries(files)) {
  if (path.isAbsolute(rel) || rel.includes('..')) die(10, `非法路径 ${rel}`);
  const abs = path.join(REPO, rel);
  if (!fs.existsSync(abs)) die(12, `missing file ${rel}`);
  const got = sha256(abs);
  if (got !== want) die(13, `file hash mismatch ${rel} (want ${want.slice(0, 12)}, got ${got.slice(0, 12)})`);
}

// 已安装 unit 必须与签名的 unit 一致，防止「改了 unit 但代码没改」
if (!(UNIT_REPO_REL in files)) die(10, `runtime manifest 未包含 ${UNIT_REPO_REL}`);
if (!fs.existsSync(UNIT_INSTALLED)) die(14, `missing installed unit ${UNIT_INSTALLED}`);
if (sha256(UNIT_INSTALLED) !== files[UNIT_REPO_REL]) {
  die(14, `unit drift: ${UNIT_INSTALLED} 与签名版本不一致（改 unit 必须重新签名并安装）`);
}

console.log(`release-gate: OK (${Object.keys(files).length} runtime files, gitHead ${manifest.gitHead})`);
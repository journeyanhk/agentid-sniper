#!/usr/bin/env node
/**
 * release-manifest.mjs v2 —— 双清单（runtime / archive）+ Ed25519
 *
 * runtime 只覆盖「启动与运行必需」：probe、systemd unit。
 *   任何 analysis 文档变化都不会影响它 → 不会阻断 shadow 启动。
 * archive 覆盖分析脚本与文档，只用于归档复现，不参与启动闸门。
 *
 * 私钥只在 memoh 工作区（AGENTID_PRIVKEY），部署机只 hash + signfile + verify。
 * 用法：
 *   node deploy/release-manifest.mjs hash --scope runtime|archive
 *   AGENTID_PRIVKEY=... node deploy/release-manifest.mjs signfile <in> <out>
 *   node deploy/release-manifest.mjs verify --scope runtime|archive
 */
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';

const ROOT = process.env.AGENTID_ROOT || path.resolve(path.dirname(new URL(import.meta.url).pathname), '..');
const PRIV = process.env.AGENTID_PRIVKEY || '';
const OUT = {
  runtime: { man: 'deploy/manifest-runtime.json', sig: 'deploy/manifest-runtime.sig' },
  archive: { man: 'deploy/manifest-archive.json', sig: 'deploy/manifest-archive.sig' },
};
const PUB = path.join(ROOT, 'deploy', 'release_pubkey.pem');

// 只列真正影响运行的文件。probe.mjs 无本地 import，unit 决定启动命令。
const RUNTIME = [
  'shadow/probe.mjs',
  'deploy/systemd/agentid-shadow.service',
];

// 归档复现用：脚本与文档。生成物（*.json/*.jsonl）刻意排除——
// 它们每次重跑都会变，纳入签名只会制造无意义漂移，且不参与启动。
const ARCHIVE_GLOBS = [
  ['analysis', '.py'],
  ['analysis', '.md'],
  ['deploy', '.sh'],
  ['deploy', '.md'],
];

function sha256(file) {
  return crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');
}

function gitHead() {
  try {
    const head = fs.readFileSync(path.join(ROOT, '.git', 'HEAD'), 'utf8').trim();
    if (!head.startsWith('ref: ')) return head;
    const ref = head.slice(5).trim();
    const direct = path.join(ROOT, '.git', ref);
    if (fs.existsSync(direct)) return fs.readFileSync(direct, 'utf8').trim();
    const packed = path.join(ROOT, '.git', 'packed-refs');
    if (fs.existsSync(packed)) {
      for (const line of fs.readFileSync(packed, 'utf8').split('\n')) {
        const [sha, name] = line.trim().split(' ');
        if (name === ref) return sha;
      }
    }
    return ref;
  } catch { return 'unknown'; }
}

function collect(scope) {
  const out = {};
  if (scope === 'runtime') {
    for (const rel of RUNTIME) {
      const abs = path.join(ROOT, rel);
      if (!fs.existsSync(abs)) throw new Error('runtime 必需文件缺失: ' + rel);
      out[rel] = sha256(abs);
    }
    return out;
  }
  for (const [dir, ext] of ARCHIVE_GLOBS) {
    const abs = path.join(ROOT, dir);
    if (!fs.existsSync(abs)) continue;
    for (const f of fs.readdirSync(abs).sort()) {
      if (!f.endsWith(ext)) continue;
      out[path.posix.join(dir, f)] = sha256(path.join(abs, f));
    }
  }
  return out;
}

function build(scope) {
  const files = collect(scope);
  return Buffer.from(JSON.stringify({
    schema: 'agentid-release/2',
    scope,
    generated: new Date().toISOString(),
    gitHead: gitHead(),
    fileCount: Object.keys(files).length,
    files,
  }, null, 1));
}

function verify(scope) {
  const o = OUT[scope];
  const man = path.join(ROOT, o.man);
  const sig = path.join(ROOT, o.sig);
  if (!fs.existsSync(man) || !fs.existsSync(sig) || !fs.existsSync(PUB)) {
    console.error(`❌ [${scope}] 缺少 manifest / sig / 公钥`);
    return 1;
  }
  const body = fs.readFileSync(man);
  const sigBuf = Buffer.from(fs.readFileSync(sig, 'utf8').trim(), 'base64');
  if (!crypto.verify(null, body, crypto.createPublicKey(fs.readFileSync(PUB)), sigBuf)) {
    console.error(`❌ [${scope}] 签名无效`);
    return 1;
  }
  const listed = JSON.parse(body.toString()).files || {};
  const now = collect(scope);
  const bad = [];
  for (const [f, h] of Object.entries(listed)) {
    if (!(f in now)) bad.push('缺失 ' + f);
    else if (now[f] !== h) bad.push(`被改 ${f}`);
  }
  for (const f of Object.keys(now)) if (!(f in listed)) bad.push('未登记 ' + f);
  if (bad.length) {
    console.error(`❌ [${scope}] 文件与清单不符：`);
    for (const b of bad) console.error('   -', b);
    return 1;
  }
  console.log(`✅ [${scope}] 签名与 ${Object.keys(listed).length} 个文件一致`);
  return 0;
}

const argv = process.argv.slice(2);
const cmd = argv[0];
const scope = (argv.includes('--scope') ? argv[argv.indexOf('--scope') + 1] : '') ||
  (cmd === 'signfile' ? '' : 'runtime');

if (cmd === 'hash') {
  if (!OUT[scope]) throw new Error('scope 必须是 runtime 或 archive');
  const body = build(scope);
  const dest = path.join(ROOT, OUT[scope].man);
  fs.writeFileSync(dest, body);
  console.log(`已生成未签名清单 ${OUT[scope].man} → ${Object.keys(JSON.parse(body).files).length} 个文件`);
} else if (cmd === 'signfile') {
  const inp = argv[1], outp = argv[2];
  if (!PRIV) throw new Error('需要 AGENTID_PRIVKEY');
  const priv = crypto.createPrivateKey(fs.readFileSync(PRIV));
  fs.writeFileSync(outp, crypto.sign(null, fs.readFileSync(inp), priv).toString('base64') + '\n');
  console.log('已签名 →', outp);
} else if (cmd === 'verify') {
  process.exit(verify(scope));
} else {
  console.log('用法: release-manifest.mjs hash|verify [--scope runtime|archive] | signfile <in> <out>');
  process.exit(2);
}
#!/usr/bin/env node
/**
 * release-manifest.mjs —— 发布清单 + Ed25519 签名（只读工具链，不碰钱包）
 *
 * 三个子命令：
 *   keygen   生成签名密钥对：私钥写 ~/.agentid_release_key.pem (600)，公钥写 deploy/release_pubkey.pem
 *   sign     对受控文件生成 release-manifest.json 并签名 → release-manifest.sig
 *   verify   校验签名 + 逐文件 SHA-256；任何不符 exit 1（用于「签名失败 → 不启动」）
 *
 * 与作者 v3.0.0 的关键差别：他的 officialCheck() 只把状态塞给 UI，主交易进程根本不读；
 * 这里的 verify 是**闸门**，失败返回非 0，调用方（systemd ExecStartPre / 归档脚本）必须据此拒绝启动。
 */
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';

const ROOT = process.env.AGENTID_ROOT || path.resolve(path.dirname(new URL(import.meta.url).pathname), '..');
const KEYDIR = process.env.AGENTID_KEYDIR || path.join(process.env.HOME || '/data', '.');
const PRIV = process.env.AGENTID_PRIVKEY || path.join(KEYDIR, '.agentid_release_key.pem');
const PUB = path.join(ROOT, 'deploy', 'release_pubkey.pem');
const MAN = path.join(ROOT, 'deploy', 'release-manifest.json');
const SIG = path.join(ROOT, 'deploy', 'release-manifest.sig');

// 受控文件：探针 + 分析脚本 + 部署脚本（不含产物、不含 state/metrics）
const GLOBS = [
  ['shadow/probe.mjs', 'file'],
  ['analysis', 'py'],
  ['deploy', 'sh'],
];

function sha256(file) {
  return crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');
}

function collect() {
  const out = {};
  for (const [rel, kind] of GLOBS) {
    const abs = path.join(ROOT, rel);
    if (!fs.existsSync(abs)) continue;
    if (kind === 'file') {
      out[rel] = sha256(abs);
      continue;
    }
    for (const f of fs.readdirSync(abs).sort()) {
      if (kind === 'py' && !f.endsWith('.py')) continue;
      if (kind === 'sh' && !f.endsWith('.sh')) continue;
      out[path.posix.join(rel, f)] = sha256(path.join(abs, f));
    }
  }
  return out;
}

function gitHead() {
  try {
    const head = fs.readFileSync(path.join(ROOT, '.git', 'HEAD'), 'utf8').trim();
    if (!head.startsWith('ref: ')) return head;
    const ref = head.slice(5).trim();
    const packed = path.join(ROOT, '.git', 'packed-refs');
    const direct = path.join(ROOT, '.git', ref);
    if (fs.existsSync(direct)) return fs.readFileSync(direct, 'utf8').trim();
    if (fs.existsSync(packed)) {
      for (const line of fs.readFileSync(packed, 'utf8').split('\n')) {
        const [sha, name] = line.trim().split(' ');
        if (name === ref) return sha;
      }
    }
    return ref;
  } catch { return 'unknown'; }
}

function keygen() {
  const { publicKey, privateKey } = crypto.generateKeyPairSync('ed25519');
  const priv = privateKey.export({ type: 'pkcs8', format: 'pem' });
  const pub = publicKey.export({ type: 'spki', format: 'pem' });
  fs.writeFileSync(PRIV, priv, { mode: 0o600 });
  fs.writeFileSync(PUB, pub);
  console.log('私钥 →', PRIV, '(600)');
  console.log('公钥 →', PUB);
  console.log('公钥指纹', crypto.createHash('sha256').update(pub).digest('hex').slice(0, 32));
}

function sign() {
  const files = collect();
  const manifest = {
    schema: 'agentid-release/1',
    generated: new Date().toISOString(),
    gitHead: gitHead(),
    fileCount: Object.keys(files).length,
    files,
  };
  const body = Buffer.from(JSON.stringify(manifest, null, 1));
  const priv = crypto.createPrivateKey(fs.readFileSync(PRIV));
  const sig = crypto.sign(null, body, priv);
  fs.writeFileSync(MAN, body);
  fs.writeFileSync(SIG, sig.toString('base64') + '\n');
  console.log('清单', path.relative(ROOT, MAN), '→', manifest.fileCount, '个文件');
  console.log('签名', path.relative(ROOT, SIG), '→', sig.toString('base64').slice(0, 24) + '…');
  return verify();
}

function verify() {
  const problems = [];
  if (!fs.existsSync(MAN) || !fs.existsSync(SIG)) {
    console.error('❌ 缺少 release-manifest.json / .sig');
    return 1;
  }
  if (!fs.existsSync(PUB)) {
    console.error('❌ 缺少 deploy/release_pubkey.pem');
    return 1;
  }
  const body = fs.readFileSync(MAN);
  const sig = Buffer.from(fs.readFileSync(SIG, 'utf8').trim(), 'base64');
  const pub = crypto.createPublicKey(fs.readFileSync(PUB));
  if (!crypto.verify(null, body, pub, sig)) {
    console.error('❌ 签名校验失败（清单被改过，或不是这把公钥签的）');
    return 1;
  }
  const manifest = JSON.parse(body.toString());
  const now = collect();
  const sigFiles = manifest.files || {};
  for (const [f, h] of Object.entries(sigFiles)) {
    if (!(f in now)) problems.push(`缺失 ${f}`);
    else if (now[f] !== h) problems.push(`被改 ${f}（${h.slice(0, 12)} → ${now[f].slice(0, 12)}）`);
  }
  for (const f of Object.keys(now)) {
    if (!(f in sigFiles)) problems.push(`未登记 ${f}`);
  }
  if (problems.length) {
    console.error('❌ 文件与清单不符：');
    for (const p of problems) console.error('   -', p);
    return 1;
  }
  console.log(`✅ 签名与 ${Object.keys(sigFiles).length} 个文件全部一致（gitHead ${manifest.gitHead}）`);
  return 0;
}

const cmd = process.argv[2];

if (cmd === 'hash') {
  const files = collect();
  const body = Buffer.from(JSON.stringify({
    schema: 'agentid-release/1', generated: new Date().toISOString(),
    gitHead: gitHead(), fileCount: Object.keys(files).length, files,
  }, null, 1));
  fs.writeFileSync(MAN, body);
  console.log('已生成未签名清单', path.relative(ROOT, MAN), '→', Object.keys(files).length, '个文件');
} else if (cmd === 'signfile') {
  const inp = process.argv[3], outp = process.argv[4];
  const priv = crypto.createPrivateKey(fs.readFileSync(PRIV));
  const sig = crypto.sign(null, fs.readFileSync(inp), priv);
  fs.writeFileSync(outp, sig.toString('base64') + '\n');
  console.log('已签名', inp, '→', outp, sig.toString('base64').slice(0, 24) + '…');
} else if (cmd === 'keygen') keygen();
else if (cmd === 'sign') process.exit(sign());
else if (cmd === 'verify') process.exit(verify());
else {
  console.log('用法: node deploy/release-manifest.mjs keygen|hash|signfile|sign|verify');
  process.exit(2);
}

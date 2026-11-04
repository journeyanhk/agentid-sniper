#!/usr/bin/env node
/**
 * release-manifest.mjs v3 —— 双清单（runtime / archive）+ Ed25519
 *
 * runtime 只覆盖「启动与运行必需」：probe、systemd unit。
 *   任何 analysis 文档变化都不会影响它 → 不会阻断 shadow 启动。
 * archive 覆盖分析脚本与文档，只用于归档复现，不参与启动闸门。
 *
 * v3（2026-11-04）：archive 不再用「目录 + 单一扩展名」的模糊规则，改为
 * 显式 allowlist（root + 扩展名，只收该目录第一层）+ 显式 exclude
 * （*.jsonl / *.log / *.tmp / *.cache / core* / artifacts/ / raw/ / cache/）。
 * 目的是把 analysis/market（P0 V4 研究代码与报告）纳入归档签名，同时
 * 不把原始 RPC 输出、动态快照、日志塞进 manifest。规则本身写进 manifest
 * 并参与 verify 比对：改了规则却没重签，验证会失败。
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

// 归档复现用：脚本、配置与文档。每条规则只收该目录「第一层」的指定扩展名，
// 子目录（artifacts/ raw/ cache/ …）不在 allowlist 内，天然不进入清单。
const ARCHIVE_RULES = [
  { root: 'analysis', exts: ['.py', '.md'] },
  { root: 'deploy', exts: ['.sh', '.md', '.mjs'] },
  { root: 'analysis/market', exts: ['.py', '.md', '.mjs', '.sh', '.yaml', '.toml', '.json'] },
];

// 防御性排除：即使将来 allowlist 放宽到子目录，这些也不会进签名。
const ARCHIVE_EXCLUDE = [
  '*.jsonl', '*.log', '*.tmp', '*.cache', 'core*',
  'artifacts/', 'raw/', 'cache/',
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

function fnmatch(name, pat) {
  const re = new RegExp('^' + pat.replace(/[.+^${}()|[\]\\]/g, '\\$&').replace(/\*/g, '.*') + '$');
  return re.test(name);
}

function excluded(rel) {
  const base = rel.split('/').pop();
  for (const pat of ARCHIVE_EXCLUDE) {
    if (pat.endsWith('/')) {
      if (rel.includes('/' + pat.slice(0, -1) + '/')) return true;
    } else if (fnmatch(base, pat) || fnmatch(rel, pat)) {
      return true;
    }
  }
  return false;
}

// 子目录里的源码：不进签名，但要让 verify 明确说出来，
// 否则「first-level only」会被误读成「漏文件」。
const SOURCE_EXTS = ['.py', '.mjs', '.sh', '.yaml', '.toml'];

function outOfScopeSources() {
  const found = [];
  const walk = (rel, depth) => {
    const abs = path.join(ROOT, rel);
    let entries;
    try { entries = fs.readdirSync(abs); } catch { return; }
    for (const e of entries.sort()) {
      const childRel = path.posix.join(rel, e);
      let st;
      try { st = fs.lstatSync(path.join(ROOT, childRel)); } catch { continue; }
      if (st.isDirectory()) {
        if (excluded(childRel + '/')) continue;
        if (depth < 4) walk(childRel, depth + 1);
      } else if (st.isFile() && depth > 0 && SOURCE_EXTS.some((x) => e.endsWith(x))) {
        // depth 0 = analysis/market 第一层，本来就在签名范围内，不能算「不纳入」
        if (!excluded(childRel)) found.push(childRel);
      }
    }
  };
  walk('analysis/market', 0);
  return found;
}

function collect(scope) {
  const files = {};
  const counts = { walked: 0, hashed: 0, excluded: 0, skippedByRule: 0 };
  if (scope === 'runtime') {
    for (const rel of RUNTIME) {
      const abs = path.join(ROOT, rel);
      if (!fs.existsSync(abs)) throw new Error('runtime 必需文件缺失: ' + rel);
      files[rel] = sha256(abs);
      counts.walked += 1;
      counts.hashed += 1;
    }
    return { files, counts };
  }
  for (const rule of ARCHIVE_RULES) {
    const abs = path.join(ROOT, rule.root);
    if (!fs.existsSync(abs)) continue;
    for (const f of fs.readdirSync(abs).sort()) {
      const rel = path.posix.join(rule.root, f);
      const full = path.join(abs, f);
      let st;
      try { st = fs.lstatSync(full); } catch { continue; }
      if (!st.isFile()) continue;              // 子目录不在 allowlist
      counts.walked += 1;
      if (!rule.exts.some((e) => f.endsWith(e))) { counts.skippedByRule += 1; continue; }
      if (excluded(rel)) { counts.excluded += 1; continue; }
      files[rel] = sha256(full);
      counts.hashed += 1;
    }
  }
  return { files, counts };
}

function build(scope) {
  const { files, counts } = collect(scope);
  const doc = {
    schema: 'agentid-release/2',
    scope,
    generated: new Date().toISOString(),
    gitHead: gitHead(),
    fileCount: Object.keys(files).length,
    files,
  };
  if (scope === 'archive') {
    doc.rules = {
      include: ARCHIVE_RULES,
      exclude: ARCHIVE_EXCLUDE,
      depth: 'first-level-only',
      note: 'analysis/market is intentionally first-level only: 子目录（tests/ fixtures/ artifacts/ …）不进入签名，verify 会以「不纳入」信息行列出其中的源码，便于区分「排除」与「漏收」',
    };
    doc.counts = counts;
  }
  return Buffer.from(JSON.stringify(doc, null, 1));
}

function rulesMatch(listed) {
  if (!listed) return true; // 旧清单纯 archive（无 rules）：交由文件比对决定，不强制
  return JSON.stringify(listed) === JSON.stringify({
    include: ARCHIVE_RULES,
    exclude: ARCHIVE_EXCLUDE,
    depth: 'first-level-only',
    note: 'analysis/market is intentionally first-level only: 子目录（tests/ fixtures/ artifacts/ …）不进入签名，verify 会以「不纳入」信息行列出其中的源码，便于区分「排除」与「漏收」',
  });
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
  const doc = JSON.parse(body.toString());
  const listed = doc.files || {};
  if (scope === 'archive' && !rulesMatch(doc.rules)) {
    console.error(`❌ [archive] 清单里的收集规则与当前脚本不一致（规则变了但没重签）`);
    return 1;
  }
  const { files: now, counts } = collect(scope);
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
  if (scope === 'archive') {
    const skipped = outOfScopeSources();
    if (skipped.length) {
      console.log(`ℹ [archive] 不纳入（first-level only）：${skipped.length} 个源码文件未被签名`);
      for (const f of skipped) console.log('   · ' + f);
    }
  }
  console.log(`✅ [${scope}] 签名与 ${Object.keys(listed).length} 个文件一致` +
    (scope === 'archive' ? `（walked ${counts.walked} / hashed ${counts.hashed} / excluded ${counts.excluded} / skipped ${counts.skippedByRule}）` : ''));
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
  const doc = JSON.parse(body);
  console.log(`已生成未签名清单 ${OUT[scope].man} → ${Object.keys(doc.files).length} 个文件` +
    (scope === 'archive' ? `（walked ${doc.counts.walked} / hashed ${doc.counts.hashed} / excluded ${doc.counts.excluded} / skipped ${doc.counts.skippedByRule}）` : ''));
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
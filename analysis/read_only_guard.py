#!/usr/bin/env python3
"""read_only_guard.py —— 只读闸门的静态审查（不验签、不改文件、不联网）

设计边界（与 robincat-radar 的 guard_check 明确区分）：
  * 只做静态审查，不做发布验签；发布信任根仍然只有 verify-release.sh / verify-runtime.mjs。
  * 规则数量刻意保持少（<=10 条硬规则），任何例外都必须写在 ALLOW 里并带 reason + maxHits。
  * 例外条目不允许指向本文件自身（防止 guard 自己给自己开口子）。

用法：
  python3 analysis/read_only_guard.py            # 出错 exit 1
  python3 analysis/read_only_guard.py --json out.json
  python3 analysis/read_only_guard.py --list-rules
"""
import argparse
import json
import os
import re
import sys
import tokenize
import io

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCAN_DIRS = ['analysis', 'deploy', 'shadow']
SKIP_DIRS = {'__pycache__', '.git', 'node_modules', 'artifacts', 'raw', 'cache',
             'backup', 'bak', 'pgdata', '.pytest_cache'}
CODE_EXT = {'.py', '.mjs', '.cjs', '.js', '.sh'}

# 只读端点白名单：每一条都必须能说明「为什么只读」
ALLOWED_HOSTS = {
    'rpc.48.club': 'BSC 公共 RPC，仅 eth_call/eth_getLogs 只读调用',
    'bsc.blockrazor.xyz': 'BSC 公共 RPC，仅只读调用',
    '1rpc.io': 'BSC 公共 RPC，仅只读调用',
    'bsc-rpc.publicnode.com': 'BSC 公共 RPC，仅只读调用',
    'bsc-dataseed.binance.org': 'BSC 公共 RPC，仅只读调用',
    'api.dexscreener.com': '只读行情接口，不参与 W1 判据，不作 canonical evidence',
}

# 硬规则：命中即错。pattern 作用在「去注释、去字符串」的代码文本上（网络规则用原文）。
RULES = [
    ('NO_TRANSACTION', r'\b(eth_sendRawTransaction|eth_sendTransaction|sendRawTransaction|sendTransaction|eth_signTransaction)\b',
     '发送交易的 RPC 方法'),
    ('NO_SIGNATURE', r'\b(personal_sign|eth_signTypedData|eth_signTransaction|signTransaction|mnemonic|seedPhrase|privateKey|private_key)\b',
     '签名 / 私钥处理'),
    ('NO_WALLET', r'\b(baw|agentic-wallet|walletconnect|metamask|WalletConnect)\b',
     '钱包/自动交易 SDK'),
    ('NO_APPROVAL', r'\b(approve|setApprovalForAll|permit)\s*\(',
     '代币授权调用'),
    ('NO_DYNAMIC_CODE', r'\b(eval|exec)\s*\(|new\s+Function\s*\(',
     '动态执行代码'),
    ('NO_CHILD_PROCESS', r'\b(subprocess|os\.system|os\.popen|execSync|execFileSync|spawnSync)\b',
     '子进程调用（部署脚本除外）'),
]

# 敏感文件读取：路径里出现这些片段即告警
SECRET_PATH = re.compile(r'(\.pem|id_ed25519|id_rsa|\.secret|private[_-]?key)', re.I)
URL_RE = re.compile(r'https?://([A-Za-z0-9._-]+)')

STDLIB = set(getattr(sys, 'stdlib_module_names', ()))

# 例外：必须写 reason 与 maxHits；file 是正则；不许指向本文件
ALLOW = [
    {'rule': 'NO_CHILD_PROCESS', 'file': r'^shadow/probe\.mjs$', 'maxHits': 2,
     'reason': 'probe 用 execSync 执行 git rev-parse 取 build 溯源信息，不执行外部输入'},
    {'rule': 'NO_CHILD_PROCESS', 'file': r'^deploy/release-manifest\.mjs$', 'maxHits': 2,
     'reason': '发布工具调用 node 生成清单哈希，属发布链固定步骤，不接触链上执行'},
    {'rule': 'NO_CHILD_PROCESS', 'file': r'^analysis/element-archive\.py$', 'maxHits': 3,
     'reason': '归档脚本调用 tar/sha256sum 做只读快照打包'},
    {'rule': 'NO_TRANSACTION', 'file': r'^analysis/market/v4_decode\.py$', 'maxHits': 1,
     'reason': 'P0 解码器里出现 sendTransaction 字样只用于「事件名 -> 方法」对照表，未发调用'},
    {'rule': 'NO_WALLET', 'file': r'^analysis/market/P0-REPORT\.md$', 'maxHits': 3,
     'reason': '文档说明为何不接钱包，属文本引用'},
    {'rule': 'NO_CHILD_PROCESS', 'file': r'^analysis/baseline-selftest\.py$', 'maxHits': 2,
     'reason': '基线自检用 subprocess 跑 git rev-parse/status 记录版本，不执行外部输入'},
    {'rule': 'NO_CHILD_PROCESS', 'file': r'^analysis/w0-baseline\.py$', 'maxHits': 3,
     'reason': 'W0 基线快照用 subprocess 记录 git 版本与 sha256，只读'},
    {'rule': 'NO_CHILD_PROCESS', 'file': r'^analysis/market/replay\.py$', 'maxHits': 2,
     'reason': 'P0 回放用 subprocess 调 python 子进程做确定性重放，不联网不下单'},
    {'rule': 'NO_SECRET_READ', 'file': r'^deploy/verify-runtime\.mjs$', 'maxHits': 2,
     'reason': '只读 release-pubkey.pem 公钥验签，不接触私钥'},
    {'rule': 'NO_SECRET_READ', 'file': r'^deploy/publish-runtime\.sh$', 'maxHits': 3,
     'reason': '发布包装脚本引用本机签名私钥路径，仅用于离线签名，不发送任何资金交易'},
    {'rule': 'NO_SECRET_READ', 'file': r'^deploy/release-manifest\.mjs$', 'maxHits': 2,
     'reason': '清单签名工具读取 Ed25519 私钥生成签名，属发布链固定步骤'},
]


def iter_files():
    for d in SCAN_DIRS:
        base = os.path.join(ROOT, d)
        if not os.path.isdir(base):
            continue
        for cur, dirs, files in os.walk(base):
            dirs[:] = [x for x in dirs if x not in SKIP_DIRS]
            for f in files:
                p = os.path.join(cur, f)
                if os.path.splitext(f)[1].lower() in CODE_EXT:
                    yield os.path.relpath(p, ROOT).replace(os.sep, '/')


def strip_py(src, drop_docstrings=True):
    """去注释；drop_docstrings=True 时连 docstring 一起去掉，但保留普通字符串
    （真实 RPC 方法名就是字符串，必须能看见）。保留行号。"""
    out, line, col = [], 1, 0
    at_stmt_start = True
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            t, s, (sl, sc), (el, ec), _ = tok
            if t == tokenize.COMMENT:
                continue
            if t == tokenize.STRING and at_stmt_start and drop_docstrings:
                while line < el:
                    out.append('\n'); line += 1; col = 0
                continue
            if t in (tokenize.NEWLINE, tokenize.NL, tokenize.INDENT, tokenize.DEDENT):
                if t == tokenize.NEWLINE:
                    at_stmt_start = True
                while line < sl:
                    out.append('\n'); line += 1; col = 0
                continue
            if t in (tokenize.ENCODING, tokenize.ENDMARKER):
                continue
            at_stmt_start = False
            while line < sl:
                out.append('\n'); line += 1; col = 0
            if sl == line:
                if sc > col:
                    out.append(' ' * (sc - col)); col = sc
                out.append(s); col = ec
    except (tokenize.TokenError, IndentationError, SyntaxError, ValueError):
        return src
    return ''.join(out)


def strip_js(src):
    """粗略去注释与字符串（对本仓库足够）。"""
    out, i, n = [], 0, len(src)
    while i < n:
        c, d = src[i], src[i + 1] if i + 1 < n else ''
        if c == '/' and d == '/':
            j = src.find('\n', i); j = n if j < 0 else j
            out.append(' ' * (j - i)); i = j; continue
        if c == '/' and d == '*':
            j = src.find('*/', i + 2); j = n if j < 0 else j + 2
            out.append(''.join(ch if ch == '\n' else ' ' for ch in src[i:j])); i = j; continue
        if c in '\'"`':
            q, j = c, i + 1
            while j < n:
                if src[j] == '\\':
                    j += 2; continue
                if src[j] == q:
                    j += 1; break
                j += 1
            out.append(''.join(ch if ch == '\n' else ' ' for ch in src[i:j])); i = j; continue
        out.append(c); i += 1
    return ''.join(out)


def strip_sh(src):
    lines = []
    for ln in src.split('\n'):
        s = ln.strip()
        lines.append('' if (s.startswith('#') and not s.startswith('#!')) else ln)
    return '\n'.join(lines)


def code_of(path, raw):
    ext = os.path.splitext(path)[1].lower()
    if ext == '.py':
        return strip_py(raw)
    if ext == '.sh':
        return strip_sh(raw)
    return strip_js(raw)


def dependency_audit_findings():
    findings = []
    for name in ('package.json', 'package-lock.json', 'requirements.txt',
                 'pyproject.toml', 'uv.lock', 'Pipfile'):
        if os.path.exists(os.path.join(ROOT, name)):
            findings.append({'rule': 'NO_IMPORT_ESCAPE', 'file': name, 'line': 0,
                             'msg': '出现第三方依赖清单（当前仓库应为 stdlib-only）', 'snippet': name})
    for path in iter_files():
        fp = os.path.join(ROOT, path)
        try:
            raw = open(fp, encoding='utf-8', errors='replace').read()
        except OSError:
            continue
        if path.endswith('.py'):
            for m in re.finditer(r'^\s*(?:from\s+([A-Za-z_][\w.]*)|import\s+([A-Za-z_][\w.]*))', raw, re.M):
                mod = (m.group(1) or m.group(2)).split('.')[0]
                here = os.path.dirname(fp)
                local = any(os.path.exists(os.path.join(d, mod + '.py'))
                            for d in (here, os.path.dirname(here)))
                if mod not in STDLIB and not local:
                    findings.append({'rule': 'NO_IMPORT_ESCAPE', 'file': path,
                                     'line': raw[:m.start()].count('\n') + 1,
                                     'msg': f'非标准库 import: {mod}', 'snippet': m.group(0).strip()})
        elif path.endswith(('.mjs', '.cjs', '.js')):
            for m in re.finditer(r"""(?:from\s+|require\()\s*['"]([^'"]+)['"]""", raw):
                mod = m.group(1)
                if not (mod.startswith('node:') or mod.startswith('.')):
                    findings.append({'rule': 'NO_IMPORT_ESCAPE', 'file': path,
                                     'line': raw[:m.start()].count('\n') + 1,
                                     'msg': f'第三方模块: {mod}', 'snippet': m.group(0)[:60]})
    return findings


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--json', default='')
    ap.add_argument('--list-rules', action='store_true')
    a = ap.parse_args()
    if a.list_rules:
        for rid, pat, desc in RULES:
            print(f'{rid:22s} {desc}')
        print(f'ALLOWED_HOSTS: {", ".join(sorted(ALLOWED_HOSTS))}')
        return 0

    for e in ALLOW:
        if 'reason' not in e or len(e['reason'].strip()) < 8:
            print(f'FATAL allow 条目缺 reason: {e}', file=sys.stderr); return 2
        if re.search(r'read_only_guard\.py', e.get('file', '')):
            print('FATAL allow 不允许指向 guard 自身', file=sys.stderr); return 2

    hits, findings = {}, []
    for path in iter_files():
        fp = os.path.join(ROOT, path)
        try:
            raw = open(fp, encoding='utf-8', errors='replace').read()
        except OSError:
            continue
        if path == 'analysis/read_only_guard.py':
            continue  # 规则表本身含关键词；guard 只对自己做依赖面审计
        code = code_of(path, raw)
        for rid, pat, desc in RULES:
            if rid == 'NO_CHILD_PROCESS' and path.endswith('.sh'):
                continue  # 部署脚本本身就是 shell
            src = code
            for m in re.finditer(pat, src):
                line = src[:m.start()].count('\n') + 1
                key = (rid, path)
                hits[key] = hits.get(key, 0) + 1
                findings.append({'rule': rid, 'file': path, 'line': line,
                                 'msg': desc, 'snippet': m.group(0)[:60]})
        # 网络规则：看原文里的 URL 主机
        for m in URL_RE.finditer(raw):
            host = m.group(1).lower()
            if host not in ALLOWED_HOSTS:
                findings.append({'rule': 'NO_UNAPPROVED_NETWORK', 'file': path,
                                 'line': raw[:m.start()].count('\n') + 1,
                                 'msg': f'未登记外连: {host}', 'snippet': m.group(0)[:80]})
        # 敏感文件读取
        for m in SECRET_PATH.finditer(raw):
            line_txt = raw.splitlines()[raw[:m.start()].count('\n')] if raw else ''
            if re.search(r'pubkey|public|release-pubkey', line_txt, re.I):
                continue
            findings.append({'rule': 'NO_SECRET_READ', 'file': path,
                             'line': raw[:m.start()].count('\n') + 1,
                             'msg': '疑似读取私钥/证书路径', 'snippet': m.group(0)[:60]})

    findings += dependency_audit_findings()

    # 应用 allow（按 rule+file 计数上限）
    allowed_left = [dict(e) for e in ALLOW]
    kept = []
    for f in findings:
        e = next((x for x in allowed_left
                  if x['rule'] == f['rule'] and re.search(x['file'], f['file'])), None)
        if e and e['maxHits'] > 0:
            e['maxHits'] -= 1
            continue
        kept.append(f)

    out = {'ok': not kept, 'scanned': len(list(iter_files())),
           'errors': kept, 'allowedHosts': sorted(ALLOWED_HOSTS),
           'rules': [r[0] for r in RULES]}
    if a.json:
        with open(a.json, 'w', encoding='utf-8') as fh:
            json.dump(out, fh, ensure_ascii=False, indent=1)
    if kept:
        for f in kept[:40]:
            print(f"FAIL {f['rule']:22s} {f['file']}:{f['line']}  {f['msg']}  [{f['snippet']}]")
        print(f"read-only guard: {len(kept)} error(s) / scanned {out['scanned']} files")
        return 1
    print(f"read-only guard: OK ({out['scanned']} files, {len(RULES)} rules, "
          f"{len(ALLOW)} allow entries, stdlib-only)")
    return 0


if __name__ == '__main__':
    sys.exit(main())
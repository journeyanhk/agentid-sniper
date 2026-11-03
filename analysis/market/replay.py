#!/usr/bin/env python3
"""固定区间回放测试：同一窗口跑两遍，逐片 hash 与总 hash 必须完全一致。

用法：
  python3 replay.py --from-block 131400000 --to-block 131402000 --runs 2
  python3 replay.py --verify out/run1          # 校验落盘产物自身是否自洽

判定：
  * recordsHash 一致 + chunksHash 一致 → PASS（可复现）
  * 只有 chunk hash 变而记录不变 → 说明 RPC 返回顺序/多余日志变了，需要排查而不是放行
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from evidence import canonical_bytes, records_hash, sha256_hex  # noqa: E402


def run_scan(outdir: str, from_block: int, to_block: int, chunk: int, retries: int) -> dict:
    cmd = [sys.executable, os.path.join(HERE, "v4_scan.py"),
           "--from-block", str(from_block), "--to-block", str(to_block),
           "--chunk", str(chunk), "--retries", str(retries), "--out", outdir]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise SystemExit(f"scan failed rc={proc.returncode}\n{proc.stderr[-2000:]}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


def verify(dirpath: str) -> int:
    env = json.load(open(os.path.join(dirpath, "envelope.json"), encoding="utf-8"))
    recs = [json.loads(line) for line in open(os.path.join(dirpath, "records.jsonl"), encoding="utf-8") if line.strip()]
    recomputed = records_hash(recs)
    file_hash = sha256_hex(open(os.path.join(dirpath, "records.jsonl"), "rb").read())
    sums = {}
    sums_path = os.path.join(dirpath, "SHA256SUMS")
    if os.path.exists(sums_path):
        for line in open(sums_path, encoding="utf-8"):
            parts = line.split()
            if len(parts) == 2:
                sums[parts[1]] = parts[0]
    checks = {
        "recordsHashMatchesEnvelope": recomputed == env["hashes"]["output"],
        "recordsFileHashMatchesSums": sums.get("records.jsonl") == file_hash,
        "dataCompleteConsistent": env["dataComplete"] == (
            env["chunks"]["requested"] > 0
            and env["chunks"]["ok"] == env["chunks"]["requested"]
            and not env["chunks"]["skipped"]),
    }
    ok = all(checks.values())
    print(json.dumps({"verify": "PASS" if ok else "FAIL", "dir": dirpath, "checks": checks,
                      "records": len(recs), "recordsHash": recomputed}, ensure_ascii=False))
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-block", type=int)
    ap.add_argument("--to-block", type=int)
    ap.add_argument("--chunk", type=int, default=500)
    ap.add_argument("--runs", type=int, default=2)
    ap.add_argument("--retries", type=int, default=3)
    ap.add_argument("--workdir", default=None)
    ap.add_argument("--verify")
    args = ap.parse_args()

    if args.verify:
        return verify(args.verify)
    if args.from_block is None or args.to_block is None:
        print("need --from-block/--to-block (or --verify DIR)", file=sys.stderr)
        return 2

    root = args.workdir or tempfile.mkdtemp(prefix="p0-replay-")
    results = []
    for i in range(args.runs):
        out = os.path.join(root, f"run{i + 1}")
        res = run_scan(out, args.from_block, args.to_block, args.chunk, args.retries)
        res["dir"] = out
        results.append(res)
        print(json.dumps({"run": i + 1, **res}, ensure_ascii=False))

    base = results[0]
    checks = {
        "outputHashEqual": all(r["outputHash"] == base["outputHash"] for r in results),
        "chunksHashEqual": all(r["chunksHash"] == base["chunksHash"] for r in results),
        "recordsCountEqual": all(r["records"] == base["records"] for r in results),
        "dataCompleteEqual": all(r["dataComplete"] == base["dataComplete"] for r in results),
    }
    ok = all(checks.values())
    print(json.dumps({"replay": "PASS" if ok else "FAIL", "window": [args.from_block, args.to_block],
                      "chunkSize": args.chunk, "runs": args.runs, "checks": checks,
                      "outputHash": base["outputHash"], "chunksHash": base["chunksHash"],
                      "workdir": root}, ensure_ascii=False))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
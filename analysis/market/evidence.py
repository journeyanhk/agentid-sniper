"""最小 evidence envelope（v1）。

设计约束（P0 批准口径）：
  1. 每个 chunk 必须有 ok / attempts / skipped 记录，dataComplete 只能由台账推导；
  2. dataFresh 与 dataComplete 分开，滞后不等于缺口；
  3. 输出必须可复现：inputHash 固定输入，outputHash 固定逐行记录；
  4. 只描述事实，不给结论；不能算的字段写 null 并在 caveats 里说明。

不依赖任何第三方库。
"""
from __future__ import annotations

import hashlib
import json
import time

ENVELOPE_VERSION = "evidence-v1"


def canonical_bytes(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def records_hash(records) -> str:
    """逐行 canonical 后串联哈希：行序改变即 hash 改变，用来抓不确定性。"""
    h = hashlib.sha256()
    for rec in records:
        h.update(canonical_bytes(rec))
        h.update(b"\n")
    return h.hexdigest()


class ChunkLog:
    """分片台账。任何一片失败都必须留痕，禁止静默跳过。"""

    def __init__(self):
        self.rows = []

    def add(self, from_block: int, to_block: int, *, ok: bool, attempts: int,
            logs: int = 0, error: str | None = None, ms: int | None = None,
            endpoint: str | None = None, chunk_hash: str | None = None) -> None:
        self.rows.append({
            "fromBlock": from_block,
            "toBlock": to_block,
            "ok": bool(ok),
            "attempts": int(attempts),
            "logs": int(logs),
            "error": error,
            "ms": ms,
            "endpoint": endpoint,
            # 每片单独留 hash：重放时逐片比对，能把「哪一片变了」指出来
            "hash": chunk_hash,
        })

    def summary(self) -> dict:
        ok = sum(1 for r in self.rows if r["ok"])
        retried = sum(1 for r in self.rows if r["attempts"] > 1)
        skipped = [
            {"fromBlock": r["fromBlock"], "toBlock": r["toBlock"], "reason": r["error"] or "unknown"}
            for r in self.rows if not r["ok"]
        ]
        hashes = [r["hash"] for r in self.rows if r.get("hash")]
        sizes = [r["toBlock"] - r["fromBlock"] + 1 for r in self.rows] or [0]
        return {
            "requested": len(self.rows),
            "ok": ok,
            "retried": retried,
            "skipped": skipped,
            "minSize": min(sizes),
            "maxSize": max(sizes),
            "logsTotal": sum(r["logs"] for r in self.rows),
            "chunkHashes": hashes,
            "chunksHash": sha256_hex("".join(hashes).encode()),
        }


def input_hash(chain_cfg: dict, window: dict, decoder_version: str) -> str:
    core = {
        "chain": chain_cfg.get("chain"),
        "chainId": chain_cfg.get("chainId"),
        "poolManager": chain_cfg.get("poolManager"),
        "topics": chain_cfg.get("topics"),
        "layouts": {"init": chain_cfg.get("initLayout"), "swap": chain_cfg.get("swapLayout")},
        "window": {"fromBlock": window["fromBlock"], "toBlock": window["toBlock"]},
        "decoderVersion": decoder_version,
    }
    return sha256_hex(canonical_bytes(core))


def build_envelope(*, run_id: str, chain_cfg: dict, window: dict, decoder_version: str,
                   endpoints: list[str], chunks: ChunkLog, records: list[dict],
                   counts: dict, head_block: int, fresh_blocks_max: int,
                   block_seconds: float, caveats: list[str]) -> dict:
    lag_blocks = max(0, head_block - window["toBlock"])
    lag_minutes = round(lag_blocks * block_seconds / 60.0, 2)
    chunk_sum = chunks.summary()
    data_complete = chunk_sum["requested"] > 0 and chunk_sum["ok"] == chunk_sum["requested"] and not chunk_sum["skipped"]
    data_fresh = {
        "headBlock": head_block,
        "lagBlocks": lag_blocks,
        "lagMinutes": lag_minutes,
        "maxLagBlocks": fresh_blocks_max,
        "ok": lag_blocks <= fresh_blocks_max,
        "note": "滞后不等于数据缺口；缺口只看 chunks.skipped",
    }
    return {
        "envelope": ENVELOPE_VERSION,
        "runId": run_id,
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "chain": chain_cfg.get("chain"),
        "chainId": chain_cfg.get("chainId"),
        "source": {
            "kind": "jsonrpc-eth_getLogs",
            "endpoints": endpoints,
            "poolManager": chain_cfg.get("poolManager"),
        },
        "decoderVersion": decoder_version,
        "window": window,
        "chunks": chunk_sum,
        "dataComplete": data_complete,
        "dataFresh": data_fresh,
        "counts": counts,
        "hashes": {
            "input": input_hash(chain_cfg, window, decoder_version),
            "output": records_hash(records),
        },
        "caveats": caveats,
    }


def write_artifacts(outdir: str, records: list[dict], envelope: dict, extra_files: dict | None = None) -> dict:
    """写 records.jsonl + envelope.json + sha256 清单，返回路径。"""
    import os

    os.makedirs(outdir, exist_ok=True)
    jsonl_path = os.path.join(outdir, "records.jsonl")
    with open(jsonl_path, "w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n")
    env_path = os.path.join(outdir, "envelope.json")

    extra_hashes = {}
    for name, payload in (extra_files or {}).items():
        p = os.path.join(outdir, name)
        with open(p, "w", encoding="utf-8") as fh:
            if isinstance(payload, (dict, list)):
                json.dump(payload, fh, indent=2, ensure_ascii=False, sort_keys=True)
                fh.write("\n")
            else:
                fh.write(str(payload))
        extra_hashes[name] = sha256_hex(open(p, "rb").read())
    envelope.setdefault("hashes", {}).update(extra_hashes)

    with open(env_path, "w", encoding="utf-8") as fh:
        json.dump(envelope, fh, indent=2, ensure_ascii=False, sort_keys=True)
        fh.write("\n")
    sums = {
        "records.jsonl": sha256_hex(open(jsonl_path, "rb").read()),
        "envelope.json": sha256_hex(open(env_path, "rb").read()),
        "recordsHash": envelope["hashes"]["output"],
    }
    sum_path = os.path.join(outdir, "SHA256SUMS")
    with open(sum_path, "w", encoding="utf-8") as fh:
        for k in ("records.jsonl", "envelope.json"):
            fh.write(f"{sums[k]}  {k}\n")
        for name, h in extra_hashes.items():
            fh.write(f"{h}  {name}\n")
        fh.write(f"{sums['recordsHash']}  recordsHash\n")
    return {"records": jsonl_path, "envelope": env_path, "sums": sum_path, "sha256": sums}

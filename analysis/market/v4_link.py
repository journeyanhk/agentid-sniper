#!/usr/bin/env python3
"""V4 池子 ↔ Element collection 链接（分级，不硬凑同一性）。

身份问题的本质：V4 池子里的 currency0/1 是 ERC-20，Element 成交的是 ERC-721，
两者没有天然同一性。因此只给三级证据，并且只有 exact_address 能算「同一 collection」：

  exact_address  池子 token 地址 == collection 合约地址（强，几乎不会出现）
  same_deployer  需要 trace/创建交易，P0 免费 RPC 拿不到 → 一律 null，不臆造
  symbol_only    归一化后符号相同（弱，只作候选，不计入「同一 collection」）
  unlinked       无证据

用法：
  python3 v4_link.py --pools out/w0-init/pools.json --rows w0-rows.jsonl --out out/w0-init/links.json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

UA = "agentid-p0/1.0 (read-only)"
BLOCK_SECONDS = 0.45


def rpc_call(endpoints: list[str], method: str, params: list, timeout: float = 20.0):
    last = None
    for url in endpoints:
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
        req = urllib.request.Request(url, data=body, headers={"content-type": "application/json", "user-agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode())
            if "error" in payload:
                raise RuntimeError(str(payload["error"])[:160])
            return payload.get("result")
        except Exception as exc:
            last = f"{type(exc).__name__}: {exc}"
    raise RuntimeError(last or "rpc failed")


def parse_abi_string(hexstr: str | None) -> str | None:
    if not hexstr or len(hexstr) < 130:
        return None
    body = hexstr[2:]
    try:
        offset = int(body[0:64], 16) * 2
        length = int(body[offset:offset + 64], 16)
        if length == 0 or length > 128:
            raise ValueError
        return bytes.fromhex(body[offset + 64:offset + 64 + length * 2]).decode("utf-8", "replace").strip() or None
    except Exception:
        try:
            raw = body[0:64].rstrip("0")
            text = bytes.fromhex(raw).decode("utf-8", "replace").strip("\x00")
            return text or None
        except Exception:
            return None


def norm(sym: str | None) -> str | None:
    if not sym:
        return None
    v = re.sub(r"[^a-z0-9]", "", sym.lower())
    return v if len(v) >= 3 else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pools", required=True)
    ap.add_argument("--rows", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--config", default=os.path.join(HERE, "chain-config.json"))
    ap.add_argument("--ev-table", default=None, help="可选：collection→netKnownCostEV 的 JSON（来自 W0 报告）")
    args = ap.parse_args()

    cfg = json.load(open(args.config, encoding="utf-8"))["chains"]["bsc"]
    endpoints = cfg["rpc"]

    pools = json.load(open(args.pools, encoding="utf-8"))
    if isinstance(pools, dict):
        pools = pools.get("pools", [])

    collections: dict[str, dict] = {}
    for line in open(args.rows, encoding="utf-8"):
        if not line.strip():
            continue
        r = json.loads(line)
        c = r["collection"].lower()
        rec = collections.setdefault(c, {"collection": c, "fills": 0, "buyers": set(), "sellers": set(),
                                         "volumeWei": 0, "firstBlock": None, "lastBlock": None})
        rec["fills"] += 1
        if r.get("buyer"):
            rec["buyers"].add(r["buyer"].lower())
        if r.get("seller"):
            rec["sellers"].add(r["seller"].lower())
        try:
            rec["volumeWei"] += int(r.get("priceNative") or 0)
        except ValueError:
            pass
        b = r["blockNumber"]
        rec["firstBlock"] = b if rec["firstBlock"] is None else min(rec["firstBlock"], b)
        rec["lastBlock"] = b if rec["lastBlock"] is None else max(rec["lastBlock"], b)

    sym_cache: dict[str, str | None] = {}
    for c in collections:
        try:
            sym_cache[c] = parse_abi_string(rpc_call(endpoints, "eth_call", [{"to": c, "data": "0x95d89b41"}, "latest"]))
        except Exception:
            sym_cache[c] = None
        time.sleep(0.05)

    ev_table = json.load(open(args.ev_table, encoding="utf-8")) if args.ev_table else {}

    by_symbol: dict[str, list[str]] = {}
    for c, sym in sym_cache.items():
        n = norm(sym)
        if n:
            by_symbol.setdefault(n, []).append(c)
    by_addr = {c: c for c in collections}

    candidates = []
    tier_counts = {"exact_address": 0, "symbol_only": 0, "same_deployer": 0, "unlinked": 0}
    for pool in pools:
        tokens = [pool.get("currency0"), pool.get("currency1")]
        syms = pool.get("symbol") or {}
        hit = None
        tier = "unlinked"
        for idx, token in enumerate(tokens):
            if token and token.lower() in by_addr:
                hit, tier = token.lower(), "exact_address"
                break
        if hit is None:
            for idx, token in enumerate(tokens):
                n = norm((syms.get(f"currency{idx}") or None))
                if n and n in by_symbol and len(by_symbol[n]) == 1:
                    hit, tier = by_symbol[n][0], "symbol_only"
                    break
        tier_counts[tier] += 1
        if tier == "unlinked":
            continue
        c = collections[hit]
        lead_blocks = (c["firstBlock"] - pool["initBlock"]) if pool.get("initBlock") is not None else None
        candidates.append({
            "poolId": pool["poolId"],
            "tier": tier,
            "collection": hit,
            "collectionSymbol": sym_cache.get(hit),
            "poolSymbols": syms,
            "poolInitBlock": pool.get("initBlock"),
            "collectionFirstFillBlock": c["firstBlock"],
            "v4LeadBlocks": lead_blocks,
            "v4LeadMinutes": round(lead_blocks * BLOCK_SECONDS / 60.0, 2) if lead_blocks is not None else None,
            "v4EarlierThanElement": (lead_blocks is not None and lead_blocks > 0),
            "fills": c["fills"],
            "distinctBuyers": len(c["buyers"]),
            "distinctSellers": len(c["sellers"]),
            "volumeNativeWei": str(c["volumeWei"]),
            "netKnownCostEV": ev_table.get(hit) if isinstance(ev_table, dict) else None,
        })

    candidates.sort(key=lambda x: (x["tier"] != "exact_address", -(x["fills"] or 0)))
    strict = [c for c in candidates if c["tier"] == "exact_address"]
    strict_pass = [c for c in strict if c["v4EarlierThanElement"] and c["fills"] >= 20 and c["distinctBuyers"] >= 20]
    out = {
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "inputs": {"pools": args.pools, "rows": args.rows},
        "tierCounts": tier_counts,
        "collections": len(collections),
        "collectionsWithSymbol": sum(1 for v in sym_cache.values() if v),
        "poolCount": len(pools),
        "candidates": candidates,
        "strictCriterion": {
            "definition": "tier==exact_address 且 V4 先于 Element 且 fills>=20 且 buyers>=20",
            "matched": len(strict_pass),
            "ids": [c["collection"] for c in strict_pass],
        },
        "caveats": [
            "same_deployer 需要 trace/创建交易，免费 RPC 不可得 → 不做，不臆造",
            "symbol_only 是弱证据，不能当同一 collection；只列出供人工确认",
            "netKnownCostEV 只有在 --ev-table 提供时才有值，否则为 null",
        ],
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2, ensure_ascii=False, sort_keys=True)
        fh.write("\n")
    print(json.dumps({"ok": True, "out": args.out, "tiers": tier_counts, "pools": len(pools),
                      "collections": len(collections), "candidates": len(candidates),
                      "strictMatched": len(strict_pass)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
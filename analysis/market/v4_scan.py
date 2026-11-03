#!/usr/bin/env python3
"""BSC PancakeSwap Infinity (V4 类) 只读扫描器。

用法：
  python3 v4_scan.py --from-block 130769296 --to-block 130789296 --out out/run1
  python3 v4_scan.py --blocks 2000 --out out/run1          # 以当前 head 为终点

保证：
  * 每片都有 retry / skipped / hash；dataComplete 由台账推导；
  * 失败不静默：缩片、重试、仍失败则记 skipped 并继续（记录里能看出缺哪一段）；
  * 价格与 tick 全用整数/Decimal 定点；decimals 查不到就是 null，不默认 18；
  * 参与者一律 null（sender 是路由/池子调用者）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from evidence import ChunkLog, build_envelope, canonical_bytes, sha256_hex, write_artifacts  # noqa: E402
from v4_decode import classify, decode_initialize, decode_swap, price_token1_per_token0, sqrt_to_tick  # noqa: E402

DECODER_VERSION = "v4-decoder-v1"
UA = "agentid-p0/1.0 (read-only)"


class RpcError(Exception):
    pass


class Rpc:
    def __init__(self, endpoints: list[str], timeout: float = 25.0):
        self.endpoints = endpoints
        self.timeout = timeout
        self.idx = 0
        self.calls = 0

    def call(self, method: str, params: list):
        last = None
        for k in range(len(self.endpoints)):
            url = self.endpoints[(self.idx + k) % len(self.endpoints)]
            body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
            req = urllib.request.Request(url, data=body, headers={"content-type": "application/json", "user-agent": UA})
            try:
                self.calls += 1
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    payload = json.loads(resp.read().decode())
                if "error" in payload:
                    raise RpcError(str(payload["error"].get("message") or payload["error"])[:200])
                self.idx = (self.idx + k) % len(self.endpoints)
                return payload.get("result")
            except RpcError:
                raise
            except Exception as exc:  # 网络/HTTP/解析
                last = f"{type(exc).__name__}: {exc}"
                continue
        raise RpcError(last or "rpc failed")


def is_range_error(msg: str) -> bool:
    m = msg.lower()
    return any(k in m for k in ("limit", "range", "too many", "exceed", "more than", "block range"))


def call_data(to: str, data: str) -> str | None:
    res = RPC.call("eth_call", [{"to": to, "data": data}, "latest"])
    if not res or not res.startswith("0x") or len(res) < 3:
        return None
    return res


def parse_uint(hexstr: str | None) -> int | None:
    if not hexstr or len(hexstr) < 66:
        return None
    try:
        return int(hexstr[2:66], 16)
    except ValueError:
        return None


def parse_abi_string(hexstr: str | None) -> str | None:
    if not hexstr or len(hexstr) < 130:
        return None
    body = hexstr[2:]
    try:
        offset = int(body[0:64], 16) * 2
        length = int(body[offset:offset + 64], 16)
        raw = body[offset + 64:offset + 64 + length * 2]
        return bytes.fromhex(raw).decode("utf-8", "replace").strip("\x00").strip() or None
    except Exception:
        try:  # bytes32 symbol
            raw = body[0:64].rstrip("0")
            text = bytes.fromhex(raw).decode("utf-8", "replace").strip("\x00")
            return text or None
        except Exception:
            return None


TOKEN_CACHE: dict[str, dict] = {}


QUOTE_ASSETS: dict[str, dict] = {}


def token_meta(addr: str) -> dict:
    if addr in TOKEN_CACHE:
        return TOKEN_CACHE[addr]
    if addr in QUOTE_ASSETS:
        qa = QUOTE_ASSETS[addr]
        meta = {"address": addr, "decimals": qa.get("decimals"), "decimalsSource": "config:quoteAsset",
                "symbol": qa.get("symbol")}
        TOKEN_CACHE[addr] = meta
        return meta
    dec = None
    dec_src = "unavailable"
    try:
        raw = parse_uint(call_data(addr, "0x313ce567"))
        if raw is not None and 0 < raw <= 36:
            dec, dec_src = raw, "onchain:decimals()"
    except RpcError:
        dec_src = "rpc_error"
    sym = None
    try:
        sym = parse_abi_string(call_data(addr, "0x95d89b41"))
    except RpcError:
        pass
    meta = {"address": addr, "decimals": dec, "decimalsSource": dec_src, "symbol": sym}
    TOKEN_CACHE[addr] = meta
    return meta


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "chain-config.json"))
    ap.add_argument("--chain", default="bsc")
    ap.add_argument("--from-block", type=int)
    ap.add_argument("--to-block", type=int)
    ap.add_argument("--blocks", type=int, default=2000, help="未指定 from/to 时，按 head 往回取多少块")
    ap.add_argument("--out", required=True)
    ap.add_argument("--chunk", type=int, default=2000)
    ap.add_argument("--min-chunk", type=int, default=200)
    ap.add_argument("--retries", type=int, default=3)
    ap.add_argument("--fresh-blocks", type=int, default=60)
    ap.add_argument("--max-tokens", type=int, default=150)
    ap.add_argument("--events", choices=["both", "init", "swap"], default="both")
    args = ap.parse_args()

    cfg_all = json.load(open(args.config, encoding="utf-8"))
    cfg = cfg_all["chains"][args.chain]
    endpoints = cfg["rpc"]
    global RPC, QUOTE_ASSETS
    RPC = Rpc(endpoints)
    QUOTE_ASSETS = {k.lower(): v for k, v in (cfg.get("quoteAssets") or {}).items()}

    head = int(RPC.call("eth_blockNumber", []), 16)
    to_block = args.to_block if args.to_block is not None else head
    from_block = args.from_block if args.from_block is not None else to_block - args.blocks + 1
    if from_block > to_block:
        print("from-block > to-block", file=sys.stderr)
        return 2

    def block_ts(num: int) -> int | None:
        blk = RPC.call("eth_getBlockByNumber", [hex(num), False])
        return int(blk["timestamp"], 16) if blk else None

    ts_from = block_ts(from_block)
    ts_to = block_ts(to_block)
    block_seconds = ((ts_to - ts_from) / (to_block - from_block + 1)) if (ts_from and ts_to and to_block > from_block) else 0.45

    if args.events == "init":
        topics = [[cfg["topics"]["initialize"]]]
    elif args.events == "swap":
        topics = [[cfg["topics"]["swap"]]]
    else:
        topics = [[cfg["topics"]["initialize"], cfg["topics"]["swap"]]]
    chunks = ChunkLog()
    records: list[dict] = []
    decode_errors = 0
    decode_error_samples: list[dict] = []
    size = max(args.min_chunk, args.chunk)
    cur = from_block
    while cur <= to_block:
        hi = min(to_block, cur + size - 1)
        attempts = 0
        ok = False
        err = None
        logs = []
        started = time.time()
        while attempts < args.retries:
            attempts += 1
            try:
                logs = RPC.call("eth_getLogs", [{
                    "address": cfg["poolManager"],
                    "topics": topics,
                    "fromBlock": hex(cur),
                    "toBlock": hex(hi),
                }]) or []
                ok = True
                break
            except RpcError as exc:
                err = str(exc)
                if is_range_error(err) and size > args.min_chunk:
                    size = max(args.min_chunk, size // 2)
                    hi = min(to_block, cur + size - 1)
                    err = f"range-limited, shrink to {size}: {err}"
                    continue
                time.sleep(0.4 * attempts)
        ms = int((time.time() - started) * 1000)
        chunk_hash = sha256_hex(canonical_bytes(logs)) if ok else None
        chunks.add(cur, hi, ok=ok, attempts=attempts, logs=len(logs), error=None if ok else err, ms=ms, chunk_hash=chunk_hash)
        if ok:
            for log in logs:
                try:
                    t0 = log["topics"][0]
                    if t0 == cfg["topics"]["initialize"]:
                        records.append(decode_initialize(log, cfg["initLayout"]))
                    elif t0 == cfg["topics"]["swap"]:
                        records.append(decode_swap(log, cfg["swapLayout"]))
                except Exception as exc:
                    decode_errors += 1
                    if len(decode_error_samples) < 5:
                        decode_error_samples.append({"txHash": log.get("transactionHash"),
                                                     "topics": len(log.get("topics", [])),
                                                     "dataWords": max(0, (len(log.get("data", "0x")) - 2) // 64),
                                                     "error": f"{type(exc).__name__}: {exc}"[:160]})
        cur = hi + 1
        if ok and size < args.chunk:
            size = min(args.chunk, size + 50)

    records.sort(key=lambda r: (r["blockNumber"], r["logIndex"], r["kind"]))

    # 汇总池子状态
    pools: dict[str, dict] = {}
    tokens: set[str] = set()
    for rec in records:
        pid = rec["poolId"]
        pool = pools.setdefault(pid, {
            "poolId": pid,
            "initBlock": None,
            "currency0": None,
            "currency1": None,
            "hooks": None,
            "initSqrtPriceX96": None,
            "initTick": None,
            "swaps": 0,
            "buys": 0,
            "sells": 0,
            "maxLiquidity": 0,
            "lastSqrtPriceX96": None,
            "lastTick": None,
            "firstSwapBlock": None,
            "lastSwapBlock": None,
            "participants": None,
        })
        if rec["kind"] == "initialize":
            pool.update({
                "initBlock": rec["blockNumber"],
                "currency0": rec["currency0"],
                "currency1": rec["currency1"],
                "hooks": rec["hooks"],
                "initSqrtPriceX96": rec["sqrtPriceX96"],
                "initTick": rec["tick"],
            })
            tokens.update([rec["currency0"], rec["currency1"]])
        else:
            pool["swaps"] += 1
            pool["buys" if rec["direction"] == "buy" else "sells"] += 1
            pool["maxLiquidity"] = max(pool["maxLiquidity"], int(rec["liquidity"]))
            pool["lastSqrtPriceX96"] = rec["sqrtPriceX96"]
            pool["lastTick"] = rec["tick"]
            pool["firstSwapBlock"] = pool["firstSwapBlock"] or rec["blockNumber"]
            pool["lastSwapBlock"] = rec["blockNumber"]
            tokens.add(pool["currency0"] or "")
            tokens.add(pool["currency1"] or "")

    tokens.discard("")
    resolved = 0
    for addr in list(tokens)[: args.max_tokens]:
        meta = token_meta(addr)
        if meta["decimals"] is not None:
            resolved += 1

    state_counts = {"poolDiscovered": 0, "swapObserved": 0, "priceComputable": 0,
                    "liquidityObserved": 0, "executionTradableUnknown": 0}
    for pool in pools.values():
        m0 = TOKEN_CACHE.get(pool["currency0"] or "", {})
        m1 = TOKEN_CACHE.get(pool["currency1"] or "", {})
        dec0, dec1 = m0.get("decimals"), m1.get("decimals")
        sqrt_last = int(pool["lastSqrtPriceX96"]) if pool["lastSqrtPriceX96"] else (
            int(pool["initSqrtPriceX96"]) if pool["initSqrtPriceX96"] else 0)
        price = price_token1_per_token0(sqrt_last, dec0, dec1)
        pool["decimals"] = {"currency0": dec0, "currency1": dec1}
        pool["decimalsSource"] = {"currency0": m0.get("decimalsSource"), "currency1": m1.get("decimalsSource")}
        pool["symbol"] = {"currency0": m0.get("symbol"), "currency1": m1.get("symbol")}
        pool["priceToken1PerToken0"] = str(price) if price is not None else None
        pool["tickFromSqrt"] = sqrt_to_tick(sqrt_last) if sqrt_last else None
        pool["tickConsistent"] = (
            pool["tickFromSqrt"] is not None and pool["initTick"] is not None
            and abs(pool["tickFromSqrt"] - int(pool["initTick"])) <= 3
        ) if pool["initTick"] is not None and pool["swaps"] == 0 else None
        pool["states"] = classify({
            "init": bool(pool["initBlock"]),
            "swaps": pool["swaps"],
            "price": price,
            "decimalsKnown": dec0 is not None and dec1 is not None,
            "maxLiquidity": pool["maxLiquidity"],
        })
        for key in ("poolDiscovered", "swapObserved"):
            state_counts[key] += 1 if pool["states"][key] else 0
        state_counts["priceComputable"] += 1 if pool["states"]["priceComputable"] == "yes" else 0
        state_counts["liquidityObserved"] += 1 if pool["states"]["liquidityObserved"] == "yes" else 0
        state_counts["executionTradableUnknown"] += 1

    pools_sorted = [pools[k] for k in sorted(pools)]
    window = {
        "fromBlock": from_block,
        "toBlock": to_block,
        "fromTimestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts_from)) if ts_from else None,
        "toTimestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts_to)) if ts_to else None,
        "blockMinutes": round((to_block - from_block + 1) * block_seconds / 60.0, 2),
    }
    counts = {
        "records": len(records),
        "initialize": sum(1 for r in records if r["kind"] == "initialize"),
        "swap": sum(1 for r in records if r["kind"] == "swap"),
        "pools": len(pools),
        "tokensSeen": len(tokens),
        "tokensResolved": resolved,
        "tokensUnresolved": max(0, len(tokens) - resolved),
        "decodeErrors": decode_errors,
        "decodeErrorSamples": decode_error_samples,
        "tickCheckedAtInit": sum(1 for p in pools.values() if p.get("tickConsistent") is not None),
        "tickMismatchAtInit": sum(1 for p in pools.values() if p.get("tickConsistent") is False),
        "states": state_counts,
    }
    caveats = [
        "participant=null: V4 Swap 的 sender 是路由/池子调用者（实测 sender==tx.to，!=tx.from），不能当交易者",
        "fee/tickSpacing 未断言语义：Initialize 中间字段原样保留在 unresolvedInitWords",
        "executionTradable=unknown: P0 没有真实 quote 或模拟，任何肯定都是编造",
        "priceToken1PerToken0 是池子中间价（sqrtPriceX96），不是可成交价；未换算 USD",
        "decimals 查不到时为 null，价格随之为 null（不默认 18）",
    ]
    envelope = build_envelope(
        run_id=f"v4-{args.chain}-{from_block}-{to_block}",
        chain_cfg=cfg,
        window=window,
        decoder_version=DECODER_VERSION,
        endpoints=endpoints,
        chunks=chunks,
        records=records,
        counts=counts,
        head_block=head,
        fresh_blocks_max=args.fresh_blocks,
        block_seconds=block_seconds,
        caveats=caveats,
    )
    paths = write_artifacts(args.out, records, envelope, extra_files={"pools.json": pools_sorted})
    print(json.dumps({
        "ok": True,
        "out": args.out,
        "records": len(records),
        "pools": len(pools),
        "dataComplete": envelope["dataComplete"],
        "skipped": len(envelope["chunks"]["skipped"]),
        "outputHash": envelope["hashes"]["output"],
        "chunksHash": envelope["chunks"]["chunksHash"],
        "states": state_counts,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
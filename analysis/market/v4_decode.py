"""V4 / PancakeSwap Infinity 事件解码（BSC，只读）。

口径要点：
  * 全部用整数 / Decimal 定点，禁止 float 参与价格与 tick；
  * 字段位置先用真实日志的不变量验证过（见 P0-REPORT.md §验证）：
      Initialize: topics[1]=poolId, topics[2]=currency0, topics[3]=currency1,
                  data[0]=hooks, data[-2]=sqrtPriceX96, data[-1]=tick（101/101 一致性）
      Swap:       topics[1]=poolId, topics[2]=sender,
                  data = [amount0, amount1, sqrtPriceX96, liquidity, tick, fee?, protocolFee?]
                  （27,976 条实测：amount0/amount1 符号相反 99.99%，tick 与 sqrt 一致 100%）
  * 中间字段（fee / tickSpacing / parameters）不猜语义，原样保留并标 unresolved；
  * sender 实测等于 tx.to（路由/池子调用者），不等于交易者 → 参与者一律 null。
"""
from __future__ import annotations

from decimal import Decimal, getcontext, ROUND_FLOOR

getcontext().prec = 60

ZERO_ADDR = "0x" + "0" * 40
Q96 = 1 << 96
Q192 = 1 << 192
TICK_BASE = Decimal("1.0001")  # 注意：不是 0.0001，写错会把 179883 算成 -2


def _words(data_hex: str) -> list[int]:
    body = data_hex[2:] if data_hex.startswith("0x") else data_hex
    return [int(body[i * 64:(i + 1) * 64], 16) for i in range(len(body) // 64)]


def _addr(topic_or_word) -> str:
    """接受 topic 字符串或 ABI word 整数；hooks 等地址字段两处都会用到。"""
    if isinstance(topic_or_word, int):
        return "0x" + format(topic_or_word, "064x")[-40:]
    return "0x" + str(topic_or_word)[-40:].lower()


def to_signed(word: int) -> int:
    """256 位补码 → 有符号（ABI 对 int128/int24 会符号扩展到 256 位）。"""
    return word - (1 << 256) if word >= (1 << 255) else word


def sqrt_to_tick(sqrt_price_x96: int) -> int | None:
    if sqrt_price_x96 <= 0:
        return None
    raw = (Decimal(sqrt_price_x96) / Decimal(Q96)) ** 2
    if raw <= 0:
        return None
    return int((raw.ln() / TICK_BASE.ln()).to_integral_value(rounding=ROUND_FLOOR))


def price_token1_per_token0(sqrt_price_x96: int, dec0: int | None, dec1: int | None) -> Decimal | None:
    """池子中间价：1 个 token0 值多少 token1（已按 decimals 归一到人类单位）。"""
    if sqrt_price_x96 <= 0 or dec0 is None or dec1 is None:
        return None
    raw = (Decimal(sqrt_price_x96) ** 2) / Decimal(Q192)
    return raw * (Decimal(10) ** (dec0 - dec1))


def decode_initialize(log: dict, layout: dict) -> dict:
    topics = log["topics"]
    words = _words(log["data"])
    hook_idx = layout.get("hookWord", 0)
    sqrt_idx = len(words) - layout.get("sqrtFromEnd", 2)
    tick_idx = len(words) - layout.get("tickFromEnd", 1)
    unresolved = [w for i, w in enumerate(words) if i not in (hook_idx, sqrt_idx, tick_idx)]
    return {
        "kind": "initialize",
        "blockNumber": int(log["blockNumber"], 16),
        "logIndex": int(log["logIndex"], 16),
        "txHash": log["transactionHash"],
        "poolId": log["topics"][1],
        "currency0": _addr(topics[2]),
        "currency1": _addr(topics[3]),
        "hooks": _addr(words[hook_idx]) if words[hook_idx] else ZERO_ADDR,
        "sqrtPriceX96": str(words[sqrt_idx]),
        "tick": to_signed(words[tick_idx]),
        "unresolvedInitWords": [str(w) for w in unresolved],
        "fieldStatus": {
            "currency0": "decoded",
            "currency1": "decoded",
            "hooks": "decoded",
            "sqrtPriceX96": "decoded",
            "tick": "decoded",
            "fee": "unresolved",
            "tickSpacing": "unresolved",
        },
    }


def decode_swap(log: dict, layout: dict) -> dict:
    words = _words(log["data"])
    amount0 = to_signed(words[layout["amount0"]])
    amount1 = to_signed(words[layout["amount1"]])
    return {
        "kind": "swap",
        "blockNumber": int(log["blockNumber"], 16),
        "logIndex": int(log["logIndex"], 16),
        "txHash": log["transactionHash"],
        "poolId": log["topics"][1],
        "swapSender": _addr(log["topics"][2]),
        "amount0": str(amount0),
        "amount1": str(amount1),
        "sqrtPriceX96": str(words[layout["sqrtPriceX96"]]),
        "liquidity": str(words[layout["liquidity"]]),
        "tick": to_signed(words[layout["tick"]]),
        "feeRaw": str(words[layout["feeWord"]]) if len(words) > layout.get("feeWord", 99) else None,
        "protocolFeeRaw": str(words[layout["protocolFeeWord"]]) if len(words) > layout.get("protocolFeeWord", 99) else None,
        # 参与者：sender 是路由/池子调用者，不是交易者。宁可 null 也不编造。
        "participant": None,
        "participantReason": "sender_is_router_or_pool_manager_caller",
        "direction": "buy" if amount1 > 0 else ("sell" if amount1 < 0 else "flat"),
    }


def classify(states: dict) -> dict:
    """五态：只描述观测到的事实，executionTradable 在无真实 quote 时必须是 unknown。"""
    return {
        "poolDiscovered": bool(states.get("init")),
        "swapObserved": bool(states.get("swaps", 0) > 0),
        "priceComputable": "yes" if states.get("price") is not None else ("unknown" if states.get("decimalsKnown") is False else "no"),
        "liquidityObserved": "yes" if states.get("maxLiquidity", 0) > 0 else ("no" if states.get("swaps", 0) > 0 else "unknown"),
        "executionTradable": "unknown",
        "executionReason": "no_quote_or_simulation_adapter_in_p0",
    }
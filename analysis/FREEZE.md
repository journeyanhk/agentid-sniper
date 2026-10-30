# W0 代码冻结（2026-10-30 15:20 CST）

- 冻结对象：`shadow/probe.mjs`（`probe-4.0.0`，commit `d0d27bc`）。24 小时观察期内不再修改探针逻辑、评分口径、扫描参数。
- 仅允许新增：`analysis/` 下**只读**证据采集脚本（`recheck-pools.py`、`curve-exit.py`、`market-watch.py`）。它们不签名、不发交易、不接钱包、不执行 baw。
- 禁止：接钱包、approve、mint、sell、买 RPC、修改自动交易配置、改止盈止损参数。
- 观察期：至 2026-10-31 15:20 CST。判定见 `analysis/FINDINGS.md` 与 2 小时巡检记录。

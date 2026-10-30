# shadow/ — W0 只读影子探针

无钱包、无私钥、不下单、不依赖 `baw`。只做一件事：把 BSC 新盘的铸造事件聚合成信号，验证这条赛道真实长什么样。

```bash
node shadow/probe.mjs            # 输出到脚本同级目录
SHADOW_DIR=/tmp/shadow node shadow/probe.mjs
```

输出：`signals.jsonl`（每个候选盘的体检结果）、`metrics.jsonl`（每轮 RPC 指标）。

判据：字节码包含 Agent ID registry `0x60b9262456de011f7d15a54cdd8c48baaa8b022f` 视为 gated；每个盘抽样 7 个铸造钱包查 registry 余额，验证门槛是否真实生效。

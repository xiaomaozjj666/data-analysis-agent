# Agent 评测（配对统计护栏）

改 prompt、换工具、调参数之后，怎么证明「真的更好」？这个目录给出一个可复现的答案：
**同一批用例的配对结果 + McNemar 精确检验 + 成本/延迟护栏 + 哈希链台账**。
`accept` 之外一律不放行，`inconclusive`（样本不足）也算不通过。

工具本身不调用模型，只消费 JSONL，所以在 CI、评审机器、离线容器里都能跑出同一结论。

## 结果格式

每行一个用例（也接受 JSON 数组）：

```json
{"case_id": "smoke-01", "passed": true, "cost_usd": 0.0123, "latency_ms": 4300}
```

- `case_id`：稳定标识，两次运行必须完全一致。
- `passed`：该用例是否通过（由你的断言决定）。
- `cost_usd` / `latency_ms`：可选；用于成本与延迟护栏。

## 用法

```bash
# 比较两次运行；accept=0，reject=1，inconclusive=2
data-agent-eval compare \
  --baseline eval/examples/baseline.jsonl \
  --candidate eval/examples/candidate.jsonl \
  --ledger eval/ledger.jsonl \
  --git-sha "$(git rev-parse --short HEAD)" \
  --notes "prompt: 强制先调用 inspect_data"

# 校验台账哈希链（任何一条被改写都会报错）
data-agent-eval verify-ledger eval/ledger.jsonl
```

可用 `--policy policy.json` 调整门槛：

```json
{"alpha": 0.05, "max_cost_increase": 0.10, "max_latency_increase": 0.10, "min_cases": 8}
```

## CI 接法

```yaml
- name: Agent eval gate
  run: |
    data-agent-eval compare \
      --baseline eval/baseline.jsonl \
      --candidate eval/candidate.jsonl
```

只有 `accept` 返回 0。把 `inconclusive` 当通过，等于把「样本不足」包装成「已验证」。

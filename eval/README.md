# Agent 评测（确定性离线门禁 + 配对统计护栏）

本目录提供两种用途不同的评测：

1. **提交门禁（CI 必跑）**：`run-offline` 用脚本化 ChatModel 驱动真实
   `DataWorkspace → LangGraph → inspect_data → finalize` 工作流，不需要 API Key，
   不访问模型服务。通过/失败来自当前源码的实际输出，不允许手工填写结果。
2. **改动对比（有两次真实运行结果时）**：`compare` 对同一批用例做 McNemar
   精确检验，并检查成本/延迟护栏；`accept` 之外一律不放行。

## 离线提交门禁

```bash
data-agent-eval run-offline \
  --suite eval/examples/suite.json \
  --output "$RUNNER_TEMP/offline-results.jsonl" \
  --min-cases 8 \
  --min-accuracy 1.0
```

- `suite.json` 定义真实表格数据、任务与确定性断言（工具调用、计划、完成步骤、
  行列结构和报告片段）。
- 输出 JSONL 除标准字段外还带 `details.actual`（实际工具轨迹、计划、数据结构和
  报告 SHA256）与 `details.failures`，便于审计。`offline-results.jsonl` 是一次
  真实生成的示例，不是维护者手工编写的期望值。
- 离线模型成本固定为 `0.0`；`latency_ms` 是本机实际耗时，因此两次运行的耗时
  可以不同，但断言结果必须一致。
- 退出码：`0` 表示用例数和通过率均达到阈值，`1` 表示门禁失败，`2` 表示参数、
  套件结构或文件错误。CI 原样使用该退出码，不依赖任何真实外部 API。

## 结果格式

每行一个用例（也接受 JSON 数组）：

```json
{"case_id": "sales-basic", "passed": true, "cost_usd": 0.0, "latency_ms": 97.1, "details": {"mode": "offline_fake", "actual": {}, "failures": []}}
```

- `case_id`：稳定标识，配对比较时两次运行必须完全一致。
- `passed`：由 runner 根据实际输出计算，读取器要求布尔值。
- `cost_usd` / `latency_ms`：可选；用于成本与延迟护栏。
- `details`：可选对象，保存可审计证据，不参与统计计算。

## 配对改动对比

```bash
data-agent-eval compare \
  --baseline path/to/baseline-results.jsonl \
  --candidate path/to/candidate-results.jsonl \
  --ledger eval/ledger.jsonl \
  --git-sha "$(git rev-parse --short HEAD)" \
  --notes "prompt: 强制先调用 inspect_data"

data-agent-eval verify-ledger eval/ledger.jsonl
```

`accept=0`、`reject=1`、`inconclusive=2`。可用 `--policy policy.json` 调整门槛：

```json
{"alpha": 0.05, "max_cost_increase": 0.10, "max_latency_increase": 0.10, "min_cases": 8}
```

## CI 接法

```yaml
- name: Agent eval gate (offline)
  run: |
    data-agent-eval run-offline \
      --suite eval/examples/suite.json \
      --output "$RUNNER_TEMP/offline-results.jsonl" \
      --min-cases 8 \
      --min-accuracy 1.0
```

仓库 `.github/workflows/ci.yml` 已配置独立 `eval` job。不要把 `inconclusive`
或失败用例当通过，也不要为了变绿而降低阈值或删减套件。

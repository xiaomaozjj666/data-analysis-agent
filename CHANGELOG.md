# Changelog

本项目的所有显著变更都记录在本文件中。
格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循[语义化版本](https://semver.org/lang/zh-CN/)。

项目自 2026-07 起以 trunk 方式开发（`main` 即最新可用版），本文件从 v1.0.0
起维护；更早的历史按月份归档在版本条目里。

## [Unreleased]

### 新增

- **发布前评测闸门 `data-agent-eval`**：读取同一套用例的基线与候选结果（JSONL /
  JSON 数组），做配对 McNemar 精确检验、Wilson 区间与成本/延迟护栏，输出
  `accept` / `reject` / `inconclusive` 三态裁决并以 `0/1/2` 退出码接入 CI；裁决可
  追加进带 `prev`/`hash` 链的 ledger，`verify-ledger` 可检测篡改。示例与说明见
  `eval/README.md`、`docs/agent-eval.md`。
- **分析溯源哈希链**：分析完成时写 `provenance.jsonl` / `provenance.json`，覆盖
  任务元信息、数据集、逐条工具调用、产物与报告正文；`GET /api/sessions/{id}/provenance`
  当场复核，`/provenance/export` 下载自包含 JSON，前端导出菜单同步增加入口。
  溯源失败只记日志，不影响已完成的分析。

### 修复

- **评测 / 溯源的输入校验收紧**：ledger 追加前先复核现有链，历史被改写或含
  非对象行时拒绝继续写入（不再在坏链上续写）；`cost_usd` / `latency_ms` 拒绝
  负数、NaN 与 Infinity；`session.json` 存在但无法解析或缺少报告正文时，溯源
  复核明确报错，不再静默跳过报告比对。
- **`DATA_AGENT_TRUSTED_PROXY_HOPS` 改为调用期读取**：此前在中间件模块导入期
  求值，而 `load_dotenv()` 在更晚的导入链里才执行——只写在 `.env` 里的该变量
  被静默忽略，反代后部署的限流键退化为代理 IP，全部客户端共享同一个限流桶。
  现与 `sql_guard.query_time_budget_ms` 同一约定按调用读取，并补回归测试。
- 移除根目录 `ENGINEERING-NOTES.md` 迁移残留（内容已并入
  `docs/engineering-notes.md`，并修复其中被吞字符的 `full_page` /
  `api_bigtest0001`）；`docs/competitive-analysis.md` 工具数 10 → 11 对齐现实。
- `render.yaml` 补 `DEEPSEEK_API_KEY` / `DEEPSEEK_MODEL` 入口（默认 provider
  为 deepseek，此前 Render 部署按 README 走默认链路时无处填 Key）。
- `.gitignore` 补 `.mypy_cache/`；`.env.example` 的 `LANGSMITH_TRACING`
  默认改 `false`（避免照抄示例即向占位 Key 开启 tracing）；前端包版本与
  后端统一为 1.0.0。

## [1.0.0] - 2026-10

首个版本化发布。自 2026-07 起按 trunk 持续演进的完整能力面：

### 数据面

- 受控工具集 11 项：统计/绘图/清洗/SQL 查询/沙箱代码执行等，清洗硬护栏
  （缺失值删除 >50% 拒绝执行、主数据行数不低于原始 20%）。
- 只读 SQL 数据源：SQLite + PostgreSQL，5000 行 / 500 列结果上限，跨源合并。
- MCP 数据面服务（stdio），把受控数据访问开放给其他 Agent。

### 分析与报告

- LangGraph 主流程：规划 → 受控执行 → 独立评审 → 报告；SSE 流式输出，
  15 秒心跳，配方（recipe）复跑与报告导出。
- Plotly + ECharts 双图表引擎：大数据散点自动密度视图（≥2 万行）、LTTB
  降采样、3D 散点、SPLOM、统计图服务端聚合，亮暗双主题与对比度 AA 实测。

### 工程与安全

- 环回保护（非环回绑定必须配访问令牌）、按 Key/IP 滑动窗口限流、
  XFF 信任跳数、并发与成本闸门；沙箱进程隔离。
- FastAPI + React 全流程界面；本地 / Docker / Render 云端可部署；
  后端 pytest 覆盖率门禁 97%，前端 vitest + tsc + build 门禁。

[Unreleased]: https://github.com/xiaomaozjj666/data-analysis-agent/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/xiaomaozjj666/data-analysis-agent/commits/v1.0.0

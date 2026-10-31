# 评审指南

本文件定义本仓库的评审优先级与可跳过范围。

## 最可能失效的地方

1. **分析溯源的哈希链与逐结论核验**（`src/data_agent/provenance.py`、`src/data_agent/verification.py`、`src/data_agent/nodes/finalize.py`）。链路依赖 `json.dumps(..., sort_keys=True, separators=(",", ":"))` 的规范化结果；字段增删、编码变化或覆盖写语义调整都会让 `verify()` 的 `hash`/`prev` 校验失效。核验侧的分句绑定、单位换算（`%`、千/万/亿、元/万元/亿元）、小数位容差与「核验附录」切分，任何一处改动都会改变 `verified` / `conflict` / `unsupported` 的判定。
2. **评测裁决台账**（`src/data_agent/evalkit/ledger.py`、`src/data_agent/evalkit/harness.py`）。追加记录前必须整链校验，既有台账损坏时应拒绝追加而不是续写；McNemar 精确检验、成本与延迟护栏、接受/拒绝/证据不足三态裁决的边界条件都容易在重构中被绕过。
3. **只读 SQL 护栏**（`src/data_agent/sql_guard.py`、`src/data_agent/sqlite_source.py`、`src/data_agent/postgres_source.py`）。语句白名单只看首词，分号扫描需要正确处理字符串字面量与转义；SQLite 的 `mode=ro` 与 progress handler、Postgres 在 `autocommit` 下执行 `SET default_transaction_read_only = on` 与 `SET statement_timeout` 并在超时后 `rollback()`，必须确认真正在驱动或服务端生效，而不是只存在于客户端。
4. **跨源合并护栏**（`src/data_agent/tools/_join.py`）。键类型不一致、键含空值、多对多扇出都不会抛异常，只会得到「看起来正常」的错误结论；`MAX_ROW_MULTIPLIER`、未匹配键比例告警、`how` 与告警文案的对应关系，以及合并后行数基线的重置都要一起检查。
5. **沙箱隔离**（`src/data_agent/tools/_sandbox.py`、`src/data_agent/sandbox_runtime.py`、`src/data_agent/sandbox_worker.py`）。AST 审查依赖下划线属性、文件 I/O 方法黑名单与受限 builtins；子进程的 15 秒超时、RSS 熔断与输出截断是最后一道边界。Python 版本升级引入的新语法或新内建可能绕过静态审查。
6. **访问控制与路径边界**（`src/data_agent/middleware.py`、`src/data_agent/api.py`、`src/data_agent/workspace.py`、`src/data_agent/storage.py`）。`X-Forwarded-For` 的信任跳数、速率桶上限、非 ASCII 令牌头比较、上传文件名清洗、产物必须落在 `artifacts/` 内的约束、S3 归档解压的路径穿越检查、SPA 静态资源的符号链接处理，任一处写松都会扩大暴露面。

## 重点看

- `src/data_agent/provenance.py`、`src/data_agent/verification.py`、`src/data_agent/nodes/finalize.py`、`src/data_agent/evalkit/ledger.py`
- `src/data_agent/sql_guard.py`、`src/data_agent/sqlite_source.py`、`src/data_agent/postgres_source.py`
- `src/data_agent/tools/_sandbox.py`、`src/data_agent/sandbox_runtime.py`、`src/data_agent/sandbox_worker.py`
- `src/data_agent/tools/_join.py`、`src/data_agent/tools/_cleaning.py`、`src/data_agent/workspace.py`
- `src/data_agent/middleware.py`、`src/data_agent/api.py`、`src/data_agent/storage.py`

## 可以跳过

- `frontend/package-lock.json`：依赖锁定文件，仅在依赖变更时核对版本与审计结果。
- `docs/screenshots/*.png`：可由 `python scripts/capture_docs_screenshots.py` 重新抓取；仅当界面改动时才需要逐张比对。
- 本地未入库目录：`.venv/`、`runs/`、`build/`、`frontend/dist/`、`frontend/node_modules/`、`.pw-browsers/`。它们不进入 diff，无需评审。

## 本地验证

```powershell
.\.venv\Scripts\python.exe -m pytest -n 4
.\.venv\Scripts\python.exe -m ruff check .
cd frontend; npm run typecheck; npm test; npm run build
```

CI 在 `push` 到 `main` 与所有 PR 上执行 `.github/workflows/ci.yml`：后端作业覆盖 Python 3.11/3.12/3.13、`ruff check .`、pytest 与 `--cov-fail-under=97`；前端作业覆盖类型检查、单元测试、生产构建与依赖审计。提交前请按同一组命令自查。

## 评审规则

- 每条发现必须带 file:line 证据，不接受凭命名或猜测。
- 标严重度：Important / Nit / Pre-existing；区分本次引入与既有问题。
- 先报真实 bug，再报可读性；最多 3 条 nit。
- 已提过的发现不重复；第 3 轮起只审新增 diff，并请作者重新考虑方案。
- 每条发现必须被处理：修复，或在回复中说明为何不修。
- 安全类发现未解决前不得合并。

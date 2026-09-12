# 工程笔记

本文件沉淀本项目开发与维护中已验证的工程事实，供后续改动复用。

## 运行与重启

- 本地 API（uvicorn, 127.0.0.1:8000）以 supervisor 方式托管，不是 `run.ps1` 一次性拉起。杀掉 uvicorn 子进程会被自动重启。
- uvicorn 启动参数**无 `--reload`**。要让后端代码生效：① 清 `src/data_agent/**/__pycache__/*.pyc`（陈旧字节码）；② 用 PowerShell `Stop-Process -Force` 杀 uvicorn 进程；③ supervisor 自动用新源码重启。
- 前端走 Vite（5173）HMR；改动后建议硬刷新（Ctrl+Shift+R）确保拿到最新 bundle。

## 测试

- 跑测试：`.venv/Scripts/python.exe -m pytest tests/ -q`。
- Python `urllib` 取响应头用 `r.getheader('X')`；`dict(r.headers)` 在本机环境会漏掉 ETag/Content-Type 等头，曾导致误判。

## 鉴权

- `/preview` 等需鉴权端点：token 在 `.env` 的 `APP_ACCESS_TOKEN`，请求头 `X-App-Token`。

## 图表预览链路

- 图表为 ECharts/Plotly 内联 HTML，存于 `runs/<session>/artifacts/*.html`，由后端 `/preview` 注入 CSP + 内联 echarts bundle 后返回。
- 编码：写出层 `_atomic_write_text` 默认 UTF-8；CSV 读取候选 `("utf-8-sig", "utf-8", "gb18030")` 顺序探测。
- 乱码历史根因是前端 LRU 缓存（`useArtifactPreview` 的 `previewCacheRef`）按 `preview_url` 复用旧 HTML；已改为带 `If-None-Match` 的 ETag 条件请求（后端 `_preview_etag` + `_read_utf8_robust`）。

## 批量改生成类 HTML 的必踩坑（复用）

- **绝不用 `re.sub(r'<script>(.*?applyTheme.*?)</script>', NEW, flags=re.DOTALL)` 这类跨标签替换**：`re.DOTALL` 下 `.*?` 会跨过主脚本的 `</script>` 边界，把含 `echarts.init`/`var option` 的图表初始化主脚本整段删掉，导致文件损坏（图表白屏）。
- 正确做法：用负向前瞻阻止跨标签，如 `<script>(?:(?!</script>).)*?锚点(?:(?!</script>).)*?</script>`；或按脚本唯一锚点精确替换。改完务必校验 `echarts.init` 仍存在。
- **损坏恢复**：每个图表 HTML 都有 sibling `*.echarts.json`（完整 option）。可从它 + `<title>` + `.interpretation` 块（反转义）用 `echarts_engine._build_echarts_html` 重建。
- **ECharts formatter 需真实 JS 函数**：Python 端 `json.dumps` 会把函数源码序列化成带引号的字符串，前端 ECharts 当普通字符串标签渲染 → 用 `_JsFunction` 标记类 + `_serialize_option()` 在序列化后还原为无引号 JS 函数字面量。
- **图表 HTML 暗色切换**：由 `#theme-toggle` 按钮 + `_ECHARTS_DARK_MODE_SCRIPT`（`getIsDark`/`applyTheme`）实现；父页面 `PreviewModal` 在 iframe onLoad 注入应用主题。沙箱 iframe 无 `allow-same-origin`，`localStorage` 必须 try/catch。

## 自动选图（chart_type="auto"）

- 推断规则：时间序列（datetime/可解析日期串）→ line；两数值 → scatter；分类+数值 → bar（无 color 且类别≤8 → pie 构成占比，否则 bar）；单数值分布 → histogram；仅分类 → bar(count)；path_columns → sunburst；无 x/y 但 ≥3 数值列 → correlation_heatmap；dimensions≥3 → scatter_matrix；z → scatter_3d。
- 正确性兜底：auto 选 bar/pie 且 x 含重复行时，bar 走 `aggregation="sum"`，pie 先 `groupby(x).sum()`，避免重复类别错乱。

## 交互 UI 规范

- 交互类 UI 优先用 ReactBits 组件（`frontend/src/components/rb/`，已集成：Aurora/ClickSpark/CountUp/DotField/GlareHover/GradientText/Reveal/RotatingText/ShinyText/SplitText/SpotlightCard/StarBorder）。
- `GlareHover` 默认 `overflow:hidden` 会裁切子按钮的 translateX/box-shadow，侧栏场景必须放开并给流光层补 border-radius。
- 移动端侧边栏抽屉依赖 `.sidebar.is-open` 类；触控关闭用 onTouchStart/onTouchEnd 判断横向滑动。

## 常见问题

- `vite build` 首次会因清理 dist 失败：先 `rm -rf dist` 再 build。
- 图表 iframe 主题联动用 postMessage 双向桥接（`useArtifactPreview` 的 `withThemeBridge`），避免 sandbox 跨域死代码。
- 隐藏无图表的引擎筛选标签（`ArtifactCenter.tsx`）：仅渲染 `enginesPresent`，筛选后无图显示提示而非静默空白。

## 新增受控工具需要同步的四处

加一个工具不是改 `builder.py` 一处就完事，漏掉任何一处都会出问题：

1. `tools/builder.py` 的 `build_tools`：定义闭包工具 + 加进返回列表（列表顺序即模型看到的顺序）。
2. `tests/test_agent.py::test_deepseek_provider_*`：对工具名做**精确集合断言**，必须同步加入新名字。
3. `README.md` 工具数量与清单（"受控数据工具集（N 个内置工具）"）。
4. `prompts.py` 的系统提示词：不写引导模型就不会用新工具（会退回去手写 `run_python_code`）。
5. 若新工具产生跨模块辅助函数，按现有约定放 `tools/_<name>.py`（纯函数）并在 `tools/__init__.py` 里 re-export。

## 行数基线（source_row_count）与快照

- `source_row_count` 是 `clean_data` 20% 安全下限的参照，只在 `load()` 时锚定原始上传行数。
- 跨源合并会显著改变行数规模，所以 `join_datasets` 在护栏通过后调用 `adopt_dataset(reset_source_baseline=True)` 重置基线。**不重置会出现真实障碍**：主数据 10 行、合并后剩 1 行时，下限按旧基数算是 2 行，合并结果连去重都会被拒绝。
- 因此快照必须覆盖基线：`snapshot_state()` 返回 `WorkspaceSnapshot(dataframe, files, version, source_row_count)`，`restore_state()` 无条件还原基线。只恢复 DataFrame 会让"合并步骤失败回滚"后基线仍停留在合并后的规模，护栏在同一会话内失效。
- 改这个元组结构要同步 `tests/test_workspace.py::test_snapshot_state_handles_missing_artifacts_dir` 的解包。

## 合并护栏的设计取向：不提供绕过参数

`tools/_join.py` 的护栏全部由模块常量固定（`MAX_ROW_MULTIPLIER`、`UNMATCHED_WARN_RATIO`），没有"放宽阈值"的入参，与 `clean_data` 的护栏同策略。理由是这些护栏拦的是**不抛异常的静默错误**：

- 键含空值 → 空值与空值不相等，这些行静默丢失；
- 键类型不一致（`"1001"` vs `1001`）→ 一行都匹配不上，产出空表；
- 键不唯一 → 笛卡尔放大，聚合指标随分母一起虚增数倍。

注意 pandas 对"数值键与文本键合并"会自己抛英文错误并建议改用 `concat`，那不是用户想做的事；`merge_datasets` 捕获后换成"先 repair_data_format 统一格式"的中文引导。indicator 列名也要探测：源表自带 `_merge` 列时 pandas 会直接报错。

## 指标口径登记（metrics.json）

- 加载优先级：`DATA_AGENT_METRICS_PATH` 指向的全局文件 > 工作区根目录的 `metrics.json`；两者都没有则返回空列表，规划提示词退化成原来那样（`{metric_context}` 渲染为空串）。
- 定义文件写坏时**直接报错**，不降级为忽略：静默跳过用户登记的口径会让分析用错定义却看起来一切正常，比失败代价大。错误消息带文件路径与 1 起算的条目序号，便于直接改 JSON。
- 选择策略：与问题匹配的条目优先，其余按文件原顺序补足到 6 条。不能只注入命中项——像"分析这份销售数据"这种不点名指标的问题会完全看不到定义，那就失去了语义层的意义。

## SQLite 数据源（把 .db 当作一种数据格式，而不是新接口）

- **设计取舍**：会话由"上传一个文件 → `load()` 产出表格"创建，没有"给已有会话挂旁路文件"的接口。所以数据库文件不是新端点也不是新界面，而是直接进 `SUPPORTED_EXTENSIONS`，由 `_read_table` 分派到 `sqlite_source.read_default_table()`：**默认载入行数最多的表**，并把这个选择写进 `load_warnings`（否则用户会以为载入的是别的主表）；其余表用 `query_database` 查询。这样零前端改动、零新依赖（stdlib `sqlite3`）。
- **只读是三层叠加的**：`mode=ro` 的 URI 连接（驱动层拒绝写入，绕过语句白名单也写不进去）+ 语句前缀白名单（只放行 `SELECT`/`WITH`，`PRAGMA`/`ATTACH`/DDL/DML 逐个给针对性说明）+ 结果行列上限。
- **超时必须用 progress handler**：SQLite 没有语句级超时，`connect(timeout=)` 只管锁等待。一张跨表笛卡尔积会一直跑下去把分析线程占死，所以 `_query_deadline` 用 `set_progress_handler` 每 1000 个虚拟指令检查墙钟预算，超时返回非零使查询中断（`OperationalError: interrupted`），再翻译成可执行的中文提示。`count_rows` 也复用它，超预算返回 -1（"看一眼结构"不该变成长任务）。
- **表名必须先收敛再拼接**：`PRAGMA table_info(<name>)` 的表名无法用占位符绑定，只能拼字符串，所以 `describe_table` 先把入参比对真实表列表，杜绝注入。
- ⚠️ **与图表标识符护栏的相互作用**：把 `GROUP BY` 结果 `adopt` 成数据集后，分组列在结果里是"每行一个取值"，会命中 `_looks_like_id_column`（命名像 ID 且唯一率 ≥ 0.5）而被图表工具拒绝。**这是既有护栏在正常工作，不要为了数据库功能去放宽它**；需要画图时取行级结果（保留类别的多行重复），或在 SQL 里把分组列改名成非 ID 风格。
- **前端扩展名清单必须与后端同步**：`App.tsx` 的 `accept` 与 `EmptyWorkspace.tsx` 的 `SUPPORTED_EXTENSIONS` 共同决定用户能选哪些文件。历史上它们只列了 7 种结构化格式，导致 README 宣称的 PDF/TXT/Word 在界面上根本选不到；现已与后端 13 种对齐。**改后端支持格式时，这两处一起改。**

## Postgres 数据源（postgres_source.py）

- **驱动是可选依赖**（`pip install ".[postgres]"`，dev 环境默认包含），模块内 try/except 导入；未安装时 `is_configured()` 为 False，`connect_readonly` 给出可操作的中文安装提示而不是裸 ImportError。**不要**把它挪进运行时依赖——只分析 CSV 的用户不该背一个编译版 libpq。
- **只读在服务端，不在客户端**：连接后先 `autocommit=True` 执行 `SET default_transaction_read_only = on` 和 `SET statement_timeout`，再回读 `SHOW transaction_read_only` 确认生效（没生效就报错断开）。两个 SET 必须在 autocommit 下执行，否则落在事务里、事务结束就被回滚，保护等于没设。
- **超时用服务端 `statement_timeout`**，与 SQLite 的 progress handler 是两套机制（SQLite 没有服务端超时）。Postgres 超时抛 `QueryCanceled`（SQLSTATE 57014），要 `rollback()` 清掉中止的事务再抛中文提示——只读事务里出错后不回滚，后续语句会继续报错。
- 连接串走 `DATA_AGENT_DATABASE_URL` 环境变量，与 S3/R2 凭证同一约定，**不要**再引入 keyring（无头服务器没有后端，会失败）。
- CI 用 GitHub Actions 的 **service container**（postgres:16 + 健康检查）跑真实集成测试；测试在连不上时**自动跳过而不是失败**，本地无 Docker 的环境不受影响。判断可用性要在 fixture 里真实连接一次，只看环境变量是不够的。
- ⚠️ 测试要防 xdist 并发互踩：每个用例用唯一表名（`da_test_<uuid>`）并 try/finally 清理；建表/删表走独立的**可写**管理连接，被测的只读连接只负责查。

## MCP 数据面服务（mcp_server.py）

- **只开数据面，不开分析流程**：暴露 list_sessions / open_dataset / inspect_data / sql_query 四个工具。完整分析流程是分钟级 LLM 长任务，塞进 MCP 工具调用等于多一个有状态长任务入口，两条状态机没有收益——它走 HTTP API。开放面做粗是刻意的：给其他 Agent 的是"受控的数据访问"，不是内部工具原样透出。
- **独立进程，不依附 FastAPI**：会话以 `runs/<id>/session.json` 落盘自描述，MCP 服务按同一 `DATA_AGENT_RUNS_DIR` 自建 `SessionRegistry` 即可读到 HTTP 服务创建的会话。不要 import `data_agent.api` 的单例——那会拖起整个应用装配。
- **注册表的 `get()` 抛的是 fastapi 的 HTTPException**：MCP 工具层要把它转成 `ValueError`（带"用 list_sessions 查看"的指引），否则客户端看到的是 HTTP 语义的异常。
- **测试走官方 SDK 的内存客户端**（`mcp.shared.memory.create_connected_server_and_client_session`），经真实协议（initialize/tools/list/tools/call）交互，任何io 运行时用 `anyio.run(scenario)` 包住即可，不需要 pytest-asyncio 插件。**必须断言错误契约**：工具抛异常要以 `isError=True` + 中文原因返回、且协议流不中断（出错后还能继续 list_tools）——这是 MCP 客户端的实际依赖行为。
- MCP SDK 也是可选依赖（`extras mcp`，dev 默认包含）；未安装时 `create_server` 给中文提示。
- **边界**：stdio 传输由本地受信客户端拉起，天然无网络暴露面；不要把它挂到网上——远程场景走带令牌与限流的 HTTP API。

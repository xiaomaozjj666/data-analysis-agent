# Changelog

本项目的所有显著变更都记录在本文件中。
格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循[语义化版本](https://semver.org/lang/zh-CN/)。

项目自 2026-07 起以 trunk 方式开发（`main` 即最新可用版），本文件从 v1.0.0
起维护；更早的历史按月份归档在版本条目里。

## [2026-11-19]

### 新增

- **经营简报核验升级到 `schema_version=2`**：源文件只读一次字节快照，SHA-256、
  编码识别与 CSV 解析基于同一份 bytes，消除读两遍的 TOCTOU；新增范围门——
  登记指标必须整行精确匹配，「华东区订单数」「平均订单数」「订单数（华东区）」
  一律判为 `unsupported`，即使 `confirm_full_table=true` 也不放行；订单号近似
  重复（NFKC、大小写、零宽字符、前导 0）只写入 `warnings` 并给相关指标附
  `caveat`，不改变订单数口径；表头接受 订单号/金额/数量 等中文别名，仅唯一
  匹配时接受，歧义直接报错；新增「客单价」口径（销售额合计 ÷ 订单数，按分
  HALF_UP）；未登记指标按单位给出口径候选 `candidates`。`audit_id` 为源文件
  哈希、草稿、确认标志与复算结果的确定性哈希。
- **服务端确认门**：新增 `POST /api/sessions/{id}/sales-audit/confirm`，服务端
  按原始 CSV 重新复算，重算的 `audit_id` 与提交值不一致返回 409；通过后写
  `confirm` 记录并返回权威简报与回执（`seq` / `ts` / `hash` / `prev` /
  `brief_sha256`）。
- **核验账本与离线校验**：`runs/<会话>/audit_ledger.jsonl` 记录每次 audit 与
  confirm，逐条 `prev` / `hash` 串联，追加前先校验整链；新增
  `GET /api/sessions/{id}/sales-audit/ledger` 与
  `scripts/verify_audit_ledger.py`（校验链条；`--brief` 文件改 1 个字节即
  退出码 1；`--source` 校验原始 CSV；`--trace` 校验快速起草轨迹）。
- **快速起草对照入口与轨迹绑定**：新增
  `POST /api/sessions/{id}/sales-audit/quick-draft`，不调用工具、关闭思考，直接
  让模型读表写三行指标；返回模型名、耗时、脱敏、丢弃行数与轨迹哈希。提示词不
  提供去重口径或公式，仅作为核验器的对照输入。轨迹写入
  `draft_traces/<sha256>.json`，复算/确认接口可通过 `draft_trace_sha256` 校验
  「模型原话 → 过滤后的草稿」逐字对应并绑定进 `audit_id` 与账本。
- **Render 保活脚本**：新增 `scripts/keepalive.ps1`，按间隔 ping 健康检查地址
  并打印状态码与耗时，失败后继续下一轮。

### 变更

- README 与 `docs/wuxi-trusted-ai-challenge.md` 补充范围门、`audit_id`、服务端
  确认门、账本与离线校验命令、快速起草入口，并新增 90 秒路演脚本；测试徽章按
  实测收集数（约 1650 项）更新为保守下限 1600+。
- `render.yaml` 注释补充 Free 档 15 分钟休眠与 35–90 秒冷启动说明，以及
  `healthCheckPath` 在冷启动期间可能失败；未改动 region / disk 等不可变字段。

### 修复

- **kaleido 1.5.1 PNG 导出竞态**：包装 `_KaleidoTab.navigate`，在原导航完成后
  以 0.1 秒间隔轮询 `kaleido_scopes` 就绪，上限 10 秒；超时不引入新异常，只记
  日志并按原流程继续。仅当版本与内部结构均符合预期时才打补丁。环境缺失
  （无 Chrome / 无 kaleido）仍返回 503 与可操作提示。
- **邮箱脱敏正则回溯**：原 `[A-Z0-9._%+-]+@...` 对不含 `@` 的长串会按起点反复
  扫到串尾，耗时随长度二次方增长；改为占有量词的分支扫描，总耗时线性，命中范围
  与原正则保持一致。
- **`run.ps1` 环境自检加固**：按 3.13 / 3.12 / 3.11 探测受支持的 Python、校验
  Node.js ≥ 22.12、端口可用性改用实际 bind 判定、缺少 `.env` 时给出创建指引。

## [Unreleased]

### 新增

- **邮件与短信告警通道**：任务定义可配 `email_to`（最多 10 个）与 `sms_to`
  （E.164，最多 5 个）；邮件走 SMTP（`DATA_AGENT_SMTP_URL`，支持
  `smtp://`/`smtps://`、STARTTLS，发件人可用 `DATA_AGENT_SMTP_FROM` 覆盖），
  短信走 Twilio（`DATA_AGENT_TWILIO_ACCOUNT_SID` / `_AUTH_TOKEN` / `_FROM`）。
  凭据只从环境变量读取、错误文本脱敏；发送失败记录
  `last_email_error` / `last_sms_error` 与状态码，不影响任务状态回滚。
  本地假 SMTP 服务器端到端用例覆盖成功与配置错误分支。
- **技能包安装与清单**：`skill.py` 新增 `install`（项目级 `.data-agent/skills`
  或全局级 `~/.data-agent/skills`，`DATA_AGENT_SKILLS_DIR` 可覆盖）与 `list`。
  安装按不可信输入处理：只复制三个约定文件、拒绝符号链接、slug 安全字符集、
  校验 `recipe_sha256`，覆盖需 `--force` 且失败回滚旧版本；`list` 对坏包逐项
  标记 `valid/error`。见 `docs/skills.md`。
- **定时任务异常归因摘要**：告警新增阈值归一化的 `severity` 与 `direction`，
  `run`/`run-due` 结果、`alerts.jsonl` 与 webhook 增加 `attribution`
  （最大偏离指标、方向、`same_direction` 同向判断、Top-10 排名），
  `state.json` 记录 `last_alert_headline`。归因只到统计层，不推导业务原因。
- **任务巡检视图 `automation status`**：只读汇总任务的上次运行、到期状态、
  下次预计到期、最近归因与错误，`--fail-on-alert` 在到期、失败或有告警时
  退出码为 1，供看板/监控调用；不触发任何执行、不写状态。
- **群机器人推送适配**：webhook 目标为 Slack / 飞书（Lark）/ 钉钉时自动
  转换为对应文本格式，其他地址保持原始结构化 JSON；完整明细仍写
  `alerts.jsonl`。
- **静态 HTML 看板**：`automation dashboard` 把巡检视图渲染成单文件 HTML
  （无外部资源、CSP 限制、全部字段 HTML 转义，`--refresh-seconds` 支持
  meta refresh），可挂到任意静态目录。
- **会话评论（协作层）**：新增 `GET/POST/DELETE /api/sessions/{id}/comments`
  与工作台「评论」Tab（快捷键 `5`）。评论存 `comments.json`（原子写、200 条 /
  2000 字上限、纯文本渲染防 XSS），随会话导出 ZIP 归档。
- **只读令牌与审计日志**：新增 `APP_VIEWER_TOKEN`（仅 GET/HEAD，写请求 403；
  只配置它时服务进入只读模式）；写操作与 401/403 可追加 JSONL 审计
  （`DATA_AGENT_AUDIT_LOG`，不含请求体/令牌/IP，写失败不影响请求）。
- **联网搜索工具 `web_search`**：固定 DuckDuckGo HTML 出口、重定向主机
  白名单、结果按不可信外部数据标注；工具集由 11 个增至 12 个。真实网络
  冒烟验证可解析出结果。
- **通用 DB-API 外部数据源**：`query_database`/MCP 新增 `source="external"`，
  通过 `DATA_AGENT_EXTERNAL_DB_MODULE` + `_KWARGS` 接入 Snowflake /
  Databricks / BigQuery DB-API；模块白名单、SQL 护栏、`role-required` 能力
  声明；离线用 sqlite3 验证连接/查询/降级，真实云连接属部署方验收项。
- **报告级叙事编排**：finalize 提示词（中英）新增论证链要求——全局结论 →
  驱动拆解 → 效率/对比 → 异常/风险 → 行动，每张图必须说明支撑哪条判断。
- **经营简报核验（Sales Audit）**：新增 `POST /api/sessions/{id}/sales-audit`
  与「简报核验」工作区 Tab。只读原始上传 CSV，按 `SUM(sales)` / `SUM(quantity)`
  / `COUNT(DISTINCT order_id)` 复算，把草稿里的指标行逐条判为
  verified / conflict / unsupported；重复指标、代码块与缩进示例不参与发布，
  草稿含上下文时要求确认「全表范围」，确认后生成核验版简报。金额用 Decimal
  精确到分、行数上限复用上传时 `max_rows`、读前读后 SHA-256 校验。含路演样例
  （同一订单两条明细）与 `docs/wuxi-trusted-ai-challenge.md`。
- **设置面板重构与连接测试**：设置面板改为原生 `<dialog>`（Esc/遮罩关闭、
  焦点管理）；新增 `POST /api/settings/test` 只做连通性探测（不落盘、不回显
  Key、错误分类 401/429/504/502）；`GET /api/settings` 细分 LangSmith 状态
  （configured / missing_key / key_unconfirmed / local / disabled）；`httpx`
  提升为运行时依赖。前端新增「简报核验」Tab 与 `4` 快捷键。
- **数据源注册表 `data_sources.py`**：统一 session/postgres 的标签、只读保证
  等级（driver / server-confirmed）、超时机制与配置状态；`query_database` 与
  MCP `sql_query` 的发现结果新增 `sources`（含能力声明），并把此前只在 MCP
  出现的 `available_sources` 补齐到工具侧。外部源分派收敛到
  `open_external()`，新增驱动只需注册一次。
- **方言感知 SQL 护栏**：多语句扫描支持注释、反引号标识符（BigQuery /
  Databricks / MySQL）与美元引号字符串（Postgres），修复「注释里的分号被
  误判为多语句」与「反引号内分号被漏判」；禁用语句表扩充 MERGE / CALL /
  EXPORT / COPY / UNLOAD / GRANT / SET / USE 等数仓写操作；
  `quote_identifier(name, dialect=...)` 支持反引号方言（默认行为不变）。
- **服务端 PDF 导出**：`build_pdf` 从同一份结构化报告生成 PDF（reportlab +
  内置 CID 中文字体 `STSong-Light`，无外部字体依赖），新增
  `GET /api/sessions/{id}/report.pdf` 与前端导出菜单「PDF (.pdf)」。运行时
  依赖 reportlab（开发/CI 由 dev extras 提供；缺失时接口返回 503 并提示安装）。
- **定时分析任务 `automation.py`（最小闭环）**：按 `interval_minutes` 或本地
  `daily_at HH:MM` 调度；加载数据 → 与 `compute_registered_metrics` 同源地调用 `metrics.compute_metrics_on_frame` →
  历史窗口上的 z-score / 环比异常检测 → `alerts.jsonl` + 可选 webhook POST。
  `python -m data_agent.automation` 提供 add/list/remove/run/run-due/loop；
  样本不足（默认 <3）不下结论，单任务失败不阻断批次，历史每行带指标定义哈希。
  见 `docs/automation.md`。webhook 带 SSRF 防护：仅 http/https、禁止内嵌账号密码、
  IP/域名解析命中内网/环回/链路本地/保留地址时拒绝发送（配置与发送两阶段校验）。
- **分析技能包导出 `skill.py`**：把 `analysis_recipe.json` 导出为
  `SKILL.md + analysis_recipe.json + skill.json` 技能包；`run --bundle --data`
  按配方确定性重跑（不调用 LLM）。覆盖已有目录需显式 `--force`。见 `docs/skills.md`。
- **指标定义文件纳入溯源链**：`record_analysis` 新增 `metrics_path`，把本次生效的
  `metrics.json` 哈希写入链并参与 `verify()`；文件缺失时不写空条目，链长度不变。
- **指标定义支持「已验证问法」`examples`**：`metrics.json` 的每条指标可登记
  业务确认过的问题样例；规划阶段随口径一起注入提示词，让模型把口语问题对齐到
  已登记的标准指标，而不是另起同名口径（对标 Power BI verified answers 的人工
  策展思路，来源见 `docs/competitive-analysis.md` 第六节）。解析走既有严格校验
  （非数组/空白项处理与 aliases 一致），序列化可往返，渲染最多列 3 条并截断。
- **竞品实地复核（2026-11-17）**：用 web-crawler 对六类竞品的官方一手页面实地
  抓取与逐句核对（OpenAI 帮助中心经无头浏览器渲染取得全文、Julius 经 TLS 隐身
  取得连接器与代码可见性原文、WorkBuddy 渲染后取得 JSON-LD 能力清单、FineBI
  全文实取；Tableau 营销站三层全 403，但帮助门户经渲染拿到官方 llms-full.txt
  6.1MB 全量文档 + tableau.github.io/tableau-mcp 官方文档，Hosted MCP/OAuth 2.1
  获一手确认；Julius 的 docs.julius.ai 子域 526，改走 julius.ai/docs 渲染入口 +
  /docs/llms.txt 全量目录取回三页官方原文）。`docs/competitive-analysis.md`
  新增第六节：抓取矩阵、逐家复核结论（原文/推断分标）、路线图增量影响——定时
  调度证据变硬（WorkBuddy/Trae/Pulse 三家一手；Julius 改判为单一二手来源）、语义层被微软官方前置条件
  背书、技能层有了 Trae SKILL.md 完整设计可对标、FineBI Data Agent 归属改判、
  Claude for Excel 按无可证不收录。过期数字（文件数 39→88、测试 733→1114、
  工具数口径）以"2026-11 复核注"原位标注；来源附录按初查/复核分组。
- **发布前评测闸门 `data-agent-eval`**：读取同一套用例的基线与候选结果（JSONL /
  JSON 数组），做配对 McNemar 精确检验、Wilson 区间与成本/延迟护栏，输出
  `accept` / `reject` / `inconclusive` 三态裁决并以 `0/1/2` 退出码接入 CI；裁决可
  追加进带 `prev`/`hash` 链的 ledger，`verify-ledger` 可检测篡改。示例与说明见
  `eval/README.md`、`docs/agent-eval.md`。
- **分析溯源哈希链**：分析完成时写 `provenance.jsonl` / `provenance.json`，覆盖
  任务元信息、数据集、逐条工具调用、产物与报告正文；`GET /api/sessions/{id}/provenance`
  当场复核，`/provenance/export` 下载自包含 JSON，前端导出菜单同步增加入口。
  溯源失败只记日志，不影响已完成的分析。
- **逐结论确定性核验**：finalize / replay 在事实附录后调用 `verify_report`，把报告
  正文中的数字与文本结论逐项判为 `verified` / `conflict` / `unsupported`（不调用
  第二个模型）：按数字所在分句绑定事实字段，原文小数位一半容差接受舍入，仅当事实
  也声明单位时才做 `%` / 千 / 万 / 亿与元系换算；同字段事实互相矛盾即判 conflict，
  无可绑定事实判 unsupported 且证据为空。报告追加「结论核验」表，证据带 trace 序号
  与 JSON Pointer 定位；结果原子写入 `artifacts/report-verification.json` 并注册为
  verification 产物，自动进入溯源链。事实提取（`extract_facts`）同步升级为携带单位
  与可回放定位信息。

### 变更

- 首次启动且未配置 API Key 时不再自动弹出设置面板，由用户从侧栏「分析引擎」
  主动打开（`useAuthBootstrap` 行为调整，相关冒烟测试已同步）。

### 修复

- **webhook DNS rebinding 窗口关闭**：发送改为"一次 getaddrinfo 同时完成
  校验与 IP 钉扎"——连接直连已校验 IP，TLS/SNI/Host 头仍用原域名；每一跳
  （含重定向）重走完整校验，公网地址 302 到内网/元数据地址在钉扎层被拒。
- **external DB-API 源补齐查询超时**：sqlite3 用 progress handler 执行中打断、
  psycopg 服务端 `statement_timeout`、Snowflake 会话超时之外，所有驱动再叠
  工作线程 join(timeout) 硬顶——DB-API 无法中止服务端语句时也保证调用方不被
  无限期阻塞；sqlite3 连接默认 `check_same_thread=False`。
- **邮件发件人归一化**：`DATA_AGENT_SMTP_URL` username 段可能带 %0d%0a，
  From/Subject 统一折叠空白，消息构造移入 try——配置手滑降级为发送错误记录，
  不再违反"不抛异常"契约。
- **`status_overview` 类型防御**：手改 state.json 的 `last_alerts` 为非数值
  时按 0 处理，不再拖垮整个看板/巡检。
- **会话导入孤儿目录**：上传超限（413）/成员过多/路径逃逸/坏 ZIP 路径统一
  清理未注册的 `runs/api_*` 目录，TTL 不再回收不到。
- **技能包重跑 fail-closed**：`run` 缺 `skill.json` 或缺 `recipe_sha256` 时
  拒绝执行（与 install/list 同口径），删清单绕过完整性校验的路径已封死。
- **唯一 tmp 后缀扫尾**：`_atomic_write_text`、评测结果、上传 `.part`、
  checkpoint parquet 全部改随机后缀，多进程/多线程写同一目标不再互相截断。
- **相关分析性能**：p 值由相关矩阵 r + 有效样本量按 t 分布导出（与
  scipy.pearsonr 数值一致），常数列判定改 O(n) 比较——200k 行 × 7 数值列
  0.472s → 0.025s（约 19×），回归测试逐对比对 scipy。
- **SQL 只读护栏第二轮加固**：反斜杠失步多语句（`SELECT 'x\'; DROP ...`）、
  数据修改 CTE（`WITH ... DELETE/INSERT/UPDATE`）、`SELECT ... INTO` /
  `INTO OUTFILE`、危险函数（`pg_read_file`、`pg_current_setting`、
  `pg_advisory_*`、`pg_terminate_backend`、`load_extension` 等）全部拦截；
  标准 SQL 与 MySQL 反斜杠语义、双写引号 `''` 双解释取并集（Postgres
  `E'...'` 无法绕过）；合法查询（`/* c */ SELECT 1`、`SELECT/**/1`）不再误杀。
- **定时任务可观测性**：坏 `state.json` 打 `state_corrupt` 标记并告警（不再
  静默按「从未运行」重跑）；坏历史行计数告警；`history/alerts` JSONL 超 2MB
  自动轮转；运行记录新增 `duration_ms`；webhook 记录 HTTP 状态码与耗时，
  4xx/5xx 经 `HTTPError` 分支单独取码（此前失败场景看不到状态码）。
- **销售核验支持 GB18030**：中文 Excel 常见的 GB18030 导出不再被拒；
  解码失败仍给出可操作提示（UTF-8/GB18030、逗号分隔）。
- **metrics.json 原子写**：`save_workspace_metrics` 改为 tmp + rename，
  替换失败时旧文件保持完整，不再出现半写的指标定义。
- **修复 main 不可导入**：`3a9aed8` 漏提交 `_AUDIT_SAMPLE_SALES_CSV`，
  `import data_agent.api` 直接 ImportError；已补常量并推送（`619b125`）。
- **沙箱子进程清理**：Windows 下 venv 启动器先退出、派生解释器仍持有临时目录；
  kill 前先收集子进程并在主进程退出后等待，减少清理失败与 ResourceWarning。
- **定时任务/技能包/PDF 的评审修复**：dry-run 不再发 webhook、不再创建持久
  工作区；webhook 逐跳校验重定向（公网 302 到内网被拦），登记阶段不做 DNS；
  `run-due` 对坏 `last_run_at` 降级、失败写 `next_retry_at`（5 分钟后重试），
  不再整批中止或每秒空转；同任务加跨进程锁；异常检测补零基线规则，全空指标
  记 `partial` 而不是当 0；历史只与相同 `metrics_sha256` 比较；PDF 超长图按
  A4 高度缩回；技能包 `--force` 先备份后替换、重跑校验配方哈希；分析开始时
  固定指标快照再写溯源。
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

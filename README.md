# Data Analysis Agent

<p align="center">
  <a href="https://github.com/xiaomaozjj666/data-analysis-agent/actions/workflows/ci.yml"><img src="https://github.com/xiaomaozjj666/data-analysis-agent/actions/workflows/ci.yml/badge.svg" alt="CI" /></a>
  <img src="https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-3776AB" alt="Python 3.11–3.13" />
  <img src="https://img.shields.io/badge/react-19-61DAFB" alt="React 19" />
  <img src="https://img.shields.io/badge/FastAPI-API-009688" alt="FastAPI" />
  <img src="https://img.shields.io/badge/LangGraph-agent-4D6BFE" alt="LangGraph" />
  <img src="https://img.shields.io/badge/Docker-ready-2496ED" alt="Docker" />
  <img src="https://img.shields.io/badge/tests-1681%2B%20backend%20%C2%B7%20193%2B%20frontend-brightgreen" alt="后端 1681+ 项、前端 193+ 项自动化测试" />
  <img src="https://img.shields.io/badge/license-MIT-blue" alt="MIT License" />
</p>

一个基于 LLM 的数据分析工作台：上传数据集，自动完成数据检查、清洗、统计分析、可视化与报告生成。

后端采用 LangChain + LangGraph 的 Plan-and-Execute + ReAct 混合架构，前端为独立 React 应用，本地通过 HTTP/SSE 通信。可一键本地运行，也可部署到 LangSmith / Render / Docker。

## 最快上手

```powershell
.\run.bat     # Windows 一键启动：首次自动建虚拟环境、装依赖，之后自动开浏览器
```

不想要界面：`data-agent analyze examples/sample_sales.csv --task "分析各区域销售趋势"`（需先在 `.env` 填 `DEEPSEEK_API_KEY`）。完整步骤、访问令牌与端口规则见 [快速开始](#快速开始)。

<p align="center">
  <img src="docs/screenshots/overview-light.png" alt="数据分析工作台主界面（浅色主题）" width="88%" />
  <br />
  <em>全流程工作台：数据概览 → 分析任务 → 实时执行进度 → 结构化报告</em>
</p>

## 系统架构

```mermaid
flowchart LR
    U[上传数据集] --> W[工作区 DataWorkspace<br/>数据档案 / 清洗 / 产物]
    W --> P[Plan-and-Execute 规划器<br/>生成 2–6 步结构化计划]
    P --> R[ReAct 执行器]
    R --> T[受控工具集]
    T --> T1[数据检查 / 格式修复]
    T --> T2[安全清洗 / 筛选视图]
    T --> T3[统计分析 · 假设检验]
    T --> T4[Plotly / ECharts 图表]
    T --> T5[导出 / Python 沙箱]
    R -->|重规划器<br/>裁剪与补充步骤| P
    R --> F[汇总 · 生成报告]
    F --> G[结构化分析报告]
    F --> C[交互式图表产物]
    G --> Q[多轮追问对话]
    C --> Q
```

## 界面预览

| 起始页 · 拖拽上传与示例数据 | 分析工作区 · 数据概览与任务 |
| :---: | :---: |
| <img src="docs/screenshots/start-light.png" alt="起始页" width="100%" /> | <img src="docs/screenshots/overview-light.png" alt="数据分析工作台主界面（浅色主题）" width="100%" /> |
| 深色主题 · 全流程工作台 | 产物中心 · 图表缩略图与下载 |
| <img src="docs/screenshots/overview-dark.png" alt="深色主题主界面" width="100%" /> | <img src="docs/screenshots/artifacts-light.png" alt="产物中心" width="100%" /> |
| 图表预览 · 密度视图与细节图层 | 数据预览 · 类型识别与列对齐 |
| <img src="docs/screenshots/preview-light.png" alt="图表预览模态" width="100%" /> | <img src="docs/screenshots/data-preview.png" alt="数据预览表格" width="100%" /> |

以上截图可用 `python scripts/capture_docs_screenshots.py` 一键重新生成（本地起服务后逐页抓图）。

## 功能特性

- **计划-执行式分析流程**：每次分析先生成 2–6 个结构化步骤，每个步骤由 ReAct 执行器调用受控工具完成；重规划器根据真实工具结果删除无用步骤、补充后续分析，证据充分时提前结束，达到步骤上限时强制汇总，避免无限循环。
- **受控数据工具集**（12 个内置工具）：数据检查、格式修复、安全清洗、非破坏性筛选视图、跨源合并、公开网页检索（`web_search`，固定出口、结果按不可信数据对待）、数据库查询、登记指标计算、统计分析、图表生成、数据导出、受限 Python 沙箱（可导入 scipy / scikit-learn / statsmodels，需安装 `[stats]` 可选依赖；文件与网络入口仍被拦截）。
- **MCP 数据面服务**：把会话发现、数据集载入、结构探查与只读 SQL 以 MCP 标准开放给其他 Agent，护栏同源、传输 stdio（详见"快速开始"）。
  - 跨源合并带连接键校验与行数膨胀护栏：键不存在、键类型不一致导致一行都匹配不上、键不唯一导致行数被放大数倍，三种情况一律拒绝执行并说明原因；结果同时给出未匹配键数量与一对多放大警告，避免把扇出后的行数当作原始记录数。合并结果会成为新的活动数据集，行数基线同步重置。
  - 数据库查询面向三类数据源：上传的 **SQLite** 文件、环境变量 `DATA_AGENT_DATABASE_URL` 配置的 **PostgreSQL**（驱动按需安装：`pip install ".[postgres]"`），以及 `DATA_AGENT_EXTERNAL_DB_MODULE` / `DATA_AGENT_EXTERNAL_DB_KWARGS` 配置的 **通用 DB-API 数仓**（Snowflake、Databricks、BigQuery DB-API 等，模块走白名单，只读保证等级为 `role-required`——SQL 护栏 + 账号/角色权限，项目不谎称服务端已确认）。**只读**（SQLite 以 `mode=ro` 打开、Postgres 在服务端设置 `default_transaction_read_only`，写入在驱动/服务端即被拒绝），只接受 `SELECT` / `WITH` 语句；单条查询默认 30 秒墙钟预算（`DATA_AGENT_SQL_TIMEOUT_SECONDS`，1–120；SQLite 用 progress handler 中断、Postgres 用服务端 `statement_timeout`），结果上限 5000 行、500 列。空 `sql` 调用可一次拿到可用数据源、各源的只读保证等级（`readonly_guarantee`：driver / server-confirmed / role-required）与全库表结构；传 `adopt=true` 可把查询结果接管为活动数据集，后续统计与图表直接在其上工作。
  - 清洗带安全护栏：缺失值删除比例超过 50% 会拒绝执行，主数据行数始终不低于原始行数的 20%。
  - 统计方法覆盖描述统计、相关分析（含 Pearson p 值）、分组聚合、独立/配对 t 检验、ANOVA、卡方检验、线性回归（R² / RMSE / MAE）。
- **指标口径登记（可选）**：在会话目录放置 `metrics.json`，或用 `DATA_AGENT_METRICS_PATH` 指定一份全局定义，登记指标的标准名称、别名、口径说明、算式与**已验证问法**（`examples`：业务确认过的问题样例，随口径一起注入，让模型把口语问题对齐到标准指标，对标 Power BI verified answers 的人工策展思路）；规划阶段会把相关定义注入提示词，避免同一指标在不同会话被各自定义为不同口径。未提供定义文件时该特性完全不生效，行为与原来一致。
- **跨会话记忆（claude-mem 式沉淀注入）**：`POST /api/memories` 把口径教训/用户偏好沉淀为显式条目（`memories.json` 原子写、上限 100 条、单条 ≤500 字、重复提交只加复用计数），规划阶段按复用次数注入最多 10 条，并明确标注「非当前用户指令，与当前任务冲突时以当前任务为准」——记忆可人工审阅/删除，不做不可审计的向量黑盒；未添加记忆时行为与原来完全一致。
- **定时分析任务与主动告警（最小闭环）**：`python -m data_agent.automation` 按间隔或每日 `HH:MM` 运行任务——加载数据、计算登记指标、用 z-score 与环比在历史窗口上做确定性异常检测，按阈值归一化严重度排序并生成 `attribution` 归因摘要（最大偏离指标、方向、是否同向），告警先落 `alerts.jsonl` 再按需推送（webhook 支持 Slack / 飞书 / 钉钉自动转文本格式，其他地址发原始 JSON；任务可配 `email_to` 经 SMTP 发邮件、`sms_to` 经 Twilio 发短信，凭据全部走环境变量）；支持 `add`（创建/更新任务）、`run-due`（供 cron / Windows 任务计划调用）、`run`、`list`、`remove`、`status`（巡检视图，`--fail-on-alert` 供监控）与前台 `loop`。样本不足（默认 <3）不下结论，单任务失败不阻断批次，历史每行带指标定义哈希。见 [docs/automation.md](docs/automation.md)。
- **分析技能包导出与安装**：把会话的 `analysis_recipe.json` 导出为 `SKILL.md + analysis_recipe.json + skill.json` 技能包（对标 Trae SKILL.md 设计与 Agent Skills 规范 front-matter），可分发、可版本化；`python -m data_agent.skill run --bundle … --data …` 按配方确定性重跑，不调用 LLM；`install --scope project|global` 安装到 `.data-agent/skills` 或 `~/.data-agent/skills`（校验配方哈希、拒绝符号链接与越界 slug，**安装前强制静态安全扫描**——对标 SkillSpector 评分口径，HIGH/CRITICAL 默认拒绝、`--accept-risk` 显式越过），`list` 输出带完整性状态的清单，`scan` 可独立扫描任意技能包。覆盖已有目录/技能需显式 `--force`。见 [docs/skills.md](docs/skills.md)。
- **会话评论（协作层）**：每个会话带「评论」Tab，队友可留下口径疑问/复核意见；评论存 `comments.json`（原子写、单会话 200 条上限、纯文本渲染防 XSS），随导出 ZIP 一起归档。见 [docs/collaboration.md](docs/collaboration.md)。
- **token 用量遥测**：会话级累计器覆盖 plan/replan/execute/finalize/chat 全部 LLM 调用，报告头部 chip 展示输入/输出/总计，溯源链新增 `usage` 条目（成本可审计）；`run()`/`stream()` 入口重置避免复跑串账，`chat_done` 事件为单次追问增量。
- **双图表引擎**（Plotly + ECharts）：共享同一套数据准备逻辑，支持折线、柱状、面积图、散点（含 3D）、直方图、箱线图、小提琴图、饼图、热力图、相关热力图、散点矩阵、旭日图、矩形树图；自动选图、散点图自动叠加 OLS 趋势线（标注 r 值）与双轴均值象限参考线、极端值自动检测与主体尺度/全量视图切换、暗色主题联动、PNG 导出、数据驱动的白话解读。
- **大数据图表的可读性**：几十万行时"每点一个标记"的散点会因 alpha 饱和糊成一团（实测 30 万点全是同色噪点）。**散点 ≥ 2 万行自动切换为密度视图**：服务端聚合成网格（单格约 7px）、用分档颜色表达每格记录数（档位图例直接给出"1 / 2–4 / 5–9 …"的记录数区间）、零记录格完全透明，并在有分组时按分组拆成共享色标的分面面板（类别用分面表达、颜色只表达密度）；无分组时补顶部/右侧边缘直方图（jointplot 布局）、趋势线、均值参考线与最外围原始点。判定"是否真有聚集"会先扣掉均匀随机点云的统计涨落，没有聚集就直说。**放大后想看具体记录**：图上带一层默认隐藏的分层抽样原始点，Plotly 用右上角按钮、ECharts 用图例开关一键叠加（名字与解读都写明是抽样、条数，完整数据在同名 JSON）。直方图 ≥ 5 万行改为服务端分箱（纵轴仍是真实记录数）、箱线图改为服务端五数概括、小提琴图与 3D 散点分层抽样（3D 会把"已抽样 N/M 点"写在副标题上）——**同一批图表 HTML 体积从 3~12MB 降到 20~200KB**，密度视图是精确聚合而非抽样，完整数据始终保留在同名 JSON 产物里。折线/面积按 **LTTB（Largest-Triangle-Three-Buckets）保峰降采样**（数值轴与类目轴都覆盖），短促尖峰不会被等距抽样抹掉。
- **可访问性**：全站文字对比度按 WCAG AA 校准并用浏览器计算样式实测（浅/暗两主题 × 起始页/工作台/产物/历史/命令面板/预览模态），三级文字阶梯与"实心强调色"分令牌管理（白字压底专用 `--accent-solid`，避免暗色下提亮的品牌色当背景时只有 3.5:1）；滑块、按钮、卡片均带 hover/active 反馈与键盘焦点环。
- **Word / PPT / PDF 原生导出**：`report.docx` / `report.pptx` / `report.pdf` 三个接口从同一份结构化 Markdown 报告生成；PDF 用 reportlab（内置 CID 中文字体，无外部字体依赖），前端导出菜单同步新增「PDF (.pdf)」。PDF 运行时需 `pip install reportlab`（开发/CI 环境已由 dev extras 安装），缺失时接口返回 503 并给出安装提示。
- **多格式数据接入**：CSV / TSV / Excel / JSON / JSONL / Parquet、SQLite（`.db` / `.sqlite` / `.sqlite3`，上传后默认载入行数最多的表，其余表由数据库查询工具访问），以及 PDF 表格提取、TXT、Word 表格；自动探测编码（UTF-8 / GB18030）与分隔符，大文件分块流式读取，数值列自动降级数据类型以节省内存。
- **全流程 Web 工作台**：文件上传（含进度与取消）、数据概览指标、分析任务与预设模板、计划审阅/批准、实时步骤与工具调用进度（SSE + 15 秒心跳）、随时取消分析、报告生成后的多轮追问、产物中心（预览/对比/PNG/批量下载）、历史会话（重命名/删除/导出 ZIP/导入）、命令面板与快捷键、深浅主题、移动端适配。
- **断点续跑与自动恢复**：分析中断后可从已完成的步骤继续，无需重跑；前端支持 SSE 断线自动恢复与一键重试。
- **安全与访问控制**：可选访问令牌（`APP_ACCESS_TOKEN` 全权、`APP_VIEWER_TOKEN` 只读——只允许 GET/HEAD，写请求返回 403）；滑动窗口速率限制、CORS / GZip / 安全响应头；写操作与拒绝访问追加审计日志（`DATA_AGENT_AUDIT_LOG` 指向 JSONL，不记请求体/令牌）；API Key 通过操作系统凭据库（keyring）保存，不落明文文件。
- **会话持久化**：本地或 S3 兼容对象存储（如 Cloudflare R2）双后端，会话按 session id 自动归档/恢复，带路径穿越防护。
- **设置与连接诊断**：设置面板用原生 `<dialog>`，支持「测试连接」——只探测模型服务连通性，不落盘、不回显 Key，按 401/429/超时/网络错误分类提示；LangSmith 状态细分为 configured / missing_key / key_unconfirmed / local / disabled。
- **可观测与部署**：LangSmith 全链路 Trace（规划、每次工具调用、重规划、汇总）；LangGraph Agent Server 部署；Dockerfile（非 root 用户 + 健康检查）；Render Blueprint 一键部署；GitHub Actions 自动 CI。
- **可复核的分析溯源与逐结论核验**：每次分析在会话目录生成哈希链，覆盖任务元信息、数据集、指标定义文件、逐条工具调用、产物文件与报告正文；finalize/replay 另将正文数字逐项判为 `verified` / `conflict` / `unsupported`，在报告追加结论核验表并生成可下载的 `report-verification.json`，按 trace 序号和 JSON Pointer 定位证据。见 [docs/analysis-provenance.md](docs/analysis-provenance.md)。
- **有据经营简报核验（schema v2）**：工作台「简报核验」把待审草稿逐行判为 `verified` / `conflict` / `unsupported`，核验器独立读取原始 CSV 复算，不调用第二个模型。源文件只读一次字节快照，SHA-256、编码识别与 CSV 解析基于同一份 bytes，不存在读两遍的 TOCTOU；**范围门**要求登记指标整行精确匹配，「华东区订单数」「平均订单数」「订单数（华东区）」一律 `unsupported`，即使 `confirm_full_table=true` 也不放行；订单号近似重复（NFKC、大小写、零宽字符、前导 0）只进 `warnings` 并给相关指标附 `caveat`，不改变订单数口径；表头接受 订单号/金额/数量 等中文别名，仅唯一匹配时接受，歧义直接报错。指标口径为 销售额合计 / 销量合计 / 订单数 / 客单价（销售额合计 ÷ 订单数，按分 HALF_UP）。每次复算返回确定性 `audit_id`（源文件哈希 + 草稿 + 确认标志 + 复算结果，带起草轨迹时再绑定轨迹哈希，最终取 SHA-256 截断），未登记指标按单位给出口径候选 `candidates`。会话可在「核验口径」里登记声明式指标（列、聚合、单位、精度、类型、空值策略、舍入、正负策略、过滤与时间窗口），核验器按登记口径独立复算，口径指纹绑定进 `audit_id` 与账本；内置销售口径作为默认。口径支持草稿/已批准状态与批准人，批准人不能与作者相同；`audit_lineage.json` 可登记上游表、抽取时间、负责人与新鲜度 SLA，超期会标记 `source_stale` 并写入账本。
- **服务端确认门与核验账本**：`POST /api/sessions/{id}/sales-audit/confirm` 由服务端按会话里的原始 CSV 重新复算，重算的 `audit_id` 与提交值不一致返回 409；通过后写一条 `confirm` 记录并返回权威简报与回执（`seq` / `ts` / `hash` / `prev` / `brief_sha256`）。每次复算也会尝试追加一条 `audit` 记录（写失败时响应带 `ledger_error`，不影响本次核验结果），账本为 `runs/<会话>/audit_ledger.jsonl`，逐条 `prev` / `hash` 串联，追加前先校验整链。`GET /api/sessions/{id}/sales-audit/ledger` 返回最近记录与链校验结果；`POST /api/sessions/{id}/sales-audit/bundle` 可导出零信任证据包（原始 CSV、草稿、口径、血缘、账本与核验结果）；`scripts/independent_replay.py` 只用 Python 标准库从头复算并重现 `audit_id`。离线脚本 `scripts/verify_audit_ledger.py` 可校验链条，`--brief` 文件改 1 个字节即退出码 1，`--source` 校验原始 CSV，`--trace` 校验快速起草轨迹。账本每条记录带 Ed25519 签名（见「签名」条目），`--pubkey` 可离线验签；私钥与数据同目录（工作区访问即密钥访问），仍无外部时间戳，属演示级篡改可见性。
- **快速起草对照入口**：`POST /api/sessions/{id}/sales-audit/quick-draft` 不调用任何工具、关闭思考，直接把订单表交给模型写三行指标，作为核验器的对照输入；返回模型名、耗时、脱敏单元格数、丢弃行数与轨迹哈希 `trace_sha256`。轨迹写到 `draft_traces/<sha256>.json`；把 `trace_sha256` 作为 `draft_trace_sha256` 传给复算/确认接口，即可把「模型原话 → 过滤后的草稿 → 复算 → 确认」绑定起来。它刻意不提示去重口径，不能代表产品的分析能力。
- **发布前质量闸门（可选）**：`data-agent-eval compare` 对同一套用例的基线与候选结果做配对 McNemar 精确检验，同时检查成本与延迟回归，给出接受 / 拒绝 / 证据不足三态裁决，并写入可校验的哈希链 ledger。见 [docs/agent-eval.md](docs/agent-eval.md)。

- **Ed25519 签名**：账本每条记录用 Ed25519 签名链哈希，公钥随证据包导出；`scripts/verify_audit_ledger.py --pubkey` 可离线验签（需 `cryptography`，已随主依赖安装；`scripts/independent_replay.py` 复算 audit_id 仅用标准库）；账本含签名而未提供 `--pubkey` 时，校验脚本与证据包回放都会显式输出「签名未校验」，不再静默通过。

## 演示数据目录

首页「打开示例」菜单内置七份可讲故事的数据（旁边的「体验简报核验」按钮直接载入其中的销售样例并切到核验视图）：

| kind | 演示点 |
| --- | --- |
| `sales_audit` | 草稿把销量当订单数，462 → 20 的差额复算、确认、验签 |
| `regional_policy` | 区域 RLS + 手机号脱敏 + 预置口径/血缘 |
| `quality_injection` | 缺失、负数、极端值、混合日期，备注含提示注入 |
| `growth` | 12 个月 × 3 渠道 × 4 品类，覆盖趋势、构成、漏斗、树图等 10 种图形 |
| `grades` | 3,000 名学生 × 60 个班 × 3 次考试，分布、班级对比、学科相关 |
| `finance` | 12 个月利润表、现金流与预算，利润拆解、成本结构、季度净利率 |
| `bigdata` | 服务端生成 20 万行，演示密度视图与大数据路径 |

加载后自动生成预分析：识别日期/数值/分类列，按语义选折线、条形、散点、直方图、占比环或密度网格，打开即有 KPI 与结论。

接口：`GET /api/samples`、`POST /api/sessions/sample?kind=...`。

## 技术栈

- 后端：Python 3.11–3.13、FastAPI、Uvicorn、LangChain、LangGraph、LangSmith
- 模型接入：DeepSeek（Web 默认，支持 thinking mode）；CLI / MCP 通过 `MODEL_PROVIDER` 也支持 OpenAI 及任意 OpenAI 兼容服务
- 数据分析：pandas、NumPy、SciPy、scikit-learn、Plotly、Kaleido、openpyxl、PyArrow、pdfplumber、python-docx
- 存储：本地文件系统 / boto3（S3 / R2）
- 前端：TypeScript、React 19、Vite、Zustand、lucide-react、motion、react-markdown（Vitest）
- CLI：Typer + Rich

## 快速开始

### 环境要求
- Python 3.11–3.13
- Node.js 22.12 或更高版本（Vite 8 的最低要求）
- 一个 LLM API Key（Web 默认 DeepSeek；CLI / MCP 兼容 OpenAI）
- 可选：`pip install -e ".[stats]"` 安装统计建模依赖（scipy / scikit-learn / statsmodels）。受限 Python 沙箱只有在装了该 extra 后才能 `import scipy / sklearn / statsmodels`；未安装时沙箱返回 `pip install "data-analysis-agent[stats]"` 的可操作提示，而不是裸 `ModuleNotFoundError`。

### 一键启动（Windows）
双击 `run.bat`。首次运行会自动创建虚拟环境、安装后端依赖并 `npm install`，随后分别启动（脚本会打印实际地址并自动打开浏览器）：

- 前端：默认 http://127.0.0.1:5173；5173 被占用时自动在 5173–5182 间选可用端口
- 后端 API：默认 http://127.0.0.1:8000；8000 被占用时自动在 8000–8009 间选可用端口
- API 文档：`<实际后端地址>/docs`

**访问令牌（可选）**：如果在 `.env` 里设置了 `APP_ACCESS_TOKEN`，新打开的浏览器会先停在「进入数据工作台」令牌页。输入一次令牌（值就是 `.env` 里的 `APP_ACCESS_TOKEN`）后会保存在该浏览器的 localStorage；只监听 127.0.0.1 时可以留空不设。

### 手动启动

```powershell
# 后端
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
Copy-Item .env.example .env   # 编辑填入 DEEPSEEK_API_KEY
.\.venv\Scripts\python.exe -m uvicorn data_agent.api:app --host 127.0.0.1 --port 8000

# 前端（另开终端）
cd frontend
npm install
npm run dev
```

### 命令行分析

```bash
pip install -e ".[dev]"
export DEEPSEEK_API_KEY=<your-key>        # Windows PowerShell: $env:DEEPSEEK_API_KEY="<your-key>"
data-agent analyze examples/sample_sales.csv --task "分析各区域销售趋势"
```

输出为 Markdown 格式分析报告，产物路径一并打印。报告走 stdout、进度与错误走 stderr，可以直接 `data-agent analyze x.csv > report.md`。

命令行失败时不会打印 traceback：用法/配置错误退出码 2，运行期错误（网络、上游模型）1，`Ctrl+C` 取消 130。加 `--debug` 可看完整堆栈。

### 清理运行目录

CLI / MCP 每次运行都会在 `runs/` 下新建 `cli_*` / `mcp_*` 目录，长期使用会堆积：

```bash
data-agent clean --dry-run                 # 预览：默认删除超过 7 天未修改的，保留最新 20 个
data-agent clean --days 30 --keep 10       # 改阈值（删除前会确认）
data-agent clean --include-web             # 连同 Web 会话 api_* 一起清理（建议先停后端）
```

`--days 0` 会清空全部候选目录（保留数由 `--keep` 决定），必须显式再加 `--all` 才执行；路径写错或指向文件会直接报错而不是静默成功。

希望每次 `analyze` 自动清理时，在 `.env` 设置 `DATA_AGENT_CLI_RETENTION_DAYS=7`（始终保留最新 10 个）。

### MCP 数据面服务（供其他 Agent 调用）

把**受控的数据访问能力**以 MCP（Model Context Protocol）标准开放给 Claude Desktop、
WorkBuddy 等任意 MCP 客户端：其他 Agent 拿到的是"安全的数据访问"，而不是一个
无限制的代码沙箱——这是本项目差异化的直接延伸。

```bash
pip install ".[mcp]"        # 或开发环境 pip install -e ".[dev]"
data-agent-mcp              # stdio 传输，供本地 MCP 客户端拉起
```

客户端配置示例（Claude Desktop / 通用 MCP 客户端）：

```json
{
  "mcpServers": {
    "data-analysis-agent": {
      "command": "data-agent-mcp",
      "env": { "DATA_AGENT_RUNS_DIR": "./runs" }
    }
  }
}
```

开放 4 个工具：`list_sessions`（发现会话）、`open_dataset`（载入本地数据文件并建会话）、
`inspect_data`（结构与预览）、`sql_query`（只读 SQL，支持会话内 SQLite 与
`DATA_AGENT_DATABASE_URL` 配置的 PostgreSQL）。护栏与工作区内工具同源：只接受
`SELECT`/`WITH`，写入在驱动或服务端被拒绝，结果上限 5000 行。

边界说明：完整分析流程（LLM 规划-执行-报告）是分钟级长任务，仍走 HTTP API，
不在 MCP 数据面内。传输为 stdio、由本地受信客户端拉起，**不要**把该服务挂到
网络上对外提供；远程访问请使用带令牌与限流的 HTTP API。

### 有据经营简报（服务端确认与账本）

「简报核验」页复算后会得到一个确定性的 `audit_id`。确认门不是前端本地状态：
服务端会按会话里的原始 CSV 重新复算，只有重算的 `audit_id` 与提交值一致才写账本
并返回权威简报。下面的命令在仓库根目录、后端已启动时执行：

```powershell
$base = "http://127.0.0.1:8000"
$sid = "<会话 ID>"   # 上传 CSV 后从会话 URL 或 GET /api/sessions 取得
$draft = @"
销售额合计：17224.00 元
销量合计：462 件
订单数：20 笔
"@

# 1. 独立复算：返回逐行判定、warnings 与 audit_id
$audit = Invoke-RestMethod "$base/api/sessions/$sid/sales-audit" -Method Post `
  -ContentType "application/json; charset=utf-8" `
  -Body ([Text.Encoding]::UTF8.GetBytes((@{ draft = $draft; confirm_full_table = $false } | ConvertTo-Json)))

# 2. 服务端确认：audit_id 不一致返回 409；通过后写账本并返回回执
$confirm = Invoke-RestMethod "$base/api/sessions/$sid/sales-audit/confirm" -Method Post `
  -ContentType "application/json; charset=utf-8" `
  -Body ([Text.Encoding]::UTF8.GetBytes((@{ draft = $draft; audit_id = $audit.audit_id; confirm_full_table = $false } | ConvertTo-Json)))
$confirm.receipt   # seq / ts / hash / prev / brief_sha256

# 3. 账本：最近记录与整链校验结果
Invoke-RestMethod "$base/api/sessions/$sid/sales-audit/ledger"
```

确认成功后即可导出零信任证据包。页面按钮：路演视图证据轨底部的「下载证据包 (.zip)」，或草稿工作台「核验结果」的下载操作区（与「下载核验明细 (.json)」并列；确认后才出现）。不依赖 UI 时：

```powershell
# 生成 evidence-<会话>.zip（按实际端口替换 $base；$sid / $draft 见上）
Invoke-RestMethod "$base/api/sessions/$sid/sales-audit/bundle" -Method Post `
  -ContentType "application/json; charset=utf-8" `
  -Body ([Text.Encoding]::UTF8.GetBytes((@{ draft = $draft; confirm_full_table = $false; draft_trace_sha256 = $null } | ConvertTo-Json))) `
  -OutFile "evidence-$sid.zip"

# 只用标准库从头复算并重现 audit_id，同时校验 manifest 与账本链
.\.venv\Scripts\python.exe scripts\independent_replay.py "evidence-$sid.zip"
# 退出码 0 = 全部通过；包内任一文件或账本被改动即退出码 1
```

证据包内含原始 CSV、草稿、口径快照（未登记时为内置销售四项）、来源血缘、账本、核验报告与 manifest；现场可把 ZIP 交给评委，让他们用自己的 Python 验证，而不必信任本服务。`scripts/prepare_demo_evidence.py` 可一键完成「演示会话 → 复算 → 确认 → 证据包 → 独立复算」，不调用模型、不依赖外网，可重复执行。

把确认接口返回的 `brief` 存成 `经营简报-核验版.md` 后，可离线校验账本与导出文件。**必须按原始字节保存**：直接使用页面「下载简报 (.md)」，或按下例写入；PowerShell 的 `Set-Content` / `Out-File` 会加 UTF-8 BOM 或把换行转成 CRLF，脚本按原始字节哈希，会误报「简报被篡改」：

```powershell
# 字节精确保存（等价于页面下载）
[IO.File]::WriteAllBytes("$PWD\经营简报-核验版.md", [Text.Encoding]::UTF8.GetBytes($confirm.brief))

.\.venv\Scripts\python.exe scripts\verify_audit_ledger.py "runs\$sid\audit_ledger.jsonl" `
  --brief .\经营简报-核验版.md --source "runs\$sid\input\<上传文件名>.csv"
# 退出码 0 = 通过；改动简报任意 1 个字节后再跑，退出码 1
```

快速起草用于展示「未经验证的模型输出」作为对照（不调用工具、关闭思考）：

```powershell
Invoke-RestMethod "$base/api/sessions/$sid/sales-audit/quick-draft" -Method Post
```

返回值里的 `trace_sha256` 可作为 `draft_trace_sha256` 传给复算或确认接口；离线时用
`--trace "runs\$sid\draft_traces\<sha256>.json"` 一并校验「模型原话 → 草稿 → 复算」是否对应。

路演页的「签章并出具简报」（草稿工作台里为「确认并生成简报」/「生成简报」）会调用
上面的 confirm 接口：成功后在「确认记录」区展示回执（序号 #N、时间、hash 前 16
位）与「服务端按原始 CSV 重算后写入」提示，下载的也是服务端返回的权威简报；若草稿或
数据在核验后被改动，服务端返回 409，页面会要求重新复算。账本逐条带 Ed25519 签名（私钥与数据同目录，仍无外部时间戳），能发现内容被改动，
不能证明「谁在何时确认」之外的强不可否认性。完整路演步骤见上方「简报核验」说明。

### 声明式核验口径

核验器不只认销售额、销量、订单数、客单价这四个内置销售指标。每个会话可以在
工作区根目录放一份 `audit_schema.json`，用纯声明式字段描述自己的指标；核验时
按登记口径独立读取原始 CSV 复算，不执行模型生成的表达式。字段如下：

| 字段 | 说明 |
| --- | --- |
| `name` | 指标名称，草稿行按该名称匹配，会话内不可重复 |
| `column` | 原始 CSV 列名（表头必须存在）；`ratio` 可省略，缺省时取 `numerator` |
| `aggregation` | `sum` / `count` / `count_distinct` / `min` / `max` / `mean`；`ratio` 为派生比率，需配 `numerator` / `denominator` |
| `numerator` / `denominator` | 仅 `aggregation=ratio` 时使用，引用同一口径中已登记的两个非 `ratio` 指标；`ratio` 不支持 `filters` / `time_window` |
| `unit` | 单位，最多 8 个字符 |
| `decimals` | 输出小数位，0–6，默认 2 |
| `data_type` | `decimal` / `integer` / `date` / `string`；`sum`、`mean`、`min`、`max` 只接受 `decimal` 或 `integer` |
| `null_policy` | `exclude`（跳过）/ `fail`（报错）/ `zero`（空值按 0 参与求和与均值） |
| `rounding` | `HALF_UP` / `HALF_EVEN`，默认 `HALF_UP` |
| `sign_policy` | `allow` / `non_negative`（数值聚合出现负数即报错） |
| `filters` | 数组，最多 8 项；`op` 支持 `eq` / `ne` / `gt` / `ge` / `lt` / `le` / `in` / `contains` |
| `time_window` | `{column, start, end}`，日期为 `YYYY-MM-DD`，起止均包含 |

一个会话最多登记 12 项指标，`filters` 内条件按 AND 组合。`ratio` 的分母为 0 或
引用未登记指标时直接报错，不输出猜测值。下面是一张非销售表
（`customer_id, region, revenue, signup_date`）的口径示例：

```json
{
  "metrics": [
    {
      "name": "收入合计",
      "column": "revenue",
      "aggregation": "sum",
      "unit": "元",
      "data_type": "decimal",
      "null_policy": "exclude",
      "rounding": "HALF_UP",
      "sign_policy": "non_negative",
      "decimals": 2
    },
    {
      "name": "客户数",
      "column": "customer_id",
      "aggregation": "count_distinct",
      "unit": "人"
    },
    {
      "name": "华东收入",
      "column": "revenue",
      "aggregation": "sum",
      "unit": "元",
      "filters": [{ "column": "region", "op": "eq", "value": "East" }]
    },
    {
      "name": "Q4 华东收入",
      "column": "revenue",
      "aggregation": "sum",
      "unit": "元",
      "filters": [{ "column": "region", "op": "eq", "value": "East" }],
      "time_window": {
        "column": "signup_date",
        "start": "2026-10-01",
        "end": "2026-12-31"
      }
    }
  ]
}
```

对应的数据文件：

```text
customer_id,region,revenue,signup_date
C1,East,100.50,2026-10-02
C1,East,99.50,2026-11-03
C2,West,50.00,2026-11-04
C2,East,30.00,2026-12-31
C3,East,40.00,2027-01-05
```

验收命令（仓库根目录；本机实测 29 passed）：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_audit_schema.py -q
# 29 passed
```

把上面的 JSON 存为 `$env:TEMP\audit-schema-demo\audit_schema.json`、数据存为
同目录的 `customers.csv` 后，也可以直接跑一次复算：

```powershell
$demo = Join-Path $env:TEMP "audit-schema-demo"
.\.venv\Scripts\python.exe -c "from pathlib import Path; from data_agent.audit_schema import load_schema, read_metrics; print(read_metrics(Path(r'$demo\customers.csv'), load_schema(Path(r'$demo'))).metrics)"
# {'收入合计': '320.00', '客户数': '3', '华东收入': '270.00', 'Q4 华东收入': '230.00'}
```

`GET/PUT /api/sessions/{id}/audit-schema` 读取或覆盖当前会话口径，前端「核验口径」
折叠面板编辑的就是这段 JSON。保存时写入 `audit_schema.json`，并向
`audit_schema_history.jsonl` 追加一条带 `ts`、`sha256` 和完整指标定义的记录；
`GET /api/sessions/{id}/audit-schema/history` 返回最近 `limit` 条保存历史（默认
20，最多 100），并逐条复算 `fingerprint_valid`，用 `consistent` 汇总整份历史
是否自洽。未登记或保存空数组时
回退到内置销售四项。口径指纹是对登记项规范化 JSON 取 SHA-256，会进入
`audit_id`，并随 `audit` / `confirm` 记录写入 `audit_ledger.jsonl`；口径一变，
旧 `audit_id` 就对不上，确认接口返回 409。离线可用
`scripts/verify_audit_ledger.py <账本> --schema audit_schema.json` 校验当前口径
指纹是否命中账本记录，口径被改动时退出码 1。空值、类型或负数策略失败时，接口
返回带「第 N 条记录的列名」的 422 诊断。

当前接口返回完整策略字段：`GET/PUT` 响应除 `name` / `column` / `aggregation` /
`unit` / `decimals` / `aliases` 外，还会回显 `data_type`、`null_policy`、
`rounding`、`sign_policy`、`filters`、`time_window`、`numerator`、`denominator`。
前端「核验口径」面板编辑和保存的就是同一份完整 JSON；这些字段会一起写入
`audit_schema.json` 并参与复算。

## 配置

复制 `.env.example` 为 `.env` 并填写（所有密钥均为占位符）：

```dotenv
MODEL_PROVIDER=deepseek
DEEPSEEK_API_KEY=<your-deepseek-api-key>
DEEPSEEK_MODEL=deepseek-flash
DEEPSEEK_THINKING=true
DEEPSEEK_REASONING_EFFORT=high

# 或使用 OpenAI 兼容服务（CLI / MCP；Web 演示默认 DeepSeek）
OPENAI_API_KEY=<your-api-key>
OPENAI_MODEL=gpt-4.1-mini

# 前端构建产物目录（源码树内运行可省略；Docker / Render / wheel 安装必须显式指定）
DATA_AGENT_STATIC_DIR=frontend/dist

# 线上访问保护（绑定 0.0.0.0 等非环回地址时必填，否则拒绝启动）
APP_ACCESS_TOKEN=<your-access-token>

# 可选：指标口径登记文件（登记后规划阶段会注入口径定义，防止跨会话口径漂移）
DATA_AGENT_METRICS_PATH=<path-to-metrics.json>

# 可选：PostgreSQL 数据源（配置后 query_database 可只读查询该库；驱动 pip install ".[postgres]"）
DATA_AGENT_DATABASE_URL=postgresql://user:password@host:5432/dbname

# 可选：Cloudflare R2 / S3 兼容持久化
DATA_AGENT_STORAGE_BACKEND=s3
DATA_AGENT_STORAGE_BUCKET=<bucket-name>
DATA_AGENT_STORAGE_ENDPOINT_URL=https://<ACCOUNT_ID>.r2.cloudflarestorage.com
AWS_ACCESS_KEY_ID=<your-access-key-id>
AWS_SECRET_ACCESS_KEY=<your-secret-access-key>
```

完整变量清单见 `.env.example`。

## 用于实践时请先知道

- **本进程是一个信任域。** 单一 `APP_ACCESS_TOKEN` 能列出并打开 `runs/` 下全部会话。团队共用一台机器时必须设令牌，也不要把不同客户的数据放进同一个服务。
- **默认先审阅计划。** `⌘/Ctrl+Enter` 与主按钮「生成计划」只出步骤；确认后再批准执行。「跳过审阅」才会直接跑。
- **指标要登记才会按同一口径算。** 工作台「业务口径」或 `DATA_AGENT_METRICS_PATH` 写入 `metrics.json`，表达式必须使用真实列名（如 `sales - returns`）。分析时会调用 `compute_registered_metrics`，报告里的「核验附录」默认收起。
- **经营简报可以独立复算，并由服务端确认。** 工作台「简报核验」读取原始订单 CSV（UTF-8 / GB18030），核对销售额、销量、订单数和客单价，提供核验版简报及证据下载；范围限定指标（如「华东区订单数」）不会被当成全表口径放行。核验返回 `audit_id`，`/confirm` 由服务端按原始 CSV 重算后才写账本并返回回执，账本可用 `scripts/verify_audit_ledger.py` 离线校验（改简报 1 个字节即退出码 1）。页面上的「签章并出具简报」（草稿工作台为「确认并生成简报」/「生成简报」）会直接调用该接口，成功后展示「确认记录」回执序号与 hash，并下载服务端返回的权威简报。
- **SQLite 大表不会整表进内存。** 超过 10 万行只载入前 10 万行；全量请用 `query_database` 聚合后再 `adopt`。CSV 也可用该工具，表名为 `data`。
- **受限 Python 沙箱只做内存计算。** 可导入 pandas / numpy / 标准库，装了 `[stats]` extra 后还可导入 scipy / scikit-learn / statsmodels；白名单外的 `os` / `subprocess` / `socket` 一律拒绝，统计库的任意路径读写（`scipy.io`、`statsmodels.iolib`、`load_svmlight_file`…）、网络抓取（`sklearn.datasets.fetch_*`、`statsmodels.datasets.webuse`、`scipy.datasets`）与显式并行入口（`Parallel` / `delayed` / `joblib`）都在 AST 与子进程运行时两层拦截。导入 sklearn 后 joblib 默认后端固定为线程，`n_jobs>1` 不会另起子进程，15 秒超时与 2GB RSS 护栏对整次计算继续生效。
- **换新数据重跑：** 报告上的「按配方重跑」读取 `analysis_recipe.json`，按上次工具参数执行，不重新规划。
- **给业务的文件：** 导出菜单可下载 Word / PPT；「分析溯源 (.json)」给出报告对应的数据集、工具调用与产物哈希链及复核结果。
- 样例行在发给模型前会脱敏手机号、邮箱、身份证号；不要依赖它代替完整的数据治理。

## 项目结构

```
src/data_agent/
  api.py             FastAPI 应用装配与前端静态资源托管
  agent.py           DataAnalysisAgent：Plan-and-Execute + ReAct
  cli.py             Typer 命令行入口
  config.py          环境变量驱动的运行时配置
  nodes/             LangGraph 节点（validate/plan/execute/replan/finalize）
  tools/             ReAct 工具集（检查/清洗/转换/合并/数据库/统计/可视化/导出/沙箱）
  metrics.py         指标口径登记（可选，规划阶段注入；含已验证问法）
  data_sources.py    数据源注册表（只读保证等级 / 发现列表 / 外部源连接）
  automation.py      定时分析任务（调度 / 指标异常检测 / 告警推送）
  report_export.py   报告导出（Word / PPT / PDF；PDF 按需 reportlab）
  skill.py           分析配方 → 技能包导出 / 安装 / 清单与确定性重跑
  provenance.py      分析溯源哈希链（数据集 / 指标定义 / 工具 / 产物 / 报告）
  verification.py    逐结论确定性核验与 JSON 报告（verified/conflict/unsupported）
  sales_audit.py     经营简报核验（单次字节快照 / 范围门 / audit_id）
  audit_ledger.py    核验账本（prev/hash 哈希链，追加前整链校验）
  audit_draft.py     快速起草对照入口（不调工具、关闭思考）
  evalkit/           发布前评测闸门（McNemar / Wilson / ledger / CLI）
  sqlite_source.py   只读 SQLite 数据源（表发现 / 结构描述 / 受限查询）
  postgres_source.py 只读 PostgreSQL 数据源（服务端只读 + statement_timeout）
  sql_guard.py       驱动无关的只读 SQL 护栏（两个数据源共用）
  mcp_server.py      MCP 数据面服务（stdio，供其他 Agent 调用）
  echarts_engine.py  ECharts 图表引擎（与 Plotly 引擎并列）
  deployment.py      LangSmith Agent Server 图入口
  storage.py         本地 / S3 兼容对象存储后端
  middleware.py      CORS / GZip / 访问令牌 / 速率限制
  routers/           会话 / 分析 / 产物 / 设置路由
frontend/            React + Vite 前端（构建产物 frontend/dist 由 `npm run build` 生成，不入 git）
examples/            示例数据
tests/               pytest 测试
```

## 部署

- **Docker**：根目录 `Dockerfile` 两阶段构建（Node 构建前端 + Python 运行时），以非 root 用户运行并带健康检查。容器监听 `0.0.0.0`，必须设置 `APP_ACCESS_TOKEN`，否则进程启动即报错退出。Dockerfile / Render 构建命令会额外安装 `reportlab`，保证 `report.pdf` 开箱可用；自行部署时若未安装，该接口返回 503 并提示安装。
- **访问令牌**：监听非环回地址（如 Docker / Render 的 `0.0.0.0`）时必须至少设置 `APP_ACCESS_TOKEN` 或 `APP_VIEWER_TOKEN`（后者只读，写请求 403）；本地 `127.0.0.1` 开发可留空。审计日志用 `DATA_AGENT_AUDIT_LOG` 指向 JSONL 文件。
- **LangSmith**：`langgraph.json` 导出 `data_analysis_agent` 图，可直接创建 LangSmith Deployment；数据集通过 `dataset_id` 或受控 `dataset_path` 提供。
- **Render**：根目录 `render.yaml` 提供 Blueprint 一键部署（Free 档可直接同步成功）。首次创建 Blueprint 需在 Render 控制台批准首次同步——状态为 `Created` 时 `autoSync` 不生效，批准后推送才会自动同步；文件内注释含 Starter 及以上档位启用持久磁盘的完整配置。Free 实例休眠后容器存储会清空，长期保存数据请启用 S3/R2 后端。
- **CI**：`.github/workflows/ci.yml` 在 push / PR 时自动执行 ruff、pytest（含 240s 单测超时保护与 `--cov-fail-under=97` 覆盖率门禁）与前端类型检查、测试及生产构建。

## 质量检查

```powershell
.\.venv\Scripts\python.exe -m pytest -n 4   # 后端测试（默认并行 4 路；不调用真实 LLM，不产生费用）
.\.venv\Scripts\python.exe -m ruff check .  # 后端代码检查
cd frontend && npm run typecheck            # 前端 TypeScript 类型检查
cd frontend && npm test                     # 前端单元/组件测试
cd frontend && npm run build                # 前端生产构建
```

后端测试覆盖 `src/data_agent` 全部核心路径（节点、工具、路由、注册表、存储、
CLI、部署），含异常分支与降级路径；CI 中以 `DATA_AGENT_SKIP_KALEIDO=1` 跳过
偶发挂起的无头 PNG 渲染（503/500 降级分支仍被覆盖）。

Windows 本机跑全量时建议加 `--ignore=tests/test_tools.py`：该文件含依赖外网/模型的用例，
Windows 上不 fail-fast 会挂起。`pyproject.toml` 的 `addopts` 已含 `-q`，命令行再加一个
`-q` 会变成 `-qq` 并抑制「N passed」汇总行；现场核对可用
`-o addopts="--disable-warnings" -q` 保留汇总。
## 深入文档

- [docs/engineering-notes.md](docs/engineering-notes.md) — 工程笔记：已验证的工程事实与踩坑记录（大数据图表可读性方案、可访问性校准方法、性能与体积预算、沙箱/隐私/指标模块设计要点、平台差异实证）。
- [docs/competitive-analysis.md](docs/competitive-analysis.md) — 竞品分析：与同类数据分析 Agent 产品的定位与能力对比（2026-09 初查 + 2026-11 官方一手页实地复核，含抓取矩阵与可达性负证据）。
- [docs/automation.md](docs/automation.md) — 定时分析：任务定义、调度命令、异常判定与告警产物、cron/任务计划接法。
- [docs/skills.md](docs/skills.md) — 技能包：从配方导出 SKILL.md、确定性重跑与边界。
- [docs/collaboration.md](docs/collaboration.md) — 协作：会话评论、全权/只读令牌、审计日志与边界。
- [docs/external-databases.md](docs/external-databases.md) — 外部数据库：PostgreSQL 与通用 DB-API 数仓的配置、只读保证与安全边界。
- [docs/agent-eval.md](docs/agent-eval.md) — 发布前评测：配对结果格式、McNemar 精确检验、成本/延迟护栏、ledger 校验与 CI 接法。
- [docs/analysis-provenance.md](docs/analysis-provenance.md) — 分析溯源：哈希链格式、API 与离线复核方法、信任边界。

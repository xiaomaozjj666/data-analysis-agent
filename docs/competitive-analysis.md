# 数据分析 Agent 竞品调研与本项目路线图

调研时间：2026 年 9 月（初查）；2026 年 11 月 17 日（实地复核，见第六节）
调研对象：WorkBuddy（腾讯）、Trae SOLO（字节跳动）、ChatGPT Advanced Data Analysis（OpenAI）、Julius AI、Copilot in Excel / Power BI Copilot（微软）、Tableau Agent & Pulse / FineBI（BI 侧）
被评估对象：本项目 data-analysis-agent（下文简称"本项目"）

> 落地进展：P0 三项均已实现并通过测试——指标口径登记（`metrics.py`）、跨源合并
> （`tools/_join.py`）、数据库直连（`sqlite_source.py` 的 SQLite 文件源 +
> `postgres_source.py` 的 PostgreSQL 数据源，CI 内以真实 service 验证）；
> MCP 数据面服务（`mcp_server.py`，stdio，供其他 Agent 安全访问数据）也已落地，
> 以官方 SDK 客户端做协议互操作测试。尚余 Snowflake/BigQuery 等其余数仓驱动。
> 2026-11 实地复核结论与路线图增量见第六节；正文（2026-09 初查）保留原状，
> 过期数字以「2026-11 复核注」在原位标注、不静默改写。

---

## 一、为什么选这六类

市面上的"数据分析 Agent"表面看都在做同一件事：传个表进去，出来图和结论。实际拆开看，它们分属六种完全不同的产品形态，能力边界、成本结构、失败模式都不一样。只挑形态相似的比，会得出误导性结论。

| 形态 | 代表产品 | 本质是什么 |
| --- | --- | --- |
| 通用对话 + 代码沙箱 | ChatGPT ADA | 大模型能力的一个内嵌工具，不是独立产品 |
| AI 编程/办公 Agent | Trae SOLO | 通用 Agent 泛化到办公场景，靠技能包补齐专业度 |
| 桌面常驻 Agent | WorkBuddy | 以本地文件与生态连接器为主轴的办公助手 |
| 垂直数据分析 SaaS | Julius AI | 专为数据分析设计，重连接与可复用工作流 |
| 办公套件内嵌 | Copilot in Excel / Power BI Copilot | 增强既有软件，不替代它 |
| BI 平台 AI 层 | Tableau Pulse / FineBI | AI 建在语义模型与治理底座之上 |

这个分类本身就是一条重要结论：**真正拉开差距的不是模型，而是数据接入口径、可复用性、治理底座这三件事。**

---

## 二、各产品核心能力与已知短板

### 1. ChatGPT Advanced Data Analysis

能力：服务端 Python 沙箱，预装 pandas、NumPy、matplotlib、seaborn、Plotly、scikit-learn、SciPy、statsmodels、openpyxl，等于把一套标准数据分析栈直接递到手里。上传 CSV/Excel/JSON/PDF 后用自然语言下指令即可，EDA、清洗、回归、时间序列、图表一条龙。2026 年版本放宽了数据集规模并支持交互式图表（可调轴标签与配色后再下载）。

短板：上传表格约 50MB 上限，且 128K 上下文里还要分给系统指令与对话历史，宽表很容易在远低于 50MB 时就失败；沙箱随会话结束重置，中间变量、训练好的模型、生成的中间文件全部不保留；**不能直连数据库**，必须先导出成文件再上传。数值类错误的失败模式最危险——它会给出看起来极其自信的错误答案，而不是承认不确定。付费门槛 Plus $20/月，免费层额度很紧。

### 2. Trae SOLO（字节跳动）

能力：2026 年 3 月底上线的 SOLO 独立端，分 PC 与 Web 两形态，内部再分 Code 与 MTC（More Than Coding）两种模式。MTC 模式首页给了四个入口：网页读取、调研分析、数据挖掘、文件管理。数据分析的实际路径是——自动编写并执行 pandas 脚本完成去重、空值处理、日期格式统一、分表拼接，再产出图表与 Word/PPT/PDF/网页报告。配套技能市场里有 chart-visualization（内置 26 种图表类型）、data-analysis、consulting-analysis、slides 等技能包。依托云端算力支持多任务并行，桌面端发起的任务可在网页端接续查看进度。

短板：第三方实测明确指出，把气泡图导出到 PPT 时它用 matplotlib 画图再截图插入，**没有调用 PPT 原生绘图能力**；再用提示词强制要求原生作图后，美观度只到"基本可用"。本质是"通用 Agent + 技能包"路线，专业深度靠提示词和技能兜底，稳定性和口径一致性依赖每次任务的上下文。

### 3. WorkBuddy（腾讯）

能力：桌面常驻形态，官方归纳为八项：批量文件处理、数据分析与可视化、文档与 PPT 生成、深度信息研究、多 Agent 并行、远程监控、Skill 技能扩展、连接器与 MCP。数据分析走"专家"入口——内置"数据分析报告师""数据分析专家"等角色，上传 Excel/CSV 后自动完成数据读取、清洗整理、指标计算、图表生成，最终产出可交互网页报告。连接器可直连腾讯文档、QQ 邮箱、腾讯乐享、腾讯会议/TAPD，也支持自定义 MCP 接入自有 API 或数据库。另有探索面板把常用场景做成卡片（AI 生图、网页制作、数据分析、深度研究、幻灯片、文档处理、视频生成、日常自动化）。

短板：数据分析能力定位在**表格级办公分析**，面向 Excel/CSV 的清洗与指标计算，不触及数仓、大规模数据治理、行级权限这类企业级议题。能力上限受生态连接器覆盖范围限制，脱离腾讯生态的部分要靠自定义 MCP 自己补。

### 4. Julius AI

能力：这是六家里**数据接口最宽**的。数据连接器直连 PostgreSQL、MySQL、SQL Server、Snowflake、BigQuery、Databricks、Supabase、Vertica，以及 Google Ads、Meta Ads 两个广告平台；还能连 MCP 源。分析引擎输出 SQL、Python、R 三种代码且**代码可见可改**，这对需要审计的场景是硬需求。Notebook 可编辑、可重跑、保留变量，配合 Scheduled Runs 做定时任务，结果通过邮件或 Slack Agent 推送。内置联网搜索与美股数据（基本面、10-K/10-Q/8-K、财报、内部人交易、机构持仓、历史价格、利率）。合规上有 SOC 2 Type II 与 TX-RAMP，明确承诺客户数据不用于模型训练。用户量 200 万+，2025 年拿到 Bessemer 领投的 1000 万美元种子轮。

短板：官方文档自己就提醒——生成的 SQL、join 逻辑、口径需要人工核对，重要数字要和源系统对账。免费版每月只有 5 条消息，实际上不足以评估产品，等于必须付费才能试。

### 5. Copilot in Excel / Power BI Copilot（微软）

能力：Excel 侧分 edit、plan、chat 三种模式，Agent Mode 会先把任务拆成分步计划，再直接在工作簿里动手——用表格、图表、透视表、公式这些 Excel 原生构件，改完内容保持可编辑、可协同、可撤销。支持切换到 Claude 或 GPT 模型（需商业许可）。Power BI 侧 Copilot 能用自然语言生成整页报告、生成并解释 DAX、对语义模型问答、生成叙事摘要。

短板：两处硬约束。一是**质量高度依赖语义模型准备程度**，微软官方反复强调，没经过 AI 数据架构、AI 指令、已验证答案标记等准备工作的语义模型，Copilot 输出会含糊甚至误导。二是许可门槛，Power BI Copilot 需要 Fabric 或 Premium 容量（不同来源对最低 SKU 说法不一，F2+ 与 F64 都有人提，采购前必须按官方当前口径核实），Excel 侧需要符合条件的 M365 订阅。私有化部署场景 AI 功能受限，中国市场还受云服务覆盖范围限制。Excel Agent Mode 还要求计算选项保持自动。

### 6. Tableau Agent & Pulse / FineBI

能力：Tableau 2026 年推 Agentic Analytics，Tableau Agent 管对话式数据探索与可视化创建，Tableau Pulse 管**主动监控**——持续盯着定义好的指标，检测到异常、趋势变化就主动推送到 Slack、Teams 或邮件，用户不用打开 Tableau；Agent Actions 进一步做到基于洞察触发工作流。2026.2 版本推出 Hosted Tableau MCP，云托管、带 OAuth 2.1，任何 MCP 兼容的 Agent 都能连到受信数据。FineBI 走的是另一条路：以指标中心为底座，通过 FineBINEXT 的 Data Agent 做"取数→拆解→归因→推演→看板→报告→预警"的完整闭环，AI 分析继承完整行级/列级权限、脱敏规则与全链路血缘。IDC 认证 FineBI 在 2024 年中国 BI 市场份额第一（20.8%）。

短板：两者的 AI 价值都**依附于既有平台投资**。Tableau Agent 与增强问答需要额外的 Tableau+ 订阅；Power BI 同样要先有容量。如果不打算用它们的 BI 底座，为了 AI 去采购平台是划不来的——这一点第三方评测说得很直白：如果两个平台都没在用，AI 不是采纳它的理由。

---

## 三、逐项评估：这些能力对本项目意味着什么

下面按能力维度拆，每项都落到本项目的具体位置。

### 3.1 数据库/数仓直连与 MCP —— 价值最高，缺口最大

Julius、Tableau（Hosted MCP）、WorkBuddy（自定义 MCP）、Power BI 四家都能让分析直接在数据源上跑，不落文件。本项目目前**只能靠文件上传**：我通读了 `src/data_agent/` 全部 39 个 Python 文件，没有任何 `sqlalchemy`、`create_engine`、`psycopg`、`snowflake`、`bigquery` 痕迹，也没有 MCP 客户端。

> **2026-11 复核注（原位改判）**：上文描述的是 2026-09 初查时点。当前 `src/data_agent/` 已有 88 个 Python 文件，SQLite / PostgreSQL 直连与 MCP 数据面服务均已落地（见顶部"落地进展"），"只能靠文件上传"不再成立；Snowflake/BigQuery 驱动仍未落地，该缺口维持。

这件事的重要性被行业实践反复确认。2026 年 MCP 数据库访问已经形成明确参考架构：MCP server 暴露 `list_tables`、`describe_table`、`run_query`、`sample_rows` 四个精简工具，配专用只读角色（Snowflake 用 `TYPE=SERVICE` 用户 + RSA 密钥对；BigQuery 用 Job User + Data Viewer + Read Session User），再叠加行数上限、查询超时、方言校验、SQL 哈希审计，连接池配合数仓的自动挂起策略控制成本。

对本项目的直接价值：一旦接上，`DataWorkspace.load()` 的入口就从"文件路径"扩展为"连接标识 + 查询"，下游的清洗、统计、图表、报告链路一行不用改——因为它们是围绕 pandas DataFrame 设计的。这是本项目现有架构少有的"改造收益比极高"的位置。

### 3.2 Notebook / 可复用分析资产 —— 价值高，属产品形态级缺口

Julius 的 Notebook 可编辑、可重跑、保留变量、可调度；Trae 会把分析固化成可复用脚本（其官方技术文章明确把"分析过程沉淀为可复用脚本，明年换新数据一键运行"列为核心卖点）；Claude Code 在数据场景的差异化也在于每次任务都留一个可重跑的 Python 文件。

本项目产出的是一份报告加一批图表 HTML。**分析过程本身没有被固化成资产。**下次换一批新数据，同样的分析要重新跑一遍 LLM，且因为 LLM 的随机性，两轮口径可能不一致。这既是能力缺口，也直接连着下一条。

### 3.3 语义层 / 指标中心 —— 决定"分析可信度"，本项目最该补的差异化点

Power BI、Tableau、FineBI 三家的 AI 全部建立在语义模型或指标中心之上，理由高度一致：口径统一、可溯源、权限继承。微软官方文档把话说得很透——Copilot 输出质量取决于语义模型的准备程度，跳过准备工作的团队拿到的就是含糊甚至不准的答案。

本项目的现状是：每次分析的口径由 LLM 现场推断。同一个"销售额"概念，今天算含退货还是不含退货，明天可能变。这不影响单次演示，但在真实业务里是硬伤——**跨会话口径漂移**是数据分析产品最难被原谅的错误类型。

补这一层不需要做到 Power BI 那种规模。把业务指标落成一份可版本化的 YAML/JSON 定义（指标名、口径说明、计算公式、来源列、更新频率），分析时让 LLM 引用而非现推，就能拿到 80% 的收益。这也是本项目相对通用 Agent 最有机会建立壁垒的地方——通用 Agent 没有动力做业务口径治理。

### 3.4 调度与主动洞察 —— 价值中高，属"人找数据"到"数据找人"的跨越

Julius 有 Scheduled Runs + 邮件/Slack 投递；Tableau Pulse 主动监测指标异常并推送；Power BI 有订阅与叙事摘要；FineBI 的 Data Agent 闭环里带主动预警。本项目是纯粹的"你问我才动"。

对本项目：产物已经存在 `runs/<session>/artifacts/`，持久化后端也已支持本地与 S3/R2 双路，加一层定时触发 + 异常检测 + 推送器的工程成本并不高。真正的难点在"检测什么算异常"——这又回到 3.3 的指标定义，指标层立起来之后异常检测才有稳定基准。

### 3.5 交付形态（Word/PPT/PDF）—— 已补齐 Word/PPT，PDF 暂缺

Trae 能直出 Word/PPT/PDF；WorkBuddy 有文档与 PPT 生成能力；Power BI 的叙事摘要可直接嵌进报表和邮件订阅。

本项目交付 Markdown 报告 + HTML 图表 + 产物中心（预览/对比/PNG/批量下载/ZIP），并已实现 `report_export.py`：从结构化报告直接生成 **Word / PPT**（导出菜单下载，供业务转发）。PDF 尚未覆盖。报告本身是结构化 Markdown、图表是独立资产，补 PDF 是纯增量工作。

### 3.6 图表能力与报告叙事 —— 本项目已有局部优势，但叙事编排偏弱

Trae 实测里有一段值得注意的评价：图表类型匹配得当、配色统一、图表之间存在逻辑递进（先看整体趋势，再看渠道拆解，再看转化效率，最后落到异常检测），"数据的叙事非常符合逻辑"。

本项目的图表工程明显更扎实：Plotly + ECharts 双引擎共享同一套数据准备逻辑，覆盖折线、柱状、散点（含三维）、直方图、箱线图、小提琴图、饼图、热力图、相关热力图、散点矩阵、旭日图、矩形树图，`chart_type="auto"` 的推断规则在 `charts.py` 中有明确实现（时间序列→line、两数值→scatter、分类+数值→bar、类别≤8 且无颜色→pie、路径列→sunburst、三个以上数值列→correlation_heatmap 等），还带极端值检测、主体尺度/全量视图切换、暗色主题联动、PNG 导出、数据驱动的白话解读。

差距在**报告级的叙事编排**：把多张图组织成一条论证链，而不是并列摆图。这是提示词与产物组织层面的工作，不动渲染内核。

### 3.7 安全护栏 —— 本项目独有的、被低估的优势

ChatGPT 的代码沙箱和 Trae 的脚本执行给的是一把几乎无限制的 Python 执行权限。本项目的工具集是**受控**的：11 个内置工具，清洗带硬护栏——缺失值删除比例超过 50% 直接拒绝执行，主数据行数始终不得低于原始行数的 20%。

这不是限制，这是产品级的安全设计。它针对的正是 AI 数据分析最危险的失败模式：模型为了让"清洗成功"而悄悄删掉大半数据，用户拿到一份基于残缺数据集、却看起来完全正常的报告。CW 系产品线里没有看到等价的业务级护栏。

### 3.8 技能/插件生态 —— 价值中，本项目可低成本借鉴

Trae 的技能市场（chart-visualization、data-analysis、consulting-analysis、slides）、WorkBuddy 的 Skill 扩展与连接器生态，本质都是把"某类任务的最佳实践"打包成可复用模块，让 Agent 不必每次从头理解。Trae 官方对此的表述很准确：通用 Agent 基础能力已经很强，但遇到特定工作流时效率瓶颈仍在——每次做数据分析都要重新解释一遍。

本项目已有 10 个受控工具，但仍是固定集合，**没有声明式的技能/插件层**。分析长报告类任务、行业特定口径、特定图表风格这类东西，适合同一个可声明、可加载的技能机制管理。

> **2026-11 复核注（原位改判）**："10 个受控工具"为初稿笔误——`tools/builder.py` 工具清单与 `tests/test_agent.py` 的精确集合断言均为 11 个（含 `run_python_code`），与 3.7 节口径一致。技能/插件层仍缺，该结论维持。

### 3.9 权限治理 / 协作 / 联网搜索

这三项对本项目当前阶段优先级靠后，但决定了能否进企业：FineBI 有行级列级权限、脱敏、血缘；Julius 有共享空间、评论、版本化 Notebook；本项目目前只有单一 `APP_ACCESS_TOKEN` 加滑动窗口限流，无用户体系。联网搜索方面，Julius 有 web search 与美股数据，WorkBuddy 有深度研究与金融数据连接器，本项目完全依赖上传文件，无法补充行业基准、汇率这类外部上下文。

### 3.10 模型选择与成本结构 —— 本项目双向

Copilot in Excel 支持切换 Claude 与 GPT；Julius 支持 SQL/Python/R 与多模型；Claude Code 则绑定 Anthropic 单家。本项目默认 DeepSeek、兼容任意 OpenAI 兼容服务，模型可换、Key 自持、可完全本地私有化。成本可控是优势，但**让用户自己申请 API Key 是最大的首启摩擦**——大厂产品都是订阅即用。这个权衡要明确取舍。

---

## 四、对比结论

### 4.1 本项目的优势（都有代码或配置证据）

1. **分析可控性是结构性优势。** 计划-执行式流程先生成 2–6 步结构化计划，用户可审阅、可批准；重规划器根据真实工具结果裁剪无用步骤、补充后续分析，证据充分时提前结束，达到步骤上限强制汇总。加上 3.7 的安全护栏，本项目在"过程透明 + 结果不被静默篡改"上，明显强于 ChatGPT ADA 与 Trae 的黑盒式一把梭。
2. **图表工程深度领先通用型产品。** 双引擎、十余种图表类型、明确定义的自动选图规则、极端值处理、暗色主题联动、PNG 导出、白话解读——这套东西通用 Agent 要靠临时写 matplotlib 代码，Trae 实测里甚至要用截图插 PPT。
3. **数据接入的格式广度扎实。** CSV/TSV/Excel/JSON/JSONL/Parquet 全支持，加 PDF 表格提取、TXT、Word 表格；UTF-8/GB18030 编码探测、大文件分块流式读取、数值列自动降型。在"文件级"这个维度上不输任何一家，且明确避开了 ChatGPT 的 50MB 天花板。
4. **统计方法边界清晰。** 描述统计、相关分析（含 Pearson p 值）、分组聚合、独立/配对 t 检验、ANOVA、卡方检验、线性回归（R²/RMSE/MAE）逐项列明。通用 Agent 不保证这些方法的质量与适用性检查。
5. **工程完成度高于多数同类项目。** 733 项后端测试、CI 全绿（刚把后端作业从约 23 分钟压到约 1.5 分钟）、Docker 非 root + 健康检查、Render Blueprint、LangGraph Agent Server、LangSmith 全链路 Trace、本地/S3-R2 双持久化、keyring 存 Key、访问令牌 + 滑窗限流、SSE 15 秒心跳与断线自动恢复、断点续跑。这套底子意味着功能迭代可以快，而不是每次都在还技术债。（2026-11 复核注：`def test_` 计数已达 1114 个，本行数字为 2026-09 时点。）
6. **自主可控与私有化。** MIT 许可、可本地运行、模型可换、Key 自持。对大厂清一色托管 SaaS + 订阅 + 强制生态绑定（微软要 Fabric 容量、Tableau 要 Tableau+、WorkBuddy 强绑腾讯生态），这是实打实的差异化。

### 4.2 本项目的不足（按严重度排序）

| 严重度 | 不足 | 事实依据 |
| --- | --- | --- |
| 致命 → 已补 | 无数据库/数仓直连，无 MCP 接入 | 数据库直连：SQLite 文件源 + PostgreSQL 数据源（`query_database`，只读 + 超时 + 行列护栏，CI 真实 service 验证）；MCP：数据面服务已落地（stdio，协议互操作测试）。Snowflake/BigQuery 等其余驱动仍待做（需可验证手段） |
| 致命 → 已补 | 无指标语义层，跨会话口径漂移 | 原状态：口径由 LLM 现推，无任何指标定义文件。现已实现 `metrics.json` 登记 + 规划阶段注入 |
| 高 → 部分已补 | 分析过程不可复用、不可调度 | 已实现 `recipe.py`：一次分析固化为 `analysis_recipe.json`，按上次工具参数重跑而不重新规划（前端"按配方重跑"）；定时调度仍缺 |
| 高 → 已补 | 跨文件 join 不是一等能力 | 原状态：`.merge()` 仅出现在 builder.py 的内部元数据拼接；`pd.concat` 仅用于单文件分块读取与同文件多表提取。现已实现受控的 `join_datasets`（键校验 + 扇出护栏 + 未匹配键诊断） |
| 中高 | 无主动洞察与推送 | 只有被动响应，无定时、无异常预警 |
| 中高 → 部分已补 | 无 Word/PPT/PDF 原生导出 | 已实现 `report_export.py` 导出 Word / PPT（导出菜单下载）；PDF 尚未覆盖（2026-11 注：前端已有浏览器"打印/另存 PDF"入口，缺的是服务端原生 PDF 生成，表述由"暂缺"收窄为此） |
| 中 | 报告级叙事编排弱 | 图表类型选择强，但多图构成论证链的能力不明显 |
| 中 | 无技能/插件层 | 工具集为固定 11 项，无声明式扩展机制 |
| 中 | 无联网搜索与外部数据 | 完全依赖上传文件 |
| 低 | 无用户体系/权限治理/协作 | 仅单一 APP_ACCESS_TOKEN + 限流 |
| 低 | 首启需自备 API Key | 无内置额度或托管模式 |

需要说明一处判断依据：跨文件 join 这条原先是按代码事实下的结论，不是按 README 的描述。`.merge()` 的两处调用都在 `builder.py` 内部，用于把列描述和分组计数拼回主表，不是暴露给用户的工具；`workspace.py` 里的 `pd.concat` 服务于分块读取和同一文件内多表提取。所以"多份季度分表合并成年度总表"这类任务当时只能靠 LLM 写沙箱脚本临时实现，而不是一个受控、可复现的一等工具——这正是后来补上 `join_datasets` 的理由。

---

## 五、补齐方向与优先级

排序依据是"是否卡住真实数据场景"，而不是"是否容易做"。

### P0：不补就进不了真实数据场景

**P0-1 数据库/数仓直连接入（以 MCP 为实现方式）**

写一个只读 MCP server，暴露 `list_tables`、`describe_table`、`run_query`、`sample_rows`。安全设计照搬行业已验证的做法：专用只读角色、禁止 DDL/DML、强制 LIMIT、查询超时、SQL 哈希审计日志、schema 结果缓存（TTL 5 分钟）、连接池配合数仓自动挂起。

为什么这个做法：一次性同时解决两个问题——本项目能读外部数据源，同时本项目的工具集也能以 MCP 形式暴露给其他 Agent 调用。Python 生态有官方 MCP SDK，本项目已经是 FastAPI + LangGraph，接入成本低，属于现有架构红利。

**P0-2 指标语义层（轻量版）**

定义一份可版本化的指标文件，字段包括指标名、业务口径说明、计算表达式、依赖列、更新频率。分析流程改为：先解析用户问题命中的指标，把定义注入 LLM 上下文，再执行。收益是口径不再漂移，且让 P0-3 的异常检测有稳定基准。

不要一上来做 FineBI 那种指标中心（原子指标、衍生指标、动态计算、血缘追踪）。那是企业级工程量，对当前阶段是过度设计。

**P0-3 跨源合并成为一等工具**

把 join/union 做成受控工具：指定左表、右表、连接键、连接类型、预期输出行数区间，超区间报警而非静默通过。理由来自 Trae 实测的结论——数据分析师 60% 以上时间花在前置体力活（多份格式不一的表、脏数据、脚本合并），而这恰是"受控工具"比"让 LLM 现写沙箱脚本"更可靠的场景。本项目已有受控工具集的架构，加一个工具的边际成本远低于重建。

### P1：决定能不能被业务真正用起来

**P1-1 Word/PPT 导出——已落地**

`report_export.py` 从现有结构化 Markdown 报告 + 图表资产生成 Word / PPT（python-docx / python-pptx），导出菜单直接下载。PDF 暂未覆盖，属纯增量。

**P1-2 分析流程可复用——重跑已落地，调度未做**

`recipe.py` 把一次分析固化为 `analysis_recipe.json`（步骤 + 工具参数），前端"按配方重跑"按上次参数直接执行、不重新规划。定时执行与结果推送仍未做。

**P1-3 主动洞察与异常推送**

定时跑指标、检测异常与趋势变化、生成归因摘要、推送出去。对标 Tableau Pulse。做之前先把"什么算异常"用指标层定义清楚，否则推送会变成噪音。指标层现已支持可执行表达式（`compute_registered_metrics`），这一前置条件已就绪。

**P1-4 报告级叙事编排**

让多图组织成论证链：先整体、再拆解、再效率、最后异常。属于提示词工程与产物组织，成本低、观感提升明显。`facts.py` 的可核验事实附录（报告引用工具产出的数字而非模型现编）已为它铺了地基。

### P2：决定能不能进企业

**P2-1 用户体系与权限治理**——多租户、行级/列级权限、敏感字段脱敏、操作审计。当前单一 token 的形态无法进任何有合规要求的组织。

**P2-2 协作能力**——共享空间、评论、分析版本化。

**P2-3 联网搜索与外部数据**——行业基准、汇率、公开数据集，解决"只有内部数据、缺乏参照"的问题。

**P2-4 首启体验**——内置试用额度或托管模式，降低自备 Key 的门槛。这是个取舍题：保留自持 Key 的成本优势，同时给一条零配置的试用通路。

### 技术架构方向（三条主线）

1. **工具层 MCP 化。** 既是接入外部数据与工具的入口，也是对外暴露本项目能力的出口。一次投入两个方向的收益。
2. **语义层 + 缓存 + 血缘。** 指标定义可版本化，查询结果可缓存，分析引用可回溯。这是把"能分析"升级为"分析可信"的关键。
3. **成本与并发控制。** 查询预算（扫描行数、墙钟时间）、元数据缓存、失败熔断——这些在数据源从文件换成数仓后会立刻变成刚需，因为每一次 Agent 循环都在烧数仓算力。

### P0-1 的推进节奏：为什么先落 SQLite，网络型数仓还在后面

P0-2 与 P0-3 属于"在既有架构里加一层"，改动封闭、可用现有测试体系完整验证，已先行落地。P0-1 的完整形态涉及三件必须先定的事，而且都是产品决策而非编码决策：

1. **驱动与依赖。** 支持哪几家（只做 Postgres，还是同时铺 Snowflake/BigQuery/Databricks）直接决定要引入多少重量级依赖，以及测试如何在 CI 里跑起来——CI 上现在没有可连的数据库实例。
2. **凭证归属。** 数据库连接串放哪：跟当前 API Key 一样进操作系统凭据库，还是走管理员集中配置？前者适合个人自托管，后者适合团队部署，两条路的接口设计不同。
3. **权限模型的起点。** 参考架构要求专用只读角色、行数上限、查询超时、SQL 审计；这套东西一旦落地就必须同时想清楚"谁来配、配错了怎么发现"，否则会变成一个新的运维黑洞。

因此 P0-1 按"先定型工具层与护栏，再接驱动"的节奏推进，两步都已完成：

1. **工具层与护栏**（SQLite 后端先行）：`sqlite_source.py` 实现只读访问、语句白名单、
   查询超时、行列上限、表结构发现；共享护栏抽到 `sql_guard.py` 供两个驱动共用。
2. **PostgreSQL 驱动**：`postgres_source.py` + `query_database` 的 `source` 参数。
   凭证走环境变量 `DATA_AGENT_DATABASE_URL`（与 S3/R2 凭证同一约定）；只读在服务端
   `default_transaction_read_only` 生效并回读确认；超时用服务端 `statement_timeout`；
   CI 以 postgres:16 service container 跑真实集成测试（连不上时自动跳过）。

尚未做的与前置条件：

- **MCP 对外暴露（数据面）已完成**：`mcp_server.py` 以 stdio 传输开放 4 个工具
  （list_sessions / open_dataset / inspect_data / sql_query），把"受控的数据访问"
  而不是内部工具原样透给其他 Agent；护栏与工作区内工具同源，测试用官方 SDK 的
  内存客户端走真实协议。完整分析流程（分钟级 LLM 长任务）不进 MCP，仍走 HTTP API。
- **其余数仓驱动**（Snowflake/BigQuery/Databricks）：沿 `postgres_source` 的模式即可，
  但需要真实凭证或模拟层才能验证——没有可验证手段就不该合入。

---

## 六、竞品实地复核（2026-11-17）

### 6.1 复核方式与可信度边界

与 2026-09 初查（依赖二手评测与官网宣传页）不同，本次用自研爬虫（web-crawler）对六类产品的**官方一手页面**做了实地抓取与逐句核对：静态 HTTP 优先，被拦则换 TLS 指纹隐身（`--stealth`）或无头浏览器渲染（`--render-js`），遵守 robots、限速、不登录不绕过访问控制；每个站点的请求预算封顶，抓不到就如实记"无法核实"。每条结论均标注【原文】（逐字引文 + URL）或【推断】。原始抓取产物（HTML / 正文 txt / run_report）存于 `D:\XIAOMAO\Projects\web-crawler\.research\agent-data-analysis\`，不在本仓库入库。

抓取可达性本身也是数据，而「官方文档换个入口」往往就是解法：docs.julius.ai 子域全站 HTTP 526（Cloudflare 证书故障），但官方文档实际在 **julius.ai/docs**——渲染后直接给出 `/docs/llms.txt` 全量目录，按目录取回 Tools / Data Connectors / Containers 三页原文；tableau.com 营销站对纯 HTTP / TLS 隐身 / 无头浏览器三层全部 403，但 **help.tableau.com 帮助门户经无头渲染可打开**，并拿到 Tableau 官方 `llms-full.txt`（6.1 MB 全量文档），其 MCP 章节又指向官方 `tableau.github.io/tableau-mcp/`（Hosted MCP / OAuth 2.1 原文）。Julius 与 Tableau 的结论因此都从「二手单一来源」升级为「官方一手页」。

### 6.2 抓取矩阵（页面级结果）

| 站点 | 关键页面 | 结果 | 方式 |
| --- | --- | --- | --- |
| help.openai.com | Data analysis with ChatGPT（8437071） | 403 → **渲染成功**（45 行全文） | `--render-js` |
| platform.openai.com | /docs/guides/tools | 200（28.6k 字） | 静态 |
| docs.anthropic.com | Tool use overview | 200（11.5k 字；canonical 指向 platform.claude.com） | 静态 |
| www.anthropic.com | /news 索引 | 200；全文检索 excel/spreadsheet **0 命中** | `--stealth` |
| learn.microsoft.com | Power BI Copilot overview（页面自述 Last updated 2026-08-24） | 200（15.8k 字） | 静态 |
| support.microsoft.com | Copilot in Excel get-started | 200（5.0k 字） | 静态 |
| learn.microsoft.com | /en-us/dax/dax-copilot | 200（9.9k 字） | 静态 |
| www.julius.ai | 首页 / chat-with-your-data / data-connectors | 200（`--stealth`；正文在 `<main>`，首 `<article>` 仅 111 字） | `--stealth` |
| docs.julius.ai | 全站 | **HTTP 526**（证书故障，三个入口均失败） | — |
| julius.ai/docs | 文档索引 + llms.txt | **渲染成功**；`/docs/llms.txt` 13 KB 全量目录 | `--render-js` |
| julius.ai/docs/*.md | Tools / Data Connectors / Containers | 200（官方 Markdown 原文） | `--stealth` |
| tableau.github.io/tableau-mcp | 官方 MCP 文档首页 | 200（Hosted MCP / OAuth 2.1 原文） | 静态 |
| www.trae.ai / docs.trae.ai | 首页 / solo / solo/skills | 200；正文在内嵌 Quill delta JSON，爬虫 delta 解析后 0 字 → 1.8–2.5 万字符 | 静态 |
| workbuddy.tencent.com | 首页 | JS 壳 → **渲染成功**（正文 + JSON-LD featureList） | 渲染 |
| www.finebi.com | 首页 / 指标中心 / 数据连接 | 200（正文完整，UTF-8 中文无损） | 静态 |
| www.tableau.com | /products/tableau-pulse | **403**（纯 HTTP、`--stealth`、渲染三层全失败） | — |
| help.tableau.com | 旧 Pulse/Agent URL → 帮助门户根 | 旧 URL **404**；根页为 JS 垫片 → **渲染成功**（3.2k 字，且页面提供 llms.txt 入口） | `--render-js` |
| docs.salesforce.com | /tableau/current/en-us/pro/llms-full.txt | 200（**6.1 MB 全量官方文档**；Agent/Pulse/metric 原文可检索） | 静态 |

### 6.3 逐家复核结论（与 2026-09 初查的差异）

#### ChatGPT 数据分析（OpenAI 官方帮助中心，2026-11-17 渲染全文）

- 【原文】执行载体确认为"有状态的 Jupyter notebook 环境"，"can display pandas DataFrames as interactive tables"。
- 【原文】交互式图表只覆盖四类："Interactive charts are supported for bar, line, pie, and scatter charts. Other chart types may be returned as static images."——本项目双引擎 12+ 图型的判断（4.1 第 2 条）**维持并加强**。
- 【原文】沙箱隔离是官方口径："The Python environment used for data analysis cannot make external web requests or API calls."——与本项目"受控工具 + 只读 SQL"是两条路线（见 6.4 第 3 条）。
- 【原文】官方承认的失败模式："ChatGPT may choose an analysis method or chart type that does not match your intent on the first try"；扫描件/图片表格 "may not reliably extract exact values"；官方建议 "review the generated code, outputs, and assumptions before relying on the result"。**2026-09 文档"给出看起来极其自信的错误答案"的判断，官方现在自己写了缓解指引**——本项目的事实抽取 + 逐数字核验（`facts.py` / `verification.py`）正是这个方向的工程化，差异化叙事可引用官方原文。
- 【原文】数据接入：上传文件 + 连接源（"Google Drive, OneDrive, and SharePoint"）；未直连数据库——初查判断维持。

#### OpenAI 平台工具面（platform.openai.com/docs/guides/tools）

- 【原文】工具清单 10 项（Function calling / Web search / Remote MCP / **Skills** / Shell / Computer use / Image generation / File search / Tool search / Programmatic Tool Calling）；Shell 是 "Run shell commands in hosted containers"；Skills 是 "Upload and reuse versioned skill bundles"。平台层趋势明确：**托管执行 + 版本化技能包**——本项目 recipe（配方重跑）与未来的技能层方向与此一致。

#### Anthropic（docs.anthropic.com Tool use overview）

- 【原文】数据分析执行载体："Run Python and Bash code in a sandboxed container"，能力定位 "analyze data and generate files"。
- 【原文】已知风险官方原话："Claude (particularly Claude Sonnet) might guess values you didn’t supply"（参数缺失时臆造；注意原文是弯引号，逐字引用以此为准）。
- 【无法证实】"Claude for Excel"：anthropic.com/news 全文检索 excel/spreadsheet 0 命中——2026-09 文档未收录此条，维持不引入；如需引用必须先找到官方一手页。

#### 微软 Power BI Copilot（learn.microsoft.com，页面自述 2026-08-24 更新）

- 【原文】语义模型准备是官方前置条件："Prepare your semantic model before you use Copilot... Without preparation, Copilot can misinterpret the data and return generic or inaccurate results."——3.3 节"语义层决定分析可信度"的判断被微软官方原话确认。
- 【原文】门槛口径：F2+ 或 P1+ 容量（"Trial capacities and free SKUs aren't supported"）；租户级开关；主权云不支持；单条 prompt 上限 10,000 字符；同 prompt + 未变模型 24 小时内返回缓存答案。
- 【原文】新增能力（2026-09 初查未覆盖）：独立 Copilot（全屏、跨 item 找数据）与应用内 Copilot 均为 preview，支持 "verified answers prepared by app authors"——人工策展问答对，与本项目指标登记的"口径统一"同思想，可作为 metrics 演化参考（别名/示例问答）。
- 【原文】计费："Token consumption drives billing for Copilot in Fabric"，下游 DAX/刷新/订阅另计——成本结构判断维持。
- 【原文】DAX Copilot 硬限制：模型超 500 表 / 1,000 列 / 3,000 度量 / 2,000 关系即降级；度量描述只用前 200 字符；执行是 Run → Keep/Discard，失败自动重试一次。
- 【原文】主动推送唯一证据仍是"报表订阅可带 Copilot 摘要"（用户配置后推送），agent 自主监控未见——3.4 节判断维持。

#### 微软 Excel Copilot（support.microsoft.com）

- 【原文】三态工作流："three modes: edit, plan, or chat"，plan 模式 "creates a step-by-step plan" 供用户 "review and confirm" 后执行，执行中 "evaluate whether the outcome matches"；chat 模式只读不改工作簿。
- 【原文】产出保持原生可编辑："Your content stays editable"，直接用 "tables, charts, PivotTables, and formulas"。
- 【原文】技能沉淀：可把重复流程固化为 custom skill，甚至 "record a skill by demonstrating a task"（演示一遍即录制）——技能层（3.8）的第三家佐证。

#### Julius AI（产品页 `--stealth` + 官方文档 `julius.ai/docs` 实取）

`docs.julius.ai` 子域 526 不可用，但官方文档实际在 **`julius.ai/docs`**：该页渲染后给出 `/docs/llms.txt` 全量目录（13 KB），按目录取回了 Tools / Data Connectors / Containers 三页官方原文。

- 【原文】工具化执行："it calls tools: running code, querying data, researching the web, and building finished deliverables"；每步在聊天里可见并展开（"Generated code / Searched internet / Created slides"）——初查"代码可见可改"的判断获得官方原文支撑。
- 【原文】代码工具："Julius writes **Python** or **R** and runs it in a secure cloud container… You can read the code, copy it, and see its output inline."；支持 apply patch（"surgical edits… with a diff of what changed"）。
- 【原文】SQL 直连清单（官方文档）：PostgreSQL、MySQL、SQL Server、Snowflake、BigQuery、Supabase、Google Ads，另有独立页面覆盖 **Databricks**；"Julius writes and runs SQL against it directly"。产品页另列 Google Drive / OneDrive / Meta Ads。初查 3.1 节判断维持，且比初查多出 Databricks。
- 【原文】查库前先探 schema："Julius can search the schema to find the right tables and columns, so it writes correct SQL instead of guessing."——这是本项目 `inspect_data` 之外值得补的交互（先看表结构再写查询）。
- 【原文】模糊需求先反问："Julius may pause to ask you a quick clarifying question rather than guess"——与 Tableau Agent 的误读风险提示同方向。
- 【原文】容器生命周期："all free and paid user containers are live for up to 1 hour of inactivity. After 1 hour… spun down."——初查"沙箱随会话结束重置"可精确化为「1 小时无活动回收」。
- 【原文】合规（产品页）："SOC 2 Type II"、"TX-RAMP"、"GDPR compliance actively in progress"、"your data never leaves your cloud"（厂商声明）。
- 【改判】定时调度：当前官方文档目录（llms.txt 全量条目）**没有任何 Scheduled Runs / recurring 页面**；初查"Julius 有 Scheduled Runs"降级为「2026-09 二手来源，2026-11 官方文档未提及」。

#### Trae SOLO（docs.trae.ai，内嵌 JSON 实取）

- 【原文】文档已改版为合并后的 "all-new TRAE"（"supports the entire development lifecycle"）——初查按"SOLO 独立端"描述的形态已过时，引用时注意。
- 【原文】技能体系实锤：`SKILL.md` 定义技能（指令/脚本/资源），项目级 `.trae/skills/` 与全局 `~/.trae/skills` 目录，Marketplace 支持 zip/.skill 上传安装，"SOPs can be encapsulated as skills"，加载侧 "Dynamic on-demand loading" 省 token——3.8 节"声明式技能层"从评价变为可直接对标的设计（文件格式 + 双目录 + 市场 + 按需加载）。
- 【原文】交付物："My Artifacts" 统一管理 "slide decks, documents, images, videos, reports"；Automation 承接重复任务（"generating reports"、"agents for continuous execution"）。
- 【推断】数据分析本身仍是"Agent + 技能"泛化路线，无指标/语义层证据——与初查一致。

#### WorkBuddy（腾讯，渲染后正文 + JSON-LD 实取）

- 【原文】形态："全场景 AI 办公工作台"，"自主规划、调用工具、生成文件"。
- 【原文】数据分析能力清单（页面 JSON-LD featureList）："Analyzes CSV and spreadsheet data"、"generates charts"、产出 "Word, Excel, PowerPoint, PDF or Markdown"。
- 【原文】自动化与技能："Schedules recurring tasks hourly, daily, weekly"；"installable skills and Model Context Protocol (MCP) servers"。
- 【原文】治理：RBAC + 操作审计；ISO 27001、SOC 2、等保三级；"pause high-risk operations for confirmation"。**初查 3.7 节"CW 系产品线里没有看到等价的业务级护栏"需要收窄：WorkBuddy 官方页宣称了高风险操作确认与审计，但清洗比例类硬护栏仍未见**——护栏差异化叙事保留，表述改为"官方页未见数据级硬护栏"。
- 【推断】面向文件级办公分析，连接器以腾讯生态为主——初查判断维持。

#### FineBI（帆软，静态全取）

- 【原文】7.5 版本叙事："指标驱动的数据智能平台"、"AI原生数据分析平台"；首页明确列出一手能力"指标中心、指标拆解树、语义模型、全链路血缘"，指标中心页补充"统一指标定义、口径、权限、血缘和复用关系"与审批发布。
- 【原文】连接器："支持连接50+数据库"、"一键获取100+应用数据源"，含 GaussDB / Kingbase / 达梦 / OceanBase / Gbase 8A / 星环等国产库。
- 【改判】初查 2.6 节把 Data Agent 写成 "FineBINEXT 的 Data Agent"（FineBI 内置模块）；实抓页面显示 Data Agent 是**帆软另一 AI Agent 产品与 BI 7.5 对接**（"BI7.5支持对接帆软的AI Agent产品"），不是 FineBI 原生子模块——归因修正：FineBI 的 AI 价值依附指标中心底座这一判断不变，但"Data Agent 闭环"是产品族协同而非内置能力。
- 【推断】"AI 问数效果取决于指标与权限建设"——与本项目 metrics.json 的定位互为印证。

#### Tableau（官方帮助门户 + llms-full.txt，一手结论）

营销站 403，但官方帮助门户经无头渲染取得入口，并拿到 `llms-full.txt` 全量文档（`docs.salesforce.com/tableau/current/en-us/pro/llms-full.txt`，6.1 MB）。以下为原文口径：

- 【原文】Tableau Agent 覆盖端侧：Desktop 2025.1+、Cloud、Server 2025.3+；能力是"Explore your data / Create visualizations / Create and explain calculations"，可按自然语言做时间序列分析、选图型、生成与解释计算字段（"What month had the largest growth..."、"Explain the 'Days to Ship Actual' calculation"）。
- 【原文】Dashboard 内三能力：Dashboard Q&A（"What/Why" 追问，可下钻到数据源）、Dashboard Overview（扫描看板生成摘要）、Dashboard Insights（逐 viz 生成摘要洞察）。2026.3 起 Desktop 只能在 Dashboard settings 配置，且需 Tableau Cloud+ / Tableau+，Server 站点不可用；需同时授予 AI Access 与 Full Data Query 权限。
- 【原文】洞察类型是受数据形态约束的："Not all insight types will be evaluated for all vizzes. For example time comparisons won't be available unless there's a continuous date dimension present."——这与本项目"图表解读只在数据支持时才下结论"同思路。
- 【原文】官方最佳实践直接点出 LLM 误读风险："Duplicate field names... can confuse Tableau Agent"；瀑布图用 Gantt + 负值构造，"The LLM receives the negative measure and will therefore misconstrue the direction of the mark"——**厂商自己承认图表实现细节会误导模型**，可作为本项目"数据准备 + 受控工具 + 核验"路线的外部佐证。
- 【原文】信任与计费：Cloud 侧建在 Einstein Trust Layer 上（数据不写入 LLM、不用于训练）；Server 侧直连 LLM 提供商、不走 Trust Layer；AI 功能当前 **unmetered**（不再消耗 credits）。
- 【原文】Pulse："the metrics you create are used to generate insights about your data. These data insights are sent directly to users who follow the metrics"——"人找数据 → 数据找人"的官方定义；指标定义（metric definition）是核心对象，legacy metrics 已退役（Cloud 2024-02 / Server 2024.2，不自动迁移）。
- 【原文】官方承认的限制：Pulse 与 multi-fact relationship 模型可能不兼容——"You may be prevented from creating a metric definition, or any metrics that are created may be blank. This is not expected behavior but there is no currently planned fix yet."；导出侧 Pulse 对象只进 PNG 快照、不进 PDF。
- 【原文】**Hosted Tableau MCP 成立**：llms-full 的 Slackbot 章节写明 "After you connect the Tableau Model Context Protocol (MCP) and Slackbot…"，并链接官方 `tableau.github.io/tableau-mcp/`；该官方文档站原文："Hosted Tableau MCP — Tableau MCP as a managed cloud service — no installation required. OAuth 2.1 secured and available to all Tableau Cloud customers."——初查的 Hosted MCP / OAuth 2.1 / 云托管三条全部获得一手确认。
- 【维护差】2026-09 初查中 "Agent Actions" 与 "2026.2 推出" 两条在 llms-full 与 MCP 官方文档中均未检索到对应原文（可能是营销页/发布说明的时间线），**继续标单一来源**，不与上述一手结论混写。

### 6.4 对路线图的增量影响（按优先级）

1. **定时调度与主动洞察（P1）优先级上调，证据变硬**：WorkBuddy "Schedules recurring tasks hourly, daily, weekly" + Trae Automation + **Tableau Pulse 官方原文**（"insights... sent directly to users who follow the metrics"）——三家一手确认把"自动化重复分析/主动推送"当一等能力；Julius 的 Scheduled Runs 在 2026-11 官方文档中已找不到，不再作为佐证。本项目 recipe 已有重跑语义、指标层已能算，补"定时触发 + 指标异常检测 + 推送"是当前最短路径；Tableau 的 multi-fact 限制也提醒我们：指标定义与数据模型不匹配时要显式拒绝，而不是产出空白指标。
2. **指标语义层（P0-2 已落地）继续加厚**：FineBI 7.5 全链路血缘与审批发布、Power BI verified answers、微软官方"不准备语义模型就误读数据"——轻量版 metrics.json 之后补了两步：**已验证问法 `examples` 已落地**（2026-11，规划提示词随口径注入，对标 verified answers）；把 metrics 文件纳入溯源链（口径本身可审计）仍待做。
3. **执行路线差异要写成叙事**：ChatGPT 官方明确沙箱禁外呼、Anthropic/OpenAI 平台走托管容器——"沙箱一把梭"是大厂主线；本项目"受控工具 + 只读 SQL + 逐数字核验"是结构上不同的第二条路，且官方建议"review the generated code"与本项目的溯源/核验能力一一对应。定位章节的"每一步都能被验证"可以直接引用官方原文背书。
4. **Snowflake/BigQuery/Databricks 驱动（P0-1 尾巴）维持待办**：Julius 官方文档确认了 Snowflake / BigQuery / Databricks 的真实需求（连接器页 + SQL 直跑原文），但仍受"无真实凭证就无法验证"约束，节奏不变；可先做 schema 探查交互（Julius 的 "search the schema… instead of guessing" 是本项目 `inspect_data` 之后值得补的一步）。
5. **技能层（P2）有了可抄的作业**：Trae 的 SKILL.md + 项目/全局双目录 + 按需加载是完整设计；本项目 recipe → 技能的自然演化路径是"把 recipe 里的工具参数模板抽成可版本化文件"，不必新起炉灶。
6. **Excel/PPT 之外的交付**：WorkBuddy 官方页把 PDF 列为交付物之一；本项目服务端 PDF 生成仍是纯增量，优先级不变（中高）。
7. **中文/多语言是相对机会**：Power BI 官方 "multilingual use isn't officially supported"——本项目全中文报告 + 白话解读在中文业务场景是结构性优势，可写进定位。
8. **Claude for Excel 不引入**：官方站无法证实，按"无可证不收录"处理。

---

## 七、定位建议

不建议把"补成 Julius 或 Tableau"当目标，那样会在数据连接广度和治理深度上同时和资源充足的大厂拼消耗，赢面很小。

本项目真正的位置是：**面向业务团队的可控型数据分析工作台**。可控性——计划审阅、受控工具、安全护栏、断点续跑、私有化部署、模型可换——已经落地为代码事实，而不是路线图承诺，这是通用型大厂产品结构上做不到的：它们的黑盒决策链是为了降低门槛而刻意选择的，改不动。而数据库直连、指标口径、流程复用、主动洞察这几项，属于工程问题，补上就是完整的竞争力。

一句话：**别在"什么都能做"上竞争，在"每一步都能被验证、被解释、被推翻"上竞争。** 后者才是数据分析场景真正的采购理由。2026-11 复核给这个定位补了官方背书：ChatGPT 官方让用户 "review the generated code, outputs, and assumptions before relying on the result"，微软把语义模型准备写成前置条件——"可验证"不是差异化修辞，是两家头部厂商自己承认的前置需求。

---

## 附：事实来源

产品能力与数据均取自公开资料，重点结论均有两处以上来源或官方文档支撑。

### 2026-09 初查（二手评测与官方宣传页）

- Trae SOLO：量子位实测（36Kr，2026-04-03）；CSDN 转载（2026-03-31）；Trae 战略分析师《不止是写代码｜如何用 TRAE 进行复杂数据分析》（火山引擎开发者社区，2026-02-09）；腾讯新闻实测（2026-04-08）；Trae 官网 trae.cn
- ChatGPT Advanced Data Analysis：Coursera 官方说明；letdataspeak 2026 实操指南；datafield.dev《Working with AI Tools Effectively》
- WorkBuddy：WorkBuddy 实战手册·进阶版；WorkBuddy 功能介绍（v4.22 探索功能、连接器、八大核心能力，ima.qq.com 知识库）
- Julius AI：julius.ai 官方文档《Data Connectors》；Julius 官网与 landing page；The Rundown AI 产品评测；AIToolCurator 评测
- Copilot in Excel / Power BI Copilot：Microsoft Learn《Copilot in Excel》培训模块；Microsoft Support FAQ；products.office.com/excel；IT168《AI+BI 工具怎么选》（2026-06-24）；beyondtheanalytics（2026-04）
- Tableau / FineBI：IT168 同篇横向评测；beyondtheanalytics；diffstudy 2026 对比；thesys.dev 对比
- MCP 数据库访问参考架构：InfiniSynapse《MCP for Databases: A 2026 Guide to Agent Data Access》；dataworkers.io《MCP Data Stack》；querio.ai 实操指南；mcprepo.ai 集成指南

### 2026-11 实地复核（官方一手页面，web-crawler 实抓；原始产物在 web-crawler 仓库 `.research/agent-data-analysis/`）

- help.openai.com/en/articles/8437071-data-analysis-with-chatgpt（渲染全文，"Updated: 28 days ago"）
- platform.openai.com/docs/guides/tools（工具清单 / Shell hosted containers / Skills versioned bundles 原文）
- docs.anthropic.com/en/docs/build-with-claude/tool-use/overview（code execution sandbox / "might guess values" 原文；canonical：platform.claude.com）
- www.anthropic.com/news（索引全文检索 excel/spreadsheet 0 命中 → Claude for Excel 不收录）
- learn.microsoft.com/en-us/power-bi/create-reports/copilot-introduction（容量门槛 / 语义模型准备 / verified answers / token 计费 / 24h 缓存原文，页面自述 2026-08-24 更新）
- learn.microsoft.com/en-us/dax/dax-copilot（500 表等降级阈值 / 200 字符描述截断 / Run→Keep 原文）
- support.microsoft.com/en-us/Excel/copilot/get-started-with-copilot-in-excel（edit/plan/chat 三态 / 原生对象 / custom skill 录制原文）
- www.julius.ai/product/data-connectors 与 /product/chat-with-your-data（连接器清单 / "Every insight comes with the underlying code" / SOC 2 原文）
- julius.ai/docs（渲染）与 /docs/llms.txt 全量目录；/docs/get-started/tools.md、/docs/data-connectors/overview.md、/docs/get-started/containers.md（工具化执行 / Python·R 容器 / SQL 直连含 Databricks / schema 先探 / 1 小时空闲回收原文；docs.julius.ai 子域 526 已被该入口绕过）
- docs.trae.ai/solo 与 /solo/skills（all-new TRAE 改版 / SKILL.md / .trae/skills 双目录 / Marketplace / SOP 技能化原文，正文取自内嵌 Quill delta JSON）
- workbuddy.tencent.com（渲染后正文与 JSON-LD：CSV 分析 / Word/PPT/PDF/Markdown 交付 / 每日周定期任务 / skills+MCP / RBAC+审计 / ISO27001·SOC2·等保三级 原文）
- www.finebi.com 首页与 /functions/indicator-center、/functions/data-connect（7.5 指标中心 / 审批发布 / 全链路血缘 / 50+ 数据库含国产库 原文）
- Tableau：help.tableau.com/current/pro/desktop/en-us/help.htm（门户经渲染取得 llms.txt 入口）；docs.salesforce.com/tableau/current/en-us/pro/llms-full.txt（6.1 MB 全量官方文档，Tableau Agent / Pulse / 指标定义 / 导出限制原文）
- 可达性负证据：www.tableau.com 营销站三层 403、help.tableau.com 旧 Pulse/Agent URL 404、docs.julius.ai 子域 526（均已用官方替代入口补证）；Claude for Excel 三个官方直连 URL 全部 404、新闻索引检索 0 命中

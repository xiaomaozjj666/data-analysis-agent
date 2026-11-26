# Changelog

本项目的所有显著变更都记录在本文件中。
格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循[语义化版本](https://semver.org/lang/zh-CN/)。

项目自 2026-07 起以 trunk 方式开发（`main` 即最新可用版），本文件从 v1.0.0
起维护；更早的历史按月份归档在版本条目里。

## [2026-11-26]

### 新增

- **会话级持久分析内核**：`SessionKernel` 快照（目标/计划/完成步骤/当前节点/工具
  时间线）随 `session.json` 原子落盘并进入 `GET /api/sessions/{id}`；流式分析按
  节点与工具事件增量更新，切换或重启后仍能恢复计划面板与时间线。
- **定时任务面板**：新增 `/api/automation/tasks`（列表/历史/启停/立即运行/试运行）
  与前端 `SchedulePanel`，侧栏与命令面板均可进入；锁定返回 409、配置损坏红线
  422，试运行不落历史。
- **中英文界面**：零依赖 i18n，zh-CN 默认（文案与既有界面逐字一致）、en-US 增量，
  localStorage 记忆，设置面板与命令面板可随时切换。
- **受限 Python 沙箱支持 scipy / scikit-learn / statsmodels**：导入白名单新增三个
  可选统计库（含子模块），新增 `[stats]` extra（`pip install ".[stats]"`；scipy 与
  scikit-learn 原本就是核心依赖，statsmodels 随该 extra 安装）。未安装时 import 返回
  `pip install "data-analysis-agent[stats]"` 的可操作提示，而不是裸
  `ModuleNotFoundError`，并保留 `ImportError` 类型以便父进程翻译。

### 质量

- **前端覆盖率门禁抬高**：新增 App 行为测试（上传/示例/会话切换/命令面板/错误
  恢复/候选数据集/审批重跑/追问/恢复轮询/重放/核验重载/访问门禁/手势），App.tsx
  行覆盖 35% → 91%；全量实测 lines 77.1% / statements 74.3% / functions 66.9% /
  branches 61.7%，阈值同步提高到 75.5 / 72.5 / 65 / 59.5。
- **前端覆盖率第二轮补齐**：为此前完全没有测试的组件/工具补行为测试
  （DatasetOverview、ToolTraceItem、ConversationThread、PlanPanel、HistoryPanel、
  EmptyWorkspace、CodeBlock、ReportParts、utils/api、utils/notify），错误路径
  （请求非 2xx、上传失败/取消、超时、剪贴板失败、任务启停 404）与交互分支一并
  覆盖；全量实测 lines 84.7% / statements 82.3% / functions 79.4% / branches
  72.6%，App.tsx 单文件 98.4% / 98.8%，阈值提高到 83 / 80.5 / 77.5 / 70.5。
- **后端覆盖率余量**：补 `audit_state` 损坏/缺失状态文件、`_downloads` 打包
  中途失败、`render.primitives` 空数组/全 NaN/零宽度、`mcp_server` 超限拒绝与
  未配置数据源、`chart_sampling` 退化输入、API 预热与静态目录回退；本地无
  Postgres 实测 97.00% → 97.54%，门禁 97 → 97.4（CI 含真实 Postgres 更高）。

### 修复

- **定时任务并发启停丢更新**：`add_spec` / `remove_spec` 与新增的
  `set_spec_enabled` 都是「读全量 → 改一条 → 写全量」，原子写只能防半截文件、
  防不住旧快照覆盖。三者统一在进程内 RLock 内串行化，路由启停改走新函数；
  回归测试用屏障强制两个线程同时持有旧快照，验证最终两条任务都在。
- **配置/文档与门禁口径对齐**：CI 注释里的前端阈值仍是上一版数字（68.5/66.5），
  README 的后端门禁仍写 97；现与实际阈值、实测值保持一致。
- **会话文件被外部改坏时不再 500**：`session.json` 被改成数组/字符串/null、
  `created_at` 变成字符串、`chat`/`pending_plan`/`last_result.trace` 类型错乱
  时，恢复链路逐字段降级为空值，仍能打开会话；进程在分析途中退出时，manifest
  里的 `running` / `cancelling` 及内核状态统一降级为 `idle` 并清空
  `current_node`（计划、已完成步骤与工具轨迹保留），避免前端永远轮询一个
  不可能推进的节点。回归覆盖"重启恢复 + 外部改坏 + 分析中刷新"。
- **带取消信号的请求缺少超时**：轮询/恢复请求传入 `AbortSignal` 时此前会跳过
  超时兜底，服务端半开连接会让界面永久停在"检查中"；现在两类请求统一保留
  `timeoutMs`，外部中止转接到内部 controller，并在 `finally` 移除监听，避免
  长生命周期 signal 上堆积回调。已取消的上传不再创建 XHR。
- **i18n 静态检查**：新增字典键集合 / 占位符的一致性校验、英文值残留中文检测、
  以及源码中 `t("...")` 字面量 key 的存在性扫描，新增文案漏翻会让前端测试直接
  失败（语言名称如"简体中文"加入白名单）。
- **定时任务稳定性回归**：真实服务链路下验证两个并发触发只执行一次（另一个
  409、历史仅一条）、停用任务不参与调度但"立即运行"仍按显式操作执行、配置
  损坏返回 422、试运行不落状态。

### 安全

- **白名单扩展没有放松 I/O 边界**：统计库的任意路径读写（`scipy.io`、
  `statsmodels.iolib`、`load_svmlight_file`…）、网络抓取（`fetch_*`、`webuse`、
  `get_rdataset`、`scipy.datasets`）与显式并行入口（`Parallel` / `delayed` /
  `joblib`）在 AST 与子进程运行时两层拦截（别名还原后按模块前缀判断）；
  星号导入一律拒绝（`from scipy import *` 会把 `io` 直接绑成变量）。导入
  sklearn 后 joblib 默认后端固定为 threading，`n_jobs>1` 不再另起子进程，
  15 秒超时与 2GB RSS 护栏继续覆盖整次计算。

## [2026-11-25]

### 新增

- **`data-agent clean`：清理 runs/ 旧会话目录**。默认只清理 CLI / MCP 产生的
  `cli_*` / `mcp_*`（超过 7 天未修改、始终保留最新 20 个），支持 `--dry-run`
  预览、`--include-web` 纳入 `api_*`；`--days 0` 必须显式加 `--all`。设置
  `DATA_AGENT_CLI_RETENTION_DAYS` 后，每次 `analyze` 也会自动清理（保留最新 10 个）。
- **`data-agent --version`**：报 bug 时可自证版本。

### 修复

- **CLI 失败不再甩 traceback**：`analyze` 的未预期异常（网络/上游模型/工具链）
  归为一行可读错误并以退出码 1 结束；用法/配置错误仍为 2，取消为 130。
- **报告与进度分流**：Markdown 报告走 stdout，加载/清理/产物提示与错误走 stderr，
  `data-agent analyze x.csv > report.md` 重定向出来的只有报告。
- **`.env.example` 占位 Key 提前拦截**：照抄 `your-deepseek-api-key` 等占位值
  会直接提示换成真实 Key，而不是等到模型侧 401。
- **移动端样式冲突**：删除 ≤680px 被 ≤1024px 抽屉规则覆盖的死代码侧栏样式；
  固定底部追问输入框不再遮住最后 1–2 条消息。

## [2026-11-21]

### 新增

- **token 用量遥测全链覆盖并入溯源链**：会话级累计器经 `_invoke_config`
  注入每一次 LLM 调用（plan/replan/execute/finalize/chat 全覆盖）——修复前
  `WorkflowState` 未声明 `usage`/`reasoning` 通道，run()/stream() 的用量
  **恒为 None**、思考过程恒为空（前端 chip 从未渲染过），且规划与执行步骤
  的用量全部漏计。现在 `AnalysisResult.usage` 为会话级累计并随报告透出，
  溯源链新增 `usage` 条目（`verify()` 兼容旧链）；`run()`/`stream()` 入口
  重置累计器避免复跑串账；`chat_done` 事件改为本次追问增量。前端 chip
  标题语义对齐（报告头部=整次分析累计，追问气泡=单次追问）。
- **技能包扫描 SARIF 输出与 references/ 资源目录**：`skill scan --sarif`
  输出 SARIF v2.1.0（GitHub Code Scanning 兼容，critical/high→error、
  medium→warning、low→note，逐条带文件行号）；技能包支持 Agent Skills
  规范的 `references/` 文档目录——install 与三件套同级校验（仅
  `.md/.txt/.json/.csv`、拒绝链接、单文件 ≤5MB / 总量 ≤20MB）并一起复制，
  内容按一等文本参与安全扫描，`list` 给出每个技能的 references 文件数。
- **跨会话记忆（claude-mem 式沉淀注入）**：新增 `memory.py` 与
  `/api/memories`（GET/POST/DELETE）。用户显式沉淀口径教训与偏好——
  `memories.json` 原子写、上限 100 条、单条 ≤500 字、重复文本只加复用计数；
  规划节点按复用次数注入最多 10 条，并标注「非当前用户指令，与当前任务
  冲突时以当前任务为准」，防止旧条目变成跨会话持久的提示注入载体。
  不做向量检索与自动摘要：记忆必须可人工审阅。未添加记忆时行为不变。
- **技能包安装前强制安全扫描**（对标 GitHub 趋势榜 NVIDIA SkillSpector 的
  公开评分口径）：纯静态检测指令覆盖（中英文）、零宽隐藏文本、数据外传
  （`curl|sh`、POST）、密钥收集、base64/eval 混淆、`rm -rf`/sudo、约定外
  可执行文件等 15 类模式；评分 critical +50 / high +25 / medium +10 / low +5
  （配方含 `run_python_code` 或包内可执行文件 ×1.3），≤20 SAFE / ≤50 CAUTION /
  >80 拒绝安装。新增 `skill scan --bundle [--json]` 子命令；`install` 默认
  拒绝 HIGH/CRITICAL，`--accept-risk` 显式越过并留痕。SKILL.md 顶部按
  Agent Skills 规范写入 `name/description/version/license` front-matter，
  与 `skill.json` slug 一致性由扫描校验。

### 修复

- **核验结果随会话持久化**：经营简报复算结果此前只活在前端组件 state，刷新/重开/切标签即丢，只能重新复算。现在每次复算原子写入会话工作区 `audit/last_audit.json`（含草稿正文与时间戳），确认发布把回执并入同一状态；新增 `GET /api/sessions/{id}/sales-audit/last`，前端打开核验页自动恢复"上次核验结果"横幅并提示草稿是否已改动。演示会话从路演退入工作台时也会恢复（实测发现挂载即拉取会让演示路径永远不恢复）。
- **join 不再静默接管主数据**：`join_datasets` 此前护栏通过后直接把合并结果设为活动数据集（实测发生过 28,010 行被 inner join 静默换成 200 行）。现在合并结果只落为候选数据集 `joined_data.csv` 并登记到会话，主数据与行数基线不动；显式采纳走新增的 `adopt_dataset` 工具（模型）或 `POST /api/sessions/{id}/datasets/adopt`（用户在产物中心点「设为主数据」，可忽略）；收缩超一半时附加警示；候选登记持久化到 session.json，重启不丢。
- **产物落盘必进产物页**：手工放置/外部脚本写进 artifacts 目录的文件此前在服务端内存注册表里不存在，产物页显示 0。现在构建会话 payload 前自动做磁盘→注册表同步（幂等），并新增 `POST /api/sessions/{id}/artifacts/rescan`；产物空态提供「扫描产物目录」出口。
- **空图兜底（双引擎纵深防御）**：所有序列都没有数据点（聚合列全空、轴写反等）时，Plotly/ECharts 都直接报错让模型重试并给出修正建议，不再产出"能打开但一片空白"的 HTML；scatter_matrix（SPLOM）按 dimensions 数据判定，不误杀。前端演示散点/趋势/热力/多序列图空数据改为显式空态说明，不再静默消失。
- **演示目录收敛**：首屏主推从 7 份减为 3 份（经营简报核验 / 区域权限与 PII / 脏数据与提示注入），其余 4 份收进二级「更多示例」折叠目录，数据与入口保留。
- **统一空态组件**：新增共享 `EmptyState`（图标 + 结论 + 可选动作），产物中心空态/筛选空态接入，替代各页面手写文案。
- **黄金路径 E2E**：新增 `tests/test_golden_path_e2e.py`——上传 → FakeAgent 驱动真实工具链（真写图表/真落盘视图）→ 产物预览可打开 → 模拟服务重启后产物与 last_result 完整恢复 → 核验 → 确认 → 账本 → 证据包 ZIP → 会话导出 ZIP，外加 join 候选不静默接管的分支；CI 可跑。

### 修复

- **柱状图可能出现空白画布**：模型把类目列写到数值轴（如「各省订单量」给成 x=订单量、y=省份）时，ECharts 的类目轴拿到数字、数值轴拿到字符串，一根柱子都画不出来。生成前检测列类型，类目/数值轴颠倒时自动交换，并补回归测试。
- **离线复算不再可被剥离签名或清空账本绕过**：`scripts/independent_replay.py` 现在要求账本非空、每条记录都带 Ed25519 签名且验签通过、账本中存在与复算 `audit_id` 匹配的 `confirm` 记录。删光签名、删公钥、清空/删除账本、只 audit 未 confirm 的证据包一律退出码 1；输出改为「签名 N/M 条」，不再把 0 条签名报成有效。
- **证据文件指纹复算**：`schema.json` / `lineage.json` / `policy.json` / `draft_trace.json` 的指纹必须与 `report.json` 及账本记录一致；替换口径展示元数据、来源血缘或起草轨迹都会被识别。内置销售口径也在脚本内复算，替换同样失败。
- **空账本/不存在账本不再判通过**：`verify_audit_ledger.py` 对 0 字节账本退出 1、对不存在的账本退出 2；`read_entries(missing_ok=False)` 供离线校验使用，会话首写等 UI 路径保持兼容。
- **账本末行缺少换行时追加会写坏 JSON**：`append_entry` 在追加前补换行，并补回归测试。
- **访问策略不再把会话锁死**：未登记声明式口径时回退内置销售四项，PII/RLS 同样生效；新增 `DELETE /api/sessions/{id}/audit-policy` 清除策略；主体切换按会话 `demo.json` 的 kind 取 principals，不再把区域策略写进其它演示。
- **证据包导出改为流式打包原始 CSV**，不再把整份源文件读进内存；ZIP 临时文件在发送异常或客户端断开时也会删除。
- **演示层**：财报现金结论符号修正（-69% → +223%）、年级结论改为实算差值与标准差、区域预分析随主体切换重算、演示初始化先算产物再注册会话（失败不留孤儿会话）、grades 补充声明式口径；演示目录统一为七份。
- **前端**：确认后可下载「证据包 (.zip)」；「谁能看」按 RLS/PII 实际启用判断；多序列扩到 6 色并在更多序列时叠加虚线；堆叠图点击锁定；从历史打开销售核验演示会恢复草稿；区域权限演示落到预分析视图。
- **启动与文档**：前端端口在 5173–5182 回退并按项目特征判断健康（修复 Windows PowerShell 5.1 把无 charset 的 HTML 按 ISO-8859-1 解码、中文标题永远匹配不上导致的启动误判）；README 与参赛文档按真实按钮名重写 90 秒脚本，补第 0 步访问令牌、证据包导出与简报字节精确保存说明；新增 `scripts/prepare_demo_evidence.py` 一键生成现场证据包。

## [2026-11-19]

### 新增

- **账本 Ed25519 签名**：每条记录签名链哈希，公钥随证据包导出；`verify_audit_ledger.py --pubkey` 与纯标准库 `independent_replay.py` 都能验签，改签名即失败。

- **自动预分析**：演示数据加载后先做确定性分析，按列语义自动选图（趋势/结构/关系/分布/占比/密度），生成 KPI 与结论，不依赖模型。
- 演示菜单改为目录，状态区对齐修复。

- **演示数据目录**：七份内置数据（经营核验 / 区域权限与 PII / 脏数据与注入 / 多维增长月报 / 年级成绩分析 / 公司财报分析 / 20 万行密度视图），加载时自动写入口径、策略与血缘；首页「打开示例」菜单可一键载入。
- 状态区按 Gemini 评审微调：内外分隔分层，只有可点击模型行有悬浮底，健康点统一 #71717a。

- **访问策略（PII / RLS）**：主体、claims、PII 脱敏列、行级过滤；过滤发生在聚合之前，预览脱敏，账本记录 `principal_id`、`policy_sha256`、`rls_filter_hash`；证据包附带策略，独立脚本同样按策略复算。
- 界面状态统一为中性色：顶栏“服务正常”、侧栏“分析引擎”不再使用绿色光晕；空状态移除点阵/光晕/轮播装饰。

- **经营简报核验升级到 `schema_version=2`**：源文件只读一次字节快照，SHA-256、
  编码识别与 CSV 解析基于同一份 bytes，消除读两遍的 TOCTOU；新增范围门——
  登记指标必须整行精确匹配，「华东区订单数」「平均订单数」「订单数（华东区）」
  一律判为 `unsupported`，即使 `confirm_full_table=true` 也不放行；订单号近似
  重复（NFKC、大小写、零宽字符、前导 0）只写入 `warnings` 并给相关指标附
  `caveat`，不改变订单数口径；表头接受 订单号/金额/数量 等中文别名，仅唯一
  匹配时接受，歧义直接报错；新增「客单价」口径（销售额合计 ÷ 订单数，按分
  HALF_UP）；未登记指标按单位给出口径候选 `candidates`。`audit_id` 为源文件
  哈希、草稿、确认标志与复算结果的确定性哈希。
- **零信任证据包**：新增 `POST /api/sessions/{id}/sales-audit/bundle` 导出 ZIP（原始 CSV、草稿、口径、血缘、账本、核验结果与 manifest）；`scripts/independent_replay.py` 不 import 项目代码，只用标准库复算全部指标并重现 `audit_id`，manifest 与账本任一被改动即退出码 1。
- **来源血缘与新鲜度**：会话可登记 `audit_lineage.json`（上游表、catalog URN、抽取时间、负责人、新鲜度 SLA），超期在核验结果标记 `source_stale`，血缘指纹写入账本并可用 `verify_audit_ledger.py --lineage` 校验。
- **口径审批**：口径文档带草稿/已批准状态与批准人，批准人与作者分离；每次保存或批准追加版本历史。
- **声明式核验口径**：会话可登记 `audit_schema.json`，按
  `名称 / 列 / 聚合 / 单位 / 精度 / 数据类型 / 空值策略 / 舍入 / 正负策略 /
  过滤条件 / 时间窗口` 描述指标；支持 `sum` / `count` / `count_distinct` /
  `min` / `max` / `mean`，以及引用两项已登记指标的 `ratio`，核验器用
  `Decimal` 精确复算。空值、类型或负数策略失败时给出「第 N 条记录 / 列名」
  诊断；过滤条件按 AND 组合，时间窗口起止均包含。新增
  `GET/PUT /api/sessions/{id}/audit-schema` 与
  `GET /api/sessions/{id}/audit-schema/history`（逐条复算指纹），前端「核验口径」
  折叠面板可直接编辑 JSON。每次保存追加 `audit_schema_history.jsonl` 版本记录，
  口径指纹绑定进 `audit_id` 与账本；离线脚本 `verify_audit_ledger.py --schema`
  可校验口径指纹是否命中账本。内置销售口径保持不变。
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
  确认门、账本与离线校验命令、快速起草入口、声明式核验口径示例与治理说明，并
  更新 90 秒路演脚本；测试徽章按本次实测数更新为后端 1681+（pytest 收集 1681
  项）、前端 193+（Vitest 实测 193 项通过）。
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
- **大额金额导致核验 500**：sales 整数位超过 16 位时，`Decimal` 除法在 28 位
  精度下会抛 `InvalidOperation`，接口返回 500。现在按行校验并返回可读的 422。
- **Windows 账本锁键漂移**：`Path.resolve()` 在文件存在后会返回带 Windows 扩展长度前缀的路径，
  同一账本被映射成两把锁，并发首写会写坏哈希链。锁键改用
  `abspath + normcase`。
- **账本字段含 Unicode 行分隔符时读不回来**：写入是合法 JSON，但读取用
  `str.splitlines()` 会在 U+2028/U+2029/U+0085 处误切行。改为只按 `\n` 切分。
- **核验页异步竞态与焦点**：快速起草或确认在途时禁用草稿框与操作按钮；响应回来
  若草稿已变则丢弃旧结果，避免覆盖用户输入或展示旧简报；确认成功后焦点落到回执，
  404 恢复面板出现时焦点落到标题。
- **普通工作台首屏按钮越界**：1366×768 下压缩留白、压矮草稿框，并把格式说明在
  窄高度下改为读屏可见、视觉隐藏，保证复算主按钮留在首屏。
- **Web 模型 provider 的文档口径**：Web 路径固定使用 DeepSeek；README 已把
  OpenAI 兼容能力限定为 CLI / MCP，避免与实现不符。
- **部署死配置**：`DATA_AGENT_DATA_DIR` 从未被应用读取；`render.yaml` 与
  Dockerfile 改设 `DATA_AGENT_RUNS_DIR`，持久磁盘方案才会真正落盘。

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

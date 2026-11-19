# 有据经营简报

2026 无锡可信 AI 挑战赛 · 03 进阶赛道「AI 应用创新挑战」

## 作品简介

运营团队把订单明细交给 AI 写经营简报时，销量、订单数和销售额的统计口径很容易混淆。有据经营简报将 [Data Analysis Agent](https://github.com/xiaomaozjj666/data-analysis-agent) 的报告起草能力与独立复算接在一起。用户导入订单 CSV，可以让 Agent 起草或直接粘贴待审简报。核验页回读原表，展示草稿原值、复算值、计算口径和源表记录。确认不是前端本地状态：服务端会按原始 CSV 重新复算，`audit_id` 一致才写入哈希链账本并返回回执。证据不足或口径不清的内容留在核验明细中待复核。

## 启动和演示

在 Windows 双击仓库的 `run.bat`。首次启动会安装项目依赖，赛前提前跑一遍。已有依赖时，`简报核验` 的内置样例可全程在本机运行，无需模型 API Key。

1. **口径面板。** 首页点「体验简报核验」进入路演，投影时可点「全屏」。先展开
   「核验口径」面板，说明默认是内置销售四项（销售额合计、销量合计、订单数、
   客单价）；其他业务表可以粘贴声明式 JSON，保存并复算后结果区会显示口径指纹。现场先
   保持内置口径，停在草稿的 462 笔，说明样例为合成数据，草稿含预设错误。
2. **复算。** 点「复算并核验」，现场揭晓原表去重后的 20 笔。把 442 笔的差额和
   计算口径一起指给评委看；展开「查看来源」，指出前两行都属于订单 1001，所以
   21 条明细只对应 20 笔订单，来源卡也展示原表 SHA-256。有人问错误出在哪，点
   「草稿工作台」，再点冲突项的「定位第 3 行」；评委也可以改一个数字，由程序
   重新计算。市场份额缺少市场总量，留待人工复核。
3. **确认。** 点击「确认更正并生成简报」，服务端按原始 CSV 重新复算，只有
   `audit_id` 一致才写账本并返回权威简报；页面展示回执序号、时间与 hash 前
   16 位，并可下载 Markdown 和含回执的 `sales-audit.json`。明细保留草稿原文、
   判定、计算口径、数据来源和本次确认状态。
4. **哈希链。** 点「查看哈希链账本」展开抽屉，指出每条记录的 `seq` / `hash` /
   `prev` 与链校验 `valid=true`；也可在 PowerShell 调
   `GET /api/sessions/<会话>/sales-audit/ledger` 复核。讲完可点「重开演示」
   恢复开场。
5. **1 字节篡改。** 用页面下载的核验版简报，执行
   `Add-Content .\经营简报-核验版.md " "`，再跑
   `scripts/verify_audit_ledger.py --brief ...`，展示退出码 1。强调源表未动，
   只改了导出文件的一个字节。

注意：演示页的这个确认按钮会调用服务端 `/confirm`。服务端按原始 CSV 重新复算，`audit_id` 一致才写账本并返回权威简报；页面会展示回执序号、时间与 hash 前 16 位。草稿或数据在核验后被改动时返回 409，页面会清空结果并要求重新复算。

已配置模型时，可从核验页点「准备 AI 起草任务」，在分析页生成并执行计划。报告生成后回到「简报核验」点「使用分析报告草稿」。会优先导入「待核验指标（全表）」小节。如果模型只返回普通报告，可将所需指标整理成下方格式后复算。现场可提前准备 AI 草稿，把路演时间留给复算和纠错。

本机一次实测中，DeepSeek 关闭深度思考后约 20 秒生成样例报告，三项指标经核验页全部通过。内置草稿可用于现场展示错数和无依据结论。

## 核验器（schema v2）

核验器在 `src/data_agent/sales_audit.py`，只读原始 CSV 复算，不调用第二个模型。当前行为：

- **单次字节快照**：源文件只读一次，SHA-256、编码识别与 CSV 解析都基于同一份 bytes，不存在「先算哈希、再读一遍内容」的 TOCTOU。编码支持 UTF-8（含 BOM）与 GB18030。
- **范围门**：登记指标必须整行精确匹配。「华东区订单数」「平均订单数」「订单数（华东区）」一律 `unsupported`，即使点「确认指标范围为全表并复算」也不放行。非登记标签行里只有 `%` 与「人」可以豁免范围门，元/件/笔 仍要走口径判定。

```text
华东区订单数：8 笔        → unsupported（范围/分组限定）
平均订单数：10 笔         → unsupported（含平均、分组意图）
订单数（华东区）：8 笔    → unsupported（登记名被范围限定夹带）
订单数：20 笔             → verified（全表口径）
```

- **近似重复告警**：订单号按原样去重；NFKC 归一化后相同、大小写不同、夹零宽字符、前导 0 不同（如 `001` 与 `1`）只写入 `warnings`（`near_duplicate_order_id`），并在订单数、客单价两项上附 `caveat`。告警不改变订单数口径，避免把「看起来像同一单」的明细擅自合并。
- **表头别名**：接受 `order_id` / `订单号` / `订单编号`、`sales` / `金额` / `销售额`、`quantity` / `数量` / `销量` 等写法；每个标准列必须恰好唯一匹配，歧义直接报错。
- **指标口径**：销售额合计按分精确求和；销量合计为非负整数求和；订单数为 `COUNT(DISTINCT order_id)`；客单价为销售额合计 ÷ 订单数，按分 `HALF_UP` 舍入。
- **口径候选**：未登记指标按单位给 `candidates`（如以「元」结尾时列出同单位指标），只作提示，不自动采纳。
- **确定性 `audit_id`**：由源文件 SHA-256、草稿、`confirm_full_table` 与复算结果共同计算，带上快速起草轨迹时还会绑定 `draft_trace_sha256`，形如 `aud-` 加 32 位十六进制。草稿、数据、确认标志或轨迹任一变化，`audit_id` 都会变。

## 企业级口径与治理

内置销售四项只适用于订单明细。其他业务表可以在会话工作区放一份
`audit_schema.json`，用声明式指标描述口径；核验器按登记项独立复算，不执行模型
生成的表达式。一个会话最多登记 12 项指标，每项最多 8 个过滤条件；聚合支持
`sum` / `count` / `count_distinct` / `min` / `max` / `mean`，以及引用两项已登记
指标的 `ratio`（配 `numerator` / `denominator`，不支持过滤与时间窗）。

- **版本历史**：每次保存都会向 `audit_schema_history.jsonl` 追加一条记录，含
  `ts`、`sha256` 与完整指标定义；历史只追加不覆盖，可回看口径何时被改过。
  `GET /api/sessions/{id}/audit-schema/history` 返回最近 `limit` 条历史（默认 20，
  最多 100）并逐条复算 `fingerprint_valid`，`consistent` 汇总整份历史是否自洽。未登记或保存空数组时
  回退内置销售四项。
- **指纹绑定**：登记项规范化 JSON 的 SHA-256 作为 `schema_fingerprint`，进入
  `audit_id`，并随 `audit` / `confirm` 记录写入 `audit_ledger.jsonl`。口径改动
  后旧 `audit_id` 失效，服务端确认返回 409，避免用旧口径的核验结果发布新口径
  的简报。离线可用 `scripts/verify_audit_ledger.py <账本> --schema <口径文件>`
  校验口径指纹是否命中账本，改一项口径即退出码 1。
- **空值与类型策略**：`null_policy` 支持 `exclude` / `fail` / `zero`；
  `data_type` 支持 `decimal` / `integer` / `date` / `string`，`sum`、`mean`、
  `min`、`max` 只接受 `decimal` 或 `integer`；数值聚合下 `integer` 要求单元格
  为整数。
  `sign_policy=non_negative` 拒绝负数。策略失败时返回「第 N 条记录的列名」诊断，
  不静默跳过。
- **过滤与时间窗**：`filters` 支持 `eq` / `ne` / `gt` / `ge` / `lt` / `le` /
  `in` / `contains`，同一指标内的条件按 AND 组合；`time_window` 用
  `{column, start, end}` 限定日期，起止均包含，日期格式为 `YYYY-MM-DD`。
- **精度与舍入**：`decimals` 为 0–6；`rounding` 支持 `HALF_UP` 与 `HALF_EVEN`，
  金额与均值按登记精度输出；计数类指标始终输出整数。
- **API 与面板**：`GET/PUT /api/sessions/{id}/audit-schema` 读取或覆盖当前口径，
  前端「核验口径」折叠面板直接编辑 JSON。`PUT` 接受完整策略字段；当前
  `GET/PUT` 响应只回显名称、列、聚合、单位、精度与别名，`filters`、`time_window`、
  `data_type`、`null_policy`、`rounding`、`sign_policy`、`numerator`、`denominator`
  等完整策略以工作区 `audit_schema.json` 为准。非销售表的完整 JSON 示例与验收命令见
  [README](../README.md#声明式核验口径)。

## 服务端确认与账本

`POST /api/sessions/{id}/sales-audit/confirm` 不信任客户端传来的核验结果，会按会话里保存的原始 CSV 重新复算。重算的 `audit_id` 与提交值不一致返回 409；通过后返回权威简报和回执（`seq` / `ts` / `hash` / `prev` / `brief_sha256` / `draft_trace_sha256`），并在 `runs/<会话>/audit_ledger.jsonl` 追加一条 `confirm` 记录。每次普通复算也会追加一条 `audit` 记录。复算与确认接口都接受可选的 `draft_trace_sha256`：带上它时，服务端会先校验 `draft_traces/<哈希>.json` 存在、内容哈希可复算，并且轨迹里的 `parsed_draft_sha256` 与本次草稿逐字一致，否则返回 422。

账本逐条用 `prev` 与 `hash` 串联，追加前先校验整链，链损坏时拒绝继续写入。`GET /api/sessions/{id}/sales-audit/ledger` 返回最近记录与链校验结果。离线校验不依赖服务进程：

```powershell
# 账本 + 原始 CSV + 核验版简报三者对照
.\.venv\Scripts\python.exe scripts\verify_audit_ledger.py "runs\<会话>\audit_ledger.jsonl" `
  --brief .\经营简报-核验版.md --source "runs\<会话>\input\<上传文件名>.csv" `
  --trace "runs\<会话>\draft_traces\<sha256>.json"
# 退出码 0 = 通过；没走快速起草时可省略 --trace

# 现场演示：只改简报 1 个字节（追加一个空格），再校验
Add-Content .\经营简报-核验版.md " "
.\.venv\Scripts\python.exe scripts\verify_audit_ledger.py "runs\<会话>\audit_ledger.jsonl" `
  --brief .\经营简报-核验版.md
# 退出码 1 = 简报内容与账本中的确认记录不匹配
```

服务端确认的最小调用示例（仓库根目录、后端已启动）：

```powershell
$base = "http://127.0.0.1:8000"
$sid = "<会话 ID>"
$draft = @"
销售额合计：17224.00 元
销量合计：462 件
订单数：20 笔
"@

$audit = Invoke-RestMethod "$base/api/sessions/$sid/sales-audit" -Method Post `
  -ContentType "application/json; charset=utf-8" `
  -Body ([Text.Encoding]::UTF8.GetBytes((@{ draft = $draft; confirm_full_table = $false } | ConvertTo-Json)))

$confirm = Invoke-RestMethod "$base/api/sessions/$sid/sales-audit/confirm" -Method Post `
  -ContentType "application/json; charset=utf-8" `
  -Body ([Text.Encoding]::UTF8.GetBytes((@{ draft = $draft; audit_id = $audit.audit_id; confirm_full_table = $false } | ConvertTo-Json)))
$confirm.receipt

Invoke-RestMethod "$base/api/sessions/$sid/sales-audit/ledger"
```

## 快速起草（真实模型对照）

`POST /api/sessions/{id}/sales-audit/quick-draft` 用来回答「462 笔是预设的，真实模型会错吗」。它不绑定任何工具、关闭思考，只把表按 CSV 文本交给模型写三行指标；提示词不给去重口径、不给公式、不举例。返回：

- `draft`：模型原话里符合三行格式的内容，不做修正或补算；
- `model` / `thinking`：实际模型名；`thinking` 仅在响应的 `additional_kwargs.reasoning_content` 非空时为 `true`（以 content 块返回的推理只计入 `dropped`，不改变该字段）；
- `elapsed_ms`：本次调用耗时；
- `rows_sent` / `masked` / `masked_cells`：发送行数与非指标列的脱敏情况（`order_id` / `sales` / `quantity` 不脱敏，否则会改变真值）；
- `dropped`：被丢弃的非指标行与推理块数量；
- `trace_sha256` / `raw_output_sha256` / `parsed_draft_sha256`：轨迹文件（`runs/<会话>/draft_traces/<sha256>.json`，含模型原话与过滤后的草稿）的内容哈希、原话哈希与草稿哈希。把 `trace_sha256` 作为 `draft_trace_sha256` 传给复算或确认接口，可把「模型原话 → 草稿 → 复算 → 确认」绑定到同一条账本记录。

边界：单表最多 300 行、1 MB，单次调用 60 秒上限，同一会话 10 秒内只能调用一次；未配置模型 Key 时返回 409。它实测会出现销售额合计算错、含退款表的订单数写成行数等问题，但这只代表「直接读表心算」这一种设置，不能据此宣称模型总是算错，也不能代表产品的分析能力。核验仍由 `sales_audit.py` 独立完成。

## 数据约定

- 上传 UTF-8 或 GB18030 编码、逗号分隔的 CSV，包含 `order_id`、`sales`、`quantity` 三列。每行是订单明细，同一订单可包含多行。
- 全表统计。销售额按元输入、按分精确求和，销量为非负整数求和，订单数按 `order_id` 去重。缺少所需列或存在无效数据会提示修正源表。
- 草稿每行一个指标，单位为元、件、笔。数值使用半角数字，金额最多两位小数。重复指标、缺失单位和其他口径进入人工复核。草稿带标题或说明时，先确认保留的指标均描述原始 CSV 全表，再复算。

```text
销售额合计：17224.00 元
销量合计：462 件
订单数：20 笔
```

## 路演讲法

**解决的问题。** 运营团队可以复核 AI 起草的经营简报，避免把商品件数当成订单数。

**为何可信。** Agent 根据用户任务调工具形成草稿。核验器独立读取原始 CSV，按公开口径复算，并标出草稿行号、数据字段和原表 SHA-256。文件哈希用于定位那一版数据。确认由服务端重新复算后放行，账本把源文件、草稿和核验版简报的哈希串起来。

**如何纠错。** 对照 462 笔与 20 笔，跳到草稿第 3 行，再让用户确认更正。市场份额留待人工复核。演示中的修改发生在生成的简报里，源表始终作为证据；确认由页面调用服务端接口完成，回执在页面可见，也可用离线脚本复核。

### 90 秒现场节奏

赛前准备：本机 `run.bat` 已启动，浏览器停在「简报核验」页；另开一个 PowerShell 窗口，处在仓库根目录并设好 `$sid`（用于最后调 `ledger` 与离线脚本）。

- **0–15 秒 口径面板。** 停在 462 笔的草稿上问：「午会前收到这份简报，上面写着 462 笔订单。你敢直接把它当成订单量吗？」展开「核验口径」，说明默认是内置销售四项，其他业务表可粘贴声明式 JSON 登记；口径不是页面写死的。
- **15–35 秒 复算与范围门。** 点「复算并核验」，展示 20 笔和多报的 442 笔，用一条明细和 `COUNT(DISTINCT order_id)` 讲清口径；展开「查看来源」指出订单 1001 的两行。随后在草稿里加一行「华东区订单数：8 笔」，点「确认指标范围为全表并复算」，指出它仍是 `unsupported`——范围限定指标不会被确认开关放行。待复核的市场份额一并指出。
- **35–55 秒 服务端确认。** 点「确认更正并生成简报」，页面展示回执：`seq`、时间、`hash` 前 16 位，以及「已写入审计账本 #序号」；说明服务端是按原始 CSV 重算后才写的，客户端改过草稿会得到 409。
- **55–75 秒 哈希链。** 点「查看哈希链账本」展开抽屉，指出每条记录的 `seq` / `hash` / `prev` 与 `valid=true`；再在 PowerShell 调一次 `ledger`，指出 `verification.valid=true`。
- **75–90 秒 改 1 个字节与收尾。** 用页面下载的核验版简报，执行 `Add-Content .\经营简报-核验版.md " "`，再跑 `verify_audit_ledger.py --brief`，展示退出码 1；强调源表未动、只改了导出文件的一个字节。最后一句话交代边界：账本能发现改动，但没有签名和外部时间戳，不是不可否认基础设施；核心价值是让「AI 写的数」必须经过独立复算和用户确认才能发布。

快速起草是可选对照：若评委追问「真实模型会错吗」，再点「让模型直接起草（不调工具）」，说明这是关闭思考、不调工具、只让模型读表心算的结果，不属于上述五步主线；不要把它说成产品的分析能力。如果现场模型调用超时或没有 Key，改用赛前保存的真实输出，并如实说明是预先生成的，不要现场伪造模型返回。

## 诚实边界

- **账本能力有限。** 账本只提供演示级的篡改可见性：无签名、无私钥、无外部时间戳，能发现内容被改动，不能证明「谁在何时确认」，也不防持有写权限的人重建整条链。生产环境需要签名、可信时间戳与外部存证。
- **Free 档 Render 会休眠。** 免费实例约 15 分钟无请求后休眠，冷启动通常 35–90 秒；`healthCheckPath` 在冷启动期间可能失败，不能把一次健康检查失败当成服务崩溃。现场以本机 `run.bat` 为主，赛前先预热；Render 只作为备用地址。
- **范围门不是通用语义核验。** 它只覆盖已登记指标与已识别的范围/分组写法，没有登记的口径会进入 `unsupported` 或人工复核，不会被自动判为正确。
- **快速起草只代表一种设置。** 它展示的是「关闭思考、不调工具、直接读表」的对照结果，不能推广为模型能力结论，也不能据此宣称模型总是算错。
- **演示页确认走服务端。** 页面「确认更正并生成简报」会 POST `/confirm`，只有服务端按原始 CSV 重算出的 `audit_id` 一致才返回权威简报与回执；409 表示草稿或数据在核验后发生变化。

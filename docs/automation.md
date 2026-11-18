# 定时分析任务（Automation）

把「同一条分析」变成按计划自动运行的任务：加载数据 → 计算登记指标 → 用确定性统计
检测异常 → 落历史/告警，并按需 POST 到 webhook。目标是补上从「人找数据」到
「数据找人」的最短路径（对标 WorkBuddy 周期任务、Trae Automation、Tableau Pulse）。

> 设计边界：不引入调度框架、不在 Web 进程里起守护线程。定时由外部调度器
> （cron / Windows 任务计划）调用 `run-due`，或前台 `loop` 模式；异常检测不调用 LLM。

## 1. 前置条件

1. 一份数据文件（CSV / Excel / Parquet / JSONL / SQLite 等，见工作区支持列表）。
2. 一份 `metrics.json` 指标定义（`name` / `description` / `expression`；可选
   `aliases`、`source_columns`、`examples`）。自动化只计算登记过的指标。
3. Python 环境与项目依赖（`pip install -e ".[dev]"`）。

## 2. 任务定义

任务定义是 JSON，整体保存在 `<runs-dir>/automation/automations.json`（由 `add`
写入，可直接编辑后重跑）。`--runs-dir` 默认取 `DATA_AGENT_RUNS_DIR` 或 `./runs`。

```json
{
  "id": "daily-sales",
  "name": "每日销售额巡检",
  "dataset_path": "./data/sales.csv",
  "metrics_path": "./data/metrics.json",
  "interval_minutes": 60,
  "metric_names": ["销售额"],
  "zscore_threshold": 3.0,
  "pct_change_threshold": 0.2,
  "history_limit": 30,
  "min_samples": 3,
  "webhook_url": "https://example.invalid/alerts",
  "enabled": true
}
```

| 字段 | 说明 |
| --- | --- |
| `id` | 必填，`[A-Za-z0-9_-]{1,64}` |
| `name` | 必填，展示名 |
| `dataset_path` | 必填，数据文件 |
| `metrics_path` | 可选；缺省时找数据集同目录的 `metrics.json` |
| `interval_minutes` / `daily_at` | **二选一**：间隔分钟数，或本地时间 `HH:MM` |
| `metric_names` | 可选；只关注这些指标（默认全部）。写了未登记的指标名会让该任务失败 |
| `zscore_threshold` | 默认 3.0，历史窗口上的 z-score 阈值 |
| `pct_change_threshold` | 默认 0.2（20%），与上一次值的环比阈值 |
| `history_limit` / `min_samples` | 默认 30 / 3；历史窗口与最少基线样本 |
| `webhook_url` | 可选；告警 JSON POST 目标（安全规则见第 5 节） |
| `email_to` | 可选；邮件收件人数组或逗号分隔串（SMTP 连接信息走环境变量） |
| `sms_to` | 可选；E.164 手机号数组（最多 5 个，Twilio 凭据走环境变量） |
| `enabled` | 默认 true；false 时不参与 `run-due` |

## 3. 命令

`--runs-dir` 是**全局参数，必须写在子命令之前**（argparse 限制）：

```bash
# 新增 / 更新任务（--spec 指向上面的 JSON 文件；也接受 {"automations": [...]}）
python -m data_agent.automation --runs-dir ./runs add --spec ./automation.json

# 查看任务
python -m data_agent.automation --runs-dir ./runs list

# 巡检：汇总每个任务的上次运行、到期状态、下次预计到期与最近归因（不触发执行）
python -m data_agent.automation --runs-dir ./runs status
# 有到期任务或上次运行失败时退出码为 1，供监控系统调用
python -m data_agent.automation --runs-dir ./runs status --fail-on-alert

# 生成零依赖的静态 HTML 看板（--out 省略则打印到 stdout；--refresh-seconds 用 meta refresh）
python -m data_agent.automation --runs-dir ./runs dashboard --out ./runs/dashboard.html --refresh-seconds 300

# 立即跑一次（--dry-run 只计算与检测：不写历史/告警、不推送、不建持久工作区）
python -m data_agent.automation --runs-dir ./runs run --id daily-sales
python -m data_agent.automation --runs-dir ./runs run --id daily-sales --dry-run

# 跑所有到期任务（供 cron / 任务计划调用）
python -m data_agent.automation --runs-dir ./runs run-due --no-notify

# 前台轮询（Ctrl+C 退出；适合开发与演示）
python -m data_agent.automation --runs-dir ./runs loop --poll-seconds 60
```

外部调度示例：

```bash
# cron：每 5 分钟检查一次是否有到期任务（先确保日志目录存在）
mkdir -p runs/automation
*/5 * * * * cd /srv/data-agent && python -m data_agent.automation run-due >> runs/automation/cron.log 2>&1
```

Windows 任务计划同理，动作写 `python -m data_agent.automation run-due`，起始目录设为项目根。

## 4. 异常判定与产物

对每个指标，取该任务最近 `history_limit` 条**相同口径版本**的成功历史值作为基线
（历史行带 `metrics_sha256`；口径文件一变，基线自动重置，不与旧口径混比）：

- 基线样本数 < `min_samples`：不判定（首次运行不会误报）。
- `|z-score| >= zscore_threshold`：标记；
- `|(当前 - 上一次) / |上一次|| >= pct_change_threshold`：标记；
- 基线标准差为 0 且上一次为 0、当前非零：标记「基线为 0，当前值非零」；
- 指标表达式失败或整列无有效数值：该指标不进基线、不参与告警，整次运行标
  `partial`，错误写进 `metric_errors` 与历史行的 `errors`。

产物位于 `<runs-dir>/automation/<id>/`：

| 文件 | 内容 |
| --- | --- |
| `history.jsonl` | 每次运行的 `at` / 各指标值 / `errors` / **指标定义文件哈希** |
| `alerts.jsonl` | 触发的异常（指标、值、基线均值/标准差、z、环比、`direction`、`severity`、原因），行级还有 `at`/`task_id`/`attribution` |
| `state.json` | `last_run_at` / `last_attempt_at` / `last_status`（ok/partial/error）/ `last_alerts` / `last_errors` / `metrics_sha256` / webhook 失败原因 / `last_alert_headline` / `next_retry_at` |
| `.lock` | 任务级锁文件（跨进程互斥，防止 cron 与手动运行重复推送） |

运行还会创建 `<runs-dir>/automation_workspaces/<id>/` 作为只读工作副本；它只是
工具执行时的临时工作区，可安全清理（任务下次运行会重建）。

`run` / `run-due` 的结果 JSON 包含：`metrics`（逐指标状态/汇总值）、
`metric_errors`、`alerts`、`attribution`、`notified`、`webhook_error`、
`history_samples`、`metrics_file`、`metrics_sha256`、`dry_run`；拿不到锁时返回
`{"skipped": "locked"}`。

### 4.1 归因摘要（`attribution`）

多条告警会按**阈值归一化严重度**排序（z-score ÷ 阈值、环比 ÷ 阈值取大），
输出一段确定性摘要，随结果、`alerts.jsonl` 和 webhook 一起发出：

```json
{
  "headline": "订单数 下降（环比 -60.0%、z-score -4.10），为本次偏离最大的指标",
  "top_metric": "订单数",
  "max_severity": 3.0,
  "count": 2,
  "same_direction": false,
  "directions": {"up": 1, "down": 1, "flat": 0},
  "ranking": [{"metric": "订单数", "severity": 3.0, "direction": "down", "value": 40.0, "baseline_mean": 100.0}]
}
```

`same_direction=true` 表示所有告警同向（通常指向数据源/口径整体变化，而非
单个业务事件）；它只是统计事实，不构成因果结论。`ranking` 最多保留前 10 项。

### 4.2 巡检视图（`status`）

`status` 只读汇总，不运行任务、不写状态：

```json
{"generated_at": "2026-11-18T08:00:00+08:00",
 "summary": {"total": 2, "enabled": 2, "due": 1, "alerts": 3, "errors": 0},
 "tasks": [{"id": "daily-sales", "name": "每日销售额巡检", "enabled": true,
            "schedule": {"interval_minutes": 60}, "due": true,
            "next_due_at": "2026-11-18T08:00:00+08:00",
            "last_run_at": "2026-11-18T07:00:00+08:00", "last_status": "ok",
            "last_alerts": 1, "last_alert_headline": "…", "state_corrupt": false}]}
```

`next_due_at` 是展示用估算（间隔=上次+间隔，每日=当天/次日本地目标时刻）；
错过的任务会返回一个已过去的时刻，表示“现在已到期”。`--fail-on-alert` 在
`due>0`、`errors>0` 或上次运行 `alerts>0` 时退出码为 1，可直接接监控告警规则。

## 5. 失败、并发与 webhook 安全

- **推送顺序**：先写历史与 `alerts.jsonl`，再尝试 webhook；推送失败把错误写入
  `state.json.last_webhook_error`，告警本身不丢，任务状态不回滚。
- **单任务失败隔离**：`run-due` 对每个任务独立 try；失败写
  `last_status=error` / `last_error` / `next_retry_at`（默认 5 分钟后重试），
  不影响同批其他任务。任务定义文件本身非法时整批不执行（刻意 fail-fast，
  避免静默跳过配置错误）。
- **并发**：同一任务用跨进程锁串行化；拿不到锁的运行直接跳过（`skipped=locked`），
  不会重复写历史或重复推送。
- **webhook SSRF 防护**：只接受 http/https、禁止地址内嵌账号密码；IP 字面量在
  登记阶段拒绝；**每一跳（含重定向）都是"一次 getaddrinfo 同时完成校验与
  IP 钉扎"**——连接直接连到已校验的 IP，TLS/SNI/Host 头仍用原域名，解析与
  连接之间不存在 DNS rebinding 窗口（2026-11-18 修复，此前是"先校验再连接"
  两次解析）。任一解析结果命中内网/环回/链路本地/保留地址，该跳整体拒绝。
- **群机器人渠道适配**：目标主机是 `hooks.slack.com` / `open.feishu.cn`
  （Lark 同域）/ `oapi.dingtalk.com` 时，自动把结构化告警压成对应机器人的
  文本格式（Slack `{"text": …}`、飞书 `{"msg_type":"text",…}`、钉钉
  `{"msgtype":"text",…}`）；其他地址保持原始 JSON。文本最多 3500 字符、
  只列前 5 条告警，完整明细始终在 `alerts.jsonl`。
- **邮件告警（SMTP）**：任务定义里只写收件人 `email_to`（最多 10 个），
  连接信息全部放环境变量，凭据不落任务文件：
  `DATA_AGENT_SMTP_URL`（`smtp://user:pass@host:587?starttls=1` 或
  `smtps://…:465`）、可选 `DATA_AGENT_SMTP_FROM`（缺省用 URL 里的用户名）。
  默认 STARTTLS；错误文本会脱敏密码后写入 `last_email_error`。发送在告警落盘之后，
  失败不影响任务状态回滚。
- **短信告警（Twilio）**：任务定义写 `sms_to`（E.164，最多 5 个），凭据放
  `DATA_AGENT_TWILIO_ACCOUNT_SID` / `DATA_AGENT_TWILIO_AUTH_TOKEN` /
  `DATA_AGENT_TWILIO_FROM`。短信正文截断到 300 字（运营商分段与计费考虑），
  完整明细仍在 `alerts.jsonl`；同样脱敏错误信息。
- **未内置的投递方式**：邮件与短信之外的自定义渠道用 webhook（任意 HTTP
  网关即可转发到企业 IM / 工单系统）；运营商直连短信需按供应商协议单独实现。
- **dry-run 无副作用**：不写历史/告警/状态、不推送（webhook/邮件/短信都不发），
  也不创建持久工作区。
- **时区**：`daily_at` 按运行机器的本地时区解释；DST 切换日目标时刻仍按
  当天本地时间计算，最多偏移一小时，不会跳过或重复执行（以 `last_run_at`
  与目标时刻比较）。

## 6. 边界

- **归因只到统计层**：`attribution` 给出偏离排序、方向与同向判断，不推导业务
  原因；需要解释时用分析工作台追问（参见 `docs/competitive-analysis.md` §6.4）。
- **口径可追溯**：历史每行都带 `metrics_sha256`，事后能回答「这次用的是哪版口径」。

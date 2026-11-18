# 协作、只读访问与审计

面向小团队的轻量协作：不引入用户体系/数据库，评论跟着会话目录走；访问控制
用两个环境变量区分「能改」与「只能看」；审计日志可选落盘。

## 1. 会话评论

- 入口：工作台「评论」Tab（快捷键 `5`），或 API：
  - `GET /api/sessions/{id}/comments`
  - `POST /api/sessions/{id}/comments` `{"text": "...", "author": "可选署名"}`
  - `DELETE /api/sessions/{id}/comments/{comment_id}`
- 存储：会话根目录 `comments.json`，原子写；单会话最多 200 条、单条 2000 字。
- 显示：纯文本（不渲染 Markdown/HTML），React 默认转义，把评论变成 XSS 注入面的
  风险降到最低；署名只用于展示，不是权限身份。
- 并发：进程内加锁；多进程（多 worker）同时写同一会话可能丢一条，这是轻量
  取舍。需要强一致时应换成数据库并加行锁。
- 导出：评论随会话 ZIP 一起归档（`comments.json` 在包内），导入后恢复。

## 2. 访问令牌：全权 / 只读

| 环境变量 | 权限 |
| --- | --- |
| `APP_ACCESS_TOKEN` | 全权：读写、上传、分析、删除 |
| `APP_VIEWER_TOKEN` | 只读：仅 `GET/HEAD/OPTIONS`；写请求返回 403 |

- 两者都未设置：只允许绑定环回地址（原有行为）。
- 只设置 viewer：服务整体进入只读模式（写操作全部 403），启动日志会告警。
- 请求头用 `X-App-Token: <token>` 或 `Authorization: Bearer <token>`。
- 两个令牌都比较一次，避免通过响应时间区分身份；`/api/auth` 对任一令牌都返回
  `authenticated: true`。

## 3. 审计日志

设置 `DATA_AGENT_AUDIT_LOG=/path/to/audit.jsonl` 后，以下事件追加一行 JSON：

- 写操作（POST/PUT/PATCH/DELETE）成功或失败后的最终状态；
- 被拒绝的请求（401/403）。

字段：`at`、`actor`（admin/viewer/anonymous）、`method`、`path`、`status`、
`request_id`。**不记录**请求体、查询串、令牌与客户端 IP（最小化留存）。
写日志失败只告警一次，不影响请求——审计是旁路，不能变成可用性风险。

## 4. 边界

- 没有多租户隔离：所有会话共享同一个 `runs/`；需要强隔离请一租户一实例。
- 没有行级/列级权限：敏感字段请在数据接入前脱敏（`privacy.py` 的样例脱敏
  只作用于展示，不替代存储层脱敏）。
- 评论不是审批流：它记录讨论，不改变分析状态。

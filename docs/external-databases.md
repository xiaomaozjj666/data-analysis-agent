# 外部数据库：PostgreSQL 与通用 DB-API 数仓

`query_database` / MCP `sql_query` 支持三类源：

| source | 说明 | 只读保证 |
| --- | --- | --- |
| `session` | 上传的 SQLite 文件；CSV/Excel 会话把活动数据集暴露为表 `data` | driver（`mode=ro` / `PRAGMA query_only`） |
| `postgres` | `DATA_AGENT_DATABASE_URL` 配置的 PostgreSQL | server-confirmed（`default_transaction_read_only` + 回读确认） |
| `external` | 配置式 DB-API 源（Snowflake / Databricks / BigQuery DB-API 等） | role-required（SQL 护栏 + 账号权限） |

## 1. PostgreSQL

```bash
export DATA_AGENT_DATABASE_URL='postgresql://readonly_user:password@host:5432/dbname'
pip install ".[postgres]"
```

服务端设置 `default_transaction_read_only=on` 并回读 `SHOW transaction_read_only`
确认；失败直接报错，不降级。

## 2. 通用 DB-API 数仓

项目不自带各云厂商 SDK；按需安装后，用两个环境变量接入：

```bash
# Snowflake
pip install snowflake-connector-python
export DATA_AGENT_EXTERNAL_DB_MODULE=snowflake.connector
export DATA_AGENT_EXTERNAL_DB_KWARGS='{"account":"...","user":"...","password":"...","warehouse":"...","database":"...","schema":"..."}'
export DATA_AGENT_EXTERNAL_DB_LABEL='Snowflake 生产仓'   # 可选，只用于展示

# Databricks SQL
pip install databricks-sql-connector
export DATA_AGENT_EXTERNAL_DB_MODULE=databricks.sql
export DATA_AGENT_EXTERNAL_DB_KWARGS='{"server_hostname":"...","http_path":"...","access_token":"..."}'

# BigQuery（DB-API 2.0）
pip install google-cloud-bigquery
export DATA_AGENT_EXTERNAL_DB_MODULE=google.cloud.bigquery.dbapi
export DATA_AGENT_EXTERNAL_DB_KWARGS='{"project":"..."}'
```

白名单模块：`sqlite3`、`snowflake.connector`、`databricks.sql`、
`google.cloud.bigquery.dbapi`、`psycopg`；其他模块名拒绝导入，避免配置误填执行
任意代码。`DATA_AGENT_EXTERNAL_DB_TIMEOUT_SECONDS`（1–120，默认取 SQL 预算）
用于支持会话级超时的驱动（Snowflake 会执行 `ALTER SESSION SET
STATEMENT_TIMEOUT_IN_SECONDS`）。

### 安全边界（务必读完）

- 所有语句先过 `sql_guard.validate_select`：只允许单条 `SELECT`/`WITH`，拦截
  多语句、CTE 写入、`INTO`、危险函数与方言特定禁用语句。
- `external` 的只读**不是驱动层保证**：请用专用只读角色，并把账号权限限制到
  所需 schema。能力声明 `role-required` 就是在提醒这一点，项目不会把它标成
  `server-confirmed`。
- 内省用 DB-API 可选的 `cursor.tables()/columns()`：驱动不支持时返回空结构并
  提示直接写 SQL，不伪造表清单。
- 行/列上限、JSON 安全转换与超时预算与 session/postgres 共用同一套实现。

### 离线验证

CI 用 `sqlite3` 作为 DB-API 模块跑完整链路（连接、内省降级、SELECT、写语句
拒绝），并用伪 Snowflake 模块验证会话超时设置。真实云账号的连接、IAM 权限与
服务端超时取消属于部署方验收项，仓库内没有凭据，不宣称已验证。

# 安全策略

Data Analysis Agent 是一个基于 LangGraph 的数据分析工作台：上传数据集后自动完成检查、清洗、统计、可视化与报告，并提供受限 Python 沙箱与只读 SQL 工具。默认面向本机单用户运行。本文件说明如何私密报告**本仓库自身**的安全问题。

## 支持的版本

版本号见 `pyproject.toml`（当前为 1.0.0）；仓库未发布 tag / release，安全修复进入 `main` 最新提交。

| 版本 | 支持状态 |
|---|---|
| 1.0.x（当前） | ✅ 接收安全报告并在 `main` 修复 |
| 更早的历史提交 | ❌ 不单独维护，请使用 `main` 最新代码 |

## 私密报告漏洞

**请勿在公开 Issue 中粘贴漏洞细节、利用代码或真实业务数据。**

请通过 GitHub 的私密漏洞报告渠道提交：

1. 打开 [Security Advisories](https://github.com/xiaomaozjj666/data-analysis-agent/security/advisories) 页面；
2. 点击 **Report a vulnerability**；
3. 填写复现步骤与影响范围。

也可直接访问：<https://github.com/xiaomaozjj666/data-analysis-agent/security/advisories/new>

若页面上看不到该入口（仓库尚未开启私密报告），请先开一个**不含漏洞细节**的 Issue 说明需要私密渠道，维护者开启后会与你联系。

报告中建议包含：

- 受影响的版本 / 提交号、Python / Node 版本与部署方式（本地 / Docker / Render）；
- 是否配置了 `APP_ACCESS_TOKEN`、S3 / PostgreSQL / LangSmith 等外部存储；
- 复现步骤或最小复现；
- 影响说明，例如未授权访问、路径穿越、沙箱逃逸、SQL 只读绕过、会话或上传数据泄露；
- 如有可能，附上修复建议。

**请勿粘贴真实数据集、表结构、列名样本或含个人信息的行**，请用合成数据复现。API Key、对象存储凭证与 `DATA_AGENT_DATABASE_URL` 也请脱敏。在修复发布前，请勿公开细节。

## 范围说明

- 覆盖后端 API、React 前端、上传与解析链路、受限 Python 沙箱、只读 SQL / MCP 数据面，以及本地或 S3 兼容的会话存储。
- 访问控制：未设置 `APP_ACCESS_TOKEN` 时服务只允许绑定环回地址，否则启动即失败；对外提供服务时必须设置令牌，并置于可信反向代理之后（见 `src/data_agent/middleware.py`）。
- 上传的数据集、会话与报告产物保存在 `DATA_AGENT_RUNS_DIR`（默认 `./runs`）或对象存储中，请自行负责目录权限、备份与清理。
- 不覆盖：上传数据本身的合规性、LLM 供应商与第三方数据源，以及分析结论的正确性。

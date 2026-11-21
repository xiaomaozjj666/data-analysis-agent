# 分析技能包（Skill）

把一次分析固化的 `analysis_recipe.json` 导出成**可分发、可版本化的技能包**：

```
<out>/<slug>/
├── SKILL.md              # 人/LLM 可读：适用场景、输入指纹、执行步骤、边界
├── analysis_recipe.json  # 机器可读：确定性工具调用参数
└── skill.json            # 清单：名称、版本、配方哈希、工具列表
```

来源：2026-11 竞品实地复核里，Trae 用 `SKILL.md` + 项目级/全局目录 + 市场分发技能，
Excel Copilot 支持"演示一遍录制技能"。本项目不改重跑内核——技能包只是把已有的
配方变成可复用资产。

## 1. 从会话导出

分析完成后，会话目录里会有 `artifacts/analysis_recipe.json`（finalize 阶段写入）：

```bash
python -m data_agent.skill export --session runs/<session_id> --out ./skills
# 自定义技能名（--name），已存在同名目录时需显式覆盖（--force）
python -m data_agent.skill export --session runs/<session_id> --out ./skills --name "季度销售复盘" --force
```

输出（JSON 摘要）：

```json
{"path": "/abs/path/skills/分析各区域销售趋势", "name": "分析各区域销售趋势",
 "slug": "分析各区域销售趋势", "version": 1,
 "recipe": "runs/<session_id>/artifacts/analysis_recipe.json",
 "files": ["SKILL.md", "analysis_recipe.json", "skill.json"],
 "recipe_sha256": "…", "tools": ["inspect_data", "statistical_analysis"]}
```

## 2. 按技能包重跑

```bash
python -m data_agent.skill run --bundle ./skills/分析各区域销售趋势 --data ./data/new_sales.csv
```

重跑走 `recipe.replay_recipe`：按配方顺序调用工具，**不再调用规划器/LLM**。
输出包含本次数据指纹与逐次工具调用结果，便于对比"导出时 vs 现在"。

## 3. 安装到项目级 / 全局技能目录

导出目录可以放在任何地方；要让其他 Agent（或下一次会话）稳定找到技能，用
`install` 把它复制进约定的技能根目录：

```bash
# 项目级：<cwd>/.data-agent/skills/<slug>/
python -m data_agent.skill install --bundle ./skills/分析各区域销售趋势

# 全局级：~/.data-agent/skills/<slug>/
python -m data_agent.skill install --bundle ./skills/分析各区域销售趋势 --scope global

# 自定义根目录（优先级最高，覆盖 scope 默认值与环境变量）
python -m data_agent.skill install --bundle ./dist/skill --root ./my-skills
python -m data_agent.skill list --root ./my-skills
```

`DATA_AGENT_SKILLS_DIR` 可统一覆盖项目级/全局级的默认根目录，适合把技能放进
容器挂载卷或只读镜像层。

安装与列出：

```bash
python -m data_agent.skill list
# {"root": ".../.data-agent/skills",
#  "skills": [{"slug": "...", "name": "...", "version": 1, "valid": true,
#              "recipe_sha256": "...", "tools": ["inspect_data"], "path": "..."}]}
```

安装规则（把技能包当不可信输入处理）：

- 只复制 `SKILL.md` / `analysis_recipe.json` / `skill.json` 三个约定文件；
- 拒绝符号链接与缺失文件；
- `slug` 必须匹配 `[A-Za-z0-9_\u4e00-\u9fff-]{1,64}`，防止 `../` 越界；
- 安装前校验 `skill.json.recipe_sha256`，被篡改过的配方拒绝落盘；
- 覆盖同名技能必须 `--force`；写入先落 staging 再整体替换，失败回滚旧版本；
- `list` 对坏包只标记 `valid: false` 与 `error`，不会因一个坏包让整张清单失败。

## 3.1 安装前强制安全扫描

安装的技能包等于把外部指令引入 Agent 上下文。对标 NVIDIA SkillSpector
（GitHub 趋势月增 27k★ 的技能安全扫描器）的公开评分口径，`install` 前会
自动做一次**纯静态扫描**（不执行包内任何内容）：

- 检测类别：指令覆盖（中英文）、零宽/隐藏文本、HTML 注释指令、
  数据外传（`curl|sh`、POST 上传）、密钥收集（环境变量/凭据文件）、
  混淆（base64 解码、eval/exec）、过度授权（`rm -rf /`、sudo、chmod 777）、
  约定外可执行文件；
- 评分：critical +50 / high +25 / medium +10 / low +5；配方含
  `run_python_code` 或包内有额外可执行文件时 ×1.3；
- 分级：≤20 LOW（SAFE）/ ≤50 MEDIUM（CAUTION）/ ≤80 HIGH / >80 CRITICAL；
- **HIGH / CRITICAL 默认拒绝安装**，确认来源可信后加
  `--accept-risk`（函数参数 `accept_risk=True`）越过，安装结果里记录
  `scan.accept_risk: true`；
- 也可单独扫描不安装：

```bash
python -m data_agent.skill scan --bundle ./skills/下载来的技能
python -m data_agent.skill scan --bundle ./skills/下载来的技能 --json
```

已知边界：规则是模式级子集（不做 LLM 复核与 OSV 在线查询），能拦住常见
恶意模式与明显注入，不能替代人工审查来源。

**SARIF 输出**：`scan --bundle <目录> --sarif` 输出 SARIF v2.1.0（GitHub
Code Scanning 兼容），`critical/high → error`、`medium → warning`、
`low → note`，逐条带文件与行号定位——可直接接入代码扫描门禁。

SKILL.md 顶部现在按 Agent Skills 规范写入 YAML front-matter
（`name` / `description` / `version` / `license`），`name` 与 `skill.json`
的 slug 一致；扫描会对不一致给出 low 级提示。

**`references/` 资源目录**（Agent Skills 规范的可选文档资源）：技能包可带
`references/` 放给人读的口径/方法文档。安装时与三件套同级校验并一起复制：

- 只允许 `.md` / `.txt` / `.json` / `.csv`，拒绝符号链接与 junction；
- 单文件 ≤5 MB、整目录 ≤20 MB，超限直接报错而不是静默截断；
- `references/` 内容按一等文本参与安全扫描（注入模式同样会被拦）；
- 可执行后缀（`.py` / `.sh` 等）在 references 里视为「约定外可执行」，
  计入评分倍率并默认拒绝安装；
- `list` 会给出每个技能的 `references` 文件数。

## 4. 边界

- 固化的是**工具调用参数**，不是模型推理；结论解释仍需重新生成。
- 重跑前会校验 `skill.json.recipe_sha256` 与 `analysis_recipe.json` 内容是否一致；
  分发包里的配方被改过会直接失败，不会静默执行。
- 每次重跑使用独立的临时工作区并在结束时清理，不会在 `runs/` 下无限累积。
- 数据 schema 变化（列名/类型/行数显著变化）时先核对 `SKILL.md` 里的指纹。
- 导出目录**不内嵌 metrics.json**。重跑时工作区是临时目录，指标定义不会从
  数据文件所在目录读取——请设置 `DATA_AGENT_METRICS_PATH`，或把 `metrics.json`
  放进 `$DATA_AGENT_RUNS_DIR/skill-<任务slug>/` 后再重跑。
- 覆盖已有目录必须 `--force`：技能包会被拷进其他 Agent 的技能目录，静默覆盖
  会让别人手里的旧版本凭空变化。

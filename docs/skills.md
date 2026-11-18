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

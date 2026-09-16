# 工程笔记

本文件沉淀本项目开发与维护中已验证的工程事实，供后续改动复用。

## 运行与重启

- 本地 API（uvicorn, 127.0.0.1:8000）以 supervisor 方式托管，不是 `run.ps1` 一次性拉起。杀掉 uvicorn 子进程会被自动重启。
- uvicorn 启动参数**无 `--reload`**。要让后端代码生效：① 清 `src/data_agent/**/__pycache__/*.pyc`（陈旧字节码）；② 用 PowerShell `Stop-Process -Force` 杀 uvicorn 进程；③ supervisor 自动用新源码重启。
- 前端走 Vite（5173）HMR；改动后建议硬刷新（Ctrl+Shift+R）确保拿到最新 bundle。

## 测试

- 跑测试：`.venv/Scripts/python.exe -m pytest tests/ -q`。
- Python `urllib` 取响应头用 `r.getheader('X')`；`dict(r.headers)` 在本机环境会漏掉 ETag/Content-Type 等头，曾导致误判。

## 鉴权

- `/preview` 等需鉴权端点：token 在 `.env` 的 `APP_ACCESS_TOKEN`，请求头 `X-App-Token`。

## 大数据图表的可读性（密度视图 · 服务端聚合 · LTTB）

**问题**：30 万行散点按"每点一个标记"渲染时，同一像素叠几十个半透明点，alpha 迅速饱和成一团灰噪声——品类色互相覆盖、密度差异被抹平（用户实测反馈"所有的点和数据都堆在一起，根本看不出来什么"）。直方图/箱线图更隐蔽：形状由分布决定，但 px 会把**全部原始数值**塞进 HTML 交给浏览器现算，30 万行时 HTML 3.2MB / 5.2MB，产物卡缩略图甚至渲染不出来。

**做法**（业界通行：datashader 光栅化、2D 直方图、seaborn jointplot）：

- `data_agent/density.py` 只做纯 numpy 聚合与文案，Plotly 与 ECharts 渲染**同一份网格与同一套档位色板**——不要让两个引擎各自算分箱，否则同一份数据两张图不一致。
- 散点 ≥ 2 万行（`DENSITY_MIN_POINTS`）自动切换：网格单格约 7px、记录数按 1-2-5 系列分 5~7 档着色、零记录格**完全透明**（datashader 同样规定 0 值不参与着色，不能拿背景色冒充数据）。有分组时**按组拆分面**（颜色只表达密度、类别用分面表达）；无分组时 jointplot 布局（主图 + 顶部/右侧边缘直方图，两侧必须与主图共用同一组 bin edges 才对得齐）。
- **聚集判定要先扣掉统计涨落**：均匀随机点云的"最密格"只是采样噪声，`noise_peak_ratio ≈ 1 + sqrt(2·ln N / λ)` 给出纯随机能造出的峰均比上限，低于门槛就直说"分布比较均匀"，不硬报热点。
- 极端值走"主体尺度"时网格不覆盖范围外数据，条数写在图上与解读里；**不要**再挂"全量视图"按钮（切过去只会看到角落一小块）。
- 直方图 ≥ 5 万行走服务端分箱（纵轴仍是真实记录数）、箱线图走服务端五数概括（`go.Box(q1=…, median=…, q3=…, lowerfence=…, upperfence=…)`）、小提琴图按组分层抽样、3D 散点按组分层抽样且把"已抽样 N/M 点"写在副标题上。实测 HTML：直方图 3.2MB→24KB、箱线图 5.2MB→22KB、ECharts 散点 11MB→214KB。
- 折线/面积在数值轴上走 LTTB（`chart_sampling._lttb_indices`）保峰；等距步进会把落在两个保留点之间的尖峰整段丢掉。类别轴仍按轴步长抽样（必须与 `xAxis.data` 对齐，不能按 LTTB 重排）。

**踩过的坑（复现成本很高，别再踩）**：

1. **plotly.js 会静默丢弃整条色标**：档位色标首尾不是正好 `0.0` / `1.0` 时直接回退成内置彩虹色（不报错、不告警）。相邻档用**完全相同**的边界位置（plotly 接受重复位置），不要用 `end - 1e-6` 这类近似值——末档就不落在 1.0 上了。
2. **共享 coloraxis 会被模板色板覆盖**：heatmap 在 plotly.js 里默认绑到 `coloraxis`，而 plotly.py 默认模板的 `layout.colorscale` 会按"是否跨零"自动挑 sequential/diverging 盖掉自定义色板。结论：档位色标写在 **trace** 上（`colorscale` + `zmin/zmax`），不要走 layout coloraxis。
3. **暗色脚本必须遍历全部子图轴**：分面/边缘直方图的轴是 `xaxis2/xaxis3/…`，只改 `xaxis/yaxis` 会在深色底上留下刺眼的浅色网格线；标题字体色是显式写死的深色，也要一起换。
4. **`df[[x, y]]` 在同名列上返回的是 DataFrame**：`pair[x].tolist()` 直接 `AttributeError`；取列一律用 `iloc[:, 0] / iloc[:, 1]`（x 与 y 填同一列就会踩到）。
5. **预计算统计量的 `go.Box` 必须显式给 `x=[组名]`**：每条轨迹只有 1 个"样本"，不给 x 时所有箱体会叠在同一个刻度上。
6. **图表编辑端点不能盲写 `marker.color`**：heatmap 没有 `marker` 属性，`go.Figure` 校验抛 ValueError（用户看到 500 和一句看不懂的报错）。按 `_MARKER_TRACE_TYPES` 白名单应用；图内存在"颜色即数据"的图型（heatmap / contour / histogram2d / image / splom）时改色返回 422 并解释原因——**不要部分生效**（密度图还叠着"最外围记录"散点，改一半会让用户以为已经改好了）。改标题要用**合并**而不是替换 `layout.title`，否则字号/对齐样式被一并抹掉。
7. **NO_PROXY 里的 `[::1]` 会让 httpx 直接不可用**：httpx 0.28 把 `[::1]` 当成"主机 + 端口"，构造客户端即抛 `InvalidURL: Invalid port: ':1]'`，于是所有模型调用失败、报错完全看不出跟代理有关。`config.sanitize_no_proxy_env()` 在 `AgentSettings.from_env()` 里清掉带方括号的条目，`tests/conftest.py` 调用同一函数（本机启动器会注入该变量，CI 不会）。

**验证方式**（单测之外必须做的）：

- 生成 30 万行**有结构**的数据（分类别不同分布 + 少量极端值），在真实应用里看缩略图与预览：`runs/api_bigtest0001` 就是为此保留的演示会话。注意 `session.json` 只有 `registry.create()` 会写——只生成 HTML 的话历史列表里看不到这个会话。
- 悬浮读格子：tooltip 应给出"销售额 689 ~ 722 / 利润 295~300 / 记录数 16（占 0.01%）"。分箱区间必须作为 `customdata` 显式带上——Plotly 的 heatmap hover 本身没有区间字段。
- 缩放：热力图缩放是栅格拉伸（不是 datashader 那种随缩放的动态重分箱），hover 仍准；这是有意取舍，别当 bug 去"修"。

**深度放大后看具体记录**：既然不做服务端动态重分箱，就用"随图带一份分层抽样点"补上：

- `density.sample_detail_points()`（纯 numpy）：按分组配额（每组至少 100，不足全取）、只取视口内记录。
- Plotly：`_add_detail_layer()` 加一条**默认隐藏**的 Scattergl，配图右上角两个 restyle 按钮；trace 名写明"抽样原始点（N 条）"。
- ECharts：没有图内按钮，改用**原生图例**——series 进 `legend.data`，`legend.selected` 里默认 false，点图例即开关；不依赖自定义 JS，单文件下载照常可用。
- 两边都只在**单面板**布局加：分面图上一整层点会落到错误面板（实测踩过）。解读文案必须写明按钮/图例在哪、是抽样、完整数据在同名 JSON——否则没人会知道那层点可以打开。
- 载荷 +约 145KB/6,000 点，与"几百 KB 的密度图"同一量级。

**可访问性（对比度）**：全站文字按 WCAG AA 校准过，方法是用计算样式实测而不是凭感觉——

- **CI 门禁**：`frontend/src/styles/__tests__/contrast.test.ts` 解析 `tokens.css`，把"文字令牌 × 承载文字的底色"矩阵在两套主题下逐一算对比度并断言 ≥4.5:1（毫秒级、零依赖）。**新增承载文字的表面底色时要把它加进 `SURFACES`**，否则该表面不会被约束。
- **浏览器实测（手工复核）**：起服务后用 Playwright 遍历所有"直接含文字"的元素、按祖先背景合成有效底色、逐条判 AA，覆盖 起始页/工作台(分析·数据·产物)/历史/命令面板/预览模态 × 浅暗两主题；另跑一轮"悬停态"审计——`--control-hover` 这类底色只在 hover 时出现，静息态审计根本看不见（暗色 `--fg-subtle` 压在它上面只有 4.17:1，就是这么发现的）。
- 三级文字令牌**按"最差背景"标定**，不是按页面底色：暗色下 `--control-hover` / `--canvas-inset` / `--neutral-muted` 同为 #2a2b2f，是最暗的可承载文字底；取值要让它仍有 ≥4.5:1（现值给出 15.3 / 8.1 / 5.9 的三级间距）。
- **实心 vs 文字必须分令牌**：`--accent-fg` 在暗色被提亮到 #7c7cf0 以保证"作为文字"的对比度，但同一值当**背景**时白字只有 3.5:1。所以白字压底的填充一律用 `--accent-solid`（浅/暗同值，白字 5.4:1）。
- 浮层徽章（如右上角引擎角标）要按**它自己的底色**校验：暗色下 `rgba(28,36,51,.92)` 上的品牌色小字只有 4.44:1，压深底色才达标。

**文档截图怎么重拍**：`python scripts/capture_docs_screenshots.py`（需 `pip install playwright && playwright install chromium`，以及本地已起服务、`.env` 里的 `APP_ACCESS_TOKEN`、演示会话 `api_bigtest0001`）。两个坑写在脚本注释里：① 缩略图懒加载且**离开视口会被 purge**，所以产物页截图不能用 `full_page`（折叠线以下的卡片会是空白）；② 也不能中途改视口——ECharts 迷你画布不会跟着重绘，会截成空白——要用高视口**新开一个页面**。

**三处"分层抽样"是同一思路的三个变体，不是意外重复**：`tools/builder._stratified_sample`（小提琴，按组配额、无硬上限）、`echarts_engine._stratified_3d_sample`（3D 散点，配额 + 收敛到硬预算）、`density.sample_detail_points`（密度细节层，额外做视口过滤）。差异是刻意的（各自的预算/过滤语义不同），合并成一个函数反而要引入模式开关；新增第四处前先看看能不能复用其中任何一个。

## 图表预览链路

- 图表为 ECharts/Plotly 内联 HTML，存于 `runs/<session>/artifacts/*.html`，由后端 `/preview` 注入 CSP + 内联 echarts bundle 后返回。
- 编码：写出层 `_atomic_write_text` 默认 UTF-8；CSV 读取候选 `("utf-8-sig", "utf-8", "gb18030")` 顺序探测。
- 乱码历史根因是前端 LRU 缓存（`useArtifactPreview` 的 `previewCacheRef`）按 `preview_url` 复用旧 HTML；已改为带 `If-None-Match` 的 ETag 条件请求（后端 `_preview_etag` + `_read_utf8_robust`）。

## 批量改生成类 HTML 的必踩坑（复用）

- **绝不用 `re.sub(r'<script>(.*?applyTheme.*?)</script>', NEW, flags=re.DOTALL)` 这类跨标签替换**：`re.DOTALL` 下 `.*?` 会跨过主脚本的 `</script>` 边界，把含 `echarts.init`/`var option` 的图表初始化主脚本整段删掉，导致文件损坏（图表白屏）。
- 正确做法：用负向前瞻阻止跨标签，如 `<script>(?:(?!</script>).)*?锚点(?:(?!</script>).)*?</script>`；或按脚本唯一锚点精确替换。改完务必校验 `echarts.init` 仍存在。
- **损坏恢复**：每个图表 HTML 都有 sibling `*.echarts.json`（完整 option）。可从它 + `<title>` + `.interpretation` 块（反转义）用 `echarts_engine._build_echarts_html` 重建。
- **ECharts formatter 需真实 JS 函数**：Python 端 `json.dumps` 会把函数源码序列化成带引号的字符串，前端 ECharts 当普通字符串标签渲染 → 用 `_JsFunction` 标记类 + `_serialize_option()` 在序列化后还原为无引号 JS 函数字面量。
- **图表 HTML 暗色切换**：由 `#theme-toggle` 按钮 + `_ECHARTS_DARK_MODE_SCRIPT`（`getIsDark`/`applyTheme`）实现；父页面 `PreviewModal` 在 iframe onLoad 注入应用主题。沙箱 iframe 无 `allow-same-origin`，`localStorage` 必须 try/catch。

## 自动选图（chart_type="auto"）

- 推断规则：时间序列（datetime/可解析日期串）→ line；两数值 → scatter；分类+数值 → bar（无 color 且类别≤8 → pie 构成占比，否则 bar）；单数值分布 → histogram；仅分类 → bar(count)；path_columns → sunburst；无 x/y 但 ≥3 数值列 → correlation_heatmap；dimensions≥3 → scatter_matrix；z → scatter_3d。
- 正确性兜底：auto 选 bar/pie 且 x 含重复行时，bar 走 `aggregation="sum"`，pie 先 `groupby(x).sum()`，避免重复类别错乱。

## 交互 UI 规范

- 交互类 UI 优先用 ReactBits 组件（`frontend/src/components/rb/`，已集成：Aurora/ClickSpark/CountUp/DotField/GlareHover/GradientText/Reveal/RotatingText/ShinyText/SplitText/SpotlightCard/StarBorder）。
- `GlareHover` 默认 `overflow:hidden` 会裁切子按钮的 translateX/box-shadow，侧栏场景必须放开并给流光层补 border-radius。
- 移动端侧边栏抽屉依赖 `.sidebar.is-open` 类；触控关闭用 onTouchStart/onTouchEnd 判断横向滑动。

## 常见问题

- `vite build` 首次会因清理 dist 失败：先 `rm -rf dist` 再 build。
- 图表 iframe 主题联动用 postMessage 双向桥接（`useArtifactPreview` 的 `withThemeBridge`），避免 sandbox 跨域死代码。
- 隐藏无图表的引擎筛选标签（`ArtifactCenter.tsx`）：仅渲染 `enginesPresent`，筛选后无图显示提示而非静默空白。

## 新增受控工具需要同步的四处

加一个工具不是改 `builder.py` 一处就完事，漏掉任何一处都会出问题：

1. `tools/builder.py` 的 `build_tools`：定义闭包工具 + 加进返回列表（列表顺序即模型看到的顺序）。
2. `tests/test_agent.py::test_deepseek_provider_*`：对工具名做**精确集合断言**，必须同步加入新名字。
3. `README.md` 工具数量与清单（"受控数据工具集（N 个内置工具）"）。
4. `prompts.py` 的系统提示词：不写引导模型就不会用新工具（会退回去手写 `run_python_code`）。
5. 若新工具产生跨模块辅助函数，按现有约定放 `tools/_<name>.py`（纯函数）并在 `tools/__init__.py` 里 re-export。

## 行数基线（source_row_count）与快照

- `source_row_count` 是 `clean_data` 20% 安全下限的参照，只在 `load()` 时锚定原始上传行数。
- 跨源合并会显著改变行数规模，所以 `join_datasets` 在护栏通过后调用 `adopt_dataset(reset_source_baseline=True)` 重置基线。**不重置会出现真实障碍**：主数据 10 行、合并后剩 1 行时，下限按旧基数算是 2 行，合并结果连去重都会被拒绝。
- 因此快照必须覆盖基线：`snapshot_state()` 返回 `WorkspaceSnapshot(dataframe, files, version, source_row_count)`，`restore_state()` 无条件还原基线。只恢复 DataFrame 会让"合并步骤失败回滚"后基线仍停留在合并后的规模，护栏在同一会话内失效。
- 改这个元组结构要同步 `tests/test_workspace.py::test_snapshot_state_handles_missing_artifacts_dir` 的解包。

## 合并护栏的设计取向：不提供绕过参数

`tools/_join.py` 的护栏全部由模块常量固定（`MAX_ROW_MULTIPLIER`、`UNMATCHED_WARN_RATIO`），没有"放宽阈值"的入参，与 `clean_data` 的护栏同策略。理由是这些护栏拦的是**不抛异常的静默错误**：

- 键含空值 → 空值与空值不相等，这些行静默丢失；
- 键类型不一致（`"1001"` vs `1001`）→ 一行都匹配不上，产出空表；
- 键不唯一 → 笛卡尔放大，聚合指标随分母一起虚增数倍。

注意 pandas 对"数值键与文本键合并"会自己抛英文错误并建议改用 `concat`，那不是用户想做的事；`merge_datasets` 捕获后换成"先 repair_data_format 统一格式"的中文引导。indicator 列名也要探测：源表自带 `_merge` 列时 pandas 会直接报错。

## 指标口径登记（metrics.json）

- 加载优先级：`DATA_AGENT_METRICS_PATH` 指向的全局文件 > 工作区根目录的 `metrics.json`；两者都没有则返回空列表，规划提示词退化成原来那样（`{metric_context}` 渲染为空串）。
- 定义文件写坏时**直接报错**，不降级为忽略：静默跳过用户登记的口径会让分析用错定义却看起来一切正常，比失败代价大。错误消息带文件路径与 1 起算的条目序号，便于直接改 JSON。
- 选择策略：与问题匹配的条目优先，其余按文件原顺序补足到 6 条。不能只注入命中项——像"分析这份销售数据"这种不点名指标的问题会完全看不到定义，那就失去了语义层的意义。

## SQLite 数据源（把 .db 当作一种数据格式，而不是新接口）

- **设计取舍**：会话由"上传一个文件 → `load()` 产出表格"创建，没有"给已有会话挂旁路文件"的接口。所以数据库文件不是新端点也不是新界面，而是直接进 `SUPPORTED_EXTENSIONS`，由 `_read_table` 分派到 `sqlite_source.read_default_table()`：**默认载入行数最多的表**，并把这个选择写进 `load_warnings`（否则用户会以为载入的是别的主表）；其余表用 `query_database` 查询。这样零前端改动、零新依赖（stdlib `sqlite3`）。
- **只读是三层叠加的**：`mode=ro` 的 URI 连接（驱动层拒绝写入，绕过语句白名单也写不进去）+ 语句前缀白名单（只放行 `SELECT`/`WITH`，`PRAGMA`/`ATTACH`/DDL/DML 逐个给针对性说明）+ 结果行列上限。
- **超时必须用 progress handler**：SQLite 没有语句级超时，`connect(timeout=)` 只管锁等待。一张跨表笛卡尔积会一直跑下去把分析线程占死，所以 `_query_deadline` 用 `set_progress_handler` 每 1000 个虚拟指令检查墙钟预算，超时返回非零使查询中断（`OperationalError: interrupted`），再翻译成可执行的中文提示。`count_rows` 也复用它，超预算返回 -1（"看一眼结构"不该变成长任务）。
- **表名必须先收敛再拼接**：`PRAGMA table_info(<name>)` 的表名无法用占位符绑定，只能拼字符串，所以 `describe_table` 先把入参比对真实表列表，杜绝注入。
- ⚠️ **与图表标识符护栏的相互作用**：把 `GROUP BY` 结果 `adopt` 成数据集后，分组列在结果里是"每行一个取值"，会命中 `_looks_like_id_column`（命名像 ID 且唯一率 ≥ 0.5）而被图表工具拒绝。**这是既有护栏在正常工作，不要为了数据库功能去放宽它**；需要画图时取行级结果（保留类别的多行重复），或在 SQL 里把分组列改名成非 ID 风格。
- **前端扩展名清单必须与后端同步**：`App.tsx` 的 `accept` 与 `EmptyWorkspace.tsx` 的 `SUPPORTED_EXTENSIONS` 共同决定用户能选哪些文件。历史上它们只列了 7 种结构化格式，导致 README 宣称的 PDF/TXT/Word 在界面上根本选不到；现已与后端 13 种对齐。**改后端支持格式时，这两处一起改。**

## Postgres 数据源（postgres_source.py）

- **驱动是可选依赖**（`pip install ".[postgres]"`，dev 环境默认包含），模块内 try/except 导入；未安装时 `is_configured()` 为 False，`connect_readonly` 给出可操作的中文安装提示而不是裸 ImportError。**不要**把它挪进运行时依赖——只分析 CSV 的用户不该背一个编译版 libpq。
- **只读在服务端，不在客户端**：连接后先 `autocommit=True` 执行 `SET default_transaction_read_only = on` 和 `SET statement_timeout`，再回读 `SHOW transaction_read_only` 确认生效（没生效就报错断开）。两个 SET 必须在 autocommit 下执行，否则落在事务里、事务结束就被回滚，保护等于没设。
- **超时用服务端 `statement_timeout`**，与 SQLite 的 progress handler 是两套机制（SQLite 没有服务端超时）。Postgres 超时抛 `QueryCanceled`（SQLSTATE 57014），要 `rollback()` 清掉中止的事务再抛中文提示——只读事务里出错后不回滚，后续语句会继续报错。
- 连接串走 `DATA_AGENT_DATABASE_URL` 环境变量，与 S3/R2 凭证同一约定，**不要**再引入 keyring（无头服务器没有后端，会失败）。
- CI 用 GitHub Actions 的 **service container**（postgres:16 + 健康检查）跑真实集成测试；测试在连不上时**自动跳过而不是失败**，本地无 Docker 的环境不受影响。判断可用性要在 fixture 里真实连接一次，只看环境变量是不够的。
- ⚠️ 测试要防 xdist 并发互踩：每个用例用唯一表名（`da_test_<uuid>`）并 try/finally 清理；建表/删表走独立的**可写**管理连接，被测的只读连接只负责查。

## MCP 数据面服务（mcp_server.py）

- **只开数据面，不开分析流程**：暴露 list_sessions / open_dataset / inspect_data / sql_query 四个工具。完整分析流程是分钟级 LLM 长任务，塞进 MCP 工具调用等于多一个有状态长任务入口，两条状态机没有收益——它走 HTTP API。开放面做粗是刻意的：给其他 Agent 的是"受控的数据访问"，不是内部工具原样透出。
- **独立进程，不依附 FastAPI**：会话以 `runs/<id>/session.json` 落盘自描述，MCP 服务按同一 `DATA_AGENT_RUNS_DIR` 自建 `SessionRegistry` 即可读到 HTTP 服务创建的会话。不要 import `data_agent.api` 的单例——那会拖起整个应用装配。
- **注册表的 `get()` 抛的是 fastapi 的 HTTPException**：MCP 工具层要把它转成 `ValueError`（带"用 list_sessions 查看"的指引），否则客户端看到的是 HTTP 语义的异常。
- **测试走官方 SDK 的内存客户端**（`mcp.shared.memory.create_connected_server_and_client_session`），经真实协议（initialize/tools/list/tools/call）交互，任何io 运行时用 `anyio.run(scenario)` 包住即可，不需要 pytest-asyncio 插件。**必须断言错误契约**：工具抛异常要以 `isError=True` + 中文原因返回、且协议流不中断（出错后还能继续 list_tools）——这是 MCP 客户端的实际依赖行为。
- MCP SDK 也是可选依赖（`extras mcp`，dev 默认包含）；未安装时 `create_server` 给中文提示。
- **边界**：stdio 传输由本地受信客户端拉起，天然无网络暴露面；不要把它挂到网上——远程场景走带令牌与限流的 HTTP API。

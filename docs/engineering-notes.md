# 工程笔记

本文件沉淀本项目开发与维护中已验证的工程事实，供后续改动复用。

## 运行与重启

- 本地 API 用 `run.ps1` / `run.bat` 拉起（uvicorn, 127.0.0.1:8000）；进程管理器托管时杀掉 uvicorn 子进程会被自动重启。
- uvicorn 启动参数**无 `--reload`**。要让后端代码生效：① 清 `src/data_agent/**/__pycache__/*.pyc`（陈旧字节码）；② 重启服务（托管场景直接杀 uvicorn 进程，由管理器拉起）。
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
7. **NO_PROXY 里的 `[::1]` 会让 httpx 直接不可用**：httpx 0.28 把 `[::1]` 当成"主机 + 端口"，构造客户端即抛 `InvalidURL: Invalid port: ':1]'`，于是所有模型调用失败、报错完全看不出跟代理有关。`config.sanitize_no_proxy_env()` 在 `AgentSettings.from_env()` 里清掉带方括号的条目，`tests/conftest.py` 调用同一函数（部分本地开发环境会注入该变量，CI 不会）。

**验证方式**（单测之外必须做的）：

- 生成 30 万行**有结构**的数据（分类别不同分布 + 少量极端值），在真实应用里看缩略图与预览：`runs/aapi_bigtest0001` 就是为此保留的演示会话。注意 `session.json` 只有 `registry.create()` 会写——只生成 HTML 的话历史列表里看不到这个会话。
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
- **对比度之外还要查**（浏览器实测，11 个视图/模态 × 浅暗）：交互元素的可访问名称、Tab 焦点链是否都有可见焦点环、`prefers-reduced-motion` 下装饰动画是否真的停、以及 414px 下的横向溢出。当前结论：可访问名称 0 缺失、8 个 Tab 停靠点全部有 2px 焦点环、减弱动效下 0 个元素仍在播动画、页面级横向溢出为 0。
- 窄屏下唯一"超出视口"的是字段质量表（447px 内容 / 388px 容器）——它的容器是 `overflow: auto`，**属于可横向滚动的面板而不是被裁掉**（实测设 `scrollLeft` 能滚动、页面 scrollWidth 仍等于视口宽）。同一位置还暴露出"视图外 N 条"提示在 414px 下压住轴标题，已把提示下调一行（`y=-0.10`）。

**性能与体积预算（tests/test_bigdata_budget.py）**：把"实测记录"变成会失败的断言，否则一次不小心的 `.tolist()` 或丢掉抽样就能让 30 万行图表从 2 秒变 30 秒、从 200KB 变 10MB。

- 体积与点数上限是**确定性**的，卡得紧（HTML <2MB、JSON <3MB、缩略图 ≤2500 点）；时间断言只做"灾难性回归"护栏（12s），因为 CI runner 在同类工作上慢 4~5 倍（LTTB 用例本机 0.18s / CI 0.94s）。
- 这套用例第一次跑就抓到真问题：**ECharts 30 万行密度图要 7.3 秒，而 Plotly 只要 1.8 秒**。Profiler 显示时间不在渲染里，而在 `ensure_echarts_bundle()`——它**按会话**从 CDN 下载 1MB 的 `echarts.min.js`，每个新会话的首张 ECharts 图都要重下一次。改为机器级共享缓存（`runs/_bundles`，各会话硬链接引用）+ 服务启动时后台预热（`api._warm_chart_bundles`），ECharts 降到 2.5 秒；离线且有缓存时照常可用，无缓存时返回 None 让调用方 fallback 到 CDN 直引（`tests/test_bundle_cache.py` 覆盖这四种情形）。
- 教训：**"首图慢"和"渲染慢"要分开量**。cProfile 里渲染只占 1.8s，剩下 6s 全在第一次网络 I/O；只看总耗时会误判成渲染性能问题。

**文档截图怎么重拍**：python scripts/capture_docs_screenshots.py（需 pip install playwright && playwright install chromium，以及本地已起服务、.env 里的 APP_ACCESS_TOKEN、演示会话 api_bigtest0001）。两个坑写在脚本注释里：① 缩略图懒加载且**离开视口会被 purge**，所以产物页截图不能用 full_page（折叠线以下的卡片会是空白）；② 也不能中途改视口——ECharts 迷你画布不会跟着重绘，会截成空白——要用高视口**新开一个页面**。

**一次分析的耗时构成（实测时间线，不是估算）**：用 SSE 事件时间戳量过完整一轮"按地区对比销售额 + 画关系图"（4k 行样例数据）：

| 阶段 | 优化前 | 优化后 |
|---|---|---|
| 总耗时 | **427s** | **230s** |
| 工具调用次数 | 62 | 36（单步上限 6） |
| replan 阶段 | 37.5s（6 轮，每轮 ~6s） | 5.5s（正常进度跳过咨询） |
| 单轮 LLM（带 thinking/high） | ~2.8s | 不变（属模型档位，用户可调） |
| 报告生成静默期 | 45s（只发 15s 心跳） | 不变 |

- **单步工具调用上限**（`AGENT_MAX_TOOL_CALLS_PER_STEP`，默认 6）：ReAct 循环原本没有工具调用预算，实测一步能烧掉 23 次调用、每次都要一次 thinking 往返。现在超出即结束本步、用已有结果写小结（`exit_behavior="end"`，属可预期降级而非报错）。
- **正常进度下跳过 replan 咨询**：`nodes/replan.py::_needs_replan` —— 只有"有步骤失败 / 计划已执行完 / 连续两步零产物"才花那次 LLM 往返；其余情况沿用原计划剩余步骤，`replan_reason` 里写明是"跳过咨询"而不是模型的决定（不假装是模型判断）。
- **降级要可见**：规划失败（模型没返回结构化计划）时 `plan_source="fallback"` 会随 `plan_ready` 事件送到前端，计划面板显示"以下为内置默认步骤"。此前 thinking 模式拒绝 `tool_choice` 导致 planner 100% 失败却只有日志知道。
- **墙钟上限**（AGENT_MAX_ANALYSIS_SECONDS，默认 600s）：同一句任务在不同数据上实测 230s（顺利）
  也可能 680s（样例数据里没有利润列，模型得现场拼数据），没有上限时用户无法预期何时拿到结果。
  超预算后 replan 与 execute 都不再开新步骤、直接汇总已完成部分，reason 与步骤小结里写明"已用完预算"
  ——降级要说出来，不能悄悄少做几步。
- 想更快：调低 `AGENT_REASONING_EFFORT`（如 `medium`）或模型档位——每轮 thinking 是本项目最大的单点延迟，但会牺牲推理质量，属用户取舍，不擅自改默认。

**结构化输出必须绕开 `tool_choice`**：`model.with_structured_output(Schema)` 会强制 tool_choice，DeepSeek thinking 模式直接 `400 Thinking mode does not support this tool_choice`。改用 `bind_tools([Schema])`（auto）+ 正文 JSON 兜底解析（`_ToolSchemaRunnable`），并注意 JSON 扫描要**跟踪字符串状态**——分析文本里 `{"steps": ["统计 profit} 列"]}` 这种带右花括号的字符串很常见，纯数括号会提前截断。

**图表表达完整性（"看起来像数据错了"的三类）**：

- **柱状图零基线**：三地区销售额 66~68 万而轴从 21 万起，1.9% 的差异被放大成肉眼上的三四倍。`_echarts_value_axis(zero_base=True)` 对柱状图强制含 0；折线图不受此限（斜率才是信息）。
- **标题说"堆叠"、图上是并排柱**：工具原先没有堆叠能力。新增 `stacked` 参数（ECharts `series.stack` / Plotly `barmode`），轴范围按堆叠总和计算，解读文案同步区分"堆叠/分组"，并在工具描述里告诉模型何时该用。
- **点云糊成色块**：点径/透明度改为按点数分档的共享表 `SCATTER_POINT_STYLE`（两引擎同值，观感一致），4 千点从 10px/0.78 降到 5.5px/0.60，分组色才混得开、放大后仍能分辨单点。

**3D 散点的可读性**（`_refine_scatter_3d`）：默认 `px.scatter_3d` 是一团看不出深度的毛球。三处调整：点径比 2D 再小一档并加白色细描边（留缝才有前后层次）、立方体用**对数压缩比例**（`_scatter_3d_aspect`：`data` 会把小量程轴压成一条缝、`cube` 丢失形状信息，取中间并夹在 [0.75, 1.25]）、相机抬高俯视角（默认视角下竖轴几乎与视线平行，整团点看起来像一张平板）。另外**不要给分类着色的 3D 图强开 `showscale`**——会画出一条 1~9 的"类别编号色条"，看着像多了一个连续维度。

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

## 新增受控工具需要同步的五处

加一个工具不是改一处就完事，漏掉任何一处都会出问题：

1. 对应域的 `src/data_agent/tools/_tools_*.py`（`build_*_tools`）里定义工具，再到 `builder.py` 的 `build_tools` 组装列表里加入该工厂（列表顺序即模型看到的顺序）。
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

## 2026-10 批次：沙箱进程隔离 / 隐私 / 指标可执行化 / 配方重跑 / 报告导出

各模块的设计动机见其 docstring（`sandbox_runtime`/`sandbox_worker`/`privacy`/`facts`/
`recipe`/`report_export`/`dataframe_source`/`tools/_stats`），这里只记跨模块的要点：

- **结论核验不再调用第二个模型**：`verification.py` 从 finalize/replay 的正文提取数字，
  对照 `extract_facts` 的 source/field/trace_index/json_path；按数字所在分句绑定字段，
  同字段不同值是 conflict，无匹配事实是 unsupported，按原文小数位容差接受舍入，
  且只在事实带单位时换算。
  结果写入 `report-verification.json` 并注册为 verification 产物，同时追加 Markdown 核验表。
- **沙箱从线程进程化**：用户代码经 `python -m data_agent.sandbox_worker` 在子进程执行，
  `sandbox_runtime.py` 刻意不依赖 langchain/FastAPI，否则子进程启动会把整个 Web 栈拉起来。
- **SQL 墙钟预算可配置**：`DATA_AGENT_SQL_TIMEOUT_SECONDS`（1-120，默认 30），取代硬编码
  5s——5s 会掐死合法的百行级 JOIN。SQLite 走 progress handler、Postgres 走
  `statement_timeout`，两处都读 `sql_guard.query_time_budget_ms()`（导入期求值，进程内不变）。
- **SQLite 默认表 10 万行上限**：超大表不再整表进内存，提示改用 `query_database`
  聚合后 `adopt`；CSV 会话由 `dataframe_source.py` 提供同名只读表 `data`。
- **样例脱敏只动送出的副本**（privacy.py），活动数据集一字不改——别把它扩展成
  "落库前清洗"，那是 clean_data 的职责。
- **指标表达式是 AST 白名单**（四则/幂/模 + 列名，拒绝属性/调用/下标），
  不是 `eval`；放宽白名单前先想清楚它会被用户提交的 metrics.json 注入。

## 沙箱统计库白名单（scipy / scikit-learn / statsmodels）

沙箱原来只允许 pandas / numpy / math / statistics / datetime / re / json /
collections / itertools / functools，超出 12 个受控工具的问题只能降级。2026-11
把白名单扩到可选统计库，边界按"新增的是计算能力，不是 I/O 能力"设计：

- **两层闸门都改，缺一不可**：父进程 `_validate_code` 做 AST 审查，子进程
  `_safe_import` 是第二道（`run_user_code` 不经过 AST 审查，**别把静态检查当唯一防线**）。
- **文件名/模块名拦截用"属性名 + 别名还原 + 模块前缀"三种口径**：
  属性名层直接拒 `loadmat` / `savemat` / `mmread` / `wavfile` / `load_svmlight_file` /
  `webuse` / `get_rdataset` / `fetch_*`（前缀规则）/ `Parallel` / `delayed` / `joblib` 等；
  `import scipy as sp; sp.datasets.face()` 靠 `_collect_import_aliases` 把别名还原成
  `scipy.datasets` 再按前缀拒绝；`scipy.io` / `scipy.datasets` / `statsmodels.iolib` /
  `statsmodels.tools.web` 按模块路径整棵拦截。只按顶层白名单放行的话，
  `import scipy.io as sio` 会直接绕过去（`import a.b as c` 的 `IMPORT_FROM` 不经过
  受限 builtins 的 `getattr`）。
- **星号导入一律拒绝**：`from scipy import *` 会把 `io` / `datasets` 直接绑成变量，
  等于把上面所有入口审查清零。
- **joblib 默认后端钉成 threading**：sklearn 的 `n_jobs>1` / `cross_val_score` /
  `GridSearchCV` 默认走 loky 另起子进程——子进程内存不计入父进程盯着的那一个
  RSS，超时收割也只看直接子进程。导入 sklearn / statsmodels 后立刻改写
  `joblib.parallel.DEFAULT_BACKEND`（joblib 1.6 已不再读 `JOBLIB_MULTIPROCESSING`，
  实测默认后端是 loky）。线程后端仍在同一进程，护栏继续生效。
- **未安装要能行动**：只在"顶层模块整体缺失"（`exc.name` 无点且等于请求的顶层）
  时把 `ModuleNotFoundError` 改写成 `pip install "data-analysis-agent[stats]"` 的
  `ImportError`；已安装包内部子模块损坏时保留原异常，避免把"装坏了"误报成"没装"。
  父进程 `_CHILD_EXCEPTIONS` 只映射 `ImportError`，所以子进程必须抛 ImportError 而不是
  ModuleNotFoundError——`tools/_sandbox.py` 未改动也是这个原因。
- **成本**：本机冷启动实测 `scipy.stats` 1.4s、`sklearn.linear_model` 2.5s（15s
  超时内），`run_user_code` 不预导入这三个库——不写 import 的沙箱调用不为它们付费。

## 取消告警分支的测试为什么不能走 HTTP 断开（平台差异实证）

`routers/analysis.py` 的取消分支（`_handle_stream_cancel`）曾经通过"客户端断开流"来测，
结果 Windows/Proactor 本地 6/6 失败、CI/Linux 间歇绿：**传输层把取消注入协程的方式
因平台而异**——断开若落在 `yield "started"`（worker 尚未 start）处以 GeneratorExit 收尾，
`except CancelledError` 分支根本不进入。修法：把该分支从闭包提取为模块级
`_handle_stream_cancel(record, session_id, worker, loop)`，测试直接以真实 record +
阻塞在 `Event` 上的伪 worker 线程驱动，完全确定。教训：**依赖传输层断开时序的测试
不可写**，把分支提为可显式传参的函数才是解。附带修正：告警消息里的窗口秒数改为
插值（原硬编码 "5s"，而窗口是可配置的——消息在说谎）。

## 代理环境变量会卡死 vitest worker（前端测试"无输出/超时"真因）

部分本地开发环境（带代理注入的工具链）会设置 `HTTP_PROXY/HTTPS_PROXY` +
`NODE_USE_ENV_PROXY=1`，undici 的 EnvHttpProxyAgent 会维持与代理的空闲连接——
vitest 收尾终止 forks worker 时等不到退出，全部报 `Timeout terminating forks worker`，
测试汇总丢失（显示 no tests / N errors）。
**解法**：`env -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy npm test`。
CI 无这些变量不受影响。同族坑：某些受管环境里 pip 做**卸载/升级**会触发批量删除
守卫，venv 级依赖变更一律重建环境 + 后台安装。

## 覆盖率门槛在哪、怎么过（2026-10 实战）

- **门槛不在 pyproject**：`--cov-fail-under=97` 写在 ci.yml 的 pytest 命令行
  （`--cov=data_agent --cov-report=term-missing`）。本地裸跑 `-n 4` 不触发，
  "本地全绿、CI 挂在 Coverage failure: total of 96"就是这种——改 src 后用 CI 同款
  命令本地验证一遍再推。
- **缺口定位**：term-missing 报告逐文件给出缺失行号，对照写定向测试。迁移批次
  （一次 +800 行新模块）曾把总覆盖率稀释 1 个点，模块落地时同步补测试才是正解。
- **子进程不进覆盖率**：`python -m data_agent.sandbox_worker` 的集成测试验证的是
  进程边界，模块本体要在测试进程内直调 `main()` 才计入覆盖。两种测试都留。
- **测试文件的追加要用精确 Edit**：heredoc `cat >>` 一旦重复执行就是 F811 重复
  定义 + 字典重复键，ruff 报错只在 CI lint 步骤出现，本地截断输出容易漏看。

## 竞品文档的数字会腐烂：复核批次与"动态取证"惯例（2026-11 批次）

2026-11-17 用 web-crawler 对六类竞品做了官方一手页实地复核（方法、抓取矩阵与逐条结论见
`docs/competitive-analysis.md` 第六节），顺带发现竞品文档里的"工程事实数字"第二次腐烂：
`src/data_agent` 文件数 39 → 88、`def test_` 计数 733 → 1114、"10 个工具"与 11 个的
内部矛盾——上次（2026-10）已修过一次工具数。三条惯例固化：

- **文档里的数量一律标时点**：写"39 个文件 / 733 项测试"时附"（2026-09 时点）"，
  或者干脆只写"全部核心路径"不给精确数；精确数永远以 `git ls-files | grep -c` /
  `grep -c "def test_"` 现场取证，不靠文档。
- **改判在原位留注，不静默改旧文**：初查结论按当时事实成立、现在不成立的
  （如"只能靠文件上传"、"Data Agent 是 FineBINEXT 内置"），在原文位置加
  "2026-11 复核注"，保留当时为何成立——这与全局准则的改判规则同源。
- **官方一手页 > 二手评测**：本次逐句核对后修正了初查的两处偏差（FineBI
  Data Agent 归属、WorkBuddy 护栏表述），并新增官方才有的硬口径（Power BI
  语义模型准备前置、ChatGPT 沙箱禁外呼、DAX 元数据降级阈值）。抓取产物存
  web-crawler 仓库 `.research/agent-data-analysis/`（不入库），文档只留
  URL + 逐字引文，保持本仓库轻量且结论可回溯。

## 2026-11 批次：定时任务 / 技能包 / PDF / 数据源注册表的工程事实

本轮把竞品复核（`docs/competitive-analysis.md` 第六节）里可验证的缺口落成代码，
沉淀几条跨模块取舍：

- **automation 不引调度框架**：`run-due` 交给 cron / Windows 任务计划，`loop` 只做
  前台演示；进程内不起守护线程，避免与 Web 生命周期纠缠。异常检测是纯统计
  （z-score / 环比 / 零基线起跳），`min_samples` 默认 3，样本不足不下结论。
- **推送顺序是"先落盘再发"**：`history.jsonl` 与 `alerts.jsonl` 先写，再 POST；
  推送失败只写 `state.last_webhook_error`，不回滚任务状态。webhook 做两段校验：
  登记阶段只拦 scheme/账号/IP 字面量（不做 DNS，避免解析阶段网络 I/O 与整份
  任务列表加载失败）；发送阶段解析 DNS 并对**每一跳重定向**重新校验，防公网
  地址 302 到内网/元数据地址。**2026-11-18 改判**：发送侧已改为「一次
  getaddrinfo 同时校验并钉扎 IP」，连接直连已校验地址，DNS rebinding 窗口
  关闭；此处原「未做 IP 钉扎」的边界不再成立。**2026-11-24 补**：DNS 解析
  结果落在 198.18.0.0/15（RFC 2544 基准段，Clash/Mihomo 等 fake-IP 模式
  的标记段）时按「代理前置」放行——该段不可公网路由，若按内网拒绝，这类
  机器上所有公网 webhook 都会在发送前失败（实测）；IP 字面量以及
  127/10/172.16/192.168/169.254 等私网段仍一律拒绝。
- **失败隔离边界**：`run-due` 对每个任务的 due 判断与执行都包在独立 try 里，坏
  `last_run_at` 按"从未运行"降级；失败写 `last_status=error` + `next_retry_at`
  （默认 5 分钟），避免 loop 每秒重试。任务定义文件本身非法时整批 fail-fast，
  这是刻意选择（静默跳过配置错误更危险）。
- **同任务并发用跨进程锁**：`<task>/.lock`（Windows `msvcrt.locking` / POSIX
  `flock`，非阻塞）；拿不到锁返回 `skipped=locked`，不重复写历史、不重复推送。
- **口径版本化**：每次运行按加载时读到的**同一份字节**算 `metrics_sha256`，
  历史只与相同哈希比较；口径文件一改基线自动重置。分析侧在开始前固定
  `metrics_snapshot`（path/sha256/size）交给溯源，避免结束后才取哈希导致
  "记录的口径 ≠ 实际用的口径"。
- **skill 导出是"先备份再替换"**：`staging` 临时目录 + `os.replace`；覆盖时先把
  旧目录改名为 `.bak-*`，替换失败回滚，避免 Windows 目标被占用时"旧包新包都没了"。
  `run` 前校验 `skill.json.recipe_sha256`，分发包被改过直接失败；每次重跑用独立
  临时工作区并清理，不再在 `runs/` 下累积 `skill-*` 目录。
- **PDF 用 reportlab 可选运行时依赖**：内置 CID 中文字体 `STSong-Light`，无外部
  字体文件；超长图按 A4 可用高度等比缩回（否则 `LayoutError` 会把导出变 500）；
  缺依赖时 `build_pdf` 抛 `RuntimeError`，路由返回 503。Dockerfile / Render 构建
  命令显式安装 reportlab，避免部署开箱 503。
- **数据源先做可验证前置**：Snowflake / BigQuery / Databricks 无真实凭据不可验证，
  按项目规矩不合入占位驱动；已落 `data_sources.py` 注册表（只读保证等级 / 超时
  机制 / 标识符引号）与方言感知 SQL 护栏（注释、反引号、美元引号、数仓禁用语句、
  `quote_identifier(dialect=...)`），真实驱动只差"模块 + 注册 + 跳过式集成测试"。

## 2026-11-19 性能批次：实测数字、两次优化与一次否决

方法：独立基准脚本（temp 目录，warm-up + `perf_counter` + 中位数），随后对
最慢路径跑 cProfile；所有结论都带"改前/改后"数字，不凭感觉优化。

### 已落地

- **相关分析 p 值（`tools/_stats.py`）**：200k 行 × 7 数值列
  `0.472s → 0.025s`（约 19×）。做法：相关矩阵只算一次，p 值由 r 与有效样本量
  按 t 分布导出（与 `scipy.stats.pearsonr` 的 beta 公式数学等价，实测相对误差
  ~1e-13）；常数列判定从每对 `nunique()` 改为掩码后 `O(n)` 全等比较。
  回归：`tests/test_coverage_fill.py::test_correlation_p_values_match_scipy_and_constant_guard`
  逐对比对 scipy，覆盖常数列/完全线性/NaN 掩码三种边界。
- **离线验收（web-crawler `_offline_verify.py`）**：坏链密集归档（26k 引用、
  13.5% 缺失）主因是缺失分支对同一目标重复 `resolve()`（3.5 万次系统调用，
  96% 冗余）——按目标缓存后同类样本墙钟约 **−43%**；再把 `contained()` 的
  `is_relative_to` 换成词法前缀快路径（只加速"确在 root 内"的判定，词法在外
  仍走原解析），再降约 **10%**。
  等价性验证不是靠读代码：用 `git worktree` 在旧提交跑同一归档，与新实现
  输出 JSON **逐字段相等**（缺失/越界/未验/示例全一致）。

### 实测后否决

- **共享 factorize 优化 profile 冷路径**：子代理测得冷 profile 0.333s 中
  `duplicated()` 占 57%、`nunique()` 占 37%，看似可共享一次 factorize。
  实测三种实现（200k×15 混合列）：现有 0.124s、factorize + `np.unique(axis=0)`
  0.197s、factorize + MultiIndex.duplicated 0.120s——**没有显著收益**，且
  `np.unique` 更慢。结论：pandas 的哈希去重已接近最优，不做该改动；这条
  负结果写下来，避免下一轮重复尝试。

### 其它基线（无需优化）

`sha256_file` 100MB 807 MB/s；流式上传 50MB 886 MB/s、RSS 不随文件增长；
CJK 500KB 正文抽取 0.09s；crawl state 5000+20000 条目 round-trip 约 20ms；
报告原子写约 3.5–5.0ms/次（其中 fsync 约 0.5–0.75ms，主要成本是
mkstemp + os.replace）。Windows 文件系统波动可达 ±40%，绝对耗时门槛类断言
必须留足抖动余量并叠加"算法复杂度"断言，不能只靠单次阈值。

## 大数据基准（2026-11-19）

2026-11-19 优化：`read_metrics` 改用 `csv.reader` 索引访问、10^-9 定标整数累加、
纯 ASCII 值的归一化快路径，并把近似重复归一化移到循环之后；100 万行复算从约 55 秒
降到约 16 秒，结果保持逐行精确、未抽样。

本节把此前散落在各处的"大数据优化"固化成一条可复跑命令。基准脚本
`scripts/bench_bigdata.py` 生成临时数据、逐项计时并输出 JSON；临时文件在
脚本结束时删除，除 numpy / pandas / plotly（均为现有依赖）外不引入任何新依赖。

复跑命令（仓库根目录，默认 20 万行 × 7 列 + 100 万行核验 CSV）：

```powershell
.venv\Scripts\python.exe scripts/bench_bigdata.py
```

可用 `--rows` / `--audit-rows` / `--repeat` / `--seed` 调整规模与轮数。
2026-11-19 默认参数连续三次实跑：总墙钟 50s / 72s / 105s，退出码均为 0，
全部基准项 `ok=true`。下表取**三次实跑的中位数**（每项内部仍是 3 次计时的
中位数，`load` 与 `read_metrics` 是一次性 I/O）；波动范围见文末说明。

**机器与版本**：Intel Core Ultra 9 275HX（24 逻辑核）、31.4GB 内存、
Windows 11 家庭版中文版 10.0.26300；`.venv` 内 Python 3.13.14、
pandas 3.1.1、numpy 2.5.5、plotly 7.1.0。表中耗时先按"每项 3 次计时
（预热 1 次）取中位数"，再对三次完整实跑取中位数；`load` 与
`read_metrics` 是一次性 I/O，按单次值参与三跑中位数。

| 基准项 | 规模 | 入口 | 耗时 | 额外峰值内存 | 结果 |
| --- | --- | --- | --- | --- | --- |
| 工作区加载（含首次 profile） | 20 万行 × 7 列，CSV 26.43MB | `DataWorkspace.load` | 0.362s | 28.71MB | DataFrame 5.341MB |
| profile 冷路径（清缓存后重算） | 同上 | `DataWorkspace.profile(5)` | 0.245s | 23.30MB | 重复行 0 |
| profile 热路径（全量统计缓存命中） | 同上 | `DataWorkspace.profile(8)` | 0.000003s | — | LRU 命中 |
| 相关分析（7 列 / 21 对） | 20 万行 × 7 列 | `run_statistical_analysis(..., "correlation")` | 0.063s | — | p 值走 t 分布导出 |
| 图表嵌入抽样 · 散点 | 20 万点 | `sample_plotly_figure_for_embed` | 0.020s | — | 20 万 → 5 万点 |
| 图表嵌入抽样 · 折线（LTTB） | 20 万点 | 同上 | 0.051s | — | 20 万 → 5 万点 |
| 密度视图聚合 | 20 万点 | `build_density_view` | 0.049s | — | 154×80 网格、5809 个非空格、峰值 234 |
| WebGL 切换 | 20 万行 | `_plotly_webgl_if_large` | 0.018s | — | `scatter` → `scattergl` |
| 核验复算 | 100 万行 × 4 列，CSV 33.28MB | `audit_schema.read_metrics` | 15.5–19.7s | 210.08MB | 100 万行全量逐行复算（优化后） |

阈值触发结果（脚本内直接断言并输出，避免"配置在但没生效"）：

- 密度视图：`should_use_density(20_000) = True`、`(19_999) = False`、
  20 万行 `= True`；阈值常量 `DENSITY_MIN_POINTS = 20_000`。
- WebGL：`_PLOTLY_WEBGL_THRESHOLD = 10_000`；20 万行散点与折线均切到
  `scattergl`，恰好 10,000 行仍是 `scatter`（判断条件是 `>` 而非 `>=`），
  9,999 行保持 `scatter`。
- 核验复算的读数可对账：金额合计 `501120671.01`、订单数 `1000000`、
  区域数 `4`，`evidence.rows = 1000000`，证明 100 万行全部参与而非抽样。

几点口径说明：

1. **"额外峰值内存"是 `tracemalloc` 从该项开始计时起的峰值**，不是进程
   RSS，也不能跨项相加；单位按 1024² 计算（MiB，脚本字段名沿用 MB）。
   本机实测 numpy 的缓冲区分配会被 tracemalloc 计入
   （`np.zeros(10_000_000)` 实测计入 76.29MiB），但解释器与已导入模块的
   常驻内存不在其中。
2. `load` 内部会调用一次 `profile`，所以 0.362s / 28.71MB 是"解析 CSV +
   首次全表统计"的合计；单看 profile 冷路径是 0.245s / 23.30MB。
3. `read_metrics` 是核验链路的**全量逐行**复算（`csv.DictReader` + `Decimal`），
   15.5–19.7s/100 万行是刻意不抽样换来的可审计性；这条路径不属于交互式
   预览，不要按图表路径的毫秒级预算要求它。100 万行时 210MB 峰值主要来自
   原始字节 + 解码文本两份副本。
4. 与上一节"相关分析 0.472s → 0.025s"的差异来自测量上下文：上一节是
   优化前后对照的独立脚本与最快档，本节是通用基准的 3 次中位数（含结果
   字典构建、生成数据的相关结构也不同）。**需要复现时以本节命令为准**，
   不要拿两个不同脚本的单次数字直接当回归判据。
5. 同一命令三次实跑的漂移：`load` 0.276 / 0.362 / 0.434s、相关分析
   0.061 / 0.063 / 0.072s、密度聚合 0.038 / 0.049 / 0.055s、
   `read_metrics` 15.5 / 19.7s（优化后；其余项差异在 1–3ms 内），总墙钟
   50 / 72 / 105s。这与上一节"Windows 文件系统波动可达 ±40%"量级一致，
   且三次运行期间本机工作树还有其它并行改动在变动。**不要拿单次绝对耗时当回归判据**；
   脚本的确定性输出（点数、网格、阈值触发、读数）才是可对账的部分。
6. 本脚本只读现有实现，不改 `src/` 与 `tests/`；它调用的是**运行时工作树**
   里的代码，工作树存在未提交改动时以工作树为准。相关回归由
   `tests/test_bigdata_budget.py`（体积/点数/时间上限）与
   `tests/test_density*.py`、`tests/test_line_sampling.py`、
   `tests/test_audit_schema.py`、`tests/test_coverage_fill.py` 覆盖。

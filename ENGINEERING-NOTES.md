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

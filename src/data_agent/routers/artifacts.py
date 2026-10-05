"""产物预览、下载、缩略图与编辑路由。

- GET  /api/sessions/{id}/artifacts/{filename}/preview：图表在线预览（CSP 沙箱）。
- GET  /api/sessions/{id}/artifacts/{filename}：产物下载（HTML 内联 bundle 自包含）。
- GET  /api/sessions/{id}/artifacts/{filename}/thumbnail：Plotly 图表缩略图 PNG。
- PUT  /api/sessions/{id}/artifacts/{filename}/edit：基于 .plotly.json 重新生成 HTML。

辅助函数已拆分到 ``_artifacts`` 子包（bundle 内联、HTML 修复、脚本注入、
预览缓存），本模块 re-export 以兼容测试。``_artifact_file`` 已迁移至
``data_agent.registry``，内部通过 ``api.registry`` 访问以兼容 monkeypatch。
"""

from __future__ import annotations

import json
import os
from email.utils import formatdate
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, Response

from data_agent.chart_sampling import (  # noqa: F401
    _THUMB_MAX_POINTS,
    _decode_plotly_typed_arrays,
    _sample_echarts_option_for_thumb,
    _sample_plotly_figure_for_thumb,
)
from data_agent.registry import ChartEditRequest, _artifact_file
from data_agent.tools import _PLOTLY_DARK_MODE_SCRIPT
from data_agent.tools.builder import _render_plotly_html
from data_agent.workspace import _atomic_write_text

from ._artifacts import (  # noqa: F401
    _BUNDLE_CACHE_MAX,
    _BUNDLE_TEXT_CACHE,
    _PREVIEW_TEXT_CANDIDATES,
    _get_prepared_preview,
    _harden_preview_document,
    _inject_gl_refresh_fix,
    _inject_legend_anchor_fix,
    _inject_modebar_i18n,
    _inline_echarts_bundle,
    _inline_echarts_gl_bundle,
    _inline_plotly_bundle,
    _preview_etag,
    _put_prepared_preview,
    _read_bundle_cached,
    _read_utf8_robust,
    _repair_legacy_plotly_theme_keys,
    _repair_unterminated_plotly_script,
)

router = APIRouter()

#: 支持 ``marker.color`` 的 Plotly 轨迹类型——图表编辑的"改配色"只对它们生效。
#: heatmap / histogram2d / histogram2dcontour / contour / image / splom 等按
#: 数值着色的图型没有 marker 属性，盲写会让 `go.Figure` 校验抛 ValueError
#: （实测：30 万行散点的密度图点"编辑→改色"直接 500，且报错信息无法理解）。
_MARKER_TRACE_TYPES = frozenset(
    {
        "scatter",
        "scattergl",
        "scatter3d",
        "scatterpolar",
        "scatterpolargl",
        "scatterternary",
        "scattergeo",
        "scattermapbox",
        "bar",
        "histogram",
        "box",
        "violin",
        "pie",
        "funnel",
        "funnelarea",
        "waterfall",
        "sunburst",
        "treemap",
        "icicle",
    }
)

#: 用颜色表达"数值"的图型（密度图/热力图/等高线/图像）。它们的配色是数据编码，
#: 不是装饰，改色等于换掉一种语义；只改其中少数 trace（例如密度图叠加的
#: "最外围记录"散点）会造成"改了一半"的错觉，因此改色请求一律明确拒绝并说明，
#: 而不是部分生效。
_VALUE_SCALED_TRACE_TYPES = frozenset(
    {
        "heatmap",
        "heatmapgl",
        "histogram2d",
        "histogram2dcontour",
        "contour",
        "contourcarpet",
        "image",
        "splom",
        "densitymapbox",
    }
)


@router.get("/api/sessions/{session_id}/dashboard")
def export_dashboard(session_id: str) -> Response:
    """导出数据画像仪表盘：KPI 指标卡 + 全部图表 + 数据质量告警，
    单一自包含 HTML（离线可开、亮暗双主题）。实时基于当前工作区
    数据与已生成图表组装，不落盘为产物。"""
    from data_agent import api
    from data_agent.dashboard import build_dashboard_html

    record = api.registry.get(session_id)
    try:
        html_text = build_dashboard_html(record.workspace)
    except RuntimeError as exc:
        raise HTTPException(status_code=404, detail="尚未加载数据集，无法生成仪表盘。") from exc
    filename = quote("数据画像仪表盘.html")
    return Response(
        content=html_text,
        media_type="text/html; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"},
    )


@router.get("/api/sessions/{session_id}/artifacts/{filename}/preview")
def preview_artifact(session_id: str, filename: str, request: Request) -> Response:
    record, path = _artifact_file(session_id, filename)
    if path.suffix.lower() != ".html":
        raise HTTPException(status_code=415, detail="该产物不支持在线预览。")
    etag = _preview_etag(path)
    # 条件请求：文件未变（ETag 一致）时直接返回 304，前端复用其 LRU 缓存，
    # 既避免重复下载内联后的大体积 HTML（Plotly 约 3.5MB），又保证文件一旦
    # 被重写（如重新生成图表）缓存立即失效、拿到最新内容，永不滞留旧版乱码。
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})
    # 准备结果缓存命中：文件未变时直接复用上次注入/修复完成的文档，
    # 省去重读文件 + 三段注入 + 内联 bundle 的重复开销。
    cached_html = _get_prepared_preview(path)
    if cached_html is not None:
        return Response(
            content=cached_html,
            media_type="text/html",
            headers={
                "Cache-Control": "private, no-store",
                "ETag": etag,
                "Last-Modified": formatdate(path.stat().st_mtime, usegmt=True),
            },
        )
    # 旧版生成器（<2026-08）的三个问题统一修复后再内联 bundle：
    # 1) to_html 脚本块闭合标签被误转义导致预览空白；
    # 2) 暗色脚本 relayout 键带 'layout.' 前缀被 Plotly v3 静默忽略；
    # 3) 图例默认右侧竖排，窄容器下溢出 SVG 被裁（历史图注入锚定修正）。
    # 另注入 modebar 按钮提示中文本地化（plotly 自带 locale 不含 zh-CN）。
    html_text = _repair_legacy_plotly_theme_keys(
        _repair_unterminated_plotly_script(_read_utf8_robust(path))
    )
    html_text = _inject_legend_anchor_fix(html_text)
    html_text = _inject_modebar_i18n(html_text)
    html_text = _inject_gl_refresh_fix(html_text)
    html_text = _inline_plotly_bundle(record, html_text)
    # gl 扩展先内联：其标签更具体，先处理可避免主 bundle 正则的 CDN
    # 分支（https?://...echarts....js）误吞 echarts-gl 的 CDN 引用。
    html_text = _inline_echarts_gl_bundle(record, html_text)
    html_text = _inline_echarts_bundle(record, html_text)
    html_text = _harden_preview_document(html_text)
    _put_prepared_preview(path, html_text)
    return Response(
        content=html_text,
        media_type="text/html",
        headers={
            "Cache-Control": "private, no-store",
            "ETag": etag,
            "Last-Modified": formatdate(path.stat().st_mtime, usegmt=True),
        },
    )


@router.get("/api/sessions/{session_id}/artifacts/{filename}")
def download_artifact(session_id: str, filename: str) -> Response:
    record, path = _artifact_file(session_id, filename)
    if path.suffix.lower() == ".html":
        # Downloads must remain self-contained so they open offline.
        html_text = _repair_legacy_plotly_theme_keys(
            _repair_unterminated_plotly_script(_read_utf8_robust(path))
        )
        html_text = _inject_legend_anchor_fix(html_text)
        html_text = _inject_modebar_i18n(html_text)
        html_text = _inline_plotly_bundle(record, html_text)
        html_text = _inline_echarts_gl_bundle(record, html_text)
        html_text = _inline_echarts_bundle(record, html_text)
        html_text = _harden_preview_document(html_text)
        return Response(
            content=html_text,
            media_type="text/html",
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(path.name)}"},
        )
    return FileResponse(path, filename=path.name)


@router.get("/api/sessions/{session_id}/artifacts/{filename}/echarts-json")
def get_echarts_option(session_id: str, filename: str) -> Response:
    """返回 ECharts 图表的 option JSON，供前端产物卡片渲染迷你图。

    ECharts 没有像 Plotly 那样的服务端 PNG 缩略图通道（kaleido 仅支持
    Plotly），前端直接读取 option 并用 echarts 原地渲染迷你图，让产物
    卡片无需点击即可预览。安全：文件名经 _artifact_file 基名校验；
    option 中存档的 JS 函数以字符串形式返回，前端渲染迷你图时剥离
    函数字段（不执行任意代码）。大数据兜底：散点系列按等距抽样到
    ``_THUMB_MAX_POINTS``，避免几十万行时迷你图传输/渲染卡顿。
    """
    record, _path = _artifact_file(session_id, filename)
    stem = Path(filename).name
    if stem.endswith(".html"):
        stem = stem[: -len(".html")]
    json_path = record.workspace.artifacts_dir / f"{stem}.echarts.json"
    if not json_path.is_file():
        raise HTTPException(status_code=404, detail="该图表没有 ECharts 数据文件。")
    try:
        option = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=500, detail=f"ECharts 数据读取失败：{exc}") from exc
    # 大数据兜底：30 万行散点的 option 可达十几 MB，卡片迷你图无需
    # 全量点云——按等距抽样到固定上限，传输与渲染恒定开销。
    _sample_echarts_option_for_thumb(option)
    return Response(
        content=json.dumps(option, ensure_ascii=False),
        media_type="application/json",
        headers={"Cache-Control": "private, no-store"},
    )


@router.get("/api/sessions/{session_id}/artifacts/{filename}/plotly-json")
def get_plotly_option(session_id: str, filename: str) -> Response:
    """返回 Plotly 图表的 figure JSON，供前端产物卡片渲染交互迷你图。

    镜像 echarts-json 端点：Plotly 卡片的缩略图是服务端渲染的静态 PNG
    （kaleido），悬停无任何反应；前端拿到 figure JSON 后用 plotly.js
    原地渲染迷你图，即可像 ECharts 卡片一样悬停查看数据。安全：文件名
    经 _artifact_file 基名校验；Plotly 的 JSON 是纯数据（无函数字段），
    前端不会执行任何代码。大数据兜底：按点图型（散点/箱线等）等距抽样
    到 ``_THUMB_MAX_POINTS``。
    """
    record, _path = _artifact_file(session_id, filename)
    stem = Path(filename).name
    if stem.endswith(".html"):
        stem = stem[: -len(".html")]
    json_path = record.workspace.artifacts_dir / f"{stem}.plotly.json"
    if not json_path.is_file():
        raise HTTPException(status_code=404, detail="该图表没有 Plotly 数据文件。")
    try:
        figure = json.loads(_read_utf8_robust(json_path))
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=500, detail=f"Plotly 数据读取失败：{exc}") from exc
    # plotly.py 把 numpy 数组写成了 typed-array（{"dtype","bdata"}），
    # 先解码成普通列表；再按大数据兜底规则等距抽样到 _THUMB_MAX_POINTS
    # （散点/箱线等按点图型），迷你图传输与渲染恒定开销。
    figure = _decode_plotly_typed_arrays(figure)
    _sample_plotly_figure_for_thumb(figure)
    return Response(
        content=json.dumps(figure, ensure_ascii=False),
        media_type="application/json",
        headers={"Cache-Control": "private, no-store"},
    )


@router.get("/api/sessions/{session_id}/artifacts/{filename}/thumbnail")
def get_chart_thumbnail(session_id: str, filename: str) -> FileResponse:
    """生成图表缩略图 PNG（best-effort，依赖 Kaleido）。

    优先返回已缓存的 ``{stem}_thumb.png``；缓存不存在时从 ``{stem}.plotly.json``
    重新渲染。Kaleido 未安装时返回 503，其他渲染异常返回 500。缩略图尺寸
    400×250，去掉 margin 节省空间。
    """
    from data_agent import api

    record = api.registry.get(session_id)
    workspace = record.workspace
    # 取基名防止路径遍历，并去掉可能的 .html 后缀得到原始 stem。
    stem = Path(filename).name
    if stem.endswith(".html"):
        stem = stem[: -len(".html")]
    # 先查是否已有缓存的缩略图。命中的前提：缩略图不早于图表数据文件——
    # 图表被同名重新生成后（.plotly.json mtime 更新）旧缩略图必须失效
    # 重渲染，否则卡片上一直显示覆盖前的旧图。
    thumb_path = workspace.artifacts_dir / f"{stem}_thumb.png"
    json_path = workspace.artifacts_dir / f"{stem}.plotly.json"
    if thumb_path.is_file() and (
        not json_path.is_file() or thumb_path.stat().st_mtime >= json_path.stat().st_mtime
    ):
        return FileResponse(thumb_path, media_type="image/png")
    # 从 .plotly.json 重新生成
    if not json_path.is_file():
        raise HTTPException(status_code=404, detail="图表数据文件不存在。")
    try:
        import plotly.graph_objects as go

        fig_dict = json.loads(_read_utf8_robust(json_path))
        fig = go.Figure(fig_dict)
        # 缩略图尺寸 400x250，去掉 margin 节省空间
        fig.update_layout(margin=dict(l=20, r=20, t=30, b=20), showlegend=False)
        # 原子写：先写临时文件再 os.replace，防止并发缩略图请求交错写入损坏 PNG。
        # 临时文件名必须保留 .png 后缀：plotly/kaleido 从扩展名推断输出格式，
        # 用 .tmp 结尾会触发 "Invalid format 'tmp'" 导致缩略图渲染失败。
        tmp_thumb = thumb_path.with_name(thumb_path.name + ".tmp.png")
        fig.write_image(str(tmp_thumb), width=400, height=250, scale=1)
        os.replace(str(tmp_thumb), str(thumb_path))
        return FileResponse(thumb_path, media_type="image/png")
    except ImportError:
        raise HTTPException(
            status_code=503, detail="服务器未安装图片渲染依赖（kaleido）。"
        ) from None
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"缩略图生成失败：{exc}") from exc


@router.put("/api/sessions/{session_id}/artifacts/{filename}/edit")
def edit_chart(session_id: str, filename: str, request: ChartEditRequest) -> dict[str, Any]:
    """编辑已有图表：修改标题或配色，基于 .plotly.json 重新生成 HTML。

    流程：
    1. 根据传入的 filename（可为 ``xxx.html`` 或 ``xxx``）定位同名
       ``xxx.plotly.json`` 数据文件。
    2. 读取 fig_dict，应用 title/color 修改。
    3. 用与 tools.py 一致的 HTML 模板（含暗色模式脚本、XSS 转义、
       原子写）重新生成 HTML，覆盖原文件。
    4. 同步更新 .plotly.json，保证后续编辑基于最新数据。

    安全：
    - filename 经 Path(filename).name 取基名，防止路径遍历。
    - HTML 生成复用 tools.py 的 ``</script>`` → ``<\\/script>`` 转义。
    """
    from html import escape

    import plotly.graph_objects as go

    from data_agent import api

    record = api.registry.get(session_id)
    # 编辑图表需要独占访问：与 worker 并发读写同一 .plotly.json/HTML
    # 会导致读到半写状态的文件。尝试获取 run_lock，失败时返回 409。
    if not record.run_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="当前会话有分析正在运行，请稍后再编辑图表。")
    try:
        workspace = record.workspace
        # 取基名防止路径遍历，并去掉可能的 .html 后缀得到原始 stem。
        stem = Path(filename).name
        if stem.endswith(".html"):
            stem = stem[: -len(".html")]
        json_path = workspace.artifacts_dir / f"{stem}.plotly.json"
        html_path = workspace.artifacts_dir / f"{stem}.html"
        if not json_path.is_file():
            raise HTTPException(status_code=404, detail="图表数据文件不存在，无法编辑。")
        try:
            fig_dict = json.loads(_read_utf8_robust(json_path))
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=500, detail=f"图表数据读取失败：{exc}") from exc

        # 应用修改：title 写入 layout.title.text（保持 Plotly 标准结构）；
        # color 应用到支持 marker 的 trace——按数值着色的图型（heatmap /
        # histogram2d / contour / image/splom 等）没有 marker 属性，盲写会让
        # go.Figure 校验直接抛 ValueError（实测报 500 且错误信息用户看不懂）。
        if request.title is not None:
            layout = fig_dict.setdefault("layout", {})
            # 合并而不是整块替换：替换会丢掉标题原有的字号/对齐等样式字段。
            existing_title = layout.get("title") if isinstance(layout.get("title"), dict) else {}
            layout["title"] = {**existing_title, "text": request.title}
        if request.color is not None:
            trace_types = {str(trace.get("type", "scatter")) for trace in fig_dict.get("data", [])}
            if trace_types & _VALUE_SCALED_TRACE_TYPES:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "该图表按数值密集程度分档着色（密度图/热力图），颜色由记录数决定，"
                        "不支持单独修改配色；可以修改标题，或在重新生成图表时指定分组列按类别着色。"
                    ),
                )
            colored = 0
            for trace in fig_dict.get("data", []):
                if str(trace.get("type", "scatter")) not in _MARKER_TRACE_TYPES:
                    continue
                if isinstance(trace.get("marker"), dict):
                    trace["marker"]["color"] = request.color
                else:
                    trace["marker"] = {"color": request.color}
                colored += 1
            if colored == 0:  # pragma: no cover - 非数值着色图型必然至少有一条 marker 轨迹
                raise HTTPException(
                    status_code=422,
                    detail="该图表没有可修改颜色的数据系列。",
                )

        # 重新生成 HTML：与 tools.py 保持一致的模板和转义逻辑，
        # 确保编辑后的图表预览/下载体验与原始生成一致。
        try:
            fig = go.Figure(fig_dict)
            shared_plotly = workspace.ensure_plotly_bundle()
            relative_script = (
                shared_plotly.relative_to(workspace.artifacts_dir).as_posix()
                if shared_plotly
                else None
            )
            display_title = (fig_dict.get("layout", {}) or {}).get("title", {}).get(
                "text", ""
            ) or stem
            if relative_script:
                div = fig.to_html(
                    full_html=False,
                    include_plotlyjs=False,
                    default_width="100%",
                    default_height="100%",
                    config={
                        "responsive": True,
                        "displaylogo": False,
                        "modeBarButtonsToRemove": ["lasso2d", "select2d"],
                        "scrollZoom": True,
                    },
                )
                # XSS 防护：与 tools.py 一致，转义 </script> 避免 Plotly
                # 序列化数据中的 </script> 提前关闭 script 块导致注入。
                # 必须保留最后一个 </script>（to_html 自身脚本块的闭合
                # 标签），否则 script 元素无法闭合，会与后续注入的暗色
                # 脚本合并成无效 JS（"Unexpected token '<'"），预览空白。
                close_idx = div.rfind("</script>")
                if close_idx != -1:
                    div = div[:close_idx].replace("</script>", "<\\/script>") + div[close_idx:]
                _atomic_write_text(
                    html_path,
                    _render_plotly_html(
                        title=escape(display_title),
                        script_src=relative_script,
                        div=div,
                        dark_script=_PLOTLY_DARK_MODE_SCRIPT,
                        full_page=False,
                    ),
                )
            else:
                # plotly bundle 不可用（极少见）时回退到内联 plotlyjs 的完整 HTML。
                fig.write_html(html_path, include_plotlyjs=True, full_html=True)
            # 同步更新 .plotly.json，保证后续编辑基于最新数据。
            # 使用原子写入（写 .tmp 再 replace），防止进程被杀时留下损坏的 JSON。
            import json as _json
            import os as _os

            tmp_path = json_path.with_suffix(".tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                _json.dump(fig.to_dict(), f, ensure_ascii=False, default=str)
            _os.replace(tmp_path, json_path)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"图表重新生成失败：{exc}") from exc
        # 标题变更后同步产物描述并持久化：产物卡片、预览模态头部与会话
        # 归档读的都是 description，只改图表 HTML 会让 UI 停留在旧标题。
        if request.title is not None:
            workspace.update_artifact_description(html_path, display_title)
            try:
                api.registry._persist_locked(session_id, record)
            except Exception:
                # 持久化失败不影响本次编辑结果，仅记录
                import logging

                logging.getLogger(__name__).exception("Failed to persist manifest after chart edit")
        return {"status": "ok", "message": "图表已更新。"}
    finally:
        record.run_lock.release()

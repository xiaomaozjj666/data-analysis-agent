"""``create_visualization`` 的产物落盘与响应组装。

从 ``_tools_charts.create_visualization`` 拆出的后半段（原函数 725~968 行）：
白话解读 → 大数据嵌入降采样 → 3D 数据摘要 → HTML/JSON 原子写入与产物注册
→ 响应字典（覆盖率、来源绑定、抽样/密度/统计聚合元信息、PNG 导出）。

写文件仍走 ``_atomic_write_text``（tmp + os.replace），进程被 kill / 磁盘满时
不会留下半截 HTML 让预览 iframe 加载到损坏页面。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import Any

from data_agent import _kaleido_compat
from data_agent.chart_sampling import (
    _EMBED_MAX_POINTS,
    sample_plotly_figure_for_embed,
    sampling_note,
)
from data_agent.density import band_label, should_use_density
from data_agent.serialization import json_text
from data_agent.workspace import DataWorkspace, _atomic_write_text

from .._helpers import _human_column_label
from .._plotly_render import (
    _PLOTLY_DARK_MODE_SCRIPT,
    _render_plotly_html,
    _stat_agg_note,
    append_html_body_block,
    render_data_table_html,
    scatter3d_data_summary,
)
from ..density_plotly import density_interpretation
from .interpret import _plotly_auto_interpret
from .validate import _ensure_plotly_fig_has_data


@dataclass
class ChartArtifacts:
    """一次 Plotly 图表落盘后供响应组装使用的产物信息。"""

    html_path: Path
    json_path: Path
    fig_json_text: str
    sampling_info: dict[str, Any] | None
    data_summary: dict[str, Any] | None


def dispatch_echarts(workspace: DataWorkspace, spec: Any) -> str:
    """ECharts 分支：独立渲染并提前返回，避免白跑一次 Plotly 构建。

    ``data_agent.echarts_engine`` 惰性导入：该模块运行时会反向导入
    ``tools`` 的图表辅助，模块级导入会形成循环。
    """
    from data_agent.echarts_engine import _render_echarts

    return json_text(
        _render_echarts(
            workspace,
            spec.df,
            chart_type=spec.chart_type,
            x=spec.x,
            y=spec.y,
            color=spec.color,
            z=spec.z,
            size=spec.size,
            values=spec.values,
            path_columns=spec.path_columns,
            dimensions=spec.dimensions,
            aggregation=spec.aggregation,
            title=spec.title,
            bins=spec.bins,
            display_title=spec.display_title,
            stem=spec.stem,
            chart_type_source="auto" if spec.was_auto else "explicit",
            export_png=spec.export_png,
            stacked=spec.stacked,
        )
    )


def finalize_plotly_artifacts(fig: Any, spec: Any, workspace: DataWorkspace) -> ChartArtifacts:
    """校验非空 → 生成解读 → 降采样嵌入 → 写 HTML/JSON 并注册产物。"""
    # 空图兜底：所有序列都没有数据点时直接报错让模型重试，不产出
    # "能打开但一片空白"的 HTML（轴写反/聚合列全空时曾静默出空白图）。
    _ensure_plotly_fig_has_data(fig, spec.chart_type)
    html_path = workspace.artifacts_dir / f"{spec.stem}.html"
    shared_plotly = workspace.ensure_plotly_bundle()
    relative_script = (
        shared_plotly.relative_to(workspace.artifacts_dir).as_posix() if shared_plotly else None
    )
    interpretation = _build_interpretation(spec)
    # 全量 figure 序列化一次复用：完整数据写入 .plotly.json 产物，
    # 大数据时也作为嵌入降采样的输入源。抽样决策必须在解读块构建
    # 之前完成——抽样声明要追加进解读文本一起渲染。
    fig_json_text = fig.to_json()
    html_fig, sampling_info, sampling_text = _downsample_for_embed(
        fig, spec, workspace, fig_json_text
    )
    interpretation += sampling_text
    # 3D 散点的可访问数据摘要：取抽样后真正写进 HTML 的 figure 点，
    # 保证表格数值与画面一致（Gemini 审计第 5 条：读屏/无法交互场景
    # 也必须能拿到数据）。points 即嵌入点数，sampled 标记抽样事实。
    data_summary: dict[str, Any] | None = None
    if spec.chart_type == "scatter_3d" and spec.x and spec.y and spec.z:
        data_summary = scatter3d_data_summary(
            html_fig,
            x=spec.x,
            y=spec.y,
            z=spec.z,
            color=spec.color,
            sampled=sampling_info is not None,
        )
    _write_html_artifact(
        spec=spec,
        html_fig=html_fig,
        html_path=html_path,
        relative_script=relative_script,
        interpretation=interpretation,
        data_summary=data_summary,
    )
    workspace.register_artifact(html_path, "visualization", spec.display_title)
    json_path = workspace.artifacts_dir / f"{spec.stem}.plotly.json"
    # plotly write_json 不带 encoding 时在 Windows 上按 locale(cp936/GBK) 写文件，
    # 必须显式 UTF-8，否则后续 UTF-8 读取会 UnicodeDecodeError。
    # fig_json_text 在 div 生成前已序列化（完整 figure，非抽样副本）。
    _atomic_write_text(json_path, fig_json_text)
    workspace.register_artifact(json_path, "chart_data", "Plotly figure JSON")
    return ChartArtifacts(
        html_path=html_path,
        json_path=json_path,
        fig_json_text=fig_json_text,
        sampling_info=sampling_info,
        data_summary=data_summary,
    )


def _build_interpretation(spec: Any) -> str:
    """生成 Plotly 分支的白话解读（与 ECharts _auto_interpret 语义对齐）。"""
    # 密度图的解读换了编码语义（颜色读记录数、没有可框选的离散点），单独生成。
    if spec.density_view is not None:
        interpretation = density_interpretation(
            spec.df,
            view=spec.density_view,
            x=spec.x,
            y=spec.y,
            color=spec.color,
            title=spec.display_title,
            x_label=spec.labels.get(spec.x, _human_column_label(spec.x)),
            y_label=spec.labels.get(spec.y, _human_column_label(spec.y)),
        )
        if spec.size:
            # 密度视图用颜色表达每格记录数，逐点大小这个编码通道没有位置可放；
            # 与其静默丢掉用户要求的维度，不如把这件事说清楚。
            interpretation += (
                f"\n\n说明：密度视图按记录数着色，因此未使用「{_human_column_label(spec.size)}」"
                "作为点的大小——几十万点上逐点大小无法辨认；该列仍保留在数据与 JSON 产物中。"
            )
        return interpretation
    interpretation = _plotly_auto_interpret(
        spec.df,
        chart_type=spec.chart_type,
        x=spec.x,
        y=spec.y,
        color=spec.color,
        aggregation=spec.aggregation,
        title=spec.display_title,
    )
    if spec.stat_agg is not None:
        interpretation += _stat_agg_note(spec.stat_agg)
    if (
        spec.chart_type == "scatter"
        and spec.density_view is None
        and spec.x
        and spec.y
        and should_use_density(len(spec.df))
    ):
        # 密度视图只适用于"数值 × 数值"（阈值与密度视图一致：本该走
        # 密度的那批数据，因为横轴是类别而退回逐点渲染）。这里不擅自
        # 换图型（用户明确要的是散点），但必须说清楚并给出建议。
        interpretation += (
            "\n\n提示：这张散点的横轴不是数值列，无法聚合成密度网格，"
            "几十万个点会互相覆盖成一团深色。查看各类别的分布建议改用"
            "箱线图 / 小提琴图，或把数值列放到横轴上重新生成（会自动切换密度视图）。"
        )
    return interpretation


def _downsample_for_embed(
    fig: Any, spec: Any, workspace: DataWorkspace, fig_json_text: str
) -> tuple[Any, dict[str, Any] | None, str]:
    """大数据 HTML 嵌入降采样；返回 (用于 HTML 的 figure, 响应元信息, 解读附言)。

    散点/折线/3D 散点超过嵌入上限时，交互 HTML 按抽样渲染（5 万点以上视觉上
    已是密度饱和，HTML 体积、传输、iframe 解析、tab 内存却随点数线性增长）。
    完整数据不丢：全量 figure 仍写入 ``.plotly.json`` 产物。
    """
    if not (
        spec.chart_type in {"scatter", "scatter_3d", "line"}
        and len(spec.df) > _EMBED_MAX_POINTS
        and spec.density_view is None
    ):
        return fig, None, ""
    sampled_dict, original_pts, embedded_pts = sample_plotly_figure_for_embed(
        json.loads(fig_json_text), _EMBED_MAX_POINTS
    )
    if embedded_pts >= original_pts:
        return fig, None, ""
    import plotly.io as pio

    html_fig = pio.from_json(json.dumps(sampled_dict))
    sampling_info = {
        "applied": True,
        "original_points": original_pts,
        "embedded_points": embedded_pts,
        "full_data_json": str(workspace.artifacts_dir / f"{spec.stem}.plotly.json"),
    }
    return html_fig, sampling_info, sampling_note("plotly", original_pts, embedded_pts)


def _write_html_artifact(
    *,
    spec: Any,
    html_fig: Any,
    html_path: Path,
    relative_script: str | None,
    interpretation: str,
    data_summary: dict[str, Any] | None,
) -> None:
    """按共享 Plotly bundle / 内联离线两种方式原子写入 HTML。"""
    data_table = render_data_table_html(data_summary)
    interpretation_block = ""
    if interpretation:
        interpretation_block = (
            '<div class="plotly-interpretation">'
            '<div class="plotly-interpretation-title">数据解读</div>'
            f"{escape(interpretation)}"
            "</div>"
        )
    if relative_script is not None:
        div = html_fig.to_html(
            full_html=False,
            include_plotlyjs=False,
            default_width="100%",
            default_height="100%",
            config={
                "responsive": True,
                "displaylogo": False,
                "modeBarButtonsToRemove": ["lasso2d", "select2d"],
                # 滚轮缩放：预览模态是独立 iframe，页面本身不滚动，
                # 滚轮交给图表做缩放是用户预期（此前滚轮无效）。
                "scrollZoom": True,
            },
        )
        # XSS 纵深防御：Plotly 的 JSON 编码器会把数据中的 < 转义为
        # \u003c，正常情况 div 中只有 to_html 自身脚本块的那一个
        # </script>。但若未来版本或数据路径出现未转义的 </script>
        # （浏览器解析时它会提前关闭 script 块、剩余 JS 在 iframe
        # 上下文执行），这里统一转义 <\/script> 兜底。
        # 注意：必须保留最后一个 </script> —— 它是 to_html 自身脚本
        # 块的闭合标签。若一并转义（旧版 bug），该 script 元素无法
        # 闭合，会与后续注入的暗色脚本合并成同一 script 块（内含
        # 字面 "<script>"），产生 "Unexpected token '<'" 语法错误，
        # 图表预览直接空白。数据中的 </script> 均位于闭合标签之前，
        # 因此"转义全部但保留最后一个"同时满足防护与结构正确。
        close_idx = div.rfind("</script>")
        if close_idx != -1:
            div = div[:close_idx].replace("</script>", "<\\/script>") + div[close_idx:]
        # 原子写：进程被 kill / 磁盘满时不会留下半截 HTML 让预览 iframe
        # 加载到损坏页面。tmp + os.replace 对同目录文件是原子的。
        _atomic_write_text(
            html_path,
            _render_plotly_html(
                title=escape(spec.display_title),
                script_src=relative_script,
                div=div,
                interpretation_block=interpretation_block,
                data_table=data_table,
                dark_script=_PLOTLY_DARK_MODE_SCRIPT,
            ),
        )
        return
    # 内联 Plotly 的离线分支同样走 tmp + replace：进程中断时不会
    # 留下半截 HTML 让预览 iframe 加载到损坏页面。write_html 直接
    # 产出完整文档，数据表在 </body> 前补入（与共享模板同一段 HTML）。
    temp_html = html_path.with_name(html_path.name + ".tmp")
    try:
        html_fig.write_html(temp_html, include_plotlyjs=True, full_html=True)
        inline_html = temp_html.read_text(encoding="utf-8")
        _atomic_write_text(html_path, append_html_body_block(inline_html, data_table))
    finally:
        temp_html.unlink(missing_ok=True)


def build_response(
    spec: Any,
    workspace: DataWorkspace,
    artifacts: ChartArtifacts,
    scale_details: dict[str, Any],
    fig: Any,
) -> dict[str, Any]:
    """组装工具返回值（字段顺序与原实现一致）。"""
    coverage = spec.coverage
    response: dict[str, Any] = {
        "status": "ok",
        "chart_type": spec.chart_type,
        "chart_type_source": "auto" if spec.was_auto else "explicit",
        "rows_plotted": len(spec.df),
        **scale_details,
        "category_coverage": {
            "complete": coverage["complete"],
            "observed_combinations": coverage["observed_combinations"],
            "total_combinations": coverage["total_combinations"],
            "missing_count": len(coverage.get("missing_combinations", [])),
        },
        "html": artifacts.html_path,
        "plotly_json": artifacts.json_path,
    }
    if artifacts.data_summary is not None:
        # 读屏/无交互场景的数据兜底：columns + 前 20 行实际绘图值 +
        # 嵌入点数与抽样标记（与 HTML 里的折叠数据表同源）。
        response["data_summary"] = artifacts.data_summary
    # 图表与来源绑定：导出物可追溯到那一版 CSV，而不是只靠会话目录。
    if workspace.source_path and workspace.source_path.is_file():
        from data_agent.provenance import sha256_file

        response["source"] = {
            "filename": workspace.source_path.name,
            "sha256": sha256_file(workspace.source_path),
        }
    if artifacts.sampling_info is not None:
        response["sampling"] = artifacts.sampling_info
    if spec.density_view is not None:
        # 渲染方式对调用方（Agent/前端）可见：密度视图不是抽样，是精确
        # 聚合，因此既没有 sampling 字段，也不需要"完整数据在 JSON 里"
        # 的免责声明；网格覆盖范围与面板构成在这里说清楚。
        response["density_view"] = {
            "applied": True,
            "bins": spec.density_view.bin_label,
            "panels": [panel.name for panel in spec.density_view.panels],
            "bands": [band_label(band) for band in spec.density_view.bands],
            "rows_used": spec.density_view.rows_used,
            "rows_outside_view": spec.density_view.dropped_rows,
            "full_data_json": str(workspace.artifacts_dir / f"{spec.stem}.plotly.json"),
        }
    if spec.stat_agg is not None:
        response["stat_aggregation"] = {
            "kind": spec.stat_agg.get("kind"),
            "rows_aggregated": int(spec.stat_agg.get("total", 0)),
        }
        if spec.stat_agg.get("kind") == "box":
            response["stat_aggregation"]["outliers"] = int(spec.stat_agg.get("outliers", 0))
        else:
            response["stat_aggregation"]["bins"] = int(spec.stat_agg.get("bins", 0))
    if spec.auto_color_applied and spec.color:
        response["auto_color"] = spec.color
    if spec.export_png:
        _export_png(workspace, spec, fig, response)
    return response


def _export_png(workspace: DataWorkspace, spec: Any, fig: Any, response: dict[str, Any]) -> None:
    """best-effort PNG 导出：失败只写 png_warning，不阻塞 HTML 产物。"""
    png_path = workspace.artifacts_dir / f"{spec.stem}.png"
    try:
        # 惰性安装 kaleido 1.5.1 的就绪等待补丁（绕 kaleido_scopes 竞态）；
        # 不在模块顶部装，避免导入时就加载 kaleido。
        _kaleido_compat.install()
        png_width = 980 if spec.chart_type == "scatter_3d" else 1400
        fig.write_image(png_path, width=png_width, height=850, scale=1.5)
        workspace.register_artifact(png_path, "image", spec.title or spec.chart_type)
        response["png"] = png_path
    except Exception as exc:
        # 区分"服务器环境缺失"（没装 kaleido / 没有 Chrome）与"渲染失败"：
        # 前者给出可操作的安装提示，后者保留原始报错便于排查。
        hint = _kaleido_compat.render_env_problem(exc)
        if hint is not None:
            response["png_warning"] = f"PNG 导出不可用（环境缺失），但 HTML 图表已生成：{hint}"
        else:
            response["png_warning"] = f"PNG 渲染失败，但 HTML 图表已生成：{exc}"

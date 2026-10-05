"""产物策展、会话/结果载荷构造与历史消息辅助函数。

从 ``registry.py`` 拆分而来。这些函数是纯转换/查询逻辑，不持有可变状态，
仅通过参数接收 ``SessionRecord`` 实例。``_artifact_file`` 保留对
``data_agent.api`` 的延迟导入以兼容 monkeypatch。
"""

from __future__ import annotations

import enum
import re
import time
import urllib.parse
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException
from langchain_core.messages import AIMessage, HumanMessage

from data_agent.serialization import to_jsonable

if TYPE_CHECKING:
    from data_agent.agent import AnalysisResult
    from data_agent.registry import SessionRecord


class _ArtifactPriority(enum.IntEnum):
    """数据集产物的展示优先级（数字越小优先级越高）。

    ``_curate_artifacts`` 保留优先级最高的两个数据集产物展示给用户。
    ``transformed_data`` 故意排在 ``DEFAULT`` 之后，仅在没有更权威产物时
    才展示，确保清洗步骤始终在 ArtifactCenter 中有可见产物。
    """

    #: 显式 "final" / "result" 导出（如 cleaned_data_final.csv、analysis_result.csv）
    EXPLICIT_FINAL = 0
    #: clean_data 工具产出的 cleaned_data.csv
    CLEANER_OUTPUT = 1
    #: 其他含 final / result / report 关键词的导出
    OTHER_FINAL = 2
    #: 默认数据集（无特殊标记的普通导出）
    DEFAULT = 3
    #: transform_data 的非破坏性视图，仅在无更权威产物时展示
    TRANSFORMED_VIEW = 4


#: 数据集产物优先级匹配表：按顺序匹配，首个命中即决定优先级。
#: 每个条目为 ``(预编译正则, 优先级, 可读标签)``。未命中任何模式的产物
#: 回退到 ``_ArtifactPriority.DEFAULT``。
_ARTIFACT_PATTERNS: list[tuple[re.Pattern[str], _ArtifactPriority, str]] = [
    (
        re.compile(r"cleaned_data_final|analysis_result"),
        _ArtifactPriority.EXPLICIT_FINAL,
        "explicit final export",
    ),
    (re.compile(r"(^|[_/])cleaned_data\.csv$"), _ArtifactPriority.CLEANER_OUTPUT, "cleaner output"),
    (
        re.compile(r"final|result|report"),
        _ArtifactPriority.OTHER_FINAL,
        "other final/result/report",
    ),
    (
        re.compile(r"(^|[_/])transformed_data\.csv$"),
        _ArtifactPriority.TRANSFORMED_VIEW,
        "transformed view",
    ),
]


def _dataset_priority(name: str) -> _ArtifactPriority:
    """Return the curation priority for a dataset artifact by its filename.

    遍历 ``_ARTIFACT_PATTERNS`` 配置表，首个匹配的模式决定优先级；
    未匹配时回退到 ``_ArtifactPriority.DEFAULT``。
    """
    lowered = name.lower()
    for pattern, priority, _label in _ARTIFACT_PATTERNS:
        if pattern.search(lowered):
            return priority
    return _ArtifactPriority.DEFAULT


def _visualization_engine(item: dict[str, str]) -> str:
    """从产物文件推断图表引擎：同名 .plotly.json → plotly，.echarts.json → echarts。

    可视化产物的去重键需要引擎维度：同一语义标题下，不同引擎的图是
    独立产物（双引擎对比是明确的产品诉求），只有同引擎的重试才视为
    「最新覆盖旧版」。无 path 或找不到姊妹 JSON 时返回空串。
    """
    raw_path = item.get("path") or ""
    if not raw_path:
        return ""
    stem = Path(raw_path).stem
    parent = Path(raw_path).parent
    if (parent / f"{stem}.plotly.json").is_file():
        return "plotly"
    if (parent / f"{stem}.echarts.json").is_file():
        return "echarts"
    return ""


#: 只含计数/样本量说明的括号注释（重试时同一张图会换个写法），去重键里忽略它。
#: 判据：括号内去掉数字与标点后，剩下的只能是这些"计数词"，或干脆是空。
_COUNT_ANNOTATION_WORDS = frozenset(
    {
        "n",
        "样本",
        "样本数",
        "条",
        "行",
        "行数",
        "记录",
        "记录数",
        "个",
        "共",
        "总计",
        "合计",
        "有效",
        "有效值",
        "缺失",
        "缺失值",
        "占",
        "占比",
        "百分比",
    }
)
_COUNT_ANNOTATION = re.compile(r"[（(]([^）)]*)[）)]")


def _is_count_annotation(inner: str) -> bool:
    stripped = re.sub(r"[\d\s.,%=/·、，。:\-—~]+", "", inner).strip().lower()
    return not stripped or stripped in _COUNT_ANNOTATION_WORDS


def _strip_count_annotations(description: str) -> str:
    return _COUNT_ANNOTATION.sub(
        lambda match: "" if _is_count_annotation(match.group(1)) else match.group(0),
        description,
    )


def _curate_artifacts(artifacts: list[dict[str, str]]) -> list[dict[str, str]]:
    """Return a concise, user-facing result set instead of every intermediate file."""
    latest_visualizations: dict[str, dict[str, str]] = {}
    images: dict[str, dict[str, str]] = {}
    datasets: list[dict[str, str]] = []
    documents: list[dict[str, str]] = []
    for item in artifacts:
        kind = item.get("kind", "dataset")
        if kind == "chart_data":
            continue
        description = re.sub(r"\s+", " ", item.get("description", "").strip().lower())
        # 去重键：完整描述，但**去掉"样本量注释"式的括号**。
        # 两类括号必须区别对待（否则不是合并过多就是显示过少）：
        #   - "（n=2）" / "（样本=2）" / "（1325/1345）" 只是同一张图重画时的计数备注
        #     → 去掉，重试才会合并；
        #   - "（整体）" / "（按品类）" 是真正区分语义的限定词 → 必须保留，
        #     否则一张 30 万行分面密度图会被另一张顶掉、在界面上凭空消失
        #     （实测演示会话就是这样少了一张图）。
        semantic_title = _strip_count_annotations(description)
        semantic_title = semantic_title.replace("相关系数", "相关").replace("相关性", "相关")
        key = re.sub(r"[^\w\u4e00-\u9fff]+", "", semantic_title)
        key = key or Path(item.get("name", "artifact")).stem.lower()
        if kind == "visualization":
            # 去重键带引擎维度：双引擎同标题是两张独立图（实测发现
            # ECharts/Plotly 双图被旧逻辑互顶只剩一张），同引擎重试才覆盖
            engine = _visualization_engine(item)
            latest_visualizations[(key, engine)] = item
        elif kind == "image":
            images[key] = item
        elif kind == "dataset":
            datasets.append(item)
        else:
            documents.append(item)

    # Prefer explicit "final" / "result" exports, then the cleaner's output,
    # then a non-destructive transformed view. ``transformed_data.csv`` is only
    # surfaced when nothing more authoritative exists, so that a cleaning step
    # always shows up in the ArtifactCenter even if the agent never called
    # export_data with a "final" filename.
    selected_datasets = sorted(
        datasets, key=lambda item: (_dataset_priority(item.get("name", "")),)
    )[-2:]
    return [
        *list(latest_visualizations.values())[-6:],
        *list(images.values())[-3:],
        *selected_datasets,
        *documents[-2:],
    ]


def _artifact_payload(session_id: str, artifacts: list[dict[str, str]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in _curate_artifacts(artifacts):
        value = dict(item)
        # 产物名多为中文（散点图_1.html…）：URL 里必须百分号编码，否则标准 HTTP
        # 客户端（curl / python-urllib / Java）拿到这个字符串会直接抛编码错误
        # ——浏览器会自动编码，所以前端看不出问题，只有 API 调用方会踩到。
        quoted = urllib.parse.quote(item["name"], safe="")
        value["download_url"] = f"/api/sessions/{session_id}/artifacts/{quoted}"
        value["previewable"] = item.get("kind") == "visualization"
        if item.get("kind") == "visualization":
            value["preview_url"] = f"/api/sessions/{session_id}/artifacts/{quoted}/preview"
            # 图表缩略图与引擎标识：仅 Plotly 图表（存在 .plotly.json）提供缩略图，
            # ECharts 图表无 JSON 数据文件，回退到图标展示。
            artifact_name = item.get("name", "")
            if artifact_name.endswith(".html"):
                stem = artifact_name[: -len(".html")]
                raw_path = item.get("path")
                artifacts_dir = Path(raw_path).parent if raw_path else None
                has_plotly_json = bool(
                    artifacts_dir and (artifacts_dir / f"{stem}.plotly.json").is_file()
                )
                if has_plotly_json:
                    value["thumbnail_url"] = (
                        f"/api/sessions/{session_id}/artifacts/{quoted}/thumbnail"
                    )
                    value["engine"] = "plotly"
                else:
                    value["engine"] = "echarts"
        try:
            value["size_bytes"] = Path(item["path"]).stat().st_size
        except (OSError, KeyError):
            value["size_bytes"] = 0
        value.pop("path", None)
        result.append(value)
    return result


def _elapsed_seconds(record: SessionRecord) -> float | None:
    """Return the analysis duration in seconds, or None when no timing data.

    - running / cancelling: now - started_at
    - completed / cancelled / failed: completed_at - started_at
    - idle: None
    """
    with record._status_lock:  # noqa: SLF001 - 同模块内访问
        status = record._analysis_status  # noqa: SLF001
        started = record.analysis_started_at
        completed = record.analysis_completed_at
    if not started:
        return None
    if status in {"running", "cancelling"}:
        return max(0.0, time.time() - started)
    if completed:
        return max(0.0, completed - started)
    return None


def _session_payload(session_id: str, record: SessionRecord) -> dict[str, Any]:
    workspace = record.workspace
    profile = workspace.profile(sample_rows=8)
    return {
        "id": session_id,
        "filename": workspace.source_path.name if workspace.source_path else "dataset",
        "title": record.title,
        "profile": profile,
        "preview": to_jsonable(workspace.dataframe.head(100)),
        "chat": record.chat,
        "artifacts": _artifact_payload(session_id, workspace.artifacts),
        "analysis_status": record.analysis_status,
        "analysis_started_at": record.analysis_started_at,
        "analysis_completed_at": record.analysis_completed_at,
        "elapsed_seconds": _elapsed_seconds(record),
        "last_result": (
            _result_payload(session_id, record.last_result)
            if record.last_result is not None
            else None
        ),
        # plan_only 模式产出的待审批计划，前端据此渲染审批面板；
        # 为 None 表示当前没有待审批计划。
        "pending_plan": record.pending_plan,
    }


def _result_payload(session_id: str, result: AnalysisResult) -> dict[str, Any]:
    payload = asdict(result)
    payload["artifacts"] = _artifact_payload(session_id, result.artifacts)
    return to_jsonable(payload)


#: 历史消息注入 LLM 上下文时每条内容的最大字符数。assistant 回复是
#: 完整分析报告（结论速览在最前），截断保留头部即可保住核心结论；
#: 避免 8 条长报告在每步 ReAct 调用中重复吃掉上万 token。
_HISTORY_MESSAGE_MAX_CHARS = 2_000


def _trim_history_content(content: str) -> str:
    if len(content) <= _HISTORY_MESSAGE_MAX_CHARS:
        return content
    return content[:_HISTORY_MESSAGE_MAX_CHARS] + "\n…（历史消息过长，已截断）"


def _history(record: SessionRecord) -> list[HumanMessage | AIMessage]:
    messages: list[HumanMessage | AIMessage] = []
    for item in record.chat[-8:]:
        content = _trim_history_content(item["content"])
        if item["role"] == "user":
            messages.append(HumanMessage(content=content))
        else:
            messages.append(AIMessage(content=content))
    return messages


def _artifact_file(session_id: str, filename: str) -> tuple[SessionRecord, Path]:
    # 延迟导入：_artifact_file 通过 ``api.registry`` 访问注册表，以便测试
    # 用 monkeypatch.setattr(api, "registry", ...) 替换时此处也能感知。
    from data_agent import api

    record = api.registry.get(session_id)
    matches = [item for item in record.workspace.artifacts if item["name"] == Path(filename).name]
    if not matches:
        raise HTTPException(status_code=404, detail="产物不存在。")
    path = Path(matches[0]["path"])
    if not path.is_file():
        raise HTTPException(status_code=404, detail="产物文件已被移除。")
    return record, path


# 内置示例数据集：让新用户无需自备文件即可体验完整分析流程。
# 采用销售主题的小型 CSV（订单/地区/品类/金额/数量/日期），覆盖数值、
# 文本、日期三类字段，足以触发清洗、统计、图表等典型工具链。
_SAMPLE_SALES_CSV = (
    "order_id,region,category,product,sales,quantity,order_date,customer_segment\n"
    "1001,华东,电子产品,无线耳机,1280.5,2,2024-01-15,企业\n"
    "1002,华南,办公用品,打印纸,320.0,10,2024-01-18,零售\n"
    "1003,华北,电子产品,机械键盘,890.0,3,2024-01-22,企业\n"
    "1004,华东,家具,人体工学椅,2100.0,1,2024-02-03,政府\n"
    "1005,西南,办公用品,签字笔,75.5,50,2024-02-11,零售\n"
    "1006,华南,电子产品,移动硬盘,560.0,4,2024-02-14,企业\n"
    "1007,华东,家具,书架,780.0,2,2024-02-20,零售\n"
    "1008,华北,电子产品,智能手环,430.0,5,2024-03-01,企业\n"
    "1009,西南,家具,折叠桌,620.0,3,2024-03-05,政府\n"
    "1010,华南,办公用品,文件夹,45.0,100,2024-03-10,零售\n"
    "1011,华东,电子产品,蓝牙音箱,720.0,3,2024-03-15,企业\n"
    "1012,华北,家具,办公沙发,3500.0,1,2024-03-22,政府\n"
    "1013,西南,电子产品,充电宝,210.0,8,2024-04-02,零售\n"
    "1014,华南,办公用品,订书机,38.0,20,2024-04-08,企业\n"
    "1015,华东,家具,储物柜,950.0,2,2024-04-12,零售\n"
    "1016,华北,电子产品,显示器,1800.0,2,2024-04-18,企业\n"
    "1017,西南,办公用品,计算器,65.0,15,2024-04-25,政府\n"
    "1018,华南,家具,会议桌,2800.0,1,2024-05-03,企业\n"
    "1019,华东,电子产品,键盘膜,28.0,30,2024-05-09,零售\n"
    "1020,华北,办公用品,胶带,12.0,200,2024-05-15,零售\n"
)

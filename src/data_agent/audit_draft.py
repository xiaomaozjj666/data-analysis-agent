"""「直接问模型」对照入口：让模型不借助任何工具，直接读表写出三个指标行。

用途与诚实边界
==============
评委现场常问："462 笔是预设的，真实模型会错吗？"这个入口把真实模型的真实输出
摆出来，再交给核验器逐行复算，让人看到「未经验证的模型输出」会错在哪里。

它**刻意不是**一个尽力把数算对的功能，而是一个对照组：

- 提示词只给表和输出格式，**不提示去重口径、不提供公式、不举例**；
- 关闭思考、不绑定任何工具，模型只能凭阅读表格心算；
- 实测（deepseek-flash）这种方式下销售额合计会算错、含退款表的订单数会写成行数；
  而开启思考或调用工具时通常能算对。因此它**不能**代表产品的分析能力，
  也不能据此宣称「模型总是算错」，只说明核验器对未经验证的输出能做什么。
- 本模块只负责取得模型原话并按格式过滤，**绝不替模型修正、补算或猜测数值**；
  模型不参与核验，核验仍由 ``sales_audit`` 读原始 CSV 复算。

隐私：表在发送前会对非指标列做脱敏（手机号、邮箱、身份证与敏感列名），
``order_id`` / ``sales`` / ``quantity`` 三列不脱敏——脱敏会让不同订单号合并、
让金额或销量失真，从而改变要对照的真值。本入口还会在调用期间关闭追踪上报，
避免整张订单明细被送往追踪服务。
"""

from __future__ import annotations

import csv
import dataclasses
import hashlib
import io
import json
import logging
import math
import os
import re
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from langchain_core.messages import HumanMessage
from langsmith import tracing_context

from data_agent.agent import create_chat_model
from data_agent.config import AgentSettings
from data_agent.privacy import mask_cell

logger = logging.getLogger(__name__)

#: 单次最多发送给模型的订单明细行数，超过直接 422（不截断，避免模型对着半张表作答）。
MAX_ROWS = 300
#: 原始 CSV 的字节上限：300 行正常表远小于它，用来挡住单元格异常巨大的文件。
_MAX_BYTES = 1024 * 1024
#: 同一会话两次起草之间的最小间隔（秒），进程内计数。
MIN_INTERVAL_SECONDS = 10.0
#: 一次模型调用的总墙钟上限（秒），同时作为 HTTP 客户端单次请求超时。
_TIMEOUT_SECONDS = 60.0
#: 输出上限：三行指标远用不到，留足余量避免截断到答案之前。
_MAX_TOKENS = 1024

#: 指标列：脱敏会改变它们的取值或去重语义，因此不脱敏。
_PLAIN_COLUMNS = frozenset({"order_id", "sales", "quantity"})
#: 起草轨迹落盘目录（相对工作区根目录），文件名是轨迹自身的 SHA-256。
TRACE_DIR_NAME = "draft_traces"
TRACE_SCHEMA_VERSION = 1

DISCLAIMER = (
    "模型直接读表心算，未调用任何工具；开启思考或调用工具时通常能算对。"
    "这里只展示核验器对未经验证的模型输出能做什么。"
)
_UNCONFIGURED_DETAIL = (
    "未配置模型 API Key。请在页面侧栏的设置里填写 DeepSeek API Key，"
    "或在 .env 中设置 DEEPSEEK_API_KEY 后重试。"
)
_BAD_CONFIG_DETAIL = "模型配置不完整或无效，请在设置里检查 API Key、模型名称与接口地址。"
_UPSTREAM_DETAIL = "模型服务调用失败（可能是网络、额度或 API Key 无效），请稍后重试或检查设置。"
_TIMEOUT_DETAIL = "模型在 60 秒内没有返回结果，请稍后重试。"

# 提示词只含任务、输出格式与表本身。不要在这里加去重口径、公式或示例答案：
# 一旦提示，对照实验就失去了意义（见模块文档）。
_PROMPT_HEAD = (
    "下面是一张销售明细表（CSV 格式）。请直接阅读这张表，写出下面三个指标。\n"
    "只输出这三行，每行严格按此格式；数值只写阿拉伯数字，不加千分位逗号，"
    "销售额最多两位小数；不要输出解释、标题或其他任何文字：\n"
    "销售额合计：<数> 元\n"
    "销量合计：<数> 件\n"
    "订单数：<数> 笔\n"
    "\n"
    "表格如下：\n"
)

# 只认这三种行；其余一律丢弃。数值写法与核验器的草稿解析口径一致。
_LINE_PATTERNS = {
    "销售额合计": re.compile(r"销售额合计：-?[0-9]+(?:\.[0-9]{1,2})? ?元"),
    "销量合计": re.compile(r"销量合计：[0-9]+ ?件"),
    "订单数": re.compile(r"订单数：[0-9]+ ?笔"),
}

_THINK_CLOSED = re.compile(r"<think(?:ing)?>.*?</think(?:ing)?>", re.DOTALL | re.IGNORECASE)
_THINK_ORPHAN_CLOSE = re.compile(r"\A.*?</think(?:ing)?>", re.DOTALL | re.IGNORECASE)
_THINK_UNCLOSED = re.compile(r"<think(?:ing)?>.*\Z", re.DOTALL | re.IGNORECASE)

_LAST_CALL: dict[str, float] = {}
_LAST_CALL_LOCK = threading.Lock()


class QuickDraftError(Exception):
    """起草失败，携带应返回给调用方的状态码与固定中文提示。

    ``detail`` 只含本模块自己写的文案，绝不拼接上游原始报错或任何密钥。
    """

    def __init__(self, status_code: int, detail: str, *, retry_after: int | None = None) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.retry_after = retry_after


def _now() -> float:
    """单调时钟；单独成函数，便于测试注入时间。"""
    return time.monotonic()


def resolve_source(workspace: Any) -> Path:
    """取会话的原始 CSV；与 ``/sales-audit`` 相同：必须是会话 input 目录内的 .csv。"""
    source = workspace.source_path
    if (
        source is None
        or source.suffix.lower() != ".csv"
        or workspace.input_dir.resolve() not in source.resolve().parents
    ):
        raise QuickDraftError(422, "快速起草需要会话上传的原始 CSV。")
    return source


def load_table(source: Path, *, max_rows: int = MAX_ROWS) -> tuple[list[str], list[list[str]]]:
    """读出表头与明细行（字符串原样保留），校验必需列、行数与体积。

    编码支持 UTF-8（含 BOM）与 GB18030，与核验器一致。空行跳过。
    """
    try:
        with source.open("rb") as handle:
            raw = handle.read(_MAX_BYTES + 1)
    except OSError as exc:
        raise QuickDraftError(422, "无法读取原始 CSV，请重新上传数据。") from exc
    if len(raw) > _MAX_BYTES:
        raise QuickDraftError(
            422, f"表体积超过 {_MAX_BYTES // 1024} KB，快速起草只适合小表。请改用完整核验流程。"
        )
    text = None
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise QuickDraftError(422, "快速起草使用 UTF-8 或 GB18030 编码的 CSV，请检查编码。")

    header: list[str] = []
    rows: list[list[str]] = []
    try:
        reader = csv.reader(io.StringIO(text, newline=""), strict=True)
        header = next(reader, [])
        if any(header.count(column) != 1 for column in _PLAIN_COLUMNS):
            raise QuickDraftError(422, "CSV 必须各有一列 order_id、sales、quantity。")
        for row in reader:
            if not row:
                continue
            if len(rows) >= max_rows:
                raise QuickDraftError(
                    422,
                    f"快速起草最多支持 {max_rows} 行订单明细，这张表更长。"
                    "请换一份更小的表，或改用完整核验流程。",
                )
            if len(row) != len(header):
                raise QuickDraftError(422, f"第 {len(rows) + 1} 条记录的列数和表头不一致。")
            rows.append(row)
    except csv.Error as exc:
        raise QuickDraftError(422, "CSV 格式有误，请检查引号与分隔符。") from exc
    if not rows:
        raise QuickDraftError(422, "CSV 至少需要一条订单明细。")
    return header, rows


def mask_table(header: list[str], rows: list[list[str]]) -> tuple[list[list[str]], int]:
    """对非指标列脱敏，返回脱敏后的行与被改动的单元格数。"""
    masked_cells = 0
    result: list[list[str]] = []
    for row in rows:
        out: list[str] = []
        for column, cell in zip(header, row, strict=True):
            if column in _PLAIN_COLUMNS:
                out.append(cell)
                continue
            masked = str(mask_cell(column, cell))
            masked_cells += masked != cell
            out.append(masked)
        result.append(out)
    return result, masked_cells


def build_prompt(header: list[str], rows: list[list[str]]) -> str:
    """把表原样（CSV 文本）放进提示词。"""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    return _PROMPT_HEAD + buffer.getvalue()


def message_text(message: Any) -> tuple[str, int]:
    """取模型消息里的正文；非文本块（如推理块）丢弃并计数。"""
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content, 0
    texts: list[str] = []
    skipped = 0
    for block in content if isinstance(content, list) else []:
        if isinstance(block, str):
            texts.append(block)
        elif isinstance(block, dict) and isinstance(block.get("text"), str):
            if block.get("type", "text") == "text":
                texts.append(block["text"])
            else:
                skipped += 1
        else:
            skipped += 1
    return "\n".join(texts), skipped


def _strip_think(text: str) -> tuple[str, int]:
    """去掉思考标签及其内容，返回剩余文本与被丢弃的行数。"""
    dropped = 0

    def _count(match: re.Match[str]) -> str:
        nonlocal dropped
        dropped += max(1, sum(bool(line.strip()) for line in match.group(0).splitlines()))
        return "\n"

    for pattern in (_THINK_CLOSED, _THINK_ORPHAN_CLOSE, _THINK_UNCLOSED):
        text = pattern.sub(_count, text)
    return text, dropped


def parse_draft(text: str) -> tuple[str, int]:
    """只保留符合「三行格式」的行，返回 (草稿, 被丢弃的非空行数)。

    同一指标重复出现时只留第一行，其余计入丢弃。不做任何改写或补全。
    """
    text, dropped = _strip_think(text)
    kept: list[str] = []
    seen: set[str] = set()
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        label = next((name for name, pat in _LINE_PATTERNS.items() if pat.fullmatch(line)), None)
        if label is None or label in seen:
            dropped += 1
            continue
        seen.add(label)
        kept.append(line)
    return "\n".join(kept), dropped


def _claim_slot(session_id: str) -> None:
    """占用该会话的起草名额；窗口内重复调用抛 429。"""
    now = _now()
    with _LAST_CALL_LOCK:
        for key, stamp in list(_LAST_CALL.items()):
            if now - stamp >= MIN_INTERVAL_SECONDS:
                del _LAST_CALL[key]
        last = _LAST_CALL.get(session_id)
        if last is not None:
            wait = math.ceil(MIN_INTERVAL_SECONDS - (now - last))
            raise QuickDraftError(
                429,
                f"同一会话 {int(MIN_INTERVAL_SECONDS)} 秒内只能起草一次，请 {wait} 秒后再试。",
                retry_after=wait,
            )
        _LAST_CALL[session_id] = now


def _invoke(llm: Any, prompt: str, box: dict[str, Any]) -> None:
    """在工作线程里调用模型。不绑定任何工具；追踪上报在此线程内关闭。"""
    try:
        with tracing_context(enabled=False):
            box["message"] = llm.invoke([HumanMessage(content=prompt)], max_tokens=_MAX_TOKENS)
    except Exception as exc:
        box["error"] = exc


def _call_with_deadline(llm: Any, prompt: str) -> Any:
    """带墙钟上限地调用模型；超时与上游异常分别映射为固定文案。"""
    box: dict[str, Any] = {}
    worker = threading.Thread(
        target=_invoke, args=(llm, prompt, box), name="quick-draft", daemon=True
    )
    worker.start()
    worker.join(_TIMEOUT_SECONDS)
    if worker.is_alive():
        raise QuickDraftError(504, _TIMEOUT_DETAIL)
    if "error" in box:
        # 只记录异常类型：上游报错文本可能带出密钥片段，不写日志也不回显。
        logger.warning("quick-draft 上游调用失败: %s", type(box["error"]).__name__)
        raise QuickDraftError(502, _UPSTREAM_DETAIL)
    return box["message"]


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def write_trace(trace_dir: Path, trace: dict[str, Any]) -> tuple[str, Path]:
    """把轨迹写成规范 JSON，文件名取内容哈希；返回 (哈希, 路径)。

    内容哈希做文件名，让「同一份轨迹」可被重复验证；写入仍走 tmp + replace，
    避免并发读到半截文件。
    """
    text = json.dumps(trace, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    digest = _sha256_text(text)
    trace_dir.mkdir(parents=True, exist_ok=True)
    target = trace_dir / f"{digest}.json"
    if not target.exists():
        temporary = target.with_name(f"{target.name}.{uuid4().hex[:8]}.tmp")
        # 必须按字节写：Windows 的文本模式会把 \n 换成 \r\n，文件名哈希就对不上了。
        temporary.write_bytes(text.encode("utf-8"))
        os.replace(temporary, target)
    return digest, target


def verify_trace(trace_dir: Path, digest: str, draft: str) -> dict[str, Any]:
    """校验轨迹文件存在、内容哈希一致，并且与当前草稿逐字对应。

    任何一项不满足都抛 QuickDraftError(422)；返回解析后的轨迹，供账本记录。
    """
    if not re.fullmatch(r"[0-9a-f]{64}", digest or ""):
        raise QuickDraftError(422, "起草轨迹标识格式不正确。")
    target = trace_dir / f"{digest}.json"
    try:
        raw = target.read_bytes()
    except OSError as exc:
        raise QuickDraftError(422, "找不到对应的起草轨迹，请重新起草。") from exc
    actual = hashlib.sha256(raw).hexdigest()
    if actual != digest:
        raise QuickDraftError(422, "起草轨迹内容与标识不一致，可能已被改动。")
    try:
        trace = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise QuickDraftError(422, "起草轨迹无法解析。") from exc
    if not isinstance(trace, dict):
        raise QuickDraftError(422, "起草轨迹格式不正确。")
    if trace.get("parsed_draft_sha256") != _sha256_text(draft):
        raise QuickDraftError(
            422,
            "草稿在快速起草后被修改过，与轨迹不一致；请重新起草或去掉轨迹绑定后再核验。",
        )
    return trace


def run_quick_draft(
    session_id: str,
    source: Path,
    settings: AgentSettings,
    *,
    trace_dir: Path | None = None,
) -> dict[str, Any]:
    """读表、脱敏、让模型直接作答，并按三行格式过滤其输出。

    ``settings`` 是调用方本次请求合成的配置副本；这里再复制一份并关闭思考，
    只影响本次请求创建的模型实例，不改全局环境变量或进程级设置。
    """
    header, rows = load_table(source)
    if not settings.api_key:
        raise QuickDraftError(409, _UNCONFIGURED_DETAIL)
    masked_rows, masked_cells = mask_table(header, rows)
    prompt = build_prompt(header, masked_rows)

    local = dataclasses.replace(settings, thinking_enabled=False, timeout_seconds=_TIMEOUT_SECONDS)
    try:
        llm = create_chat_model(local)
    except ValueError as exc:
        raise QuickDraftError(409, _BAD_CONFIG_DETAIL) from exc
    except Exception as exc:
        logger.warning("quick-draft 模型创建失败: %s", type(exc).__name__)
        raise QuickDraftError(502, _UPSTREAM_DETAIL) from exc

    _claim_slot(session_id)
    started = time.perf_counter()
    message = _call_with_deadline(llm, prompt)
    elapsed_ms = round((time.perf_counter() - started) * 1000)

    text, skipped_blocks = message_text(message)
    draft, dropped = parse_draft(text)
    dropped += skipped_blocks
    reasoning = (getattr(message, "additional_kwargs", None) or {}).get("reasoning_content")

    trace = {
        "schema_version": TRACE_SCHEMA_VERSION,
        "session_id": session_id,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "mode": "direct_no_tools",
        "model": local.model,
        "thinking_requested": False,
        # 如实反映：网关若忽略了关闭思考的参数并返回了推理内容，这里就是 True。
        "thinking_observed": bool(reasoning),
        "elapsed_ms": elapsed_ms,
        "rows_sent": len(rows),
        "masked_cells": masked_cells,
        "prompt_sha256": _sha256_text(prompt),
        "raw_output": text,
        "raw_output_sha256": _sha256_text(text),
        "parsed_draft": draft,
        "parsed_draft_sha256": _sha256_text(draft),
        "dropped": dropped,
    }
    trace_sha256 = None
    if trace_dir is not None:
        trace_sha256, _ = write_trace(trace_dir, trace)
    return {
        "draft": draft,
        "model": local.model,
        "thinking": bool(reasoning),
        "mode": "direct_no_tools",
        "elapsed_ms": elapsed_ms,
        "rows_sent": len(rows),
        "masked": masked_cells > 0,
        "masked_cells": masked_cells,
        "dropped": dropped,
        "disclaimer": DISCLAIMER,
        # 轨迹绑定：前端把 trace_sha256 随核验请求带上，服务端校验后写进 audit_id
        # 与账本，离线脚本即可验证「模型原话 → 草稿 → 复算 → 确认」整条链。
        "trace_sha256": trace_sha256,
        "raw_output_sha256": trace["raw_output_sha256"],
        "parsed_draft_sha256": trace["parsed_draft_sha256"],
    }

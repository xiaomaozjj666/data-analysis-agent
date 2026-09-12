"""跨源合并辅助：来源解析、键校验、扇出护栏与匹配诊断。

``join_datasets`` 工具把两张表合并成新的活动数据集。合并是数据分析里最容易
被低估的一步，因为它的典型错误都不会抛异常：

- 键不唯一 → 结果行数被静默放大数倍，指标随分母一起变大；
- 键类型不一致（如 ``"1001"`` 与 ``1001``）→ 所有行都匹配不上，产出一张空表
  或全空列；
- 键含缺失值 → NaN 永远不与 NaN 相等，这些行静默丢失。

以上三种都会得到一份"看起来正常"的错误结论。本模块的职责就是在结果进入下游
之前把它们拦下或显著标注，与 ``clean_data`` 的清洗护栏是同一设计取向：护栏由
常量固定，不提供可绕过的参数。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from data_agent.workspace import SUPPORTED_EXTENSIONS

#: 合并结果行数相对两张输入表较大者的最大允许放大倍数。
#: 超过即判定为键不唯一引发的笛卡尔式扇出：例如左表 1000 行、右表 200 行
#: 按重复键连接后得到 2 万行，聚合类指标会随之虚增 20 倍。
MAX_ROW_MULTIPLIER = 5.0

#: 未匹配键占比超过该阈值时，在结果中给出显著警告。
UNMATCHED_WARN_RATIO = 0.2


def list_available_sources(root: Path, subdirs: tuple[Path, ...]) -> list[str]:
    """列出工作区内可被 ``join_datasets`` 读取的数据文件（相对工作区的路径）。

    只列 ``SUPPORTED_EXTENSIONS`` 里的文件：artifacts 目录同时存放
    plotly.min.js、图表 HTML 等非数据产物，把它们混进"可用数据文件"清单
    会直接把模型引向一个必然失败的选项。
    """
    root_resolved = root.resolve()
    found: list[str] = []
    for base in (*subdirs, root):
        try:
            children = sorted(base.iterdir())
        except (FileNotFoundError, NotADirectoryError):
            continue
        for path in children:
            if not path.is_file() or path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                continue
            resolved = path.resolve()
            if root_resolved not in resolved.parents:
                continue
            try:
                relative = str(resolved.relative_to(root_resolved)).replace("\\", "/")
            except ValueError:  # pragma: no cover - 上面的包含检查已覆盖
                continue
            if relative not in found:
                found.append(relative)
    return found


def resolve_source(root: Path, subdirs: tuple[Path, ...], name: str) -> Path:
    """把工具入参里的文件名解析为工作区内的真实文件路径。

    只接受文件名或相对于工作区的相对路径。绝对路径、``..`` 穿越片段，以及
    解析后（含符号链接跟随）落在工作区之外的路径一律拒绝，避免工具被诱导
    读取工作区外的任意文件。

    Args:
        root: 会话工作区根目录。
        subdirs: 优先尝试的子目录（``input`` 存上传文件，``artifacts`` 存导出数据）。
        name: 工具入参中的文件名。

    Returns:
        已确认存在的文件绝对路径。

    Raises:
        ValueError: 名称为空、含路径穿越、为绝对路径或在工作区内找不到。
    """
    raw = (name or "").strip()
    if not raw:
        raise ValueError("right_source 不能为空，请传入工作区内的数据文件名。")
    candidate = Path(raw)
    if candidate.is_absolute():
        raise ValueError("right_source 只接受工作区内的文件名或相对路径，不接受绝对路径。")
    if ".." in candidate.parts:
        raise ValueError("right_source 不允许包含 .. 路径穿越片段。")
    root_resolved = root.resolve()
    for base in (*subdirs, root):
        target = (base / candidate).resolve()
        if root_resolved in target.parents and target.is_file():
            return target
    available = list_available_sources(root, subdirs)
    listing = "、".join(available) if available else "（无）"
    raise ValueError(f"工作区内找不到数据文件「{raw}」，可用文件：{listing}")


def normalize_keys(left_on: list[str] | str, right_on: list[str] | str | None) -> tuple[list[str], list[str]]:
    """把左右连接键规整为等长的字符串列表。

    ``right_on`` 省略时默认与 ``left_on`` 同名；两侧数量不一致直接报错，
    因为静默对齐到较短的列表会按错误的列连接。
    """
    left_keys = [left_on] if isinstance(left_on, str) else list(left_on)
    if not left_keys:
        raise ValueError("left_on 不能为空，请指定左表的连接键列。")
    if right_on is None:
        right_keys = list(left_keys)
    else:
        right_keys = [right_on] if isinstance(right_on, str) else list(right_on)
    if len(left_keys) != len(right_keys):
        raise ValueError(
            f"连接键数量不一致：left_on 有 {len(left_keys)} 列，right_on 有 {len(right_keys)} 列。"
            "多列连接时两侧数量必须相同。"
        )
    if len(set(left_keys)) != len(left_keys):
        raise ValueError("left_on 中存在重复列，请去掉重复后再连接。")
    return left_keys, right_keys


def validate_keys(
    left_frame: pd.DataFrame,
    right_frame: pd.DataFrame,
    left_keys: list[str],
    right_keys: list[str],
) -> None:
    """确认两侧连接键都真实存在，缺失时给出可用列清单便于重试。"""
    missing_left = [key for key in left_keys if key not in left_frame.columns]
    if missing_left:
        raise ValueError(
            f"主数据（左表）中不存在连接键 {missing_left}。"
            f"可用列：{'、'.join(map(str, left_frame.columns))}"
        )
    missing_right = [key for key in right_keys if key not in right_frame.columns]
    if missing_right:
        raise ValueError(
            f"待合并文件（右表）中不存在连接键 {missing_right}。"
            f"可用列：{'、'.join(map(str, right_frame.columns))}"
        )


def key_dtype_notes(
    left_frame: pd.DataFrame,
    right_frame: pd.DataFrame,
    left_keys: list[str],
    right_keys: list[str],
) -> list[str]:
    """找出两侧类型不一致的连接键。

    类型不一致是"一行都匹配不上"的最常见原因：``"1001"`` 与 ``1001``、日期串与
    时间戳都不会相等。这里只报告不自动转换——静默转换可能把 ``"0012"`` 变成
    ``12`` 而改变键语义，是否转换应由模型显式调用 repair_data_format 决定。
    """
    notes: list[str] = []
    for left_key, right_key in zip(left_keys, right_keys, strict=True):
        left_dtype = str(left_frame[left_key].dtype)
        right_dtype = str(right_frame[right_key].dtype)
        if left_dtype != right_dtype:
            notes.append(
                f"连接键类型不一致：「{left_key}」为 {left_dtype}，「{right_key}」为 {right_dtype}，"
                "这通常会导致匹配失败，如需转换请先显式调用 repair_data_format。"
            )
    return notes


def _indicator_name(*frames: pd.DataFrame) -> str:
    """挑一个不与输入表列名冲突的 indicator 列名。

    pandas 在 indicator 列名与已有列重名时会直接抛错，而真实业务表里出现
    ``_merge`` 这类名字并非不可能，因此这里先探测再使用。
    """
    occupied = {str(column) for frame in frames for column in frame.columns}
    name = "_merge"
    while name in occupied:
        name = f"_{name}"
    return name


def _distinct_key_match(
    left_frame: pd.DataFrame,
    right_frame: pd.DataFrame,
    left_keys: list[str],
    right_keys: list[str],
) -> dict[str, int]:
    """在去重后的键集合上统计匹配情况。

    用去重键而非全部行做诊断：一是语义更清楚（"有多少个键没对上"而不是"多少行
    参与了扇出"），二是去重集合通常远小于行数，避免为诊断再造一份全量中间表。
    """
    left_unique = left_frame[left_keys].drop_duplicates()
    right_unique = right_frame[right_keys].drop_duplicates()
    indicator = _indicator_name(left_unique, right_unique)
    probe = left_unique.merge(
        right_unique,
        left_on=left_keys,
        right_on=right_keys,
        how="outer",
        indicator=indicator,
    )
    matches = probe[indicator]
    return {
        "left_distinct_keys": int(len(left_unique)),
        "right_distinct_keys": int(len(right_unique)),
        "matched_keys": int((matches == "both").sum()),
        "left_unmatched_keys": int((matches == "left_only").sum()),
        "right_unmatched_keys": int((matches == "right_only").sum()),
    }


def _null_key_counts(
    left_frame: pd.DataFrame,
    right_frame: pd.DataFrame,
    left_keys: list[str],
    right_keys: list[str],
) -> tuple[int, int]:
    """统计两侧连接键含缺失值的行数（这些行在 pandas 中永远匹配不上）。"""
    left_null = int(left_frame[left_keys].isna().any(axis=1).sum())
    right_null = int(right_frame[right_keys].isna().any(axis=1).sum())
    return left_null, right_null


#: 允许的连接方式。显式校验而不是交给 pandas 报错：这里的错误消息会被
#: 模型读到并据此重试，需要是中文且直接点明可选值。
_VALID_JOIN_HOWS: tuple[str, ...] = ("inner", "left", "right", "outer")


def _validate_how(how: str) -> str:
    normalized = (how or "").strip().lower()
    if normalized not in _VALID_JOIN_HOWS:
        raise ValueError(
            f"不支持的连接方式「{how}」，请从 {'、'.join(_VALID_JOIN_HOWS)} 中选择。"
            "默认用 inner（只保留两侧都匹配上的行）。"
        )
    return normalized


def merge_datasets(
    left_frame: pd.DataFrame,
    right_frame: pd.DataFrame,
    left_keys: list[str],
    right_keys: list[str],
    how: str,
    suffixes: tuple[str, str] = ("_x", "_y"),
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """执行合并并施加护栏，返回 ``(合并结果, 诊断信息)``。

    护栏（不提供绕过参数，与清洗护栏同策略）：
        1. 合并结果为空 → 拒绝。空结果几乎总是键类型或键取值不匹配的信号，
          交付一张空表比报错更糟。
        2. 结果行数超过两张输入表较大者的 ``MAX_ROW_MULTIPLIER`` 倍 → 拒绝。
           这是键不唯一导致的扇出，聚合指标会随之虚增。

    Raises:
        ValueError: 触发上述任一护栏。
    """
    how = _validate_how(how)
    left_rows = int(len(left_frame))
    right_rows = int(len(right_frame))
    indicator = _indicator_name(left_frame, right_frame)
    try:
        merged = left_frame.merge(
            right_frame,
            left_on=left_keys,
            right_on=right_keys,
            how=how,
            suffixes=suffixes,
            indicator=indicator,
        )
    except ValueError as exc:
        # pandas 对"数值列与文本列做连接键"会直接抛英文错误（并建议改用 concat），
        # 但那不是用户想做的事。这里换成可执行的中文引导：统一格式后重试。
        notes = key_dtype_notes(left_frame, right_frame, left_keys, right_keys)
        detail = "；".join(notes) if notes else str(exc)
        raise ValueError(
            f"无法执行合并：{detail} 两侧连接键必须能直接比较，"
            "请先调用 repair_data_format 统一格式后重试，或改用类型一致的列作为连接键。"
        ) from exc
    result_rows = int(len(merged))
    baseline = max(left_rows, right_rows, 1)

    if result_rows == 0:
        dtype_notes = key_dtype_notes(left_frame, right_frame, left_keys, right_keys)
        hint = ("；".join(dtype_notes) + "。") if dtype_notes else ""
        raise ValueError(
            f"合并结果为空（{how} join）：左表 {left_rows} 行、右表 {right_rows} 行，没有任何一行匹配成功。"
            f"{hint}请先确认两侧连接键的取值是否可直接比较；"
            "两侧键类型或取值体系不同时，先调用 inspect_data 查看样例，再用 repair_data_format 统一格式后重试。"
        )

    if result_rows > baseline * MAX_ROW_MULTIPLIER:
        raise ValueError(
            f"拒绝执行：合并会使行数从较大输入表 {baseline} 行膨胀到 {result_rows} 行"
            f"（超过 {MAX_ROW_MULTIPLIER:g} 倍安全上限）。这通常意味着连接键在两侧都不唯一，"
            "产生了多对多的笛卡尔式放大，聚合指标会随之虚增。"
            "请先对键做聚合去重（例如用 statistical_analysis 的分组聚合把明细压成每键一行）后再连接。"
        )

    diagnostics = _distinct_key_match(left_frame, right_frame, left_keys, right_keys)
    left_null, right_null = _null_key_counts(left_frame, right_frame, left_keys, right_keys)
    diagnostics.update(
        {
            "left_rows": left_rows,
            "right_rows": right_rows,
            "result_rows": result_rows,
            "left_null_key_rows": left_null,
            "right_null_key_rows": right_null,
        }
    )
    diagnostics["warnings"] = _build_warnings(diagnostics, how, right_keys)
    merged = merged.drop(columns=[indicator])
    diagnostics["renamed_columns"] = _ambiguous_columns(left_frame, right_frame, left_keys, right_keys, suffixes)
    return merged, diagnostics


#: 未匹配键的实际后果随连接方式变化，文案必须跟着变，否则会误导读者。
#: 例如 right join 下"主数据未匹配的行"是被丢弃的，说成"新增列为空值"就错了。
_LEFT_UNMATCHED_EFFECT: dict[str, str] = {
    "inner": "这些行会被整体排除在结果之外",
    "left": "这些行仍会保留，但右侧新增列为空值",
    "right": "这些行不会出现在结果中",
    "outer": "这些行会保留，但右侧新增列为空值",
}
_RIGHT_UNMATCHED_EFFECT: dict[str, str] = {
    "inner": "这部分数据不会进入结果",
    "left": "这部分数据不会进入结果",
    "right": "这些行会保留，但左侧字段为空值",
    "outer": "这些行会保留，但左侧字段为空值",
}


def _build_warnings(
    diagnostics: dict[str, Any],
    how: str,
    right_keys: list[str],
) -> list[str]:
    """把匹配诊断翻译成模型和用户都能直接引用的警示文案。"""
    warnings: list[str] = []
    left_unmatched = int(diagnostics["left_unmatched_keys"])
    right_unmatched = int(diagnostics["right_unmatched_keys"])
    left_total = max(int(diagnostics["left_distinct_keys"]), 1)
    right_total = max(int(diagnostics["right_distinct_keys"]), 1)

    if left_unmatched and left_unmatched / left_total > UNMATCHED_WARN_RATIO:
        warnings.append(
            f"主数据有 {left_unmatched} 个连接键（占 {left_unmatched / left_total:.0%}）在待合并文件中无匹配，"
            f"{_LEFT_UNMATCHED_EFFECT[how]}，统计时需单独说明。"
        )
    if right_unmatched and right_unmatched / right_total > UNMATCHED_WARN_RATIO:
        warnings.append(
            f"待合并文件有 {right_unmatched} 个连接键（占 {right_unmatched / right_total:.0%}）未被主数据使用，"
            f"{_RIGHT_UNMATCHED_EFFECT[how]}。"
        )
    if int(diagnostics["left_null_key_rows"]):
        warnings.append(
            f"主数据有 {int(diagnostics['left_null_key_rows'])} 行连接键为空，"
            "空值与空值不相等，这些行不会匹配到任何记录。"
        )
    if int(diagnostics["right_null_key_rows"]):
        warnings.append(
            f"待合并文件有 {int(diagnostics['right_null_key_rows'])} 行连接键为空，同样不会参与匹配。"
        )
    if int(diagnostics["result_rows"]) > int(diagnostics["left_rows"]) and how in {"left", "inner"}:
        warnings.append(
            f"结果行数（{int(diagnostics['result_rows'])}）多于主数据行数（{int(diagnostics['left_rows'])}），"
            f"说明待合并文件的「{'、'.join(right_keys)}」存在重复值，已发生一对多放大；"
            "引用按行汇总的指标时须改用分组聚合，不能直接对结果行求和。"
        )
    return warnings


def _ambiguous_columns(
    left_frame: pd.DataFrame,
    right_frame: pd.DataFrame,
    left_keys: list[str],
    right_keys: list[str],
    suffixes: tuple[str, str],
) -> list[str]:
    """列出因两侧重名而被加后缀区分的非键列。"""
    left_only = [column for column in left_frame.columns if column not in left_keys]
    right_only = [column for column in right_frame.columns if column not in right_keys]
    overlapping = [column for column in left_only if column in set(right_only)]
    return [f"{column}{suffix}" for column in overlapping for suffix in suffixes]

"""隐私脱敏的回溯（ReDoS）回归与画像字段泄露测试。

单元格内容来自用户上传，属于不可信输入。``privacy`` 里的邮箱正则曾对不含 ``@``
的长串二次方回溯（60k 个 ``A`` 约 8 秒），一个 20KB 的 ``order_id`` 单元格就能让
上传接口卡住。这里既验证耗时，也用旧正则做差分，保证脱敏语义没有退化。
"""

from __future__ import annotations

import random
import re
import time
from pathlib import Path

import pandas as pd
import pytest

from data_agent.privacy import (
    _EMAIL_SCAN,
    _mask_email_match,
    _mask_text,
    mask_cell,
    mask_sample_records,
)
from data_agent.workspace import DataWorkspace

# 修复前的邮箱正则，仅作差分基准；只喂短串，避免触发它自己的二次方回溯。
_LEGACY_EMAIL = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE)
_EMAIL_MASK = "[已脱敏-邮箱]"

# 修复前 60k 个 A 约 8 秒、20k 约 0.9 秒；线性实现实测约 1ms。
# 预算放宽到 250ms 以留出 CI 调度抖动（GitHub runner 曾出现 50.4ms 的
# 边缘失败）；仍比旧实现低一个数量级以上，且下面的线性放大用例单独守住
# 算法复杂度回归，不靠这个绝对阈值。
_BUDGET_SECONDS = 0.25


def _best_of(func, repeat: int = 3) -> float:
    """取多次中的最短耗时，削掉偶发的调度抖动。"""
    best = float("inf")
    for _ in range(repeat):
        started = time.perf_counter()
        func()
        best = min(best, time.perf_counter() - started)
    return best


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param("A" * 60_000, id="60k个字母"),
        pytest.param("A" * 60_000 + "@", id="60k个字母加@"),
        pytest.param("a." * 30_000, id="点号交替"),
        pytest.param("A-" * 30_000 + "@x", id="连字符交替加@x"),
        pytest.param("a@" * 20_000, id="重复@"),
        pytest.param("a@" + "A." * 20_000, id="域名段重复"),
        pytest.param("x@y" * 20_000, id="重复短片段"),
    ],
)
def test_mask_cell_adversarial_cells_stay_fast(payload: str) -> None:
    elapsed = _best_of(lambda: mask_cell("order_id", payload))
    assert elapsed < _BUDGET_SECONDS, f"{len(payload)} 字符耗时 {elapsed:.3f}s"


def test_mask_cell_scales_linearly_not_quadratically() -> None:
    """长度放大 10 倍，耗时不应放大到接近 100 倍（二次方的特征）。"""
    small = _best_of(lambda: mask_cell("note", "A" * 20_000))
    large = _best_of(lambda: mask_cell("note", "A" * 200_000))
    # 线性约 10 倍；二次方约 100 倍。给到 40 倍的余量并加一个绝对下限，
    # 避免 small 过小导致比值被计时精度放大。
    assert large < max(small * 40, 0.05)


def test_load_with_oversized_cell_does_not_stall(tmp_path: Path) -> None:
    """复现路径：上传带 20KB 单元格的 CSV → load → profile → 样例脱敏。"""
    csv = tmp_path / "huge_cell.csv"
    csv.write_text("order_id,amount\n" + "A" * 20_000 + ",10\nB1,20\n", encoding="utf-8")
    workspace = DataWorkspace(tmp_path / "runs", session_id="redos")

    started = time.perf_counter()
    profile = workspace.load(csv, copy_into_workspace=True)
    elapsed = time.perf_counter() - started

    assert elapsed < 2.0, f"load+profile 耗时 {elapsed:.3f}s"
    assert profile["rows"] == 2
    # 脱敏只作用于送出的样例副本，活动数据集里的原值不变。
    assert workspace.dataframe.loc[0, "order_id"] == "A" * 20_000


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("a@example.com", _EMAIL_MASK),
        ("联系 first.last+tag@sub.example.co.uk 谢谢", f"联系 {_EMAIL_MASK} 谢谢"),
        ("A@B.COM", _EMAIL_MASK),
        ("中文前缀a@b.com", f"中文前缀{_EMAIL_MASK}"),
        ("a@b.com,c@d.org", f"{_EMAIL_MASK},{_EMAIL_MASK}"),
        # 第二个地址紧贴前一个邮箱时也必须脱敏（前向锚点方案会漏掉这一种）。
        ("a@b.com1x@y.org", f"{_EMAIL_MASK}{_EMAIL_MASK}"),
        ("a@b.com.c@d.org", f"{_EMAIL_MASK}{_EMAIL_MASK}"),
        # 不构成邮箱的内容原样保留。
        ("a@b", "a@b"),
        ("no at sign here", "no at sign here"),
        ("@example.com", "@example.com"),
        # 第二个 @ 前没有本地部分字符，新旧实现都不视为邮箱。
        ("user@@example.com", "user@@example.com"),
        ("", ""),
    ],
)
def test_email_masking_semantics_unchanged(text: str, expected: str) -> None:
    assert _mask_text(text) == expected


def test_mobile_and_id_masking_still_work_next_to_emails() -> None:
    text = "邮箱 a@b.com 手机 13812345678 证件 11010519491231002X"
    assert _mask_text(text) == (f"邮箱 {_EMAIL_MASK} 手机 [已脱敏-手机] 证件 [已脱敏-证件]")
    assert mask_cell("note", text) != text
    assert mask_cell("note", "普通备注") == "普通备注"


def test_sensitive_column_name_masking_unchanged() -> None:
    assert mask_cell("customer_phone", "13800001111") == "1*********1"
    assert mask_cell("邮箱", "ab") == "**"
    assert mask_cell("email", "") == ""
    assert mask_cell("email", None) is None


def test_differential_against_legacy_email_regex() -> None:
    """随机短串上，新旧实现的脱敏输出必须逐字符一致。"""
    rng = random.Random(20261119)
    alphabet = [*"aAzZ09._%+-@@@  ,;/中é[]", "com", "org", "x.y", "K", "ſ"]
    for _ in range(20_000):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 40)))
        legacy = _LEGACY_EMAIL.sub(_EMAIL_MASK, text)
        # 只比对邮箱这一层：手机/证件正则本次未改动。
        assert _EMAIL_SCAN.sub(_mask_email_match, text) == legacy, repr(text)


def test_mask_sample_records_masks_embedded_email_and_keeps_numbers() -> None:
    frame = pd.DataFrame(
        {
            "note": ["请回复 a@b.com", "无敏感"],
            "amount": [3.5, float("nan")],
            "qty": [1, 2],
        }
    )
    records = mask_sample_records(frame, 5)
    assert records[0]["note"] == f"请回复 {_EMAIL_MASK}"
    assert records[1]["note"] == "无敏感"
    assert records[0]["amount"] == 3.5
    assert pd.isna(records[1]["amount"])
    assert records[1]["qty"] == 2


def test_profile_source_is_file_name_only(tmp_path: Path) -> None:
    """profile.source 不得带服务器目录（含本机用户名）。"""
    csv = tmp_path / "private_dir" / "sales.csv"
    csv.parent.mkdir()
    csv.write_text("region,sales\nEast,1\n", encoding="utf-8")
    workspace = DataWorkspace(tmp_path / "runs", session_id="source")

    profile = workspace.load(csv, copy_into_workspace=True)

    assert profile["source"] == "sales.csv"
    assert str(tmp_path) not in str(profile)
    assert "private_dir" not in str(profile["source"])


def test_profile_source_is_none_without_loaded_file(tmp_path: Path) -> None:
    workspace = DataWorkspace(tmp_path / "runs", session_id="nosource")
    workspace.dataframe = pd.DataFrame({"x": [1]})
    assert workspace.profile(sample_rows=1)["source"] is None

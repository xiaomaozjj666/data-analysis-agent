from __future__ import annotations

import hashlib

import pytest

from data_agent.sales_audit import audit_sales_brief


@pytest.fixture()
def sales_csv(tmp_path):
    path = tmp_path / "sales.csv"
    path.write_text("order_id,sales,quantity\nA,12.99,9\nA,-2.99,0\n", encoding="utf-8-sig")
    return path


def test_evidence_digest_and_markdown_claims(sales_csv):
    result = audit_sales_brief(
        sales_csv, "- **销售额合计**：**10.00 元**\n* 销量合计: 9 件\n订单数：1 笔"
    )
    assert result["summary"] == {"verified": 3, "conflict": 0, "unsupported": 0}
    assert result["source"]["sha256"] == hashlib.sha256(sales_csv.read_bytes()).hexdigest()


def test_duplicate_metrics_and_code_examples_do_not_become_publishable(sales_csv):
    result = audit_sales_brief(
        sales_csv, "订单数：1 笔\n订单数：2 笔\n~~~md\n销量合计：9 件\n~~~\n销售额合计：10 万元"
    )
    assert result["summary"] == {"verified": 0, "conflict": 0, "unsupported": 6}
    assert result["corrected_brief"] is None


def test_context_requires_scope_confirmation_and_prose_is_withheld(sales_csv):
    draft = "## 经营简报\n订单数：1 笔\n下月销量有望翻倍"
    pending = audit_sales_brief(sales_csv, draft)
    assert pending["needs_scope_confirmation"]
    assert pending["corrected_brief"] is None

    confirmed = audit_sales_brief(sales_csv, draft, confirm_full_table=True)
    assert confirmed["confirmed_full_table"]
    assert confirmed["summary"] == {"verified": 1, "conflict": 0, "unsupported": 2}
    assert "翻倍" not in confirmed["corrected_brief"]
    assert confirmed["claims"][2]["text"] == "下月销量有望翻倍"


@pytest.mark.parametrize("line", ["    订单数：1 笔", "-     订单数：1 笔", "\t订单数：1 笔"])
def test_indented_examples_are_not_treated_as_claims(sales_csv, line):
    result = audit_sales_brief(sales_csv, line, confirm_full_table=True)
    assert result["summary"]["unsupported"] == 1
    assert result["corrected_brief"] is None


def test_exact_id_and_aggregate_beyond_single_row_width(sales_csv):
    sales_csv.write_text(
        "order_id,sales,quantity\nA,9999999999999999.99,999999999999999999\n"
        '"A ",9999999999999999.99,999999999999999999\n',
        encoding="utf-8",
    )
    result = audit_sales_brief(
        sales_csv,
        "销售额合计：19999999999999999.98 元\n销量合计：1999999999999999998 件\n订单数：2 笔",
    )
    assert result["summary"]["verified"] == 3
    assert result["source"]["preview"][1]["order_id"] == "A "


@pytest.mark.parametrize(
    ("row", "message"),
    [
        ("A,0.001,1", "最多两位小数"),
        ("A,,1", "最多两位小数"),
        ("A,1,1.5", "非负整数"),
        (",1,1", "order_id 为空"),
        ("A,1", "列数"),
    ],
)
def test_invalid_source_rows_block_the_report(sales_csv, row, message):
    sales_csv.write_text(f"order_id,sales,quantity\n{row}\n", encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        audit_sales_brief(sales_csv, "订单数：1 笔")


def test_row_limit_blocks_oversized_csv(sales_csv):
    sales_csv.write_text(
        "order_id,sales,quantity\n" + "".join(f"{index},1.00,1\n" for index in range(5)),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="行上限"):
        audit_sales_brief(sales_csv, "订单数：5 笔", max_rows=3)


def test_gb18030_csv_is_accepted(sales_csv):
    """中文 Excel 常见的 GB18030 导出也应能核验。"""
    sales_csv.write_bytes(
        "order_id,sales,quantity\n订单A,12.34,2\n订单B,2.66,1\n".encode("gb18030")
    )
    result = audit_sales_brief(sales_csv, "销售额合计：15.00 元\n销量合计：3 件\n订单数：2 笔")
    assert result["summary"] == {"verified": 3, "conflict": 0, "unsupported": 0}
    assert result["source"]["preview"][0]["order_id"] == "订单A"


def test_undecodable_csv_is_rejected(sales_csv):
    sales_csv.write_bytes(b"order_id,sales,quantity\n\xff\xfe\xff,1,1\n")
    with pytest.raises(ValueError, match="UTF-8 或 GB18030"):
        audit_sales_brief(sales_csv, "订单数：1 笔")

# --- 加固用例：范围门 / 近似重复 / 别名 / 候选 / audit_id ---


def test_scoped_metric_is_never_published_even_after_confirmation(sales_csv):
    """「华东区订单数」不是全表口径，确认全表也不能放行。"""
    draft = "华东区订单数：1 笔"
    pending = audit_sales_brief(sales_csv, draft)
    confirmed = audit_sales_brief(sales_csv, draft, confirm_full_table=True)
    for result in (pending, confirmed):
        assert result["summary"] == {"verified": 0, "conflict": 0, "unsupported": 1}
        assert result["claims"][0]["status"] == "unsupported"
        assert "范围" in result["claims"][0]["reason"]
        assert result["corrected_brief"] is None


@pytest.mark.parametrize("line", ["平均订单数：2 笔", "TOP 订单数：2 笔", "订单数（华东区）：2 笔"])
def test_intent_and_group_qualifiers_are_not_full_table_claims(sales_csv, line):
    result = audit_sales_brief(sales_csv, line, confirm_full_table=True)
    assert result["summary"]["verified"] == 0
    assert result["corrected_brief"] is None


def test_near_duplicate_order_ids_only_warn(tmp_path):
    path = tmp_path / "sales.csv"
    path.write_text(
        "order_id,sales,quantity\n001,1.00,1\n1,1.00,1\nA,1.00,1\na,1.00,1\n",
        encoding="utf-8",
    )
    result = audit_sales_brief(path, "订单数：4 笔")
    assert result["summary"]["verified"] == 1
    codes = {warning["code"] for warning in result["warnings"]}
    assert "near_duplicate_order_id" in codes
    warning = next(w for w in result["warnings"] if w["code"] == "near_duplicate_order_id")
    assert warning["total_groups"] == 2
    assert "caveat" in result["claims"][0]


def test_chinese_column_aliases_are_resolved(tmp_path):
    path = tmp_path / "sales.csv"
    path.write_text(
        "订单号,金额,数量\nA,12.50,2\nB,-2.50,1\n", encoding="utf-8"
    )
    result = audit_sales_brief(path, "销售额合计：10.00 元\n销量合计：3 件\n订单数：2 笔")
    assert result["summary"] == {"verified": 3, "conflict": 0, "unsupported": 0}


def test_ambiguous_alias_columns_are_rejected(tmp_path):
    path = tmp_path / "sales.csv"
    path.write_text("order_id,sales,金额,quantity\nA,1,1,1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="order_id"):
        audit_sales_brief(path, "订单数：1 笔")


def test_unknown_label_with_unit_offers_candidates(sales_csv):
    result = audit_sales_brief(sales_csv, "营业额：10.00 元", confirm_full_table=True)
    claim = result["claims"][0]
    assert claim["status"] == "unsupported"
    assert [item["metric"] for item in claim["candidates"]] == ["销售额合计", "客单价"]


def test_average_order_value_uses_half_up_cents(tmp_path):
    path = tmp_path / "sales.csv"
    path.write_text(
        "order_id,sales,quantity\nA,10.00,1\nB,0.01,1\nC,0.01,1\n", encoding="utf-8"
    )
    result = audit_sales_brief(path, "客单价：3.34 元")
    assert result["summary"] == {"verified": 1, "conflict": 0, "unsupported": 0}


def test_audit_id_binds_source_draft_and_confirmation(sales_csv):
    draft = "订单数：1 笔"
    first = audit_sales_brief(sales_csv, draft)
    again = audit_sales_brief(sales_csv, draft)
    assert first["audit_id"] == again["audit_id"]
    assert first["audit_id"].startswith("aud-")
    assert audit_sales_brief(sales_csv, draft + "\n")["audit_id"] != first["audit_id"]
    assert (
        audit_sales_brief(sales_csv, draft, confirm_full_table=True)["audit_id"]
        != first["audit_id"]
    )


def test_recompute_uses_the_same_byte_snapshot(tmp_path, monkeypatch):
    """哈希必须对应实际参与解析的字节，不能在读取后二次读盘。"""
    from pathlib import Path as _Path

    path = tmp_path / "sales.csv"
    path.write_text("order_id,sales,quantity\nA,1.00,1\n", encoding="utf-8")
    calls = {"n": 0}
    original = _Path.read_bytes

    def one_shot(self):
        calls["n"] += 1
        if calls["n"] > 1:
            raise AssertionError("核验不应二次读取源文件")
        return original(self)

    monkeypatch.setattr(_Path, "read_bytes", one_shot)
    result = audit_sales_brief(path, "订单数：1 笔")
    assert result["summary"] == {"verified": 1, "conflict": 0, "unsupported": 0}
    assert calls["n"] == 1


def test_crlf_draft_lines_are_parsed(sales_csv):
    result = audit_sales_brief(sales_csv, "订单数：1 笔\r\n销量合计：9 件\r\n")
    assert result["summary"] == {"verified": 2, "conflict": 0, "unsupported": 0}

def test_summary_rows_and_blank_lines(sales_csv):
    sales_csv.write_text(
        "order_id,sales,quantity\nA,1.00,1\n合计,1.00,1\n", encoding="utf-8"
    )
    result = audit_sales_brief(sales_csv, "\n\n订单数：2 笔\n")
    assert result["summary"]["verified"] == 1
    codes = {warning["code"] for warning in result["warnings"]}
    assert "summary_row" in codes


def test_empty_csv_and_malformed_quotes_are_rejected(sales_csv):
    sales_csv.write_text("order_id,sales,quantity\n", encoding="utf-8")
    with pytest.raises(ValueError, match="至少需要一条"):
        audit_sales_brief(sales_csv, "订单数：1 笔")

    sales_csv.write_text('order_id,sales,quantity\n"A,1,1\n', encoding="utf-8")
    with pytest.raises(ValueError, match="UTF-8 或 GB18030"):
        audit_sales_brief(sales_csv, "订单数：1 笔")


def test_intent_only_lines_are_context_not_metrics(sales_csv):
    """意图词行是上下文；没有可发布的指标行时，不应弹「确认全表范围」。"""
    result = audit_sales_brief(sales_csv, "同比增长：12%")
    claim = result["claims"][0]
    assert claim["status"] == "unsupported"
    assert "统计范围" in claim["reason"]
    assert result["needs_scope_confirmation"] is False
    assert result["corrected_brief"] is None

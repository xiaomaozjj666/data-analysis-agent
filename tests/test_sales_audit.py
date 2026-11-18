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

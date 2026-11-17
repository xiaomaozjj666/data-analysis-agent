"""方言感知的 SQL 护栏：注释/反引号/美元引号与数仓禁用语句。"""

from __future__ import annotations

import pytest

from data_agent.sql_guard import quote_identifier, validate_select


def test_comments_with_semicolons_are_not_multi_statement():
    assert validate_select("SELECT 1 -- ; DROP TABLE x") == "SELECT 1 -- ; DROP TABLE x"
    assert validate_select("SELECT /* ; */ 1") == "SELECT /* ; */ 1"


def test_backtick_identifier_with_semicolon_is_allowed():
    statement = "SELECT `a;b` FROM `t`"
    assert validate_select(statement) == statement


def test_dollar_quoted_string_with_semicolon_is_allowed():
    statement = "SELECT $$a;b$$ AS v"
    assert validate_select(statement) == statement
    tagged = "SELECT $tag$a;b$tag$ AS v"
    assert validate_select(tagged) == tagged


def test_multiple_statements_still_rejected():
    with pytest.raises(ValueError, match="只允许单条"):
        validate_select("SELECT 1; SELECT 2")
    with pytest.raises(ValueError, match="只允许单条"):
        validate_select("SELECT 1; DELETE FROM t")


@pytest.mark.parametrize(
    "sql,keyword",
    [
        ("MERGE INTO t USING s ON t.id = s.id", "MERGE"),
        ("CALL refresh_metrics()", "CALL"),
        ("EXPORT DATA OPTIONS(uri='gs://x') AS SELECT 1", "EXPORT"),
        ("COPY t FROM '/tmp/x.csv'", "COPY"),
        ("UNLOAD ('select 1') TO 's3://x'", "UNLOAD"),
        ("GRANT SELECT ON t TO role", "GRANT"),
    ],
)
def test_warehouse_write_statements_rejected(sql, keyword):
    with pytest.raises(ValueError) as excinfo:
        validate_select(sql)
    assert keyword in str(excinfo.value)


def test_quote_identifier_by_dialect():
    assert quote_identifier("order") == '"order"'
    assert quote_identifier('a"b') == '"a""b"'
    assert quote_identifier("order", dialect="postgres") == '"order"'
    assert quote_identifier("order", dialect="bigquery") == "`order`"
    assert quote_identifier("a`b", dialect="databricks") == "`a``b`"


def test_default_behavior_unchanged():
    assert validate_select("  select * from t;  ") == "select * from t"
    with pytest.raises(ValueError, match="不能为空"):
        validate_select("   ")
    with pytest.raises(ValueError, match="INSERT"):
        validate_select("INSERT INTO t VALUES (1)")


def test_invalid_timeout_env_falls_back(monkeypatch):
    from data_agent import sql_guard

    monkeypatch.setenv(sql_guard.SQL_TIMEOUT_SECONDS_ENV, "abc")
    assert sql_guard.query_time_budget_ms() == 30_000


def test_non_query_prefix_rejected():
    with pytest.raises(ValueError, match="只允许 SELECT 或 WITH"):
        validate_select("EXPLAIN SELECT 1")


def test_too_many_columns_rejected():
    from data_agent.sql_guard import MAX_RESULT_COLUMNS, collect_rows

    class _Cursor:
        description = [(f"c{i}",) for i in range(MAX_RESULT_COLUMNS + 1)]

        def fetchmany(self, size):
            return []

        def fetchone(self):
            return None

    with pytest.raises(ValueError, match="列上限"):
        collect_rows(_Cursor(), 10)

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


def test_leading_comments_before_keyword_are_allowed():
    """注释在前/夹在关键字后不应误杀（旧实现按空白切首词）。"""
    assert validate_select("/* c */ SELECT 1") == "/* c */ SELECT 1"
    assert validate_select("-- c\nSELECT 1") == "-- c\nSELECT 1"
    assert validate_select("SELECT/**/1") == "SELECT/**/1"


def test_backslash_desync_multi_statement_rejected():
    """反斜杠在标准 SQL 里不转义：'x\\' 已闭合，其后是第二条语句。"""
    with pytest.raises(ValueError, match="只允许单条"):
        validate_select("SELECT 'x\\'; DROP TABLE secrets --'")
    with pytest.raises(ValueError, match="只允许单条"):
        validate_select(
            "SELECT 'x\\'; COMMIT; SET default_transaction_read_only = off; DROP TABLE s --'"
        )


def test_cte_with_write_operations_rejected():
    """Postgres 数据修改 CTE：首词是 WITH，但子句内 DELETE/INSERT/UPDATE。"""
    for sql in (
        "WITH d AS (DELETE FROM t RETURNING *) SELECT * FROM d",
        "WITH s AS (SELECT 1) INSERT INTO log VALUES (1)",
        "WITH u AS (UPDATE t SET x = 1 RETURNING *) SELECT * FROM u",
    ):
        with pytest.raises(ValueError, match="修改数据"):
            validate_select(sql)


def test_select_into_rejected():
    for sql in ("SELECT 1 AS a INTO new_t", "SELECT 1 INTO OUTFILE '/tmp/x'"):
        with pytest.raises(ValueError, match="INTO"):
            validate_select(sql)


def test_dangerous_functions_rejected():
    for sql in (
        "SELECT pg_read_file('/etc/passwd')",
        "SELECT current_setting('DATA_AGENT_DATABASE_URL', true)",
        "SELECT pg_advisory_lock(42)",
        "SELECT pg_advisory_xact_lock(42)",
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity",
        "SELECT lo_import('/etc/shadow')",
        "SELECT load_extension('evil')",
    ):
        with pytest.raises(ValueError, match="不支持调用"):
            validate_select(sql)


def test_dangerous_names_inside_strings_or_comments_are_allowed():
    assert validate_select("SELECT 'pg_read_file' AS word") == "SELECT 'pg_read_file' AS word"
    assert validate_select("SELECT 1 -- pg_read_file") == "SELECT 1 -- pg_read_file"


def test_postgres_estring_backslash_cannot_bypass_checks():
    """Postgres E'...' 无条件反斜杠转义：token 扫描必须双解释取并集。"""
    with pytest.raises(ValueError, match="不支持调用"):
        validate_select("SELECT E'x\\'' , pg_read_file('/etc/passwd')")
    with pytest.raises(ValueError, match="修改数据"):
        validate_select("WITH x AS (SELECT E'a\\'') DELETE FROM t")
    with pytest.raises(ValueError, match="INTO"):
        validate_select("SELECT E'a\\'' INTO new_t")


def test_escaped_double_quote_multi_statement_rejected():
    """MySQL/BigQuery 双引号串支持 \\" 转义；两种解释取并集后必须拒绝。"""
    with pytest.raises(ValueError, match="只允许单条"):
        validate_select('SELECT "a\\"x" ; DROP TABLE t')


def test_estring_with_semicolon_inside_is_single_statement():
    # E-string 内部的分号属于字符串，不是多语句
    assert validate_select("SELECT E'a;b' AS v") == "SELECT E'a;b' AS v"


def test_normal_analytics_queries_still_allowed():
    for sql in (
        "WITH x AS (SELECT 1 AS n) SELECT * FROM x",
        "SELECT 'insert' AS word, 'update' AS other",
        "SELECT count(*) FROM t WHERE note = 'delete'",
        "SELECT * FROM t JOIN s ON t.id = s.id",
    ):
        assert validate_select(sql) == sql


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

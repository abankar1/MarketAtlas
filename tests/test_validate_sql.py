"""Tests for the SQL validator. Run with: pytest tests/test_validate_sql.py"""
import pytest

from src.db.readonly import UnsafeSQLError, validate_sql


# ---- Should pass ----

VALID_QUERIES = [
    # Simple SELECT
    "SELECT 1",
    "SELECT * FROM assets",
    # WITH (CTE)
    "WITH a AS (SELECT * FROM assets) SELECT * FROM a",
    # JOIN
    "SELECT a.symbol FROM assets a JOIN sp500_constituents s ON s.symbol = a.symbol",
    # UNION
    "SELECT symbol FROM sp500_constituents UNION SELECT symbol FROM dow30_constituents",
    # Aggregate
    "SELECT COUNT(*) FROM assets",
    # Window function
    "SELECT symbol, ROW_NUMBER() OVER (ORDER BY symbol) FROM assets",
    # Subquery
    "SELECT * FROM assets WHERE symbol IN (SELECT symbol FROM sp500_constituents)",
    # Trailing semicolon (single)
    "SELECT 1;",
    # Existing LIMIT under cap
    "SELECT * FROM assets LIMIT 50",
]


@pytest.mark.parametrize("sql", VALID_QUERIES)
def test_valid_queries_pass(sql):
    out = validate_sql(sql)
    assert out  # non-empty
    # Every output should have a LIMIT
    assert "LIMIT" in out.upper()


def test_existing_limit_under_cap_preserved():
    out = validate_sql("SELECT * FROM assets LIMIT 50")
    assert "LIMIT 50" in out.upper()


def test_existing_limit_over_cap_capped():
    out = validate_sql("SELECT * FROM assets LIMIT 99999", max_limit=1000)
    assert "1000" in out


def test_no_limit_gets_default():
    out = validate_sql("SELECT * FROM assets", max_limit=1000)
    assert "1000" in out


# ---- Should fail ----

UNSAFE_QUERIES = [
    # Direct DDL/DML
    ("DROP TABLE assets", "drop"),
    ("DELETE FROM assets WHERE 1=1", "delete"),
    ("UPDATE assets SET name = 'x'", "update"),
    ("INSERT INTO assets VALUES ('X')", "insert"),
    ("TRUNCATE assets", "truncate"),
    ("ALTER TABLE assets ADD COLUMN x INT", "alter"),
    ("CREATE TABLE foo (id INT)", "create"),
    ("GRANT ALL ON assets TO public", "grant"),
    # Multi-statement
    ("SELECT 1; DROP TABLE assets", "multiple"),
    ("SELECT 1; SELECT 2", "multiple"),
    # Embedded DML in CTE (Postgres-specific)
    (
        "WITH d AS (DELETE FROM assets RETURNING *) SELECT * FROM d",
        "delete",
    ),
    # Forbidden function
    ("SELECT pg_read_file('/etc/passwd')", "pg_read_file"),
    ("SELECT pg_sleep(60)", "pg_sleep"),
    # Empty
    ("", "empty"),
    ("   ", "empty"),
    # Garbage — sqlglot may or may not raise; either way validator should fail.
    ("not sql at all", None),
]


@pytest.mark.parametrize("sql, hint", UNSAFE_QUERIES)
def test_unsafe_queries_rejected(sql, hint):
    with pytest.raises(UnsafeSQLError) as exc:
        validate_sql(sql)
    if hint:
        assert hint.lower() in str(exc.value).lower()


# ---- Tricky cases ----

def test_select_with_function_call_allowed():
    """Calling a normal aggregate function should be fine."""
    out = validate_sql("SELECT AVG(close) FROM daily_bars WHERE symbol = 'AAPL'")
    assert "AVG" in out.upper()


def test_set_in_set_transaction_blocked():
    with pytest.raises(UnsafeSQLError):
        validate_sql("SET search_path = public")

"""Tests for parsing model output. Does not call the API."""
from src.ai.nl_to_sql import _SQL_TAG


def test_sql_tag_basic():
    text = "<sql>SELECT 1</sql>"
    m = _SQL_TAG.search(text)
    assert m is not None
    assert m.group(1).strip() == "SELECT 1"


def test_sql_tag_multiline():
    text = "<sql>\nSELECT 1\nFROM t\n</sql>"
    m = _SQL_TAG.search(text)
    assert m is not None
    assert "FROM t" in m.group(1)


def test_sql_tag_case_insensitive():
    text = "<SQL>SELECT 1</SQL>"
    m = _SQL_TAG.search(text)
    assert m is not None

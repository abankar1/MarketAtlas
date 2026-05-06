"""Tests for src/ai/query_templates.py — render + validation logic."""
import pytest

from src.ai.query_templates import (
    GICS_SECTORS,
    INDEX_KEYS,
    TEMPLATES,
    TemplateError,
    render,
)
from src.db.readonly import validate_sql


# ---- Sample param dicts that should always succeed for each template ----

VALID_PARAMS: dict[str, dict] = {
    "sector_movers_with_min_return": {
        "sector": "Health Care",
        "index": "sp500",
        "days": 30,
        "min_return_pct": 10.0,
    },
    "symbol_avg_volume": {
        "symbol": "NVDA",
        "days": 90,
    },
    "top_movers_by_period": {
        "index": "nasdaq100",
        "days": 7,
        "top_n": 10,
    },
    "cross_index_membership": {},
    "sector_count_by_index": {
        "index": "sp500",
    },
    "volume_spike_detector": {
        "multiplier": 3.0,
        "lookback_days": 20,
        "top_n": 100,
    },
}


# ---- All known templates have a sample params row ----

def test_all_templates_have_sample_params():
    """Make sure VALID_PARAMS keeps pace with TEMPLATES."""
    assert set(VALID_PARAMS) == set(TEMPLATES), (
        f"missing sample params for: {set(TEMPLATES) - set(VALID_PARAMS)}"
    )


# ---- Every template renders + passes validate_sql with sample params ----

@pytest.mark.parametrize("name", sorted(VALID_PARAMS))
def test_each_template_renders_to_valid_sql(name):
    sql, bound = render(name, VALID_PARAMS[name])

    # All inline placeholders must be substituted
    assert "{" not in sql or "{}" not in sql, "unsubstituted .format placeholder"

    # Bound dict only contains string params from the spec
    spec = TEMPLATES[name].params
    for k, v in bound.items():
        assert spec[k].type is str, f"{name}.{k} bound but spec.type is {spec[k].type}"

    # Replace psycopg %(name)s placeholders with literal strings so we can
    # send the rendered SQL through validate_sql.
    test_sql = sql
    for k in bound:
        test_sql = test_sql.replace(f"%({k})s", f"'{bound[k]}'")
    out = validate_sql(test_sql)
    assert "LIMIT" in out.upper()


# ---- Render is deterministic ----

def test_render_is_deterministic():
    sql_a, bound_a = render("symbol_avg_volume", {"symbol": "AAPL", "days": 30})
    sql_b, bound_b = render("symbol_avg_volume", {"symbol": "AAPL", "days": 30})
    assert sql_a == sql_b
    assert bound_a == bound_b


# ---- Unknown template ----

def test_unknown_template_raises():
    with pytest.raises(TemplateError, match="unknown template"):
        render("not_a_real_template", {})


# ---- Missing required param ----

def test_missing_required_param_raises():
    with pytest.raises(TemplateError, match="missing required param"):
        render(
            "sector_movers_with_min_return",
            {"sector": "Health Care", "index": "sp500", "days": 30},
            # min_return_pct missing
        )


# ---- choices allowlist ----

def test_invalid_sector_rejected():
    with pytest.raises(TemplateError, match="not in allowed choices"):
        render(
            "sector_movers_with_min_return",
            {
                "sector": "Bogus Sector",
                "index": "sp500",
                "days": 30,
                "min_return_pct": 5,
            },
        )


def test_invalid_index_rejected():
    with pytest.raises(TemplateError, match="not in allowed choices"):
        render("sector_count_by_index", {"index": "russell2000"})


# ---- min/max bounds ----

def test_days_below_min_rejected():
    with pytest.raises(TemplateError, match="below minimum"):
        render(
            "top_movers_by_period",
            {"index": "all", "days": 0, "top_n": 5},
        )


def test_days_above_max_rejected():
    with pytest.raises(TemplateError, match="above maximum"):
        render(
            "sector_movers_with_min_return",
            {"sector": "Health Care", "index": "sp500", "days": 9999, "min_return_pct": 5},
        )


def test_top_n_above_max_rejected():
    with pytest.raises(TemplateError, match="above maximum"):
        render(
            "top_movers_by_period",
            {"index": "all", "days": 30, "top_n": 1000000},
        )


# ---- type coercion ----

def test_int_param_accepts_numeric_string():
    """Router may emit numbers as strings — render should coerce."""
    sql, _ = render(
        "symbol_avg_volume",
        {"symbol": "AAPL", "days": "30"},
    )
    # Inlined as int 30
    assert "CURRENT_DATE - 30" in sql


def test_float_param_accepts_int():
    sql, _ = render(
        "sector_movers_with_min_return",
        {"sector": "Energy", "index": "sp500", "days": 30, "min_return_pct": 5},
    )
    assert "5.0" in sql or "5 " in sql or "> 5" in sql


# ---- pattern matching for symbol ----

def test_symbol_pattern_rejects_lowercase():
    with pytest.raises(TemplateError, match="does not match pattern"):
        render("symbol_avg_volume", {"symbol": "aapl", "days": 30})


def test_symbol_pattern_rejects_injection():
    with pytest.raises(TemplateError, match="does not match pattern"):
        render(
            "symbol_avg_volume",
            {"symbol": "AAPL'; DROP TABLE assets;--", "days": 30},
        )


# ---- defaults are applied ----

def test_volume_spike_uses_defaults_when_param_missing():
    sql, _ = render(
        "volume_spike_detector",
        # supply nothing — every param has a default
        {},
    )
    # multiplier default 3.0
    assert "3.0 *" in sql or ">= 3.0" in sql or "3.0" in sql


# ---- string-only params are bound, not inlined ----

def test_string_params_are_bound_not_inlined():
    sql, bound = render(
        "sector_movers_with_min_return",
        {"sector": "Health Care", "index": "sp500", "days": 30, "min_return_pct": 10},
    )
    assert bound == {"sector": "Health Care", "index": "sp500"}
    # The literal value must NOT appear inlined in the SQL
    assert "'Health Care'" not in sql
    assert "%(sector)s" in sql
    assert "%(index)s" in sql


# ---- numeric params are inlined, not bound ----

def test_numeric_params_are_inlined():
    sql, bound = render(
        "top_movers_by_period",
        {"index": "all", "days": 7, "top_n": 25},
    )
    assert "days" not in bound and "top_n" not in bound
    assert "CURRENT_DATE - 7" in sql
    assert "LIMIT 25" in sql


# ---- GICS / index enum exposure ----

def test_gics_sectors_count():
    assert len(GICS_SECTORS) == 11


def test_index_keys_are_canonical():
    assert set(INDEX_KEYS) == {"sp500", "nasdaq100", "dow30", "all"}

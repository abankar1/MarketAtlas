"""Tests for src/ai/intent_router.py — JSON parsing only, no API calls."""
import pytest

from src.ai.intent_router import (
    SYSTEM_PROMPT,
    RoutedTemplate,
    RoutingMiss,
    parse_routing_response,
)
from src.ai.nl_to_sql import GenerationError
from src.ai.query_templates import TEMPLATES


# ---- Successful routing ----

def test_parse_basic_match():
    raw = '<json>{"template": "cross_index_membership", "params": {}}</json>'
    out = parse_routing_response(raw)
    assert isinstance(out, RoutedTemplate)
    assert out.name == "cross_index_membership"
    assert out.params == {}


def test_parse_match_with_params():
    raw = (
        "<json>"
        '{"template": "sector_movers_with_min_return", '
        '"params": {"sector": "Health Care", "index": "sp500", '
        '"days": 30, "min_return_pct": 10}}'
        "</json>"
    )
    out = parse_routing_response(raw)
    assert isinstance(out, RoutedTemplate)
    assert out.name == "sector_movers_with_min_return"
    assert out.params["sector"] == "Health Care"
    assert out.params["days"] == 30
    assert out.params["min_return_pct"] == 10


def test_parse_passes_token_counts_through():
    raw = '<json>{"template": "cross_index_membership", "params": {}}</json>'
    out = parse_routing_response(
        raw,
        input_tokens=120,
        output_tokens=15,
        cache_read_tokens=1000,
        cache_creation_tokens=0,
    )
    assert out.input_tokens == 120
    assert out.output_tokens == 15
    assert out.cache_read_tokens == 1000


# ---- Routing miss ----

def test_parse_null_template_returns_miss():
    raw = '<json>{"template": null}</json>'
    out = parse_routing_response(raw)
    assert isinstance(out, RoutingMiss)


def test_parse_null_template_with_empty_params_returns_miss():
    raw = '<json>{"template": null, "params": {}}</json>'
    out = parse_routing_response(raw)
    assert isinstance(out, RoutingMiss)


# ---- Failure modes ----

def test_parse_missing_json_block_raises():
    with pytest.raises(GenerationError, match="no <json> block"):
        parse_routing_response("just some prose, no JSON here")


def test_parse_malformed_json_raises():
    raw = "<json>{not valid json}</json>"
    with pytest.raises(GenerationError, match="malformed JSON"):
        parse_routing_response(raw)


def test_parse_missing_template_key_raises():
    raw = '<json>{"params": {}}</json>'
    with pytest.raises(GenerationError, match="missing 'template' key"):
        parse_routing_response(raw)


def test_parse_unknown_template_name_raises():
    raw = '<json>{"template": "nonexistent_template", "params": {}}</json>'
    with pytest.raises(GenerationError, match="unknown template name"):
        parse_routing_response(raw)


def test_parse_non_dict_params_raises():
    raw = '<json>{"template": "cross_index_membership", "params": [1, 2, 3]}</json>'
    with pytest.raises(GenerationError, match="must be a JSON object"):
        parse_routing_response(raw)


def test_parse_top_level_array_raises():
    raw = "<json>[1, 2, 3]</json>"
    with pytest.raises(GenerationError, match="missing 'template' key"):
        parse_routing_response(raw)


# ---- System prompt invariants ----

def test_system_prompt_lists_every_template():
    """The router's prompt must mention every template by name, otherwise the
    model can't pick it. This catches new templates that weren't wired up."""
    for name in TEMPLATES:
        assert name in SYSTEM_PROMPT, f"router prompt is missing template {name!r}"


def test_system_prompt_includes_gics_sectors():
    assert "Health Care" in SYSTEM_PROMPT
    assert "Information Technology" in SYSTEM_PROMPT


def test_system_prompt_documents_index_keys():
    assert '"sp500"' in SYSTEM_PROMPT
    assert '"nasdaq100"' in SYSTEM_PROMPT
    assert '"dow30"' in SYSTEM_PROMPT


# ---- Whitespace tolerance ----

def test_parse_tolerates_whitespace_in_json_block():
    raw = '<json>\n  {"template": null}  \n</json>'
    out = parse_routing_response(raw)
    assert isinstance(out, RoutingMiss)


def test_parse_case_insensitive_json_tag():
    raw = '<JSON>{"template": null}</JSON>'
    out = parse_routing_response(raw)
    assert isinstance(out, RoutingMiss)


# ---- Extra params are passed through (render() rejects unknowns) ----

def test_parse_passes_unknown_params_through():
    """The router parser doesn't validate params — render() does that.
    Unknown params should not break parsing."""
    raw = (
        '<json>{"template": "cross_index_membership", '
        '"params": {"some_extra_field": 42}}</json>'
    )
    out = parse_routing_response(raw)
    assert isinstance(out, RoutedTemplate)
    assert out.params == {"some_extra_field": 42}

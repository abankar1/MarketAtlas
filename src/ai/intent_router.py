"""
Intent router — single Claude call that maps a natural-language question to
one of the pre-approved templates in src/ai/query_templates.py.

The model never sees the templates' SQL bodies. Its only outputs are a
JSON object {"template": "<name>"|null, "params": {...}}. That output passes
through query_templates.render(), which validates every parameter against
its declared spec before any SQL is executed.

If the model returns {"template": null}, the caller should fall back to the
existing src/ai/nl_to_sql.py free-form generator.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from .client import AIClient
from .nl_to_sql import GenerationError
from .query_templates import GICS_SECTORS, INDEX_KEYS, TEMPLATES


# ----------------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------------

@dataclass
class RoutedTemplate:
    name: str
    params: dict
    raw_response: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int


@dataclass
class RoutingMiss:
    """Returned when the model decides no template fits."""
    raw_response: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int


_JSON_TAG = re.compile(r"<json>(.*?)</json>", re.DOTALL | re.IGNORECASE)


# ----------------------------------------------------------------------------
# System prompt — built once at import time, cached at the API on first use
# ----------------------------------------------------------------------------

def _format_param(name: str, spec) -> str:
    bits = [f"{name} ({spec.type.__name__}"]
    if spec.choices:
        bits.append(f"; one of: {', '.join(repr(c) for c in spec.choices)}")
    if spec.min is not None or spec.max is not None:
        rng = []
        if spec.min is not None:
            rng.append(f"min {spec.min}")
        if spec.max is not None:
            rng.append(f"max {spec.max}")
        bits.append("; " + ", ".join(rng))
    if spec.pattern:
        bits.append(f"; pattern {spec.pattern}")
    if not spec.required and spec.default is not None:
        bits.append(f"; default {spec.default!r}")
    bits.append(")")
    out = "".join(bits)
    if spec.description:
        out += f" — {spec.description}"
    return out


def _build_template_catalogue() -> str:
    sections = []
    for i, t in enumerate(TEMPLATES.values(), start=1):
        param_lines = (
            "\n".join(f"    - {_format_param(n, s)}" for n, s in t.params.items())
            if t.params else "    (none)"
        )
        examples = "\n".join(f"    - {ex}" for ex in t.nl_examples)
        sections.append(
            f"{i}. {t.name}\n"
            f"   {t.description}\n"
            f"   Params:\n{param_lines}\n"
            f"   Example questions:\n{examples}"
        )
    return "\n\n".join(sections)


SYSTEM_PROMPT = f"""\
You are an intent router for MarketAtlas, a stock-market data warehouse.

Your job: given the user's question, output JSON identifying which pre-approved
query template (if any) answers it, with parameter values extracted from the
question.

ABSOLUTE RULES:
1. Output JSON ONLY, wrapped in <json>...</json> tags. No prose, no markdown.
2. Schema: {{"template": "<name>" | null, "params": {{...}}}}
3. If no template fits the question, output {{"template": null}}. NEVER invent
   template names or parameter values.
4. Every parameter value must be a literal JSON value (string, number, true/false,
   null) — no expressions, no SQL.
5. For string params with a `one of` allowlist, use one of the listed values
   exactly (case-sensitive). If the user's phrasing maps clearly to one of them
   (e.g. "S&P 500" → "sp500", "tech" → "Information Technology"), use that.
   If you cannot map it confidently, output {{"template": null}}.
6. For numeric params, infer reasonable defaults from common phrases:
     "the past week"  → 7
     "this month"     → 30
     "last quarter"   → 90
     "this year"      → 365
   But if the user states an explicit number, use that.
7. FOLLOW-UPS: The user message may begin with a
   <previous_context>previously discussed ticker: XYZ</previous_context>
   line. Use that ticker for `symbol` ONLY when the new question is a
   referential follow-up with no ticker, company, sector, or index of its
   own — e.g. "what about volume?", "and the highs?", "how about 90 days?".
   If the new question names ANY ticker, company, sector, or index, ignore
   the previous context. Never bind the previous ticker into a template
   that operates over a sector or index instead of a single symbol.
8. RELEVANCE OVER ELIGIBILITY: Match a template ONLY when its OUTPUT
   directly answers what the user asked. If the question is about price,
   return, performance, highs/lows, or "doing better/worse" and the
   only candidate template returns just volume (or vice versa), output
   {{"template": null}} — the AI-SQL fallback will write a query that
   actually answers the question. Do not stretch a template just because
   one of its params (e.g. `days`) happens to fit a phrase in the
   question.

Available index-key mapping (string → INDEX_KEYS):
  S&P 500          → "sp500"
  NASDAQ-100       → "nasdaq100"
  NASDAQ           → "nasdaq100"
  Dow / Dow 30     → "dow30"
  All / any        → "all"

Available GICS sectors (use exact spelling): {", ".join(GICS_SECTORS)}

Available templates:

{_build_template_catalogue()}

Examples:

<example>
<question>Which Health Care stocks in the S&P 500 are up more than 10% in the last 30 days?</question>
<json>{{"template": "sector_movers_with_min_return", "params": {{"sector": "Health Care", "index": "sp500", "days": 30, "min_return_pct": 10}}}}</json>
</example>

<example>
<question>Top 5 NASDAQ-100 movers this week</question>
<json>{{"template": "top_movers_by_period", "params": {{"index": "nasdaq100", "days": 7, "top_n": 5}}}}</json>
</example>

<example>
<question>What's NVDA's average volume over 90 days?</question>
<json>{{"template": "symbol_avg_volume", "params": {{"symbol": "NVDA", "days": 90}}}}</json>
</example>

<example>
<question>Stocks in all three indices?</question>
<json>{{"template": "cross_index_membership", "params": {{}}}}</json>
</example>

<example>
<question>Sector breakdown of the Dow</question>
<json>{{"template": "sector_count_by_index", "params": {{"index": "dow30"}}}}</json>
</example>

<example>
<question>Symbols with 3x normal volume today</question>
<json>{{"template": "volume_spike_detector", "params": {{"multiplier": 3.0, "lookback_days": 20, "top_n": 100}}}}</json>
</example>

<example>
<question>What's the P/E ratio for Apple?</question>
<json>{{"template": null}}</json>
</example>

<example>
<question>Highest closing price for AAPL in 2024</question>
<json>{{"template": null}}</json>
</example>

<example>
<question><previous_context>previously discussed ticker: GOOGL</previous_context>
has it been any better in the last year?</question>
<json>{{"template": null}}</json>
</example>

<example>
<question>How is AAPL doing this year?</question>
<json>{{"template": null}}</json>
</example>
"""


# ----------------------------------------------------------------------------
# route()
# ----------------------------------------------------------------------------

def route(
    client: AIClient,
    question: str,
    *,
    last_ticker: str | None = None,
    recent_turns: tuple | list | None = None,
) -> RoutedTemplate | RoutingMiss:
    """
    Single Claude call. Returns RoutedTemplate on a clean match, RoutingMiss
    if the model decided no template fits.

    `last_ticker`, when supplied, lets the router resolve referential
    follow-ups ("what about its volume?") by binding the prior ticker into
    the matching template's `symbol` param. Explicit tickers in the new
    question always override.

    `recent_turns`, when supplied, provides multi-turn conversational
    memory — a sliding window of the user's recent questions and the
    symbols/summary each query returned. Lets the router resolve
    "their names", "and the bottom 3", etc. when last_ticker alone isn't
    enough (multi-stock previous results clear last_ticker).

    Raises GenerationError if the response can't be parsed as JSON or the
    chosen template name is not in the registry.
    """
    from src.ai.memory import format_transcript  # avoid circular at import-time

    question = question.strip()
    if not question:
        raise GenerationError("empty question")

    transcript = format_transcript(recent_turns)
    ticker_line = (
        f"<previous_context>previously discussed ticker: {last_ticker}</previous_context>\n"
        if last_ticker else ""
    )
    user_message = (
        (transcript + "\n" if transcript else "")
        + ticker_line
        + question
    )

    response = client.complete(
        system=SYSTEM_PROMPT,
        user=user_message,
        max_tokens=400,
        stop_sequences=["</json>"],
        temperature=0.0,
        cacheable_system=True,
    )

    raw = response.text
    if response.stop_reason == "stop_sequence":
        raw = raw + "</json>"

    return parse_routing_response(
        raw,
        input_tokens=response.input_tokens,
        output_tokens=response.output_tokens,
        cache_read_tokens=response.cache_read_tokens,
        cache_creation_tokens=response.cache_creation_tokens,
    )


def parse_routing_response(
    raw: str,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
) -> RoutedTemplate | RoutingMiss:
    """
    Pure parser — broken out so tests can drive it without an AIClient.

    Pulls the JSON out of <json>...</json>, validates structure, and either
    returns a RoutedTemplate (template name + params) or a RoutingMiss
    (template was null).
    """
    match = _JSON_TAG.search(raw)
    if not match:
        raise GenerationError(
            f"router: no <json> block in response: {raw[:200]!r}"
        )
    body = match.group(1).strip()

    try:
        data = json.loads(body)
    except json.JSONDecodeError as e:
        raise GenerationError(f"router: malformed JSON: {e}; got {body[:200]!r}") from e

    if not isinstance(data, dict) or "template" not in data:
        raise GenerationError(
            f"router: missing 'template' key in JSON: {body[:200]!r}"
        )

    name = data.get("template")
    if name is None:
        return RoutingMiss(
            raw_response=raw,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_creation_tokens=cache_creation_tokens,
        )

    if not isinstance(name, str) or name not in TEMPLATES:
        raise GenerationError(
            f"router: unknown template name {name!r}; "
            f"valid names are {sorted(TEMPLATES)}"
        )

    params = data.get("params", {})
    if not isinstance(params, dict):
        raise GenerationError(
            f"router: 'params' must be a JSON object, got {type(params).__name__}"
        )

    return RoutedTemplate(
        name=name,
        params=params,
        raw_response=raw,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_creation_tokens=cache_creation_tokens,
    )


# Reference to silence linters and let tests assert the catalogue covers all
# known templates and enum values.
__all__ = [
    "RoutedTemplate",
    "RoutingMiss",
    "SYSTEM_PROMPT",
    "route",
    "parse_routing_response",
    "GICS_SECTORS",
    "INDEX_KEYS",
]

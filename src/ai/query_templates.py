"""
Pre-approved parameterized SQL templates for the Ask tab.

Each entry in TEMPLATES is a code-reviewed SELECT/WITH query with a small,
typed parameter surface. The router (src/ai/intent_router.py) chooses one
by name and extracts the parameter values from the user's natural-language
question; this module then validates the params and renders the final SQL
+ bound psycopg params for execute_safe().

Why two parameter surfaces?
    - String / date params are passed through psycopg's named-parameter
      binding (%(name)s) — psycopg handles escaping and SQL injection.
    - Int / float params are inlined into the SQL string at render time
      via str.format(). They are *only* substituted after a strict type
      and range check — the substituted value is therefore guaranteed to
      be a literal Python int/float, which has a safe str() representation.
      We inline numerics because PostgreSQL forbids parameterised LIMIT in
      a way that survives the AST-level validator's LIMIT-rewriting logic,
      and INTERVAL arithmetic is cleanest with literal day counts.

Adding a template:
    1. Append a QueryTemplate to TEMPLATES.
    2. Make sure every {placeholder} is declared as an int/float ParamSpec
       and every %(placeholder)s is declared as a str/date ParamSpec.
    3. Provide 1-3 nl_examples — these are the router's primary signal.
    4. Module import runs _check_template() on every entry; structural
       errors (typos, dangling placeholders, missing params) abort startup.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import sqlglot
from sqlglot import expressions as exp


# Whitelisted enum values — used both as router prompt copy and as
# render-time validation guards.

GICS_SECTORS: tuple[str, ...] = (
    "Communication Services",
    "Consumer Discretionary",
    "Consumer Staples",
    "Energy",
    "Financials",
    "Health Care",
    "Industrials",
    "Information Technology",
    "Materials",
    "Real Estate",
    "Utilities",
)

INDEX_KEYS: tuple[str, ...] = ("sp500", "nasdaq100", "dow30", "all")


# ----------------------------------------------------------------------------
# Param specs and template dataclass
# ----------------------------------------------------------------------------

@dataclass(frozen=True)
class ParamSpec:
    type: type                       # str | int | float
    required: bool = True
    default: Any = None
    choices: tuple = ()              # whitelist (e.g. GICS_SECTORS)
    min: Any = None
    max: Any = None
    pattern: str = ""                # regex (str only, e.g. ^[A-Z]{1,5}$)
    description: str = ""            # router prompt copy


@dataclass(frozen=True)
class QueryTemplate:
    name: str
    description: str
    sql: str
    params: dict[str, ParamSpec] = field(default_factory=dict)
    nl_examples: tuple[str, ...] = ()


class TemplateError(ValueError):
    """Raised by render() when params fail validation or template is unknown."""


# ----------------------------------------------------------------------------
# Reusable SQL fragments
# ----------------------------------------------------------------------------

# Universe filter: which constituents qualify, given an `index` param.
# %(index)s is psycopg-bound — value is one of INDEX_KEYS.
_INDEX_FILTER = """\
(%(index)s = 'all'
 OR (%(index)s = 'sp500'     AND EXISTS (SELECT 1 FROM sp500_constituents     sp WHERE sp.symbol = a.symbol AND sp.is_active IS NOT FALSE))
 OR (%(index)s = 'nasdaq100' AND EXISTS (SELECT 1 FROM nasdaq100_constituents nq WHERE nq.symbol = a.symbol AND nq.is_active IS NOT FALSE))
 OR (%(index)s = 'dow30'     AND EXISTS (SELECT 1 FROM dow30_constituents     d  WHERE d.symbol  = a.symbol AND d.is_active  IS NOT FALSE)))\
"""


# ----------------------------------------------------------------------------
# Template definitions
# ----------------------------------------------------------------------------

_T_SECTOR_MOVERS = QueryTemplate(
    name="sector_movers_with_min_return",
    description=(
        "Stocks in a given GICS sector + index whose adjusted close return "
        "over the last N days exceeds a threshold."
    ),
    params={
        "sector":         ParamSpec(str, choices=GICS_SECTORS,
                                    description="Exact GICS sector name"),
        "index":          ParamSpec(str, choices=INDEX_KEYS,
                                    description="sp500 | nasdaq100 | dow30 | all"),
        "days":           ParamSpec(int, min=1, max=730,
                                    description="Lookback window in calendar days (1-730)"),
        "min_return_pct": ParamSpec(float,
                                    description="Return threshold, e.g. 10 for +10%"),
    },
    sql="""\
WITH universe AS (
  SELECT a.symbol, a.name
  FROM assets a
  WHERE a.gics_sector = %(sector)s AND {index_filter}
),
start_px AS (
  SELECT DISTINCT ON (b.symbol) b.symbol, b.adj_close AS px
  FROM daily_bars b
  JOIN universe u ON u.symbol = b.symbol
  WHERE (b.ts AT TIME ZONE 'UTC')::date >= (SELECT MAX((ts AT TIME ZONE 'UTC')::date) FROM daily_bars) - {days}
  ORDER BY b.symbol, b.ts ASC
),
end_px AS (
  SELECT DISTINCT ON (b.symbol) b.symbol, b.adj_close AS px
  FROM daily_bars b
  JOIN universe u ON u.symbol = b.symbol
  ORDER BY b.symbol, b.ts DESC
)
SELECT u.symbol, u.name,
       ROUND(((e.px - s.px) / s.px * 100)::numeric, 2) AS return_pct
FROM universe u
JOIN start_px s ON s.symbol = u.symbol
JOIN end_px   e ON e.symbol = u.symbol
WHERE ((e.px - s.px) / s.px * 100) > {min_return_pct}
ORDER BY return_pct DESC
LIMIT 100""",
    nl_examples=(
        "Which Health Care stocks in the S&P 500 are up more than 10% in the last 30 days?",
        "Energy names in the NASDAQ-100 that gained 5% this quarter",
    ),
)


_T_SYMBOL_AVG_VOLUME = QueryTemplate(
    name="symbol_avg_volume",
    description="Average daily volume for one symbol over the last N days.",
    params={
        "symbol": ParamSpec(str, pattern=r"^[A-Z][A-Z0-9.\-]{0,9}$",
                            description="Ticker symbol, uppercase"),
        "days":   ParamSpec(int, min=1, max=3650,
                            description="Lookback window in calendar days (1-3650)"),
    },
    sql="""\
SELECT ROUND(AVG(volume)) AS avg_volume,
       COUNT(*)           AS trading_days
FROM daily_bars
WHERE symbol = %(symbol)s
  AND (ts AT TIME ZONE 'UTC')::date >= (SELECT MAX((ts AT TIME ZONE 'UTC')::date) FROM daily_bars) - {days}
LIMIT 1""",
    nl_examples=(
        "What's the average daily volume for NVDA over the past 90 days?",
        "Average volume of AAPL last 30 days",
    ),
)


_T_TOP_MOVERS = QueryTemplate(
    name="top_movers_by_period",
    description=(
        "Top N stocks in the chosen index by adjusted-close return over "
        "the last N days, regardless of sector."
    ),
    params={
        "index": ParamSpec(str, choices=INDEX_KEYS,
                           description="sp500 | nasdaq100 | dow30 | all"),
        "days":  ParamSpec(int, min=1, max=3650,
                           description="Lookback window in calendar days"),
        "top_n": ParamSpec(int, min=1, max=100,
                           description="Number of rows to return"),
    },
    sql="""\
WITH universe AS (
  SELECT DISTINCT a.symbol, a.name
  FROM assets a
  WHERE {index_filter}
),
start_px AS (
  SELECT DISTINCT ON (b.symbol) b.symbol, b.adj_close AS px
  FROM daily_bars b
  JOIN universe u ON u.symbol = b.symbol
  WHERE (b.ts AT TIME ZONE 'UTC')::date >= (SELECT MAX((ts AT TIME ZONE 'UTC')::date) FROM daily_bars) - {days}
  ORDER BY b.symbol, b.ts ASC
),
end_px AS (
  SELECT DISTINCT ON (b.symbol) b.symbol, b.adj_close AS px
  FROM daily_bars b
  JOIN universe u ON u.symbol = b.symbol
  ORDER BY b.symbol, b.ts DESC
)
SELECT u.symbol, u.name,
       ROUND(((e.px - s.px) / s.px * 100)::numeric, 2) AS return_pct
FROM universe u
JOIN start_px s ON s.symbol = u.symbol
JOIN end_px   e ON e.symbol = u.symbol
ORDER BY return_pct DESC
LIMIT {top_n}""",
    nl_examples=(
        "Show me the top 10 NASDAQ-100 stocks by return over the past week.",
        "Top 25 S&P 500 gainers in the last month",
        "Worst performers in the Dow 30 over 90 days",
    ),
)


_T_CROSS_INDEX = QueryTemplate(
    name="cross_index_membership",
    description="Symbols that are constituents of all three indices simultaneously.",
    params={},
    sql="""\
SELECT a.symbol, a.name, a.gics_sector
FROM assets a
JOIN sp500_constituents     sp ON sp.symbol = a.symbol AND sp.is_active IS NOT FALSE
JOIN nasdaq100_constituents nq ON nq.symbol = a.symbol AND nq.is_active IS NOT FALSE
JOIN dow30_constituents     d  ON d.symbol  = a.symbol AND d.is_active  IS NOT FALSE
ORDER BY a.symbol
LIMIT 100""",
    nl_examples=(
        "Which stocks appear in all three indices?",
        "Symbols common to S&P 500, NASDAQ-100 and Dow 30",
    ),
)


_T_SECTOR_COUNT = QueryTemplate(
    name="sector_count_by_index",
    description="Count of active constituents per GICS sector for the chosen index.",
    params={
        "index": ParamSpec(str, choices=INDEX_KEYS,
                           description="sp500 | nasdaq100 | dow30 | all"),
    },
    sql="""\
SELECT a.gics_sector, COUNT(*) AS n
FROM assets a
WHERE a.gics_sector IS NOT NULL AND {index_filter}
GROUP BY a.gics_sector
ORDER BY n DESC
LIMIT 50""",
    nl_examples=(
        "How many stocks are in each sector across the S&P 500?",
        "Sector breakdown for the NASDAQ-100",
    ),
)


_T_VOLUME_SPIKE = QueryTemplate(
    name="volume_spike_detector",
    description=(
        "Symbols whose most recent volume is at least N× their lookback "
        "average daily volume."
    ),
    params={
        "multiplier":     ParamSpec(float, default=3.0, min=1.0, max=100.0,
                                    description="Spike threshold (e.g. 3.0)"),
        "lookback_days":  ParamSpec(int, default=20, min=5, max=90,
                                    description="Average window in days"),
        "top_n":          ParamSpec(int, default=100, min=1, max=100,
                                    description="Maximum rows to return"),
    },
    sql="""\
WITH recent AS (
  SELECT symbol,
         (ts AT TIME ZONE 'UTC')::date AS d,
         volume
  FROM daily_bars
  WHERE (ts AT TIME ZONE 'UTC')::date >= (SELECT MAX((ts AT TIME ZONE 'UTC')::date) FROM daily_bars) - {lookback_days_plus_5}
),
ranked AS (
  SELECT symbol, d, volume,
         ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY d DESC) AS rn
  FROM recent
),
today_vol AS (
  SELECT symbol, volume AS today_volume
  FROM ranked
  WHERE rn = 1
),
avg_vol AS (
  SELECT symbol, AVG(volume) AS avg_volume
  FROM ranked
  WHERE rn BETWEEN 2 AND ({lookback_days} + 1)
  GROUP BY symbol
)
SELECT t.symbol, a.name,
       t.today_volume,
       ROUND(av.avg_volume) AS avg_volume,
       ROUND((t.today_volume / NULLIF(av.avg_volume, 0))::numeric, 2) AS volume_multiple
FROM today_vol t
JOIN avg_vol av ON av.symbol = t.symbol
JOIN assets  a  ON a.symbol  = t.symbol
WHERE t.today_volume >= {multiplier} * av.avg_volume
ORDER BY volume_multiple DESC NULLS LAST
LIMIT {top_n}""",
    nl_examples=(
        "Find symbols where today's volume is at least 3x the 20-day average.",
        "Stocks trading at 5x normal volume",
    ),
)


TEMPLATES: dict[str, QueryTemplate] = {
    t.name: t
    for t in (
        _T_SECTOR_MOVERS,
        _T_SYMBOL_AVG_VOLUME,
        _T_TOP_MOVERS,
        _T_CROSS_INDEX,
        _T_SECTOR_COUNT,
        _T_VOLUME_SPIKE,
    )
}


# ----------------------------------------------------------------------------
# Render — validate params, return executable SQL + bound params
# ----------------------------------------------------------------------------

# Special inline-substitution keys that aren't first-class params but are
# derived from them. Currently just _lookback_days_plus_5_ used by the
# volume_spike_detector to size the recent-rows window.
_SPECIAL_INLINE = {
    "volume_spike_detector": (
        lambda p: {"lookback_days_plus_5": int(p["lookback_days"]) + 5}
    ),
}


def _check_value(name: str, spec: ParamSpec, value: Any) -> Any:
    """Validate a single param value against its spec; return the coerced value."""
    if value is None:
        if spec.required and spec.default is None:
            raise TemplateError(f"missing required param: {name}")
        if spec.default is not None:
            value = spec.default
        else:
            return None

    # Type coerce: accept ints into float specs and numeric strings into int specs.
    if spec.type is int:
        try:
            value = int(value)
        except (TypeError, ValueError) as e:
            raise TemplateError(f"{name}: expected int, got {value!r}") from e
    elif spec.type is float:
        try:
            value = float(value)
        except (TypeError, ValueError) as e:
            raise TemplateError(f"{name}: expected float, got {value!r}") from e
    elif spec.type is str:
        if not isinstance(value, str):
            raise TemplateError(f"{name}: expected str, got {type(value).__name__}")

    if spec.choices and value not in spec.choices:
        raise TemplateError(
            f"{name}: {value!r} not in allowed choices "
            f"({', '.join(map(repr, spec.choices))})"
        )
    if spec.min is not None and value < spec.min:
        raise TemplateError(f"{name}: {value} below minimum {spec.min}")
    if spec.max is not None and value > spec.max:
        raise TemplateError(f"{name}: {value} above maximum {spec.max}")
    if spec.pattern and not re.fullmatch(spec.pattern, value):
        raise TemplateError(
            f"{name}: {value!r} does not match pattern {spec.pattern}"
        )
    return value


def render(template_name: str, params: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """
    Validate `params` against the template's spec and produce executable SQL
    plus a dict of psycopg-named bound params.

    Returns (sql, bound_params). Caller passes them straight to execute_safe().

    Raises TemplateError for:
      - unknown template_name
      - missing required params
      - wrong type / out of range / not in choices / pattern mismatch
    """
    if template_name not in TEMPLATES:
        raise TemplateError(f"unknown template: {template_name}")
    template = TEMPLATES[template_name]

    coerced: dict[str, Any] = {}
    for name, spec in template.params.items():
        coerced[name] = _check_value(name, spec, params.get(name))

    # Numeric (int/float) params are inlined via .format(); strings are bound.
    inline: dict[str, Any] = {}
    bound: dict[str, Any] = {}
    for name, spec in template.params.items():
        v = coerced[name]
        if spec.type in (int, float):
            inline[name] = v
        else:
            bound[name] = v

    # Add the index_filter fragment when the template uses it.
    if "{index_filter}" in template.sql:
        inline["index_filter"] = _INDEX_FILTER

    # Per-template derived inline values.
    if template_name in _SPECIAL_INLINE:
        inline.update(_SPECIAL_INLINE[template_name](coerced))

    sql = template.sql.format(**inline)
    return sql, bound


# ----------------------------------------------------------------------------
# Module-import-time check — every template must parse as a single SELECT/WITH
# ----------------------------------------------------------------------------

_PLACEHOLDER_INT_RE = re.compile(r"\{[a-zA-Z_][a-zA-Z0-9_]*\}")
_PLACEHOLDER_NAMED_RE = re.compile(r"%\(([a-zA-Z_][a-zA-Z0-9_]*)\)s")


def _check_template(t: QueryTemplate) -> None:
    """
    Substitute every placeholder with a safe dummy value and parse the result
    with sqlglot to make sure the SQL is syntactically valid and rooted in
    a SELECT/WITH/UNION/INTERSECT/EXCEPT.

    Run once per template at module import. Any failure raises immediately,
    aborting application startup with a descriptive error.
    """
    sql = t.sql
    # Replace named %(x)s placeholders (strings/dates) with a quoted dummy
    sql = _PLACEHOLDER_NAMED_RE.sub("'__placeholder__'", sql)
    # Replace {x} format placeholders (ints/floats/fragments) with a literal 0
    # — but for index_filter we substitute a benign predicate instead.
    sql = sql.replace("{index_filter}", "(TRUE)")
    sql = _PLACEHOLDER_INT_RE.sub("0", sql)

    try:
        parsed = sqlglot.parse_one(sql, dialect="postgres")
    except Exception as e:
        raise RuntimeError(
            f"template {t.name!r}: SQL fails to parse — {e}\n--- rendered ---\n{sql}"
        ) from e
    allowed = (exp.Select, exp.With, exp.Union, exp.Intersect, exp.Except)
    if not isinstance(parsed, allowed):
        raise RuntimeError(
            f"template {t.name!r}: root node must be SELECT/WITH/UNION/INTERSECT/"
            f"EXCEPT, got {type(parsed).__name__}"
        )

    # Verify every {placeholder} in the SQL is either a declared param or a
    # known special inline key. Catches typos.
    declared = set(t.params) | {"index_filter"}
    if t.name in _SPECIAL_INLINE:
        declared |= set(_SPECIAL_INLINE[t.name]({k: 1 for k in t.params}).keys())
    for ph in _PLACEHOLDER_INT_RE.findall(t.sql):
        key = ph[1:-1]
        if key not in declared:
            raise RuntimeError(
                f"template {t.name!r}: undeclared {{{key}}} placeholder"
            )

    # And every %(name)s placeholder must map to a declared str param.
    for name in _PLACEHOLDER_NAMED_RE.findall(t.sql):
        if name not in t.params:
            raise RuntimeError(
                f"template {t.name!r}: undeclared %({name})s placeholder"
            )
        if t.params[name].type is not str:
            raise RuntimeError(
                f"template {t.name!r}: %({name})s is bound but spec.type is "
                f"{t.params[name].type.__name__} — only str/date params should "
                "be psycopg-bound. Use {{{name}}} for numeric params."
            )


for _t in TEMPLATES.values():
    _check_template(_t)

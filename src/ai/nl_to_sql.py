"""Natural-language → SQL generation for MarketAtlas."""
from __future__ import annotations

import re
from dataclasses import dataclass

import sqlglot
from sqlglot import expressions as exp

from .client import AIClient


# ----------------------------------------------------------------------------
# Schema injection — must match the actual database schema in src/db/.
# Keep this DDL in sync with reality. If you add a column, add it here too.
# ----------------------------------------------------------------------------

SCHEMA_DDL = """\
-- All tables are in the public schema. Read-only access only.

CREATE TABLE assets (
  symbol TEXT PRIMARY KEY,
  name TEXT,                      -- company display name
  exchange_code TEXT,
  exchange TEXT,
  asset_type TEXT,
  price_currency TEXT,
  last_refreshed DATE,            -- date of most recent bar fetch
  gics_sector TEXT,               -- one of 11 GICS sectors, may be NULL
  updated_at TIMESTAMPTZ
);

CREATE TABLE daily_bars (
  symbol TEXT,                    -- FK to assets.symbol
  ts TIMESTAMPTZ,                 -- ALWAYS cast: (ts AT TIME ZONE 'UTC')::date
  open NUMERIC,
  high NUMERIC,
  low NUMERIC,
  close NUMERIC,
  volume BIGINT,
  adj_open NUMERIC,
  adj_high NUMERIC,
  adj_low NUMERIC,
  adj_close NUMERIC,              -- PREFER over close for return calculations
  adj_volume BIGINT,
  split_factor NUMERIC,
  dividend NUMERIC,
  PRIMARY KEY (symbol, ts)
);
-- daily_bars is a TimescaleDB hypertable. Queries over date ranges are fast.

CREATE TABLE sp500_constituents (
  symbol TEXT PRIMARY KEY,
  security TEXT,                  -- name column (DIFFERS from other constituent tables)
  is_active BOOLEAN,
  removed_date DATE,
  updated_at TIMESTAMPTZ
);

CREATE TABLE nasdaq100_constituents (
  symbol TEXT PRIMARY KEY,
  company TEXT,                   -- name column
  is_active BOOLEAN,
  removed_date DATE,
  updated_at TIMESTAMPTZ
);

CREATE TABLE dow30_constituents (
  symbol TEXT PRIMARY KEY,
  company TEXT,                   -- name column
  is_active BOOLEAN,
  removed_date DATE,
  updated_at TIMESTAMPTZ
);
"""

VALID_SECTORS = """\
The 11 valid values for assets.gics_sector:
'Communication Services', 'Consumer Discretionary', 'Consumer Staples',
'Energy', 'Financials', 'Health Care', 'Industrials',
'Information Technology', 'Materials', 'Real Estate', 'Utilities'
"""


# ----------------------------------------------------------------------------
# Examples — these are the model's primary style guide. Adding well-chosen
# examples is the most effective way to fix output quality issues. Each
# example demonstrates a non-obvious pattern.
# ----------------------------------------------------------------------------

EXAMPLES = """\
<example>
<question>Which Health Care stocks in the S&P 500 are up more than 10% in the last 30 days?</question>
<sql>
WITH universe AS (
  SELECT a.symbol, a.name
  FROM assets a
  JOIN sp500_constituents sp ON sp.symbol = a.symbol
  WHERE sp.is_active IS NOT FALSE
    AND a.gics_sector = 'Health Care'
),
start_px AS (
  SELECT DISTINCT ON (b.symbol) b.symbol, b.adj_close AS px
  FROM daily_bars b
  JOIN universe u ON u.symbol = b.symbol
  WHERE (b.ts AT TIME ZONE 'UTC')::date >= CURRENT_DATE - INTERVAL '30 days'
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
JOIN end_px e ON e.symbol = u.symbol
WHERE ((e.px - s.px) / s.px * 100) > 10
ORDER BY return_pct DESC
LIMIT 100;
</sql>
</example>

<example>
<question>What's the average daily volume for NVDA over the past 90 days?</question>
<sql>
SELECT ROUND(AVG(volume)) AS avg_volume
FROM daily_bars
WHERE symbol = 'NVDA'
  AND (ts AT TIME ZONE 'UTC')::date >= CURRENT_DATE - INTERVAL '90 days';
</sql>
</example>

<example>
<question>Which stocks appear in all three indices?</question>
<sql>
SELECT a.symbol, a.name, a.gics_sector
FROM assets a
JOIN sp500_constituents sp ON sp.symbol = a.symbol AND sp.is_active IS NOT FALSE
JOIN nasdaq100_constituents nq ON nq.symbol = a.symbol AND nq.is_active IS NOT FALSE
JOIN dow30_constituents d ON d.symbol = a.symbol AND d.is_active IS NOT FALSE
ORDER BY a.symbol;
</sql>
</example>

<example>
<question>Show me the top 10 NASDAQ-100 stocks by return over the past week.</question>
<sql>
WITH universe AS (
  SELECT a.symbol, a.name
  FROM assets a
  JOIN nasdaq100_constituents nq ON nq.symbol = a.symbol
  WHERE nq.is_active IS NOT FALSE
),
start_px AS (
  SELECT DISTINCT ON (b.symbol) b.symbol, b.adj_close AS px
  FROM daily_bars b
  JOIN universe u ON u.symbol = b.symbol
  WHERE (b.ts AT TIME ZONE 'UTC')::date >= CURRENT_DATE - INTERVAL '7 days'
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
JOIN end_px e ON e.symbol = u.symbol
ORDER BY return_pct DESC
LIMIT 10;
</sql>
</example>

<example>
<question>Find symbols where today's volume is at least 3x the 20-day average volume.</question>
<sql>
WITH recent AS (
  SELECT symbol,
         (ts AT TIME ZONE 'UTC')::date AS d,
         volume
  FROM daily_bars
  WHERE (ts AT TIME ZONE 'UTC')::date >= CURRENT_DATE - INTERVAL '25 days'
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
  SELECT symbol, AVG(volume) AS avg_20d_volume
  FROM ranked
  WHERE rn BETWEEN 2 AND 21
  GROUP BY symbol
)
SELECT t.symbol, a.name,
       t.today_volume,
       ROUND(av.avg_20d_volume) AS avg_20d_volume,
       ROUND((t.today_volume / av.avg_20d_volume)::numeric, 2) AS volume_multiple
FROM today_vol t
JOIN avg_vol av ON av.symbol = t.symbol
JOIN assets a ON a.symbol = t.symbol
WHERE t.today_volume >= 3 * av.avg_20d_volume
ORDER BY volume_multiple DESC
LIMIT 100;
</sql>
</example>

<example>
<question>How many stocks are in each sector across the S&P 500?</question>
<sql>
SELECT a.gics_sector, COUNT(*) AS n
FROM assets a
JOIN sp500_constituents sp ON sp.symbol = a.symbol
WHERE sp.is_active IS NOT FALSE
  AND a.gics_sector IS NOT NULL
GROUP BY a.gics_sector
ORDER BY n DESC;
</sql>
</example>

<example>
<question>What was the highest closing price for AAPL in 2024?</question>
<sql>
SELECT MAX(close) AS max_close, MAX(adj_close) AS max_adj_close
FROM daily_bars
WHERE symbol = 'AAPL'
  AND (ts AT TIME ZONE 'UTC')::date >= '2024-01-01'
  AND (ts AT TIME ZONE 'UTC')::date <= '2024-12-31';
</sql>
</example>

<example>
<question>What's the P/E ratio for Apple?</question>
<sql>
SELECT 'CANNOT_ANSWER' AS reason,
       'fundamentals data (P/E, EPS, market cap) is not in this database' AS detail;
</sql>
</example>

<example>
<question>what is going on with netflix stock?</question>
<sql>
-- "netflix" → NFLX (resolved here, not in SQL).
WITH recent AS (
  SELECT ts, adj_close, volume,
         ROW_NUMBER() OVER (ORDER BY ts DESC) AS rn
  FROM daily_bars
  WHERE symbol = 'NFLX'
    AND (ts AT TIME ZONE 'UTC')::date >= CURRENT_DATE - INTERVAL '30 days'
),
window_stats AS (
  SELECT
    MAX(CASE WHEN rn = 1 THEN adj_close END) AS latest_close,
    MIN(CASE WHEN rn = 1 THEN ts END)::date AS latest_date,
    MAX(CASE WHEN rn = (SELECT MAX(rn) FROM recent) THEN adj_close END) AS start_close,
    AVG(volume) AS avg_volume,
    COUNT(*) AS trading_days
  FROM recent
)
SELECT
  'NFLX' AS symbol,
  latest_date,
  ROUND(latest_close::numeric, 2) AS latest_close,
  ROUND(start_close::numeric, 2) AS price_30d_ago,
  ROUND(((latest_close - start_close) / start_close * 100)::numeric, 2) AS return_pct_30d,
  ROUND(avg_volume) AS avg_daily_volume_30d,
  trading_days
FROM window_stats
LIMIT 1;
</sql>
</example>

<example>
<question>how is tesla doing</question>
<sql>
-- "tesla" → TSLA.
WITH recent AS (
  SELECT ts, adj_close, volume,
         ROW_NUMBER() OVER (ORDER BY ts DESC) AS rn
  FROM daily_bars
  WHERE symbol = 'TSLA'
    AND (ts AT TIME ZONE 'UTC')::date >= CURRENT_DATE - INTERVAL '30 days'
),
window_stats AS (
  SELECT
    MAX(CASE WHEN rn = 1 THEN adj_close END) AS latest_close,
    MIN(CASE WHEN rn = 1 THEN ts END)::date AS latest_date,
    MAX(CASE WHEN rn = (SELECT MAX(rn) FROM recent) THEN adj_close END) AS start_close,
    AVG(volume) AS avg_volume,
    COUNT(*) AS trading_days
  FROM recent
)
SELECT
  'TSLA' AS symbol,
  latest_date,
  ROUND(latest_close::numeric, 2) AS latest_close,
  ROUND(start_close::numeric, 2) AS price_30d_ago,
  ROUND(((latest_close - start_close) / start_close * 100)::numeric, 2) AS return_pct_30d,
  ROUND(avg_volume) AS avg_daily_volume_30d,
  trading_days
FROM window_stats
LIMIT 1;
</sql>
</example>

<example>
<question>microsft daily volume past 60 days</question>
<sql>
-- Misspelling "microsft" → MSFT (typo of "microsoft").
SELECT ROUND(AVG(volume)) AS avg_volume,
       COUNT(*) AS trading_days
FROM daily_bars
WHERE symbol = 'MSFT'
  AND (ts AT TIME ZONE 'UTC')::date >= CURRENT_DATE - INTERVAL '60 days'
LIMIT 1;
</sql>
</example>

<example>
<question>hello</question>
<sql>
SELECT 'CANNOT_ANSWER' AS reason,
       'not a data question — ask about a stock, sector, or index (e.g. "what is going on with NFLX?", "Top 5 NASDAQ-100 movers this week")' AS detail;
</sql>
</example>

<example>
<question>are you working?</question>
<sql>
SELECT 'CANNOT_ANSWER' AS reason,
       'meta-question — ask a stock data question instead (e.g. "How is AAPL doing?", "Top S&P 500 gainers this month")' AS detail;
</sql>
</example>
"""


SYSTEM_PROMPT = f"""\
You are a SQL generator for MarketAtlas, a stock-market data warehouse running on
PostgreSQL with the TimescaleDB extension.

YOUR ONLY JOB is to convert the user's question into ONE PostgreSQL query.

ABSOLUTE RULES (never violate these):
1. Output exactly ONE SQL statement, wrapped in <sql>...</sql> tags. Output nothing else
   — no preamble, no explanation, no markdown fences.
2. The query MUST start with SELECT or WITH. NEVER produce INSERT, UPDATE, DELETE,
   DROP, ALTER, CREATE, TRUNCATE, GRANT, REVOKE, COPY, CALL, DO, or any other
   statement type.
3. Always include LIMIT (max 1000) unless the query returns a single aggregate value.
4. Always cast timestamps with (ts AT TIME ZONE 'UTC')::date when comparing to dates.
5. Always filter constituent tables with: WHERE is_active IS NOT FALSE
   (NOT "= TRUE" — there are legacy NULL rows that must be included.)
6. Use adj_close, not close, when calculating returns or comparing prices across time.
7. The name column differs by constituent table: sp500_constituents.security,
   nasdaq100_constituents.company, dow30_constituents.company. Prefer assets.name
   when joining.
8. CANNOT_ANSWER is for exactly two situations:
   (a) The input contains NO reference to any company, stock, index, or
       market concept — pure greetings ("hello", "hi"), meta-questions
       about the system ("are you working?"), or gibberish with no
       financial meaning.
   (b) The question asks for data this database does not contain:
       P/E ratios, EPS, earnings, analyst ratings, news, options.
   In every other case — including vague, casual, or ambiguous questions
   that mention any company or market topic — generate SQL. When in
   doubt, generate SQL.
9. COMPANY NAME → TICKER: Resolve any company name (full, partial,
   misspelled, nicknamed) to its correct US exchange ticker using your
   training knowledge, then write SQL with only the ticker symbol:
   WHERE symbol = 'ABNB'. DO NOT use ILIKE on assets.name.
   You know the tickers for all major US-listed companies.
   If a name genuinely has no matching US-listed ticker, use
   CANNOT_ANSWER with: 'could not identify a US ticker for "<name>"'.
10. Vague questions naming a company ARE data questions. "tell me about
    airbnb", "what's going on with amazon", "okay what about nvidia",
    "how is uber doing" — any of these must return a last-30-days price
    summary: latest close, 30-day return %, avg daily volume. NEVER
    refuse on grounds of vagueness. NEVER ask for clarification.
11. NEVER respond conversationally. Output ONLY the <sql>...</sql> block.
12. FOLLOW-UPS: The user message may begin with a
    <previous_context>previously discussed ticker: XYZ</previous_context>
    line. Use that ticker ONLY when the new question is clearly a
    follow-up with no ticker, company name, or sector of its own —
    e.g. "what about volume?", "and the highs?", "how about last 90 days?".
    If the new question names ANY ticker, company, sector, or index, treat
    the previous context as irrelevant and answer the new question on its
    own terms. Never bind the previous ticker into a multi-stock or
    sector-level query.

Schema:
{SCHEMA_DDL}

{VALID_SECTORS}

Examples:
{EXAMPLES}
"""


# ----------------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------------

@dataclass
class GeneratedQuery:
    sql: str
    raw_response: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int


class CannotAnswerError(Exception):
    """The model declined to generate SQL because the question is out of scope."""
    def __init__(self, reason: str, detail: str = ""):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


class GenerationError(Exception):
    """The model output could not be parsed as SQL."""


_SQL_TAG = re.compile(r"<sql>(.*?)</sql>", re.DOTALL | re.IGNORECASE)
_CANNOT_ANSWER = re.compile(r"CANNOT_ANSWER", re.IGNORECASE)
_SYMBOL_LITERAL = re.compile(r"symbol\s*=\s*'([A-Z0-9.\-]{1,10})'", re.IGNORECASE)


def _with_context(question: str, last_ticker: str | None) -> str:
    """
    Prepend a one-line context preamble when we have a remembered ticker.
    The model is instructed (rule 12 in SYSTEM_PROMPT) to use it ONLY when
    the new question is referential ("what about volume?", "and the highs?")
    and contains no ticker of its own.
    """
    if not last_ticker:
        return question
    return (
        f"<previous_context>previously discussed ticker: {last_ticker}</previous_context>\n"
        f"{question}"
    )


def extract_single_ticker(sql: str) -> str | None:
    """
    Return the single ticker referenced by `WHERE symbol = 'XXX'` if (and only
    if) the SQL references exactly one. Used by the Ask tab to remember the
    "current" stock so a follow-up like "what about its volume?" can reuse it
    without the user retyping the ticker.

    Multi-ticker queries (cross-stock comparisons), sector/index queries with
    no symbol literal, and ambiguous matches all return None — we deliberately
    only carry context forward when it's unambiguous.
    """
    matches = {m.group(1).upper() for m in _SYMBOL_LITERAL.finditer(sql or "")}
    return next(iter(matches)) if len(matches) == 1 else None


def generate_sql(
    client: AIClient,
    question: str,
    *,
    last_ticker: str | None = None,
) -> GeneratedQuery:
    """
    Generate a SQL query from a natural-language question.

    `last_ticker` carries forward the single ticker (if any) referenced by
    the immediately preceding successful query, so follow-ups like
    "what about its volume?" or "and the highs?" resolve correctly. The
    model is told to use it only when the new question has no ticker of
    its own and is clearly referential — explicit tickers always override.

    Raises CannotAnswerError if the model declines.
    Raises GenerationError if the output cannot be parsed.
    """
    question = question.strip()
    if not question:
        raise GenerationError("empty question")

    user_message = _with_context(question, last_ticker)

    response = client.complete(
        system=SYSTEM_PROMPT,
        user=user_message,
        max_tokens=800,
        stop_sequences=["</sql>"],
        temperature=0.0,
        cacheable_system=True,  # the system prompt is large and stable
    )

    # If the model hit the stop sequence, the closing tag was eaten — restore it
    # so the regex can match.
    raw = response.text
    if response.stop_reason == "stop_sequence":
        raw = raw + "</sql>"

    match = _SQL_TAG.search(raw)
    if not match:
        # The model went off-script — produced prose instead of a <sql> block.
        # Treat this the same as a CANNOT_ANSWER refusal: it's almost always
        # because the input wasn't a data question (e.g. "hello", "is this
        # working?"). Surface it as a CannotAnswerError so the UI shows the
        # friendly blue info box rather than a red error.
        raise CannotAnswerError(
            "model produced no SQL block",
            detail=(
                "Try asking about stock data — e.g. "
                "\"Which Energy stocks gained more than 5% this month?\" "
                "or \"Average daily volume for AAPL over 90 days\"."
            ),
        )
    sql = match.group(1).strip()

    if _CANNOT_ANSWER.search(sql):
        # Parse the SQL with sqlglot and extract string literals — this
        # correctly handles SQL's '' escape and any embedded apostrophes
        # (e.g. "Netflix's recent performance"), which a naïve regex
        # like '([^']+)' truncates at the first inner apostrophe.
        detail = ""
        try:
            parsed = sqlglot.parse_one(sql, dialect="postgres")
            literals = [
                lit.this for lit in parsed.find_all(exp.Literal) if lit.is_string
            ]
            # Expected literal order: ['CANNOT_ANSWER', '<detail>'].
            if len(literals) >= 2 and literals[0].upper() == "CANNOT_ANSWER":
                detail = literals[1]
        except Exception:
            # Fall back to the simple regex if sqlglot can't parse the
            # CANNOT_ANSWER stub for some reason — better a truncated
            # detail than no detail at all.
            m = re.search(
                r"'CANNOT_ANSWER'\s*AS\s*reason\s*,\s*'([^']+)'",
                sql,
                re.IGNORECASE,
            )
            detail = m.group(1) if m else ""
        raise CannotAnswerError("question is out of scope", detail=detail)

    return GeneratedQuery(
        sql=sql,
        raw_response=raw,
        input_tokens=response.input_tokens,
        output_tokens=response.output_tokens,
        cache_read_tokens=response.cache_read_tokens,
        cache_creation_tokens=response.cache_creation_tokens,
    )

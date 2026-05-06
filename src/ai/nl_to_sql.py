"""Natural-language → SQL generation for MarketAtlas."""
from __future__ import annotations

import re
from dataclasses import dataclass

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
8. If the question cannot be answered from this schema, output:
   <sql>SELECT 'CANNOT_ANSWER' AS reason, '<short reason>' AS detail;</sql>

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


def generate_sql(client: AIClient, question: str) -> GeneratedQuery:
    """
    Generate a SQL query from a natural-language question.

    Raises CannotAnswerError if the model declines.
    Raises GenerationError if the output cannot be parsed.
    """
    question = question.strip()
    if not question:
        raise GenerationError("empty question")

    response = client.complete(
        system=SYSTEM_PROMPT,
        user=question,
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
        raise GenerationError(
            f"no <sql> block in response (first 200 chars): {raw[:200]!r}"
        )
    sql = match.group(1).strip()

    if _CANNOT_ANSWER.search(sql):
        detail_match = re.search(
            r"'CANNOT_ANSWER'\s*AS\s*reason\s*,\s*'([^']+)'", sql, re.IGNORECASE
        )
        detail = detail_match.group(1) if detail_match else ""
        raise CannotAnswerError("question is out of scope", detail=detail)

    return GeneratedQuery(
        sql=sql,
        raw_response=raw,
        input_tokens=response.input_tokens,
        output_tokens=response.output_tokens,
        cache_read_tokens=response.cache_read_tokens,
        cache_creation_tokens=response.cache_creation_tokens,
    )

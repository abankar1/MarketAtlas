# Database setup

Market Atlas runs on plain PostgreSQL (14+). The hosted app uses
[Neon](https://neon.com); a local Postgres works the same way. Run the SQL
below once on an empty database, then load prices with the backfill
(`python -m src.backfill.backfill_10y`) and the daily job (`python -m src.main`).

## Tables

```sql
CREATE TABLE public.assets (
    symbol          text PRIMARY KEY,
    name            text,
    exchange_code   text,
    exchange        text,
    asset_type      text,
    price_currency  text,
    last_refreshed  date,
    updated_at      timestamptz NOT NULL DEFAULT now(),
    gics_sector     text
);

-- One row per symbol per trading day.
CREATE TABLE public.daily_bars (
    symbol        text        NOT NULL REFERENCES public.assets(symbol) ON DELETE CASCADE,
    ts            timestamptz NOT NULL,
    open          double precision,
    high          double precision,
    low           double precision,
    close         double precision,
    volume        double precision,
    adj_open      double precision,
    adj_high      double precision,
    adj_low       double precision,
    adj_close     double precision,
    adj_volume    double precision,
    split_factor  double precision,
    dividend      double precision,
    PRIMARY KEY (symbol, ts)
);
CREATE INDEX daily_bars_symbol_ts_desc ON public.daily_bars (symbol, ts DESC);
CREATE INDEX daily_bars_ts_idx         ON public.daily_bars (ts DESC);

-- Index membership. is_active / removed_date implement soft deletes;
-- the constituent sync also adds these columns if they are missing.
CREATE TABLE public.sp500_constituents (
    symbol                 text PRIMARY KEY,
    security               text,
    gics_sector            text,
    gics_sub_industry      text,
    headquarters_location  text,
    date_added             date,
    cik                    bigint,
    founded                text,
    is_active              boolean DEFAULT true,
    removed_date           date,
    updated_at             timestamptz DEFAULT now()
);

CREATE TABLE public.nasdaq100_constituents (
    symbol         text PRIMARY KEY,
    updated_at     timestamptz DEFAULT now(),
    company        text,
    icb_industry   text,
    icb_subsector  text,
    is_active      boolean DEFAULT true,
    removed_date   date
);

CREATE TABLE public.dow30_constituents (
    symbol           text PRIMARY KEY,
    exchange         text,
    updated_at       timestamptz DEFAULT now(),
    company          text,
    industry         text,
    date_added       date,
    notes            text,
    index_weighting  numeric,
    is_active        boolean DEFAULT true,
    removed_date     date
);

-- Ask AI audit log.
CREATE TABLE public.nl_queries (
    id                     bigserial PRIMARY KEY,
    ts                     timestamptz NOT NULL DEFAULT now(),
    question               text NOT NULL,
    generated_sql          text,
    status                 text NOT NULL,
    error_message          text,
    row_count              integer,
    duration_ms            integer,
    input_tokens           integer,
    output_tokens          integer,
    cache_read_tokens      integer,
    cache_creation_tokens  integer,
    path                   text,
    template_name          text,
    template_params        jsonb,
    from_cache             boolean NOT NULL DEFAULT false,
    raw_response           text,
    session_id             text
);
CREATE INDEX nl_queries_ts_idx            ON public.nl_queries (ts DESC);
CREATE INDEX nl_queries_status_idx        ON public.nl_queries (status);
CREATE INDEX nl_queries_path_idx          ON public.nl_queries (path);
CREATE INDEX nl_queries_template_name_idx ON public.nl_queries (template_name);
CREATE INDEX nl_queries_from_cache_idx    ON public.nl_queries (from_cache);
CREATE INDEX nl_queries_session_idx       ON public.nl_queries (session_id);
```

`usage_events` (session loads and tab clicks) is created automatically by the
dashboard on first use.

## Read-only role for Ask AI

LLM-generated SQL runs as a separate role that can only read:

```sql
CREATE ROLE atlas_reader LOGIN PASSWORD 'PICK-A-STRONG-PASSWORD';
GRANT CONNECT ON DATABASE neondb TO atlas_reader;   -- your database name
GRANT USAGE ON SCHEMA public TO atlas_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO atlas_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO atlas_reader;

-- Belt and braces on top of the per-query limits in src/db/readonly.py.
ALTER ROLE atlas_reader SET statement_timeout = '15s';
ALTER ROLE atlas_reader SET lock_timeout = '2s';
ALTER ROLE atlas_reader SET idle_in_transaction_session_timeout = '10s';
ALTER ROLE atlas_reader SET search_path = public;
```

Put its connection string in `db_url_readonly`.

## Query performance note

Always filter `daily_bars` on `ts` directly (`WHERE ts >= NOW() - INTERVAL '30 days'`).
Wrapping it in a function inside `WHERE`, such as `(ts AT TIME ZONE 'UTC')::date >= …`,
stops Postgres using the `ts` index and scans all ten years of bars. Casting
in the `SELECT` list is fine.

## Useful checks

```sql
-- Row counts
SELECT 'assets' AS t, count(*) FROM assets
UNION ALL SELECT 'daily_bars', count(*) FROM daily_bars
UNION ALL SELECT 'sp500_constituents', count(*) FROM sp500_constituents
UNION ALL SELECT 'nasdaq100_constituents', count(*) FROM nasdaq100_constituents
UNION ALL SELECT 'dow30_constituents', count(*) FROM dow30_constituents;

-- Latest bar date and how many symbols have it
SELECT max(ts)::date AS latest,
       count(DISTINCT symbol) FILTER (WHERE ts = (SELECT max(ts) FROM daily_bars)) AS symbols
FROM daily_bars
WHERE ts >= NOW() - INTERVAL '14 days';

-- Database size
SELECT pg_size_pretty(pg_database_size(current_database()));
```

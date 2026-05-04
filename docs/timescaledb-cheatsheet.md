TimescaleDB – Basic Commands & Usage (macOS)

Overview
--------
TimescaleDB is a PostgreSQL extension optimized for time-series data.
It runs inside PostgreSQL and is managed using PostgreSQL service commands.

This document covers:
- Market Atlas database setup (project-specific)
- Starting and stopping PostgreSQL (with TimescaleDB)
- Navigating databases and tables (psql meta-commands)
- Common inspection queries
- Data export and import
- Maintenance and troubleshooting
- Verifying TimescaleDB
- Basic usage commands
- Common operational checks


==========================================================
MARKET ATLAS — DATABASE SETUP (run once on a fresh install)
==========================================================

-- Step 1: Create the database
CREATE DATABASE market_timeseries;

-- Step 2: Connect to it and enable TimescaleDB
\c market_timeseries
CREATE EXTENSION IF NOT EXISTS timescaledb;

-- Step 3: Create tables

CREATE TABLE IF NOT EXISTS assets (
    symbol          TEXT PRIMARY KEY,
    name            TEXT,
    exchange_code   TEXT,
    exchange        TEXT,
    asset_type      TEXT,
    price_currency  TEXT,
    last_refreshed  DATE
);

CREATE TABLE IF NOT EXISTS daily_bars (
    symbol          TEXT        NOT NULL,
    ts              TIMESTAMPTZ NOT NULL,
    open            NUMERIC,
    high            NUMERIC,
    low             NUMERIC,
    close           NUMERIC,
    volume          BIGINT,
    adj_open        NUMERIC,
    adj_high        NUMERIC,
    adj_low         NUMERIC,
    adj_close       NUMERIC,
    adj_volume      BIGINT,
    split_factor    NUMERIC,
    dividend        NUMERIC,
    PRIMARY KEY (symbol, ts)
);

CREATE TABLE IF NOT EXISTS sp500_constituents (
    symbol          TEXT PRIMARY KEY,
    security        TEXT,
    gics_sector     TEXT,
    gics_sub_industry TEXT,
    hq_location     TEXT,
    date_added      DATE,
    cik             TEXT,
    founded         TEXT
);

CREATE TABLE IF NOT EXISTS nasdaq100_constituents (
    symbol          TEXT PRIMARY KEY,
    company         TEXT,
    icb_industry    TEXT,
    icb_subsector   TEXT
);

CREATE TABLE IF NOT EXISTS dow30_constituents (
    symbol          TEXT PRIMARY KEY,
    company         TEXT,
    exchange        TEXT,
    industry        TEXT,
    date_added      DATE,
    notes           TEXT,
    index_weighting NUMERIC
);

-- Step 4: Convert daily_bars to a TimescaleDB hypertable
SELECT create_hypertable('daily_bars', 'ts', if_not_exists => TRUE);

-- Step 5: Add compression (segment by symbol for best query performance)
ALTER TABLE daily_bars
SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'symbol',
    timescaledb.compress_orderby   = 'ts DESC'
);

SELECT add_compression_policy('daily_bars', INTERVAL '30 days');

-- Optional: retention policy (uncomment to auto-drop data older than 11 years)
-- SELECT add_retention_policy('daily_bars', INTERVAL '11 years');

==========================================================


1. Starting and Stopping PostgreSQL
----------------------------------

Start PostgreSQL:
brew services start postgresql@17

Stop PostgreSQL:
brew services stop postgresql@17

Restart PostgreSQL:
brew services restart postgresql@17

Check service status:
brew services list | grep postgres


2. Connecting to PostgreSQL
---------------------------

Connect to default database:
psql -d postgres

Connect to a specific database:
psql -d my_database

Check PostgreSQL version:
psql -d postgres -c "SELECT version();"


3. Navigating Databases & Tables (psql meta-commands)
-----------------------------------------------------
These commands are typed inside psql (after connecting).

List all databases:
\l

List databases with sizes:
\l+

Switch to a different database:
\c market_timeseries

List all tables in the current schema:
\dt

List tables with sizes:
\dt+

Describe a table (columns, types, constraints):
\d daily_bars

Describe with storage and extra info:
\d+ daily_bars

List schemas:
\dn

List roles / users:
\du

List indexes:
\di

List views:
\dv

List functions:
\df

Toggle expanded (vertical) output — useful for wide rows:
\x

Toggle query execution timing:
\timing

Show current connection info (user, database, port):
\conninfo

Search command history:
\s

Get help on SQL commands:
\h SELECT

Get help on psql meta-commands:
\?

Quit psql:
\q


4. Common Queries for Inspection
---------------------------------

Count rows in a table:
SELECT count(*) FROM daily_bars;

Check size of all tables in current database:
SELECT
  relname AS table_name,
  pg_size_pretty(pg_total_relation_size(relid)) AS total_size
FROM pg_catalog.pg_statio_user_tables
ORDER BY pg_total_relation_size(relid) DESC;

Check size of a specific table:
SELECT pg_size_pretty(pg_total_relation_size('daily_bars'));

Check total database size:
SELECT pg_size_pretty(pg_database_size('market_timeseries'));

List all columns of a table:
SELECT column_name, data_type, is_nullable
FROM information_schema.columns
WHERE table_name = 'daily_bars'
ORDER BY ordinal_position;

Show all indexes on a table:
SELECT indexname, indexdef
FROM pg_indexes
WHERE tablename = 'daily_bars';

Show currently running queries:
SELECT pid, state, query_start, query
FROM pg_stat_activity
WHERE state != 'idle'
ORDER BY query_start;

Show table row counts (estimate, fast — no full scan):
SELECT relname, n_live_tup AS estimated_rows
FROM pg_stat_user_tables
ORDER BY n_live_tup DESC;

Show disk usage per database:
SELECT datname, pg_size_pretty(pg_database_size(datname))
FROM pg_database
ORDER BY pg_database_size(datname) DESC;


5. Data Export & Import
------------------------

Export a table to CSV (from inside psql):
\copy daily_bars TO '/tmp/daily_bars.csv' WITH CSV HEADER

Export with a query filter:
\copy (SELECT * FROM daily_bars WHERE symbol = 'AAPL') TO '/tmp/aapl_bars.csv' WITH CSV HEADER

Import CSV into a table (from inside psql):
\copy daily_bars FROM '/tmp/daily_bars.csv' WITH CSV HEADER

Dump a single table (from shell, not psql):
pg_dump -d market_timeseries -t daily_bars -F c -f /tmp/daily_bars.dump

Dump entire database (from shell):
pg_dump -d market_timeseries -F c -f /tmp/market_timeseries.dump

Restore from dump (from shell):
pg_restore -d market_timeseries /tmp/market_timeseries.dump


6. Maintenance & Troubleshooting
---------------------------------

Reclaim space and update planner statistics:
VACUUM ANALYZE daily_bars;

Full vacuum (reclaims more space, locks table):
VACUUM FULL daily_bars;

Show the query execution plan (without running):
EXPLAIN SELECT * FROM daily_bars WHERE symbol = 'AAPL' AND ts > '2025-01-01';

Show the query plan AND actually run it (with timing):
EXPLAIN ANALYZE SELECT * FROM daily_bars WHERE symbol = 'AAPL' AND ts > '2025-01-01';

Cancel a running query (graceful):
SELECT pg_cancel_backend(<pid>);

Terminate a stuck connection (forceful):
SELECT pg_terminate_backend(<pid>);

(Get the pid from pg_stat_activity — see section 4)

Check if any tables need vacuuming:
SELECT relname, last_vacuum, last_autovacuum, n_dead_tup
FROM pg_stat_user_tables
ORDER BY n_dead_tup DESC;

Reload config without restart (after editing postgresql.conf):
SELECT pg_reload_conf();


7. Verify TimescaleDB Installation
----------------------------------

Check preload configuration:
psql -d postgres -c "SHOW shared_preload_libraries;"

Expected output:
timescaledb

List installed extensions:
psql -d postgres -c "\dx"

Check TimescaleDB version:
psql -d postgres -c "SELECT extname, extversion FROM pg_extension WHERE extname='timescaledb';"


8. Enable TimescaleDB in a Database
----------------------------------

TimescaleDB must be enabled per database.

psql -d my_database -c "CREATE EXTENSION IF NOT EXISTS timescaledb;"


9. Creating a Hypertable
-----------------------

Create a regular table:
CREATE TABLE metrics (
  ts timestamptz NOT NULL,
  value double precision,
  source text
);

Convert it to a hypertable:
SELECT create_hypertable('metrics', 'ts');


10. Inserting Data
------------------

INSERT INTO metrics (ts, value, source)
VALUES (now(), 42.5, 'sensor-1');


11. Querying Time-Series Data
-----------------------------

Last 24 hours:
SELECT *
FROM metrics
WHERE ts >= now() - interval '24 hours'
ORDER BY ts;

Hourly aggregation:
SELECT
  time_bucket('1 hour', ts) AS bucket,
  avg(value)
FROM metrics
GROUP BY bucket
ORDER BY bucket;


12. Retention Policy
--------------------

Automatically delete old data:
SELECT add_retention_policy(
  'metrics',
  INTERVAL '30 days'
);


13. Compression
---------------

Enable compression:
ALTER TABLE metrics
SET (
  timescaledb.compress,
  timescaledb.compress_segmentby = 'source'
);

Add compression policy:
SELECT add_compression_policy(
  'metrics',
  INTERVAL '7 days'
);


14. Operational Commands
------------------------

Check data directory:
psql -d postgres -c "SHOW data_directory;"

Check config file in use:
psql -d postgres -c "SHOW config_file;"

List hypertables:
SELECT * FROM timescaledb_information.hypertables;

List chunks:
SELECT * FROM timescaledb_information.chunks;


Summary
-------
- TimescaleDB runs inside PostgreSQL
- Service control is done via PostgreSQL
- Hypertables are the core abstraction
- Standard SQL applies, optimized for time-series
- Use \l, \dt, \d to navigate databases and tables
- Use \copy for quick CSV import/export
- Use EXPLAIN ANALYZE to debug slow queries
- Use pg_stat_activity to find and kill stuck queries

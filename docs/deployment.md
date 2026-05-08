# Deployment runbook

Hosting setup: **Streamlit Community Cloud** (web UI) + **Timescale Cloud**
(Postgres) + **GitHub Actions** (daily incremental updates). All on free
tiers; expect $0–25/month depending on DB row count.

This runbook covers the one-time steps to go from a local dev setup to a
public HTTPS URL serving the dashboard. Follow the phases in order.

---

## Phase 1 — Timescale Cloud setup

1. Sign up at <https://console.cloud.timescale.com/>. The free tier (no
   credit card) gives a 30-day trial; after that the smallest paid tier is
   $25/month. The DB is large enough for 20+ years of S&P 500 / NASDAQ-100 /
   Dow 30 daily bars.

2. Create a new service:
   - Name: `market-atlas`
   - Cloud / region: pick whichever is closest to where you and your viewers
     are (latency only — the daily job is async).
   - Postgres major version: 16+ recommended.
   - Enable **TimescaleDB extension** (default).

3. Copy the **connection string** for the default `tsdbadmin` user. Keep it
   somewhere safe — you'll paste it into both Streamlit Cloud and GitHub
   Secrets later. It looks like:
   ```
   postgresql://tsdbadmin:PASSWORD@HOST.tsdb.cloud.timescale.com:PORT/tsdb?sslmode=require
   ```

4. Open the **SQL editor** in the Timescale console (or `psql`). Create a
   read-only role for the Ask AI tab:
   ```sql
   CREATE ROLE atlas_reader LOGIN PASSWORD 'PICK-A-STRONG-PASSWORD';
   GRANT CONNECT ON DATABASE tsdb TO atlas_reader;
   GRANT USAGE ON SCHEMA public TO atlas_reader;
   GRANT SELECT ON ALL TABLES IN SCHEMA public TO atlas_reader;
   ALTER DEFAULT PRIVILEGES IN SCHEMA public
     GRANT SELECT ON TABLES TO atlas_reader;
   ```
   The Ask AI tab uses this role's credentials so LLM-generated SQL cannot
   write or drop anything.

5. Build the readonly connection string by swapping in `atlas_reader` and
   its password. Keep both strings handy.

---

## Phase 2 — Migrate data from local Postgres → Timescale Cloud

Use `pg_dump` from your local machine and restore directly into the cloud
service. Timescale provides `timescaledb_pre_restore()` /
`timescaledb_post_restore()` helpers that make this painless.

> **Time estimate:** 2–10 minutes for a typical 10-year backfill of ~700
> symbols. Run from a machine with a stable connection.

```bash
# 1. Dump local DB. Use --no-owner / --no-privileges so role grants from
#    your local DB don't conflict with the cloud service.
pg_dump \
  --format=plain \
  --no-owner \
  --no-privileges \
  --no-tablespaces \
  --quote-all-identifiers \
  --file=market_atlas.sql \
  "postgresql://localhost:5432/your_local_db_name"

# 2. On the cloud DB, prepare TimescaleDB for a restore. This temporarily
#    disables background workers + triggers that would otherwise interfere.
psql "$CLOUD_DB_URL" -c "SELECT timescaledb_pre_restore();"

# 3. Restore.
psql "$CLOUD_DB_URL" -f market_atlas.sql

# 4. Re-enable Timescale background work.
psql "$CLOUD_DB_URL" -c "SELECT timescaledb_post_restore();"
```

Verify the restore:
```bash
psql "$CLOUD_DB_URL" <<'SQL'
SELECT 'assets' AS table, COUNT(*) FROM assets
UNION ALL SELECT 'daily_bars', COUNT(*) FROM daily_bars
UNION ALL SELECT 'sp500_constituents', COUNT(*) FROM sp500_constituents
UNION ALL SELECT 'nasdaq100_constituents', COUNT(*) FROM nasdaq100_constituents
UNION ALL SELECT 'dow30_constituents', COUNT(*) FROM dow30_constituents;

-- Confirm daily_bars is still a hypertable on the cloud side.
SELECT hypertable_name FROM timescaledb_information.hypertables;
SQL
```

If anything looks wrong, you can wipe and redo: drop the schema in the
Timescale console and re-run Phase 2.

---

## Phase 3 — GitHub setup

1. Push the merged `main` to GitHub if you haven't already:
   ```bash
   git checkout main
   git merge GenAILayer
   git push origin main
   ```

2. In the GitHub repo (`abankar1/MarketAtlas`) → **Settings → Secrets and
   variables → Actions → New repository secret**, add:

   | Secret name          | Value                                                    |
   | -------------------- | -------------------------------------------------------- |
   | `DB_URL`             | Read-write Timescale Cloud connection string             |
   | `MARKETDATA_TOKEN`   | Marketstack API key                                      |
   | `DAYS`               | `1000` (or your preferred lookback window)               |
   | `API_SLEEP_SECONDS`  | `0.2`                                                    |
   | `ANTHROPIC_API_KEY`  | Anthropic API key (only for the daily sector-classifier) |
   | `ANTHROPIC_MODEL`    | e.g. `claude-haiku-4-5` (optional, has a default)        |
   | `MARKETAUX_TOKEN`    | Marketaux API key (not used by the daily job, but kept here so the same secrets cover both) |

   `DB_URL_READONLY` is **not** needed by GitHub Actions — only the
   dashboard reads from it.

3. Confirm the workflow runs. Push the change, then go to **Actions →
   Daily market data update → Run workflow**. If the run is green, the
   schedule will take over from there (`30 22 * * 1-5`).

---

## Phase 4 — Streamlit Community Cloud deployment

1. Sign in at <https://share.streamlit.io/> with your GitHub account and
   authorize access to `abankar1/MarketAtlas`.

2. **Create app**:
   - Repository: `abankar1/MarketAtlas`
   - Branch: `main`
   - Main file path: `src/dashboard/app.py`
   - App URL: pick a subdomain like `market-atlas.streamlit.app`
   - Python version: **3.12** (matches the GHA workflow)
   - Click **Advanced settings → Secrets** and paste the contents of
     `.streamlit/secrets.toml.example` with your real values. Required
     keys at minimum:
     ```toml
     db_url = "postgresql://tsdbadmin:...@HOST.tsdb.cloud.timescale.com:PORT/tsdb?sslmode=require"
     marketdata_token = "..."
     anthropic_api_key = "sk-ant-..."
     db_url_readonly = "postgresql://atlas_reader:...@HOST..."
     marketaux_token = "..."
     ```

3. Click **Deploy**. First boot installs `requirements.txt` (~2 min). The
   app should come up at your chosen URL.

4. **Smoke test:**
   - Heatmap tab loads with treemap + Top 5 movers.
   - Stock Detail tab loads OHLCV chart for any symbol.
   - News tab loads headlines (proves Marketaux key works).
   - Ask AI tab answers a basic question like "top gainer last week"
     (proves Anthropic key + readonly DB work).

---

## Phase 5 — Day-2 operations

- **Updating the deployed app** — every push to `main` triggers a Streamlit
  Cloud rebuild automatically. Monitor the build log via the app's "Manage
  app" view.
- **Rotating secrets** — Timescale rotates passwords from the console;
  update the secret in *both* GitHub Actions and Streamlit Cloud after
  each rotation.
- **Watching the daily job** — GitHub → Actions → "Daily market data
  update" shows green/red runs on a calendar. Failed runs email you by
  default.
- **Cost watch** — Timescale Cloud monthly invoice + Anthropic usage are
  the only paid services. Marketstack and Marketaux are on free tiers
  (rate-limited). Monitor `daily_bars` row count vs. your Timescale plan
  limit.

---

## Troubleshooting

**Dashboard boots but every query says "no data"**
The `db_url` secret points at an empty DB. Re-run Phase 2 (data migration).

**"Missing required config key: db_url" on Streamlit Cloud**
The secrets TOML wasn't parsed. Check the formatting — keys must be at the
top level OR inside a `[market_atlas]` table.

**GitHub Actions run fails on `import anthropic`**
`ANTHROPIC_API_KEY` is set, so the daily job tries to import the SDK.
Confirm `anthropic>=0.40.0` is in `requirements.txt`.

**Ask AI tab returns "permission denied for table assets"**
The readonly role doesn't have SELECT. Re-run the GRANT statements in
Phase 1 — note that GRANTs only apply to *existing* tables; the `ALTER
DEFAULT PRIVILEGES` line ensures future tables work too.

**Streamlit Cloud rebuilds take >5 min**
Add `psycopg[binary]` (already in `requirements.txt`) to avoid compiling
psycopg from source on every rebuild. If still slow, pin specific versions
to leverage the wheel cache.

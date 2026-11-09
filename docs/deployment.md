# Deployment runbook

Hosting setup: **Streamlit Community Cloud** (web UI) + **Neon** (Postgres)
+ **GitHub Actions** (daily price updates). Streamlit Cloud and Neon are
free; the paid pieces are the Marketstack Basic plan and Anthropic API usage.

Follow the phases in order for a fresh deployment.

---

## Phase 1 — Neon database

1. Sign up at <https://neon.com> and create a project on the **Free** plan
   (1 GB storage per project, 100 compute-hours a month). The full 10-year
   dataset is about 365 MB.

2. Copy the **direct** connection string (host *without* `-pooler`). The
   pooled endpoint rejects the `statement_timeout` startup option the Ask AI
   tab sets, so the app must use the direct one. It looks like:
   ```
   postgresql://neondb_owner:PASSWORD@ep-xxxx.REGION.aws.neon.tech/neondb?sslmode=require&channel_binding=require
   ```

3. Open the Neon **SQL Editor** (or `psql`) and run the table and
   `atlas_reader` SQL from [database-setup.md](database-setup.md).

4. Build the read-only connection string by swapping in `atlas_reader` and
   its password. Keep both strings handy.

The compute scales to zero after 5 idle minutes, so the first page load
after a quiet spell takes a moment longer.

---

## Phase 2 — Load data

Either start fresh or copy an existing database.

**Fresh:** point `src/config/configuration.json` at the new `db_url`, then:
```bash
python -m src.sync_constituents      # index membership from Wikipedia
python -m src.load_sectors           # GICS sectors from data/gics_sectors.json
python -m src.backfill.backfill_10y  # historical bars (one-time, ~1 request/symbol)
```

**Copy from another Postgres** (tables already created in Phase 1):
```bash
pg_dump --data-only --no-owner --no-privileges \
  --table='public.*' "$OLD_DB_URL" | psql "$NEW_DB_URL"
```

Verify with the row-count query in [database-setup.md](database-setup.md).

---

## Phase 3 — GitHub Actions

In the GitHub repo (`abankar1/MarketAtlas`) → **Settings → Secrets and
variables → Actions**, add:

| Secret name         | Value                                                     |
| ------------------- | --------------------------------------------------------- |
| `DB_URL`            | Read-write (direct) Neon connection string                |
| `MARKETDATA_TOKEN`  | Marketstack API key                                       |
| `ANTHROPIC_API_KEY` | Anthropic API key (sector classifier for new symbols)     |
| `MARKETAUX_TOKEN`   | Marketaux API key (optional; kept so one set covers both) |

`DB_URL_READONLY` is not needed here — only the dashboard uses it.

`.github/workflows/daily-update.yml` runs from `main` Tuesday–Saturday at
09:00 UTC, picking up each weekday's close. Symbols are fetched 100 per
Marketstack request, so a run costs about 6 requests (~130 a month). That is
over the Marketstack free plan's 100, so use the **Basic** plan (10,000 a month).

To test it: **Actions → Daily market data update → Run workflow**.

---

## Phase 4 — Streamlit Community Cloud

1. Sign in at <https://share.streamlit.io/> with GitHub and create an app:
   - Repository: `abankar1/MarketAtlas`
   - Branch: `release`
   - Main file path: `src/dashboard/app.py`
   - Python version: **3.12** (matches the workflow)

2. **Advanced settings → Secrets**: paste `.streamlit/secrets.toml.example`
   with real values. At minimum:
   ```toml
   db_url = "postgresql://neondb_owner:...@ep-xxxx.REGION.aws.neon.tech/neondb?sslmode=require"
   db_url_readonly = "postgresql://atlas_reader:...@ep-xxxx.REGION.aws.neon.tech/neondb?sslmode=require"
   marketdata_token = "..."
   anthropic_api_key = "sk-ant-..."
   marketaux_token = "..."
   ```
   Keys go at the top level (or all under one `[market_atlas]` table), with
   straight quotes.

3. **Deploy**, then smoke test:
   - Heatmap loads with the Top 5 movers and the "Prices as of …" line.
   - Stock Detail draws a chart; News shows headlines.
   - Ask AI answers "top gainer last week".

---

## Phase 5 — Day-2 operations

- **Releasing** — work lands on `main` through PRs; the app deploys from
  `release`, so merge `main` → `release` to ship. Streamlit Cloud rebuilds
  on every push to `release`.
- **Rotating secrets** — reset passwords in the Neon console (Roles), then
  update both GitHub Actions and Streamlit Cloud.
- **Watching the daily job** — GitHub → Actions → "Daily market data update".
  Each run logs its Marketstack request count and how many symbols are
  current. A run fails only if the batched update fails.
- **Cost watch** — Neon usage page (storage and compute-hours), the
  Marketstack dashboard (requests), and Anthropic usage.

---

## Troubleshooting

**"Missing database configuration" on Streamlit Cloud**
The secrets TOML didn't parse, so no `db_url` was found. Look for curly
quotes, duplicate keys, or keys accidentally placed under a section header.

**"unsupported startup parameter in options: statement_timeout"**
`db_url_readonly` points at the pooled (`-pooler`) host. Use the direct one.

**Dashboard boots but every query says "no data"**
`db_url` points at an empty database. Re-run Phase 2.

**Ask AI returns "permission denied for table …"**
`atlas_reader` is missing SELECT. Re-run the GRANT statements from
[database-setup.md](database-setup.md); the `ALTER DEFAULT PRIVILEGES` line
covers tables created later.

**A new index member has no prices**
Marketstack sometimes lists a renamed or newly listed ticker a few days late
(it returns 422). The daily job retries it every run and picks it up once
Marketstack has it.

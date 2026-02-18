from src.config.settings import load_settings
from src.db.connection import connect
from src.db.repositories import fetch_sp500_symbols
from src.db.repositories import fetch_dow30_symbols
from src.db.repositories import fetch_nasdaq100_symbols
from src.marketdata.client import MarketDataClient
from src.services.daily_bar_importer import DailyBarImporter


def main() -> None:
    settings = load_settings()

    with connect(settings.db_url) as conn:
        sp = fetch_sp500_symbols(conn)
        nas = fetch_nasdaq100_symbols(conn)
        dow = fetch_dow30_symbols(conn)

        # dedupe overlaps and keep stable order for logging
        symbols = sorted(set(sp) | set(nas) | set(dow))

        client = MarketDataClient(
            token=settings.marketdata_token, sleep_s=settings.api_sleep_seconds
        )
        importer = DailyBarImporter(conn=conn, client=client)

        success = 0
        failed: list[str] = []

        for i, sym in enumerate(symbols, start=1):
            try:
                n = importer.import_symbol(sym, days=settings.days)
                print(f"[{i}/{len(symbols)}] {sym}: upserted {n} rows")
                success += 1
            except Exception as e:
                print(f"[{i}/{len(symbols)}] {sym}: ERROR {e}")
                failed.append(sym)

        print(f"Done. Success: {success}, Failed: {len(failed)}")

        # Summary: how many symbols are current through today
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH latest AS (
                    SELECT symbol, max(ts)::date AS last_day
                    FROM public.daily_bars
                    GROUP BY symbol
                )
                SELECT
                    count(*) FILTER (WHERE last_day = current_date) AS up_to_today,
                    count(*) AS total_symbols
                FROM latest
                """
            )
            up_to_today, total_symbols = cur.fetchone()

        print(
            f"Summary: {up_to_today}/{total_symbols} symbols "
            f"have data through today ({up_to_today * 100 // max(total_symbols, 1)}%)"
        )
        if failed:
            print("Failed:", ", ".join(failed))


if __name__ == "__main__":
    main()

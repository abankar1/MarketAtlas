from src.config.settings import load_settings
from src.db.connection import connect
from src.services.nasdaq_loader import NasdaqConstituentsLoader


def main() -> None:
    settings = load_settings()

    # Update this path to wherever your NASDAQ file lives inside the repo.
    # Example: data/nasdaq100.tsv
    input_path = "data/nasdaq100.tsv"

    with connect(settings.db_url) as conn:
        loader = NasdaqConstituentsLoader(conn)
        n = loader.load_from_tsv(input_path)

    print(f"Done. Upserted {n} NASDAQ-100 constituents from {input_path}")


if __name__ == "__main__":
    main()

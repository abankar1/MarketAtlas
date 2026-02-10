from src.config.settings import load_settings
from src.db.connection import connect
from src.services.dow_loader import DowConstituentsLoader


def main() -> None:
    settings = load_settings()
    input_path = "data/dow30.tsv"

    with connect(settings.db_url) as conn:
        loader = DowConstituentsLoader(conn)
        n = loader.load_from_tsv(input_path)

    print(f"Done. Upserted {n} Dow 30 constituents.")


if __name__ == "__main__":
    main()

import psycopg


def connect(db_url: str) -> psycopg.Connection:
    return psycopg.connect(db_url)

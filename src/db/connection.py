"""
Database connection factory.

Usage:
    from src.db.connection import connect
    from src.config.settings import load_settings

    settings = load_settings()
    with connect(settings.db_url) as conn:
        ...
"""
import psycopg


def connect(db_url: str) -> psycopg.Connection:
    return psycopg.connect(db_url)

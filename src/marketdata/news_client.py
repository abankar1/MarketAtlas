"""
Marketaux API client — fetches per-symbol news headlines with sentiment scores.

Free tier: 100 requests/day.
Configure via src/config/configuration.json as `marketaux_token`.

Usage:
    from src.marketdata.news_client import NewsClient, NewsClientError

    client = NewsClient(token="your_marketaux_key")
    articles = client.fetch_news("AAPL", limit=10)
"""
from __future__ import annotations

import datetime as dt
from typing import Any

import requests


_API_URL = "https://api.marketaux.com/v1/news/all"


class NewsClientError(RuntimeError):
    """Raised when the Marketaux API call fails."""


def _parse_published_at(s: str | None) -> dt.datetime | None:
    """Parse Marketaux ISO timestamps (e.g. '2026-04-27T13:21:00.000000Z')."""
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _aggregate_sentiment(entities: list[dict[str, Any]], symbol: str) -> float | None:
    """
    Average sentiment_score across entities matching the requested symbol.
    Returns None if no entity has a numeric score.
    """
    sym = symbol.upper()
    scores: list[float] = []
    for e in entities or []:
        if str(e.get("symbol", "")).upper() != sym:
            continue
        raw = e.get("sentiment_score")
        if isinstance(raw, (int, float)):
            scores.append(float(raw))
    if scores:
        return sum(scores) / len(scores)
    # Fallback: any entity with a score
    fallback = [
        float(e["sentiment_score"])
        for e in (entities or [])
        if isinstance(e.get("sentiment_score"), (int, float))
    ]
    return sum(fallback) / len(fallback) if fallback else None


def _normalise_limit(limit: int) -> int:
    return min(max(int(limit), 1), 50)


def build_request_url(symbol: str, limit: int = 10) -> str:
    """
    Return the canonical Marketaux request URL (excluding the secret api_token)
    used as the cache key in get_news_cached. Two calls with the same symbol +
    limit produce the same URL string.
    """
    n = _normalise_limit(limit)
    return (
        f"{_API_URL}?symbols={symbol}"
        f"&language=en&filter_entities=true&limit={n}"
    )


class NewsClient:
    def __init__(self, token: str, timeout_s: int = 30):
        self._token = token
        self._timeout_s = timeout_s

    def fetch_news(self, symbol: str, limit: int = 10) -> list[dict[str, Any]]:
        """
        Fetch up to `limit` recent English-language articles mentioning `symbol`.

        Returns a list of normalised dicts:
            {title, description, url, source, published_at: datetime|None,
             sentiment: float|None}
        """
        params = {
            "api_token": self._token,
            "symbols": symbol,
            "language": "en",
            "filter_entities": "true",
            "limit": _normalise_limit(limit),
        }
        try:
            resp = requests.get(_API_URL, params=params, timeout=self._timeout_s)
        except requests.RequestException as e:
            raise NewsClientError(f"Network error fetching news: {e}") from e

        if resp.status_code != 200:
            try:
                err = resp.json().get("error", {}).get("message")
            except Exception:
                err = None
            msg = err or f"HTTP {resp.status_code}"
            raise NewsClientError(f"Marketaux error: {msg}")

        try:
            payload = resp.json()
        except ValueError as e:
            raise NewsClientError(f"Invalid JSON from Marketaux: {e}") from e

        data = payload.get("data") or []
        articles: list[dict[str, Any]] = []
        for r in data:
            articles.append(
                {
                    "title":        r.get("title") or "",
                    "description":  r.get("description") or "",
                    "url":          r.get("url") or "",
                    "source":       r.get("source") or "",
                    "published_at": _parse_published_at(r.get("published_at")),
                    "sentiment":    _aggregate_sentiment(r.get("entities") or [], symbol),
                }
            )
        return articles

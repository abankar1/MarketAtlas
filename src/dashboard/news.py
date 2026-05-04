"""
News tab — per-symbol headlines from Marketaux.

render_news_tab(df, marketaux_token)
    Ticker selectbox (shared with Stock Detail via detail_selected_ticker)
    + compact list of headlines with title link, source, relative time,
    and a sentiment badge driven by Marketaux's per-entity sentiment_score.
"""
from __future__ import annotations

import datetime as dt
import html

import pandas as pd
import streamlit as st

from src.dashboard.data import get_news_cached
from src.marketdata.news_client import NewsClientError


_NEUTRAL_THRESHOLD = 0.15

_PILL_STYLE = (
    "display:inline-block;padding:1px 8px;border-radius:10px;"
    "font-size:0.72em;font-weight:600;letter-spacing:0.03em;"
    "color:white;margin-left:8px;vertical-align:middle;"
)
# Use semi-transparent gray so cards look right in both light and dark themes.
_META_STYLE = (
    "color:rgba(128,128,128,0.95);font-size:0.82em;margin-top:6px;"
)
_DESC_STYLE = (
    "color:rgba(128,128,128,1);font-size:0.88em;margin-top:4px;"
    "line-height:1.4;"
)
_CARD_STYLE = (
    "padding:10px 12px;margin-bottom:8px;border-radius:8px;"
    "background:rgba(128,128,128,0.08);"
    "border-left:3px solid rgba(128,128,128,0.25);"
)
# Inherit text color from the active Streamlit theme — looks right on both light and dark.
_TITLE_STYLE = (
    "color:inherit;font-size:1.0em;font-weight:600;"
    "text-decoration:none;line-height:1.35;"
)

_DESC_MAX_CHARS = 240


def _relative_time(ts: dt.datetime | None) -> str:
    """Return '3h ago', '12m ago', '2d ago' for a timezone-aware datetime."""
    if ts is None:
        return ""
    now = dt.datetime.now(dt.timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=dt.timezone.utc)
    delta = now - ts
    secs = int(delta.total_seconds())
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    if secs < 86400 * 30:
        return f"{secs // 86400}d ago"
    return ts.strftime("%b %d, %Y")


def _sentiment_pill(score: float | None) -> str:
    if score is None:
        return ""
    if score >= _NEUTRAL_THRESHOLD:
        label, color = "Positive", "#26a113"
    elif score <= -_NEUTRAL_THRESHOLD:
        label, color = "Negative", "#a11318"
    else:
        label, color = "Neutral", "#5a6268"
    return f'<span style="{_PILL_STYLE}background:{color}">{label}</span>'


def _truncate(text: str, n: int) -> str:
    text = text.strip()
    if len(text) <= n:
        return text
    cut = text[:n].rsplit(" ", 1)[0].rstrip(",;:.")
    return cut + "…"


def _render_card(article: dict) -> None:
    title  = html.escape(article.get("title") or "(untitled)")
    url    = article.get("url") or ""
    source = html.escape(article.get("source") or "Unknown")
    when   = _relative_time(article.get("published_at"))
    pill   = _sentiment_pill(article.get("sentiment"))
    desc   = (article.get("description") or "").strip()

    title_html = (
        f'<a href="{html.escape(url)}" target="_blank" rel="noopener" style="{_TITLE_STYLE}">{title}</a>'
        if url
        else f'<span style="{_TITLE_STYLE}">{title}</span>'
    )
    desc_html = (
        f'<div style="{_DESC_STYLE}">{html.escape(_truncate(desc, _DESC_MAX_CHARS))}</div>'
        if desc
        else ""
    )
    meta_bits = [source]
    if when:
        meta_bits.append(when)
    meta = " · ".join(meta_bits)

    st.markdown(
        f'<div style="{_CARD_STYLE}">'
        f"{title_html}{pill}"
        f"{desc_html}"
        f'<div style="{_META_STYLE}">{meta}</div>'
        f"</div>",
        unsafe_allow_html=True,
    )


def render_news_tab(df: pd.DataFrame, marketaux_token: str) -> None:
    """
    Render the News tab: ticker selector + headline list.

    Token is read from the loaded config (marketaux_token). When unset, an inline
    notice is shown — the rest of the app continues to work.
    """
    symbols = sorted(df["symbol"].dropna().unique().tolist())
    if not symbols:
        st.info("No symbols available in the current universe.")
        return

    stored = st.session_state.get("detail_selected_ticker")
    default = stored if stored in symbols else ("AAPL" if "AAPL" in symbols else symbols[0])
    default_idx = symbols.index(default)

    selected_symbol = st.selectbox(
        "Symbol",
        symbols,
        index=default_idx,
        help="Pick a ticker to see recent headlines. Selection is shared with Stock Detail.",
    )
    st.session_state["detail_selected_ticker"] = selected_symbol

    if not marketaux_token:
        st.info(
            "Set `marketaux_token` in `src/config/configuration.json` to enable news. "
            "Free tier (100 requests/day) is available at https://www.marketaux.com."
        )
        return

    with st.spinner(f"Loading headlines for {selected_symbol}…"):
        try:
            articles = get_news_cached(marketaux_token, selected_symbol, limit=10)
        except NewsClientError as err:
            st.warning(str(err))
            return

    if not articles:
        st.caption(f"No recent headlines for {selected_symbol}.")
        return

    st.caption(f"{len(articles)} recent headlines · sentiment scored by Marketaux")
    for a in articles:
        _render_card(a)

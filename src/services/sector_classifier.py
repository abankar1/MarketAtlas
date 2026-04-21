"""
Reusable GICS sector classification utilities.

Extracted from scripts/migrate_add_sector.py so both the migration script
and the constituent sync service can share the same logic.
"""
from __future__ import annotations

import json

import psycopg


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GICS_SECTORS = [
    "Communication Services",
    "Consumer Discretionary",
    "Consumer Staples",
    "Energy",
    "Financials",
    "Health Care",
    "Industrials",
    "Information Technology",
    "Materials",
    "Real Estate",
    "Utilities",
]

ICB_TO_GICS: dict[str, str] = {
    "Technology":             "Information Technology",
    "Health Care":            "Health Care",
    "Consumer Discretionary": "Consumer Discretionary",
    "Consumer Staples":       "Consumer Staples",
    "Industrials":            "Industrials",
    "Telecommunications":     "Communication Services",
    "Energy":                 "Energy",
    "Utilities":              "Utilities",
    "Basic Materials":        "Materials",
    "Real Estate":            "Real Estate",
    "Financials":             "Financials",
}


# ---------------------------------------------------------------------------
# Claude API classification
# ---------------------------------------------------------------------------

def classify_via_api(
    symbols: list[tuple[str, str]],
    api_key: str,
) -> dict[str, str]:
    """
    Use the Claude API to classify a batch of (symbol, name) pairs into
    GICS sectors.  Returns ``{symbol: sector}`` for successfully classified
    symbols.

    Raises ``ImportError`` if the ``anthropic`` package is not installed.
    """
    try:
        import anthropic
    except ImportError:
        raise ImportError(
            "anthropic package not installed. Run: pip install anthropic"
        )

    client = anthropic.Anthropic(api_key=api_key)

    symbol_list = "\n".join(f"- {sym}: {name}" for sym, name in symbols)
    prompt = (
        f"Classify each stock into exactly one GICS sector.\n"
        f"The valid GICS sectors are: {', '.join(GICS_SECTORS)}\n\n"
        f"Stocks to classify:\n{symbol_list}\n\n"
        f"Respond with ONLY a JSON object mapping symbol to sector, like:\n"
        f'{{"AAPL": "Information Technology", "JNJ": "Health Care"}}'
    )

    message = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )

    response_text = message.content[0].text.strip()

    # Handle markdown code blocks in response
    if "```" in response_text:
        response_text = response_text.split("```")[1]
        if response_text.startswith("json"):
            response_text = response_text[4:]
        response_text = response_text.strip()

    raw: dict[str, str] = json.loads(response_text)

    # Validate — keep only known GICS sectors
    result: dict[str, str] = {}
    for sym, sector in raw.items():
        if sector in GICS_SECTORS:
            result[sym] = sector
        else:
            print(f"  WARNING: AI returned unknown sector '{sector}' for {sym} — skipping")

    return result


# ---------------------------------------------------------------------------
# High-level helper
# ---------------------------------------------------------------------------

def ensure_sectors(
    conn: psycopg.Connection,
    new_symbols: list[tuple[str, str]],
    api_key: str | None,
) -> int:
    """
    Ensure every symbol in *new_symbols* has a ``gics_sector`` in the
    ``assets`` table.

    1. Skip symbols that already have a sector.
    2. If *api_key* is provided, batch-classify the rest via Claude API.
    3. Otherwise print a warning and leave them as NULL (dashboard shows
       'Unknown').

    Returns the number of symbols that were classified.
    """
    if not new_symbols:
        return 0

    sym_list = [s for s, _ in new_symbols]
    placeholders = ",".join(["%s"] * len(sym_list))

    with conn.cursor() as cur:
        cur.execute(
            f"SELECT symbol FROM public.assets "
            f"WHERE symbol IN ({placeholders}) AND gics_sector IS NULL;",
            sym_list,
        )
        missing = {r[0] for r in cur.fetchall()}

    if not missing:
        return 0

    # Build the (symbol, name) pairs for only the missing ones
    to_classify = [(s, n) for s, n in new_symbols if s in missing]

    if not api_key:
        print(
            f"  WARNING: {len(to_classify)} new symbol(s) need sector classification "
            f"but no anthropic_api_key configured: {[s for s, _ in to_classify]}"
        )
        return 0

    print(f"  Classifying {len(to_classify)} new symbol(s) via Claude API...")

    try:
        classifications = classify_via_api(to_classify, api_key)
    except ImportError as e:
        print(f"  WARNING: {e}")
        return 0
    except Exception as e:
        print(f"  ERROR during AI classification: {e}")
        return 0

    count = 0
    for sym, sector in classifications.items():
        conn.execute(
            "UPDATE public.assets SET gics_sector = %s WHERE symbol = %s AND gics_sector IS NULL;",
            (sector, sym),
        )
        print(f"    {sym} -> {sector}")
        count += 1

    conn.commit()
    return count

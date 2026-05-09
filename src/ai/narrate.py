"""
One-line conversational summary of a SQL result.

After the SQL runs and we have rows, we send (question, columns, rows) to
a short Haiku call and ask for a 1-2 sentence answer in stock context. The
SQL-generation LLM stays focused on SQL; this layer turns numbers into the
kind of reply a human would write.

Cached on (question, columns, rows-hash) — re-asking the same question
against the same underlying data costs nothing.
"""
from __future__ import annotations

import hashlib
import json
import re

from .cache import TTLCache, PROMPT_VERSION
from .client import AIClient, AIClientError


_NARRATE_CACHE = TTLCache()


SYSTEM_PROMPT = """\
You are a financial-data narrator for a stock-market dashboard.

Given (1) the user's original question and (2) rows returned by a SQL query,
write a SHORT one-liner that directly answers what the user asked.

Style:
- 1-2 sentences, ~30 words max (excluding any optional tab pointer below).
- Use the actual numbers from the rows (price as $X.XX, % with one decimal
  like +12.6%, volume in M/B).
- Match the tone of the question. "Looks like X is dying" → respond
  directly ("Actually holding up — TSLA up 4.2% over 30d on 92M avg vol")
  rather than a neutral readout.
- Refer to stocks by ticker symbol.
- Never invent data not in the rows. Never speculate beyond the numbers.
- No disclaimers, no "not financial advice", no addressing the user.
- For aggregate / multi-row results, give the headline finding (top mover,
  count, range) instead of listing rows.

OPTIONAL TAB POINTER:
The Market Atlas web app has these other tabs:
  • Heatmap — full-market treemap with daily moves, top/bottom 5 banner.
  • Sector Synopsis — every GICS sector's gainers, losers, breadth.
  • Stock Detail — full OHLCV history + chart for a single ticker.
  • News — recent headlines + sentiment for a ticker.

If — and only if — one of those tabs would show MEANINGFULLY MORE than
the answer already gives, append ONE short sentence pointing the user
there. Use the format: " See the **Stock Detail** tab for the full chart."
— always include the literal word "tab" after the bold name, so the user
knows it's a tab in this app. One tab name in **bold**, one trailing
sentence. Never recommend "Ask AI" itself. Never append more than one
pointer. Skip the pointer entirely if the answer is already complete
(e.g. a single-aggregate question whose exact answer is in the row).

Output ONLY the one-liner (and at most one optional tab pointer). No
preamble, no markdown headers, no quotes wrapping it.
"""


def _coerce(v):
    """psycopg Decimal/date/datetime → JSON-friendly types."""
    if hasattr(v, "isoformat"):
        return v.isoformat()
    if hasattr(v, "__float__") and not isinstance(v, bool):
        return float(v)
    return v


def _serialise(columns: list[str], rows: list[tuple]) -> str:
    """Pack rows into compact JSON. Cap large results to keep prompt small."""
    cols = list(columns)
    total = len(rows)
    if total > 25:
        sample = list(rows[:20]) + list(rows[-5:])
        payload = {
            "columns": cols,
            "row_count": total,
            "rows_shown": len(sample),
            "rows": [{c: _coerce(v) for c, v in zip(cols, r)} for r in sample],
        }
    else:
        payload = {
            "columns": cols,
            "row_count": total,
            "rows": [{c: _coerce(v) for c, v in zip(cols, r)} for r in rows],
        }
    return json.dumps(payload, default=str)


def _key(
    question: str,
    columns: list[str],
    rows: list[tuple],
    *,
    last_ticker: str | None = None,
) -> str:
    h = hashlib.sha1()
    # PROMPT_VERSION invalidates stale cached narratives whenever the
    # narrate system prompt changes (e.g. tab list or formatting rules).
    h.update(PROMPT_VERSION.encode())
    h.update(b"|")
    h.update(question.strip().lower().encode())
    h.update(b"|")
    h.update((last_ticker or "").upper().encode())
    h.update(b"|")
    h.update(",".join(columns).encode())
    h.update(b"|")
    # Order matters for "top N" results, so don't sort. Cap to avoid hashing
    # gigantic tables — first 50 rows are enough to discriminate.
    for row in rows[:50]:
        h.update(repr(row).encode())
    return h.hexdigest()


def summarize(
    client: AIClient,
    question: str,
    columns: list,
    rows: list,
    *,
    last_ticker: str | None = None,
) -> tuple[str, dict] | None:
    """
    Return (narrative, token_usage_dict) or None on failure.

    `last_ticker` is the conversational anchor from the prior query — when
    present, the narrator can resolve referential pronouns ("it", "that
    stock") even if the SQL result columns don't include the symbol.

    Never raises — narration is best-effort. The dataframe still renders
    if this call fails, and the user just doesn't get the one-liner.
    """
    cols = list(columns)
    row_list = list(rows)

    if not row_list:
        return ("No rows matched.", {"input": 0, "output": 0, "cached": True})

    key = _key(question, cols, row_list, last_ticker=last_ticker)
    cached = _NARRATE_CACHE.get(key)
    if cached is not None:
        return (cached, {"input": 0, "output": 0, "cached": True})

    context_line = (
        f"Previously discussed ticker: {last_ticker}\n"
        if last_ticker else ""
    )
    user_payload = (
        f"{context_line}Question: {question}\n\n"
        f"Result data:\n{_serialise(cols, row_list)}"
    )

    try:
        resp = client.complete(
            system=SYSTEM_PROMPT,
            user=user_payload,
            max_tokens=120,
            temperature=0.2,
            cacheable_system=True,
        )
    except AIClientError:
        return None

    text = resp.text.strip()
    text = re.sub(r'^[\'"]\s*|\s*[\'"]$', "", text).strip()
    if not text:
        return None

    _NARRATE_CACHE.set(key, text)
    return (text, {
        "input": resp.input_tokens,
        "output": resp.output_tokens,
        "cached": False,
    })


def clear_cache() -> None:
    _NARRATE_CACHE.clear()


def cache_stats() -> dict:
    return _NARRATE_CACHE.stats()

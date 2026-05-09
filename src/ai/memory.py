"""
Short-term conversational memory for the Ask tab.

A sliding window of the last few user turns + a one-line summary of what
each query returned. Folded into router / nl-to-sql / narrate prompts so
referential follow-ups like "give me their names" or "what about volume?"
can be resolved when the previous query returned multiple symbols (and
``last_ticker`` therefore got cleared).

Why a separate module?
    - Both ``cache.py`` (cache key) and ``ask.py`` (session state) need to
      hash the same transcript shape — keeping the dataclass + helpers in
      one place avoids divergent encodings.
    - Tests can construct ``ConversationTurn`` objects without pulling in
      Streamlit / AIClient.

Sized for ~3 turns × short summaries → ~200-400 extra input tokens per
LLM call, which is essentially free at Haiku pricing.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass


# Window size — tune at the call site (render_ask_tab) by slicing the
# session_state list before passing in.
MAX_TURNS = 3

# Cap symbols-per-turn so a "top 100" result doesn't bloat the prompt.
_MAX_SYMBOLS_PER_TURN = 10


@dataclass(frozen=True)
class ConversationTurn:
    """
    One past user turn worth remembering.

    `top_symbols` is the most-relevant symbols from the result (up to 10),
    in the order the result returned them — the LLM uses this to resolve
    referential pronouns ("their names", "and the bottom 3").

    `summary` is the one-line narrative we already produce, or a synthesised
    fallback when the narrator failed/wasn't called.
    """
    question: str
    top_symbols: tuple[str, ...] = ()
    summary: str = ""


def extract_top_symbols(
    columns: list[str], rows: list[tuple], max_n: int = _MAX_SYMBOLS_PER_TURN,
) -> tuple[str, ...]:
    """
    Pull up to `max_n` symbols out of a query result.

    Looks for a column named exactly 'symbol' (case-insensitive) and takes
    its values in row order. Returns an empty tuple if no such column or
    rows are empty — callers should treat that as "this query had no
    natural symbol-list to remember".
    """
    if not columns or not rows:
        return ()
    try:
        col_idx = next(
            i for i, c in enumerate(columns) if str(c).lower() == "symbol"
        )
    except StopIteration:
        return ()

    out: list[str] = []
    for row in rows:
        if col_idx >= len(row):
            continue
        sym = row[col_idx]
        if sym is None:
            continue
        s = str(sym).strip().upper()
        if s and s not in out:
            out.append(s)
        if len(out) >= max_n:
            break
    return tuple(out)


def format_transcript(turns: tuple[ConversationTurn, ...] | list[ConversationTurn] | None) -> str:
    """
    Render the recent turns as a plain-text block for prompt injection.

    Empty/None → empty string (caller can concatenate unconditionally).
    The format is intentionally human-readable so the LLM doesn't have to
    do any parsing — just read the conversation flow.
    """
    if not turns:
        return ""

    lines = ["<conversation>"]
    for i, t in enumerate(turns, start=1):
        lines.append(f"Turn {i}:")
        lines.append(f"  User: {t.question.strip()}")
        if t.top_symbols:
            lines.append(f"  Result symbols: {', '.join(t.top_symbols)}")
        if t.summary:
            lines.append(f"  Summary: {t.summary.strip()}")
    lines.append("</conversation>")
    return "\n".join(lines)


def transcript_hash(
    turns: tuple[ConversationTurn, ...] | list[ConversationTurn] | None,
) -> str:
    """
    Stable short hash of the transcript, suitable for folding into cache
    keys. Empty input → empty string so old cached entries (with no
    transcript) keep their existing keys.
    """
    if not turns:
        return ""
    h = hashlib.sha1()
    for t in turns:
        h.update(t.question.strip().lower().encode())
        h.update(b"|")
        h.update(",".join(t.top_symbols).encode())
        h.update(b"|")
        h.update(t.summary.strip().lower().encode())
        h.update(b"||")
    return h.hexdigest()[:12]

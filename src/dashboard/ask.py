"""Conversational query tab for MarketAtlas — Layer 3 'Ask' tab."""
from __future__ import annotations

import datetime as dt
import os
import sys

import pandas as pd
import streamlit as st


# Developer-only affordances (e.g. "Clear AI cache") are hidden from end
# users by default. Same convention as .streamlit/config.toml's
# `toolbarMode = "minimal"` — operators opt back in via the env var:
#
#   STREAMLIT_CLIENT_TOOLBAR_MODE=developer streamlit run src/dashboard/app.py
_DEVELOPER_MODE = os.environ.get("STREAMLIT_CLIENT_TOOLBAR_MODE") == "developer"

from src.ai.cache import (
    cache_stats,
    clear_all as clear_ai_cache,
    lookup_ai_sql,
    lookup_route,
    store_ai_sql,
    store_route_miss,
    store_route_template,
)
from src.ai.client import AIClient, AIClientError
from src.ai.intent_router import RoutedTemplate, RoutingMiss, route
from src.ai.narrate import summarize as narrate_result
from src.ai.nl_to_sql import (
    CannotAnswerError,
    GeneratedQuery,
    GenerationError,
    extract_single_ticker,
    generate_sql,
)
from src.ai.query_templates import TEMPLATES, TemplateError, render
from src.db.readonly import (
    ExecutionError,
    QueryTimeoutError,
    UnsafeSQLError,
    execute_safe,
    validate_sql,
)
from src.db.repositories import log_nl_query


EXAMPLE_QUESTIONS = [
    "Which Health Care stocks in the S&P 500 are up more than 10% in the last 30 days?",
    "What's the average daily volume for NVDA over the past 90 days?",
    "Show me the top 10 NASDAQ-100 stocks by return over the past week.",
    "Which stocks appear in all three indices?",
    "How many stocks are in each sector across the S&P 500?",
    "Find symbols where today's volume is at least 3x the 20-day average.",
]


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def _truncate(s: str, n: int = 100) -> str:
    s = s.replace("\n", " ").replace("\r", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


def _log_summary(
    question: str,
    *,
    model: str,
    status: str,                       # success | cannot_answer | unsafe | timeout | execution | generation | ai_error
    path: str | None = None,           # template | ai_sql
    cached: bool = False,
    template: str | None = None,
    rows: int | None = None,
    duration_ms: int | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    detail: str | None = None,
) -> None:
    """
    One-line stderr summary per Ask query — every relevant field on one row
    so grepping for a single question's full lifecycle is straightforward
    even when many sessions interleave concurrently.

    Example:
      [ask] 2026-05-06T09:32:58 status=success path=template tmpl=cross_index_membership
            cached=true rows=8 ms=24 tokens=0/0 q='Which stocks appear in all three indices?'

    Full LLM response + bound params live in public.nl_queries — this log
    is for at-a-glance triage, not full audit.
    """
    parts = [f"[ask] {_now()}", f"status={status}"]
    if path:
        parts.append(f"path={path}")
    if template:
        parts.append(f"tmpl={template}")
    parts.append(f"cached={'true' if cached else 'false'}")
    if rows is not None:
        parts.append(f"rows={rows}")
    if duration_ms is not None:
        parts.append(f"ms={duration_ms}")
    parts.append(f"tokens={input_tokens}/{output_tokens}")
    parts.append(f"model={model}")
    if detail:
        parts.append(f"detail='{_truncate(detail, 120)}'")
    parts.append(f"q='{_truncate(question, 100)}'")
    print(" ".join(parts), file=sys.stderr, flush=True)


def _attach_narrative(
    outcome: dict, ai_client: AIClient, question: str
) -> dict:
    """
    Best-effort one-liner summary attached to a successful outcome. Mutates
    and returns the same dict so callers can `return _attach_narrative(...)`
    inline. Failures (API down, empty rows beyond the empty handling in
    narrate.summarize) leave the outcome unchanged.
    """
    if not outcome.get("ok"):
        return outcome
    r = outcome.get("result")
    if r is None:
        return outcome
    narration = narrate_result(ai_client, question, r.columns, r.rows)
    if narration is None:
        return outcome
    text, tokens = narration
    outcome["narrative"] = text
    outcome["narrative_tokens"] = tokens
    return outcome


def _run_query(
    question: str,
    ai_client: AIClient,
    db_url_readonly: str,
    db_url: str,
    *,
    last_ticker: str | None = None,
) -> dict:
    """
    Route → render → execute, with the AI-SQL flow as the fallback path.

    Returns a result dict for rendering. Never raises — all exceptions are
    converted into result dicts so the UI can render them.
    """
    # 1. Consult the route cache. A hit means we've already paid for this
    # routing decision recently — no API call needed. The actual SQL still
    # runs against the current DB state, so daily data changes are picked up.
    model_id = ai_client.model
    route_hit = lookup_route(question, model_id, last_ticker=last_ticker)
    routed_from_cache = False

    if route_hit is not None:
        routed_from_cache = True
        if route_hit.kind == "template":
            routing = RoutedTemplate(
                name=route_hit.name,
                params=route_hit.params or {},
                raw_response="<cache>",
                input_tokens=0,
                output_tokens=0,
                cache_read_tokens=0,
                cache_creation_tokens=0,
            )
        else:
            routing = RoutingMiss(
                raw_response="<cache>",
                input_tokens=0,
                output_tokens=0,
                cache_read_tokens=0,
                cache_creation_tokens=0,
            )
    else:
        try:
            routing = route(ai_client, question, last_ticker=last_ticker)
        except GenerationError as e:
            _log_summary(
                question, model=model_id, status="generation_error",
                path="template", detail=f"router: {e}",
            )
            log_nl_query(
                db_url, question=question, status="generation_error",
                error_message=f"router: {e}", path="template",
            )
            return {"ok": False, "kind": "generation", "msg": f"router: {e}"}
        except AIClientError as e:
            _log_summary(
                question, model=model_id, status="ai_error",
                path="template", detail=f"router: {e}",
            )
            log_nl_query(
                db_url, question=question, status="generation_error",
                error_message=f"router: {e}", path="template",
            )
            return {"ok": False, "kind": "ai_error", "msg": f"router: {e}"}

        # Cache the successful routing decision (including a deliberate miss
        # — re-asking the same out-of-template question shouldn't burn another
        # router call).
        if isinstance(routing, RoutedTemplate):
            store_route_template(
                question, model_id, routing.name, routing.params,
                last_ticker=last_ticker,
            )
        else:
            store_route_miss(question, model_id, last_ticker=last_ticker)

    if isinstance(routing, RoutedTemplate):
        try:
            sql, bound = render(routing.name, routing.params)
            result = execute_safe(db_url_readonly, sql, bound_params=bound)
            log_nl_query(
                db_url,
                question=question,
                generated_sql=sql,
                status="success",
                row_count=result.row_count,
                duration_ms=result.duration_ms,
                input_tokens=routing.input_tokens,
                output_tokens=routing.output_tokens,
                cache_read_tokens=routing.cache_read_tokens,
                cache_creation_tokens=routing.cache_creation_tokens,
                path="template",
                template_name=routing.name,
                template_params=routing.params,
                from_cache=routed_from_cache,
                raw_response=routing.raw_response,
            )
            _log_summary(
                question, model=model_id, status="success", path="template",
                template=routing.name, cached=routed_from_cache,
                rows=result.row_count, duration_ms=result.duration_ms,
                input_tokens=routing.input_tokens, output_tokens=routing.output_tokens,
            )
            return _attach_narrative({
                "ok": True,
                "via": "template",
                "template_name": routing.name,
                "template_params": routing.params,
                "from_cache": routed_from_cache,
                "sql": sql,
                "result": result,
                "tokens": {
                    "input": routing.input_tokens,
                    "output": routing.output_tokens,
                    "cache_read": routing.cache_read_tokens,
                    "cache_creation": routing.cache_creation_tokens,
                },
            }, ai_client, question)
        except TemplateError as e:
            # Param mismatch from the router (e.g. unknown sector). Don't
            # fall through to AI-SQL — the model already chose this template,
            # so it understood the question; the parameter extraction is
            # what failed. Surface it clearly.
            _log_summary(
                question, model=model_id, status="unsafe", path="template",
                template=routing.name, cached=routed_from_cache,
                detail=f"template params: {e}",
                input_tokens=routing.input_tokens, output_tokens=routing.output_tokens,
            )
            log_nl_query(
                db_url, question=question, status="unsafe_sql",
                error_message=f"template params: {e}",
                template_name=routing.name,
                template_params=routing.params,
                path="template",
                input_tokens=routing.input_tokens,
                output_tokens=routing.output_tokens,
                cache_read_tokens=routing.cache_read_tokens,
                cache_creation_tokens=routing.cache_creation_tokens,
                from_cache=routed_from_cache,
                raw_response=routing.raw_response,
            )
            return {
                "ok": False,
                "kind": "unsafe",
                "msg": f"Template '{routing.name}' got invalid params: {e}",
            }
        except QueryTimeoutError as e:
            _log_summary(
                question, model=model_id, status="timeout", path="template",
                template=routing.name, cached=routed_from_cache,
                input_tokens=routing.input_tokens, output_tokens=routing.output_tokens,
            )
            log_nl_query(
                db_url, question=question, generated_sql=sql, status="timeout",
                error_message=str(e), path="template", template_name=routing.name,
                template_params=routing.params,
                from_cache=routed_from_cache,
                raw_response=routing.raw_response,
            )
            return {"ok": False, "kind": "timeout", "msg": str(e), "sql": sql}
        except ExecutionError as e:
            _log_summary(
                question, model=model_id, status="execution", path="template",
                template=routing.name, cached=routed_from_cache,
                detail=str(e),
                input_tokens=routing.input_tokens, output_tokens=routing.output_tokens,
            )
            log_nl_query(
                db_url, question=question, generated_sql=sql, status="execution_error",
                error_message=str(e), path="template", template_name=routing.name,
                template_params=routing.params,
                from_cache=routed_from_cache,
                raw_response=routing.raw_response,
            )
            return {"ok": False, "kind": "execution", "msg": str(e), "sql": sql}

    # routing is a RoutingMiss — fall through to free-form AI-SQL generation.
    router_tokens = {
        "input": routing.input_tokens,
        "output": routing.output_tokens,
        "cache_read": routing.cache_read_tokens,
        "cache_creation": routing.cache_creation_tokens,
    }

    generated: GeneratedQuery | None = None
    sql_from_cache = False
    try:
        # 1. Generate (or pull from cache)
        cached_sql = lookup_ai_sql(question, model_id, last_ticker=last_ticker)
        if cached_sql is not None:
            sql_from_cache = True
            # Synthesise a GeneratedQuery so downstream logging is uniform.
            generated = GeneratedQuery(
                sql=cached_sql,
                raw_response="<cache>",
                input_tokens=0,
                output_tokens=0,
                cache_read_tokens=0,
                cache_creation_tokens=0,
            )
        else:
            generated = generate_sql(ai_client, question, last_ticker=last_ticker)
            store_ai_sql(
                question, model_id, generated.sql, last_ticker=last_ticker
            )

        # 2. Validate (cheap; do this even on cache hits — the cached SQL
        # was validated before being stored, but a future schema/validator
        # change shouldn't let stale unsafe SQL through).
        safe_sql = validate_sql(generated.sql)

        # 3. Execute
        result = execute_safe(db_url_readonly, safe_sql)

        # An AI-SQL run is "from cache" only if BOTH the routing miss and the
        # SQL itself were cached — otherwise we paid for at least one API call.
        ai_sql_fully_cached = routed_from_cache and sql_from_cache

        # 4. Log success
        log_nl_query(
            db_url,
            question=question,
            generated_sql=safe_sql,
            status="success",
            row_count=result.row_count,
            duration_ms=result.duration_ms,
            input_tokens=generated.input_tokens + router_tokens["input"],
            output_tokens=generated.output_tokens + router_tokens["output"],
            cache_read_tokens=generated.cache_read_tokens + router_tokens["cache_read"],
            cache_creation_tokens=generated.cache_creation_tokens + router_tokens["cache_creation"],
            path="ai_sql",
            from_cache=ai_sql_fully_cached,
            raw_response=generated.raw_response,
        )
        _log_summary(
            question, model=model_id, status="success", path="ai_sql",
            cached=ai_sql_fully_cached,
            rows=result.row_count, duration_ms=result.duration_ms,
            input_tokens=generated.input_tokens + router_tokens["input"],
            output_tokens=generated.output_tokens + router_tokens["output"],
        )
        return _attach_narrative({
            "ok": True,
            "via": "ai_sql",
            "from_cache": ai_sql_fully_cached,
            "sql": safe_sql,
            "result": result,
            "generated": generated,
            "tokens": {
                "input": generated.input_tokens + router_tokens["input"],
                "output": generated.output_tokens + router_tokens["output"],
                "cache_read": generated.cache_read_tokens + router_tokens["cache_read"],
                "cache_creation": generated.cache_creation_tokens + router_tokens["cache_creation"],
            },
        }, ai_client, question)

    except CannotAnswerError as e:
        _log_summary(
            question, model=model_id, status="cannot_answer", path="ai_sql",
            cached=sql_from_cache, detail=e.detail or str(e),
            input_tokens=generated.input_tokens if generated else 0,
            output_tokens=generated.output_tokens if generated else 0,
        )
        log_nl_query(
            db_url,
            question=question,
            generated_sql=generated.sql if generated else None,
            status="cannot_answer",
            error_message=e.detail or str(e),
            input_tokens=generated.input_tokens if generated else 0,
            output_tokens=generated.output_tokens if generated else 0,
            cache_read_tokens=generated.cache_read_tokens if generated else 0,
            cache_creation_tokens=generated.cache_creation_tokens if generated else 0,
            path="ai_sql",
            from_cache=sql_from_cache,
            raw_response=generated.raw_response if generated else None,
        )
        return {"ok": False, "kind": "cannot_answer", "msg": e.detail or str(e)}

    except UnsafeSQLError as e:
        _log_summary(
            question, model=model_id, status="unsafe", path="ai_sql",
            cached=sql_from_cache, detail=f"unsafe_sql: {e}",
            input_tokens=generated.input_tokens if generated else 0,
            output_tokens=generated.output_tokens if generated else 0,
        )
        log_nl_query(
            db_url,
            question=question,
            generated_sql=generated.sql if generated else None,
            status="unsafe_sql",
            error_message=str(e),
            input_tokens=generated.input_tokens if generated else 0,
            output_tokens=generated.output_tokens if generated else 0,
            path="ai_sql",
            from_cache=sql_from_cache,
            raw_response=generated.raw_response if generated else None,
        )
        return {
            "ok": False,
            "kind": "unsafe",
            "msg": str(e),
            "sql": generated.sql if generated else None,
        }

    except QueryTimeoutError as e:
        _log_summary(
            question, model=model_id, status="timeout", path="ai_sql",
            cached=sql_from_cache,
            input_tokens=generated.input_tokens if generated else 0,
            output_tokens=generated.output_tokens if generated else 0,
        )
        log_nl_query(
            db_url,
            question=question,
            generated_sql=generated.sql if generated else None,
            status="timeout",
            error_message=str(e),
            path="ai_sql",
            from_cache=sql_from_cache,
            raw_response=generated.raw_response if generated else None,
        )
        return {
            "ok": False,
            "kind": "timeout",
            "msg": str(e),
            "sql": generated.sql if generated else None,
        }

    except GenerationError as e:
        _log_summary(
            question, model=model_id, status="generation_error", path="ai_sql",
            detail=str(e),
        )
        log_nl_query(
            db_url, question=question, status="generation_error",
            error_message=str(e), path="ai_sql",
        )
        return {"ok": False, "kind": "generation", "msg": str(e)}

    except AIClientError as e:
        _log_summary(
            question, model=model_id, status="ai_error", path="ai_sql",
            detail=str(e),
        )
        log_nl_query(
            db_url, question=question, status="generation_error",
            error_message=str(e), path="ai_sql",
        )
        return {"ok": False, "kind": "ai_error", "msg": str(e)}

    except ExecutionError as e:
        _log_summary(
            question, model=model_id, status="execution", path="ai_sql",
            cached=sql_from_cache, detail=str(e),
            input_tokens=generated.input_tokens if generated else 0,
            output_tokens=generated.output_tokens if generated else 0,
        )
        log_nl_query(
            db_url,
            question=question,
            generated_sql=generated.sql if generated else None,
            status="execution_error",
            error_message=str(e),
            path="ai_sql",
            from_cache=sql_from_cache,
            raw_response=generated.raw_response if generated else None,
        )
        return {
            "ok": False,
            "kind": "execution",
            "msg": str(e),
            "sql": generated.sql if generated else None,
        }


def render_ask_tab(
    ai_client: AIClient | None,
    db_url_readonly: str | None,
    db_url: str,
) -> None:
    """Render the Ask tab. Entry point called from app.py."""

    st.markdown("### Ask AI")
    st.caption(
        "Ask questions in plain English. Queries are read-only, capped at "
        "1,000 rows, and timed out at 5 seconds."
    )

    # ---- Configuration check ----
    if ai_client is None:
        st.warning(
            "The Ask tab requires `anthropic_api_key` in configuration.json. "
            "Restart the dashboard after adding it."
        )
        return
    if not db_url_readonly:
        st.warning(
            "The Ask tab requires `db_url_readonly` in configuration.json. "
            "See the README for setup steps."
        )
        return

    # ---- Session state init ----
    if "ask_history" not in st.session_state:
        st.session_state.ask_history = []  # chronological: oldest first, newest last
    if "_ask_pending_example" not in st.session_state:
        st.session_state["_ask_pending_example"] = None
    if "ask_last_ticker" not in st.session_state:
        # Carries forward the single ticker (if any) referenced by the previous
        # successful query, so a follow-up like "what about volume?" can resolve
        # without the user retyping the symbol. Cleared on history clear.
        st.session_state.ask_last_ticker = None

    # ---- Example chips — click to run immediately ----
    # chat_input doesn't accept programmatic prefill, so a chip click sets a
    # "pending example" sentinel that we run later in the function (after
    # chat_input has rendered).
    st.markdown("**Example questions:**")
    chip_cols = st.columns(2)
    for i, ex in enumerate(EXAMPLE_QUESTIONS):
        with chip_cols[i % 2]:
            if st.button(ex, key=f"ask_ex_{i}", use_container_width=True):
                st.session_state["_ask_pending_example"] = ex
                st.rerun()

    # ---- Input — st.chat_input gives us Enter-to-submit natively ----
    # Trade-off: chat_input is single-line (no Shift+Enter newline support).
    # Custom JS to map Enter onto a text_area + button is unreliable because
    # Streamlit's React-based button widget doesn't accept synthetic clicks
    # consistently — only real trusted user clicks trigger the WebSocket
    # message that runs the submit handler. chat_input bypasses this by
    # building its own submit pipeline that responds to Enter directly.
    _MIN_CHARS = 10
    submitted = st.chat_input(
        placeholder="Ask a question — e.g. \"Which Health Care S&P 500 stocks gained 10% in the last 30 days?\"",
        max_chars=500,
        key="ask_chat_input",
    )

    # Layout for the secondary action buttons (Clear history, dev-only Clear cache).
    if _DEVELOPER_MODE:
        clear_history_col, clear_cache_col = st.columns([1, 1])
    else:
        clear_history_col = st.container()
        clear_cache_col = None

    with clear_history_col:
        if st.button("Clear history", use_container_width=True):
            st.session_state.ask_history = []
            st.session_state.ask_last_ticker = None
            st.rerun()
    if clear_cache_col is not None:
        with clear_cache_col:
            stats = cache_stats()
            cached_total = stats["route"]["size"] + stats["ai_sql"]["size"]
            if st.button(
                f"Clear AI cache ({cached_total})",
                use_container_width=True,
                help=(
                    "Drop every cached LLM decision. The cache normally serves "
                    "repeat questions without an API call. Developer-only — "
                    "set STREAMLIT_CLIENT_TOOLBAR_MODE=developer to expose it."
                ),
            ):
                clear_ai_cache()
                st.rerun()

    # ---- Run query — either chat_input submission or chip click ----
    # chat_input returns the submitted text on the rerun after Enter,
    # otherwise None. Chip clicks set _ask_pending_example for one rerun.
    pending = submitted or st.session_state.pop("_ask_pending_example", None)
    if pending:
        trimmed = pending.strip()
        if len(trimmed) < _MIN_CHARS:
            st.warning(
                f"Please ask a longer question (at least {_MIN_CHARS} characters)."
            )
        else:
            with st.spinner("Asking AI..."):
                outcome = _run_query(
                    trimmed, ai_client, db_url_readonly, db_url,
                    last_ticker=st.session_state.ask_last_ticker,
                )
            st.session_state.ask_history.append(
                {"question": trimmed, "outcome": outcome}
            )
            # Update the conversational anchor: only carry forward when the
            # query unambiguously narrowed to a single ticker. Multi-stock,
            # sector, and index queries clear it so a follow-up doesn't
            # silently pin to a stale stock.
            if outcome.get("ok") and outcome.get("sql"):
                ticker = extract_single_ticker(outcome["sql"])
                st.session_state.ask_last_ticker = ticker
            st.rerun()

    # ---- Render history (oldest first, newest at bottom — chat-style) ----
    if st.session_state.ask_history:
        st.markdown("---")
        for i, entry in enumerate(st.session_state.ask_history[-10:]):
            _render_history_entry(entry, idx=i)


def _render_history_entry(entry: dict, idx: int) -> None:
    """
    User-facing rendering. End users see only:
      - their question
      - the data table (or a friendly error)
      - row count and timing

    Developer mode (STREAMLIT_CLIENT_TOOLBAR_MODE=developer) additionally
    surfaces: routing path, template name, SQL, bound params, token usage.
    Implementation details (queries, SQL, "AI-SQL", template names) are not
    exposed to end users.
    """
    with st.container(border=True):
        st.markdown(f"**Q:** {entry['question']}")
        outcome = entry["outcome"]

        if outcome["ok"]:
            r = outcome["result"]
            cached = outcome.get("from_cache", False)

            # Dev-only: which path served this query
            if _DEVELOPER_MODE:
                via = outcome.get("via", "ai_sql")
                badge = (
                    f"via template · `{outcome['template_name']}`"
                    if via == "template"
                    else "via AI-SQL"
                )
                if cached:
                    badge += " · cached (no API call)"
                st.caption(badge)
            elif cached:
                # End users still get a small "instant" hint when no API was hit
                st.caption("instant response (no AI call)")

            narrative = outcome.get("narrative")
            if narrative:
                st.markdown(f"**A:** {narrative}")

            df = pd.DataFrame(r.rows, columns=r.columns)
            st.dataframe(df, use_container_width=True, hide_index=True)

            meta_parts = [f"{r.row_count} rows", f"{r.duration_ms} ms"]
            if r.truncated:
                meta_parts.append("truncated to 1000")
            if _DEVELOPER_MODE:
                tokens = outcome.get("tokens") or {}
                if tokens.get("cache_read", 0) > 0:
                    meta_parts.append(f"cached {tokens['cache_read']} tok")
            st.caption(" · ".join(meta_parts))

            # Dev-only: technical details (SQL + bound params)
            if _DEVELOPER_MODE:
                via = outcome.get("via", "ai_sql")
                label = (
                    "Show template SQL + bound params"
                    if via == "template"
                    else "Show generated SQL"
                )
                with st.expander(label):
                    st.code(outcome["sql"], language="sql")
                    if via == "template" and outcome.get("template_params"):
                        st.markdown("**Bound parameters:**")
                        st.json(outcome["template_params"])
        else:
            kind = outcome["kind"]
            if kind == "cannot_answer":
                st.info(
                    f"AI couldn't answer this from the available data. "
                    f"{outcome['msg']}"
                )
            elif kind == "unsafe":
                st.error(
                    "Could not generate a safe AI response. Please rephrase "
                    "your question."
                )
                if _DEVELOPER_MODE and outcome.get("sql"):
                    with st.expander("Show generated SQL (rejected)"):
                        st.code(outcome["sql"], language="sql")
                    st.caption(f"Detail: {outcome['msg']}")
            elif kind == "timeout":
                st.warning(
                    "AI response took too long and was cancelled. "
                    "Try narrowing the date range or filtering by sector."
                )
                if _DEVELOPER_MODE and outcome.get("sql"):
                    with st.expander("Show generated SQL"):
                        st.code(outcome["sql"], language="sql")
            elif kind == "execution":
                st.error("Could not retrieve data for this question.")
                if _DEVELOPER_MODE:
                    if outcome.get("sql"):
                        with st.expander("Show generated SQL"):
                            st.code(outcome["sql"], language="sql")
                    st.caption(f"Detail: {outcome['msg']}")
            elif kind in ("generation", "ai_error"):
                st.error(
                    "Could not generate an AI response. "
                    "Please try rephrasing your question."
                )
                if _DEVELOPER_MODE:
                    st.caption(f"Detail: {outcome['msg']}")
            else:
                st.error("Something went wrong. Please try again.")
                if _DEVELOPER_MODE:
                    st.caption(f"Detail: {outcome['msg']}")

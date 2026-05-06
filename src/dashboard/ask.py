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
from src.ai.nl_to_sql import (
    CannotAnswerError,
    GeneratedQuery,
    GenerationError,
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


def _log(msg: str) -> None:
    """Single-line stderr log with [ask] prefix and timestamp.
    Visible in the Streamlit server's terminal/log file — same convention
    as src/dashboard/data.py uses for Marketaux fetches."""
    print(f"[ask] {_now()} {msg}", file=sys.stderr, flush=True)


def _run_query(
    question: str,
    ai_client: AIClient,
    db_url_readonly: str,
    db_url: str,
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
    route_hit = lookup_route(question, model_id)
    routed_from_cache = False

    _log(f"Q='{question[:120]}' model={model_id}")

    if route_hit is not None:
        routed_from_cache = True
        if route_hit.kind == "template":
            _log(f"  router: CACHE HIT → template={route_hit.name}")
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
            _log(f"  router: CACHE HIT → miss (will fall back to AI-SQL)")
            routing = RoutingMiss(
                raw_response="<cache>",
                input_tokens=0,
                output_tokens=0,
                cache_read_tokens=0,
                cache_creation_tokens=0,
            )
    else:
        try:
            routing = route(ai_client, question)
        except GenerationError as e:
            _log(f"  router: ERROR (GenerationError) {e}")
            log_nl_query(
                db_url, question=question, status="generation_error",
                error_message=f"router: {e}", path="template",
            )
            return {"ok": False, "kind": "generation", "msg": f"router: {e}"}
        except AIClientError as e:
            _log(f"  router: ERROR (AIClientError) {e}")
            log_nl_query(
                db_url, question=question, status="generation_error",
                error_message=f"router: {e}", path="template",
            )
            return {"ok": False, "kind": "ai_error", "msg": f"router: {e}"}

        # Cache the successful routing decision (including a deliberate miss
        # — re-asking the same out-of-template question shouldn't burn another
        # router call).
        if isinstance(routing, RoutedTemplate):
            _log(f"  router: API call → template={routing.name} "
                 f"params={routing.params} tokens={routing.input_tokens}/{routing.output_tokens}"
                 f" cache_read={routing.cache_read_tokens}")
            store_route_template(question, model_id, routing.name, routing.params)
        else:
            _log(f"  router: API call → miss (will fall back to AI-SQL) "
                 f"tokens={routing.input_tokens}/{routing.output_tokens}"
                 f" cache_read={routing.cache_read_tokens}")
            store_route_miss(question, model_id)

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
            _log(f"  → SUCCESS path=template rows={result.row_count} "
                 f"db={result.duration_ms}ms cached={routed_from_cache}")
            return {
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
            }
        except TemplateError as e:
            # Param mismatch from the router (e.g. unknown sector). Don't
            # fall through to AI-SQL — the model already chose this template,
            # so it understood the question; the parameter extraction is
            # what failed. Surface it clearly.
            _log(f"  → REJECT path=template reason='template params: {e}'")
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
            _log(f"  → TIMEOUT path=template")
            log_nl_query(
                db_url, question=question, generated_sql=sql, status="timeout",
                error_message=str(e), path="template", template_name=routing.name,
                template_params=routing.params,
                from_cache=routed_from_cache,
                raw_response=routing.raw_response,
            )
            return {"ok": False, "kind": "timeout", "msg": str(e), "sql": sql}
        except ExecutionError as e:
            _log(f"  → EXEC ERROR path=template msg='{e}'")
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
        cached_sql = lookup_ai_sql(question, model_id)
        if cached_sql is not None:
            sql_from_cache = True
            _log(f"  ai_sql: CACHE HIT")
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
            generated = generate_sql(ai_client, question)
            _log(f"  ai_sql: API call → tokens={generated.input_tokens}/{generated.output_tokens}"
                 f" cache_read={generated.cache_read_tokens}")
            store_ai_sql(question, model_id, generated.sql)

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
        _log(f"  → SUCCESS path=ai_sql rows={result.row_count} "
             f"db={result.duration_ms}ms cached={ai_sql_fully_cached}")
        return {
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
        }

    except CannotAnswerError as e:
        _log(f"  → CANNOT_ANSWER path=ai_sql detail='{e.detail or e}'")
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
        _log(f"  → REJECT path=ai_sql reason='unsafe_sql: {e}'")
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
        _log(f"  → TIMEOUT path=ai_sql")
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
        _log(f"  → GEN ERROR path=ai_sql msg='{e}'")
        log_nl_query(
            db_url, question=question, status="generation_error",
            error_message=str(e), path="ai_sql",
        )
        return {"ok": False, "kind": "generation", "msg": str(e)}

    except AIClientError as e:
        _log(f"  → AI ERROR path=ai_sql msg='{e}'")
        log_nl_query(
            db_url, question=question, status="generation_error",
            error_message=str(e), path="ai_sql",
        )
        return {"ok": False, "kind": "ai_error", "msg": str(e)}

    except ExecutionError as e:
        _log(f"  → EXEC ERROR path=ai_sql msg='{e}'")
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

    st.markdown("### Ask MarketAtlas")
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
        st.session_state.ask_history = []  # newest-first list of {question, outcome}
    if "ask_input_text" not in st.session_state:
        st.session_state.ask_input_text = ""

    # If the previous run requested an input clear (after submit / "Clear
    # history"), do it BEFORE the text_area widget renders. Streamlit forbids
    # writing to a widget's session_state key after the widget has rendered.
    if st.session_state.pop("_ask_clear_pending", False):
        st.session_state.ask_input_text = ""

    # ---- Example chips ----
    # Clicking a chip writes the example directly into the text_area's
    # session_state key BEFORE the widget renders on the next run. Streamlit
    # ignores the `value=` parameter on a widget that already has a `key`,
    # so we must mutate session_state[key], not a separate variable.
    st.markdown("**Example questions:**")
    chip_cols = st.columns(2)
    for i, ex in enumerate(EXAMPLE_QUESTIONS):
        with chip_cols[i % 2]:
            if st.button(ex, key=f"ask_ex_{i}", use_container_width=True):
                st.session_state.ask_input_text = ex
                st.rerun()

    # ---- Input ----
    question = st.text_area(
        "Your question",
        height=80,
        placeholder="e.g. Which Energy stocks gained more than 5% this month?",
        key="ask_input_text",
    )

    # Layout: Ask is always primary; "Clear history" is user-facing; the
    # "Clear AI cache" affordance is developer-only (toggle by launching
    # with STREAMLIT_CLIENT_TOOLBAR_MODE=developer).
    if _DEVELOPER_MODE:
        submit_col, clear_history_col, clear_cache_col = st.columns([2, 1, 1])
    else:
        submit_col, clear_history_col = st.columns([2, 1])
        clear_cache_col = None

    with submit_col:
        submit = st.button(
            "Ask",
            type="primary",
            disabled=not question.strip(),
            use_container_width=True,
        )
    with clear_history_col:
        if st.button("Clear history", use_container_width=True):
            st.session_state.ask_history = []
            st.session_state["_ask_clear_pending"] = True
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

    # ---- Run query ----
    if submit and question.strip():
        with st.spinner("Generating query..."):
            outcome = _run_query(
                question.strip(), ai_client, db_url_readonly, db_url
            )
        st.session_state.ask_history.insert(
            0, {"question": question.strip(), "outcome": outcome}
        )
        st.session_state["_ask_clear_pending"] = True
        st.rerun()

    # ---- Render history (newest first, max 10) ----
    if st.session_state.ask_history:
        st.markdown("---")
        for i, entry in enumerate(st.session_state.ask_history[:10]):
            _render_history_entry(entry, idx=i)


def _render_history_entry(entry: dict, idx: int) -> None:
    with st.container(border=True):
        st.markdown(f"**Q:** {entry['question']}")
        outcome = entry["outcome"]

        if outcome["ok"]:
            r = outcome["result"]
            via = outcome.get("via", "ai_sql")
            cached = outcome.get("from_cache", False)
            badge = (
                f"via template · `{outcome['template_name']}`"
                if via == "template"
                else "via AI-SQL"
            )
            if cached:
                badge += " · cached (no API call)"
            st.caption(badge)

            df = pd.DataFrame(r.rows, columns=r.columns)
            st.dataframe(df, use_container_width=True, hide_index=True)

            meta_parts = [f"{r.row_count} rows", f"{r.duration_ms} ms"]
            if r.truncated:
                meta_parts.append("truncated to 1000")
            tokens = outcome.get("tokens") or {}
            if tokens.get("cache_read", 0) > 0:
                meta_parts.append(f"cached {tokens['cache_read']} tok")
            st.caption(" · ".join(meta_parts))

            sql_label = (
                "Show template SQL + bound params"
                if via == "template"
                else "Show generated SQL"
            )
            with st.expander(sql_label):
                st.code(outcome["sql"], language="sql")
                if via == "template" and outcome.get("template_params"):
                    st.markdown("**Bound parameters:**")
                    st.json(outcome["template_params"])
        else:
            kind = outcome["kind"]
            if kind == "cannot_answer":
                st.info(
                    f"This question can't be answered from the available data. "
                    f"{outcome['msg']}"
                )
            elif kind == "unsafe":
                st.error(
                    f"The generated query was rejected for safety: {outcome['msg']}"
                )
                if outcome.get("sql"):
                    with st.expander("Show generated SQL (rejected)"):
                        st.code(outcome["sql"], language="sql")
            elif kind == "timeout":
                st.warning(
                    "Query took longer than 5 seconds and was cancelled. "
                    "Try narrowing the date range or filtering by sector."
                )
                if outcome.get("sql"):
                    with st.expander("Show generated SQL"):
                        st.code(outcome["sql"], language="sql")
            elif kind == "execution":
                st.error(f"Query execution failed: {outcome['msg']}")
                if outcome.get("sql"):
                    with st.expander("Show generated SQL"):
                        st.code(outcome["sql"], language="sql")
            elif kind in ("generation", "ai_error"):
                st.error(f"Could not generate a query: {outcome['msg']}")
            else:
                st.error(f"Something went wrong: {outcome['msg']}")

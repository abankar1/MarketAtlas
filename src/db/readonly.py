"""Validation and execution of AI-generated SQL on the read-only role."""
from __future__ import annotations

import time
from dataclasses import dataclass

import psycopg
import sqlglot
from sqlglot import expressions as exp


# ----------------------------------------------------------------------------
# Validation
# ----------------------------------------------------------------------------

# Node types that are allowed to APPEAR AS THE ROOT of the parse tree.
# A SELECT can contain subqueries, CTEs, set operations, etc.
ALLOWED_ROOTS: tuple = (exp.Select, exp.With, exp.Union, exp.Intersect, exp.Except)


def _collect_forbidden_node_types() -> tuple:
    """
    Build the FORBIDDEN_NODES tuple defensively — sqlglot's expression class
    names shift between releases, so we only include classes that actually
    exist on the installed version.
    """
    candidates = [
        "Insert", "Update", "Delete", "Drop", "Alter",
        "Create", "TruncateTable", "Truncate", "Command", "Transaction",
        "Use", "Set", "Pragma", "AlterColumn", "Grant", "Revoke",
        "Copy", "Call", "Notify", "Listen", "Unlisten",
    ]
    found: list = []
    for name in candidates:
        cls = getattr(exp, name, None)
        if cls is not None:
            found.append(cls)
    return tuple(found)


FORBIDDEN_NODES: tuple = _collect_forbidden_node_types()


# Function names that are forbidden — e.g., functions that can write or
# escape sandbox boundaries. sqlglot doesn't have explicit nodes for all of
# these, so we check by name (lowercased).
FORBIDDEN_FUNCTION_NAMES = {
    "pg_read_file", "pg_read_binary_file",
    "pg_ls_dir", "pg_stat_file",
    "lo_import", "lo_export",
    "dblink", "dblink_exec",
    "pg_terminate_backend", "pg_cancel_backend",
    "pg_sleep",  # could be used to evade timeouts in clever ways
    "copy",
}


class UnsafeSQLError(Exception):
    """The SQL was rejected by the validator."""


class QueryTimeoutError(Exception):
    """The query exceeded the statement timeout."""


class ExecutionError(Exception):
    """The query failed for a reason other than safety or timeout."""


@dataclass
class ExecutionResult:
    columns: list[str]
    rows: list[tuple]
    row_count: int
    duration_ms: int
    truncated: bool


def validate_sql(sql: str, max_limit: int = 1000) -> str:
    """
    Validate that `sql` is a single read-only SELECT/WITH/set-operation
    statement, with no forbidden nodes or functions, and enforce a LIMIT.

    Returns the (possibly modified) SQL string.
    Raises UnsafeSQLError on any violation.
    """
    sql = sql.strip()
    if not sql:
        raise UnsafeSQLError("empty SQL")

    # Strip a single trailing semicolon for the parser; reject multiple statements.
    sql = sql.rstrip(";").strip()
    # If there are still semicolons inside, it might be a multi-statement injection.
    if ";" in sql:
        raise UnsafeSQLError("multiple statements are not allowed")

    # Parse with sqlglot in postgres dialect.
    try:
        parsed = sqlglot.parse_one(sql, dialect="postgres")
    except Exception as e:
        raise UnsafeSQLError(f"could not parse SQL: {e}") from e

    if parsed is None:
        raise UnsafeSQLError("parser returned no statement")

    # Root must be a SELECT, WITH, or set operation.
    if not isinstance(parsed, ALLOWED_ROOTS):
        raise UnsafeSQLError(
            f"root must be SELECT or WITH, got {type(parsed).__name__}"
        )

    # Walk the entire tree. If any forbidden node type appears, reject.
    for node in parsed.walk():
        # walk() yields (node, parent, key) tuples in some sqlglot versions
        # and bare nodes in others. Normalize.
        actual = node[0] if isinstance(node, tuple) else node
        if FORBIDDEN_NODES and isinstance(actual, FORBIDDEN_NODES):
            raise UnsafeSQLError(
                f"forbidden statement type: {type(actual).__name__}"
            )

    # Check for forbidden function calls by name.
    for func in parsed.find_all(exp.Func):
        name = ""
        if hasattr(func, "sql_name"):
            try:
                name = func.sql_name().lower()
            except Exception:
                name = ""
        if name in FORBIDDEN_FUNCTION_NAMES:
            raise UnsafeSQLError(f"forbidden function: {name}")
    # Anonymous function calls are exp.Anonymous
    for anon in parsed.find_all(exp.Anonymous):
        raw = anon.this if hasattr(anon, "this") else None
        name = (raw or "").lower() if isinstance(raw, str) else ""
        if name in FORBIDDEN_FUNCTION_NAMES:
            raise UnsafeSQLError(f"forbidden function: {name}")

    # Enforce LIMIT on the outermost SELECT.
    # sqlglot's .limit() method handles this idempotently.
    existing_limit = parsed.args.get("limit")
    needs_limit = True
    if existing_limit is not None:
        try:
            limit_expr = existing_limit.expression
            if isinstance(limit_expr, exp.Literal) and limit_expr.is_int:
                n = int(limit_expr.this)
                if n <= max_limit:
                    needs_limit = False
        except Exception:
            # If we can't read it, replace it.
            pass

    if needs_limit:
        parsed = parsed.limit(max_limit)

    return parsed.sql(dialect="postgres")


# ----------------------------------------------------------------------------
# Execution
# ----------------------------------------------------------------------------

def execute_safe(
    db_url_readonly: str,
    sql: str,
    bound_params: dict | None = None,
    max_rows: int = 1000,
    timeout_ms: int = 15000,
) -> ExecutionResult:
    """
    Execute already-validated SQL on the read-only role.

    Default 15s timeout fits Timescale Cloud round-trip + analytical query
    headroom (e.g. NASDAQ-100 7-day return ~4.5s on cloud free tier);
    local Postgres queries that finish in <100ms are unaffected by the
    higher ceiling.

    The role itself has statement_timeout=5s set at the role level; this
    function ALSO sets statement_timeout per-connection as belt-and-braces.

    bound_params: optional dict of psycopg named parameters
    (e.g. {"sector": "Health Care"}). psycopg handles escaping safely;
    no string interpolation happens in this module.
    """
    started = time.perf_counter()
    try:
        with psycopg.connect(
            db_url_readonly,
            options=f"-c statement_timeout={timeout_ms}",
            connect_timeout=5,
        ) as conn:
            # Defensive: explicitly mark the transaction as read-only. If the
            # role accidentally has writes granted in the future, this still
            # blocks them.
            with conn.cursor() as cur:
                cur.execute("SET TRANSACTION READ ONLY")
                try:
                    cur.execute(sql, bound_params or None)
                except psycopg.errors.QueryCanceled as e:
                    raise QueryTimeoutError(
                        f"query exceeded {timeout_ms}ms timeout"
                    ) from e
                except psycopg.errors.InsufficientPrivilege as e:
                    # Should never happen — the validator should have caught
                    # any write operation. If it does, surface it loudly.
                    raise ExecutionError(
                        f"insufficient privilege (validator missed something?): {e}"
                    ) from e
                except psycopg.Error as e:
                    raise ExecutionError(f"query failed: {e}") from e

                columns = [d[0] for d in cur.description] if cur.description else []
                # Fetch one extra row to detect truncation.
                rows = cur.fetchmany(max_rows + 1)
                truncated = len(rows) > max_rows
                rows = rows[:max_rows]
    except psycopg.OperationalError as e:
        raise ExecutionError(f"could not connect to readonly DB: {e}") from e

    duration_ms = int((time.perf_counter() - started) * 1000)
    return ExecutionResult(
        columns=columns,
        rows=[tuple(r) for r in rows],
        row_count=len(rows),
        duration_ms=duration_ms,
        truncated=truncated,
    )

"""Read-only telemetry SQL tool.

Defense in depth, outermost first:

1. `guard_sql()` — a tokenizer-based guard: one SELECT/WITH statement, no DML/DDL or
   dangerous functions, and every relation must be `fde_views.vw_active_fleet` (or a CTE).
   It also enforces a row cap.
2. The statement runs inside `SET TRANSACTION READ ONLY` with a local statement timeout.
3. The connection authenticates as `usr_fde_ro`, which PostgreSQL itself limits to SELECT on
   the view (plus INSERT on the audit log, unused here).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from typing import Any

import psycopg
from langchain_core.tools import BaseTool, tool
from psycopg import errors as pg_errors
from psycopg import sql
from psycopg_pool import ConnectionPool, PoolTimeout
from pydantic import BaseModel, Field

from cold_chain_copilot.agent.tools import SQL_TOOL_NAME, ToolOutcome, dumps, to_jsonable
from cold_chain_copilot.core.exceptions import RBACViolationError, SQLGuardError

ALLOWED_SCHEMA = "fde_views"
ALLOWED_VIEW = "vw_active_fleet"
TRACE_ROW_PREVIEW = 50

_TOKEN_RE = re.compile(
    r"""
      (?P<ws>\s+)
    | (?P<line_comment>--[^\n]*)
    | (?P<block_comment>/\*.*?\*/)
    | (?P<string>'(?:[^']|'')*')
    | (?P<qident>"(?:[^"]|"")+")
    | (?P<number>\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)
    | (?P<word>[A-Za-z_][A-Za-z0-9_]*)
    | (?P<op>::|<=|>=|<>|!=|\|\||[(),.;*+\-/%<>=\[\]:~!])
    | (?P<other>.)
    """,
    re.VERBOSE | re.DOTALL,
)

_FORBIDDEN_KEYWORDS = frozenset(
    {
        "alter", "analyze", "begin", "call", "checkpoint", "cluster", "comment", "commit",
        "copy", "create", "deallocate", "delete", "discard", "do", "drop", "execute", "grant",
        "import", "insert", "into", "listen", "load", "lock", "merge", "notify", "owner",
        "prepare", "reassign", "refresh", "reindex", "release", "reset", "revoke", "rollback",
        "savepoint", "security", "set", "share", "truncate", "unlisten", "update", "vacuum",
    }
)  # fmt: skip

_FORBIDDEN_FUNCTIONS = frozenset(
    {
        "current_setting", "currval", "cursor_to_xml", "database_to_xml", "dblink",
        "dblink_exec", "lastval", "nextval", "query_to_xml", "query_to_xml_and_xmlschema",
        "query_to_xmlschema", "schema_to_xml", "set_config", "setval", "table_to_xml",
    }
)  # fmt: skip

_FORBIDDEN_FUNCTION_PREFIXES = ("pg_", "lo_", "dblink")
_FORBIDDEN_SCHEMAS = frozenset({"public", "pg_catalog", "information_schema", "pg_toast"})
_FROM_FUNCTIONS = frozenset({"extract", "substring", "trim", "overlay", "position"})
_RELATION_PREFIX_WORDS = frozenset({"lateral", "only"})
_CLAUSE_WORDS = frozenset(
    {
        "where", "group", "having", "order", "limit", "offset", "fetch", "union", "intersect",
        "except", "window", "join", "inner", "left", "right", "full", "cross", "natural", "on",
        "using", "for", "returning", "and", "or", "not", "lateral",
    }
)  # fmt: skip


@dataclass(frozen=True, slots=True)
class _Token:
    kind: str
    text: str

    @property
    def lower(self) -> str:
        return self.text.lower()

    @property
    def ident(self) -> str | None:
        """Normalized identifier for words and quoted identifiers, else None."""
        if self.kind == "word":
            return self.text.lower()
        if self.kind == "qident":
            return self.text[1:-1].replace('""', '"').lower()
        return None


def _tokenize(query: str) -> list[_Token]:
    tokens: list[_Token] = []
    for match in _TOKEN_RE.finditer(query):
        kind = match.lastgroup or "other"
        text = match.group()
        if kind == "other":
            raise SQLGuardError(f"Unsupported character {text!r} in query")
        if kind in {"ws", "line_comment", "block_comment"}:
            continue
        tokens.append(_Token(kind, text))
    return tokens


def _cte_names(tokens: list[_Token]) -> set[str]:
    """Names declared as `name AS (`, `name AS [NOT] MATERIALIZED (`, or `name (cols) AS (`."""
    names: set[str] = set()
    for index, token in enumerate(tokens):
        if token.lower != "as":
            continue
        follower = index + 1
        while follower < len(tokens) and tokens[follower].lower in {"not", "materialized"}:
            follower += 1
        if follower >= len(tokens) or tokens[follower].text != "(":
            continue
        cursor = index - 1
        if cursor >= 0 and tokens[cursor].text == ")":
            depth = 0
            while cursor >= 0:
                if tokens[cursor].text == ")":
                    depth += 1
                elif tokens[cursor].text == "(":
                    depth -= 1
                    if depth == 0:
                        break
                cursor -= 1
            cursor -= 1
        if cursor >= 0 and tokens[cursor].ident is not None:
            names.add(tokens[cursor].ident or "")
    return names


def _check_relation(tokens: list[_Token], start: int, ctes: set[str]) -> int:
    """Validate the relation starting at `start`; return the index after it (and its alias)."""
    index = start
    while index < len(tokens) and tokens[index].lower in _RELATION_PREFIX_WORDS:
        index += 1
    if index >= len(tokens):
        raise SQLGuardError("FROM/JOIN is missing a relation")
    if tokens[index].text == "(":
        return index
    name = tokens[index].ident
    if name is None:
        raise SQLGuardError(f"Unexpected token {tokens[index].text!r} after FROM/JOIN")

    qualified = index + 2 < len(tokens) and tokens[index + 1].text == "."
    if qualified:
        relation = tokens[index + 2].ident
        if name != ALLOWED_SCHEMA or relation != ALLOWED_VIEW:
            raise SQLGuardError(
                f"Relation {name}.{relation} is not allowed; "
                f"query {ALLOWED_SCHEMA}.{ALLOWED_VIEW} only"
            )
        index += 3
    else:
        if index + 1 < len(tokens) and tokens[index + 1].text == "(":
            raise SQLGuardError(f"Set-returning function {name!r} is not allowed in FROM")
        if name != ALLOWED_VIEW and name not in ctes:
            raise SQLGuardError(
                f"Relation {name!r} is not allowed; query {ALLOWED_SCHEMA}.{ALLOWED_VIEW} only"
            )
        index += 1

    if index < len(tokens) and tokens[index].lower == "as":
        index += 1
    if (
        index < len(tokens)
        and tokens[index].ident is not None
        and tokens[index].lower not in _CLAUSE_WORDS
    ):
        index += 1
    return index


def guard_sql(query: str, row_limit: int = 200) -> str:
    """Validate a model-written query and return the normalized SQL that will execute.

    Raises `SQLGuardError` with a model-readable reason when the query is not a single,
    read-only SELECT over `fde_views.vw_active_fleet`.
    """
    if not query or not query.strip():
        raise SQLGuardError("Query is empty")
    if "$" in query:
        raise SQLGuardError("Dollar-quoting and positional parameters are not allowed")

    tokens = _tokenize(query)
    while tokens and tokens[-1].text == ";":
        tokens.pop()
    if not tokens:
        raise SQLGuardError("Query is empty")
    if any(token.text == ";" for token in tokens):
        raise SQLGuardError("Only one statement is allowed")
    if tokens[0].lower not in {"select", "with"}:
        raise SQLGuardError("Only SELECT (or WITH ... SELECT) queries are allowed")

    for index, token in enumerate(tokens):
        if token.kind == "string" and index > 0 and tokens[index - 1].lower in {"e", "u", "b", "x"}:
            raise SQLGuardError("Prefixed string literals (E'', U&'', B'', X'') are not allowed")
        if token.kind != "word":
            continue
        word = token.lower
        if word in _FORBIDDEN_KEYWORDS:
            raise SQLGuardError(f"Keyword {word.upper()} is not allowed in read-only queries")
        is_call = index + 1 < len(tokens) and tokens[index + 1].text == "("
        if is_call and (
            word in _FORBIDDEN_FUNCTIONS or word.startswith(_FORBIDDEN_FUNCTION_PREFIXES)
        ):
            raise SQLGuardError(f"Function {word}() is not allowed")

    for index in range(len(tokens) - 2):
        if tokens[index + 1].text != ".":
            continue
        qualifier = tokens[index].ident
        if qualifier is None:
            continue
        if qualifier in _FORBIDDEN_SCHEMAS or qualifier.startswith("pg_"):
            raise SQLGuardError(f"Schema {qualifier!r} is not accessible")
        if qualifier == ALLOWED_SCHEMA and tokens[index + 2].ident != ALLOWED_VIEW:
            raise SQLGuardError(f"Only {ALLOWED_SCHEMA}.{ALLOWED_VIEW} is accessible")

    ctes = _cte_names(tokens)
    paren_owner: list[str] = []
    for index, token in enumerate(tokens):
        if token.text == "(":
            previous = tokens[index - 1].lower if index > 0 else ""
            paren_owner.append(previous)
            continue
        if token.text == ")":
            if paren_owner:
                paren_owner.pop()
            continue
        if token.lower == "from":
            if paren_owner and paren_owner[-1] in _FROM_FUNCTIONS:
                continue
            if index > 0 and tokens[index - 1].lower == "distinct":
                continue
            cursor = _check_relation(tokens, index + 1, ctes)
            while cursor < len(tokens) and tokens[cursor].text == ",":
                cursor = _check_relation(tokens, cursor + 1, ctes)
        elif token.lower == "join":
            _check_relation(tokens, index + 1, ctes)

    return _apply_row_limit(tokens, row_limit)


def _apply_row_limit(tokens: list[_Token], row_limit: int) -> str:
    depth = 0
    limit_index: int | None = None
    fetch_index: int | None = None
    for index, token in enumerate(tokens):
        if token.text == "(":
            depth += 1
        elif token.text == ")":
            depth -= 1
        elif depth == 0 and token.lower == "limit":
            limit_index = index
        elif depth == 0 and token.lower == "fetch":
            fetch_index = index

    texts = [token.text for token in tokens]
    if limit_index is not None:
        value_index = limit_index + 1
        value = tokens[value_index] if value_index < len(tokens) else None
        if value is not None and value.lower == "all":
            texts[value_index] = str(row_limit)
        elif value is None or value.kind != "number" or "." in value.text:
            raise SQLGuardError("LIMIT must be a positive integer literal")
        elif int(value.text) > row_limit:
            texts[value_index] = str(row_limit)
    elif fetch_index is not None:
        number = next(
            (i for i in range(fetch_index + 1, min(fetch_index + 4, len(tokens)))
             if tokens[i].kind == "number"),
            None,
        )  # fmt: skip
        if number is None:
            raise SQLGuardError("FETCH FIRST must specify a row count")
        if int(float(tokens[number].text)) > row_limit:
            texts[number] = str(row_limit)
    else:
        texts.append(f"LIMIT {row_limit}")
    return _join(texts)


def _join(texts: list[str]) -> str:
    out: list[str] = []
    for text in texts:
        if out and not (text in {")", ",", ".", "::", "]"} or out[-1] in {"(", ".", "::", "["}):
            out.append(" ")
        out.append(text)
    return "".join(out)


# --- Execution -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class QueryResult:
    columns: list[str]
    rows: list[dict[str, Any]]
    truncated: bool

    @property
    def row_count(self) -> int:
        return len(self.rows)


QueryExecutor = Callable[[str], QueryResult]


def execute_readonly(
    pool: ConnectionPool,
    query: str,
    *,
    row_limit: int,
    timeout_ms: int,
) -> QueryResult:
    """Run an already-guarded query in a read-only transaction on the least-privilege pool."""
    with pool.connection(timeout=10.0) as conn, conn.transaction():
        conn.execute("SET TRANSACTION READ ONLY")
        conn.execute(
            sql.SQL("SET LOCAL statement_timeout = {}").format(sql.Literal(f"{timeout_ms}ms"))
        )
        with conn.cursor() as cur:
            cur.execute(query.encode("utf-8"))
            columns = [column.name for column in cur.description or []]
            fetched = cur.fetchmany(row_limit + 1)
    truncated = len(fetched) > row_limit
    rows = [
        {column: to_jsonable(value) for column, value in zip(columns, row, strict=True)}
        for row in fetched[:row_limit]
    ]
    return QueryResult(columns=columns, rows=rows, truncated=truncated)


def _describe(exc: psycopg.Error) -> str:
    primary = getattr(exc.diag, "message_primary", None) or str(exc).strip()
    hint = getattr(exc.diag, "message_hint", None)
    return f"{primary} (hint: {hint})" if hint else primary


def run_sql_tool(query: str, *, executor: QueryExecutor, row_limit: int) -> ToolOutcome:
    """Guard, execute, and package one model-issued query. Never raises for SQL problems."""
    try:
        guarded = guard_sql(query, row_limit=row_limit)
    except SQLGuardError as exc:
        return ToolOutcome(
            tool_name=SQL_TOOL_NAME,
            status="blocked",
            content=f"SQL_GUARD_BLOCKED: {exc.reason}. Allowed: one SELECT over "
            f"{ALLOWED_SCHEMA}.{ALLOWED_VIEW}.",
            executed_sql=query,
            error_code="SQL_GUARD_BLOCKED",
            error=exc.reason,
        )

    try:
        result = executor(guarded)
    except pg_errors.InsufficientPrivilege as exc:
        violation = RBACViolationError(_describe(exc), sqlstate=exc.sqlstate)
        return ToolOutcome(
            tool_name=SQL_TOOL_NAME,
            status="blocked",
            content=f"RBAC_VIOLATION: {violation}. The agent role can only read "
            f"{ALLOWED_SCHEMA}.{ALLOWED_VIEW}.",
            executed_sql=guarded,
            error_code="RBAC_VIOLATION",
            error=str(violation),
        )
    except pg_errors.QueryCanceled as exc:
        return _execution_error(guarded, f"Query timed out: {_describe(exc)}")
    except (PoolTimeout, psycopg.OperationalError, psycopg.InterfaceError) as exc:
        return ToolOutcome(
            tool_name=SQL_TOOL_NAME,
            status="error",
            content="DATABASE_UNAVAILABLE: the telemetry database could not be reached.",
            executed_sql=guarded,
            error_code="DATABASE_UNAVAILABLE",
            error=str(exc),
        )
    except psycopg.Error as exc:
        return _execution_error(guarded, _describe(exc))

    preview = result.rows[:TRACE_ROW_PREVIEW]
    return ToolOutcome(
        tool_name=SQL_TOOL_NAME,
        status="ok",
        content=dumps(
            {
                "row_count": result.row_count,
                "truncated": result.truncated,
                "columns": result.columns,
                "rows": result.rows,
            }
        ),
        payload={
            "row_count": result.row_count,
            "truncated": result.truncated,
            "columns": result.columns,
            "rows": preview,
        },
        executed_sql=guarded,
    )


def _execution_error(executed_sql: str, message: str) -> ToolOutcome:
    return ToolOutcome(
        tool_name=SQL_TOOL_NAME,
        status="error",
        content=f"SQL_EXECUTION_ERROR: {message}",
        executed_sql=executed_sql,
        error_code="SQL_EXECUTION_ERROR",
        error=message,
    )


class SqlQueryInput(BaseModel):
    query: str = Field(
        min_length=6,
        max_length=4000,
        description=(
            "One PostgreSQL SELECT (or WITH ... SELECT) statement over "
            "fde_views.vw_active_fleet. No semicolons, no DML/DDL."
        ),
    )


def build_sql_tool(executor: QueryExecutor, *, row_limit: int) -> BaseTool:
    """The LangChain tool bound to the Flash model."""

    @tool(SQL_TOOL_NAME, args_schema=SqlQueryInput)
    def query_fleet_telemetry(query: str) -> ToolOutcome:
        """Run one read-only SQL SELECT against fde_views.vw_active_fleet (live fleet
        telemetry: location, reefer temperature vs target band, door, battery, ETA, status)
        and return the matching rows as JSON."""
        return run_sql_tool(query, executor=executor, row_limit=row_limit)

    return query_fleet_telemetry


def pool_executor(pool: ConnectionPool, *, row_limit: int, timeout_ms: int) -> QueryExecutor:
    return partial(execute_readonly, pool, row_limit=row_limit, timeout_ms=timeout_ms)

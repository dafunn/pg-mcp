"""pg-mcp: Postgres MCP server."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import psycopg
import yaml
from mcp.server import Server
from mcp.types import (
    CallToolRequestParams,
    CallToolResult,
    ListToolsResult,
    PaginatedRequestParams,
    TextContent,
    Tool,
)

# -- Config -----------------------------------------------------------

PG_HOST = os.getenv("PG_HOST", "localhost")
PG_PORT = int(os.getenv("PG_PORT", "5432"))
PG_USER = os.getenv("PG_USER", "")
MAX_ROWS = int(os.getenv("PG_MAX_ROWS", "200"))

EXPOSE_RAW_QUERY = os.getenv("EXPOSE_RAW_QUERY", "false").lower() in {"1", "true", "yes", "on"}
EXPOSE_METADATA = os.getenv("EXPOSE_METADATA", "false").lower() in {"1", "true", "yes", "on"}

_allowlist_raw = os.getenv("PG_ALLOWED_DATABASES", "").strip()
ALLOWED_DATABASES: frozenset[str] | None = (
    frozenset(db.strip() for db in _allowlist_raw.split(",") if db.strip())
    if _allowlist_raw
    else None
)

CURATED_QUERIES_FILE = os.getenv(
    "CURATED_QUERIES_FILE", "/etc/pg-mcp/curated-queries.yaml"
)

# -- Password ---------------------------------------------------------


def _get_password() -> str:
    pw_file = os.getenv("PG_PASSWORD_FILE", "/run/secrets/pg-password")
    p = Path(pw_file)
    if p.exists():
        return p.read_text().strip()
    pw = os.getenv("PG_PASSWORD")
    if pw:
        return pw
    raise RuntimeError(
        f"No PostgreSQL password: {pw_file} not found and PG_PASSWORD not set"
    )


# -- Curated query loading ---------------------------------------------

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_VALID_TYPES = {"string", "integer", "number", "boolean"}


def _load_curated_queries(path: str) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    with p.open() as f:
        data = yaml.safe_load(f)
    if not data or "queries" not in data:
        return []
    queries = []
    for q in data["queries"]:
        name = q.get("name", "")
        if not _NAME_RE.match(name):
            raise ValueError(f"Invalid curated query name: {name!r}")
        if not q.get("description", "").strip():
            raise ValueError(f"Curated query {name!r}: description is required")
        if not q.get("database", ""):
            raise ValueError(f"Curated query {name!r}: database is required")
        if not q.get("sql", "").strip():
            raise ValueError(f"Curated query {name!r}: sql is required")
        for param in q.get("params", []):
            if param.get("type") not in _VALID_TYPES:
                raise ValueError(
                    f"Curated query {name!r} param {param.get('name')!r}: "
                    f"type must be one of {sorted(_VALID_TYPES)}"
                )
        queries.append(
            {
                "name": name,
                "description": q["description"],
                "database": q["database"],
                "sql": q["sql"],
                "params": q.get("params", []),
            }
        )
    return queries


CURATED_QUERIES: list[dict] = _load_curated_queries(CURATED_QUERIES_FILE)

# -- Connection + formatting -------------------------------------------


def _connect(database: str = "postgres") -> psycopg.Connection:
    if not PG_USER:
        raise RuntimeError("PG_USER environment variable is required")
    return psycopg.connect(
        host=PG_HOST,
        port=PG_PORT,
        user=PG_USER,
        password=_get_password(),
        dbname=database,
        autocommit=True,
        connect_timeout=10,
    )


def _format_rows(cur: psycopg.Cursor) -> str:
    if cur.description is None:
        return f"OK ({cur.rowcount} rows affected)"

    columns = [desc[0] for desc in cur.description]
    rows = cur.fetchmany(MAX_ROWS + 1)

    truncated = len(rows) > MAX_ROWS
    if truncated:
        rows = rows[:MAX_ROWS]

    col_widths = [len(c) for c in columns]
    str_rows: list[list[str]] = []
    for row in rows:
        sr = [str(v) if v is not None else "NULL" for v in row]
        str_rows.append(sr)
        for i, val in enumerate(sr):
            col_widths[i] = max(col_widths[i], min(len(val), 80))

    header = " | ".join(c.ljust(col_widths[i]) for i, c in enumerate(columns))
    separator = "-+-".join("-" * w for w in col_widths)
    lines = [header, separator]
    for sr in str_rows:
        lines.append(" | ".join(v[:80].ljust(col_widths[i]) for i, v in enumerate(sr)))

    result = "\n".join(lines)
    result += f"\n({len(str_rows)} row{'s' if len(str_rows) != 1 else ''})"
    if truncated:
        result += (
            f"\n[TRUNCATED - showing first {MAX_ROWS} of more results. "
            "Use LIMIT to narrow.]"
        )
    return result


def _query(sql: str, database: str = "postgres", params: tuple = ()) -> str:
    """Execute SQL with positional params (%(name)s not supported here)."""
    with _connect(database) as conn:
        with conn.cursor() as cur:
            # Only pass params when non-empty: psycopg parses % as placeholder
            # even for an empty tuple, breaking literal % in SQL (e.g. ILIKE '%x%').
            if params:
                cur.execute(sql, params)
            else:
                cur.execute(sql)
            return _format_rows(cur)


def _query_named(sql: str, database: str, params: dict) -> str:
    """Execute SQL with named params %(name)s bound from a dict."""
    with _connect(database) as conn:
        with conn.cursor() as cur:
            if params:
                cur.execute(sql, params)
            else:
                cur.execute(sql)
            return _format_rows(cur)


# -- Allowlist helper -------------------------------------------------


def _check_db(database: str) -> str | None:
    """Return an error string if database is not in the allowlist, else None."""
    if ALLOWED_DATABASES is not None and database not in ALLOWED_DATABASES:
        return f"Error: database '{database}' is not in the allowed set"
    return None


# -- Built-in tool handlers --------------------------------------------

_FIRST_WORD_ALLOWED = {"SELECT", "WITH", "EXPLAIN", "SHOW", "TABLE"}


def _handle_pg_query(sql: str, database: str = "postgres") -> str:
    err = _check_db(database)
    if err:
        return err

    stripped = sql.strip().upper()
    words = stripped.split()
    first_word = words[0] if words else ""
    if first_word not in _FIRST_WORD_ALLOWED:
        return (
            f"Error: only read-only queries allowed "
            f"({', '.join(sorted(_FIRST_WORD_ALLOWED))}). Got: {first_word}"
        )

    try:
        return _query(sql, database)
    except Exception as e:
        return f"Error: {e}"


def _handle_pg_databases() -> str:
    try:
        if ALLOWED_DATABASES is not None:
            sql = """
                SELECT d.datname AS database,
                       pg_catalog.pg_get_userbyid(d.datdba) AS owner,
                       pg_catalog.pg_size_pretty(pg_catalog.pg_database_size(d.datname)) AS size
                FROM pg_catalog.pg_database d
                WHERE NOT d.datistemplate
                  AND d.datname = ANY(%s)
                ORDER BY pg_catalog.pg_database_size(d.datname) DESC
            """
            return _query(sql, "postgres", (list(ALLOWED_DATABASES),))
        else:
            sql = """
                SELECT d.datname AS database,
                       pg_catalog.pg_get_userbyid(d.datdba) AS owner,
                       pg_catalog.pg_size_pretty(pg_catalog.pg_database_size(d.datname)) AS size
                FROM pg_catalog.pg_database d
                WHERE NOT d.datistemplate
                ORDER BY pg_catalog.pg_database_size(d.datname) DESC
            """
            return _query(sql)
    except Exception as e:
        return f"Error: {e}"


def _handle_pg_tables(database: str = "postgres", schema: str = "") -> str:
    err = _check_db(database)
    if err:
        return err
    # With no schema, list tables in every non-system schema, schema-qualified; a
    # schema narrows the listing.
    if schema:
        sql = """
            SELECT n.nspname AS schema,
                   c.relname AS table,
                   pg_catalog.pg_size_pretty(pg_catalog.pg_total_relation_size(c.oid)) AS total_size,
                   c.reltuples::bigint AS estimated_rows
            FROM pg_catalog.pg_class c
            JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = %s
              AND c.relkind = 'r'
            ORDER BY pg_catalog.pg_total_relation_size(c.oid) DESC
        """
        params: tuple | None = (schema,)
    else:
        sql = """
            SELECT n.nspname AS schema,
                   c.relname AS table,
                   pg_catalog.pg_size_pretty(pg_catalog.pg_total_relation_size(c.oid)) AS total_size,
                   c.reltuples::bigint AS estimated_rows
            FROM pg_catalog.pg_class c
            JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relkind = 'r'
              AND n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
            ORDER BY pg_catalog.pg_total_relation_size(c.oid) DESC
        """
        params = None
    try:
        return _query(sql, database, params) if params else _query(sql, database)
    except Exception as e:
        return f"Error: {e}"


def _handle_pg_schema(
    table: str, database: str = "postgres", schema: str = ""
) -> str:
    err = _check_db(database)
    if err:
        return err
    # With no schema, look the table up in every non-system schema; a schema
    # narrows the lookup.
    try:
        col_sql = """
            SELECT n.nspname AS schema,
                   a.attname AS column,
                   pg_catalog.format_type(a.atttypid, a.atttypmod) AS type,
                   CASE WHEN a.attnotnull THEN 'NOT NULL' ELSE '' END AS nullable,
                   COALESCE(pg_catalog.pg_get_expr(d.adbin, d.adrelid), '') AS default
            FROM pg_catalog.pg_attribute a
            JOIN pg_catalog.pg_class c ON c.oid = a.attrelid
            JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
            LEFT JOIN pg_catalog.pg_attrdef d
                   ON d.adrelid = a.attrelid AND d.adnum = a.attnum
            WHERE c.relname = %s
              AND a.attnum > 0
              AND NOT a.attisdropped
              AND (%s = '' OR n.nspname = %s)
              AND n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
            ORDER BY n.nspname, a.attnum
        """
        result = _query(col_sql, database, (table, schema, schema))
        idx_sql = """
            SELECT schemaname AS schema, indexname AS index, indexdef AS definition
            FROM pg_indexes
            WHERE tablename = %s
              AND (%s = '' OR schemaname = %s)
              AND schemaname NOT IN ('pg_catalog', 'information_schema')
            ORDER BY schemaname, indexname
        """
        indexes = _query(idx_sql, database, (table, schema, schema))
        return f"=== Columns ===\n{result}\n\n=== Indexes ===\n{indexes}"
    except Exception as e:
        return f"Error: {e}"


def _handle_pg_size(database: str = "postgres") -> str:
    err = _check_db(database)
    if err:
        return err
    try:
        db_size_sql = (
            "SELECT pg_catalog.pg_size_pretty("
            "pg_catalog.pg_database_size(current_database())) AS db_size"
        )
        db_size = _query(db_size_sql, database)
        tables_sql = """
            SELECT n.nspname AS schema,
                   c.relname AS table,
                   pg_catalog.pg_size_pretty(pg_catalog.pg_total_relation_size(c.oid)) AS total,
                   pg_catalog.pg_size_pretty(pg_catalog.pg_relation_size(c.oid)) AS data,
                   pg_catalog.pg_size_pretty(
                       pg_catalog.pg_total_relation_size(c.oid)
                       - pg_catalog.pg_relation_size(c.oid)
                   ) AS indexes
            FROM pg_catalog.pg_class c
            JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relkind = 'r'
              AND n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
            ORDER BY pg_catalog.pg_total_relation_size(c.oid) DESC
            LIMIT 50
        """
        tables = _query(tables_sql, database)
        return f"=== Database: {database} ===\n{db_size}\n\n=== Tables by size ===\n{tables}"
    except Exception as e:
        return f"Error: {e}"


def _handle_pg_activity(database: str = "") -> str:
    conditions: list[str] = ["state != 'idle'"]
    params: list[Any] = []

    if ALLOWED_DATABASES is not None:
        conditions.append("datname = ANY(%s)")
        params.append(list(ALLOWED_DATABASES))

    if database:
        err = _check_db(database)
        if err:
            return err
        conditions.append("datname = %s")
        params.append(database)

    where = "WHERE " + " AND ".join(conditions)
    sql = f"""
        SELECT pid,
               datname AS database,
               usename AS user,
               state,
               LEFT(query, 100) AS query,
               now() - query_start AS duration
        FROM pg_stat_activity
        {where}
        ORDER BY query_start
    """
    try:
        return _query(sql, "postgres", tuple(params) if params else ())
    except Exception as e:
        return f"Error: {e}"


# -- Curated query helpers --------------------------------------------

_TYPE_MAP = {
    "string": "string",
    "integer": "integer",
    "number": "number",
    "boolean": "boolean",
}


def _make_curated_tool(spec: dict) -> Tool:
    properties: dict[str, Any] = {}
    required: list[str] = []
    for p in spec.get("params", []):
        prop: dict[str, Any] = {
            "type": _TYPE_MAP[p["type"]],
            "description": p.get("description", ""),
        }
        if "default" in p:
            prop["default"] = p["default"]
        else:
            required.append(p["name"])
        properties[p["name"]] = prop
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return Tool(name=spec["name"], description=spec["description"], inputSchema=schema)


def _run_curated(spec: dict, arguments: dict) -> str:
    err = _check_db(spec["database"])
    if err:
        return err

    bound: dict[str, Any] = {}
    for p in spec.get("params", []):
        name = p["name"]
        if name in arguments:
            bound[name] = arguments[name]
        elif "default" in p:
            bound[name] = p["default"]
        else:
            return f"Error: required parameter '{name}' was not supplied"

    try:
        return _query_named(spec["sql"], spec["database"], bound)
    except Exception as e:
        return f"Error: {e}"


# -- Static tool definitions ------------------------------------------

_PG_QUERY_TOOL = Tool(
    name="pg_query",
    description=(
        "Run a read-only SQL query against a PostgreSQL database. "
        "Allowed statements: SELECT, WITH, EXPLAIN, SHOW, TABLE. "
        "Results capped at PG_MAX_ROWS rows - use LIMIT to narrow large sets."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "sql": {
                "type": "string",
                "description": "SQL query (SELECT, WITH, EXPLAIN, SHOW, or TABLE only)",
            },
            "database": {
                "type": "string",
                "description": "Database name to connect to",
                "default": "postgres",
            },
        },
        "required": ["sql"],
    },
)

_METADATA_TOOLS: list[Tool] = [
    Tool(
        name="pg_databases",
        description=(
            "List PostgreSQL databases with their sizes and owners. "
            "When PG_ALLOWED_DATABASES is set, only allowed databases are shown."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="pg_tables",
        description=(
            "List tables with row counts and sizes. Omit `schema` to list tables "
            "across every schema (schema-qualified) - important because many DBs "
            "keep their tables in a named schema, not `public`. Pass `schema` to "
            "narrow to one."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "database": {
                    "type": "string",
                    "description": "Database name",
                    "default": "postgres",
                },
                "schema": {
                    "type": "string",
                    "description": "Schema name; omit to list all non-system schemas",
                    "default": "",
                },
            },
        },
    ),
    Tool(
        name="pg_schema",
        description=(
            "Describe a table's columns, types, constraints, and indexes. Omit "
            "`schema` to locate the table in any non-system schema (not just "
            "`public`)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "table": {"type": "string", "description": "Table name"},
                "database": {
                    "type": "string",
                    "description": "Database name",
                    "default": "postgres",
                },
                "schema": {
                    "type": "string",
                    "description": "Schema name; omit to search all non-system schemas",
                    "default": "",
                },
            },
            "required": ["table"],
        },
    ),
    Tool(
        name="pg_size",
        description="Show table sizes in a database, ordered by total size.",
        inputSchema={
            "type": "object",
            "properties": {
                "database": {
                    "type": "string",
                    "description": "Database name",
                    "default": "postgres",
                },
            },
        },
    ),
    Tool(
        name="pg_activity",
        description=(
            "Show active database connections and running queries. "
            "When PG_ALLOWED_DATABASES is set, output is filtered to the allowed set."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "database": {
                    "type": "string",
                    "description": "Filter to a specific database (empty = all allowed databases)",
                    "default": "",
                },
            },
        },
    ),
]

# -- MCP server -------------------------------------------------------

_server = Server("pg-mcp")


async def list_tools(_ctx: Any, _params: PaginatedRequestParams) -> ListToolsResult:
    tools: list[Tool] = []
    if EXPOSE_RAW_QUERY:
        tools.append(_PG_QUERY_TOOL)
    if EXPOSE_METADATA:
        tools.extend(_METADATA_TOOLS)
    tools.extend(_make_curated_tool(q) for q in CURATED_QUERIES)
    return ListToolsResult(tools=tools)


async def call_tool(_ctx: Any, params: CallToolRequestParams) -> CallToolResult:
    result = _dispatch(params.name, params.arguments or {})
    return CallToolResult(content=[TextContent(type="text", text=result)])

# Each handler is registered with its wire method and params model; the runner
# validates a request against the model before calling the handler.
_server.add_request_handler("tools/list", PaginatedRequestParams, list_tools)
_server.add_request_handler("tools/call", CallToolRequestParams, call_tool)


def _dispatch(name: str, args: dict) -> str:
    if name == "pg_query":
        if not EXPOSE_RAW_QUERY:
            return f"Error: tool '{name}' is not enabled on this instance"
        return _handle_pg_query(args.get("sql", ""), args.get("database", "postgres"))

    if name == "pg_databases":
        if not EXPOSE_METADATA:
            return f"Error: tool '{name}' is not enabled on this instance"
        return _handle_pg_databases()

    if name == "pg_tables":
        if not EXPOSE_METADATA:
            return f"Error: tool '{name}' is not enabled on this instance"
        return _handle_pg_tables(
            args.get("database", "postgres"), args.get("schema", "")
        )

    if name == "pg_schema":
        if not EXPOSE_METADATA:
            return f"Error: tool '{name}' is not enabled on this instance"
        table = args.get("table")
        if not table:
            return "Error: 'table' parameter is required"
        return _handle_pg_schema(
            table, args.get("database", "postgres"), args.get("schema", "")
        )

    if name == "pg_size":
        if not EXPOSE_METADATA:
            return f"Error: tool '{name}' is not enabled on this instance"
        return _handle_pg_size(args.get("database", "postgres"))

    if name == "pg_activity":
        if not EXPOSE_METADATA:
            return f"Error: tool '{name}' is not enabled on this instance"
        return _handle_pg_activity(args.get("database", ""))

    for q in CURATED_QUERIES:
        if q["name"] == name:
            return _run_curated(q, args)

    return f"Error: unknown tool '{name}'"


# -- Entry point ------------------------------------------------------

if __name__ == "__main__":
    transport = os.getenv("MCP_TRANSPORT", "stdio")
    if transport == "sse":
        from mcp.server.sse import SseServerTransport
        from starlette.applications import Starlette
        from starlette.requests import Request
        from starlette.responses import Response
        from starlette.routing import Mount, Route
        import uvicorn

        host = os.getenv("MCP_HOST", "0.0.0.0")
        port = int(os.getenv("MCP_PORT", "3000"))

        sse = SseServerTransport("/messages/")

        async def handle_sse(request: Request) -> Response:
            async with sse.connect_sse(
                request.scope, request.receive, request._send
            ) as (read_stream, write_stream):
                await _server.run(
                    read_stream,
                    write_stream,
                    _server.create_initialization_options(),
                )
            # Starlette >= 1.0 calls the endpoint's return value as an ASGI app; the SSE
            # handler has already sent the response, so return an empty Response.
            return Response()

        web_app = Starlette(
            routes=[
                Route("/sse", endpoint=handle_sse),
                Mount("/messages/", app=sse.handle_post_message),
            ]
        )
        uvicorn.run(web_app, host=host, port=port)
    else:
        import asyncio
        from mcp.server.stdio import stdio_server

        async def _run_stdio() -> None:
            async with stdio_server() as (read_stream, write_stream):
                await _server.run(
                    read_stream,
                    write_stream,
                    _server.create_initialization_options(),
                )

        asyncio.run(_run_stdio())

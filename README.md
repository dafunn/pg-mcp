# pg-mcp

Postgres MCP server. Each deployment connects as one Postgres role and exposes
only the tools its configuration enables.

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `PG_HOST` | `localhost` | Postgres host |
| `PG_PORT` | `5432` | Postgres port |
| `PG_USER` | _(required)_ | Role to connect as |
| `PG_PASSWORD_FILE` | `/run/secrets/pg-password` | Path to password file (mounted secret) |
| `PG_PASSWORD` | - | Password fallback for local dev |
| `PG_MAX_ROWS` | `200` | Row cap per result |
| `EXPOSE_RAW_QUERY` | `false` | Enable `pg_query` (arbitrary read-only SQL) |
| `EXPOSE_METADATA` | `false` | Enable `pg_databases`, `pg_tables`, `pg_schema`, `pg_size`, `pg_activity` |
| `PG_ALLOWED_DATABASES` | - | Comma-separated allowlist of database names |
| `CURATED_QUERIES_FILE` | `/etc/pg-mcp/curated-queries.yaml` | Path to curated query registry |
| `MCP_TRANSPORT` | `stdio` | Set to `sse` for in-cluster HTTP/SSE |
| `MCP_HOST` | `0.0.0.0` | SSE bind host |
| `MCP_PORT` | `3000` | SSE bind port |

Truthy values for boolean flags: `1`, `true`, `yes`, `on` (case-insensitive).

### Database allowlist

When `PG_ALLOWED_DATABASES` is set:
- **Argument-bearing tools** (`pg_query`, `pg_tables`, `pg_schema`, `pg_size`) reject
  any `database` argument not in the list.
- **Enumeration tools** (`pg_databases`, `pg_activity`) filter their output to the
  allowed set rather than erroring.

## Curated query registry

Mount a YAML file at `CURATED_QUERIES_FILE`. Each entry becomes an MCP tool:

```yaml
queries:
  - name: appdb_recent_events             # MCP tool name: ^[a-z][a-z0-9_]*$
    description: "Recent events"         # shown to the LLM - required
    database: appdb                      # must pass PG_ALLOWED_DATABASES if set
    params:
      - name: hours
        type: integer                    # string | integer | number | boolean
        default: 24
        description: "Hours to look back"
      - name: limit
        type: integer
        default: 50
        description: "Row limit"
    sql: >
      SELECT * FROM events
      WHERE created_at > now() - make_interval(hours => %(hours)s)
      LIMIT %(limit)s
```

Params with `default` are optional; params without are required. Use `%(name)s`
placeholders - params are bound via psycopg named binding, never string-interpolated.
Literal `%` in SQL (e.g. `ILIKE '%%foo%%'`) must be escaped as `%%`.

## Running locally

```bash
export PG_USER=myuser
echo mypassword > /tmp/pg-password && export PG_PASSWORD_FILE=/tmp/pg-password
export EXPOSE_RAW_QUERY=true
python server.py
```

`PG_PASSWORD` (plain env var) also works as a simpler alternative for local dev.

## Image

Build the image from the included `Dockerfile` (`docker build .` or
`podman build .`).

## Running tests

Requires Python 3.14 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --frozen --extra dev
.venv/bin/python -m pytest tests/ -v
```

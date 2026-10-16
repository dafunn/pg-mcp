"""pg-mcp unit tests - hermetic, no real DB required."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, call, patch

import pytest
from mcp.types import CallToolRequestParams

import server as srv


# -- Helpers ----------------------------------------------------------


def run(coro):
    return asyncio.run(coro)


def _make_mock_conn(description=None, rowcount=0, rows=None):
    """Return (conn, cursor) mocks suitable for use as _connect() return value."""
    mock_cur = MagicMock()
    mock_cur.description = description
    mock_cur.rowcount = rowcount
    mock_cur.fetchmany.return_value = rows or []

    mock_conn = MagicMock()
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    return mock_conn, mock_cur


# -- Capability flags --------------------------------------------------


class TestListTools:
    def test_both_flags_off_no_builtins(self):
        with (
            patch.object(srv, "EXPOSE_RAW_QUERY", False),
            patch.object(srv, "EXPOSE_METADATA", False),
            patch.object(srv, "CURATED_QUERIES", []),
        ):
            tools = run(srv.list_tools(None, None)).tools
            assert tools == []

    def test_raw_query_flag_on_includes_pg_query(self):
        with (
            patch.object(srv, "EXPOSE_RAW_QUERY", True),
            patch.object(srv, "EXPOSE_METADATA", False),
            patch.object(srv, "CURATED_QUERIES", []),
        ):
            tools = run(srv.list_tools(None, None)).tools
            names = [t.name for t in tools]
            assert "pg_query" in names
            assert "pg_databases" not in names

    def test_metadata_flag_on_includes_all_five(self):
        with (
            patch.object(srv, "EXPOSE_RAW_QUERY", False),
            patch.object(srv, "EXPOSE_METADATA", True),
            patch.object(srv, "CURATED_QUERIES", []),
        ):
            tools = run(srv.list_tools(None, None)).tools
            names = [t.name for t in tools]
            for expected in ("pg_databases", "pg_tables", "pg_schema", "pg_size", "pg_activity"):
                assert expected in names
            assert "pg_query" not in names

    def test_raw_flag_off_excludes_pg_query(self):
        with (
            patch.object(srv, "EXPOSE_RAW_QUERY", False),
            patch.object(srv, "EXPOSE_METADATA", True),
            patch.object(srv, "CURATED_QUERIES", []),
        ):
            tools = run(srv.list_tools(None, None)).tools
            assert not any(t.name == "pg_query" for t in tools)

    def test_curated_always_present_when_defined(self):
        spec = {
            "name": "test_q",
            "description": "A test query",
            "database": "appdb",
            "sql": "SELECT 1",
            "params": [],
        }
        with (
            patch.object(srv, "EXPOSE_RAW_QUERY", False),
            patch.object(srv, "EXPOSE_METADATA", False),
            patch.object(srv, "CURATED_QUERIES", [spec]),
        ):
            tools = run(srv.list_tools(None, None)).tools
            assert len(tools) == 1
            assert tools[0].name == "test_q"

    def test_all_enabled_with_curated(self):
        spec = {
            "name": "my_query",
            "description": "desc",
            "database": "appdb",
            "sql": "SELECT 1",
            "params": [],
        }
        with (
            patch.object(srv, "EXPOSE_RAW_QUERY", True),
            patch.object(srv, "EXPOSE_METADATA", True),
            patch.object(srv, "CURATED_QUERIES", [spec]),
        ):
            tools = run(srv.list_tools(None, None)).tools
            names = [t.name for t in tools]
            assert "pg_query" in names
            assert "pg_databases" in names
            assert "my_query" in names


# -- Curated registry --------------------------------------------------


class TestCuratedRegistry:
    def test_input_schema_types_and_required(self):
        spec = {
            "name": "recent_events",
            "description": "Recent events",
            "database": "appdb",
            "sql": "SELECT 1",
            "params": [
                {"name": "hours", "type": "integer", "default": 24, "description": "hours back"},
                {"name": "region", "type": "string", "description": "region (required)"},
            ],
        }
        tool = srv._make_curated_tool(spec)

        assert tool.name == "recent_events"
        assert tool.description == "Recent events"
        schema = tool.input_schema
        assert schema["properties"]["hours"]["type"] == "integer"
        assert schema["properties"]["hours"]["default"] == 24
        assert schema["properties"]["region"]["type"] == "string"
        # region has no default -> required; hours has default -> not required
        assert "region" in schema.get("required", [])
        assert "hours" not in schema.get("required", [])

    def test_no_required_when_all_have_defaults(self):
        spec = {
            "name": "q",
            "description": "d",
            "database": "db",
            "sql": "SELECT 1",
            "params": [
                {"name": "n", "type": "integer", "default": 10, "description": "n"},
            ],
        }
        tool = srv._make_curated_tool(spec)
        assert "required" not in tool.input_schema

    def test_empty_params_produces_empty_schema(self):
        spec = {
            "name": "q",
            "description": "d",
            "database": "db",
            "sql": "SELECT 1",
            "params": [],
        }
        tool = srv._make_curated_tool(spec)
        assert tool.input_schema["properties"] == {}

    def test_call_binds_named_params_to_query_named(self):
        spec = {
            "name": "recent",
            "description": "desc",
            "database": "appdb",
            "sql": "SELECT * FROM t WHERE h > %(hours)s",
            "params": [
                {"name": "hours", "type": "integer", "default": 24, "description": "h"},
            ],
        }
        with (
            patch.object(srv, "ALLOWED_DATABASES", None),
            patch("server._query_named") as mock_qn,
        ):
            mock_qn.return_value = "1 row"
            srv._run_curated(spec, {"hours": 12})
            mock_qn.assert_called_once_with(
                "SELECT * FROM t WHERE h > %(hours)s", "appdb", {"hours": 12}
            )

    def test_call_fills_defaults_for_omitted_params(self):
        spec = {
            "name": "recent",
            "description": "desc",
            "database": "appdb",
            "sql": "SELECT 1 LIMIT %(limit)s",
            "params": [
                {"name": "limit", "type": "integer", "default": 50, "description": "l"},
            ],
        }
        with (
            patch.object(srv, "ALLOWED_DATABASES", None),
            patch("server._query_named") as mock_qn,
        ):
            mock_qn.return_value = "1 row"
            srv._run_curated(spec, {})
            mock_qn.assert_called_once_with(spec["sql"], "appdb", {"limit": 50})

    def test_missing_required_param_returns_error_without_connecting(self):
        spec = {
            "name": "q",
            "description": "d",
            "database": "appdb",
            "sql": "SELECT %(region)s",
            "params": [
                {"name": "region", "type": "string", "description": "region (no default)"},
            ],
        }
        with (
            patch.object(srv, "ALLOWED_DATABASES", None),
            patch("server._connect") as mock_connect,
        ):
            result = srv._run_curated(spec, {})
            assert "Error" in result
            assert "region" in result
            mock_connect.assert_not_called()

    def test_params_bound_as_dict_not_interpolated_into_sql(self):
        """The SQL string passed to execute() must be unchanged; params go as a dict."""
        sql = "SELECT %(val)s AS v"
        spec = {
            "name": "q",
            "description": "d",
            "database": "appdb",
            "sql": sql,
            "params": [{"name": "val", "type": "string", "description": "v"}],
        }
        mock_conn, mock_cur = _make_mock_conn(description=None, rowcount=1)
        with (
            patch.object(srv, "ALLOWED_DATABASES", None),
            patch("server._connect", return_value=mock_conn),
        ):
            srv._run_curated(spec, {"val": "safe_value"})
            mock_cur.execute.assert_called_once_with(sql, {"val": "safe_value"})


# -- DB allowlist ------------------------------------------------------


class TestAllowlist:
    def test_pg_query_rejects_off_list_db_without_connecting(self):
        with (
            patch.object(srv, "ALLOWED_DATABASES", frozenset({"appdb"})),
            patch("server._connect") as mock_connect,
        ):
            result = srv._handle_pg_query("SELECT 1", "otherdb")
            assert "not in the allowed set" in result
            mock_connect.assert_not_called()

    def test_pg_query_allows_listed_db(self):
        with (
            patch.object(srv, "ALLOWED_DATABASES", frozenset({"appdb"})),
            patch("server._query") as mock_q,
        ):
            mock_q.return_value = "1 row"
            srv._handle_pg_query("SELECT 1", "appdb")
            mock_q.assert_called_once()

    def test_no_allowlist_passes_any_db(self):
        with (
            patch.object(srv, "ALLOWED_DATABASES", None),
            patch("server._query") as mock_q,
        ):
            mock_q.return_value = "1 row"
            srv._handle_pg_query("SELECT 1", "any_database")
            mock_q.assert_called_once()

    def test_pg_tables_rejects_off_list_db(self):
        with (
            patch.object(srv, "ALLOWED_DATABASES", frozenset({"appdb"})),
            patch("server._connect") as mock_connect,
        ):
            result = srv._handle_pg_tables("otherdb")
            assert "not in the allowed set" in result
            mock_connect.assert_not_called()

    def test_pg_schema_rejects_off_list_db(self):
        with (
            patch.object(srv, "ALLOWED_DATABASES", frozenset({"appdb"})),
            patch("server._connect") as mock_connect,
        ):
            result = srv._handle_pg_schema("users", "otherdb")
            assert "not in the allowed set" in result
            mock_connect.assert_not_called()

    def test_pg_size_rejects_off_list_db(self):
        with (
            patch.object(srv, "ALLOWED_DATABASES", frozenset({"appdb"})),
            patch("server._connect") as mock_connect,
        ):
            result = srv._handle_pg_size("otherdb")
            assert "not in the allowed set" in result
            mock_connect.assert_not_called()

    def test_pg_tables_lists_all_schemas_when_unspecified(self):
        # With no schema, pg_tables lists every non-system schema, schema-qualified,
        # and passes no schema param.
        with patch("server._query") as mock_q:
            mock_q.return_value = "rows"
            srv._handle_pg_tables("appdb")
            sql = mock_q.call_args.args[0]
            assert "n.nspname = %s" not in sql
            assert "n.nspname AS schema" in sql
            assert "not in ('pg_catalog', 'information_schema', 'pg_toast')" in sql.lower()
            assert len(mock_q.call_args.args) == 2  # (sql, database) - no params

    def test_pg_tables_narrows_to_given_schema(self):
        with patch("server._query") as mock_q:
            mock_q.return_value = "rows"
            srv._handle_pg_tables("appdb", "appdb")
            assert "n.nspname = %s" in mock_q.call_args.args[0]
            assert mock_q.call_args.args[2] == ("appdb",)

    def test_pg_schema_searches_all_schemas_when_unspecified(self):
        with patch("server._query") as mock_q:
            mock_q.return_value = "rows"
            srv._handle_pg_schema("events", "appdb")  # no schema given
            col_sql = mock_q.call_args_list[0].args[0]
            assert "(%s = '' OR n.nspname = %s)" in col_sql
            assert mock_q.call_args_list[0].args[2] == ("events", "", "")

    def test_curated_rejects_off_list_db_without_connecting(self):
        spec = {
            "name": "q",
            "description": "d",
            "database": "otherdb",
            "sql": "SELECT 1",
            "params": [],
        }
        with (
            patch.object(srv, "ALLOWED_DATABASES", frozenset({"appdb"})),
            patch("server._connect") as mock_connect,
        ):
            result = srv._run_curated(spec, {})
            assert "not in the allowed set" in result
            mock_connect.assert_not_called()

    def test_pg_databases_filters_to_allowlist(self):
        """pg_databases with allowlist set passes the list to `ANY(%s)`, not an error."""
        with (
            patch.object(srv, "ALLOWED_DATABASES", frozenset({"appdb", "seconddb"})),
            patch("server._query") as mock_q,
        ):
            mock_q.return_value = "2 rows"
            srv._handle_pg_databases()
            # Should be called with the allowed list as a positional param
            args, kwargs = mock_q.call_args
            assert args[2] is not None  # params tuple passed
            passed_list = args[2][0]
            assert set(passed_list) == {"appdb", "seconddb"}

    def test_pg_databases_no_allowlist_no_params(self):
        with (
            patch.object(srv, "ALLOWED_DATABASES", None),
            patch("server._query") as mock_q,
        ):
            mock_q.return_value = "5 rows"
            srv._handle_pg_databases()
            # Called with no params (no filtering)
            args, kwargs = mock_q.call_args
            assert len(args) == 1  # only sql, no database/params

    def test_pg_activity_filters_to_allowlist(self):
        with (
            patch.object(srv, "ALLOWED_DATABASES", frozenset({"appdb"})),
            patch("server._query") as mock_q,
        ):
            mock_q.return_value = "1 row"
            srv._handle_pg_activity()
            args, _ = mock_q.call_args
            sql = args[0]
            assert "ANY(%s)" in sql
            params = args[2]
            assert ["appdb"] in params or list(params[0]) == ["appdb"]

    def test_pg_activity_specific_db_off_list_returns_error(self):
        with (
            patch.object(srv, "ALLOWED_DATABASES", frozenset({"appdb"})),
            patch("server._connect") as mock_connect,
        ):
            result = srv._handle_pg_activity("otherdb")
            assert "not in the allowed set" in result
            mock_connect.assert_not_called()


# -- pg_query first-word allowlist -------------------------------------


class TestPgQueryFirstWordAllowlist:
    @pytest.mark.parametrize(
        "sql",
        [
            "DROP TABLE users",
            "INSERT INTO t VALUES (1)",
            "UPDATE t SET x=1",
            "DELETE FROM t",
            "CREATE TABLE x (id int)",
            "TRUNCATE users",
            "ALTER TABLE t ADD COLUMN x int",
        ],
    )
    def test_blocks_write_statements(self, sql):
        with (
            patch.object(srv, "ALLOWED_DATABASES", None),
            patch("server._connect") as mock_connect,
        ):
            result = srv._handle_pg_query(sql, "postgres")
            assert "Error" in result
            mock_connect.assert_not_called()

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT 1",
            "select * from users",
            "WITH cte AS (SELECT 1) SELECT * FROM cte",
            "EXPLAIN SELECT 1",
            "SHOW search_path",
            "TABLE users",
        ],
    )
    def test_allows_read_statements(self, sql):
        with (
            patch.object(srv, "ALLOWED_DATABASES", None),
            patch("server._query") as mock_q,
        ):
            mock_q.return_value = "1 row"
            srv._handle_pg_query(sql, "postgres")
            mock_q.assert_called_once()

    def test_empty_sql_returns_error(self):
        with patch.object(srv, "ALLOWED_DATABASES", None):
            result = srv._handle_pg_query("", "postgres")
            assert "Error" in result

    def test_whitespace_only_sql_returns_error(self):
        with patch.object(srv, "ALLOWED_DATABASES", None):
            result = srv._handle_pg_query("   ", "postgres")
            assert "Error" in result


# -- Dispatch gating ---------------------------------------------------


class TestDispatch:
    def test_pg_query_disabled_when_flag_off(self):
        with patch.object(srv, "EXPOSE_RAW_QUERY", False):
            result = srv._dispatch("pg_query", {"sql": "SELECT 1", "database": "postgres"})
            assert "not enabled" in result

    def test_metadata_tool_disabled_when_flag_off(self):
        with patch.object(srv, "EXPOSE_METADATA", False):
            for name in ("pg_databases", "pg_tables", "pg_schema", "pg_size", "pg_activity"):
                result = srv._dispatch(name, {})
                assert "not enabled" in result, f"{name} should be disabled"

    def test_unknown_tool_returns_error(self):
        with patch.object(srv, "CURATED_QUERIES", []):
            result = srv._dispatch("nonexistent_tool", {})
            assert "unknown tool" in result

    def test_curated_tool_dispatched_by_name(self):
        spec = {
            "name": "my_curated",
            "description": "d",
            "database": "appdb",
            "sql": "SELECT 1",
            "params": [],
        }
        with (
            patch.object(srv, "CURATED_QUERIES", [spec]),
            patch("server._run_curated") as mock_rc,
        ):
            mock_rc.return_value = "ok"
            srv._dispatch("my_curated", {})
            mock_rc.assert_called_once_with(spec, {})


# -- Error results carry isError -----------------------------------------------


def _call(name, args):
    return run(srv.call_tool(None, CallToolRequestParams(name=name, arguments=args)))


class TestErrorResults:
    def test_database_refusal_is_an_error_result(self):
        with (
            patch.object(srv, "EXPOSE_RAW_QUERY", True),
            patch.object(srv, "ALLOWED_DATABASES", frozenset({"appdb"})),
        ):
            result = _call("pg_query", {"sql": "SELECT 1", "database": "otherdb"})
        assert result.is_error is True
        assert result.content[0].text == "Error: database 'otherdb' is not in the allowed set"

    def test_write_query_refusal_is_an_error_result(self):
        with (
            patch.object(srv, "EXPOSE_RAW_QUERY", True),
            patch.object(srv, "ALLOWED_DATABASES", None),
        ):
            result = _call("pg_query", {"sql": "DROP TABLE t", "database": "postgres"})
        assert result.is_error is True
        assert "only read-only queries allowed" in result.content[0].text

    def test_metadata_missing_argument_is_an_error_result(self):
        with patch.object(srv, "EXPOSE_METADATA", True):
            result = _call("pg_schema", {})
        assert result.is_error is True
        assert result.content[0].text == "Error: 'table' parameter is required"

    def test_query_failure_is_an_error_result(self):
        with (
            patch.object(srv, "EXPOSE_RAW_QUERY", True),
            patch.object(srv, "ALLOWED_DATABASES", None),
            patch("server._query", side_effect=RuntimeError("connection refused")),
        ):
            result = _call("pg_query", {"sql": "SELECT 1", "database": "postgres"})
        assert result.is_error is True
        assert result.content[0].text == "Error: connection refused"

    def test_curated_missing_parameter_is_an_error_result(self):
        spec = {"name": "recent", "description": "d", "database": "appdb",
                "sql": "SELECT %(hours)s", "params": [{"name": "hours", "type": "integer",
                                                       "description": "h"}]}
        with (
            patch.object(srv, "CURATED_QUERIES", [spec]),
            patch.object(srv, "ALLOWED_DATABASES", None),
        ):
            result = _call("recent", {})
        assert result.is_error is True
        assert result.content[0].text == "Error: required parameter 'hours' was not supplied"

    def test_unknown_tool_is_an_error_result(self):
        with patch.object(srv, "CURATED_QUERIES", []):
            result = _call("pg_nope", {})
        assert result.is_error is True
        assert result.content[0].text == "Error: unknown tool 'pg_nope'"

    def test_a_row_reading_like_an_error_is_not_an_error(self):
        with (
            patch.object(srv, "EXPOSE_RAW_QUERY", True),
            patch.object(srv, "ALLOWED_DATABASES", None),
            patch("server._query", return_value="Error: disk full"),
        ):
            result = _call("pg_query", {"sql": "SELECT msg FROM log", "database": "postgres"})
        assert not result.is_error
        assert result.content[0].text == "Error: disk full"


# -- Release version ------------------------------------------------


def test_release_version_is_dev_when_unset(monkeypatch):
    monkeypatch.delenv("PG_MCP_VERSION", raising=False)
    assert srv.release_version() == "dev"


def test_release_version_is_dev_when_blank(monkeypatch):
    monkeypatch.setenv("PG_MCP_VERSION", "  ")
    assert srv.release_version() == "dev"


def test_release_version_reports_the_build_value(monkeypatch):
    monkeypatch.setenv("PG_MCP_VERSION", "0.2.0-3-gabc1234")
    assert srv.release_version() == "0.2.0-3-gabc1234"


def test_server_reports_the_release_version():
    assert srv._server.version == srv.release_version()

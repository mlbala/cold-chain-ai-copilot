"""The read-only SQL guard: what the agent may run against PostgreSQL."""

from __future__ import annotations

import pytest

from cold_chain_copilot.agent.tools.sql_telemetry import guard_sql, run_sql_tool
from cold_chain_copilot.core.exceptions import SQLGuardError
from tests.conftest import fake_executor

ALLOWED = [
    "SELECT shipment_id, reefer_temp_c FROM fde_views.vw_active_fleet "
    "WHERE reefer_temp_c > target_max_temp_c OR reefer_temp_c < target_min_temp_c",
    "WITH hot AS (SELECT * FROM fde_views.vw_active_fleet WHERE reefer_temp_c > 8) "
    "SELECT count(*) FROM hot",
    "WITH recent (id, temp) AS MATERIALIZED (SELECT shipment_id, reefer_temp_c "
    "FROM fde_views.vw_active_fleet) SELECT id FROM recent",
    "SELECT shipment_id, EXTRACT(EPOCH FROM (now() - last_updated_utc)) / 60 AS minutes "
    "FROM fde_views.vw_active_fleet",
    "select f.truck_id from vw_active_fleet f join fde_views.vw_active_fleet g "
    "on f.truck_id = g.truck_id",
    "SELECT shipment_id FROM fde_views.vw_active_fleet WHERE driver_name = 'Ryan O''Connell';",
    "SELECT * FROM fde_views.vw_active_fleet -- trailing note: DROP TABLE is blocked\n",
    "SELECT cargo_type, count(*) FILTER (WHERE shipment_status = 'DELAYED_WEATHER') "
    "FROM fde_views.vw_active_fleet GROUP BY cargo_type ORDER BY 2 DESC",
    "SELECT shipment_id FROM fde_views.vw_active_fleet WHERE door_status IS DISTINCT FROM 'CLOSED'",
    "SELECT shipment_id FROM fde_views.vw_active_fleet WHERE driver_name = 'please delete me'",
    "SELECT shipment_id FROM fde_views.vw_active_fleet ORDER BY eta_hours FETCH FIRST 5 ROWS ONLY",
    'SELECT "shipment_id" FROM "fde_views"."vw_active_fleet"',
    "SELECT shipment_id, reefer_temp_c::numeric(5, 1) FROM fde_views.vw_active_fleet",
    "SELECT shipment_id FROM fde_views.vw_active_fleet "
    "WHERE shipment_id IN "
    "(SELECT shipment_id FROM fde_views.vw_active_fleet WHERE battery_pct < 25)",
]

BLOCKED = [
    ("DROP VIEW fde_views.vw_active_fleet", "Only SELECT"),
    ("DELETE FROM fde_views.agent_audit_log", "Only SELECT"),
    ("UPDATE public.legacy_fleet_shipments SET internal_margin_usd = 0", "Only SELECT"),
    ("SELECT * FROM public.legacy_fleet_shipments", "public"),
    ("SELECT * FROM legacy_fleet_shipments", "not allowed"),
    ("SELECT * FROM fde_views.agent_audit_log", "vw_active_fleet"),
    ("SELECT 1; DROP TABLE fde_views.agent_audit_log", "one statement"),
    ("SELECT 1 /* hidden */ ; SELECT 2", "one statement"),
    ("SELECT pg_sleep(10)", "pg_sleep"),
    ("SELECT * FROM pg_catalog.pg_roles", "pg_catalog"),
    ("SELECT * FROM information_schema.tables", "information_schema"),
    ("SELECT * INTO stolen FROM fde_views.vw_active_fleet", "INTO"),
    (
        "WITH d AS (DELETE FROM fde_views.agent_audit_log RETURNING *) SELECT * FROM d",
        "DELETE",
    ),
    ("SELECT query_to_xml('select * from public.legacy_fleet_shipments', true, true, '')", ""),
    ("SELECT * FROM fde_views.vw_active_fleet FOR UPDATE", "UPDATE"),
    ("SELECT $$x$$", "Dollar"),
    ("SELECT * FROM generate_series(1, 10)", "Set-returning"),
    (
        "SELECT * FROM fde_views.vw_active_fleet a, public.legacy_fleet_shipments b",
        "public",
    ),
    ('SELECT * FROM "public"."legacy_fleet_shipments"', "public"),
    ("SELECT E'abc' FROM fde_views.vw_active_fleet", "Prefixed"),
    ("SELECT E'\\'' FROM fde_views.vw_active_fleet", "Unsupported"),
    ("SELECT set_config('role', 'avnadmin', false)", "SET"),
    ("SELECT * FROM fde_views.vw_active_fleet LIMIT (SELECT 5)", "LIMIT"),
    ("EXPLAIN ANALYZE SELECT 1", "Only SELECT"),
    ("SELECT * FROM fde_views.vw_active_fleet WHERE truck_id = 'x' & 1", "Unsupported"),
    ("", "empty"),
    ("  ;  ", "empty"),
]


@pytest.mark.parametrize("query", ALLOWED)
def test_allows_read_only_view_queries(query: str) -> None:
    guarded = guard_sql(query)
    assert guarded.upper().startswith(("SELECT", "WITH"))


@pytest.mark.parametrize(("query", "reason"), BLOCKED)
def test_blocks_dml_ddl_and_restricted_relations(query: str, reason: str) -> None:
    with pytest.raises(SQLGuardError) as excinfo:
        guard_sql(query)
    assert reason.lower() in excinfo.value.reason.lower()


def test_appends_row_limit_when_missing() -> None:
    guarded = guard_sql("SELECT shipment_id FROM fde_views.vw_active_fleet", row_limit=200)
    assert guarded.endswith("LIMIT 200")


def test_clamps_oversized_limit_and_keeps_small_limit() -> None:
    assert guard_sql("SELECT * FROM vw_active_fleet LIMIT 5000", row_limit=200).endswith(
        "LIMIT 200"
    )
    assert guard_sql("SELECT * FROM vw_active_fleet LIMIT 5", row_limit=200).endswith("LIMIT 5")
    assert guard_sql("SELECT * FROM vw_active_fleet LIMIT ALL", row_limit=50).endswith("LIMIT 50")


def test_limit_inside_subquery_does_not_count_as_top_level() -> None:
    guarded = guard_sql(
        "SELECT * FROM fde_views.vw_active_fleet WHERE shipment_id IN "
        "(SELECT shipment_id FROM fde_views.vw_active_fleet LIMIT 3)",
        row_limit=100,
    )
    assert guarded.endswith("LIMIT 100")


def test_strips_comments_and_semicolons_but_preserves_literals() -> None:
    guarded = guard_sql(
        "SELECT shipment_id /* id */ FROM fde_views.vw_active_fleet "
        "WHERE driver_name = 'Ryan O''Connell' -- note\n;"
    )
    assert "/*" not in guarded
    assert "--" not in guarded
    assert ";" not in guarded
    assert "'Ryan O''Connell'" in guarded


def test_tool_reports_guard_block_with_error_code() -> None:
    outcome = run_sql_tool(
        "SELECT internal_margin_usd FROM public.legacy_fleet_shipments",
        executor=fake_executor,
        row_limit=200,
    )
    assert outcome.status == "blocked"
    assert outcome.error_code == "SQL_GUARD_BLOCKED"
    assert outcome.is_fixable_sql_failure


def test_tool_reports_execution_error_with_error_code() -> None:
    outcome = run_sql_tool(
        "SELECT bad_column FROM fde_views.vw_active_fleet",
        executor=fake_executor,
        row_limit=200,
    )
    assert outcome.status == "error"
    assert outcome.error_code == "SQL_EXECUTION_ERROR"
    assert "bad_column" in (outcome.error or "")


def test_tool_returns_rows_and_executed_sql() -> None:
    outcome = run_sql_tool(
        "SELECT shipment_id FROM fde_views.vw_active_fleet",
        executor=fake_executor,
        row_limit=200,
    )
    assert outcome.status == "ok"
    assert outcome.executed_sql is not None
    assert outcome.executed_sql.endswith("LIMIT 200")
    assert outcome.payload["row_count"] == 2

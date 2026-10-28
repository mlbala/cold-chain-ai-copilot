"""Admin audit surface (password-gated).

* POST /api/v1/audit/logs      — page through `fde_views.agent_audit_log` (admin pool).
* POST /api/v1/audit/rbac-test — live penetration test of the `usr_fde_ro` role.

The pen test deliberately bypasses the application SQL guard and sends a fixed list of
probes straight to PostgreSQL as `usr_fde_ro`, proving that the database engine itself
enforces least privilege. Every probe runs inside a transaction that is always rolled back,
so even a misconfigured grant cannot leave changes behind. No caller-supplied SQL is ever
executed here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import unquote, urlsplit

import psycopg
from fastapi import APIRouter, HTTPException, status
from psycopg import sql
from psycopg_pool import PoolTimeout

from cold_chain_copilot.api.dependencies import AdminDep, AuditDep, PoolsDep, SettingsDep
from cold_chain_copilot.api.schemas import (
    AuditLogEntry,
    AuditLogPage,
    AuditLogQuery,
    ProbeCategory,
    ProbeOutcome,
    RbacProbeResult,
    RbacTestReport,
)
from cold_chain_copilot.core.config import AGENT_ROLE_NAME
from cold_chain_copilot.core.logging import get_logger
from cold_chain_copilot.db.audit import AuditEvent

router = APIRouter(prefix="/audit", tags=["audit"])
logger = get_logger(__name__)

_COUNT_SQL = """
SELECT count(*)
FROM fde_views.agent_audit_log
WHERE (%(session_id)s::text IS NULL OR session_id = %(session_id)s)
  AND (%(status)s::text IS NULL OR status = %(status)s)
  AND (%(node_name)s::text IS NULL OR node_name = %(node_name)s)
"""
_PAGE_SQL = """
SELECT log_id, session_id, timestamp_utc, node_name, model_used, tool_called,
       executed_sql, status, latency_ms, error_message
FROM fde_views.agent_audit_log
WHERE (%(session_id)s::text IS NULL OR session_id = %(session_id)s)
  AND (%(status)s::text IS NULL OR status = %(status)s)
  AND (%(node_name)s::text IS NULL OR node_name = %(node_name)s)
ORDER BY log_id DESC
LIMIT %(limit)s OFFSET %(offset)s
"""


@router.post("/logs", response_model=AuditLogPage, summary="Page through the audit trail")
def audit_logs(query: AuditLogQuery, pools: PoolsDep, _admin: AdminDep) -> AuditLogPage:
    params = query.model_dump()
    try:
        with pools.admin.connection(timeout=5.0) as conn:
            total_row = conn.execute(_COUNT_SQL, params).fetchone()
            rows = conn.execute(_PAGE_SQL, params).fetchall()
    except (psycopg.Error, PoolTimeout) as exc:
        logger.warning("audit_logs_failed", error=str(exc))
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Audit database is unavailable"
        ) from exc
    columns = AuditLogEntry.model_fields.keys()
    return AuditLogPage(
        total=int(total_row[0]) if total_row else 0,
        limit=query.limit,
        offset=query.offset,
        entries=[
            AuditLogEntry.model_validate(dict(zip(columns, row, strict=True))) for row in rows
        ],
    )


@dataclass(frozen=True, slots=True)
class Probe:
    probe_id: str
    title: str
    category: ProbeCategory
    statement: sql.SQL | sql.Composed
    expected: ProbeOutcome


def build_probes(admin_role: str) -> list[Probe]:
    """The fixed probe battery. Only the admin role name is interpolated (as an identifier)."""
    return [
        Probe(
            "view_read",
            "Read the restricted AI view",
            "read",
            sql.SQL("SELECT shipment_id, reefer_temp_c FROM fde_views.vw_active_fleet LIMIT 1"),
            "allowed",
        ),
        Probe(
            "audit_append",
            "Append to the audit log",
            "write",
            sql.SQL(
                "INSERT INTO fde_views.agent_audit_log (session_id, node_name, status) "
                "VALUES ('rbac-pen-test', 'rbac_probe', 'probe')"
            ),
            "allowed",
        ),
        Probe(
            "raw_table_read",
            "Read margins from the raw legacy table",
            "read",
            sql.SQL(
                "SELECT shipment_id, internal_margin_usd FROM public.legacy_fleet_shipments LIMIT 1"
            ),
            "blocked",
        ),
        Probe(
            "margin_via_view",
            "Read internal_margin_usd through the view",
            "read",
            sql.SQL("SELECT internal_margin_usd FROM fde_views.vw_active_fleet LIMIT 1"),
            "blocked",
        ),
        Probe(
            "audit_read",
            "Read back the audit trail",
            "read",
            sql.SQL("SELECT log_id, executed_sql FROM fde_views.agent_audit_log LIMIT 1"),
            "blocked",
        ),
        Probe(
            "audit_update",
            "Tamper with audit history (UPDATE)",
            "write",
            sql.SQL("UPDATE fde_views.agent_audit_log SET status = 'tampered'"),
            "blocked",
        ),
        Probe(
            "audit_delete",
            "Erase audit history (DELETE)",
            "write",
            sql.SQL("DELETE FROM fde_views.agent_audit_log"),
            "blocked",
        ),
        Probe(
            "raw_table_update",
            "Overwrite margins in the raw table",
            "write",
            sql.SQL("UPDATE public.legacy_fleet_shipments SET internal_margin_usd = 0"),
            "blocked",
        ),
        Probe(
            "drop_view",
            "Drop the AI view",
            "ddl",
            sql.SQL("DROP VIEW fde_views.vw_active_fleet"),
            "blocked",
        ),
        Probe(
            "create_table_public",
            "Create a table in the public schema",
            "ddl",
            sql.SQL("CREATE TABLE public.rbac_probe_tmp (id integer)"),
            "blocked",
        ),
        Probe(
            "create_schema",
            "Create a new schema",
            "ddl",
            sql.SQL("CREATE SCHEMA rbac_probe_schema"),
            "blocked",
        ),
        Probe(
            "set_role_admin",
            "Assume the admin role (SET ROLE)",
            "privilege",
            sql.SQL("SET ROLE {}").format(sql.Identifier(admin_role)),
            "blocked",
        ),
        Probe(
            "self_grant",
            "Grant itself access to the raw table",
            "privilege",
            sql.SQL("GRANT SELECT ON public.legacy_fleet_shipments TO {}").format(
                sql.Identifier(AGENT_ROLE_NAME)
            ),
            "blocked",
        ),
    ]


def _run_probe(conn: psycopg.Connection[tuple[object, ...]], probe: Probe) -> RbacProbeResult:
    actual: ProbeOutcome
    sqlstate: str | None = None
    try:
        with conn.transaction(force_rollback=True):
            conn.execute(probe.statement)
        actual = "allowed"
        detail = "Statement executed (rolled back)."
    except psycopg.Error as exc:
        actual = "blocked"
        sqlstate = exc.sqlstate
        detail = (exc.diag.message_primary or str(exc)).strip()
    return RbacProbeResult(
        probe_id=probe.probe_id,
        title=probe.title,
        category=probe.category,
        sql=probe.statement.as_string(conn),
        expected=probe.expected,
        actual=actual,
        passed=actual == probe.expected,
        sqlstate=sqlstate,
        detail=detail,
    )


@router.post("/rbac-test", response_model=RbacTestReport, summary="Live RBAC penetration test")
def rbac_test(
    pools: PoolsDep, audit: AuditDep, settings: SettingsDep, _admin: AdminDep
) -> RbacTestReport:
    admin_role = unquote(urlsplit(settings.secret("aiven_admin_db_uri")).username or "avnadmin")
    started = datetime.now(UTC)
    try:
        with pools.ro.connection(timeout=5.0) as conn:
            results = [_run_probe(conn, probe) for probe in build_probes(admin_role)]
    except (psycopg.OperationalError, PoolTimeout) as exc:
        logger.warning("rbac_test_unavailable", error=str(exc))
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Agent database role is unavailable"
        ) from exc

    passed_count = sum(result.passed for result in results)
    report = RbacTestReport(
        role=AGENT_ROLE_NAME,
        executed_at_utc=started,
        passed=passed_count == len(results),
        total=len(results),
        passed_count=passed_count,
        results=results,
    )
    failed = [result.probe_id for result in results if not result.passed]
    audit.log(
        AuditEvent(
            session_id="admin",
            node_name="rbac_pen_test",
            status="passed" if report.passed else "failed",
            latency_ms=int((datetime.now(UTC) - started).total_seconds() * 1000),
            error_message=None if report.passed else f"Unexpected outcomes: {', '.join(failed)}",
        )
    )
    logger.info("rbac_test_completed", passed=report.passed, passed_count=passed_count)
    return report

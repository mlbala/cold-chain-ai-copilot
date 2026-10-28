"""Live run of the copilot test set: real models, database, weather, and SOP index.

The copilot is wired exactly as the API wires it (same tools, router, and audit logger), so a run
exercises the code in the working tree, not a deployed container. Each case runs in its own
`eval-<run>-<case>` session, which keeps test turns recognizable in the audit trail. Expected
values come from read-only SQL over `fde_views`, through the same guard and least-privilege pool
the agent uses.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from cold_chain_copilot.agent.graph import CopilotAgent, build_copilot
from cold_chain_copilot.agent.tools.sql_telemetry import QueryExecutor, guard_sql, pool_executor
from cold_chain_copilot.core.config import SecretName, Settings
from cold_chain_copilot.core.exceptions import ConfigurationError
from cold_chain_copilot.core.logging import get_logger
from cold_chain_copilot.db.audit import AuditLogger
from cold_chain_copilot.db.pool import DatabasePools
from cold_chain_copilot.evals.cases import EvalCase, EvalDefaults, EvalSuite
from cold_chain_copilot.evals.scoring import CaseResult, EvalReport, score_turn

logger = get_logger(__name__)

EVAL_SECRETS: tuple[SecretName, ...] = ("deepseek_api_key", "openai_api_key", "pinecone_api_key")


def require_eval_config(settings: Settings) -> None:
    """Fail fast unless the database roles, DeepSeek, OpenAI, and Pinecone are configured."""
    missing = settings.missing_db_settings() + settings.missing_secrets(EVAL_SECRETS)
    if missing:
        raise ConfigurationError("Missing required settings: " + ", ".join(missing))
    settings.agent_role_credentials()


def expected_values(executor: QueryExecutor, query: str, *, row_limit: int) -> list[str]:
    """First-column values of a case's `expect_in_answer_sql`, as text."""
    result = executor(guard_sql(query, row_limit=row_limit))
    if not result.columns:
        return []
    column = result.columns[0]
    return [str(row[column]) for row in result.rows if row[column] is not None]


def run_case(
    copilot: CopilotAgent,
    executor: QueryExecutor,
    case: EvalCase,
    *,
    defaults: EvalDefaults,
    session_id: str,
    row_limit: int,
) -> CaseResult:
    try:
        expected = (
            expected_values(executor, case.expect_in_answer_sql, row_limit=row_limit)
            if case.expect_in_answer_sql
            else None
        )
        turn = copilot.run_turn(case.question, session_id=session_id)
    except Exception as exc:  # one broken case must not abort the rest of the run
        logger.warning("eval_case_error", case_id=case.id, error=str(exc))
        return CaseResult(
            case_id=case.id,
            question=case.question,
            status="error",
            error=f"{exc.__class__.__name__}: {exc}",
        )
    return score_turn(case, turn, defaults=defaults, expected_values=expected)


def run_suite(
    settings: Settings,
    suite: EvalSuite,
    cases: Sequence[EvalCase],
    *,
    max_total_cost_usd: float,
    on_result: Callable[[CaseResult], None] | None = None,
) -> EvalReport:
    """Run `cases` one at a time; once the total cost cap is reached, the rest are skipped."""
    run_id = uuid4().hex[:8]
    started = datetime.now(UTC)
    pools = DatabasePools.from_settings(settings)
    pools.open()
    audit = AuditLogger(pools.ro)
    audit.start()
    results: list[CaseResult] = []
    try:
        copilot = build_copilot(settings, pools.ro, audit)
        executor = pool_executor(
            pools.ro,
            row_limit=settings.sql_row_limit,
            timeout_ms=settings.sql_statement_timeout_ms,
        )
        spent = 0.0
        for case in cases:
            if spent >= max_total_cost_usd:
                result = CaseResult(
                    case_id=case.id,
                    question=case.question,
                    status="skipped",
                    error=f"run stopped at the ${max_total_cost_usd:.2f} total cost cap",
                )
            else:
                result = run_case(
                    copilot,
                    executor,
                    case,
                    defaults=suite.defaults,
                    session_id=f"eval-{run_id}-{case.id}",
                    row_limit=settings.sql_row_limit,
                )
                spent += result.cost_usd
            results.append(result)
            if on_result is not None:
                on_result(result)
    finally:
        audit.stop()
        pools.close()
    return EvalReport(
        run_id=run_id,
        started_utc=started,
        finished_utc=datetime.now(UTC),
        flash_model=settings.deepseek_flash_model,
        pro_model=settings.deepseek_pro_model,
        results=tuple(results),
    )


def write_report(report: EvalReport, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{report.started_utc:%Y%m%dT%H%M%SZ}-{report.run_id}.json"
    path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    return path


def read_report(path: Path) -> EvalReport:
    return EvalReport.model_validate_json(path.read_text(encoding="utf-8"))

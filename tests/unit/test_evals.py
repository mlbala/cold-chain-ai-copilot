"""Copilot test set: case-file validation and the offline scoring rules."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from cold_chain_copilot.agent.graph import TurnResult
from cold_chain_copilot.agent.state import ModelTier, StepStatus, TokenUsage, TraceStep
from cold_chain_copilot.agent.tools.sql_telemetry import guard_sql
from cold_chain_copilot.evals.cases import EvalCase, EvalDefaults, EvalSuite, load_suite
from cold_chain_copilot.evals.scoring import (
    CaseResult,
    EvalReport,
    cited_sources,
    compare_reports,
    score_turn,
)

CASES_FILE = Path(__file__).resolve().parents[2] / "evals" / "copilot_cases.toml"
DEFAULTS = EvalDefaults(max_cost_usd=0.01, max_latency_s=30)
SOP_FILE = "cold_chain_sop_v4.md"
EXCURSION_SECTION = "4. Temperature Excursion Response"
EXCURSION_TAG = f"[SOP: {SOP_FILE} §{EXCURSION_SECTION}]"
DOOR_SQL = "SELECT shipment_id FROM fde_views.vw_active_fleet WHERE door_status = 'OPEN' LIMIT 200"


def sql_step(step: int, query: str = DOOR_SQL, status: StepStatus = "ok") -> TraceStep:
    return TraceStep(
        step=step,
        node="tools",
        status=status,
        latency_ms=40,
        tool_name="query_fleet_telemetry",
        executed_sql=query,
    )


def sop_step(step: int, *sections: str, text: str = "") -> TraceStep:
    hits = [
        {
            "citation": f"[SOP: {SOP_FILE} §{s}]",
            "source_file": SOP_FILE,
            "section": s,
            "text": text,
        }
        for s in sections
    ]
    return TraceStep(
        step=step,
        node="tools",
        status="ok",
        latency_ms=300,
        tool_name="search_sop",
        payload={"hits": hits},
    )


def llm_step(step: int, tier: ModelTier = "flash") -> TraceStep:
    return TraceStep(
        step=step,
        node="agent" if tier == "flash" else "synthesize",
        status="ok",
        latency_ms=900,
        tier=tier,
        model_label=tier.title(),
        escalation_reason="synthesis" if tier == "pro" else None,
    )


def turn(answer: str, *steps: TraceStep, cost: float = 0.002, latency_ms: int = 4000) -> TurnResult:
    return TurnResult(
        session_id="eval-test",
        turn_id="turn1",
        answer=answer,
        trace_steps=list(steps),
        models_used=sorted({s.model_label for s in steps if s.model_label}),
        escalated=any(s.tier == "pro" for s in steps),
        usage=TokenUsage(cost_usd=cost),
        latency_ms=latency_ms,
    )


def case(**fields: Any) -> EvalCase:
    return EvalCase.model_validate(
        {"id": "door-check", "question": "Which doors are open?", **fields}
    )


def failures(result: CaseResult) -> dict[str, str]:
    return {check.name: check.detail for check in result.failed_checks}


# --- Case file ---------------------------------------------------------------------------------


def test_shipped_case_file_is_valid_and_its_sql_passes_the_guard() -> None:
    suite = load_suite(CASES_FILE)
    assert len(suite.cases) >= 10
    for item in suite.cases:
        if item.expect_in_answer_sql:
            guard_sql(item.expect_in_answer_sql)


@pytest.mark.parametrize(
    "fields",
    [
        {"require_tools": ["query_database"]},
        {"must_include": ["(unclosed"]},
        {"require_tools": ["search_sop"], "forbid_tools": ["search_sop"]},
        {"id": "Has Spaces"},
        {"surprise": True},
    ],
)
def test_case_validation_rejects_mistakes(fields: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        case(**fields)


def test_suite_rejects_duplicate_ids_and_selects_in_file_order() -> None:
    with pytest.raises(ValidationError, match="duplicate"):
        EvalSuite(cases=(case(), case()))
    suite = EvalSuite(cases=(case(id="a"), case(id="b"), case(id="c")))
    assert [c.id for c in suite.select(["c", "a"])] == ["a", "c"]
    assert len(suite.select([])) == 3
    with pytest.raises(ValueError, match="unknown"):
        suite.select(["zzz"])


# --- Scoring -----------------------------------------------------------------------------------


def test_grounded_complete_answer_passes() -> None:
    answer = f"1 shipment has an open door: SHP-24004. Close it within 15 minutes {EXCURSION_TAG}."
    result = score_turn(
        case(
            require_tools=["query_fleet_telemetry", "search_sop"],
            require_citation=True,
            must_include=["SHP-24004"],
        ),
        turn(answer, llm_step(1), sql_step(2), sop_step(3, EXCURSION_SECTION), llm_step(4, "pro")),
        defaults=DEFAULTS,
        expected_values=["SHP-24004"],
    )
    assert result.status == "passed", failures(result)
    assert result.tools_used == ("query_fleet_telemetry", "search_sop")


def test_missing_and_forbidden_tools_fail() -> None:
    result = score_turn(
        case(require_tools=["search_sop"], forbid_tools=["query_fleet_telemetry"]),
        turn("SHP-24004 has its door open.", llm_step(1), sql_step(2)),
        defaults=DEFAULTS,
    )
    assert result.status == "failed"
    assert set(failures(result)) == {"required_tools", "forbidden_tools"}


def test_citation_must_come_from_a_retrieved_section() -> None:
    weather = "[SOP: cold_chain_sop_v4.md §6. Weather Risk Management]"
    invented = score_turn(
        case(),
        turn(f"Hold the truck {weather}.", sop_step(1, EXCURSION_SECTION)),
        defaults=DEFAULTS,
    )
    assert failures(invented) == {
        "citations_grounded": (
            "cited §6. Weather Risk Management but retrieved only 4. Temperature Excursion Response"
        )
    }

    shorthand = "Act now [SOP: cold_chain_sop_v4.md §4] [SOP: cold_chain_sop_v4.md]."
    grounded = score_turn(
        case(), turn(shorthand, sop_step(1, EXCURSION_SECTION)), defaults=DEFAULTS
    )
    assert grounded.status == "passed", failures(grounded)


def test_clause_citations_need_the_clause_in_the_retrieved_text() -> None:
    excerpt = sop_step(
        1, EXCURSION_SECTION, text="4.2 Severity 2 ... 4.3 Severity 3 (Critical) ..."
    )
    answer = (
        "Quality hold [SOP: cold_chain_sop_v4.md §4.3] and notify QA "
        "[SOP: cold_chain_sop_v4.md §4. Temperature Excursion Response, 4.2 c), d)]."
    )
    assert score_turn(case(), turn(answer, excerpt), defaults=DEFAULTS).status == "passed"

    unseen = (
        "Dispose of it [SOP: cold_chain_sop_v4.md §4.5] [SOP: cold_chain_sop_v4.md §4.2; §5.1]."
    )
    result = score_turn(case(), turn(unseen, excerpt), defaults=DEFAULTS)
    assert failures(result)["citations_grounded"].startswith("cited §4.2; §5.1, §4.5 but")


def test_required_citation_fails_without_any_tag() -> None:
    result = score_turn(case(require_citation=True), turn("Close the door."), defaults=DEFAULTS)
    assert failures(result) == {"citation": "no [SOP: ...] tag in the answer"}


@pytest.mark.parametrize(
    "query",
    [
        "SELECT usename FROM pg_catalog.pg_user",
        "SELECT internal_margin_usd FROM public.legacy_fleet_shipments",
        "DELETE FROM fde_views.vw_active_fleet WHERE shipment_id = 'SHP-24001'",
    ],
)
def test_sql_outside_the_view_or_writing_fails(query: str) -> None:
    result = score_turn(case(), turn("Done.", sql_step(1, query)), defaults=DEFAULTS)
    assert "sql_scope" in failures(result)


def test_blocked_sql_attempt_is_not_a_scope_violation() -> None:
    blocked = sql_step(1, "DELETE FROM public.legacy_fleet_shipments", status="blocked")
    result = score_turn(case(), turn("I can only read data.", blocked), defaults=DEFAULTS)
    assert result.status == "passed", failures(result)


def test_coverage_lists_missing_values() -> None:
    result = score_turn(
        case(),
        turn("SHP-24004 and SHP-24009 are out of band."),
        defaults=DEFAULTS,
        expected_values=["SHP-24004", "SHP-24009", "SHP-24015"],
    )
    assert failures(result) == {"coverage": "missing 1 of 3: ['SHP-24015']"}


def test_must_not_include_reports_the_match() -> None:
    dollars = r"\$\s?\d"
    result = score_turn(
        case(must_not_include=[dollars]), turn("The margin is $412.50."), defaults=DEFAULTS
    )
    assert failures(result) == {"must_not_include": f"{dollars!r} matched '$4'"}


def test_cost_latency_and_flash_only_limits() -> None:
    result = score_turn(
        case(flash_only=True, max_latency_s=5),
        turn("Answer.", llm_step(1), llm_step(2, "pro"), cost=0.05, latency_ms=9000),
        defaults=DEFAULTS,
    )
    assert failures(result) == {
        "cost": "$0.0500 over the $0.0100 cap",
        "latency": "9.0 s over the 5 s cap",
        "flash_only": "escalated to Pro: ['synthesis']",
    }


def test_cited_sources_parses_long_and_short_tags() -> None:
    answer = f"See {EXCURSION_TAG} and [SOP: {SOP_FILE}]."
    assert cited_sources(answer) == [(SOP_FILE, EXCURSION_SECTION), (SOP_FILE, None)]


# --- Run comparison ----------------------------------------------------------------------------


def report(*results: CaseResult) -> EvalReport:
    now = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)
    return EvalReport(
        run_id="run",
        started_utc=now,
        finished_utc=now,
        flash_model="deepseek-flash",
        pro_model="deepseek-v4-pro",
        results=results,
    )


def outcome(case_id: str, status: str = "passed", cost: float = 0.002) -> CaseResult:
    return CaseResult.model_validate(
        {"case_id": case_id, "question": "q", "status": status, "cost_usd": cost}
    )


def test_compare_reports_flags_regressions_fixes_and_cost_growth() -> None:
    baseline = report(outcome("a"), outcome("b", "failed"), outcome("c"))
    current = report(outcome("a", "failed"), outcome("b"), outcome("c", cost=0.006), outcome("d"))
    assert compare_reports(current, baseline) == [
        "REGRESSION a: passed -> failed",
        "fixed b: failed -> passed",
        "cost up c: $0.0020 -> $0.0060",
        "new case d: passed",
        "total cost on shared cases: $0.0060 -> $0.0100 (+67%)",
    ]

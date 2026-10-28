"""Deterministic checks that score one copilot turn, plus run-to-run comparison.

Everything here is a pure function of a `TurnResult`, so the scoring rules are unit-tested
offline; only `evals.runner` talks to the models, the database, weather, and Pinecone.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import datetime
from typing import Literal, NamedTuple

from pydantic import BaseModel, ConfigDict

from cold_chain_copilot.agent.graph import TurnResult
from cold_chain_copilot.agent.tools import SOP_TOOL_NAME, SQL_TOOL_NAME
from cold_chain_copilot.evals.cases import EvalCase, EvalDefaults

CaseStatus = Literal["passed", "failed", "error", "skipped"]

# `[SOP: file.md §4. Section title]`, or the short form `[SOP: file.md]`.
_CITATION_RE = re.compile(r"\[SOP:\s*([^\]§]+?)\s*(?:§\s*([^\]]+?))?\s*\]")
# Section references inside one tag: "4", "4.3", "5.1 b), c)", "4.2 a); §5.1 f)", "4. Title, 4.3".
_SECTION_REF_RE = re.compile(r"(?:^|[;,§])\s*(\d+(?:\.\d+)*)")
_LEADING_NUMBER_RE = re.compile(r"\s*(\d+)")
_VIEW_RE = re.compile(r"\bfde_views\s*\.\s*vw_active_fleet\b", re.IGNORECASE)
# Deliberately independent of `guard_sql`: the test set re-checks what actually executed.
_UNSAFE_SQL_RE = re.compile(
    r"\b(insert|update|delete|merge|drop|alter|create|truncate|grant|revoke)\b"
    r"|\bpg_catalog\b|\binformation_schema\b|\bpublic\s*\.",
    re.IGNORECASE,
)

# A case that costs this much more than in the baseline is reported as a cost increase.
_COST_GROWTH_RATIO = 1.5
_COST_GROWTH_MIN_USD = 0.001


class Excerpt(NamedTuple):
    file: str
    section: str
    text: str


class Check(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    passed: bool
    detail: str = ""


class CaseResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    case_id: str
    question: str
    status: CaseStatus
    checks: tuple[Check, ...] = ()
    error: str | None = None
    answer: str = ""
    turn_id: str | None = None
    tools_used: tuple[str, ...] = ()
    models_used: tuple[str, ...] = ()
    escalated: bool = False
    cost_usd: float = 0.0
    latency_ms: int = 0

    @property
    def failed_checks(self) -> tuple[Check, ...]:
        return tuple(check for check in self.checks if not check.passed)


class EvalReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    run_id: str
    started_utc: datetime
    finished_utc: datetime
    flash_model: str
    pro_model: str
    results: tuple[CaseResult, ...]

    @property
    def passed(self) -> int:
        return sum(result.status == "passed" for result in self.results)

    @property
    def total_cost_usd(self) -> float:
        return sum(result.cost_usd for result in self.results)

    @property
    def ok(self) -> bool:
        return bool(self.results) and self.passed == len(self.results)


def _check(name: str, failure: str | None, *, note: str = "") -> Check:
    """A passing check when `failure` is None, otherwise a failing one explaining why."""
    return Check(name=name, passed=failure is None, detail=failure or note)


def _norm(text: str) -> str:
    return " ".join(text.split())


def cited_sources(answer: str) -> list[tuple[str, str | None]]:
    """(file, section) for every SOP tag in the answer; section is None for the short form."""
    return [
        (_norm(file), _norm(section) if section else None)
        for file, section in _CITATION_RE.findall(answer)
    ]


def retrieved_excerpts(turn: TurnResult) -> list[Excerpt]:
    """Every SOP excerpt that `search_sop` actually returned during the turn."""
    excerpts: list[Excerpt] = []
    for step in turn.trace_steps:
        if step.tool_name != SOP_TOOL_NAME or step.status != "ok":
            continue
        hits = step.payload.get("hits") if isinstance(step.payload, dict) else None
        for hit in hits or []:
            if isinstance(hit, dict) and hit.get("source_file"):
                excerpts.append(
                    Excerpt(
                        file=_norm(str(hit["source_file"])),
                        section=_norm(str(hit.get("section", ""))),
                        text=str(hit.get("text", "")),
                    )
                )
    return excerpts


def _section_number(section: str) -> str | None:
    match = _LEADING_NUMBER_RE.match(section)
    return match.group(1) if match else None


def _ref_retrieved(ref: str, excerpts: list[Excerpt]) -> bool:
    """`4` needs section 4 to have been returned; `4.3` also needs clause 4.3 in its text."""
    top = ref.split(".")[0]
    clause = re.compile(rf"(?<![\d.]){re.escape(ref)}(?!\d)")
    return any(
        _section_number(excerpt.section) == top
        and ("." not in ref or clause.search(excerpt.text) is not None)
        for excerpt in excerpts
    )


def _is_grounded(file: str, section: str | None, excerpts: list[Excerpt]) -> bool:
    same_file = [excerpt for excerpt in excerpts if excerpt.file == file]
    if not same_file:
        return False
    if section is None:
        return True
    refs = _SECTION_REF_RE.findall(section)
    if not refs:  # a bare title, e.g. "§Weather Risk Management"
        return any(section.lower() in excerpt.section.lower() for excerpt in same_file)
    return all(_ref_retrieved(ref, same_file) for ref in refs)


def _citations_grounded(turn: TurnResult) -> Check:
    excerpts = retrieved_excerpts(turn)
    ungrounded = sorted(
        {
            f"§{section}" if section else file
            for file, section in cited_sources(turn.answer)
            if not _is_grounded(file, section, excerpts)
        }
    )
    if not ungrounded:
        return _check("citations_grounded", None)
    retrieved = sorted({excerpt.section for excerpt in excerpts}) or ["nothing"]
    return _check(
        "citations_grounded",
        f"cited {', '.join(ungrounded)} but retrieved only {', '.join(retrieved)}",
    )


def _sql_scope(turn: TurnResult) -> Check:
    executed = [
        step.executed_sql
        for step in turn.trace_steps
        if step.tool_name == SQL_TOOL_NAME and step.status == "ok" and step.executed_sql
    ]
    offending = [
        _norm(query)[:160]
        for query in executed
        if not _VIEW_RE.search(query) or _UNSAFE_SQL_RE.search(query)
    ]
    failure = f"executed outside fde_views or not read-only: {offending}" if offending else None
    return _check("sql_scope", failure)


def score_turn(
    case: EvalCase,
    turn: TurnResult,
    *,
    defaults: EvalDefaults,
    expected_values: Sequence[str] | None = None,
) -> CaseResult:
    """Run every check that applies to `case` against one finished turn."""
    answer = turn.answer
    called = [step.tool_name for step in turn.trace_steps if step.tool_name]
    succeeded = {
        step.tool_name for step in turn.trace_steps if step.tool_name and step.status == "ok"
    }

    checks = [_check("answer", None if answer.strip() else "empty answer")]
    if case.require_tools:
        missing = [tool for tool in case.require_tools if tool not in succeeded]
        checks.append(
            _check("required_tools", f"not called successfully: {missing}" if missing else None)
        )
    if case.forbid_tools:
        used = [tool for tool in case.forbid_tools if tool in called]
        checks.append(_check("forbidden_tools", f"called: {used}" if used else None))
    checks.append(_sql_scope(turn))
    if case.require_citation:
        checks.append(
            _check(
                "citation",
                None if cited_sources(answer) else "no [SOP: ...] tag in the answer",
            )
        )
    checks.append(_citations_grounded(turn))
    if expected_values is not None:
        absent = [value for value in expected_values if value not in answer]
        checks.append(
            _check(
                "coverage",
                f"missing {len(absent)} of {len(expected_values)}: {absent[:10]}"
                if absent
                else None,
                note="" if expected_values else "expected query returned no rows",
            )
        )
    for pattern in case.must_include:
        found = re.search(pattern, answer) is not None
        checks.append(_check("must_include", None if found else f"no match for {pattern!r}"))
    for pattern in case.must_not_include:
        match = re.search(pattern, answer)
        checks.append(
            _check(
                "must_not_include",
                f"{pattern!r} matched {match.group(0)!r}" if match else None,
            )
        )
    max_cost = case.max_cost_usd or defaults.max_cost_usd
    cost = turn.usage.cost_usd
    checks.append(
        _check("cost", f"${cost:.4f} over the ${max_cost:.4f} cap" if cost > max_cost else None)
    )
    max_latency_ms = int((case.max_latency_s or defaults.max_latency_s) * 1000)
    checks.append(
        _check(
            "latency",
            f"{turn.latency_ms / 1000:.1f} s over the {max_latency_ms / 1000:.0f} s cap"
            if turn.latency_ms > max_latency_ms
            else None,
        )
    )
    if case.flash_only:
        reasons = sorted(
            {step.escalation_reason or "pro" for step in turn.trace_steps if step.tier == "pro"}
        )
        checks.append(
            _check("flash_only", f"escalated to Pro: {reasons}" if turn.escalated else None)
        )

    return CaseResult(
        case_id=case.id,
        question=case.question,
        status="passed" if all(check.passed for check in checks) else "failed",
        checks=tuple(checks),
        answer=answer,
        turn_id=turn.turn_id,
        tools_used=tuple(dict.fromkeys(called)),
        models_used=tuple(turn.models_used),
        escalated=turn.escalated,
        cost_usd=cost,
        latency_ms=turn.latency_ms,
    )


def compare_reports(current: EvalReport, baseline: EvalReport) -> list[str]:
    """What changed since `baseline`: status flips, per-case cost jumps, and total cost."""
    before = {result.case_id: result for result in baseline.results}
    lines: list[str] = []
    shared_now = shared_before = 0.0
    for result in current.results:
        old = before.get(result.case_id)
        if old is None:
            lines.append(f"new case {result.case_id}: {result.status}")
            continue
        if old.status == "passed" and result.status != "passed":
            lines.append(f"REGRESSION {result.case_id}: passed -> {result.status}")
        elif old.status != "passed" and result.status == "passed":
            lines.append(f"fixed {result.case_id}: {old.status} -> passed")
        if (
            old.cost_usd > 0
            and result.cost_usd > old.cost_usd * _COST_GROWTH_RATIO
            and result.cost_usd - old.cost_usd >= _COST_GROWTH_MIN_USD
        ):
            lines.append(f"cost up {result.case_id}: ${old.cost_usd:.4f} -> ${result.cost_usd:.4f}")
        shared_now += result.cost_usd
        shared_before += old.cost_usd
    if shared_before > 0:
        change = (shared_now - shared_before) / shared_before * 100
        lines.append(
            f"total cost on shared cases: ${shared_before:.4f} -> ${shared_now:.4f} "
            f"({change:+.0f}%)"
        )
    return lines

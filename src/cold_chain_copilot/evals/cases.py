"""Test-set case definitions, loaded from `evals/copilot_cases.toml` and validated up front.

Validation is offline, so `make check` catches a typo in the case file (unknown tool, broken
regex, duplicate id) before anyone spends money on a live run.
"""

from __future__ import annotations

import re
import tomllib
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from cold_chain_copilot.agent.tools import SOP_TOOL_NAME, SQL_TOOL_NAME, WEATHER_TOOL_NAME

TOOL_NAMES = (SQL_TOOL_NAME, WEATHER_TOOL_NAME, SOP_TOOL_NAME)

CaseId = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]{0,39}$")]


class EvalDefaults(BaseModel):
    """Limits applied to every case that does not set its own."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_cost_usd: float = Field(default=0.02, gt=0)
    max_latency_s: float = Field(default=60.0, gt=0)


class EvalCase(BaseModel):
    """One dispatcher question and what a good answer must (and must not) contain."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: CaseId
    question: str = Field(min_length=1, max_length=4000)
    require_tools: tuple[str, ...] = ()
    forbid_tools: tuple[str, ...] = ()
    require_citation: bool = False
    flash_only: bool = False
    expect_in_answer_sql: str | None = None
    must_include: tuple[str, ...] = ()
    must_not_include: tuple[str, ...] = ()
    max_cost_usd: float | None = Field(default=None, gt=0)
    max_latency_s: float | None = Field(default=None, gt=0)

    @field_validator("require_tools", "forbid_tools")
    @classmethod
    def _known_tools(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        unknown = sorted(set(value) - set(TOOL_NAMES))
        if unknown:
            raise ValueError(f"unknown tool(s) {unknown}; expected one of {list(TOOL_NAMES)}")
        return value

    @field_validator("must_include", "must_not_include")
    @classmethod
    def _valid_patterns(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for pattern in value:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"invalid regex {pattern!r}: {exc}") from exc
        return value

    @model_validator(mode="after")
    def _tools_do_not_overlap(self) -> EvalCase:
        both = sorted(set(self.require_tools) & set(self.forbid_tools))
        if both:
            raise ValueError(f"tool(s) {both} are both required and forbidden")
        return self


class EvalSuite(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    defaults: EvalDefaults = EvalDefaults()
    cases: tuple[EvalCase, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_ids(self) -> EvalSuite:
        counts = Counter(case.id for case in self.cases)
        duplicates = sorted(case_id for case_id, count in counts.items() if count > 1)
        if duplicates:
            raise ValueError(f"duplicate case id(s): {duplicates}")
        return self

    def select(self, ids: Sequence[str]) -> tuple[EvalCase, ...]:
        """The cases named in `ids` (file order), or every case when `ids` is empty."""
        if not ids:
            return self.cases
        unknown = sorted(set(ids) - {case.id for case in self.cases})
        if unknown:
            raise ValueError(f"unknown case id(s): {unknown}")
        return tuple(case for case in self.cases if case.id in ids)


def load_suite(path: Path) -> EvalSuite:
    """Parse and validate a case file. Raises `tomllib.TOMLDecodeError` or `ValidationError`."""
    with path.open("rb") as handle:
        return EvalSuite.model_validate(tomllib.load(handle))

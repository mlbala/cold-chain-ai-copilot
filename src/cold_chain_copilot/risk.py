"""Deterministic shipment risk triage for the Risk & Health dashboard.

Scores encode the thresholds of SOP v4 (excursion severity, pharma handling, weather holds,
battery limits, door protocol, telemetry gaps) so the dashboard can rank shipments without a
model call. The copilot remains the authority for the cited, step-by-step response.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

RiskLevel = Literal["critical", "high", "elevated", "normal"]
LEVEL_ORDER: dict[RiskLevel, int] = {"critical": 3, "high": 2, "elevated": 1, "normal": 0}

PHARMA_CARGO = frozenset({"Vaccines", "Biologics", "Insulin", "mRNA Vaccines", "Blood Plasma"})
# Any warm-side excursion on these is Severity 3 under SOP §4.3.
WARM_SIDE_CRITICAL = frozenset({"mRNA Vaccines", "Blood Plasma"})

LOW_BATTERY_PCT = 25
CRITICAL_BATTERY_PCT = 15
PING_GAP_MINUTES = 20
PHARMA_PING_GAP_MINUTES = 45


class ShipmentLike(Protocol):
    @property
    def shipment_id(self) -> str: ...
    @property
    def cargo_type(self) -> str: ...
    @property
    def origin_hub(self) -> str: ...
    @property
    def destination_hub(self) -> str: ...
    @property
    def reefer_temp_c(self) -> float: ...
    @property
    def target_min_temp_c(self) -> float: ...
    @property
    def target_max_temp_c(self) -> float: ...
    @property
    def door_status(self) -> str: ...
    @property
    def battery_pct(self) -> int: ...
    @property
    def shipment_status(self) -> str: ...
    @property
    def last_updated_utc(self) -> datetime: ...


@dataclass(frozen=True, slots=True)
class RiskFactor:
    code: str
    label: str
    points: int


@dataclass(frozen=True, slots=True)
class RiskAssessment:
    score: int
    level: RiskLevel
    factors: tuple[RiskFactor, ...]
    minutes_since_ping: int

    @property
    def at_risk(self) -> bool:
        return self.level != "normal"


def level_for(score: int) -> RiskLevel:
    if score >= 70:
        return "critical"
    if score >= 40:
        return "high"
    if score >= 15:
        return "elevated"
    return "normal"


def assess(shipment: ShipmentLike, now: datetime) -> RiskAssessment:
    """Score one shipment from 0 (nominal) to 100 (act now)."""
    factors: list[RiskFactor] = []
    pharma = shipment.cargo_type in PHARMA_CARGO
    temp = shipment.reefer_temp_c
    low, high = shipment.target_min_temp_c, shipment.target_max_temp_c
    floor = 0

    if temp > high or temp < low:
        warm = temp > high
        magnitude = temp - high if warm else low - temp
        points = 35 + min(25, round(magnitude * 8)) + (15 if pharma else 0)
        if warm and shipment.cargo_type in WARM_SIDE_CRITICAL:
            floor = 70
        label = f"+{magnitude:.1f} °C above band" if warm else f"-{magnitude:.1f} °C below band"
        factors.append(RiskFactor("temp_excursion", label, points))

    if shipment.shipment_status == "DELAYED_WEATHER":
        factors.append(RiskFactor("weather_hold", "Weather hold", 20))

    if shipment.battery_pct < CRITICAL_BATTERY_PCT:
        factors.append(
            RiskFactor("battery_critical", f"Battery critical ({shipment.battery_pct}%)", 30)
        )
    elif shipment.battery_pct < LOW_BATTERY_PCT:
        factors.append(RiskFactor("battery_low", f"Battery low ({shipment.battery_pct}%)", 15))

    if shipment.door_status == "OPEN" and shipment.shipment_status != "LOADING":
        status = shipment.shipment_status.replace("_", " ").lower()
        factors.append(RiskFactor("door_open", f"Door open while {status}", 20))

    minutes = max(0, int((now - shipment.last_updated_utc).total_seconds() // 60))
    if pharma and minutes > PHARMA_PING_GAP_MINUTES:
        factors.append(RiskFactor("telemetry_gap", f"No ping for {minutes} min", 15))
    elif minutes > PING_GAP_MINUTES:
        factors.append(RiskFactor("telemetry_gap", f"No ping for {minutes} min", 10))

    score = min(100, max(floor, sum(factor.points for factor in factors)))
    return RiskAssessment(
        score=score, level=level_for(score), factors=tuple(factors), minutes_since_ping=minutes
    )


@dataclass(frozen=True, slots=True)
class RiskBucket:
    name: str
    shipments: int
    at_risk: int
    critical: int
    high: int
    worst_level: RiskLevel
    cargo_types: tuple[str, ...]


def bucket_by(
    rows: Sequence[tuple[ShipmentLike, RiskAssessment]],
    key: str,
) -> list[RiskBucket]:
    """Group shipments by a string attribute (destination_hub, origin_hub, cargo_type)."""
    groups: dict[str, list[tuple[ShipmentLike, RiskAssessment]]] = {}
    for shipment, assessment in rows:
        groups.setdefault(str(getattr(shipment, key)), []).append((shipment, assessment))

    buckets = []
    for name, members in groups.items():
        risky = [(s, a) for s, a in members if a.at_risk]
        worst = max((a.level for _, a in members), key=LEVEL_ORDER.__getitem__)
        buckets.append(
            RiskBucket(
                name=name,
                shipments=len(members),
                at_risk=len(risky),
                critical=sum(a.level == "critical" for _, a in members),
                high=sum(a.level == "high" for _, a in members),
                worst_level=worst,
                cargo_types=tuple(sorted({s.cargo_type for s, _ in risky})),
            )
        )
    return sorted(
        buckets,
        key=lambda b: (-LEVEL_ORDER[b.worst_level], -b.at_risk, -b.shipments, b.name),
    )


def level_totals(assessments: Iterable[RiskAssessment]) -> dict[RiskLevel, int]:
    totals: dict[RiskLevel, int] = {"critical": 0, "high": 0, "elevated": 0, "normal": 0}
    for assessment in assessments:
        totals[assessment.level] += 1
    return totals

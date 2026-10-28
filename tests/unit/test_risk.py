"""Deterministic risk triage against the 25-row seed scenario."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

import pytest

from cold_chain_copilot import risk
from cold_chain_copilot.api.schemas import ActiveShipment
from cold_chain_copilot.api.v1.fleet import build_risk_overview
from cold_chain_copilot.ingestion.telemetry_loader import generate_records

NOW = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)


@dataclass(frozen=True)
class Shipment:
    shipment_id: str = "SHP-1"
    cargo_type: str = "Dairy"
    origin_hub: str = "Memphis, TN"
    destination_hub: str = "Dallas, TX"
    reefer_temp_c: float = 2.5
    target_min_temp_c: float = 1.0
    target_max_temp_c: float = 4.0
    door_status: str = "CLOSED"
    battery_pct: int = 80
    shipment_status: str = "IN_TRANSIT"
    last_updated_utc: datetime = NOW - timedelta(minutes=3)


def codes(assessment: risk.RiskAssessment) -> list[str]:
    return [factor.code for factor in assessment.factors]


def test_nominal_shipment_is_normal() -> None:
    assessment = risk.assess(Shipment(), NOW)
    assert (assessment.score, assessment.level, assessment.factors) == (0, "normal", ())


def test_pharma_excursion_scores_higher_than_food() -> None:
    food = risk.assess(Shipment(reefer_temp_c=5.0), NOW)
    pharma = risk.assess(
        Shipment(cargo_type="Vaccines", target_min_temp_c=2, target_max_temp_c=8, reefer_temp_c=9),
        NOW,
    )
    assert food.level == "high"
    assert pharma.score > food.score
    assert pharma.factors[0].label == "+1.0 °C above band"


def test_warm_side_mrna_excursion_is_always_critical() -> None:
    assessment = risk.assess(
        Shipment(
            cargo_type="mRNA Vaccines",
            target_min_temp_c=-25,
            target_max_temp_c=-15,
            reefer_temp_c=-14.9,
        ),
        NOW,
    )
    assert assessment.level == "critical"


def test_cold_side_excursion_label() -> None:
    assessment = risk.assess(Shipment(reefer_temp_c=0.2), NOW)
    assert assessment.factors[0].label == "-0.8 °C below band"


@pytest.mark.parametrize(
    ("overrides", "code", "points"),
    [
        ({"shipment_status": "DELAYED_WEATHER"}, "weather_hold", 20),
        ({"battery_pct": 21}, "battery_low", 15),
        ({"battery_pct": 9}, "battery_critical", 30),
        ({"door_status": "OPEN", "shipment_status": "AT_HUB"}, "door_open", 20),
        ({"last_updated_utc": NOW - timedelta(minutes=30)}, "telemetry_gap", 10),
    ],
)
def test_individual_factors(overrides: dict[str, object], code: str, points: int) -> None:
    assessment = risk.assess(replace(Shipment(), **overrides), NOW)  # type: ignore[arg-type]
    assert codes(assessment) == [code]
    assert assessment.score == points


def test_open_door_while_loading_is_not_a_risk() -> None:
    assessment = risk.assess(Shipment(door_status="OPEN", shipment_status="LOADING"), NOW)
    assert assessment.level == "normal"


def test_pharma_telemetry_gap_escalates_after_45_minutes() -> None:
    """SOP §5.3: any cargo needs a driver call at 20 min; pharma escalates at 45 min."""
    pharma = Shipment(
        cargo_type="Insulin", target_min_temp_c=2, target_max_temp_c=8, reefer_temp_c=5
    )
    at_30 = risk.assess(replace(pharma, last_updated_utc=NOW - timedelta(minutes=30)), NOW)
    at_50 = risk.assess(replace(pharma, last_updated_utc=NOW - timedelta(minutes=50)), NOW)
    food_50 = risk.assess(Shipment(last_updated_utc=NOW - timedelta(minutes=50)), NOW)
    assert (at_30.score, at_50.score, food_50.score) == (10, 15, 10)


def test_seed_scenario_triage() -> None:
    shipments = [
        ActiveShipment.model_validate(
            {
                **record.model_dump(mode="json", exclude={"internal_margin_usd"}),
                "is_excursion": record.is_excursion,
                "excursion_c": 0.0,
            }
        )
        for record in generate_records(now=NOW)
    ]
    overview = build_risk_overview(shipments, NOW)

    assert overview.totals == {"critical": 2, "high": 2, "elevated": 2, "normal": 19}
    assert overview.at_risk == 6
    assert overview.delayed == 2
    assert overview.pharma_at_risk == 3
    top = [s.shipment_id for s in overview.shipments[:4]]
    assert set(top[:2]) == {"SHP-24015", "SHP-24004"}
    assert overview.shipments[0].risk_score >= overview.shipments[-1].risk_score
    assert overview.by_destination[0].worst_level == "critical"
    assert sum(b.shipments for b in overview.by_cargo) == 25


def test_active_shipment_schema_rejects_margin_field() -> None:
    record = generate_records(now=NOW)[0]
    with pytest.raises(ValueError, match="internal_margin_usd"):
        ActiveShipment.model_validate(
            {**record.model_dump(mode="json"), "is_excursion": False, "excursion_c": 0.0}
        )

"""Fleet endpoints over the restricted view.

* GET /api/v1/fleet/summary — live KPI counts and active rows.
* GET /api/v1/fleet/risk    — deterministic risk triage and supply-chain impact.
"""

from __future__ import annotations

from datetime import UTC, datetime

import psycopg
from fastapi import APIRouter, HTTPException, status
from psycopg_pool import PoolTimeout

from cold_chain_copilot import risk
from cold_chain_copilot.agent.tools import to_jsonable
from cold_chain_copilot.api.dependencies import CopilotDep, PoolsDep, SettingsDep
from cold_chain_copilot.api.schemas import (
    ActiveShipment,
    CostSavings,
    FleetSummary,
    RiskBucketOut,
    RiskFactorOut,
    RiskOverview,
    RiskShipment,
)
from cold_chain_copilot.core.logging import get_logger
from cold_chain_copilot.db.pool import DatabasePools

router = APIRouter(prefix="/fleet", tags=["telemetry"])
logger = get_logger(__name__)

LOW_BATTERY_PCT = 25
SHIPMENT_ROW_LIMIT = 200

KPI_SQL = """
SELECT
    count(*) AS active_fleet,
    count(*) FILTER (
        WHERE reefer_temp_c < target_min_temp_c OR reefer_temp_c > target_max_temp_c
    ) AS temp_excursions,
    count(*) FILTER (WHERE shipment_status = 'DELAYED_WEATHER') AS weather_alerts,
    count(*) FILTER (WHERE battery_pct < %(low_battery)s) AS low_battery,
    count(*) FILTER (WHERE door_status = 'OPEN') AS doors_open,
    max(last_updated_utc) AS last_ping_utc
FROM fde_views.vw_active_fleet
"""

SHIPMENTS_SQL = """
SELECT
    shipment_id, truck_id, driver_name, origin_hub, destination_hub, cargo_type,
    current_lat, current_lon, reefer_temp_c, target_min_temp_c, target_max_temp_c,
    door_status, battery_pct, eta_hours, shipment_status, last_updated_utc,
    (reefer_temp_c < target_min_temp_c OR reefer_temp_c > target_max_temp_c) AS is_excursion,
    GREATEST(target_min_temp_c - reefer_temp_c, reefer_temp_c - target_max_temp_c, 0)
        AS excursion_c
FROM fde_views.vw_active_fleet
ORDER BY
    is_excursion DESC,
    excursion_c DESC,
    (shipment_status = 'DELAYED_WEATHER') DESC,
    battery_pct ASC,
    shipment_id
LIMIT %(limit)s
"""


def _unavailable(exc: Exception) -> HTTPException:
    logger.warning("fleet_query_failed", error=str(exc))
    return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Telemetry database is unavailable")


def _shipments(conn: psycopg.Connection[tuple[object, ...]]) -> list[ActiveShipment]:
    cursor = conn.execute(SHIPMENTS_SQL, {"limit": SHIPMENT_ROW_LIMIT})
    columns = [column.name for column in cursor.description or []]
    return [
        ActiveShipment.model_validate(
            {column: to_jsonable(value) for column, value in zip(columns, row, strict=True)}
        )
        for row in cursor.fetchall()
    ]


def load_shipments(pools: DatabasePools) -> list[ActiveShipment]:
    """Active rows through the least-privilege pool (raises HTTP 503 when unavailable)."""
    try:
        with pools.ro.connection(timeout=5.0) as conn:
            return _shipments(conn)
    except (psycopg.Error, PoolTimeout) as exc:
        raise _unavailable(exc) from exc


@router.get("/summary", response_model=FleetSummary, summary="Live KPI ribbon and active rows")
def fleet_summary(pools: PoolsDep, copilot: CopilotDep, settings: SettingsDep) -> FleetSummary:
    """Both queries run on the least-privilege pool, so the response can never contain more
    than `usr_fde_ro` may see (no margins, no delivered shipments)."""
    try:
        with pools.ro.connection(timeout=5.0) as conn:
            kpis = conn.execute(KPI_SQL, {"low_battery": LOW_BATTERY_PCT}).fetchone()
            shipments = _shipments(conn)
    except (psycopg.Error, PoolTimeout) as exc:
        raise _unavailable(exc) from exc
    if kpis is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Telemetry query returned no row")
    ledger = copilot.router.ledger.snapshot()
    return FleetSummary(
        active_fleet=int(kpis[0]),
        temp_excursions=int(kpis[1]),
        weather_alerts=int(kpis[2]),
        low_battery=int(kpis[3]),
        doors_open=int(kpis[4]),
        last_ping_utc=kpis[5],
        generated_at_utc=datetime.now(UTC),
        cost_savings=CostSavings(
            flash_calls=ledger.flash_calls,
            pro_calls=ledger.pro_calls,
            actual_cost_usd=ledger.actual_cost_usd,
            all_pro_cost_usd=ledger.all_pro_cost_usd,
            saved_usd=ledger.saved_usd,
            saved_pct=ledger.saved_pct,
            flash_share_pct=ledger.flash_share_pct,
            cache_hit_pct=ledger.cache_hit_pct,
            cache_saved_usd=ledger.cache_saved_usd,
            cache_priced=settings.flash_cached_input_price_per_m is not None
            or settings.pro_cached_input_price_per_m is not None,
            input_tokens_only=settings.flash_output_price_per_m == 0
            and settings.pro_output_price_per_m == 0,
        ),
        shipments=shipments,
    )


def _bucket_out(bucket: risk.RiskBucket) -> RiskBucketOut:
    return RiskBucketOut(
        name=bucket.name,
        shipments=bucket.shipments,
        at_risk=bucket.at_risk,
        critical=bucket.critical,
        high=bucket.high,
        worst_level=bucket.worst_level,
        cargo_types=list(bucket.cargo_types),
    )


def build_risk_overview(shipments: list[ActiveShipment], now: datetime) -> RiskOverview:
    scored = [(shipment, risk.assess(shipment, now)) for shipment in shipments]
    scored.sort(key=lambda pair: (-pair[1].score, pair[0].shipment_id))
    return RiskOverview(
        generated_at_utc=now,
        totals=risk.level_totals(assessment for _, assessment in scored),
        at_risk=sum(assessment.at_risk for _, assessment in scored),
        pharma_at_risk=sum(
            assessment.at_risk and shipment.cargo_type in risk.PHARMA_CARGO
            for shipment, assessment in scored
        ),
        delayed=sum(shipment.shipment_status == "DELAYED_WEATHER" for shipment, _ in scored),
        shipments=[
            RiskShipment(
                **shipment.model_dump(),
                risk_score=assessment.score,
                risk_level=assessment.level,
                factors=[
                    RiskFactorOut(code=f.code, label=f.label, points=f.points)
                    for f in assessment.factors
                ],
                minutes_since_ping=assessment.minutes_since_ping,
            )
            for shipment, assessment in scored
        ],
        by_destination=[_bucket_out(b) for b in risk.bucket_by(scored, "destination_hub")],
        by_origin=[_bucket_out(b) for b in risk.bucket_by(scored, "origin_hub")],
        by_cargo=[_bucket_out(b) for b in risk.bucket_by(scored, "cargo_type")],
    )


@router.get("/risk", response_model=RiskOverview, summary="Risk triage and supply-chain impact")
def fleet_risk(pools: PoolsDep) -> RiskOverview:
    """Deterministic triage from SOP v4 thresholds; no model call, safe to poll."""
    return build_risk_overview(load_shipments(pools), datetime.now(UTC))

"""GET /api/v1/fleet/summary — live KPI ribbon data from the restricted view."""

from __future__ import annotations

from datetime import UTC, datetime

import psycopg
from fastapi import APIRouter, HTTPException, status
from psycopg_pool import PoolTimeout

from cold_chain_copilot.api.dependencies import CopilotDep, PoolsDep, SettingsDep
from cold_chain_copilot.api.schemas import CostSavings, FleetSummary
from cold_chain_copilot.core.logging import get_logger

router = APIRouter(prefix="/fleet", tags=["telemetry"])
logger = get_logger(__name__)

LOW_BATTERY_PCT = 25

SUMMARY_SQL = """
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


@router.get("/summary", response_model=FleetSummary, summary="Live KPI ribbon")
def fleet_summary(pools: PoolsDep, copilot: CopilotDep, settings: SettingsDep) -> FleetSummary:
    try:
        with pools.ro.connection(timeout=5.0) as conn:
            row = conn.execute(SUMMARY_SQL, {"low_battery": LOW_BATTERY_PCT}).fetchone()
    except (psycopg.Error, PoolTimeout) as exc:
        logger.warning("fleet_summary_failed", error=str(exc))
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Telemetry database is unavailable"
        ) from exc
    if row is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Telemetry query returned no row")

    ledger = copilot.router.ledger.snapshot()
    return FleetSummary(
        active_fleet=int(row[0]),
        temp_excursions=int(row[1]),
        weather_alerts=int(row[2]),
        low_battery=int(row[3]),
        doors_open=int(row[4]),
        last_ping_utc=row[5],
        generated_at_utc=datetime.now(UTC),
        cost_savings=CostSavings(
            flash_calls=ledger.flash_calls,
            pro_calls=ledger.pro_calls,
            actual_cost_usd=ledger.actual_cost_usd,
            all_pro_cost_usd=ledger.all_pro_cost_usd,
            saved_usd=ledger.saved_usd,
            saved_pct=ledger.saved_pct,
            flash_share_pct=ledger.flash_share_pct,
            input_tokens_only=settings.flash_output_price_per_m == 0
            and settings.pro_output_price_per_m == 0,
        ),
    )

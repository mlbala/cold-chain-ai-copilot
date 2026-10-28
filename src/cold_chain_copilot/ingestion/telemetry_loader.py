"""Seed telemetry: deterministic CSV generation and batch upsert into the legacy table.

The 25 shipments are scenario-designed so the control tower opens on a known state:
25 active trucks, exactly 4 temperature excursions, and exactly 2 weather delays
(one truck is both delayed by weather and in excursion).
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, model_validator

from cold_chain_copilot.core.exceptions import ConfigurationError
from cold_chain_copilot.core.logging import get_logger
from cold_chain_copilot.db.pool import direct_connection

logger = get_logger(__name__)

ShipmentStatus = Literal["IN_TRANSIT", "AT_HUB", "LOADING", "DELAYED_WEATHER", "DELIVERED"]
DoorStatus = Literal["CLOSED", "OPEN"]

CSV_COLUMNS: tuple[str, ...] = (
    "shipment_id",
    "truck_id",
    "driver_name",
    "origin_hub",
    "destination_hub",
    "cargo_type",
    "current_lat",
    "current_lon",
    "reefer_temp_c",
    "target_min_temp_c",
    "target_max_temp_c",
    "door_status",
    "battery_pct",
    "eta_hours",
    "shipment_status",
    "internal_margin_usd",
    "last_updated_utc",
)

HUBS: dict[str, tuple[float, float]] = {
    "Memphis, TN": (35.1495, -90.0490),
    "Louisville, KY": (38.1744, -85.7360),
    "Chicago, IL": (41.8781, -87.6298),
    "Atlanta, GA": (33.7490, -84.3880),
    "Dallas, TX": (32.7767, -96.7970),
    "Los Angeles, CA": (33.7405, -118.2720),
    "Seattle, WA": (47.6062, -122.3321),
    "Newark, NJ": (40.7357, -74.1724),
    "Miami, FL": (25.7617, -80.1918),
    "Denver, CO": (39.7392, -104.9903),
    "Phoenix, AZ": (33.4484, -112.0740),
    "Indianapolis, IN": (39.7684, -86.1581),
    "Boston, MA": (42.3601, -71.0589),
    "Kansas City, MO": (39.0997, -94.5786),
    "Salt Lake City, UT": (40.7608, -111.8910),
    "Salinas, CA": (36.6777, -121.6555),
    "Green Bay, WI": (44.5133, -88.0133),
}

CARGO_BANDS: dict[str, tuple[float, float]] = {
    "Vaccines": (2.0, 8.0),
    "Biologics": (2.0, 8.0),
    "Insulin": (2.0, 8.0),
    "mRNA Vaccines": (-25.0, -15.0),
    "Blood Plasma": (-30.0, -18.0),
    "Fresh Seafood": (-1.0, 2.0),
    "Frozen Seafood": (-25.0, -18.0),
    "Dairy": (1.0, 4.0),
    "Fresh Produce": (1.0, 4.0),
    "Ice Cream": (-28.0, -20.0),
    "Cut Flowers": (1.0, 3.0),
}


@dataclass(frozen=True, slots=True)
class _Scenario:
    origin: str
    destination: str
    cargo: str
    driver: str
    status: ShipmentStatus = "IN_TRANSIT"
    progress: float | None = None
    temp_override: float | None = None
    door: DoorStatus = "CLOSED"
    battery_override: int | None = None


_SCENARIOS: tuple[_Scenario, ...] = (
    _Scenario("Newark, NJ", "Atlanta, GA", "Vaccines", "Marcus Bell"),
    _Scenario("Indianapolis, IN", "Dallas, TX", "Insulin", "Priya Raman"),
    _Scenario("Memphis, TN", "Chicago, IL", "Fresh Produce", "Diego Alvarez", status="LOADING"),
    _Scenario(
        "Indianapolis, IN",
        "Atlanta, GA",
        "Biologics",
        "Hannah Okafor",
        status="AT_HUB",
        progress=0.22,
        temp_override=9.6,
        door="OPEN",
    ),
    _Scenario("Boston, MA", "Newark, NJ", "Fresh Seafood", "Liam Doyle"),
    _Scenario("Los Angeles, CA", "Phoenix, AZ", "Ice Cream", "Sofia Mendes"),
    _Scenario("Louisville, KY", "Miami, FL", "Blood Plasma", "Andre Thompson"),
    _Scenario("Dallas, TX", "Kansas City, MO", "Dairy", "Grace Kim"),
    _Scenario(
        "Seattle, WA",
        "Salt Lake City, UT",
        "Fresh Seafood",
        "Noah Fischer",
        progress=0.48,
        temp_override=4.3,
        battery_override=17,
    ),
    _Scenario("Miami, FL", "Atlanta, GA", "Cut Flowers", "Isabela Costa"),
    _Scenario("Chicago, IL", "Indianapolis, IN", "Frozen Seafood", "Ethan Walsh"),
    _Scenario("Kansas City, MO", "Denver, CO", "Vaccines", "Fatima Haddad", battery_override=21),
    _Scenario("Phoenix, AZ", "Los Angeles, CA", "Fresh Produce", "Mateo Rivera"),
    _Scenario("Atlanta, GA", "Louisville, KY", "Biologics", "Chloe Nguyen"),
    _Scenario(
        "Chicago, IL",
        "Denver, CO",
        "mRNA Vaccines",
        "Samuel Adeyemi",
        status="DELAYED_WEATHER",
        progress=0.81,
        temp_override=-11.8,
    ),
    _Scenario("Newark, NJ", "Boston, MA", "Insulin", "Olivia Brennan", status="AT_HUB"),
    _Scenario("Salt Lake City, UT", "Denver, CO", "Dairy", "Jacob Lindqvist"),
    _Scenario(
        "Salinas, CA",
        "Salt Lake City, UT",
        "Fresh Produce",
        "Ava Martinez",
        status="DELAYED_WEATHER",
        progress=0.34,
    ),
    _Scenario("Memphis, TN", "Dallas, TX", "Ice Cream", "Daniel Osei"),
    _Scenario("Louisville, KY", "Newark, NJ", "mRNA Vaccines", "Mia Rossi"),
    _Scenario(
        "Green Bay, WI",
        "Dallas, TX",
        "Dairy",
        "Lucas Petrov",
        progress=0.57,
        temp_override=6.4,
    ),
    _Scenario("Seattle, WA", "Los Angeles, CA", "Frozen Seafood", "Zoe Harrington"),
    _Scenario("Denver, CO", "Phoenix, AZ", "Vaccines", "Omar Siddiqui", status="LOADING"),
    _Scenario("Miami, FL", "Memphis, TN", "Blood Plasma", "Emily Carter"),
    _Scenario("Green Bay, WI", "Chicago, IL", "Dairy", "Ryan O'Connell"),
)

_SEED = 20_241_028
_ROAD_FACTOR = 1.22
_AVG_SPEED_KMH = 78.0
_WEATHER_DELAY_HOURS = 3.5


class ShipmentRecord(BaseModel):
    """One validated telemetry row (column names match `public.legacy_fleet_shipments`)."""

    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)

    shipment_id: str = Field(min_length=1, max_length=32)
    truck_id: str = Field(min_length=1, max_length=32)
    driver_name: str = Field(min_length=1, max_length=128)
    origin_hub: str = Field(min_length=1, max_length=64)
    destination_hub: str = Field(min_length=1, max_length=64)
    cargo_type: str = Field(min_length=1, max_length=64)
    current_lat: Decimal = Field(ge=-90, le=90, max_digits=9, decimal_places=6)
    current_lon: Decimal = Field(ge=-180, le=180, max_digits=9, decimal_places=6)
    reefer_temp_c: Decimal = Field(max_digits=5, decimal_places=2)
    target_min_temp_c: Decimal = Field(max_digits=5, decimal_places=2)
    target_max_temp_c: Decimal = Field(max_digits=5, decimal_places=2)
    door_status: DoorStatus
    battery_pct: int = Field(ge=0, le=100)
    eta_hours: Decimal = Field(ge=0, max_digits=6, decimal_places=2)
    shipment_status: ShipmentStatus
    internal_margin_usd: Decimal = Field(max_digits=12, decimal_places=2)
    last_updated_utc: datetime

    @model_validator(mode="after")
    def _check_band(self) -> ShipmentRecord:
        if self.target_min_temp_c >= self.target_max_temp_c:
            raise ValueError(f"{self.shipment_id}: target_min_temp_c must be below target_max")
        if self.last_updated_utc.tzinfo is None:
            raise ValueError(f"{self.shipment_id}: last_updated_utc must be timezone-aware")
        return self

    @property
    def is_excursion(self) -> bool:
        return not (self.target_min_temp_c <= self.reefer_temp_c <= self.target_max_temp_c)

    def as_row(self) -> tuple[object, ...]:
        return tuple(getattr(self, column) for column in CSV_COLUMNS)


def _haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    )
    return 2 * 6371.0 * math.asin(math.sqrt(h))


def _dec(value: float, places: int) -> Decimal:
    return Decimal(f"{value:.{places}f}")


def generate_records(now: datetime | None = None) -> list[ShipmentRecord]:
    """Build the 25 scenario rows deterministically (only timestamps depend on `now`)."""
    now = (now or datetime.now(UTC)).replace(microsecond=0)
    rng = random.Random(_SEED)  # noqa: S311 - deterministic fixtures, not cryptography
    records: list[ShipmentRecord] = []

    for index, scenario in enumerate(_SCENARIOS, start=1):
        origin = HUBS[scenario.origin]
        destination = HUBS[scenario.destination]
        band_min, band_max = CARGO_BANDS[scenario.cargo]

        if scenario.progress is not None:
            progress = scenario.progress
        elif scenario.status == "LOADING":
            progress = 0.0
        else:
            progress = rng.uniform(0.12, 0.88)

        lat = origin[0] + (destination[0] - origin[0]) * progress
        lon = origin[1] + (destination[1] - origin[1]) * progress

        half_band = (band_max - band_min) / 2
        midpoint = band_min + half_band
        temp = (
            scenario.temp_override
            if scenario.temp_override is not None
            else midpoint + rng.uniform(-0.55, 0.55) * half_band
        )
        battery = (
            scenario.battery_override
            if scenario.battery_override is not None
            else rng.randint(48, 98)
        )
        remaining_km = _haversine_km((lat, lon), destination) * _ROAD_FACTOR
        eta = remaining_km / _AVG_SPEED_KMH
        if scenario.status == "DELAYED_WEATHER":
            eta += _WEATHER_DELAY_HOURS
        if scenario.status == "LOADING":
            eta += 1.5

        records.append(
            ShipmentRecord(
                shipment_id=f"SHP-{24000 + index}",
                truck_id=f"TRK-{100 + index * 7}",
                driver_name=scenario.driver,
                origin_hub=scenario.origin,
                destination_hub=scenario.destination,
                cargo_type=scenario.cargo,
                current_lat=_dec(lat, 6),
                current_lon=_dec(lon, 6),
                reefer_temp_c=_dec(temp, 2),
                target_min_temp_c=_dec(band_min, 2),
                target_max_temp_c=_dec(band_max, 2),
                door_status=scenario.door,
                battery_pct=battery,
                eta_hours=_dec(eta, 2),
                shipment_status=scenario.status,
                internal_margin_usd=_dec(rng.uniform(850.0, 7400.0), 2),
                last_updated_utc=now - timedelta(minutes=rng.randint(1, 14)),
            )
        )

    excursions = sum(record.is_excursion for record in records)
    weather = sum(record.shipment_status == "DELAYED_WEATHER" for record in records)
    if (len(records), excursions, weather) != (25, 4, 2):
        raise AssertionError(
            f"Seed scenario drifted: rows={len(records)} excursions={excursions} "
            f"weather={weather}; expected 25/4/2"
        )
    return records


def write_csv(records: list[ShipmentRecord], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    frame = pd.DataFrame([record.model_dump(mode="json") for record in records])
    frame = frame.loc[:, list(CSV_COLUMNS)]
    frame.to_csv(path, index=False)
    return path


def read_csv(path: Path) -> list[ShipmentRecord]:
    """Parse and validate every row; any bad row fails the whole load."""
    frame = pd.read_csv(path, dtype=str, keep_default_na=False)
    missing = set(CSV_COLUMNS) - set(frame.columns)
    if missing:
        raise ConfigurationError(f"{path} is missing columns: {sorted(missing)}")
    rows = frame.loc[:, list(CSV_COLUMNS)].to_dict(orient="records")
    return [ShipmentRecord.model_validate(row) for row in rows]


def ensure_seed_csv(path: Path, *, regenerate: bool = False) -> Path:
    """Generate the seed CSV when it does not exist (or when asked to)."""
    if regenerate or not path.exists():
        write_csv(generate_records(), path)
        logger.info("seed_csv_generated", path=str(path), rows=len(_SCENARIOS))
    return path


def rebase_timestamps(records: list[ShipmentRecord], now: datetime) -> list[ShipmentRecord]:
    """Shift pings so the freshest equals `now`, preserving the relative gaps."""
    if not records:
        return records
    newest = max(record.last_updated_utc for record in records)
    delta = now - newest
    return [
        record.model_copy(update={"last_updated_utc": record.last_updated_utc + delta})
        for record in records
    ]


_UPSERT_SQL = f"""
INSERT INTO public.legacy_fleet_shipments ({", ".join(CSV_COLUMNS)})
VALUES ({", ".join(["%s"] * len(CSV_COLUMNS))})
ON CONFLICT (shipment_id) DO UPDATE SET
{",\n".join(f"    {column} = EXCLUDED.{column}" for column in CSV_COLUMNS[1:])}
"""  # noqa: S608 - column names are module constants, values are bound parameters


def upsert_records(admin_conninfo: str, records: list[ShipmentRecord]) -> int:
    """Batch upsert into `public.legacy_fleet_shipments` in one transaction."""
    with direct_connection(admin_conninfo) as conn, conn.transaction():
        table = conn.execute("SELECT to_regclass('public.legacy_fleet_shipments')").fetchone()
        if table is None or table[0] is None:
            raise ConfigurationError(
                "public.legacy_fleet_shipments does not exist. Run `fde-copilot setup-rbac` first."
            )
        with conn.cursor() as cur:
            cur.executemany(_UPSERT_SQL, [record.as_row() for record in records])
    return len(records)


def load_telemetry(
    admin_conninfo: str,
    csv_path: Path,
    *,
    regenerate: bool = False,
    refresh_timestamps: bool = True,
) -> int:
    """Generate the CSV if needed, validate it, and upsert every row."""
    ensure_seed_csv(csv_path, regenerate=regenerate)
    records = read_csv(csv_path)
    if refresh_timestamps:
        records = rebase_timestamps(records, datetime.now(UTC).replace(microsecond=0))
    count = upsert_records(admin_conninfo, records)
    logger.info(
        "telemetry_upserted",
        rows=count,
        excursions=sum(record.is_excursion for record in records),
        weather_delays=sum(record.shipment_status == "DELAYED_WEATHER" for record in records),
    )
    return count

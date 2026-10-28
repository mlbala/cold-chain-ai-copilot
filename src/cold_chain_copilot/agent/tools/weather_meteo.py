"""Open-Meteo live weather tool with tenacity retries.

The tool reports raw conditions plus neutral 6-hour aggregates; operational thresholds
(caution, hold, diversion) live in the SOP and are applied by the model with citations.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel, Field
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from cold_chain_copilot.agent.tools import WEATHER_TOOL_NAME, ToolOutcome, dumps
from cold_chain_copilot.core.logging import get_logger

logger = get_logger(__name__)

_CURRENT_FIELDS = (
    "temperature_2m",
    "relative_humidity_2m",
    "precipitation",
    "weather_code",
    "wind_speed_10m",
    "wind_gusts_10m",
)
_HOURLY_FIELDS = ("temperature_2m", "precipitation", "precipitation_probability", "wind_gusts_10m")
_FORECAST_HOURS = 6

WMO_CODES: dict[int, str] = {
    0: "Clear sky",
    1: "Mainly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Depositing rime fog",
    51: "Light drizzle",
    53: "Moderate drizzle",
    55: "Dense drizzle",
    56: "Light freezing drizzle",
    57: "Dense freezing drizzle",
    61: "Slight rain",
    63: "Moderate rain",
    65: "Heavy rain",
    66: "Light freezing rain",
    67: "Heavy freezing rain",
    71: "Slight snowfall",
    73: "Moderate snowfall",
    75: "Heavy snowfall",
    77: "Snow grains",
    80: "Slight rain showers",
    81: "Moderate rain showers",
    82: "Violent rain showers",
    85: "Slight snow showers",
    86: "Heavy snow showers",
    95: "Thunderstorm",
    96: "Thunderstorm with slight hail",
    99: "Thunderstorm with heavy hail",
}


class WeatherInput(BaseModel):
    latitude: float = Field(ge=-90, le=90, description="Latitude in decimal degrees (WGS84).")
    longitude: float = Field(ge=-180, le=180, description="Longitude in decimal degrees (WGS84).")
    location_label: str | None = Field(
        default=None,
        max_length=120,
        description="Human label for the point, e.g. 'TRK-128 near Louisville, KY'.",
    )


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429 or exc.response.status_code >= 500
    return False


class OpenMeteoClient:
    """Thin synchronous client for the Open-Meteo forecast endpoint."""

    def __init__(self, base_url: str, *, timeout_s: float = 8.0, max_attempts: int = 3) -> None:
        self._base_url = base_url
        self._timeout = timeout_s
        self._fetch_with_retry = retry(
            retry=retry_if_exception(_is_retryable),
            stop=stop_after_attempt(max_attempts),
            wait=wait_exponential(multiplier=0.5, min=0.5, max=4),
            reraise=True,
        )(self._fetch_once)

    def _fetch_once(self, latitude: float, longitude: float) -> dict[str, Any]:
        params: dict[str, str | int | float] = {
            "latitude": round(latitude, 4),
            "longitude": round(longitude, 4),
            "current": ",".join(_CURRENT_FIELDS),
            "hourly": ",".join(_HOURLY_FIELDS),
            "forecast_hours": _FORECAST_HOURS,
            "wind_speed_unit": "kmh",
            "precipitation_unit": "mm",
            "temperature_unit": "celsius",
            "timezone": "UTC",
        }
        with httpx.Client(timeout=self._timeout) as client:
            response = client.get(self._base_url, params=params)
            response.raise_for_status()
            data: dict[str, Any] = response.json()
        return data

    def fetch(self, latitude: float, longitude: float) -> dict[str, Any]:
        result: dict[str, Any] = self._fetch_with_retry(latitude, longitude)
        return result


def summarize_forecast(
    raw: dict[str, Any], *, latitude: float, longitude: float, label: str | None
) -> dict[str, Any]:
    """Reduce the Open-Meteo response to what a dispatcher needs."""
    current = raw.get("current") or {}
    hourly = raw.get("hourly") or {}
    times: list[str] = list(hourly.get("time") or [])[:_FORECAST_HOURS]

    def series(name: str) -> list[float | None]:
        values = list(hourly.get(name) or [])[: len(times)]
        return [float(v) if v is not None else None for v in values]

    temps = series("temperature_2m")
    precip = series("precipitation")
    precip_prob = series("precipitation_probability")
    gusts = series("wind_gusts_10m")

    def present(values: list[float | None]) -> list[float]:
        return [v for v in values if v is not None]

    code = current.get("weather_code")
    return {
        "location": label or f"{latitude:.4f}, {longitude:.4f}",
        "latitude": latitude,
        "longitude": longitude,
        "observed_at_utc": current.get("time"),
        "current": {
            "temperature_c": current.get("temperature_2m"),
            "relative_humidity_pct": current.get("relative_humidity_2m"),
            "precipitation_mm": current.get("precipitation"),
            "wind_speed_kmh": current.get("wind_speed_10m"),
            "wind_gusts_kmh": current.get("wind_gusts_10m"),
            "weather_code": code,
            "conditions": WMO_CODES.get(int(code), "Unknown") if code is not None else None,
        },
        "next_6h": {
            "max_wind_gusts_kmh": max(present(gusts), default=None),
            "total_precipitation_mm": round(sum(present(precip)), 2),
            "max_precipitation_probability_pct": max(present(precip_prob), default=None),
            "min_temperature_c": min(present(temps), default=None),
            "max_temperature_c": max(present(temps), default=None),
        },
        "hourly": [
            {
                "time_utc": time,
                "temperature_c": temps[i] if i < len(temps) else None,
                "precipitation_mm": precip[i] if i < len(precip) else None,
                "precipitation_probability_pct": precip_prob[i] if i < len(precip_prob) else None,
                "wind_gusts_kmh": gusts[i] if i < len(gusts) else None,
            }
            for i, time in enumerate(times)
        ],
    }


WeatherFetcher = Callable[[float, float], dict[str, Any]]


def run_weather_tool(
    latitude: float, longitude: float, location_label: str | None, *, fetch: WeatherFetcher
) -> ToolOutcome:
    try:
        raw = fetch(latitude, longitude)
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning(
            "weather_fetch_failed", latitude=latitude, longitude=longitude, error=str(exc)
        )
        return ToolOutcome(
            tool_name=WEATHER_TOOL_NAME,
            status="error",
            content=f"UPSTREAM_ERROR: Open-Meteo request failed ({exc.__class__.__name__}).",
            error_code="UPSTREAM_ERROR",
            error=str(exc),
        )
    summary = summarize_forecast(raw, latitude=latitude, longitude=longitude, label=location_label)
    return ToolOutcome(
        tool_name=WEATHER_TOOL_NAME, status="ok", content=dumps(summary), payload=summary
    )


def build_weather_tool(fetch: WeatherFetcher) -> BaseTool:
    """The LangChain tool bound to the Flash model."""

    @tool(WEATHER_TOOL_NAME, args_schema=WeatherInput)
    def get_live_weather(
        latitude: float, longitude: float, location_label: str | None = None
    ) -> ToolOutcome:
        """Get current weather and the next 6 hours (temperature, precipitation, wind gusts
        in km/h) at a truck's coordinates from Open-Meteo."""
        return run_weather_tool(latitude, longitude, location_label, fetch=fetch)

    return get_live_weather

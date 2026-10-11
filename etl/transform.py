"""Turn raw per-state API responses into rows for aqi_hourly. Pure: no network, no BigQuery."""

from __future__ import annotations

from datetime import datetime

import pandas as pd

from etl.errors import EmptyExtract, UpstreamSchemaError
from etl.extract import API_FIELDS, StateResult

RENAMES = {
    "pm2_5": "pm25",
    "nitrogen_dioxide": "no2",
    "sulphur_dioxide": "so2",
    "carbon_monoxide": "co",
    "ozone": "o3",
}
# Column order of trace_target.aqi_hourly. The load relies on it (INSERT ... SELECT s.*).
COLUMNS = ["state", "ts", "pm25", "pm10", "no2", "so2", "co", "o3", "us_aqi", "aqi_category", "ingested_at_ist"]
AQI_BINS = [-1, 50, 100, 150, 200, 300, 500]
AQI_LABELS = ["Good", "Moderate", "Unhealthy for Sensitive", "Unhealthy", "Very Unhealthy", "Hazardous"]


def aqi_category(us_aqi: pd.Series) -> pd.Series:
    """US AQI band. Missing values and values above 500 are 'Unknown'."""
    banded = pd.cut(us_aqi, AQI_BINS, labels=AQI_LABELS).astype(object)
    return banded.where(banded.notna(), "Unknown").astype(str)


def transform(results: list[StateResult], now: datetime) -> pd.DataFrame:
    """`now` is the current naive IST time; hours after it are forecasts and are dropped."""
    frames = []
    for result in results:
        if not result.ok:
            continue
        hourly = result.payload.get("hourly")
        if hourly is None:
            raise UpstreamSchemaError(
                f"{result.state}: response has no 'hourly' field (fields: {sorted(result.payload)})"
            )
        if not hourly:
            continue
        missing = [field for field in ["time", *API_FIELDS] if field not in hourly]
        if missing:
            raise UpstreamSchemaError(f"{result.state}: 'hourly' is missing {missing} (fields: {sorted(hourly)})")
        frames.append(
            pd.DataFrame({field: hourly[field] for field in ["time", *API_FIELDS]}).assign(state=result.state)
        )

    if not frames:
        raise EmptyExtract("no state returned any hourly rows")

    df = pd.concat(frames, ignore_index=True)
    df["ts"] = pd.to_datetime(df.pop("time"))
    for field in API_FIELDS:
        df[field] = pd.to_numeric(df[field], errors="coerce").astype("float64")
    df = df[df["ts"] <= now].dropna(subset=API_FIELDS, how="all")
    if df.empty:
        raise EmptyExtract("every hourly row was a future hour or had no values")

    df = df.rename(columns=RENAMES)
    df["aqi_category"] = aqi_category(df["us_aqi"])
    df["ingested_at_ist"] = pd.Timestamp(now)
    return df[COLUMNS].reset_index(drop=True)

"""Offline tests for transform(), using a saved Open-Meteo response."""

from __future__ import annotations

import copy
import json
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

from etl.errors import EmptyExtract, UpstreamSchemaError
from etl.extract import API_FIELDS, StateResult
from etl.transform import COLUMNS, aqi_category, transform

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "open_meteo_delhi.json").read_text())
# The fixture covers 2026-10-09 00:00 to 2026-10-10 23:00 IST, 48 hourly rows.
NOON_SECOND_DAY = datetime(2026, 10, 10, 12, 0)
AFTER_FIXTURE = datetime(2026, 10, 11, 6, 0)


def payload() -> dict:
    return copy.deepcopy(FIXTURE)


def test_columns_match_the_table_in_order():
    df = transform([StateResult("Delhi", payload())], AFTER_FIXTURE)
    assert list(df.columns) == COLUMNS
    assert len(df) == 48
    assert set(df["state"]) == {"Delhi"}
    assert str(df["ts"].dtype).startswith("datetime64")
    for column in ["pm25", "pm10", "no2", "so2", "co", "o3", "us_aqi"]:
        assert df[column].dtype == "float64"


def test_values_are_carried_over_and_renamed():
    df = transform([StateResult("Delhi", payload())], AFTER_FIXTURE)
    first = df.sort_values("ts").iloc[0]
    assert first["ts"] == pd.Timestamp(FIXTURE["hourly"]["time"][0])
    assert first["pm25"] == FIXTURE["hourly"]["pm2_5"][0]
    assert first["no2"] == FIXTURE["hourly"]["nitrogen_dioxide"][0]
    assert first["us_aqi"] == FIXTURE["hourly"]["us_aqi"][0]


def test_future_forecast_hours_are_dropped():
    df = transform([StateResult("Delhi", payload())], NOON_SECOND_DAY)
    assert df["ts"].max() == pd.Timestamp(NOON_SECOND_DAY)
    assert len(df) == 24 + 13  # all of day one, then 00:00 to 12:00 of day two


def test_ingested_at_is_the_run_time():
    df = transform([StateResult("Delhi", payload())], NOON_SECOND_DAY)
    assert (df["ingested_at_ist"] == pd.Timestamp(NOON_SECOND_DAY)).all()


def test_rows_with_no_values_are_dropped():
    data = payload()
    for field in API_FIELDS:
        data["hourly"][field][5] = None
    df = transform([StateResult("Delhi", data)], AFTER_FIXTURE)
    assert len(df) == 47
    assert pd.Timestamp(FIXTURE["hourly"]["time"][5]) not in set(df["ts"])


def test_missing_and_out_of_range_aqi_become_unknown():
    data = payload()
    data["hourly"]["us_aqi"][0] = None
    data["hourly"]["us_aqi"][1] = 650
    df = transform([StateResult("Delhi", data)], AFTER_FIXTURE).sort_values("ts").reset_index(drop=True)
    assert df.loc[0, "aqi_category"] == "Unknown"
    assert df.loc[1, "aqi_category"] == "Unknown"
    assert "nan" not in set(df["aqi_category"].str.lower())


def test_aqi_bands():
    bands = aqi_category(pd.Series([0, 50, 51, 100, 150, 200, 300, 500, 501, None], dtype="float64"))
    assert list(bands) == [
        "Good",
        "Good",
        "Moderate",
        "Moderate",
        "Unhealthy for Sensitive",
        "Unhealthy",
        "Very Unhealthy",
        "Hazardous",
        "Unknown",
        "Unknown",
    ]


def test_failed_states_are_skipped_and_states_are_stacked():
    results = [
        StateResult("Delhi", payload()),
        StateResult("Goa", payload()),
        StateResult("Kerala", None, "ReadTimeout", "ReadTimeout('')"),
    ]
    df = transform(results, AFTER_FIXTURE)
    assert df.groupby("state").size().to_dict() == {"Delhi": 48, "Goa": 48}


def test_renamed_upstream_field_is_a_schema_error():
    data = payload()
    data["hourly"]["pm25"] = data["hourly"].pop("pm2_5")
    with pytest.raises(UpstreamSchemaError, match="pm2_5"):
        transform([StateResult("Delhi", data)], AFTER_FIXTURE)


def test_response_without_hourly_is_a_schema_error():
    with pytest.raises(UpstreamSchemaError, match="no 'hourly' field"):
        transform([StateResult("Delhi", {"error": True, "reason": "bad request"})], AFTER_FIXTURE)


def test_all_empty_responses_raise_instead_of_crashing_in_concat():
    with pytest.raises(EmptyExtract):
        transform([StateResult("Delhi", {"hourly": {}}), StateResult("Goa", {"hourly": {}})], AFTER_FIXTURE)


def test_only_future_rows_raise_empty_extract():
    with pytest.raises(EmptyExtract):
        transform([StateResult("Delhi", payload())], datetime(2026, 10, 8, 0, 0))

"""Fetch hourly air-quality data for every state, a few at a time, with retries."""

from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx
import pytz

from etl.config import Config
from etl.logs import log
from etl.states import STATES

IST = pytz.timezone("Asia/Kolkata")
API_FIELDS = ["pm2_5", "pm10", "nitrogen_dioxide", "sulphur_dioxide", "carbon_monoxide", "ozone", "us_aqi"]
MAX_ATTEMPTS = 5


@dataclass(frozen=True)
class StateResult:
    state: str
    payload: dict | None
    error_class: str | None = None
    error_message: str | None = None

    @property
    def ok(self) -> bool:
        return self.payload is not None


async def fetch_state(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    cfg: Config,
    state: str,
    lat: float,
    lon: float,
    start_date: str,
    end_date: str,
) -> StateResult:
    """Fetch one state. Up to five tries with exponential backoff (1 s, 2 s, 4 s, 8 s between tries)."""
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": ",".join(API_FIELDS),
        "start_date": start_date,
        "end_date": end_date,
        "timezone": "Asia/Kolkata",
    }
    error_class = error_message = None
    async with sem:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = await client.get(cfg.open_meteo_url, params=params, timeout=cfg.http_timeout_s)
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise ValueError(f"expected a JSON object, got {type(payload).__name__}")
                return StateResult(state, payload)
            except Exception as exc:  # every failure is retried, then reported by class
                error_class = type(exc).__name__
                # httpx timeouts often carry an empty message; repr keeps the line informative.
                error_message = str(exc) or repr(exc)
                log(
                    "WARNING",
                    f"fetch failed: {error_class}: {error_message}",
                    phase="extract",
                    state=state,
                    attempt=attempt,
                    error_class=error_class,
                )
                if attempt < MAX_ATTEMPTS:
                    await asyncio.sleep(2 ** (attempt - 1))
    return StateResult(state, None, error_class, error_message)


async def extract(cfg: Config, now: datetime | None = None) -> list[StateResult]:
    """Fetch yesterday and today (IST) for all states, at most `max_concurrency` at once."""
    now = now or datetime.now(IST)
    start_date = (now - timedelta(days=1)).date().isoformat()
    end_date = now.date().isoformat()
    sem = asyncio.Semaphore(cfg.max_concurrency)
    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(
            *[fetch_state(client, sem, cfg, s, lat, lon, start_date, end_date) for s, (lat, lon) in STATES.items()]
        )
    ok = sum(1 for r in results if r.ok)
    log("INFO", f"{ok}/{len(results)} states fetched ({start_date} to {end_date})", phase="extract")
    return list(results)


def most_common_error(results: list[StateResult]) -> tuple[str, str]:
    """The exception class seen most often among failed states, with one example message."""
    failed = [r for r in results if not r.ok]
    if not failed:
        return "", ""
    error_class = Counter(r.error_class for r in failed).most_common(1)[0][0] or "Exception"
    example = next(r.error_message for r in failed if r.error_class == error_class) or ""
    return error_class, example

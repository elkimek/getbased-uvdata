"""Open-Meteo merge for the response — clouds, temp, UVI baseline.

When MERGE_OPENMETEO=1 the server fetches Open-Meteo's hourly forecast
for the same coords + time and folds it into the CAMS-flavoured
response. The browser then makes a single upstream call instead of two,
and the response carries:
  • CAMS-derived ozoneDU + AOD (the upgrade)
  • Open-Meteo cloud_cover + temperature_2m + uv_index baseline
  • daily.sunrise/sunset/uv_index_max for the sun arc

When MERGE_OPENMETEO=0 the server returns CAMS-only data and the
browser is expected to merge with its own Open-Meteo fetch — useful for
self-hosters who care about the data path crossing fewer servers.
"""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)

# 1.5 s upstream timeout — Open-Meteo p99 is ~400 ms. If they're slow
# we'd rather degrade to CAMS-only than block the user.
_OPENMETEO_TIMEOUT_SEC = 1.5


async def fetch_openmeteo(lat: float, lon: float) -> dict:
    """Pull Open-Meteo hourly forecast + air-quality. Returns merged dict
    with the same shape the browser already parses; never raises (any
    failure logs and returns an empty dict so the caller can degrade)."""
    fc_url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        "&hourly=uv_index,uv_index_clear_sky,cloud_cover,temperature_2m"
        "&daily=sunrise,sunset,uv_index_max"
        "&timezone=auto&past_days=2&forecast_days=1"
    )
    aq_url = (
        "https://air-quality-api.open-meteo.com/v1/air-quality"
        f"?latitude={lat}&longitude={lon}"
        "&hourly=pm10,pm2_5,nitrogen_dioxide,aerosol_optical_depth"
        "&current=pm2_5,pm10,european_aqi"
        "&past_days=2"
    )
    out: dict = {}
    try:
        async with httpx.AsyncClient(timeout=_OPENMETEO_TIMEOUT_SEC) as client:
            fc_resp, aq_resp = await _gather_safe(client.get(fc_url), client.get(aq_url))
            if fc_resp is not None and fc_resp.status_code == 200:
                out["forecast"] = fc_resp.json()
            if aq_resp is not None and aq_resp.status_code == 200:
                out["airQuality"] = aq_resp.json()
    except Exception as e:  # noqa: BLE001
        logger.warning("Open-Meteo fetch failed: %s", e)
    return out


async def _gather_safe(*coros):
    """asyncio.gather but tolerates per-coro exceptions — return None
    in their slot. Saves the caller from littering try/except for each
    side of the parallel fetch."""
    import asyncio

    results = await asyncio.gather(*coros, return_exceptions=True)
    return [None if isinstance(r, Exception) else r for r in results]

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

Resilience: a per-coord last-good cache (`_LAST_GOOD`) keeps the most
recent successful Open-Meteo forecast / airQuality response per
(rounded lat, rounded lon). When a fresh fetch times out or 5xxs we
serve the last-good entry instead of degrading to a 1-row CAMS-only
synthesised envelope (which would push the browser into the
sparse-uv merge branch and surface as `cams+open_meteo` source —
indistinguishable to users from a hard fallback). Cached entries
expire after `_LAST_GOOD_TTL_SEC`.
"""

from __future__ import annotations

import logging
import time
from collections import OrderedDict

import httpx

logger = logging.getLogger(__name__)

# 5s upstream timeout — Open-Meteo's free-tier P50 is ~150ms with a
# warm TLS connection, but cold-handshake + transient jitter can push
# real responses past 1.5s and trigger a sparse-envelope fallback.
# 5s gives genuine headroom while still degrading fast enough that a
# real outage doesn't block the /uv handler.
_OPENMETEO_TIMEOUT_SEC = 5.0

# Last-good cache: serves stale-but-valid responses when a fresh fetch
# fails. 30-minute TTL — Open-Meteo's hourly forecast updates hourly,
# so a 30-min-stale row is fine for a fallback band that only fires
# during transient upstream errors. Bounded LRU at 256 coord buckets.
_LAST_GOOD_TTL_SEC = 30 * 60
_LAST_GOOD_MAX_ENTRIES = 256
_LAST_GOOD: "OrderedDict[tuple[float, float], dict]" = OrderedDict()


def _coord_key(lat: float, lon: float) -> tuple[float, float]:
    """Quantise to 0.1° (~11 km) so nearby callers share cache slots
    without colliding across actually-distinct forecast cells.
    Matches the privacy rounding the browser applies before calling
    so cache hits land naturally."""
    return (round(float(lat), 1), round(float(lon), 1))


def _last_good_get(lat: float, lon: float) -> dict | None:
    """Return cached entry if fresh, else None. Touches LRU on hit."""
    key = _coord_key(lat, lon)
    entry = _LAST_GOOD.get(key)
    if entry is None:
        return None
    if time.time() - entry["stored_at"] > _LAST_GOOD_TTL_SEC:
        # Expired — drop and miss.
        _LAST_GOOD.pop(key, None)
        return None
    _LAST_GOOD.move_to_end(key)  # LRU touch
    return entry["data"]


def _last_good_put(lat: float, lon: float, data: dict) -> None:
    """Store a fresh entry, evicting the oldest if at capacity."""
    if not data:
        return  # never cache an empty dict — nothing to fall back to
    key = _coord_key(lat, lon)
    _LAST_GOOD[key] = {"stored_at": time.time(), "data": data}
    _LAST_GOOD.move_to_end(key)
    while len(_LAST_GOOD) > _LAST_GOOD_MAX_ENTRIES:
        _LAST_GOOD.popitem(last=False)


def _last_good_reset() -> None:
    """Test hook — clear the cache between cases."""
    _LAST_GOOD.clear()


async def fetch_openmeteo(
    lat: float,
    lon: float,
    client: httpx.AsyncClient | None = None,
) -> dict:
    """Pull Open-Meteo hourly forecast + air-quality. Returns merged dict
    with the same shape the browser already parses; never raises (any
    failure logs and returns either the per-coord last-good entry or
    an empty dict so the caller can degrade).

    Pass a shared `client` (created once at app startup) to amortise
    TLS handshake cost across requests — drops typical latency from
    ~700ms to ~150ms and squashes the timeout-induced sparse responses
    that surfaced as `cams+open_meteo` in the browser's source label.
    """
    # Cast defensively — caller is FastAPI with float Query validators,
    # but this also pins the host of the request away from any value
    # an attacker could substitute via the lat/lon channel.
    lat_f = float(lat)
    lon_f = float(lon)
    fc_url = "https://api.open-meteo.com/v1/forecast"
    fc_params = {
        "latitude": lat_f,
        "longitude": lon_f,
        "hourly": "uv_index,uv_index_clear_sky,cloud_cover,temperature_2m",
        "daily": "sunrise,sunset,uv_index_max",
        "timezone": "auto",
        "past_days": 2,
        "forecast_days": 1,
    }
    aq_url = "https://air-quality-api.open-meteo.com/v1/air-quality"
    aq_params = {
        "latitude": lat_f,
        "longitude": lon_f,
        "hourly": "pm10,pm2_5,nitrogen_dioxide,aerosol_optical_depth",
        "current": "pm2_5,pm10,european_aqi",
        "past_days": 2,
    }
    # `fresh` tracks ONLY data from the live fetch — never anything we
    # filled from the last-good cache. `out` is what we return to the
    # caller (fresh + cache-fill). Persisting `fresh` instead of `out`
    # is what prevents a stale-but-served cache entry from rewriting
    # itself with a fresh timestamp on every failed request — which
    # would defeat _LAST_GOOD_TTL_SEC during sustained Open-Meteo
    # outages (Greptile follow-up to #14).
    fresh: dict = {}
    # Use the shared client when available; fall back to a per-call
    # client so existing call sites and tests keep working. We rely
    # on the client's own Timeout config (the server creates the
    # shared one with `Timeout(5.0, connect=3.0)`) and do NOT pass a
    # per-request `timeout=` scalar — that would override every field
    # of the Timeout, collapsing the differentiated connect budget
    # back to a flat 5s (Greptile follow-up to #14).
    owned_client = client is None
    c = client or httpx.AsyncClient(timeout=httpx.Timeout(_OPENMETEO_TIMEOUT_SEC))
    try:
        fc_resp, aq_resp = await _gather_safe(
            c.get(fc_url, params=fc_params),
            c.get(aq_url, params=aq_params),
        )
        if fc_resp is not None and fc_resp.status_code == 200:
            try:
                fresh["forecast"] = fc_resp.json()
            except Exception as e:  # noqa: BLE001
                logger.warning("Open-Meteo forecast JSON parse failed: %s", e)
        elif fc_resp is not None:
            logger.warning("Open-Meteo forecast non-200: %s", fc_resp.status_code)
        if aq_resp is not None and aq_resp.status_code == 200:
            try:
                fresh["airQuality"] = aq_resp.json()
            except Exception as e:  # noqa: BLE001
                logger.warning("Open-Meteo airQuality JSON parse failed: %s", e)
    except Exception as e:  # noqa: BLE001
        logger.warning("Open-Meteo fetch failed: %s", e)
    finally:
        if owned_client:
            try:
                await c.aclose()
            except Exception:  # noqa: BLE001
                pass

    out: dict = dict(fresh)

    # Resilience step — if the forecast leg failed (this is the path
    # that produces sparse 1-row envelopes downstream), serve the
    # per-coord last-good entry. The CAMS overlay still runs against
    # the real snapshot so ozone/AOD reflect current state; only the
    # Open-Meteo-derived columns (uv_index, cloud_cover, temperature,
    # sunrise/sunset, daily peak) are stale-but-valid.
    if "forecast" not in out:
        cached = _last_good_get(lat_f, lon_f)
        if cached and "forecast" in cached:
            logger.info(
                "Open-Meteo forecast unavailable for (%.3f, %.3f); serving last-good cache",
                lat_f,
                lon_f,
            )
            out["forecast"] = cached["forecast"]
            # Only fill airQuality from cache if the live fetch also
            # missed — a fresh AQ response should win.
            if "airQuality" not in out and "airQuality" in cached:
                out["airQuality"] = cached["airQuality"]

    # Persist ONLY fresh data, never re-persist cache-served data.
    # The rule is "only refresh `stored_at` when forecast is fresh" —
    # otherwise a stale forecast served from cache would keep extending
    # its own TTL on every failing request. Fresh AQ alongside a stale
    # forecast still updates the entry's `data` (so the next fetch can
    # fall back to a more recent AQ) but the entry's `stored_at` stays
    # anchored to whenever the forecast was last successfully fetched.
    if "forecast" in fresh:
        # Fresh forecast → full refresh with current timestamp,
        # merging any still-fresh AQ from the prior entry so we don't
        # lose it if the AQ leg happened to fail this round.
        prior = _LAST_GOOD.get(_coord_key(lat_f, lon_f))
        prior_fresh = (
            prior["data"]
            if prior is not None
            and time.time() - prior["stored_at"] <= _LAST_GOOD_TTL_SEC
            else {}
        )
        _last_good_put(lat_f, lon_f, {**prior_fresh, **fresh})
    elif "airQuality" in fresh:
        # AQ-only fresh — update the existing entry's data in place
        # so future fallbacks see the more recent AQ, but PRESERVE
        # its `stored_at` so the cached forecast still expires on its
        # original schedule (defends against the stale-forever loop).
        key = _coord_key(lat_f, lon_f)
        prior = _LAST_GOOD.get(key)
        if (
            prior is not None
            and time.time() - prior["stored_at"] <= _LAST_GOOD_TTL_SEC
        ):
            prior["data"] = {**prior["data"], **fresh}
            _LAST_GOOD.move_to_end(key)  # LRU touch only

    return out


async def _gather_safe(*coros):
    """asyncio.gather but tolerates per-coro exceptions — return None
    in their slot. Saves the caller from littering try/except for each
    side of the parallel fetch."""
    import asyncio

    results = await asyncio.gather(*coros, return_exceptions=True)
    return [None if isinstance(r, Exception) else r for r in results]

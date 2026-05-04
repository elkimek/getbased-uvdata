"""Final response shape — Open-Meteo-compatible JSON + CAMS extras.

The browser's `js/sun-uvdata.js` already parses Open-Meteo's hourly
forecast shape. We mirror it exactly, then add CAMS-specific fields
under the same hourly array so the browser can opt in via:

    fcJson.hourly.ozone_du[idx]   // total column ozone, Dobson Units
    fcJson.hourly.aod[idx]        // 550 nm aerosol optical depth

When MERGE_OPENMETEO=1 the server produces a single merged response;
when 0, only the CAMS-driven fields are populated and the browser is
expected to keep doing its own Open-Meteo round trip.
"""

from __future__ import annotations

import datetime as _dt
import time
from typing import Any


def build_response(
    lat: float,
    lon: float,
    when_iso: str | None,
    cams_lookup: dict[str, float | None],
    openmeteo: dict[str, Any] | None,
    cams_pulled_at: float,
    snapshot_valid_from: float,
    snapshot_valid_to: float,
) -> dict[str, Any]:
    """Compose the JSON the browser sees."""
    when = _parse_iso(when_iso)
    if openmeteo and "forecast" in openmeteo:
        # Start from Open-Meteo's response, then overlay CAMS extras into
        # the hourly arrays. Index 0 alignment isn't required because the
        # browser does its own nearestHourIndex() scan.
        out = dict(openmeteo["forecast"])
        hourly = dict(out.get("hourly", {}))
        times = hourly.get("time", [])
        # Fill ozone_du + aod parallel to hourly.time. We only have one
        # CAMS lookup at the requested instant, so we broadcast it to
        # every hour in the response window — for the daily-scale
        # variables CAMS publishes (ozoneDU, AOD) the within-day
        # variation is small enough that this is acceptable for v0.1.
        # Future: per-hour CAMS interpolation across the snapshot's
        # leadtime axis.
        ozone_arr = [cams_lookup.get("ozoneDU")] * len(times)
        aod_arr = [cams_lookup.get("aod")] * len(times)
        hourly["ozone_du"] = ozone_arr
        hourly["aod"] = aod_arr
        out["hourly"] = hourly
        # Embed AQ into the same envelope so the browser's existing
        # hourly-parallel scanner finds AOD where it expects.
        if openmeteo.get("airQuality"):
            out["airQuality"] = openmeteo["airQuality"]
    else:
        # CAMS-only path — synthesise a minimal Open-Meteo-shaped
        # envelope so the browser's parser doesn't need a special case.
        # Single hour at `when`; the browser hour-bucket scanner snaps
        # to it for any nearby request.
        ts_iso = when.replace(microsecond=0, tzinfo=None).isoformat(timespec="minutes")
        out = {
            "latitude": lat,
            "longitude": lon,
            "timezone": "GMT",
            "utc_offset_seconds": 0,
            "hourly": {
                "time": [ts_iso],
                "uv_index": [None],
                "uv_index_clear_sky": [None],
                "cloud_cover": [None],
                "temperature_2m": [None],
                "ozone_du": [cams_lookup.get("ozoneDU")],
                "aod": [cams_lookup.get("aod")],
            },
            "daily": {
                "time": [when.date().isoformat()],
                "sunrise": [None],
                "sunset": [None],
                "uv_index_max": [None],
            },
        }

    # CAMS provenance block — the browser uses it for the source dot +
    # the "via CAMS" label and to distinguish stale-grid responses from
    # fresh ones.
    out["_camsMeta"] = {
        "pulledAt": cams_pulled_at,
        "validFrom": snapshot_valid_from,
        "validTo": snapshot_valid_to,
        "ageSec": time.time() - cams_pulled_at,
        "source": "cams",
    }
    return out


def _parse_iso(s: str | None) -> _dt.datetime:
    if not s:
        return _dt.datetime.now(_dt.UTC).replace(tzinfo=None)
    try:
        return _dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return _dt.datetime.now(_dt.UTC).replace(tzinfo=None)

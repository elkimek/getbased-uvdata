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
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from .cams import GridSnapshot


def build_response(
    lat: float,
    lon: float,
    when_iso: str | None,
    cams_lookup: dict[str, float | None],
    openmeteo: dict[str, Any] | None,
    cams_pulled_at: float,
    snapshot_valid_from: float,
    snapshot_valid_to: float,
    snapshot: "GridSnapshot | None" = None,
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
        # Per-hour CAMS lookup against the snapshot, so different times
        # in the response window get the right ozone/AOD (e.g. ozone
        # has a real diurnal cycle of ~10-15 DU; broadcasting a single
        # value across 24 h would muddy hours far from `when`). Fall
        # back to the broadcast scalar when no snapshot is plumbed
        # through (older callers, tests).
        fc_offset_s = float(out.get("utc_offset_seconds") or 0)
        if snapshot is not None and times:
            ozone_arr: list[float | None] = []
            aod_arr: list[float | None] = []
            for t_str in times:
                t_epoch = _open_meteo_time_to_epoch(t_str, fc_offset_s)
                hourly_lookup = snapshot.lookup(lat, lon, t_epoch) if t_epoch is not None else cams_lookup
                ozone_arr.append(hourly_lookup.get("ozoneDU"))
                aod_arr.append(hourly_lookup.get("aod"))
        else:
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


def _open_meteo_time_to_epoch(t_str: str, utc_offset_s: float) -> float | None:
    """Open-Meteo with `timezone=auto` returns naive local-clock strings
    like '2024-06-01T13:00'. Multiply through utc_offset_seconds to get
    real UTC epoch — matches js/sun-uvdata.js's nearestHourIndex math
    so hourly indices align between server and browser."""
    if not isinstance(t_str, str):
        return None
    try:
        d = _dt.datetime.fromisoformat(t_str.replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is not None:
        return d.timestamp()
    # Naive local-clock string; subtract utc_offset to get UTC epoch.
    return d.replace(tzinfo=_dt.UTC).timestamp() - utc_offset_s

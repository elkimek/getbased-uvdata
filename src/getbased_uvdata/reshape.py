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
import math
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
        # in the response window get the right values (ozone has a real
        # diurnal cycle of ~10-15 DU; PM2.5 swings 3-5× over a day in
        # cities). Fall back to the broadcast scalar when no snapshot
        # is plumbed through (older callers, tests).
        # `utc_offset_seconds`: distinguish missing (None → can't parse
        # the local-clock strings safely; broadcast) from explicit 0
        # (UTC; parse as-is). Open-Meteo always emits the field with
        # timezone=auto, but a future endpoint variant or partial
        # response shouldn't silently mis-shift hours.
        raw_offset = out.get("utc_offset_seconds")
        fc_offset_s: float | None = (
            float(raw_offset) if isinstance(raw_offset, (int, float)) else None
        )
        if fc_offset_s is None and times:
            import logging as _logging

            _logging.getLogger(__name__).warning(
                "Open-Meteo response missing utc_offset_seconds; "
                "broadcasting CAMS scalar across hourly array"
            )
        # Field roster — keys here must match GridSnapshot.lookup() output.
        cams_field_keys = [
            "uvIndexCams",
            "uvClearSkyCams",
            "ozoneDU",
            "aod",
            "pm25",
            "pm10",
        ]
        cams_field_to_hourly = {
            "uvIndexCams": "uv_index_cams_total_sky",
            "uvClearSkyCams": "uv_index_cams_clear_sky",
            "ozoneDU": "ozone_du",
            "aod": "aod",
            "pm25": "pm2_5",
            "pm10": "pm10",
        }
        # Initialise per-field arrays — only emit a key in `hourly` if
        # SOME hour got a real value (avoids polluting the response
        # with all-None columns when a field is absent from the
        # snapshot, e.g. an older persisted cache without AQ fields).
        per_field: dict[str, list[float | None]] = {k: [] for k in cams_field_keys}
        for t_str in times:
            t_epoch = None
            if snapshot is not None and fc_offset_s is not None:
                t_epoch = _open_meteo_time_to_epoch(t_str, fc_offset_s)
            lookup = (
                snapshot.lookup(lat, lon, t_epoch)
                if (snapshot is not None and t_epoch is not None)
                else cams_lookup
            )
            cams_time_usable = lookup.get("_camsTimeInRange", True) is not False
            for k in cams_field_keys:
                per_field[k].append(lookup.get(k) if cams_time_usable else None)
        for cams_key, hourly_key in cams_field_to_hourly.items():
            arr = per_field[cams_key]
            if any(_finite(v) is not None for v in arr):
                arr = [_finite(v) for v in arr]
                hourly[hourly_key] = arr

        # Preserve Open-Meteo's model UVI for diagnostics/fallback, then
        # promote CAMS direct UVBED to the standard fields. Where recent
        # DWD/EUMETSAT radiation observations are available, apply their
        # bounded observed/clear-sky ratio to CAMS clear-sky UVI. This is
        # a broadband cloud-modification estimate, not a UV measurement,
        # so provenance remains explicit in a parallel source array.
        om_uvi = list(hourly.get("uv_index", []))
        om_clear = list(hourly.get("uv_index_clear_sky", []))
        if om_uvi:
            hourly["uv_index_open_meteo"] = om_uvi
        if om_clear:
            hourly["uv_index_clear_sky_open_meteo"] = om_clear
        cams_uvi = per_field["uvIndexCams"]
        cams_clear = per_field["uvClearSkyCams"]
        satellite = (openmeteo or {}).get("satellite") or {}
        satellite_sources = _satellite_cloud_factors(satellite)
        fused_uvi: list[float | None] = []
        fused_clear: list[float | None] = []
        fused_sources: list[str] = []
        satellite_adjusted: list[float | None] = []
        for i, t_str in enumerate(times):
            direct = _finite(cams_uvi[i] if i < len(cams_uvi) else None)
            clear = _finite(cams_clear[i] if i < len(cams_clear) else None)
            om_value = _finite(om_uvi[i] if i < len(om_uvi) else None)
            om_clear_value = _finite(om_clear[i] if i < len(om_clear) else None)
            factor = _nearest_satellite_factor(t_str, fc_offset_s, satellite_sources)
            adjusted = clear * factor if clear is not None and factor is not None else None
            satellite_adjusted.append(adjusted)
            if adjusted is not None:
                fused_uvi.append(round(adjusted, 3))
                fused_sources.append("cams_uvbedcs+satellite_cmf")
            elif direct is not None:
                fused_uvi.append(direct)
                fused_sources.append("cams_uvbed")
            else:
                fused_uvi.append(om_value)
                fused_sources.append("open_meteo_gfs" if om_value is not None else "unavailable")
            fused_clear.append(clear if clear is not None else om_clear_value)
        if any(v is not None for v in fused_uvi):
            hourly["uv_index"] = fused_uvi
            hourly["uv_index_source"] = fused_sources
        if any(v is not None for v in fused_clear):
            hourly["uv_index_clear_sky"] = fused_clear
        if any(v is not None for v in satellite_adjusted):
            hourly["uv_index_satellite_adjusted"] = satellite_adjusted
        out["hourly"] = hourly

        # Keep the provider's current block aligned with the fused hourly
        # series; otherwise the browser would prefer Open-Meteo's current
        # UVI and silently bypass CAMS for requests near wall-clock now.
        current = dict(out.get("current", {}))
        current_time = current.get("time")
        if current_time and hourly.get("uv_index"):
            current_idx = _nearest_time_index(times, current_time, fc_offset_s)
            if current_idx >= 0:
                current["uv_index_open_meteo"] = current.get("uv_index")
                current["uv_index"] = hourly["uv_index"][current_idx]
                current["uv_index_clear_sky"] = hourly["uv_index_clear_sky"][current_idx]
                current["uv_index_source"] = hourly["uv_index_source"][current_idx]
                out["current"] = current

        _replace_daily_uv_peak(out, hourly)
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
        cams_time_usable = cams_lookup.get("_camsTimeInRange", True) is not False
        cams_uvi = _finite(cams_lookup.get("uvIndexCams")) if cams_time_usable else None
        cams_clear = _finite(cams_lookup.get("uvClearSkyCams")) if cams_time_usable else None
        out = {
            "latitude": lat,
            "longitude": lon,
            "timezone": "GMT",
            "utc_offset_seconds": 0,
            "hourly": {
                "time": [ts_iso],
                "uv_index": [cams_uvi],
                "uv_index_clear_sky": [cams_clear],
                "uv_index_cams_total_sky": [cams_uvi],
                "uv_index_cams_clear_sky": [cams_clear],
                "uv_index_source": ["cams_uvbed" if cams_uvi is not None else "unavailable"],
                "cloud_cover": [None],
                "temperature_2m": [None],
                "ozone_du": [cams_lookup.get("ozoneDU") if cams_time_usable else None],
                "aod": [cams_lookup.get("aod") if cams_time_usable else None],
                **(
                    {"pm2_5": [cams_lookup.get("pm25")]}
                    if cams_time_usable and cams_lookup.get("pm25") is not None
                    else {}
                ),
                **(
                    {"pm10": [cams_lookup.get("pm10")]}
                    if cams_time_usable and cams_lookup.get("pm10") is not None
                    else {}
                ),
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
    requested_time_in_range = cams_lookup.get("_camsTimeInRange", True) is not False
    out["_camsMeta"] = {
        "pulledAt": cams_pulled_at,
        "validFrom": snapshot_valid_from,
        "validTo": snapshot_valid_to,
        "ageSec": time.time() - cams_pulled_at,
        "source": "cams_global_forecast",
        "gridResolutionDeg": 0.4,
        "requestedTimeInRange": requested_time_in_range,
        "requestedValidTime": cams_lookup.get("_camsValidTime"),
        "directUv": requested_time_in_range and _finite(cams_lookup.get("uvIndexCams")) is not None,
    }
    if openmeteo:
        out["_openMeteoMeta"] = dict(openmeteo.get("meta") or {})
    requested_source = _requested_uv_source(out, when_iso)
    out["_fieldSources"] = {
        "uvIndex": requested_source,
        "uvClearSky": (
            "cams_uvbedcs"
            if requested_time_in_range and _finite(cams_lookup.get("uvClearSkyCams")) is not None
            else ("open_meteo_gfs" if openmeteo and "forecast" in openmeteo else "unavailable")
        ),
        "ozoneDU": "cams_global_forecast" if requested_time_in_range else "unavailable",
        "aod": "cams_global_forecast" if requested_time_in_range else "unavailable",
        "cloudCover": "open_meteo_best_match"
        if openmeteo and "forecast" in openmeteo
        else "unavailable",
        "temperature": "open_meteo_best_match"
        if openmeteo and "forecast" in openmeteo
        else "unavailable",
    }
    return out


def _finite(value: Any) -> float | None:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if math.isfinite(numeric) else None


def _nearest_time_index(times: list, target: str, offset_s: float | None) -> int:
    target_epoch = _open_meteo_time_to_epoch(target, offset_s or 0)
    if target_epoch is None:
        return -1
    best = (-1, float("inf"))
    for i, value in enumerate(times):
        epoch = _open_meteo_time_to_epoch(value, offset_s or 0)
        if epoch is None:
            continue
        delta = abs(epoch - target_epoch)
        if delta < best[1]:
            best = (i, delta)
    return best[0]


def _satellite_cloud_factors(satellite: dict[str, Any]) -> list[tuple[float, float]]:
    hourly = satellite.get("hourly", {}) if isinstance(satellite, dict) else {}
    times = hourly.get("time", [])
    observed = hourly.get("shortwave_radiation_instant", [])
    clear = hourly.get("shortwave_radiation_clear_sky_instant", [])
    offset = satellite.get("utc_offset_seconds", 0) if isinstance(satellite, dict) else 0
    factors: list[tuple[float, float]] = []
    for i, t_str in enumerate(times):
        obs = _finite(observed[i] if i < len(observed) else None)
        clr = _finite(clear[i] if i < len(clear) else None)
        epoch = _open_meteo_time_to_epoch(t_str, float(offset or 0))
        if epoch is None or obs is None or clr is None or clr < 20:
            continue
        factors.append((epoch, max(0.0, min(1.3, obs / clr))))
    return factors


def _nearest_satellite_factor(
    provider_time: str,
    provider_offset_s: float | None,
    factors: list[tuple[float, float]],
) -> float | None:
    target = _open_meteo_time_to_epoch(provider_time, provider_offset_s or 0)
    if target is None or not factors:
        return None
    epoch, factor = min(factors, key=lambda item: abs(item[0] - target))
    return factor if abs(epoch - target) <= 45 * 60 else None


def _replace_daily_uv_peak(out: dict[str, Any], hourly: dict[str, Any]) -> None:
    daily = dict(out.get("daily", {}))
    days = daily.get("time", [])
    times = hourly.get("time", [])
    values = hourly.get("uv_index", [])
    sources = hourly.get("uv_index_source", [])
    if not days or not times or not values:
        return
    prior = list(daily.get("uv_index_max", []))
    peaks: list[float | None] = []
    peak_times: list[str | None] = []
    peak_sources: list[str] = []
    for day_index, day in enumerate(days):
        candidates = [
            (i, _finite(values[i]))
            for i, t_str in enumerate(times)
            if i < len(values) and isinstance(t_str, str) and t_str.startswith(str(day))
        ]
        candidates = [(i, value) for i, value in candidates if value is not None]
        if candidates:
            peak_i, peak = max(candidates, key=lambda item: item[1])
            peaks.append(peak)
            peak_times.append(times[peak_i])
            peak_sources.append(sources[peak_i] if peak_i < len(sources) else "unknown")
        else:
            peaks.append(_finite(prior[day_index] if day_index < len(prior) else None))
            peak_times.append(None)
            peak_sources.append("open_meteo_gfs")
    if prior:
        daily["uv_index_max_open_meteo"] = prior
    daily["uv_index_max"] = peaks
    daily["uv_index_max_at"] = peak_times
    daily["uv_index_max_source"] = peak_sources
    out["daily"] = daily


def _requested_uv_source(out: dict[str, Any], when_iso: str | None) -> str:
    hourly = out.get("hourly", {})
    times = hourly.get("time", [])
    sources = hourly.get("uv_index_source", [])
    offset = out.get("utc_offset_seconds", 0)
    if times and sources:
        target = when_iso or _dt.datetime.now(_dt.UTC).isoformat()
        parsed_target = _parse_iso(target)
        target_epoch = (
            parsed_target.replace(tzinfo=_dt.UTC).timestamp()
            if parsed_target.tzinfo is None
            else parsed_target.timestamp()
        )
        best = -1
        delta = float("inf")
        for i, t_str in enumerate(times):
            epoch = _open_meteo_time_to_epoch(t_str, float(offset or 0))
            if epoch is not None and abs(epoch - target_epoch) < delta:
                best, delta = i, abs(epoch - target_epoch)
        if 0 <= best < len(sources):
            return str(sources[best])
    return "cams_uvbed" if out.get("_camsMeta", {}).get("directUv") else "open_meteo_gfs"


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

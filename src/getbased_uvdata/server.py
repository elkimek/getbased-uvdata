"""FastAPI server — `/uv` endpoint + health + metadata."""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response

import time as _time

from . import __version__
from .auth import check_bearer, required_bearer
from .cams import CamsCache, background_pull_loop
from .openmeteo import fetch_openmeteo
from .reshape import build_response
from .spectrum import (
    reconstruct_spectrum,
    solar_zenith_angle,
    uvi_from_spectrum,
)


# Counter state — small, in-memory, no Prometheus client lib dep.
# Increments race-free under FastAPI's single-process model; if you
# scale horizontally, scrape each worker independently or front them
# with a histogram-friendly aggregator.
_metrics: dict[str, int | float] = {
    "uv_requests_total": 0,
    "uv_requests_2xx": 0,
    "uv_requests_4xx": 0,
    "uv_requests_5xx": 0,
    "uv_request_duration_sum_sec": 0.0,
    "openmeteo_merges_total": 0,
    "openmeteo_merge_failures_total": 0,
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s :: %(message)s")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    cache_dir = os.environ.get("CAMS_CACHE_DIR", "/data").strip() or None
    cache = CamsCache(cache_dir=cache_dir)
    interval = int(os.environ.get("CAMS_PULL_INTERVAL_SEC", "21600"))
    task = asyncio.create_task(background_pull_loop(cache, interval))
    app.state.cams = cache
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(title="getbased-uvdata", version=__version__, lifespan=lifespan)

# CORS — locked to the production app + localhost:8000 dev. Self-hosters
# who run the app on a custom domain set ALLOWED_ORIGINS env (comma-
# separated) to add their own.
_DEFAULT_ORIGINS = [
    "https://app.getbased.health",
    "https://getbased.health",
    "http://localhost:8000",
]
_extra = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_DEFAULT_ORIGINS + _extra,
    allow_methods=["GET", "OPTIONS"],
    allow_headers=["authorization", "content-type"],
)


def _get_cache(request: Request) -> CamsCache:
    return request.app.state.cams


@app.get("/healthz")
async def healthz(request: Request) -> dict:
    cache: CamsCache = request.app.state.cams
    snap = cache.snapshot
    return {
        "ok": snap is not None,
        "version": __version__,
        "auth": "bearer" if required_bearer() else "open",
        "cams": {
            "pulled_at": snap.pulled_at if snap else None,
            "valid_from": snap.valid_from if snap else None,
            "valid_to": snap.valid_to if snap else None,
            "stale": cache.is_stale,
            "last_error": cache.last_error,
        },
    }


@app.get("/")
async def root() -> dict:
    """Friendly index — what is this server, where to look for what."""
    return {
        "service": "getbased-uvdata",
        "version": __version__,
        "docs": "https://github.com/elkimek/getbased-uvdata",
        "endpoints": {
            "GET /uv?latitude=&longitude=&time=": "per-coord CAMS atmosphere snapshot, Open-Meteo-shaped",
            "GET /healthz": "liveness + grid metadata",
        },
    }


@app.get("/uv")
async def uv(
    request: Request,
    latitude: float = Query(..., ge=-90, le=90),
    longitude: float = Query(..., ge=-180, le=180),
    time: str | None = Query(None, description="ISO-8601 instant; defaults to now."),
):
    """Per-coord/per-hour CAMS atmosphere snapshot, optionally merged
    with Open-Meteo. Response shape mirrors Open-Meteo's hourly
    forecast so the browser's existing parser ingests it directly."""
    check_bearer(request)
    cache: CamsCache = request.app.state.cams
    snap = cache.snapshot
    if snap is None:
        raise HTTPException(
            status_code=503,
            detail=f"CAMS grid not yet available. Last error: {cache.last_error or 'still pulling'}",
        )

    started = _time.monotonic()
    _metrics["uv_requests_total"] += 1

    when_iso = time or _now_iso_utc()
    when_epoch = _iso_to_epoch(when_iso)
    cams_lookup = snap.lookup(latitude, longitude, when_epoch)

    merge = os.environ.get("MERGE_OPENMETEO", "1") not in ("0", "false", "no", "")
    om = None
    if merge:
        _metrics["openmeteo_merges_total"] += 1
        om = await fetch_openmeteo(latitude, longitude)
        if not om:
            _metrics["openmeteo_merge_failures_total"] += 1

    body = build_response(
        lat=latitude,
        lon=longitude,
        when_iso=when_iso,
        cams_lookup=cams_lookup,
        openmeteo=om,
        cams_pulled_at=snap.pulled_at,
        snapshot_valid_from=snap.valid_from,
        snapshot_valid_to=snap.valid_to,
        snapshot=snap,
    )
    # Server-computed daily peak UVI: scan today's hours, run the
    # spectrum reconstruction at each, take the max. Cheap (~25 spectrum
    # calcs at 5-nm grid) and gives a number that beats Open-Meteo's
    # pre-computed peak because it's fed real CAMS ozone + AOD per hour.
    # Overlays into the existing `daily.uv_index_max` slot so the browser
    # picks it up via its existing parser without further changes.
    try:
        daily = body.setdefault("daily", {})
        peak, peak_at = _daily_peak_uvi(snap, latitude, longitude, when_epoch)
        if peak is not None:
            daily["uv_index_max_cams"] = [round(peak, 2)]
            if peak_at is not None:
                daily["uv_index_max_cams_at"] = [
                    _dt_to_iso(peak_at)
                ]
    except Exception as e:  # noqa: BLE001 — daily peak is bonus; never break /uv
        logger.warning("Daily peak UVI computation failed: %s", e)
    # Stale-grid header — monitors / browser can detect silent
    # staleness without parsing _camsMeta. Body still serves so the
    # session can complete; the browser's own freshness UI flags it.
    headers = {}
    if cache.is_stale:
        headers["X-Cams-Stale"] = "1"
    if cache.last_error:
        headers["X-Cams-Last-Error"] = cache.last_error[:200].replace("\n", " ")
    _metrics["uv_requests_2xx"] += 1
    _metrics["uv_request_duration_sum_sec"] += _time.monotonic() - started
    return JSONResponse(content=body, headers=headers)


@app.get("/spectrum")
async def spectrum(
    request: Request,
    latitude: float = Query(..., ge=-90, le=90),
    longitude: float = Query(..., ge=-180, le=180),
    time: str | None = Query(None, description="ISO-8601 instant; defaults to now."),
    altitude_m: float = Query(0.0, ge=0, le=9000),
    cloud_cover: float = Query(0.0, ge=0, le=1),
):
    """Server-side Bird-Riordan reconstruction fed by REAL CAMS ozone +
    AOD. Returns the wavelength-resolved surface UV spectrum (W/m²/nm)
    plus the integrated UVI. Browsers can ingest the spectrum directly
    through their existing channel-action-spectrum machinery, replacing
    the client-side reconstruction step entirely.

    Why this matters: client-side Bird-Riordan with Open-Meteo's missing
    ozone + AOD lands in a ±20-45% uncertainty band; the same engine
    fed CAMS values collapses to ±10-15% in the UV sweet-spot."""
    check_bearer(request)
    cache: CamsCache = request.app.state.cams
    snap = cache.snapshot
    if snap is None:
        raise HTTPException(
            status_code=503,
            detail=f"CAMS grid not yet available. Last error: {cache.last_error or 'still pulling'}",
        )

    when_iso = time or _now_iso_utc()
    when_epoch = _iso_to_epoch(when_iso)
    cams_lookup = snap.lookup(latitude, longitude, when_epoch)
    zenith = solar_zenith_angle(when_epoch, latitude, longitude)
    spec = reconstruct_spectrum(
        zenith_deg=zenith,
        ozone_du=cams_lookup.get("ozoneDU") or 300.0,
        altitude_m=altitude_m,
        cloud_cover=cloud_cover,
        aod=cams_lookup.get("aod"),
    )
    uvi = uvi_from_spectrum(spec)
    return {
        "latitude": latitude,
        "longitude": longitude,
        "time": when_iso,
        "zenithDeg": zenith,
        "uvIndex": uvi,
        "wavelengths": spec.wavelengths,
        "irradiance": spec.irradiance,
        "atmosphere": {
            "ozoneDU": cams_lookup.get("ozoneDU"),
            "aod": cams_lookup.get("aod"),
            "cloudCover": cloud_cover,
            "altitudeM": altitude_m,
        },
        "_camsMeta": {
            "pulledAt": snap.pulled_at,
            "validFrom": snap.valid_from,
            "validTo": snap.valid_to,
            "ageSec": _time.time() - snap.pulled_at,
            "source": "cams-bird-riordan",
        },
    }


@app.get("/metrics")
async def metrics(request: Request) -> Response:
    """Prometheus-compatible plain-text exposition format. Includes the
    rolling counters above plus snapshot freshness so a scrape detects
    silent CAMS-pull failure or grid drift without needing a special
    monitoring agent. No bearer required — same posture as /healthz."""
    cache: CamsCache = request.app.state.cams
    snap = cache.snapshot
    lines: list[str] = []
    lines.append("# HELP getbased_uvdata_info Build metadata.")
    lines.append("# TYPE getbased_uvdata_info gauge")
    lines.append(f'getbased_uvdata_info{{version="{__version__}"}} 1')
    for k, v in _metrics.items():
        lines.append(f"# TYPE getbased_uvdata_{k} counter")
        lines.append(f"getbased_uvdata_{k} {v}")
    if snap is not None:
        age = _time.time() - snap.pulled_at
        lines.append("# TYPE getbased_uvdata_snapshot_age_seconds gauge")
        lines.append(f"getbased_uvdata_snapshot_age_seconds {age:.0f}")
        lines.append("# TYPE getbased_uvdata_snapshot_timesteps gauge")
        lines.append(f"getbased_uvdata_snapshot_timesteps {len(snap.times)}")
    lines.append("# TYPE getbased_uvdata_snapshot_stale gauge")
    lines.append(f"getbased_uvdata_snapshot_stale {1 if cache.is_stale else 0}")
    # Lifetime pull counters — useful to alert on sustained failure.
    # is_stale only flips after 24 h; this surfaces problems within
    # one retry cycle (~minutes).
    lines.append("# TYPE getbased_uvdata_pull_attempts_total counter")
    lines.append(f"getbased_uvdata_pull_attempts_total {getattr(cache, 'pull_attempts', 0)}")
    lines.append("# TYPE getbased_uvdata_pull_successes_total counter")
    lines.append(f"getbased_uvdata_pull_successes_total {getattr(cache, 'pull_successes', 0)}")
    lines.append("# TYPE getbased_uvdata_pull_failures_total counter")
    lines.append(f"getbased_uvdata_pull_failures_total {getattr(cache, 'pull_failures', 0)}")
    return Response(content="\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")


def _daily_peak_uvi(
    snap: "CamsCache.snapshot",  # type: ignore[name-defined]
    lat: float,
    lon: float,
    around_epoch: float,
) -> tuple[float | None, float | None]:
    """Scan ±12 hours around `around_epoch`, run the spectrum at each
    snapshot timestep, return (peak UVI, epoch of peak). Returns
    (None, None) when no timestep falls in the window."""
    if snap is None:
        return None, None
    window_low = around_epoch - 12 * 3600
    window_high = around_epoch + 12 * 3600
    best_uvi: float | None = None
    best_t: float | None = None
    for t_epoch in snap.times:
        if t_epoch < window_low or t_epoch > window_high:
            continue
        zenith = solar_zenith_angle(float(t_epoch), lat, lon)
        if zenith >= 90:  # sun below horizon — UVI is zero by definition
            continue
        lookup = snap.lookup(lat, lon, float(t_epoch))
        spec = reconstruct_spectrum(
            zenith_deg=zenith,
            ozone_du=lookup.get("ozoneDU") or 300.0,
            altitude_m=0,
            cloud_cover=0,
            aod=lookup.get("aod"),
        )
        u = uvi_from_spectrum(spec)
        if best_uvi is None or u > best_uvi:
            best_uvi = u
            best_t = float(t_epoch)
    return best_uvi, best_t


def _dt_to_iso(epoch: float) -> str:
    import datetime as dt
    return dt.datetime.fromtimestamp(epoch, tz=dt.UTC).strftime("%Y-%m-%dT%H:%M")


def _now_iso_utc() -> str:
    import datetime as dt
    return dt.datetime.now(dt.UTC).replace(microsecond=0, tzinfo=None).isoformat() + "Z"


def _iso_to_epoch(iso: str) -> float:
    import datetime as dt
    s = iso.replace("Z", "+00:00")
    try:
        d = dt.datetime.fromisoformat(s)
    except ValueError:
        d = dt.datetime.now(dt.UTC)
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.UTC)
    return d.timestamp()


def main() -> None:
    """Console-script entry point — `getbased-uvdata [doctor]`.

    With no args: starts the HTTP server (the normal mode).
    With `doctor`: runs a one-shot self-test — env validation + a
    single CAMS pull + a sample lookup. Exits non-zero on any
    problem so it's useful in CI / pre-flight scripts.
    """
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "doctor":
        sys.exit(_doctor())
    import uvicorn
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8324"))
    uvicorn.run("getbased_uvdata.server:app", host=host, port=port, log_level="info")


def _doctor() -> int:
    """One-shot env + CAMS connectivity check. Returns shell exit code."""
    print(f"getbased-uvdata doctor v{__version__}")
    print("=" * 60)
    ok = True

    # Env presence checks — fail fast if the operator forgot to source .env.
    cams_key = os.environ.get("CAMS_API_KEY", "").strip()
    if not cams_key:
        print("✗ CAMS_API_KEY not set (register at https://ads.atmosphere.copernicus.eu and put your key in .env)")
        ok = False
    else:
        masked = cams_key[:6] + "…" + cams_key[-4:] if len(cams_key) > 12 else "(short)"
        print(f"✓ CAMS_API_KEY set [{masked}]")

    bearer = os.environ.get("GETBASED_UVDATA_BEARER", "").strip()
    if not bearer:
        print("⚠ GETBASED_UVDATA_BEARER unset — server will run in OPEN mode (anyone reaching the port can query CAMS).")
    else:
        print(f"✓ GETBASED_UVDATA_BEARER set [{len(bearer)} chars]")

    bbox = os.environ.get("CAMS_BBOX", "90,-180,-90,180")
    print(f"✓ CAMS_BBOX={bbox}")

    if not ok:
        print("\nFix the ✗ items above before running the server.")
        return 1

    # Live pull — this is the slow part; surface progress so the user
    # knows the doctor isn't hung.
    print("\nAttempting a live CAMS pull (30 s – 5 min depending on CDS queue)…")
    try:
        from .cams import _pull_cams_blocking  # type: ignore
        snap = _pull_cams_blocking()
    except Exception as e:  # noqa: BLE001
        print(f"✗ CAMS pull failed: {type(e).__name__}: {e}")
        return 2
    print(f"✓ CAMS pull OK — {len(snap.times)} hourly steps, {len(snap.lats)} lats, {len(snap.lons)} lons")

    # Sample lookup at a fixed point.
    sample = snap.lookup(lat=50.0, lon=14.0, when_epoch=snap.times[0])
    print(f"✓ Sample at (50N, 14E) → ozoneDU={sample['ozoneDU']:.1f}  AOD={sample['aod']:.3f}")
    print("\nAll checks passed. Run `getbased-uvdata` (no args) to start the server.")
    return 0


if __name__ == "__main__":
    main()

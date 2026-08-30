"""FastAPI server — `/uv` endpoint + health + metadata."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
import logging
import os
import re
from contextlib import asynccontextmanager

import httpx
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
    "rate_limited_total": 0,
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
    # Shared httpx client for the Open-Meteo merge path. Keeps TLS
    # connections warm across requests, which drops typical merge-leg
    # latency from ~700ms (cold handshake per call) to ~150ms and
    # squashes the timeout-induced sparse responses that caused the
    # browser to label the source as `cams+open_meteo`.
    app.state.openmeteo_client = httpx.AsyncClient(
        timeout=httpx.Timeout(5.0, connect=3.0),
        limits=httpx.Limits(max_keepalive_connections=8, max_connections=32),
    )
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        # Expected — we just cancelled the task; awaiting re-raises so
        # the loop can finish its `finally` blocks. Nothing to handle.
        pass
    try:
        await app.state.openmeteo_client.aclose()
    except Exception:  # noqa: BLE001
        # Shutdown path — never let a cleanup error mask the real
        # exit reason.
        pass


app = FastAPI(title="getbased-uvdata", version=__version__, lifespan=lifespan)

# CORS — locked to the production app domain. Local dev (e.g. the
# Lab Charts dev server on localhost:8000) needs ALLOWED_ORIGINS set
# explicitly to add its origin; we don't bake it into the default
# because every public deploy then accepts CORS-credentialed XHR from
# any local app on the user's machine, which is a credential-replay
# surface when the bearer is shared with self-hosters.
_DEFAULT_ORIGINS = [
    "https://app.getbased.health",
    "https://getbased.health",
]
# Validate operator-supplied origins so a typo doesn't silently produce
# an "any" semantic match. Each must be a fully-qualified scheme://host
# (port optional). Invalid entries logged + dropped.
_ORIGIN_RE = re.compile(r"^https?://[a-z0-9.-]+(:\d+)?$")
_extra: list[str] = []
for raw in os.environ.get("ALLOWED_ORIGINS", "").split(","):
    o = raw.strip().lower()
    if not o:
        continue
    if not _ORIGIN_RE.match(o):
        logging.getLogger(__name__).warning(
            "Dropping invalid ALLOWED_ORIGINS entry: %r (must be scheme://host[:port])", raw
        )
        continue
    _extra.append(o)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_DEFAULT_ORIGINS + _extra,
    allow_methods=["GET", "OPTIONS"],
    allow_headers=["authorization", "content-type"],
)

# Per-source request limiting lives in the application instead of relying on a
# non-standard Caddy module. The hosted deployment is reachable only through
# the local reverse proxy, so its left-most X-Forwarded-For address is the real
# client. Self-hosters can leave UVDATA_TRUST_PROXY unset and key on the direct
# peer instead. The default is deliberately generous for shared networks.
_RATE_LIMIT_PER_MINUTE = int(os.environ.get("UVDATA_RATE_LIMIT_PER_MINUTE", "300"))
_TRUST_PROXY = os.environ.get("UVDATA_TRUST_PROXY", "").lower() in ("1", "true", "yes")
_RATE_BUCKETS: OrderedDict[str, tuple[float, int]] = OrderedDict()
_RATE_BUCKET_MAX = 10_000


def _rate_limit_check(ip: str, now: float | None = None, limit: int | None = None) -> bool:
    """Fixed one-minute window with bounded source tracking."""
    current = _time.monotonic() if now is None else now
    maximum = _RATE_LIMIT_PER_MINUTE if limit is None else limit
    if maximum <= 0:
        return True
    started, count = _RATE_BUCKETS.pop(ip, (current, 0))
    if current - started >= 60:
        started, count = current, 0
    count += 1
    _RATE_BUCKETS[ip] = (started, count)
    while len(_RATE_BUCKETS) > _RATE_BUCKET_MAX:
        _RATE_BUCKETS.popitem(last=False)
    return count <= maximum


def _request_ip(request: Request) -> str:
    if _TRUST_PROXY:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",", 1)[0].strip()[:64]
    return (request.client.host if request.client else "unknown")[:64]


@app.middleware("http")
async def security_and_rate_limit(request: Request, call_next):
    protected = request.url.path in ("/uv", "/spectrum", "/metrics")
    if protected and not _rate_limit_check(_request_ip(request)):
        _metrics["rate_limited_total"] += 1
        return JSONResponse(
            status_code=429,
            content={"detail": "rate_limited"},
            headers={"Retry-After": "60", "Cache-Control": "no-store"},
        )
    response = await call_next(request)
    response.headers.setdefault("Cache-Control", "no-store")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    return response


def _get_cache(request: Request) -> CamsCache:
    return request.app.state.cams


@app.get("/healthz")
async def healthz(request: Request) -> dict:
    """Liveness probe — minimal information leak. Detailed pull state
    (last_error, lifetime counters) lives behind the bearer on /metrics
    so unauthenticated callers can't side-channel CDS state."""
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
        # Use the shared client when lifespan wired one; tests that
        # spin up `app` without lifespan (TestClient without `with`)
        # fall through to a per-call client so they keep working.
        shared_client = getattr(request.app.state, "openmeteo_client", None)
        om = await fetch_openmeteo(latitude, longitude, client=shared_client)
        # `om` carries `forecast` and/or `airQuality`; record a failure
        # only when BOTH legs missed — the partial-success case still
        # produces a valid merged response.
        if not om or "forecast" not in om:
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
                daily["uv_index_max_cams_at"] = [_dt_to_iso(peak_at)]
    except Exception as e:  # noqa: BLE001 — daily peak is bonus; never break /uv
        logger.warning("Daily peak UVI computation failed: %s", e)
    # Stale-grid header — monitors / browser can detect silent
    # staleness without parsing _camsMeta. Body still serves so the
    # session can complete; the browser's own freshness UI flags it.
    # NOTE: we deliberately don't expose `last_error` here — it's a
    # cross-origin info leak (CDS state, internal exception types).
    # /metrics carries it for authenticated scrapers.
    headers = {}
    if cache.is_stale:
        headers["X-Cams-Stale"] = "1"
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
    rolling counters plus snapshot freshness + last-error string so a
    scrape detects silent CAMS-pull failure or grid drift.

    Bearer-gated — exposes lifetime counters + redacted error strings
    that, while not credentials themselves, are useful enough for
    fingerprinting and timing-attack baselining that we keep them off
    the unauthenticated surface. /healthz remains open for liveness."""
    check_bearer(request)
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
    # Surface last error as a 0-value gauge with a label so Prometheus
    # rules can alert on transitions. Redacted in cams.py before it
    # reaches us, but quote-escape defensively.
    if cache.last_error:
        sanitized = cache.last_error.replace("\\", "\\\\").replace('"', '\\"')[:200]
        lines.append("# TYPE getbased_uvdata_last_error gauge")
        lines.append(f'getbased_uvdata_last_error{{message="{sanitized}"}} 1')
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
    """Parse ISO-8601 → epoch seconds. Caller is expected to have
    routed unparsable values to a 400 — silent fallback to "now"
    hides bugs and skews metrics counters."""
    import datetime as dt

    s = iso.replace("Z", "+00:00")
    try:
        d = dt.datetime.fromisoformat(s)
    except (ValueError, TypeError) as e:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid ISO-8601 time {iso!r}: {e}",
        ) from e
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
    """One-shot env + CAMS connectivity check. Returns shell exit code:
        0 — all checks pass
        1 — environment / config problem (no CDS call attempted)
        2 — CDS pull failed (auth, license, network, etc.)

    ASCII glyphs only — Windows cmd.exe / piped logs / `LC_ALL=C` CI
    runners all break on Unicode check marks; pure ASCII renders
    correctly everywhere.
    """
    import sys

    print(f"getbased-uvdata doctor v{__version__}")
    print("=" * 60)
    ok = True

    cams_key = os.environ.get("CAMS_API_KEY", "").strip()
    if not cams_key:
        print(
            "[FAIL] CAMS_API_KEY not set (register at https://ads.atmosphere.copernicus.eu and put your key in .env)"
        )
        ok = False
    else:
        # Don't print key prefix/suffix — combined with length it's a
        # fingerprint. Just confirm presence.
        print("[ OK ] CAMS_API_KEY set")

    bearer = os.environ.get("GETBASED_UVDATA_BEARER", "").strip()
    if not bearer:
        print("[WARN] GETBASED_UVDATA_BEARER unset -- server will run in OPEN mode")
        print("       (anyone reaching the port can burn your CAMS quota)")
    else:
        print("[ OK ] GETBASED_UVDATA_BEARER set")

    bbox = os.environ.get("CAMS_BBOX", "90,-180,-90,180")
    print(f"[ OK ] CAMS_BBOX={bbox}")

    if not ok:
        print("\nFix the [FAIL] items above before running the server.", file=sys.stderr)
        return 1

    print("\nAttempting a live CAMS pull (30 s - 5 min depending on CDS queue)...")
    try:
        from .cams import _pull_cams_blocking  # type: ignore

        # Pass CAMS_CACHE_DIR so the staging dir lands on the persistent
        # volume — same path as the production CamsCache.refresh code.
        # Empty/unset env → cache_dir=None → /tmp fallback (acceptable
        # for the doctor command, which is a one-shot operator probe).
        smoke_cache_dir = os.environ.get("CAMS_CACHE_DIR", "").strip() or None
        snap = _pull_cams_blocking(smoke_cache_dir)
    except Exception as e:  # noqa: BLE001
        # Sanitize: the exception body can include the API key.
        from .cams import _redact_secrets

        print(
            f"[FAIL] CAMS pull failed: {type(e).__name__}: {_redact_secrets(str(e))}",
            file=sys.stderr,
        )
        return 2
    print(
        f"[ OK ] CAMS pull OK -- {len(snap.times)} hourly steps, "
        f"{len(snap.lats)} lats, {len(snap.lons)} lons"
    )

    sample = snap.lookup(lat=50.0, lon=14.0, when_epoch=float(snap.times[0]))
    print(
        f"[ OK ] Sample at (50N, 14E) -> ozoneDU={sample['ozoneDU']:.1f}  AOD={sample['aod']:.3f}"
    )
    print("\nAll checks passed. Run `getbased-uvdata` (no args) to start the server.")
    return 0


if __name__ == "__main__":
    main()

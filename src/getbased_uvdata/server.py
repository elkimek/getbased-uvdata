"""FastAPI server — `/uv` endpoint + health + metadata."""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware

from . import __version__
from .auth import check_bearer, required_bearer
from .cams import CamsCache, background_pull_loop
from .openmeteo import fetch_openmeteo
from .reshape import build_response

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s :: %(message)s")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    cache = CamsCache()
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

    when_iso = time or _now_iso_utc()
    when_epoch = _iso_to_epoch(when_iso)
    cams_lookup = snap.lookup(latitude, longitude, when_epoch)

    merge = os.environ.get("MERGE_OPENMETEO", "1") not in ("0", "false", "no", "")
    om = await fetch_openmeteo(latitude, longitude) if merge else None

    return build_response(
        lat=latitude,
        lon=longitude,
        when_iso=when_iso,
        cams_lookup=cams_lookup,
        openmeteo=om,
        cams_pulled_at=snap.pulled_at,
        snapshot_valid_from=snap.valid_from,
        snapshot_valid_to=snap.valid_to,
    )


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
    """Console-script entry point — `getbased-uvdata`."""
    import uvicorn
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8324"))
    uvicorn.run("getbased_uvdata.server:app", host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()

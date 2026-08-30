# Changelog

All notable changes to this project will be documented here. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows [SemVer](https://semver.org/).

## [Unreleased]

## [0.1.6] — 2026-08-30

### Added

- **Authenticated, privacy-minimised `POST /v1/uv` route for the hosted app relay.** The route fails closed unless `GETBASED_UVDATA_BEARER` is configured, accepts only a bounded JSON object, rejects duplicate or extra fields, re-rounds coordinates to 0.1°, and performs only an in-memory CAMS lookup. It does not forward request coordinates to Open-Meteo or Copernicus and keeps them out of the request URL.
- **Direct CAMS UV dose-rate fields.** Total-sky and clear-sky biologically effective UV dose rates now anchor the returned UVI, with explicit field-level provenance and out-of-range handling instead of silently clamping to a boundary timestep.

### Fixed

- CAMS refreshes no longer retain raw decoded grids beside normalized arrays. Live and persisted grids are normalized to float32 and xarray's decoded-array cache is disabled, eliminating the repeated 1.5 GiB cgroup OOM loop without reducing the global bounding box, five-day forecast, or response precision.

### Security

- Protected data routes now have a bounded per-source request limiter after bearer authentication, so unauthenticated traffic cannot consume valid clients' buckets. The deployment uses a dedicated client-IP header which is accepted only from configured proxy networks and which Caddy overwrites from the socket peer; ordinary caller-controlled `X-Forwarded-For` is ignored.
- The runtime container now has a read-only root filesystem, dropped Linux capabilities, `no-new-privileges`, PID and memory boundaries, an init process, and a digest-pinned Python base image.
- Request-line access logs are disabled by default in the bundled Uvicorn launcher because legacy `GET /uv` and `/spectrum` URLs contain coordinates. Operators using another ASGI launcher must apply the same logging policy.
- Upstream error logging is sanitised so request coordinates and query strings are not written through exception messages.

### Changed

- CAMS-only hosted lookups no longer invoke the optional Open-Meteo merge or populate its per-coordinate cache. The legacy authenticated endpoints remain available for existing and self-hosted clients.
- The Compose image tag now tracks the application release version.

## [0.1.5] — 2026-05-11

### Fixed

- **Last-good cache no longer refreshes its own `stored_at` on a cache-hit fallback.** v0.1.4's resilience cache stored the response after the cache-fill step, so a 30-min-old forecast served from cache got re-stamped with a fresh timestamp on every failing request and kept living forever — defeating `_LAST_GOOD_TTL_SEC` during sustained Open-Meteo outages and silently serving hours-old data labelled as fresh. The persist step now operates on the live-fetch dict only, never on data filled from the cache. A fresh AQ-only response is merged into the existing entry (preserving the cached forecast and its original `stored_at`) so partial successes don't blow away the forecast cache either.
- **Differentiated connect timeout on the shared `httpx.AsyncClient` is no longer silently overridden per request.** `server.py` builds the shared client with `Timeout(5.0, connect=3.0)`, but `fetch_openmeteo` was passing `timeout=5.0` as a scalar on each `c.get()` call. httpx treats a per-request scalar as a full Timeout replacement — every field (including `connect`) collapsed to 5 s, eroding the headroom that gives the relay graceful degradation on cold-TLS spikes. The per-request override has been removed; the client's own Timeout applies. Owned (per-call) clients still get a `Timeout(5.0)` fallback for backwards compatibility.

Both findings flagged by Greptile review on #14 (confidence 3/5). Two new regression tests in `TestOpenMeteoLastGoodFallback` lock down the cache-hit `stored_at` invariant and the no-per-request-`timeout=` invariant.

## [0.1.4] — 2026-05-10

### Fixed

- **Open-Meteo merge no longer collapses to a sparse 1-row envelope on transient upstream failures.** When the Open-Meteo forecast leg timed out (1.5 s budget against a P50 of ~150 ms but real P99 jitter past 2 s) or 5xxd, `fetch_openmeteo` returned `{"airQuality": ...}` with no `forecast` key, and `build_response` fell into the CAMS-only synthesis path — emitting a single hourly row with `uv_index=null`, `cloud_cover=null`, `utc_offset_seconds=0`. The browser's sparse-uv merge branch (`js/sun-uvdata.js`) then re-fetched Open-Meteo client-side and labelled the source as `cams+open_meteo`, indistinguishable to users from a hard fallback away from the relay. Three changes together: (a) timeout raised 1.5 s → 5 s with separate 3 s connect budget; (b) shared `httpx.AsyncClient` created once at lifespan startup with HTTP/2 keepalive (8 keepalive conns, 32 max), eliminating the per-request TLS handshake — typical merge-leg latency drops from ~700 ms to ~150 ms; (c) per-coord last-good cache (LRU, 256 entries quantised to 0.1°, 30-min TTL) — when a fresh forecast fetch fails, the prior successful response is served instead of degrading to CAMS-only. The CAMS overlay still runs against the live snapshot so ozone/AOD reflect current state; only Open-Meteo-derived columns are stale-but-valid. Failure metric also tightened to count only fully-failed merges (was incrementing on partial successes).

## [0.1.3] — 2026-05-05

### Fixed

- **Container `mem_limit` raised 512m → 1500m.** The 512m cap was sized for the single-variable (ozone-only) snapshot from before PM2.5 / PM10 / total AOD got added. With all four AQ variables in flight, the CAMS pull peaks at ~900 MB-1.1 GB during xarray decode of the 234 MB zip, OOM-killing every pull mid-decode. The container would auto-restart, the v0.1.1 startup sweep would correctly clean up the orphan staging dir, but the actual snapshot never landed — so on-disk data drifted further past the 24h `is_stale` line over the v0.1.0 → v0.1.2 window. Bumped to 1500m, which leaves comfortable headroom on the 2 GB production VPS while still capping a runaway parse.

## [0.1.2] — 2026-05-05

### Fixed

- **Default request date is now `today_utc - 1`, not `today_utc`.** This was the real root cause of the post-incident `CAMS pull failed` symptom. CAMS Atmospheric Composition Forecasts publishes the daily 00:00 UTC cycle 6–12h after midnight; during that lag, ADS rejects requests carrying today's date with a generic `400 invalid combination of values`. The actual reason (`date` out of the dataset's valid enum) is only visible via `/retrieve/v1/processes/<id>/constraints` — that endpoint is now in our incident triage runbook. Anchoring the default one day back keeps us inside the published window regardless of where in the publication cycle we hit. Yesterday's forecast still gives 4–5 days of forward coverage. Operators backfilling can still override via `CAMS_DATE_OVERRIDE=YYYY-MM-DD`.

## [0.1.1] — 2026-05-05

### Fixed

- **Staging dir leak: CAMS retrieve no longer accumulates orphan `tmpXXXX` dirs in the container writable layer.** `_pull_cams_blocking` previously created `tempfile.TemporaryDirectory()` with no `dir=` argument — defaulting to `/tmp` inside the container. On graceful exit the `with` block cleaned up; on SIGKILL/OOM mid-retrieve, `TemporaryDirectory`'s finalizer never fired, leaving ~470 MB of unzipped netCDF behind. Each ungraceful shutdown leaked one dir, and over weeks of dev-cycle restarts the host disk filled to 100% on the production VPS, wedging the colocated evolu-relay's SQLite (`SQLITE_FULL` / `ENOSPC: no space left on device`). Staging is now placed under `cache_dir` (the persistent `/data` volume) with a stable `cams-stage-*` prefix, and `CamsCache.__init__` sweeps any orphan `cams-stage-*` dirs from prior runs at startup. The volume is monitored separately from the host disk, so even a worst-case future leak class can't take down a colocated service. (#11)

### Changed

- **Request body uses ADS-native keys (`data_format`, list-shaped `time`/`type`) instead of the legacy `format` form.** Originally framed as a fix for a `400 invalid combination of values` symptom we were seeing in production logs (#12) — turned out to be inert. The translation we attributed to `ecmwf-datastores-client`'s compatibility shim is actually ADS-side display normalization of the error response (the API echoes back `'netcdf'` even when you send `'netcdf_zip'`, both are accepted; `grid: [0.4, 0.4]` is auto-injected into the echoed body for documentation, not as a real constraint). The 400 was caused by the request date (see v0.1.2). The new shape is still the right one to use going forward — it matches the ADS form output and survives any future shim quirks — but it's a code-quality cleanup, not a bug fix. Keeping the change; correcting the framing.

## [0.1.0] — initial public release

### Added

- `GET /uv?latitude=&longitude=&time=` — per-coord/per-hour CAMS atmosphere snapshot, Open-Meteo-shaped JSON with `hourly.ozone_du`, `hourly.aod`, `hourly.pm2_5`, `hourly.pm10`, plus optional Open-Meteo merge for cloud cover / temperature / UVI baseline.
- `GET /spectrum?latitude=&longitude=&time=&altitude_m=&cloud_cover=` — server-side Bird-Riordan spectral reconstruction fed by real CAMS values. Returns wavelength-resolved surface UV (W/m²/nm, 280-2500 nm @ 5 nm) plus integrated UVI.
- `GET /healthz` — liveness + minimal grid metadata (open).
- `GET /metrics` — Prometheus-compatible exposition with lifetime pull counters + last-error gauge (bearer-gated).
- `GET /` — service index.
- Background pull from CDS-API every `CAMS_PULL_INTERVAL_SEC` (default 6 h) with exponential backoff (60 s → 1800 s) on failure.
- Snapshot persistence to `CAMS_CACHE_DIR` (default `/data`) — process restarts warm-start instead of waiting for CDS to queue a fresh request.
- Bilinear spatial interpolation across the four corner cells (~5 km error at 0.4° resolution vs ~44 km nearest-cell).
- Per-hour CAMS interpolation across the response window — different times in the response array get different ozone/AOD/PM lookups against the snapshot leadtime axis.
- Server-computed daily peak UVI (`daily.uv_index_max_cams[]` + `_at[]`) by scanning the snapshot per-hour through Bird-Riordan reconstruction.
- 5-day forecast horizon by default (configurable via `CAMS_FORECAST_HORIZON_HOURS` 24-120). Hourly through day 1, 3-hourly through day 5.
- `getbased-uvdata doctor` CLI subcommand — env validation + one-shot CAMS pull + sample lookup.
- Multi-stage Dockerfile + non-root user + `/data` volume.
- GitHub Actions CI: pytest matrix (Python 3.11, 3.12) + `ruff check` + Docker build smoke-boot.
- AGPL-3.0-or-later license.

### Security

- Bearer-token auth via `GETBASED_UVDATA_BEARER` (constant-time compare).
- CORS allowlist locked to production app origins; operator extras validated as `scheme://host[:port]`.
- Live `CAMS_API_KEY` / `GETBASED_UVDATA_BEARER` values scrubbed from error strings before they reach `/healthz`, response headers, or stdout.
- `/metrics` bearer-gated; lifetime counters + last-error not exposed to unauthenticated callers.
- Disk snapshot loaded with `allow_pickle=False`.
- 10-minute timeout on the CDS-API pull thread (prevents the background loop from hanging forever on a wedged upstream).

### Not yet implemented

- NO2 / SO2 / CO / surface ozone — these live in the regional `cams-european-air-quality-forecasts` dataset (Europe-only) or behind a `model_level=60` query on the global dataset; both are Phase-2 candidates. For now Open-Meteo's AQI endpoint covers them.
- McRad surface UV / spectral irradiance — CAMS Solar Radiation Service is structurally incompatible with on-demand per-coord serving (queue-based file delivery, pre-registered locations only). Bird-Riordan reconstruction fed by CAMS atmosphere is the architecturally correct answer.

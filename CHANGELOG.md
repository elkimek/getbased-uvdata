# Changelog

All notable changes to this project will be documented here. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows [SemVer](https://semver.org/).

## [Unreleased]

## [0.1.3] — 2026-05-05

### Fixed

- **Container `mem_limit` raised 512m → 1500m.** The 512m cap was sized for the single-variable (ozone-only) snapshot from before PM2.5 / PM10 / total AOD got added. With all four AQ variables in flight, the CAMS pull peaks at ~900 MB-1.1 GB during xarray decode of the 234 MB zip, OOM-killing every pull mid-decode. The container would auto-restart, the v0.1.1 startup sweep would correctly clean up the orphan staging dir, but the actual snapshot never landed — so on-disk data drifted further past the 24h `is_stale` line over the v0.1.0 → v0.1.2 window. Bumped to 1500m, which leaves comfortable headroom on the 2 GB production VPS while still capping a runaway parse.

## [0.1.2] — 2026-05-05

### Fixed

- **Default request date is now `today_utc - 1`, not `today_utc`.** CAMS Atmospheric Composition Forecasts publishes the daily 00:00 UTC cycle 6–12h after midnight; ADS rejects requests carrying today's date during that window with a generic `400 invalid combination of values`. The actual reason (`date` out of the enum's valid range) only surfaces via the `/constraints` endpoint, which we now know to consult during incident triage. Anchoring the default one day back keeps us inside the published window regardless of where in the publication cycle we hit. Yesterday's forecast still gives 4–5 days of forward coverage. Operators backfilling can still override via `CAMS_DATE_OVERRIDE=YYYY-MM-DD`. Closes the same `CAMS pull failed` symptom that v0.1.1's `data_format` rename did NOT fix — the request shape was a red herring; the date was the real blocker.

## [0.1.1] — 2026-05-05

### Fixed

- **CAMS pull was failing with HTTP 400 against the new ADS portal.** The legacy `format: 'netcdf_zip'` key passed through `ecmwf-datastores-client`'s compatibility shim, which silently rewrote it to `data_format: 'netcdf'` AND injected an explicit `grid: [0.4, 0.4]`. CAMS Atmospheric Composition Forecasts has a fixed native grid; an explicit grid field is rejected as `invalid combination of values`. Switched the request to use the ADS-native `data_format` key directly (and wrapped `time`/`type` as lists to match the form's emitted shape) so the shim no longer touches our payload. Atmosphere data is fresh again. (#12)
- **Staging dir leak: CAMS retrieve no longer accumulates orphan `tmpXXXX` dirs in the container writable layer.** `_pull_cams_blocking` previously created `tempfile.TemporaryDirectory()` with no `dir=` argument — defaulting to `/tmp` inside the container. On graceful exit the `with` block cleaned up; on SIGKILL/OOM mid-retrieve, `TemporaryDirectory`'s finalizer never fired, leaving ~470 MB of unzipped netCDF behind. Each ungraceful shutdown leaked one dir, and over weeks of dev-cycle restarts the host disk filled to 100% on the production VPS, wedging the colocated evolu-relay's SQLite (`SQLITE_FULL` / `ENOSPC: no space left on device`). Staging is now placed under `cache_dir` (the persistent `/data` volume) with a stable `cams-stage-*` prefix, and `CamsCache.__init__` sweeps any orphan `cams-stage-*` dirs from prior runs at startup. The volume is monitored separately from the host disk, so even a worst-case future leak class can't take down a colocated service. (#11)

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

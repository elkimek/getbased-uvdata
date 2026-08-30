# getbased-uvdata

CAMS-fed UV/atmosphere relay for the [getbased.health](https://getbased.health) Light & Sun module.

Pulls the [CAMS Atmospheric Composition Forecast](https://atmosphere.copernicus.eu/) on a schedule, indexes the grid in memory, and serves per-coord/per-hour lookups in the same JSON shape the browser already accepts from Open-Meteo. A `/spectrum` endpoint runs server-side Bird-Riordan reconstruction so browsers can skip their own client-side reconstruction step.

**Why this exists.** Open-Meteo's free tier doesn't expose total-column ozone in Dobson Units — only surface µg/m³ pollution ozone, which doesn't drive UVB transmission. CAMS supplies direct total-sky and clear-sky biologically effective UV dose rate (converted to UVI), total-column ozone, AOD@550 nm, and particulates. Direct CAMS UV anchors the headline UVI; the local Bird-Riordan model remains an explicitly modeled wavelength-resolved input for exploratory wellness channels.

```
        [browser]
            │  POST {meteo: 'cams', latitude, longitude, time}
            ▼
        [/api/proxy]   <- Vercel function, injects bearer server-side
            │  POST /v1/uv + Authorization: Bearer …
            ▼
        [getbased-uvdata]   <- this repo
            │
            ├─ background pull every 6 h ──> CAMS ADS (CDS-API)
            ├─ POST /v1/uv ──> local in-memory CAMS grid only
            └─ legacy GET /uv ──> optional Open-Meteo weather and
                                   DWD/EUMETSAT radiation context
```

## Two ways to use it

### Hosted

The browser posts `{meteo: "cams", ...}` to `/api/proxy` on the getbased app domain. That function rounds coordinates to 0.1° and sends a bounded JSON body to the authenticated `POST /v1/uv` route. The relay rounds again and performs only a lookup in its pre-downloaded CAMS grid: it does not send or cache the request coordinates through Open-Meteo or Copernicus. Copernicus receives only the relay operator's scheduled grid/bounding-box download. The browser falls back directly to Open-Meteo if the CAMS-only response is unavailable or lacks a usable UVI.

### Self-host

Run the Docker image on any box that can reach the CDS-API.

```bash
git clone https://github.com/elkimek/getbased-uvdata
cd getbased-uvdata
cp .env.example .env
# Edit .env — set CAMS_API_KEY (from ads.atmosphere.copernicus.eu)
#             + GETBASED_UVDATA_BEARER (e.g. `openssl rand -hex 32`)
docker compose up -d
```

Then in the app: **Settings → Light & Sun → Sun Data Source → Self-hosted server** and paste your URL + bearer.

## Prerequisites

1. **Free Copernicus ADS account** at https://ads.atmosphere.copernicus.eu/. Register via the ECMWF SSO (one verification email, no credit card).
2. **API token** — visit your profile, copy the key under "API Token" (UUID format).
3. **Accept the dataset licence**: open https://ads.atmosphere.copernicus.eu/datasets/cams-global-atmospheric-composition-forecasts?tab=download#manage-licences and tick "Licence to use Copernicus Products". The CDS-API returns 403 with a clear message until you do this. **Easy to miss — most "doctor" failures land here.**
4. A self-chosen bearer token: `openssl rand -hex 32`.

## Configuration

| env var | default | what it does |
| --- | --- | --- |
| `CAMS_API_KEY` | _(required)_ | Your [CDS-API](https://atmosphere.copernicus.eu/data/registration) key. |
| `CAMS_API_URL` | `https://ads.atmosphere.copernicus.eu/api` | CDS-API endpoint. Override only if you're hitting a mirror. |
| `CAMS_BBOX` | `90,-180,-90,180` | Region of interest (N,W,S,E in degrees). Smaller box = faster pull + less RAM. |
| `CAMS_PULL_INTERVAL_SEC` | `21600` | How often to refresh the grid. CAMS publishes every 12 h; 6 h covers a missed cycle. |
| `CAMS_PULL_TIMEOUT_SEC` | `600` | Per-pull timeout. CDS queue worst-case is ~5 min; this caps it at 10 to prevent a wedged thread from holding the lock forever. |
| `CAMS_FORECAST_HORIZON_HOURS` | `120` | Forecast horizon in hours (24–120). Hourly through day 1, 3-hourly through day 5. |
| `CAMS_DATE_OVERRIDE` | _(empty)_ | Force a fixed forecast date (`YYYY-MM-DD`) instead of the default previous UTC day. Only useful for testing/backfills; leave empty in production. |
| `CAMS_CACHE_DIR` | `/data` | Directory the latest snapshot persists to. On restart the server warm-starts from this file instead of waiting for a fresh CDS pull. Empty string disables persistence. |
| `GETBASED_UVDATA_BEARER` | _(empty)_ | Token clients must present in `Authorization: Bearer …`. **Always set in production** — empty mode lets any reachable client burn your CAMS quota. |
| `MERGE_OPENMETEO` | `1` | Merge Open-Meteo weather/fallback UVI and recent DWD/EUMETSAT radiation observations. Set `0` for CAMS-only. |
| `UVICORN_ACCESS_LOG` | `0` | Opt in to Uvicorn request-line access logs. Disabled by default because legacy `GET /uv` URLs contain coordinates. |
| `ALLOWED_ORIGINS` | _(empty)_ | Extra CORS origins (comma-separated) on top of `https://app.getbased.health` + `https://getbased.health`. Each must be `scheme://host[:port]`. |
| `UVDATA_RATE_LIMIT_PER_MINUTE` | `300` | Per-source request cap for `/uv`, `/spectrum`, and `/metrics`; `0` disables it. |
| `UVDATA_CLIENT_IP_HEADER` | _(empty)_ | Dedicated reverse-proxy-overwritten client-IP header used by the limiter. Compose sets `x-getbased-client-ip`; configure Caddy as shown in `docker-compose.yml`. Ordinary `X-Forwarded-For` is never trusted. |
| `UVDATA_TRUSTED_PROXY_CIDRS` | _(empty)_ | Comma-separated proxy source networks permitted to supply `UVDATA_CLIENT_IP_HEADER`. Compose trusts only the Docker bridge range and keeps the published port on loopback. Use a narrower CIDR for a fixed gateway. |
| `HOST` / `PORT` | `0.0.0.0` / `8324` | Listen address. |

## Endpoints

### `GET /`

Friendly index — service metadata + endpoint list.

### `GET /healthz`

Liveness probe (no bearer required). Minimal info — detailed pull state lives behind the bearer on `/metrics`.

```json
{
  "ok": true,
  "version": "0.1.0",
  "auth": "bearer",
  "cams": {
    "pulled_at": 1714680000.0,
    "valid_from": 1714680000.0,
    "valid_to": 1714766400.0,
    "stale": false
  }
}
```

### `GET /uv?latitude=&longitude=&time=`

Returns Open-Meteo-shaped JSON with extra hourly arrays:
- `hourly.uv_index_cams_total_sky[i]` — direct CAMS biologically effective dose rate converted to UVI.
- `hourly.uv_index_cams_clear_sky[i]` — direct CAMS clear-sky biologically effective dose rate converted to UVI.
- `hourly.uv_index_satellite_adjusted[i]` — CAMS clear-sky UVI multiplied by a bounded recent observed/clear-sky broadband radiation ratio, when available.
- `hourly.uv_index_source[i]` — field-level provenance (`cams_uvbed`, `cams_uvbedcs+satellite_cmf`, or `open_meteo_gfs`).
- `hourly.ozone_du[i]` — total column ozone in Dobson Units, from CAMS.
- `hourly.aod[i]` — 550 nm aerosol optical depth, from CAMS.
- `hourly.pm2_5[i]` / `hourly.pm10[i]` — surface particulates in µg/m³, from CAMS.
- `daily.uv_index_max[]`, `daily.uv_index_max_at[]`, and `daily.uv_index_max_source[]` — peaks calculated from the same fused hourly UVI series.
- `_fieldSources` and `_openMeteoMeta` — requested-time provenance and freshness metadata.

`time` is optional; defaults to "now". When it falls outside the available CAMS forecast snapshot, CAMS values are not clamped into that date; a date-matched Open-Meteo historical-forecast response remains the fallback. Bearer required if `GETBASED_UVDATA_BEARER` is set. Sets `X-Cams-Stale: 1` when the in-memory grid is past its 24h freshness window.

### `POST /v1/uv`

Authenticated, CAMS-only route for a trusted same-operator application relay. It fails closed with `503` unless `GETBASED_UVDATA_BEARER` is configured, accepts only a JSON object containing numeric `latitude`, numeric `longitude`, and an optional bounded ISO-8601 `time`, and requires the matching bearer. The request body is capped at 2 KiB, duplicate or additional keys are rejected, and coordinates are rounded to 0.1° before lookup. This route never invokes Open-Meteo, never creates a per-coordinate cache entry, and keeps coordinates out of the HTTP request URL. It returns `X-Coordinate-Precision: 0.1`.

### `GET /spectrum?latitude=&longitude=&time=&altitude_m=&cloud_cover=`

Server-side Bird-Riordan reconstruction fed by CAMS ozone + AOD. Returns modeled wavelength-resolved surface irradiance (W/m²/nm, 280–2500 nm @ 5 nm) plus its integrated UVI. This endpoint rejects times outside the loaded CAMS snapshot rather than substituting a boundary timestep:

```json
{
  "latitude": 50.0,
  "longitude": 14.0,
  "time": "2024-06-01T12:00:00Z",
  "zenithDeg": 30.2,
  "uvIndex": 7.4,
  "wavelengths": [280, 285, 290, ..., 2500],
  "irradiance": [0.0, 0.0, 0.001, ..., 0.038],
  "atmosphere": {
    "ozoneDU": 373.9,
    "aod": 0.095,
    "cloudCover": 0,
    "altitudeM": 0
  },
  "_camsMeta": {...}
}
```

Browsers can ingest the spectrum directly through their existing channel-action-spectrum machinery, replacing the client-side reconstruction step entirely.

### `GET /metrics`

Prometheus-compatible plain-text exposition (bearer required):

```
getbased_uvdata_info{version="0.1.0"} 1
getbased_uvdata_uv_requests_total 142
getbased_uvdata_uv_requests_2xx 140
getbased_uvdata_uv_requests_4xx 2
getbased_uvdata_uv_request_duration_sum_sec 12.4
getbased_uvdata_openmeteo_merges_total 142
getbased_uvdata_openmeteo_merge_failures_total 0
getbased_uvdata_snapshot_age_seconds 7320
getbased_uvdata_snapshot_timesteps 25
getbased_uvdata_snapshot_stale 0
getbased_uvdata_pull_attempts_total 12
getbased_uvdata_pull_successes_total 11
getbased_uvdata_pull_failures_total 1
```

Useful alerts: `snapshot_stale == 1` (background pull is wedged), `openmeteo_merge_failures_total / merges_total > 0.1` (Open-Meteo flaky), `snapshot_age_seconds > 86400` (grid more than a day old), `pull_failures_total - pull_successes_total > 3` (CDS auth or licence problem).

## CLI

```bash
getbased-uvdata          # start the HTTP server (default)
getbased-uvdata doctor   # one-shot env validation + live CAMS pull + sample lookup
```

`doctor` exit codes:
- `0` — all checks passed
- `1` — environment / config problem (CDS call not attempted)
- `2` — CDS pull failed (auth, licence, network)

Sample output:
```
getbased-uvdata doctor v0.1.0
============================================================
[ OK ] CAMS_API_KEY set
[ OK ] GETBASED_UVDATA_BEARER set
[ OK ] CAMS_BBOX=60,5,40,25

Attempting a live CAMS pull (30 s - 5 min depending on CDS queue)...
[ OK ] CAMS pull OK -- 33 hourly steps, 51 lats, 50 lons
[ OK ] Sample at (50N, 14E) -> ozoneDU=370.6  AOD=0.254
```

## Troubleshooting

**`/healthz` returns `ok: false` for the first 30 s – 5 min after start.** Normal — CDS queues new request shapes. Watch `docker logs uvdata` for `CAMS pull OK: ...`.

**`last_error: "...required licences not accepted..."` on `/metrics`.** You missed step 3 in [Prerequisites](#prerequisites). Open the dataset page, tick the licence checkbox, then `docker compose restart`. The licence acceptance hits CAMS instantly; no propagation delay.

**`last_error: "...invalid request..."` with a future-dated request.** Your clock is ahead of CAMS's published forecast horizon (CAMS only has data up to "today + 5 days"). On normal-clock production this never fires. On a clock-shifted dev box (e.g. an integration harness baked to a future date), set `CAMS_DATE_OVERRIDE=2024-06-01` to a known-published date.

**Doctor exits 1 with `[FAIL] CAMS_API_KEY not set`.** Check `.env` is in the repo root (not `~/`), and that you ran `cp .env.example .env` (not `mv`). For docker-compose, the `env_file` directive looks in the working directory of the `docker compose` command.

**Container crashes on startup with `PermissionError: [Errno 13] Permission denied: '/data'`.** The default `CAMS_CACHE_DIR=/data` needs the volume to be writable by uid 10001 (the non-root user inside the container). With docker-compose's named volume this is automatic; with a bind mount, run `chown -R 10001:10001 /your/host/path`.

**`X-Cams-Stale: 1` on every `/uv` response.** The background pull has been failing for >24 h. Hit `/metrics` (with bearer) and look at `getbased_uvdata_last_error`. Most common: licence revoked, API key rotated, or CAMS_BBOX reformatted incorrectly.

**Open-Meteo merge failure but `/uv` still returns 200.** The merge is non-fatal; CAMS data stays in the response. `getbased_uvdata_openmeteo_merge_failures_total` increments. Open-Meteo intermittent failures are normal under heavy load; persistent failures suggest the upstream is reachable but rejecting our IP — try from a different network.

## Operational notes

- **First request after boot** waits for the initial CAMS pull. CDS-API queue time is typically 30 s – 5 min depending on global load. Endpoint returns `503` until the first pull completes.
- **Memory footprint** is bounded by the configured grid, forecast horizon, requested variable roster, and Compose cgroup. Global fields are normalized to float32 and xarray's decoded-grid cache is disabled, preventing raw and normalized grids from accumulating together during refresh.
- **CDS-API quotas**: free tier is 4 concurrent requests / user. With one pull every 6 h there's no realistic way to hit the limit on a per-instance basis. Multi-instance fleets should set `CAMS_PULL_INTERVAL_SEC` higher and share a snapshot via `CAMS_CACHE_DIR` on a shared volume.
- **Stale grid**: if a pull fails, the previous snapshot keeps serving. `/healthz.cams.stale` flips `true` after 24 h with no successful refresh; `getbased_uvdata_snapshot_stale` mirrors it on `/metrics`. Monitor both.
- **Single-worker only.** `_metrics` counters are per-process; running with `--workers N > 1` produces fragmented metrics. Front the relay with a reverse proxy if you need horizontal scaling.

## Testing

```bash
pip install -e ".[dev]"
pytest                    # 32 tests, ~1 s
ruff check src tests      # lint
```

CI runs the matrix on Python 3.11 + 3.12 plus a Docker build smoke-boot — see `.github/workflows/ci.yml`.

## Security

See [SECURITY.md](SECURITY.md) for vulnerability reporting and the threat model.

## Architecture decisions

For deeper context on the direct-CAMS UVI anchor, the separate modeled spectrum path, and what CAMS does *not* provide for the getbased use case, see the project memory in the [getbased repository](https://github.com/elkimek/get-based).

## License

AGPL-3.0-or-later. Same license as the rest of the getbased.health stack. See [LICENSE](LICENSE).

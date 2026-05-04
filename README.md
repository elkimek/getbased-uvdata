# getbased-uvdata

CAMS-fed UV/atmosphere relay for the [getbased.health](https://getbased.health) Light & Sun module.

Pulls the [CAMS Atmospheric Composition Forecast](https://atmosphere.copernicus.eu/) on a schedule, indexes the grid in memory, and serves per-coord/per-hour lookups in the same JSON shape the browser already accepts from Open-Meteo. Optionally merges Open-Meteo's hourly clouds/temp/UVI into the response so a single upstream call delivers everything Light & Sun needs.

**Why?** Open-Meteo's free tier doesn't expose total-column ozone in Dobson Units — only surface µg/m³ pollution ozone, which doesn't drive UVB transmission. CAMS gives the real KNMI-validated DU value, which feeds the browser's Bird-Riordan engine for sharper UVI / vit-D / retinal-UV math, especially around ozone-hole season and at low solar elevation.

## Two ways to use it

### Hosted

Use `https://uvdata.getbased.health` directly — the maintainer-run instance fronted by the Vercel proxy. The browser calls `/api/proxy?meteo=cams&latitude=...&longitude=...` and the proxy injects the bearer server-side. No setup on your end.

### Self-host

Run the Docker image on any box that can reach the CDS-API.

```bash
git clone https://github.com/elkimek/getbased-uvdata
cd getbased-uvdata
cp .env.example .env
# Edit .env — set CAMS_API_KEY + GETBASED_UVDATA_BEARER
docker compose up -d
```

Then in the app: **Settings → Light & Sun → Sun Data Source → Self-hosted server** and paste your URL + bearer.

## Configuration

| env var | default | what it does |
| --- | --- | --- |
| `CAMS_API_KEY` | _(required)_ | Your [CDS-API](https://atmosphere.copernicus.eu/data/registration) key. |
| `CAMS_API_URL` | `https://ads.atmosphere.copernicus.eu/api` | CDS-API endpoint. Override only if you're hitting a mirror. |
| `CAMS_BBOX` | `90,-180,-90,180` | Region of interest (N,W,S,E in degrees). Smaller box = faster pull + less RAM. |
| `CAMS_PULL_INTERVAL_SEC` | `21600` | How often to refresh the grid. CAMS publishes every 12 h; 6 h covers a missed cycle. |
| `GETBASED_UVDATA_BEARER` | _(empty)_ | Token clients must present in `Authorization: Bearer …`. Empty = open public (only safe behind your own access control). |
| `MERGE_OPENMETEO` | `1` | Merge Open-Meteo clouds/temp/UVI into the response. Set `0` for CAMS-only — useful if you want fewer servers in the data path. |
| `ALLOWED_ORIGINS` | _(empty)_ | Extra CORS origins (comma-separated) on top of the production app + localhost dev. |
| `HOST` / `PORT` | `0.0.0.0` / `8324` | Listen address. |

## Endpoints

### `GET /uv?latitude=&longitude=&time=`

Returns Open-Meteo-shaped JSON with two extra hourly arrays:
- `hourly.ozone_du[i]` — total column ozone in Dobson Units, from CAMS.
- `hourly.aod[i]` — 550 nm aerosol optical depth, from CAMS.

`time` is optional; defaults to "now". Bearer required if `GETBASED_UVDATA_BEARER` is set.

### `GET /healthz`

Liveness + grid metadata:
```json
{
  "ok": true,
  "version": "0.1.0",
  "auth": "bearer",
  "cams": {
    "pulled_at": 1714680000.0,
    "valid_from": 1714680000.0,
    "valid_to": 1714766400.0,
    "stale": false,
    "last_error": null
  }
}
```

## Operational notes

- **First request after boot** waits for the initial CAMS pull to land. CDS-API queue time is typically 30 s – 5 min depending on global load. The endpoint returns `503` until the first pull completes.
- **Memory footprint**: ~150 MB for the global grid at 0.4° resolution × 24 hourly steps × 2 variables. Bounded — no growth over time.
- **CDS-API quotas**: free tier is 4 concurrent requests / user. With one pull every 6 h there's no realistic way to hit the limit on a per-instance basis. Multi-instance fleets should set `CAMS_PULL_INTERVAL_SEC` higher and share storage.
- **Stale grid**: if a pull fails, the previous snapshot keeps serving. `/healthz.cams.stale` flips `true` after 24 h with no successful refresh; monitor that alongside `last_error`.

## License

AGPL-3.0-or-later. Same license as the rest of the getbased.health stack.

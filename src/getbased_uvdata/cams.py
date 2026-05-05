"""CAMS Atmosphere Data Store pull + in-memory grid index.

The CDS-API is queue-based: we submit a retrieve job, poll, then
download a netCDF. That's too slow for per-request use, so this module
runs on a background schedule, grabbing the latest forecast every
`CAMS_PULL_INTERVAL_SEC` seconds. The HTTP layer reads from the
in-memory grid via `lookup()` — bilinear interpolation per coord, no
network on the request hot path.

What we pull from CAMS:
  • total_column_ozone          → Dobson Units (drives UVB transmission)
  • total_aerosol_optical_depth → 550 nm AOD (modulates UVA/UVB scatter)
  • particulate_matter_2.5um    → surface PM2.5, µg/m³
  • particulate_matter_10um     → surface PM10, µg/m³

UV spectra come from `spectrum.reconstruct_spectrum` fed these CAMS
values rather than from CAMS-McRad — the Solar Radiation Service is
queue-based with pre-registered locations, structurally incompatible
with synchronous per-coord serving.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

# Variables we ask CAMS for. Names match the CDS-API short-name table for
# the CAMS global atmospheric composition forecast product. Adjust if
# Copernicus renames them — they re-key occasionally.
#
# UV-math drivers: total column ozone (DU) + aerosol optical depth.
# Particulate AQ (PM2.5/PM10) replaces Open-Meteo's AQI — CAMS is the
# actual upstream Open-Meteo wraps for these, so we save a hop and
# get the original satellite-assimilated values.
#
# We DON'T fetch UV spectra / surface UV irradiance from CAMS-McRad —
# that product is gated behind pre-registered locations + queue-based
# file delivery, which doesn't fit a synchronous per-coord API. The
# /spectrum endpoint runs Bird-Riordan + Bass-Paur server-side, fed
# the real ozone DU + AOD from this pull, which gets us close enough
# without the architectural mismatch.
#
# NO2, SO2, CO, surface ozone are NOT available on this dataset at
# single-level — CAMS silently drops them from the response. They
# live in the regional `cams-european-air-quality-forecasts` (Europe
# only) or require a model_level=60 parameter (heavyweight 3D query).
# Phase-2 candidate; for now Open-Meteo's AQI endpoint covers these.
_CAMS_DATASET = "cams-global-atmospheric-composition-forecasts"
_CAMS_VARIABLES = [
    "total_column_ozone",
    "total_aerosol_optical_depth_550nm",
    "particulate_matter_2.5um",
    "particulate_matter_10um",
]


def _redact_secrets(s: str) -> str:
    """Strip live CAMS_API_KEY / GETBASED_UVDATA_BEARER values from a
    string before it lands in /healthz, response headers, or stdout.

    cdsapi exception strings can include the request URL with the API
    key embedded in the path / Authorization header; FastAPI exception
    handlers can include the bearer when validation rejects an Authn
    header. Replace either with `<redacted>` so the unauthenticated
    /healthz response and X-Cams-Last-Error header don't side-channel
    them out.
    """
    if not s:
        return s
    for env_var in ("CAMS_API_KEY", "GETBASED_UVDATA_BEARER"):
        v = os.environ.get(env_var, "").strip()
        if v and len(v) >= 8:
            s = s.replace(v, f"<{env_var}-redacted>")
    return s


@dataclass
class GridSnapshot:
    """One CAMS pull, indexed for O(1) bilinear lookup.

    Optional AQ fields default to None when the operator pulls a
    legacy snapshot from disk (pre-AQ schema). Lookups gracefully skip
    missing fields rather than crashing, so a smooth migration path
    exists across version bumps.
    """

    pulled_at: float  # epoch seconds
    valid_from: float  # forecast valid period start
    valid_to: float  # forecast valid period end
    times: np.ndarray  # (T,) — epoch seconds for each forecast hour
    lats: np.ndarray  # (LAT,) — descending (CAMS convention)
    lons: np.ndarray  # (LON,) — typically -180..180
    ozone_du: np.ndarray  # (T, LAT, LON) — Dobson Units
    aod_550: np.ndarray  # (T, LAT, LON) — unitless
    pm2_5: np.ndarray | None = None  # (T, LAT, LON) — µg/m³
    pm10: np.ndarray | None = None  # (T, LAT, LON) — µg/m³
    no2: np.ndarray | None = None  # (T, LAT, LON) — µg/m³ (converted from kg/m³)
    so2: np.ndarray | None = None  # (T, LAT, LON) — µg/m³
    co: np.ndarray | None = None  # (T, LAT, LON) — µg/m³
    o3_surface: np.ndarray | None = None  # (T, LAT, LON) — µg/m³ (tropospheric)

    def lookup(self, lat: float, lon: float, when_epoch: float) -> dict[str, float | None]:
        """Bilinear-interpolate the grid at (lat, lon, time). Returns {ozoneDU, aod}.

        Time axis snaps to the nearest forecast hour (CAMS leadtimes are
        already 1-hour-granular). Spatial axes interpolate between the
        four corner cells — at 0.4° resolution that's ~44 km error if
        we picked just the nearest cell, vs ~5 km when bilinear weighs
        the corners by Euclidean fraction. Material near coastlines and
        topographic boundaries where ozone/AOD gradients are steep.
        """
        # ── time axis (nearest forecast hour) ────────────────────────
        if when_epoch < self.times[0] or when_epoch > self.times[-1]:
            ti = 0 if when_epoch < self.times[0] else len(self.times) - 1
        else:
            ti = int(np.searchsorted(self.times, when_epoch))
            if ti >= len(self.times):
                ti = len(self.times) - 1

        # ── longitude wrap to the grid's domain ──────────────────────
        lon_wrapped = lon
        lon_min, lon_max = float(self.lons.min()), float(self.lons.max())
        if lon < lon_min:
            lon_wrapped = lon + 360
        elif lon > lon_max:
            lon_wrapped = lon - 360

        # ── bilinear over the 2x2 cell containing (lat, lon) ─────────
        # CAMS lats descend (90 → -90), so np.searchsorted on REVERSED
        # lats finds the upper edge; map back to the original index.
        # If outside the grid's bounding box, fall back to nearest cell.
        if lat > self.lats[0] or lat < self.lats[-1]:
            li = int(np.argmin(np.abs(self.lats - lat)))
            return self._cell(ti, li, self._nearest_lon_idx(lon_wrapped))
        if lon_wrapped < self.lons[0] or lon_wrapped > self.lons[-1]:
            gi = int(np.argmin(np.abs(self.lons - lon_wrapped)))
            return self._cell(ti, self._nearest_lat_idx(lat), gi)

        # Find the two grid lines that bracket the point. For a degenerate
        # 1-row or 1-column grid (test fixtures, regional pulls), fall back
        # to nearest-cell — bilinear math collapses to identity anyway.
        if len(self.lats) < 2 or len(self.lons) < 2:
            return self._cell(ti, self._nearest_lat_idx(lat), self._nearest_lon_idx(lon_wrapped))
        diffs = np.abs(self.lats - lat)
        li_a, li_b = sorted(np.argsort(diffs)[:2].tolist())

        gi_diffs = np.abs(self.lons - lon_wrapped)
        gi_a, gi_b = sorted(np.argsort(gi_diffs)[:2].tolist())

        # Fractional weights — closer cell carries more weight.
        lat_a, lat_b = float(self.lats[li_a]), float(self.lats[li_b])
        lon_a, lon_b = float(self.lons[gi_a]), float(self.lons[gi_b])
        w_lat = 0.5 if lat_a == lat_b else (lat - lat_a) / (lat_b - lat_a)
        w_lon = 0.5 if lon_a == lon_b else (lon_wrapped - lon_a) / (lon_b - lon_a)
        # Clamp to [0,1] in case lat == grid edge and rounding pushes it
        # microscopically out of range.
        w_lat = max(0.0, min(1.0, w_lat))
        w_lon = max(0.0, min(1.0, w_lon))

        def _bilin(arr: np.ndarray) -> float:
            v00 = float(arr[ti, li_a, gi_a])
            v01 = float(arr[ti, li_a, gi_b])
            v10 = float(arr[ti, li_b, gi_a])
            v11 = float(arr[ti, li_b, gi_b])
            return (
                (1 - w_lat) * (1 - w_lon) * v00
                + (1 - w_lat) * w_lon * v01
                + w_lat * (1 - w_lon) * v10
                + w_lat * w_lon * v11
            )

        out: dict[str, float | None] = {
            "ozoneDU": _bilin(self.ozone_du),
            "aod": _bilin(self.aod_550),
        }
        for key, arr in self._aq_arrays():
            if arr is not None:
                out[key] = _bilin(arr)
        return out

    def _cell(self, ti: int, li: int, gi: int) -> dict[str, float | None]:
        out: dict[str, float | None] = {
            "ozoneDU": float(self.ozone_du[ti, li, gi]),
            "aod": float(self.aod_550[ti, li, gi]),
        }
        for key, arr in self._aq_arrays():
            if arr is not None:
                out[key] = float(arr[ti, li, gi])
        return out

    def _aq_arrays(self) -> list[tuple[str, np.ndarray | None]]:
        """Iterable of optional air-quality fields paired with their
        public response key. Centralises the field roster so the two
        lookup paths (bilinear + nearest) share the same coverage."""
        return [
            ("pm25", self.pm2_5),
            ("pm10", self.pm10),
            ("no2", self.no2),
            ("so2", self.so2),
            ("co", self.co),
            ("o3Surface", self.o3_surface),
        ]

    def _nearest_lat_idx(self, lat: float) -> int:
        return int(np.argmin(np.abs(self.lats - lat)))

    def _nearest_lon_idx(self, lon: float) -> int:
        return int(np.argmin(np.abs(self.lons - lon)))


class CamsCache:
    """Holds the latest grid snapshot, refreshed on a background task.

    Threading model: the background pull writes to `_snapshot` under a
    lock; readers grab the reference (atomic) and the snapshot itself is
    immutable, so no read lock is needed.

    Persistence: when `cache_dir` is set, the snapshot is saved as an
    npz on every successful pull and reloaded on startup so a process
    restart doesn't fall through to 503 for 30 s – 5 min while CDS
    queues the first request. Stale-on-disk snapshots are still loaded;
    `is_stale` reflects their age the same way fresh-pull snapshots do.
    """

    SNAPSHOT_FILENAME = "cams-snapshot.npz"

    def __init__(self, cache_dir: str | None = None) -> None:
        self._snapshot: GridSnapshot | None = None
        self._lock = asyncio.Lock()
        self._last_error: str | None = None
        self._last_pull_attempt: float = 0.0
        # Lifetime counters surfaced on /metrics so monitors can alert
        # on sustained failure (the boolean is_stale only flips after
        # 24h; the counter triggers immediately).
        self.pull_attempts: int = 0
        self.pull_successes: int = 0
        self.pull_failures: int = 0
        self._cache_dir = cache_dir
        if cache_dir:
            os.makedirs(cache_dir, exist_ok=True)
            self._try_load_from_disk()

    @property
    def snapshot(self) -> GridSnapshot | None:
        return self._snapshot

    @property
    def last_error(self) -> str | None:
        return self._last_error

    @property
    def is_stale(self) -> bool:
        if self._snapshot is None:
            return True
        # Snapshot considered stale when the underlying forecast has aged
        # past 24h (CAMS publishes every 12h; 24h covers a missed pull).
        return (time.time() - self._snapshot.pulled_at) > 86400

    async def refresh(self) -> bool:
        async with self._lock:
            self._last_pull_attempt = time.time()
            self.pull_attempts += 1
            try:
                # Bound the CDS-API call so a hung pull doesn't wedge the
                # background loop forever (no failures counted, no
                # retries fired). Default 10 min — covers the worst-case
                # CDS queue we've seen, well below any operator's idea
                # of "too slow."
                timeout = float(os.environ.get("CAMS_PULL_TIMEOUT_SEC", "600"))
                snap = await asyncio.wait_for(
                    asyncio.to_thread(_pull_cams_blocking),
                    timeout=timeout,
                )
                self._snapshot = snap
                self._last_error = None
                self.pull_successes += 1
                logger.info(
                    "CAMS pull OK: %s timesteps, %s lats, %s lons",
                    len(snap.times),
                    len(snap.lats),
                    len(snap.lons),
                )
                if self._cache_dir:
                    try:
                        self._save_to_disk(snap)
                    except Exception as e:  # noqa: BLE001
                        logger.warning("Snapshot persist failed: %s", _redact_secrets(str(e)))
                return True
            except Exception as e:  # noqa: BLE001 — we WANT to keep serving stale on failure
                # Redact secrets BEFORE storing on the cache — the
                # value flows into /healthz JSON, the X-Cams-Last-Error
                # response header, and stdout via logger.exception.
                # cdsapi-style errors have a documented history of
                # including the request URL with the API key embedded
                # in their .args; keep them out of unauthenticated
                # observability surfaces.
                self._last_error = _redact_secrets(f"{type(e).__name__}: {e}")
                self.pull_failures += 1
                logger.exception("CAMS pull failed")
                return False

    # ── Disk persistence ─────────────────────────────────────────────

    def _disk_path(self) -> str:
        return os.path.join(self._cache_dir or ".", self.SNAPSHOT_FILENAME)

    def _save_to_disk(self, snap: GridSnapshot) -> None:
        """Persist the snapshot as an npz so the next process boot can
        warm-start instead of waiting for CDS to queue a fresh request.

        np.savez owns the file handle (no manual `with open()` wrap) so
        the ZipFile inside savez can flush its central directory before
        the underlying file closes — passing our own handle had a
        documented "I/O on closed file" failure mode on some numpy +
        filesystem combinations. We let savez add its `.npz` suffix to
        the temp name, then rename to the canonical filename atomically.
        """
        path = self._disk_path()
        tmp_base = path + ".tmp"  # savez appends .npz → tmp_base.npz
        tmp_full = tmp_base + ".npz"
        payload: dict[str, np.ndarray] = {
            "pulled_at": np.float64(snap.pulled_at),
            "valid_from": np.float64(snap.valid_from),
            "valid_to": np.float64(snap.valid_to),
            "times": snap.times,
            "lats": snap.lats,
            "lons": snap.lons,
            "ozone_du": snap.ozone_du,
            "aod_550": snap.aod_550,
        }
        # AQ fields are optional — only persist when present so older
        # snapshots remain compatible with newer code that gates on
        # `key in archive` instead of unconditional reads.
        for name, arr in (
            ("pm2_5", snap.pm2_5),
            ("pm10", snap.pm10),
            ("no2", snap.no2),
            ("so2", snap.so2),
            ("co", snap.co),
            ("o3_surface", snap.o3_surface),
        ):
            if arr is not None:
                payload[name] = arr
        np.savez_compressed(tmp_base, **payload)
        os.replace(tmp_full, path)
        logger.info("Snapshot persisted to %s (%d AQ fields)", path, len(payload) - 8)

    def _try_load_from_disk(self) -> None:
        """Best-effort load of a previous snapshot at startup. Failure
        is logged + swallowed; the background pull will refresh anyway."""
        path = self._disk_path()
        if not os.path.exists(path):
            return
        try:
            data = np.load(path, allow_pickle=False)

            def _opt(key: str) -> np.ndarray | None:
                # `key in data` is supported on NpzFile but cheap to be
                # defensive — older snapshots predate the AQ fields.
                return data[key] if key in data.files else None

            self._snapshot = GridSnapshot(
                pulled_at=float(data["pulled_at"]),
                valid_from=float(data["valid_from"]),
                valid_to=float(data["valid_to"]),
                times=data["times"],
                lats=data["lats"],
                lons=data["lons"],
                ozone_du=data["ozone_du"],
                aod_550=data["aod_550"],
                pm2_5=_opt("pm2_5"),
                pm10=_opt("pm10"),
                no2=_opt("no2"),
                so2=_opt("so2"),
                co=_opt("co"),
                o3_surface=_opt("o3_surface"),
            )
            age_h = (time.time() - self._snapshot.pulled_at) / 3600
            logger.info(
                "Loaded snapshot from disk: %.1f h old, %s timesteps",
                age_h,
                len(self._snapshot.times),
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("Failed to load snapshot from %s: %s", path, e)


def _pull_cams_blocking() -> GridSnapshot:
    """Synchronous CAMS retrieve via the CDS-API. Run from a worker thread."""
    import cdsapi  # heavy import deferred to first pull
    import xarray as xr

    api_url = os.environ.get("CAMS_API_URL", "https://ads.atmosphere.copernicus.eu/api")
    api_key = os.environ.get("CAMS_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "CAMS_API_KEY is not set. Register at "
            "https://ads.atmosphere.copernicus.eu and put your key in .env."
        )

    bbox = os.environ.get("CAMS_BBOX", "90,-180,-90,180")
    try:
        north, west, south, east = (float(x) for x in bbox.split(","))
    except ValueError as e:
        raise RuntimeError(f"CAMS_BBOX must be 'N,W,S,E' degrees; got {bbox!r}") from e

    client = cdsapi.Client(url=api_url, key=api_key, quiet=True, verify=True)

    # Forecast leadtime hours: 0..N where N = CAMS_FORECAST_HORIZON_HOURS
    # (default 120, max 120 — CAMS daily 00:00 cycle). Hourly through
    # the first 24h matches Open-Meteo's hourly resolution; 3-hourly
    # beyond that keeps payload manageable while still tracking the
    # diurnal cycle (~10-15 DU swing on ozone). App-side
    # `nearestHourIndex` snaps any in-between timestamp to the closest
    # available step.
    horizon_hours = int(os.environ.get("CAMS_FORECAST_HORIZON_HOURS", "120"))
    horizon_hours = max(24, min(120, horizon_hours))
    leadtimes_set: set[int] = set(range(0, 25))
    leadtimes_set.update(range(27, horizon_hours + 1, 3))
    leadtimes = [str(h) for h in sorted(leadtimes_set)]

    # CAMS publishes forecasts up to ~5 days ahead of the current real-
    # world date. CDS rejects requests with `date` in the future. Any
    # operator running on a clock-shifted dev box (e.g. an integration
    # harness with a baked-in date) can override this to a known-good
    # past date for smoke testing — `CAMS_DATE_OVERRIDE=2024-06-01`.
    requested_date = os.environ.get("CAMS_DATE_OVERRIDE", "").strip() or _today_utc_iso()

    # Keys match the new ADS portal contract directly. The legacy
    # cdsapi `format` key passes through `ecmwf-datastores-client`'s
    # compatibility shim, which on CAMS Atmospheric Composition
    # Forecasts auto-rewrote `format: 'netcdf_zip'` to
    # `data_format: 'netcdf'` AND injected `grid: [0.4, 0.4]` — the
    # combination ADS rejected with HTTP 400 "invalid combination of
    # values" (CAMS forecast products have a fixed native grid; an
    # explicit `grid` field is invalid for this dataset). Using the
    # ADS-native `data_format` key bypasses the shim. Scalar values
    # (`time`, `type`) are wrapped in lists to match what the ADS
    # form emits, since the shim's list-coercion path is the same
    # one that injected `grid`.
    request = {
        "variable": _CAMS_VARIABLES,
        "date": requested_date,
        "time": ["00:00"],  # most recent run; CDS auto-selects the published cycle
        "leadtime_hour": leadtimes,
        "type": ["forecast"],
        "data_format": "netcdf_zip",
        "area": [north, west, south, east],
    }

    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "cams.bin"
        client.retrieve(_CAMS_DATASET, request, str(out))
        # CDS may return a raw .nc OR a zip wrapping one or more .nc
        # files (depends on dataset config + format param). Detect the
        # magic bytes and extract before xarray tries to open. Without
        # this, an `xr.open_dataset(zip)` call fails with the unhelpful
        # "did not find a match in any of xarray's currently installed
        # IO backends" error.
        nc_path = _materialize_netcdf(out, td)
        ds = xr.open_dataset(nc_path, decode_times=True, engine="netcdf4")
        # Variable rename: CDS short names are stable but verbose; map to
        # our internal keys here so the rest of the code stays clean.
        var_map = {
            "gtco3": "ozone_du",  # current CAMS short name (since 2023)
            "tco3": "ozone_du",  # legacy short name
            "go3": "ozone_du",  # alternate name some products use
            "aod550": "aod_550",
            "aod_550": "aod_550",  # already-correct passthrough
            "t550aer": "aod_550",
            "tau_550": "aod_550",  # forecast-product variant
            # Air-quality (surface) — CAMS short names are stable for
            # the AQ fields. Add aliases as Copernicus rotates them.
            "pm2p5": "pm2_5",
            "pm2_5": "pm2_5",
            "pm10": "pm10",
            "no2": "no2",
            "so2": "so2",
            "co": "co",
            # Surface ozone (tropospheric) shares its short-name with
            # total-column in some legacy products; the dataset variant
            # we request keeps them disambiguated. If `go3` already got
            # routed to ozone_du above, this branch is never hit.
            "o3": "o3_surface",
        }
        renamed = {}
        for cds_name, our_name in var_map.items():
            if cds_name in ds and cds_name != our_name:
                renamed[cds_name] = our_name
        if renamed:
            ds = ds.rename(renamed)

        if "ozone_du" not in ds or "aod_550" not in ds:
            raise RuntimeError(
                f"CAMS response missing expected variables. Got: {list(ds.data_vars)}"
            )

        # CAMS units are kg/m² for total column ozone — convert to DU.
        # 1 DU = 2.1414e-5 kg/m² (NIST). So DU = ozone_kgm2 / 2.1414e-5.
        # If CAMS already returned DU (depends on dataset config) skip.
        # Squeeze ALL singleton dims (forecast/reference axes that drop
        # in when `time:'00:00'` is specified once); leaves us with
        # (T, LAT, LON) regardless of how many wrap dimensions CDS chose
        # to add. Without this an arbitrary singleton axis breaks the
        # (T, LAT, LON) assumption downstream.
        ozone = np.squeeze(ds["ozone_du"].values)
        if ozone.ndim != 3:
            raise RuntimeError(
                f"CAMS ozone_du array has unexpected shape {ozone.shape} after squeeze; expected (T, LAT, LON)"
            )
        units = ds["ozone_du"].attrs.get("units", "").lower()
        if "kg" in units:
            ozone = ozone / 2.1414e-5

        aod = np.squeeze(ds["aod_550"].values)
        if aod.ndim != 3:
            raise RuntimeError(
                f"CAMS aod_550 array has unexpected shape {aod.shape} after squeeze; expected (T, LAT, LON)"
            )

        # Air-quality fields. CAMS units for these are kg/m³ at the
        # surface; convert to µg/m³ (1 kg/m³ = 1e9 µg/m³). Missing
        # variables (older snapshots, partial pulls) stay None — the
        # consumer skips them gracefully. Unit detection pins to the
        # expected per-volume form; matching just `"kg"` would also
        # accept `kg/m²` (column burden) which would silently produce
        # nonsense if CAMS ever changed its product.
        _KG_VOLUME_UNITS = ("kg m**-3", "kg/m**3", "kg/m^3", "kg m^-3", "kg.m-3")

        def _aq(name: str) -> np.ndarray | None:
            if name not in ds:
                return None
            arr = np.squeeze(ds[name].values)
            if arr.ndim != 3:
                return None
            units = ds[name].attrs.get("units", "").lower().replace(" ", "")
            if any(u.replace(" ", "") in units for u in _KG_VOLUME_UNITS):
                arr = arr * 1e9
            elif "kg" in units:
                # Unknown kg-based unit — skip the convert + warn. The
                # raw value still flows through; downstream consumers
                # can still detect anomalies via range checks.
                logger.warning(
                    "CAMS field %s has unrecognised kg-based unit %r; leaving raw",
                    name,
                    units,
                )
            return arr

        pm2_5 = _aq("pm2_5")
        pm10 = _aq("pm10")
        no2 = _aq("no2")
        so2 = _aq("so2")
        co = _aq("co")
        o3_surface = _aq("o3_surface")

        # Time axis: CAMS uses CDS-MAGICS conventions — `valid_time` is
        # the per-step timestamp we want (= forecast_reference_time +
        # forecast_period). When a request fixes `time: 00:00` and asks
        # for N leadtimes, `valid_time` arrives shaped (1, N) instead of
        # (N,). Flatten to a 1-D array so downstream code can index with
        # `times[i]` without runtime shape surprises. Also collapse the
        # corresponding lead dimension on the data variables below.
        time_var = "valid_time" if "valid_time" in ds else "time"
        raw_times = ds[time_var].values
        times = (
            np.asarray(raw_times).reshape(-1).astype("datetime64[s]").astype(np.int64).astype(float)
        )
        lats = ds["latitude"].values.astype(float)
        lons = ds["longitude"].values.astype(float)

        # Sort lats descending (CAMS convention) so np.argmin behaves.
        # The same flip must reach every (T, LAT, LON) data array — UV
        # drivers AND AQ fields — otherwise their index math diverges.
        if lats[0] < lats[-1]:
            lats = lats[::-1]
            ozone = ozone[:, ::-1, :]
            aod = aod[:, ::-1, :]
            if pm2_5 is not None:
                pm2_5 = pm2_5[:, ::-1, :]
            if pm10 is not None:
                pm10 = pm10[:, ::-1, :]
            if no2 is not None:
                no2 = no2[:, ::-1, :]
            if so2 is not None:
                so2 = so2[:, ::-1, :]
            if co is not None:
                co = co[:, ::-1, :]
            if o3_surface is not None:
                o3_surface = o3_surface[:, ::-1, :]

        # Sort the time axis ascending so np.searchsorted in lookup() is
        # sound. With mixed 1h/3h leadtimes (cams.py request shape),
        # CAMS happens to return them in order today, but xarray's
        # coordinate handling isn't a contract we should rely on — a
        # silently-unsorted axis would corrupt every per-hour lookup
        # with no error surfacing.
        if not np.all(np.diff(times) >= 0):
            order = np.argsort(times)
            times = times[order]
            ozone = ozone[order, :, :]
            aod = aod[order, :, :]
            if pm2_5 is not None:
                pm2_5 = pm2_5[order, :, :]
            if pm10 is not None:
                pm10 = pm10[order, :, :]
            if no2 is not None:
                no2 = no2[order, :, :]
            if so2 is not None:
                so2 = so2[order, :, :]
            if co is not None:
                co = co[order, :, :]
            if o3_surface is not None:
                o3_surface = o3_surface[order, :, :]

        return GridSnapshot(
            pulled_at=time.time(),
            valid_from=float(times[0]),
            valid_to=float(times[-1]),
            times=times,
            lats=lats,
            lons=lons,
            ozone_du=ozone,
            aod_550=aod,
            pm2_5=pm2_5,
            pm10=pm10,
            no2=no2,
            so2=so2,
            co=co,
            o3_surface=o3_surface,
        )


def _today_utc_iso() -> str:
    """UTC date string for the CDS request 'date' field."""
    import datetime as dt

    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%d")


def _materialize_netcdf(downloaded: Path, work_dir: str) -> Path:
    """Return a path to a usable .nc file, unzipping if CDS gave us a zip.

    Magic bytes:
      • zip   → 50 4b 03 04
      • HDF5  → 89 48 44 46 (modern netCDF-4)
      • CDF   → 43 44 46 01 / 43 44 46 02 (classic netCDF-3)
    """
    import zipfile

    head = downloaded.read_bytes()[:4]
    if head[:4] == b"PK\x03\x04":
        with zipfile.ZipFile(downloaded) as zf:
            nc_members = [n for n in zf.namelist() if n.lower().endswith(".nc")]
            if not nc_members:
                raise RuntimeError(f"CAMS zip contains no .nc file. Members: {zf.namelist()}")
            # CDS occasionally splits per-variable into separate .nc files.
            # Prefer the largest one (the merged forecast); the small ones
            # are typically per-step companions we don't need.
            target = max(
                nc_members,
                key=lambda n: zf.getinfo(n).file_size,
            )
            zf.extract(target, work_dir)
            return Path(work_dir) / target
    if head[:4] in (b"\x89HDF", b"CDF\x01", b"CDF\x02"):
        return downloaded
    # Anything else: peek at the first few bytes to surface a useful
    # error. If CDS returned an HTML/JSON error page, we want the
    # message visible in the logs rather than xarray's generic
    # "no IO backend" failure.
    raise RuntimeError(
        f"Downloaded CAMS file is not a netCDF or zip. First bytes: {head!r}. "
        f"Full content (truncated): {downloaded.read_bytes()[:512]!r}"
    )


async def background_pull_loop(cache: CamsCache, interval_sec: int) -> None:
    """Refresh `cache` every `interval_sec` seconds, with exponential
    backoff retries on failure so a transient CDS hiccup doesn't leave
    the grid stale for the full 6-hour interval.

    Backoff: 60 s → 120 s → 240 s → 480 s → 960 s → 1800 s (cap).
    Resets to 60 s after a successful pull. Capped to never exceed the
    nominal interval so the schedule still moves forward.
    """
    INITIAL_BACKOFF_SEC = 60
    MAX_BACKOFF_SEC = min(1800, interval_sec)

    # Initial pull on boot — server should serve real data ASAP. Failure
    # is logged but doesn't kill the process; reads return 503 (or serve
    # the persisted snapshot if one was loaded from disk) until a
    # successful pull lands.
    backoff = INITIAL_BACKOFF_SEC
    while True:
        ok = await cache.refresh()
        if ok:
            await asyncio.sleep(interval_sec)
            backoff = INITIAL_BACKOFF_SEC
            continue
        # Failure path: short retry sleep with exponential backoff,
        # capped so we never block longer than the nominal interval.
        logger.warning(
            "CAMS pull failed; retrying in %d s (next backoff: %d s)",
            backoff,
            min(backoff * 2, MAX_BACKOFF_SEC),
        )
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, MAX_BACKOFF_SEC)

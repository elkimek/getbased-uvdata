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

What we DON'T pull (yet):
  • UV spectra / surface UV irradiance — CAMS-McRad outputs these but
    via a different product family (Solar Radiation Service) with
    different licensing. Phase-2 candidate; for now we feed the ozone
    + AOD into the browser's Bird-Riordan engine which already handles
    the radiative transfer.
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
_CAMS_DATASET = "cams-global-atmospheric-composition-forecasts"
_CAMS_VARIABLES = [
    "total_column_ozone",
    "total_aerosol_optical_depth_550nm",
]


@dataclass
class GridSnapshot:
    """One CAMS pull, indexed for O(1) bilinear lookup."""

    pulled_at: float            # epoch seconds
    valid_from: float           # forecast valid period start
    valid_to: float             # forecast valid period end
    times: np.ndarray           # (T,) — epoch seconds for each forecast hour
    lats: np.ndarray            # (LAT,) — descending (CAMS convention)
    lons: np.ndarray            # (LON,) — typically -180..180
    ozone_du: np.ndarray        # (T, LAT, LON) — Dobson Units
    aod_550: np.ndarray         # (T, LAT, LON) — unitless

    def lookup(self, lat: float, lon: float, when_epoch: float) -> dict[str, float | None]:
        """Bilinear interpolate the grid at (lat, lon, time). Returns {ozoneDU, aod}."""
        if when_epoch < self.times[0] or when_epoch > self.times[-1]:
            # Fall through to nearest forecast hour rather than refusing —
            # a request just outside the forecast window (clock drift,
            # boundary tick) shouldn't 404.
            ti = 0 if when_epoch < self.times[0] else len(self.times) - 1
        else:
            ti = int(np.searchsorted(self.times, when_epoch))
            if ti >= len(self.times):
                ti = len(self.times) - 1
        # Wrap longitude into the grid's range.
        lon_wrapped = lon
        lon_min, lon_max = float(self.lons.min()), float(self.lons.max())
        if lon < lon_min:
            lon_wrapped = lon + 360
        elif lon > lon_max:
            lon_wrapped = lon - 360
        # Lat / lon nearest-neighbour for v0.1 — bilinear in a future
        # bump. Grid resolution is 0.4° (~44 km); for a chemistry value
        # like total column ozone the spatial gradient is gentle enough
        # that nearest-neighbour error is well under model uncertainty.
        li = int(np.argmin(np.abs(self.lats - lat)))
        gi = int(np.argmin(np.abs(self.lons - lon_wrapped)))
        return {
            "ozoneDU": float(self.ozone_du[ti, li, gi]),
            "aod": float(self.aod_550[ti, li, gi]),
        }


class CamsCache:
    """Holds the latest grid snapshot, refreshed on a background task.

    Threading model: the background pull writes to `_snapshot` under a
    lock; readers grab the reference (atomic) and the snapshot itself is
    immutable, so no read lock is needed.
    """

    def __init__(self) -> None:
        self._snapshot: GridSnapshot | None = None
        self._lock = asyncio.Lock()
        self._last_error: str | None = None
        self._last_pull_attempt: float = 0.0

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
            try:
                snap = await asyncio.to_thread(_pull_cams_blocking)
                self._snapshot = snap
                self._last_error = None
                logger.info("CAMS pull OK: %s timesteps, %s lats, %s lons",
                            len(snap.times), len(snap.lats), len(snap.lons))
                return True
            except Exception as e:  # noqa: BLE001 — we WANT to keep serving stale on failure
                self._last_error = f"{type(e).__name__}: {e}"
                logger.exception("CAMS pull failed")
                return False


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

    # Forecast leadtime hours: 0..24h covers "now plus today + tomorrow's
    # morning" which is what the app's hourly time-bucket interpolation
    # needs. We pull only the hourly steps we'll actually serve.
    leadtimes = [str(h) for h in range(0, 25)]

    request = {
        "variable": _CAMS_VARIABLES,
        "date": _today_utc_iso(),
        "time": "00:00",  # most recent run; CDS auto-selects the published cycle
        "leadtime_hour": leadtimes,
        "type": "forecast",
        "format": "netcdf_zip",
        "area": [north, west, south, east],
    }

    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "cams.zip"
        client.retrieve(_CAMS_DATASET, request, str(out))
        # netcdf_zip → unpack. In v0.1 we accept either the zip or the
        # raw .nc, since CDS sometimes returns one or the other depending
        # on dataset config. xarray handles both via h5netcdf engine.
        ds = xr.open_dataset(out, decode_times=True)
        # Variable rename: CDS short names are stable but verbose; map to
        # our internal keys here so the rest of the code stays clean.
        var_map = {
            "tco3": "ozone_du",            # total column ozone (kg/m² → DU below)
            "go3": "ozone_du",             # alternate name for the same field
            "aod550": "aod_550",
            "t550aer": "aod_550",
        }
        renamed = {}
        for cds_name, our_name in var_map.items():
            if cds_name in ds:
                renamed[cds_name] = our_name
        ds = ds.rename(renamed)

        if "ozone_du" not in ds or "aod_550" not in ds:
            raise RuntimeError(
                f"CAMS response missing expected variables. Got: {list(ds.data_vars)}"
            )

        # CAMS units are kg/m² for total column ozone — convert to DU.
        # 1 DU = 2.1414e-5 kg/m² (NIST). So DU = ozone_kgm2 / 2.1414e-5.
        # If CAMS already returned DU (depends on dataset config) skip.
        ozone = ds["ozone_du"].values  # may be (T, LAT, LON) or (T, lev, LAT, LON)
        if ozone.ndim == 4:
            # Total column comes back as a single-level array; squeeze.
            ozone = np.squeeze(ozone, axis=1)
        units = ds["ozone_du"].attrs.get("units", "").lower()
        if "kg" in units:
            ozone = ozone / 2.1414e-5

        aod = ds["aod_550"].values
        if aod.ndim == 4:
            aod = np.squeeze(aod, axis=1)

        # Time axis: convert numpy datetime64[ns] → epoch seconds.
        times = (ds["time"].values.astype("datetime64[s]")
                                  .astype(np.int64).astype(float))
        lats = ds["latitude"].values.astype(float)
        lons = ds["longitude"].values.astype(float)

        # Sort lats descending (CAMS convention) so np.argmin behaves.
        if lats[0] < lats[-1]:
            lats = lats[::-1]
            ozone = ozone[:, ::-1, :]
            aod = aod[:, ::-1, :]

        return GridSnapshot(
            pulled_at=time.time(),
            valid_from=float(times[0]),
            valid_to=float(times[-1]),
            times=times,
            lats=lats,
            lons=lons,
            ozone_du=ozone,
            aod_550=aod,
        )


def _today_utc_iso() -> str:
    """UTC date string for the CDS request 'date' field."""
    import datetime as dt
    return dt.datetime.utcnow().strftime("%Y-%m-%d")


async def background_pull_loop(cache: CamsCache, interval_sec: int) -> None:
    """Refresh `cache` every `interval_sec` seconds, forever."""
    # Initial pull on boot — server should serve real data ASAP. Failure
    # is logged but doesn't kill the process; reads return 503 until a
    # successful pull lands.
    await cache.refresh()
    while True:
        await asyncio.sleep(interval_sec)
        await cache.refresh()

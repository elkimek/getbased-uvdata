"""Smoke tests — exercise the parts that don't need CDS-API access.

Real CAMS pulls can't run in CI without a key + queue time, so this
suite covers the response-shape contract and the auth gate. The
end-to-end CAMS path is exercised by `getbased-uvdata` running on the
hosted instance behind health monitoring.
"""

from __future__ import annotations

import time

import numpy as np
import pytest
from fastapi.testclient import TestClient

from getbased_uvdata.cams import CamsCache, GridSnapshot
from getbased_uvdata.reshape import build_response


def _fake_snapshot(with_aq: bool = False) -> GridSnapshot:
    """One-hour, 2x2 grid with predictable values for assertion math."""
    now = time.time()
    times = np.array([now], dtype=float)
    lats = np.array([10.0, 0.0])  # descending, CAMS convention
    lons = np.array([0.0, 10.0])
    ozone = np.array([[[300.0, 310.0], [320.0, 330.0]]])  # (T, LAT, LON)
    aod = np.array([[[0.10, 0.15], [0.20, 0.25]]])
    aq_kwargs: dict = {}
    if with_aq:
        aq_kwargs = {
            "pm2_5": np.array([[[5.0, 6.0], [7.0, 8.0]]]),
            "pm10": np.array([[[10.0, 12.0], [14.0, 16.0]]]),
            "no2": np.array([[[20.0, 22.0], [24.0, 26.0]]]),
            "so2": np.array([[[1.0, 1.5], [2.0, 2.5]]]),
            "co": np.array([[[200.0, 210.0], [220.0, 230.0]]]),
            "o3_surface": np.array([[[60.0, 65.0], [70.0, 75.0]]]),
        }
    return GridSnapshot(
        pulled_at=now,
        valid_from=now,
        valid_to=now + 3600,
        times=times,
        lats=lats,
        lons=lons,
        ozone_du=ozone,
        aod_550=aod,
        **aq_kwargs,
    )


class TestGridLookup:
    def test_exact_grid_corner(self):
        snap = _fake_snapshot()
        # Exact grid coords still return the corresponding cell — bilinear
        # weights collapse to 1.0/0.0 at the corners.
        out = snap.lookup(10.0, 0.0, snap.times[0])
        assert abs(out["ozoneDU"] - 300.0) < 1e-9
        assert abs(out["aod"] - 0.10) < 1e-9
        out = snap.lookup(0.0, 10.0, snap.times[0])
        assert abs(out["ozoneDU"] - 330.0) < 1e-9
        assert abs(out["aod"] - 0.25) < 1e-9

    def test_bilinear_midpoint(self):
        """Centre of the 2×2 cell averages the four corners."""
        snap = _fake_snapshot()
        # Corners: (10,0)=300, (10,10)=310, (0,0)=320, (0,10)=330.
        # Centre (5,5) → (300+310+320+330)/4 = 315.
        out = snap.lookup(5.0, 5.0, snap.times[0])
        assert abs(out["ozoneDU"] - 315.0) < 1e-9
        # AOD same: (0.10+0.15+0.20+0.25)/4 = 0.175
        assert abs(out["aod"] - 0.175) < 1e-9

    def test_bilinear_off_centre(self):
        """Quarter into the cell weights closer corners more."""
        snap = _fake_snapshot()
        # (lat=7.5, lon=2.5) — 25% from (10,0), 75% from the row
        # that mixes (10,0) and (10,10).
        out = snap.lookup(7.5, 2.5, snap.times[0])
        # Expected:
        #  w_lat: lats are [10, 0]; 7.5 between → (7.5-10)/(0-10)=0.25
        #  w_lon: lons are [0, 10]; 2.5 between → 0.25
        #  v = (1-0.25)(1-0.25)*300 + (1-0.25)*0.25*310
        #    + 0.25*(1-0.25)*320 + 0.25*0.25*330
        expected = 0.5625 * 300 + 0.1875 * 310 + 0.1875 * 320 + 0.0625 * 330
        assert abs(out["ozoneDU"] - expected) < 1e-9

    def test_outside_bbox_falls_back_to_nearest(self):
        """Points outside the grid's bounding box use nearest-cell, not extrapolation."""
        snap = _fake_snapshot()
        # lat=20 is north of the grid (grid lats are 10, 0). Should
        # snap to the lat=10 row.
        out = snap.lookup(20.0, 0.0, snap.times[0])
        assert abs(out["ozoneDU"] - 300.0) < 1e-9

    def test_clamps_outside_time_window(self):
        snap = _fake_snapshot()
        # Way before the snapshot — clamps to first timestep, not 404.
        out = snap.lookup(10.0, 0.0, snap.times[0] - 86400)
        assert abs(out["ozoneDU"] - 300.0) < 1e-9

    def test_air_quality_fields_surface_when_present(self):
        """Lookup includes AQ fields when the snapshot carries them.
        Bilinear interp is the same math as ozone/aod, so verifying
        midpoint averaging confirms the loop covers every AQ var."""
        snap = _fake_snapshot(with_aq=True)
        out = snap.lookup(5.0, 5.0, snap.times[0])
        # Midpoint of (5,6,7,8) = 6.5 for pm2_5
        assert abs(out["pm25"] - 6.5) < 1e-9
        # (200+210+220+230)/4 = 215 for CO
        assert abs(out["co"] - 215.0) < 1e-9
        # (60+65+70+75)/4 = 67.5 for surface ozone
        assert abs(out["o3Surface"] - 67.5) < 1e-9

    def test_air_quality_fields_absent_when_not_pulled(self):
        """Lookup omits AQ keys entirely when fields are None — no
        Nones surfacing in the response, no KeyError."""
        snap = _fake_snapshot(with_aq=False)
        out = snap.lookup(5.0, 5.0, snap.times[0])
        assert "pm25" not in out
        assert "co" not in out
        assert "ozoneDU" in out  # core field still there


class TestSnapshotPersistence:
    def test_save_and_reload_round_trip(self, tmp_path):
        """A snapshot persisted to disk and reloaded into a fresh
        CamsCache produces identical lookup output — restart warm-start
        works."""

        original = _fake_snapshot()
        cache_a = CamsCache(cache_dir=str(tmp_path))
        cache_a._snapshot = original  # type: ignore[attr-defined]
        cache_a._save_to_disk(original)  # type: ignore[attr-defined]
        # Fresh cache instance reads the file on init.
        cache_b = CamsCache(cache_dir=str(tmp_path))
        assert cache_b.snapshot is not None
        # Lookups match between original and reloaded snapshots.
        for lat, lon in [(10.0, 0.0), (5.0, 5.0), (0.0, 10.0)]:
            a = original.lookup(lat, lon, original.times[0])
            b = cache_b.snapshot.lookup(lat, lon, original.times[0])
            assert abs(a["ozoneDU"] - b["ozoneDU"]) < 1e-9
            assert abs(a["aod"] - b["aod"]) < 1e-9

    def test_missing_file_is_silent(self, tmp_path):
        """Empty cache directory just yields no snapshot — not an error."""
        cache = CamsCache(cache_dir=str(tmp_path))
        assert cache.snapshot is None

    def test_aq_fields_round_trip_through_disk(self, tmp_path):
        """A snapshot WITH air-quality fields persists and reloads
        them — proving the npz-payload extension works."""
        original = _fake_snapshot(with_aq=True)
        cache_a = CamsCache(cache_dir=str(tmp_path))
        cache_a._snapshot = original  # type: ignore[attr-defined]
        cache_a._save_to_disk(original)  # type: ignore[attr-defined]
        cache_b = CamsCache(cache_dir=str(tmp_path))
        assert cache_b.snapshot is not None
        out = cache_b.snapshot.lookup(5.0, 5.0, original.times[0])
        # Same midpoint values as the in-memory test above.
        assert abs(out["pm25"] - 6.5) < 1e-9
        assert abs(out["o3Surface"] - 67.5) < 1e-9


class TestStaleStagingSweep:
    """Regression: a SIGKILL-during-retrieve used to leak `tmpXXXX`
    dirs (~470 MB each) under `/tmp` inside the container's writable
    layer. In production this filled the host disk to 100% and wedged
    the colocated evolu-relay's SQLite (`SQLITE_FULL`). The fix moves
    staging into the persistent cache_dir AND sweeps orphans on
    startup, so post-crash recovery is automatic."""

    def test_orphan_staging_dirs_cleaned_at_startup(self, tmp_path):
        """A stale `cams-stage-*` dir from a prior run is removed
        when a fresh CamsCache is constructed."""
        stale = tmp_path / "cams-stage-leftover"
        stale.mkdir()
        (stale / "cams.bin").write_bytes(b"orphan netcdf payload")
        assert stale.exists()
        CamsCache(cache_dir=str(tmp_path))
        assert not stale.exists(), "stale staging dir should be swept at init"

    def test_sweep_does_not_touch_unrelated_files(self, tmp_path):
        """Sweep is prefix-scoped — it must not delete the snapshot
        npz, user files, or any non-prefixed entry."""
        snapshot = tmp_path / CamsCache.SNAPSHOT_FILENAME
        snapshot.write_bytes(b"important snapshot")
        unrelated = tmp_path / "user-config.json"
        unrelated.write_bytes(b"{}")
        stale = tmp_path / "cams-stage-doomed"
        stale.mkdir()
        CamsCache(cache_dir=str(tmp_path))
        assert snapshot.exists(), "snapshot must survive sweep"
        assert unrelated.exists(), "unrelated files must survive sweep"
        assert not stale.exists(), "only cams-stage-* dirs are removed"


class TestRedaction:
    def test_redact_secrets_strips_live_values(self, monkeypatch):
        """`_redact_secrets` MUST scrub live env-var values from any
        string before it reaches /healthz, response headers, or logs.
        Critical: cdsapi exception strings can include the API key in
        the URL and the bearer in 401 bodies."""
        from getbased_uvdata.cams import _redact_secrets

        monkeypatch.setenv("CAMS_API_KEY", "abcdef-1234567890")
        monkeypatch.setenv("GETBASED_UVDATA_BEARER", "secret-bearer-token-xyz")
        msg = "HTTPError: 401 from https://ads...api/?key=abcdef-1234567890 (bearer secret-bearer-token-xyz)"
        out = _redact_secrets(msg)
        assert "abcdef-1234567890" not in out
        assert "secret-bearer-token-xyz" not in out
        assert "<CAMS_API_KEY-redacted>" in out
        assert "<GETBASED_UVDATA_BEARER-redacted>" in out

    def test_redact_secrets_skips_short_values(self, monkeypatch):
        """Don't replace empty / very-short env values — they'd match
        too aggressively."""
        from getbased_uvdata.cams import _redact_secrets

        monkeypatch.setenv("CAMS_API_KEY", "")
        out = _redact_secrets("error: connection refused")
        assert out == "error: connection refused"


class TestSpectrum:
    """Bird-Riordan port pinned against published TUV/NIWA reference
    points so it stays in lockstep with js/sun-spectrum.js. If either
    side drifts, both these and the JS test-sun-spectrum suite catch it."""

    def test_extraterrestrial_irradiance_anchors(self):
        from getbased_uvdata.spectrum import extraterrestrial_irradiance

        # Anchor points from ASTM E490 — must match exactly (same table).
        assert abs(extraterrestrial_irradiance(300) - 0.541) < 1e-6
        assert abs(extraterrestrial_irradiance(450) - 2.066) < 1e-6
        # Linear interp at 310 nm = 300 + 50% of (320 - 300) = 0.541 + 0.5*(0.815-0.541)
        assert abs(extraterrestrial_irradiance(310) - 0.678) < 1e-3

    def test_erythemal_action_spectrum(self):
        from getbased_uvdata.spectrum import erythemal_at

        # Plateau in the UVB peak
        assert erythemal_at(280) == 1.0
        assert erythemal_at(298) == 1.0
        # 313 nm: 10^(0.094*(298-313)) = 10^-1.41 ≈ 0.039
        assert abs(erythemal_at(313) - 10 ** (0.094 * (298 - 313))) < 1e-9
        # Long-UVA tail at 380 nm: 10^(0.015*(140-380)) = 10^-3.6 ≈ 2.5e-4
        assert erythemal_at(380) > 0
        assert erythemal_at(380) < 1e-3
        assert erythemal_at(401) == 0.0

    def test_ozone_absorption_table_interpolation(self):
        from getbased_uvdata.spectrum import ozone_absorption

        # Direct table hit at 305 nm (UVB-cutoff sensitive wavelength).
        # σ = 1.50e-19 cm² × 2.69e19 normalisation = 4.035 unitless.
        assert abs(ozone_absorption(305) - 4.035) < 0.01
        # Log-space interp at 308 nm should fall between 305 and 310.
        s305 = ozone_absorption(305)
        s310 = ozone_absorption(310)
        s308 = ozone_absorption(308)
        # In log-space, s308 should be between s305 and s310
        assert s310 < s308 < s305

    def test_reconstruct_spectrum_zero_below_horizon(self):
        from getbased_uvdata.spectrum import reconstruct_spectrum

        spec = reconstruct_spectrum(zenith_deg=90, ozone_du=300, altitude_m=0, cloud_cover=0)
        assert all(v == 0 for v in spec.irradiance)

    def test_reconstruct_spectrum_clear_noon_uvi_in_band(self):
        """At zenith=30° / 300 DU / sea level / no cloud the implied UVI
        should land 5-9 (real summer-noon midlatitude UVI ~7-8)."""
        from getbased_uvdata.spectrum import reconstruct_spectrum, uvi_from_spectrum

        spec = reconstruct_spectrum(zenith_deg=30, ozone_du=300, altitude_m=0, cloud_cover=0)
        uvi = uvi_from_spectrum(spec)
        assert 5.0 < uvi < 9.0, f"got UVI {uvi:.2f}, expected 5-9"

    def test_reconstruct_spectrum_lower_uvi_at_low_sun(self):
        from getbased_uvdata.spectrum import reconstruct_spectrum, uvi_from_spectrum

        # Same atmosphere, two zenith angles — the lower sun must
        # produce a lower UVI through path-length attenuation alone.
        spec_high = reconstruct_spectrum(zenith_deg=30, ozone_du=300, altitude_m=0, cloud_cover=0)
        spec_low = reconstruct_spectrum(zenith_deg=70, ozone_du=300, altitude_m=0, cloud_cover=0)
        assert uvi_from_spectrum(spec_high) > uvi_from_spectrum(spec_low) * 5

    def test_solar_zenith_angle_noon_at_equator(self):
        """Noon UTC at (0°N, 0°E) on the equinox → near-zero zenith."""
        from getbased_uvdata.spectrum import solar_zenith_angle

        # 2024-03-21 12:00 UTC = roughly equinox noon at Greenwich
        z = solar_zenith_angle(1711022400, 0, 0)
        assert z < 5, f"got {z:.2f}° expected <5°"

    def test_solar_zenith_angle_midnight_below_horizon(self):
        from getbased_uvdata.spectrum import solar_zenith_angle

        # 2024-06-21 00:00 UTC at (50°N, 0°E) — midnight, sun well below horizon
        z = solar_zenith_angle(1718928000, 50, 0)
        assert z > 90, f"got {z:.2f}° expected >90°"

    def test_solar_zenith_angle_js_lockstep_anchor(self):
        """JS lockstep: at 2024-06-01 12:00 UTC at (50°N, 14°E)
        (Prague past-solar-noon, ~13:00 local solar time given +56 min
        longitudinal offset) the formula produces a zenith of ~30.2°.
        Anchored to a manual computation of the JS implementation
        (declination ~22.1°, hour-angle ~14.6°, cos(zenith) ~0.865).

        If this test fails the Python port has drifted from
        `js/sun-uvdata.js solarZenithAngle` — that's a P0 lockstep
        bug because /spectrum responses would no longer match what
        the browser would compute locally for the same coords. Fix
        by aligning both sides simultaneously, never just here."""
        from getbased_uvdata.spectrum import solar_zenith_angle

        z = solar_zenith_angle(1717243200, 50.0, 14.0)
        assert 29.5 < z < 31.0, f"got {z:.2f}° expected 30.0-30.5 (JS lockstep)"


class TestRetryBackoff:
    @pytest.mark.asyncio
    async def test_backoff_retries_on_failure_then_recovers(self, monkeypatch):
        """`background_pull_loop` should retry quickly after a failed
        pull (not wait the full interval), then return to the nominal
        cadence once a refresh succeeds. Validates the failure-counter
        as a side effect."""
        import asyncio as _asyncio
        from getbased_uvdata import cams as cams_mod

        # Sequence: fail, fail, succeed, then loop forever sleeping.
        results = [False, False, True]

        class FakeCache:
            pull_attempts = 0
            pull_failures = 0
            pull_successes = 0

            async def refresh(self) -> bool:
                self.pull_attempts += 1
                if not results:
                    return True
                ok = results.pop(0)
                if ok:
                    self.pull_successes += 1
                else:
                    self.pull_failures += 1
                return ok

        cache = FakeCache()
        # Replace asyncio.sleep so the test doesn't actually wait minutes.
        # Capture the REAL sleep before patching so our fake can yield
        # to the event loop without infinite recursion.
        real_sleep = _asyncio.sleep
        sleeps: list[float] = []

        async def _instant_sleep(s):
            sleeps.append(s)
            await real_sleep(0)

        monkeypatch.setattr(cams_mod.asyncio, "sleep", _instant_sleep)
        # Run the loop briefly, then cancel.
        task = _asyncio.create_task(cams_mod.background_pull_loop(cache, interval_sec=600))
        # Give the task time to step through 3 refresh calls. Each call
        # awaits exactly one sleep, so 3 attempts → 3 entries in `sleeps`.
        for _ in range(20):
            await _asyncio.sleep(0)
            if cache.pull_attempts >= 3 and len(sleeps) >= 3:
                break
        task.cancel()
        try:
            await task
        except _asyncio.CancelledError:
            # Expected after cancel(); swallow so the test continues.
            pass

        assert cache.pull_attempts >= 3
        assert cache.pull_failures == 2
        assert cache.pull_successes == 1
        # Filter out 0-second sleeps (our test harness's yield), keep
        # the actual backoff durations the loop emitted.
        backoff_sleeps = [s for s in sleeps if s > 0]
        # First fail → 60s sleep; second fail → 120s; third (success) → interval_sec=600.
        assert backoff_sleeps[:3] == [60, 120, 600], f"got {backoff_sleeps[:3]}"


class TestReshape:
    def test_cams_only_envelope_is_open_meteo_shaped(self):
        cams = {"ozoneDU": 305.0, "aod": 0.12}
        resp = build_response(
            lat=50.0,
            lon=14.0,
            when_iso=None,
            cams_lookup=cams,
            openmeteo=None,
            cams_pulled_at=1700000000,
            snapshot_valid_from=1700000000,
            snapshot_valid_to=1700086400,
        )
        # Browser parser expects these keys.
        assert "hourly" in resp
        assert "time" in resp["hourly"]
        assert "ozone_du" in resp["hourly"]
        assert resp["hourly"]["ozone_du"] == [305.0]
        assert resp["hourly"]["aod"] == [0.12]
        assert resp["_camsMeta"]["source"] == "cams"

    def test_merge_overlays_cams_extras_into_openmeteo_envelope(self):
        cams = {"ozoneDU": 290.0, "aod": 0.08}
        om = {
            "forecast": {
                "latitude": 50.0,
                "longitude": 14.0,
                "hourly": {
                    "time": ["2026-05-04T10:00", "2026-05-04T11:00"],
                    "uv_index": [3.0, 4.5],
                    "cloud_cover": [10, 20],
                },
            },
            "airQuality": {"current": {"european_aqi": 30}},
        }
        resp = build_response(
            lat=50.0,
            lon=14.0,
            when_iso="2026-05-04T10:30",
            cams_lookup=cams,
            openmeteo=om,
            cams_pulled_at=1700000000,
            snapshot_valid_from=1700000000,
            snapshot_valid_to=1700086400,
        )
        # No snapshot plumbed through → fall back to broadcasting the
        # single CAMS lookup across every hourly entry.
        assert resp["hourly"]["ozone_du"] == [290.0, 290.0]
        assert resp["hourly"]["aod"] == [0.08, 0.08]
        # Open-Meteo fields preserved untouched.
        assert resp["hourly"]["uv_index"] == [3.0, 4.5]
        assert resp["airQuality"]["current"]["european_aqi"] == 30

    def test_per_hour_interpolation_when_snapshot_supplied(self):
        """Each hourly entry gets its own CAMS lookup against the
        snapshot's leadtime axis — different hours of the day pick up
        different ozone/AOD values, not a flat broadcast."""
        import time as _time
        import numpy as np

        from getbased_uvdata.cams import GridSnapshot

        # Two-hour snapshot at the same gridpoint with deliberately
        # different ozone values so we can prove per-hour selection.
        # Hour A = epoch 1717239600 (2024-06-01 11:00 UTC), DU 300
        # Hour B = epoch 1717243200 (2024-06-01 12:00 UTC), DU 320
        snap = GridSnapshot(
            pulled_at=_time.time(),
            valid_from=1717239600.0,
            valid_to=1717243200.0,
            times=np.array([1717239600.0, 1717243200.0]),
            lats=np.array([50.0]),
            lons=np.array([14.0]),
            ozone_du=np.array([[[300.0]], [[320.0]]]),
            aod_550=np.array([[[0.1]], [[0.2]]]),
        )
        om = {
            "forecast": {
                "latitude": 50.0,
                "longitude": 14.0,
                "utc_offset_seconds": 0,  # Open-Meteo strings already UTC
                "hourly": {
                    "time": ["2024-06-01T11:00", "2024-06-01T12:00"],
                    "uv_index": [3.0, 4.5],
                },
            },
        }
        resp = build_response(
            lat=50.0,
            lon=14.0,
            when_iso="2024-06-01T11:30Z",
            cams_lookup={"ozoneDU": -999, "aod": -999},  # should NOT leak through
            openmeteo=om,
            cams_pulled_at=_time.time(),
            snapshot_valid_from=1717239600.0,
            snapshot_valid_to=1717243200.0,
            snapshot=snap,
        )
        # Per-hour values, not the broadcast scalar.
        assert resp["hourly"]["ozone_du"] == [300.0, 320.0]
        assert resp["hourly"]["aod"] == [0.1, 0.2]
        # The cams_lookup fallback value (-999) must NOT appear.
        assert -999 not in resp["hourly"]["ozone_du"]


class TestServer:
    @pytest.fixture
    def client_with_cache(self, monkeypatch):
        # Skip the real lifespan / CDS-API pull by injecting a fake cache
        # straight into app.state. TestClient still wires routes correctly.
        # Disable the disk cache (default /data needs root) so lifespan's
        # mkdirs doesn't blow up before we get to inject our fake.
        monkeypatch.setenv("CAMS_CACHE_DIR", "")
        from getbased_uvdata.server import app as real_app

        client = TestClient(real_app)
        with client:
            real_app.state.cams = type(
                "FakeCache",
                (),
                {
                    "snapshot": _fake_snapshot(),
                    "is_stale": False,
                    "last_error": None,
                },
            )()
            yield client

    def test_healthz_reports_grid_metadata(self, client_with_cache):
        r = client_with_cache.get("/healthz")
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        # last_error is intentionally NOT exposed on the open /healthz
        # endpoint — see the security audit. It lives on /metrics behind
        # the bearer.
        assert "last_error" not in body["cams"]

    def test_uv_returns_cams_fields(self, client_with_cache, monkeypatch):
        # Disable Open-Meteo merge so we don't make real outbound calls.
        monkeypatch.setenv("MERGE_OPENMETEO", "0")
        r = client_with_cache.get("/uv?latitude=10&longitude=0")
        assert r.status_code == 200
        body = r.json()
        assert body["hourly"]["ozone_du"][0] == 300.0
        assert body["hourly"]["aod"][0] == 0.10
        assert body["_camsMeta"]["source"] == "cams"

    def test_metrics_requires_bearer_when_set(self, client_with_cache, monkeypatch):
        """/metrics is bearer-gated — no exposure of internal pull
        state to unauthenticated callers."""
        monkeypatch.setenv("GETBASED_UVDATA_BEARER", "metrics-secret")
        r = client_with_cache.get("/metrics")
        assert r.status_code == 401

    def test_metrics_exposed_in_prometheus_format(self, client_with_cache, monkeypatch):
        """/metrics returns Prometheus exposition format with the
        expected counter + gauge series so a scrape can monitor health."""
        monkeypatch.setenv("MERGE_OPENMETEO", "0")
        # No bearer set in fixture env → /metrics is open here.
        client_with_cache.get("/uv?latitude=10&longitude=0")
        r = client_with_cache.get("/metrics")
        assert r.status_code == 200
        assert "text/plain" in r.headers.get("content-type", "")
        body = r.text
        assert "getbased_uvdata_uv_requests_total" in body
        assert "getbased_uvdata_uv_requests_2xx" in body
        assert "getbased_uvdata_snapshot_stale" in body
        assert "getbased_uvdata_info{version=" in body

    def test_iso_time_400_on_garbage(self, client_with_cache, monkeypatch):
        """Malformed time string returns 400, not silent fallback to now."""
        monkeypatch.setenv("MERGE_OPENMETEO", "0")
        r = client_with_cache.get("/uv?latitude=10&longitude=0&time=not-a-date")
        assert r.status_code == 400
        assert "invalid iso-8601" in r.text.lower()

    def test_no_cams_last_error_header_on_uv(self, client_with_cache, monkeypatch):
        """X-Cams-Last-Error header should NOT leak from /uv — even
        when an error is set on the cache, it stays internal."""
        monkeypatch.setenv("MERGE_OPENMETEO", "0")
        from getbased_uvdata.server import app as real_app

        # Inject a fake error on the cache.
        real_app.state.cams = type(
            "FakeCacheWithErr",
            (),
            {
                "snapshot": _fake_snapshot(),
                "is_stale": False,
                "last_error": "FakeError: secret-leak",
            },
        )()
        r = client_with_cache.get("/uv?latitude=10&longitude=0")
        assert r.status_code == 200
        assert "x-cams-last-error" not in {k.lower() for k in r.headers}

    def test_bearer_enforced_when_set(self, monkeypatch):
        from getbased_uvdata.server import app as real_app

        monkeypatch.setenv("GETBASED_UVDATA_BEARER", "secret-token-xyz")
        monkeypatch.setenv("MERGE_OPENMETEO", "0")
        monkeypatch.setenv("CAMS_CACHE_DIR", "")
        client = TestClient(real_app)
        with client:
            real_app.state.cams = type(
                "FakeCache",
                (),
                {
                    "snapshot": _fake_snapshot(),
                    "is_stale": False,
                    "last_error": None,
                },
            )()
            # No bearer → 401
            r = client.get("/uv?latitude=10&longitude=0")
            assert r.status_code == 401
            # Wrong bearer → 401
            r = client.get(
                "/uv?latitude=10&longitude=0",
                headers={"Authorization": "Bearer wrong"},
            )
            assert r.status_code == 401
            # Correct bearer → 200
            r = client.get(
                "/uv?latitude=10&longitude=0",
                headers={"Authorization": "Bearer secret-token-xyz"},
            )
            assert r.status_code == 200

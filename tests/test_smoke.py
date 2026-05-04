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
from getbased_uvdata.server import app


def _fake_snapshot() -> GridSnapshot:
    """One-hour, 2x2 grid with predictable values for assertion math."""
    now = time.time()
    times = np.array([now], dtype=float)
    lats = np.array([10.0, 0.0])  # descending, CAMS convention
    lons = np.array([0.0, 10.0])
    ozone = np.array([[[300.0, 310.0], [320.0, 330.0]]])  # (T, LAT, LON)
    aod = np.array([[[0.10, 0.15], [0.20, 0.25]]])
    return GridSnapshot(
        pulled_at=now,
        valid_from=now,
        valid_to=now + 3600,
        times=times,
        lats=lats,
        lons=lons,
        ozone_du=ozone,
        aod_550=aod,
    )


class TestGridLookup:
    def test_nearest_grid_point(self):
        snap = _fake_snapshot()
        # exact grid coords return the corresponding cell
        out = snap.lookup(10.0, 0.0, snap.times[0])
        assert out["ozoneDU"] == 300.0
        assert out["aod"] == 0.10
        out = snap.lookup(0.0, 10.0, snap.times[0])
        assert out["ozoneDU"] == 330.0
        assert out["aod"] == 0.25

    def test_clamps_outside_time_window(self):
        snap = _fake_snapshot()
        # Way before the snapshot — clamps to first timestep, not 404.
        out = snap.lookup(10.0, 0.0, snap.times[0] - 86400)
        assert out["ozoneDU"] == 300.0


class TestReshape:
    def test_cams_only_envelope_is_open_meteo_shaped(self):
        cams = {"ozoneDU": 305.0, "aod": 0.12}
        resp = build_response(
            lat=50.0, lon=14.0, when_iso=None,
            cams_lookup=cams, openmeteo=None,
            cams_pulled_at=1700000000, snapshot_valid_from=1700000000, snapshot_valid_to=1700086400,
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
            lat=50.0, lon=14.0, when_iso="2026-05-04T10:30",
            cams_lookup=cams, openmeteo=om,
            cams_pulled_at=1700000000, snapshot_valid_from=1700000000, snapshot_valid_to=1700086400,
        )
        # CAMS ozone broadcast across every hourly entry, parallel to time[].
        assert resp["hourly"]["ozone_du"] == [290.0, 290.0]
        assert resp["hourly"]["aod"] == [0.08, 0.08]
        # Open-Meteo fields preserved untouched.
        assert resp["hourly"]["uv_index"] == [3.0, 4.5]
        assert resp["airQuality"]["current"]["european_aqi"] == 30


class TestServer:
    @pytest.fixture
    def client_with_cache(self, monkeypatch):
        # Skip the real lifespan / CDS-API pull by injecting a fake cache
        # straight into app.state. TestClient still wires routes correctly.
        from getbased_uvdata.server import app as real_app
        client = TestClient(real_app)
        # TestClient's __enter__ runs lifespan; we replace the cache it
        # populated with our deterministic one.
        with client:
            real_app.state.cams = type("FakeCache", (), {
                "snapshot": _fake_snapshot(),
                "is_stale": False,
                "last_error": None,
            })()
            yield client

    def test_healthz_reports_grid_metadata(self, client_with_cache):
        r = client_with_cache.get("/healthz")
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["cams"]["last_error"] is None

    def test_uv_returns_cams_fields(self, client_with_cache, monkeypatch):
        # Disable Open-Meteo merge so we don't make real outbound calls.
        monkeypatch.setenv("MERGE_OPENMETEO", "0")
        r = client_with_cache.get("/uv?latitude=10&longitude=0")
        assert r.status_code == 200
        body = r.json()
        assert body["hourly"]["ozone_du"][0] == 300.0
        assert body["hourly"]["aod"][0] == 0.10
        assert body["_camsMeta"]["source"] == "cams"

    def test_bearer_enforced_when_set(self, monkeypatch):
        from getbased_uvdata.server import app as real_app
        monkeypatch.setenv("GETBASED_UVDATA_BEARER", "secret-token-xyz")
        monkeypatch.setenv("MERGE_OPENMETEO", "0")
        client = TestClient(real_app)
        with client:
            real_app.state.cams = type("FakeCache", (), {
                "snapshot": _fake_snapshot(),
                "is_stale": False,
                "last_error": None,
            })()
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

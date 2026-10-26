from pathlib import Path
import httpx
import pytest
from fastapi.testclient import TestClient
from app.main import app as fastapi_app, get_service
import app.main
from app.service import MeterService
from app.client import PortalClient

FIXTURES_DIR = Path(__file__).parent / "fixtures"

def mock_portal_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/login":
        return httpx.Response(200, text="OK")
    elif path == "/meters/J100000":
        html = (FIXTURES_DIR / "meter_detail_legacy.html").read_text(encoding="utf-8")
        return httpx.Response(200, text=html)
    elif path == "/meters/J100004":
        html = (FIXTURES_DIR / "meter_detail_v2.html").read_text(encoding="utf-8")
        return httpx.Response(200, text=html)
    elif "J100008" in path:
        return httpx.Response(502, text="Upstream Portal Unavailable")
    elif path.startswith("/meters/J999"):
        return httpx.Response(404, text="Not Found")
    return httpx.Response(404, text="Not Found")

transport = httpx.MockTransport(mock_portal_handler)
mock_portal_client = PortalClient(base_url="https://urja-ops.flockenergy.tech", transport=transport)
test_service = MeterService(client=mock_portal_client)
fastapi_app.dependency_overrides[get_service] = lambda: test_service
app.main.service = test_service

client = TestClient(fastapi_app)


def test_health_check():
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] in ("healthy", "degraded")
    assert "simulated" not in str(data).lower()
    assert "indexed_meters_count" in data
    assert "stats" in data
    assert data["stats"]["total_meters"] == 403
    assert data["stats"]["statuses"]["Installed"] == 238
    assert data["stats"]["statuses"]["Faulty"] == 90
    assert data["stats"]["statuses"]["Decommissioned"] == 75
    assert data["stats"]["phases"]["single"] == 277
    assert data["stats"]["phases"]["three"] == 126
    assert "X-Data-Source" in response.headers


def test_dashboard_stats():
    response = client.get("/api/v1/stats")
    assert response.status_code == 200
    stats = response.json()
    assert stats["total_meters"] == 403
    assert stats["statuses"]["Installed"] == 238
    assert stats["statuses"]["Faulty"] == 90
    assert stats["statuses"]["Decommissioned"] == 75
    assert stats["phases"]["single"] == 277
    assert stats["phases"]["three"] == 126
    assert stats["phases"]["single_pct"] == 68.7
    assert stats["phases"]["three_pct"] == 31.3


def test_list_meters_pagination():
    response = client.get("/api/v1/meters?page=1&page_size=10")
    assert response.status_code == 200
    data = response.json()
    assert "items" in data
    assert len(data["items"]) == 10
    assert data["page"] == 1
    assert data["page_size"] == 10
    assert data["total"] > 0
    assert "meter_id" in data["items"][0]
    assert "serial_no" in data["items"][0]


def test_list_meters_filtering():
    # Filter by specific meter_id
    response = client.get("/api/v1/meters?q=J100000")
    assert response.status_code == 200
    data = response.json()
    assert data["total"] >= 1
    assert data["items"][0]["meter_id"] == "J100000"

    # Filter by make
    response_hpl = client.get("/api/v1/meters?make=HPL")
    assert response_hpl.status_code == 200
    for item in response_hpl.json()["items"]:
        assert item["make"] == "HPL"


def test_get_meter_legacy_detail_and_caching():
    # First call: cache MISS
    resp1 = client.get("/api/v1/meters/J100000")
    assert resp1.status_code == 200
    assert resp1.headers.get("X-Cache") == "MISS"
    meter = resp1.json()
    assert meter["meter_id"] == "J100000"
    assert meter["serial_no"] == "SE33962"
    assert meter["make"] == "HPL"
    assert "hierarchy" in meter
    assert "zone" in meter["hierarchy"]
    assert "location" in meter
    assert meter["location"]["lat"] != 0.0

    # Second call: cache HIT
    resp2 = client.get("/api/v1/meters/J100000")
    assert resp2.status_code == 200
    assert resp2.headers.get("X-Cache") == "HIT"

    # Force refresh bypass: cache MISS
    resp3 = client.get("/api/v1/meters/J100000?refresh=true")
    assert resp3.status_code == 200
    assert resp3.headers.get("X-Cache") == "MISS"


def test_get_meter_v2_detail_normalization():
    # J100004 is a v2 build in the dataset
    resp = client.get("/api/v1/meters/J100004")
    assert resp.status_code == 200
    meter = resp.json()
    assert meter["meter_id"] == "J100004"
    assert meter["build"] == "v2"
    assert "location" in meter
    assert "hierarchy" in meter
    assert meter["phase_type"] in ("single", "three")


def test_invalid_meter_id_format():
    resp = client.get("/api/v1/meters/invalid_abc")
    assert resp.status_code == 400
    err = resp.json()
    assert "error" in err
    assert err["error"]["code"] == "BAD_REQUEST"


def test_meter_not_found():
    resp = client.get("/api/v1/meters/J99999999")
    assert resp.status_code == 404
    err = resp.json()
    assert "error" in err
    assert err["error"]["code"] == "NOT_FOUND"


def test_meter_consumption():
    resp = client.get("/api/v1/meters/J100000/consumption")
    assert resp.status_code == 200
    data = resp.json()
    assert data["meter_id"] == "J100000"
    assert "readings" in data
    assert len(data["readings"]) > 0

    # Readings must be sorted ascending by timestamp
    timestamps = [r["timestamp"] for r in data["readings"]]
    assert timestamps == sorted(timestamps)

    # Check reading fields
    r0 = data["readings"][0]
    assert "timestamp" in r0
    assert "kwh" in r0
    assert "kvah" in r0
    assert resp.headers.get("X-Data-Source") == "live"


def test_consumption_unreachable_returns_502():
    # J100008 has upstream 502 error; consumption must never fall back
    resp = client.get("/api/v1/meters/J100008/consumption")
    assert resp.status_code == 502
    err = resp.json()
    assert "error" in err
    assert err["error"]["code"] == "BAD_GATEWAY"


def test_meter_metadata_fallback_to_export_snapshot():
    # When live fetch fails on /meters/J100008, metadata falls back to snapshot
    resp = client.get("/api/v1/meters/J100008")
    assert resp.status_code == 200
    assert resp.headers.get("X-Data-Source") == "export_snapshot"
    meter = resp.json()
    assert meter["meter_id"] == "J100008"
    assert meter["source"] == "export_snapshot"


def test_hierarchy_tree_and_anomalies():
    resp = client.get("/api/v1/hierarchy")
    assert resp.status_code == 200
    data = resp.json()
    assert "hierarchy" in data
    assert "data_quality" in data
    dq = data["data_quality"]
    assert "reused_codes" in dq
    assert "conflicting_dt_names" in dq
    assert "unique_hierarchy_paths" in dq


def test_nearby_meters():
    # Center around Jaipur coordinates
    resp = client.get("/api/v1/meters/nearby?lat=26.9389&lng=75.8309&radius_km=10.0&limit=5")
    assert resp.status_code == 200
    nearby = resp.json()
    assert isinstance(nearby, list)
    assert len(nearby) > 0
    assert "distance_km" in nearby[0]
    assert nearby[0]["distance_km"] <= 10.0
    # Must be sorted by distance ascending
    distances = [m["distance_km"] for m in nearby]
    assert distances == sorted(distances)

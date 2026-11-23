"""Stage 45: safe presentation API must not touch non-disposable catalogues."""
from fastapi.testclient import TestClient

from cloud.demo_cooperation import visual_gateway as demo


def test_denies_browser_write_without_request_header(monkeypatch):
    monkeypatch.setattr(demo, "_attempted", False)
    response = TestClient(demo.app).post("/api/v1/demo/cooperation/run")
    assert response.status_code == 403


def test_denies_unexpected_origin(monkeypatch):
    monkeypatch.setattr(demo, "_attempted", False)
    response = TestClient(demo.app).post(
        "/api/v1/demo/cooperation/run",
        headers={"X-Requested-With": "XMLHttpRequest", "Origin": "http://outside.invalid"},
    )
    assert response.status_code == 403


def test_prevents_running_on_full_catalogues(monkeypatch):
    monkeypatch.setattr(demo, "_attempted", False)
    monkeypatch.setattr(demo, "_failure", None)
    monkeypatch.setattr(demo, "_result", None)
    monkeypatch.setattr(demo, "_capacities", lambda client: {
        "fog-a": {"fog_id": "fog-a", "total_piles": 6083},
        "fog-b": {"fog_id": "fog-b", "total_piles": 7403},
        "fog-c": {"fog_id": "fog-c", "total_piles": 8652},
    })
    response = TestClient(demo.app).post(
        "/api/v1/demo/cooperation/run",
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    assert response.status_code == 503
    assert demo._attempted is False  # No PREPARE was submitted.
    assert "non-disposable" in response.json()["detail"]


def test_one_shot_guard(monkeypatch):
    monkeypatch.setattr(demo, "_attempted", True)
    response = TestClient(demo.app).post(
        "/api/v1/demo/cooperation/run",
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    assert response.status_code == 409

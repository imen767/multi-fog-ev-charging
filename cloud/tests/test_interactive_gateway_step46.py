"""Guard rails and idempotency for the interactive Fog cooperation lab."""
from fastapi.testclient import TestClient
from cloud.demo_cooperation import interactive_gateway as demo

HEADERS = {"X-Requested-With": "XMLHttpRequest"}


def _clear_demo(monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "1")
    monkeypatch.setattr(demo, "_halted", None)
    monkeypatch.setattr(demo, "_last_round", None)
    monkeypatch.setattr(demo, "_round_history", [])
    monkeypatch.setattr(demo, "_events", [])
    monkeypatch.setattr(demo, "_operations", {})


def test_step_requires_browser_write_header(monkeypatch):
    _clear_demo(monkeypatch)
    resp = TestClient(demo.app).post("/api/v1/demo/cooperation/step", json={
        "expected_tick": 0, "operation_id": "step46-test-header-1"})
    assert resp.status_code == 403


def test_rejects_non_demo_mode(monkeypatch):
    _clear_demo(monkeypatch)
    monkeypatch.delenv("DEMO_MODE")
    resp = TestClient(demo.app).post("/api/v1/demo/cooperation/reset",
        json={"expected_tick": 0}, headers=HEADERS)
    assert resp.status_code == 403


def test_origin_rejection(monkeypatch):
    _clear_demo(monkeypatch)
    resp = TestClient(demo.app).post("/api/v1/demo/cooperation/step",
        json={"expected_tick": 0, "operation_id": "step46-test-origin-1"},
        headers={**HEADERS, "Origin": "https://untrusted.example"})
    assert resp.status_code == 403


def test_stale_step_rejected_before_running_fog_round(monkeypatch):
    _clear_demo(monkeypatch)
    capacities = {fog: {"fog_id": fog, "total_piles": n, "available_piles": n, "occupied_piles": 0}
                  for fog, n in demo.EXPECTED.items()}
    fog_rounds = {fog: {"fog_id": fog, "tick": 2, "phase": "SETTLED"} for fog in demo.FOG_IDS}
    monkeypatch.setattr(demo, "_snapshot", lambda _client: (capacities, fog_rounds, 2))
    resp = TestClient(demo.app).post("/api/v1/demo/cooperation/step",
        json={"expected_tick": 1, "operation_id": "step46-test-stale-1"}, headers=HEADERS)
    assert resp.status_code == 409
    assert demo._halted is None


def test_idempotent_operation_id_does_not_reexecute(monkeypatch):
    _clear_demo(monkeypatch)
    tick = {"value": 0, "count": 0}
    capacities = {fog: {"fog_id": fog, "total_piles": n, "available_piles": n, "occupied_piles": 0}
                  for fog, n in demo.EXPECTED.items()}
    monkeypatch.setattr(demo, "_snapshot", lambda _client: (
        capacities,
        {fog: {"fog_id": fog, "tick": tick["value"], "phase": "IDLE" if tick["value"] == 0 else "SETTLED"} for fog in demo.FOG_IDS},
        tick["value"],
    ))

    class FakeOrchestrator:
        def __init__(self, urls, client):
            self.current_tick = 0
        def run_round(self, arrivals, completions):
            tick["count"] += 1
            tick["value"] = self.current_tick + 1
            return {"tick": tick["value"], "locally_started": 1, "remote_started": 0,
                    "auto_completed": 0, "total_unserved": 0, "transfers": [], "cooperation_events": []}
    monkeypatch.setattr(demo, "FogCooperativeRoundOrchestrator", FakeOrchestrator)
    client = TestClient(demo.app)
    payload = {"expected_tick": 0, "operation_id": "step46-test-duplicate-1", "count": 1}
    assert client.post("/api/v1/demo/cooperation/step", json=payload, headers=HEADERS).status_code == 200
    assert client.post("/api/v1/demo/cooperation/step", json=payload, headers=HEADERS).status_code == 200
    assert tick["count"] == 1
    assert client.post("/api/v1/demo/cooperation/step",
                       json={**payload, "count": 2}, headers=HEADERS).status_code == 409

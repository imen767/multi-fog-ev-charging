"""Tests rapides : orchestration deja terminee -> capture, et erreur -> pas de tick suivant."""
import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("cloud_orchestrator_under_test", ROOT / "cloud" / "orchestrator_api" / "main.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
client = TestClient(module.app)
HEADERS = {"X-Orchestrator-Token": "test-token"}


def test_round_captured_only_once():
    payload = {"simulation_run_id": "11111111-1111-4111-8111-111111111111", "tick": 1, "snapshots": [{"fog_id": f"fog-{f}", "total_piles": 10, "occupied_piles": 0, "reservable_piles": 10} for f in "abc"]}
    with patch.object(module, "ORCHESTRATOR_API_TOKEN", "test-token"), patch.object(module, "CLOUD_HISTORY_URL", "http://mock/history"), patch.object(module, "_bootstrap_tick"), patch.object(module.orchestrator, "run_round", return_value={"tick": 1}) as run, patch.object(module, "_collect_snapshot", return_value=payload), patch.object(module, "_send_snapshot", return_value={"status": "captured", "tick": 1, "recorded_fogs": 3}) as send:
        response = client.post("/api/v1/orchestrator/step", json={}, headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["history_sync"]["status"] == "captured"
    run.assert_called_once()
    send.assert_called_once_with(payload)
    assert module._pending_snapshot is None


def test_capture_failure_blocks_next_tick_and_retry_saves_without_new_round():
    payload = {"simulation_run_id": "11111111-1111-4111-8111-111111111111", "tick": 2, "snapshots": []}
    with patch.object(module, "ORCHESTRATOR_API_TOKEN", "test-token"), patch.object(module, "CLOUD_HISTORY_URL", "http://mock/history"), patch.object(module, "_bootstrap_tick"), patch.object(module.orchestrator, "run_round", return_value={"tick": 2}) as run, patch.object(module, "_collect_snapshot", return_value=payload), patch.object(module, "_send_snapshot", side_effect=[ValueError("cloud offline"), {"status": "captured", "tick": 2}]) as send:
        first = client.post("/api/v1/orchestrator/step", json={}, headers=HEADERS)
        blocked = client.post("/api/v1/orchestrator/step", json={}, headers=HEADERS)
        retried = client.post("/api/v1/orchestrator/retry-history", headers=HEADERS)
    assert first.json()["history_sync"]["status"] == "pending"
    assert blocked.status_code == 409
    assert retried.json()["status"] == "captured"
    run.assert_called_once()  # SETTLE non rejoue
    assert send.call_count == 2
    assert module._pending_snapshot is None


def test_requests_require_auth():
    assert client.post("/api/v1/orchestrator/step", json={}).status_code == 401
    assert client.post("/api/v1/orchestrator/retry-history").status_code == 401

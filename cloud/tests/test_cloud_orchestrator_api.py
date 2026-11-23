import importlib.util
import sys
from pathlib import Path
from fastapi.testclient import TestClient
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("cloud_orchestrator_api", ROOT / "cloud" / "orchestrator_api" / "main.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
client = TestClient(module.app)


def test_health():
    assert client.get("/health").json()["status"] == "healthy"


def test_step_requires_token():
    assert client.post("/api/v1/orchestrator/step", json={}).status_code == 401


def test_step_calls_existing_orchestrator():
    with patch.object(module, "ORCHESTRATOR_API_TOKEN", "test-token"), patch.object(
        module.orchestrator, "run_round", return_value={"tick": 1, "transfers": []}
    ) as call, patch.object(module, "_bootstrap_tick"):
        response = client.post(
            "/api/v1/orchestrator/step",
            headers={"X-Orchestrator-Token": "test-token"},
            json={"arrivals": {"fog-b": [{"vehicle_id": "ev-001", "required_power_kw": 22.0}]}},
        )
        assert response.status_code == 200, response.text
        assert response.json()["tick"] == 1
        assert call.call_args.kwargs["arrivals"]["fog-b"][0]["vehicle_id"] == "ev-001"


def test_step_rejects_unknown_fog():
    with patch.object(module, "ORCHESTRATOR_API_TOKEN", "test-token"):
        response = client.post(
            "/api/v1/orchestrator/step",
            headers={"X-Orchestrator-Token": "test-token"},
            json={"arrivals": {"fog-x": [{"vehicle_id": "ev-001"}]}},
        )
        assert response.status_code == 422

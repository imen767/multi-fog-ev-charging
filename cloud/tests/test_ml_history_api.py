"""Tests ETAPE24 : source des donnees, continuité, auth et absence d'effets de bord."""
import importlib.util
import io
import json
import sys
import types
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

try:
    import psycopg
except ImportError:
    psycopg = types.ModuleType("psycopg")
    class FakeError(Exception):
        pass
    psycopg.Error = FakeError
    psycopg.connect = lambda *a, **k: None
    sys.modules["psycopg"] = psycopg

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("cloud_api_ml_step24", ROOT / "cloud" / "cloud_api" / "main.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
client = TestClient(module.app)
AUTH = {"X-ML-Token": "test-ml-token"}
RUN_ID = "11111111-1111-4111-8111-111111111111"


class FakeDB:
    def __init__(self, data):
        self.data = data
        self.query = None
        self.parameters = None
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def execute(self, query, parameters):
        self.query = query
        self.parameters = parameters
        return self
    def fetchall(self):
        return self.data


def test_history_selects_one_fog_in_chronological_order():
    db = FakeDB([(n, 100 + n) for n in reversed(range(1, 13))])
    with patch.object(module, "DATABASE_URL", "postgresql://test"), patch.object(module, "SIMULATION_RUN_ID", RUN_ID), patch.object(module, "ML_API_TOKEN", "test-ml-token"), patch.object(module.psycopg, "connect", return_value=db):
        resp = client.get("/api/v1/ml/history/fog-a", headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["fog_id"] == "fog-a"
    assert body["ready"] is True
    assert body["history"] == [101.0 + i for i in range(12)]
    assert body["ticks"] == list(range(1, 13))
    assert db.parameters == ("fog-a", RUN_ID)
    assert "simulation_run_id" in db.query
    assert body["simulation_run_id"] == RUN_ID
    assert "orchestrator_settled" in db.query
    assert "ORDER BY simulation_tick DESC" in db.query
    assert "LIMIT 12" in db.query


def test_missing_and_discontinuous_history_fail_safely():
    with patch.object(module, "ML_API_TOKEN", "test-ml-token"), patch.object(module, "SIM_TICK_MINUTES", "60"):
        with patch.object(module, "_read_fog_history", return_value={"ready": False, "reason": "insufficient_history", "history_points": 3, "ticks": [1, 2, 3]}):
            r1 = client.post("/api/v1/ml/forecast/fog-a", headers=AUTH)
        with patch.object(module, "_read_fog_history", return_value={"ready": False, "reason": "non_consecutive_ticks", "history_points": 12, "ticks": [1, 2, 4] + list(range(5,14))}):
            r2 = client.post("/api/v1/ml/forecast/fog-b", headers=AUTH)
    assert r1.status_code == r2.status_code == 409
    assert r1.json()["detail"]["reason"] == "insufficient_history"
    assert r2.json()["detail"]["reason"] == "non_consecutive_ticks"


def test_forecast_requires_auth_and_hourly_tick():
    assert client.post("/api/v1/ml/forecast/fog-a").status_code == 401
    series = {"ready": True, "history": [float(x) for x in range(12)], "source": "history", "metric": "occupied_piles", "ticks": list(range(1,13))}
    with patch.object(module, "ML_API_TOKEN", "test-ml-token"), patch.object(module, "SIM_TICK_MINUTES", "5"), patch.object(module, "_read_fog_history", return_value=series):
        r = client.post("/api/v1/ml/forecast/fog-a", headers=AUTH)
    assert r.status_code == 409
    assert "60" in r.json()["detail"]


def test_forecast_sends_only_selected_fog_history_to_chronos():
    series = {"ready": True, "history": [float(x) for x in range(12)], "source": "history", "metric": "occupied_piles", "ticks": list(range(20,32))}
    model_response = {"model": "amazon/chronos-2", "horizon_steps": 1, "step_minutes": 60, "forecasts": {"q10": [2.], "q50": [3.], "q90": [4.]}}
    requests = []
    class Response:
        def __enter__(self):
            return io.BytesIO(json.dumps(model_response).encode())
        def __exit__(self, *args):
            return False
    def fake_urlopen(request, timeout):
        requests.append((request, timeout))
        return Response()
    with patch.object(module, "ML_API_TOKEN", "test-ml-token"), patch.object(module, "SIM_TICK_MINUTES", "60"), patch.object(module, "_read_fog_history", return_value=series), patch.object(module, "_urlopen", side_effect=fake_urlopen):
        resp = client.post("/api/v1/ml/forecast/fog-a", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["role"] == "recommendation_only_no_reservation"
    assert resp.json()["input_ticks"] == list(range(20,32))
    sent = json.loads(requests[0][0].data.decode())
    assert sent == {"history": series["history"], "prediction_length": 1}
    assert requests[0][0].full_url.endswith("/api/v1/forecast")

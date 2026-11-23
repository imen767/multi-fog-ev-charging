"""Tests de l'API d'historique en simulant PostgreSQL (pas besoin de Docker)."""
import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient

# Simuler uniquement le pilote DB si non installe dans le runner de tests.
try:
    import psycopg
except ImportError:
    psycopg = types.ModuleType("psycopg")
    class Error(Exception):
        pass
    psycopg.Error = Error
    psycopg.connect = lambda *a, **k: None
    sys.modules["psycopg"] = psycopg

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("cloud_sync_api_under_test", ROOT / "cloud" / "cloud_api" / "main.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
client = TestClient(module.app)
HEADERS = {"X-Orchestrator-Token": "token"}
RUN_ID = "11111111-1111-4111-8111-111111111111"
OTHER_RUN = "22222222-2222-4222-8222-222222222222"


class FakeCursor:
    def __init__(self, store):
        self.store = store
        self.result = []
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def execute(self, sql, params=None):
        if sql.startswith("SELECT fog_id, COUNT"):
            self.result = [("fog-a", 10), ("fog-b", 10), ("fog-c", 10)]
        elif "FROM occupancy_history" in sql:
            run_id, tick = params
            self.result = [(fog, *values) for (rid, fog, t), values in self.store.items()
                           if rid == run_id and t == tick]
        else:
            raise AssertionError(sql)
    def executemany(self, sql, rows):
        assert "ON CONFLICT DO NOTHING" in sql
        for run_id, fog, tick, total, occupied, reservable in rows:
            self.store.setdefault((run_id,fog,tick), (total,occupied,reservable,"orchestrator_settled"))
    def fetchall(self):
        return self.result


class FakeConnection:
    def __init__(self, store):
        self.store = store
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def cursor(self):
        return FakeCursor(self.store)


def data(occupied=2):
    return {"simulation_run_id": RUN_ID, "tick": 4, "snapshots": [
        {"fog_id": f"fog-{f}", "total_piles": 10, "occupied_piles": occupied, "reservable_piles": 6}
        for f in "abc"]}


def test_capture_idempotent_and_rejects_changed_replay():
    store = {}
    with patch.object(module, "DATABASE_URL", "postgresql://example"), patch.object(module, "SIMULATION_RUN_ID", RUN_ID), patch.dict(module.os.environ, {"ORCHESTRATOR_API_TOKEN": "token"}), patch.object(module.psycopg, "connect", return_value=FakeConnection(store)):
        one = client.post("/api/v1/history/round", json=data(), headers=HEADERS)
        two = client.post("/api/v1/history/round", json=data(), headers=HEADERS)
        changed = client.post("/api/v1/history/round", json=data(occupied=3), headers=HEADERS)
        invalid = client.post("/api/v1/history/round", json={"tick": 4, "snapshots": data()["snapshots"][:2]}, headers=HEADERS)
    assert one.status_code == 200 and two.status_code == 200
    assert len(store) == 3
    assert changed.status_code == 409
    assert invalid.status_code == 422
    assert one.json()["simulation_run_id"] == RUN_ID


def test_capture_requires_orchestrator_token():
    assert client.post("/api/v1/history/round", json=data()).status_code == 401


def test_other_simulation_can_reuse_tick_one_without_overwriting_legacy():
    store = {('legacy', 'fog-b', 4): (10, 0, 10, 'orchestrator_settled')}
    with patch.object(module, 'DATABASE_URL', 'postgresql://example'), \
         patch.object(module, 'SIMULATION_RUN_ID', RUN_ID), \
         patch.dict(module.os.environ, {'ORCHESTRATOR_API_TOKEN': 'token'}), \
         patch.object(module.psycopg, 'connect', return_value=FakeConnection(store)):
        new = client.post('/api/v1/history/round', json=data(occupied=3), headers=HEADERS)
    assert new.status_code == 200
    assert store[('legacy', 'fog-b', 4)][1] == 0
    assert store[(RUN_ID, 'fog-b', 4)][1] == 3


def test_run_id_mismatch_is_rejected():
    with patch.object(module, 'SIMULATION_RUN_ID', RUN_ID), \
         patch.dict(module.os.environ, {'ORCHESTRATOR_API_TOKEN': 'token'}):
        bad = data()
        bad['simulation_run_id'] = OTHER_RUN
        r = client.post('/api/v1/history/round', json=bad, headers=HEADERS)
    assert r.status_code == 409

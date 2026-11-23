"""Local-only gateway: real vehicle injection, guardrails and API contract."""
import importlib.util
import json
from pathlib import Path

from fastapi.testclient import TestClient

path = Path(__file__).resolve().parents[1] / 'dashboard_gateway' / 'main.py'
spec = importlib.util.spec_from_file_location('gateway_step32', path)
gateway = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gateway)


def _status():
    return {
        'history_pending_tick': None, 'tick': 6, 'capacities': {},
        'is_running': False, 'last_round': None,
    }


def test_real_injection_creates_requested_vehicles_only(monkeypatch):
    monkeypatch.setattr(gateway, 'ORCHESTRATOR_API_TOKEN', 'unit-test-secret')
    monkeypatch.setattr(gateway, '_status', _status)
    requests = []

    class FakeResponse:
        status_code = 200
        def json(self): return {'tick': 7, 'history_sync': {'status': 'captured'}}

    class FakeClient:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *exc): pass
        def post(self, url, headers, json):
            requests.append((url, headers, json))
            return FakeResponse()

    monkeypatch.setattr(gateway.httpx, 'Client', FakeClient)
    client = TestClient(gateway.app)
    res = client.post('/api/v1/simulator/inject',
        json={'fog_id': 'fog-b', 'count': 10},
        headers={'Origin': 'http://localhost:5173', 'X-Requested-With': 'XMLHttpRequest'})
    assert res.status_code == 200
    assert len(requests) == 1
    url, headers, body = requests[0]
    assert url.endswith('/api/v1/orchestrator/step')
    assert headers['X-Orchestrator-Token'] == 'unit-test-secret'
    assert body['completions'] == {}
    assert set(body['arrivals']) == {'fog-b'}
    vehicles = body['arrivals']['fog-b']
    assert len(vehicles) == 10
    assert len({v['vehicle_id'] for v in vehicles}) == 10
    assert all(v['required_power_kw'] == 22.0 for v in vehicles)
    assert 'unit-test-secret' not in res.text


def test_invalid_injections_are_rejected(monkeypatch):
    monkeypatch.setattr(gateway, 'ORCHESTRATOR_API_TOKEN', 'unit-test-secret')
    client = TestClient(gateway.app)
    headers = {'X-Requested-With': 'XMLHttpRequest'}
    for body in ({'fog_id': 'fog-b', 'count': 0}, {'fog_id': 'fog-b', 'count': 51},
                 {'fog_id': 'fog-d', 'count': 5}):
        assert client.post('/api/v1/simulator/inject', json=body, headers=headers).status_code == 422
    assert client.post('/api/v1/simulator/inject',
        json={'fog_id': 'fog-b', 'count': 10}, headers={'Origin':'http://evil.example',**headers}).status_code == 403


def test_pending_history_blocks_injection(monkeypatch):
    monkeypatch.setattr(gateway, 'ORCHESTRATOR_API_TOKEN', 'unit-test-secret')
    monkeypatch.setattr(gateway, '_status', lambda: {'history_pending_tick': 6})
    client = TestClient(gateway.app)
    response = client.post('/api/v1/simulator/inject', json={'fog_id':'fog-b','count': 10},
                           headers={'X-Requested-With':'XMLHttpRequest'})
    assert response.status_code == 409

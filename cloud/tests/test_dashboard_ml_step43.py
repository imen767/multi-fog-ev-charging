"""Etape 43 : ML du run courant, attente 12 ticks, secrets et cache idempotent."""
from fastapi.testclient import TestClient
from cloud.dashboard_gateway import ml_routes

class Response:
    def __init__(self, data, status=200):
        self.data = data
        self.status_code = status
    def json(self): return self.data
    def raise_for_status(self):
        if self.status_code >= 400:
            raise ValueError(f'HTTP {self.status_code}')


def _mock(monkeypatch, ready=True, pending=False, run_id='run-1'):
    calls = []
    class Client:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def get(self, url, headers=None):
            calls.append(('GET', url, headers))
            if url.endswith('/api/v1/orchestrator/status'):
                return Response({'simulation_run_id':run_id, 'current_tick':12,
                                 'history_pending_tick':12 if pending else None})
            return Response({'fog_id':'fog-b', 'simulation_run_id':run_id,
                             'ready':ready, 'reason':'ok' if ready else 'insufficient_history',
                             'history_points':12 if ready else 11,
                             'ticks':list(range(1,13)) if ready else list(range(1,12)),
                             'history':[1.0]* (12 if ready else 11),
                             'source':'occupancy_history/orchestrator_settled'})
        def post(self, url, params, headers):
            calls.append(('POST', url, headers))
            return Response({'simulation_run_id':run_id, 'input_ticks':list(range(1,13)),
                             'step_minutes':60, 'horizon_steps':1,
                             'forecasts': {'q10':[2.0], 'q50':[3.0], 'q90':[4.0]}})
    monkeypatch.setenv('CLOUD_ML_URL','http://cloud-sync-api:8000')
    monkeypatch.setenv('CLOUD_ML_API_TOKEN','SECRET_NOT_FOR_BROWSER')
    monkeypatch.setenv('ORCHESTRATOR_URL','http://cloud-orchestrator:8000')
    monkeypatch.setattr(ml_routes.httpx, 'Client', Client)
    ml_routes._cache.clear()
    return calls


def test_insufficient_history_returns_no_forecast_and_no_inference(monkeypatch):
    calls=_mock(monkeypatch, ready=False)
    data=ml_routes.forecast_for_dashboard('fog-b')
    assert data['ready'] is False and data['history_points']==11
    assert not any(c[0]=='POST' for c in calls)
    assert 'forecasts' not in data
    assert 'SECRET_NOT_FOR_BROWSER' not in str(data)


def test_ready_calls_chronos_once_per_run_tick(monkeypatch):
    calls=_mock(monkeypatch)
    one=ml_routes.forecast_for_dashboard('fog-b')
    two=ml_routes.forecast_for_dashboard('fog-b')
    assert one == two and one['forecasts']['q50']==[3.0]
    assert sum(c[0]=='POST' for c in calls)==1
    assert all(c[2] == {'X-ML-Token':'SECRET_NOT_FOR_BROWSER'}
               for c in calls if '/ml/history' in c[1] or c[0]=='POST')
    assert 'SECRET_NOT_FOR_BROWSER' not in str(one)


def test_pending_history_never_triggers_inference(monkeypatch):
    calls=_mock(monkeypatch, pending=True)
    data=ml_routes.forecast_for_dashboard('fog-b')
    assert data['ready'] is False and data['reason']=='history_pending'
    assert not any(c[0]=='POST' for c in calls)


def test_no_secret_fails_closed(monkeypatch):
    monkeypatch.delenv('CLOUD_ML_API_TOKEN', raising=False)
    monkeypatch.setenv('CLOUD_ML_URL','http://cloud')
    try:
        ml_routes.forecast_for_dashboard('fog-b')
        assert False, 'Un jeton ML devrait être obligatoire'
    except Exception as exc:
        assert getattr(exc,'status_code',None)==503

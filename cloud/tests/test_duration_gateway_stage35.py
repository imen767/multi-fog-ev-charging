"""Dashboard injects independent durations, no browser token disclosure."""
import importlib.util
from pathlib import Path
from fastapi.testclient import TestClient

path=Path(__file__).resolve().parents[1] / 'dashboard_gateway' / 'main.py'
spec=importlib.util.spec_from_file_location('gateway_durations',path)
gateway=importlib.util.module_from_spec(spec)
spec.loader.exec_module(gateway)


def test_injection_assigns_distinct_durations(monkeypatch):
    monkeypatch.setattr(gateway,'ORCHESTRATOR_API_TOKEN','test-secret')
    monkeypatch.setattr(gateway,'_status',lambda: {'history_pending_tick':None,'tick':3})
    calls=[]
    class FakeResp:
        status_code=200
        def json(self):return {'tick':4,'history_sync':{'status':'captured'}}
    class FakeClient:
        def __init__(self,**kw):pass
        def __enter__(self):return self
        def __exit__(self,*exc):pass
        def post(self,url,headers,json):calls.append(json);return FakeResp()
    monkeypatch.setattr(gateway.httpx,'Client',FakeClient)
    r=TestClient(gateway.app).post('/api/v1/simulator/inject',
        json={'fog_id':'fog-b','count':5},headers={'X-Requested-With':'XMLHttpRequest'})
    assert r.status_code==200
    vehicles=calls[0]['arrivals']['fog-b']
    assert [v['duration_ticks'] for v in vehicles]==[1,2,3,4,1]
    assert all(v['vehicle_id'] for v in vehicles)


def test_override_fixed_duration(monkeypatch):
    monkeypatch.setattr(gateway,'ORCHESTRATOR_API_TOKEN','test-secret')
    monkeypatch.setattr(gateway,'_status',lambda: {'history_pending_tick':None,'tick':3})
    saved=[]
    class Resp:
        status_code=200
        def json(self):return {'history_sync':{'status':'captured'}}
    class Client:
        def __init__(self,**kw):pass
        def __enter__(self):return self
        def __exit__(self,*exc):pass
        def post(self,url,headers,json):saved.append(json);return Resp()
    monkeypatch.setattr(gateway.httpx,'Client',Client)
    client=TestClient(gateway.app)
    r=client.post('/api/v1/simulator/inject',json={'fog_id':'fog-b','count':2,'duration_ticks':4},
      headers={'X-Requested-With':'XMLHttpRequest'})
    assert r.status_code==200
    assert [v['duration_ticks'] for v in saved[0]['arrivals']['fog-b']]==[4,4]
    bad=client.post('/api/v1/simulator/inject',json={'fog_id':'fog-b','count':2,'duration_ticks':0},
      headers={'X-Requested-With':'XMLHttpRequest'})
    assert bad.status_code==422

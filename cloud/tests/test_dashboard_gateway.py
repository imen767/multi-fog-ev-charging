import importlib.util
from pathlib import Path

from fastapi.testclient import TestClient

path = Path(__file__).resolve().parents[1] / 'dashboard_gateway' / 'main.py'
spec = importlib.util.spec_from_file_location('dashboard_gateway', path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def fake_status():
    cap = {fog: {'fog_id':fog,'total_piles':10,'available_piles':9,'occupied_piles':1,
                 'out_of_service_piles':0, 'available_power_kw':500} for fog in module.FOG_IDS}
    return {
        'is_running': False, 'tick': 3, 'interval_sec': 0, 'scenario':'S1_NORMAL',
        'history_pending_tick':None,'capacities':cap,
        'total_arrivals':0,'active_sessions_count':3,'total_completed':0,
        'total_cooperations':0,'total_unserved':0,'recent_events':[],'stats_available':False,
    }


def test_no_auth_headers_returns_403(monkeypatch):
    monkeypatch.setattr(module,'_status',fake_status)
    monkeypatch.setattr(module,'ORCHESTRATOR_API_TOKEN','test-only')
    client=TestClient(module.app)
    assert client.post('/api/v1/simulator/step').status_code==403
    assert client.post('/api/v1/simulator/step',headers={'X-Requested-With':'XMLHttpRequest','Origin':'http://evil.test'}).status_code==403


def test_status_reports_real_backend(monkeypatch):
    monkeypatch.setattr(module,'_status',fake_status)
    client=TestClient(module.app)
    r=client.get('/api/v1/simulator/status')
    assert r.status_code==200
    assert r.json()['active_sessions_count']==3


def test_pending_prevents_round(monkeypatch):
    data=fake_status()
    data['history_pending_tick']=3
    monkeypatch.setattr(module,'_status',lambda:data)
    monkeypatch.setattr(module,'ORCHESTRATOR_API_TOKEN','test-only')
    client=TestClient(module.app)
    r=client.post('/api/v1/simulator/step',headers={'X-Requested-With':'XMLHttpRequest'})
    assert r.status_code==409


def test_real_status_from_api_responses(monkeypatch):
    class Client:
        def __init__(self,**_):pass
        def __enter__(self):return self
        def __exit__(self,*_):pass
    def mock_json(_client,url):
        if 'orchestrator/status' in url:return {'current_tick':3,'history_pending_tick':None}
        if 'orchestrator/history' in url:return {'rounds':[{'tick':3,'total_unserved':0,'transfers':[]}]}
        fog=next(f for f in module.FOG_IDS if f in url)
        if url.endswith('round/status'):return {'fog_id':fog,'tick':3,'phase':'SETTLED'}
        return {'fog_id':fog,'total_piles':10,'available_piles':9,'occupied_piles':1,'out_of_service_piles':0,'available_power_kw':500}
    monkeypatch.setattr(module.httpx,'Client',Client)
    monkeypatch.setattr(module,'_json',mock_json)
    result=module._status()
    assert result['tick']==3 and result['active_sessions_count']==3
    assert result['recent_events'][0]['category']=='ROUND'


def test_step_forwards_one_empty_round_without_token_leak(monkeypatch):
    monkeypatch.setattr(module,'ORCHESTRATOR_API_TOKEN','test-only')
    monkeypatch.setattr(module,'_status',fake_status)
    requests=[]
    class Response:
        status_code=200
        def json(self):return {'tick':4,'history_sync':{'status':'captured'}}
    class Client:
        def __init__(self,**_): pass
        def __enter__(self):return self
        def __exit__(self,*_):pass
        def post(self,url,headers,json):
            requests.append((url,headers,json))
            return Response()
    monkeypatch.setattr(module.httpx,'Client',Client)
    client=TestClient(module.app)
    r=client.post('/api/v1/simulator/step',headers={'X-Requested-With':'XMLHttpRequest','Origin':'http://localhost:5173'})
    assert r.status_code==200
    assert len(requests)==1
    assert requests[0][1]['X-Orchestrator-Token']=='test-only'
    assert requests[0][2]=={'arrivals':{},'completions':{}}
    assert 'test-only' not in r.text

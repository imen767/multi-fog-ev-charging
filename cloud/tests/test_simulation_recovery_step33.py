"""Recovery after Cloud reboot: no new Fog PREPARE/SETTLE to repair a missing snapshot."""
import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location('orchestrator_run_recovery', ROOT/'cloud/orchestrator_api/main.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
RUN_ID = '11111111-1111-4111-8111-111111111111'
AUTH = {'X-Orchestrator-Token': 'testing'}


def fake_states():
    return {f'fog-{fog}': {'fog_id':f'fog-{fog}', 'tick':1, 'phase':'SETTLED'} for fog in 'abc'}


def test_restart_detects_pending_and_retry_records_without_replaying_round():
    client=TestClient(module.app)
    module._bootstrapped=False
    module._pending_snapshot=None
    module.orchestrator.current_tick=0
    payload={'simulation_run_id':RUN_ID, 'tick':1, 'snapshots':[]}
    probe={'simulation_run_id':RUN_ID, 'latest_tick':0, 'tick_status':'missing', 'tick':1, 'recorded_fogs': []}
    with patch.object(module,'ORCHESTRATOR_API_TOKEN','testing'), \
         patch.object(module,'CLOUD_HISTORY_URL','http://cloud/api/v1/history/round'), \
         patch.object(module,'SIMULATION_RUN_ID',RUN_ID), \
         patch.object(module,'_fog_round_statuses', return_value=fake_states()), \
         patch.object(module,'_history_probe', return_value=probe), \
         patch.object(module,'_collect_snapshot', return_value=payload), \
         patch.object(module,'_send_snapshot', return_value={'status':'captured','tick':1,'simulation_run_id':RUN_ID,'recorded_fogs':3}), \
         patch.object(module.orchestrator,'run_round') as run_round:
        status=client.get('/api/v1/orchestrator/status')
        assert status.status_code==200
        assert status.json()['history_pending_tick']==1
        blocked=client.post('/api/v1/orchestrator/step',headers=AUTH,json={})
        assert blocked.status_code==409
        captured=client.post('/api/v1/orchestrator/retry-history',headers=AUTH)
        assert captured.status_code==200
        assert captured.json()['status']=='captured'
        assert module._pending_snapshot is None
    run_round.assert_not_called()


def test_new_run_cannot_advance_if_old_fog_tick_is_twelve():
    module._bootstrapped=False
    module._pending_snapshot=None
    states={f'fog-{f}':{'fog_id':f'fog-{f}','tick':12,'phase':'SETTLED'} for f in 'abc'}
    probe={'simulation_run_id':RUN_ID,'latest_tick':0,'tick_status':'missing'}
    with patch.object(module,'SIMULATION_RUN_ID',RUN_ID), \
         patch.object(module,'CLOUD_HISTORY_URL','http://cloud/api/v1/history/round'), \
         patch.object(module,'_fog_round_statuses',return_value=states), \
         patch.object(module,'_history_probe',return_value=probe):
        try:
            module._bootstrap_tick()
            assert False,'must reject 11 missing ticks'
        except ValueError as exc:
            assert 'Missing historical ticks' in str(exc)


def test_collected_snapshot_is_tagged_with_run_id():
    class Response:
        def __init__(self, j): self.j=j
        def raise_for_status(self): return None
        def json(self): return self.j
    class FakeHTTP:
        def __init__(self, timeout): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def get(self,url):
            fog=url.split('/')[2].split(':')[0]
            if url.endswith('round/status'):
                return Response({'fog_id':fog,'tick':1,'phase':'SETTLED'})
            return Response({'fog_id':fog,'total_piles':10,'occupied_piles':3,'available_piles':7})
    with patch.object(module,'SIMULATION_RUN_ID',RUN_ID), patch.object(module.httpx,'Client',FakeHTTP):
        snap=module._collect_snapshot(1)
    assert snap['simulation_run_id']==RUN_ID
    assert len(snap['snapshots'])==3

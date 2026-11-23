"""Etape 37: journal réentrant, conservation des anciens ticks, statistiques HTTP.

Ces tests utilisent un faux client PostgreSQL : un test d'integration sur PostgreSQL réel
reste nécessaire sur le PC de l'utilisateur.
"""
import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

try:
    import psycopg
except ImportError:
    psycopg = types.ModuleType("psycopg")
    psycopg.Error = type("Error", (Exception,), {})
    sys.modules["psycopg"] = psycopg

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location('cloud_stats_stage37', ROOT/'cloud/cloud_api/main.py')
api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(api)
client = TestClient(api.app)
RUN = '11111111-1111-4111-8111-111111111111'
AUTH = {'X-Orchestrator-Token':'token'}


class FakePG:
    def __init__(self):
        self.occupancy = {}  # (run, tick, fog) -> (total, occupied, reservable, source)
        self.rounds = {}  # (run, tick) -> metrics tuple

    def __enter__(self): return self
    def __exit__(self, *_): return False
    def cursor(self): return FakeCursor(self)
    def execute(self, sql, params=None):
        cursor = self.cursor()
        cursor.execute(sql, params)
        return cursor


class FakeCursor:
    def __init__(self, db):
        self.db=db
        self.rows=[]
    def __enter__(self): return self
    def __exit__(self,*_): return False
    def execute(self, sql, params=None):
        if 'SELECT fog_id, COUNT(*)' in sql:
            self.rows = [(f'fog-{x}', 10) for x in 'abc']
        elif 'SELECT fog_id, total_piles, occupied_piles' in sql:
            run,tick=params
            self.rows=[(fog,*row) for (rid,t,fog),row in self.db.occupancy.items() if rid==run and t==tick]
        elif 'INSERT INTO round_statistics' in sql:
            run,tick,*metrics=params
            self.db.rounds.setdefault((run,tick),tuple(metrics))
            self.rows=[]
        elif 'SELECT arrivals_requested, locally_started' in sql and 'FROM round_statistics' in sql:
            run,tick=params
            self.rows=[self.db.rounds[(run,tick)]] if (run,tick) in self.db.rounds else []
        elif 'SELECT simulation_tick, arrivals_requested' in sql and 'FROM round_statistics' in sql:
            (run,) = params
            self.rows=[(tick,*data) for (rid,tick),data in sorted(self.db.rounds.items()) if rid==run]
        elif 'SELECT simulation_tick, fog_id, occupied_piles' in sql and 'FROM occupancy_history' in sql:
            run,limit=params
            self.rows=[(tick,fog,vals[1]) for (rid,tick,fog),vals in sorted(self.db.occupancy.items(),key=lambda x:(-x[0][1],x[0][2])) if rid==run][:limit]
        elif 'SELECT COALESCE(MAX(simulation_tick),0)' in sql:
            (run,)=params
            self.rows=[(max((tick for rid,tick,fog in self.db.occupancy if rid==run),default=0),)]
        else: raise AssertionError(sql)
        return self
    def executemany(self,sql,data):
        assert 'ON CONFLICT DO NOTHING' in sql
        for run,fog,tick,total,occupied,reservable in data:
            self.db.occupancy.setdefault((run,tick,fog),(total,occupied,reservable,'orchestrator_settled'))
    def fetchall(self):return self.rows
    def fetchone(self):return self.rows[0] if self.rows else None


def payload(tick=9, arrivals=2):
    return {'simulation_run_id':RUN,'tick':tick,'snapshots':[
        {'fog_id':f'fog-{x}','total_piles':10,'occupied_piles':2,'reservable_piles':8} for x in 'abc'],
        'metrics':{'arrivals_requested':arrivals,'locally_started':arrivals,'remote_started':0,'auto_completed':0,'explicit_completed':0,'unserved':0}}


def patched(db):
    return patch.multiple(api, DATABASE_URL='postgresql://example', SIMULATION_RUN_ID=RUN), \
        patch.dict(api.os.environ, {'ORCHESTRATOR_API_TOKEN':'token'}), \
        patch.object(api.psycopg, 'connect', return_value=db, create=True)


def test_same_round_retry_idempotent_and_conflict_rejected():
    db=FakePG()
    a,b,c=patched(db)
    with a,b,c:
        first=client.post('/api/v1/history/round',json=payload(),headers=AUTH)
        duplicate=client.post('/api/v1/history/round',json=payload(),headers=AUTH)
        changed=client.post('/api/v1/history/round',json=payload(arrivals=3),headers=AUTH)
    assert first.status_code==200,first.text
    assert duplicate.status_code==200
    assert changed.status_code==409,changed.text
    assert first.json()['metrics_in_request'] is True
    assert len(db.rounds)==1 and len(db.occupancy)==3
    assert db.rounds[(RUN,9)][0] == 2


def test_historic_occupancy_without_metrics_not_fabricated():
    db=FakePG()
    # Les occupations historiques de ticks 1-8 existent, mais les statistiques non.
    for tick in range(1,9):
        for fog in ('fog-a','fog-b','fog-c'):
            db.occupancy[(RUN,tick,fog)]=(10,0,10,'orchestrator_settled')
    db.occupancy[('other',1,'fog-a')] = (10,9,1,'orchestrator_settled')
    a,b,c=patched(db)
    with a,b,c:
        assert client.post('/api/v1/history/round',json=payload(),headers=AUTH).status_code==200
        r=client.get('/api/v1/history/summary',headers=AUTH)
    assert r.status_code==200, r.text
    data=r.json()
    assert data['latest_occupancy_tick']==9
    assert data['complete'] is False
    assert data['recorded_rounds']==1
    assert data['first_recorded_tick']==9
    assert data['totals']['arrivals_requested']==2
    assert len(data['points'])==9
    assert data['points'][0]['metrics'] is None
    assert data['points'][-1]['metrics']['arrivals_requested']==2
    assert data['points'][0]['occupied_total']==0


def test_recovery_without_metrics_preserves_occupancy_only():
    db=FakePG()
    request=payload()
    request.pop('metrics')
    a,b,c=patched(db)
    with a,b,c:
        result=client.post('/api/v1/history/round',json=request,headers=AUTH)
        summary=client.get('/api/v1/history/summary',headers=AUTH)
    assert result.status_code==200
    assert result.json()['metrics_in_request'] is False
    assert summary.json()['recorded_rounds']==0
    assert summary.json()['points'][0]['metrics'] is None
    assert len(db.rounds)==0


def test_stats_route_requires_token_and_is_run_isolated():
    db=FakePG()
    a,b,c=patched(db)
    with a,b,c:
        assert client.get('/api/v1/history/summary').status_code==401
        assert client.get('/api/v1/history/summary',headers=AUTH,params={'limit':101}).status_code==422


def test_cloud_payload_includes_real_round_metrics():
    s=importlib.util.spec_from_file_location('orch_stats_stage37', ROOT/'cloud/orchestrator_api/main.py')
    orch=importlib.util.module_from_spec(s)
    s.loader.exec_module(orch)
    api_client=TestClient(orch.app)
    output={'tick':9,'arrivals_requested':5,'locally_started':3,'remote_started':1,
            'auto_completed':2,'explicit_completed':1,'total_unserved':1}
    seen=[]
    def snapshot(tick, metrics=None):
        seen.append((tick,metrics))
        return {'tick':tick,'snapshots':[], 'metrics':metrics}
    with patch.object(orch,'ORCHESTRATOR_API_TOKEN','token'), \
         patch.object(orch,'CLOUD_HISTORY_URL','http://stub/history'), \
         patch.object(orch,'_bootstrap_tick'), \
         patch.object(orch.orchestrator,'run_round',return_value=output), \
         patch.object(orch,'_collect_snapshot',side_effect=snapshot), \
         patch.object(orch,'_send_snapshot',return_value={'status':'captured'}):
        resp=api_client.post('/api/v1/orchestrator/step',json={},headers=AUTH)
    assert resp.status_code==200,resp.text
    assert seen == [(9,{'arrivals_requested':5,'locally_started':3,'remote_started':1,
                        'auto_completed':2,'explicit_completed':1,'unserved':1})]


def test_gateway_reads_persisted_kpis_after_orchestrator_memory_restart():
    s=importlib.util.spec_from_file_location('gateway_stats_stage37', ROOT/'cloud/dashboard_gateway/main.py')
    gateway=importlib.util.module_from_spec(s)
    s.loader.exec_module(gateway)
    report={
        'simulation_run_id':RUN,'latest_occupancy_tick':9,'recorded_rounds':1,
        'first_recorded_tick':9,'last_recorded_tick':9,'complete':False,
        'totals':{'arrivals_requested':2,'locally_started':2,'remote_started':0,
                  'auto_completed':0,'explicit_completed':0,'unserved':0},
        'points':[{'tick':9,'occupied_by_fog':{'fog-a':0,'fog-b':2,'fog-c':0},'occupied_total':2,
                   'metrics':{'arrivals_requested':2,'locally_started':2,'remote_started':0,
                              'auto_completed':0,'explicit_completed':0,'unserved':0}}],
    }
    class Client:
        def __init__(self,**_): pass
        def __enter__(self): return self
        def __exit__(self,*_): return False
        def get(self,url,headers=None):
            assert 'history/summary' in url and headers['X-Orchestrator-Token']=='secret'
            class Response:
                def raise_for_status(self):pass
                def json(self):return report
            return Response()
    def rest(_client,url):
        if 'orchestrator/status' in url: return {'current_tick':9,'simulation_run_id':RUN,'history_pending_tick':None}
        if 'orchestrator/history' in url: return {'rounds':[]}  # restart erased RAM
        fog=next(f for f in gateway.FOG_IDS if f in url)
        if 'round/status' in url:return {'fog_id':fog,'tick':9}
        return {'fog_id':fog,'total_piles':10,'occupied_piles':2 if fog=='fog-b' else 0,
                'available_piles':8,'available_power_kw':200,'out_of_service_piles':0}
    with patch.object(gateway,'CLOUD_STATS_URL','http://private/history/summary'), \
         patch.object(gateway,'CLOUD_STATS_TOKEN','secret'), \
         patch.object(gateway.httpx,'Client',Client), \
         patch.object(gateway,'_json',side_effect=rest):
        result=gateway._status()
    assert result['stats_available'] is True
    assert result['statistics_scope']=='persistent_partial_run'
    assert result['last_round']['tick']==9
    assert result['last_round']['arrivals_requested']==2
    assert result['total_arrivals'] is None  # 1..8 unknowable; not fake zero
    assert result['historical_stats']['totals']['arrivals_requested']==2
    assert result['recent_events'][0]['id']=='round-9'


def test_full_history_marks_global_totals_complete():
    db=FakePG()
    for fog in ('fog-a','fog-b','fog-c'):
        db.occupancy[(RUN,1,fog)]=(10,0,10,'orchestrator_settled')
    db.rounds[(RUN,1)]=(0,0,0,0,0,0)
    a,b,c=patched(db)
    with a,b,c:
        result=client.get('/api/v1/history/summary',headers=AUTH)
    assert result.status_code==200
    assert result.json()['complete'] is True
    assert result.json()['scope']=='full_simulation_run'

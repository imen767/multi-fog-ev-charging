"""Per-vehicle simulated durations: local and remote flows, transaction safety.

All tests operate on disposable SQLite DBs, NOT the live project's data.
"""
import sqlite3

import pytest

from fog import database
from fog.repositories import reservation_repository as repo
from fog.repositories import pile_repository
from fog.services import round_service
from shared.schemas import HoldRequest


@pytest.fixture
def local_db(monkeypatch, tmp_path):
    path = tmp_path / 'fog_test.db'
    monkeypatch.setattr(database, 'DB_PATH', path)
    monkeypatch.setattr(database, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(repo, 'DB_PATH', path)
    monkeypatch.setattr(pile_repository, 'DB_PATH', path)
    database.init_db()
    with sqlite3.connect(path) as db:
        db.execute('''INSERT INTO stations
            (station_id, longitude, latitude, fog_id)
            VALUES ('station-1', 0, 0, 'fog-b')''')
        for i in range(4):
            db.execute('''INSERT INTO piles
                (pile_id, station_id, power_kw, pile_type)
                VALUES (?, 'station-1', 22, 'AC')''', (f'pile-{i}',))
    return path


def test_local_session_expires_on_exact_due_tick_and_frees_pile(local_db):
    session = repo.create_local_session('vehicle-1', 22, fog_id='fog-b',
                                        started_tick=4, duration_ticks=2)
    assert session['due_tick'] == 6
    assert repo.list_due_hold_ids(5) == []
    assert repo.list_due_hold_ids(6) == [session['session_id']]
    result = repo.complete_hold(session['session_id'], completed_tick=6)
    assert result['already_completed'] is False
    assert repo.complete_hold(session['session_id'], completed_tick=6)['already_completed'] is True
    assert repo.list_due_hold_ids(10) == []
    with sqlite3.connect(local_db) as db:
        assert db.execute("SELECT COUNT(*) FROM piles WHERE status='OCCUPIED'").fetchone()[0] == 0
        assert db.execute("SELECT status, completed_tick FROM charging_schedules").fetchone() == ('COMPLETED', 6)


def test_two_cars_different_durations_and_no_double_completion(monkeypatch, local_db, tmp_path):
    monkeypatch.setattr(round_service, '_CHECKPOINT_PATH', tmp_path / '.round_checkpoint.json')
    monkeypatch.setattr(round_service, 'FOG_ID', 'fog-b')
    previous = dict(round_service._round_state)
    round_service._round_state.update({'tick': 0, 'phase':'IDLE', 'prepare_result':None,
        'settle_result':None, 'unserved_vehicles':[], 'unserved_requests':[],
        'served_sessions':[], 'released_piles':[]})
    try:
        first = round_service.prepare_round(1, [
            {'vehicle_id':'v-1', 'required_power_kw':22, 'duration_ticks':1},
            {'vehicle_id':'v-2', 'required_power_kw':22, 'duration_ticks':3},
        ], [])
        assert len(first['served_sessions']) == 2
        assert [s['due_tick'] for s in first['served_sessions']] == [2, 4]
        round_service.settle_round(1, [], False)
        second = round_service.prepare_round(2, [], [])
        assert second['auto_completed'] == 1
        assert second['occupied_piles'] == 1
        assert len(second['completed_hold_ids']) == 1
        round_service.settle_round(2, [], False)
        third = round_service.prepare_round(3, [], [])
        assert third['auto_completed'] == 0
        assert third['occupied_piles'] == 1
        round_service.settle_round(3, [], False)
        fourth = round_service.prepare_round(4, [], [])
        assert fourth['auto_completed'] == 1
        assert fourth['occupied_piles'] == 0
        assert round_service.prepare_round(4, [], []) == fourth
    finally:
        round_service._round_state.clear()
        round_service._round_state.update(previous)


def test_remote_hold_activation_stores_duration_at_host_fog(local_db):
    request = HoldRequest(request_id='remote-1', origin_fog='fog-b',
                          required_piles=1, required_power_kw=22)
    hold = repo.create_hold(request)
    assert repo.confirm_hold(hold['hold_id']) is not None
    activated = repo.activate_hold(hold['hold_id'], started_tick=7,
                                   duration_ticks=3, vehicle_id='remote-car-1')
    assert activated['status'] == 'ACTIVE'
    # The host (Fog C in deployment) owns the deadline, not the origin (Fog B).
    assert repo.list_due_hold_ids(9) == []
    assert repo.list_due_hold_ids(10) == [hold['hold_id']]
    repo.activate_hold(hold['hold_id'], started_tick=7,
                       duration_ticks=3, vehicle_id='remote-car-1')
    with pytest.raises(ValueError, match='Cannot reschedule'):
        repo.activate_hold(hold['hold_id'], started_tick=7,
                           duration_ticks=4, vehicle_id='remote-car-1')
    assert repo.complete_hold(hold['hold_id'], completed_tick=10)['already_completed'] is False
    assert repo.list_due_hold_ids(10) == []
    with sqlite3.connect(local_db) as db:
        assert db.execute("SELECT status FROM reservations WHERE hold_id=?", (hold['hold_id'],)).fetchone()[0] == 'COMPLETED'


def test_preexisting_session_without_duration_is_not_prematurely_completed(local_db):
    old = repo.create_local_session('legacy', 22, fog_id='fog-b')
    assert old['due_tick'] is None
    assert repo.list_due_hold_ids(100) == []
    with sqlite3.connect(local_db) as db:
        assert db.execute('SELECT COUNT(*) FROM charging_schedules').fetchone()[0] == 0
        assert db.execute('SELECT status FROM reservations WHERE hold_id=?', (old['session_id'],)).fetchone()[0] == 'ACTIVE'

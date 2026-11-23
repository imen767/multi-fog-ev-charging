"""Fog reset must be impossible on production and safe on disposable SQLite."""
import sqlite3
from pathlib import Path
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fog.api import demo_control as demo
from fog.services import round_service


def test_reset_forbidden_in_real_fog(monkeypatch):
    monkeypatch.delenv('DEMO_ISOLATED_FOG', raising=False)
    app = FastAPI()
    app.include_router(demo.router)
    assert TestClient(app).post('/api/v1/demo/reset').status_code == 403


def test_disposable_reset_clears_only_demo_state(monkeypatch, tmp_path):
    path = tmp_path / 'fog_a.db'
    with sqlite3.connect(path) as con:
        con.execute('CREATE TABLE piles (pile_id TEXT, status TEXT)')
        con.execute("INSERT INTO piles VALUES ('one', 'OCCUPIED'), ('two', 'AVAILABLE')")
        con.execute('CREATE TABLE reservations (hold_id TEXT)')
        con.execute("INSERT INTO reservations VALUES ('hold')")
        con.execute('CREATE TABLE charging_schedules (hold_id TEXT)')
        con.execute("INSERT INTO charging_schedules VALUES ('hold')")
    cp = tmp_path / 'checkpoint.json'
    cp.write_text('{"tick": 2, "phase": "SETTLED"}')
    monkeypatch.setattr(demo, 'DB_PATH', path)
    monkeypatch.setattr(demo, 'FOG_ID', 'fog-a')
    # Only bypass absolute /app/data path check in isolated test fixture.
    monkeypatch.setattr(demo, 'assert_disposable_configuration', lambda: None)
    monkeypatch.setattr(round_service, '_CHECKPOINT_PATH', cp)
    monkeypatch.setattr(round_service, '_round_state', {
        'tick': 2, 'phase': 'SETTLED', 'unserved_vehicles': [],
        'unserved_requests': [], 'prepare_result': {}, 'settle_result': {},
        'served_sessions': [], 'released_piles': [],
    })
    monkeypatch.setenv('DEMO_RESET_TOKEN', 'test-local-token')
    app = FastAPI()
    app.include_router(demo.router)
    client = TestClient(app)
    assert client.post('/api/v1/demo/reset', headers={'X-Demo-Reset-Token': 'wrong'}).status_code == 403
    response = client.post('/api/v1/demo/reset', headers={'X-Demo-Reset-Token': 'test-local-token'})
    assert response.status_code == 200, response.text
    assert not cp.exists()
    assert round_service._round_state['tick'] == 0
    with sqlite3.connect(path) as con:
        assert con.execute('SELECT count(*) FROM reservations').fetchone()[0] == 0
        assert con.execute('SELECT count(*) FROM charging_schedules').fetchone()[0] == 0
        assert con.execute("SELECT count(*) FROM piles WHERE status='AVAILABLE'").fetchone()[0] == 2

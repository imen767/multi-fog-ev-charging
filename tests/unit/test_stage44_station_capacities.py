"""Step 44 — station-level live AC/DC availability from SQLite."""
import sqlite3
from datetime import datetime, timedelta, timezone

from fog.repositories import pile_repository


def test_live_availability_respects_active_hold_and_connector(tmp_path, monkeypatch):
    db = tmp_path / 'fog.db'
    with sqlite3.connect(db) as conn:
        conn.executescript("""
            CREATE TABLE stations (station_id TEXT PRIMARY KEY);
            INSERT INTO stations VALUES ('1488');
            CREATE TABLE piles (
              pile_id TEXT PRIMARY KEY, station_id TEXT, power_kw REAL,
              pile_type TEXT, status TEXT, is_reservable INTEGER
            );
            CREATE TABLE reservations (
              pile_id TEXT, status TEXT, expires_at TEXT
            );
            INSERT INTO piles VALUES
              ('ac-free','1488',7,'AC','AVAILABLE',1),
              ('dc-held','1488',50,'DC','AVAILABLE',1),
              ('dc-busy','1488',150,'DC','OCCUPIED',1),
              ('dc-free','1488',120,'DC','AVAILABLE',1),
              ('ac-locked','1488',7,'AC','AVAILABLE',0);
        """)
        expiry = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        conn.execute("INSERT INTO reservations VALUES (?,?,?)", ('dc-held', 'HOLD', expiry))
    monkeypatch.setattr(pile_repository, 'DB_PATH', db)
    row, = pile_repository.get_station_capacities()
    assert row['station_id'] == '1488'
    assert row['total_piles'] == 5
    assert row['available_piles'] == 2
    assert row['available_ac_piles'] == 1
    assert row['available_dc_piles'] == 1
    assert row['occupied_piles'] == 1
    assert row['max_available_power_kw'] == 120
    assert row['max_available_ac_kw'] == 7
    assert row['max_available_dc_kw'] == 120
    assert row['available_power_kw'] == 127


def test_dc_fully_booked_but_ac_available(tmp_path, monkeypatch):
    db = tmp_path / 'fog.db'
    with sqlite3.connect(db) as conn:
        conn.executescript("""
            CREATE TABLE stations (station_id TEXT PRIMARY KEY);
            INSERT INTO stations VALUES ('88');
            CREATE TABLE piles (pile_id TEXT, station_id TEXT, power_kw REAL,
                                pile_type TEXT, status TEXT, is_reservable INTEGER);
            CREATE TABLE reservations (pile_id TEXT, status TEXT, expires_at TEXT);
            INSERT INTO piles VALUES ('ac','88',7,'AC','AVAILABLE',1),
                                     ('dc','88',150,'DC','OCCUPIED',1);
        """)
    monkeypatch.setattr(pile_repository, 'DB_PATH', db)
    row, = pile_repository.get_station_capacities()
    assert row['available_piles'] == 1
    assert row['available_ac_piles'] == 1
    assert row['available_dc_piles'] == 0
    assert row['max_available_dc_kw'] == 0


def test_station_without_piles_is_reported_as_zero(tmp_path, monkeypatch):
    db = tmp_path / 'fog.db'
    with sqlite3.connect(db) as conn:
        conn.executescript("""
            CREATE TABLE stations (station_id TEXT PRIMARY KEY);
            CREATE TABLE piles (pile_id TEXT, station_id TEXT, power_kw REAL,
                                pile_type TEXT, status TEXT, is_reservable INTEGER);
            CREATE TABLE reservations (pile_id TEXT, status TEXT, expires_at TEXT);
            INSERT INTO stations VALUES ('1497');
        """)
    monkeypatch.setattr(pile_repository, 'DB_PATH', db)
    row, = pile_repository.get_station_capacities()
    assert row['station_id'] == '1497'
    assert row['total_piles'] == 0
    assert row['available_piles'] == 0
    assert row['available_ac_piles'] == 0
    assert row['available_dc_piles'] == 0
    assert row['max_available_power_kw'] == 0

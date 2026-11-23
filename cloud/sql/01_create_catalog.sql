CREATE TABLE IF NOT EXISTS stations (
    station_id TEXT PRIMARY KEY,
    longitude DOUBLE PRECISION NOT NULL,
    latitude DOUBLE PRECISION NOT NULL,
    slow_count INTEGER NOT NULL,
    fast_count INTEGER NOT NULL,
    charge_count INTEGER NOT NULL,
    tazid TEXT,
    fog_id TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS piles (
    pile_id TEXT PRIMARY KEY,
    station_id TEXT NOT NULL
        REFERENCES stations(station_id),
    power_kw DOUBLE PRECISION NOT NULL
        CHECK (power_kw >= 0),
    pile_type TEXT NOT NULL
        CHECK (pile_type IN ('AC', 'DC')),
    fog_id TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_stations_fog
    ON stations(fog_id);

CREATE INDEX IF NOT EXISTS idx_piles_fog
    ON piles(fog_id);

CREATE INDEX IF NOT EXISTS idx_piles_station
    ON piles(station_id);

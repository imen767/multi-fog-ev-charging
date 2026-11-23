CREATE TABLE IF NOT EXISTS occupancy_history (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,

    fog_id TEXT NOT NULL
        CHECK (fog_id IN ('fog-a', 'fog-b', 'fog-c')),

    captured_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    simulation_tick INTEGER,

    total_piles INTEGER NOT NULL CHECK (total_piles >= 0),

    occupied_piles INTEGER NOT NULL CHECK (occupied_piles >= 0),

    reservable_piles INTEGER NOT NULL CHECK (reservable_piles >= 0),

    source TEXT NOT NULL DEFAULT 'cloud_snapshot',

    CHECK (occupied_piles <= total_piles),
    CHECK (reservable_piles <= total_piles)
);

CREATE INDEX IF NOT EXISTS idx_occupancy_history_fog_time
    ON occupancy_history(fog_id, captured_at DESC);

CREATE UNIQUE INDEX IF NOT EXISTS idx_occupancy_history_fog_tick
    ON occupancy_history(fog_id, simulation_tick)
    WHERE simulation_tick IS NOT NULL;

CREATE TABLE IF NOT EXISTS pile_states (
    pile_id TEXT PRIMARY KEY
        REFERENCES piles(pile_id),

    fog_id TEXT NOT NULL,

    status TEXT NOT NULL
        CHECK (status IN (
            'AVAILABLE',
            'OCCUPIED',
            'OUT_OF_SERVICE'
        )),

    is_reservable BOOLEAN NOT NULL,

    reservation_status TEXT
        CHECK (
            reservation_status IS NULL
            OR reservation_status IN (
                'HOLD',
                'CONFIRMED',
                'ACTIVE',
                'COMPLETED',
                'CANCELLED',
                'EXPIRED'
            )
        ),

    currently_reservable BOOLEAN NOT NULL,

    synced_at TIMESTAMPTZ NOT NULL
        DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_pile_states_fog
    ON pile_states(fog_id);

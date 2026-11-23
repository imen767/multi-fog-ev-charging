
BEGIN;

INSERT INTO pile_states (
    pile_id,
    fog_id,
    status,
    is_reservable,
    reservation_status,
    currently_reservable,
    synced_at
)
SELECT
    pile_id,
    fog_id,
    status,
    is_reservable,
    reservation_status,
    currently_reservable,
    NOW()
FROM pile_states_import
WHERE TRUE

ON CONFLICT (pile_id)
DO UPDATE SET
    fog_id = EXCLUDED.fog_id,
    status = EXCLUDED.status,
    is_reservable = EXCLUDED.is_reservable,
    reservation_status = EXCLUDED.reservation_status,
    currently_reservable = EXCLUDED.currently_reservable,
    synced_at = EXCLUDED.synced_at;

COMMIT;

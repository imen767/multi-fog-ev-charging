-- Step 33: preserve all legacy tick observations (run_id NULL).
-- New recordings must always specify SIMULATION_RUN_ID.
-- Run with psql -v ON_ERROR_STOP=1 inside postgres-cloud, after a pg_dump backup.
BEGIN;
ALTER TABLE occupancy_history ADD COLUMN IF NOT EXISTS simulation_run_id UUID;

-- Remove the legacy unique index (fog_id,tick), which wrongly merges distinct runs.
-- The initial project uses a partial UNIQUE INDEX, not a table CONSTRAINT.
DO $$
DECLARE old_index RECORD;
BEGIN
    FOR old_index IN
        SELECT ns.nspname AS schema_name, idx.relname AS index_name
        FROM pg_index i
        JOIN pg_class tbl ON tbl.oid=i.indrelid
        JOIN pg_namespace ns ON ns.oid=tbl.relnamespace
        JOIN pg_class idx ON idx.oid=i.indexrelid
        WHERE ns.nspname=current_schema() AND tbl.relname='occupancy_history'
          AND i.indisunique AND NOT i.indisprimary
          AND pg_get_indexdef(i.indexrelid) ~* '\(fog_id, simulation_tick\)'
    LOOP
        EXECUTE format('DROP INDEX %I.%I', old_index.schema_name, old_index.index_name);
    END LOOP;
END $$;

-- Retain the same uniqueness guarantee for legacy records.
CREATE UNIQUE INDEX IF NOT EXISTS uq_occupancy_history_legacy_tick
  ON occupancy_history(fog_id, simulation_tick)
  WHERE simulation_tick IS NOT NULL AND simulation_run_id IS NULL;

-- Allow tick 1 to recur across new simulation runs, never within the same run.
CREATE UNIQUE INDEX IF NOT EXISTS uq_occupancy_history_run_tick
  ON occupancy_history(simulation_run_id, fog_id, simulation_tick)
  WHERE simulation_tick IS NOT NULL AND simulation_run_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_occupancy_history_run_recent
  ON occupancy_history(simulation_run_id, fog_id, simulation_tick DESC)
  WHERE source='orchestrator_settled';
COMMIT;

-- Étape 37 : journal de métriques réelles, une ligne par (simulation, tick).
-- À exécuter APRES cloud/sql/05_simulation_runs.sql.
-- Les anciennes occupations restent intactes et aucune métrique passée n'est inventée.
BEGIN;
CREATE TABLE IF NOT EXISTS round_statistics (
    simulation_run_id UUID NOT NULL,
    simulation_tick INTEGER NOT NULL CHECK (simulation_tick >= 1),
    arrivals_requested INTEGER NOT NULL CHECK (arrivals_requested >= 0),
    locally_started INTEGER NOT NULL CHECK (locally_started >= 0),
    remote_started INTEGER NOT NULL CHECK (remote_started >= 0),
    auto_completed INTEGER NOT NULL CHECK (auto_completed >= 0),
    explicit_completed INTEGER NOT NULL CHECK (explicit_completed >= 0),
    unserved INTEGER NOT NULL CHECK (unserved >= 0),
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (simulation_run_id, simulation_tick)
);
CREATE INDEX IF NOT EXISTS idx_round_statistics_run_recent
  ON round_statistics(simulation_run_id, simulation_tick DESC);
COMMIT;

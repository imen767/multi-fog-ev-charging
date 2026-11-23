"""API Cloud recevant les instantanés d'état des bornes des Fog.

Chaque Fog garde SQLite comme source de vérité. Cette API maintient une copie
centralisée (éventuellement différée) dans PostgreSQL.
"""

import os
import secrets
from uuid import UUID
from typing import Literal

import psycopg
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

DATABASE_URL = os.environ.get("DATABASE_URL", "")
SYNC_TOKEN = os.environ.get("CLOUD_SYNC_TOKEN", "")
SIMULATION_RUN_ID = os.environ.get("SIMULATION_RUN_ID", "")


def _active_run_id() -> str:
    """One stable run id provided by Compose, not recreated on service restart."""
    try:
        return str(UUID(SIMULATION_RUN_ID))
    except (ValueError, TypeError):
        raise HTTPException(status_code=503, detail="SIMULATION_RUN_ID missing/invalid; configure .env.cloud")

app = FastAPI(title="Multi-Fog Cloud Sync API", version="0.1.0")


class PileState(BaseModel):
    pile_id: str = Field(min_length=1)
    status: Literal["AVAILABLE", "OCCUPIED", "OUT_OF_SERVICE"]
    is_reservable: bool
    reservation_status: Literal[
        "HOLD", "CONFIRMED", "ACTIVE", "COMPLETED", "CANCELLED", "EXPIRED"
    ] | None = None
    currently_reservable: bool


class PileStatesBatch(BaseModel):
    fog_id: Literal["fog-a", "fog-b", "fog-c"]
    states: list[PileState] = Field(min_length=1, max_length=500)


@app.get("/health")
def health():
    return {"status": "healthy", "service": "cloud-sync-api"}


@app.post("/api/v1/sync/pile-states")
def ingest_pile_states(
    batch: PileStatesBatch,
    x_sync_token: str | None = Header(default=None, alias="X-Sync-Token"),
):
    if not SYNC_TOKEN or not x_sync_token or not secrets.compare_digest(
        x_sync_token, SYNC_TOKEN
    ):
        raise HTTPException(status_code=401, detail="Invalid sync token")

    if not DATABASE_URL:
        raise HTTPException(status_code=503, detail="Database is not configured")

    ids = [state.pile_id for state in batch.states]
    if len(ids) != len(set(ids)):
        raise HTTPException(status_code=400, detail="Duplicate pile_id in batch")

    try:
        with psycopg.connect(DATABASE_URL, connect_timeout=5) as conn:
            # Le Fog ne peut déclarer que les bornes qui lui appartiennent.
            owners = dict(
                conn.execute(
                    "SELECT pile_id, fog_id FROM piles WHERE pile_id = ANY(%s)",
                    (ids,),
                ).fetchall()
            )
            wrong = [pid for pid in ids if owners.get(pid) != batch.fog_id]
            if wrong:
                raise HTTPException(
                    status_code=400,
                    detail=f"Unknown/foreign piles for {batch.fog_id}: {wrong[:5]}",
                )

            rows = [
                (
                    state.pile_id,
                    batch.fog_id,
                    state.status,
                    state.is_reservable,
                    state.reservation_status,
                    state.currently_reservable,
                )
                for state in batch.states
            ]
            with conn.cursor() as cursor:
                cursor.executemany(
                    """
                    INSERT INTO pile_states (
                        pile_id, fog_id, status, is_reservable,
                        reservation_status, currently_reservable, synced_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, NOW())
                    ON CONFLICT (pile_id) DO UPDATE SET
                        fog_id = EXCLUDED.fog_id,
                        status = EXCLUDED.status,
                        is_reservable = EXCLUDED.is_reservable,
                        reservation_status = EXCLUDED.reservation_status,
                        currently_reservable = EXCLUDED.currently_reservable,
                        synced_at = EXCLUDED.synced_at
                    """,
                    rows,
                )
            # Commit automatique de la transaction à la sortie du `with`.
    except HTTPException:
        raise
    except psycopg.Error:
        raise HTTPException(status_code=503, detail="PostgreSQL unavailable")

    return {"status": "synced", "fog_id": batch.fog_id, "received": len(ids)}

# --- Extension DevOps : capture atomique de l'occupation apres SETTLE ---
class FogOccupancy(BaseModel):
    fog_id: Literal["fog-a", "fog-b", "fog-c"]
    total_piles: int = Field(ge=0)
    occupied_piles: int = Field(ge=0)
    reservable_piles: int = Field(ge=0)


class RoundMetrics(BaseModel):
    # Comptages réellement observés durant ce round, pas des estimations ML.
    arrivals_requested: int = Field(ge=0)
    locally_started: int = Field(ge=0)
    remote_started: int = Field(ge=0)
    auto_completed: int = Field(ge=0)
    explicit_completed: int = Field(ge=0)
    unserved: int = Field(ge=0)


class RoundOccupancyBatch(BaseModel):
    simulation_run_id: UUID
    tick: int = Field(ge=1)
    snapshots: list[FogOccupancy] = Field(min_length=3, max_length=3)
    # Optionnel pour le replay d'anciens rounds et la reprise post-crash :
    # absente != zeros. Ne jamais remplir par défaut des statistiques inconnues.
    metrics: RoundMetrics | None = None


@app.post("/api/v1/history/round")
def capture_round_occupancy(
    batch: RoundOccupancyBatch,
    x_orchestrator_token: str | None = Header(default=None, alias="X-Orchestrator-Token"),
):
    """Reçoit une photographie par Fog APRES SETTLE ; enregistre 3 points dans une transaction."""
    expected_token = os.environ.get("ORCHESTRATOR_API_TOKEN", "")
    if not expected_token or not x_orchestrator_token or not secrets.compare_digest(
        expected_token, x_orchestrator_token
    ):
        raise HTTPException(status_code=401, detail="Invalid orchestrator token")

    run_id = _active_run_id()
    if str(batch.simulation_run_id) != run_id:
        raise HTTPException(status_code=409, detail="Wrong simulation_run_id for this Cloud deployment")

    expected_fogs = {"fog-a", "fog-b", "fog-c"}
    observed_fogs = [s.fog_id for s in batch.snapshots]
    if len(set(observed_fogs)) != 3 or set(observed_fogs) != expected_fogs:
        raise HTTPException(status_code=422, detail="Exactly one snapshot per Fog required")
    if any(s.occupied_piles + s.reservable_piles > s.total_piles for s in batch.snapshots):
        raise HTTPException(status_code=422, detail="Invalid occupancy counters")
    if not DATABASE_URL:
        raise HTTPException(status_code=503, detail="Database is not configured")

    try:
        with psycopg.connect(DATABASE_URL, connect_timeout=5) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT fog_id, COUNT(*) FROM piles WHERE fog_id IN ('fog-a','fog-b','fog-c') GROUP BY fog_id"
                )
                catalog = dict(cursor.fetchall())
                for snapshot in batch.snapshots:
                    if catalog.get(snapshot.fog_id) != snapshot.total_piles:
                        raise HTTPException(
                            status_code=422,
                            detail=f"Incorrect total_piles for {snapshot.fog_id} (catalog mismatch)",
                        )

                # Conflit = ne jamais ecraser une ancienne valeur de tick.
                cursor.executemany(
                    """
                    INSERT INTO occupancy_history (
                        simulation_run_id, fog_id, simulation_tick, total_piles,
                        occupied_piles, reservable_piles, source
                    ) VALUES (%s::uuid, %s, %s, %s, %s, %s, 'orchestrator_settled')
                    ON CONFLICT DO NOTHING
                    """,
                    [
                        (run_id, s.fog_id, batch.tick, s.total_piles, s.occupied_piles, s.reservable_piles)
                        for s in batch.snapshots
                    ],
                )
                cursor.execute(
                    """SELECT fog_id, total_piles, occupied_piles, reservable_piles, source
                       FROM occupancy_history WHERE simulation_run_id=%s::uuid AND simulation_tick=%s""",
                    (run_id, batch.tick),
                )
                persisted = {
                    fog: (total, occupied, reservable, source)
                    for fog, total, occupied, reservable, source in cursor.fetchall()
                }
                for s in batch.snapshots:
                    expected = (s.total_piles, s.occupied_piles, s.reservable_piles, "orchestrator_settled")
                    if persisted.get(s.fog_id) != expected:
                        raise HTTPException(
                            status_code=409,
                            detail=f"Run {run_id}, tick {batch.tick} already recorded differently for {s.fog_id}",
                        )

                # Enregistrement atomique, dans la MÊME transaction que les 3 snapshots.
                # Une retransmission identique est idempotente ; une contradiction échoue.
                # Si metrics=None (reprise sans le résultat en mémoire), ne rien inventer.
                if batch.metrics is not None:
                    values = (
                        batch.metrics.arrivals_requested, batch.metrics.locally_started,
                        batch.metrics.remote_started, batch.metrics.auto_completed,
                        batch.metrics.explicit_completed, batch.metrics.unserved,
                    )
                    cursor.execute(
                        """INSERT INTO round_statistics (
                               simulation_run_id, simulation_tick, arrivals_requested,
                               locally_started, remote_started, auto_completed,
                               explicit_completed, unserved
                           ) VALUES (%s::uuid, %s, %s, %s, %s, %s, %s, %s)
                           ON CONFLICT (simulation_run_id, simulation_tick) DO NOTHING""",
                        (run_id, batch.tick, *values),
                    )
                    cursor.execute(
                        """SELECT arrivals_requested, locally_started, remote_started,
                                  auto_completed, explicit_completed, unserved
                           FROM round_statistics
                           WHERE simulation_run_id=%s::uuid AND simulation_tick=%s""",
                        (run_id, batch.tick),
                    )
                    row = cursor.fetchone()
                    if row is None or tuple(row) != values:
                        raise HTTPException(
                            status_code=409,
                            detail=f"Run {run_id}, tick {batch.tick} already has different round metrics",
                        )
    except HTTPException:
        raise
    except psycopg.Error as exc:
        raise HTTPException(status_code=503, detail=f"PostgreSQL error: {type(exc).__name__}") from exc

    return {"status": "captured", "simulation_run_id": run_id, "tick": batch.tick,
            "recorded_fogs": 3, "metrics_in_request": batch.metrics is not None}


@app.get("/api/v1/history/summary")
def round_statistics_summary(
    limit: int = 50,
    x_orchestrator_token: str | None = Header(default=None, alias="X-Orchestrator-Token"),
):
    """Métriques réelles par run, depuis PostgreSQL. Lecture réservée à la gateway.

    Les ticks plus anciens sans statistiques restent visibles via leur occupation,
    mais jamais comme des arrivées/completions nulles.
    """
    expected = os.environ.get("ORCHESTRATOR_API_TOKEN", "")
    if not expected or not x_orchestrator_token or not secrets.compare_digest(expected, x_orchestrator_token):
        raise HTTPException(status_code=401, detail="Invalid orchestrator token")
    if limit < 1 or limit > 100:
        raise HTTPException(status_code=422, detail="limit must be 1..100")
    run_id = _active_run_id()
    if not DATABASE_URL:
        raise HTTPException(status_code=503, detail="Database is not configured")
    try:
        with psycopg.connect(DATABASE_URL, connect_timeout=5) as conn:
            # Toutes les statistiques enregistrées de ce run, y compris si >100 ticks.
            rows = conn.execute(
                """SELECT simulation_tick, arrivals_requested, locally_started,
                          remote_started, auto_completed, explicit_completed, unserved
                   FROM round_statistics
                   WHERE simulation_run_id=%s::uuid ORDER BY simulation_tick""",
                (run_id,),
            ).fetchall()
            # Les 3 lignes /tick sont atomiques, elles forment l'historique d'occupation.
            occupancy = conn.execute(
                """SELECT simulation_tick, fog_id, occupied_piles
                   FROM occupancy_history
                   WHERE simulation_run_id=%s::uuid
                     AND source='orchestrator_settled' AND simulation_tick IS NOT NULL
                   ORDER BY simulation_tick DESC, fog_id LIMIT %s""",
                (run_id, limit * 3),
            ).fetchall()
    except psycopg.Error as exc:
        raise HTTPException(status_code=503, detail=f"PostgreSQL unavailable: {type(exc).__name__}") from exc

    by_tick = {
        int(t): {
            "arrivals_requested": int(a), "locally_started": int(l),
            "remote_started": int(r), "auto_completed": int(ac),
            "explicit_completed": int(ec), "unserved": int(u),
        }
        for t, a, l, r, ac, ec, u in rows
    }
    by_occupancy: dict[int, dict] = {}
    for tick, fog_id, occupied in occupancy:
        by_occupancy.setdefault(int(tick), {})[fog_id] = int(occupied)
    points = [
        {"tick": tick, "occupied_by_fog": by_occupancy[tick],
         "occupied_total": sum(by_occupancy[tick].values()),
         "metrics": by_tick.get(tick)}
        for tick in sorted(by_occupancy)
        if set(by_occupancy[tick]) == {"fog-a", "fog-b", "fog-c"}
    ]
    latest = max(by_occupancy, default=0)
    # Couverture globale : pour toutes les anciennes observations du run,
    # pas simplement sur les 50 derniers points. Cela évite un faux 'complet'.
    try:
        with psycopg.connect(DATABASE_URL, connect_timeout=5) as conn:
            full_latest = int(conn.execute(
                """SELECT COALESCE(MAX(simulation_tick),0) FROM occupancy_history
                   WHERE simulation_run_id=%s::uuid AND source='orchestrator_settled'""",
                (run_id,),
            ).fetchone()[0])
    except psycopg.Error as exc:
        raise HTTPException(status_code=503, detail=f"PostgreSQL unavailable: {type(exc).__name__}") from exc
    complete = full_latest > 0 and all(t in by_tick for t in range(1, full_latest + 1))
    return {
        "simulation_run_id": run_id,
        "latest_occupancy_tick": full_latest,
        "recorded_rounds": len(rows),
        "first_recorded_tick": min(by_tick) if by_tick else None,
        "last_recorded_tick": max(by_tick) if by_tick else None,
        "complete": complete,
        "totals": {key: sum(v[key] for v in by_tick.values())
                   for key in ("arrivals_requested", "locally_started", "remote_started",
                               "auto_completed", "explicit_completed", "unserved")},
        "points": points,
        "scope": "recorded_rounds_only" if not complete else "full_simulation_run",
    }


@app.get("/api/v1/history/run-status")
def history_run_status(
    tick: int = 0,
    x_orchestrator_token: str | None = Header(default=None, alias="X-Orchestrator-Token"),
):
    """Read-only journal probe for safe orchestrator recovery after restart."""
    expected = os.environ.get("ORCHESTRATOR_API_TOKEN", "")
    if not expected or not x_orchestrator_token or not secrets.compare_digest(expected, x_orchestrator_token):
        raise HTTPException(status_code=401, detail="Invalid orchestrator token")
    if tick < 0:
        raise HTTPException(status_code=422, detail="tick must be non-negative")
    run_id = _active_run_id()
    if not DATABASE_URL:
        raise HTTPException(status_code=503, detail="Database is not configured")
    try:
        with psycopg.connect(DATABASE_URL, connect_timeout=5) as conn:
            latest = conn.execute(
                "SELECT COALESCE(MAX(simulation_tick), 0) FROM occupancy_history "
                "WHERE simulation_run_id=%s::uuid AND source='orchestrator_settled'",
                (run_id,),
            ).fetchone()[0]
            fogs = []
            if tick:
                fogs = [row[0] for row in conn.execute(
                    "SELECT fog_id FROM occupancy_history "
                    "WHERE simulation_run_id=%s::uuid AND simulation_tick=%s "
                    "AND source='orchestrator_settled' ORDER BY fog_id",
                    (run_id, tick),
                ).fetchall()]
    except psycopg.Error as exc:
        raise HTTPException(status_code=503, detail=f"PostgreSQL unavailable: {type(exc).__name__}") from exc
    if not fogs:
        state = "missing"
    elif fogs == ["fog-a", "fog-b", "fog-c"]:
        state = "recorded"
    else:
        state = "partial"
    return {"simulation_run_id": run_id, "latest_tick": int(latest),
            "tick": tick, "tick_status": state, "recorded_fogs": fogs}


# --- Etape 24 : prevision issue de l'historique des ticks PostgreSQL ---
# Pas de modification des Fog ni du code du modele Chronos-2.
import json as _json
from urllib.request import Request as _Request, urlopen as _urlopen
from urllib.error import HTTPError as _HTTPError, URLError as _URLError

from fastapi import Query as _Query

ML_API_TOKEN = os.environ.get("CLOUD_ML_API_TOKEN", "")
CHRONOS_SERVICE_URL = os.environ.get("CHRONOS_SERVICE_URL", "http://chronos-cloud:8000")
SIM_TICK_MINUTES = os.environ.get("SIM_TICK_MINUTES", "")


def _ml_authorized(token: str | None) -> None:
    if not ML_API_TOKEN or not token or not secrets.compare_digest(token, ML_API_TOKEN):
        raise HTTPException(status_code=401, detail="Invalid ML API token")


def _read_fog_history(fog_id: str) -> dict:
    if not DATABASE_URL:
        raise HTTPException(status_code=503, detail="Database is not configured")
    run_id = _active_run_id()
    try:
        with psycopg.connect(DATABASE_URL, connect_timeout=5) as conn:
            # Seulement les ticks valides de l'orchestrateur, jamais les snapshots manuels.
            data = conn.execute(
                """SELECT simulation_tick, occupied_piles
                   FROM occupancy_history
                   WHERE fog_id = %s
                     AND simulation_run_id = %s::uuid
                     AND simulation_tick IS NOT NULL
                     AND source = 'orchestrator_settled'
                   ORDER BY simulation_tick DESC
                   LIMIT 12""",
                (fog_id, run_id),
            ).fetchall()
    except psycopg.Error as exc:
        raise HTTPException(status_code=503, detail=f"PostgreSQL unavailable: {type(exc).__name__}") from exc

    data = list(reversed(data))  # Ancien -> recent, requis par Chronos.
    ticks = [int(row[0]) for row in data]
    values = [float(row[1]) for row in data]
    enough = len(values) == 12
    consecutive = enough and all(b == a + 1 for a, b in zip(ticks, ticks[1:]))
    reason = (
        "ok" if consecutive else
        "insufficient_history" if not enough else
        "non_consecutive_ticks"
    )
    return {
        "fog_id": fog_id,
        "simulation_run_id": run_id,
        "metric": "occupied_piles",
        "source": "occupancy_history/orchestrator_settled",
        "history_points": len(values),
        "ticks": ticks,
        "history": values,
        "ready": consecutive,
        "reason": reason,
    }


@app.get("/api/v1/ml/history/{fog_id}")
def get_ml_history(
    fog_id: Literal["fog-a", "fog-b", "fog-c"],
    x_ml_token: str | None = Header(default=None, alias="X-ML-Token"),
):
    """Consulter la fenetre d'historique sans declencher d'inference."""
    _ml_authorized(x_ml_token)
    return _read_fog_history(fog_id)


@app.post("/api/v1/ml/forecast/{fog_id}")
def forecast_from_history(
    fog_id: Literal["fog-a", "fog-b", "fog-c"],
    prediction_length: int = _Query(default=1, ge=1, le=12),
    x_ml_token: str | None = Header(default=None, alias="X-ML-Token"),
):
    """Historique PostgreSQL -> Chronos-2 ; n'agit JAMAIS sur les reservations."""
    _ml_authorized(x_ml_token)
    sample = _read_fog_history(fog_id)
    if not sample["ready"]:
        raise HTTPException(
            status_code=409,
            detail={
                "reason": sample["reason"],
                "history_points": sample["history_points"],
                "ticks": sample["ticks"],
                "required": "12 consecutive settled simulation ticks",
            },
        )

    # Dans le code original, forecast_service.STEP_MINUTES = 60 (fixe).
    # Interdire de qualifier la prediction d'horaire si le tick n'est pas horaire.
    if SIM_TICK_MINUTES != "60":
        raise HTTPException(
            status_code=409,
            detail="SIM_TICK_MINUTES must explicitly equal 60 for the current hourly Chronos-2 service",
        )

    payload = _json.dumps({
        "history": sample["history"],
        "prediction_length": prediction_length,
    }).encode("utf-8")
    request = _Request(
        CHRONOS_SERVICE_URL.rstrip("/") + "/api/v1/forecast",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with _urlopen(request, timeout=300) as response:
            result = _json.load(response)
    except (_HTTPError, _URLError, TimeoutError, OSError) as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Chronos service unavailable: {type(exc).__name__}",
        ) from exc
    except (ValueError, _json.JSONDecodeError) as exc:
        raise HTTPException(status_code=502, detail="Invalid Chronos response") from exc

    try:
        if result["step_minutes"] != 60 or not all(k in result["forecasts"] for k in ("q10", "q50", "q90")):
            raise ValueError("invalid Chronos schema")
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=502, detail="Unexpected Chronos output") from exc

    return {
        "fog_id": fog_id,
        "simulation_run_id": sample.get("simulation_run_id"),
        "source": sample["source"],
        "metric": sample["metric"],
        "input_ticks": sample["ticks"],
        "input_history": sample["history"],
        "model": result.get("model", "unknown"),
        "horizon_steps": result.get("horizon_steps", prediction_length),
        "step_minutes": result["step_minutes"],
        "forecasts": result["forecasts"],
        "role": "recommendation_only_no_reservation",
    }

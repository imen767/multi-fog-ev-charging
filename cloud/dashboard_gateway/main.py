"""Development dashboard gateway to the existing cooperative round orchestrator.

Only forwards safe manual rounds. Never contains simulation logic or selects peers.
Do not expose this prototype outside localhost without proper user authentication.
"""
import os
import secrets
import threading
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
from typing import Literal

FOG_IDS = ("fog-a", "fog-b", "fog-c")
FOG_URLS = {fog: os.getenv(f"FOG_{fog[-1].upper()}_URL", f"http://{fog}:8000") for fog in FOG_IDS}
ORCHESTRATOR_URL = os.getenv("ORCHESTRATOR_URL", "http://cloud-orchestrator:8000").rstrip("/")
ORCHESTRATOR_API_TOKEN = os.getenv("ORCHESTRATOR_API_TOKEN", "")
CLOUD_STATS_URL = os.getenv("CLOUD_STATS_URL", "").rstrip("/")
CLOUD_STATS_TOKEN = os.getenv("CLOUD_STATS_TOKEN", "")
ALLOWED_ORIGINS = {"http://localhost:5173", "http://127.0.0.1:5173"}
app = FastAPI(title="Fog Dashboard Development Gateway", version="0.2")
_action_lock = threading.Lock()


class InjectRequest(BaseModel):
    fog_id: Literal["fog-a", "fog-b", "fog-c"]
    count: int = Field(ge=1, le=50)
    # Optional fixed duration; if omitted, vehicles get individual 1-4 tick durations.
    duration_ticks: int | None = Field(default=None, ge=1, le=48)


def _json(client: httpx.Client, url: str) -> dict:
    r = client.get(url)
    r.raise_for_status()
    return r.json()


def _status() -> dict[str, Any]:
    with httpx.Client(timeout=12) as client:
        orch = _json(client, f"{ORCHESTRATOR_URL}/api/v1/orchestrator/status")
        hist = _json(client, f"{ORCHESTRATOR_URL}/api/v1/orchestrator/history").get("rounds", [])
        stats = None
        if CLOUD_STATS_URL:
            if not CLOUD_STATS_TOKEN:
                raise ValueError("CLOUD_STATS_TOKEN absent : statistiques non accessibles")
            response = client.get(
                CLOUD_STATS_URL, headers={"X-Orchestrator-Token": CLOUD_STATS_TOKEN}
            )
            response.raise_for_status()
            stats = response.json()
            if stats.get("simulation_run_id") != orch.get("simulation_run_id"):
                raise ValueError("Simulation Cloud incohérente entre orchestrateur et statistiques")
        capacities = {}
        ticks = set()
        now = datetime.now(timezone.utc).isoformat()
        for fog_id, url in FOG_URLS.items():
            fog_status = _json(client, f"{url}/api/v1/round/status")
            cap = _json(client, f"{url}/api/v1/piles/capacity")
            if fog_status.get("fog_id") != fog_id or cap.get("fog_id") != fog_id:
                raise ValueError(f"Réponse incorrecte du {fog_id}")
            ticks.add(fog_status["tick"])
            capacities[fog_id] = {
                **cap,
                "isOnline": True,
                "lastUpdated": now,
            }
        # Never show an arbitrary tick if the Fog nodes have diverged.
        if len(ticks) != 1:
            raise ValueError(f"Ticks des Fog désynchronisés : {sorted(ticks)}")
        tick = ticks.pop()
        if orch.get("current_tick") != tick:
            raise ValueError("Tick de l'orchestrateur différent des ticks des Fog")
        events = []
        for round_info in reversed(hist[-20:]):
            n = round_info.get("tick", 0)
            transfers = round_info.get("transfers", [])
            unserved = round_info.get("total_unserved", 0)
            events.append({
                "id": f"round-{n}", "tick": n, "time": f"Tick {n}",
                "category": "COOPERATION" if transfers else "ROUND",
                "title": f"Tick {n} — round terminé",
                "message": (
                    f"{round_info.get('arrivals_requested', '—')} arrivée(s), "
                    f"{len(transfers)} recharge(s) via coopération, "
                    f"{unserved} véhicule(s) non servi(s)"
                ),
                "level": "warning" if unserved else ("success" if transfers else "info"),
            })
        # Round summaries are real, but remain held in orchestrator RAM.
        # No lifetime KPI is claimed: after a restart, the history is empty.
        latest = hist[-1] if hist else None
        last_round = None
        if latest is not None and "arrivals_requested" in latest:
            last_round = {
                "tick": latest["tick"],
                "arrivals_requested": latest["arrivals_requested"],
                "locally_started": latest["locally_started"],
                "remote_started": latest["remote_started"],
                "auto_completed": latest.get("auto_completed", 0),
                "explicit_completed": latest.get("explicit_completed", 0),
                "unserved": latest["total_unserved"],
            }
        recorded = bool(stats and stats.get("recorded_rounds", 0) > 0)
        complete = bool(recorded and stats["complete"])
        # Le dernier round peut être perdu en RAM après un restart de l'orchestrateur.
        # On le restitue alors à partir du journal PostgreSQL si le tick est enregistré.
        if last_round is None and recorded:
            recent_points = [p for p in stats.get("points", []) if p.get("metrics") is not None]
            if recent_points and recent_points[-1]["tick"] == tick:
                last = recent_points[-1]
                last_round = {"tick": last["tick"], **last["metrics"]}

        if recorded:
            # Les événements sont reconstitués depuis PostgreSQL, survivent au restart.
            # Les anciens points sans metrics ne deviennent jamais de faux événements.
            events = []
            for point in reversed(stats.get("points", [])):
                metrics = point.get("metrics")
                if metrics is None:
                    continue
                n = point["tick"]
                events.append({
                    "id": f"round-{n}", "tick": n, "time": f"Tick {n}",
                    "category": "COOPERATION" if metrics["remote_started"] else "ROUND",
                    "title": f"Tick {n} — round enregistré",
                    "message": (
                        f"{metrics['arrivals_requested']} arrivée(s), "
                        f"{metrics['remote_started']} recharge(s) distante(s), "
                        f"{metrics['auto_completed'] + metrics['explicit_completed']} fin(s), "
                        f"{metrics['unserved']} non servi(s)"
                    ),
                    "level": "warning" if metrics["unserved"] else (
                        "success" if metrics["remote_started"] else "info"
                    ),
                })
                if len(events) == 20:
                    break
        totals = stats.get("totals", {}) if recorded else {}
        return {
            "last_round": last_round,
            "statistics_scope": ("persistent_full_run" if complete else
                                 "persistent_partial_run" if recorded else
                                 "unavailable_or_not_yet_recorded"),
            "simulation_run_id": orch.get("simulation_run_id"),
            "is_running": False,
            "interval_sec": 0,
            "tick": tick,
            "scenario": "S1_NORMAL",
            # Les totaux 'tout le run' restent INCONNUS quand la couverture est partielle.
            "total_arrivals": totals.get("arrivals_requested") if complete else None,
            "active_sessions_count": sum(c["occupied_piles"] for c in capacities.values()),
            "total_completed": (totals.get("auto_completed", 0) + totals.get("explicit_completed", 0)) if complete else None,
            "total_cooperations": totals.get("remote_started") if complete else None,
            "total_unserved": totals.get("unserved") if complete else None,
            "stats_available": recorded,
            "historical_stats": stats,
            "manual_only": True,
            "history_pending_tick": orch.get("history_pending_tick"),
            "capacities": capacities,
            "recent_events": events,
        }


@app.get("/health")
def health():
    return {"status": "healthy", "service": "dashboard-gateway"}


@app.get("/api/v1/simulator/status")
def simulator_status():
    try:
        return _status()
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        raise HTTPException(status_code=503, detail=f"Données réelles indisponibles : {exc}") from exc


def _validate_browser_action(origin: str | None, x_requested_with: str | None) -> None:
    # This gateway is restricted to localhost by Docker/WSL. These headers
    # mitigate accidental cross-site calls; they are NOT user authentication.
    if origin is not None and origin not in ALLOWED_ORIGINS:
        raise HTTPException(status_code=403, detail="Origine non autorisée")
    if x_requested_with != "XMLHttpRequest":
        raise HTTPException(status_code=403, detail="En-tête X-Requested-With requis")
    if not ORCHESTRATOR_API_TOKEN:
        raise HTTPException(status_code=503, detail="Jeton orchestrateur non configuré")


def _execute_real_round(arrivals: dict) -> dict:
    """One real round. Never retry after an uncertain network outcome."""
    if not _action_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="Une commande de simulation est déjà en cours")
    try:
        before = _status()
        if before["history_pending_tick"] is not None:
            raise HTTPException(status_code=409, detail="Historique en attente : réconcilier avant un nouveau tick")
        with httpx.Client(timeout=120) as client:
            response = client.post(
                f"{ORCHESTRATOR_URL}/api/v1/orchestrator/step",
                headers={"X-Orchestrator-Token": ORCHESTRATOR_API_TOKEN},
                json={"arrivals": arrivals, "completions": {}},
            )
            if response.status_code >= 400:
                raise HTTPException(status_code=response.status_code,
                                    detail=f"Orchestrateur : {response.text[:400]}")
            completed = response.json()
        if completed.get("history_sync", {}).get("status") == "pending":
            raise HTTPException(status_code=409, detail="Tick exécuté, historique en attente : NE PAS relancer le tick")
        return _status()
    except HTTPException:
        raise
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        raise HTTPException(status_code=503, detail=f"Résultat du round incertain : {exc}. Ne pas réessayer sans vérification.") from exc
    finally:
        _action_lock.release()


@app.post("/api/v1/simulator/step")
def simulator_step(origin: str | None = Header(default=None),
                   x_requested_with: str | None = Header(default=None, alias="X-Requested-With")):
    _validate_browser_action(origin, x_requested_with)
    return _execute_real_round({})


@app.post("/api/v1/simulator/inject")
def simulator_inject(
    request: InjectRequest,
    origin: str | None = Header(default=None),
    x_requested_with: str | None = Header(default=None, alias="X-Requested-With"),
):
    """Inject REAL vehicles into one Fog during one real PREPARE/SETTLE round.

    Does not choose peers: each Fog negotiates directly if overloaded.
    No automatic retries or background loop are performed.
    """
    _validate_browser_action(origin, x_requested_with)
    vehicles = [
        {"vehicle_id": f"dashboard-{request.fog_id}-{uuid.uuid4().hex}",
         "required_power_kw": 22.0,
         "duration_ticks": request.duration_ticks if request.duration_ticks is not None else (i % 4) + 1}
        for i in range(request.count)
    ]
    return _execute_real_round({request.fog_id: vehicles})

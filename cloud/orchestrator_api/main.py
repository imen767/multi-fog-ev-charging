"""API Cloud : pilotage du code d'orchestration existant + historique des SETTLE.

L'orchestrateur ne manipule ni SQLite ni PostgreSQL directement.
L'historique est capture via des API HTTP, puis pousse vers Cloud Sync API.
"""
import os
import secrets
import threading
from uuid import UUID
from typing import Literal

import httpx
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from simulator.cooperative_orchestrator import FogCooperativeRoundOrchestrator

FogId = Literal["fog-a", "fog-b", "fog-c"]
FOG_URLS = {
    fog: os.environ.get(f"FOG_{fog[-1].upper()}_URL", f"http://{fog}:8000")
    for fog in ("fog-a", "fog-b", "fog-c")
}
ORCHESTRATOR_API_TOKEN = os.environ.get("ORCHESTRATOR_API_TOKEN", "")
CLOUD_HISTORY_URL = os.environ.get("CLOUD_HISTORY_URL", "")
CLOUD_HISTORY_TOKEN = os.environ.get("CLOUD_HISTORY_TOKEN", "")
CLOUD_HISTORY_STATUS_URL = os.environ.get("CLOUD_HISTORY_STATUS_URL", "")
SIMULATION_RUN_ID = os.environ.get("SIMULATION_RUN_ID", "")


def _run_id() -> str:
    try:
        return str(UUID(SIMULATION_RUN_ID))
    except (ValueError, TypeError):
        raise ValueError("SIMULATION_RUN_ID missing/invalid; never generate it on restart")

orchestrator = FogCooperativeRoundOrchestrator(FOG_URLS)
round_lock = threading.Lock()
# Prototype : files d'attente ET index des ticks en RAM, donc pas resilients aux crashs.
_bootstrapped = False
_pending_snapshot: dict | None = None
app = FastAPI(title="Multi-Fog Cloud Orchestrator", version="0.2.0")


class VehicleArrival(BaseModel):
    vehicle_id: str = Field(min_length=1)
    required_power_kw: float = Field(default=22.0, gt=0)
    duration_ticks: int = Field(default=2, ge=1, le=48)


class RoundRequest(BaseModel):
    arrivals: dict[FogId, list[VehicleArrival]] = Field(default_factory=dict)
    completions: dict[FogId, list[str]] = Field(default_factory=dict)
    ml_predictions: dict[FogId, int] = Field(default_factory=dict)


def _authorized(token: str | None) -> bool:
    return bool(ORCHESTRATOR_API_TOKEN and token and secrets.compare_digest(token, ORCHESTRATOR_API_TOKEN))


def _fog_round_statuses() -> dict[str, dict]:
    with httpx.Client(timeout=10) as client:
        statuses = {}
        for fog_id, url in FOG_URLS.items():
            response = client.get(f"{url}/api/v1/round/status")
            response.raise_for_status()
            result = response.json()
            if result.get("fog_id") != fog_id:
                raise ValueError(f"Fog identity mismatch: {fog_id}")
            statuses[fog_id] = result
        return statuses


def _history_probe(tick: int) -> dict:
    if not CLOUD_HISTORY_URL or not CLOUD_HISTORY_TOKEN:
        raise ValueError("Cloud history API/token not configured")
    url = CLOUD_HISTORY_STATUS_URL or CLOUD_HISTORY_URL.replace(
        "/api/v1/history/round", "/api/v1/history/run-status"
    )
    with httpx.Client(timeout=10) as client:
        response = client.get(url, params={"tick": tick},
                              headers={"X-Orchestrator-Token": CLOUD_HISTORY_TOKEN})
        response.raise_for_status()
        data = response.json()
    if data.get("simulation_run_id") != _run_id():
        raise ValueError("History API run id differs from orchestrator run id")
    return data


def _bootstrap_tick() -> None:
    """Recover Fog tick, then detect missing persisted snapshot BEFORE allowing a new round.

    A restart must not silently skip a failed capture or replay PREPARE/SETTLE.
    """
    global _bootstrapped, _pending_snapshot
    if _bootstrapped:
        return
    _run_id()  # Fail closed when no stable run id exists.
    states = _fog_round_statuses()
    ticks = {s["tick"] for s in states.values()}
    if len(ticks) != 1 or any(s["phase"] == "PREPARED" for s in states.values()):
        raise ValueError("Fog rounds are not aligned; manual reconciliation required")
    tick = ticks.pop()
    if tick > 0 and any(s["phase"] != "SETTLED" for s in states.values()):
        raise ValueError("Fog checkpoint has invalid phase")
    if CLOUD_HISTORY_URL:
        journal = _history_probe(tick)
        latest = int(journal["latest_tick"])
        if latest > tick:
            raise ValueError(f"Cloud run is at tick {latest} but Fogs are only at {tick}")
        if latest < tick - 1:
            raise ValueError(f"Missing historical ticks 1..{tick - 1} in this run; manual reconciliation required")
        if journal["tick_status"] == "partial":
            raise ValueError("Partial history write: inspect DB before continuing")
        if tick > 0 and journal["tick_status"] == "missing":
            _pending_snapshot = {"tick": tick, "payload": None}
    orchestrator.current_tick = tick
    _bootstrapped = True


def _collect_snapshot(tick: int, metrics: dict | None = None) -> dict:
    """Lecture synchrone des compteurs APRES les SETTLE des 3 Fog.

    Pas de lecture de pile_states : les sync agents tournent de facon asynchrone.
    """
    snapshots = []
    with httpx.Client(timeout=10) as client:
        for fog_id, url in FOG_URLS.items():
            phase = client.get(f"{url}/api/v1/round/status")
            phase.raise_for_status()
            phase_data = phase.json()
            if phase_data.get("fog_id") != fog_id or phase_data.get("tick") != tick or phase_data.get("phase") != "SETTLED":
                raise ValueError(f"Fog {fog_id} not SETTLED on tick {tick}")

            cap = client.get(f"{url}/api/v1/piles/capacity")
            cap.raise_for_status()
            data = cap.json()
            if data.get("fog_id") != fog_id:
                raise ValueError(f"Unexpected capacity response for {fog_id}")
            snapshots.append({
                "fog_id": fog_id,
                "total_piles": data["total_piles"],
                "occupied_piles": data["occupied_piles"],
                "reservable_piles": data["available_piles"],
            })
    payload = {"simulation_run_id": _run_id(), "tick": tick, "snapshots": snapshots}
    if metrics is not None:
        payload["metrics"] = metrics
    return payload


def _send_snapshot(payload: dict) -> dict:
    if not CLOUD_HISTORY_URL or not CLOUD_HISTORY_TOKEN:
        raise ValueError("Cloud history API not configured")
    with httpx.Client(timeout=10) as client:
        response = client.post(
            CLOUD_HISTORY_URL,
            json=payload,
            headers={"X-Orchestrator-Token": CLOUD_HISTORY_TOKEN},
        )
        response.raise_for_status()
        return response.json()


@app.get("/health")
def health():
    return {"status": "healthy", "service": "cloud-orchestrator"}


@app.get("/api/v1/orchestrator/status")
def status():
    try:
        _bootstrap_tick()
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        raise HTTPException(status_code=503, detail=f"Cannot recover orchestrator: {exc}") from exc
    return {
        "simulation_run_id": _run_id(),
        "current_tick": orchestrator.current_tick,
        "completed_rounds": len(orchestrator.history),
        "fogs": list(FOG_URLS),
        "history_capture_enabled": bool(CLOUD_HISTORY_URL),
        "history_pending_tick": _pending_snapshot["tick"] if _pending_snapshot else None,
    }


@app.get("/api/v1/orchestrator/history")
def history():
    return {"rounds": orchestrator.history[-20:]}


@app.post("/api/v1/orchestrator/step")
def step(body: RoundRequest, x_orchestrator_token: str | None = Header(default=None, alias="X-Orchestrator-Token")):
    global _pending_snapshot
    if not _authorized(x_orchestrator_token):
        raise HTTPException(status_code=401, detail="Invalid orchestrator token")
    if not round_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="A round is already in progress")
    try:
        try:
            _bootstrap_tick()
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            raise HTTPException(status_code=409, detail=f"Cannot align Fog ticks: {exc}") from exc
        if _pending_snapshot is not None:
            raise HTTPException(status_code=409, detail="Historical capture pending: use /retry-history; do not advance tick")

        # Logique PREPARE/SETTLE originale non modifiee.
        try:
            result = orchestrator.run_round(
                arrivals={fog: [v.model_dump() for v in values] for fog, values in body.arrivals.items()},
                completions=body.completions,
                ml_predictions=body.ml_predictions or None,
            )
        except (httpx.HTTPError, AssertionError, ValueError, KeyError) as exc:
            raise HTTPException(
                status_code=503,
                detail=f"Round incomplete; check Fog /api/v1/round/status before retrying: {type(exc).__name__}: {exc}",
            ) from exc

        if not CLOUD_HISTORY_URL:
            return {**result, "history_sync": {"status": "disabled"}}

        # Memoriser le tick avant d'essayer : ne pas rejouer SETTLE en cas d'erreur Cloud.
        _pending_snapshot = {"tick": result["tick"], "payload": None}
        try:
            # Le résumé provient des réponses PREPARE/SETTLE des Fog.
            # Les mocks/anciens orchestrateurs dépourvus de métriques ne reçoivent
            # PAS de statistiques inventées (compatibilité reprise historique).
            metrics = None
            required = ("arrivals_requested", "locally_started", "remote_started",
                        "auto_completed", "explicit_completed", "total_unserved")
            if all(k in result for k in required):
                metrics = {k: int(result[source]) for k, source in (
                    ("arrivals_requested", "arrivals_requested"),
                    ("locally_started", "locally_started"),
                    ("remote_started", "remote_started"),
                    ("auto_completed", "auto_completed"),
                    ("explicit_completed", "explicit_completed"),
                    ("unserved", "total_unserved"),
                )}
            payload = _collect_snapshot(result["tick"], metrics) if metrics is not None else _collect_snapshot(result["tick"])
            _pending_snapshot["payload"] = payload
            confirmation = _send_snapshot(payload)
            _pending_snapshot = None
            return {**result, "history_sync": confirmation}
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            return {**result, "history_sync": {"status": "pending", "reason": str(exc)}}
    finally:
        round_lock.release()


@app.post("/api/v1/orchestrator/retry-history")
def retry_history(x_orchestrator_token: str | None = Header(default=None, alias="X-Orchestrator-Token")):
    global _pending_snapshot
    if not _authorized(x_orchestrator_token):
        raise HTTPException(status_code=401, detail="Invalid orchestrator token")
    if not round_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="A round is already in progress")
    try:
        try:
            _bootstrap_tick()
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            raise HTTPException(status_code=409, detail=f"Cannot align Fog ticks: {exc}") from exc
        if _pending_snapshot is None:
            return {"status": "nothing_pending", "simulation_run_id": _run_id()}
        try:
            if _pending_snapshot["payload"] is None:
                _pending_snapshot["payload"] = _collect_snapshot(_pending_snapshot["tick"])
            confirmation = _send_snapshot(_pending_snapshot["payload"])
            _pending_snapshot = None
            return confirmation
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            raise HTTPException(status_code=503, detail=f"Historical capture still pending: {exc}") from exc
    finally:
        round_lock.release()

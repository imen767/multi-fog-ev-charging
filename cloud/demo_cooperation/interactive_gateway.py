"""Interactive, repeatable *isolated* Multi-Fog cooperation lab.

All charging and peer selection use the actual Fog FastAPI services. This
service only coordinates the PREPARE/SETTLE barrier and presents response
traces; it does not choose which Fog will accept a remote charging session.
No production ports, databases, or Cloud endpoints are reachable from here.

For a failed/ambiguous HTTP step, do not automatically retry: PREPARE or
ACTIVATE might already be committed. Require an explicit *demo-only* reset.
"""
from __future__ import annotations

import os
import threading
from uuid import uuid4
from typing import Any, Literal

import httpx
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from simulator.cooperative_orchestrator import FogCooperativeRoundOrchestrator

FOG_IDS = ("fog-a", "fog-b", "fog-c")
FOG_URLS = {fog: f"http://demo-{fog}:8000" for fog in FOG_IDS}
EXPECTED = {"fog-a": 2, "fog-b": 1, "fog-c": 3}
ALLOWED_ORIGINS = {"http://localhost:5173", "http://127.0.0.1:5173"}

app = FastAPI(title="Interactive Multi-Fog Cooperation Laboratory", version="2.0")
_guard = threading.Lock()
_lab_run_id = str(uuid4())
_last_round: dict[str, Any] | None = None
_round_history: list[dict[str, Any]] = []
_events: list[dict[str, Any]] = []
_operations: dict[str, dict[str, Any]] = {}
_halted: str | None = None


class StepRequest(BaseModel):
    fog_id: Literal["fog-a", "fog-b", "fog-c"] = "fog-b"
    count: int = Field(default=0, ge=0, le=6)
    duration_ticks: int = Field(default=3, ge=1, le=12)
    required_power_kw: float = Field(default=22.0, gt=0, le=50)
    expected_tick: int = Field(ge=0)
    operation_id: str = Field(min_length=8, max_length=128)


class ResetRequest(BaseModel):
    expected_tick: int = Field(ge=0)


def _ensure_demo_mode():
    if os.getenv("DEMO_MODE") != "1":
        raise HTTPException(status_code=403, detail="This endpoint requires an isolated demo environment")


def _authorize_write(origin: str | None, requested_with: str | None):
    _ensure_demo_mode()
    if origin and origin not in ALLOWED_ORIGINS:
        raise HTTPException(status_code=403, detail="Origin not allowed")
    if requested_with != "XMLHttpRequest":
        raise HTTPException(status_code=403, detail="X-Requested-With header required")


def _snapshot(client: httpx.Client) -> tuple[dict, dict, int]:
    capacities, fog_rounds = {}, {}
    for fog, base in FOG_URLS.items():
        capacity = client.get(base + "/api/v1/piles/capacity")
        capacity.raise_for_status()
        cp = capacity.json()
        if cp.get("fog_id") != fog or cp.get("total_piles") != EXPECTED[fog]:
            raise ValueError(f"Untrusted/non-disposable catalogue at {fog}: {cp.get('total_piles')}")
        state_response = client.get(base + "/api/v1/round/status")
        state_response.raise_for_status()
        state = state_response.json()
        if state.get("fog_id") != fog or not isinstance(state.get("tick"), int):
            raise ValueError(f"Invalid round status from {fog}")
        capacities[fog] = cp
        fog_rounds[fog] = state
    ticks = {item["tick"] for item in fog_rounds.values()}
    phases = {item["phase"] for item in fog_rounds.values()}
    if len(ticks) != 1 or phases - {"IDLE", "SETTLED"}:
        raise ValueError(f"Non-recoverable or in-progress Fog round state: {fog_rounds}")
    tick = next(iter(ticks))
    if tick == 0 and phases != {"IDLE"}:
        raise ValueError(f"Inconsistent initial demo phases: {phases}")
    if tick > 0 and phases != {"SETTLED"}:
        raise ValueError(f"Inconsistent settled demo phases: {phases}")
    return capacities, fog_rounds, tick


def _status() -> dict:
    error = _halted
    try:
        with httpx.Client(timeout=5.0) as client:
            capacities, fog_rounds, tick = _snapshot(client)
        ready = error is None
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        capacities, fog_rounds, tick = {}, {}, None
        error = error or f"Demo Fog unavailable or inconsistent: {type(exc).__name__}: {exc}"
        ready = False
    return {
        "isolated": True, "interactive": True, "ready": ready,
        "run_id": _lab_run_id, "tick": tick, "capacities": capacities,
        "fog_rounds": fog_rounds, "last_round": _last_round,
        "round_history": _round_history[-30:], "events": _events[-300:],
        "error": error, "reset_available": tick is not None and _halted is None,
    }


@app.get("/health")
def health():
    return {"status": "healthy", "isolated": True, "service": "demo-cooperation-api"}


@app.get("/api/v1/demo/cooperation/status")
def status():
    _ensure_demo_mode()
    return _status()


@app.post("/api/v1/demo/cooperation/step")
def step(
    payload: StepRequest,
    origin: str | None = Header(default=None),
    x_requested_with: str | None = Header(default=None, alias="X-Requested-With"),
):
    global _last_round, _halted
    _authorize_write(origin, x_requested_with)
    if not _guard.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="Another demo command is running")
    try:
        if _halted:
            raise HTTPException(status_code=409, detail=f"Demo halted: {_halted}. Reset only demo containers")
        # Same operation ID with identical payload never advances the tick twice.
        cached = _operations.get(payload.operation_id)
        if cached is not None:
            if cached["payload"] != payload.model_dump():
                raise HTTPException(status_code=409, detail="Operation ID used with different parameters")
            return _status()
        with httpx.Client(timeout=15.0) as client:
            _, _, tick = _snapshot(client)  # Verify 2/1/3 disposable Fog catalogues.
            if payload.expected_tick != tick:
                raise HTTPException(status_code=409, detail=f"Stale command: expected tick {payload.expected_tick}, current {tick}")
            next_tick = tick + 1
            vehicles = [{
                "vehicle_id": f"lab-{_lab_run_id[:8]}-t{next_tick}-{payload.fog_id}-{i}",
                "required_power_kw": payload.required_power_kw,
                "duration_ticks": payload.duration_ticks,
            } for i in range(payload.count)]
            orchestrator = FogCooperativeRoundOrchestrator(FOG_URLS, client=client)
            orchestrator.current_tick = tick  # Recover from isolated Fog checkpoints.
            try:
                result = orchestrator.run_round({payload.fog_id: vehicles}, {})
                if result["tick"] != next_tick:
                    raise ValueError("Orchestrator returned unexpected tick")
                # Check actual destination SQLite before claiming a successful transfer.
                verified = []
                for transfer in result.get("transfers", []):
                    target = transfer["to_fog"]
                    r = client.get(FOG_URLS[target] + "/api/v1/piles")
                    r.raise_for_status()
                    rows = r.json()
                    expected_ids = set(transfer.get("pile_ids", []))
                    matching = [p for p in rows if p.get("pile_id") in expected_ids
                                and p.get("status") == "OCCUPIED"
                                and p.get("reservation_status") == "ACTIVE"]
                    if not expected_ids or len(matching) != len(expected_ids):
                        raise ValueError(f"Remote ACTIVATE not verified in {target} SQLite")
                    verified.append(transfer["hold_id"])
                _, _, observed_tick = _snapshot(client)
                if observed_tick != next_tick:
                    raise ValueError("Fog ticks diverged after round")
            except Exception as exc:
                _halted = f"Ambiguous/failed tick {next_tick}: {type(exc).__name__}: {exc}"
                raise HTTPException(status_code=503, detail=_halted) from exc
            summary = {
                "tick": result["tick"], "fog_id": payload.fog_id, "count": payload.count,
                "locally_started": result["locally_started"],
                "remote_started": result["remote_started"],
                "auto_completed": result["auto_completed"],
                "unserved": result["total_unserved"],
                "transfers": result["transfers"],
                "verified_hold_ids": verified,
            }
            _last_round = summary
            _round_history.append(summary)
            # These are received trace records from actual Fog HTTP responses.
            for e in result.get("cooperation_events", []):
                _events.append({**e, "tick": next_tick})
            _operations[payload.operation_id] = {"payload": payload.model_dump(), "tick": next_tick}
        return _status()
    except HTTPException:
        raise
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        # Snapshot/read failures happen before mutation: do not automatically halt.
        raise HTTPException(status_code=503, detail=f"Demo service unavailable: {type(exc).__name__}: {exc}") from exc
    finally:
        _guard.release()


@app.post("/api/v1/demo/cooperation/reset")
def reset(
    payload: ResetRequest,
    origin: str | None = Header(default=None),
    x_requested_with: str | None = Header(default=None, alias="X-Requested-With"),
):
    global _lab_run_id, _last_round, _halted
    _authorize_write(origin, x_requested_with)
    if not _guard.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="Another demo command is running")
    try:
        with httpx.Client(timeout=10.0) as client:
            _, _, current_tick = _snapshot(client)
            if payload.expected_tick != current_tick:
                raise HTTPException(status_code=409, detail="Stale reset; refresh the demo status")
            reset_token = os.getenv("DEMO_RESET_TOKEN", "")
            if not reset_token:
                raise HTTPException(status_code=503, detail="Missing isolated demo reset token")
            for fog, base in FOG_URLS.items():
                try:
                    response = client.post(base + "/api/v1/demo/reset",
                                           headers={"X-Demo-Reset-Token": reset_token})
                    response.raise_for_status()
                    data = response.json()
                    if data.get("fog_id") != fog or data.get("reset") is not True:
                        raise ValueError(f"Invalid reset acknowledgement from {fog}")
                except (httpx.HTTPError, ValueError, KeyError) as exc:
                    _halted = f"Partial demo reset: {fog}: {exc}; recreate only demo containers"
                    raise HTTPException(status_code=503, detail=_halted) from exc
            _, _, new_tick = _snapshot(client)
            if new_tick != 0:
                _halted = "Demo reset not aligned: recreate only demo containers"
                raise HTTPException(status_code=503, detail=_halted)
            _lab_run_id = str(uuid4())
            _last_round = None
            _round_history.clear()
            _events.clear()
            _operations.clear()
            _halted = None
        return _status()
    except HTTPException:
        raise
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        raise HTTPException(status_code=503, detail=f"Demo reset unavailable: {type(exc).__name__}: {exc}") from exc
    finally:
        _guard.release()

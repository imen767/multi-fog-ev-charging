"""One-shot *real HTTP* isolated cooperation demo for the React presentation.

Separate Docker Compose project `fog-cooperation-demo`, no production Cloud,
SQLite volumes or orchestrator. HTTP messages are instrumented at the requesting
Fog (not synthesized from the UI). The demo is intentionally non-repeatable
until the isolated containers are recreated; this avoids double bookings.
"""
from __future__ import annotations

import os
import threading
from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException
from simulator.cooperative_orchestrator import FogCooperativeRoundOrchestrator

FOG_IDS = ("fog-a", "fog-b", "fog-c")
FOG_URLS = {fog: f"http://demo-{fog}:8000" for fog in FOG_IDS}
ALLOWED_ORIGINS = {"http://localhost:5173", "http://127.0.0.1:5173"}
app = FastAPI(title="Isolated Multi-Fog Cooperation Demonstrator", version="1.0")
_guard = threading.Lock()
_attempted = False
_result: dict[str, Any] | None = None
_failure: str | None = None


def _capacities(client: httpx.Client) -> dict:
    results = {}
    for fog_id, base in FOG_URLS.items():
        res = client.get(base + "/api/v1/piles/capacity")
        res.raise_for_status()
        data = res.json()
        if data.get("fog_id") != fog_id:
            raise ValueError(f"Invalid response from {fog_id}")
        results[fog_id] = data
    return results


def _assert_disposable(capacities: dict) -> None:
    expected = {"fog-a": 2, "fog-b": 1, "fog-c": 3}
    got = {fog: capacities[fog]["total_piles"] for fog in FOG_IDS}
    if got != expected:
        raise ValueError(f"Demo refuses non-disposable Fog catalogues: {got}")


def _read_status() -> dict:
    try:
        with httpx.Client(timeout=5.0) as client:
            capacity = _capacities(client)
        _assert_disposable(capacity)
        online = True
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        capacity = {}
        online = False
        err = f"Fog de démonstration indisponibles : {type(exc).__name__}"
    return {
        "isolated": True,
        "ready": online and not _attempted,
        "attempted": _attempted,
        "result": _result,
        "error": _failure if _attempted else (None if online else err),
        "capacities": capacity,
        "restart_required": _attempted,
    }


@app.get("/health")
def health():
    return {"status": "healthy", "isolated": True, "service": "demo-cooperation-api"}


@app.get("/api/v1/demo/cooperation/status")
def status():
    return _read_status()


@app.post("/api/v1/demo/cooperation/run")
def run(
    origin: str | None = Header(default=None),
    x_requested_with: str | None = Header(default=None, alias="X-Requested-With"),
):
    global _attempted, _result, _failure
    if origin and origin not in ALLOWED_ORIGINS:
        raise HTTPException(status_code=403, detail="Origin not allowed")
    if x_requested_with != "XMLHttpRequest":
        raise HTTPException(status_code=403, detail="X-Requested-With header required")
    if not _guard.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="Demo already running")
    try:
        if _attempted:
            raise HTTPException(status_code=409, detail="Demo already executed; recreate only demo containers")
        # Test isolation BEFORE calling any mutation endpoint.
        with httpx.Client(timeout=10.0) as client:
            initial = _capacities(client)
            _assert_disposable(initial)
            if any(initial[f]["occupied_piles"] != 0 for f in FOG_IDS):
                raise HTTPException(status_code=409, detail="Demo not fresh: recreate the demo containers")
            if initial["fog-b"]["available_piles"] != 1:
                raise HTTPException(status_code=409, detail="Fog B should have exactly one free pile")

            # Once a PREPARE is submitted, a timeout might be ambiguous.
            # Never retry a partially executed simulation automatically.
            _attempted = True
            orch = FogCooperativeRoundOrchestrator(FOG_URLS, client=client)
            local = orch.run_round({"fog-b": [{"vehicle_id": "presentation-local-B",
                                                "required_power_kw": 22.0,
                                                "duration_ticks": 4}]}, {})
            if local.get("remote_started") != 0 or local.get("locally_started") != 1:
                raise ValueError("First vehicle should be served locally by B")
            after_local = _capacities(client)
            if after_local["fog-b"]["available_piles"] != 0:
                raise ValueError("Fog B must be saturated before round two")

            distant = orch.run_round({"fog-b": [{"vehicle_id": "presentation-remote-B",
                                                  "required_power_kw": 22.0,
                                                  "duration_ticks": 3}]}, {})
            if distant.get("remote_started") != 1 or distant.get("total_unserved") != 0:
                raise ValueError("Expected exactly one ACTIVE remote charging session")
            transfers = distant.get("transfers", [])
            if len(transfers) != 1 or transfers[0]["from_fog"] != "fog-b":
                raise ValueError("Invalid Fog-to-Fog transfer")
            receiver = transfers[0]["to_fog"]
            if receiver not in {"fog-a", "fog-c"}:
                raise ValueError("Unexpected receiving Fog")
            active = client.get(FOG_URLS[receiver] + "/api/v1/piles")
            active.raise_for_status()
            matched = [pile for pile in active.json() if
                       pile.get("pile_id") in transfers[0].get("pile_ids", []) and
                       pile.get("status") == "OCCUPIED" and pile.get("reservation_status") == "ACTIVE"]
            if len(matched) != 1:
                raise ValueError("Remote pile not ACTIVE in receiving Fog's SQLite")
            trace = distant.get("cooperation_events", [])
            stages = {e.get("stage") for e in trace}
            if not {"HELP_REQUEST", "HOLD_GRANTED", "CONFIRMED", "ACTIVE"} <= stages:
                raise ValueError("HTTP cooperation trace incomplete")
            final = _capacities(client)
            _result = {
                "request_id": next((e.get("request_id") for e in trace if e.get("request_id")), None),
                "local_round": {"tick": local["tick"], "locally_started": local["locally_started"]},
                "remote_round": {"tick": distant["tick"],
                                 "remote_started": distant["remote_started"],
                                 "unserved": distant["total_unserved"]},
                "transfer": transfers[0],
                "events": trace,
                "before": initial,
                "after_local": after_local,
                "after": final,
                "verified_active": True,
            }
            _failure = None
        return _read_status()
    except HTTPException:
        raise
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        _failure = f"{type(exc).__name__}: {exc}"
        raise HTTPException(status_code=503, detail=(
            "Demo incomplete; do not retry automatically. " + _failure
        )) from exc
    finally:
        _guard.release()

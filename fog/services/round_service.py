"""
Scenario 5 — PREPARE / SETTLE distributed round service.

Each Fog node exposes two endpoints to participate in the synchronised round:

  POST /api/v1/round/prepare  — PREPARE phase:
      Processes local vehicle arrivals and charging completions.
      Returns the list of unserved vehicles that overflowed local capacity.

  POST /api/v1/round/settle   — SETTLE phase (called after the global barrier):
      Accepts remote vehicles transferred by the orchestrator and
      (optionally) triggers peer-cooperation for remaining unserved vehicles.

  GET  /api/v1/round/status   — returns current round phase and capacities.

This replaces the previous in-memory FogNodeState simulation:
the three Fog processes now communicate through real HTTP calls, so
PREPARE and SETTLE run on each node's real SQLite database.
"""

import threading
import json
import os
from pathlib import Path
from datetime import datetime, timezone
from typing import Literal

from fog.repositories.pile_repository import get_pile_capacity
from fog.repositories.reservation_repository import create_local_session, complete_hold, list_due_hold_ids
from fog.settings import FOG_ID, DB_PATH


# ---------------------------------------------------------------------------
# In-process round state (per Fog node, cleared every new round)
# ---------------------------------------------------------------------------

_lock = threading.Lock()

# This file lives in the same persistent bind mount as the Fog's SQLite DB.
_CHECKPOINT_PATH = Path(os.getenv(
    "FOG_ROUND_CHECKPOINT_PATH", str(Path(DB_PATH).parent / ".round_checkpoint.json")
))


def _read_round_checkpoint() -> tuple[int, str]:
    if not _CHECKPOINT_PATH.exists():
        return 0, "IDLE"
    data = json.loads(_CHECKPOINT_PATH.read_text(encoding="utf-8"))
    if data.get("phase") != "SETTLED" or not isinstance(data.get("tick"), int) or data["tick"] < 0:
        raise ValueError(f"Invalid round checkpoint in {_CHECKPOINT_PATH}")
    return data["tick"], "SETTLED"


def _save_round_checkpoint(tick: int):
    _CHECKPOINT_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _CHECKPOINT_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps({"tick": tick, "phase": "SETTLED"}), encoding="utf-8")
    os.replace(tmp, _CHECKPOINT_PATH)


_initial_tick, _initial_phase = _read_round_checkpoint()
_round_state: dict = {
    "tick": _initial_tick,
    "phase": _initial_phase,           # IDLE | PREPARED | SETTLED
    "unserved_vehicles": [],   # vehicles that did not fit locally (from PREPARE)
    "unserved_requests": [],   # preserve vehicle power requirements
    "prepare_result": None,
    "settle_result": None,
    "served_sessions": [],     # sessions created this round
    "released_piles": [],      # piles freed during this round's PREPARE
}


def _get_state() -> dict:
    with _lock:
        return dict(_round_state)


def _update_state(**kwargs):
    with _lock:
        _round_state.update(kwargs)


# ---------------------------------------------------------------------------
# PREPARE
# ---------------------------------------------------------------------------

def prepare_round(
    tick: int,
    arrivals: list[dict],       # list of {vehicle_id, required_power_kw}
    completions: list[str],     # list of hold_ids to complete
) -> dict:
    """
    PREPARE phase: executed independently on each Fog node.

    1. Complete sessions in `completions` (release piles).
    2. Serve arriving vehicles locally up to available capacity.
    3. Record unserved vehicles for the SETTLE phase.

    Thread-safe; uses SQLite IMMEDIATE transaction for atomicity.
    """
    with _lock:
        # No double creation of local sessions on an accidental HTTP retry.
        if _round_state["tick"] == tick and _round_state["phase"] == "PREPARED":
            if _round_state["prepare_result"] is None:
                raise ValueError("PREPARE still in progress; wait for completion")
            return dict(_round_state["prepare_result"])
        if tick <= _round_state["tick"]:
            raise ValueError(f"PREPARE tick {tick} already processed")
        _round_state["tick"] = tick
        _round_state["phase"] = "PREPARED"
        _round_state["unserved_vehicles"] = []
        _round_state["unserved_requests"] = []
        _round_state["served_sessions"] = []
        _round_state["released_piles"] = []
        _round_state["prepare_result"] = None
        _round_state["settle_result"] = None

    released = []
    completed_hold_ids = []
    auto_completed = 0
    explicit_completed = 0
    # Step 1: terminate scheduled sessions BEFORE processing new arrivals.
    # The charging destination owns the SQLite record, including remote HOLDs.
    due_holds = list_due_hold_ids(tick)
    for hold_id in due_holds:
        result = complete_hold(hold_id, completed_tick=tick)
        if result and not result.get("already_completed", False):
            released.extend(result.get("pile_ids", []))
            completed_hold_ids.append(hold_id)
            auto_completed += 1

    # Explicit completions still work; they must not be counted twice.
    for hold_id in dict.fromkeys(completions):
        if hold_id in completed_hold_ids:
            continue
        result = complete_hold(hold_id, completed_tick=tick)
        if result and not result.get("already_completed", False):
            released.extend(result.get("pile_ids", []))
            completed_hold_ids.append(hold_id)
            explicit_completed += 1

    # Step 2: serve arriving vehicles locally
    served_sessions = []
    unserved = []
    unserved_requests = []

    for vehicle in arrivals:
        vehicle_id = vehicle.get("vehicle_id", "unknown")
        required_power_kw = vehicle.get("required_power_kw", 22.0)
        duration_ticks = vehicle.get("duration_ticks", 2)

        session = create_local_session(
            vehicle_id=vehicle_id,
            required_power_kw=required_power_kw,
            fog_id=FOG_ID,
            started_tick=tick,
            duration_ticks=duration_ticks,
        )
        if session:
            served_sessions.append(session)
        else:
            unserved.append(vehicle_id)
            unserved_requests.append({
                "vehicle_id": vehicle_id,
                "required_power_kw": required_power_kw,
                "duration_ticks": duration_ticks,
            })

    # Snapshot capacity after PREPARE
    cap = get_pile_capacity()

    result = {
        "fog_id": FOG_ID,
        "tick": tick,
        "status": "PREPARED",
        "served_sessions": served_sessions,
        "auto_completed": auto_completed,
        "explicit_completed": explicit_completed,
        "completed_hold_ids": completed_hold_ids,
        "unserved_vehicles": unserved,
        "released_piles": released,
        "available_piles": cap["available_piles"],
        "occupied_piles": cap["occupied_piles"],
    }
    _update_state(
        unserved_vehicles=unserved,
        unserved_requests=unserved_requests,
        served_sessions=served_sessions,
        released_piles=released,
        prepare_result=result,
    )
    return result


# ---------------------------------------------------------------------------
# SETTLE
# ---------------------------------------------------------------------------

def settle_round(
    tick: int,
    remote_transfers: list[dict],
    trigger_cooperation: bool = True,
) -> dict:
    """SETTLE after the PREPARE barrier; each Fog owns its peer selection.

    Existing remote_transfers support is retained for backwards compatibility,
    but the Cloud's new cooperative orchestrator ALWAYS sends an empty list.
    """
    with _lock:
        if _round_state["tick"] != tick or _round_state["phase"] not in ("PREPARED", "SETTLED"):
            raise ValueError(f"Fog {FOG_ID} not PREPARED for tick {tick}")
        if _round_state["phase"] == "SETTLED":
            if _round_state["settle_result"] is None:
                raise ValueError("Already SETTLED; result unavailable after restart")
            return dict(_round_state["settle_result"])

    accepted_transfers = []
    for vehicle in remote_transfers:
        session = create_local_session(
            vehicle_id=vehicle.get("vehicle_id", "unknown"),
            required_power_kw=vehicle.get("required_power_kw", 22.0),
            fog_id=vehicle.get("origin_fog", FOG_ID),
            started_tick=tick,
            duration_ticks=vehicle.get("duration_ticks", 2),
        )
        if session:
            accepted_transfers.append(session)

    state = _get_state()
    remaining_unserved = list(state.get("unserved_vehicles", []))
    cooperation_results = []

    if trigger_cooperation and remaining_unserved:
        from shared.schemas import HelpRequest
        from fog.services.peer_communication_service import (
            send_help_request_to_peers, confirm_and_activate_remote_hold,
        )

        # One HOLD per vehicle, not one HOLD for several vehicles: this lets
        # the destination Fog complete each charging session independently.
        for index, vehicle in enumerate(state.get("unserved_requests", [])):
            request = HelpRequest(
                request_id=f"round-{tick}-{FOG_ID}-{index}",
                origin_fog=FOG_ID,
                required_piles=1,
                required_power_kw=vehicle["required_power_kw"],
            )
            negotiation = send_help_request_to_peers(request)
            if negotiation.get("status") == "HOLD_GRANTED":
                completed = confirm_and_activate_remote_hold(
                    negotiation, started_tick=tick,
                    duration_ticks=vehicle.get("duration_ticks", 2),
                    vehicle_id=vehicle["vehicle_id"],
                )
            else:
                completed = {"status": "UNSERVED"}

            entry = {
                "vehicle_id": vehicle["vehicle_id"],
                "request_id": request.request_id,
                "negotiation_status": negotiation.get("status"),
                **completed,
            }
            cooperation_results.append(entry)
            if completed["status"] == "ACTIVE":
                remaining_unserved.remove(vehicle["vehicle_id"])

    cap = get_pile_capacity()
    result = {
        "fog_id": FOG_ID,
        "tick": tick,
        "status": "SETTLED",
        "cooperation_results": cooperation_results,
        "accepted_transfers": accepted_transfers,
        "remaining_unserved": remaining_unserved,
        "available_piles": cap["available_piles"],
        "occupied_piles": cap["occupied_piles"],
    }
    _update_state(
        phase="SETTLED", unserved_vehicles=remaining_unserved,
        settle_result=result,
    )
    _save_round_checkpoint(tick)
    return result


# ---------------------------------------------------------------------------
# STATUS
# ---------------------------------------------------------------------------

def get_round_status() -> dict:
    """Return the current round phase and live capacity for this Fog node."""
    cap = get_pile_capacity()
    state = _get_state()
    return {
        "fog_id": FOG_ID,
        "tick": state["tick"],
        "phase": state["phase"],
        "available_piles": cap["available_piles"],
        "occupied_piles": cap["occupied_piles"],
        "unserved_vehicles": state["unserved_vehicles"],
    }

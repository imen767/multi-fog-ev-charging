"""Reset endpoint limited to *disposable* mini-Fog Docker containers.

Not included in the production Fog API unless DEMO_ISOLATED_FOG=1. Even when
included it checks the database path and exact 2/1/3 demo catalogue sizes.
The endpoint is used only for a fresh presentation run, never for the real Fog.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from fastapi import APIRouter, Header, HTTPException

from fog.settings import DB_PATH, FOG_ID
from fog.services import round_service

router = APIRouter(prefix="/api/v1/demo", tags=["Isolated demo control"])
EXPECTED = {"fog-a": 2, "fog-b": 1, "fog-c": 3}


def assert_disposable_configuration() -> None:
    db_path = Path(DB_PATH)
    expected_path = Path("/app/data") / f"{FOG_ID.replace('-', '_')}.db"
    if (os.getenv("DEMO_ISOLATED_FOG") != "1"
            or os.getenv("DEMO_MODE") != "1"
            or FOG_ID not in EXPECTED
            or db_path != expected_path):
        raise HTTPException(status_code=403, detail="Reset forbidden outside isolated demo")


@router.post("/reset")
def reset_demo(x_demo_reset_token: str | None = Header(default=None, alias="X-Demo-Reset-Token")):
    assert_disposable_configuration()
    token = os.getenv("DEMO_RESET_TOKEN", "")
    if not token or x_demo_reset_token != token:
        raise HTTPException(status_code=403, detail="Invalid demo reset token")
    # No concurrent PREPARE/SETTLE inside this particular Fog.
    with round_service._lock:
        if round_service._round_state["phase"] == "PREPARED":
            raise HTTPException(status_code=409, detail="Cannot reset during PREPARE")
        with sqlite3.connect(DB_PATH, timeout=10) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                total = conn.execute("SELECT COUNT(*) FROM piles").fetchone()[0]
                if total != EXPECTED[FOG_ID]:
                    raise HTTPException(status_code=403, detail="Non-demo pile catalogue, reset forbidden")
                conn.execute("DELETE FROM charging_schedules")
                conn.execute("DELETE FROM reservations")
                conn.execute("UPDATE piles SET status = 'AVAILABLE'")
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        # If checkpoint removal fails, require container recreation rather
        # than return a misleading success. This endpoint is demo-only.
        round_service._CHECKPOINT_PATH.unlink(missing_ok=True)
        round_service._round_state.update({
            "tick": 0, "phase": "IDLE", "unserved_vehicles": [],
            "unserved_requests": [], "prepare_result": None,
            "settle_result": None, "served_sessions": [], "released_piles": [],
        })
    return {"fog_id": FOG_ID, "reset": True, "tick": 0, "total_piles": total}

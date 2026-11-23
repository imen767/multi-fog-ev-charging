#!/usr/bin/env python3
"""Etape 40: controlled, isolated, real-HTTP Fog-to-Fog fault-injection demo.

Runs ONLY containers in the private Docker Compose project fog-resilience-demo.
Does not access or modify production Fog/Cloud/PostgreSQL. Requires Docker Compose.

Scenarios:
  1. Baseline saturated Fog B -> active remote charging (PREPARE/SETTLE)
  2. Fog A offline -> B negotiates with C, HOLD/CONFIRM/ACTIVATE
  3. Fog A and C offline -> NO_PROPOSALS, Fog B remains healthy
  4. Fog A and C recovered -> B can negotiate again

Caveats: tmpfs SQLite deliberately resets on stopped demo containers. This test
is NOT a persistence/restart test for production databases or Cloud failover.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "compose.demo.resilience.yml"
PROJECT = "fog-resilience-demo"  # fixed, cannot point to production project
ENDPOINTS = {f"fog-{x}": f"http://127.0.0.1:1820{i}" for i, x in enumerate("abc", 1)}


def docker_compose(*args: str, show: bool = False) -> str:
    """Only ever run compose on the hard-coded disposable demo project."""
    command = ["docker", "compose", "-p", PROJECT, "-f", str(COMPOSE), *args]
    completed = subprocess.run(command, capture_output=True, text=True)
    if completed.returncode:
        raise RuntimeError(f"Command failed: {' '.join(command)}\n{completed.stdout}\n{completed.stderr}")
    if show and completed.stdout.strip():
        print(completed.stdout.strip())
    return completed.stdout.strip()


def request_json(fog: str, route: str, payload: dict | None = None, *, timeout: int = 22) -> dict:
    if fog not in ENDPOINTS:
        raise ValueError("Invalid Fog name")
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = Request(ENDPOINTS[fog] + route, data=data, headers={"Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urlopen(req, timeout=timeout) as response:
            result = json.load(response)
    except HTTPError as exc:
        detail = exc.read(400).decode("utf-8", errors="replace")
        raise AssertionError(f"HTTP {exc.code} from {fog}{route}: {detail}") from exc
    except URLError as exc:
        raise AssertionError(f"Fog {fog} unreachable at {route}: {exc}") from exc
    if not isinstance(result, dict):
        raise AssertionError(f"Expected JSON object from {fog}{route}")
    return result


def wait_healthy(fogs: tuple[str, ...], *, attempts: int = 60, interval: float = 1.0) -> None:
    pending = set(fogs)
    for _ in range(attempts):
        for fog in list(pending):
            try:
                if request_json(fog, "/health", timeout=2).get("status") == "healthy":
                    pending.remove(fog)
            except (AssertionError, OSError, TimeoutError):
                pass
        if not pending:
            return
        time.sleep(interval)
    raise AssertionError(f"Services not healthy: {sorted(pending)}")


def negotiate_and_activate(request_id: str, *, expected_peer: str | None = None) -> dict:
    """Request help from B, confirm and activate on selected remote Fog.

    IMPORTANT: HOLD_GRANTED is not a successful charging session; ACTIVATE is.
    """
    negotiation = request_json("fog-b", "/api/v1/cooperation/request-help", {
        "request_id": request_id,
        "origin_fog": "fog-b",
        "required_piles": 1,
        "required_power_kw": 22.0,
    })
    assert negotiation.get("status") == "HOLD_GRANTED", negotiation
    selected = (negotiation.get("selected_proposal") or {}).get("proposer_fog")
    if expected_peer:
        assert selected == expected_peer, f"Expected {expected_peer}, got {selected}"
    assert selected in ("fog-a", "fog-c"), negotiation
    hold_id = (negotiation.get("hold") or {}).get("hold_id")
    assert isinstance(hold_id, str) and hold_id, negotiation

    before = request_json(selected, "/api/v1/piles/capacity")["occupied_piles"]
    confirm = request_json(selected, "/api/v1/reservations/confirm", {"hold_id": hold_id})
    assert confirm.get("status") == "CONFIRMED", confirm
    activated = request_json(selected, "/api/v1/reservations/activate", {"hold_id": hold_id})
    assert activated.get("status") == "ACTIVE", activated
    after = request_json(selected, "/api/v1/piles/capacity")["occupied_piles"]
    assert after == before + 1, (before, after, selected)
    return {"selected_fog": selected, "hold_id": hold_id, "occupied_before": before,
            "occupied_after": after, "peer_errors": negotiation.get("errors", [])}


def run_scenarios() -> None:
    if not shutil.which("docker"):
        raise RuntimeError("Docker CLI not found; run from WSL with Docker Desktop started")
    if not COMPOSE.is_file():
        raise RuntimeError(f"Missing isolated Compose file: {COMPOSE}")
    current = docker_compose("ps", "--all", "-q")
    if current:
        raise RuntimeError("Demo project already exists. Inspect it and run:\n"
                           "docker compose -p fog-resilience-demo -f compose.demo.resilience.yml down\n"
                           "then rerun this script. NEVER use down -v on the production project.")

    print("\n=== Etape 40: ISOLATED demo, no production Fog/Cloud touched ===", flush=True)
    docker_compose("up", "-d", "--build", "demo-fog-a", "demo-fog-b", "demo-fog-c", show=True)
    wait_healthy(tuple(ENDPOINTS))
    capacities = {fog: request_json(fog, "/api/v1/piles/capacity") for fog in ENDPOINTS}
    assert {fog: cap["total_piles"] for fog, cap in capacities.items()} == {
        "fog-a": 2, "fog-b": 1, "fog-c": 3}, capacities

    print("\n1) Normal path: B saturated -> remote ACTIVE through PREPARE/SETTLE", flush=True)
    baseline = docker_compose("--profile", "runner", "run", "--rm", "demo-runner")
    assert "SUCCÈS" in baseline and "HOLD → CONFIRM → ACTIVATE" in baseline, baseline
    print("   PASS: baseline Fog B saturation and ACTIVE remote charging")
    assert request_json("fog-b", "/api/v1/piles/capacity")["available_piles"] == 0

    print("\n2) Fault: stop only DEMO Fog A; Fog B should use C", flush=True)
    docker_compose("stop", "demo-fog-a", show=True)
    result = negotiate_and_activate("resilience-a-down-" + uuid.uuid4().hex, expected_peer="fog-c")
    assert any(e.get("peer_fog") == "fog-a" for e in result["peer_errors"]), result
    print("   PASS: Fog A unavailable, Fog C ACTIVE; occupation:", result["occupied_before"], "->", result["occupied_after"])

    print("\n3) Fault: stop only DEMO Fog C too; no peer available", flush=True)
    docker_compose("stop", "demo-fog-c", show=True)
    unavailable = request_json("fog-b", "/api/v1/cooperation/request-help", {
        "request_id": "resilience-both-down-" + uuid.uuid4().hex,
        "origin_fog": "fog-b", "required_piles": 1, "required_power_kw": 22.0,
    }, timeout=30)
    assert unavailable.get("status") == "NO_PROPOSALS", unavailable
    assert unavailable.get("hold") is None, unavailable
    assert request_json("fog-b", "/health").get("status") == "healthy"
    assert {e.get("peer_fog") for e in unavailable.get("errors", [])} >= {"fog-a", "fog-c"}, unavailable
    print("   PASS: no false ACTIVE, Fog B stays healthy and reports NO_PROPOSALS")

    print("\n4) Recovery: restart isolated peers A and C", flush=True)
    docker_compose("start", "demo-fog-a", "demo-fog-c", show=True)
    wait_healthy(("fog-a", "fog-c"))
    recovered = negotiate_and_activate("resilience-recovered-" + uuid.uuid4().hex)
    print("   PASS: remote ACTIVE on", recovered["selected_fog"], "after peer recovery")
    print("\nSUCCÈS : scenarios 1–4 succeeded on disposable demo containers.")
    print("The orchestrated round barrier was tested in baseline ONLY; it is not fault tolerant")
    print("when a participating Fog is offline. No production SQLite/PostgreSQL/Cloud touched.")
    print("\nCleanup when finished:")
    print("docker compose -p fog-resilience-demo -f compose.demo.resilience.yml down")


def main() -> None:
    parser = argparse.ArgumentParser(description="Isolated Multi-Fog resilience demo")
    parser.add_argument("--run", action="store_true", help="Start and control ONLY fog-resilience-demo disposable containers")
    args = parser.parse_args()
    if not args.run:
        parser.print_help()
        print("\nNothing changed. Add --run to execute the isolated fault scenarios.")
        return
    run_scenarios()


if __name__ == "__main__":
    try:
        main()
    except (AssertionError, RuntimeError, subprocess.SubprocessError, KeyboardInterrupt) as exc:
        print(f"\nFAILED: {exc}", file=sys.stderr)
        print("Only isolated demo services may be running/stopped. Inspect demo logs, then:", file=sys.stderr)
        print("docker compose -p fog-resilience-demo -f compose.demo.resilience.yml down", file=sys.stderr)
        sys.exit(1)

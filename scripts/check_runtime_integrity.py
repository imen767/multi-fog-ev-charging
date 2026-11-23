#!/usr/bin/env python3
"""Read-only preflight of production Multi-Fog/Cloud (no state mutations).

Usage: python3 scripts/check_runtime_integrity.py [--monitoring]

The script performs GETs only and NEVER advances ticks or edits databases.
"""
from __future__ import annotations

import argparse
import json
import sys
from urllib.parse import urlencode
from urllib.request import Request, urlopen

FOGS = {f"fog-{name}": f"http://127.0.0.1:{18000 + index}"
        for index, name in enumerate("abc", 1)}
ORCH = "http://127.0.0.1:18081"
PROM = "http://127.0.0.1:19090"


def get_json(url: str) -> dict:
    with urlopen(Request(url, headers={"Accept": "application/json"}), timeout=5) as reply:
        data = json.load(reply)
    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object at {url}")
    return data


def check_states(fog_states: dict, capacities: dict, orch_state: dict) -> list[str]:
    errors = []
    current_tick = orch_state.get("current_tick")
    if type(current_tick) is not int:
        errors.append("Orchestrator current_tick missing or not integer")
    if orch_state.get("history_pending_tick") is not None:
        errors.append(f"Cloud history pending at tick {orch_state['history_pending_tick']}; do not advance ticks")
    for fog in FOGS:
        state = fog_states.get(fog) or {}
        cap = capacities.get(fog) or {}
        if state.get("fog_id") != fog or cap.get("fog_id") != fog:
            errors.append(f"Fog identity mismatch: {fog}")
        if state.get("tick") != current_tick:
            errors.append(f"Tick mismatch: {fog} tick={state.get('tick')} Cloud tick={current_tick}")
        if state.get("phase") not in ("IDLE", "SETTLED"):
            errors.append(f"Fog {fog} in {state.get('phase')} phase; no new tick")
        for field in ("available_piles", "occupied_piles"):
            if state.get(field) != cap.get(field):
                errors.append(f"{fog}: mismatch {field} between round and capacity APIs")
    return errors


def prometheus_query(metric: str) -> dict:
    data = get_json(PROM + "/api/v1/query?" + urlencode({"query": metric}))
    if data.get("status") != "success":
        raise ValueError(f"Prometheus did not return a success for {metric}")
    return data


def check_monitoring() -> list[str]:
    errors: list[str] = []
    node = prometheus_query("multi_fog_node_up").get("data", {}).get("result", [])
    values = {item.get("metric", {}).get("fog"): item.get("value", [None, None])[1] for item in node}
    for fog in FOGS:
        if values.get(fog) != "1":
            errors.append(f"Prometheus node_up for {fog} is {values.get(fog)!r}, expected '1'")
    pg = prometheus_query("pg_up").get("data", {}).get("result", [])
    if not any(item.get("value", [None, None])[1] == "1" for item in pg):
        errors.append("Prometheus pg_up is not 1: PostgreSQL monitoring reports inaccessible")
    return errors


def main() -> int:
    p = argparse.ArgumentParser(description="Read-only health and tick consistency checks")
    p.add_argument("--monitoring", action="store_true", help="Also query Prometheus and pg_up")
    args = p.parse_args()
    try:
        states = {fog: get_json(url + "/api/v1/round/status") for fog, url in FOGS.items()}
        caps = {fog: get_json(url + "/api/v1/piles/capacity") for fog, url in FOGS.items()}
        health = {fog: get_json(url + "/health") for fog, url in FOGS.items()}
        orch = get_json(ORCH + "/api/v1/orchestrator/status")
        issues = check_states(states, caps, orch)
        for fog in FOGS:
            if health[fog].get("status") != "healthy":
                issues.append(f"Fog {fog} health != healthy")
        if args.monitoring:
            issues.extend(check_monitoring())
    except Exception as exc:
        print(f"ERROR: read-only preflight could not complete: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print("Cloud tick:", orch.get("current_tick"), "| Pending history:", orch.get("history_pending_tick"))
    for fog in FOGS:
        s, cap = states[fog], caps[fog]
        print(f"{fog}: tick={s.get('tick')} phase={s.get('phase')} occupied={cap.get('occupied_piles')} available={cap.get('available_piles')}")
    if issues:
        print("CHECK FAILED:")
        for issue in issues:
            print(" -", issue)
        return 2
    print("OK: Fog/Cloud ticks, capacities, status" + (", Prometheus and PostgreSQL" if args.monitoring else "") + " are consistent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

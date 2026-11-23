#!/usr/bin/env python3
"""Controle passif du cluster LAN, sans creer de hold ni avancer le tick."""
from __future__ import annotations

import argparse
import json
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

FOGS = ("fog-a", "fog-b", "fog-c")


def get_json(url: str) -> dict:
    with urlopen(Request(url, headers={"Accept": "application/json"}), timeout=10) as reply:
        body = json.load(reply)
        if not isinstance(body, dict):
            raise ValueError(f"Reponse non JSON object : {url}")
        return body


def check(urls: dict[str, str], orch: str, expected_tick: int | None) -> dict:
    fogs = {}
    for fog_id, base in urls.items():
        health = get_json(f"{base.rstrip('/')}/health")
        round_info = get_json(f"{base.rstrip('/')}/api/v1/round/status")
        cap = get_json(f"{base.rstrip('/')}/api/v1/piles/capacity")
        if health.get("fog_id") != fog_id or round_info.get("fog_id") != fog_id or cap.get("fog_id") != fog_id:
            raise ValueError(f"Adresse invalide ou Fog incorrect pour {fog_id}")
        if round_info.get("phase") != "SETTLED":
            raise ValueError(f"{fog_id} n'est pas SETTLED : {round_info.get('phase')}")
        fogs[fog_id] = {"tick": round_info["tick"], "phase": round_info["phase"],
                        "occupied_piles": cap["occupied_piles"], "total_piles": cap["total_piles"]}
    ticks = {f["tick"] for f in fogs.values()}
    if len(ticks) != 1:
        raise ValueError(f"Ticks differents : {fogs}")
    tick = ticks.pop()
    if expected_tick is not None and expected_tick != tick:
        raise ValueError(f"Tick actuel {tick}, attendu {expected_tick}")
    orchestrator = get_json(f"{orch.rstrip('/')}/api/v1/orchestrator/status")
    if orchestrator.get("current_tick") != tick:
        raise ValueError(f"Orchestrateur {orchestrator.get('current_tick')} != Fogs {tick}")
    if orchestrator.get("history_pending_tick") is not None:
        raise ValueError(f"Historique Cloud en attente: {orchestrator['history_pending_tick']}")
    return {"status": "OK", "tick": tick, "fogs": fogs,
            "run_id": orchestrator.get("simulation_run_id"), "history_pending_tick": None}


def main() -> int:
    p = argparse.ArgumentParser()
    for name in FOGS:
        p.add_argument(f"--{name}", required=True, help="Ex: http://192.168.1.20:18002")
    p.add_argument("--orchestrator", default="http://127.0.0.1:18081")
    p.add_argument("--expected-tick", type=int)
    args = p.parse_args()
    try:
        urls = {key: getattr(args, key.replace("-", "_")) for key in FOGS}
        print(json.dumps(check(urls, args.orchestrator, args.expected_tick), indent=2, ensure_ascii=False))
        return 0
    except (OSError, HTTPError, URLError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"CLUSTER NON PRET: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

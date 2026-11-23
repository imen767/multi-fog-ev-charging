#!/usr/bin/env python3
"""Précontrôle 100% lecture seule AVANT le gel/migration multi-PC.
N'écrit aucune base, ne déclenche aucun tick et ne crée aucun conteneur.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[2]
URLS = {"fog-a": "http://127.0.0.1:18001",
        "fog-b": "http://127.0.0.1:18002",
        "fog-c": "http://127.0.0.1:18003"}
ORCH = "http://127.0.0.1:18081/api/v1/orchestrator/status"
FREQUENTLY_REQUIRED = (
    "fog/services/peer_communication_service.py",
    "fog/services/round_service.py",
    "cloud/fog_sync_agent.py",
    "cloud/demo_cooperation/interactive_gateway.py",
    "deploy/multi_pc/prepare_handoff.py",
    "deploy/multi_pc/compose.fog-node.yml",
    "deploy/multi_pc/compose.cloud.remote.yml",
)

def json_get(url):
    with urlopen(Request(url, headers={"Accept": "application/json"}), timeout=5) as resp:
        doc = json.load(resp)
        if not isinstance(doc, dict):
            raise ValueError(f"JSON non-objet: {url}")
        return doc

def preflight(root=ROOT, urls=URLS, orch_url=ORCH):
    issues = []
    warnings = []
    fogs = {}
    try:
        orch = json_get(orch_url)
    except (OSError, ValueError, json.JSONDecodeError) as e:
        raise RuntimeError(f"Orchestrateur inaccessible: {e}") from e
    tick = orch.get("current_tick")
    if type(tick) is not int or tick < 0:
        issues.append(f"Tick orchestrateur invalide: {tick}")
    if orch.get("history_pending_tick") is not None:
        issues.append(f"Historique en attente: {orch['history_pending_tick']}")
    if not orch.get("simulation_run_id"):
        issues.append("simulation_run_id absent")
    for fid, base in urls.items():
        try:
            rd = json_get(base + "/api/v1/round/status")
            cap = json_get(base + "/api/v1/piles/capacity")
            healthy = json_get(base + "/health")
        except (OSError, ValueError, json.JSONDecodeError) as e:
            issues.append(f"{fid} inaccessible: {e}")
            continue
        if rd.get("fog_id") != fid or cap.get("fog_id") != fid:
            issues.append(f"Identité incorrecte sur {fid}")
        if rd.get("tick") != tick or rd.get("phase") != "SETTLED":
            issues.append(f"{fid}: tick={rd.get('tick')} phase={rd.get('phase')} ; attendu {tick}/SETTLED")
        if healthy.get("status") != "healthy":
            issues.append(f"{fid}: statut /health non healthy")
        fogs[fid] = {"tick": rd.get("tick"), "phase": rd.get("phase"),
                     "total": cap.get("total_piles"), "occupied": cap.get("occupied_piles")}
        # Le checkpoint est nécessaire au futur kit. On ne l'écrit jamais ici.
        cp = root / "docker-data" / fid / ".round_checkpoint.json"
        if not cp.is_file():
            issues.append(f"Checkpoint manquant : {cp.relative_to(root)}")
        else:
            try:
                state = json.loads(cp.read_text(encoding="utf-8"))
                if state.get("tick") != tick or state.get("phase") != "SETTLED":
                    issues.append(f"Checkpoint {fid} décalé: {state.get('tick')}/{state.get('phase')}")
            except (OSError, ValueError, json.JSONDecodeError) as e:
                issues.append(f"Checkpoint {fid} illisible: {e}")
        dbname = fid.replace("-", "_") + ".db"
        if not (root / "docker-data" / fid / dbname).is_file():
            issues.append(f"SQLite absente pour {fid}: docker-data/{fid}/{dbname}")
    for name in FREQUENTLY_REQUIRED:
        if not (root / name).is_file():
            issues.append(f"Fichier requis non présent: {name}")
    # Les sources sont à jour; la migration restera en attente de la disponibilité des autres PC.
    if (root / "deploy/multi_pc/vite.remote.config.ts").is_file():
        warnings.append("NE PAS remplacer dashboard/vite.config.ts pendant la préparation : config distant seulement au déploiement")
    warnings.append("Ne jamais fournir .env.cloud aux binômes ; jeton Sync transmis séparément")
    warnings.append("Round PREPARED après coupure réseau: reprise distribuée à valider, ne pas rejouer un tick à l'aveugle")
    return {"status": "OK" if not issues else "NON_PRET", "simulation_run_id": orch.get("simulation_run_id"),
            "current_tick": tick, "fogs": fogs, "blocking_issues": issues, "warnings": warnings}

def main():
    p = argparse.ArgumentParser(description="Audit en lecture seule avant préparation du transfert Fog B/C")
    p.add_argument("--json", action="store_true", help="Sortie JSON uniquement")
    args = p.parse_args()
    try:
        result = preflight()
    except RuntimeError as e:
        print(f"NON PRET : {e}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(f"Cluster : {result['status']} | run={result['simulation_run_id']} | tick={result['current_tick']}")
        for f, row in result['fogs'].items():
            print(f"{f}: tick={row['tick']} phase={row['phase']} occupied={row['occupied']}/{row['total']}")
        for issue in result['blocking_issues']:
            print("ERREUR : " + issue)
        for warning in result['warnings']:
            print("NOTE : " + warning)
    return 0 if result["status"] == "OK" else 1
if __name__ == "__main__":
    sys.exit(main())

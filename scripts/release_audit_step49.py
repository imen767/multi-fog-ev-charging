#!/usr/bin/env python3
"""Audit de pre-livraison, UNIQUEMENT en lecture (aucun POST ni Docker mutatif).

A executer sur le PC central WSL apres installation de l'etape 47.
Le resultat est un precontrole local, pas un test de reseau entre trois PC.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
SERVICES = {
    "cloud-sync-api": "http://127.0.0.1:18080/health",
    "orchestrateur": "http://127.0.0.1:18081/health",
    "chronos-2": "http://127.0.0.1:18082/health",
    "dashboard-gateway": "http://127.0.0.1:18083/health",
}
REQUIRED_FILES = (
    "deploy/multi_pc/preflight.py",
    "deploy/multi_pc/prepare_handoff.py",
    "deploy/multi_pc/verify_fog_kit.py",
    "deploy/multi_pc/compose.fog-node.yml",
    "deploy/multi_pc/compose.cloud.remote.yml",
    "deploy/multi_pc/compose.fog-a.remote.yml",
    "deploy/multi_pc/check_cluster.py",
    "cloud/fog_sync_agent.py",
    "cloud/demo_cooperation/interactive_gateway.py",
    "scripts/check_cloud_sync_consistency.py",
    "scripts/check_runtime_integrity.py",
    "compose.cloud.monitoring.yml",
    "compose.demo.cooperation.yml",
    "compose.demo.faults.yml",
    "scripts/test_peer_failover_step48.py",
    ".github/workflows/ci.yml",
)


def get_health(url: str) -> bool:
    request = Request(url, headers={"Accept": "application/json"}, method="GET")
    with urlopen(request, timeout=4) as response:
        data = json.load(response)
    return isinstance(data, dict) and data.get("status") == "healthy"


def run_preflight(root: Path) -> dict:
    path = root / "deploy/multi_pc/preflight.py"
    if not path.is_file():
        return {"status": "NON_PRET", "error": "Installer l'étape 47 (preflight.py absent)"}
    spec = importlib.util.spec_from_file_location("step47_preflight", path)
    if spec is None or spec.loader is None:
        return {"status": "NON_PRET", "error": "Impossible de charger preflight.py"}
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        return module.preflight(root=root)
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        return {"status": "NON_PRET", "error": f"{type(exc).__name__}: {exc}"}


def run_git_summary(root: Path) -> dict:
    cmd = subprocess.run(["git", "status", "--porcelain", "--untracked-files=normal"],
                         cwd=root, capture_output=True, text=True, check=False)
    if cmd.returncode:
        return {"status": "NON_DISPONIBLE", "detail": (cmd.stderr or "Git inaccessible").strip()}
    changed = [line for line in cmd.stdout.splitlines() if line.strip()]
    remote = subprocess.run(["git", "remote", "get-url", "origin"], cwd=root,
                            capture_output=True, text=True, check=False)
    # Ne pas imprimer le remote: il peut contenir des identifiants dans des configurations inadaptees.
    return {"status": "OK", "modifications_locales": len(changed),
            "origin_configure": remote.returncode == 0}


def audit(root: Path = ROOT, fetch=get_health, preflight=run_preflight,
          git_summary=run_git_summary) -> dict:
    base = preflight(root)
    missing = [file for file in REQUIRED_FILES if not (root / file).is_file()]
    services = {}
    for name, url in SERVICES.items():
        try:
            services[name] = "OK" if fetch(url) else "NON_HEALTHY"
        except (OSError, HTTPError, URLError, ValueError, json.JSONDecodeError):
            services[name] = "INACCESSIBLE"
    project_git = git_summary(root)
    blocks = list(base.get("blocking_issues") or [])
    if base.get("status") != "OK":
        blocks.append(base.get("error") or "Précontrôle multi-PC étape 47 NON_PRET")
    if missing:
        blocks.append(f"{len(missing)} fichier(s) de livraison manquant(s)")
    unavailable = [k for k, v in services.items() if v != "OK"]
    if unavailable:
        blocks.append("Services Cloud locaux indisponibles : " + ", ".join(unavailable))
    if project_git.get("status") == "NON_DISPONIBLE":
        blocks.append("Git local indisponible")
    warnings = [
        "Le résultat ne prouve PAS la réussite du test de panne étape 48 : conserver sa sortie PASS séparément.",
        "Le déploiement sur trois PC et les pare-feu LAN/VPN ne sont PAS encore validés.",
        "Ne pas lancer prepare_handoff.py tant que Fog B/C sont actifs sur le PC central.",
        "Ne pas partager .env.cloud, .env.fog renseigné ou les mots de passe Cloud.",
        "Ne jamais utiliser docker compose down -v pour ce projet.",
    ]
    if project_git.get("modifications_locales", 0):
        warnings.append("Des modifications Git sont encore locales : relire puis commit/push avant la livraison.")
    return {
        "status": "PRET_POUR_PREPARATION" if not blocks else "A_CORRIGER",
        "execution": "AUDIT_LOCAL_LECTURE_SEULE",
        "current_tick": base.get("current_tick"),
        "simulation_run_id": base.get("simulation_run_id"),
        "fogs": base.get("fogs", {}),
        "services": services,
        "fichiers_manquants": missing,
        "git": project_git,
        "blocages": blocks,
        "a_confirmer": warnings,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit en lecture seule avant livraison multi-PC")
    parser.add_argument("--json", action="store_true", help="Sortie JSON lisible par outil")
    args = parser.parse_args()
    result = audit()
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("=== Audit Multi-Fog (lecture seule) ===")
        print("État :", result["status"], "| tick :", result["current_tick"])
        for fog, row in result["fogs"].items():
            print(f"{fog}: tick={row.get('tick')} phase={row.get('phase')} occupied={row.get('occupied')}")
        for name, status in result["services"].items():
            print(f"Service {name}: {status}")
        print("Git: fichiers modifiés/non suivis =", result["git"].get("modifications_locales", "inconnu"))
        for message in result["blocages"]:
            print("BLOCAGE :", message)
        for message in result["a_confirmer"]:
            print("ATTENTION :", message)
    return 0 if not result["blocages"] else 1


if __name__ == "__main__":
    sys.exit(main())

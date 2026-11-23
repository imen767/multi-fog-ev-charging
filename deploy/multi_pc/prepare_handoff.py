#!/usr/bin/env python3
"""Fabrique sur le PC central deux archives autonomes Fog B/C (étape 47).

A lancer UNIQUEMENT apres avoir stoppe les anciens conteneurs fog-b et fog-c,
et apres verification de la coherence du tick et de l'historique Cloud.
N'ecrit JAMAIS dans les SQLite sources. N'inclut AUCUN secret du Cloud.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import zipfile
from hashlib import sha256

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
SOURCE_FILES = [
    "Dockerfile", "requirements.txt", "bootstrap_db.py", "import_urban_ev.py",
    "stations_catalog.csv", "piles_catalog.csv", "cloud/fog_sync_agent.py",
    "cloud/Dockerfile.sync-agent", "deploy/multi_pc/compose.fog-node.yml",
    "deploy/multi_pc/verify_fog_kit.py", "deploy/multi_pc/.gitignore.kit",
]
SOURCE_DIRS = ("fog", "shared", "simulator")
CONFIG = {
    "fog-b": {"zone": "shopping_center", "db": "fog_b.db", "port": 18002},
    "fog-c": {"zone": "public_parking", "db": "fog_c.db", "port": 18003},
}


def ensure_container_stopped(fog_id: str) -> None:
    if not shutil.which("docker"):
        raise RuntimeError("docker absent; impossible de verifier si le Fog source est arrete")
    result = subprocess.run(
        ["docker", "inspect", "--format", "{{.State.Running}}", fog_id],
        capture_output=True, text=True, check=False,
    )
    if result.returncode == 0 and result.stdout.strip().lower() == "true":
        raise RuntimeError(f"{fog_id} tourne encore : docker compose stop {fog_id} AVANT l'export")
    if result.returncode != 0 and "No such object" not in (result.stderr or ""):
        raise RuntimeError(f"Etat Docker inconnu pour {fog_id}: {result.stderr.strip()}")


def validate_checkpoint(path: Path, expected_tick: int) -> dict:
    if not path.is_file():
        raise FileNotFoundError(f"Checkpoint introuvable: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("phase") != "SETTLED" or type(data.get("tick")) is not int:
        raise ValueError(f"Checkpoint non SETTLED dans {path}")
    if data["tick"] != expected_tick:
        raise ValueError(f"Tick du {path}: {data['tick']} != {expected_tick}; ne pas redistribuer des etats divergents")
    return data


def snapshot_sqlite(source: Path, dest: Path) -> int:
    if not source.is_file():
        raise FileNotFoundError(f"SQLite introuvable: {source}")
    # SQLite backup API au lieu d'une copie de fichier potentiellement incoherente.
    # Le Fog source est arrete par precondition, donc son checkpoint ne peut plus changer.
    with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as src:
        with sqlite3.connect(dest) as dst:
            src.backup(dst)
            dst.commit()
    with sqlite3.connect(f"file:{dest}?mode=ro", uri=True) as check:
        integrity = check.execute("PRAGMA integrity_check").fetchone()[0]
        piles = check.execute("SELECT COUNT(*) FROM piles").fetchone()[0]
        if integrity != "ok" or piles <= 0:
            raise RuntimeError(f"SQLite suspecte (integrity={integrity}, piles={piles}): {source}")
        return piles


def env_example(fog_id: str) -> str:
    meta = CONFIG[fog_id]
    return (f"# Configurer les 3 adresses IP LAN ou VPN des PC avant de demarrer.\n"
            f"# Ne jamais publier ce fichier sur GitHub ni envoyer le token dans le ZIP.\n"
            f"FOG_ID={fog_id}\n"
            f"FOG_ZONE={meta['zone']}\n"
            f"FOG_DB_FILE={meta['db']}\n"
            f"FOG_HOST_PORT={meta['port']}\n"
            f"FOG_A_URL=http://IP_PC_A:18001\n"
            f"FOG_B_URL=http://IP_PC_B:18002\n"
            f"FOG_C_URL=http://IP_PC_C:18003\n"
            f"CLOUD_SYNC_URL=http://IP_PC_A:18084\n"
            f"CHRONOS_URL=http://IP_PC_A:18085\n"
            f"CLOUD_SYNC_TOKEN=REMPLACER_PAR_TOKEN_CLOUD_SYNC_PARTAGE_PRIVE\n")


def add_tree(zf: zipfile.ZipFile, source: Path, target: str) -> None:
    if not source.is_dir():
        raise FileNotFoundError(source)
    for f in sorted(source.rglob("*")):
        if not f.is_file() or any(part in {"__pycache__", ".git", ".venv", "node_modules", "backup"} for part in f.parts):
            continue
        if f.name in {".env", ".env.cloud", ".env.fog"} or f.name.startswith(".env."):
            continue
        if f.suffix.lower() in (".pyc", ".pyo", ".db", ".sqlite", ".sqlite3", ".pem", ".key", ".dump"):
            continue
        zf.write(f, f"{target}/{f.relative_to(source).as_posix()}")


def generate_bundle(fog_id: str, expected_tick: int, output: Path) -> dict:
    meta = CONFIG[fog_id]
    ensure_container_stopped(fog_id)
    state_source = ROOT / "docker-data" / fog_id
    checkpoint_source = state_source / ".round_checkpoint.json"
    validate_checkpoint(checkpoint_source, expected_tick)
    db_source = state_source / meta["db"]
    output.mkdir(parents=True, exist_ok=True)
    dest = output / f"{fog_id}-node.zip"
    prefix = f"{fog_id}-node"
    with tempfile.TemporaryDirectory(prefix="fog-handoff-") as temp:
        db_snapshot = Path(temp) / meta["db"]
        piles = snapshot_sqlite(db_source, db_snapshot)
        with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for file in SOURCE_FILES:
                source = ROOT / file
                if not source.is_file():
                    raise FileNotFoundError(f"Fichier requis manquant: {source}")
                target = {
                    "deploy/multi_pc/compose.fog-node.yml": "compose.fog-node.yml",
                    "deploy/multi_pc/verify_fog_kit.py": "verify_fog_kit.py",
                    "deploy/multi_pc/.gitignore.kit": ".gitignore",
                }.get(file, file)
                zf.write(source, f"{prefix}/{target}")
            for dir_name in SOURCE_DIRS:
                add_tree(zf, ROOT / dir_name, f"{prefix}/{dir_name}")
            series = ROOT / "ml-service" / "data" / f"{fog_id}.csv"
            if not series.is_file():
                raise FileNotFoundError(series)
            zf.write(series, f"{prefix}/ml-service/data/{fog_id}.csv")
            zf.write(db_snapshot, f"{prefix}/state/{meta['db']}")
            zf.write(checkpoint_source, f"{prefix}/state/.round_checkpoint.json")
            # Attention: .env.fog contiendra le token une fois renseigné sur le PC cible.
            # L'exclure absolument du contexte du build Docker, ainsi que la SQLite.
            zf.writestr(f"{prefix}/.dockerignore", (
                ".env\n.env.*\nstate/\n*.db\n*.sqlite\n*.sqlite3\n"
                "*.db-wal\n*.db-shm\n*.pem\n*.key\n*.dump\n"
                "__pycache__/\n.venv/\n.git/\n"
            ))
            zf.writestr(f"{prefix}/cloud/.dockerignore", (
                ".env\n.env.*\n*.pem\n*.key\n__pycache__/\n"
            ))
            zf.writestr(f"{prefix}/.env.fog", env_example(fog_id))
            zf.writestr(f"{prefix}/README-PC.md", (
                f"# {fog_id.upper()} — demarrage sur le PC du binome\n\n"
                f"1. Copier ce repertoire sur le systeme Linux WSL2: `~/fog-{fog_id[-1]}-node`.\n"
                f"2. Modifier `.env.fog` avec les IP des trois PC, Cloud et le token transmis en prive.\n"
                f"3. Lancer `python3 verify_fog_kit.py` (doit afficher PRECHECK OK).\n"
                f"4. Lancer `docker compose -p {fog_id}-node --env-file .env.fog -f compose.fog-node.yml up -d --build`.\n"
                f"5. Tester `curl -fsS http://localhost:{meta['port']}/health` et les logs via "
                f"`docker compose -p {fog_id}-node --env-file .env.fog -f compose.fog-node.yml logs --tail=30`.\n"
                f"6. Ne JAMAIS utiliser le `docker-compose.yml` de la maquette 1 PC.\n"
                f"7. Ne JAMAIS partager `.env.fog` ni faire `down -v`/supprimer `state/`.\n"
                f"8. Ouvrir le port {meta['port']} seulement aux deux autres PC et au Cloud sur un LAN/VPN prive.\n\n"
                f"Au demarrage, Fog {fog_id[-1].upper()} doit reprendre au tick {expected_tick} (pas au tick 0).\n"
            ))
    digest = sha256(dest.read_bytes()).hexdigest()
    return {"fog_id": fog_id, "tick": expected_tick, "piles": piles,
            "archive": str(dest), "size_bytes": dest.stat().st_size, "sha256": digest}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prepare les kits Fog B et C depuis SQLite et checkpoint du PC central")
    parser.add_argument("--expected-tick", type=int, required=True, help="Dernier tick SETTLED commun, ex: 8")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "fog-node-bundles")
    args = parser.parse_args(argv)
    if args.expected_tick < 1:
        parser.error("--expected-tick doit etre >= 1")
    try:
        reports = [generate_bundle(fog, args.expected_tick, args.output_dir) for fog in CONFIG]
        report_path = args.output_dir / "MANIFEST.json"
        report_path.write_text(json.dumps(reports, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print("KITS PREPARES :")
        for r in reports:
            print(f"{r['fog_id']}: tick={r['tick']}, piles={r['piles']}, fichier={r['archive']}")
            print(f"  SHA256 {r['sha256']}")
        print("ATTENTION: jeton Cloud Sync a transmettre SEPAREMENT, ne jamais envoyer .env.cloud.")
        return 0
    except (OSError, RuntimeError, ValueError, KeyError, sqlite3.Error, json.JSONDecodeError) as exc:
        print(f"ERREUR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

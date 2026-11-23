#!/usr/bin/env python3
"""Verification locale sans effet de bord, sur PC Fog B ou Fog C."""
from __future__ import annotations
import json
import os
from pathlib import Path
import sqlite3
import sys
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
EXPECTED = {"fog-b": ("fog_b.db", 18002), "fog-c": ("fog_c.db", 18003)}
REQUIRED = ("FOG_ID", "FOG_ZONE", "FOG_DB_FILE", "FOG_HOST_PORT",
            "FOG_A_URL", "FOG_B_URL", "FOG_C_URL", "CLOUD_SYNC_URL",
            "CHRONOS_URL", "CLOUD_SYNC_TOKEN")


def parse_env(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise ValueError(f"Fichier de configuration manquant : {path}")
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        values[k.strip()] = v.strip().strip('"').strip("'")
    return values


def verify(root: Path) -> dict:
    cfg = parse_env(root / ".env.fog")
    missing = [k for k in REQUIRED if not cfg.get(k)]
    if missing:
        raise ValueError(f"Variables absentes : {', '.join(missing)}")
    fog_id = cfg["FOG_ID"]
    if fog_id not in EXPECTED:
        raise ValueError("FOG_ID doit etre fog-b ou fog-c")
    db_name, host_port = EXPECTED[fog_id]
    if cfg["FOG_DB_FILE"] != db_name or int(cfg["FOG_HOST_PORT"]) != host_port:
        raise ValueError("Fichier SQLite ou port incompatible avec FOG_ID")
    if cfg["FOG_ZONE"] != ("shopping_center" if fog_id == "fog-b" else "public_parking"):
        raise ValueError("Zone incompatible avec FOG_ID")
    for key in REQUIRED:
        v = cfg[key]
        if any(tag in v.upper() for tag in ("REMPLACER", "CHANGE_ME", "IP_PC_", "TODO", "REPLACE_ME")):
            raise ValueError(f"{key} doit etre renseigne")
    if len(cfg["CLOUD_SYNC_TOKEN"]) < 16:
        raise ValueError("Le jeton CLOUD_SYNC_TOKEN semble trop court")
    for key in ("FOG_A_URL", "FOG_B_URL", "FOG_C_URL", "CLOUD_SYNC_URL", "CHRONOS_URL"):
        url = urlparse(cfg[key])
        if url.scheme not in ("http", "https") or not url.hostname or not url.port:
            raise ValueError(f"URL invalide : {key}")
        if url.hostname in ("localhost", "127.0.0.1", "fog-a", "fog-b", "fog-c"):
            raise ValueError(f"{key} ne peut pas utiliser localhost/DNS Docker dans le deploiement multi-PC")
    state = root / "state"
    checkpoint = json.loads((state / ".round_checkpoint.json").read_text(encoding="utf-8"))
    if checkpoint.get("phase") != "SETTLED" or type(checkpoint.get("tick")) is not int:
        raise ValueError("Checkpoint de round invalide")
    db_path = state / db_name
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as con:
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        pile_count = con.execute("SELECT COUNT(*) FROM piles").fetchone()[0]
    if integrity != "ok" or pile_count < 1:
        raise ValueError("SQLite invalide ou vide")
    return {"fog_id": fog_id, "tick": checkpoint["tick"], "piles": pile_count,
            "host_port": host_port, "integrity": integrity}


if __name__ == "__main__":
    try:
        report = verify(ROOT)
        print("PRECHECK OK :", json.dumps(report, ensure_ascii=False))
    except (OSError, KeyError, ValueError, sqlite3.Error, json.JSONDecodeError) as exc:
        print(f"PRECHECK ECHEC : {exc}", file=sys.stderr)
        sys.exit(1)

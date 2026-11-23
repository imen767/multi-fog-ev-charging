"""Agent de synchronisation Fog -> Cloud (maquette de laboratoire).

Lit les etats via l'API HTTP locale du Fog, puis envoie les etats
nouveaux/modifies par lots a Cloud Sync API. Aucune connexion SQLite/PG
et aucune modification des services Fog existants.

Limites: cache en RAM, pas d'historique d'evenements ni protection contre
les paquets arrives dans le mauvais ordre. A durcir avant production.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

FOG_PORTS = {"fog-a": 18001, "fog-b": 18002, "fog-c": 18003}
BATCH_SIZE = 500  # Limite du schema Pydantic de Cloud Sync API.
STATE_KEYS = (
    "pile_id", "status", "is_reservable", "reservation_status", "currently_reservable"
)


def load_sync_token(env_file: Path) -> str:
    """Lit une valeur cle=valeur sans executer le contenu du fichier .env."""
    token = os.environ.get("CLOUD_SYNC_TOKEN", "")
    if token:
        return token
    if not env_file.is_file():
        raise ValueError(f"Fichier de configuration introuvable : {env_file}")
    for line in env_file.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("CLOUD_SYNC_TOKEN="):
            token = line.split("=", 1)[1].strip().strip('"').strip("'")
    if not token:
        raise ValueError("CLOUD_SYNC_TOKEN absent du fichier .env.cloud")
    return token


def get_json(url: str) -> object:
    with urlopen(Request(url, headers={"Accept": "application/json"}), timeout=30) as response:
        return json.load(response)


def post_json(url: str, payload: dict, token: str) -> object:
    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-Sync-Token": token,
        },
        method="POST",
    )
    with urlopen(request, timeout=30) as response:
        return json.load(response)


def prepare_state(pile: dict) -> dict:
    """N'envoie que les champs attendus par Cloud Sync API."""
    missing = [key for key in STATE_KEYS if key not in pile]
    if missing:
        raise ValueError(f"Champs absents pour la borne : {missing}")
    return {key: pile[key] for key in STATE_KEYS}


def fingerprint(row: dict) -> tuple:
    """Signature en memoire d'un etat deja confirme par le Cloud."""
    return tuple(row[key] for key in STATE_KEYS)


class FogSyncAgent:
    def __init__(self, fog_id: str, fog_url: str, cloud_url: str, token: str):
        self.fog_id = fog_id
        self.fog_url = fog_url.rstrip("/")
        self.cloud_url = cloud_url.rstrip("/")
        self.token = token
        self.confirmed: dict[str, tuple] = {}

    def sync_once(self) -> tuple[int, int, int]:
        """Retourne (nombre_total, nombre_envoye, nombre_de_lots)."""
        health = get_json(f"{self.fog_url}/health")
        if not isinstance(health, dict) or health.get("fog_id") != self.fog_id:
            raise ValueError("L'URL Fog ne correspond pas au fog_id choisi")

        piles = get_json(f"{self.fog_url}/api/v1/piles")
        capacity = get_json(f"{self.fog_url}/api/v1/piles/capacity")
        if not isinstance(piles, list) or not piles:
            raise ValueError("L'API Fog ne renvoie pas une liste non vide de bornes")
        if not isinstance(capacity, dict) or capacity.get("total_piles") != len(piles):
            raise ValueError("Nombre de bornes incoherent entre /piles et /capacity")

        rows = [prepare_state(pile) for pile in piles]
        ids = [row["pile_id"] for row in rows]
        if len(set(ids)) != len(ids):
            raise ValueError("Identifiants de bornes dupliques dans la reponse Fog")

        changed = [r for r in rows if self.confirmed.get(r["pile_id"]) != fingerprint(r)]
        sent, batches = 0, 0
        for start in range(0, len(changed), BATCH_SIZE):
            batch = changed[start : start + BATCH_SIZE]
            reply = post_json(
                f"{self.cloud_url}/api/v1/sync/pile-states",
                {"fog_id": self.fog_id, "states": batch},
                self.token,
            )
            if (not isinstance(reply, dict) or reply.get("status") != "synced"
                    or reply.get("fog_id") != self.fog_id
                    or reply.get("received") != len(batch)):
                raise RuntimeError("Accuse de reception Cloud incoherent")
            # Cache mis a jour UNIQUEMENT apres confirmation du Cloud.
            for row in batch:
                self.confirmed[row["pile_id"]] = fingerprint(row)
            sent += len(batch)
            batches += 1
        return len(rows), sent, batches


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Synchronise un Fog vers PostgreSQL via Cloud Sync API")
    parser.add_argument("--fog-id", choices=sorted(FOG_PORTS), default="fog-b")
    parser.add_argument("--fog-url", help="URL du Fog; par defaut localhost:18001/2/3")
    parser.add_argument("--cloud-url", default="http://127.0.0.1:18080")
    parser.add_argument("--env-file", type=Path, default=Path(".env.cloud"))
    parser.add_argument("--interval", type=int, default=0,
                        help="0 = une fois; sinon repetition toutes les N secondes (>=5)")
    args = parser.parse_args(argv)
    if args.interval < 0 or (args.interval != 0 and args.interval < 5):
        parser.error("--interval doit valoir 0 ou au moins 5 secondes")

    try:
        token = load_sync_token(args.env_file)
    except (OSError, ValueError) as exc:
        print(f"ERREUR: {exc}", file=sys.stderr)
        return 1

    fog_url = args.fog_url or f"http://127.0.0.1:{FOG_PORTS[args.fog_id]}"
    agent = FogSyncAgent(args.fog_id, fog_url, args.cloud_url, token)
    print(f"Agent {args.fog_id}: Fog={fog_url}, Cloud={args.cloud_url}", flush=True)
    try:
        while True:
            try:
                total, sent, batches = agent.sync_once()
                print(f"{args.fog_id}: {total} bornes lues, {sent} etats transmis "
                      f"en {batches} lots ({total - sent} inchanges)", flush=True)
            except (HTTPError, URLError, TimeoutError, OSError, ValueError,
                    RuntimeError, json.JSONDecodeError) as exc:
                print(f"ERREUR de synchronisation ({args.fog_id}): {exc}",
                      file=sys.stderr, flush=True)
                if args.interval == 0:
                    return 1
            if args.interval == 0:
                return 0
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("Agent arrete (Ctrl+C)")
        return 0


if __name__ == "__main__":
    sys.exit(main())

"""Read-only comparison of each real Fog's live pile states with Cloud PostgreSQL.

Requires Docker CLI access to postgres-cloud and running local Fog APIs.
Does not need or print Cloud tokens. Exits 0 when states agree, 2 when they differ,
1 for missing services/broken data.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import subprocess
import sys
from collections import Counter
from urllib.request import Request, urlopen

FOGS = {"fog-a": 18001, "fog-b": 18002, "fog-c": 18003}
STATE_KEYS = ("pile_id", "status", "is_reservable", "reservation_status", "currently_reservable")


def normalize(row: dict) -> tuple:
    """Keep the exact fields shipped by fog_sync_agent.py; booleans normalized."""
    def boolean(value):
        if isinstance(value, bool):
            return value
        if value in ("t", "true", "True", "1"):
            return True
        if value in ("f", "false", "False", "0"):
            return False
        raise ValueError(f"Invalid boolean value: {value!r}")

    return (
        str(row["pile_id"]),
        str(row["status"]),
        boolean(row["is_reservable"]),
        row.get("reservation_status") or None,
        boolean(row["currently_reservable"]),
    )


def compare(local: dict[str, tuple], cloud: dict[str, tuple], samples: int = 3):
    missing = sorted(set(local) - set(cloud))
    extra = sorted(set(cloud) - set(local))
    differences = sorted(k for k in set(local) & set(cloud) if local[k] != cloud[k])
    return {
        "local": len(local), "cloud": len(cloud),
        "missing": len(missing), "extra": len(extra), "different": len(differences),
        "examples": ([(x, "missing_from_cloud") for x in missing[:samples]] +
                     [(x, "extra_in_cloud") for x in extra[:samples]] +
                     [(x, "different_values") for x in differences[:samples]])[:samples],
    }


def read_fogs() -> dict[str, dict[str, tuple]]:
    all_states: dict[str, dict[str, tuple]] = {}
    for fog, port in FOGS.items():
        req = Request(f"http://127.0.0.1:{port}/api/v1/piles", headers={"Accept": "application/json"})
        with urlopen(req, timeout=30) as response:
            items = json.load(response)
        if not isinstance(items, list) or not items:
            raise ValueError(f"{fog}: empty or invalid /piles response")
        states = {}
        for item in items:
            value = normalize(item)
            pid = value[0]
            if pid in states:
                raise ValueError(f"{fog}: duplicate pile_id {pid}")
            states[pid] = value
        all_states[fog] = states
    return all_states


def read_cloud(db_container: str) -> dict[str, dict[str, tuple]]:
    # COPY CSV avoids ambiguities with blank NULL columns.
    sql = """COPY (
      SELECT fog_id, pile_id, status, is_reservable,
             reservation_status, currently_reservable
      FROM pile_states WHERE fog_id IN ('fog-a','fog-b','fog-c')
      ORDER BY fog_id, pile_id
    ) TO STDOUT WITH (FORMAT CSV, NULL '')"""
    process = subprocess.run(
        ["docker", "exec", db_container, "psql", "-U", "fog_admin", "-d", "fog_cloud",
         "-X", "-q", "-v", "ON_ERROR_STOP=1", "-c", sql],
        capture_output=True, text=True, check=True, timeout=50,
    )
    all_states = {fog: {} for fog in FOGS}
    for raw in csv.reader(io.StringIO(process.stdout)):
        if len(raw) != 6:
            raise ValueError(f"Unexpected PostgreSQL row with {len(raw)} columns")
        fog, pile_id, status, reservable, reservation_status, current = raw
        if fog not in all_states:
            raise ValueError(f"Unexpected Fog in PostgreSQL: {fog}")
        row = {
            "pile_id": pile_id,
            "status": status,
            "is_reservable": reservable,
            "reservation_status": reservation_status or None,
            "currently_reservable": current,
        }
        if pile_id in all_states[fog]:
            raise ValueError(f"Duplicate Cloud pile_id {pile_id}")
        all_states[fog][pile_id] = normalize(row)
    return all_states


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare actual Fog SQLite/API states with PostgreSQL Cloud copy (read-only)")
    parser.add_argument("--db-container", default="postgres-cloud")
    args = parser.parse_args()
    try:
        local = read_fogs()
        cloud = read_cloud(args.db_container)
    except (OSError, ValueError, subprocess.CalledProcessError, subprocess.TimeoutExpired,
            json.JSONDecodeError) as exc:
        print(f"ERREUR de lecture : {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    mismatched = False
    print("=== Étape 41 : cohérence Fog API ↔ copie Cloud PostgreSQL (lecture seule) ===")
    for fog in FOGS:
        report = compare(local[fog], cloud[fog])
        good = report["missing"] == report["extra"] == report["different"] == 0
        mismatched = mismatched or not good
        print(f"{fog}: {'OK' if good else 'DÉCALAGE'} — local={report['local']} cloud={report['cloud']} "
              f"absents_cloud={report['missing']} supplémentaires_cloud={report['extra']} "
              f"états_différents={report['different']}")
        for pid, reason in report["examples"]:
            print(f"    exemple: {pid} ({reason})")
    if mismatched:
        print("Les agents synchronisent toutes les ~15 s. Attends 20–30 s puis relance."
              " Si le décalage persiste, examine les logs des agents et de Cloud Sync API.")
        return 2
    print("SUCCÈS : les états actuels des bornes concordent (à l'instant de la lecture).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

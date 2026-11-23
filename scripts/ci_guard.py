#!/usr/bin/env python3
"""Échec CI si Git suit des secrets, bases de production ou sauvegardes.

Ce contrôle regarde la liste git des fichiers *suivis*, pas les fichiers non suivis
sur un PC de développement. Il ne lit ni ne publie le contenu des secrets.
"""
from pathlib import PurePosixPath
import subprocess
import sys


def is_sensitive(path: str) -> bool:
    p = PurePosixPath(path)
    name = p.name.lower()
    parts = [item.lower() for item in p.parts]
    if any(part == "docker-data" or part.startswith("backup-etape") or
           part == "fog-node-bundles" for part in parts):
        return True
    if name == ".env.example" or name.endswith(".env.example"):
        return False
    if name == ".env" or name.startswith(".env.") or name.endswith(".env"):
        return True
    if name.endswith((".db", ".db-wal", ".db-shm", ".sqlite", ".sqlite3",
                      ".sqlite-wal", ".sqlite-shm", ".dump", ".pem", ".key")):
        return True
    if name in ("id_rsa", "id_ed25519"):
        return True
    return False


def main() -> int:
    check = subprocess.run(["git", "ls-files", "-z"], capture_output=True)
    if check.returncode:
        print("ERREUR : cette vérification doit s'exécuter dans un dépôt Git.", file=sys.stderr)
        return 2
    tracked = [raw.decode("utf-8", errors="replace") for raw in check.stdout.split(b"\0") if raw]
    unsafe = sorted(path for path in tracked if is_sensitive(path))
    if unsafe:
        print("ERREUR : Git suit des fichiers potentiellement sensibles :", file=sys.stderr)
        for path in unsafe:
            print("  - " + path, file=sys.stderr)
        print("Retirer de l'index via git rm --cached <chemin> (sans supprimer le fichier local).", file=sys.stderr)
        return 1
    print(f"OK : aucun fichier sensible parmi les {len(tracked)} fichiers suivis par Git.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

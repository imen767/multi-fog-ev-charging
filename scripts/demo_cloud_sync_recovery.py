"""Etape 41 : exercise the REAL FogSyncAgent against disposable local HTTP stand-ins.

No Docker, PostgreSQL, or production Fog is contacted. The fake Fog and fake Cloud
listen on 127.0.0.1 ephemeral ports, and use in-memory data only.
"""
from __future__ import annotations

import contextlib
import importlib.util
import json
import sys
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError

REPO = Path(__file__).resolve().parents[1]
AGENT_PATH = REPO / "cloud" / "fog_sync_agent.py"
if not AGENT_PATH.exists():
    raise SystemExit(f"Code de l'agent introuvable : {AGENT_PATH}")
spec = importlib.util.spec_from_file_location("fog_sync_agent_step41", AGENT_PATH)
assert spec is not None and spec.loader is not None
agentmod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agentmod)


def fake_pile(number: int, status: str = "AVAILABLE") -> dict:
    return {
        "pile_id": f"demo-pile-{number:04d}",
        "status": status,
        "is_reservable": status == "AVAILABLE",
        "reservation_status": None,
        "currently_reservable": status == "AVAILABLE",
    }


@dataclass
class DemoState:
    piles: list[dict] = field(default_factory=lambda: [fake_pile(i) for i in range(604)])
    cloud_states: dict[str, dict] = field(default_factory=dict)
    accepted_batches: list[int] = field(default_factory=list)
    post_attempts: int = 0
    fail_on_attempt: int | None = None
    fail_after_apply: bool = False
    cloud_down: bool = False
    fog_down: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)


class DemoHandler(BaseHTTPRequestHandler):
    state: DemoState
    role: str

    def log_message(self, *args) -> None:
        pass

    def reply(self, status: int, payload: dict | list) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if self.role != "fog":
            self.reply(404, {"detail": "Not Found"})
            return
        with self.state.lock:
            if self.state.fog_down:
                self.reply(503, {"detail": "Fog offline (demo)"})
                return
            if self.path == "/health":
                self.reply(200, {"fog_id": "fog-b", "status": "healthy"})
            elif self.path == "/api/v1/piles":
                self.reply(200, self.state.piles)
            elif self.path == "/api/v1/piles/capacity":
                self.reply(200, {"total_piles": len(self.state.piles)})
            else:
                self.reply(404, {"detail": "Not Found"})

    def do_POST(self) -> None:
        if self.role != "cloud" or self.path != "/api/v1/sync/pile-states":
            self.reply(404, {"detail": "Not Found"})
            return
        if self.headers.get("X-Sync-Token") != "demo-secret":
            self.reply(401, {"detail": "Invalid token"})
            return
        size = int(self.headers.get("Content-Length", "0"))
        try:
            payload = json.loads(self.rfile.read(size))
        except (ValueError, UnicodeDecodeError):
            self.reply(400, {"detail": "Invalid JSON"})
            return
        if payload.get("fog_id") != "fog-b" or not 1 <= len(payload.get("states", [])) <= 500:
            self.reply(422, {"detail": "Invalid batch"})
            return
        with self.state.lock:
            self.state.post_attempts += 1
            fail = self.state.cloud_down or self.state.post_attempts == self.state.fail_on_attempt
            if fail and not self.state.fail_after_apply:
                self.reply(503, {"detail": "Cloud temporarily unavailable (demo)"})
                return
            for item in payload["states"]:
                self.state.cloud_states[item["pile_id"]] = dict(item)
            self.state.accepted_batches.append(len(payload["states"]))
            if fail:
                self.reply(503, {"detail": "Response lost after write (demo)"})
            else:
                self.reply(200, {"status": "synced", "fog_id": "fog-b", "received": len(payload["states"])})


@contextlib.contextmanager
def demo_servers():
    state = DemoState()
    fog_handler = type("FogHandler", (DemoHandler,), {"state": state, "role": "fog"})
    cloud_handler = type("CloudHandler", (DemoHandler,), {"state": state, "role": "cloud"})
    fog = ThreadingHTTPServer(("127.0.0.1", 0), fog_handler)
    cloud = ThreadingHTTPServer(("127.0.0.1", 0), cloud_handler)
    threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in (fog, cloud)]
    for thread in threads:
        thread.start()
    try:
        agent = agentmod.FogSyncAgent(
            "fog-b", f"http://127.0.0.1:{fog.server_port}",
            f"http://127.0.0.1:{cloud.server_port}", "demo-secret"
        )
        yield state, agent
    finally:
        for server in (fog, cloud):
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=3)


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def scenario_partial_outage() -> None:
    with demo_servers() as (state, agent):
        # Premier lot 500 confirmé, second lot 104 refusé AVANT écriture.
        state.fail_on_attempt = 2
        try:
            agent.sync_once()
        except HTTPError as exc:
            check(exc.code == 503, f"HTTP inattendu {exc.code}")
        else:
            raise AssertionError("Une panne 503 était attendue")
        check(len(agent.confirmed) == 500, "Le cache doit ne confirmer que 500 bornes")
        check(len(state.cloud_states) == 500, "500 bornes seulement doivent être côté Cloud")
        state.fail_on_attempt = None
        check(agent.sync_once() == (604, 104, 1), "Reprise : 104 états restants attendus")
        check(agent.sync_once() == (604, 0, 0), "Un cycle sans changements doit envoyer 0")
        check(len(state.cloud_states) == 604, "Cloud doit contenir les 604 états")


def scenario_network_outage() -> None:
    with demo_servers() as (state, agent):
        check(agent.sync_once() == (604, 604, 2), "Initialisation complète attendue")
        state.piles[0] = fake_pile(0, "OCCUPIED")
        state.cloud_down = True
        try:
            agent.sync_once()
        except HTTPError as exc:
            check(exc.code == 503, f"HTTP inattendu {exc.code}")
        else:
            raise AssertionError("Le Cloud devait refuser la requête")
        check(state.cloud_states["demo-pile-0000"]["status"] == "AVAILABLE", "Ancien état Cloud attendu")
        state.cloud_down = False
        check(agent.sync_once() == (604, 1, 1), "Un seul état doit être renvoyé après reprise")
        check(state.cloud_states["demo-pile-0000"]["status"] == "OCCUPIED", "Nouvel état Cloud attendu")


def scenario_ambiguous_ack() -> None:
    with demo_servers() as (state, agent):
        # Le cloud a écrit le premier lot mais perd sa réponse.
        state.fail_on_attempt = 1
        state.fail_after_apply = True
        try:
            agent.sync_once()
        except HTTPError as exc:
            check(exc.code == 503, "La réponse devrait être un 503 simulé")
        else:
            raise AssertionError("Un accusé de réception perdu était attendu")
        check(len(agent.confirmed) == 0, "Sans ACK, ne pas confirmer le cache")
        check(len(state.cloud_states) == 500, "Mais le Cloud a bien le premier lot")
        state.fail_on_attempt = None
        state.fail_after_apply = False
        check(agent.sync_once() == (604, 604, 2), "Après ACK perdu, retransmettre les 604 bornes")
        check(len(state.cloud_states) == 604, "L'UPSERT simulé ne doit pas créer de doublons")


def scenario_fog_down() -> None:
    with demo_servers() as (state, agent):
        state.fog_down = True
        try:
            agent.sync_once()
        except HTTPError as exc:
            check(exc.code == 503, "Le Fog était censé répondre 503")
        else:
            raise AssertionError("Une panne du Fog était attendue")
        check(state.post_attempts == 0, "Aucun état ne doit être envoyé si le Fog ne répond pas")
        state.fog_down = False
        check(agent.sync_once() == (604, 604, 2), "Après retour Fog, synchroniser normalement")


def run() -> None:
    scenarios = [
        ("Coupure au milieu des lots (500/104)", scenario_partial_outage),
        ("Cloud indisponible puis retour (delta)", scenario_network_outage),
        ("Réponse perdue après écriture (idempotence)", scenario_ambiguous_ack),
        ("Fog indisponible puis retour", scenario_fog_down),
    ]
    print("=== Étape 41 : simulation locale isolée Cloud Sync ===")
    print("Aucun conteneur Docker réel et aucune base de données contactés.\n")
    for title, callback in scenarios:
        callback()
        print(f"PASS — {title}")
    print("SUCCÈS : 4 scénarios de reprise validés sur doubles HTTP locaux.")


if __name__ == "__main__":
    try:
        run()
    except Exception as exc:
        print(f"ÉCHEC : {type(exc).__name__} : {exc}", file=sys.stderr)
        raise SystemExit(1)

"""Real HTTP end-to-end demonstration against three ISOLATED mini-Fog services."""
import json
import httpx
from simulator.cooperative_orchestrator import FogCooperativeRoundOrchestrator

FOG_URLS = {name: f"http://demo-{name}:8000" for name in ("fog-a", "fog-b", "fog-c")}

def capacity(fog_id):
    r = httpx.get(FOG_URLS[fog_id] + "/api/v1/piles/capacity", timeout=10)
    r.raise_for_status()
    return r.json()

def run():
    with httpx.Client(timeout=15.0) as client:
        orchestrator = FogCooperativeRoundOrchestrator(FOG_URLS, client=client)
        initial = {fog: capacity(fog) for fog in FOG_URLS}
        print("Capacités initiales :", json.dumps({fog: {"total":c["total_piles"], "available":c["available_piles"]} for fog,c in initial.items()}, ensure_ascii=False))
        assert [initial[k]["total_piles"] for k in FOG_URLS] == [2,1,3], "Mauvais catalogue de démonstration"
        assert initial["fog-b"]["available_piles"] == 1, "Fog B devrait avoir 1 borne libre"

        print("\nTICK 1 : première voiture arrive sur B et occupe la seule borne disponible")
        first = orchestrator.run_round({"fog-b": [{"vehicle_id":"demo-b-local-1", "required_power_kw":22}]}, {})
        print(json.dumps({"tick": first["tick"], "transfers": first["transfers"], "fog_states": first["fog_states"]}, ensure_ascii=False, indent=2))
        assert not first["transfers"], "Un transfert inattendu au tick 1"
        assert capacity("fog-b")["available_piles"] == 0, "Fog B n'est pas saturé"

        print("\nTICK 2 : deuxième voiture arrive sur B ; B doit demander de l'aide directement")
        second = orchestrator.run_round({"fog-b": [{"vehicle_id":"demo-b-remote-2", "required_power_kw":22}]}, {})
        print(json.dumps({"tick":second["tick"], "cooperation_triggered":second["cooperation_triggered"], "transfers":second["transfers"], "total_unserved":second["total_unserved"], "fog_states":second["fog_states"]}, ensure_ascii=False, indent=2))
        assert second["cooperation_triggered"], "Pas de coopération détectée"
        assert second["total_unserved"] == 0, "Une voiture reste non servie"
        transfers = second["transfers"]
        assert len(transfers) == 1 and transfers[0]["from_fog"] == "fog-b", "Transfert absent ou origine incorrecte"
        assert transfers[0]["to_fog"] in {"fog-a", "fog-c"}, "Destination inattendue"
        dest = transfers[0]["to_fog"]
        assert capacity(dest)["occupied_piles"] == 1, "Destination pas effectivement occupée"

        r = client.get(FOG_URLS[dest] + "/api/v1/piles")
        r.raise_for_status()
        active = [p for p in r.json() if p.get("status") == "OCCUPIED" and p.get("reservation_status") == "ACTIVE"]
        assert len(active) == 1, f"L'activation distante n'est pas visible chez {dest}"
        print("\nSUCCÈS : Fog B saturé → aide directe → HOLD → CONFIRM → ACTIVATE chez " + dest)
        print("Borne distante active :", active[0]["pile_id"], " | hold_id :", transfers[0]["hold_id"])
        print("Aucun orchestrateur Cloud de production et aucune base PostgreSQL de production n'ont été sollicités.")

if __name__ == "__main__":
    run()

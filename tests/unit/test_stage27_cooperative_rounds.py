"""Regression tests for Fog-driven (not Cloud-driven) cooperation."""
import json
import httpx
import pytest

from simulator.cooperative_orchestrator import FogCooperativeRoundOrchestrator
from fog.services import round_service
from fog.services import peer_communication_service


def test_cloud_only_synchronizes_and_fog_cooperation_is_reported():
    calls = []
    addresses = {"fog-a": "http://fog-a:8000", "fog-b": "http://fog-b:8000", "fog-c": "http://fog-c:8000"}

    def handler(request):
        fog = request.url.host
        body = json.loads(request.content) if request.content else {}
        calls.append((fog, request.url.path, body))
        if request.url.path.endswith("/prepare"):
            return httpx.Response(200, json={
                "fog_id": fog, "tick": body["tick"], "status": "PREPARED",
                "unserved_vehicles": ["vehicle-B"] if fog == "fog-b" else [],
                "available_piles": 0 if fog == "fog-b" else 10,
            })
        if request.url.path.endswith("/settle"):
            assert body["remote_transfers"] == []
            assert body["trigger_cooperation"] is True
            return httpx.Response(200, json={
                "fog_id": fog, "tick": body["tick"], "status": "SETTLED",
                "remaining_unserved": [],
                "cooperation_results": [
                    {"vehicle_id": "vehicle-B", "status": "ACTIVE",
                     "destination_fog": "fog-c", "hold_id": "hold-B",
                     "pile_ids": ["pile-C"]}
                ] if fog == "fog-b" else [],
            })
        if request.url.path.endswith("/status"):
            return httpx.Response(200, json={
                "fog_id": fog, "tick": 1, "phase": "SETTLED",
                "available_piles": 9, "occupied_piles": 1,
            })
        return httpx.Response(404)

    orchestrator = FogCooperativeRoundOrchestrator(
        addresses, client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    result = orchestrator.run_round({"fog-b": ["vehicle-B"]}, {})
    assert result["arrivals_requested"] == 1
    assert result["locally_started"] == 0
    assert result["remote_started"] == 1
    assert result["cooperation_triggered"] is True
    assert result["transfers"] == [
        {"from_fog": "fog-b", "to_fog": "fog-c", "vehicles": ["vehicle-B"],
         "hold_id": "hold-B", "pile_ids": ["pile-C"]}
    ]
    assert len([x for x in calls if x[1].endswith("/prepare")]) == 3
    assert len([x for x in calls if x[1].endswith("/settle")]) == 3
    # No Cloud POST to another Fog to place a vehicle.
    assert not any("reservations" in path for _,path,_ in calls)


def _reset_round_state():
    with round_service._lock:
        round_service._round_state.update({
            "tick": 0, "phase": "IDLE", "unserved_vehicles": [],
            "unserved_requests": [], "served_sessions": [], "released_piles": [],
            "prepare_result": None, "settle_result": None,
        })


def test_fog_requests_one_hold_per_unserved_vehicle_and_activates(monkeypatch):
    _reset_round_state()
    monkeypatch.setattr(round_service, "_save_round_checkpoint", lambda tick: None)
    monkeypatch.setattr(round_service, "FOG_ID", "fog-b")
    monkeypatch.setattr(round_service, "create_local_session", lambda **kw: None)
    monkeypatch.setattr(round_service, "get_pile_capacity", lambda: {"available_piles":0,"occupied_piles":7403})
    monkeypatch.setattr(round_service, "complete_hold", lambda hold_id, **kw: None)
    monkeypatch.setattr(round_service, "list_due_hold_ids", lambda tick: [])
    seen = []
    def help_peer(request):
        seen.append((request.required_piles, request.required_power_kw, request.request_id))
        return {"status": "HOLD_GRANTED", "hold": {"hold_id": f"hold-{len(seen)}"},
                "selected_proposal": {"proposer_fog": "fog-c"}}
    monkeypatch.setattr(peer_communication_service, "send_help_request_to_peers", help_peer)
    monkeypatch.setattr(peer_communication_service, "confirm_and_activate_remote_hold",
                        lambda answer, **kw: {"status": "ACTIVE", "destination_fog": "fog-c",
                                        "hold_id": answer["hold"]["hold_id"], "pile_ids": ["p1"]})
    prep = round_service.prepare_round(1, [
        {"vehicle_id":"v1", "required_power_kw":22},
        {"vehicle_id":"v2", "required_power_kw":50},
    ], [])
    assert prep["unserved_vehicles"] == ["v1", "v2"]
    result = round_service.settle_round(1, [], True)
    assert result["remaining_unserved"] == []
    assert [x["status"] for x in result["cooperation_results"]] == ["ACTIVE", "ACTIVE"]
    assert [x[:2] for x in seen] == [(1,22), (1,50)]
    # A duplicate SETTLE must not create new HOLDs.
    assert round_service.settle_round(1, [], True) == result
    assert len(seen) == 2


def test_hold_alone_is_not_counted_as_charging(monkeypatch):
    _reset_round_state()
    monkeypatch.setattr(round_service, "_save_round_checkpoint", lambda tick: None)
    monkeypatch.setattr(round_service, "FOG_ID", "fog-b")
    monkeypatch.setattr(round_service, "create_local_session", lambda **kw: None)
    monkeypatch.setattr(round_service, "get_pile_capacity", lambda: {"available_piles":0,"occupied_piles":7403})
    monkeypatch.setattr(round_service, "complete_hold", lambda hold_id, **kw: None)
    monkeypatch.setattr(round_service, "list_due_hold_ids", lambda tick: [])
    monkeypatch.setattr(peer_communication_service, "send_help_request_to_peers", lambda request: {
        "status": "HOLD_GRANTED", "hold": {"hold_id": "h2"},
        "selected_proposal": {"proposer_fog": "fog-c"},
    })
    monkeypatch.setattr(peer_communication_service, "confirm_and_activate_remote_hold", lambda answer, **kw: {
        "status": "FAILED", "hold_id": "h2", "stage": "ACTIVATE"})
    round_service.prepare_round(1, [{"vehicle_id":"v1","required_power_kw":22}], [])
    result = round_service.settle_round(1, [], True)
    assert result["remaining_unserved"] == ["v1"]
    assert result["cooperation_results"][0]["status"] == "FAILED"


def test_activation_protocol_posts_confirm_then_activate(monkeypatch):
    monkeypatch.setattr(peer_communication_service, "FOG_ID", "fog-b")
    monkeypatch.setattr(peer_communication_service, "FOG_URLS", {"fog-c":"http://fog-c:8000"})
    calls=[]
    def fake_post(url, json, timeout):
        calls.append((url, json))
        state = "CONFIRMED" if url.endswith("/confirm") else "ACTIVE"
        return httpx.Response(200, json={"status":state, "pile_ids":["p1"]}, request=httpx.Request("POST", url))
    monkeypatch.setattr(peer_communication_service.httpx, "post", fake_post)
    result = peer_communication_service.confirm_and_activate_remote_hold({
        "hold": {"hold_id": "h1"}, "selected_proposal": {"proposer_fog": "fog-c"}})
    assert result["status"] == "ACTIVE"
    assert [url.rsplit("/",1)[-1] for url,_ in calls] == ["confirm", "activate"]


def test_fog_no_proposal_keeps_vehicle_unserved(monkeypatch):
    _reset_round_state()
    monkeypatch.setattr(round_service, "_save_round_checkpoint", lambda tick: None)
    monkeypatch.setattr(round_service, "FOG_ID", "fog-b")
    monkeypatch.setattr(round_service, "create_local_session", lambda **kw: None)
    monkeypatch.setattr(round_service, "get_pile_capacity", lambda: {"available_piles":0,"occupied_piles":7403})
    monkeypatch.setattr(round_service, "complete_hold", lambda hold_id, **kw: None)
    monkeypatch.setattr(round_service, "list_due_hold_ids", lambda tick: [])
    monkeypatch.setattr(peer_communication_service, "send_help_request_to_peers", lambda request: {
        "status": "NO_PROPOSALS", "hold":None})
    round_service.prepare_round(1, [{"vehicle_id":"v1","required_power_kw":22}], [])
    result = round_service.settle_round(1, [], True)
    assert result["remaining_unserved"] == ["v1"]
    assert result["cooperation_results"][0]["status"] == "UNSERVED"


def test_round_checkpoint_persists_last_tick(monkeypatch, tmp_path):
    p = tmp_path / ".round_checkpoint.json"
    monkeypatch.setattr(round_service, "_CHECKPOINT_PATH", p)
    round_service._save_round_checkpoint(12)
    assert round_service._read_round_checkpoint() == (12, "SETTLED")

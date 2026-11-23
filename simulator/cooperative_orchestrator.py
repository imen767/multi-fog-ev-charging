"""Cooperative variant of the existing distributed round orchestrator.

The Cloud coordinates PREPARE and SETTLE, but never chooses a receiving peer.
Each Fog initiates its own HTTP HELP_REQUEST / HOLD / CONFIRM / ACTIVATE.

This class intentionally leaves the original DistributedRoundOrchestrator intact
for backwards compatibility with your binome's existing tests.
"""

from simulator.orchestrator import DistributedRoundOrchestrator


class FogCooperativeRoundOrchestrator(DistributedRoundOrchestrator):
    def run_round(self, arrivals, completions, ml_predictions=None):
        self.current_tick += 1
        tick = self.current_tick
        prepare_results = {}

        # 1. All Fog nodes process their own arrivals before any SETTLE starts.
        for fog_id, url in self.fog_urls.items():
            vehicles = [
                {"vehicle_id": vehicle, "required_power_kw": 22.0}
                if isinstance(vehicle, str) else vehicle
                for vehicle in arrivals.get(fog_id, [])
            ]
            response = self._client.post(
                f"{url}/api/v1/round/prepare",
                json={"tick": tick, "arrivals": vehicles,
                      "completions": completions.get(fog_id, [])},
            )
            response.raise_for_status()
            result = response.json()
            if result.get("fog_id") != fog_id or result.get("tick") != tick or result.get("status") != "PREPARED":
                raise ValueError(f"Invalid PREPARE response from {fog_id}: {result}")
            prepare_results[fog_id] = result

        # Global PREPARE barrier reached: now each Fog negotiates with its peers.
        all_unserved = {}
        transfers = []
        cooperation_triggered = False
        cooperation_events = []

        for fog_id, url in self.fog_urls.items():
            response = self._client.post(
                f"{url}/api/v1/round/settle",
                json={"tick": tick, "remote_transfers": [], "trigger_cooperation": True},
            )
            response.raise_for_status()
            result = response.json()
            if result.get("fog_id") != fog_id or result.get("tick") != tick or result.get("status") != "SETTLED":
                raise ValueError(f"Invalid SETTLE response from {fog_id}: {result}")
            all_unserved[fog_id] = result.get("remaining_unserved", [])

            for item in result.get("cooperation_results", []):
                for event in item.get("trace", []):
                    cooperation_events.append({**event, "tick": tick,
                                               "request_id": item.get("request_id"),
                                               "vehicle_id": item.get("vehicle_id")})
                if item.get("status") == "ACTIVE":
                    cooperation_triggered = True
                    transfers.append({
                        "from_fog": fog_id,
                        "to_fog": item["destination_fog"],
                        "vehicles": [item["vehicle_id"]],
                        "hold_id": item["hold_id"],
                        "pile_ids": item.get("pile_ids", []),
                    })

        # Re-read ALL states, since a Fog can receive a remote reservation after
        # its own SETTLE response was already returned to the orchestrator.
        fog_states = {}
        for fog_id, url in self.fog_urls.items():
            response = self._client.get(f"{url}/api/v1/round/status")
            response.raise_for_status()
            state = response.json()
            if state.get("fog_id") != fog_id or state.get("tick") != tick or state.get("phase") != "SETTLED":
                raise ValueError(f"Fog {fog_id} did not finish SETTLE for tick {tick}")
            fog_states[fog_id] = {
                "available": state["available_piles"],
                "occupied": state["occupied_piles"],
            }

        # Round-level measured metrics. These are in RAM only until a persistent
        # per-round journal is implemented; never present them as all-time totals.
        arrivals_requested = sum(len(arrivals.get(fog_id, [])) for fog_id in self.fog_urls)
        locally_started = sum(len(result.get("served_sessions", [])) for result in prepare_results.values())
        remote_started = len(transfers)  # Each entry represents one ACTIVE remote session.
        auto_completed = sum(int(result.get("auto_completed", 0)) for result in prepare_results.values())
        explicit_completed = sum(int(result.get("explicit_completed", 0)) for result in prepare_results.values())

        summary = {
            "tick": tick,
            "arrivals_requested": arrivals_requested,
            "locally_started": locally_started,
            "remote_started": remote_started,
            "auto_completed": auto_completed,
            "explicit_completed": explicit_completed,
            "unserved": all_unserved,
            "total_unserved": sum(map(len, all_unserved.values())),
            "transfers": transfers,
            "cooperation_triggered": cooperation_triggered,
            "cooperation_events": cooperation_events,
            "ml_predictions": ml_predictions or {},
            "fog_states": fog_states,
        }
        self.history.append(summary)
        return summary

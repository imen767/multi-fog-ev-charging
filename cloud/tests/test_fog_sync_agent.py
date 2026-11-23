import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE_PATH = Path(__file__).resolve().parents[1] / "fog_sync_agent.py"
spec = importlib.util.spec_from_file_location("fog_sync_agent", MODULE_PATH)
agentmod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agentmod)


def pile(n, status="AVAILABLE"):
    return {
        "pile_id": str(n), "status": status,
        "is_reservable": True, "reservation_status": None,
        "currently_reservable": status == "AVAILABLE",
    }


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.piles = [pile(n) for n in range(1001, 1605)]  # 604 -> 2 lots
        self.payloads = []
        self.agent = agentmod.FogSyncAgent(
            "fog-b", "http://fog.test", "http://cloud.test", "test-token"
        )

    def fake_get(self, url):
        if url.endswith("/health"):
            return {"fog_id": "fog-b", "status": "healthy"}
        if url.endswith("/capacity"):
            return {"total_piles": len(self.piles)}
        return list(self.piles)

    def fake_post(self, url, payload, token):
        self.assertEqual(token, "test-token")
        self.assertLessEqual(len(payload["states"]), 500)
        self.payloads.append(payload)
        return {"status": "synced", "fog_id": "fog-b",
                "received": len(payload["states"])}

    def test_initial_then_delta_then_no_change(self):
        with patch.object(agentmod, "get_json", side_effect=self.fake_get), \
             patch.object(agentmod, "post_json", side_effect=self.fake_post):
            self.assertEqual(self.agent.sync_once(), (604, 604, 2))
            self.assertEqual(self.agent.sync_once(), (604, 0, 0))
            self.piles[20] = pile(1021, status="OCCUPIED")
            self.assertEqual(self.agent.sync_once(), (604, 1, 1))
        self.assertEqual(len(self.payloads), 3)
        self.assertEqual(self.payloads[-1]["states"][0]["pile_id"], "1021")

    def test_failed_batch_is_retried(self):
        fail = True

        def post(url, payload, token):
            nonlocal fail
            if fail:
                fail = False
                raise RuntimeError("cloud indisponible")
            return self.fake_post(url, payload, token)

        with patch.object(agentmod, "get_json", side_effect=self.fake_get), \
             patch.object(agentmod, "post_json", side_effect=post):
            with self.assertRaises(RuntimeError):
                self.agent.sync_once()
            self.assertEqual(self.agent.sync_once(), (604, 604, 2))

    def test_wrong_fog_rejected(self):
        with patch.object(agentmod, "get_json", return_value={"fog_id": "fog-a"}):
            with self.assertRaisesRegex(ValueError, "fog_id"):
                self.agent.sync_once()


if __name__ == "__main__":
    unittest.main()

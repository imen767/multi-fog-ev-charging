import importlib.util
from pathlib import Path
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "release_audit_step49.py"
spec = importlib.util.spec_from_file_location("release_audit_step49", SCRIPT)
assert spec and spec.loader
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class AuditTests(unittest.TestCase):
    def test_preflight_failure_is_blocking(self):
        with tempfile.TemporaryDirectory() as directory:
            result = mod.audit(Path(directory), fetch=lambda _: True,
                               preflight=lambda _: {"status": "NON_PRET", "error": "wrong tick"},
                               git_summary=lambda _: {"status": "OK", "modifications_locales": 0})
            assert result["status"] == "A_CORRIGER"
            assert any("wrong tick" in x for x in result["blocages"])

    def test_preflight_success_and_present_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for file in mod.REQUIRED_FILES:
                dest = root / file
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text("", encoding="utf-8")
            result = mod.audit(root, fetch=lambda _: True,
                               preflight=lambda _: {"status": "OK", "current_tick": 8,
                                                    "simulation_run_id": "run-1", "fogs": {}},
                               git_summary=lambda _: {"status": "OK", "modifications_locales": 0})
            assert result["status"] == "PRET_POUR_PREPARATION"
            assert result["current_tick"] == 8
            assert result["blocages"] == []

    def test_down_service_blocks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for file in mod.REQUIRED_FILES:
                dest = root / file
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text("", encoding="utf-8")
            result = mod.audit(root, fetch=lambda url: "18082" not in url,
                               preflight=lambda _: {"status": "OK", "current_tick": 8},
                               git_summary=lambda _: {"status": "OK", "modifications_locales": 0})
            assert result["status"] == "A_CORRIGER"
            assert result["services"]["chronos-2"] == "NON_HEALTHY"


if __name__ == "__main__":
    unittest.main()

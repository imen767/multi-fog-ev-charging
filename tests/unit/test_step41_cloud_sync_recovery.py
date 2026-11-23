"""Unit test wrappers for the isolated step 41 HTTP integration scenarios."""
import importlib.util
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "demo_cloud_sync_recovery.py"
_spec = importlib.util.spec_from_file_location("step41_cloud_sync_demo", _SCRIPT)
assert _spec and _spec.loader
_module = importlib.util.module_from_spec(_spec)
import sys
sys.modules[_spec.name] = _module
_spec.loader.exec_module(_module)


def test_partial_outage():
    _module.scenario_partial_outage()


def test_network_outage():
    _module.scenario_network_outage()


def test_ambiguous_ack():
    _module.scenario_ambiguous_ack()


def test_fog_down():
    _module.scenario_fog_down()

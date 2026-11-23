import importlib.util
import sys
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_cloud_sync_consistency.py"
_spec = importlib.util.spec_from_file_location("step41_sync_check", _SCRIPT)
assert _spec and _spec.loader
_module = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _module
_spec.loader.exec_module(_module)


def row(pid="p1", status="AVAILABLE", reservable=True, reservation=None):
    return {"pile_id": pid, "status": status, "is_reservable": reservable,
            "reservation_status": reservation, "currently_reservable": reservable}


def test_bool_normalization():
    a = _module.normalize(row())
    b = _module.normalize({**row(), "is_reservable": "t", "currently_reservable": "true"})
    assert a == b


def test_difference_report():
    a = {"p1": _module.normalize(row()), "p2": _module.normalize(row("p2"))}
    b = {"p1": _module.normalize(row(status="OCCUPIED", reservable=False)),
         "p3": _module.normalize(row("p3"))}
    report = _module.compare(a, b)
    assert report["missing"] == report["extra"] == report["different"] == 1


def test_equal_report():
    a = {"p1": _module.normalize(row())}
    report = _module.compare(a, dict(a))
    assert report["missing"] == report["extra"] == report["different"] == 0

"""Non-Docker unit tests for the read-only validator and isolated scenario runner."""
import importlib.util
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]

def load(path, module_name):
    spec = importlib.util.spec_from_file_location(module_name, ROOT / path)
    obj = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(obj)
    return obj


def test_readonly_health_consistent():
    m = load('scripts/check_runtime_integrity.py', 'readonly40')
    states = {f: {'fog_id': f, 'tick': 8, 'phase': 'SETTLED',
                  'available_piles': 10, 'occupied_piles': 1} for f in m.FOGS}
    caps = {f: {'fog_id': f, 'available_piles': 10, 'occupied_piles': 1} for f in m.FOGS}
    assert m.check_states(states, caps, {'current_tick': 8, 'history_pending_tick': None}) == []


def test_readonly_detects_pending_and_tick_mismatch():
    m = load('scripts/check_runtime_integrity.py', 'readonly40b')
    states = {f: {'fog_id': f, 'tick': 8, 'phase': 'SETTLED',
                  'available_piles': 10, 'occupied_piles': 1} for f in m.FOGS}
    caps = {f: {'fog_id': f, 'available_piles': 10, 'occupied_piles': 1} for f in m.FOGS}
    states['fog-c']['tick'] = 7
    result = m.check_states(states, caps, {'current_tick': 8, 'history_pending_tick': 8})
    assert any('pending' in err for err in result)
    assert any('Tick mismatch' in err for err in result)


def test_runner_is_hardcoded_to_disposable_demo_project_only():
    m = load('cloud/demo_cooperation/run_resilience.py', 'demo40')
    assert m.PROJECT == 'fog-resilience-demo'
    assert [m.ENDPOINTS[f] for f in ('fog-a', 'fog-b', 'fog-c')] == [
        'http://127.0.0.1:18201', 'http://127.0.0.1:18202', 'http://127.0.0.1:18203'
    ]
    calls = []
    def fake_run(command, **kwargs):
        calls.append(command)
        class Done:
            returncode = 0
            stdout = 'ok'
            stderr = ''
        return Done()
    with patch.object(m.subprocess, 'run', side_effect=fake_run):
        assert m.docker_compose('stop', 'demo-fog-a') == 'ok'
    assert calls[0][0:5] == ['docker', 'compose', '-p', 'fog-resilience-demo', '-f']
    assert calls[0][-2:] == ['stop', 'demo-fog-a']


def test_run_is_explicit_opt_in():
    m = load('cloud/demo_cooperation/run_resilience.py', 'demo40b')
    with patch('sys.argv', ['run_resilience.py']):
        with patch.object(m, 'run_scenarios') as scenarios:
            m.main()
        scenarios.assert_not_called()

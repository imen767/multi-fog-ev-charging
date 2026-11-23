#!/usr/bin/env python3
"""Safe preflight + online SQLite backups for Step 35 (single-PC WSL layout).

Does NOT change Fog data or the orchestrator. Run after extracting the ZIP and
before rebuilding Docker services. Assumes docker-compose Fog data bind mounts
exist under ./docker-data/, as in the user's current project.
"""
import json
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
FOG_PORTS = {'fog-a': 18001, 'fog-b': 18002, 'fog-c': 18003}


def get_json(url):
    with urlopen(url, timeout=8) as response:
        return json.load(response)


def main():
    orch = get_json('http://127.0.0.1:18081/api/v1/orchestrator/status')
    if orch.get('history_pending_tick') is not None:
        raise SystemExit('STOP: historical capture pending. Resolve before rebuilding Fog.')
    ticks = set()
    statuses = {}
    for fog, port in FOG_PORTS.items():
        status = get_json(f'http://127.0.0.1:{port}/api/v1/round/status')
        if status.get('fog_id') != fog or status.get('phase') != 'SETTLED':
            raise SystemExit(f'STOP: {fog} is not SETTLED: {status}')
        ticks.add(status['tick'])
        statuses[fog] = status
    if ticks != {orch['current_tick']}:
        raise SystemExit(f'STOP: Fog ticks {sorted(ticks)} disagree with Cloud tick {orch["current_tick"]}')
    tick = orch['current_tick']
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    destination = ROOT / 'backup-etape35' / f'tick-{tick}-{stamp}'
    data_paths = {}
    for fog in FOG_PORTS:
        letter = fog[-1]
        folder = ROOT / 'docker-data' / fog
        db_file = folder / f'fog_{letter}.db'
        checkpoint = folder / '.round_checkpoint.json'
        if not db_file.is_file() or not checkpoint.is_file():
            raise SystemExit(f'STOP: missing Fog database or checkpoint: {db_file}, {checkpoint}')
        data_paths[fog] = (db_file, checkpoint)

    destination.mkdir(parents=True, exist_ok=False)
    for fog, (db_file, checkpoint) in data_paths.items():
        target = destination / fog
        target.mkdir()
        with sqlite3.connect(f'file:{db_file}?mode=ro', uri=True) as origin:
            with sqlite3.connect(target / db_file.name) as saved:
                origin.backup(saved)
                ok = saved.execute('PRAGMA integrity_check').fetchone()[0]
                if ok != 'ok':
                    raise RuntimeError(f'Backup integrity check failed for {fog}: {ok}')
        shutil.copy2(checkpoint, target / checkpoint.name)
    (destination / 'manifest.json').write_text(json.dumps({
        'tick': tick,
        'simulation_run_id': orch.get('simulation_run_id'),
        'fog_statuses': statuses,
        'created_at_utc': stamp,
        'description': 'Step 35 backup, no secrets included',
    }, indent=2), encoding='utf-8')
    print(f'BACKUP OK: {destination}')
    print(f'Tick {tick}, all 3 Fogs SETTLED, pending history = None')


if __name__ == '__main__':
    main()

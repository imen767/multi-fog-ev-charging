"""One-time initialization. Never regenerate on normal container restart."""
from pathlib import Path
from uuid import UUID, uuid4

file = Path(__file__).resolve().parents[1] / '.env.cloud'
if not file.exists():
    raise SystemExit('.env.cloud not found; stop, do not create a new configuration')
lines = file.read_text(encoding='utf-8').splitlines()
values = [v.split('=', 1)[1].strip() for v in lines if v.startswith('SIMULATION_RUN_ID=')]
if values:
    if len(values) != 1:
        raise SystemExit('Duplicate SIMULATION_RUN_ID entries; fix manually')
    print('Existing SIMULATION_RUN_ID:', str(UUID(values[0])))
    print('Keeping existing run; do not regenerate on routine restart')
else:
    run_id = str(uuid4())
    with file.open('a', encoding='utf-8') as f:
        f.write('\nSIMULATION_RUN_ID=' + run_id + '\n')
    print('Created stable SIMULATION_RUN_ID:', run_id)
    print('Keep this ID across Cloud restarts for the same Fog simulation')

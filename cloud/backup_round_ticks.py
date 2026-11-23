"""Run ONCE before replacing/restarting Fog containers (Stage 27).

Copies the current /round/status of each running Fog into its persistent
/app/data/.round_checkpoint.json; refuses to proceed if rounds differ or are
in PREPARED state, or if PostgreSQL has a newer tick.
"""
import json
import subprocess
from pathlib import Path
from uuid import UUID
from urllib.request import urlopen

FOG_URLS = {"fog-a": "http://localhost:18001", "fog-b": "http://localhost:18002", "fog-c": "http://localhost:18003"}


def run_command(argv, input_text=None):
    return subprocess.run(argv, input=input_text, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          check=True).stdout.strip()


def main():
    statuses = {}
    for fog_id, url in FOG_URLS.items():
        with urlopen(f"{url}/api/v1/round/status", timeout=10) as response:
            status = json.load(response)
        if status.get("fog_id") != fog_id:
            raise RuntimeError(f"Unexpected Fog identity: {status}")
        statuses[fog_id] = status
        print(f"{fog_id}: tick={status['tick']} phase={status['phase']}")

    ticks = {status["tick"] for status in statuses.values()}
    phases = {status["phase"] for status in statuses.values()}
    if len(ticks) != 1 or phases not in ({"SETTLED"}, {"IDLE"}):
        raise RuntimeError("Fog ticks/phases disagree or a round is in progress. Stop: do not restart Fog containers.")

    tick = ticks.pop()
    env_file = Path(__file__).resolve().parents[1] / ".env.cloud"
    config = dict(line.split("=", 1) for line in env_file.read_text().splitlines()
                  if line.startswith("SIMULATION_RUN_ID="))
    try:
        run_id = str(UUID(config["SIMULATION_RUN_ID"].strip()))
    except (KeyError, ValueError):
        raise RuntimeError("Missing stable SIMULATION_RUN_ID in .env.cloud")
    latest = int(run_command([
        "docker", "exec", "postgres-cloud", "psql", "-At", "-U", "fog_admin", "-d", "fog_cloud",
        "-c", f"SELECT COALESCE(MAX(simulation_tick), 0) FROM occupancy_history WHERE simulation_run_id='{run_id}'::uuid;",
    ]))
    if latest != tick:
        raise RuntimeError(
            f"PostgreSQL history reached tick {latest}, but Fogs report tick {tick}. "
            "Do not restart. Reconcile rounds manually."
        )

    # No pending history write should be left unresolved before restarting.
    with urlopen("http://localhost:18081/api/v1/orchestrator/status", timeout=10) as response:
        orchestrator_status = json.load(response)
    if orchestrator_status.get("history_pending_tick") is not None:
        raise RuntimeError("Cloud Orchestrator has a pending history write: resolve it first")

    py_script = """import json, pathlib, sys, os
p = pathlib.Path('/app/data/.round_checkpoint.json')
if not p.parent.is_dir(): raise SystemExit('Persistent Fog volume missing')
data = json.load(sys.stdin)
q = p.with_suffix('.tmp')
q.write_text(json.dumps(data), encoding='utf-8')
os.replace(q, p)
print(str(p))
"""
    for fog_id in FOG_URLS:
        run_command(["docker", "exec", "-i", fog_id, "python", "-c", py_script],
                    json.dumps({"tick": tick, "phase": "SETTLED"}))
    print(f"OK: persisted checkpoint of tick {tick} on the 3 Fog volumes")


if __name__ == "__main__":
    main()

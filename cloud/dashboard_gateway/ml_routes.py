"""Gateway de lecture des prévisions du run courant (étape 43).

Le secret ML reste côté serveur. Aucun changement d'état Fog/Cloud n'est déclenché.
Ce service de développement est exposé uniquement sur loopback dans Docker Compose.
"""
import math
import os
import threading
from typing import Literal

import httpx
from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/api/v1/ml", tags=["live-chronos"])
FOGS = ("fog-a", "fog-b", "fog-c")
_cache_lock = threading.Lock()
_cache: dict[tuple[str, int, str], dict] = {}


def _urls() -> tuple[str, str, str]:
    cloud = os.getenv("CLOUD_ML_URL", "").rstrip("/")
    orchestrator = os.getenv("ORCHESTRATOR_URL", "http://cloud-orchestrator:8000").rstrip("/")
    token = os.getenv("CLOUD_ML_API_TOKEN", "")
    if not cloud or not token:
        raise HTTPException(status_code=503, detail="ML Cloud non configuré dans la passerelle")
    return cloud, orchestrator, token


def _get_json(client: httpx.Client, url: str, headers: dict | None = None) -> dict:
    r = client.get(url, headers=headers)
    r.raise_for_status()
    value = r.json()
    if not isinstance(value, dict):
        raise ValueError("Réponse API inattendue")
    return value


def _pending(fog: str, run: str, tick: int, history: dict, reason: str) -> dict:
    return {
        "fog_id": fog, "simulation_run_id": run, "tick": tick,
        "ready": False, "reason": reason,
        "history_points": history.get("history_points", 0),
        "required_points": 12,
        "ticks": history.get("ticks", []), "history": history.get("history", []),
        "source": "occupancy_history/orchestrator_settled",
        "role": "recommendation_only_no_reservation",
    }


@router.get("/forecast/{fog_id}")
def forecast_for_dashboard(fog_id: Literal["fog-a", "fog-b", "fog-c"]):
    """Expose au navigateur UNIQUEMENT les prédictions validées du run courant.

    La lecture de l'historique exige la concordance run/tick. Les demandes
    d'inférence sont mises en cache par (run_id, tick, fog_id).
    """
    cloud, orchestrator, token = _urls()
    headers = {"X-ML-Token": token}
    try:
        with httpx.Client(timeout=15) as client:
            orch = _get_json(client, f"{orchestrator}/api/v1/orchestrator/status")
            run = orch["simulation_run_id"]
            tick = int(orch["current_tick"])
            history = _get_json(client, f"{cloud}/api/v1/ml/history/{fog_id}", headers)
        if history.get("simulation_run_id") != run:
            raise HTTPException(status_code=409, detail="Run ML différent du run orchestrateur")
        if orch.get("history_pending_tick") is not None:
            return _pending(fog_id, run, tick, history, "history_pending")
        if history.get("ticks") and history["ticks"][-1] != tick:
            return _pending(fog_id, run, tick, history, "history_not_at_current_tick")
        if not history.get("ready"):
            return _pending(fog_id, run, tick, history, history.get("reason", "insufficient_history"))

        key = (run, tick, fog_id)
        with _cache_lock:
            result = _cache.get(key)
            if result is None:
                # Inférence coûteuse : une seule à la fois sur Chronos-2.
                with httpx.Client(timeout=305) as client:
                    r = client.post(
                        f"{cloud}/api/v1/ml/forecast/{fog_id}",
                        params={"prediction_length": 1}, headers=headers,
                    )
                    r.raise_for_status()
                    result = r.json()
                if result.get("simulation_run_id") != run or result.get("input_ticks") != history.get("ticks"):
                    raise HTTPException(status_code=409, detail="Fenêtre ML modifiée pendant l'inférence")
                if result.get("step_minutes") != 60 or result.get("horizon_steps") != 1:
                    raise ValueError("Horizon Chronos inattendu")
                q = result["forecasts"]
                for name in ("q10", "q50", "q90"):
                    if (not isinstance(q.get(name), list) or len(q[name]) != 1
                            or not math.isfinite(float(q[name][0]))):
                        raise ValueError("Quantiles Chronos invalides")
                # Une fois validé, ne jamais recalculer le même tick.
                _cache[key] = result
                if len(_cache) > 36:
                    for old in list(_cache)[:12]:
                        _cache.pop(old, None)
        return {
            "fog_id": fog_id, "simulation_run_id": run, "tick": tick,
            "ready": True, "reason": "ok", "history_points": len(history["history"]),
            "required_points": 12, "ticks": history["ticks"], "history": history["history"],
            "forecasts": result["forecasts"], "model": result.get("model", "Chronos-2"),
            "step_minutes": result["step_minutes"], "horizon_steps": 1,
            "source": history["source"], "role": "recommendation_only_no_reservation",
        }
    except HTTPException:
        raise
    except (httpx.HTTPError, OSError, ValueError, TypeError, KeyError) as exc:
        raise HTTPException(
            status_code=503, detail=f"ML indisponible : {type(exc).__name__}"
        ) from exc

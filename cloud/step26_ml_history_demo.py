"""Etape 26 : demo de 12 ticks consecutifs pour tester Chronos-2 depuis PostgreSQL.

Ce script ne modifie aucun service. Il utilise les API existantes.
Ne pas lancer en parallele avec une autre simulation.
Les nouvelles voitures sont ajoutees uniquement au Fog B ; aucune fin de session n'est
injectee. C'est un test d'integration, pas une evaluation predictive realiste.
"""
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV = ROOT / '.env.cloud'
ORCH = 'http://127.0.0.1:18081'
CLOUD = 'http://127.0.0.1:18080'
# Petites variations d'arrivees ; aucun transfert ni saturation provoque.
ARRIVALS_BY_TICK = [0, 3, 1, 3, 0, 2, 4, 1, 0, 3, 2, 1]


def get_secret(key):
    if not ENV.exists():
        raise RuntimeError(f'Fichier introuvable : {ENV}')
    for line in ENV.read_text(encoding='utf-8').splitlines():
        if line.startswith(key + '='):
            return line.split('=', 1)[1].strip().strip('"').strip("'")
    raise RuntimeError(f'{key} absent dans {ENV}')


def http_json(method, url, token=None, header=None, payload=None, timeout=30):
    headers = {'Content-Type': 'application/json'}
    if token:
        headers[header] = token
    body = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as e:
        detail = e.read().decode('utf-8', errors='replace')[:600]
        raise RuntimeError(f'HTTP {e.code} sur {url} : {detail}') from e
    except urllib.error.URLError as e:
        raise RuntimeError(f'Connexion echouee ({url}) : {e}') from e


def main():
    orch_token = get_secret('ORCHESTRATOR_API_TOKEN')
    ml_token = get_secret('CLOUD_ML_API_TOKEN')
    if get_secret('SIM_TICK_MINUTES') != '60':
        raise RuntimeError('La convention 1 tick = 60 minutes simulees doit etre validee et configuree avant le test ML.')

    def history():
        return http_json('GET', CLOUD + '/api/v1/ml/history/fog-b', ml_token, 'X-ML-Token')

    sample = history()
    status = http_json('GET', ORCH + '/api/v1/orchestrator/status')
    current_tick = int(status['current_tick'])
    ticks = sample.get('ticks', [])
    print(f'Depart : orchestrateur tick={current_tick}, historique Fog B={ticks}')

    if status.get('history_pending_tick') is not None:
        raise RuntimeError('Historique en attente. Utiliser /api/v1/orchestrator/retry-history, PAS /step.')
    if ticks and ticks[-1] != current_tick:
        raise RuntimeError('Tick en memoire different du dernier tick historique. Verifier /orchestrator/status et les Fog ; aucun nouveau tick lance.')
    if current_tick < 1:
        raise RuntimeError('Tick actuel inconnu ou non initialise. Verifier /orchestrator/status avant de poursuivre.')

    for _ in range(20):
        if sample.get('ready') is True:
            break
        target = current_tick + 1
        count = ARRIVALS_BY_TICK[(target - 1) % len(ARRIVALS_BY_TICK)]
        arrivals = [
            {'vehicle_id': f'ml-demo-t{target}-{i+1:03d}', 'required_power_kw': 22}
            for i in range(count)
        ]
        print(f'Tick {target} : injection de {count} voiture(s) dans Fog B...')
        round_result = http_json(
            'POST', ORCH + '/api/v1/orchestrator/step',
            orch_token, 'X-Orchestrator-Token',
            {'arrivals': {'fog-b': arrivals}, 'completions': {}},
            timeout=90,
        )
        sync = round_result.get('history_sync', {})
        print(f'  history_sync={sync}; total_unserved={round_result.get("total_unserved")}')
        if sync.get('status') != 'captured' or sync.get('recorded_fogs') != 3:
            raise RuntimeError('Historique non confirme. Ne pas relancer /step ; inspecter /orchestrator/status et /retry-history.')
        if round_result.get('tick') != target:
            raise RuntimeError('Numero de tick inattendu. Ne pas lancer un autre round.')
        current_tick = target
        sample = history()
        print(f'  points={sample.get("history_points")}, dernier={sample.get("history", [None])[-1]}, ready={sample.get("ready")}')

    if not sample.get('ready'):
        raise RuntimeError(f'12 ticks consecutifs toujours indisponibles: {sample}')

    print('\nFenetre validee :')
    print(json.dumps({'ticks': sample['ticks'], 'history': sample['history'], 'ready': sample['ready']}, indent=2))
    print('\nAppel au modele Chronos-2...')
    forecast = http_json(
        'POST', CLOUD + '/api/v1/ml/forecast/fog-b?prediction_length=1',
        ml_token, 'X-ML-Token', timeout=300,
    )
    print(json.dumps(forecast, indent=2, ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        print(f'ARRET SECURISE : {e}', file=sys.stderr)
        sys.exit(1)

#!/usr/bin/env python3
"""Étape 48: test direct Fog→Fog de résilience sur 3 mini-Fog indépendants.

ATTENTION : ne touche JAMAIS au projet fog-cloud, à Fog A/B/C de production,
à PostgreSQL ni au laboratoire React fog-cooperation-demo.
La seule cible Docker est le projet fixé «fog-fault-demo48» et son Compose fixe.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / 'compose.demo.faults.yml'
PROJECT = 'fog-fault-demo48'
FOGS = {'fog-a': 'http://127.0.0.1:18301',
        'fog-b': 'http://127.0.0.1:18302',
        'fog-c': 'http://127.0.0.1:18303'}
TOTALS = {'fog-a': 2, 'fog-b': 1, 'fog-c': 3}


def compose(*args: str) -> str:
    argv = ['docker', 'compose', '-p', PROJECT, '-f', str(COMPOSE), *args]
    proc = subprocess.run(argv, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode:
        raise RuntimeError(f"Échec {' '.join(argv)}\n{proc.stdout}\n{proc.stderr}")
    return proc.stdout.strip()


def api(fog: str, path: str, payload: dict | None = None, timeout: int = 7) -> dict:
    if fog not in FOGS:
        raise ValueError('Fog non autorisé')
    req = Request(FOGS[fog] + path,
                  data=json.dumps(payload).encode() if payload is not None else None,
                  headers={'Content-Type': 'application/json', 'Accept': 'application/json'})
    try:
        with urlopen(req, timeout=timeout) as res:
            value = json.load(res)
    except HTTPError as error:
        reason = error.read(500).decode(errors='replace')
        raise AssertionError(f'{fog} {path}: HTTP {error.code} : {reason}') from error
    except (OSError, URLError, TimeoutError) as error:
        raise AssertionError(f'{fog} {path}: accès impossible : {error}') from error
    if not isinstance(value, dict):
        raise AssertionError(f'{fog} {path}: JSON inattendu')
    return value


def wait_ready(names: tuple[str, ...], seconds: int = 90) -> None:
    pending = set(names)
    for _ in range(seconds):
        for fog in list(pending):
            try:
                if api(fog, '/health', timeout=2).get('status') == 'healthy':
                    pending.remove(fog)
            except (OSError, AssertionError):
                # Connection reset during FastAPI/SQLite startup is recoverable here.
                pass
        if not pending:
            return
        time.sleep(1)
    raise AssertionError(f'Mini-Fog non joignables après démarrage : {sorted(pending)}')


def capacity(fog: str) -> dict:
    return api(fog, '/api/v1/piles/capacity')


def check_disposable() -> None:
    """Guard all destructive actions with exact expected disposable catalogues."""
    for fog, expected in TOTALS.items():
        cp = capacity(fog)
        if cp.get('fog_id') != fog or cp.get('total_piles') != expected:
            raise AssertionError(f'ABANDON : {fog} ne contient pas le mini-catalogue attendu ({expected}) : {cp}')


def help_from_b(name: str) -> dict:
    return api('fog-b', '/api/v1/cooperation/request-help', {
        'request_id': f'step48-{name}-{uuid.uuid4().hex}',
        'origin_fog': 'fog-b',
        'required_piles': 1,
        'required_power_kw': 22.0,
    }, timeout=22)


def activate_real_hold(negotiation: dict, expected: str | None = None) -> dict:
    assert negotiation.get('status') == 'HOLD_GRANTED', negotiation
    proposal = negotiation.get('selected_proposal') or {}
    dest = proposal.get('proposer_fog')
    assert dest in ('fog-a', 'fog-c'), negotiation
    if expected:
        assert dest == expected, f'Le voisin attendu était {expected}, obtenu {dest}'
    hold = negotiation.get('hold') or {}
    hold_id = hold.get('hold_id')
    assert isinstance(hold_id, str) and hold_id, negotiation
    target_piles = set(hold.get('pile_ids') or [])
    assert target_piles, 'Aucune borne réservée par HOLD'
    before = capacity(dest)['occupied_piles']
    confirmed = api(dest, '/api/v1/reservations/confirm', {'hold_id': hold_id})
    assert confirmed.get('status') == 'CONFIRMED', confirmed
    activated = api(dest, '/api/v1/reservations/activate', {'hold_id': hold_id})
    assert activated.get('status') == 'ACTIVE', activated
    after = capacity(dest)['occupied_piles']
    assert after == before + 1, f'{dest} : occupation {before}→{after} (attendu +1)'
    # Verify the exact remote pile, not only the aggregate capacity.
    r = Request(FOGS[dest] + '/api/v1/piles', headers={'Accept': 'application/json'})
    with urlopen(r, timeout=8) as res:
        piles = json.load(res)
    matching = {p['pile_id'] for p in piles
                if p.get('pile_id') in target_piles and p.get('status') == 'OCCUPIED'
                and p.get('reservation_status') == 'ACTIVE'}
    assert matching == target_piles, f'ACTIVATE non visible en SQLite : attendu {target_piles}, trouvé {matching}'
    return {'to_fog': dest, 'hold_id': hold_id, 'pile_ids': sorted(target_piles),
            'occupied_before': before, 'occupied_after': after}


def run() -> None:
    if not COMPOSE.exists():
        raise RuntimeError(f'Compose manquant : {COMPOSE}')
    existing = compose('ps', '-aq')
    if existing:
        raise RuntimeError('Le projet fog-fault-demo48 existe déjà. Vérifie-le avant de le supprimer :\n'
                           'docker compose -p fog-fault-demo48 -f compose.demo.faults.yml down')
    print('=== Étape 48 : résilience Fog ↔ Fog (mini-Fog 18301–18303 uniquement) ===', flush=True)
    compose('up', '-d', '--build', 'demo-fog-a', 'demo-fog-b', 'demo-fog-c')
    wait_ready(tuple(FOGS))
    check_disposable()

    print('\n1) Saturer Fog B avec une recharge locale...', flush=True)
    r = api('fog-b', '/api/v1/reservations/local', {
        'vehicle_id': 'test-48-local-' + uuid.uuid4().hex, 'required_power_kw': 22.0})
    assert r.get('status') == 'ACTIVE', r
    assert capacity('fog-b')['available_piles'] == 0
    print('PASS : Fog B saturé (0 borne libre).', flush=True)

    print('\n2) Couper uniquement mini-Fog A : Fog B doit choisir C...', flush=True)
    compose('stop', 'demo-fog-a')
    # No call to the orchestrator: B itself contacts A/C and selects the peer.
    n = help_from_b('a-absent')
    assert any(e.get('peer_fog') == 'fog-a' for e in n.get('errors', [])), n
    active1 = activate_real_hold(n, expected='fog-c')
    print('PASS : Fog A indisponible, HOLD→CONFIRM→ACTIVE chez C.', active1, flush=True)
    print('Trace HTTP réelle :', [e.get('stage') for e in n.get('trace', [])], flush=True)

    print('\n3) Couper également mini-Fog C : aucune fausse réussite...', flush=True)
    compose('stop', 'demo-fog-c')
    n = help_from_b('deux-absents')
    assert n.get('status') == 'NO_PROPOSALS', n
    assert not n.get('hold'), n
    assert {'fog-a', 'fog-c'} <= {e.get('peer_fog') for e in n.get('errors', [])}, n
    assert api('fog-b', '/health')['status'] == 'healthy'
    print('PASS : NO_PROPOSALS, Fog B reste opérationnel, aucune réservation annoncée ACTIVE.', flush=True)

    print('\n4) Redémarrer les deux mini-voisins...', flush=True)
    compose('start', 'demo-fog-a', 'demo-fog-c')
    wait_ready(('fog-a', 'fog-c'))
    check_disposable()
    n = help_from_b('retour')
    active2 = activate_real_hold(n)
    print('PASS : nouvelle recharge ACTIVE chez', active2['to_fog'], active2, flush=True)
    print('\nSUCCÈS : 4 contrôles terminés sur le projet isolé fog-fault-demo48.', flush=True)
    print('Limite : le redémarrage de mini-Fog utilise des bases temporaires ;', flush=True)
    print('ce test NE valide PAS la récupération d’un round Cloud interrompu ni un réseau multi-PC.', flush=True)
    compose('down')
    print('Nettoyage du mini-projet terminé (les conteneurs principaux ne sont pas concernés).', flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--run', action='store_true', help='Exécuter le scénario destructif UNIQUEMENT sur les mini-Fog isolés')
    args = parser.parse_args()
    if not args.run:
        parser.print_help()
        return
    try:
        run()
    except (AssertionError, RuntimeError, OSError, ValueError, subprocess.SubprocessError) as error:
        print('\nÉCHEC :', error, file=sys.stderr)
        print('Les conteneurs de démonstration sont conservés pour le diagnostic.', file=sys.stderr)
        print('docker compose -p fog-fault-demo48 -f compose.demo.faults.yml ps -a', file=sys.stderr)
        print('docker compose -p fog-fault-demo48 -f compose.demo.faults.yml logs --tail=80 demo-fog-a demo-fog-b demo-fog-c', file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()

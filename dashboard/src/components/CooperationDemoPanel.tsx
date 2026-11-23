import React, { useEffect, useState } from 'react';
import { ArrowLeftRight, CheckCircle, Clock3, RotateCcw, Server, ShieldCheck, AlertTriangle, RefreshCw, CarFront } from 'lucide-react';

type FogId = 'fog-a' | 'fog-b' | 'fog-c';
type Capacity = { fog_id: FogId; total_piles: number; available_piles: number; occupied_piles: number };
type Trace = {
  stage: string; from_fog?: string; to_fog?: string; result?: string;
  method?: string; path?: string; tick?: number; request_id?: string;
  hold_id?: string; vehicle_id?: string; pile_ids?: string[];
  candidates?: string[]; detail?: string; reason?: string;
};
type Transfer = { from_fog: string; to_fog: string; hold_id: string; pile_ids: string[]; vehicles: string[] };
type Round = {
  tick: number; fog_id: FogId; count: number; locally_started: number;
  remote_started: number; auto_completed: number; unserved: number;
  transfers: Transfer[]; verified_hold_ids: string[];
};
type DemoStatus = {
  isolated: boolean; interactive: boolean; ready: boolean; run_id: string;
  tick: number | null; capacities: Partial<Record<FogId, Capacity>>;
  last_round: Round | null; round_history: Round[];
  events: Trace[]; error: string | null; reset_available: boolean;
};

const FOGS: FogId[] = ['fog-a', 'fog-b', 'fog-c'];
const fogName = (id?: string) => id ? `Fog ${id.replace('fog-', '').toUpperCase()}` : '—';
const explanations: Record<string, string> = {
  HELP_REQUEST: 'Demande d’aide envoyée directement par le Fog saturé',
  PROPOSAL: 'Réponse du Fog voisin contenant sa capacité',
  DECLINE: 'Le Fog voisin ne peut pas satisfaire la demande',
  PEER_ERROR: 'Le Fog voisin ne répond pas',
  RANKING: 'Classement effectué localement par le Fog demandeur',
  HOLD_REQUEST: 'Demande de blocage provisoire d’une borne distante',
  HOLD_GRANTED: 'Borne temporairement bloquée, pas encore occupée',
  HOLD_FAILED: 'Blocage refusé, autre voisin possible',
  CONFIRM_REQUEST: 'Demande de confirmation de la réservation',
  CONFIRMED: 'Réservation confirmée par le Fog destinataire',
  ACTIVATE_REQUEST: 'Demande de démarrage de recharge distante',
  ACTIVE: 'Recharge activée et vérifiée dans le SQLite du Fog destinataire',
  REQUEST_UNCERTAIN: 'Réponse HTTP incertaine : ne pas rejouer automatiquement',
  REQUEST_FAILED: 'Échec de la réservation distante',
};

function operationId(): string {
  return `lab-${Date.now()}-${Math.random().toString(36).slice(2, 12)}`;
}

export const CooperationDemoPanel: React.FC = () => {
  const [data, setData] = useState<DemoStatus | null>(null);
  const [originFog, setOriginFog] = useState<FogId>('fog-b');
  const [count, setCount] = useState(1);
  const [duration, setDuration] = useState(3);
  const [requiredPower, setRequiredPower] = useState(22);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = async () => {
    const response = await fetch('/api/v1/demo/cooperation/status', { signal: AbortSignal.timeout(8000) });
    if (!response.ok) throw new Error(`Statut de la démonstration : HTTP ${response.status}`);
    const next: DemoStatus = await response.json();
    if (next.isolated !== true || next.interactive !== true) throw new Error('Réponse non isolée / ancien serveur de démonstration');
    setData(next);
    return next;
  };

  useEffect(() => {
    let active = true;
    let refreshing = false;
    const update = async () => {
      if (refreshing) return;
      refreshing = true;
      try {
        const response = await fetch('/api/v1/demo/cooperation/status', { signal: AbortSignal.timeout(8000) });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const next: DemoStatus = await response.json();
        if (!next.isolated || !next.interactive) throw new Error('Ancienne API de démonstration');
        if (active) setData(next);
      } catch {
        if (active) setError('Démonstration indisponible : vérifie les quatre conteneurs fog-cooperation-demo et le port 18110.');
      } finally { refreshing = false; }
    };
    update();
    const interval = window.setInterval(update, 4000);
    return () => { active = false; window.clearInterval(interval); };
  }, []);

  const send = async (path: 'step' | 'reset', body: Record<string, unknown>) => {
    if (busy || data?.tick == null) return;
    setBusy(true); setError(null);
    try {
      const response = await fetch(`/api/v1/demo/cooperation/${path}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
        body: JSON.stringify(body),
        signal: AbortSignal.timeout(90000),
      });
      if (!response.ok) {
        const detail = await response.json().catch(() => null);
        throw new Error(typeof detail?.detail === 'string' ? detail.detail : `HTTP ${response.status}`);
      }
      const next: DemoStatus = await response.json();
      if (!next.isolated || !next.interactive) throw new Error('Réponse de démonstration non isolée');
      setData(next);
    } catch (cause) {
      setError(`${String(cause)}. Si la requête a expiré, NE clique pas une deuxième fois. Actualise le statut d’abord.`);
      try { await refresh(); } catch { /* Preserve original cause. */ }
    } finally { setBusy(false); }
  };

  const executeStep = (arrivals: number) => {
    if (!data?.ready || data.tick == null) return;
    void send('step', {
      fog_id: originFog, count: arrivals, duration_ticks: duration,
      required_power_kw: requiredPower, expected_tick: data.tick,
      operation_id: operationId(),
    });
  };

  const reset = () => {
    if (!data?.reset_available || data.tick == null) return;
    if (!window.confirm('Effacer uniquement les sessions des trois mini-Fog de démonstration (2 / 1 / 3 bornes) et revenir au tick 0 ? Les vrais Fog et PostgreSQL ne seront pas modifiés.')) return;
    void send('reset', { expected_tick: data.tick });
  };

  const actualEvents = data?.events ?? [];
  const canControl = !!data?.ready && data.tick !== null && !busy;
  const last = data?.last_round;

  return (
    <section className="rounded-3xl border-2 border-cyan-200 bg-white shadow-sm overflow-hidden" aria-label="Laboratoire interactif Fog à Fog">
      <div className="bg-cyan-50 px-5 sm:px-7 py-5 border-b border-cyan-100">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <div className="text-xs font-extrabold tracking-wider text-cyan-800 uppercase mb-1">Démonstration soutenance · Architecture décentralisée</div>
            <h3 className="text-xl font-black text-slate-900 flex items-center gap-2"><ArrowLeftRight className="w-6 h-6 text-cyan-700"/> Laboratoire Fog ↔ Fog</h3>
            <p className="text-sm text-slate-600 mt-2">Trois <strong>vraies API FastAPI isolées</strong> (2 / 1 / 3 bornes). Aucune connexion à PostgreSQL ni aux Fog principaux.</p>
          </div>
          <div className="flex items-center gap-2">
            <span className="rounded-xl bg-white px-3 py-2 text-sm font-bold text-cyan-900 border">Tick {data?.tick ?? '—'}</span>
            <button type="button" onClick={() => refresh().then(() => setError(null)).catch(e => setError(String(e)))} className="rounded-xl p-3 border border-cyan-200 text-cyan-800" title="Actualiser"><RefreshCw className="w-4 h-4"/></button>
          </div>
        </div>
      </div>

      <div className="px-5 sm:px-7 py-6 space-y-6">
        {error && <div role="alert" className="rounded-xl bg-amber-50 border border-amber-200 p-4 text-sm text-amber-950">{error}</div>}
        {data?.error && <div role="alert" className="rounded-xl bg-rose-50 border border-rose-300 p-4 text-sm text-rose-950">{data.error}</div>}

        <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
          {FOGS.map((fog) => {
            const c = data?.capacities?.[fog];
            return <div key={fog} className={`rounded-2xl border p-4 ${fog === originFog ? 'border-cyan-400 bg-cyan-50/40' : 'border-slate-200 bg-slate-50'}`}>
              <div className="flex justify-between items-center"><strong className="flex items-center gap-2"><Server className="w-4 h-4"/>{fogName(fog)}</strong><span className="text-xs text-slate-500">{c?.total_piles ?? '—'} bornes</span></div>
              <div className="flex justify-between text-sm mt-4"><span>Disponibles : <strong className="text-emerald-700">{c?.available_piles ?? '—'}</strong></span><span>Occupées : <strong>{c?.occupied_piles ?? '—'}</strong></span></div>
              {fog === originFog && <p className="text-xs mt-2 text-cyan-800">Fog d’arrivée sélectionné</p>}
            </div>;
          })}
        </div>

        <div className="rounded-2xl border border-slate-200 p-4 sm:p-5 space-y-4">
          <h4 className="font-black text-lg text-slate-900">Piloter la simulation isolée</h4>
          <p className="text-sm text-slate-600">Chaque injection fait avancer un tick. « Pas +1 » fait avancer le temps sans arrivée, ce qui permet de terminer automatiquement les sessions à échéance.</p>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
            <label className="text-xs font-bold text-slate-600">Fog demandeur
              <select aria-label="Fog demandeur" value={originFog} onChange={e => setOriginFog(e.target.value as FogId)} className="block w-full mt-1 border rounded-lg p-2 text-sm text-slate-900 bg-white">
                {FOGS.map(f => <option key={f} value={f}>{fogName(f)}</option>)}
              </select>
            </label>
            <label className="text-xs font-bold text-slate-600">Voitures à injecter
              <select aria-label="Nombre de voitures" value={count} onChange={e => setCount(Number(e.target.value))} className="block w-full mt-1 border rounded-lg p-2 text-sm text-slate-900 bg-white">
                {[1, 2, 3, 4, 5, 6].map(n => <option key={n} value={n}>{n} voiture{n > 1 ? 's' : ''}</option>)}
              </select>
            </label>
            <label className="text-xs font-bold text-slate-600">Durée de recharge (ticks)
              <select aria-label="Durée de recharge" value={duration} onChange={e => setDuration(Number(e.target.value))} className="block w-full mt-1 border rounded-lg p-2 text-sm text-slate-900 bg-white">
                {[1, 2, 3, 4, 5, 6].map(n => <option key={n} value={n}>{n} tick{n > 1 ? 's' : ''}</option>)}
              </select>
            </label>
            <label className="text-xs font-bold text-slate-600">Puissance demandée
              <select aria-label="Puissance demandée" value={requiredPower} onChange={e => setRequiredPower(Number(e.target.value))} className="block w-full mt-1 border rounded-lg p-2 text-sm text-slate-900 bg-white">
                {[7, 11, 22, 50].map(n => <option key={n} value={n}>{n} kW</option>)}
              </select>
            </label>
          </div>
          <div className="flex flex-wrap gap-2">
            <button type="button" disabled={!canControl} onClick={() => executeStep(count)} className="inline-flex items-center gap-2 rounded-xl bg-cyan-700 text-white font-bold px-4 py-3 disabled:opacity-40"><CarFront className="w-4 h-4"/>{busy ? 'Traitement…' : 'Injecter et avancer'}</button>
            <button type="button" disabled={!canControl} onClick={() => executeStep(0)} className="inline-flex items-center gap-2 rounded-xl bg-slate-900 text-white font-bold px-4 py-3 disabled:opacity-40"><Clock3 className="w-4 h-4"/>Pas +1 (0 arrivée)</button>
            <button type="button" disabled={!data?.reset_available || busy} onClick={reset} className="inline-flex items-center gap-2 rounded-xl border border-amber-300 text-amber-900 font-bold px-4 py-3 disabled:opacity-40"><RotateCcw className="w-4 h-4"/>Nouvelle simulation isolée</button>
          </div>
          <p className="text-xs text-slate-500">Démonstration conseillée : Fog B → 1 voiture au tick 1 ; Fog B → 1 voiture au tick 2 ; observer HELP_REQUEST / PROPOSAL / HOLD / CONFIRM / ACTIVATE. Pour recommencer : « Nouvelle simulation isolée ».</p>
        </div>

        {last && <div className="rounded-2xl bg-emerald-50 border border-emerald-200 p-4 space-y-2">
          <div className="flex items-center gap-2 text-emerald-900 font-black"><CheckCircle className="w-5 h-5"/>Round {last.tick} terminé — {last.remote_started} recharge(s) distante(s) vérifiée(s)</div>
          <div className="text-sm text-slate-700">{last.count} demande(s) sur {fogName(last.fog_id)} · {last.locally_started} locale(s) · {last.remote_started} distante(s) · {last.auto_completed} fin(s) automatique(s) · {last.unserved} non servie(s)</div>
          {last.transfers.map(t => <div key={t.hold_id} className="text-xs text-emerald-900 break-all flex items-center gap-2"><ShieldCheck className="w-4 h-4 shrink-0"/>{fogName(t.from_fog)} → {fogName(t.to_fog)} · Borne {t.pile_ids.join(', ')} · hold_id={t.hold_id}</div>)}
        </div>}

        <div className="rounded-2xl border border-slate-200 p-4 sm:p-5">
          <h4 className="font-black text-lg text-slate-900">Journal des échanges Fog ↔ Fog</h4>
          <p className="text-xs text-slate-500 mt-1">Réponses et messages HTTP réellement produits par les Fog, affichés après chaque tick. Ce journal applicatif n’est pas une capture de paquets en direct.</p>
          {actualEvents.length === 0 ? <p className="mt-4 rounded-xl bg-slate-50 p-4 text-sm text-slate-600">Aucun message de coopération pour le moment. Sature d'abord le Fog B (une voiture), puis injecte une deuxième voiture chez B.</p> :
            <div className="mt-4 max-h-[540px] overflow-y-auto border border-slate-200 rounded-xl divide-y divide-slate-100">
              {actualEvents.map((e, idx) => <div key={`${idx}-${e.stage}-${e.tick}`} className="grid grid-cols-1 md:grid-cols-[80px_140px_1fr] gap-2 px-3 py-3 text-xs">
                <div className="font-bold text-cyan-800">Tick {e.tick}</div>
                <div><strong className="text-slate-900">{e.stage}</strong><div className="text-slate-500 mt-1">{fogName(e.from_fog)} → {fogName(e.to_fog)}</div></div>
                <div><p className="font-mono text-[11px] text-slate-800">{e.method ?? '—'} {e.path ?? '—'} · {e.result ?? '—'}</p>
                  <p className="text-slate-600 mt-1">{explanations[e.stage] ?? 'Événement issu de la communication Fog'}</p>
                  {e.request_id && <p className="font-mono break-all text-slate-500">request_id={e.request_id}</p>}
                  {e.hold_id && <p className="font-mono break-all text-slate-500">hold_id={e.hold_id}</p>}
                  {e.candidates && <p className="text-slate-600">Classement local : {e.candidates.join(' → ')}</p>}
                </div>
              </div>)}
            </div>}
        </div>

        {data && data.round_history.length > 0 && <div className="rounded-2xl border border-slate-200 p-4">
          <h4 className="font-black mb-3">Historique de cette démonstration ({data.round_history.length} rounds)</h4>
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-2">
            {data.round_history.slice().reverse().slice(0, 12).map(r => <div key={r.tick} className="rounded-xl bg-slate-50 p-3 text-xs">Tick {r.tick} — {fogName(r.fog_id)} : {r.count} arrivée(s), {r.remote_started} transfert(s), {r.auto_completed} fin(s)</div>)}
          </div>
        </div>}
        {!data?.ready && <div className="rounded-xl bg-amber-50 border border-amber-200 p-3 text-sm text-amber-900 flex gap-2"><AlertTriangle className="w-4 h-4 shrink-0"/>Le laboratoire n’est pas prêt. Si l’état est incertain, ne rejoue pas un tick : recrée seulement les conteneurs fog-cooperation-demo.</div>}
      </div>
    </section>
  );
};

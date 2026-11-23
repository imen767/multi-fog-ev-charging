/** Vérifie le mode dégradé sans prévision inventée. */
import staticStations from './src/data/stations.json' with { type: 'json' };
import { rankFogStations, PRESET_LOCATIONS } from './src/services/recommendationEngine.ts';
import type { Station, TerritoryForecast } from './src/types/index.ts';

const forecast: TerritoryForecast = {
  fogId: 'fog-a', forecastBusyQ10: 0, forecastBusyQ50: 0, forecastBusyQ90: 0,
  totalPiles: 6083, expectedOccupancyPercent: 0, occupancyLevel: 'low',
  horizonMin: 60, lastUpdated: 'Indisponible', isLive: false,
  reason: 'insufficient_history', historyPoints: 11,
};
const recs = rankFogStations('fog-a', staticStations as unknown as Station[], PRESET_LOCATIONS[0], forecast);
if (!recs.length) throw Error('Aucune recommandation Fog A trouvée');
for (const r of recs) {
  if (!r.explanation.includes('Prévision Chronos-2 indisponible')) throw Error('Le mode dégradé doit être explicite');
  if (r.explanation.includes('prévision Chronos-2 territoriale favorable')) throw Error('ML inventé');
  if (r.normalizedOccupancy !== 0) throw Error('Occupation ML ne doit pas influer sur le score si indisponible');
}
console.log('PASS : mode dégradé sans quantiles ni fausse prédiction (étape 43)');

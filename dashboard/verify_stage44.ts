import catalog from './src/data/stations.json' with { type: 'json' };
import { mergeLiveStationCapacities } from './src/services/stationAvailability.ts';
import { rankFogStations, PRESET_LOCATIONS } from './src/services/recommendationEngine.ts';
import type { Station, UserPreferences } from './src/types/index.ts';

const source = catalog as unknown as Station[];
const first = source.find((s) => s.fogId === 'fog-a')!;
const second = source.find((s) => s.fogId === 'fog-b')!;
const sample = [first, second];
const data = [{
  station_id: first.id,
  total_piles: 2,
  available_piles: 1,
  occupied_piles: 1,
  out_of_service_piles: 0,
  available_power_kw: 7,
  available_ac_piles: 1,
  available_dc_piles: 0,
  max_available_power_kw: 7,
  max_available_ac_kw: 7,
  max_available_dc_kw: 0,
}];
const live = mergeLiveStationCapacities('fog-a', sample, data);
if (live.length !== 1 || live[0].availablePiles !== 1 || live[0].occupiedPiles !== 1) {
  throw new Error('La disponibilité affichée doit provenir de l’API');
}
if (live[0].availableDcPiles !== 0) throw new Error('Pas de DC libre');
const prefs: UserPreferences = {
  vehicleType: 'DC_FAST', minPowerKw: 0, maxDistanceKm: 500,
  selectedFog: 'ALL', onlyAvailable: true,
};
if (rankFogStations('fog-a', live, PRESET_LOCATIONS[0], undefined, undefined, prefs).length) {
  throw new Error('Ne pas proposer un connecteur DC occupé');
}
const ac = rankFogStations('fog-a', live, PRESET_LOCATIONS[0], undefined, undefined, {
  ...prefs, vehicleType: 'AC_SLOW',
});
if (ac.length !== 1) throw new Error('AC disponible doit être recommandable');
const strong = rankFogStations('fog-a', live, PRESET_LOCATIONS[0], undefined, undefined, {
  ...prefs, vehicleType: 'AC_SLOW', minPowerKw: 50,
});
if (strong.length !== 0) throw new Error('Ne pas promettre une puissance AC indisponible');
const fails = (payload: unknown) => {
  try { mergeLiveStationCapacities('fog-a', sample, payload); }
  catch { return; }
  throw new Error('Le snapshot incomplet ou incorrect doit échouer sans fallback');
};
fails([]);
fails([{ ...data[0], available_piles: -1 }]);
fails([{ ...data[0], available_dc_piles: 1 }]);
fails([data[0], data[0]]);
const noFogB = mergeLiveStationCapacities('fog-a', sample, data);
if (noFogB.some((s) => s.fogId === 'fog-b')) throw new Error('Isolation Fog');
console.log('PASS étape 44 : stations SQLite, connecteurs AC/DC, puissance et fail-closed');

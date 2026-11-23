import type { FogId, Station } from '../types/index.ts';

/** Réponse de /api/v1/piles/station-capacities, calculée dans le SQLite du Fog. */
export interface LiveStationCapacity {
  station_id: string;
  total_piles: number;
  available_piles: number;
  occupied_piles: number;
  out_of_service_piles: number;
  available_power_kw: number;
  available_ac_piles: number;
  available_dc_piles: number;
  max_available_power_kw: number;
  max_available_ac_kw: number;
  max_available_dc_kw: number;
}

export function normalizeStationId(value: unknown): string {
  if (typeof value !== 'string' && typeof value !== 'number') {
    throw new Error('Identifiant de station absent');
  }
  const id = String(value).replace(/\.0$/, '');
  if (!id || !/^\d+$/.test(id)) throw new Error('Identifiant de station invalide');
  return id;
}

const nonNegative = (value: unknown, field: string): number => {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) {
    throw new Error(`Capacité invalide : ${field}`);
  }
  return value;
};

/**
 * Conserve les métadonnées de localisation du catalogue, mais utilise EXCLUSIVEMENT
 * les capacités de l'API SQLite pour décider quelles stations sont recommandables.
 * En cas de réponse incomplète, déclenche une erreur : aucune valeur statique ne fuit
 * dans le classement en se faisant passer pour une disponibilité actuelle.
 */
export function mergeLiveStationCapacities(
  fogId: FogId,
  catalog: Station[],
  payload: unknown,
): Station[] {
  if (!Array.isArray(payload)) throw new Error('Réponse stations : tableau attendu');
  const fogCatalog = catalog.filter((station) => station.fogId === fogId);
  const byId = new Map<string, LiveStationCapacity>();
  for (const item of payload) {
    if (!item || typeof item !== 'object') throw new Error('Ligne stations incorrecte');
    const row = item as Record<string, unknown>;
    const stationId = normalizeStationId(row.station_id);
    if (byId.has(stationId)) throw new Error(`Station dupliquée : ${stationId}`);
    const total = nonNegative(row.total_piles, 'total_piles');
    const available = nonNegative(row.available_piles, 'available_piles');
    const occupied = nonNegative(row.occupied_piles, 'occupied_piles');
    const outOfService = nonNegative(row.out_of_service_piles, 'out_of_service_piles');
    const power = nonNegative(row.available_power_kw, 'available_power_kw');
    const ac = nonNegative(row.available_ac_piles, 'available_ac_piles');
    const dc = nonNegative(row.available_dc_piles, 'available_dc_piles');
    const maxPower = nonNegative(row.max_available_power_kw, 'max_available_power_kw');
    const maxAc = nonNegative(row.max_available_ac_kw, 'max_available_ac_kw');
    const maxDc = nonNegative(row.max_available_dc_kw, 'max_available_dc_kw');
    if (![total, available, occupied, outOfService, ac, dc].every(Number.isInteger)) {
      throw new Error('Les compteurs doivent être entiers');
    }
    if (ac + dc !== available || available + occupied + outOfService > total) {
      throw new Error(`Capacité incohérente pour la station ${stationId}`);
    }
    if (Math.max(maxAc, maxDc) !== maxPower) throw new Error(`Puissances incompatibles : ${stationId}`);
    byId.set(stationId, {
      station_id: stationId,
      total_piles: total,
      available_piles: available,
      occupied_piles: occupied,
      out_of_service_piles: outOfService,
      available_power_kw: power,
      available_ac_piles: ac,
      available_dc_piles: dc,
      max_available_power_kw: maxPower,
      max_available_ac_kw: maxAc,
      max_available_dc_kw: maxDc,
    });
  }
  if (fogCatalog.length === 0) throw new Error(`Catalogue vide pour ${fogId}`);
  return fogCatalog.map((station) => {
    const row = byId.get(normalizeStationId(station.id));
    if (!row) throw new Error(`Station ${station.id} absente du snapshot ${fogId}`);
    const available = row.available_piles;
    return {
      ...station,
      totalPiles: row.total_piles,
      availablePiles: available,
      availableAcPiles: row.available_ac_piles,
      availableDcPiles: row.available_dc_piles,
      maxAvailablePowerKw: row.max_available_power_kw,
      maxAvailableAcKw: row.max_available_ac_kw,
      maxAvailableDcKw: row.max_available_dc_kw,
      occupiedPiles: row.occupied_piles,
      outOfServicePiles: row.out_of_service_piles,
      // Cette valeur nominale de puissance maximale vient du catalogue ; la capacité
      // réellement encore disponible (somme des kW) est disponible sur l'API.
      status: available > 0 ? (available < 3 ? 'LIMITED' : 'AVAILABLE')
        : row.out_of_service_piles === row.total_piles ? 'OUT_OF_SERVICE' : 'OCCUPIED',
      isReservable: available > 0,
    };
  });
}

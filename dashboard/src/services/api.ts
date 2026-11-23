import {
  FogCapacity,
  FogId,
  TerritoryForecast,
  ChronosForecastRaw,
  Station,
  TerritoryMeta,
  EvaluationDataPoint,
  HistoricalOccupancyPoint,
  SimulatorStatus,
  SimulatorScenario,
  SimulatorEvent
} from '../types';

import staticStations from '../data/stations.json';
import { mergeLiveStationCapacities } from './stationAvailability';
import staticTerritories from '../data/territories.json';
import staticEvaluation from '../data/evaluation_results.json';
import staticHistoricalOccupancy from '../data/historical_occupancy.json';

const FOG_CONFIG: Record<FogId, { port: number; proxyPrefix: string; totalPilesFallback: number }> = {
  'fog-a': { port: 8001, proxyPrefix: '/fog-a', totalPilesFallback: 6083 },
  'fog-b': { port: 8002, proxyPrefix: '/fog-b', totalPilesFallback: 7403 },
  'fog-c': { port: 8003, proxyPrefix: '/fog-c', totalPilesFallback: 8652 },
};

export interface MultiFogStatus {
  isLive: boolean;
  activeFogs: FogId[];
  chronosLive: boolean;
  lastUpdated: string;
}

export class FogDataService {
  private static cachedStations: Station[] = staticStations as unknown as Station[];
  private static cachedTerritories: Record<FogId, TerritoryMeta> = staticTerritories as unknown as Record<FogId, TerritoryMeta>;

  /**
   * Load full station list
   */
  public static getStations(): Station[] {
    return this.cachedStations;
  }

  /**
   * Load territory metadata
   */
  public static getTerritoriesMeta(): Record<FogId, TerritoryMeta> {
    return this.cachedTerritories;
  }

  /**
   * Load historical Chronos-2 evaluation results (24h benchmark)
   */
  public static getEvaluationResults(): EvaluationDataPoint[] {
    return staticEvaluation as EvaluationDataPoint[];
  }

  /**
   * Load historical hourly observations
   */
  public static getHistoricalOccupancy(): Record<FogId, HistoricalOccupancyPoint[]> {
    return staticHistoricalOccupancy as unknown as Record<FogId, HistoricalOccupancyPoint[]>;
  }

  /**
   * Fetch live capacity from a Fog node or simulator
   */
  public static async fetchFogCapacity(fogId: FogId): Promise<FogCapacity> {
    const config = FOG_CONFIG[fogId];
    try {
      const response = await fetch(`${config.proxyPrefix}/api/v1/piles/capacity`, {
        signal: AbortSignal.timeout(3000),
      });

      if (!response.ok) {
        throw new Error(`HTTP ${response.status}: ${response.statusText}`);
      }

      const data = await response.json();
      return {
        fog_id: fogId,
        total_piles: data.total_piles || config.totalPilesFallback,
        available_piles: data.available_piles ?? config.totalPilesFallback,
        occupied_piles: data.occupied_piles ?? 0,
        out_of_service_piles: data.out_of_service_piles ?? 0,
        non_reservable_piles: data.non_reservable_piles ?? 0,
        available_power_kw: data.available_power_kw ?? 100000,
        isOnline: true,
        lastUpdated: new Date().toLocaleTimeString(),
      };
    } catch {
      return {
        fog_id: fogId,
        total_piles: config.totalPilesFallback,
        available_piles: 0,
        occupied_piles: 0,
        out_of_service_piles: 0,
        available_power_kw: 0,
        isOnline: false,
        lastUpdated: 'Données indisponibles',
      };
    }
  }

  /**
   * Prévision du run courant : Cloud PostgreSQL -> Chronos-2, via Gateway.
   * Jamais de retour silencieux aux CSV statiques des Fog.
   */
  public static async fetchFogForecast(fogId: FogId, totalPiles: number): Promise<TerritoryForecast> {
    const unavailable = (reason: string, historyPoints = 0, history: number[] = [], ticks: number[] = []): TerritoryForecast => ({
      fogId,
      forecastBusyQ10: 0, forecastBusyQ50: 0, forecastBusyQ90: 0,
      totalPiles, expectedOccupancyPercent: 0, occupancyLevel: 'low',
      horizonMin: 60, lastUpdated: 'Indisponible', isLive: false,
      historyPoints, requiredHistoryPoints: 12, reason, history, ticks,
      source: 'occupancy_history/orchestrator_settled',
    });
    try {
      const response = await fetch(`/api/v1/ml/forecast/${fogId}`, {
        signal: AbortSignal.timeout(325000),
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const raw = await response.json();
      const history = Array.isArray(raw.history) ? raw.history : [];
      const ticks = Array.isArray(raw.ticks) ? raw.ticks : [];
      if (!raw.ready) return unavailable(raw.reason || 'insufficient_history', raw.history_points ?? 0, history, ticks);
      if (raw.horizon_steps !== 1 || raw.step_minutes !== 60) throw new Error('Horizon incorrect');
      const q10 = raw.forecasts?.q10?.[0];
      const q50 = raw.forecasts?.q50?.[0];
      const q90 = raw.forecasts?.q90?.[0];
      if (![q10, q50, q90].every((v) => typeof v === 'number' && Number.isFinite(v))) {
        throw new Error('Quantiles invalides');
      }
      const expectedOccupancyPercent = totalPiles > 0 ? Math.min(100, Math.max(0, q50 / totalPiles * 100)) : 0;
      const occupancyLevel = expectedOccupancyPercent > 65 ? 'high' : expectedOccupancyPercent > 35 ? 'moderate' : 'low';
      return {
        fogId, forecastBusyQ10: q10, forecastBusyQ50: q50, forecastBusyQ90: q90,
        totalPiles, expectedOccupancyPercent, occupancyLevel,
        horizonMin: 60, lastUpdated: new Date().toLocaleTimeString(), isLive: true,
        historyPoints: raw.history_points, requiredHistoryPoints: 12,
        reason: 'ok', history, ticks, source: raw.source, simulationRunId: raw.simulation_run_id,
      };
    } catch (error) {
      console.warn(`Chronos temps réel indisponible pour ${fogId}`, error);
      return unavailable('service_unavailable');
    }
  }

  /**
   * Fetch all fogs capacity and forecasts in parallel
   */
  public static async fetchAllFogData(): Promise<{
    capacities: Record<FogId, FogCapacity>;
    forecasts: Record<FogId, TerritoryForecast>;
    systemStatus: MultiFogStatus;
    stations: Station[];
    stationDataLive: Record<FogId, boolean>;
  }> {
    const fogs: FogId[] = ['fog-a', 'fog-b', 'fog-c'];

    // Lancer les lectures de capacité par station en parallèle ; pas de valeur statique
    // de remplacement en cas de panne. Une zone indisponible n'est pas recommandée.
    const stationRequests = Promise.all(fogs.map(async (id) => {
      try {
        const res = await fetch(`${FOG_CONFIG[id].proxyPrefix}/api/v1/piles/station-capacities`, {
          signal: AbortSignal.timeout(12000),
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return { id, stations: mergeLiveStationCapacities(id, this.cachedStations, await res.json()), live: true };
      } catch (error) {
        console.warn(`Disponibilités par station indisponibles pour ${id}`, error);
        return { id, stations: [] as Station[], live: false };
      }
    }));

    const capacityResults = await Promise.all(fogs.map((id) => this.fetchFogCapacity(id)));

    const capacities: Record<FogId, FogCapacity> = {
      'fog-a': capacityResults[0],
      'fog-b': capacityResults[1],
      'fog-c': capacityResults[2],
    };

    const forecastResults = await Promise.all(
      fogs.map((id) => this.fetchFogForecast(id, capacities[id].total_piles))
    );

    const forecasts: Record<FogId, TerritoryForecast> = {
      'fog-a': forecastResults[0],
      'fog-b': forecastResults[1],
      'fog-c': forecastResults[2],
    };

    const stationResults = await stationRequests;
    const stationDataLive = Object.fromEntries(stationResults.map(({ id, live }) => [id, live])) as Record<FogId, boolean>;
    const stations = stationResults.flatMap(({ stations }) => stations);

    const activeFogs = fogs.filter((f) => capacities[f].isOnline);
    const anyLiveForecast = fogs.some((f) => forecasts[f].isLive);

    return {
      capacities,
      forecasts,
      stations,
      stationDataLive,
      systemStatus: {
        isLive: activeFogs.length > 0,
        activeFogs,
        chronosLive: anyLiveForecast,
        lastUpdated: new Date().toLocaleTimeString(),
      },
    };
  }

  // ========================================================================
  // REAL CLOUD SIMULATION — no client-side fabricated state
  // ========================================================================

  private static async simulatorCall(url: string, options?: RequestInit): Promise<SimulatorStatus> {
    const response = await fetch(url, { signal: AbortSignal.timeout(130000), ...options });
    if (!response.ok) {
      const text = await response.text();
      throw new Error(`Simulateur réel indisponible (HTTP ${response.status}): ${text.slice(0, 250)}`);
    }
    const type = response.headers.get('content-type') || '';
    if (!type.includes('application/json')) {
      throw new Error('La réponse du simulateur n’est pas du JSON : vérifier le proxy Vite.');
    }
    return await response.json() as SimulatorStatus;
  }

  public static getSimulatorStatus(): Promise<SimulatorStatus> {
    return this.simulatorCall('/api/v1/simulator/status');
  }

  public static stepSimulator(): Promise<SimulatorStatus> {
    // Aucune arrivée inventée. Un vrai tick PREPARE/SETTLE est déclenché.
    // En cas de délai dépassé : ne PAS relancer aveuglément le tick.
    return this.simulatorCall('/api/v1/simulator/step', {
      method: 'POST',
      headers: { 'X-Requested-With': 'XMLHttpRequest' },
    });
  }

  public static async startSimulator(_intervalSec: number = 2.0): Promise<SimulatorStatus> {
    throw new Error('Mode automatique non activé dans la passerelle réelle.');
  }
  public static async stopSimulator(): Promise<SimulatorStatus> {
    throw new Error('Mode automatique non activé dans la passerelle réelle.');
  }
  public static async resetSimulator(): Promise<SimulatorStatus> {
    throw new Error('Réinitialisation désactivée pour préserver les ticks et données réelles.');
  }
  public static async setSimulatorScenario(_scenario: SimulatorScenario): Promise<SimulatorStatus> {
    throw new Error('Scénarios en cours de raccordement aux véritables arrivées.');
  }
  public static injectSimulatorTraffic(fogId: FogId, count: number): Promise<SimulatorStatus> {
    return this.simulatorCall('/api/v1/simulator/inject', {
      method: 'POST',
      headers: { 'X-Requested-With': 'XMLHttpRequest', 'Content-Type': 'application/json' },
      body: JSON.stringify({ fog_id: fogId, count }),
    });
  }
  public static onSimulatorUpdate(_listener: (status: SimulatorStatus) => void): () => void {
    return () => undefined;
  }
}

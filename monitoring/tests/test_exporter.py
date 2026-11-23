import unittest
from unittest.mock import patch
from monitoring.exporter.main import collect


class ExporterTest(unittest.TestCase):
    def test_metrics_from_real_api_schema(self):
        def fake(url):
            if url.endswith('/health'):
                return {'status': 'healthy'}
            if url.endswith('/api/v1/piles/capacity'):
                fid = next(f for f in ('fog-a','fog-b','fog-c') if f in url)
                return {'fog_id': fid, 'total_piles': 100, 'available_piles': 70,
                        'occupied_piles': 20, 'non_reservable_piles': 10,
                        'out_of_service_piles': 0, 'available_power_kw': 1500.0}
            if url.endswith('/api/v1/round/status'):
                fid = next(f for f in ('fog-a','fog-b','fog-c') if f in url)
                return {'fog_id': fid, 'tick': 8, 'phase': 'SETTLED'}
            if url.endswith('/api/v1/orchestrator/status'):
                return {'current_tick': 8, 'history_pending_tick': None}
            raise RuntimeError(url)
        text = collect(fake)
        self.assertIn('multi_fog_piles_occupied{fog="fog-b"} 20', text)
        self.assertIn('multi_fog_tick{fog="fog-c"} 8', text)
        self.assertIn('multi_fog_orchestrator_tick 8', text)
        self.assertIn('multi_fog_history_pending 0', text)
        self.assertIn('multi_fog_node_up{fog="fog-a"} 1', text)

    def test_one_fog_down_does_not_hide_others(self):
        def fake(url):
            if 'fog-b:8000' in url:
                raise OSError('unavailable')
            if url.endswith('/health'):
                return {'status': 'healthy'}
            if url.endswith('/api/v1/piles/capacity'):
                fid = 'fog-a' if 'fog-a:8000' in url else 'fog-c'
                return {'fog_id': fid, 'total_piles': 1, 'available_piles': 1,
                        'occupied_piles': 0, 'non_reservable_piles': 0,
                        'out_of_service_piles': 0, 'available_power_kw': 22}
            if url.endswith('/api/v1/round/status'):
                fid = 'fog-a' if 'fog-a:8000' in url else 'fog-c'
                return {'fog_id': fid, 'tick': 3, 'phase': 'SETTLED'}
            if url.endswith('/api/v1/orchestrator/status'):
                raise OSError('unavailable')
        text = collect(fake)
        self.assertIn('multi_fog_node_up{fog="fog-b"} 0', text)
        self.assertIn('multi_fog_node_up{fog="fog-a"} 1', text)
        self.assertNotIn('multi_fog_tick{fog="fog-b"}', text)

    def test_pending_history_alert(self):
        def fake(url):
            if url.endswith('/api/v1/orchestrator/status'):
                return {'current_tick': 9, 'history_pending_tick': 9}
            return {'status': 'healthy'}
        text = collect(fake)
        self.assertIn('multi_fog_history_pending 1', text)


if __name__ == '__main__':
    unittest.main()

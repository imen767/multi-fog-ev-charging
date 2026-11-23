"""Read-only Prometheus exporter for the existing Multi-Fog HTTP APIs.

This process never triggers rounds, writes to Fog/Cloud, or exposes credentials.
GET /metrics collects current state; GET /health only reports exporter liveness.
"""
from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request, urlopen

FOG_URLS = {
    fog: os.environ.get(f"FOG_{fog[-1].upper()}_URL", f"http://{fog}:8000").rstrip("/")
    for fog in ("fog-a", "fog-b", "fog-c")
}
SERVICES = {
    "orchestrator": os.environ.get("ORCHESTRATOR_URL", "http://cloud-orchestrator:8000").rstrip("/"),
    "cloud_sync": os.environ.get("CLOUD_SYNC_URL", "http://cloud-sync-api:8000").rstrip("/"),
    "chronos": os.environ.get("CHRONOS_URL", "http://chronos-cloud:8000").rstrip("/"),
    "dashboard_gateway": os.environ.get("DASHBOARD_GATEWAY_URL", "http://dashboard-gateway:8000").rstrip("/"),
}
METRICS = {
    "total_piles": ("multi_fog_piles_total", "Number of piles in Fog catalog"),
    "available_piles": ("multi_fog_piles_available", "Available piles in Fog SQLite"),
    "occupied_piles": ("multi_fog_piles_occupied", "Occupied piles in Fog SQLite"),
    "non_reservable_piles": ("multi_fog_piles_non_reservable", "Non reservable piles in Fog SQLite"),
    "out_of_service_piles": ("multi_fog_piles_out_of_service", "Out of service piles in Fog SQLite"),
    "available_power_kw": ("multi_fog_available_power_kw", "Power available in Fog SQLite (kW)"),
}


def http_json(url: str) -> dict:
    req = Request(url, headers={"Accept": "application/json", "User-Agent": "multi-fog-monitor/1.0"})
    with urlopen(req, timeout=3) as response:
        data = json.load(response)
    if not isinstance(data, dict):
        raise ValueError("Expected JSON object")
    return data


def _sample(name: str, value: float | int, labels: dict[str, str] | None = None) -> str:
    if labels:
        ls = ",".join(f'{k}="{v}"' for k, v in labels.items())
        return f"{name}{{{ls}}} {value}"
    return f"{name} {value}"


def _metric_header(name: str, help_text: str) -> list[str]:
    return [f"# HELP {name} {help_text}", f"# TYPE {name} gauge"]


def collect(fetch=http_json) -> str:
    """Poll in parallel, fail each target independently, and return valid Prometheus text."""
    started = time.monotonic()
    tasks = {}
    with ThreadPoolExecutor(max_workers=12) as pool:
        for fog_id, url in FOG_URLS.items():
            for endpoint in ("/health", "/api/v1/piles/capacity", "/api/v1/round/status"):
                tasks[("fog", fog_id, endpoint)] = pool.submit(fetch, url + endpoint)
        for service, url in SERVICES.items():
            tasks[("service", service, "/health")] = pool.submit(fetch, url + "/health")
        tasks[("orch", "orchestrator", "/api/v1/orchestrator/status")] = pool.submit(
            fetch, SERVICES["orchestrator"] + "/api/v1/orchestrator/status"
        )
        results = {}
        for key, future in tasks.items():
            try:
                results[key] = future.result()
            except Exception:
                results[key] = None

    lines: list[str] = []
    lines += _metric_header("multi_fog_node_up", "1 when Fog health, capacity and round status are readable")
    lines += _metric_header("multi_fog_health_up", "1 when Fog health endpoint responds")
    lines += _metric_header("multi_fog_tick", "Current simulation tick reported by each Fog")
    lines += _metric_header("multi_fog_round_settled", "1 when Fog phase is SETTLED")
    for name, help_text in METRICS.values():
        lines += _metric_header(name, help_text)
    for fog in FOG_URLS:
        health = results[("fog", fog, "/health")]
        cap = results[("fog", fog, "/api/v1/piles/capacity")]
        round_status = results[("fog", fog, "/api/v1/round/status")]
        health_ok = isinstance(health, dict) and health.get("status") == "healthy"
        cap_ok = isinstance(cap, dict) and cap.get("fog_id") == fog and all(
            isinstance(cap.get(k), (int, float)) and not isinstance(cap.get(k), bool)
            for k in METRICS
        )
        tick_ok = (isinstance(round_status, dict) and round_status.get("fog_id") == fog
                   and type(round_status.get("tick")) is int)
        labels = {"fog": fog}
        lines.append(_sample("multi_fog_health_up", int(health_ok), labels))
        lines.append(_sample("multi_fog_node_up", int(health_ok and cap_ok and tick_ok), labels))
        if tick_ok:
            lines.append(_sample("multi_fog_tick", round_status["tick"], labels))
            lines.append(_sample("multi_fog_round_settled", int(round_status.get("phase") == "SETTLED"), labels))
        if cap_ok:
            for key, (name, _) in METRICS.items():
                lines.append(_sample(name, cap[key], labels))

    lines += _metric_header("multi_fog_service_up", "1 when Cloud service health endpoint responds")
    for name in SERVICES:
        item = results[("service", name, "/health")]
        good = isinstance(item, dict) and item.get("status") == "healthy"
        lines.append(_sample("multi_fog_service_up", int(good), {"service": name}))

    lines += _metric_header("multi_fog_orchestrator_tick", "Current tick reported by Cloud orchestrator")
    lines += _metric_header("multi_fog_history_pending", "1 when latest tick has unrecorded Cloud history")
    orch = results[("orch", "orchestrator", "/api/v1/orchestrator/status")]
    if isinstance(orch, dict) and type(orch.get("current_tick")) is int:
        lines.append(_sample("multi_fog_orchestrator_tick", orch["current_tick"]))
        lines.append(_sample("multi_fog_history_pending", int(orch.get("history_pending_tick") is not None)))

    lines += _metric_header("multi_fog_exporter_collection_seconds", "Wall clock time to collect metrics")
    lines.append(_sample("multi_fog_exporter_collection_seconds", round(time.monotonic() - started, 4)))
    return "\n".join(lines) + "\n"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            data, content_type, status = b'{"status":"healthy"}\n', "application/json", 200
        elif self.path == "/metrics":
            try:
                data, content_type, status = collect().encode("utf-8"), "text/plain; version=0.0.4; charset=utf-8", 200
            except Exception:
                # The response must not leak internal URLs or credentials.
                data, content_type, status = b"metrics collection failure\n", "text/plain", 503
        else:
            data, content_type, status = b"Not found\n", "text/plain", 404
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format, *args):
        pass


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()

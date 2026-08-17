from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import shutil
import socket
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from src.db.factory import create_database
from src.utils.config import default_config_path, load_config
from src.utils.dates import now_iso
from src.utils.runtime_log import runtime_log


RANGES = {"1h": 60, "6h": 360, "24h": 1440, "7d": 10080}
DISK_NAME = re.compile(r"^(?:sd[a-z]+|vd[a-z]+|xvd[a-z]+|nvme\d+n\d+)$")


def _number(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _pressure(text: str) -> dict:
    result: dict[str, float] = {}
    for line in text.splitlines():
        parts = line.split()
        if not parts:
            continue
        prefix = parts[0]
        for item in parts[1:]:
            if "=" not in item:
                continue
            key, value = item.split("=", 1)
            if key in {"avg10", "avg60", "avg300"}:
                result[f"{prefix}_{key}"] = _number(value)
    return result


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str, timeout: float = 3.0) -> None:
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.socket_path)


class DockerReadClient:
    """Small GET-only Docker Engine client; it intentionally exposes no mutation API."""

    def __init__(self, socket_path: str = "/var/run/docker.sock", timeout: float = 3.0) -> None:
        self.socket_path = socket_path
        self.timeout = timeout

    def get(self, path: str):
        connection = UnixHTTPConnection(self.socket_path, self.timeout)
        try:
            connection.request("GET", path, headers={"Host": "docker"})
            response = connection.getresponse()
            body = response.read()
            if response.status != HTTPStatus.OK:
                raise RuntimeError(f"Docker API HTTP {response.status}")
            return json.loads(body.decode("utf-8"))
        finally:
            connection.close()

    def collect(self, allowlist: list[str]) -> list[dict]:
        wanted = set(allowlist)
        rows = self.get("/containers/json?all=1")
        by_name: dict[str, dict] = {}
        for row in rows:
            for raw_name in row.get("Names") or []:
                name = str(raw_name).lstrip("/")
                if name in wanted:
                    by_name[name] = row
        containers: list[dict] = []
        for name in allowlist:
            row = by_name.get(name)
            if not row:
                containers.append({"name": name, "state": "missing", "health": "unknown", "available": False})
                continue
            container_id = str(row.get("Id") or "")
            try:
                inspect = self.get(f"/containers/{quote(container_id, safe='')}/json")
                stats = self.get(f"/containers/{quote(container_id, safe='')}/stats?stream=false") if row.get("State") == "running" else {}
                containers.append(self._container_snapshot(name, row, inspect, stats))
            except Exception as exc:
                containers.append({
                    "name": name,
                    "state": str(row.get("State") or "unknown"),
                    "health": "unknown",
                    "available": True,
                    "error": type(exc).__name__,
                })
        return containers

    @staticmethod
    def _container_snapshot(name: str, row: dict, inspect: dict, stats: dict) -> dict:
        state = inspect.get("State") or {}
        cpu = stats.get("cpu_stats") or {}
        precpu = stats.get("precpu_stats") or {}
        cpu_usage = cpu.get("cpu_usage") or {}
        precpu_usage = precpu.get("cpu_usage") or {}
        cpu_delta = _number(cpu_usage.get("total_usage")) - _number(precpu_usage.get("total_usage"))
        system_delta = _number(cpu.get("system_cpu_usage")) - _number(precpu.get("system_cpu_usage"))
        online_cpus = int(cpu.get("online_cpus") or len(cpu_usage.get("percpu_usage") or []) or 1)
        raw_cpu_percent = max(0.0, cpu_delta / system_delta * online_cpus * 100) if system_delta > 0 else 0.0
        host_config = inspect.get("HostConfig") or {}
        nano_cpus = _number(host_config.get("NanoCpus")) / 1_000_000_000
        quota = _number(host_config.get("CpuQuota"))
        period = _number(host_config.get("CpuPeriod"), 100000)
        quota_cpus = nano_cpus or (quota / period if quota > 0 and period > 0 else float(online_cpus))
        cpu_limit_percent = max(0.0, raw_cpu_percent / max(quota_cpus, 0.01))
        memory = stats.get("memory_stats") or {}
        memory_stats = memory.get("stats") or {}
        memory_usage = max(0.0, _number(memory.get("usage")) - _number(memory_stats.get("inactive_file")))
        memory_limit = _number(memory.get("limit"))
        memory_percent = memory_usage / memory_limit * 100 if memory_limit > 0 else 0.0
        throttling = cpu.get("throttling_data") or {}
        periods = _number(throttling.get("periods"))
        throttled_periods = _number(throttling.get("throttled_periods"))
        block_read = block_write = 0.0
        for item in (stats.get("blkio_stats") or {}).get("io_service_bytes_recursive") or []:
            operation = str(item.get("op") or "").lower()
            if operation == "read":
                block_read += _number(item.get("value"))
            elif operation == "write":
                block_write += _number(item.get("value"))
        network_rx = network_tx = 0.0
        for network in (stats.get("networks") or {}).values():
            network_rx += _number(network.get("rx_bytes"))
            network_tx += _number(network.get("tx_bytes"))
        health = str(((state.get("Health") or {}).get("Status")) or ("healthy" if state.get("Running") else "unknown"))
        return {
            "name": name,
            "available": True,
            "state": str(row.get("State") or state.get("Status") or "unknown"),
            "health": health,
            "status_text": str(row.get("Status") or ""),
            "started_at": str(state.get("StartedAt") or ""),
            "finished_at": str(state.get("FinishedAt") or ""),
            "restart_count": int(inspect.get("RestartCount") or 0),
            "exit_code": int(state.get("ExitCode") or 0),
            "oom_killed": bool(state.get("OOMKilled")) or _number(memory_stats.get("oom_kill")) > 0,
            "cpu_percent": round(cpu_limit_percent, 2),
            "cpu_host_percent": round(raw_cpu_percent, 2),
            "cpu_limit_cores": round(quota_cpus, 2),
            "cpu_throttled_percent": round(throttled_periods / periods * 100, 2) if periods > 0 else 0.0,
            "memory_usage_bytes": int(memory_usage),
            "memory_limit_bytes": int(memory_limit),
            "memory_percent": round(memory_percent, 2),
            "block_read_bytes": int(block_read),
            "block_write_bytes": int(block_write),
            "network_rx_bytes": int(network_rx),
            "network_tx_bytes": int(network_tx),
        }


class HostSampler:
    def __init__(self, proc_root: str = "/host/proc", storage_path: str = "/app/storage") -> None:
        self.proc_root = Path(proc_root)
        self.storage_path = Path(storage_path)
        self.previous: dict[str, float] = {}

    def collect(self) -> dict:
        now = time.monotonic()
        cpu_total, cpu_idle = self._cpu_times()
        previous_total, previous_idle = self.previous.get("cpu_total", 0), self.previous.get("cpu_idle", 0)
        cpu_delta, idle_delta = cpu_total - previous_total, cpu_idle - previous_idle
        cpu_percent = max(0.0, min(100.0, (cpu_delta - idle_delta) / cpu_delta * 100)) if previous_total and cpu_delta > 0 else 0.0
        meminfo = self._meminfo()
        mem_total = meminfo.get("MemTotal", 0)
        mem_available = meminfo.get("MemAvailable", meminfo.get("MemFree", 0))
        disk_read, disk_write = self._disk_bytes()
        network_rx, network_tx = self._network_bytes()
        elapsed = max(0.001, now - self.previous.get("at", now))
        disk = shutil.disk_usage(self.storage_path) if self.storage_path.exists() else shutil.disk_usage(Path.cwd())
        load = _read_text(self.proc_root / "loadavg").split()
        cpu_count = max(1, sum(1 for line in _read_text(self.proc_root / "cpuinfo").splitlines() if line.strip().startswith("processor") and ":" in line))
        result = {
            "cpu_percent": round(cpu_percent, 2),
            "cpu_count": cpu_count,
            "load_1m": _number(load[0] if load else 0),
            "load_5m": _number(load[1] if len(load) > 1 else 0),
            "memory_total_bytes": int(mem_total),
            "memory_available_bytes": int(mem_available),
            "memory_percent": round((mem_total - mem_available) / mem_total * 100, 2) if mem_total else 0.0,
            "disk_total_bytes": int(disk.total),
            "disk_free_bytes": int(disk.free),
            "disk_percent": round(disk.used / disk.total * 100, 2) if disk.total else 0.0,
            "disk_read_bps": max(0, int((disk_read - self.previous.get("disk_read", disk_read)) / elapsed)),
            "disk_write_bps": max(0, int((disk_write - self.previous.get("disk_write", disk_write)) / elapsed)),
            "network_rx_bps": max(0, int((network_rx - self.previous.get("network_rx", network_rx)) / elapsed)),
            "network_tx_bps": max(0, int((network_tx - self.previous.get("network_tx", network_tx)) / elapsed)),
            "pressure": {
                "cpu": _pressure(_read_text(self.proc_root / "pressure" / "cpu")),
                "memory": _pressure(_read_text(self.proc_root / "pressure" / "memory")),
                "io": _pressure(_read_text(self.proc_root / "pressure" / "io")),
            },
        }
        self.previous = {"at": now, "cpu_total": cpu_total, "cpu_idle": cpu_idle, "disk_read": disk_read, "disk_write": disk_write, "network_rx": network_rx, "network_tx": network_tx}
        return result

    def _cpu_times(self) -> tuple[float, float]:
        first = next((line for line in _read_text(self.proc_root / "stat").splitlines() if line.startswith("cpu ")), "")
        values = [_number(value) for value in first.split()[1:]]
        return sum(values), sum(values[3:5]) if len(values) >= 5 else (values[3] if len(values) > 3 else 0)

    def _meminfo(self) -> dict[str, int]:
        result: dict[str, int] = {}
        for line in _read_text(self.proc_root / "meminfo").splitlines():
            if ":" not in line:
                continue
            key, raw = line.split(":", 1)
            result[key] = int(_number(raw.strip().split()[0]) * 1024)
        return result

    def _disk_bytes(self) -> tuple[int, int]:
        read = write = 0
        for line in _read_text(self.proc_root / "diskstats").splitlines():
            parts = line.split()
            if len(parts) < 14 or not DISK_NAME.match(parts[2]):
                continue
            read += int(_number(parts[5]) * 512)
            write += int(_number(parts[9]) * 512)
        return read, write

    def _network_bytes(self) -> tuple[int, int]:
        rx = tx = 0
        for line in _read_text(self.proc_root / "net" / "dev").splitlines():
            if ":" not in line:
                continue
            name, raw = line.split(":", 1)
            if name.strip() == "lo":
                continue
            parts = raw.split()
            if len(parts) >= 9:
                rx += int(_number(parts[0])); tx += int(_number(parts[8]))
        return rx, tx


class MetricsStore:
    def __init__(self, path: str | Path, retention_days: int = 7) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.retention_days = max(1, min(30, int(retention_days)))
        with self.connect() as db:
            db.execute("pragma journal_mode=wal")
            db.execute("create table if not exists metric_buckets(bucket_at text primary key, snapshot_json text not null)")

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=5)
        db.execute("pragma busy_timeout=5000")
        return db

    def save(self, bucket_at: str, snapshot: dict) -> None:
        cutoff = (datetime.fromisoformat(bucket_at) - timedelta(days=self.retention_days)).isoformat(timespec="minutes")
        with self.connect() as db:
            db.execute("insert or replace into metric_buckets(bucket_at,snapshot_json) values (?,?)", (bucket_at, json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))))
            db.execute("delete from metric_buckets where bucket_at<?", (cutoff,))

    def history(self, range_name: str) -> list[dict]:
        minutes = RANGES.get(range_name, RANGES["1h"])
        cutoff = (datetime.fromisoformat(now_iso()) - timedelta(minutes=minutes)).isoformat(timespec="minutes")
        with self.connect() as db:
            rows = db.execute("select bucket_at,snapshot_json from metric_buckets where bucket_at>=? order by bucket_at", (cutoff,)).fetchall()
        return [{"at": row[0], **json.loads(row[1])} for row in rows]


def _average(values: list[float]) -> float:
    return round(sum(values) / len(values), 2) if values else 0.0


def aggregate_snapshots(samples: list[dict]) -> dict:
    if not samples:
        return {}
    host_keys = ("cpu_percent", "memory_percent", "disk_percent", "disk_read_bps", "disk_write_bps", "network_rx_bps", "network_tx_bps")
    host = {key: _average([_number(sample.get("host", {}).get(key)) for sample in samples]) for key in host_keys}
    host["disk_free_bytes"] = int(samples[-1].get("host", {}).get("disk_free_bytes") or 0)
    host["pressure"] = samples[-1].get("host", {}).get("pressure") or {}
    names = {container.get("name") for sample in samples for container in sample.get("containers") or []}
    containers: dict[str, dict] = {}
    for name in names:
        rows = [next((item for item in sample.get("containers") or [] if item.get("name") == name), {}) for sample in samples]
        containers[str(name)] = {
            key: _average([_number(row.get(key)) for row in rows])
            for key in ("cpu_percent", "memory_percent", "cpu_throttled_percent")
        }
        containers[str(name)]["restart_count"] = int(rows[-1].get("restart_count") or 0)
        containers[str(name)]["oom_killed"] = bool(rows[-1].get("oom_killed"))
    return {"host": host, "containers": containers}


class ResourceCollector:
    def __init__(self, config: dict, db=None) -> None:
        self.db = db
        settings = config.get("resource_monitor", {})
        self.enabled = bool(settings.get("enabled", True))
        self.interval = max(5, int(settings.get("sample_interval_seconds", 10)))
        self.allowlist = [str(item) for item in settings.get("container_allowlist") or []]
        storage = Path(config.get("storage", {}).get("root_dir", "../storage"))
        metrics_path = settings.get("metrics_path") or str(storage / "resource_metrics.sqlite3")
        self.store = MetricsStore(metrics_path, int(settings.get("retention_days", 7)))
        self.docker = DockerReadClient(str(settings.get("docker_socket", "/var/run/docker.sock")))
        self.host = HostSampler(str(settings.get("host_proc_path", "/host/proc")), str(settings.get("storage_probe_path", storage)))
        self.snapshot: dict = {"collected_at": "", "host": {}, "containers": [], "source_status": {"collector": "starting", "docker": "unknown", "host": "unknown"}}
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.minute = ""
        self.minute_samples: list[dict] = []
        self.source_errors: dict[str, str] = {}

    def collect_once(self) -> dict:
        source = {"collector": "online", "docker": "online", "host": "online"}
        host: dict = {}
        containers: list[dict] = []
        try:
            host = self.host.collect()
        except Exception as exc:
            source["host"] = f"unavailable:{type(exc).__name__}"
            self._source_error("host", exc)
        else:
            self._source_recovered("host")
        try:
            containers = self.docker.collect(self.allowlist)
        except Exception as exc:
            source["docker"] = f"unavailable:{type(exc).__name__}"
            self._source_error("docker", exc)
        else:
            self._source_recovered("docker")
        snapshot = {"collected_at": now_iso(), "host": host, "containers": containers, "source_status": source}
        with self.lock:
            self.snapshot = snapshot
        self._rollup(snapshot)
        return snapshot

    def _source_error(self, source: str, exc: Exception) -> None:
        kind = type(exc).__name__
        if self.source_errors.get(source) == kind:
            return
        self.source_errors[source] = kind
        runtime_log(self.db, "monitor", f"{source}_unavailable", f"资源监控 {source} 数据源不可用", level="warning", context={"error_type": kind}, exc=exc)

    def _source_recovered(self, source: str) -> None:
        if source not in self.source_errors:
            return
        del self.source_errors[source]
        runtime_log(self.db, "monitor", f"{source}_recovered", f"资源监控 {source} 数据源已恢复")

    def _rollup(self, snapshot: dict) -> None:
        minute = str(snapshot["collected_at"])[:16] + ":00"
        if self.minute and minute != self.minute and self.minute_samples:
            self.store.save(self.minute, aggregate_snapshots(self.minute_samples))
            self.minute_samples = []
        self.minute = minute
        self.minute_samples.append(snapshot)

    def run(self) -> None:
        runtime_log(self.db, "monitor", "started", "资源监控采集器已启动", context={"interval_seconds": self.interval})
        while not self.stop_event.is_set():
            if self.enabled:
                try:
                    self.collect_once()
                except Exception:
                    pass
            self.stop_event.wait(self.interval)
        runtime_log(self.db, "monitor", "stopped", "资源监控采集器已停止")

    def current(self) -> dict:
        with self.lock:
            return json.loads(json.dumps(self.snapshot))


class CollectorHandler(BaseHTTPRequestHandler):
    collector: ResourceCollector | None = None

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/internal/snapshot":
            self._json(self.collector.current() if self.collector else {}, HTTPStatus.OK)
            return
        if parsed.path == "/internal/history":
            range_name = str((parse_qs(parsed.query).get("range") or ["1h"])[0])
            if range_name not in RANGES:
                self._json({"error": "invalid_range"}, HTTPStatus.BAD_REQUEST)
                return
            points = self.collector.store.history(range_name) if self.collector else []
            self._json({"range": range_name, "bucket_seconds": 60, "points": points, "collected_at": now_iso()}, HTTPStatus.OK)
            return
        self._json({"error": "not_found"}, HTTPStatus.NOT_FOUND)

    def _json(self, payload: dict, status: HTTPStatus) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args) -> None:
        return


def run_collector(config_path: str) -> None:
    config = load_config(config_path)
    collector = ResourceCollector(config, create_database(config))
    CollectorHandler.collector = collector
    thread = threading.Thread(target=collector.run, name="resource-sampler", daemon=True)
    thread.start()
    settings = config.get("resource_monitor", {})
    host = str(settings.get("collector_host", "0.0.0.0"))
    port = int(settings.get("collector_port", 8010))
    server = ThreadingHTTPServer((host, port), CollectorHandler)
    try:
        server.serve_forever()
    finally:
        collector.stop_event.set()
        server.server_close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Daily Report read-only resource monitor")
    parser.add_argument("--config", default=None)
    args = parser.parse_args()
    run_collector(args.config or str(default_config_path()))


if __name__ == "__main__":
    main()

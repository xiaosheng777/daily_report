from __future__ import annotations

from datetime import datetime
from pathlib import Path

from src.db.sqlite_backend import SQLiteBackend
from src.monitoring.collector import DockerReadClient, HostSampler, MetricsStore, aggregate_snapshots
from src.monitoring.diagnostics import build_history, build_snapshot, diagnose
from src.utils.dates import now_iso


def test_docker_stats_are_normalized_against_container_cpu_limit():
    row = {"State": "running", "Status": "Up 1 hour"}
    inspect = {
        "RestartCount": 2,
        "HostConfig": {"NanoCpus": 2_000_000_000, "CpuQuota": 0, "CpuPeriod": 100000},
        "State": {"Running": True, "Status": "running", "ExitCode": 0, "OOMKilled": False, "StartedAt": "2026-08-14T00:00:00Z", "Health": {"Status": "healthy"}},
    }
    stats = {
        "cpu_stats": {"online_cpus": 4, "system_cpu_usage": 2000, "cpu_usage": {"total_usage": 500}, "throttling_data": {"periods": 100, "throttled_periods": 25}},
        "precpu_stats": {"system_cpu_usage": 1000, "cpu_usage": {"total_usage": 250}},
        "memory_stats": {"usage": 900, "limit": 1000, "stats": {"inactive_file": 100}},
        "blkio_stats": {"io_service_bytes_recursive": [{"op": "read", "value": 40}, {"op": "write", "value": 60}]},
        "networks": {"eth0": {"rx_bytes": 70, "tx_bytes": 80}},
    }

    result = DockerReadClient._container_snapshot("daily-report-worker", row, inspect, stats)

    assert result["cpu_percent"] == 50.0
    assert result["cpu_throttled_percent"] == 25.0
    assert result["memory_percent"] == 80.0
    assert result["block_write_bytes"] == 60
    assert result["restart_count"] == 2


def test_host_sampler_reads_proc_pressure_memory_disk_and_network(tmp_path: Path):
    proc = tmp_path / "proc"
    (proc / "pressure").mkdir(parents=True)
    (proc / "net").mkdir()
    (proc / "stat").write_text("cpu  100 0 100 800 0 0 0 0\n", encoding="utf-8")
    (proc / "cpuinfo").write_text("processor : 0\nprocessor : 1\n", encoding="utf-8")
    (proc / "loadavg").write_text("1.25 0.75 0.50 1/10 1\n", encoding="utf-8")
    (proc / "meminfo").write_text("MemTotal: 1000 kB\nMemAvailable: 250 kB\n", encoding="utf-8")
    (proc / "diskstats").write_text("8 0 sda 0 0 10 0 0 0 20 0 0 0 0 0\n", encoding="utf-8")
    (proc / "net" / "dev").write_text("eth0: 100 0 0 0 0 0 0 0 200 0 0 0 0 0 0 0\n", encoding="utf-8")
    for kind, value in (("cpu", "some avg10=3.00 avg60=2.00 avg300=1.00 total=1\n"), ("memory", "some avg10=4.00 avg60=2.00 avg300=1.00 total=1\n"), ("io", "some avg10=12.50 avg60=2.00 avg300=1.00 total=1\n")):
        (proc / "pressure" / kind).write_text(value, encoding="utf-8")

    result = HostSampler(str(proc), str(tmp_path)).collect()

    assert result["cpu_count"] == 2
    assert result["memory_percent"] == 75.0
    assert result["pressure"]["io"]["some_avg10"] == 12.5
    assert result["disk_total_bytes"] > 0


def test_metric_rollup_and_store_return_minute_history(tmp_path: Path):
    samples = [
        {"host": {"cpu_percent": 20, "memory_percent": 40, "disk_percent": 50, "disk_free_bytes": 10, "pressure": {}}, "containers": [{"name": "daily-report-worker", "cpu_percent": 50, "memory_percent": 30, "cpu_throttled_percent": 0, "restart_count": 1}]},
        {"host": {"cpu_percent": 40, "memory_percent": 60, "disk_percent": 50, "disk_free_bytes": 9, "pressure": {}}, "containers": [{"name": "daily-report-worker", "cpu_percent": 70, "memory_percent": 50, "cpu_throttled_percent": 10, "restart_count": 1}]},
    ]
    bucket = aggregate_snapshots(samples)
    store = MetricsStore(tmp_path / "metrics.sqlite3", retention_days=7)
    at = now_iso()[:16] + ":00"
    store.save(at, bucket)

    points = store.history("1h")

    assert bucket["host"]["cpu_percent"] == 30.0
    assert bucket["containers"]["daily-report-worker"]["cpu_percent"] == 60.0
    assert points[-1]["at"] == at


def test_diagnostics_prioritize_worker_offline_memory_and_llm_wait():
    infra = {
        "host": {"disk_total_bytes": 1000, "disk_free_bytes": 500, "pressure": {"io": {"some_avg10": 0}}},
        "containers": [{"name": "daily-report-worker", "state": "running", "health": "healthy", "memory_percent": 96, "cpu_percent": 10, "cpu_throttled_percent": 0}],
    }
    app = {
        "worker": {"online": False, "heartbeat_at": ""},
        "counts": {"queued": 4}, "oldest_queue_seconds": 700, "active": [], "stage_p95_seconds": {},
        "llm": {"active_count": 1, "active": [{"age_seconds": 90}], "sample_count": 0, "error_count": 0},
    }

    results = diagnose(infra, app, [], 90)

    codes = [item["code"] for item in results]
    assert codes[0] in {"container_memory", "worker_offline"}
    assert {"container_memory", "worker_offline", "queue_capacity", "llm_wait"}.issubset(codes)


def test_missing_host_disk_metrics_do_not_report_disk_full():
    app = {"worker": {"online": True}, "counts": {"queued": 0}, "active": [], "oldest_queue_seconds": 0, "stage_p95_seconds": {}, "llm": {"active": [], "active_count": 0, "sample_count": 0, "error_count": 0}}

    results = diagnose({"host": {}, "containers": []}, app, [], 90)

    assert "disk_space" not in {item["code"] for item in results}


def test_sqlite_monitor_migration_progress_and_llm_telemetry(tmp_path: Path):
    db = SQLiteBackend(tmp_path / "daily.sqlite3")
    db.initialize(seed_demo_users=False)
    db.start_llm_call("call-1", "task-1", now_iso())
    db.finish_llm_call("call-1", now_iso(), 1200, True)

    summary = db.resource_monitor_summary()
    columns = {row[1] for row in db.connect().execute("pragma table_info(check_tasks)").fetchall()}

    assert "progress_updated_at" in columns
    assert summary["llm"]["sample_count"] == 1
    assert summary["llm"]["p95_ms"] == 1200


def test_snapshot_degrades_without_collector_and_invalid_history_range(tmp_path: Path):
    db = SQLiteBackend(tmp_path / "daily.sqlite3")
    db.initialize(seed_demo_users=False)
    storage = tmp_path / "storage"
    storage.mkdir()
    config = {
        "resource_monitor": {"enabled": False, "retention_days": 7},
        "task_runner": {"stale_heartbeat_seconds": 90},
        "storage": {"root_dir": str(storage)},
        "jplag": {"enabled": False, "jar_path": str(tmp_path / "missing.jar"), "java_command": "missing-java"},
        "llm_judge": {"enabled": False},
    }

    snapshot = build_snapshot(config, db)

    assert snapshot["source_status"]["collector"] == "disabled"
    assert snapshot["tasks"]["counts"]["queued"] == 0
    assert snapshot["dependencies"]["sqlite"]["size_bytes"] > 0
    assert "jplag" not in snapshot["dependencies"]
    try:
        build_history(config, "2h")
        assert False, "invalid range should fail"
    except ValueError:
        pass


def test_snapshot_maps_local_and_llm_pipeline_stages():
    class MonitorDB:
        def __init__(self, stage: str):
            self.stage = stage

        def resource_monitor_summary(self, _retention_days: int):
            return {
                "sqlite": {"size_bytes": 1},
                "counts": {"queued": 0},
                "active": [{"task_id": "task-1", "status": "running", "progress_stage": self.stage}],
                "llm": {"active_count": 0, "active": [], "sample_count": 0, "error_count": 0},
                "stage_p95_seconds": {},
            }

        def get_worker_lease(self):
            return {"worker_id": "worker-1", "heartbeat_at": now_iso()}

    config = {
        "resource_monitor": {"enabled": False},
        "task_runner": {"stale_heartbeat_seconds": 90},
        "storage": {"root_dir": "__missing_resource_monitor_storage__"},
        "llm_judge": {"enabled": True},
    }

    snapshot = build_snapshot(config, MonitorDB("local_filtering"))

    assert snapshot["tasks"]["active_chain_stage"] == "local"
    assert build_snapshot(config, MonitorDB("llm_judging"))["tasks"]["active_chain_stage"] == "llm"


def test_worker_compose_removes_cpu_cap_and_raises_memory_limit():
    root = Path(__file__).resolve().parents[1]
    for compose in (root / "docker-compose.yml", root / "deploy" / "docker-compose.yml"):
        text = compose.read_text(encoding="utf-8")
        worker = text.split("  daily-report-worker:\n", 1)[1].split("\n  daily-report-monitor:", 1)[0]

        assert "cpus:" not in worker
        assert "mem_limit: 64g" in worker

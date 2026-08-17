from __future__ import annotations

import json
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

from src.utils.dates import now_iso


VALID_RANGES = {"1h", "6h", "24h", "7d"}


def _collector_json(config: dict, path: str) -> dict:
    settings = config.get("resource_monitor", {})
    base_url = str(settings.get("collector_url") or "http://daily-report-monitor:8010").rstrip("/")
    request = urllib.request.Request(base_url + path, method="GET", headers={"Accept": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=2.5) as response:
        return json.load(response)


def _age_seconds(value: str) -> int:
    try:
        return max(0, int((datetime.fromisoformat(now_iso()) - datetime.fromisoformat(str(value or ""))).total_seconds()))
    except ValueError:
        return 0


def _diagnosis(severity: str, code: str, title: str, evidence: str, recommendation: str) -> dict:
    return {"severity": severity, "code": code, "title": title, "evidence": evidence, "recommendation": recommendation}


def _previous_container(history: list[dict], name: str) -> dict:
    for point in reversed(history):
        row = (point.get("containers") or {}).get(name)
        if row:
            return row
    return {}


def diagnose(infra: dict, app: dict, history: list[dict], stale_seconds: int) -> list[dict]:
    diagnoses: list[dict] = []
    containers = infra.get("containers") or []
    host = infra.get("host") or {}
    for container in containers:
        name = str(container.get("name") or "容器")
        state = str(container.get("state") or "unknown")
        health = str(container.get("health") or "unknown")
        if state not in {"running"}:
            diagnoses.append(_diagnosis("critical", "container_down", f"{name} 未运行", f"Docker 状态为 {state}，退出码 {container.get('exit_code', 0)}。", "检查容器日志和 Compose 服务状态；资源监控不会自动重启容器。"))
            continue
        if health == "unhealthy":
            diagnoses.append(_diagnosis("critical", "container_unhealthy", f"{name} 健康检查失败", "容器仍在运行，但 Docker health 状态为 unhealthy。", "检查对应服务健康检查与最近日志。"))
        previous = _previous_container(history[:-1], name)
        if previous and int(container.get("restart_count") or 0) > int(previous.get("restart_count") or 0):
            diagnoses.append(_diagnosis("critical", "container_restarted", f"{name} 最近发生重启", f"重启次数从 {previous.get('restart_count', 0)} 增至 {container.get('restart_count', 0)}。", "检查退出码、OOM 状态和容器日志。"))
        if container.get("oom_killed"):
            diagnoses.append(_diagnosis("critical", "container_oom", f"{name} 出现 OOM", "Docker 报告容器曾被内存不足终止。", "降低并行数或提高容器内存上限，并检查大文件任务。"))
        memory = float(container.get("memory_percent") or 0)
        cpu = float(container.get("cpu_percent") or 0)
        throttled = float(container.get("cpu_throttled_percent") or 0)
        if memory >= 95:
            diagnoses.append(_diagnosis("critical", "container_memory", f"{name} 内存接近耗尽", f"已使用容器内存上限的 {memory:.1f}%。", "降低日报检查并行数，或确认后提高内存上限。"))
        elif memory >= 85:
            diagnoses.append(_diagnosis("warning", "container_memory", f"{name} 内存压力较高", f"已使用容器内存上限的 {memory:.1f}%。", "观察任务结束后是否回落，并检查附件规模。"))
        if cpu >= 95 or throttled >= 20:
            diagnoses.append(_diagnosis("critical", "container_cpu", f"{name} CPU 已受限", f"配额使用 {cpu:.1f}%，调度周期限流 {throttled:.1f}%。", "降低单任务并行数，或为该容器增加 CPU 配额。"))
        elif cpu >= 85:
            recent = [float(((point.get("containers") or {}).get(name) or {}).get("cpu_percent") or 0) for point in history[-5:]]
            if len(recent) >= 5 and all(value >= 85 for value in recent):
                diagnoses.append(_diagnosis("warning", "container_cpu", f"{name} CPU 持续繁忙", f"最近 5 分钟配额使用率均高于 85%，当前 {cpu:.1f}%。", "任务以本地计算为主时可降低并行数或增加 CPU 配额。"))
    disk_total = int(host.get("disk_total_bytes") or 0)
    disk_free = int(host.get("disk_free_bytes") or 0)
    free_percent = disk_free / disk_total * 100 if disk_total else 100
    gib = disk_free / 1024 ** 3
    if disk_total > 0 and (free_percent <= 5 or gib <= 2):
        diagnoses.append(_diagnosis("critical", "disk_space", "存储磁盘空间严重不足", f"仅剩 {free_percent:.1f}%（{gib:.1f} GiB）。", "立即清理历史附件、导出文件或扩容 storage 所在磁盘。"))
    elif disk_total > 0 and (free_percent <= 15 or gib <= 10):
        diagnoses.append(_diagnosis("warning", "disk_space", "存储磁盘空间偏低", f"剩余 {free_percent:.1f}%（{gib:.1f} GiB）。", "安排清理历史文件并评估扩容。"))
    io_pressure = float((((host.get("pressure") or {}).get("io") or {}).get("some_avg10")) or 0)
    if io_pressure >= 25:
        diagnoses.append(_diagnosis("critical", "io_pressure", "磁盘 I/O 严重拥塞", f"Linux I/O pressure avg10 为 {io_pressure:.1f}%。", "检查大文件读写、SQLite 竞争和其他容器磁盘负载。"))
    elif io_pressure >= 10:
        diagnoses.append(_diagnosis("warning", "io_pressure", "磁盘 I/O 存在等待", f"Linux I/O pressure avg10 为 {io_pressure:.1f}%。", "观察导出、附件解析与查重是否同时执行。"))
    lease = app.get("worker") or {}
    counts = app.get("counts") or {}
    if not lease.get("online"):
        diagnoses.append(_diagnosis("critical", "worker_offline", "后台 worker 离线", f"有 {counts.get('queued', 0)} 个排队任务；最后心跳为 {lease.get('heartbeat_at') or '从未上报'}。", "检查 daily-report-worker 容器与数据库挂载。"))
    for task in app.get("active") or []:
        if task.get("status") != "running":
            continue
        heartbeat_age = int(task.get("heartbeat_age_seconds") or 0)
        if heartbeat_age > stale_seconds:
            diagnoses.append(_diagnosis("critical", "task_heartbeat", "运行任务心跳已中断", f"任务 {task.get('task_id')} 已 {heartbeat_age} 秒无心跳。", "检查 worker 子进程是否退出或被宿主机阻塞。"))
        elif heartbeat_age > 30:
            diagnoses.append(_diagnosis("warning", "task_heartbeat", "运行任务心跳延迟", f"任务 {task.get('task_id')} 已 {heartbeat_age} 秒无心跳。", "观察 worker CPU、内存和 I/O 压力。"))
        stage = str(task.get("progress_stage") or "starting")
        threshold = max(180, int((app.get("stage_p95_seconds") or {}).get(stage) or 0) * 2)
        stalled = int(task.get("progress_stalled_seconds") or 0)
        if stalled > threshold:
            llm_active = int((app.get("llm") or {}).get("active_count") or 0)
            worker = next((item for item in containers if item.get("name") == "daily-report-worker"), {})
            if llm_active:
                cause, recommendation = "任务正在等待大模型响应", "检查内网模型服务负载与网络；当前应用不会自动发起额外探测。"
            elif float(worker.get("cpu_percent") or 0) >= 85:
                cause, recommendation = "worker CPU 可能是瓶颈", "降低日报检查并行数，或增加 worker CPU 配额。"
            elif float(worker.get("memory_percent") or 0) >= 85:
                cause, recommendation = "worker 内存可能是瓶颈", "降低并行数并检查本次任务附件规模。"
            elif io_pressure >= 10:
                cause, recommendation = "磁盘 I/O 可能是瓶颈", "检查 storage 磁盘与同时运行的导出、附件解析。"
            else:
                cause, recommendation = "进度停滞但主机资源未见饱和", "检查当前阶段日志、外部命令和依赖响应。"
            diagnoses.append(_diagnosis("warning", "task_stalled", cause, f"任务 {task.get('task_id')} 在 {stage} 阶段已 {stalled} 秒无进度变化，阈值 {threshold} 秒。", recommendation))
    if int(counts.get("queued") or 0) > 3 or int(app.get("oldest_queue_seconds") or 0) > 600:
        diagnoses.append(_diagnosis("warning", "queue_capacity", "单 worker 队列容量不足", f"当前排队 {counts.get('queued', 0)} 个，最老任务已等待 {app.get('oldest_queue_seconds', 0)} 秒。", "错峰创建任务；若长期积压，再评估多 worker 架构。"))
    llm = app.get("llm") or {}
    longest_active = max([int(item.get("age_seconds") or 0) for item in llm.get("active") or []] or [0])
    if longest_active > 60:
        diagnoses.append(_diagnosis("warning", "llm_wait", "大模型响应等待较长", f"最长活跃调用已等待 {longest_active} 秒。", "检查模型服务负载、网络和上游限流。"))
    samples = int(llm.get("sample_count") or 0)
    errors = int(llm.get("error_count") or 0)
    if samples >= 5 and errors / samples > .20:
        diagnoses.append(_diagnosis("warning", "llm_errors", "大模型调用错误率偏高", f"最近 {samples} 次调用中 {errors} 次失败。", "检查模型服务状态、认证和限流。"))
    if samples >= 5 and int(llm.get("p95_ms") or 0) > 30000:
        diagnoses.append(_diagnosis("warning", "llm_latency", "大模型 P95 延迟偏高", f"最近调用 P95 为 {int(llm.get('p95_ms') or 0) / 1000:.1f} 秒。", "检查模型服务容量并减少候选复核数量。"))
    rank = {"critical": 0, "warning": 1, "info": 2}
    return sorted(diagnoses, key=lambda item: rank.get(item["severity"], 9))


def build_snapshot(config: dict, db) -> dict:
    settings = config.get("resource_monitor", {})
    retention_days = int(settings.get("retention_days", 7))
    app = db.resource_monitor_summary(retention_days)
    lease = db.get_worker_lease()
    stale_seconds = max(30, int(config.get("task_runner", {}).get("stale_heartbeat_seconds", 90)))
    app["worker"] = {
        "online": bool(lease and _age_seconds(str(lease.get("heartbeat_at") or "")) <= stale_seconds),
        "worker_id": str((lease or {}).get("worker_id") or ""),
        "heartbeat_at": str((lease or {}).get("heartbeat_at") or ""),
    }
    source = {"collector": "offline", "docker": "unknown", "host": "unknown", "application": "online"}
    infra = {"host": {}, "containers": [], "collected_at": ""}
    history: list[dict] = []
    if bool(settings.get("enabled", True)):
        try:
            infra = _collector_json(config, "/internal/snapshot")
            source.update(infra.get("source_status") or {})
            source["collector"] = "online"
            history = (_collector_json(config, "/internal/history?range=1h").get("points") or [])
        except Exception as exc:
            source["collector"] = f"offline:{type(exc).__name__}"
    else:
        source["collector"] = "disabled"
    storage_root = Path(config.get("storage", {}).get("root_dir") or "")
    storage_bytes = sum(path.stat().st_size for path in storage_root.rglob("*") if path.is_file()) if storage_root.exists() else 0
    dependencies = {
        "sqlite": app.pop("sqlite"),
        "storage": {"root_bytes": storage_bytes, "available": storage_root.exists()},
        "llm": {"enabled": bool(config.get("llm_judge", {}).get("enabled", False)), **app.get("llm", {})},
    }
    diagnoses = diagnose(infra, app, history, stale_seconds)
    if source["collector"] != "online":
        diagnoses.append(_diagnosis("warning", "collector_offline", "资源采集侧车不可用", "Docker 与宿主机实时指标暂不可见，任务和数据库状态仍可使用。", "检查 daily-report-monitor 容器及 Docker socket 挂载。"))
    overall = "critical" if any(item["severity"] == "critical" for item in diagnoses) else "warning" if diagnoses else "healthy"
    active_task = next((task for task in app.get("active") or [] if task.get("status") == "running"), None)
    stage = str((active_task or {}).get("progress_stage") or ("queued" if app.get("counts", {}).get("queued") else "idle"))
    chain_stage = "llm" if dependencies["llm"].get("active_count") else "local" if stage in {"loading", "preparing", "checking", "starting", "resuming"} else "export" if stage == "exporting" else stage
    return {
        "overall_status": overall,
        "diagnoses": diagnoses,
        "host": infra.get("host") or {},
        "containers": infra.get("containers") or [],
        "tasks": {**app, "active_chain_stage": chain_stage, "active_task_id": str((active_task or {}).get("task_id") or "")},
        "dependencies": dependencies,
        "source_status": source,
        "collected_at": str(infra.get("collected_at") or now_iso()),
    }


def build_history(config: dict, range_name: str) -> dict:
    if range_name not in VALID_RANGES:
        raise ValueError("invalid resource monitor range")
    try:
        data = _collector_json(config, f"/internal/history?range={range_name}")
        data["source_status"] = {"collector": "online"}
        return data
    except Exception as exc:
        return {"range": range_name, "bucket_seconds": 60, "points": [], "collected_at": now_iso(), "source_status": {"collector": f"offline:{type(exc).__name__}"}}

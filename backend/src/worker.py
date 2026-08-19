from __future__ import annotations

import argparse
import os
import signal
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from src.core.automation import DailyDuplicateScheduler
from src.db.factory import create_database
from src.llm.limiter import GlobalLLMPermitServer
from src.utils.config import default_config_path, load_config
from src.utils.dates import now_iso
from src.utils.security import token
from src.utils.runtime_log import runtime_log

DEFAULT_TASK_TIMEOUT_SECONDS = 6 * 60 * 60


@dataclass
class RunningTask:
    task_id: str
    attempt_no: int
    process: subprocess.Popen
    started_at: float
    timeout_seconds: int
    last_heartbeat_at: float = 0.0
    cancel_seen_at: float | None = None


class DuplicateWorker:
    def __init__(self, config_path: str) -> None:
        self.config_path = config_path
        self.config = load_config(config_path)
        self.db = create_database(self.config)
        settings = self.config.get("task_runner", {})
        self.timeout_seconds = max(600, min(86400, int(settings.get("task_timeout_seconds", DEFAULT_TASK_TIMEOUT_SECONDS))))
        self.max_concurrent_tasks = max(1, min(8, int(settings.get("max_concurrent_tasks", 2))))
        self.stale_seconds = max(30, int(settings.get("stale_heartbeat_seconds", 90)))
        self.shutdown_grace_seconds = max(5, int(settings.get("shutdown_grace_seconds", 30)))
        self.poll_seconds = max(0.5, float(settings.get("poll_interval_seconds", 2)))
        self.worker_id = f"worker-{os.getpid()}"
        self.lease_token = token("WL_", 24)
        self.stop_requested = False
        self.scheduler = DailyDuplicateScheduler(self.config, self.db, None)
        llm_cfg = self.config.get("llm_judge", {})
        self.llm_permits = GlobalLLMPermitServer(max(1, int(llm_cfg.get("global_max_concurrency", 30))))
        self.running_tasks: dict[str, RunningTask] = {}

    @staticmethod
    def _cutoff(seconds: int) -> str:
        return (datetime.fromisoformat(now_iso()) - timedelta(seconds=seconds)).isoformat(timespec="seconds")

    def request_stop(self, *_args) -> None:
        self.stop_requested = True
        runtime_log(self.db, "worker", "stop_requested", "后台 Worker 收到停止请求", context={"worker_id": self.worker_id})

    def run(self) -> int:
        if not self.db.acquire_worker_lease(self.worker_id, self.lease_token, self._cutoff(self.stale_seconds)):
            runtime_log(self.db, "worker", "lease_rejected", "后台 Worker 已有其他实例在运行", level="warning", context={"worker_id": self.worker_id})
            return 2
        runtime_log(self.db, "worker", "started", "后台 Worker 已启动并获得执行租约", context={"worker_id": self.worker_id})
        self.llm_permits.start()
        self._reap_task_deletions()
        self.db.fail_stale_tasks(self._cutoff(self.stale_seconds))
        try:
            while not self.stop_requested:
                self.db.heartbeat_worker(self.worker_id, self.lease_token)
                self._reap_task_deletions()
                self.scheduler.run_due()
                self._refresh_supervisor_settings()
                self._supervise_running_tasks()
                self._claim_available_tasks()
                time.sleep(self.poll_seconds)
        except Exception as exc:
            runtime_log(self.db, "worker", "loop_failed", "后台 Worker 主循环异常退出", level="error", context={"worker_id": self.worker_id}, exc=exc)
            raise
        finally:
            self._shutdown_running_tasks()
            self.llm_permits.stop()
            self.db.release_worker_lease(self.worker_id, self.lease_token)
            runtime_log(self.db, "worker", "stopped", "后台 Worker 已释放执行租约", context={"worker_id": self.worker_id})
        return 0

    def _start_claimed_task(self, task: dict) -> None:
        task_id = str(task["task_id"])
        attempt = int(task.get("current_attempt") or 1)
        command = [sys.executable, "-m", "src.core.task_job", "--config", self.config_path, "--task-id", task_id, "--attempt", str(attempt)]
        popen_options = {"start_new_session": True} if os.name != "nt" else {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        child_environment = os.environ.copy()
        child_environment.update(self.llm_permits.child_environment())
        process = subprocess.Popen(command, env=child_environment, **popen_options)
        task_cfg = (task.get("config_snapshot") or self.config).get("task_runner", {})
        timeout_seconds = max(600, min(86400, int(task_cfg.get("task_timeout_seconds", self.timeout_seconds))))
        self.running_tasks[task_id] = RunningTask(task_id, attempt, process, time.monotonic(), timeout_seconds)
        runtime_log(self.db, "worker", "task_process_started", "已启动查重任务子进程", task_id=task_id, attempt_no=attempt, context={"pid": process.pid, "worker_id": self.worker_id})

    def _claim_available_tasks(self) -> None:
        while len(self.running_tasks) < self.max_concurrent_tasks and not self.stop_requested:
            task = self.db.claim_next_task(self.worker_id, self.lease_token)
            if not task:
                return
            self._start_claimed_task(task)

    def _refresh_supervisor_settings(self) -> None:
        get_settings = getattr(self.db, "get_settings", None)
        if not callable(get_settings):
            return
        defaults = {
            "task_runner.max_concurrent_tasks": self.max_concurrent_tasks,
            "task_runner.task_timeout_seconds": self.timeout_seconds,
        }
        try:
            stored = get_settings(defaults)
            self.max_concurrent_tasks = max(1, min(8, int(stored.get("task_runner.max_concurrent_tasks", self.max_concurrent_tasks))))
            self.timeout_seconds = max(600, min(86400, int(stored.get("task_runner.task_timeout_seconds", self.timeout_seconds))))
        except (TypeError, ValueError):
            return

    def _supervise_running_tasks(self) -> None:
        for task_id, running in list(self.running_tasks.items()):
            if self._supervise_task(running):
                self.running_tasks.pop(task_id, None)

    def _supervise_task(self, running: RunningTask) -> bool:
        task_id, attempt, process = running.task_id, running.attempt_no, running.process
        if process.poll() is not None:
            return self._finish_exited_task(running)
        elapsed = time.monotonic() - running.started_at
        if elapsed - running.last_heartbeat_at >= 10:
            self.db.heartbeat_task(task_id, self.lease_token)
            running.last_heartbeat_at = elapsed
        control = self.db.task_control_request(task_id, attempt)
        if control == "delete":
            runtime_log(self.db, "worker", "task_delete_requested", "收到删除任务请求，正在终止子进程", task_id=task_id, attempt_no=attempt, level="warning")
            self._terminate(process)
            if self.db.complete_task_deletion(task_id, attempt):
                self._remove_task_files(task_id)
            return True
        if control == "terminate":
            runtime_log(self.db, "worker", "task_terminate_requested", "收到强制终止任务请求", task_id=task_id, attempt_no=attempt, level="warning")
            self._terminate(process)
            self.db.complete_force_termination(task_id, attempt)
            return True
        if control == "cancel":
            running.cancel_seen_at = running.cancel_seen_at or time.monotonic()
            if time.monotonic() - running.cancel_seen_at >= self.shutdown_grace_seconds:
                runtime_log(self.db, "worker", "task_cancelled", "任务取消等待超时，正在终止子进程", task_id=task_id, attempt_no=attempt, level="warning")
                self._terminate(process)
                if (self.db.get_task_status(task_id) or {}).get("status") == "running":
                    self.db.finish_task(task_id, "cancelled", {}, "任务已取消", "cancelled", attempt)
                return True
        if elapsed >= running.timeout_seconds:
            runtime_log(self.db, "worker", "task_timeout", "任务达到最大运行时限", task_id=task_id, attempt_no=attempt, level="error", context={"timeout_seconds": running.timeout_seconds})
            self._terminate(process)
            self.db.finish_task(task_id, "failed", {}, f"任务超过 {running.timeout_seconds // 60} 分钟时限", "timeout", attempt)
            return True
        return False

    def _finish_exited_task(self, running: RunningTask) -> bool:
        task_id, attempt, process = running.task_id, running.attempt_no, running.process
        current = self.db.get_task_status(task_id) or {}
        if current.get("status") != "running":
            return True
        control = self.db.task_control_request(task_id, attempt)
        if control == "delete":
            if self.db.complete_task_deletion(task_id, attempt):
                self._remove_task_files(task_id)
        elif control == "terminate":
            self.db.complete_force_termination(task_id, attempt)
        elif control == "pause":
            self.db.complete_task_pause(task_id, attempt)
        elif control == "cancel":
            self.db.finish_task(task_id, "cancelled", {}, "任务已取消", "cancelled", attempt)
        elif process.returncode:
            runtime_log(self.db, "worker", "task_process_failed", "任务子进程异常退出", task_id=task_id, attempt_no=attempt, level="error", context={"exit_code": process.returncode})
            self.db.finish_task(task_id, "failed", {}, f"任务子进程异常退出，退出码 {process.returncode}", "worker_process_exit", attempt)
        return True

    def _shutdown_running_tasks(self) -> None:
        if not self.running_tasks:
            return
        deadline = time.monotonic() + self.shutdown_grace_seconds
        while self.running_tasks and time.monotonic() < deadline:
            self._supervise_running_tasks()
            if self.running_tasks:
                time.sleep(0.2)
        for running in list(self.running_tasks.values()):
            if running.process.poll() is None:
                runtime_log(self.db, "worker", "task_interrupted_by_shutdown", "Worker 关闭导致任务中断", task_id=running.task_id, attempt_no=running.attempt_no, level="error")
                self._terminate(running.process)
                if (self.db.get_task_status(running.task_id) or {}).get("status") == "running":
                    self.db.finish_task(running.task_id, "failed", {}, "worker 关闭导致任务中断；可手动重试", "service_shutdown", running.attempt_no)
        self.running_tasks.clear()

    def _reap_task_deletions(self) -> None:
        for task_id in self.db.reap_task_deletions(self._cutoff(self.stale_seconds)):
            self._remove_task_files(task_id)

    def _remove_task_files(self, task_id: str) -> None:
        storage_root = Path(self.config["storage"]["root_dir"]).resolve()
        task_dir = (storage_root / "tasks" / task_id).resolve()
        if storage_root in task_dir.parents and task_dir.exists():
            shutil.rmtree(task_dir)

    @staticmethod
    def _terminate(process: subprocess.Popen) -> None:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, check=False)
        else:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                return
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            if os.name == "nt":
                process.kill()
            else:
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)


def main() -> None:
    parser = argparse.ArgumentParser(description="Daily Report persisted duplicate-check worker")
    parser.add_argument("--config", default=None)
    args = parser.parse_args()
    config_path = args.config or str(default_config_path())
    worker = DuplicateWorker(config_path)
    signal.signal(signal.SIGINT, worker.request_stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, worker.request_stop)
    raise SystemExit(worker.run())


if __name__ == "__main__":
    main()

from __future__ import annotations

import argparse
import os
import signal
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from src.core.automation import DailyDuplicateScheduler
from src.db.factory import create_database
from src.utils.config import default_config_path, load_config
from src.utils.dates import now_iso
from src.utils.security import token
from src.utils.runtime_log import runtime_log

TASK_TIMEOUT_SECONDS = 60 * 60


class DuplicateWorker:
    def __init__(self, config_path: str) -> None:
        self.config_path = config_path
        self.config = load_config(config_path)
        self.db = create_database(self.config)
        settings = self.config.get("task_runner", {})
        self.timeout_seconds = TASK_TIMEOUT_SECONDS
        self.stale_seconds = max(30, int(settings.get("stale_heartbeat_seconds", 90)))
        self.shutdown_grace_seconds = max(5, int(settings.get("shutdown_grace_seconds", 30)))
        self.poll_seconds = max(0.5, float(settings.get("poll_interval_seconds", 2)))
        self.worker_id = f"worker-{os.getpid()}"
        self.lease_token = token("WL_", 24)
        self.stop_requested = False
        self.scheduler = DailyDuplicateScheduler(self.config, self.db, None)

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
        self._reap_task_deletions()
        self.db.fail_stale_tasks(self._cutoff(self.stale_seconds))
        try:
            while not self.stop_requested:
                self.db.heartbeat_worker(self.worker_id, self.lease_token)
                self._reap_task_deletions()
                self.scheduler.run_due()
                task = self.db.claim_next_task(self.worker_id, self.lease_token)
                if task:
                    self._run_claimed_task(task)
                else:
                    time.sleep(self.poll_seconds)
        except Exception as exc:
            runtime_log(self.db, "worker", "loop_failed", "后台 Worker 主循环异常退出", level="error", context={"worker_id": self.worker_id}, exc=exc)
            raise
        finally:
            self.db.release_worker_lease(self.worker_id, self.lease_token)
            runtime_log(self.db, "worker", "stopped", "后台 Worker 已释放执行租约", context={"worker_id": self.worker_id})
        return 0

    def _run_claimed_task(self, task: dict) -> None:
        task_id = str(task["task_id"])
        attempt = int(task.get("current_attempt") or 1)
        command = [sys.executable, "-m", "src.core.task_job", "--config", self.config_path, "--task-id", task_id, "--attempt", str(attempt)]
        popen_options = {"start_new_session": True} if os.name != "nt" else {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        process = subprocess.Popen(command, **popen_options)
        runtime_log(self.db, "worker", "task_process_started", "已启动查重任务子进程", task_id=task_id, attempt_no=attempt, context={"pid": process.pid, "worker_id": self.worker_id})
        started = time.monotonic()
        last_heartbeat = 0.0
        cancel_seen_at: float | None = None
        while process.poll() is None:
            elapsed = time.monotonic() - started
            if elapsed - last_heartbeat >= 10:
                self.db.heartbeat_worker(self.worker_id, self.lease_token)
                self.db.heartbeat_task(task_id, self.lease_token)
                last_heartbeat = elapsed
            control = self.db.task_control_request(task_id, attempt)
            if control == "delete":
                runtime_log(self.db, "worker", "task_delete_requested", "收到删除任务请求，正在终止子进程", task_id=task_id, attempt_no=attempt, level="warning")
                self._terminate(process)
                if self.db.complete_task_deletion(task_id, attempt):
                    self._remove_task_files(task_id)
                return
            if control == "terminate":
                runtime_log(self.db, "worker", "task_terminate_requested", "收到强制终止任务请求", task_id=task_id, attempt_no=attempt, level="warning")
                self._terminate(process)
                self.db.complete_force_termination(task_id, attempt)
                return
            if control == "cancel":
                cancel_seen_at = cancel_seen_at or time.monotonic()
                if time.monotonic() - cancel_seen_at >= self.shutdown_grace_seconds:
                    runtime_log(self.db, "worker", "task_cancelled", "任务取消等待超时，正在终止子进程", task_id=task_id, attempt_no=attempt, level="warning")
                    self._terminate(process)
                    if (self.db.get_task_status(task_id) or {}).get("status") == "running":
                        self.db.finish_task(task_id, "cancelled", {}, "任务已取消", "cancelled", attempt)
                    return
            if elapsed >= self.timeout_seconds:
                runtime_log(self.db, "worker", "task_timeout", "任务达到最大运行时限", task_id=task_id, attempt_no=attempt, level="error", context={"timeout_seconds": self.timeout_seconds})
                self._terminate(process)
                self.db.finish_task(task_id, "failed", {}, f"任务超过 {self.timeout_seconds // 60} 分钟时限", "timeout", attempt)
                return
            if self.stop_requested:
                deadline = time.monotonic() + self.shutdown_grace_seconds
                while process.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.2)
                if process.poll() is None:
                    runtime_log(self.db, "worker", "task_interrupted_by_shutdown", "Worker 关闭导致任务中断", task_id=task_id, attempt_no=attempt, level="error")
                    self._terminate(process)
                    self.db.finish_task(task_id, "failed", {}, "worker 关闭导致任务中断；可手动重试", "service_shutdown", attempt)
                return
            self.scheduler.run_due()
            time.sleep(0.5)
        current = self.db.get_task_status(task_id) or {}
        if current.get("status") != "running":
            return
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

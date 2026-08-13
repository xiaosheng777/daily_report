from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta

from src.core.automation import DailyDuplicateScheduler
from src.db.factory import create_database
from src.utils.config import default_config_path, load_config
from src.utils.dates import now_iso
from src.utils.security import token


class DuplicateWorker:
    def __init__(self, config_path: str) -> None:
        self.config_path = config_path
        self.config = load_config(config_path)
        self.db = create_database(self.config)
        settings = self.config.get("task_runner", {})
        self.timeout_seconds = max(60, int(settings.get("task_timeout_seconds", 3600)))
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

    def run(self) -> int:
        if not self.db.acquire_worker_lease(self.worker_id, self.lease_token, self._cutoff(self.stale_seconds)):
            print("duplicate worker is already active")
            return 2
        self.db.fail_stale_tasks(self._cutoff(self.stale_seconds))
        try:
            while not self.stop_requested:
                self.db.heartbeat_worker(self.worker_id, self.lease_token)
                self.scheduler.run_due()
                task = self.db.claim_next_task(self.worker_id, self.lease_token)
                if task:
                    self._run_claimed_task(task)
                else:
                    time.sleep(self.poll_seconds)
        finally:
            self.db.release_worker_lease(self.worker_id, self.lease_token)
        return 0

    def _run_claimed_task(self, task: dict) -> None:
        task_id = str(task["task_id"])
        attempt = int(task.get("current_attempt") or 1)
        command = [sys.executable, "-m", "src.core.task_job", "--config", self.config_path, "--task-id", task_id, "--attempt", str(attempt)]
        popen_options = {"start_new_session": True} if os.name != "nt" else {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        process = subprocess.Popen(command, **popen_options)
        started = time.monotonic()
        last_heartbeat = 0.0
        cancel_seen_at: float | None = None
        while process.poll() is None:
            elapsed = time.monotonic() - started
            if elapsed - last_heartbeat >= 10:
                self.db.heartbeat_worker(self.worker_id, self.lease_token)
                self.db.heartbeat_task(task_id, self.lease_token)
                last_heartbeat = elapsed
            if self.db.task_cancel_requested(task_id, attempt):
                cancel_seen_at = cancel_seen_at or time.monotonic()
                if time.monotonic() - cancel_seen_at >= self.shutdown_grace_seconds:
                    self._terminate(process)
                    if (self.db.get_task_status(task_id) or {}).get("status") == "running":
                        self.db.finish_task(task_id, "cancelled", {}, "任务已取消", "cancelled")
                    return
            if elapsed >= self.timeout_seconds:
                self._terminate(process)
                self.db.finish_task(task_id, "failed", {}, f"任务超过 {self.timeout_seconds // 60} 分钟时限", "timeout")
                return
            if self.stop_requested:
                deadline = time.monotonic() + self.shutdown_grace_seconds
                while process.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.2)
                if process.poll() is None:
                    self._terminate(process)
                    self.db.finish_task(task_id, "failed", {}, "worker 关闭导致任务中断；可手动重试", "service_shutdown")
                return
            self.scheduler.run_due()
            time.sleep(0.5)
        if process.returncode and (self.db.get_task_status(task_id) or {}).get("status") == "running":
            self.db.finish_task(task_id, "failed", {}, f"任务子进程异常退出，退出码 {process.returncode}", "worker_process_exit")

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

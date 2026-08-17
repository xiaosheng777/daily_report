from __future__ import annotations

import copy
import threading
from datetime import datetime, timedelta
from typing import Any

from src.utils.dates import SHANGHAI_TIMEZONE
from src.utils.runtime_log import runtime_log


class DailyDuplicateScheduler:
    """Enqueue the company-wide duplicate check once per Shanghai calendar day."""

    def __init__(self, config: dict[str, Any], db, storage) -> None:
        self.config, self.db, self.storage = config, db, storage
        self._stop = threading.Event()
        self._run_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._logged_report_dates: set[str] = set()

    def start(self) -> None:
        if not self._settings(self._effective_config()).get("enabled", True) or self._thread:
            return
        self._thread = threading.Thread(target=self._loop, name="daily-duplicate-scheduler", daemon=True)
        self._thread.start()
        runtime_log(self.db, "scheduler", "started", "自动查重调度器已启动")

    def stop(self) -> None:
        self._stop.set()
        runtime_log(self.db, "scheduler", "stopping", "自动查重调度器正在停止")

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_due()
            except Exception as exc:  # keep the API server alive if scheduling fails unexpectedly
                runtime_log(self.db, "scheduler", "run_due_failed", "自动查重调度执行失败", level="error", exc=exc)
            self._stop.wait(30)

    def _effective_config(self) -> dict[str, Any]:
        """Apply persisted administrator settings before every scheduling decision."""
        config = copy.deepcopy(self.config)
        get_settings = getattr(self.db, "get_settings", None)
        if not callable(get_settings):
            return config
        try:
            settings = get_settings({}) or {}
        except Exception:
            return config
        for key, value in settings.items():
            if not isinstance(key, str) or "." not in key:
                continue
            target = config
            parts = key.split(".")
            for part in parts[:-1]:
                target = target.setdefault(part, {})
            target[parts[-1]] = value
        return config

    @staticmethod
    def _settings(config: dict[str, Any]) -> dict[str, Any]:
        return config.get("automatic_daily_duplicate", {})

    def _now(self) -> datetime:
        return datetime.now(SHANGHAI_TIMEZONE)

    def run_due(self, now: datetime | None = None) -> tuple[str, bool] | None:
        config = self._effective_config()
        settings = self._settings(config)
        if not settings.get("enabled", True):
            return None
        current = now or self._now()
        hour, minute = (int(part) for part in str(settings.get("run_at", "12:00")).split(":", 1))
        due_at = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if current < due_at:
            return None
        if current > due_at and not settings.get("catch_up_on_start", True):
            return None
        report_date = (current.date() - timedelta(days=1)).isoformat()
        with self._run_lock:
            task_id, created = self._run(report_date, config)
        if created or report_date not in self._logged_report_dates:
            runtime_log(self.db, "scheduler", "task_enqueued" if created else "task_skipped", "自动查重任务已入队" if created else "当天自动查重任务已存在，跳过重复创建", task_id=task_id, context={"report_date": report_date, "run_at": str(settings.get("run_at") or "")})
            self._logged_report_dates.add(report_date)
        return task_id, created

    def _run(self, report_date: str, config: dict[str, Any]) -> tuple[str, bool]:
        payload = {"report_date_start": report_date, "report_date_end": report_date, "trigger_source": "system_daily"}
        return self.db.begin_automatic_task(
            report_date,
            payload,
            config,
            f"tasks/pending_system_{report_date}",
        )

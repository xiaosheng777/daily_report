from __future__ import annotations

import threading
from datetime import datetime, timedelta
from typing import Any

from src.utils.dates import SHANGHAI_TIMEZONE


class DailyDuplicateScheduler:
    """Enqueue the company-wide duplicate check once per Shanghai calendar day."""

    def __init__(self, config: dict[str, Any], db, storage) -> None:
        self.config, self.db, self.storage = config, db, storage
        self._stop = threading.Event()
        self._run_lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if not self._settings().get("enabled", True) or self._thread:
            return
        self._thread = threading.Thread(target=self._loop, name="daily-duplicate-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_due()
            except Exception as exc:  # keep the API server alive if scheduling fails unexpectedly
                print(f"daily duplicate scheduler error: {exc}")
            self._stop.wait(30)

    def _settings(self) -> dict[str, Any]:
        return self.config.get("automatic_daily_duplicate", {})

    def _now(self) -> datetime:
        return datetime.now(SHANGHAI_TIMEZONE)

    def run_due(self, now: datetime | None = None) -> tuple[str, bool] | None:
        settings = self._settings()
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
            return self._run(report_date)

    def _run(self, report_date: str) -> tuple[str, bool]:
        payload = {"report_date_start": report_date, "report_date_end": report_date, "trigger_source": "system_daily"}
        return self.db.begin_automatic_task(
            report_date,
            payload,
            self.config,
            f"tasks/pending_system_{report_date}",
        )

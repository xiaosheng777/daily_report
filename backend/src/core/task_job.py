from __future__ import annotations

import argparse
import json
import queue
import threading
import time
from pathlib import Path

from src.core.pipeline import DailyReportPipeline, TaskCancelled, TaskPaused, TaskTerminated
from src.db.factory import open_database
from src.llm.factory import create_llm_judge
from src.storage.file_store import FileStorage
from src.utils.config import load_config
from src.utils.runtime_log import runtime_log


class AsyncLLMTelemetry:
    """Persist only HTTP telemetry off the LLM execution threads.

    Permit state is persisted by the Worker supervisor.  This queue deliberately
    drops detail under overload rather than making an acquired LLM permit wait
    for SQLite.
    """

    def __init__(self, db, task_id: str, attempt_no: int, max_events: int = 4096) -> None:
        self.db = db
        self.task_id = task_id
        self.attempt_no = attempt_no
        self.events: queue.Queue[tuple[str, dict] | None] = queue.Queue(maxsize=max_events)
        self.dropped = 0
        self._closed = False
        self._thread = threading.Thread(target=self._run, name="daily-report-llm-telemetry", daemon=True)
        self._thread.start()

    def __call__(self, event: str, payload: dict) -> None:
        if self._closed or event not in {"http_start", "http_finish"}:
            return
        try:
            self.events.put_nowait((event, dict(payload)))
        except queue.Full:
            self.dropped += 1

    def _run(self) -> None:
        while True:
            item = self.events.get()
            try:
                if item is None:
                    return
                event, payload = item
                if event == "http_start":
                    self.db.start_llm_call(str(payload["call_id"]), self.task_id, str(payload["started_at"]), int(payload.get("permit_wait_ms") or 0))
                    runtime_log(self.db, "llm", "http_started", "开始发送大模型 HTTP 请求", task_id=self.task_id, attempt_no=self.attempt_no, context={"call_id": str(payload["call_id"]), "permit_wait_ms": int(payload.get("permit_wait_ms") or 0)})
                else:
                    self.db.finish_llm_call(str(payload["call_id"]), str(payload["finished_at"]), int(payload.get("http_duration_ms") or 0), bool(payload["ok"]), str(payload.get("error_type") or ""), int(payload.get("total_duration_ms") or 0))
                    ok = bool(payload["ok"])
                    runtime_log(self.db, "llm", "http_finished" if ok else "http_failed", "大模型 HTTP 请求完成" if ok else "大模型 HTTP 请求失败", level="info" if ok else "error", task_id=self.task_id, attempt_no=self.attempt_no, context={"call_id": str(payload["call_id"]), "http_duration_ms": int(payload.get("http_duration_ms") or 0), "permit_wait_ms": int(payload.get("permit_wait_ms") or 0), "total_duration_ms": int(payload.get("total_duration_ms") or 0), "error_type": str(payload.get("error_type") or "")}, traceback_text=str(payload.get("traceback") or ""))
            finally:
                self.events.task_done()

    def close(self) -> None:
        self._closed = True
        try:
            self.events.put(None, timeout=0.1)
        except queue.Full:
            return
        self._thread.join(timeout=2)


def run_task(config_path: str, task_id: str, attempt_no: int) -> int:
    base_config = load_config(config_path)
    db = open_database(base_config)
    task = db.get_task(task_id)
    if not task or task.get("status") != "running" or int(task.get("current_attempt") or 1) != attempt_no:
        runtime_log(db, "task", "run_skipped", "任务状态不满足执行条件，跳过本次子进程", level="warning", task_id=task_id, attempt_no=attempt_no)
        return 2
    config = task.get("config_snapshot") or base_config
    storage = FileStorage(config)
    user = db.get_user_by_id(str(task.get("created_by") or "")) if task.get("trigger_source") != "system_daily" else None
    result_dir = Path(config["storage"]["root_dir"]) / "tasks" / task_id / f"attempt_{attempt_no}"

    previous_stage = ""
    stage_started_at = time.monotonic()

    def progress(stage: str, current: int, total: int, message: str = "") -> None:
        nonlocal previous_stage, stage_started_at
        db.update_task_progress(task_id, attempt_no, stage, current, total, message)
        if stage != previous_stage:
            if previous_stage:
                runtime_log(db, "task", "stage_completed", f"任务完成 {previous_stage} 阶段", task_id=task_id, attempt_no=attempt_no, context={"stage": previous_stage, "duration_ms": round((time.monotonic() - stage_started_at) * 1000)})
            previous_stage = stage
            stage_started_at = time.monotonic()
            runtime_log(db, "task", "stage_changed", message or f"任务进入 {stage} 阶段", task_id=task_id, attempt_no=attempt_no, context={"stage": stage, "current": current, "total": total})

    def control() -> str:
        return db.task_control_request(task_id, attempt_no)

    def save_case(case: dict) -> None:
        db.save_task_case(task_id, attempt_no, case)

    llm_telemetry = AsyncLLMTelemetry(db, task_id, attempt_no)

    try:
        runtime_log(db, "task", "started", "查重任务开始执行", task_id=task_id, attempt_no=attempt_no, context={"trigger_source": str(task.get("trigger_source") or "")})
        pipeline = DailyReportPipeline(config, db, storage, create_llm_judge(config, telemetry=llm_telemetry, task_id=task_id))
        result = pipeline.run(
            user,
            task.get("payload") or {},
            task_id,
            str(result_dir),
            progress_callback=progress,
            case_callback=save_case,
            control_callback=control,
            resume_cases=list((task.get("result") or {}).get("report_cases") or []),
        )
        db.save_task_missing_results(task_id, attempt_no, result.get("missing_reports", []))
        action = control()
        if action == "pause":
            db.complete_task_pause(task_id, attempt_no)
            runtime_log(db, "task", "paused", "查重任务已暂停", task_id=task_id, attempt_no=attempt_no, level="warning")
            return 4
        if action == "terminate":
            runtime_log(db, "task", "terminated", "查重任务已被强制终止", task_id=task_id, attempt_no=attempt_no, level="warning")
            return 5
        if action == "cancel":
            db.finish_task(task_id, "cancelled", {}, "任务已取消", "cancelled", attempt_no)
            runtime_log(db, "task", "cancelled", "查重任务已取消", task_id=task_id, attempt_no=attempt_no, level="warning")
            return 3
        if not db.finish_task(task_id, "finished", result, attempt_no=attempt_no):
            runtime_log(db, "task", "finish_rejected", "任务完成状态写入未生效", task_id=task_id, attempt_no=attempt_no, level="warning")
            return 2
        runtime_log(db, "task", "finished", "查重任务执行完成", task_id=task_id, attempt_no=attempt_no, context={"report_count": len(result.get("report_cases") or [])})
        return 0
    except TaskPaused:
        db.complete_task_pause(task_id, attempt_no)
        runtime_log(db, "task", "paused", "查重任务已在安全点暂停", task_id=task_id, attempt_no=attempt_no, level="warning")
        return 4
    except TaskTerminated:
        runtime_log(db, "task", "terminated", "查重任务已被强制终止", task_id=task_id, attempt_no=attempt_no, level="warning")
        return 5
    except TaskCancelled as exc:
        db.finish_task(task_id, "cancelled", {}, str(exc), "cancelled", attempt_no)
        runtime_log(db, "task", "cancelled", "查重任务已取消", task_id=task_id, attempt_no=attempt_no, level="warning", exc=exc)
        return 3
    except Exception as exc:
        result_dir.mkdir(parents=True, exist_ok=True)
        (result_dir / "errors.json").write_text(json.dumps({"error": str(exc)}, ensure_ascii=False, indent=2), encoding="utf-8")
        db.finish_task(task_id, "failed", {}, str(exc), "execution_error", attempt_no)
        runtime_log(db, "task", "failed", "查重任务执行失败", task_id=task_id, attempt_no=attempt_no, level="error", exc=exc)
        return 1
    finally:
        llm_telemetry.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Execute one persisted duplicate-check task")
    parser.add_argument("--config", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--attempt", required=True, type=int)
    args = parser.parse_args()
    raise SystemExit(run_task(args.config, args.task_id, args.attempt))


if __name__ == "__main__":
    main()

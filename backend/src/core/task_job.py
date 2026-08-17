from __future__ import annotations

import argparse
import json
from pathlib import Path

from src.core.pipeline import DailyReportPipeline, TaskCancelled, TaskPaused, TaskTerminated
from src.db.factory import create_database
from src.llm.factory import create_llm_judge
from src.storage.file_store import FileStorage
from src.utils.config import load_config
from src.utils.runtime_log import runtime_log


def run_task(config_path: str, task_id: str, attempt_no: int) -> int:
    base_config = load_config(config_path)
    db = create_database(base_config)
    task = db.get_task(task_id)
    if not task or task.get("status") != "running" or int(task.get("current_attempt") or 1) != attempt_no:
        runtime_log(db, "task", "run_skipped", "任务状态不满足执行条件，跳过本次子进程", level="warning", task_id=task_id, attempt_no=attempt_no)
        return 2
    config = task.get("config_snapshot") or base_config
    storage = FileStorage(config)
    user = db.get_user_by_id(str(task.get("created_by") or "")) if task.get("trigger_source") != "system_daily" else None
    result_dir = Path(config["storage"]["root_dir"]) / "tasks" / task_id / f"attempt_{attempt_no}"

    previous_stage = ""

    def progress(stage: str, current: int, total: int, message: str = "") -> None:
        nonlocal previous_stage
        db.update_task_progress(task_id, attempt_no, stage, current, total, message)
        if stage != previous_stage:
            previous_stage = stage
            runtime_log(db, "task", "stage_changed", message or f"任务进入 {stage} 阶段", task_id=task_id, attempt_no=attempt_no, context={"stage": stage, "current": current, "total": total})

    def control() -> str:
        return db.task_control_request(task_id, attempt_no)

    def save_case(case: dict) -> None:
        db.save_task_case(task_id, attempt_no, case)

    def llm_telemetry(event: str, payload: dict) -> None:
        if event == "start":
            db.start_llm_call(str(payload["call_id"]), task_id, str(payload["started_at"]))
            runtime_log(db, "llm", "call_started", "开始调用大模型", task_id=task_id, attempt_no=attempt_no, context={"call_id": str(payload["call_id"])})
        elif event == "finish":
            db.finish_llm_call(str(payload["call_id"]), str(payload["finished_at"]), int(payload["duration_ms"]), bool(payload["ok"]), str(payload.get("error_type") or ""))
            ok = bool(payload["ok"])
            runtime_log(db, "llm", "call_finished" if ok else "call_failed", "大模型调用完成" if ok else "大模型调用失败", level="info" if ok else "error", task_id=task_id, attempt_no=attempt_no, context={"call_id": str(payload["call_id"]), "duration_ms": int(payload["duration_ms"]), "error_type": str(payload.get("error_type") or "")}, traceback_text=str(payload.get("traceback") or ""))

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


def main() -> None:
    parser = argparse.ArgumentParser(description="Execute one persisted duplicate-check task")
    parser.add_argument("--config", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--attempt", required=True, type=int)
    args = parser.parse_args()
    raise SystemExit(run_task(args.config, args.task_id, args.attempt))


if __name__ == "__main__":
    main()

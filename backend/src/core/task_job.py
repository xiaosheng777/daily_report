from __future__ import annotations

import argparse
import json
from pathlib import Path

from src.core.pipeline import DailyReportPipeline, TaskCancelled
from src.db.factory import create_database
from src.llm.factory import create_llm_judge
from src.storage.file_store import FileStorage
from src.utils.config import load_config


def run_task(config_path: str, task_id: str, attempt_no: int) -> int:
    base_config = load_config(config_path)
    db = create_database(base_config)
    task = db.get_task(task_id)
    if not task or task.get("status") != "running" or int(task.get("current_attempt") or 1) != attempt_no:
        return 2
    config = task.get("config_snapshot") or base_config
    storage = FileStorage(config)
    user = db.get_user_by_id(str(task.get("created_by") or "")) if task.get("trigger_source") != "system_daily" else None
    result_dir = Path(config["storage"]["root_dir"]) / "tasks" / task_id / f"attempt_{attempt_no}"

    def progress(stage: str, current: int, total: int, message: str = "") -> None:
        if db.task_cancel_requested(task_id, attempt_no):
            raise TaskCancelled("任务已取消")
        db.update_task_progress(task_id, attempt_no, stage, current, total, message)

    def save_case(case: dict) -> None:
        db.save_task_case(task_id, attempt_no, case)

    try:
        pipeline = DailyReportPipeline(config, db, storage, create_llm_judge(config))
        result = pipeline.run(
            user,
            task.get("payload") or {},
            task_id,
            str(result_dir),
            progress_callback=progress,
            case_callback=save_case,
        )
        db.save_task_missing_results(task_id, attempt_no, result.get("missing_reports", []))
        db.finish_task(task_id, "finished", result)
        return 0
    except TaskCancelled as exc:
        db.finish_task(task_id, "cancelled", {}, str(exc), "cancelled")
        return 3
    except Exception as exc:
        result_dir.mkdir(parents=True, exist_ok=True)
        (result_dir / "errors.json").write_text(json.dumps({"error": str(exc)}, ensure_ascii=False, indent=2), encoding="utf-8")
        db.finish_task(task_id, "failed", {}, str(exc), "execution_error")
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

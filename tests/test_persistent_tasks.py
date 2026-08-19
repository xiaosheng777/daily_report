import sqlite3

import pytest
import yaml

from src.core.task_job import run_task
from src.core.pipeline import DailyReportPipeline, TaskPaused
from src.core.schemas import CheckResult, Report, ReviewCase
from src.db.factory import create_database
from src.db.sqlite_backend import ROLE_ADMIN
from src.utils.config import load_config

from test_organization import as_user, build_org


def test_automatic_task_has_priority_over_manual_queue(tmp_path):
    db = build_org(tmp_path)
    leader = as_user(db, "leader1")
    manual = db.create_task(leader, {"report_date_start": "2026-08-11", "report_date_end": "2026-08-11"}, {}, "pending")
    automatic, created = db.begin_automatic_task("2026-08-11", {"report_date_start": "2026-08-11", "report_date_end": "2026-08-11"}, {}, "pending-system")

    assert created is True
    first = db.claim_next_task("worker-1", "lease-1")
    assert first["task_id"] == automatic
    db.finish_task(automatic, "finished", {"summary": {}})
    second = db.claim_next_task("worker-1", "lease-1")
    assert second["task_id"] == manual


def test_stale_running_task_fails_and_keeps_partial_results(tmp_path):
    db = build_org(tmp_path)
    leader = as_user(db, "leader1")
    task_id = db.create_task(leader, {"report_date_start": "2026-08-11", "report_date_end": "2026-08-11"}, {}, "pending")
    claimed = db.claim_next_task("worker-1", "lease-1")
    attempt = claimed["current_attempt"]
    db.save_task_case(task_id, attempt, {
        "report_id": "r1", "owner_user_id": leader.user_id, "department_id": leader.department_id,
        "group_id": leader.group_id, "overall_risk": "high",
    })
    db.update_task_progress(task_id, attempt, "checking", 1, 2, "完成一半")

    assert db.fail_stale_tasks("9999-12-31T23:59:59") == 1
    task = db.get_task(task_id, leader)
    assert task["status"] == "failed"
    assert task["failure_type"] == "interrupted"
    assert task["partial_available"] == 1
    assert [case["report_id"] for case in task["result"]["report_cases"]] == ["r1"]


def test_scoped_task_summary_counts_llm_reviews_and_local_degradation(tmp_path):
    db = build_org(tmp_path)
    leader = as_user(db, "leader1")
    task_id = db.create_task(leader, {}, {}, "pending")
    claimed = db.claim_next_task("worker-1", "lease-1")
    db.save_task_case(task_id, claimed["current_attempt"], {
        "report_id": "r1",
        "owner_user_id": leader.user_id,
        "department_id": leader.department_id,
        "group_id": leader.group_id,
        "overall_risk": "medium",
        "checks": [{
            "checker_id": "report_duplicate_check",
            "findings": [
                {"details": {"llm_reviewed": True}},
                {"details": {"degraded_to_local": True}},
            ],
        }],
    })

    summary = db.get_task_summary_for_user(task_id, claimed["current_attempt"], leader)

    assert summary["local_duplicate_checked_count"] == 1
    assert summary["local_duplicate_total_count"] == 1
    assert summary["llm_review_count"] == 1
    assert summary["degraded_local_count"] == 1


def test_retry_is_atomic_and_manual_user_has_one_active_task(tmp_path):
    db = build_org(tmp_path)
    leader = as_user(db, "leader1")
    task_id = db.create_task(leader, {"report_date_start": "2026-08-11", "report_date_end": "2026-08-11"}, {}, "pending")
    try:
        db.create_task(leader, {"report_date_start": "2026-08-11", "report_date_end": "2026-08-11"}, {}, "pending")
        raise AssertionError("second active task should be rejected")
    except ValueError as exc:
        assert str(exc).startswith("active_task_exists:")

    db.finish_task(task_id, "failed", {}, "failed", "execution_error")
    assert db.retry_task(task_id, leader) is True
    assert db.retry_task(task_id, leader) is False
    assert db.get_task(task_id)["current_attempt"] == 2


def test_all_managers_can_retry_system_task_but_only_admin_can_be_selected_for_cancel_policy(tmp_path):
    db = build_org(tmp_path)
    db.create_user({"user_id": "admin-retry", "username": "admin-retry", "display_name": "管理员", "role": ROLE_ADMIN})
    task_id, _ = db.begin_automatic_task("2026-08-11", {}, {}, "pending-system")
    db.finish_task(task_id, "failed", {}, "failed", "execution_error")

    assert db.retry_task(task_id, as_user(db, "leader1")) is True
    assert db.cancel_task(task_id, as_user(db, "admin-retry")) == "cancelled"


def test_persisted_task_job_runs_claimed_task_to_completion(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({
        "app": {"seed_demo_users": True, "frontend_dir": str(tmp_path)},
        "database": {"backend": "sqlite", "sqlite_path": str(tmp_path / "daily.sqlite3")},
        "storage": {"root_dir": str(tmp_path / "storage")},
        "automatic_daily_duplicate": {"enabled": False},
        "llm_judge": {"enabled": False},
        "checks": {"enable_code_check": False, "enable_document_check": False},
        "testcase_verification": {"enabled": False},
    }, allow_unicode=True), encoding="utf-8")
    config = load_config(config_path)
    db = create_database(config)
    admin = db.get_user_by_id("u_admin")
    task_id = db.create_task(admin, {"report_date_start": "2026-08-11", "report_date_end": "2026-08-11"}, config, "pending")
    claimed = db.claim_next_task("worker-1", "lease-1")

    assert claimed["task_id"] == task_id
    assert run_task(str(config_path), task_id, 1) == 0
    finished = db.get_task(task_id, admin)
    assert finished["status"] == "finished"
    assert finished["progress_stage"] == "finished"
    assert finished["summary"]["report_count"] == 0
    assert (tmp_path / "storage" / "tasks" / task_id / "attempt_1" / "review_cases.json").exists()


def test_legacy_running_task_is_migrated_and_marked_interrupted(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript("""
            create table check_tasks (
              task_id text primary key, created_by text not null, department_id text default '',
              task_name text default '', task_date text default '', trigger_source text not null default 'manual',
              report_date text default '', attempt_count integer not null default 0, payload_json text not null,
              config_snapshot_json text default '{}', result_json text default '{}', result_dir text default '',
              status text not null, error_message text default '', created_at text not null, finished_at text default ''
            );
            insert into check_tasks(task_id,created_by,payload_json,status,created_at)
            values ('T_legacy','u1','{}','running','2026-08-01T12:00:00');
        """)
    from src.db.sqlite_backend import SQLiteBackend
    db = SQLiteBackend(path)
    db.initialize(seed_demo_users=False)

    assert db.fail_stale_tasks("2026-08-13T00:00:00") == 1
    task = db.get_task("T_legacy")
    assert task["status"] == "failed"
    assert task["failure_type"] == "interrupted"


def test_admin_console_lists_all_creators_and_queued_pause_resume_is_atomic(tmp_path):
    db = build_org(tmp_path)
    leader, director = as_user(db, "leader1"), as_user(db, "director")
    leader_task = db.create_task(leader, {}, {}, "leader")
    director_task = db.create_task(director, {}, {}, "director")

    console = db.list_admin_tasks({"view": "active"})

    assert {task["task_id"] for task in console["tasks"]} == {leader_task, director_task}
    assert {task["creator_username"] for task in console["tasks"]} == {"leader1", "director"}
    assert db.admin_pause_task(leader_task) == "paused"
    paused = db.get_task_status(leader_task)
    assert paused["status"] == paused["effective_status"] == "paused"
    with pytest.raises(ValueError, match="active_task_exists"):
        db.create_task(leader, {}, {}, "another")
    assert db.admin_resume_task(leader_task) == "queued"
    resumed = db.get_task(leader_task)
    assert resumed["current_attempt"] == 1
    assert resumed["resume_from_checkpoint"] == 1


def test_running_pause_keeps_checkpoint_and_force_termination_blocks_stale_finish(tmp_path):
    db = build_org(tmp_path)
    leader = as_user(db, "leader1")
    task_id = db.create_task(leader, {}, {}, "checkpoint")
    claimed = db.claim_next_task("worker-1", "lease-1")
    attempt = claimed["current_attempt"]
    db.save_task_case(task_id, attempt, {
        "report_id": "r1", "owner_user_id": leader.user_id, "department_id": leader.department_id,
        "group_id": leader.group_id, "overall_risk": "normal", "triggered_checks": [], "checks": [],
    })
    db.update_task_progress(task_id, attempt, "checking", 1, 2, "完成一半")

    assert db.admin_pause_task(task_id) == "pausing"
    assert db.get_task_status(task_id)["effective_status"] == "pausing"
    assert db.complete_task_pause(task_id, attempt) is True
    assert db.admin_resume_task(task_id) == "queued"
    resumed = db.claim_next_task("worker-1", "lease-1")
    assert resumed["current_attempt"] == attempt
    assert resumed["progress_current"] == 1
    assert [case["report_id"] for case in db.get_task(task_id)["result"]["report_cases"]] == ["r1"]

    assert db.admin_force_terminate_task(task_id) == "terminating"
    assert db.complete_force_termination(task_id, attempt) is True
    assert db.finish_task(task_id, "finished", {"summary": {}}, attempt_no=attempt) is False
    terminated = db.get_task(task_id)
    assert terminated["status"] == "terminated"
    assert terminated["partial_available"] == 1
    assert db.admin_restart_task(task_id) == "queued"
    restarted = db.get_task(task_id)
    assert restarted["current_attempt"] == attempt + 1
    assert restarted["progress_current"] == 0
    assert restarted["partial_available"] == 0


def test_creator_can_delete_queued_task_and_stop_then_delete_running_task(tmp_path):
    db = build_org(tmp_path)
    leader, director = as_user(db, "leader1"), as_user(db, "director")

    queued_id = db.create_task(leader, {}, {}, "queued-delete")
    assert db.delete_task(queued_id, leader) == "deleted"
    assert db.get_task(queued_id) is None

    running_id = db.create_task(leader, {}, {}, "running-delete")
    claimed = db.claim_next_task("worker-1", "lease-1")
    attempt = int(claimed["current_attempt"])
    assert db.delete_task(running_id, leader) == "deleting"
    deleting = db.get_task_status(running_id)
    assert deleting["effective_status"] == "deleting"
    assert db.task_control_request(running_id, attempt) == "delete"
    assert all(task["task_id"] != running_id for task in db.list_tasks(leader))
    replacement_id = db.create_task(leader, {}, {}, "replacement")
    assert db.get_task(replacement_id)["status"] == "queued"
    assert db.finish_task(running_id, "finished", {"summary": {}}, attempt_no=attempt) is False
    assert db.complete_task_deletion(running_id, attempt) is True
    assert db.get_task(running_id) is None

    with pytest.raises(ValueError, match="forbidden"):
        db.delete_task(replacement_id, director)


def test_resumable_pipeline_skips_saved_reports_and_pauses_after_continuous_queue():
    current = [Report(f"r{i}", f"u{i}", f"员工{i}", "d1", "研发部", "2026-08-11", "组长", f"任务{i}", "完整工作描述") for i in range(5)]
    pipeline = DailyReportPipeline({"daily_duplicate": {"report_worker_count": 2}}, object(), None)
    saved = []
    controls = iter(["", "pause"])

    dispatched = []

    def fake_build(queue, _all, _progress, callback, _context=None):
        dispatched.append([report.report_id for report in queue])
        cases = [ReviewCase(report.report_id, report.employee_name, report.owner_user_id, report.department_id,
                            report.department_name, report.group_id, report.group_name, report.report_date,
                            report.created_at, report.title_summary, "normal", [],
                            [CheckResult("quality_check", False, "success", "normal", "ok")]) for report in queue]
        for case in cases:
            callback(case.to_dict())
        return cases

    pipeline._build_cases = fake_build
    completed = {"r0": ReviewCase("r0", "员工0", "u0", "d1", "研发部", "", "", "2026-08-11", "", "任务0", "normal", [], []).to_dict()}
    with pytest.raises(TaskPaused):
        pipeline._build_cases_resumable(current, current, completed, lambda *_args: None, lambda case: saved.append(case["report_id"]), lambda: next(controls))

    assert dispatched == [["r1", "r2", "r3", "r4"]]
    assert saved == ["r1", "r2", "r3", "r4"]
    assert set(completed) == {"r0", "r1", "r2", "r3", "r4"}

import sqlite3

import yaml

from src.core.task_job import run_task
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

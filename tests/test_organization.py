from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from src.core.pipeline import DailyReportPipeline
from src.core.schemas import Report
from src.db.sqlite_backend import SQLiteBackend
from src.llm.openai_compatible import OpenAICompatibleJudge
from src.utils.dates import now_iso


def build_org(tmp_path: Path) -> SQLiteBackend:
    db = SQLiteBackend(tmp_path / "org.sqlite3")
    db.initialize(False)
    db.create_department({"department_id": "d1", "name": "研发部"})
    db.create_department({"department_id": "d2", "name": "测试部"})
    db.create_group({"group_id": "g1", "department_id": "d1", "name": "一组"})
    db.create_group({"group_id": "g2", "department_id": "d1", "name": "二组"})
    db.create_user({"user_id": "m", "username": "minister", "display_name": "部长", "role": "minister"})
    db.create_user({"user_id": "d", "username": "director", "display_name": "主任", "role": "director", "department_id": "d1"})
    db.create_user({"user_id": "l1", "username": "leader1", "display_name": "一组长", "role": "group_leader", "department_id": "d1", "group_id": "g1"})
    db.create_user({"user_id": "l2", "username": "leader2", "display_name": "二组长", "role": "group_leader", "department_id": "d1", "group_id": "g2"})
    db.create_user({"user_id": "e1", "username": "employee1", "display_name": "员工甲", "role": "employee", "department_id": "d1", "group_id": "g1"})
    db.create_user({"user_id": "e2", "username": "employee2", "display_name": "员工乙", "role": "employee", "department_id": "d1", "group_id": "g2"})
    return db


def user(db: SQLiteBackend, username: str):
    return next(item for item in db.list_users() if item["username"] == username)


def as_user(db: SQLiteBackend, username: str):
    item = user(db, username)
    with db.connect() as conn:
        row = conn.execute(db._user_select() + " where u.user_id=?", (item["user_id"],)).fetchone()
    return db._row_to_user(row)


def save_report(db: SQLiteBackend, owner, report_id: str) -> None:
    db.save_report(Report(report_id, owner.user_id, owner.display_name, owner.department_id,
                          owner.department_name, "2026-07-10", owner.manager_display_name,
                          "今日工作", "完成今日工作内容", created_at=now_iso(),
                          group_id=owner.group_id, group_name=owner.group_name,
                          submitter_role=owner.role))


def test_session_expiry_uses_the_same_shanghai_clock(tmp_path, monkeypatch):
    db = SQLiteBackend(tmp_path / "session.sqlite3")
    db.initialize(False)
    clock = datetime(2026, 8, 10, 20, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    monkeypatch.setattr("src.db.sqlite_backend.shanghai_now", lambda: clock)
    monkeypatch.setattr("src.db.sqlite_backend.now_iso", lambda: "2026-08-10T20:00:00")

    session_id = db.create_session("user-1")
    with db.connect() as conn:
        expires_at = conn.execute("select expires_at from sessions where session_id=?", (session_id,)).fetchone()[0]

    assert expires_at == "2026-08-17T20:00:00"


def test_leader_uniqueness_and_derived_manager_chain(tmp_path):
    db = build_org(tmp_path)
    assert user(db, "employee1")["manager_display_name"] == "一组长"
    assert user(db, "leader1")["manager_display_name"] == "主任"
    assert user(db, "director")["manager_display_name"] == "部长"
    assert user(db, "minister")["manager_display_name"] == ""
    with pytest.raises(ValueError):
        db.create_user({"username": "duplicate", "role": "group_leader", "department_id": "d1", "group_id": "g1"})


def test_legacy_leader_roles_migrate_without_duplicate_seed_leaders(tmp_path):
    db = SQLiteBackend(tmp_path / "legacy.sqlite3")
    db.initialize(False)
    with db.connect() as conn:
        conn.execute("insert into departments(department_id,name,enabled) values ('dept_rd','研发部',1)")
        conn.execute("""insert into users(user_id,username,password_hash,display_name,department_id,group_id,role,enabled,created_at)
                      values ('old_global','global','x','旧全局领导','','','global_manager',1,?)""", (now_iso(),))
        conn.execute("""insert into users(user_id,username,password_hash,display_name,department_id,group_id,role,enabled,created_at)
                      values ('old_manager','manager','x','旧部门领导','dept_rd','','department_manager',1,?)""", (now_iso(),))
    db.initialize(True)
    users = db.list_users()
    assert sum(u["role"] == "minister" for u in users) == 1
    assert sum(u["role"] == "director" and u["department_id"] == "dept_rd" for u in users) == 1


def test_report_visibility_follows_organisation_tree(tmp_path):
    db = build_org(tmp_path)
    people = [as_user(db, name) for name in ("employee1", "employee2", "leader1", "director", "minister")]
    for index, owner in enumerate(people):
        save_report(db, owner, f"r{index}")
    employee, _, leader, director, minister = people
    assert {r.owner_user_id for r in db.list_reports(employee)} == {employee.user_id}
    assert {r.owner_user_id for r in db.list_reports(leader)} == {employee.user_id, leader.user_id}
    assert len(db.list_reports(director)) == 4
    assert len(db.list_reports(minister)) == 5


def test_tasks_are_private_and_workspace_clear_is_display_only(tmp_path):
    db = build_org(tmp_path)
    leader, director = as_user(db, "leader1"), as_user(db, "director")
    leader_task = db.create_task(leader, {}, {}, "tasks/leader")
    director_task = db.create_task(director, {}, {}, "tasks/director")
    db.finish_task(leader_task, "finished", {"summary": {"report_count": 1}})
    db.finish_task(director_task, "finished", {"summary": {"report_count": 2}})
    assert [t["task_id"] for t in db.list_tasks(leader)] == [leader_task]
    assert db.get_task(director_task, leader) is None
    db.clear_task_page_display(leader)
    assert db.list_tasks(leader, include_hidden=False) == []
    assert [t["task_id"] for t in db.list_tasks(leader, include_hidden=True)] == [leader_task]


def test_business_reset_preserves_org_and_settings(tmp_path):
    db = build_org(tmp_path)
    employee = as_user(db, "employee1")
    save_report(db, employee, "r1")
    db.update_settings({"checks.enable_code_check": False})
    counts = db.clear_business_data()
    assert counts["reports"] == 1
    assert len(db.list_users()) == 6
    assert len(db.list_groups()) == 2
    assert db.get_settings({})["checks.enable_code_check"] is False


def test_audit_logs_are_indexed_paginated_retained_and_survive_business_reset(tmp_path, monkeypatch):
    clock = datetime(2026, 8, 14, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    monkeypatch.setattr("src.db.sqlite_backend.shanghai_now", lambda: clock)
    db = SQLiteBackend(tmp_path / "audit.sqlite3")
    db.initialize(False)
    with db.connect() as conn:
        indexes = {row[1] for row in conn.execute("pragma index_list(audit_logs)").fetchall()}
    assert {"idx_audit_logs_occurred", "idx_audit_logs_actor", "idx_audit_logs_category_result"} <= indexes

    db.append_audit_log({"occurred_at": "2026-01-01T00:00:00", "category": "authentication", "action": "auth.login", "outcome": "failure", "severity": "warning", "actor_username": "old"})
    for index in range(3):
        db.append_audit_log({"occurred_at": f"2026-08-14T10:0{index}:00", "category": "authentication", "action": "auth.login", "outcome": "failure" if index == 0 else "success", "severity": "warning" if index == 0 else "info", "actor_username": f"user{index}", "summary": f"事件 {index}"})

    first = db.list_audit_logs({"category": "authentication"}, limit=2)
    second = db.list_audit_logs({"category": "authentication"}, first["next_cursor"], limit=2)
    assert [row["actor_username"] for row in first["items"]] == ["user2", "user1"]
    assert [row["actor_username"] for row in second["items"]] == ["user0"]
    assert first["stats"] == {"total": 3, "failed": 1, "critical": 0, "login_failed": 1}
    assert not any(row["actor_username"] == "old" for row in db.export_audit_logs({}))

    db.clear_business_data()
    assert db.list_audit_logs({}, limit=10)["stats"]["total"] == 3


def test_organisation_delete_errors_explain_remaining_dependencies(tmp_path):
    db = build_org(tmp_path)
    with pytest.raises(ValueError, match="2 个小组、5 名人员"):
        db.delete_department("d1")
    with pytest.raises(ValueError, match="2 名人员"):
        db.delete_group("g1")


def test_reset_user_password_invalidates_existing_sessions(tmp_path):
    db = build_org(tmp_path)
    user = db.authenticate("employee1", "123456")
    assert user is not None
    session_id = db.create_session(user.user_id)

    db.reset_user_password(user.user_id, "new-safe-password")

    assert db.user_by_session(session_id) is None
    assert db.authenticate("employee1", "123456") is None
    assert db.authenticate("employee1", "new-safe-password") is not None


def test_bootstrap_initialization_creates_admin_and_test_employee(tmp_path):
    db = SQLiteBackend(tmp_path / "bootstrap.sqlite3")
    db.initialize(True)

    users = {u["username"]: u for u in db.list_users()}
    assert set(users) == {"admin", "test_employee"}
    assert users["test_employee"]["role"] == "employee"
    assert users["test_employee"]["is_test_account"] is True
    assert db.list_departments() == []
    assert db.list_groups() == []


def test_reset_confirmation_uses_the_same_clock_for_creation_and_validation(tmp_path, monkeypatch):
    monkeypatch.setattr("src.db.sqlite_backend.now_iso", lambda: "2030-01-01T09:00:00")
    db = SQLiteBackend(tmp_path / "reset.sqlite3")
    db.initialize(False)

    confirmation_id = db.create_reset_confirmation("u_admin")

    assert db.consume_reset_confirmation(confirmation_id, "u_admin") is True


def test_missing_reports_cover_workdays_and_submitter_scope(tmp_path):
    db = build_org(tmp_path)
    with db.connect() as conn:
        conn.execute("update users set created_at='2026-07-01T00:00:00'")
    employee, leader = as_user(db, "employee1"), as_user(db, "leader1")
    present = Report("present", employee.user_id, employee.display_name, employee.department_id, employee.department_name,
                     "2026-07-31", leader.display_name, "已提交", "工作内容足够长用于查重测试", group_id=employee.group_id,
                     group_name=employee.group_name, submitter_role=employee.role, created_at="2026-07-31T09:00:00")
    db.save_report(present)
    pipeline = DailyReportPipeline({"llm_judge": {"enabled": False}, "checks": {"enable_code_check": False, "enable_document_check": False}, "testcase_verification": {"enabled": False}}, db, None)
    result = pipeline.run(leader, {"report_date_start": "2026-07-31", "report_date_end": "2026-08-02"}, "task", str(tmp_path / "task"))

    assert result["summary"]["missing_report_count"] == 1
    assert result["missing_reports"] == [{"report_date": "2026-07-31", "user_id": leader.user_id, "employee_name": leader.display_name, "role": "group_leader", "department_id": "d1", "department_name": "研发部", "group_id": "g1", "group_name": "一组"}]
    assert (tmp_path / "task" / "missing_reports.xlsx").exists()
    assert {u["username"] for u in db.list_required_submitters(leader)} == {"leader1", "employee1"}
    assert {u["username"] for u in db.list_required_submitters(as_user(db, "director"))} == {"leader1", "leader2", "employee1", "employee2"}
    assert {u["username"] for u in db.list_required_submitters(as_user(db, "minister"))} == {"leader1", "leader2", "employee1", "employee2"}
    assert {u["username"] for u in db.list_export_submitters(employee)} == {"employee1"}


def test_report_export_scope_matches_organisation_roles(tmp_path):
    db = build_org(tmp_path)
    people = {name: as_user(db, name) for name in ("employee1", "employee2", "leader1", "director", "minister")}
    for name, person in people.items():
        report = Report(f"r_{name}", person.user_id, person.display_name, person.department_id, person.department_name,
                        "2026-07-31", person.manager_display_name, name, "完整工作描述", group_id=person.group_id,
                        group_name=person.group_name, submitter_role=person.role, created_at="2026-07-31T09:00:00")
        db.save_report(report)
    assert {r.owner_user_id for r in db.list_reports(people["employee1"], "2026-07-31", "2026-07-31")} == {people["employee1"].user_id}
    assert {r.owner_user_id for r in db.list_reports(people["leader1"], "2026-07-31", "2026-07-31")} == {people["employee1"].user_id, people["leader1"].user_id}
    assert {r.owner_user_id for r in db.list_reports(people["director"], "2026-07-31", "2026-07-31")} == {p.user_id for p in people.values() if p.role != "minister"}
    assert {r.owner_user_id for r in db.list_reports(people["minister"], "2026-07-31", "2026-07-31")} == {p.user_id for p in people.values()}


def test_api_key_file_precedes_environment_and_env_remains_fallback(tmp_path, monkeypatch):
    key_file = tmp_path / "llm_api_key"
    key_file.write_text("file-secret\n", encoding="utf-8")
    monkeypatch.setenv("DAILY_REPORT_LLM_API_KEY", "env-secret")
    judge = OpenAICompatibleJudge({"api_key_file": str(key_file), "api_key_env": "DAILY_REPORT_LLM_API_KEY"})
    assert judge.api_key == "file-secret"
    fallback = OpenAICompatibleJudge({"api_key_file": str(tmp_path / "missing"), "api_key_env": "DAILY_REPORT_LLM_API_KEY"})
    assert fallback.api_key == "env-secret"

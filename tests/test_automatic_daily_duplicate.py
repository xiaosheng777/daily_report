from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from src.core.automation import DailyDuplicateScheduler
from src.core.pipeline import DailyReportPipeline
from src.core.schemas import Report
from src.db.sqlite_backend import ROLE_ADMIN
from src.web.app import DailyReportHandler

from test_organization import as_user, build_org


def test_system_task_is_unique_and_failed_task_can_resume(tmp_path):
    db = build_org(tmp_path)
    payload = {"report_date_start": "2026-07-30", "report_date_end": "2026-07-30"}
    task_id, should_run = db.begin_automatic_task("2026-07-30", payload, {}, "tasks/pending")
    assert should_run is True
    assert db.get_task(task_id)["task_name"] == "7.30 日报自动查重（7.31 执行）"
    db.finish_task(task_id, "finished", {"summary": {"report_count": 2}})
    assert db.begin_automatic_task("2026-07-30", payload, {}, "tasks/pending") == (task_id, False)

    retry_id, retry = db.begin_automatic_task("2026-07-31", payload, {}, "tasks/pending")
    db.finish_task(retry_id, "failed", {}, "model unavailable")
    assert db.begin_automatic_task("2026-07-31", payload, {}, "tasks/pending") == (retry_id, True)
    db.finish_task(retry_id, "failed", {}, "model unavailable")
    assert db.begin_automatic_task("2026-07-31", payload, {}, "tasks/pending") == (retry_id, False)


def test_scheduler_runs_at_noon_once_and_not_before(monkeypatch, tmp_path):
    calls = []

    class Db:
        def begin_automatic_task(self, report_date, payload, config, result_dir):
            calls.append((report_date, payload))
            return "T_auto", len(calls) == 1

        def finish_task(self, *args):
            calls.append(("finish", args))

    class Pipeline:
        def __init__(self, *args):
            pass

        def run(self, *args):
            return {"summary": {"report_count": 0}}

    monkeypatch.setattr("src.core.automation.DailyReportPipeline", Pipeline)
    config = {"storage": {"root_dir": str(tmp_path)}, "llm_judge": {"enabled": False}, "automatic_daily_duplicate": {"enabled": True, "timezone": "Asia/Shanghai", "run_at": "12:00", "catch_up_on_start": True}}
    scheduler = DailyDuplicateScheduler(config, Db(), None)
    tz = ZoneInfo("Asia/Shanghai")
    assert scheduler.run_due(datetime(2026, 7, 31, 11, 59, tzinfo=tz)) is None
    assert scheduler.run_due(datetime(2026, 7, 31, 12, 0, tzinfo=tz)) == ("T_auto", True)
    assert scheduler.run_due(datetime(2026, 7, 31, 12, 1, tzinfo=tz)) == ("T_auto", False)
    assert calls[0][0] == "2026-07-30"


def test_system_task_results_are_filtered_by_manager_scope(tmp_path):
    db = build_org(tmp_path)
    db.create_user({"user_id": "a", "username": "admin", "display_name": "管理员", "role": ROLE_ADMIN})
    task_id, _ = db.begin_automatic_task("2026-07-30", {}, {}, "tasks/pending")
    result = {"report_cases": [
        {"owner_user_id": "e1", "department_id": "d1", "department_name": "研发部", "group_id": "g1", "group_name": "一组", "overall_risk": "high"},
        {"owner_user_id": "e2", "department_id": "d1", "department_name": "研发部", "group_id": "g2", "group_name": "二组", "overall_risk": "normal"},
    ], "missing_reports": [
        {"user_id": "e1", "department_id": "d1", "department_name": "研发部", "group_id": "g1", "group_name": "一组"},
        {"user_id": "e2", "department_id": "d1", "department_name": "研发部", "group_id": "g2", "group_name": "二组"},
    ]}
    db.finish_task(task_id, "finished", result)
    handler = object.__new__(DailyReportHandler)
    handler.db = db
    task = db.get_task(task_id, as_user(db, "leader1"))
    assert len(handler._visible_task_cases(task, as_user(db, "leader1"))) == 1
    assert len(handler._visible_missing_reports(task, as_user(db, "leader1"))) == 1
    assert len(handler._visible_task_cases(task, as_user(db, "director"))) == 2
    assert len(handler._visible_task_cases(task, as_user(db, "minister"))) == 2
    assert len(handler._visible_task_cases(task, as_user(db, "admin"))) == 2


def test_admin_manual_check_uses_company_scope(tmp_path):
    db = build_org(tmp_path)
    db.create_user({"user_id": "a", "username": "admin", "display_name": "管理员", "role": ROLE_ADMIN})
    for username in ("employee1", "employee2"):
        person = as_user(db, username)
        db.save_report(Report(f"r_{username}", person.user_id, person.display_name, person.department_id, person.department_name,
                              "2026-07-30", "组长", "工作", "完成日报功能", group_id=person.group_id,
                              group_name=person.group_name, submitter_role=person.role))
    config = {"llm_judge": {"enabled": False}, "checks": {"enable_code_check": False, "enable_document_check": False}, "testcase_verification": {"enabled": False}}
    result = DailyReportPipeline(config, db, None).run(as_user(db, "admin"), {"report_date_start": "2026-07-30", "report_date_end": "2026-07-30"})
    assert result["summary"]["report_count"] == 2


def test_noon_check_includes_report_submitted_before_nine_for_previous_day(tmp_path):
    db = build_org(tmp_path)
    person = as_user(db, "employee1")
    db.save_report(Report("r_early", person.user_id, person.display_name, person.department_id, person.department_name,
                          "2026-07-30", "组长", "工作", "完成日报功能", created_at="2026-07-31T08:59:59",
                          group_id=person.group_id, group_name=person.group_name, submitter_role=person.role))
    config = {"llm_judge": {"enabled": False}, "checks": {"enable_code_check": False, "enable_document_check": False}, "testcase_verification": {"enabled": False}}
    result = DailyReportPipeline(config, db, None).run(None, {"report_date_start": "2026-07-30", "report_date_end": "2026-07-30"})
    assert result["summary"]["report_count"] == 1

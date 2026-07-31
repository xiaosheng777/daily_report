from __future__ import annotations

from pathlib import Path

from openpyxl import Workbook

from src.core.pipeline import DailyReportPipeline
from src.core.schemas import Report
from src.core.testcase_excel import compare_fields_equal, load_daily_sheet_rows, rows_to_items
from src.db.factory import create_database
from src.storage.file_store import FileStorage
from src.utils.config import load_config
from src.utils.dates import now_iso


def _write_daily_xlsx(path: Path, rows: list[list[str]]) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "日报"
    headers = ["用例编号", "描述", "测试用例设计说明", "是否鲁棒", "需求号", "测试方法", "具体步骤", "期望结果", "测试程序号", "测试脚本号", "测试环境", "责任人"]
    ws.append(headers)
    for row in rows:
        ws.append(row)
    # Extra sheet should be ignored.
    wb.create_sheet("说明").append(["note"])
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)


def _base_config(tmp_path: Path) -> Path:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f"""
app:
  frontend_dir: {tmp_path}/frontend
  seed_demo_users: true
storage:
  root_dir: {tmp_path}/storage
database:
  backend: sqlite
  sqlite_path: {tmp_path}/storage/app.sqlite3
llm_judge:
  enabled: false
daily_duplicate:
  llm_candidate_top_n: 2
  self_history_days: 30
  cross_user_days: 3
  cross_user_scope: department
  max_candidates: 20
  llm_candidate_score_threshold: 0.72
  low_info_candidate_score_threshold: 0.82
  no_candidate_threshold: 0.55
  score_weights:
    text: 0.45
    semantic: 0.45
    recency: 0.10
testcase_verification:
  enabled: true
  snapshot_scope: group
  self_history_days: 7
  self_history_high_rate: 0.50
  self_history_medium_rate: 0.20
  compare_fields:
    - 描述
    - 测试用例设计说明
    - 具体步骤
    - 期望结果
checks:
  enable_code_check: false
  enable_document_check: false
  low_information_min_length: 30
  description_too_short_length: 12
""",
        encoding="utf-8",
    )
    return cfg


def test_load_daily_sheet_rows(tmp_path):
    xlsx = tmp_path / "cases.xlsx"
    _write_daily_xlsx(
        xlsx,
        [
            ["TC-001", "描述A", "设计A", "否", "R1", "Test", "步骤A", "期望A", "P1", "S1", "环境1", "张三"],
            ["TC-002", "描述B", "设计B", "否", "R2", "Test", "步骤B", "期望B", "P2", "S2", "环境1", "张三"],
        ],
    )
    rows = load_daily_sheet_rows(xlsx)
    assert set(rows) == {"TC-001", "TC-002"}
    assert rows["TC-001"]["描述"] == "描述A"
    items = rows_to_items(rows)
    assert items[0]["case_id"] in {"TC-001", "TC-002"}


def test_compare_fields_equal():
    left = {"用例编号": "TC-1", "描述": "A", "测试用例设计说明": "D", "具体步骤": "S", "期望结果": "E"}
    right = {"用例编号": "TC-1", "描述": "A", "测试用例设计说明": "D", "具体步骤": "S", "期望结果": "E"}
    assert compare_fields_equal(left, right)
    right["期望结果"] = "E2"
    assert not compare_fields_equal(left, right)


def test_self_history_rate_and_group_consistency(tmp_path):
    config = load_config(_base_config(tmp_path))
    db = create_database(config)
    storage = FileStorage(config)
    db.create_department({"department_id": "d1", "name": "研发部"})
    db.create_group({"group_id": "g1", "department_id": "d1", "name": "一组"})
    db.create_user({"username": "leader", "password": "leader123", "display_name": "组长", "role": "group_leader", "department_id": "d1", "group_id": "g1"})
    db.create_user({"username": "employee", "password": "employee123", "display_name": "员工", "role": "employee", "department_id": "d1", "group_id": "g1"})
    employee = db.authenticate("employee", "employee123")
    leader = db.authenticate("leader", "leader123")
    assert employee and leader

    case_row = ["TC-100", "描述X", "设计X", "否", "R", "Test", "步骤X", "期望X", "P", "S", "环境", "张三"]
    case_row_changed = ["TC-100", "描述X", "设计X", "否", "R", "Test", "步骤Y", "期望X", "P", "S", "环境", "张三"]
    member_only = ["TC-200", "描述M", "设计M", "否", "R", "Test", "步骤M", "期望M", "P", "S", "环境", "张三"]
    leader_extra = ["TC-300", "描述L", "设计L", "否", "R", "Test", "步骤L", "期望L", "P", "S", "环境", "组长"]

    hist_items = rows_to_items(load_daily_sheet_rows(_write_and_return(tmp_path / "hist.xlsx", [case_row])))
    today_items = rows_to_items(load_daily_sheet_rows(_write_and_return(tmp_path / "today.xlsx", [case_row, member_only])))
    leader_items = rows_to_items(load_daily_sheet_rows(_write_and_return(tmp_path / "leader.xlsx", [case_row_changed, leader_extra])))

    hist = Report(
        "", employee.user_id, employee.display_name, employee.department_id, employee.department_name,
        "2026-07-20", "leader", "历史日报", "历史工作描述足够长用于质量检查",
        testcase_items=hist_items, created_at=now_iso(), group_id=employee.group_id, group_name=employee.group_name,
        submitter_role="employee",
    )
    hist.report_id = DailyReportPipeline.build_report_id(hist)
    db.save_report(hist)

    today = Report(
        "", employee.user_id, employee.display_name, employee.department_id, employee.department_name,
        "2026-07-24", "leader", "今日日报", "今日工作描述足够长用于质量检查",
        testcase_items=today_items, created_at=now_iso(), group_id=employee.group_id, group_name=employee.group_name,
        submitter_role="employee",
    )
    today.report_id = DailyReportPipeline.build_report_id(today)
    db.save_report(today)

    leader_path = tmp_path / "leader_snapshot.xlsx"
    _write_daily_xlsx(leader_path, [case_row_changed, leader_extra])
    stored = storage.save_bytes(f"testcase_snapshots/{employee.group_id}", leader_path.name, leader_path.read_bytes(), "2026-07-24")
    db.save_testcase_snapshot({
        "department_id": employee.department_id,
        "department_name": employee.department_name,
        "group_id": employee.group_id,
        "group_name": employee.group_name,
        "snapshot_date": "2026-07-24",
        "original_filename": leader_path.name,
        "stored_path": stored.stored_path,
        "file_size": stored.file_size,
        "content_hash": stored.content_hash,
        "uploaded_by": leader.user_id,
    })

    result = DailyReportPipeline(config, db, storage).run(
        leader, {"report_date_start": "2026-07-24", "report_date_end": "2026-07-24"}
    )
    assert result["summary"]["report_count"] == 1
    case = result["report_cases"][0]
    tc = next(c for c in case["checks"] if c["checker_id"] == "testcase_check")
    statuses = {f["status"] for f in tc["findings"]}
    assert "self_history_rate" in statuses or "self_history_duplicate" in statuses
    assert "missing_in_leader" in statuses  # TC-200 missing in leader
    assert "content_mismatch" in statuses  # TC-100 steps differ
    assert "extra_in_leader" in statuses  # TC-300 only in leader
    assert tc["risk_level"] == "high"


def _write_and_return(path: Path, rows: list[list[str]]) -> Path:
    _write_daily_xlsx(path, rows)
    return path

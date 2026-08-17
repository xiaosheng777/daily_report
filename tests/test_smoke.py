from pathlib import Path

from src.core.docx_io import build_report_docx
from src.core.pipeline import DailyReportPipeline
from src.core.schemas import Report
from src.db.factory import create_database
from src.storage.file_store import FileStorage
from src.utils.config import load_config
from src.utils.dates import now_iso


def test_smoke_pipeline(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"""
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
checks:
  enable_code_check: true
  enable_document_check: true
  low_information_min_length: 30
  description_too_short_length: 12
""", encoding="utf-8")
    config = load_config(cfg)
    db = create_database(config)
    storage = FileStorage(config)
    db.create_department({"department_id": "d1", "name": "研发部"})
    db.create_group({"group_id": "g1", "department_id": "d1", "name": "一组"})
    db.create_user({"username": "employee", "password": "employee123", "display_name": "张三", "role": "employee", "department_id": "d1", "group_id": "g1"})
    user = db.authenticate("employee", "employee123")
    assert user
    for day in ("2026-06-25", "2026-06-26"):
        r = Report("", user.user_id, user.display_name, user.department_id, user.department_name, day, "m", "接口联调", "完成订单导出接口联调并提交测试", created_at=now_iso())
        r.report_id = DailyReportPipeline.build_report_id(r)
        body = build_report_docx({"name": r.employee_name, "department": r.department_name, "date": r.report_date, "manager": r.manager, "title_summary": r.title_summary, "work_description": r.work_description})
        stored = storage.save_bytes("submitted_reports", f"{day}.docx", body, day)
        r.original_filename, r.stored_path, r.file_size, r.content_hash = stored.original_filename, stored.stored_path, stored.file_size, stored.content_hash
        db.save_report(r)
    result = DailyReportPipeline(config, db, storage).run(user, {"report_date_start": "2026-06-26", "report_date_end": "2026-06-26"})
    assert result["summary"]["report_count"] == 1
    assert result["summary"]["high_risk_count"] >= 1
    assert result["summary"]["local_duplicate_checked_count"] == 1
    assert result["summary"]["local_duplicate_total_count"] == 1
    assert result["summary"]["llm_review_count"] == 0
    assert result["summary"]["degraded_local_count"] == 1

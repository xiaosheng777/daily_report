from types import SimpleNamespace
from io import BytesIO

from docx import Document
from openpyxl import load_workbook
from src.core.text import low_information
from src.db.sqlite_backend import ROLE_MINISTER
from src.web.app import DailyReportHandler, _build_periodic_reports_docx, _build_periodic_reports_xlsx, _periodic_export_args, _periodic_missing_reports, attachment_header


def test_attachment_header_encodes_non_ascii_filename():
    header = attachment_header("SR_2026-06-30_张三.docx")

    header.encode("latin-1")
    assert 'filename="SR_2026-06-30_.docx"' in header
    assert "filename*=UTF-8''SR_2026-06-30_%E5%BC%A0%E4%B8%89.docx" in header


def test_low_information_uses_only_combined_text_length():
    assert low_information("完成", "接口") is True
    assert low_information("完成", "接口开发联调测试") is False


def test_report_date_uses_request_payload_in_testing_mode(monkeypatch):
    handler = object.__new__(DailyReportHandler)
    handler.db = SimpleNamespace(get_derived_manager=lambda user: None)
    handler.storage = SimpleNamespace(save_bytes=lambda *args: SimpleNamespace(
        original_filename="report.docx", stored_path="submitted_reports/report.docx", file_size=1, content_hash="hash"
    ))
    user = SimpleNamespace(
        user_id="u1", display_name="张三", department_id="d1", department_name="研发部",
        group_id="g1", group_name="一组", role=ROLE_MINISTER,
    )
    report = handler._report_from_payload(user, {"date": "1999-01-01", "title_summary": "今日工作", "work_description": "完成日报功能"})

    assert report.report_date == "1999-01-01"


def test_periodic_export_arguments_require_a_valid_date_range():
    assert _periodic_export_args({"report_type": ["weekly"], "start": ["2026-07-01"], "end": ["2026-07-31"]}) == ("weekly", "2026-07-01", "2026-07-31")
    import pytest
    with pytest.raises(ValueError, match="开始日期"):
        _periodic_export_args({"report_type": ["monthly"], "start": ["2026-08-01"], "end": ["2026-07-31"]})


def test_periodic_exports_include_complete_report_details():
    from src.core.schemas import Report

    report = Report("r1", "u1", "张三", "d1", "研发部", "2026-07-31", "组长", "完成接口", "完成接口设计、开发和联调。",
                    code_items=[{"file_name": "main.py", "language": "python", "work_type": "修改", "description": "新增接口"}],
                    document_items=[{"document_name": "设计说明", "document_type": "docx", "work_type": "新增", "description": "补充设计"}],
                    testcase_items=[{"用例编号": "TC-1", "描述": "接口测试"}], submitter_role="employee",
                    created_at="2026-07-31T09:00:00", updated_at="2026-07-31T10:00:00")
    artifacts = {"r1": [{"original_filename": "接口说明.pdf", "artifact_type": "document", "created_at": "2026-07-31T09:00:00"}]}

    word = _build_periodic_reports_docx("周报汇总", "本人", "2026-07-28", "2026-07-31", [report], artifacts)
    text = "\n".join(paragraph.text for paragraph in Document(BytesIO(word)).paragraphs)
    assert "完成接口设计、开发和联调。" in text
    assert "代码记录" in text
    book = load_workbook(BytesIO(_build_periodic_reports_xlsx("周报汇总", "本人", "2026-07-28", "2026-07-31", [report], artifacts)))
    assert book.sheetnames == ["日报明细", "代码记录", "文档记录", "测试用例", "附件"]
    assert book["日报明细"].max_row == 3
    assert book["附件"].max_row == 2


def test_periodic_word_export_handles_empty_reports():
    word = _build_periodic_reports_docx("月报汇总", "全公司", "2026-07-01", "2026-07-31", [], {})
    assert "暂无日报" in "\n".join(paragraph.text for paragraph in Document(BytesIO(word)).paragraphs)


def test_periodic_exports_mark_workday_without_report_as_none_and_skip_weekends():
    from src.core.schemas import Report

    submitter = {"user_id": "u1", "display_name": "张三", "role": "employee", "department_name": "研发部", "group_name": "一组"}
    db = SimpleNamespace(list_export_submitters=lambda user: [submitter])
    report = Report("r1", "u1", "张三", "d1", "研发部", "2026-07-31", "组长", "已提交", "工作内容", created_at="2026-07-31T09:00:00")
    missing = _periodic_missing_reports(db, SimpleNamespace(), "2026-07-30", "2026-08-02", [report])

    assert missing == [{"日期": "2026-07-30", "姓名": "张三", "职务": "employee", "部门": "研发部", "小组": "一组", "日报": "无"}]
    word = _build_periodic_reports_docx("周报汇总", "本人", "2026-07-30", "2026-08-02", [report], {}, missing)
    assert "无" in [cell.text for table in Document(BytesIO(word)).tables for row in table.rows for cell in row.cells]
    book = load_workbook(BytesIO(_build_periodic_reports_xlsx("周报汇总", "本人", "2026-07-30", "2026-08-02", [report], {}, missing)))
    rows = list(book["日报明细"].values)
    assert (None, "2026-07-30", "张三", "employee", "研发部", "一组", "无", "无", None, None, "无") in rows

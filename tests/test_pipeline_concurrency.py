import threading
import time
from types import SimpleNamespace

from src.core.pipeline import DailyReportPipeline
from src.core.schemas import CheckResult, Report


def reports(count: int) -> list[Report]:
    return [
        Report(
            f"r{index}",
            f"u{index}",
            f"员工{index}",
            "d1",
            "研发部",
            "2026-08-11",
            "组长",
            f"任务{index}",
            f"完成第{index}个接口的开发联调和测试工作",
        )
        for index in range(count)
    ]


def test_main_pipeline_keeps_cpu_checks_serial_and_ordered(monkeypatch):
    pipeline = DailyReportPipeline(
        {
            "daily_duplicate": {"worker_count": 3},
            "checks": {"enable_code_check": False, "enable_document_check": False},
        },
        SimpleNamespace(),
        None,
    )
    active = 0
    maximum = 0
    lock = threading.Lock()

    def fake_check(report, all_reports, index_by_id, matrix):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        try:
            time.sleep(0.04)
            return CheckResult("report_duplicate_check", True, "success", "normal", report.report_id)
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(pipeline, "_daily_duplicate_check", fake_check)
    cases = pipeline._build_cases(reports(7), reports(7))

    assert maximum == 1
    assert [case.report_id for case in cases] == [f"r{index}" for index in range(7)]
    assert [case.checks[1].summary for case in cases] == [f"r{index}" for index in range(7)]


def test_daily_duplicate_worker_failure_is_isolated(monkeypatch):
    pipeline = DailyReportPipeline(
        {
            "daily_duplicate": {"worker_count": 2},
            "checks": {"enable_code_check": False, "enable_document_check": False},
        },
        SimpleNamespace(),
        None,
    )

    def fake_check(report, all_reports, index_by_id, matrix):
        if report.report_id == "r1":
            raise RuntimeError("model crashed")
        return CheckResult("report_duplicate_check", True, "success", "normal", report.report_id)

    monkeypatch.setattr(pipeline, "_daily_duplicate_check", fake_check)
    cases = pipeline._build_cases(reports(3), reports(3))

    assert [case.report_id for case in cases] == ["r0", "r1", "r2"]
    assert cases[1].checks[1].status == "failed"
    assert cases[1].checks[1].risk_level == "unknown"
    assert "model crashed" in cases[1].checks[1].summary

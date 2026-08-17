import threading
import time
from types import SimpleNamespace

from src.core.pipeline import DailyReportPipeline
from src.core.schemas import CheckResult, Report, duplicate_usage_summary
from src.llm.base import LLMJudgeResult


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


def test_daily_duplicate_checks_run_in_bounded_pool_and_keep_report_order(monkeypatch):
    pipeline = DailyReportPipeline(
        {
            "daily_duplicate": {"report_worker_count": 3},
            "checks": {"enable_code_check": False, "enable_document_check": False},
        },
        SimpleNamespace(),
        None,
    )
    active = 0
    maximum = 0
    completed = []
    emitted = []
    lock = threading.Lock()

    def fake_check(report, all_reports, index_by_id, matrix):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        try:
            time.sleep(0.01 * (3 - int(report.report_id[1:]) % 3))
            return CheckResult("report_duplicate_check", True, "success", "normal", report.report_id)
        finally:
            with lock:
                completed.append(report.report_id)
                active -= 1

    monkeypatch.setattr(pipeline, "_daily_duplicate_check", fake_check)
    cases = pipeline._build_cases(reports(7), reports(7), case_callback=lambda case: emitted.append(case["report_id"]))

    assert 1 < maximum <= 3
    assert completed[:3] != ["r0", "r1", "r2"]
    assert [case.report_id for case in cases] == [f"r{index}" for index in range(7)]
    assert [case.checks[1].summary for case in cases] == [f"r{index}" for index in range(7)]
    assert emitted == [f"r{index}" for index in range(7)]


def test_report_worker_count_defaults_to_three_and_is_capped_at_eight(monkeypatch):
    worker_counts = []

    class CapturingExecutor:
        def __init__(self, max_workers, **_kwargs):
            worker_counts.append(max_workers)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def map(self, callback, items):
            return map(callback, items)

    monkeypatch.setattr("src.core.pipeline.ThreadPoolExecutor", CapturingExecutor)

    for daily_cfg in ({}, {"report_worker_count": 99}):
        pipeline = DailyReportPipeline(
            {"daily_duplicate": daily_cfg, "checks": {"enable_code_check": False, "enable_document_check": False}},
            SimpleNamespace(),
            None,
        )
        monkeypatch.setattr(
            pipeline,
            "_daily_duplicate_check",
            lambda report, *_args: CheckResult("report_duplicate_check", True, "success", "normal", report.report_id),
        )
        pipeline._build_cases(reports(10), reports(10))

    assert worker_counts == [3, 8]


def test_daily_duplicate_worker_failure_is_isolated(monkeypatch):
    pipeline = DailyReportPipeline(
        {
            "daily_duplicate": {"report_worker_count": 2},
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


def test_every_llm_eligible_candidate_is_reviewed_without_cap_or_circuit_breaker(monkeypatch):
    reviewed_report_ids = []
    lock = threading.Lock()

    class FakeJudge:
        def judge_duplicate(self, current, _matched, _signals):
            with lock:
                reviewed_report_ids.append(current["report_id"])
            index = int(current["report_id"][1:])
            if index % 2:
                return LLMJudgeResult(True, True, "low", decision_summary="LLM 已复核")
            return LLMJudgeResult(True, False, error="temporary model failure")

    current_reports = reports(65)
    pipeline = DailyReportPipeline(
        {
            "daily_duplicate": {"report_worker_count": 8},
            # Retired settings must not limit calls even when present in an old task snapshot.
            "llm_judge": {"max_reports_per_task": 1, "task_cutoff_seconds": 0},
            "checks": {"enable_code_check": False, "enable_document_check": False},
        },
        SimpleNamespace(),
        None,
        FakeJudge(),
    )

    def eligible_candidate(current, *_args):
        return [{
            "report": current_reports[-1],
            "candidate_score": 1.0,
            "text_similarity": 1.0,
            "semantic_similarity": 1.0,
            "recency_score": 1.0,
            "match_type": "cross_user_repeat",
            "days_gap": 0,
            "hash_same": True,
        }]

    monkeypatch.setattr(pipeline, "_collect_candidates", eligible_candidate)
    cases = pipeline._build_cases(current_reports, current_reports)
    usage = duplicate_usage_summary([case.to_dict() for case in cases])

    assert sorted(reviewed_report_ids) == sorted(report.report_id for report in current_reports)
    assert usage == {
        "local_duplicate_checked_count": 65,
        "local_duplicate_total_count": 65,
        "llm_review_count": 32,
        "degraded_local_count": 33,
    }

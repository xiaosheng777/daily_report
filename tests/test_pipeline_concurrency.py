import copy
import threading
import time
from types import SimpleNamespace

import pytest

from src.core.pipeline import DailyReportPipeline, TaskPaused
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
    # Checkpoints are intentionally persisted as local workers complete; final
    # task output remains in original report order.
    assert sorted(emitted) == [f"r{index}" for index in range(7)]
    assert emitted != [f"r{index}" for index in range(7)]


def test_report_worker_count_defaults_to_three_and_is_capped_at_eight(monkeypatch):
    for daily_cfg, expected in (({}, 3), ({"report_worker_count": 99}, 8)):
        pipeline = DailyReportPipeline(
            {"daily_duplicate": daily_cfg, "checks": {"enable_code_check": False, "enable_document_check": False}},
            SimpleNamespace(),
            None,
        )
        assert pipeline._local_worker_count() == expected


def test_local_worker_count_overrides_legacy_worker_count_and_is_capped_at_thirty_two(monkeypatch):
    for daily_cfg, expected in (({"local_worker_count": 99, "report_worker_count": 1}, 32), ({"local_worker_count": 12, "report_worker_count": 1}, 12)):
        pipeline = DailyReportPipeline(
            {"daily_duplicate": daily_cfg, "checks": {"enable_code_check": False, "enable_document_check": False}},
            SimpleNamespace(),
            None,
        )
        assert pipeline._local_worker_count() == expected


def test_attachment_tokenizers_enforce_configured_hard_limits():
    pipeline = DailyReportPipeline({"checks": {"code_max_tokens": 3, "document_max_tokens": 3}}, SimpleNamespace(), None)

    with pytest.raises(ValueError, match="代码 Token"):
        pipeline._normalize_code_tokens("one two three four", 3)
    with pytest.raises(ValueError, match="文档 Token"):
        pipeline._document_token_similarity(" ".join(["one"] * 101), "one two")


def test_local_checkpoint_survives_mid_phase_crash(monkeypatch):
    source = reports(5)

    class FakeDatabase:
        def list_reports(self, *_args, **_kwargs):
            return source

        @staticmethod
        def list_required_submitters(_user):
            return []

    calls, checkpoints = [], []
    pipeline = DailyReportPipeline(
        {"daily_duplicate": {"local_worker_count": 1}, "checks": {"enable_code_check": False, "enable_document_check": False}},
        FakeDatabase(), None,
    )
    monkeypatch.setattr(pipeline, "_daily_duplicate_check", lambda report, *_args: (calls.append(report.report_id), CheckResult("report_duplicate_check", True, "success", "normal", report.report_id))[1])

    def persist_then_crash(case):
        checkpoints.append(copy.deepcopy(case))
        if len(checkpoints) == 2:
            raise RuntimeError("simulated task process crash")

    with pytest.raises(RuntimeError, match="crash"):
        pipeline.run(scope={"report_date_start": "2026-08-11", "report_date_end": "2026-08-11"}, case_callback=persist_then_crash)
    assert calls == ["r0", "r1"]

    resumed_calls = []
    resumed = DailyReportPipeline(
        {"daily_duplicate": {"local_worker_count": 1}, "checks": {"enable_code_check": False, "enable_document_check": False}},
        FakeDatabase(), None,
    )
    monkeypatch.setattr(resumed, "_daily_duplicate_check", lambda report, *_args: (resumed_calls.append(report.report_id), CheckResult("report_duplicate_check", True, "success", "normal", report.report_id))[1])
    resumed.run(scope={"report_date_start": "2026-08-11", "report_date_end": "2026-08-11"}, resume_cases=checkpoints)

    assert resumed_calls == ["r2", "r3", "r4"]


def test_pause_during_local_phase_only_drains_inflight_reports(monkeypatch):
    source = reports(8)

    class FakeDatabase:
        def list_reports(self, *_args, **_kwargs):
            return source

        @staticmethod
        def list_required_submitters(_user):
            return []

    calls, saved = [], []
    pipeline = DailyReportPipeline(
        {"daily_duplicate": {"local_worker_count": 2}, "checks": {"enable_code_check": False, "enable_document_check": False}},
        FakeDatabase(), None,
    )

    def local_check(report, *_args):
        calls.append(report.report_id)
        time.sleep(.01)
        return CheckResult("report_duplicate_check", True, "success", "normal", report.report_id)

    monkeypatch.setattr(pipeline, "_daily_duplicate_check", local_check)
    with pytest.raises(TaskPaused):
        pipeline.run(
            scope={"report_date_start": "2026-08-11", "report_date_end": "2026-08-11"},
            control_callback=lambda: "pause" if len(saved) >= 3 else "",
            case_callback=lambda case: saved.append(copy.deepcopy(case)),
        )

    assert len(saved) == len(calls)
    assert 3 <= len(calls) <= 4


def test_candidate_indexes_keep_the_existing_user_department_and_date_scope():
    def report(report_id, user_id, department_id, report_date):
        return Report(report_id, user_id, report_id, department_id, department_id, report_date, "组长", report_id, "完成接口开发和测试")

    current = report("current", "u1", "d1", "2026-08-20")
    all_reports = [
        current,
        report("self-in-window", "u1", "d1", "2026-07-21"),
        report("self-too-old", "u1", "d1", "2026-07-20"),
        report("department-in-window", "u2", "d1", "2026-08-17"),
        report("department-too-old", "u3", "d1", "2026-08-16"),
        report("other-department", "u4", "d2", "2026-08-20"),
        report("future", "u1", "d1", "2026-08-21"),
    ]
    pipeline = DailyReportPipeline({"daily_duplicate": {"self_history_days": 30, "cross_user_days": 3, "cross_user_scope": "department"}}, SimpleNamespace(), None)
    index_by_id, matrix = pipeline._prepare_duplicate_context(all_reports)

    candidates = pipeline._collect_candidates(current, all_reports, index_by_id, matrix)

    assert {candidate["report"].report_id for candidate in candidates} == {"self-in-window", "department-in-window"}


def test_resumable_task_builds_duplicate_index_once_without_report_batches(monkeypatch):
    source = reports(7)

    class FakeDatabase:
        def list_reports(self, *_args, **_kwargs):
            return source

        @staticmethod
        def list_required_submitters(_user):
            return []

    fit_transform_calls = []

    class CapturingVectorizer:
        def __init__(self, **_kwargs):
            pass

        def fit_transform(self, texts):
            fit_transform_calls.append(list(texts))
            return "matrix"

    pipeline = DailyReportPipeline(
        {"daily_duplicate": {"report_worker_count": 3}, "checks": {"enable_code_check": False, "enable_document_check": False}},
        FakeDatabase(),
        None,
    )
    built = []

    def build(queue, _all, _progress, _callback, context):
        built.append(([report.report_id for report in queue], context))
        return [
            SimpleNamespace(report_id=report.report_id, to_dict=lambda report=report: {"report_id": report.report_id})
            for report in queue
        ]

    monkeypatch.setattr("src.core.pipeline.TfidfVectorizer", CapturingVectorizer)
    monkeypatch.setattr(pipeline, "_build_cases", build)

    result = pipeline.run(scope={"report_date_start": "2026-08-11", "report_date_end": "2026-08-11"})

    assert len(fit_transform_calls) == 1
    assert len(fit_transform_calls[0]) == 7
    assert built == [([f"r{index}" for index in range(7)], ({f"r{index}": index for index in range(7)}, "matrix"))]
    assert [case["report_id"] for case in result["report_cases"]] == [f"r{index}" for index in range(7)]


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

    class FakeDatabase:
        def list_reports(self, *_args, **_kwargs):
            return current_reports

        @staticmethod
        def list_required_submitters(_user):
            return []

    pipeline = DailyReportPipeline(
        {
            "daily_duplicate": {"report_worker_count": 8},
            # Retired settings must not limit calls even when present in an old task snapshot.
            "llm_judge": {"max_reports_per_task": 1, "task_cutoff_seconds": 0},
            "checks": {"enable_code_check": False, "enable_document_check": False},
        },
        FakeDatabase(),
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
            "hash_same": False,
        }]

    monkeypatch.setattr(pipeline, "_collect_candidates", eligible_candidate)
    result = pipeline.run(scope={"report_date_start": "2026-08-11", "report_date_end": "2026-08-11"})
    usage = duplicate_usage_summary(result["report_cases"])

    assert sorted(reviewed_report_ids) == sorted(report.report_id for report in current_reports)
    assert usage == {
        "local_duplicate_checked_count": 65,
        "local_duplicate_total_count": 65,
        "llm_review_count": 32,
        "degraded_local_count": 33,
    }


def test_llm_pairs_are_persisted_after_local_phase_and_resume_skips_finished_pairs(monkeypatch):
    source = reports(2)

    class FakeDatabase:
        def list_reports(self, *_args, **_kwargs):
            return source

        @staticmethod
        def list_required_submitters(_user):
            return []

    def candidate(current, *_args):
        return [{
            "report": source[1],
            "candidate_score": 0.9,
            "text_similarity": 0.9,
            "semantic_similarity": 0.9,
            "recency_score": 1.0,
            "match_type": "cross_user_repeat",
            "days_gap": 0,
            "hash_same": False,
        }]

    persisted = []
    stages = []
    progress_messages = []

    class FirstJudge:
        def judge_duplicate(self, *_args):
            assert persisted[-1]["checks"][1]["findings"][0]["details"]["llm_pending"] is True
            return LLMJudgeResult(True, True, "medium", decision_summary="LLM 已复核")

    pipeline = DailyReportPipeline(
        {"daily_duplicate": {"report_worker_count": 1}, "llm_judge": {"per_task_max_concurrency": 1}, "checks": {"enable_code_check": False, "enable_document_check": False}},
        FakeDatabase(),
        None,
        FirstJudge(),
    )
    monkeypatch.setattr(pipeline, "_collect_candidates", candidate)
    result = pipeline.run(
        scope={"employee_scope": "u0"},
        progress_callback=lambda stage, _current, _total, message="": (stages.append(stage), progress_messages.append(message)),
        case_callback=lambda case: persisted.append(copy.deepcopy(case)),
    )

    local_case, finished_case = persisted
    assert local_case["checks"][1]["findings"][0]["details"]["llm_pending"] is True
    assert finished_case["checks"][1]["findings"][0]["details"]["llm_pending"] is False
    assert "local_filtering" in stages and "llm_judging" in stages
    assert any("active：1，queued：0" in message for message in progress_messages)
    assert result["report_cases"][0]["checks"][1]["findings"][0]["details"]["llm_reviewed"] is True

    resumed_calls = []

    class ResumeJudge:
        def judge_duplicate(self, *_args):
            resumed_calls.append(True)
            return LLMJudgeResult(True, True, "low", decision_summary="恢复后完成")

    resumed = DailyReportPipeline(
        {"daily_duplicate": {"report_worker_count": 1}, "llm_judge": {"per_task_max_concurrency": 1}, "checks": {"enable_code_check": False, "enable_document_check": False}},
        FakeDatabase(),
        None,
        ResumeJudge(),
    )
    monkeypatch.setattr(resumed, "_collect_candidates", lambda *_args: (_ for _ in ()).throw(AssertionError("finished local work must not run again")))
    resumed_result = resumed.run(scope={"employee_scope": "u0"}, resume_cases=[local_case])

    assert resumed_calls == [True]
    completed_case = resumed_result["report_cases"][0]
    resumed.run(scope={"employee_scope": "u0"}, resume_cases=[completed_case])
    assert resumed_calls == [True]


def test_pause_during_llm_phase_resumes_only_unfinished_pairs(monkeypatch):
    source = reports(3)

    class FakeDatabase:
        def list_reports(self, *_args, **_kwargs):
            return source

        @staticmethod
        def list_required_submitters(_user):
            return []

    def candidates(_current, *_args):
        return [
            {"report": source[index], "candidate_score": 0.9, "text_similarity": 0.9, "semantic_similarity": 0.9,
             "recency_score": 1.0, "match_type": "cross_user_repeat", "days_gap": 0, "hash_same": False}
            for index in (1, 2)
        ]

    called = []

    class Judge:
        def judge_duplicate(self, _current, matched, _signals):
            called.append(matched["report_id"])
            return LLMJudgeResult(True, True, "low", decision_summary="已判断")

    saved = []
    pipeline = DailyReportPipeline(
        {"daily_duplicate": {"report_worker_count": 1}, "llm_judge": {"per_task_max_concurrency": 1}, "checks": {"enable_code_check": False, "enable_document_check": False}},
        FakeDatabase(),
        None,
        Judge(),
    )
    monkeypatch.setattr(pipeline, "_collect_candidates", candidates)
    # Local checkpoints now poll controls too; leave local phase unpaused and
    # pause immediately after the first LLM pair completes.
    controls = iter([""] * 6 + ["pause"])
    with pytest.raises(TaskPaused, match="暂停"):
        pipeline.run(scope={"employee_scope": "u0"}, control_callback=lambda: next(controls), case_callback=lambda case: saved.append(copy.deepcopy(case)))

    checkpoint = saved[-1]
    pending = [finding["details"]["matched_report_id"] for finding in checkpoint["checks"][1]["findings"] if finding["details"].get("llm_pending")]
    assert called == ["r1"]
    assert pending == ["r2"]

    resumed = DailyReportPipeline(
        {"daily_duplicate": {"report_worker_count": 1}, "checks": {"enable_code_check": False, "enable_document_check": False}},
        FakeDatabase(),
        None,
        Judge(),
    )
    monkeypatch.setattr(resumed, "_collect_candidates", lambda *_args: (_ for _ in ()).throw(AssertionError("local phase must not repeat")))
    resumed.run(scope={"employee_scope": "u0"}, resume_cases=[checkpoint])
    assert called == ["r1", "r2"]


def test_llm_phase_respects_per_task_concurrency(monkeypatch):
    source = reports(6)

    class FakeDatabase:
        def list_reports(self, *_args, **_kwargs):
            return source

        @staticmethod
        def list_required_submitters(_user):
            return []

    active = 0
    maximum = 0
    lock = threading.Lock()

    class Judge:
        def judge_duplicate(self, *_args):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            try:
                time.sleep(0.02)
                return LLMJudgeResult(True, True, "low", decision_summary="已判断")
            finally:
                with lock:
                    active -= 1

    pipeline = DailyReportPipeline(
        {"daily_duplicate": {"report_worker_count": 1}, "llm_judge": {"per_task_max_concurrency": 2}, "checks": {"enable_code_check": False, "enable_document_check": False}},
        FakeDatabase(),
        None,
        Judge(),
    )
    monkeypatch.setattr(pipeline, "_collect_candidates", lambda current, *_args: [{
        "report": source[-1], "candidate_score": 0.9, "text_similarity": 0.9, "semantic_similarity": 0.9,
        "recency_score": 1.0, "match_type": "cross_user_repeat", "days_gap": 0, "hash_same": False,
    }])

    pipeline.run(scope={"report_date_start": "2026-08-11", "report_date_end": "2026-08-11"})

    assert maximum == 2

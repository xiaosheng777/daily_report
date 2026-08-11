from pathlib import Path
from unittest.mock import patch

from src.core.pipeline import DailyReportPipeline
from src.core.schemas import Report


class FakeDatabase:
    def __init__(self, reports, artifacts):
        self.reports = reports
        self.artifacts = artifacts

    def list_reports(self, _user=None):
        return self.reports

    def list_artifacts_for_report(self, report_id):
        return self.artifacts.get(report_id, [])


class FakeStorage:
    def __init__(self, files):
        self.files = files

    def read(self, stored_path):
        return self.files[stored_path]


def test_document_check_uses_token_similarity_for_unspaced_chinese():
    current = Report("current", "u1", "张三", "d1", "研发部", "2026-08-01", "组长", "当前文档", "内容")
    historical = Report("history", "u1", "张三", "d1", "研发部", "2026-07-31", "组长", "历史文档", "内容")
    artifacts = {
        "current": [{"artifact_type": "document", "original_filename": "current.txt", "stored_path": "current.txt"}],
        "history": [{"artifact_type": "document", "original_filename": "history.md", "stored_path": "history.md"}],
    }
    pipeline = DailyReportPipeline({
        "checks": {"document_history_days": 30, "document_token_similarity_threshold": 0.65, "document_token_high_risk_threshold": 0.85},
    }, FakeDatabase([current, historical], artifacts), FakeStorage({"current.txt": "订单导出接口联调测试完成".encode(), "history.md": "订单导出接口联调测试完成".encode()}))

    result = pipeline._document_token_check(current)

    assert result.status == "success"
    assert result.risk_level == "high"
    assert result.findings[0].score == 1.0
    assert result.findings[0].details["engine"] == "token_similarity"
    assert result.findings[0].details["matches"][0]["matched_token_count"] > 0


def test_document_check_does_not_require_jplag():
    current = Report("current", "u1", "张三", "d1", "研发部", "2026-08-01", "组长", "当前文档", "内容")
    historical = Report("history", "u1", "张三", "d1", "研发部", "2026-07-31", "组长", "历史文档", "内容")
    artifacts = {
        "current": [{"artifact_type": "document", "original_filename": "current.txt", "stored_path": "current.txt"}],
        "history": [{"artifact_type": "document", "original_filename": "history.txt", "stored_path": "history.txt"}],
    }
    pipeline = DailyReportPipeline({"checks": {}}, FakeDatabase([current, historical], artifacts), FakeStorage({"current.txt": b"same content", "history.txt": b"same content"}))

    result = pipeline._document_token_check(current)

    assert result.status == "success"
    assert result.findings[0].details["engine"] == "token_similarity"


def test_code_check_uses_jplag_similarity_for_self_history(tmp_path, monkeypatch):
    current = Report("current", "u1", "张三", "d1", "研发部", "2026-08-01", "组长", "当前代码", "内容")
    historical = Report("history", "u1", "张三", "d1", "研发部", "2026-07-31", "组长", "历史代码", "内容")
    artifacts = {
        "current": [{"artifact_type": "code", "original_filename": "current.py", "stored_path": "current.py"}],
        "history": [{"artifact_type": "code", "original_filename": "history.py", "stored_path": "history.py"}],
    }
    jar = Path(tmp_path / "jplag.jar")
    jar.write_bytes(b"jar")
    pipeline = DailyReportPipeline({
        "checks": {"code_history_days": 30},
        "jplag": {"enabled": True, "jar_path": str(jar), "code_similarity_threshold": 0.65, "code_high_risk_threshold": 0.85},
    }, FakeDatabase([current, historical], artifacts), FakeStorage({"current.py": b"print('same')", "history.py": b"print('same')"}))
    monkeypatch.setattr(pipeline, "_run_code_jplag", lambda *_args: (0.91, 1, 1))

    result = pipeline._code_jplag_check(current)

    assert result.status == "success"
    assert result.risk_level == "high"
    assert result.findings[0].score == 0.91
    assert result.findings[0].details["engine"] == "jplag"


def test_code_check_uses_local_token_similarity_without_jplag():
    current = Report("current", "u1", "张三", "d1", "研发部", "2026-08-02", "组长", "当前代码", "内容")
    historical = Report("history", "u1", "张三", "d1", "研发部", "2026-08-01", "组长", "历史代码", "内容")
    artifacts = {
        "current": [{"artifact_type": "code", "original_filename": "current.py", "stored_path": "current.py"}],
        "history": [{"artifact_type": "code", "original_filename": "history.py", "stored_path": "history.py"}],
    }
    source = b"def add(value, amount):\n    return value + amount\n"
    pipeline = DailyReportPipeline({"checks": {"code_history_days": 30, "code_token_similarity_threshold": 0.65, "code_token_high_risk_threshold": 0.85}}, FakeDatabase([current, historical], artifacts), FakeStorage({"current.py": source, "history.py": source}))

    result = pipeline._code_token_check(current)

    assert result.risk_level == "high"
    assert result.findings[0].details["engine"] == "code_token_similarity"
    assert result.findings[0].details["matches"][0]["matched_token_count"] > 0


def test_multiple_current_code_reports_share_one_jplag_run(tmp_path, monkeypatch):
    first = Report("first", "u1", "张三", "d1", "研发部", "2026-08-01", "组长", "第一份代码", "内容")
    second = Report("second", "u1", "张三", "d1", "研发部", "2026-08-02", "组长", "第二份代码", "内容")
    artifacts = {
        "first": [{"artifact_type": "code", "original_filename": "first.py", "stored_path": "first.py"}],
        "second": [{"artifact_type": "code", "original_filename": "second.py", "stored_path": "second.py"}],
    }
    jar = Path(tmp_path / "jplag.jar")
    jar.write_bytes(b"jar")
    pipeline = DailyReportPipeline({
        "checks": {"code_history_days": 30},
        "jplag": {"enabled": True, "jar_path": str(jar), "code_similarity_threshold": 0.65, "code_high_risk_threshold": 0.85},
    }, FakeDatabase([first, second], artifacts), FakeStorage({"first.py": b"print('same')", "second.py": b"print('same')"}))
    monkeypatch.setattr(pipeline, "_run_jplag_result", lambda *_args: (1.0, [{"source": "new\\submission_000", "matched": "old\\submission_001", "similarity": 1.0}]))

    results = pipeline._batch_code_jplag_checks([first, second], [first, second])

    assert set(results) == {"first", "second"}
    assert results["first"].findings[0].status == "no_history"
    assert results["second"].risk_level == "high"
    assert results["second"].findings[0].details["execution"] == "batched"


def test_reads_jplag_match_rows_from_csv(tmp_path):
    csv_file = tmp_path / "matches.csv"
    csv_file.write_text(
        "First Submission,Second Submission,Average Similarity,Max Similarity\n"
        "new/current,old/history_000,75.0,90.0\n",
        encoding="utf-8",
    )

    score, matches = DailyReportPipeline._read_jplag_result(tmp_path)

    assert score == 0.9
    assert matches == [{"source": "new/current", "matched": "old/history_000", "similarity": 0.9}]


def test_jplag_is_forced_to_headless_run_mode(tmp_path):
    pipeline = DailyReportPipeline({"jplag": {}}, FakeDatabase([], {}), FakeStorage({}))
    root, new_root, old_root = tmp_path / "work", tmp_path / "new", tmp_path / "old"
    root.mkdir()
    (root / "matches.csv").write_text(
        "First Submission,Second Submission,Max Similarity\nnew/current,old/history,0.9\n",
        encoding="utf-8",
    )
    with patch("src.core.pipeline.subprocess.run") as run:
        run.return_value.returncode = 0
        pipeline._run_jplag(Path("jplag.jar"), root, new_root, old_root, "text", 12, 30)

    assert "--mode" in run.call_args.args[0]
    assert run.call_args.args[0][run.call_args.args[0].index("--mode") + 1] == "RUN"


def test_local_jplag_multi_mode_compares_source_files():
    jar = Path(__file__).parents[1] / "vendor" / "jplag" / "jplag.jar"
    if not jar.exists():
        pytest.skip("本地未提供 JPlag jar")
    artifacts = {
        "current": [{"artifact_type": "code", "original_filename": "Main.java", "stored_path": "current.java"}],
        "history": [{"artifact_type": "code", "original_filename": "Main.java", "stored_path": "history.java"}],
    }
    storage = FakeStorage({
        "current.java": b"class Main { int add(int a, int b) { return a + b; } }",
        "history.java": b"class Main { int add(int a, int b) { return a + b; } }",
    })
    pipeline = DailyReportPipeline({"jplag": {"java_command": "java", "code_language": "multi", "code_multi_languages": "java", "code_min_tokens": 3, "code_timeout_seconds": 30}}, FakeDatabase([], artifacts), storage)

    score, current_files, history_files = pipeline._run_code_jplag(jar, artifacts["current"], [artifacts["history"]])

    assert current_files == history_files == 1
    assert 0.5 <= score <= 1


def test_local_jplag_batched_code_reports(tmp_path):
    jar = Path(__file__).parents[1] / "vendor" / "jplag" / "jplag.jar"
    if not jar.exists():
        return
    first = Report("first", "u1", "张三", "d1", "研发部", "2026-08-01", "组长", "第一份代码", "内容")
    second = Report("second", "u1", "张三", "d1", "研发部", "2026-08-02", "组长", "第二份代码", "内容")
    artifacts = {
        "first": [{"artifact_type": "code", "original_filename": "first.py", "stored_path": "first.py"}],
        "second": [{"artifact_type": "code", "original_filename": "second.py", "stored_path": "second.py"}],
    }
    pipeline = DailyReportPipeline({"jplag": {"java_command": "java", "jar_path": str(jar.resolve()), "code_language": "multi", "code_multi_languages": "python3", "code_min_tokens": 3, "code_timeout_seconds": 60}}, FakeDatabase([first, second], artifacts), FakeStorage({"first.py": b"def add(a, b): return a + b", "second.py": b"def add(a, b): return a + b"}))

    results = pipeline._run_batched_code_jplag(jar.resolve(), [first, second], [first, second], 30)

    assert results["second"].risk_level == "high"
    assert results["second"].findings[0].details["execution"] == "batched"

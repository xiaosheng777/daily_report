import json
from http.server import ThreadingHTTPServer
from threading import Thread
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

from src.db.sqlite_backend import SQLiteBackend
from src.storage.file_store import FileStorage
from src.utils.runtime_log import runtime_log
from src.web.app import DailyReportHandler, SESSION_COOKIE


def _open(url: str, session_id: str):
    request = Request(url, headers={"Cookie": f"{SESSION_COOKIE}={session_id}", "User-Agent": "runtime-log-test"})
    return build_opener(ProxyHandler({})).open(request, timeout=5)


def _get(url: str, session_id: str):
    try:
        with _open(url, session_id) as response:
            return response.status, response.headers, response.read()
    except HTTPError as exc:
        return exc.code, exc.headers, exc.read()


def test_runtime_logs_redact_secrets_filter_and_prune(tmp_path):
    db = SQLiteBackend(tmp_path / "runtime.sqlite3")
    db.initialize(False)
    try:
        raise RuntimeError("api_key='do-not-store' Bearer another-secret")
    except RuntimeError as exc:
        runtime_log(db, "task", "failed", "任务失败 password=plain-text", level="error", task_id="T_1", context={"api_key": "hidden", "stage": "checking"}, exc=exc)

    with db.connect() as conn:
        conn.execute("insert into runtime_logs(occurred_at,level,source,event,message) values ('2000-01-01T00:00:00','info','web','old','old')")
    db.append_runtime_log({"source": "web", "event": "alive", "message": "still running"})

    result = db.list_runtime_logs({"source": "task", "task_id": "T_1"})
    assert len(result["items"]) == 1
    row = result["items"][0]
    serialized = json.dumps(row, ensure_ascii=False)
    assert "do-not-store" not in serialized
    assert "another-secret" not in serialized
    assert "plain-text" not in serialized
    assert row["context"]["api_key"] == "[redacted]"
    assert "[redacted]" in row["traceback"]
    assert all(item["event"] != "old" for item in db.list_runtime_logs({})["items"])


def test_runtime_log_api_is_admin_only_and_sse_resumes(tmp_path):
    db = SQLiteBackend(tmp_path / "runtime-api.sqlite3")
    db.initialize(False)
    db.create_department({"department_id": "d1", "name": "研发部"})
    db.create_group({"group_id": "g1", "department_id": "d1", "name": "一组"})
    admin = db.create_user({"user_id": "admin", "username": "admin", "password": "safe-password", "display_name": "管理员", "role": "admin"})
    employee = db.create_user({"user_id": "employee", "username": "employee", "password": "safe-password", "display_name": "员工", "role": "employee", "department_id": "d1", "group_id": "g1"})
    admin_session, employee_session = db.create_session(admin["user_id"]), db.create_session(employee["user_id"])
    first = db.append_runtime_log({"source": "worker", "event": "started", "message": "Worker started"})
    second = db.append_runtime_log({"source": "task", "event": "stage_changed", "message": "processing", "task_id": "T_1"})

    config = {"app": {"frontend_dir": str(tmp_path)}, "storage": {"root_dir": str(tmp_path / "storage")}, "upload": {"max_file_mb": 5}}
    DailyReportHandler.config, DailyReportHandler.db, DailyReportHandler.storage = config, db, FileStorage(config)
    server = ThreadingHTTPServer(("127.0.0.1", 0), DailyReportHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        status, _, body = _get(base + "/api/admin/runtime-logs", employee_session)
        assert status == 403
        assert json.loads(body)["error"] == "forbidden"

        status, _, body = _get(base + "/api/admin/runtime-logs?source=task", admin_session)
        assert status == 200
        payload = json.loads(body)
        assert [item["seq"] for item in payload["items"]] == [second["seq"]]
        assert payload["latest_seq"] == second["seq"]

        with _open(base + f"/api/admin/runtime-logs/stream?after={first['seq']}", admin_session) as response:
            assert response.headers.get_content_type() == "text/event-stream"
            lines = [response.readline().decode("utf-8").strip() for _ in range(3)]
        assert lines[0] == f"id: {second['seq']}"
        assert lines[1] == "event: runtime_log"
        streamed = json.loads(lines[2].removeprefix("data: "))
        assert streamed["seq"] == second["seq"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

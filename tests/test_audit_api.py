import json
from http.server import ThreadingHTTPServer
from threading import Thread
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

from src.db.sqlite_backend import SQLiteBackend
from src.storage.file_store import FileStorage
from src.web.app import DailyReportHandler, SESSION_COOKIE


def _get(url: str, session_id: str):
    request = Request(url, headers={"Cookie": f"{SESSION_COOKIE}={session_id}", "User-Agent": "audit-api-test"})
    opener = build_opener(ProxyHandler({}))
    try:
        with opener.open(request, timeout=5) as response:
            return response.status, response.headers, response.read()
    except HTTPError as exc:
        return exc.code, exc.headers, exc.read()


def _post(url: str, payload: dict, session_id: str = ""):
    headers = {"Content-Type": "application/json", "User-Agent": "audit-api-test"}
    if session_id:
        headers["Cookie"] = f"{SESSION_COOKIE}={session_id}"
    request = Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    opener = build_opener(ProxyHandler({}))
    try:
        with opener.open(request, timeout=5) as response:
            return response.status, response.headers, response.read()
    except HTTPError as exc:
        return exc.code, exc.headers, exc.read()


def test_audit_api_is_admin_only_and_csv_export_is_itself_audited(tmp_path):
    db = SQLiteBackend(tmp_path / "audit-api.sqlite3")
    db.initialize(False)
    db.create_department({"department_id": "d1", "name": "研发部"})
    db.create_group({"group_id": "g1", "department_id": "d1", "name": "一组"})
    admin = db.create_user({"user_id": "admin-1", "username": "audit_admin", "password": "safe-test-password", "display_name": "审计管理员", "role": "admin"})
    employee = db.create_user({"user_id": "employee-1", "username": "employee", "password": "safe-test-password", "display_name": "普通员工", "role": "employee", "department_id": "d1", "group_id": "g1"})
    admin_session = db.create_session(admin["user_id"])
    employee_session = db.create_session(employee["user_id"])

    config = {"app": {"frontend_dir": str(tmp_path)}, "storage": {"root_dir": str(tmp_path / "storage")}, "upload": {"max_file_mb": 5}}
    DailyReportHandler.config = config
    DailyReportHandler.db = db
    DailyReportHandler.storage = FileStorage(config)
    server = ThreadingHTTPServer(("127.0.0.1", 0), DailyReportHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        status, _, _ = _post(base + "/api/auth/login", {"username": "missing-user", "password": "never-store-this"})
        assert status == 401
        status, headers, _ = _post(base + "/api/auth/login", {"username": "employee", "password": "safe-test-password"})
        assert status == 200
        login_session = headers["Set-Cookie"].split(";", 1)[0].split("=", 1)[1]
        status, _, _ = _post(base + "/api/auth/logout", {}, login_session)
        assert status == 200

        status, _, body = _get(base + "/api/admin/audit-logs", employee_session)
        assert status == 403
        assert json.loads(body)["error"] == "forbidden"

        status, _, body = _get(base + "/api/admin/audit-logs?limit=50", admin_session)
        assert status == 200
        payload = json.loads(body)
        assert payload["stats"]["failed"] == 2
        assert payload["stats"]["login_failed"] == 1
        denied = next(row for row in payload["items"] if row["action"] == "security.permission_denied")
        assert denied["actor_username"] == "employee"
        login_failure = next(row for row in payload["items"] if row["action"] == "auth.login" and row["outcome"] == "failure")
        assert login_failure["actor_username"] == "missing-user"
        assert login_failure["actor_user_id"] == ""
        assert b"never-store-this" not in body
        assert b"safe-test-password" not in body

        status, headers, body = _get(base + "/api/admin/audit-logs.csv", admin_session)
        assert status == 200
        assert headers.get_content_type() == "text/csv"
        assert body.startswith(b"\xef\xbb\xbf")

        _, _, body = _get(base + "/api/admin/audit-logs", admin_session)
        payload = json.loads(body)
        assert any(row["action"] == "audit.export" and row["outcome"] == "success" for row in payload["items"])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

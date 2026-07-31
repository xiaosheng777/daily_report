from __future__ import annotations

import argparse
import json
import mimetypes
import shutil
from datetime import datetime, timedelta
from email.parser import BytesParser
from email.policy import default
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

from src.core.docx_io import build_report_docx
from src.core.pipeline import DailyReportPipeline
from src.core.schemas import Report
from src.db.factory import create_database
from src.db.sqlite_backend import (
    BUSINESS_ROLES,
    ROLE_DIRECTOR,
    ROLE_GROUP_LEADER,
    ROLE_MINISTER,
    can_admin,
    can_manage,
)
from src.llm.factory import create_llm_judge
from src.storage.file_store import FileStorage
from src.utils.config import default_config_path, load_config
from src.utils.dates import now_iso, parse_date, today_iso, ym
from src.utils.security import token

SESSION_COOKIE = "daily_report_session"
# 测试模式：允许按表单日期补录日报。正式启用时移除 date 并恢复 today_iso()。
REQUIRED_REPORT_FIELDS = ("date", "title_summary", "work_description")
RESET_CONFIRMATION_PHRASE = "清除全部业务数据"


def attachment_header(download_name: str) -> str:
    safe_name = Path(download_name).name.replace('"', "")
    ascii_name = safe_name.encode("ascii", "ignore").decode("ascii") or "download"
    return f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{quote(safe_name, safe="")}'


def flatten_settings(config: dict) -> dict:
    dd = config.get("daily_duplicate", {})
    weights = dd.get("score_weights", {})
    checks = config.get("checks", {})
    llm = config.get("llm_judge", {})
    tc = config.get("testcase_verification", {})
    return {
        "daily_duplicate.llm_candidate_top_n": int(dd.get("llm_candidate_top_n", 3)),
        "daily_duplicate.llm_candidate_score_threshold": float(dd.get("llm_candidate_score_threshold", 0.72)),
        "daily_duplicate.low_info_candidate_score_threshold": float(dd.get("low_info_candidate_score_threshold", 0.82)),
        "daily_duplicate.max_candidates": int(dd.get("max_candidates", 20)),
        "daily_duplicate.self_history_days": int(dd.get("self_history_days", 30)),
        "daily_duplicate.cross_user_days": int(dd.get("cross_user_days", 3)),
        "daily_duplicate.cross_user_scope": str(dd.get("cross_user_scope", "department")),
        "daily_duplicate.score_weights.text": float(weights.get("text", 0.45)),
        "daily_duplicate.score_weights.semantic": float(weights.get("semantic", 0.45)),
        "daily_duplicate.score_weights.recency": float(weights.get("recency", 0.10)),
        "llm_judge.enabled": bool(llm.get("enabled", False)),
        "llm_judge.base_url": str(llm.get("base_url", "")),
        "llm_judge.chat_path": str(llm.get("chat_path", "/v1/chat/completions")),
        "llm_judge.model": str(llm.get("model", "")),
        "llm_judge.api_key_file": str(llm.get("api_key_file", "")),
        "llm_judge.api_key_env": str(llm.get("api_key_env", "DAILY_REPORT_LLM_API_KEY")),
        "testcase_verification.enabled": bool(tc.get("enabled", True)),
        "testcase_verification.self_history_days": int(tc.get("self_history_days", 7)),
        "testcase_verification.self_history_high_rate": float(tc.get("self_history_high_rate", 0.50)),
        "testcase_verification.self_history_medium_rate": float(tc.get("self_history_medium_rate", 0.20)),
        "checks.enable_code_check": bool(checks.get("enable_code_check", True)),
        "checks.enable_document_check": bool(checks.get("enable_document_check", True)),
        "upload.max_file_mb": int(config.get("upload", {}).get("max_file_mb", 50)),
    }


def apply_flat_settings(config: dict, settings: dict) -> dict:
    merged = json.loads(json.dumps(config, ensure_ascii=False))
    for key, value in settings.items():
        target = merged
        parts = key.split(".")
        for part in parts[:-1]:
            target = target.setdefault(part, {})
        target[parts[-1]] = value
    return merged


class DailyReportHandler(BaseHTTPRequestHandler):
    config: dict = {}
    db = None
    storage: FileStorage | None = None

    def _cookie_session_id(self) -> str:
        raw = self.headers.get("Cookie", "")
        for part in raw.split(";"):
            if "=" in part:
                k, v = part.strip().split("=", 1)
                if k == SESSION_COOKIE:
                    return v
        return ""

    def _current_user(self):
        return self.db.user_by_session(self._cookie_session_id())

    def _require_user(self):
        user = self._current_user()
        if not user:
            self._send_json({"ok": False, "error": "authentication_required"}, HTTPStatus.UNAUTHORIZED)
            return None
        return user

    def _require_manager(self):
        user = self._require_user()
        if user and not can_manage(user):
            self._send_json({"ok": False, "error": "forbidden"}, HTTPStatus.FORBIDDEN)
            return None
        return user

    def _require_business_user(self):
        user = self._require_user()
        if user and user.role not in BUSINESS_ROLES:
            self._send_json({"ok": False, "error": "forbidden"}, HTTPStatus.FORBIDDEN)
            return None
        return user

    def _require_admin(self):
        user = self._require_user()
        if user and not can_admin(user):
            self._send_json({"ok": False, "error": "forbidden"}, HTTPStatus.FORBIDDEN)
            return None
        return user

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query)
        try:
            if path == "/api/auth/me":
                user = self._current_user()
                self._send_json({"authenticated": bool(user), "user": user.to_dict() if user else None})
            elif path == "/api/public/departments":
                self._send_json(self.db.list_departments(public_only=True))
            elif path == "/api/public/groups":
                self._send_json(self.db.list_groups((query.get("department_id") or [""])[0], public_only=True))
            elif path == "/api/my/reports":
                user = self._require_business_user()
                if user:
                    filters = {k: (query.get(k) or [""])[0] for k in ("start", "end", "name", "role")}
                    reports = self.db.list_reports(user, filters["start"], filters["end"], True, filters["name"], filters["role"])
                    self._send_json([self._report_summary(r, user) for r in reports])
            elif path.startswith("/api/my/reports/") and path.endswith("/download"):
                user = self._require_business_user(); rid = path.split("/")[4]
                if user:
                    report = self.db.get_report(rid, user)
                    if not report: return self._send_json({"ok": False, "error": "not_found"}, HTTPStatus.NOT_FOUND)
                    self._send_bytes(self.storage.read(report.stored_path), "application/vnd.openxmlformats-officedocument.wordprocessingml.document", report.original_filename or "report.docx")
            elif path.startswith("/api/my/reports/"):
                user = self._require_business_user(); rid = path.split("/")[4]
                if user:
                    report = self.db.get_report(rid, user)
                    if not report or report.owner_user_id != user.user_id:
                        return self._send_json({"ok": False, "error": "not_found"}, HTTPStatus.NOT_FOUND)
                    self._send_json(self._report_detail(report))
            elif path.startswith("/api/artifacts/") and path.endswith("/download"):
                user = self._require_user(); aid = path.split("/")[3]
                if user:
                    art = self.db.get_artifact(aid, user)
                    if not art: return self._send_json({"ok": False, "error": "not_found"}, HTTPStatus.NOT_FOUND)
                    self._send_bytes(self.storage.read(art["stored_path"]), art.get("content_type") or "application/octet-stream", art.get("original_filename") or "artifact.bin")
            elif path == "/api/status":
                user = self._require_manager()
                if user:
                    tasks = self.db.list_tasks(user)
                    latest = tasks[0] if tasks else {}
                    s = latest.get("summary", {}) if latest else {}
                    self._send_json({"latest_task": latest, "result_count": s.get("report_count", 0), "high_risk_count": s.get("high_risk_count", 0), "error_count": 1 if latest.get("status") == "failed" else 0, "metadata": {"report_count": len(self.db.list_reports(user))}})
            elif path == "/api/results":
                user = self._require_manager()
                if user:
                    tasks = self.db.list_tasks(user)
                    if not tasks: return self._send_json([])
                    task = self.db.get_task(tasks[0]["task_id"], user)
                    self._send_json((task or {}).get("result", {}).get("report_cases", []))
            elif path == "/api/tasks":
                user = self._require_manager()
                if user:
                    include_hidden = (query.get("include_hidden") or ["0"])[0] == "1"
                    self._send_json(self.db.list_tasks(user, {"status": (query.get("status") or [""])[0], "start": (query.get("start") or [""])[0], "end": (query.get("end") or [""])[0]}, include_hidden=include_hidden))
            elif path.startswith("/api/tasks/") and path.endswith("/results"):
                user = self._require_manager(); tid = path.split("/")[3]
                if user:
                    task = self.db.get_task(tid, user)
                    if not task: return self._send_json({"ok": False, "error": "not_found"}, HTTPStatus.NOT_FOUND)
                    self._send_json(task.get("result", {}).get("report_cases", []))
            elif path.startswith("/api/tasks/") and path.endswith("/missing-reports"):
                user = self._require_manager(); tid = path.split("/")[3]
                if user:
                    task = self.db.get_task(tid, user)
                    if not task: return self._send_json({"ok": False, "error": "not_found"}, HTTPStatus.NOT_FOUND)
                    self._send_json(task.get("result", {}).get("missing_reports", []))
            elif path.startswith("/api/tasks/") and path.endswith("/export.xlsx"):
                self._send_task_file(path.split("/")[3], "review_cases.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            elif path.startswith("/api/tasks/") and path.endswith("/export.docx"):
                self._send_task_file(path.split("/")[3], "review_cases.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
            elif path.startswith("/api/tasks/") and path.endswith("/missing-reports.xlsx"):
                self._send_task_file(path.split("/")[3], "missing_reports.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            elif path == "/api/testcase-snapshots":
                user = self._require_user()
                if user: self._send_json(self.db.list_testcase_snapshots(user))
            elif path.startswith("/api/testcase-snapshots/") and path.endswith("/download"):
                user = self._require_user(); sid = path.split("/")[3]
                if user:
                    snap = self.db.get_testcase_snapshot(sid, user)
                    if not snap: return self._send_json({"ok": False, "error": "not_found"}, HTTPStatus.NOT_FOUND)
                    self._send_bytes(self.storage.read(snap["stored_path"]), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", snap["original_filename"])
            elif path == "/api/export/reports.docx":
                user = self._require_business_user()
                if user:
                    start, end = (query.get("start") or [""])[0], (query.get("end") or [""])[0]
                    name, role = (query.get("name") or [""])[0], (query.get("role") or [""])[0]
                    body = _build_reports_docx(self.db.list_reports(user, start, end, True, name, role))
                    stored = self.storage.save_bytes("exports/weekly_reports", f"weekly_{start or 'all'}_{end or 'all'}.docx", body, end or start or None)
                    self.db.save_export({"export_type": "weekly_reports", "created_by": user.user_id, "start_date": start, "end_date": end, "stored_path": stored.stored_path})
                    self._send_bytes(body, "application/vnd.openxmlformats-officedocument.wordprocessingml.document", stored.original_filename)
            elif path in {"/api/export/periodic.docx", "/api/export/periodic.xlsx"}:
                user = self._require_business_user()
                if user:
                    report_type, start, end = _periodic_export_args(query)
                    reports = self.db.list_reports(user, start, end)
                    artifacts = {report.report_id: self.db.list_artifacts_for_report(report.report_id) for report in reports}
                    missing_reports = _periodic_missing_reports(self.db, user, start, end, reports)
                    title = f"{'周报' if report_type == 'weekly' else '月报'}汇总"
                    scope = _export_scope_label(user)
                    if path.endswith(".docx"):
                        body = _build_periodic_reports_docx(title, scope, start, end, reports, artifacts, missing_reports)
                        filename = f"{report_type}_{start}_{end}_{scope}.docx"
                        content_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                    else:
                        body = _build_periodic_reports_xlsx(title, scope, start, end, reports, artifacts, missing_reports)
                        filename = f"{report_type}_{start}_{end}_{scope}.xlsx"
                        content_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                    stored = self.storage.save_bytes("exports/periodic_reports", filename, body, end)
                    self.db.save_export({"export_type": f"{report_type}_{'docx' if path.endswith('.docx') else 'xlsx'}", "created_by": user.user_id, "department_scope": user.department_id, "start_date": start, "end_date": end, "stored_path": stored.stored_path})
                    self._send_bytes(body, content_type, stored.original_filename)
            elif path == "/api/users":
                user = self._require_admin();
                if user: self._send_json(self.db.list_users())
            elif path == "/api/departments":
                user = self._require_admin();
                if user: self._send_json(self.db.list_departments())
            elif path == "/api/groups":
                user = self._require_admin()
                if user: self._send_json(self.db.list_groups((query.get("department_id") or [""])[0]))
            elif path == "/api/admin/settings":
                user = self._require_admin();
                if user: self._send_json(self.db.get_settings(flatten_settings(self.config)))
            elif path == "/api/admin/storage-stats":
                user = self._require_admin();
                if user: self._send_json(self._storage_stats())
            elif path == "/" or path == "/index.html":
                self._send_file(Path(self.config["app"]["frontend_dir"]) / "index.html")
            else:
                frontend = Path(self.config["app"]["frontend_dir"]).resolve()
                static_path = (frontend / path.lstrip("/")).resolve()
                if frontend in static_path.parents and static_path.exists() and static_path.is_file(): self._send_file(static_path)
                else: self.send_error(HTTPStatus.NOT_FOUND, "Not found")
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self) -> None:
        path = unquote(urlparse(self.path).path)
        try:
            if path == "/api/auth/login":
                payload = self._read_json_body(); user = self.db.authenticate(str(payload.get("username") or ""), str(payload.get("password") or ""))
                if not user: return self._send_json({"ok": False, "error": "invalid_credentials"}, HTTPStatus.UNAUTHORIZED)
                sid = self.db.create_session(user.user_id); secure = "; Secure" if self.config.get("app", {}).get("secure_cookie") else ""
                self._send_json({"ok": True, "user": user.to_dict()}, headers={"Set-Cookie": f"{SESSION_COOKIE}={sid}; Path=/; HttpOnly; SameSite=Lax{secure}"})
            elif path == "/api/auth/logout":
                self.db.delete_session(self._cookie_session_id()); self._send_json({"ok": True}, headers={"Set-Cookie": f"{SESSION_COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"})
            elif path == "/api/auth/register":
                if not self.config.get("app", {}).get("allow_self_register", True): return self._send_json({"ok": False, "error": "registration_disabled"}, HTTPStatus.FORBIDDEN)
                payload = self._read_json_body(); payload["role"] = "employee"; self._send_json({"ok": True, **self.db.create_user(payload)}, HTTPStatus.CREATED)
            elif path == "/api/reports":
                user = self._require_business_user()
                if user:
                    fields, files = self._read_multipart_or_json()
                    payload = json.loads(fields.get("payload", "{}")) if "payload" in fields else fields
                    testcase_files = files.get("testcase_files", [])
                    if testcase_files:
                        payload["testcase_items"] = self._parse_testcase_upload(testcase_files[0])
                    report = self._report_from_payload(user, payload)
                    self.db.save_report(report)
                    for f in files.get("code_files", []): self._save_artifact(report, user, f, "code")
                    for f in files.get("document_files", []): self._save_artifact(report, user, f, "document")
                    for f in testcase_files: self._save_artifact(report, user, f, "testcase")
                    self._send_json({"ok": True, "report_id": report.report_id, "file": report.original_filename}, HTTPStatus.CREATED)
            elif path == "/api/testcase-snapshots":
                user = self._require_manager()
                if user:
                    if user.role not in {ROLE_GROUP_LEADER, ROLE_DIRECTOR}:
                        return self._send_json({"ok": False, "error": "only_group_leader_or_director_uploads_snapshot"}, HTTPStatus.FORBIDDEN)
                    fields, files = self._read_multipart_or_json()
                    snapshot_date = parse_date(fields.get("snapshot_date", ""))
                    file_items = files.get("snapshot_file", [])
                    if not file_items: raise ValueError("missing snapshot_file")
                    f = file_items[0]
                    group_id, group_name, department_id, department_name = self._resolve_snapshot_group(user, fields)
                    # Validate sheet before saving.
                    from src.core.testcase_excel import load_daily_sheet_rows
                    load_daily_sheet_rows(f.data)
                    stored = self.storage.save_bytes(f"testcase_snapshots/{group_id}", f.filename, f.data, snapshot_date)
                    sid = self.db.save_testcase_snapshot({
                        "department_id": department_id,
                        "department_name": department_name,
                        "group_id": group_id,
                        "group_name": group_name,
                        "snapshot_date": snapshot_date,
                        "original_filename": f.filename,
                        "stored_path": stored.stored_path,
                        "file_size": stored.file_size,
                        "content_hash": stored.content_hash,
                        "uploaded_by": user.user_id,
                    })
                    self._send_json({"ok": True, "snapshot_id": sid}, HTTPStatus.CREATED)
            elif path in {"/api/run", "/api/tasks"}:
                user = self._require_manager()
                if user:
                    payload = {"report_date_start": today_iso(), "report_date_end": today_iso()} if path == "/api/run" else self._read_json_body()
                    start, end = str(payload.get("report_date_start") or ""), str(payload.get("report_date_end") or "")
                    if not start or not end:
                        raise ValueError("查重任务必须选择开始日期和结束日期")
                    if parse_date(start) > parse_date(end):
                        raise ValueError("开始日期不能晚于结束日期")
                    cfg = apply_flat_settings(self.config, self.db.get_settings(flatten_settings(self.config)))
                    rel_dir = f"tasks/pending_{token('', 8)}"
                    result_dir = str(Path(cfg["storage"]["root_dir"]) / rel_dir)
                    task_id = self.db.create_task(user, payload, cfg, rel_dir)
                    try:
                        final_dir = Path(cfg["storage"]["root_dir"]) / "tasks" / task_id
                        result = DailyReportPipeline(cfg, self.db, self.storage, create_llm_judge(cfg)).run(user, payload, task_id, str(final_dir))
                        self.db.finish_task(task_id, "finished", result)
                        task = self.db.get_task(task_id, user)
                        self._send_json({"ok": True, "task_id": task_id, "task_name": (task or {}).get("task_name", task_id), "summary": result.get("summary", {})}, HTTPStatus.CREATED)
                    except Exception as exc:
                        err_path = Path(cfg["storage"]["root_dir"]) / "tasks" / task_id / "errors.json"
                        err_path.parent.mkdir(parents=True, exist_ok=True)
                        err_path.write_text(json.dumps({"error": str(exc)}, ensure_ascii=False, indent=2), encoding="utf-8")
                        self.db.finish_task(task_id, "failed", {}, str(exc)); raise
            elif path == "/api/admin/test-llm":
                user = self._require_admin()
                if user:
                    cfg = apply_flat_settings(self.config, self.db.get_settings(flatten_settings(self.config)))
                    judge = create_llm_judge(cfg)
                    self._send_json(judge.health_check() if judge else {"ok": False, "error": "llm disabled"})
            elif path == "/api/admin/cleanup-sessions":
                user = self._require_admin();
                if user: self._send_json({"ok": True, "deleted": self.db.cleanup_sessions()})
            elif path == "/api/admin/backup":
                user = self._require_admin();
                if user: self._send_json(self._create_backup(user.user_id))
            elif path == "/api/tasks/display/clear":
                user = self._require_manager()
                if user: self.db.clear_task_page_display(user); self._send_json({"ok": True})
            elif path == "/api/admin/reset/prepare":
                user = self._require_admin()
                if user:
                    self._send_json({"ok": True, "confirmation_id": self.db.create_reset_confirmation(user.user_id), "confirmation_phrase": RESET_CONFIRMATION_PHRASE})
            elif path == "/api/admin/reset/confirm":
                user = self._require_admin()
                if user:
                    payload = self._read_json_body()
                    if str(payload.get("confirmation_phrase") or "") != RESET_CONFIRMATION_PHRASE:
                        raise ValueError("confirmation phrase does not match")
                    if not self.db.consume_reset_confirmation(str(payload.get("confirmation_id") or ""), user.user_id):
                        raise ValueError("confirmation expired or invalid")
                    counts = self.db.clear_business_data()
                    removed = self._clear_business_storage()
                    self._send_json({"ok": True, "deleted": counts, "removed_files": removed}, headers={"Set-Cookie": f"{SESSION_COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"})
            elif path == "/api/users":
                user = self._require_admin();
                if user: self._send_json({"ok": True, **self.db.create_user(self._read_json_body())}, HTTPStatus.CREATED)
            elif path == "/api/departments":
                user = self._require_admin();
                if user: self._send_json({"ok": True, "department": self.db.create_department(self._read_json_body())}, HTTPStatus.CREATED)
            elif path == "/api/groups":
                user = self._require_admin()
                if user: self._send_json({"ok": True, "group": self.db.create_group(self._read_json_body())}, HTTPStatus.CREATED)
            elif path == "/api/admin/settings":
                user = self._require_admin()
                if user:
                    payload = self._read_json_body()
                    self._validate_settings_payload(payload)
                    self.db.update_settings(payload)
                    self._send_json({"ok": True})
            else:
                self.send_error(HTTPStatus.NOT_FOUND, "Not found")
        except ValueError as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_PUT(self) -> None:
        path = unquote(urlparse(self.path).path)
        try:
            if path.startswith("/api/users/"):
                user = self._require_admin();
                if user: self.db.update_user(path.split("/")[3], self._read_json_body()); self._send_json({"ok": True})
            elif path.startswith("/api/departments/"):
                user = self._require_admin();
                if user: self.db.update_department(path.split("/")[3], self._read_json_body()); self._send_json({"ok": True})
            elif path.startswith("/api/groups/"):
                user = self._require_admin()
                if user: self.db.update_group(path.split("/")[3], self._read_json_body()); self._send_json({"ok": True})
            elif path.startswith("/api/my/reports/"):
                user = self._require_business_user(); rid = path.split("/")[4]
                if user:
                    old = self.db.get_report(rid, user)
                    if not old or old.owner_user_id != user.user_id: raise ValueError("日报不存在或无权更改")
                    fields, files = self._read_multipart_or_json()
                    payload = json.loads(fields.get("payload", "{}")) if "payload" in fields else fields
                    testcase_files = files.get("testcase_files", [])
                    if testcase_files:
                        payload["testcase_items"] = self._parse_testcase_upload(testcase_files[0])
                    report = self._report_from_payload(user, payload, old.report_date)
                    report.report_id, report.created_at, report.updated_at = rid, old.created_at, now_iso()
                    self.db.save_report(report)
                    for f in files.get("code_files", []): self._save_artifact(report, user, f, "code")
                    for f in files.get("document_files", []): self._save_artifact(report, user, f, "document")
                    for f in testcase_files: self._save_artifact(report, user, f, "testcase")
                    self._send_json({"ok": True})
            elif path == "/api/admin/settings":
                user = self._require_admin()
                if user:
                    payload = self._read_json_body()
                    self._validate_settings_payload(payload)
                    self.db.update_settings(payload)
                    self._send_json({"ok": True})
            else: self.send_error(HTTPStatus.NOT_FOUND, "Not found")
        except ValueError as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc: self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_DELETE(self) -> None:
        path = unquote(urlparse(self.path).path)
        try:
            if path.startswith("/api/users/"):
                user = self._require_admin();
                if user: self.db.delete_user(path.split("/")[3]); self._send_json({"ok": True})
            elif path.startswith("/api/departments/"):
                user = self._require_admin();
                if user: self.db.delete_department(path.split("/")[3]); self._send_json({"ok": True})
            elif path.startswith("/api/groups/"):
                user = self._require_admin()
                if user: self.db.delete_group(path.split("/")[3]); self._send_json({"ok": True})
            elif path.startswith("/api/tasks/"):
                user = self._require_manager();
                if user:
                    task_id = path.split("/")[3]
                    self.db.delete_task(task_id, user)
                    task_dir = (Path(self.config["storage"]["root_dir"]) / "tasks" / task_id).resolve()
                    storage_root = Path(self.config["storage"]["root_dir"]).resolve()
                    if storage_root in task_dir.parents and task_dir.exists(): shutil.rmtree(task_dir)
                    self._send_json({"ok": True})
            else: self.send_error(HTTPStatus.NOT_FOUND, "Not found")
        except Exception as exc: self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def _send_task_file(self, task_id: str, name: str, content_type: str) -> None:
        user = self._require_manager()
        if not user: return
        task = self.db.get_task(task_id, user)
        if not task: return self._send_json({"ok": False, "error": "not_found"}, HTTPStatus.NOT_FOUND)
        path = Path(self.config["storage"]["root_dir"]) / "tasks" / task_id / name
        if not path.exists(): return self._send_json({"ok": False, "error": "file_not_found"}, HTTPStatus.NOT_FOUND)
        self._send_bytes(path.read_bytes(), content_type, name)

    @staticmethod
    def _validate_settings_payload(payload: dict) -> None:
        medium = payload.get("testcase_verification.self_history_medium_rate")
        high = payload.get("testcase_verification.self_history_high_rate")
        if medium is None and high is None:
            return
        medium_v = float(medium if medium is not None else 0.20)
        high_v = float(high if high is not None else 0.50)
        if not (0 <= medium_v <= high_v <= 1):
            raise ValueError("测试用例查重阈值需满足 0 ≤ 中风险 ≤ 高风险 ≤ 1")

    def _parse_testcase_upload(self, file_item) -> list[dict]:
        from src.core.testcase_excel import load_daily_sheet_rows, rows_to_items
        name = str(getattr(file_item, "filename", "") or "").lower()
        if not name.endswith(".xlsx"):
            raise ValueError("测试用例文件须为 .xlsx")
        rows = load_daily_sheet_rows(file_item.data)
        if not rows:
            raise ValueError("日报 sheet 中没有有效测试用例")
        return rows_to_items(rows)

    def _resolve_snapshot_group(self, user, fields: dict) -> tuple[str, str, str, str]:
        if user.role == ROLE_GROUP_LEADER:
            if not user.group_id:
                raise ValueError("组长未绑定小组，无法上传汇总表")
            return user.group_id, user.group_name, user.department_id, user.department_name
        group_id = str(fields.get("group_id") or "").strip()
        if not group_id:
            raise ValueError("请选择小组")
        groups = self.db.list_groups(user.department_id)
        match = next((g for g in groups if g.get("group_id") == group_id), None)
        if not match:
            raise ValueError("小组不存在或不在本部门范围内")
        return match["group_id"], match.get("name", ""), user.department_id, user.department_name

    def _report_from_payload(self, user, payload: dict, report_date: str | None = None) -> Report:
        missing = [f for f in REQUIRED_REPORT_FIELDS if not str(payload.get(f, "")).strip()]
        if missing: raise ValueError(f"缺少必填字段: {', '.join(missing)}")
        derived_manager = self.db.get_derived_manager(user)
        manager = derived_manager.display_name if derived_manager else str(payload.get("manager") or "").strip()
        if user.role != ROLE_MINISTER and not manager:
            raise ValueError("直属领导尚未建立，请手工填写")
        # 测试模式：新日报采用用户选择的日期；编辑原日报时仍保留原日期。
        # 正式启用时改回：report_date or today_iso()
        report = Report("", user.user_id, user.display_name, user.department_id, user.department_name,
                        report_date or parse_date(str(payload["date"])), manager, str(payload.get("title_summary") or ""),
                        str(payload.get("work_description") or ""), code_items=payload.get("code_items") or [],
                        document_items=payload.get("document_items") or [], testcase_items=payload.get("testcase_items") or [],
                        created_at=now_iso(), group_id=user.group_id, group_name=user.group_name,
                        submitter_role=user.role)
        from src.core.pipeline import DailyReportPipeline
        report.report_id = DailyReportPipeline.build_report_id(report)
        body = build_report_docx(_report_to_payload(report))
        stored = self.storage.save_bytes("submitted_reports", f"SR_{report.report_date}_{report.employee_name}.docx", body, report.report_date)
        report.original_filename, report.stored_path, report.file_size, report.content_hash = stored.original_filename, stored.stored_path, stored.file_size, stored.content_hash
        return report

    def _save_artifact(self, report: Report, user, file_item, artifact_type: str) -> str:
        stored = self.storage.save_bytes(f"artifacts/{artifact_type}", file_item.filename, file_item.data, report.report_date)
        return self.db.save_artifact({"report_id": report.report_id, "owner_user_id": user.user_id, "department_id": user.department_id, "artifact_type": artifact_type, "original_filename": file_item.filename, "stored_path": stored.stored_path, "content_type": file_item.content_type, "file_size": stored.file_size, "content_hash": stored.content_hash})

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0")); raw = self.rfile.read(length).decode("utf-8")
        return json.loads(raw or "{}")

    def _read_multipart_or_json(self):
        ctype = self.headers.get("Content-Type", "")
        if not ctype.startswith("multipart/form-data"):
            return self._read_json_body(), {}
        length = int(self.headers.get("Content-Length", "0")); body = self.rfile.read(length)
        msg = BytesParser(policy=default).parsebytes(f"Content-Type: {ctype}\r\n\r\n".encode() + body)
        fields, files = {}, {}
        for part in msg.iter_parts():
            name = part.get_param("name", header="content-disposition")
            filename = part.get_filename()
            data = part.get_payload(decode=True) or b""
            if filename:
                files.setdefault(name, []).append(type("Upload", (), {"filename": filename, "data": data, "content_type": part.get_content_type()}))
            else:
                fields[name] = data.decode(part.get_content_charset() or "utf-8")
        return fields, files

    def _send_json(self, data, status: HTTPStatus = HTTPStatus.OK, headers: dict[str, str] | None = None) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status); self.send_header("Content-Type", "application/json; charset=utf-8"); self.send_header("Content-Length", str(len(body))); self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items(): self.send_header(k, v)
        self.end_headers(); self.wfile.write(body)

    def _send_file(self, path: Path, download_name: str | None = None) -> None:
        self._send_bytes(path.read_bytes(), mimetypes.guess_type(path.name)[0] or "application/octet-stream", download_name)

    def _send_bytes(self, body: bytes, content_type: str, download_name: str | None = None) -> None:
        self.send_response(HTTPStatus.OK); self.send_header("Content-Type", content_type); self.send_header("Content-Length", str(len(body))); self.send_header("Cache-Control", "no-store")
        if download_name: self.send_header("Content-Disposition", attachment_header(download_name))
        self.end_headers(); self.wfile.write(body)

    def _report_summary(self, report: Report, viewer=None) -> dict:
        return {"report_id": report.report_id, "owner_user_id": report.owner_user_id,
                "employee_name": report.employee_name, "department_name": report.department_name,
                "group_name": report.group_name, "submitter_role": report.submitter_role,
                "report_date": report.report_date, "title_summary": report.title_summary,
                "original_filename": report.original_filename, "created_at": report.created_at, "updated_at": report.updated_at,
                "status": report.status, "can_edit": bool(viewer and viewer.user_id == report.owner_user_id),
                "code_item_count": len(report.code_items), "document_item_count": len(report.document_items),
                "artifacts": self.db.list_artifacts_for_report(report.report_id)}

    def _report_detail(self, report: Report) -> dict:
        return report.to_dict() | {"artifacts": self.db.list_artifacts_for_report(report.report_id)}

    def _clear_business_storage(self) -> int:
        root = Path(self.config["storage"]["root_dir"]).resolve()
        removed = 0
        for category in ("submitted_reports", "artifacts", "testcase_snapshots", "tasks", "exports", "backups"):
            path = (root / category).resolve()
            if root not in path.parents or not path.exists():
                continue
            removed += sum(1 for item in path.rglob("*") if item.is_file())
            shutil.rmtree(path)
        raw_log = Path(str(self.config.get("llm_judge", {}).get("raw_log_path") or "")).resolve()
        if raw_log.exists() and root in raw_log.parents:
            raw_log.unlink(); removed += 1
        return removed

    def _storage_stats(self) -> dict:
        root = Path(self.config["storage"]["root_dir"])
        size = sum(p.stat().st_size for p in root.rglob("*") if p.is_file()) if root.exists() else 0
        return {"root_dir": str(root), "bytes": size, "mb": round(size / 1024 / 1024, 2)}

    def _create_backup(self, user_id: str) -> dict:
        root = Path(self.config["storage"]["root_dir"]); backups = root / "backups"; backups.mkdir(parents=True, exist_ok=True)
        name = f"storage_backup_{now_iso().replace(':','-')}.zip"; archive_base = backups / name.replace(".zip", "")
        shutil.make_archive(str(archive_base), "zip", root)
        self.db.save_export({"export_type": "storage_backup", "created_by": user_id, "stored_path": str((backups / name).relative_to(root))})
        return {"ok": True, "file": str(backups / name)}


def _report_to_payload(report: Report) -> dict:
    return {"name": report.employee_name, "department": report.department_name, "date": report.report_date, "manager": report.manager, "title_summary": report.title_summary, "work_description": report.work_description, "code_items": report.code_items, "document_items": report.document_items, "testcase_items": report.testcase_items}


def _build_reports_docx(reports: list[Report]) -> bytes:
    from io import BytesIO
    from docx import Document
    doc = Document(); doc.add_heading("周报汇总", level=1); doc.add_paragraph(f"共 {len(reports)} 份日报")
    table = doc.add_table(rows=1, cols=8); table.style = "Table Grid"
    for i, h in enumerate(["日期", "姓名", "职务", "部门", "小组", "标题", "状态", "提交时间"]): table.rows[0].cells[i].text = h
    for r in reports:
        row = table.add_row().cells
        for i, v in enumerate([r.report_date, r.employee_name, r.submitter_role, r.department_name, r.group_name, r.title_summary, r.status, r.created_at]): row[i].text = str(v or "")
    buf = BytesIO(); doc.save(buf); return buf.getvalue()


def _periodic_export_args(query: dict[str, list[str]]) -> tuple[str, str, str]:
    report_type = str((query.get("report_type") or [""])[0]).strip()
    start = str((query.get("start") or [""])[0]).strip()
    end = str((query.get("end") or [""])[0]).strip()
    if report_type not in {"weekly", "monthly"}:
        raise ValueError("报表类型必须是 weekly 或 monthly")
    if not start or not end:
        raise ValueError("请选择开始日期和结束日期")
    start, end = parse_date(start), parse_date(end)
    if start > end:
        raise ValueError("开始日期不能晚于结束日期")
    return report_type, start, end


def _export_scope_label(user) -> str:
    return {"employee": "本人", "group_leader": "本组", "director": "本部门", "minister": "全公司"}.get(user.role, "权限范围")


def _periodic_missing_reports(db, user, start: str, end: str, reports: list[Report]) -> list[dict]:
    submitted = {(report.owner_user_id, report.report_date) for report in reports}
    submitters = db.list_export_submitters(user)
    current, final = datetime.strptime(start, "%Y-%m-%d").date(), datetime.strptime(end, "%Y-%m-%d").date()
    workdays = []
    while current <= final:
        if current.weekday() < 5:
            workdays.append(current.isoformat())
        current += timedelta(days=1)
    missing = []
    for day in workdays:
        for submitter in submitters:
            if (submitter["user_id"], day) not in submitted:
                missing.append({"日期": day, "姓名": submitter.get("display_name", ""), "职务": submitter.get("role", ""), "部门": submitter.get("department_name", ""), "小组": submitter.get("group_name", ""), "日报": "无"})
    return missing


def _display_value(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _detail_headers(rows: list[dict]) -> list[str]:
    headers: list[str] = []
    for row in rows:
        for key in row:
            if key not in headers:
                headers.append(key)
    return headers


def _add_detail_table(doc, title: str, rows: list[dict]) -> None:
    doc.add_heading(title, level=3)
    if not rows:
        doc.add_paragraph("无")
        return
    headers = _detail_headers(rows)
    table = doc.add_table(rows=1, cols=len(headers)); table.style = "Table Grid"
    for index, header in enumerate(headers):
        table.rows[0].cells[index].text = _display_value(header)
    for item in rows:
        cells = table.add_row().cells
        for index, header in enumerate(headers):
            cells[index].text = _display_value(item.get(header, ""))


def _build_periodic_reports_docx(title: str, scope: str, start: str, end: str, reports: list[Report], artifacts: dict[str, list[dict]], missing_reports: list[dict] | None = None) -> bytes:
    from io import BytesIO
    from docx import Document

    doc = Document()
    doc.add_heading(title, level=1)
    missing_reports = missing_reports or []
    doc.add_paragraph(f"范围：{scope}；日期：{start} 至 {end}；共 {len(reports)} 份日报，工作日无日报 {len(missing_reports)} 条")
    if not reports and not missing_reports:
        doc.add_paragraph("暂无日报")
    for report in sorted(reports, key=lambda item: (item.report_date, item.employee_name, item.created_at)):
        doc.add_heading(f"{report.report_date} · {report.employee_name}", level=2)
        doc.add_paragraph(f"职务：{report.submitter_role}；部门/小组：{report.department_name or '—'} / {report.group_name or '—'}")
        doc.add_paragraph(f"提交时间：{report.created_at or '—'}；最后修改：{report.updated_at or '—'}")
        doc.add_paragraph(f"标题：{report.title_summary}")
        doc.add_paragraph(f"工作描述：{report.work_description}")
        _add_detail_table(doc, "代码记录", report.code_items)
        _add_detail_table(doc, "文档记录", report.document_items)
        _add_detail_table(doc, "测试用例记录", report.testcase_items)
        _add_detail_table(doc, "附件", [{"文件名": item.get("original_filename", ""), "类型": item.get("artifact_type", ""), "提交时间": item.get("created_at", "")} for item in artifacts.get(report.report_id, [])])
    _add_detail_table(doc, "工作日无日报", missing_reports)
    buf = BytesIO(); doc.save(buf); return buf.getvalue()


def _build_periodic_reports_xlsx(title: str, scope: str, start: str, end: str, reports: list[Report], artifacts: dict[str, list[dict]], missing_reports: list[dict] | None = None) -> bytes:
    from io import BytesIO
    from openpyxl import Workbook

    wb = Workbook()
    summary = wb.active; summary.title = "日报明细"
    summary.append(["报表", title, "范围", scope, "开始日期", start, "结束日期", end])
    summary.append(["日报ID", "日期", "姓名", "职务", "部门", "小组", "标题", "工作描述", "提交时间", "最后修改时间", "状态"])
    ordered = sorted(reports, key=lambda item: (item.report_date, item.employee_name, item.created_at))
    for report in ordered:
        summary.append([report.report_id, report.report_date, report.employee_name, report.submitter_role, report.department_name, report.group_name, report.title_summary, report.work_description, report.created_at, report.updated_at, "已提交"])
    for missing in missing_reports or []:
        summary.append(["", missing["日期"], missing["姓名"], missing["职务"], missing["部门"], missing["小组"], "无", "无", "", "", "无"])

    def append_detail_sheet(name: str, rows: list[dict]) -> None:
        sheet = wb.create_sheet(name)
        headers = _detail_headers(rows)
        sheet.append(headers or ["暂无记录"])
        for row in rows:
            sheet.append([_display_value(row.get(header, "")) for header in headers])

    code_rows, document_rows, testcase_rows, artifact_rows = [], [], [], []
    for report in ordered:
        prefix = {"日报ID": report.report_id, "日期": report.report_date, "姓名": report.employee_name}
        code_rows.extend([prefix | item for item in report.code_items])
        document_rows.extend([prefix | item for item in report.document_items])
        testcase_rows.extend([prefix | item for item in report.testcase_items])
        artifact_rows.extend([prefix | {"文件名": item.get("original_filename", ""), "类型": item.get("artifact_type", ""), "提交时间": item.get("created_at", "")} for item in artifacts.get(report.report_id, [])])
    append_detail_sheet("代码记录", code_rows)
    append_detail_sheet("文档记录", document_rows)
    append_detail_sheet("测试用例", testcase_rows)
    append_detail_sheet("附件", artifact_rows)
    buf = BytesIO(); wb.save(buf); return buf.getvalue()


def run_server(config_path: str, host: str | None = None, port: int | None = None) -> None:
    config = load_config(config_path); storage = FileStorage(config); db = create_database(config)
    DailyReportHandler.config = config; DailyReportHandler.storage = storage; DailyReportHandler.db = db
    bind_host = host or str(config.get("app", {}).get("host", "0.0.0.0")); bind_port = int(port or config.get("app", {}).get("port", 8000))
    server = ThreadingHTTPServer((bind_host, bind_port), DailyReportHandler)
    print(f"Daily Report V2 backend running at http://{bind_host}:{bind_port}"); server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=None, help="Path to config.yaml; defaults to project config/config.yaml")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args()
    config_path = args.config or str(default_config_path())
    run_server(config_path, args.host, args.port)


if __name__ == "__main__": main()

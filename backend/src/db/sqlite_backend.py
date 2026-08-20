from __future__ import annotations

import base64
import json
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from src.core.schemas import Report, User, duplicate_usage_summary
from src.db.base import DatabaseBackend
from src.utils.dates import now_iso, shanghai_now, today_iso
from src.utils.security import hash_password, token, verify_password

ROLE_EMPLOYEE = "employee"
ROLE_GROUP_LEADER = "group_leader"
ROLE_DIRECTOR = "director"
ROLE_MINISTER = "minister"
ROLE_ADMIN = "admin"
MANAGER_ROLES = {ROLE_GROUP_LEADER, ROLE_DIRECTOR, ROLE_MINISTER}
BUSINESS_ROLES = MANAGER_ROLES | {ROLE_EMPLOYEE}


def can_manage(user: User) -> bool:
    return user.role in MANAGER_ROLES | {ROLE_ADMIN}


def can_admin(user: User) -> bool:
    return user.role == ROLE_ADMIN


def can_access_department(user: User, department_id: str) -> bool:
    return user.role == ROLE_MINISTER or (user.role in {ROLE_GROUP_LEADER, ROLE_DIRECTOR} and user.department_id == department_id)


class SQLiteBackend(DatabaseBackend):
    """SQLite implementation with organisation-aware business visibility."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._maintenance_lock = threading.Lock()
        self._last_runtime_cleanup_at = 0.0
        self._last_metrics_cleanup_at = 0.0

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("pragma busy_timeout=10000")
        conn.execute("pragma foreign_keys=on")
        return conn

    def initialize(self, seed_demo_users: bool = True) -> None:
        with self.connect() as db:
            db.execute("pragma journal_mode=wal")
            db.execute("pragma synchronous=normal")
            existing_report_columns = {row[1] for row in db.execute("pragma table_info(reports)").fetchall()}
            backfill_report_roles = bool(existing_report_columns) and "submitter_role" not in existing_report_columns
            db.executescript("""
            create table if not exists departments (
              department_id text primary key, name text not null unique,
              parent_department_id text default '', enabled integer not null default 1
            );
            create table if not exists groups (
              group_id text primary key, department_id text not null, name text not null,
              enabled integer not null default 1, unique(department_id, name)
            );
            create table if not exists users (
              user_id text primary key, username text not null unique, password_hash text not null,
              display_name text not null, department_id text default '', group_id text default '',
              manager_user_id text default '', role text not null, enabled integer not null default 1,
              is_test_account integer not null default 0,
              created_at text not null
            );
            create table if not exists sessions (
              session_id text primary key, user_id text not null, expires_at text not null, created_at text not null
            );
            create table if not exists reports (
              report_id text primary key, owner_user_id text not null, employee_name text not null,
              department_id text default '', department_name text default '', group_id text default '', group_name text default '',
              submitter_role text default 'employee', report_date text not null, manager text default '',
              title_summary text not null, work_description text not null, source_type text default 'web',
              original_filename text default '', stored_path text default '', file_size integer default 0,
              content_hash text default '', code_items_json text default '[]', document_items_json text default '[]',
              testcase_items_json text default '[]', status text default 'active', locked_by_task_id text default '',
              revision_of_report_id text default '', created_at text not null, updated_at text default '', withdrawn_at text default ''
            );
            create table if not exists artifacts (
              artifact_id text primary key, report_id text default '', owner_user_id text not null,
              department_id text default '', artifact_type text not null, original_filename text not null,
              stored_path text not null, content_type text default '', file_size integer default 0,
              content_hash text default '', created_at text not null
            );
            create table if not exists testcase_snapshots (
              snapshot_id text primary key, department_id text not null, department_name text default '',
              group_id text default '', group_name text default '',
              snapshot_date text not null, original_filename text not null, stored_path text not null,
              file_size integer default 0, content_hash text default '', uploaded_by text not null,
              uploaded_at text not null, status text default 'active'
            );
            create table if not exists check_tasks (
              task_id text primary key, created_by text not null, department_id text default '',
              task_name text default '', task_date text default '',
              trigger_source text not null default 'manual', report_date text default '', attempt_count integer not null default 0,
              payload_json text not null, config_snapshot_json text default '{}', result_json text default '{}',
              result_dir text default '', status text not null, error_message text default '', created_at text not null,
              finished_at text default '', priority integer not null default 0, queued_at text default '',
              started_at text default '', heartbeat_at text default '', worker_id text default '', lease_token text default '',
              progress_stage text default '', progress_current integer not null default 0, progress_total integer not null default 0,
              progress_message text default '', progress_updated_at text default '', cancel_requested integer not null default 0, failure_type text default '',
              partial_available integer not null default 0, current_attempt integer not null default 1,
              summary_json text default '{}', pause_requested integer not null default 0,
              force_terminate_requested integer not null default 0, resume_from_checkpoint integer not null default 0,
              delete_requested integer not null default 0
            );
            create table if not exists task_reports (task_id text not null, report_id text not null, primary key(task_id, report_id));
            create table if not exists task_case_results (
              task_id text not null, attempt_no integer not null, report_id text not null,
              owner_user_id text default '', department_id text default '', group_id text default '', risk_level text default 'normal',
              result_json text not null, created_at text not null,
              primary key(task_id, attempt_no, report_id)
            );
            create table if not exists task_missing_results (
              task_id text not null, attempt_no integer not null, row_no integer not null,
              user_id text default '', department_id text default '', group_id text default '', result_json text not null,
              primary key(task_id, attempt_no, row_no)
            );
            create table if not exists worker_leases (
              lease_name text primary key, worker_id text not null, lease_token text not null, heartbeat_at text not null
            );
            create table if not exists llm_call_metrics (
              call_id text primary key, task_id text default '', started_at text not null, finished_at text default '',
              duration_ms integer not null default 0, total_duration_ms integer not null default 0,
              permit_wait_ms integer not null default 0, ok integer, error_type text default ''
            );
            create table if not exists llm_permit_metrics (
              singleton integer primary key check(singleton=1), updated_at text not null, global_limit integer not null,
              active_total integer not null, waiting_total integer not null, active_by_task_json text not null default '{}',
              waiting_by_task_json text not null default '{}'
            );
            create table if not exists task_stage_metrics (
              task_id text not null, attempt_no integer not null, stage text not null, started_at text not null,
              finished_at text not null, duration_seconds integer not null default 0
            );
            create table if not exists task_page_clears (user_id text primary key, cleared_at text not null);
            create table if not exists task_hidden_tasks (user_id text not null, task_id text not null, primary key(user_id, task_id));
            create table if not exists reset_confirmations (confirmation_id text primary key, user_id text not null, expires_at text not null);
            create table if not exists exports (
              export_id text primary key, export_type text not null, created_by text not null,
              department_scope text default '', start_date text default '', end_date text default '', stored_path text not null, created_at text not null
            );
            create table if not exists system_settings (setting_key text primary key, setting_value text not null, updated_at text not null);
            create table if not exists audit_logs (
              audit_id text primary key, occurred_at text not null, severity text not null,
              category text not null, action text not null, outcome text not null,
              actor_user_id text default '', actor_username text default '', actor_display_name text default '', actor_role text default '',
              target_type text default '', target_id text default '', target_label text default '', summary text default '',
              changes_json text not null default '{}', error_type text default '', client_ip text default '', user_agent text default ''
            );
            create table if not exists runtime_logs (
              log_seq integer primary key autoincrement, occurred_at text not null, level text not null,
              source text not null, event text not null, message text not null,
              task_id text default '', attempt_no integer not null default 0,
              context_json text not null default '{}', traceback_text text default ''
            );
            """)
            for stmt in [
                "alter table users add column manager_user_id text default ''",
                "alter table users add column group_id text default ''",
                "alter table users add column is_test_account integer not null default 0",
                "alter table reports add column stored_path text default ''",
                "alter table reports add column file_size integer default 0",
                "alter table reports add column content_hash text default ''",
                "alter table reports add column status text default 'active'",
                "alter table reports add column locked_by_task_id text default ''",
                "alter table reports add column revision_of_report_id text default ''",
                "alter table reports add column updated_at text default ''",
                "alter table reports add column withdrawn_at text default ''",
                "alter table reports add column group_id text default ''",
                "alter table reports add column group_name text default ''",
                "alter table reports add column submitter_role text default 'employee'",
                "alter table check_tasks add column department_id text default ''",
                "alter table check_tasks add column task_name text default ''",
                "alter table check_tasks add column task_date text default ''",
                "alter table check_tasks add column trigger_source text not null default 'manual'",
                "alter table check_tasks add column report_date text default ''",
                "alter table check_tasks add column attempt_count integer not null default 0",
                "alter table check_tasks add column config_snapshot_json text default '{}'",
                "alter table check_tasks add column result_dir text default ''",
                "alter table check_tasks add column priority integer not null default 0",
                "alter table check_tasks add column queued_at text default ''",
                "alter table check_tasks add column started_at text default ''",
                "alter table check_tasks add column heartbeat_at text default ''",
                "alter table check_tasks add column worker_id text default ''",
                "alter table check_tasks add column lease_token text default ''",
                "alter table check_tasks add column progress_stage text default ''",
                "alter table check_tasks add column progress_current integer not null default 0",
                "alter table check_tasks add column progress_total integer not null default 0",
                "alter table check_tasks add column progress_message text default ''",
                "alter table check_tasks add column progress_updated_at text default ''",
                "alter table check_tasks add column cancel_requested integer not null default 0",
                "alter table check_tasks add column failure_type text default ''",
                "alter table check_tasks add column partial_available integer not null default 0",
                "alter table check_tasks add column current_attempt integer not null default 1",
                "alter table check_tasks add column summary_json text default '{}'",
                "alter table check_tasks add column pause_requested integer not null default 0",
                "alter table check_tasks add column force_terminate_requested integer not null default 0",
                "alter table check_tasks add column resume_from_checkpoint integer not null default 0",
                "alter table check_tasks add column delete_requested integer not null default 0",
                "alter table testcase_snapshots add column group_id text default ''",
                "alter table testcase_snapshots add column group_name text default ''",
                "alter table llm_call_metrics add column total_duration_ms integer not null default 0",
                "alter table llm_call_metrics add column permit_wait_ms integer not null default 0",
            ]:
                try:
                    db.execute(stmt)
                except sqlite3.OperationalError:
                    pass
            db.execute("create unique index if not exists idx_check_tasks_system_daily on check_tasks(trigger_source, task_date) where trigger_source='system_daily'")
            db.execute("create index if not exists idx_check_tasks_queue on check_tasks(status, priority desc, queued_at)")
            db.execute("create index if not exists idx_check_tasks_creator_status on check_tasks(created_by, status)")
            db.execute("create index if not exists idx_llm_call_metrics_started on llm_call_metrics(started_at desc)")
            db.execute("create index if not exists idx_llm_call_metrics_task on llm_call_metrics(task_id, started_at desc)")
            db.execute("create index if not exists idx_task_stage_metrics_stage on task_stage_metrics(stage, finished_at desc)")
            db.execute("create index if not exists idx_reports_date_status on reports(report_date, status)")
            db.execute("create index if not exists idx_reports_owner_date on reports(owner_user_id, report_date)")
            db.execute("create index if not exists idx_reports_department_date on reports(department_id, report_date)")
            db.execute("create index if not exists idx_reports_group_date on reports(group_id, report_date)")
            db.execute("create index if not exists idx_artifacts_report_type on artifacts(report_id, artifact_type)")
            db.execute("create index if not exists idx_task_case_scope on task_case_results(task_id, attempt_no, department_id, group_id)")
            db.execute("create index if not exists idx_audit_logs_occurred on audit_logs(occurred_at desc, audit_id desc)")
            db.execute("create index if not exists idx_audit_logs_actor on audit_logs(actor_username, actor_user_id, occurred_at desc)")
            db.execute("create index if not exists idx_audit_logs_category_result on audit_logs(category, outcome, occurred_at desc)")
            db.execute("create index if not exists idx_runtime_logs_occurred on runtime_logs(occurred_at desc, log_seq desc)")
            db.execute("create index if not exists idx_runtime_logs_source on runtime_logs(source, log_seq desc)")
            db.execute("create index if not exists idx_runtime_logs_task on runtime_logs(task_id, log_seq desc)")
            db.execute("delete from audit_logs where occurred_at < ?", (self._audit_cutoff(),))
            db.execute("delete from runtime_logs where occurred_at < ?", (self._runtime_cutoff(),))
            db.execute("update check_tasks set queued_at=created_at where queued_at='' and status='queued'")
            db.execute("update check_tasks set progress_updated_at=coalesce(nullif(heartbeat_at,''),nullif(started_at,''),created_at) where progress_updated_at='' or progress_updated_at is null")
            self._migrate_testcase_snapshots_unique(db)
            db.execute("update users set role = ? where role = 'department_manager'", (ROLE_DIRECTOR,))
            db.execute("update users set role = ? where role = 'global_manager'", (ROLE_MINISTER,))
            if backfill_report_roles:
                db.execute("update reports set submitter_role = coalesce((select role from users where users.user_id = reports.owner_user_id), 'employee')")
        with self.connect() as db:
            db.execute("update reports set locked_by_task_id='' where locked_by_task_id!=''")
        if seed_demo_users:
            self._seed_bootstrap_admin()

    def _migrate_testcase_snapshots_unique(self, db) -> None:
        row = db.execute("select sql from sqlite_master where type='table' and name='testcase_snapshots'").fetchone()
        sql = (row[0] if row else "") or ""
        if "unique(group_id, snapshot_date)" in sql.lower().replace(" ", ""):
            return
        db.executescript("""
            create table if not exists testcase_snapshots_new (
              snapshot_id text primary key, department_id text not null, department_name text default '',
              group_id text default '', group_name text default '',
              snapshot_date text not null, original_filename text not null, stored_path text not null,
              file_size integer default 0, content_hash text default '', uploaded_by text not null,
              uploaded_at text not null, status text default 'active',
              unique(group_id, snapshot_date)
            );
            insert or ignore into testcase_snapshots_new(
              snapshot_id, department_id, department_name, group_id, group_name, snapshot_date,
              original_filename, stored_path, file_size, content_hash, uploaded_by, uploaded_at, status
            )
            select snapshot_id, department_id, department_name,
                   coalesce(nullif(group_id, ''), 'legacy_' || department_id || '_' || snapshot_date),
                   coalesce(group_name, ''), snapshot_date, original_filename, stored_path, file_size,
                   content_hash, uploaded_by, uploaded_at, status
            from testcase_snapshots;
            drop table testcase_snapshots;
            alter table testcase_snapshots_new rename to testcase_snapshots;
        """)

    def _seed_bootstrap_admin(self) -> None:
        with self.connect() as db:
            db.execute("""insert or ignore into users(user_id,username,password_hash,display_name,department_id,group_id,manager_user_id,role,enabled,is_test_account,created_at)
                values ('u_admin','admin',?,'Admin','','','',?,1,0,?)""", (hash_password("admin123"), ROLE_ADMIN, now_iso()))
            db.execute("""insert or ignore into users(user_id,username,password_hash,display_name,department_id,group_id,manager_user_id,role,enabled,is_test_account,created_at)
                values ('u_test_employee','test_employee',?,'测试员工','','','',?,1,1,?)""", (hash_password("test123"), ROLE_EMPLOYEE, now_iso()))

    # Authentication and organisation -------------------------------------------------
    def authenticate(self, username: str, password: str) -> User | None:
        with self.connect() as db:
            row = db.execute(self._user_select() + " where u.username = ?", (username,)).fetchone()
        return self._row_to_user(row) if row and row["enabled"] and verify_password(password, row["password_hash"]) else None

    def create_session(self, user_id: str, days: int = 7) -> str:
        sid = token("S_", 28)
        exp = (shanghai_now() + timedelta(days=days)).replace(tzinfo=None).isoformat(timespec="seconds")
        with self.connect() as db: db.execute("insert into sessions(session_id,user_id,expires_at,created_at) values (?,?,?,?)", (sid, user_id, exp, now_iso()))
        return sid

    def delete_session(self, session_id: str) -> None:
        with self.connect() as db: db.execute("delete from sessions where session_id = ?", (session_id,))

    def cleanup_sessions(self) -> int:
        with self.connect() as db: return db.execute("delete from sessions where expires_at <= ?", (now_iso(),)).rowcount

    def user_by_session(self, session_id: str) -> User | None:
        if not session_id: return None
        with self.connect() as db:
            row = db.execute(self._user_select() + " join sessions s on s.user_id=u.user_id where s.session_id=? and s.expires_at>? and u.enabled=1", (session_id, now_iso())).fetchone()
        return self._row_to_user(row) if row else None

    @staticmethod
    def _user_select() -> str:
        return """select u.*, coalesce(d.name,'') department_name, coalesce(g.name,'') group_name
            from users u left join departments d on d.department_id=u.department_id
            left join groups g on g.group_id=u.group_id"""

    def list_departments(self, public_only: bool = False) -> list[dict[str, Any]]:
        sql = "select * from departments" + (" where enabled=1" if public_only else "") + " order by name"
        with self.connect() as db: rows = db.execute(sql).fetchall()
        return [dict(r) | {"enabled": bool(r["enabled"])} for r in rows]

    def create_department(self, payload: dict[str, Any]) -> dict[str, Any]:
        did = str(payload.get("department_id") or token("D_", 14)); name = str(payload.get("name") or "").strip()
        if not name: raise ValueError("department name is required")
        with self.connect() as db: db.execute("insert into departments(department_id,name,parent_department_id,enabled) values (?,?,?,?)", (did, name, str(payload.get("parent_department_id") or ""), int(bool(payload.get("enabled", True)))))
        return {"department_id": did, "name": name, "parent_department_id": str(payload.get("parent_department_id") or ""), "enabled": bool(payload.get("enabled", True))}

    def update_department(self, department_id: str, payload: dict[str, Any]) -> None:
        fields, values = [], []
        for key in ("name", "parent_department_id", "enabled"):
            if key in payload:
                fields.append(f"{key}=?"); values.append(int(bool(payload[key])) if key == "enabled" else payload[key])
        if fields:
            with self.connect() as db: db.execute(f"update departments set {','.join(fields)} where department_id=?", [*values, department_id])

    def delete_department(self, department_id: str) -> None:
        with self.connect() as db:
            user_count = db.execute("select count(*) from users where department_id=?", (department_id,)).fetchone()[0]
            group_count = db.execute("select count(*) from groups where department_id=?", (department_id,)).fetchone()[0]
            if user_count or group_count:
                raise ValueError(f"该部门仍有关联数据：{group_count} 个小组、{user_count} 名人员。请先迁移或删除关联数据。")
            db.execute("delete from departments where department_id=?", (department_id,))

    def list_groups(self, department_id: str = "", public_only: bool = False) -> list[dict[str, Any]]:
        where, values = [], []
        if department_id: where.append("g.department_id=?"); values.append(department_id)
        if public_only: where.append("g.enabled=1")
        sql = "select g.*, d.name department_name from groups g join departments d on d.department_id=g.department_id" + (" where " + " and ".join(where) if where else "") + " order by d.name,g.name"
        with self.connect() as db: rows = db.execute(sql, values).fetchall()
        return [dict(r) | {"enabled": bool(r["enabled"])} for r in rows]

    def create_group(self, payload: dict[str, Any]) -> dict[str, Any]:
        gid = str(payload.get("group_id") or token("G_", 14)); did, name = str(payload.get("department_id") or ""), str(payload.get("name") or "").strip()
        if not did or not name: raise ValueError("group department and name are required")
        with self.connect() as db: db.execute("insert into groups(group_id,department_id,name,enabled) values (?,?,?,?)", (gid, did, name, int(bool(payload.get("enabled", True)))))
        return {"group_id": gid, "department_id": did, "name": name, "enabled": bool(payload.get("enabled", True))}

    def update_group(self, group_id: str, payload: dict[str, Any]) -> None:
        if payload.get("department_id"):
            with self.connect() as db:
                current = db.execute("select department_id from groups where group_id=?", (group_id,)).fetchone()
                has_users = db.execute("select 1 from users where group_id=? limit 1", (group_id,)).fetchone()
            if current and payload["department_id"] != current["department_id"] and has_users:
                raise ValueError("move group users before changing its department")
        fields, values = [], []
        for key in ("name", "department_id", "enabled"):
            if key in payload:
                fields.append(f"{key}=?"); values.append(int(bool(payload[key])) if key == "enabled" else payload[key])
        if fields:
            with self.connect() as db: db.execute(f"update groups set {','.join(fields)} where group_id=?", [*values, group_id])

    def delete_group(self, group_id: str) -> None:
        with self.connect() as db:
            user_count = db.execute("select count(*) from users where group_id=?", (group_id,)).fetchone()[0]
            if user_count:
                raise ValueError(f"该小组仍有 {user_count} 名人员。请先迁移人员或删除人员。")
            db.execute("delete from groups where group_id=?", (group_id,))

    def list_users(self) -> list[dict[str, Any]]:
        with self.connect() as db: rows = db.execute(self._user_select() + " order by u.created_at,u.username").fetchall()
        return [self._user_dict(self._row_to_user(r)) for r in rows]

    def list_required_submitters(self, user: User | None) -> list[dict[str, Any]]:
        if user is None:
            scope, values = "1=1", []
        elif user.role == ROLE_GROUP_LEADER:
            scope, values = "u.group_id=?", [user.group_id]
        elif user.role == ROLE_DIRECTOR:
            scope, values = "u.department_id=?", [user.department_id]
        elif user.role in {ROLE_MINISTER, ROLE_ADMIN}:
            scope, values = "1=1", []
        else:
            return []
        sql = self._user_select() + f" where {scope} and u.enabled=1 and coalesce(u.is_test_account, 0)=0 and u.role in (?,?) order by d.name,g.name,u.display_name"
        with self.connect() as db:
            rows = db.execute(sql, [*values, ROLE_EMPLOYEE, ROLE_GROUP_LEADER]).fetchall()
        return [self._user_dict(self._row_to_user(r)) | {"created_at": r["created_at"]} for r in rows]

    def list_export_submitters(self, user: User) -> list[dict[str, Any]]:
        if user.role != ROLE_EMPLOYEE:
            return self.list_required_submitters(user)
        with self.connect() as db:
            row = db.execute(self._user_select() + " where u.user_id=? and u.enabled=1", (user.user_id,)).fetchone()
        return [self._user_dict(self._row_to_user(row)) | {"created_at": row["created_at"]}] if row else []

    def create_user(self, payload: dict[str, Any]) -> dict[str, Any]:
        uid = str(payload.get("user_id") or token("U_", 14)); role = str(payload.get("role") or ROLE_EMPLOYEE)
        data = {"department_id": str(payload.get("department_id") or ""), "group_id": str(payload.get("group_id") or ""), "role": role, "enabled": bool(payload.get("enabled", True))}
        self._validate_assignment(data, uid)
        username = str(payload.get("username") or "").strip()
        if not username: raise ValueError("username is required")
        with self.connect() as db:
            db.execute("""insert into users(user_id,username,password_hash,display_name,department_id,group_id,manager_user_id,role,enabled,created_at)
                values (?,?,?,?,?,?, '',?,?,?)""", (uid, username, hash_password(str(payload.get("password") or "123456")), str(payload.get("display_name") or username).strip(), data["department_id"], data["group_id"], role, int(bool(payload.get("enabled", True))), now_iso()))
        return {"user_id": uid}

    def update_user(self, user_id: str, payload: dict[str, Any]) -> None:
        with self.connect() as db:
            current = db.execute("select * from users where user_id=?", (user_id,)).fetchone()
        if not current: raise ValueError("user not found")
        data = {"department_id": str(payload.get("department_id", current["department_id"]) or ""), "group_id": str(payload.get("group_id", current["group_id"]) or ""), "role": str(payload.get("role", current["role"]) or ROLE_EMPLOYEE), "enabled": bool(payload.get("enabled", current["enabled"]))}
        self._validate_assignment(data, user_id)
        fields, values = [], []
        for key in ("display_name", "department_id", "group_id", "role", "enabled"):
            if key in payload:
                fields.append(f"{key}=?"); values.append(int(bool(payload[key])) if key == "enabled" else payload[key])
        if payload.get("password"):
            fields.append("password_hash=?"); values.append(hash_password(str(payload["password"])))
        if fields:
            with self.connect() as db: db.execute(f"update users set {','.join(fields)} where user_id=?", [*values, user_id])

    def reset_user_password(self, user_id: str, password: str) -> None:
        password = str(password or "")
        if len(password) < 6:
            raise ValueError("新密码至少需要 6 个字符")
        with self.connect() as db:
            changed = db.execute("update users set password_hash=? where user_id=?", (hash_password(password), user_id)).rowcount
            if not changed:
                raise ValueError("user not found")
            db.execute("delete from sessions where user_id=?", (user_id,))

    def _validate_assignment(self, data: dict[str, str], exclude_user_id: str = "") -> None:
        role, did, gid = data["role"], data["department_id"], data["group_id"]
        if role not in BUSINESS_ROLES | {ROLE_ADMIN}: raise ValueError("invalid role")
        if role == ROLE_ADMIN:
            if did or gid: raise ValueError("admin must not belong to a department or group")
            return
        if role == ROLE_MINISTER:
            if did or gid: raise ValueError("minister must not belong to a department or group")
            if data.get("enabled", True): self._ensure_unique_role(role, "", "", exclude_user_id)
            return
        if not did: raise ValueError("员工、组长和主任必须选择部门")
        with self.connect() as db:
            if not db.execute("select 1 from departments where department_id=?", (did,)).fetchone(): raise ValueError("department not found")
            if gid:
                group = db.execute("select department_id from groups where group_id=?", (gid,)).fetchone()
                if not group or group["department_id"] != did: raise ValueError("group does not belong to department")
        if role in {ROLE_EMPLOYEE, ROLE_GROUP_LEADER}:
            if not gid: raise ValueError("员工和组长必须选择小组")
        elif gid: raise ValueError("主任不能归属到小组")
        if role == ROLE_GROUP_LEADER and data.get("enabled", True): self._ensure_unique_role(role, did, gid, exclude_user_id)
        if role == ROLE_DIRECTOR and data.get("enabled", True): self._ensure_unique_role(role, did, "", exclude_user_id)

    def _ensure_unique_role(self, role: str, department_id: str, group_id: str, exclude_user_id: str) -> None:
        sql, values = "select 1 from users where role=? and enabled=1", [role]
        if role == ROLE_GROUP_LEADER: sql += " and department_id=? and group_id=?"; values += [department_id, group_id]
        if role == ROLE_DIRECTOR: sql += " and department_id=?"; values.append(department_id)
        if exclude_user_id: sql += " and user_id!=?"; values.append(exclude_user_id)
        with self.connect() as db:
            if db.execute(sql + " limit 1", values).fetchone(): raise ValueError("this organisation unit already has its leader")

    def delete_user(self, user_id: str) -> None:
        with self.connect() as db:
            db.execute("delete from sessions where user_id=?", (user_id,)); db.execute("delete from users where user_id=?", (user_id,))

    def get_derived_manager(self, user: User) -> User | None:
        if user.role in {ROLE_MINISTER, ROLE_ADMIN}: return None
        if user.role == ROLE_EMPLOYEE:
            where, values = "u.role=? and u.group_id=? and u.enabled=1", [ROLE_GROUP_LEADER, user.group_id]
        elif user.role == ROLE_GROUP_LEADER:
            where, values = "u.role=? and u.department_id=? and u.enabled=1", [ROLE_DIRECTOR, user.department_id]
        else:
            where, values = "u.role=? and u.enabled=1", [ROLE_MINISTER]
        with self.connect() as db: row = db.execute(self._user_select() + " where " + where, values).fetchone()
        return self._row_to_user(row) if row else None

    # Reports and artefacts -----------------------------------------------------------
    def save_artifact(self, payload: dict[str, Any]) -> str:
        aid = str(payload.get("artifact_id") or token("A_", 14))
        with self.connect() as db:
            db.execute("""insert into artifacts(artifact_id,report_id,owner_user_id,department_id,artifact_type,original_filename,stored_path,content_type,file_size,content_hash,created_at)
                values (?,?,?,?,?,?,?,?,?,?,?)""", (aid, payload.get("report_id", ""), payload["owner_user_id"], payload.get("department_id", ""), payload["artifact_type"], payload["original_filename"], payload["stored_path"], payload.get("content_type", ""), int(payload.get("file_size") or 0), payload.get("content_hash", ""), now_iso()))
        return aid

    def list_artifacts_for_report(self, report_id: str) -> list[dict[str, Any]]:
        with self.connect() as db: return [dict(r) for r in db.execute("select * from artifacts where report_id=? order by created_at", (report_id,)).fetchall()]

    def list_artifacts_for_reports(self, report_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
        result = {report_id: [] for report_id in report_ids}
        with self.connect() as db:
            for offset in range(0, len(report_ids), 500):
                chunk = report_ids[offset:offset + 500]
                if not chunk:
                    continue
                rows = db.execute(f"select * from artifacts where report_id in ({','.join('?' for _ in chunk)}) order by created_at", chunk).fetchall()
                for row in rows:
                    item = dict(row)
                    result.setdefault(str(item["report_id"]), []).append(item)
        return result

    def get_artifact(self, artifact_id: str, user: User | None = None) -> dict[str, Any] | None:
        with self.connect() as db: row = db.execute("select * from artifacts where artifact_id=?", (artifact_id,)).fetchone()
        if not row: return None
        return dict(row) if not user or self.get_report(row["report_id"], user) else None

    def save_report(self, report: Report) -> str:
        with self.connect() as db:
            db.execute("""insert or replace into reports(report_id,owner_user_id,employee_name,department_id,department_name,group_id,group_name,submitter_role,report_date,manager,title_summary,work_description,source_type,original_filename,stored_path,file_size,content_hash,code_items_json,document_items_json,testcase_items_json,status,locked_by_task_id,revision_of_report_id,created_at,updated_at,withdrawn_at)
                values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (report.report_id, report.owner_user_id, report.employee_name, report.department_id, report.department_name, report.group_id, report.group_name, report.submitter_role, report.report_date, report.manager, report.title_summary, report.work_description, report.source_type, report.original_filename, report.stored_path, report.file_size, report.content_hash, json.dumps(report.code_items, ensure_ascii=False), json.dumps(report.document_items, ensure_ascii=False), json.dumps(report.testcase_items, ensure_ascii=False), report.status, report.locked_by_task_id, report.revision_of_report_id, report.created_at or now_iso(), report.updated_at, report.withdrawn_at))
        return report.report_id

    def _report_scope(self, user: User | None) -> tuple[str, list[str]]:
        if not user: return "", []
        if user.role == ROLE_EMPLOYEE: return "owner_user_id=?", [user.user_id]
        if user.role == ROLE_GROUP_LEADER: return "(owner_user_id=? or group_id=?)", [user.user_id, user.group_id]
        if user.role == ROLE_DIRECTOR: return "department_id=?", [user.department_id]
        if user.role == ROLE_MINISTER: return "1=1", []
        return "0=1", []

    def list_reports(self, user: User | None = None, start: str = "", end: str = "", include_withdrawn: bool = False, name: str = "", role: str = "") -> list[Report]:
        where, values = [], []
        scope, scope_values = self._report_scope(user)
        if scope: where.append(scope); values.extend(scope_values)
        if not include_withdrawn: where.append("status!='withdrawn'")
        if start: where.append("report_date>=?"); values.append(start)
        if end: where.append("report_date<=?"); values.append(end)
        if name: where.append("employee_name like ?"); values.append(f"%{name}%")
        if role: where.append("submitter_role=?"); values.append(role)
        sql = "select * from reports" + (" where " + " and ".join(where) if where else "") + " order by report_date desc,created_at desc"
        with self.connect() as db: rows = db.execute(sql, values).fetchall()
        return [self._row_to_report(r) for r in rows]

    def get_report(self, report_id: str, user: User | None = None) -> Report | None:
        scope, values = self._report_scope(user); where, params = ["report_id=?"], [report_id]
        if user: where.append(scope); params.extend(values)
        with self.connect() as db: row = db.execute("select * from reports where " + " and ".join(where), params).fetchone()
        return self._row_to_report(row) if row else None

    def update_report_status(self, report_id: str, status: str, user: User, revision_of_report_id: str = "") -> None:
        report = self.get_report(report_id, user)
        if not report: raise ValueError("report not found or forbidden")
        if report.owner_user_id != user.user_id: raise ValueError("only the submitter may withdraw a report")
        if report.locked_by_task_id and status in {"withdrawn", "deleted"}: raise ValueError("report already locked by a check task")
        with self.connect() as db: db.execute("update reports set status=?,revision_of_report_id=?,updated_at=?,withdrawn_at=case when ?='withdrawn' then ? else withdrawn_at end where report_id=?", (status, revision_of_report_id, now_iso(), status, now_iso(), report_id))

    def lock_reports_for_task(self, task_id: str, report_ids: list[str]) -> None:
        with self.connect() as db:
            for rid in report_ids: db.execute("insert or ignore into task_reports(task_id,report_id) values (?,?)", (task_id, rid))
            if report_ids: db.execute(f"update reports set locked_by_task_id=? where report_id in ({','.join('?' for _ in report_ids)}) and locked_by_task_id=''", [task_id, *report_ids])

    # Testcase snapshots ---------------------------------------------------------------
    def save_testcase_snapshot(self, payload: dict[str, Any]) -> str:
        sid = str(payload.get("snapshot_id") or token("TS_", 14))
        with self.connect() as db:
            db.execute("""insert into testcase_snapshots(
                snapshot_id,department_id,department_name,group_id,group_name,snapshot_date,
                original_filename,stored_path,file_size,content_hash,uploaded_by,uploaded_at,status)
                values (?,?,?,?,?,?,?,?,?,?,?,?,'active')
                on conflict(group_id,snapshot_date) do update set
                snapshot_id=excluded.snapshot_id,department_id=excluded.department_id,department_name=excluded.department_name,
                group_name=excluded.group_name,original_filename=excluded.original_filename,stored_path=excluded.stored_path,
                file_size=excluded.file_size,content_hash=excluded.content_hash,uploaded_by=excluded.uploaded_by,
                uploaded_at=excluded.uploaded_at,status='active'""",
                (sid, payload["department_id"], payload.get("department_name", ""), payload.get("group_id", ""),
                 payload.get("group_name", ""), payload["snapshot_date"], payload["original_filename"], payload["stored_path"],
                 int(payload.get("file_size") or 0), payload.get("content_hash", ""), payload["uploaded_by"], now_iso()))
        return sid

    def list_testcase_snapshots(self, user: User) -> list[dict[str, Any]]:
        if user.role not in MANAGER_ROLES: return []
        where, values = [], []
        if user.role == ROLE_GROUP_LEADER:
            where.append("group_id=?"); values.append(user.group_id)
        elif user.role == ROLE_DIRECTOR:
            where.append("department_id=?"); values.append(user.department_id)
        with self.connect() as db:
            rows = db.execute(
                "select * from testcase_snapshots" + (" where " + " and ".join(where) if where else "") +
                " order by snapshot_date desc,uploaded_at desc limit 200", values
            ).fetchall()
        return [dict(r) for r in rows]

    def get_testcase_snapshot(self, snapshot_id: str, user: User | None = None) -> dict[str, Any] | None:
        if not user or user.role not in MANAGER_ROLES: return None
        where, values = ["snapshot_id=?"], [snapshot_id]
        if user.role == ROLE_GROUP_LEADER:
            where.append("group_id=?"); values.append(user.group_id)
        elif user.role == ROLE_DIRECTOR:
            where.append("department_id=?"); values.append(user.department_id)
        with self.connect() as db: row = db.execute("select * from testcase_snapshots where " + " and ".join(where), values).fetchone()
        return dict(row) if row else None

    def find_testcase_snapshot(self, group_id: str, report_date: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                "select * from testcase_snapshots where group_id=? and snapshot_date=? and status='active'",
                (group_id, report_date),
            ).fetchone()
        return dict(row) if row else None

    def find_latest_testcase_snapshot_before(self, group_id: str, report_date: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                "select * from testcase_snapshots where group_id=? and snapshot_date<? and status='active' order by snapshot_date desc limit 1",
                (group_id, report_date),
            ).fetchone()
        return dict(row) if row else None

    # Tasks and maintenance ------------------------------------------------------------
    def create_task(self, user: User, payload: dict[str, Any], config_snapshot: dict[str, Any], result_dir: str) -> str:
        tid = token("T_", 14); dept = user.department_id if user.role in {ROLE_GROUP_LEADER, ROLE_DIRECTOR} else ""
        task_date = today_iso()
        with self.connect() as db:
            db.execute("begin immediate")
            active = db.execute(
                "select task_id from check_tasks where created_by=? and trigger_source='manual' and status in ('queued','running','paused') and delete_requested=0 order by created_at limit 1",
                (user.user_id,),
            ).fetchone()
            if active:
                raise ValueError(f"active_task_exists:{active['task_id']}")
            sequence = db.execute("select count(*) from check_tasks where created_by=? and task_date=?", (user.user_id, task_date)).fetchone()[0] + 1
            task_name = f"{int(task_date[5:7])}.{int(task_date[8:10])}查重({sequence})"
            created = now_iso()
            db.execute("""insert into check_tasks(
                task_id,created_by,department_id,task_name,task_date,payload_json,config_snapshot_json,result_dir,
                status,priority,queued_at,attempt_count,current_attempt,progress_stage,progress_message,created_at)
                values (?,?,?,?,?,?,?,?,'queued',0,?,1,1,'queued','等待后台 worker 执行',?)""",
                (tid, user.user_id, dept, task_name, task_date, json.dumps(payload, ensure_ascii=False),
                 json.dumps(config_snapshot, ensure_ascii=False), result_dir, created, created))
        return tid

    def begin_automatic_task(self, report_date: str, payload: dict[str, Any], config_snapshot: dict[str, Any], result_dir: str) -> tuple[str, bool]:
        """Create the single queued daily system task. Existing tasks never retry implicitly."""
        task_date = (datetime.strptime(report_date, "%Y-%m-%d").date() + timedelta(days=1)).isoformat()
        with self.connect() as db:
            db.execute("begin immediate")
            row = db.execute("select task_id from check_tasks where trigger_source='system_daily' and task_date=?", (task_date,)).fetchone()
            if row:
                return row["task_id"], False
            tid = token("T_", 14)
            task_name = (
                f"{int(report_date[5:7])}.{int(report_date[8:10])} 日报自动查重"
                f"（{int(task_date[5:7])}.{int(task_date[8:10])} 执行）"
            )
            created = now_iso()
            db.execute("""insert into check_tasks(task_id,created_by,department_id,task_name,task_date,trigger_source,report_date,
                           attempt_count,current_attempt,payload_json,config_snapshot_json,result_dir,status,priority,queued_at,
                           progress_stage,progress_message,created_at)
                           values (?,'','',?,?,'system_daily',?,1,1,?,?,?,'queued',100,?,'queued','等待后台 worker 执行',?)""",
                       (tid, task_name, task_date, report_date, json.dumps(payload, ensure_ascii=False),
                        json.dumps(config_snapshot, ensure_ascii=False), result_dir, created, created))
        return tid, True

    def acquire_worker_lease(self, worker_id: str, lease_token: str, stale_before: str) -> bool:
        with self.connect() as db:
            db.execute("begin immediate")
            row = db.execute("select * from worker_leases where lease_name='duplicate-worker'").fetchone()
            if row and row["heartbeat_at"] >= stale_before and row["lease_token"] != lease_token:
                return False
            db.execute("insert or replace into worker_leases(lease_name,worker_id,lease_token,heartbeat_at) values ('duplicate-worker',?,?,?)", (worker_id, lease_token, now_iso()))
        return True

    def heartbeat_worker(self, worker_id: str, lease_token: str) -> bool:
        with self.connect() as db:
            changed = db.execute("update worker_leases set heartbeat_at=? where lease_name='duplicate-worker' and worker_id=? and lease_token=?", (now_iso(), worker_id, lease_token)).rowcount
        return bool(changed)

    def release_worker_lease(self, worker_id: str, lease_token: str) -> None:
        with self.connect() as db:
            db.execute("delete from worker_leases where lease_name='duplicate-worker' and worker_id=? and lease_token=?", (worker_id, lease_token))

    def fail_stale_tasks(self, stale_before: str) -> int:
        message = "后台 worker 心跳中断，任务已停止；可手动重试"
        with self.connect() as db:
            terminated = db.execute("""update check_tasks set status='terminated',failure_type='force_terminated',error_message='管理员已强制终止任务',
                progress_stage='terminated',progress_message='任务已强制终止',finished_at=?,worker_id='',lease_token='',force_terminate_requested=0,
                partial_available=case when progress_current>0 then 1 else partial_available end
                where status='running' and force_terminate_requested=1 and delete_requested=0 and coalesce(heartbeat_at,'')<?""", (now_iso(), stale_before)).rowcount
            paused = db.execute("""update check_tasks set status='paused',failure_type='',error_message='',
                progress_stage='paused',progress_message='任务已在安全点暂停',worker_id='',lease_token='',pause_requested=0,
                partial_available=case when progress_current>0 then 1 else partial_available end
                where status='running' and pause_requested=1 and delete_requested=0 and coalesce(heartbeat_at,'')<?""", (stale_before,)).rowcount
            failed = db.execute("""update check_tasks set status='failed',failure_type='interrupted',error_message=?,
                progress_message=?,finished_at=?,worker_id='',lease_token='',partial_available=case when progress_current>0 then 1 else partial_available end
                where status='running' and delete_requested=0 and coalesce(heartbeat_at,'')<?""", (message, message, now_iso(), stale_before)).rowcount
        return int(terminated + paused + failed)

    def claim_next_task(self, worker_id: str, lease_token: str) -> dict[str, Any] | None:
        with self.connect() as db:
            db.execute("begin immediate")
            row = db.execute("select task_id from check_tasks where status='queued' order by priority desc,queued_at,created_at limit 1").fetchone()
            if not row:
                return None
            started = now_iso()
            changed = db.execute("""update check_tasks set status='running',started_at=case when resume_from_checkpoint=1 and started_at!='' then started_at else ? end,
                heartbeat_at=?,worker_id=?,lease_token=?,
                progress_updated_at=?,
                progress_stage=case when resume_from_checkpoint=1 then 'resuming' else 'starting' end,
                progress_current=case when resume_from_checkpoint=1 then progress_current else 0 end,
                progress_total=case when resume_from_checkpoint=1 then progress_total else 0 end,
                progress_message=case when resume_from_checkpoint=1 then '正在从检查点恢复任务' else '正在启动查重任务' end,
                cancel_requested=0,pause_requested=0,force_terminate_requested=0,resume_from_checkpoint=0,delete_requested=0,
                failure_type='',error_message='',finished_at='' where task_id=? and status='queued'""",
                (started, started, worker_id, lease_token, started, row["task_id"])).rowcount
            if not changed:
                return None
            task = db.execute("select * from check_tasks where task_id=?", (row["task_id"],)).fetchone()
        return self._decode_task(task, include_results=False) if task else None

    def heartbeat_task(self, task_id: str, lease_token: str) -> bool:
        with self.connect() as db:
            changed = db.execute("update check_tasks set heartbeat_at=? where task_id=? and status='running' and lease_token=?", (now_iso(), task_id, lease_token)).rowcount
        return bool(changed)

    def update_task_progress(self, task_id: str, attempt_no: int, stage: str, current: int, total: int, message: str = "") -> bool:
        with self.connect() as db:
            previous = db.execute("select progress_stage,progress_updated_at from check_tasks where task_id=? and current_attempt=?", (task_id, int(attempt_no))).fetchone()
            changed_at = now_iso()
            if previous and previous["progress_stage"] and previous["progress_stage"] != stage and previous["progress_updated_at"]:
                try:
                    duration = max(0, int((datetime.fromisoformat(changed_at) - datetime.fromisoformat(previous["progress_updated_at"])).total_seconds()))
                    db.execute("insert into task_stage_metrics(task_id,attempt_no,stage,started_at,finished_at,duration_seconds) values (?,?,?,?,?,?)",
                        (task_id, int(attempt_no), previous["progress_stage"], previous["progress_updated_at"], changed_at, duration))
                except ValueError:
                    pass
            changed = db.execute("""update check_tasks set progress_updated_at=case
                when progress_stage!=? or progress_current!=? or progress_total!=? or progress_message!=? then ? else progress_updated_at end,
                progress_stage=?,progress_current=?,progress_total=?,progress_message=?,heartbeat_at=?
                where task_id=? and current_attempt=? and status='running'""",
                (stage, int(current), int(total), message, changed_at, stage, int(current), int(total), message, changed_at, task_id, int(attempt_no))).rowcount
        return bool(changed)

    def task_cancel_requested(self, task_id: str, attempt_no: int) -> bool:
        with self.connect() as db:
            row = db.execute("select cancel_requested from check_tasks where task_id=? and current_attempt=?", (task_id, int(attempt_no))).fetchone()
        return bool(row and row["cancel_requested"])

    def task_control_request(self, task_id: str, attempt_no: int) -> str:
        with self.connect() as db:
            row = db.execute("""select status,cancel_requested,pause_requested,force_terminate_requested,delete_requested
                from check_tasks where task_id=? and current_attempt=?""", (task_id, int(attempt_no))).fetchone()
        if not row or row["status"] != "running":
            return ""
        if row["delete_requested"]:
            return "delete"
        if row["force_terminate_requested"]:
            return "terminate"
        if row["cancel_requested"]:
            return "cancel"
        if row["pause_requested"]:
            return "pause"
        return ""

    def complete_task_pause(self, task_id: str, attempt_no: int) -> bool:
        with self.connect() as db:
            changed = db.execute("""update check_tasks set status='paused',pause_requested=0,progress_stage='paused',
                progress_message='任务已在安全点暂停',heartbeat_at=?,worker_id='',lease_token='',
                partial_available=case when progress_current>0 then 1 else partial_available end
                where task_id=? and current_attempt=? and status='running' and pause_requested=1""",
                (now_iso(), task_id, int(attempt_no))).rowcount
        return bool(changed)

    def complete_force_termination(self, task_id: str, attempt_no: int, message: str = "任务已强制终止") -> bool:
        with self.connect() as db:
            changed = db.execute("""update check_tasks set status='terminated',force_terminate_requested=0,pause_requested=0,
                cancel_requested=0,failure_type='force_terminated',error_message=?,progress_stage='terminated',
                progress_message=?,finished_at=?,heartbeat_at=?,worker_id='',lease_token='',
                partial_available=case when progress_current>0 then 1 else partial_available end
                where task_id=? and current_attempt=? and status='running'""",
                (message, message, now_iso(), now_iso(), task_id, int(attempt_no))).rowcount
        return bool(changed)

    def save_task_case(self, task_id: str, attempt_no: int, case: dict[str, Any]) -> None:
        with self.connect() as db:
            db.execute("""insert or replace into task_case_results(task_id,attempt_no,report_id,owner_user_id,department_id,group_id,risk_level,result_json,created_at)
                values (?,?,?,?,?,?,?,?,?)""", (task_id, int(attempt_no), str(case.get("report_id") or ""), str(case.get("owner_user_id") or ""),
                str(case.get("department_id") or ""), str(case.get("group_id") or ""), str(case.get("overall_risk") or "normal"),
                json.dumps(case, ensure_ascii=False), now_iso()))
            db.execute("update check_tasks set partial_available=1 where task_id=?", (task_id,))

    def save_task_missing_results(self, task_id: str, attempt_no: int, rows: list[dict[str, Any]]) -> None:
        with self.connect() as db:
            db.execute("delete from task_missing_results where task_id=? and attempt_no=?", (task_id, int(attempt_no)))
            for index, row in enumerate(rows):
                db.execute("""insert into task_missing_results(task_id,attempt_no,row_no,user_id,department_id,group_id,result_json)
                    values (?,?,?,?,?,?,?)""", (task_id, int(attempt_no), index, str(row.get("user_id") or ""),
                    str(row.get("department_id") or ""), str(row.get("group_id") or ""), json.dumps(row, ensure_ascii=False)))
            if rows:
                db.execute("update check_tasks set partial_available=1 where task_id=?", (task_id,))

    def finish_task(self, task_id: str, status: str, result: dict[str, Any], error: str = "", failure_type: str = "", attempt_no: int | None = None) -> bool:
        summary = result.get("summary", {}) if isinstance(result, dict) else {}
        with self.connect() as db:
            active_attempt = int(attempt_no or (db.execute("select current_attempt from check_tasks where task_id=?", (task_id,)).fetchone() or {"current_attempt": 1})["current_attempt"] or 1)
            has_incremental = db.execute("select 1 from task_case_results where task_id=? and attempt_no=? limit 1", (task_id, active_attempt)).fetchone()
            stored_result = {"summary": summary} if has_incremental else result
            where = "task_id=?"
            values: list[Any] = [status, json.dumps(stored_result, ensure_ascii=False), json.dumps(summary, ensure_ascii=False),
                error, failure_type, now_iso(), now_iso(), status, error or ("查重完成" if status == "finished" else status), task_id]
            if attempt_no is not None:
                where += " and current_attempt=? and status='running'"
                values.append(int(attempt_no))
                if status == "finished":
                    where += " and cancel_requested=0 and pause_requested=0 and force_terminate_requested=0"
                elif status != "cancelled":
                    where += " and force_terminate_requested=0"
            changed = db.execute(f"""update check_tasks set status=?,result_json=?,summary_json=?,error_message=?,failure_type=?,finished_at=?,
                heartbeat_at=?,progress_stage=?,progress_message=?,worker_id='',lease_token='',
                pause_requested=0,force_terminate_requested=0,
                partial_available=case when progress_current>0 then 1 else partial_available end
                where {where}""", values).rowcount
        return bool(changed)

    def list_tasks(self, user: User, filters: dict[str, Any] | None = None, include_hidden: bool = True) -> list[dict[str, Any]]:
        filters = filters or {}
        where, values = ["(t.created_by=? or t.trigger_source='system_daily')", "t.delete_requested=0"], [user.user_id]
        if not include_hidden:
            where.append("not exists (select 1 from task_hidden_tasks h where h.user_id=? and h.task_id=t.task_id)"); values.append(user.user_id)
        for key, column in (("status", "t.status"), ("start", "t.created_at >="), ("end", "t.created_at <=")):
            value = str(filters.get(key) or "")
            if value:
                if key in {"start", "end"}: where.append(column + " ?")
                else: where.append(column + "=?")
                values.append(value)
        columns = """t.task_id,t.created_by,t.department_id,t.task_name,t.task_date,t.trigger_source,t.report_date,t.result_dir,
            t.status,t.error_message,t.failure_type,t.created_at,t.queued_at,t.started_at,t.finished_at,t.heartbeat_at,t.progress_updated_at,
            t.progress_stage,t.progress_current,t.progress_total,t.progress_message,t.cancel_requested,t.pause_requested,
            t.force_terminate_requested,t.resume_from_checkpoint,t.delete_requested,t.partial_available,
            t.attempt_count,t.current_attempt,t.summary_json,t.payload_json,coalesce(u.display_name,'系统') creator_name"""
        with self.connect() as db: rows = db.execute("select " + columns + " from check_tasks t left join users u on u.user_id=t.created_by where " + " and ".join(where) + " order by t.created_at desc limit 200", values).fetchall()
        out = []
        for row in rows:
            d = dict(row); d["payload"] = json.loads(d.pop("payload_json", "{}") or "{}"); d["summary"] = json.loads(d.pop("summary_json", "{}") or "{}"); out.append(d)
        return out

    def get_task(self, task_id: str, user: User | None = None) -> dict[str, Any] | None:
        where, values = ["task_id=?"], [task_id]
        if user: where.append("(created_by=? or trigger_source='system_daily')"); values.append(user.user_id)
        with self.connect() as db: row = db.execute("select * from check_tasks where " + " and ".join(where), values).fetchone()
        if not row: return None
        return self._decode_task(row, include_results=True)

    def get_task_status(self, task_id: str, user: User | None = None) -> dict[str, Any] | None:
        where, values = ["task_id=?"], [task_id]
        if user:
            where.append("(created_by=? or trigger_source='system_daily')")
            values.append(user.user_id)
        columns = """task_id,created_by,department_id,task_name,task_date,trigger_source,report_date,result_dir,
            status,error_message,failure_type,created_at,queued_at,started_at,finished_at,heartbeat_at,progress_updated_at,
            progress_stage,progress_current,progress_total,progress_message,cancel_requested,pause_requested,
            force_terminate_requested,resume_from_checkpoint,delete_requested,partial_available,
            attempt_count,current_attempt,summary_json"""
        with self.connect() as db:
            row = db.execute("select " + columns + " from check_tasks where " + " and ".join(where), values).fetchone()
        if not row:
            return None
        task = dict(row)
        task["summary"] = json.loads(task.pop("summary_json", "{}") or "{}")
        current, total = int(task.get("progress_current") or 0), int(task.get("progress_total") or 0)
        task["progress_percent"] = round(current * 100 / total, 1) if total else 0.0
        started = str(task.get("started_at") or "")
        ended = str(task.get("finished_at") or now_iso())
        try:
            task["elapsed_seconds"] = max(0, int((datetime.fromisoformat(ended) - datetime.fromisoformat(started)).total_seconds())) if started else 0
        except ValueError:
            task["elapsed_seconds"] = 0
        task["effective_status"] = self._effective_task_status(task)
        return task

    @staticmethod
    def _effective_task_status(task: dict[str, Any]) -> str:
        if task.get("status") == "running":
            if task.get("delete_requested"):
                return "deleting"
            if task.get("force_terminate_requested"):
                return "terminating"
            if task.get("pause_requested"):
                return "pausing"
            if task.get("cancel_requested"):
                return "cancelling"
        return str(task.get("status") or "")

    def list_admin_tasks(self, filters: dict[str, Any] | None = None, cursor: str = "", limit: int = 50) -> dict[str, Any]:
        filters = filters or {}
        view = str(filters.get("view") or "active")
        if view not in {"active", "history"}:
            raise ValueError("invalid task console view")
        where: list[str] = ["t.status in ('queued','running','paused')" if view == "active" else "t.status not in ('queued','running','paused')"]
        values: list[Any] = []
        status = str(filters.get("status") or "").strip()
        creator = str(filters.get("creator") or "").strip()
        trigger = str(filters.get("trigger_source") or "").strip()
        keyword = str(filters.get("q") or "").strip()
        if status:
            where.append("t.status=?"); values.append(status)
        if creator:
            where.append("t.created_by=?"); values.append(creator)
        if trigger:
            where.append("t.trigger_source=?"); values.append(trigger)
        if keyword:
            where.append("(t.task_id like ? or t.task_name like ? or coalesce(u.username,'') like ? or coalesce(u.display_name,'') like ?)")
            like = f"%{keyword}%"; values.extend([like, like, like, like])
        if view == "history" and cursor:
            try:
                cursor_created, cursor_id = cursor.rsplit("|", 1)
            except ValueError as exc:
                raise ValueError("invalid task cursor") from exc
            where.append("(t.created_at<? or (t.created_at=? and t.task_id<?))")
            values.extend([cursor_created, cursor_created, cursor_id])
        columns = """t.task_id,t.created_by,t.department_id,t.task_name,t.task_date,t.trigger_source,t.report_date,
            t.status,t.error_message,t.failure_type,t.created_at,t.queued_at,t.started_at,t.finished_at,t.heartbeat_at,t.progress_updated_at,
            t.progress_stage,t.progress_current,t.progress_total,t.progress_message,t.cancel_requested,t.pause_requested,
            t.force_terminate_requested,t.resume_from_checkpoint,t.delete_requested,t.partial_available,t.attempt_count,t.current_attempt,
            t.priority,t.summary_json,t.payload_json,coalesce(u.username,'system') creator_username,
            coalesce(u.display_name,'系统') creator_name,coalesce(d.name,'') creator_department_name,
            coalesce(g.name,'') creator_group_name"""
        requested_limit = max(1, min(100, int(limit or 50)))
        sql_limit = requested_limit + 1 if view == "history" else -1
        order = "t.priority desc,t.queued_at,t.created_at" if view == "active" else "t.created_at desc,t.task_id desc"
        sql = "select " + columns + " from check_tasks t left join users u on u.user_id=t.created_by left join departments d on d.department_id=u.department_id left join groups g on g.group_id=u.group_id where " + " and ".join(where) + " order by " + order
        if sql_limit > 0:
            sql += " limit ?"; values.append(sql_limit)
        with self.connect() as db:
            rows = db.execute(sql, values).fetchall()
            count_rows = db.execute("select status,count(*) amount from check_tasks group by status").fetchall()
            queue_rows = db.execute("select task_id from check_tasks where status='queued' order by priority desc,queued_at,created_at").fetchall()
        has_more = view == "history" and len(rows) > requested_limit
        rows = rows[:requested_limit] if view == "history" else rows
        queue_positions = {row["task_id"]: index for index, row in enumerate(queue_rows, 1)}
        tasks: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json", "{}") or "{}")
            item["summary"] = json.loads(item.pop("summary_json", "{}") or "{}")
            current, total = int(item.get("progress_current") or 0), int(item.get("progress_total") or 0)
            item["progress_percent"] = round(current * 100 / total, 1) if total else 0.0
            item["effective_status"] = self._effective_task_status(item)
            item["queue_position"] = queue_positions.get(item["task_id"])
            tasks.append(item)
        next_cursor = f"{rows[-1]['created_at']}|{rows[-1]['task_id']}" if has_more and rows else ""
        counts = {str(row["status"]): int(row["amount"]) for row in count_rows}
        return {"tasks": tasks, "counts": counts, "next_cursor": next_cursor}

    def get_worker_lease(self) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("select worker_id,heartbeat_at from worker_leases where lease_name='duplicate-worker'").fetchone()
        return dict(row) if row else None

    def start_llm_call(self, call_id: str, task_id: str, started_at: str, permit_wait_ms: int = 0) -> None:
        with self.connect() as db:
            db.execute("""insert or replace into llm_call_metrics(call_id,task_id,started_at,finished_at,duration_ms,total_duration_ms,permit_wait_ms,ok,error_type)
                values (?,?,?,'',0,0,?,null,'')""", (call_id, task_id, started_at, max(0, int(permit_wait_ms))))

    def finish_llm_call(self, call_id: str, finished_at: str, duration_ms: int, ok: bool, error_type: str = "", total_duration_ms: int = 0) -> None:
        with self.connect() as db:
            db.execute("update llm_call_metrics set finished_at=?,duration_ms=?,total_duration_ms=?,ok=?,error_type=? where call_id=?",
                (finished_at, max(0, int(duration_ms)), max(0, int(total_duration_ms)), 1 if ok else 0, str(error_type or "")[:80], call_id))

    def update_llm_permit_metrics(self, snapshot: dict[str, Any]) -> None:
        prune_metrics = self._claim_maintenance_slot("_last_metrics_cleanup_at", 3600)
        with self.connect() as db:
            db.execute("""insert into llm_permit_metrics(singleton,updated_at,global_limit,active_total,waiting_total,active_by_task_json,waiting_by_task_json)
                values (1,?,?,?,?,?,?) on conflict(singleton) do update set updated_at=excluded.updated_at,global_limit=excluded.global_limit,
                active_total=excluded.active_total,waiting_total=excluded.waiting_total,active_by_task_json=excluded.active_by_task_json,
                waiting_by_task_json=excluded.waiting_by_task_json""", (
                now_iso(), max(1, int(snapshot.get("global_limit") or 1)), max(0, int(snapshot.get("active_total") or 0)),
                max(0, int(snapshot.get("waiting_total") or 0)), json.dumps(snapshot.get("active_by_task") or {}, ensure_ascii=False),
                json.dumps(snapshot.get("waiting_by_task") or {}, ensure_ascii=False),
            ))
            if prune_metrics:
                cutoff = (datetime.fromisoformat(now_iso()) - timedelta(days=7)).isoformat()
                db.execute("delete from llm_call_metrics where started_at<?", (cutoff,))
                db.execute("delete from task_stage_metrics where finished_at<?", (cutoff,))

    def resource_monitor_summary(self, retention_days: int = 7) -> dict[str, Any]:
        cutoff = (datetime.fromisoformat(now_iso()) - timedelta(days=max(1, min(30, int(retention_days))))).isoformat()
        started = datetime.fromisoformat(now_iso())
        with self.connect() as db:
            probe_started = datetime.now()
            db.execute("select 1").fetchone()
            sqlite_latency_ms = round((datetime.now() - probe_started).total_seconds() * 1000, 2)
            count_rows = db.execute("select status,count(*) amount from check_tasks group by status").fetchall()
            active_rows = db.execute("""select task_id,task_name,status,queued_at,started_at,heartbeat_at,progress_updated_at,
                progress_stage,progress_current,progress_total,progress_message from check_tasks
                where status in ('queued','running','paused') order by priority desc,queued_at,created_at""").fetchall()
            recent_rows = db.execute("""select started_at,finished_at,status from check_tasks
                where started_at!='' and finished_at!='' and finished_at>=? order by finished_at desc limit 500""", (cutoff,)).fetchall()
            llm_rows = db.execute("""select call_id,task_id,started_at,finished_at,duration_ms,total_duration_ms,permit_wait_ms,ok,error_type
                from llm_call_metrics where started_at>=? order by started_at desc limit 2000""", (cutoff,)).fetchall()
            permit_row = db.execute("select * from llm_permit_metrics where singleton=1").fetchone()
            stage_rows = db.execute("select stage,duration_seconds from task_stage_metrics where finished_at>=? order by finished_at desc limit 5000", (cutoff,)).fetchall()
        counts = {str(row["status"]): int(row["amount"]) for row in count_rows}
        tasks: list[dict[str, Any]] = []
        for row in active_rows:
            item = dict(row)
            for source, target in (("heartbeat_at", "heartbeat_age_seconds"), ("progress_updated_at", "progress_stalled_seconds"), ("queued_at", "queued_seconds")):
                try:
                    item[target] = max(0, int((started - datetime.fromisoformat(str(item.get(source) or ""))).total_seconds()))
                except ValueError:
                    item[target] = 0
            tasks.append(item)
        durations = []
        failure_count = 0
        for row in recent_rows:
            try:
                durations.append(max(0, int((datetime.fromisoformat(row["finished_at"]) - datetime.fromisoformat(row["started_at"])).total_seconds())))
            except ValueError:
                pass
            if row["status"] in {"failed", "terminated"}:
                failure_count += 1
        finished_llm = [dict(row) for row in llm_rows if row["finished_at"]]
        active_llm = [dict(row) for row in llm_rows if not row["finished_at"]]
        llm_durations = sorted(int(row["duration_ms"] or 0) for row in finished_llm)
        def percentile(values: list[int], fraction: float) -> int:
            if not values:
                return 0
            return values[min(len(values) - 1, max(0, int(round((len(values) - 1) * fraction))))]
        for row in active_llm:
            try:
                row["age_seconds"] = max(0, int((started - datetime.fromisoformat(row["started_at"])).total_seconds()))
            except ValueError:
                row["age_seconds"] = 0
            row.pop("call_id", None)
        stage_values: dict[str, list[int]] = {}
        for row in stage_rows:
            stage_values.setdefault(str(row["stage"] or "unknown"), []).append(int(row["duration_seconds"] or 0))
        stage_p95 = {stage: percentile(sorted(values), .95) for stage, values in stage_values.items()}
        permit = dict(permit_row) if permit_row else {}
        if permit:
            permit["active_by_task"] = json.loads(permit.pop("active_by_task_json", "{}") or "{}")
            permit["waiting_by_task"] = json.loads(permit.pop("waiting_by_task_json", "{}") or "{}")
        http_active = int(permit.get("active_total", len(active_llm)))
        waiting_for_permit = int(permit.get("waiting_total", 0))
        return {
            "counts": {"running": counts.get("running", 0), "queued": counts.get("queued", 0), "paused": counts.get("paused", 0), "failed": counts.get("failed", 0), "terminated": counts.get("terminated", 0)},
            "active": tasks,
            "oldest_queue_seconds": max([int(item.get("queued_seconds") or 0) for item in tasks if item.get("status") == "queued"] or [0]),
            "recent": {"count": len(recent_rows), "failure_count": failure_count, "average_duration_seconds": round(sum(durations) / len(durations), 1) if durations else 0},
            "stage_p95_seconds": stage_p95,
            "sqlite": {
                # A SELECT probe cannot reveal time spent waiting for SQLite's
                # single writer lock, so do not present it as write latency.
                "read_latency_ms": sqlite_latency_ms,
                "latency_ms": sqlite_latency_ms,
                "size_bytes": self.db_path.stat().st_size if self.db_path.exists() else 0,
                "wal_size_bytes": Path(str(self.db_path) + "-wal").stat().st_size if Path(str(self.db_path) + "-wal").exists() else 0,
            },
            "llm": {
                # Call rows begin only when HTTP begins; the Permit Server
                # snapshot remains the authoritative current-concurrency view.
                "active_count": http_active, "http_active": http_active,
                "waiting_for_permit": waiting_for_permit,
                "executor_inflight": http_active + waiting_for_permit,
                "active": active_llm,
                "permit": permit,
                "sample_count": len(finished_llm),
                "success_count": sum(1 for row in finished_llm if row["ok"] == 1),
                "error_count": sum(1 for row in finished_llm if row["ok"] == 0),
                "p50_ms": percentile(llm_durations, .50), "p95_ms": percentile(llm_durations, .95),
                "last_success_at": next((str(row["finished_at"]) for row in finished_llm if row["ok"] == 1), ""),
            },
        }

    def admin_pause_task(self, task_id: str) -> str:
        with self.connect() as db:
            db.execute("begin immediate")
            task = db.execute("select status from check_tasks where task_id=?", (task_id,)).fetchone()
            if not task:
                raise ValueError("task_not_found")
            if task["status"] == "queued":
                db.execute("""update check_tasks set status='paused',pause_requested=0,progress_stage='paused',
                    progress_message='排队任务已暂停' where task_id=? and status='queued'""", (task_id,))
                return "paused"
            if task["status"] == "running":
                changed = db.execute("""update check_tasks set pause_requested=1,
                    progress_message='等待当前日报检查完成后暂停' where task_id=? and status='running'
                    and pause_requested=0 and cancel_requested=0 and force_terminate_requested=0""", (task_id,)).rowcount
                if not changed:
                    raise ValueError("task_already_pausing")
                return "pausing"
            raise ValueError("task_not_pauseable")

    def admin_resume_task(self, task_id: str) -> str:
        with self.connect() as db:
            db.execute("begin immediate")
            task = db.execute("select status from check_tasks where task_id=?", (task_id,)).fetchone()
            if not task:
                raise ValueError("task_not_found")
            if task["status"] != "paused":
                raise ValueError("task_not_resumable")
            db.execute("""update check_tasks set status='queued',queued_at=?,resume_from_checkpoint=1,pause_requested=0,
                force_terminate_requested=0,progress_stage='queued',progress_message='等待后台 worker 从检查点恢复'
                where task_id=? and status='paused'""", (now_iso(), task_id))
        return "queued"

    def admin_restart_task(self, task_id: str) -> str:
        restartable = {"paused", "failed", "cancelled", "terminated"}
        with self.connect() as db:
            db.execute("begin immediate")
            task = db.execute("select * from check_tasks where task_id=?", (task_id,)).fetchone()
            if not task:
                raise ValueError("task_not_found")
            if task["status"] not in restartable:
                raise ValueError("task_not_restartable")
            if task["trigger_source"] == "manual":
                active = db.execute("""select task_id from check_tasks where created_by=? and trigger_source='manual'
                    and task_id!=? and status in ('queued','running','paused') and delete_requested=0 limit 1""", (task["created_by"], task_id)).fetchone()
                if active:
                    raise ValueError(f"active_task_exists:{active['task_id']}")
            attempt = int(task["current_attempt"] or 1) + 1
            db.execute("""update check_tasks set status='queued',priority=case when trigger_source='system_daily' then 100 else 0 end,
                queued_at=?,started_at='',heartbeat_at='',worker_id='',lease_token='',progress_stage='queued',progress_current=0,
                progress_total=0,progress_message='等待后台 worker 重新执行',cancel_requested=0,pause_requested=0,
                force_terminate_requested=0,resume_from_checkpoint=0,failure_type='',error_message='',finished_at='',
                result_json='{}',summary_json='{}',partial_available=0,current_attempt=?,attempt_count=? where task_id=?""",
                (now_iso(), attempt, attempt, task_id))
        return "queued"

    def admin_force_terminate_task(self, task_id: str) -> str:
        with self.connect() as db:
            db.execute("begin immediate")
            task = db.execute("select status from check_tasks where task_id=?", (task_id,)).fetchone()
            if not task:
                raise ValueError("task_not_found")
            if task["status"] in {"queued", "paused"}:
                db.execute("""update check_tasks set status='terminated',failure_type='force_terminated',
                    error_message='管理员已强制终止任务',progress_stage='terminated',progress_message='任务已强制终止',
                    finished_at=?,pause_requested=0,force_terminate_requested=0 where task_id=? and status in ('queued','paused')""",
                    (now_iso(), task_id))
                return "terminated"
            if task["status"] == "running":
                changed = db.execute("""update check_tasks set force_terminate_requested=1,pause_requested=0,
                    progress_message='正在强制终止任务' where task_id=? and status='running' and force_terminate_requested=0""", (task_id,)).rowcount
                if not changed:
                    raise ValueError("task_already_terminating")
                return "terminating"
            raise ValueError("task_not_terminable")

    def get_task_summary_for_user(self, task_id: str, attempt_no: int, user: User) -> dict[str, int]:
        if user.role in {ROLE_MINISTER, ROLE_ADMIN}:
            case_scope, case_values = "1=1", []
            missing_scope, missing_values = "1=1", []
        elif user.role == ROLE_DIRECTOR:
            case_scope, case_values = "department_id=?", [user.department_id]
            missing_scope, missing_values = "department_id=?", [user.department_id]
        elif user.role == ROLE_GROUP_LEADER:
            case_scope, case_values = "(owner_user_id=? or group_id=?)", [user.user_id, user.group_id]
            missing_scope, missing_values = "(user_id=? or group_id=?)", [user.user_id, user.group_id]
        else:
            return {"report_count": 0, "high_risk_count": 0, "medium_risk_count": 0, "missing_report_count": 0}
        with self.connect() as db:
            case_rows = db.execute(
                f"select risk_level,result_json from task_case_results where task_id=? and attempt_no=? and {case_scope}",
                [task_id, int(attempt_no), *case_values],
            ).fetchall()
            missing = db.execute(f"select count(*) from task_missing_results where task_id=? and attempt_no=? and {missing_scope}",
                [task_id, int(attempt_no), *missing_values]).fetchone()[0]
        cases = [json.loads(row["result_json"]) for row in case_rows]
        return {
            "report_count": len(cases),
            "high_risk_count": sum(1 for row in case_rows if row["risk_level"] == "high"),
            "medium_risk_count": sum(1 for row in case_rows if row["risk_level"] == "medium"),
            "missing_report_count": int(missing or 0),
            **duplicate_usage_summary(cases),
        }

    def retry_task(self, task_id: str, user: User) -> bool:
        with self.connect() as db:
            db.execute("begin immediate")
            task = db.execute("select * from check_tasks where task_id=?", (task_id,)).fetchone()
            if not task or (task["trigger_source"] != "system_daily" and task["created_by"] != user.user_id):
                raise ValueError("task not found or forbidden")
            if task["status"] not in {"failed", "cancelled"}:
                return False
            # A crashed process may already have durable Local checkpoints in its
            # current attempt.  Requeue that same attempt so task_job can pass
            # them back to the pipeline; a normal retry without checkpoints
            # keeps the previous fresh-attempt behaviour.
            if task["status"] == "failed" and int(task["partial_available"] or 0):
                changed = db.execute("""update check_tasks set status='queued',queued_at=?,heartbeat_at='',worker_id='',lease_token='',
                    progress_stage='queued',progress_message='等待后台 worker 从检查点恢复',cancel_requested=0,
                    pause_requested=0,force_terminate_requested=0,resume_from_checkpoint=1,failure_type='',error_message='',finished_at=''
                    where task_id=? and status='failed'""", (now_iso(), task_id)).rowcount
                return bool(changed)
            attempt = int(task["current_attempt"] or 1) + 1
            changed = db.execute("""update check_tasks set status='queued',priority=case when trigger_source='system_daily' then 100 else 0 end,
                queued_at=?,started_at='',heartbeat_at='',worker_id='',lease_token='',progress_stage='queued',progress_current=0,
                progress_total=0,progress_message='等待后台 worker 重试',cancel_requested=0,failure_type='',error_message='',finished_at='',
                result_json='{}',summary_json='{}',partial_available=0,current_attempt=?,attempt_count=?
                where task_id=? and status in ('failed','cancelled')""", (now_iso(), attempt, attempt, task_id)).rowcount
        return bool(changed)

    def cancel_task(self, task_id: str, user: User) -> str:
        with self.connect() as db:
            db.execute("begin immediate")
            task = db.execute("select * from check_tasks where task_id=?", (task_id,)).fetchone()
            if not task or (task["trigger_source"] != "system_daily" and task["created_by"] != user.user_id):
                raise ValueError("task not found or forbidden")
            if task["status"] == "queued":
                db.execute("update check_tasks set status='cancelled',failure_type='cancelled',progress_stage='cancelled',progress_message='任务已取消',finished_at=? where task_id=? and status='queued'", (now_iso(), task_id))
                return "cancelled"
            if task["status"] == "running":
                db.execute("update check_tasks set cancel_requested=1,progress_message='正在取消任务' where task_id=? and status='running'", (task_id,))
                return "cancelling"
            if task["status"] == "paused":
                db.execute("""update check_tasks set status='cancelled',failure_type='cancelled',progress_stage='cancelled',
                    progress_message='任务已取消',finished_at=?,pause_requested=0 where task_id=? and status='paused'""", (now_iso(), task_id))
                return "cancelled"
            return str(task["status"])

    def get_user_by_id(self, user_id: str) -> User | None:
        if not user_id:
            return None
        with self.connect() as db:
            row = db.execute(self._user_select() + " where u.user_id=?", (user_id,)).fetchone()
        return self._row_to_user(row) if row else None

    def _decode_task(self, row: sqlite3.Row, include_results: bool) -> dict[str, Any]:
        d = dict(row)
        d["payload"] = json.loads(d.get("payload_json") or "{}")
        d["config_snapshot"] = json.loads(d.get("config_snapshot_json") or "{}")
        d["summary"] = json.loads(d.get("summary_json") or "{}")
        if include_results:
            attempt = int(d.get("current_attempt") or 1)
            with self.connect() as db:
                case_rows = db.execute("select result_json from task_case_results where task_id=? and attempt_no=? order by created_at,report_id", (d["task_id"], attempt)).fetchall()
                missing_rows = db.execute("select result_json from task_missing_results where task_id=? and attempt_no=? order by row_no", (d["task_id"], attempt)).fetchall()
            if case_rows or missing_rows:
                d["result"] = {"summary": d["summary"], "report_cases": [json.loads(item["result_json"]) for item in case_rows], "missing_reports": [json.loads(item["result_json"]) for item in missing_rows]}
            else:
                d["result"] = json.loads(d.get("result_json") or "{}")
        return d

    def clear_task_page_display(self, user: User) -> None:
        with self.connect() as db:
            db.execute("insert or ignore into task_hidden_tasks(user_id,task_id) select ?,task_id from check_tasks where created_by=?", (user.user_id, user.user_id))

    @staticmethod
    def _delete_task_rows(db: sqlite3.Connection, task_id: str) -> None:
        db.execute("delete from task_reports where task_id=?", (task_id,))
        db.execute("delete from task_case_results where task_id=?", (task_id,))
        db.execute("delete from task_missing_results where task_id=?", (task_id,))
        db.execute("delete from task_hidden_tasks where task_id=?", (task_id,))
        db.execute("delete from check_tasks where task_id=?", (task_id,))

    def delete_task(self, task_id: str, user: User) -> str:
        with self.connect() as db:
            db.execute("begin immediate")
            task = db.execute("select * from check_tasks where task_id=?", (task_id,)).fetchone()
            if not task or task["created_by"] != user.user_id:
                raise ValueError("task not found or forbidden")
            if task["trigger_source"] == "system_daily":
                raise ValueError("系统自动任务不能删除")
            if task["status"] == "running":
                if not task["delete_requested"]:
                    db.execute("""update check_tasks set delete_requested=1,force_terminate_requested=1,
                        cancel_requested=0,pause_requested=0,progress_stage='deleting',
                        progress_message='正在停止任务并删除' where task_id=? and status='running'""", (task_id,))
                db.execute("insert or ignore into task_hidden_tasks(user_id,task_id) values (?,?)", (user.user_id, task_id))
                return "deleting"
            self._delete_task_rows(db, task_id)
        return "deleted"

    def complete_task_deletion(self, task_id: str, attempt_no: int) -> bool:
        with self.connect() as db:
            db.execute("begin immediate")
            task = db.execute("""select 1 from check_tasks where task_id=? and current_attempt=?
                and status='running' and delete_requested=1""", (task_id, int(attempt_no))).fetchone()
            if not task:
                return False
            self._delete_task_rows(db, task_id)
        return True

    def reap_task_deletions(self, stale_before: str) -> list[str]:
        with self.connect() as db:
            db.execute("begin immediate")
            rows = db.execute("""select task_id from check_tasks where delete_requested=1
                and (status!='running' or coalesce(heartbeat_at,'')<?)""", (stale_before,)).fetchall()
            task_ids = [str(row["task_id"]) for row in rows]
            for pending_task_id in task_ids:
                self._delete_task_rows(db, pending_task_id)
        return task_ids

    def create_reset_confirmation(self, user_id: str) -> str:
        cid = token("RC_", 20); expires = (datetime.fromisoformat(now_iso()) + timedelta(minutes=3)).isoformat(timespec="seconds")
        with self.connect() as db: db.execute("delete from reset_confirmations where expires_at<=?", (now_iso(),)); db.execute("insert into reset_confirmations(confirmation_id,user_id,expires_at) values (?,?,?)", (cid, user_id, expires))
        return cid

    def consume_reset_confirmation(self, confirmation_id: str, user_id: str) -> bool:
        with self.connect() as db:
            row = db.execute("select 1 from reset_confirmations where confirmation_id=? and user_id=? and expires_at>?", (confirmation_id, user_id, now_iso())).fetchone()
            db.execute("delete from reset_confirmations where confirmation_id=?", (confirmation_id,))
        return bool(row)

    def clear_business_data(self) -> dict[str, int]:
        tables = ("task_case_results", "task_missing_results", "task_reports", "task_page_clears", "task_hidden_tasks", "check_tasks", "exports", "artifacts", "reports", "testcase_snapshots", "sessions", "reset_confirmations")
        counts: dict[str, int] = {}
        with self.connect() as db:
            if db.execute("select 1 from check_tasks where status in ('queued','running','paused') limit 1").fetchone():
                raise ValueError("存在排队、运行或暂停中的查重任务，不能清空业务数据")
            for table in tables:
                counts[table] = int(db.execute(f"select count(*) from {table}").fetchone()[0]); db.execute(f"delete from {table}")
        return counts

    def save_export(self, payload: dict[str, Any]) -> str:
        eid = str(payload.get("export_id") or token("E_", 14))
        with self.connect() as db: db.execute("insert into exports(export_id,export_type,created_by,department_scope,start_date,end_date,stored_path,created_at) values (?,?,?,?,?,?,?,?)", (eid, payload["export_type"], payload["created_by"], payload.get("department_scope", ""), payload.get("start_date", ""), payload.get("end_date", ""), payload["stored_path"], now_iso()))
        return eid

    def get_settings(self, defaults: dict[str, Any]) -> dict[str, Any]:
        with self.connect() as db: rows = db.execute("select setting_key,setting_value from system_settings").fetchall()
        data = dict(defaults)
        for row in rows:
            try: data[row["setting_key"]] = json.loads(row["setting_value"])
            except Exception: data[row["setting_key"]] = row["setting_value"]
        return data

    def update_settings(self, payload: dict[str, Any]) -> None:
        with self.connect() as db:
            for key, value in payload.items(): db.execute("insert or replace into system_settings(setting_key,setting_value,updated_at) values (?,?,?)", (str(key), json.dumps(value, ensure_ascii=False), now_iso()))

    # Append-only administration audit trail -----------------------------------------
    @staticmethod
    def _audit_cutoff() -> str:
        return (shanghai_now() - timedelta(days=180)).replace(tzinfo=None).isoformat(timespec="seconds")

    @staticmethod
    def _audit_cursor_encode(occurred_at: str, audit_id: str) -> str:
        raw = json.dumps([occurred_at, audit_id], ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    @staticmethod
    def _audit_cursor_decode(cursor: str) -> tuple[str, str]:
        try:
            raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
            occurred_at, audit_id = json.loads(raw.decode("utf-8"))
            return str(occurred_at), str(audit_id)
        except Exception as exc:
            raise ValueError("invalid audit cursor") from exc

    @staticmethod
    def _audit_where(filters: dict[str, Any]) -> tuple[list[str], list[Any]]:
        where, values = ["occurred_at >= ?"], [SQLiteBackend._audit_cutoff()]
        start, end = str(filters.get("start") or "").strip(), str(filters.get("end") or "").strip()
        if start:
            where.append("occurred_at >= ?"); values.append(start + ("T00:00:00" if len(start) == 10 else ""))
        if end:
            where.append("occurred_at <= ?"); values.append(end + ("T23:59:59" if len(end) == 10 else ""))
        for key in ("category", "severity", "outcome"):
            value = str(filters.get(key) or "").strip()
            if value:
                where.append(f"{key}=?"); values.append(value)
        actor = str(filters.get("actor") or "").strip()
        if actor:
            like = f"%{actor}%"
            where.append("(actor_username like ? or actor_display_name like ? or actor_user_id like ?)")
            values.extend([like, like, like])
        keyword = str(filters.get("q") or "").strip()
        if keyword:
            like = f"%{keyword}%"
            where.append("(action like ? or summary like ? or target_type like ? or target_id like ? or target_label like ? or actor_username like ? or actor_display_name like ?)")
            values.extend([like] * 7)
        return where, values

    @staticmethod
    def _decode_audit_row(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        try:
            item["changes"] = json.loads(item.pop("changes_json", "{}") or "{}")
        except (TypeError, ValueError):
            item["changes"] = {}
        return item

    def append_audit_log(self, payload: dict[str, Any]) -> str:
        audit_id = str(payload.get("audit_id") or token("A_", 18))
        fields = ("occurred_at", "severity", "category", "action", "outcome", "actor_user_id", "actor_username",
                  "actor_display_name", "actor_role", "target_type", "target_id", "target_label", "summary",
                  "changes_json", "error_type", "client_ip", "user_agent")
        values = {
            "occurred_at": str(payload.get("occurred_at") or shanghai_now().replace(tzinfo=None).isoformat(timespec="milliseconds")),
            "severity": str(payload.get("severity") or "info"),
            "category": str(payload.get("category") or "business")[:64],
            "action": str(payload.get("action") or "unknown")[:128],
            "outcome": str(payload.get("outcome") or "success"),
            "actor_user_id": str(payload.get("actor_user_id") or "")[:128],
            "actor_username": str(payload.get("actor_username") or "")[:128],
            "actor_display_name": str(payload.get("actor_display_name") or "")[:128],
            "actor_role": str(payload.get("actor_role") or "")[:64],
            "target_type": str(payload.get("target_type") or "")[:64],
            "target_id": str(payload.get("target_id") or "")[:256],
            "target_label": str(payload.get("target_label") or "")[:256],
            "summary": str(payload.get("summary") or "")[:500],
            "changes_json": json.dumps(payload.get("changes") or {}, ensure_ascii=False, separators=(",", ":")),
            "error_type": str(payload.get("error_type") or "")[:500],
            "client_ip": str(payload.get("client_ip") or "")[:64],
            "user_agent": str(payload.get("user_agent") or "")[:512],
        }
        with self.connect() as db:
            db.execute("delete from audit_logs where occurred_at < ?", (self._audit_cutoff(),))
            db.execute(f"insert into audit_logs(audit_id,{','.join(fields)}) values ({','.join(['?'] * (len(fields) + 1))})",
                       [audit_id, *[values[field] for field in fields]])
        return audit_id

    def _audit_rows(self, filters: dict[str, Any], cursor: str = "", limit: int | None = None) -> list[sqlite3.Row]:
        where, values = self._audit_where(filters)
        if cursor:
            occurred_at, audit_id = self._audit_cursor_decode(cursor)
            where.append("(occurred_at < ? or (occurred_at=? and audit_id<?))")
            values.extend([occurred_at, occurred_at, audit_id])
        sql = "select * from audit_logs where " + " and ".join(where) + " order by occurred_at desc,audit_id desc"
        if limit is not None:
            sql += " limit ?"; values.append(int(limit))
        with self.connect() as db:
            return db.execute(sql, values).fetchall()

    def list_audit_logs(self, filters: dict[str, Any], cursor: str = "", limit: int = 50) -> dict[str, Any]:
        page_size = max(1, min(int(limit or 50), 200))
        rows = self._audit_rows(filters, cursor, page_size + 1)
        has_more = len(rows) > page_size
        visible = rows[:page_size]
        where, values = self._audit_where(filters)
        with self.connect() as db:
            stats_row = db.execute("select count(*) total, sum(case when outcome='failure' then 1 else 0 end) failed, "
                                   "sum(case when severity='critical' then 1 else 0 end) critical, "
                                   "sum(case when action='auth.login' and outcome='failure' then 1 else 0 end) login_failed "
                                   "from audit_logs where " + " and ".join(where), values).fetchone()
        stats = {key: int(stats_row[key] or 0) for key in ("total", "failed", "critical", "login_failed")}
        next_cursor = self._audit_cursor_encode(visible[-1]["occurred_at"], visible[-1]["audit_id"]) if has_more and visible else ""
        return {"items": [self._decode_audit_row(row) for row in visible], "next_cursor": next_cursor, "stats": stats}

    def export_audit_logs(self, filters: dict[str, Any]) -> list[dict[str, Any]]:
        return [self._decode_audit_row(row) for row in self._audit_rows(filters)]

    # Structured runtime diagnostics -----------------------------------------------
    @staticmethod
    def _runtime_cutoff() -> str:
        return (shanghai_now() - timedelta(days=7)).replace(tzinfo=None).isoformat(timespec="seconds")

    def _claim_maintenance_slot(self, field: str, interval_seconds: float) -> bool:
        now = time.monotonic()
        with self._maintenance_lock:
            if now - float(getattr(self, field)) < interval_seconds:
                return False
            setattr(self, field, now)
            return True

    @staticmethod
    def _decode_runtime_row(row: sqlite3.Row) -> dict[str, Any]:
        try:
            context = json.loads(row["context_json"] or "{}")
        except Exception:
            context = {}
        return {
            "seq": int(row["log_seq"]), "occurred_at": str(row["occurred_at"]), "level": str(row["level"]),
            "source": str(row["source"]), "event": str(row["event"]), "message": str(row["message"]),
            "task_id": str(row["task_id"] or ""), "attempt_no": int(row["attempt_no"] or 0),
            "context": context, "traceback": str(row["traceback_text"] or ""),
        }

    @staticmethod
    def _runtime_where(filters: dict[str, Any], after_seq: int = 0) -> tuple[list[str], list[Any]]:
        where, values = ["occurred_at >= ?"], [SQLiteBackend._runtime_cutoff()]
        start, end = str(filters.get("start") or "").strip(), str(filters.get("end") or "").strip()
        if start:
            where.append("occurred_at >= ?"); values.append(start + ("T00:00:00" if len(start) == 10 else ""))
        if end:
            where.append("occurred_at <= ?"); values.append(end + ("T23:59:59" if len(end) == 10 else ""))
        for key in ("level", "source"):
            value = str(filters.get(key) or "").strip()
            if value:
                where.append(f"{key}=?"); values.append(value)
        task_id = str(filters.get("task_id") or "").strip()
        if task_id:
            where.append("task_id like ?"); values.append(f"%{task_id}%")
        keyword = str(filters.get("q") or "").strip()
        if keyword:
            like = f"%{keyword}%"
            where.append("(event like ? or message like ? or task_id like ? or context_json like ? or traceback_text like ?)")
            values.extend([like] * 5)
        if after_seq > 0:
            where.append("log_seq > ?"); values.append(after_seq)
        return where, values

    def append_runtime_log(self, payload: dict[str, Any]) -> dict[str, Any]:
        fields = ("occurred_at", "level", "source", "event", "message", "task_id", "attempt_no", "context_json", "traceback_text")
        values = {
            "occurred_at": str(payload.get("occurred_at") or now_iso()), "level": str(payload.get("level") or "info"),
            "source": str(payload.get("source") or "web"), "event": str(payload.get("event") or "event"),
            "message": str(payload.get("message") or ""), "task_id": str(payload.get("task_id") or ""),
            "attempt_no": int(payload.get("attempt_no") or 0),
            "context_json": json.dumps(payload.get("context") or {}, ensure_ascii=False, separators=(",", ":")),
            "traceback_text": str(payload.get("traceback") or ""),
        }
        prune_logs = self._claim_maintenance_slot("_last_runtime_cleanup_at", 3600)
        with self.connect() as db:
            if prune_logs:
                db.execute("delete from runtime_logs where occurred_at < ?", (self._runtime_cutoff(),))
            cursor = db.execute(f"insert into runtime_logs({','.join(fields)}) values ({','.join(['?'] * len(fields))})", [values[field] for field in fields])
            row = db.execute("select * from runtime_logs where log_seq=?", (cursor.lastrowid,)).fetchone()
        return self._decode_runtime_row(row)

    def list_runtime_logs(self, filters: dict[str, Any], cursor: str = "", limit: int = 100) -> dict[str, Any]:
        page_size = max(1, min(200, int(limit)))
        where, values = self._runtime_where(filters)
        if cursor:
            try:
                cursor_seq = int(cursor)
            except ValueError as exc:
                raise ValueError("invalid runtime log cursor") from exc
            where.append("log_seq < ?"); values.append(cursor_seq)
        with self.connect() as db:
            rows = db.execute("select * from runtime_logs where " + " and ".join(where) + " order by log_seq desc limit ?", [*values, page_size + 1]).fetchall()
            latest = db.execute("select coalesce(max(log_seq),0) from runtime_logs").fetchone()[0]
        visible, has_more = rows[:page_size], len(rows) > page_size
        return {"items": [self._decode_runtime_row(row) for row in reversed(visible)], "next_cursor": str(visible[-1]["log_seq"]) if has_more and visible else "", "latest_seq": int(latest or 0)}

    def stream_runtime_logs(self, filters: dict[str, Any], after_seq: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        where, values = self._runtime_where(filters, max(0, int(after_seq)))
        with self.connect() as db:
            rows = db.execute("select * from runtime_logs where " + " and ".join(where) + " order by log_seq asc limit ?", [*values, max(1, min(200, int(limit)))]).fetchall()
        return [self._decode_runtime_row(row) for row in rows]

    def _row_to_user(self, row: sqlite3.Row) -> User:
        user = User(row["user_id"], row["username"], row["display_name"], row["department_id"] or "", row["department_name"] or "", row["role"], row["manager_user_id"] or "", bool(row["enabled"]), row["group_id"] or "", row["group_name"] or "", "", bool(row["is_test_account"]))
        manager = self.get_derived_manager(user) if user.role in BUSINESS_ROLES else None
        user.manager_user_id = manager.user_id if manager else ""; user.manager_display_name = manager.display_name if manager else ""
        return user

    @staticmethod
    def _user_dict(user: User) -> dict[str, Any]: return user.to_dict()

    @staticmethod
    def _row_to_report(row: sqlite3.Row) -> Report:
        return Report(report_id=row["report_id"], owner_user_id=row["owner_user_id"], employee_name=row["employee_name"], department_id=row["department_id"] or "", department_name=row["department_name"] or "", report_date=row["report_date"], manager=row["manager"] or "", title_summary=row["title_summary"], work_description=row["work_description"], source_type=row["source_type"] or "web", original_filename=row["original_filename"] or "", stored_path=row["stored_path"] or "", file_size=int(row["file_size"] or 0), content_hash=row["content_hash"] or "", code_items=json.loads(row["code_items_json"] or "[]"), document_items=json.loads(row["document_items_json"] or "[]"), testcase_items=json.loads(row["testcase_items_json"] or "[]"), status=row["status"] or "active", locked_by_task_id=row["locked_by_task_id"] or "", revision_of_report_id=row["revision_of_report_id"] or "", created_at=row["created_at"], updated_at=row["updated_at"] or "", withdrawn_at=row["withdrawn_at"] or "", group_id=row["group_id"] or "", group_name=row["group_name"] or "", submitter_role=row["submitter_role"] or ROLE_EMPLOYEE)

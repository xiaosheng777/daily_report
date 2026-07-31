from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.core.schemas import Report, User
from src.db.base import DatabaseBackend
from src.utils.dates import now_iso
from src.utils.security import hash_password, token, verify_password

ROLE_EMPLOYEE = "employee"
ROLE_GROUP_LEADER = "group_leader"
ROLE_DIRECTOR = "director"
ROLE_MINISTER = "minister"
ROLE_ADMIN = "admin"
MANAGER_ROLES = {ROLE_GROUP_LEADER, ROLE_DIRECTOR, ROLE_MINISTER}
BUSINESS_ROLES = MANAGER_ROLES | {ROLE_EMPLOYEE}


def can_manage(user: User) -> bool:
    return user.role in MANAGER_ROLES


def can_admin(user: User) -> bool:
    return user.role == ROLE_ADMIN


def can_access_department(user: User, department_id: str) -> bool:
    return user.role == ROLE_MINISTER or (user.role in {ROLE_GROUP_LEADER, ROLE_DIRECTOR} and user.department_id == department_id)


class SQLiteBackend(DatabaseBackend):
    """SQLite implementation with organisation-aware business visibility."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def initialize(self, seed_demo_users: bool = True) -> None:
        with self.connect() as db:
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
              payload_json text not null, config_snapshot_json text default '{}', result_json text default '{}',
              result_dir text default '', status text not null, error_message text default '', created_at text not null,
              finished_at text default ''
            );
            create table if not exists task_reports (task_id text not null, report_id text not null, primary key(task_id, report_id));
            create table if not exists task_page_clears (user_id text primary key, cleared_at text not null);
            create table if not exists task_hidden_tasks (user_id text not null, task_id text not null, primary key(user_id, task_id));
            create table if not exists reset_confirmations (confirmation_id text primary key, user_id text not null, expires_at text not null);
            create table if not exists exports (
              export_id text primary key, export_type text not null, created_by text not null,
              department_scope text default '', start_date text default '', end_date text default '', stored_path text not null, created_at text not null
            );
            create table if not exists system_settings (setting_key text primary key, setting_value text not null, updated_at text not null);
            """)
            for stmt in [
                "alter table users add column manager_user_id text default ''",
                "alter table users add column group_id text default ''",
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
                "alter table check_tasks add column config_snapshot_json text default '{}'",
                "alter table check_tasks add column result_dir text default ''",
                "alter table testcase_snapshots add column group_id text default ''",
                "alter table testcase_snapshots add column group_name text default ''",
            ]:
                try:
                    db.execute(stmt)
                except sqlite3.OperationalError:
                    pass
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
            db.execute("""insert or ignore into users(user_id,username,password_hash,display_name,department_id,group_id,manager_user_id,role,enabled,created_at)
                values ('u_admin','admin',?,'Admin','','','',?,1,?)""", (hash_password("admin123"), ROLE_ADMIN, now_iso()))

    # Authentication and organisation -------------------------------------------------
    def authenticate(self, username: str, password: str) -> User | None:
        with self.connect() as db:
            row = db.execute(self._user_select() + " where u.username = ?", (username,)).fetchone()
        return self._row_to_user(row) if row and row["enabled"] and verify_password(password, row["password_hash"]) else None

    def create_session(self, user_id: str, days: int = 7) -> str:
        sid = token("S_", 28); exp = (datetime.utcnow() + timedelta(days=days)).isoformat(timespec="seconds")
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

    def list_required_submitters(self, user: User) -> list[dict[str, Any]]:
        if user.role == ROLE_GROUP_LEADER:
            scope, values = "u.group_id=?", [user.group_id]
        elif user.role == ROLE_DIRECTOR:
            scope, values = "u.department_id=?", [user.department_id]
        elif user.role == ROLE_MINISTER:
            scope, values = "1=1", []
        else:
            return []
        sql = self._user_select() + f" where {scope} and u.enabled=1 and u.role in (?,?) order by d.name,g.name,u.display_name"
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
        task_date = datetime.now(timezone(timedelta(hours=8))).date().isoformat()
        with self.connect() as db:
            sequence = db.execute("select count(*) from check_tasks where created_by=? and task_date=?", (user.user_id, task_date)).fetchone()[0] + 1
            task_name = f"{int(task_date[5:7])}.{int(task_date[8:10])}查重({sequence})"
            db.execute("insert into check_tasks(task_id,created_by,department_id,task_name,task_date,payload_json,config_snapshot_json,result_dir,status,created_at) values (?,?,?,?,?,?,?,?, 'running',?)", (tid, user.user_id, dept, task_name, task_date, json.dumps(payload, ensure_ascii=False), json.dumps(config_snapshot, ensure_ascii=False), result_dir, now_iso()))
        return tid

    def finish_task(self, task_id: str, status: str, result: dict[str, Any], error: str = "") -> None:
        with self.connect() as db: db.execute("update check_tasks set status=?,result_json=?,error_message=?,finished_at=? where task_id=?", (status, json.dumps(result, ensure_ascii=False), error, now_iso(), task_id))

    def list_tasks(self, user: User, filters: dict[str, Any] | None = None, include_hidden: bool = True) -> list[dict[str, Any]]:
        filters = filters or {}; where, values = ["t.created_by=?"], [user.user_id]
        if not include_hidden:
            where.append("not exists (select 1 from task_hidden_tasks h where h.user_id=? and h.task_id=t.task_id)"); values.append(user.user_id)
        for key, column in (("status", "t.status"), ("start", "t.created_at >="), ("end", "t.created_at <=")):
            value = str(filters.get(key) or "")
            if value:
                if key in {"start", "end"}: where.append(column + " ?")
                else: where.append(column + "=?")
                values.append(value)
        with self.connect() as db: rows = db.execute("select t.*,u.display_name creator_name from check_tasks t join users u on u.user_id=t.created_by where " + " and ".join(where) + " order by t.created_at desc limit 200", values).fetchall()
        out = []
        for row in rows:
            d = dict(row); result = json.loads(d.pop("result_json", "{}") or "{}"); d["payload"] = json.loads(d.pop("payload_json", "{}") or "{}"); d["summary"] = result.get("summary", {}); out.append(d)
        return out

    def get_task(self, task_id: str, user: User | None = None) -> dict[str, Any] | None:
        where, values = ["task_id=?"], [task_id]
        if user: where.append("created_by=?"); values.append(user.user_id)
        with self.connect() as db: row = db.execute("select * from check_tasks where " + " and ".join(where), values).fetchone()
        if not row: return None
        d = dict(row); d["payload"] = json.loads(d.get("payload_json") or "{}"); d["config_snapshot"] = json.loads(d.get("config_snapshot_json") or "{}"); d["result"] = json.loads(d.get("result_json") or "{}"); return d

    def clear_task_page_display(self, user: User) -> None:
        with self.connect() as db:
            db.execute("insert or ignore into task_hidden_tasks(user_id,task_id) select ?,task_id from check_tasks where created_by=?", (user.user_id, user.user_id))

    def delete_task(self, task_id: str, user: User) -> None:
        if not self.get_task(task_id, user): raise ValueError("task not found or forbidden")
        with self.connect() as db:
            db.execute("delete from task_reports where task_id=?", (task_id,))
            db.execute("delete from task_hidden_tasks where task_id=?", (task_id,))
            db.execute("delete from check_tasks where task_id=?", (task_id,))

    def create_reset_confirmation(self, user_id: str) -> str:
        cid = token("RC_", 20); expires = (datetime.utcnow() + timedelta(minutes=3)).isoformat(timespec="seconds")
        with self.connect() as db: db.execute("delete from reset_confirmations where expires_at<=?", (now_iso(),)); db.execute("insert into reset_confirmations(confirmation_id,user_id,expires_at) values (?,?,?)", (cid, user_id, expires))
        return cid

    def consume_reset_confirmation(self, confirmation_id: str, user_id: str) -> bool:
        with self.connect() as db:
            row = db.execute("select 1 from reset_confirmations where confirmation_id=? and user_id=? and expires_at>?", (confirmation_id, user_id, now_iso())).fetchone()
            db.execute("delete from reset_confirmations where confirmation_id=?", (confirmation_id,))
        return bool(row)

    def clear_business_data(self) -> dict[str, int]:
        tables = ("task_reports", "task_page_clears", "task_hidden_tasks", "check_tasks", "exports", "artifacts", "reports", "testcase_snapshots", "sessions", "reset_confirmations")
        counts: dict[str, int] = {}
        with self.connect() as db:
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

    def _row_to_user(self, row: sqlite3.Row) -> User:
        user = User(row["user_id"], row["username"], row["display_name"], row["department_id"] or "", row["department_name"] or "", row["role"], row["manager_user_id"] or "", bool(row["enabled"]), row["group_id"] or "", row["group_name"] or "")
        manager = self.get_derived_manager(user) if user.role in BUSINESS_ROLES else None
        user.manager_user_id = manager.user_id if manager else ""; user.manager_display_name = manager.display_name if manager else ""
        return user

    @staticmethod
    def _user_dict(user: User) -> dict[str, Any]: return user.to_dict()

    @staticmethod
    def _row_to_report(row: sqlite3.Row) -> Report:
        return Report(report_id=row["report_id"], owner_user_id=row["owner_user_id"], employee_name=row["employee_name"], department_id=row["department_id"] or "", department_name=row["department_name"] or "", report_date=row["report_date"], manager=row["manager"] or "", title_summary=row["title_summary"], work_description=row["work_description"], source_type=row["source_type"] or "web", original_filename=row["original_filename"] or "", stored_path=row["stored_path"] or "", file_size=int(row["file_size"] or 0), content_hash=row["content_hash"] or "", code_items=json.loads(row["code_items_json"] or "[]"), document_items=json.loads(row["document_items_json"] or "[]"), testcase_items=json.loads(row["testcase_items_json"] or "[]"), status=row["status"] or "active", locked_by_task_id=row["locked_by_task_id"] or "", revision_of_report_id=row["revision_of_report_id"] or "", created_at=row["created_at"], updated_at=row["updated_at"] or "", withdrawn_at=row["withdrawn_at"] or "", group_id=row["group_id"] or "", group_name=row["group_name"] or "", submitter_role=row["submitter_role"] or ROLE_EMPLOYEE)

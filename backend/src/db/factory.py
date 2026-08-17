from __future__ import annotations

from src.db.base import DatabaseBackend
from src.db.sqlite_backend import SQLiteBackend


def create_database(config: dict) -> DatabaseBackend:
    database = config.get("database", {})
    backend = database.get("backend", "sqlite")
    if backend == "sqlite":
        db = SQLiteBackend(database.get("sqlite_path", "storage/daily_report.sqlite3"))
        db.initialize(seed_demo_users=bool(config.get("app", {}).get("seed_demo_users", True)))
        return db
    raise NotImplementedError(f"Database backend '{backend}' is reserved but not implemented yet. Implement DatabaseBackend and update factory.py.")

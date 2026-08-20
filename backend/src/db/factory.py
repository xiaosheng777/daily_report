from __future__ import annotations

from src.db.base import DatabaseBackend
from src.db.sqlite_backend import SQLiteBackend


def open_database(config: dict) -> DatabaseBackend:
    """Open a database backend without performing schema migration work."""
    database = config.get("database", {})
    backend = database.get("backend", "sqlite")
    if backend == "sqlite":
        return SQLiteBackend(database.get("sqlite_path", "storage/daily_report.sqlite3"))
    raise NotImplementedError(f"Database backend '{backend}' is reserved but not implemented yet. Implement DatabaseBackend and update factory.py.")


def create_database(config: dict) -> DatabaseBackend:
    """Open the backend and run startup migration/bootstrap work."""
    db = open_database(config)
    db.initialize(seed_demo_users=bool(config.get("app", {}).get("seed_demo_users", True)))
    return db

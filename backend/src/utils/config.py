from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any

import yaml


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def default_config_path() -> Path:
    """Prefer project-root config/ over backend/config/."""
    # backend/src/utils/config.py -> parents[2]=backend, parents[3]=project
    here = Path(__file__).resolve()
    backend_root = here.parents[2]
    project_root = here.parents[3]
    project_config = project_root / "config" / "config.yaml"
    backend_config = backend_root / "config" / "config.yaml"
    if project_config.exists():
        return project_config
    return backend_config


def load_config(config_path: str | Path | None = None) -> dict[str, Any]:
    path = Path(config_path).resolve() if config_path else default_config_path().resolve()
    data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    cfg = data or {}
    cfg["config_path"] = str(path)
    cfg["config_dir"] = str(path.parent)
    backend_root, project_root = _detect_roots(path)
    cfg["backend_root"] = str(backend_root)
    cfg["project_root"] = str(project_root)
    resolve_paths(cfg)
    return cfg


def _detect_roots(config_path: Path) -> tuple[Path, Path]:
    config_dir = config_path.parent
    parent = config_dir.parent
    # Preferred layout: <project>/config/config.yaml
    if config_dir.name == "config" and (parent / "backend" / "src").exists():
        return parent / "backend", parent
    # Docker / legacy layout: <project>/backend/config/config.yaml
    if config_dir.name == "config" and parent.name == "backend":
        return parent, parent.parent
    cwd = Path.cwd().resolve()
    if cwd.name == "backend":
        return cwd, cwd.parent
    if (cwd / "backend" / "src").exists():
        return cwd / "backend", cwd
    return parent, parent.parent


def resolve_paths(cfg: dict[str, Any]) -> None:
    backend_root = Path(cfg.get("backend_root") or ".").resolve()
    project_root = Path(cfg.get("project_root") or backend_root.parent).resolve()
    config_dir = Path(cfg.get("config_dir") or (project_root / "config")).resolve()
    for section, keys in {
        "storage": ["root_dir"],
        "database": ["sqlite_path"],
        "app": ["frontend_dir"],
        "jplag": ["jar_path"],
        "llm_judge": ["raw_log_path"],
    }.items():
        container = cfg.get(section, {})
        for key in keys:
            if not container.get(key):
                continue
            p = Path(str(container[key]))
            if not p.is_absolute():
                # Paths like ../storage remain relative to backend/
                container[key] = str((backend_root / p).resolve())
    llm = cfg.setdefault("llm_judge", {})
    if llm.get("api_key_file"):
        llm["api_key_file"] = _resolve_api_key_file(str(llm["api_key_file"]), config_dir, project_root, backend_root)
    cfg.setdefault("storage", {})["project_root"] = str(project_root)


def _resolve_api_key_file(value: str, config_dir: Path, project_root: Path, backend_root: Path) -> str:
    path = Path(value)
    if path.is_absolute():
        return str(path.resolve())
    candidates = [
        config_dir / path.name,          # config/llm_api_key next to config.yaml
        config_dir / path,               # relative to config dir
        project_root / path,             # project/config/llm_api_key
        backend_root / path,             # backend/config/llm_api_key (Docker mount)
    ]
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved.exists():
            return str(resolved)
    # Prefer same directory as the active config.yaml
    return str((config_dir / path.name).resolve())


def env_or_value(value: str | None, env_name: str | None) -> str:
    if env_name and os.getenv(env_name):
        return os.getenv(env_name, "")
    return value or ""


def stable_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

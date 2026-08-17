from __future__ import annotations

import hashlib
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from src.utils.dates import ym
from src.utils.security import token


@dataclass(slots=True)
class StoredFile:
    original_filename: str
    stored_path: str
    file_size: int
    content_hash: str


class FileStorage:
    def __init__(self, config: dict) -> None:
        storage = config.get("storage", {})
        self.root = Path(storage.get("root_dir", "storage")).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.config = storage

    def save_bytes(self, category: str, original_filename: str, data: bytes, date_hint: str | None = None, subdir: str = "") -> StoredFile:
        safe_name = self._safe_filename(original_filename or "file.bin")
        month = ym(date_hint) if date_hint else "undated"
        base = self.root / category
        if subdir:
            base = base / self._safe_component(subdir)
        base = base / month
        base.mkdir(parents=True, exist_ok=True)
        file_id = token("F_", 12)
        filename = f"{file_id}_{safe_name}"
        path = base / filename
        path.write_bytes(data)
        return StoredFile(original_filename, str(path.relative_to(self.root)), len(data), hashlib.sha256(data).hexdigest())

    def read(self, stored_path: str) -> bytes:
        path = self.resolve(stored_path)
        return path.read_bytes()

    def resolve(self, stored_path: str) -> Path:
        path = (self.root / stored_path).resolve()
        if self.root not in path.parents and path != self.root:
            raise ValueError("invalid stored path")
        return path

    def copy_to(self, stored_path: str, dst: Path) -> None:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.resolve(stored_path), dst)

    @staticmethod
    def _safe_filename(name: str) -> str:
        name = Path(name).name.strip() or "file.bin"
        return re.sub(r"[^\w.\-\u4e00-\u9fff]+", "_", name)[:160]

    @staticmethod
    def _safe_component(value: str) -> str:
        return re.sub(r"[^\w.\-]+", "_", str(value or "default"))[:80]

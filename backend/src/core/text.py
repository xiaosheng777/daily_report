from __future__ import annotations

import hashlib
import re

def normalize_text(*parts: str) -> str:
    text = "\n".join(str(part or "") for part in parts).lower()
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"[，。！？、；：,.!?;:\\/\'\"`~@#$%^&*()\[\]{}<>|]+", " ", text)
    return text.strip()


def stable_hash(text: str) -> str:
    return hashlib.sha256(normalize_text(text).encode("utf-8")).hexdigest()


def low_information(title: str, description: str, min_len: int = 10) -> bool:
    text = normalize_text(title, description).replace(" ", "")
    return len(text) < min_len

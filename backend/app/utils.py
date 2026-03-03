from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path


def now_iso_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def slugify(value: str) -> str:
    value = (value or "").strip().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-") or "resource"


def normalize_text(raw: str) -> str:
    if raw is None:
        return ""
    text = str(raw).replace("\r", "")
    text = text.replace("\x0c", "\n")
    text = re.sub(r"(?<=[A-Za-z])-\n(?=[A-Za-z])", "", text)
    text = text.replace("\n", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def to_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True)

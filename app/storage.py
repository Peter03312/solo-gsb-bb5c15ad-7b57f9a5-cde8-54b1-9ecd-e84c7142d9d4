"""原子文件存储。原图与发布 PNG 均内容寻址式落盘，写入走临时文件 + rename。"""
from __future__ import annotations

import os
import uuid

from .config import settings


def ensure_dirs() -> None:
    os.makedirs(settings.originals_dir, exist_ok=True)
    os.makedirs(settings.releases_dir, exist_ok=True)
    os.makedirs(os.path.dirname(settings.db_path), exist_ok=True)


def original_path(doc_id: str) -> str:
    return os.path.join(settings.originals_dir, f"{doc_id}.bin")


def release_path(release_id: str) -> str:
    return os.path.join(settings.releases_dir, f"{release_id}.png")


def save_atomic(path: str, data: bytes) -> None:
    tmp = f"{path}.tmp-{uuid.uuid4().hex}"
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def read_file(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()

"""SQLite 持久化层与并发控制。

并发模型：
- annotations.revision 在每次整体替换标注集合时自增；
- 修改标注必须携带期望修订号（乐观锁），过期返回 409；
- 发布在一个立即提交的 IMMEDIATE 事务中读取修订号并写入发布记录，
  保证「发布与标注修改并发时，成品和记录对应同一修订」。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from typing import Iterator

from .config import settings

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS documents (
    id              TEXT PRIMARY KEY,
    content_type    TEXT NOT NULL,
    source_filename TEXT NOT NULL,
    width           INTEGER NOT NULL,
    height          INTEGER NOT NULL,
    sha256          TEXT NOT NULL,
    byte_size       INTEGER NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS annotations (
    document_id TEXT PRIMARY KEY
                REFERENCES documents(id) ON DELETE CASCADE,
    revision    INTEGER NOT NULL CHECK (revision >= 0),
    boxes       TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS releases (
    id                  TEXT PRIMARY KEY,
    document_id         TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    revision            INTEGER NOT NULL,
    source_sha256       TEXT NOT NULL,
    png_sha256          TEXT NOT NULL,
    width               INTEGER NOT NULL,
    height              INTEGER NOT NULL,
    byte_size           INTEGER NOT NULL,
    box_count           INTEGER NOT NULL,
    boxes               TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    UNIQUE(document_id, revision)
);

CREATE INDEX IF NOT EXISTS idx_releases_document ON releases(document_id);
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(
        settings.db_path, timeout=settings.busy_timeout_ms / 1000.0
    )
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={settings.busy_timeout_ms}")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


@contextmanager
def transaction(immediate: bool = False) -> Iterator[sqlite3.Connection]:
    """开启事务；immediate=True 时立即获取写锁（发布用）。"""
    conn = connect()
    try:
        if immediate:
            conn.execute("BEGIN IMMEDIATE")
        else:
            conn.execute("BEGIN")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------- documents ----------

def insert_document(
    conn: sqlite3.Connection,
    *,
    doc_id: str,
    content_type: str,
    source_filename: str,
    width: int,
    height: int,
    sha256: str,
    byte_size: int,
    created_at: str,
) -> None:
    conn.execute(
        """INSERT INTO documents
           (id, content_type, source_filename, width, height, sha256, byte_size, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (doc_id, content_type, source_filename, width, height, sha256, byte_size, created_at),
    )
    conn.execute(
        """INSERT INTO annotations (document_id, revision, boxes, updated_at)
           VALUES (?, 0, '[]', ?)""",
        (doc_id, created_at),
    )


def get_document(conn: sqlite3.Connection, doc_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()


# ---------- annotations ----------

def get_annotations(conn: sqlite3.Connection, doc_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM annotations WHERE document_id = ?", (doc_id,)
    ).fetchone()


def replace_annotations(
    conn: sqlite3.Connection,
    *,
    doc_id: str,
    expected_revision: int,
    boxes_json: str,
    updated_at: str,
) -> bool:
    """乐观锁整体替换标注。返回 False 表示修订号过期/文档不存在。"""
    cur = conn.execute(
        """UPDATE annotations
              SET revision = revision + 1, boxes = ?, updated_at = ?
            WHERE document_id = ? AND revision = ?""",
        (boxes_json, updated_at, doc_id, expected_revision),
    )
    return cur.rowcount == 1


# ---------- releases ----------

def insert_release(
    conn: sqlite3.Connection,
    *,
    release_id: str,
    doc_id: str,
    revision: int,
    source_sha256: str,
    png_sha256: str,
    width: int,
    height: int,
    byte_size: int,
    box_count: int,
    boxes_json: str,
    created_at: str,
) -> None:
    conn.execute(
        """INSERT INTO releases
           (id, document_id, revision, source_sha256, png_sha256, width, height,
            byte_size, box_count, boxes, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (release_id, doc_id, revision, source_sha256, png_sha256, width, height,
         byte_size, box_count, boxes_json, created_at),
    )


def get_release(conn: sqlite3.Connection, release_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM releases WHERE id = ?", (release_id,)).fetchone()


def list_releases(conn: sqlite3.Connection, doc_id: str) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT * FROM releases WHERE document_id = ? ORDER BY created_at", (doc_id,)
    ))


def encode_boxes(boxes: list[dict]) -> str:
    return json.dumps(
        [
            {
                "x": b["x"],
                "y": b["y"],
                "width": b["width"],
                "height": b["height"],
                "reason": b["reason"],
            }
            for b in boxes
        ],
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def decode_boxes(raw: str) -> list[dict]:
    return json.loads(raw)

"""运行期配置，全部来自环境变量。"""
from __future__ import annotations

import os
from dataclasses import dataclass


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        raise RuntimeError(f"环境变量 {name} 必须是整数，实际值: {raw!r}")


def _parse_color(raw: str) -> tuple[int, int, int]:
    raw = raw.strip().lstrip("#")
    if len(raw) != 6:
        raise RuntimeError(f"REDACTION_COLOR 必须是 6 位十六进制颜色，实际值: {raw!r}")
    try:
        return int(raw[0:2], 16), int(raw[2:4], 16), int(raw[4:6], 16)
    except ValueError:
        raise RuntimeError(f"REDACTION_COLOR 必须是十六进制颜色，实际值: {raw!r}")


@dataclass(frozen=True)
class Settings:
    data_dir: str
    db_path: str
    originals_dir: str
    releases_dir: str
    max_upload_bytes: int
    max_image_pixels: int
    redaction_color: tuple[int, int, int]
    busy_timeout_ms: int


def load_settings() -> Settings:
    data_dir = os.environ.get("DATA_DIR", "/data").rstrip("/") or "/data"
    db_path = os.environ.get("DATABASE_PATH", f"{data_dir}/app.db")
    originals_dir = os.environ.get("ORIGINALS_DIR", f"{data_dir}/originals")
    releases_dir = os.environ.get("RELEASES_DIR", f"{data_dir}/releases")
    color = _parse_color(os.environ.get("REDACTION_COLOR", "000000"))
    return Settings(
        data_dir=data_dir,
        db_path=db_path,
        originals_dir=originals_dir,
        releases_dir=releases_dir,
        max_upload_bytes=_env_int("MAX_UPLOAD_MB", 20) * 1024 * 1024,
        max_image_pixels=_env_int("MAX_IMAGE_PIXELS", 60_000_000),
        redaction_color=color,
        busy_timeout_ms=_env_int("SQLITE_BUSY_TIMEOUT_MS", 15_000),
    )


settings = load_settings()

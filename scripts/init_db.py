"""初始化数据库与数据目录，可重复执行（CREATE TABLE IF NOT EXISTS）。"""
from __future__ import annotations

from app import database as db
from app import storage


def main() -> None:
    storage.ensure_dirs()
    db.init_db()
    print(f"初始化完成：数据库位于 {db.settings.db_path}")
    print(f"原图目录: {db.settings.originals_dir}")
    print(f"发布目录: {db.settings.releases_dir}")


if __name__ == "__main__":
    main()

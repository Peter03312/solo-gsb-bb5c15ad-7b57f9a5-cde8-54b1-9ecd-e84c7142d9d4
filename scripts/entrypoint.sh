#!/bin/sh
# 容器入口：建数据目录并初始化 SQLite 表结构（幂等），随后启动 API。
set -e

DATA_DIR="${DATA_DIR:-/data}"
mkdir -p "$DATA_DIR"

python -m scripts.init_db

exec uvicorn app.main:app \
    --host "${HOST:-0.0.0.0}" \
    --port "${PORT:-8000}" \
    --workers "${UVICORN_WORKERS:-1}"

FROM python:3.11-slim

# 不让 Python 写 .pyc、不缓冲日志，容器内运行更可观测
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data

WORKDIR /app

# 先装依赖，利用 Docker 层缓存
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 拷贝应用与初始化脚本
COPY app ./app
COPY scripts ./scripts
RUN chmod +x ./scripts/entrypoint.sh

# 数据卷：SQLite 数据库、原图、发布成品均在此（原图仅服务端可读，不通过 API 暴露）
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).status==200 else 1)"

ENTRYPOINT ["./scripts/entrypoint.sh"]

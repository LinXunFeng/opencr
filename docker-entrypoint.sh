#!/bin/sh
set -e

if [ -d /app/config.yaml ]; then
  # 早期版本的 compose 会把缺失的 bind 源创建成目录，这里明确指出来
  echo "[opencr] /app/config.yaml 是一个目录，不是文件。" >&2
  echo "         宿主机上多半有一个被 Docker 自动创建的空目录 config.yaml，" >&2
  echo "         请先删掉它：rmdir config.yaml && cp config.example.yaml config.yaml" >&2
  exit 1
fi

if [ ! -f /app/config.yaml ]; then
  echo "[opencr] 缺少 /app/config.yaml。请先准备配置文件：" >&2
  echo "         cp config.example.yaml config.yaml" >&2
  exit 1
fi

# 迁移在这里跑一次，而不是在每个 gunicorn worker 里 ——
# 多个进程同时执行 DDL 只会互相抢锁。
python3 -m backend.storage.migrate

SERVER_HOST="${REVIEW_SERVER_HOST:-0.0.0.0}"
SERVER_PORT="${REVIEW_SERVER_PORT:-9034}"
# 本服务是 IO bound 且几乎无 QPS，worker 多了只会放大 SQLite 锁竞争。
# 留 2 个是为了单个慢请求不阻塞健康检查。
WORKERS="${GUNICORN_WORKERS:-2}"

echo "[opencr] starting on ${SERVER_HOST}:${SERVER_PORT} with ${WORKERS} workers"
exec gunicorn \
    --bind "${SERVER_HOST}:${SERVER_PORT}" \
    --chdir /app/backend \
    --workers "${WORKERS}" \
    --timeout 300 \
    --access-logfile - \
    --error-logfile - \
    --capture-output \
    --enable-stdio-inheritance \
    "review_server:app"

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

# compose 在 .env 没有设置时会传入空串。空密码的文件存储不等于"没有密码"，
# gogcli 会拿空串去加解密令牌；删掉这个变量，让缺配置的情况以明确的报错暴露出来。
if [ -z "${GOG_KEYRING_PASSWORD:-}" ]; then
  unset GOG_KEYRING_PASSWORD
fi

# 迁移在这里跑一次，而不是在每个 gunicorn worker 里 ——
# 多个进程同时执行 DDL 只会互相抢锁。
python3 -m backend.storage.migrate

# gogcli：启动时只做不需要人参与的部分——导入 OAuth 客户端信息、打印授权状态。
# 账号授权要在浏览器里点同意，由 scripts/setup-gogcli.sh 在首次部署时做一次，令牌留在 opencr-gogcli 卷里。
# 刻意不支持挂载令牌文件、每次启动自动导入：明文 refresh token 会一直留在宿主机上，
# 而且每次重启都会用这份旧令牌覆盖卷里在容器内重新授权过的令牌。
# 这一段任何失败都不阻止启动：审查服务不依赖 gogcli，问题留给日志与后台的连通性测试。
GOGCLI_CLIENT_SECRET=/app/secrets/gogcli-client-secret.json
if command -v gog >/dev/null 2>&1; then
  if [ -f "$GOGCLI_CLIENT_SECRET" ]; then
    # 每次启动都导入而不是"没有才导入"：客户端信息不含用户令牌，覆盖是幂等的，
    # 而且宿主机上换了 JSON 之后重启一下就能生效
    gog auth credentials set "$GOGCLI_CLIENT_SECRET" >/dev/null \
      || echo "[opencr] gogcli 导入 OAuth 客户端信息失败：$GOGCLI_CLIENT_SECRET" >&2
  fi
  python3 -m backend.survey.destinations.gogcli_status \
    || echo "[opencr] gogcli 授权存在问题（不影响审查服务），首次部署请在宿主机执行 ./scripts/setup-gogcli.sh <账号>" >&2
fi

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

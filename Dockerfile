# ---------------------------------------------------------------------------
# 阶段一：编译后台前端
#
# 构建产物不进 Git（仓库保持干净），因此必须在镜像构建时现编译。
# 单独一个阶段的好处是 Node 与 node_modules 都不会进入最终镜像。
# ---------------------------------------------------------------------------
FROM node:22-slim AS web-builder

WORKDIR /web

RUN corepack enable

# 先只拷依赖清单与 .npmrc：前端源码改动不会让依赖层失效。
# .npmrc 必须一起拷 —— 它里面配了国内镜像源，漏掉会退回官方源导致拉包极慢甚至超时。
COPY web/package.json web/pnpm-lock.yaml web/.npmrc ./
RUN pnpm install --frozen-lockfile

COPY web/ ./
# vite 的 outDir 指向 ../backend/admin/static，因此产物落在 /backend/admin/static
RUN pnpm build

# ---------------------------------------------------------------------------
# 阶段二：运行时
#
# python:3.12-slim 而非 alpine：musl 下 openai/requests 的依赖链需要现场编译，
# 构建时间和镜像体积都更差。
# ---------------------------------------------------------------------------
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    OPENCR_LOG_FILE=/app/logs/server.log \
    OPENCR_DATABASE_URL=sqlite:////app/data/opencr.db

WORKDIR /app

# 依赖单独一层：改代码不必重装依赖
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# migrations 与 alembic.ini 都在 backend/ 内，随它一起进来
COPY backend ./backend
# 内置一份默认 skills 作兜底；挂载 ./skills 会整体覆盖它
COPY skills ./skills
COPY docker-entrypoint.sh ./

# 只取编译产物，Node 与 node_modules 都留在构建阶段
COPY --from=web-builder /backend/admin/static ./backend/admin/static

RUN chmod +x docker-entrypoint.sh && mkdir -p /app/data /app/logs

EXPOSE 9034

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:9034/health', timeout=4).status==200 else 1)"

ENTRYPOINT ["./docker-entrypoint.sh"]

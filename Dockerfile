# python:3.12-slim 而非 alpine：musl 下 openai/requests 的依赖链需要现场编译，
# 构建时间和镜像体积都更差。
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    OPENCR_LOG_FILE=/app/logs/server.log \
    OPENCR_DATABASE_URL=sqlite:////app/data/opencr.db

WORKDIR /app

# 依赖单独一层：改代码不必重装依赖
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY alembic.ini ./
COPY migrations ./migrations
COPY src ./src
# 内置一份默认 skills 作兜底；挂载 ./skills 会整体覆盖它
COPY skills ./skills
COPY docker-entrypoint.sh ./
RUN chmod +x docker-entrypoint.sh && mkdir -p /app/data /app/logs

EXPOSE 9034

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:9034/health', timeout=4).status==200 else 1)"

ENTRYPOINT ["./docker-entrypoint.sh"]

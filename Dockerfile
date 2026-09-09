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
    OPENCR_DATABASE_URL=sqlite:////app/data/opencr.db \
    OPENCR_SURVEY_WORKSPACE_DIR=/app/workspaces

WORKDIR /app

# git 是定期巡检的**硬依赖**（拉取仓库靠它），python:3.12-slim 里没有。
# ca-certificates 同理：缺了它 https 克隆会直接失败。
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# ---------------------------------------------------------------------------
# codegraph：定期巡检的仓库画像来源，**默认安装**。
#
# 版本钉死而不是取 latest：巡检的画像质量直接取决于它的抽取行为，
# 两次构建装出不同版本会让"同一份代码这周多了十条问题"变成无从解释的现象。
#
# 自己下载而不是用官方一键脚本：后者不续传不重试，实测在网络不佳时会拉了
# 二十多分钟后以 HTTP/2 协议错误整体失败。这里用 HTTP/1.1 + 断点续传 + 重试，
# 并在解包前校验压缩包完整性（断点续传可能拿到被截断的文件）。
#
# 确实不想装它时把版本置空即可，巡检会退化为依赖清单级画像并记一条降级：
#     docker compose build --build-arg CODEGRAPH_VERSION=
# ---------------------------------------------------------------------------
ARG CODEGRAPH_VERSION=v1.6.0
ARG TARGETARCH
RUN if [ -n "$CODEGRAPH_VERSION" ]; then \
        set -eux; \
        arch="$(case "${TARGETARCH:-amd64}" in arm64) echo arm64 ;; *) echo x64 ;; esac)"; \
        apt-get update && apt-get install -y --no-install-recommends curl; \
        curl -fL --http1.1 --retry 8 --retry-all-errors --retry-delay 3 -C - \
            --connect-timeout 20 -o /tmp/codegraph.tar.gz \
            "https://github.com/colbymchenry/codegraph/releases/download/${CODEGRAPH_VERSION}/codegraph-linux-${arch}.tar.gz"; \
        gzip -t /tmp/codegraph.tar.gz; \
        mkdir -p /opt/codegraph; \
        tar -xzf /tmp/codegraph.tar.gz -C /opt/codegraph --strip-components=1; \
        ln -sf /opt/codegraph/bin/codegraph /usr/local/bin/codegraph; \
        rm -f /tmp/codegraph.tar.gz; \
        apt-get purge -y curl && apt-get autoremove -y; \
        rm -rf /var/lib/apt/lists/*; \
        codegraph --version; \
        codegraph telemetry off || true; \
    else \
        echo "CODEGRAPH_VERSION is empty: skipping codegraph, surveys will use manifest-level profiles"; \
    fi

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

RUN chmod +x docker-entrypoint.sh && mkdir -p /app/data /app/logs /app/workspaces

EXPOSE 9034

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:9034/health', timeout=4).status==200 else 1)"

ENTRYPOINT ["./docker-entrypoint.sh"]

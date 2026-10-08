#!/bin/bash
#
# Docker 部署下配置 gogcli 授权（Google Sheet 输出目标的 auth: gogcli）。在宿主机、仓库根目录下执行：
#
#   ./scripts/setup-gogcli.sh [--client-secret client_secret.json] [--manual] [账号 ...]
#
# 不写账号时，授权 config.yaml 里所有 auth: gogcli 实例中尚未授权的账号。重复执行是安全的：
# 已授权的账号直接跳过，已有的令牌加密密码不会被改动。
#
# 依次完成：
#   1. .env 里没有 GOG_KEYRING_PASSWORD 时生成一个（容器里没有钥匙串，令牌以加密文件存放）
#   2. 启动容器，必要时导入 OAuth 客户端信息
#   3. 宿主机的 gogcli 已登录该账号时导出令牌再导入容器，否则在容器里走粘贴回调地址的授权
#   4. 打印与后台「测试连通性」同一份实现的检查结果
#
# 这些步骤不放进容器启动流程，是因为授权要人在浏览器里点同意，而密码要写在宿主机的 .env 里，
# 两者都是容器做不到的。令牌导入后留在 opencr-gogcli 卷里，之后重新部署、升级镜像都不用再执行。
# launchd 部署不需要本脚本，install.sh 会在安装过程中完成授权。

set -euo pipefail

SERVICE="opencr"
STATUS_MODULE="backend.survey.destinations.gogcli_status"
CONTAINER_TMP="/tmp/opencr-gogcli-setup"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

info()    { echo -e "${BLUE}[INFO]${NC} $1"; }
success() { echo -e "${GREEN}[SUCCESS]${NC} $1"; }
warn()    { echo -e "${YELLOW}[WARNING]${NC} $1"; }
fail()    { echo -e "${RED}[ERROR]${NC} $1" >&2; exit 1; }

usage() {
    sed -n '3,6p' "$0" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

CLIENT_SECRET=""
FORCE_MANUAL=false
ACCOUNTS=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --client-secret) [[ $# -ge 2 ]] || usage 1; CLIENT_SECRET="$2"; shift 2 ;;
        --manual) FORCE_MANUAL=true; shift ;;
        -h|--help) usage 0 ;;
        -*) echo "未知参数: $1" >&2; usage 1 ;;
        *) ACCOUNTS+=("$1"); shift ;;
    esac
done

cd "$(dirname "$0")/.."
[[ -f docker-compose.yml ]] || fail "未找到 docker-compose.yml，请在仓库根目录下保留本脚本的相对位置"
[[ -f config.yaml ]] || fail "未找到 config.yaml，请先 cp config.example.yaml config.yaml 并配置 destinations"
docker compose version >/dev/null 2>&1 || fail "需要 docker compose"
# 在改动 .env 之前确认 Docker 可用：否则密码写进去了、后面每一步却都失败，
# 用户会以为 .env 也要回滚
docker info >/dev/null 2>&1 || fail "连不上 Docker daemon，请先启动 Docker（Docker Desktop / OrbStack 等）后重试"
if [[ -n "$CLIENT_SECRET" && ! -f "$CLIENT_SECRET" ]]; then
    fail "找不到 OAuth 客户端文件: $CLIENT_SECRET"
fi

# 令牌文件含 refresh token：无论脚本在哪一步退出，宿主机与容器里的临时副本都要删掉
HOST_TMP="$(mktemp -d)"
chmod 700 "$HOST_TMP"
cleanup() {
    rm -rf "$HOST_TMP"
    docker compose exec -T "$SERVICE" rm -rf "$CONTAINER_TMP" >/dev/null 2>&1 || true
}
trap cleanup EXIT

in_container() {
    docker compose exec -T "$SERVICE" "$@"
}

# 1. 令牌加密密码。只在缺失时生成，已有的绝不改：改了之后卷里已存的令牌就解不开了
if [[ -n "${GOG_KEYRING_PASSWORD:-}" ]]; then
    # compose 里 shell 环境变量的优先级高于 .env：这时生成新密码不会生效，
    # 反而会在某次没带这个变量启动时换掉密码，让卷里的令牌解不开
    warn "当前 shell 设置了 GOG_KEYRING_PASSWORD，compose 会优先使用它而不是 .env，本脚本不再生成"
    warn "建议把它写进 .env 后从 shell 中去掉，否则换个终端启动容器就会用错密码"
elif grep -Eq '^GOG_KEYRING_PASSWORD=.+' .env 2>/dev/null; then
    info ".env 已设置 GOG_KEYRING_PASSWORD，保持不变"
else
    # 卷里已有 gogcli 数据说明以前用某个密码存过令牌，生成新密码只会让它们全部解不开
    if docker compose exec -T "$SERVICE" sh -c '[ -n "$(ls -A "${GOG_HOME:-/app/gogcli}" 2>/dev/null)" ]' >/dev/null 2>&1; then
        fail ".env 中没有 GOG_KEYRING_PASSWORD，但 opencr-gogcli 卷里已有令牌。请把当初使用的密码写回 .env 再重试；找不回的话见 README 的「密码丢失或对不上」"
    fi
    command -v openssl >/dev/null 2>&1 || fail "需要 openssl 来生成令牌加密密码"
    if [[ -f .env ]]; then
        # 去掉空值的那一行，避免同一变量出现两次时 compose 取到空串
        grep -Ev '^GOG_KEYRING_PASSWORD=' .env > "$HOST_TMP/env" || true
        cat "$HOST_TMP/env" > .env
    fi
    echo "GOG_KEYRING_PASSWORD=$(openssl rand -hex 32)" >> .env
    chmod 600 .env
    success "已生成 GOG_KEYRING_PASSWORD 并写入 .env（请备份，丢失后需要重新授权）"
fi

# 2. 启动容器。环境变量变了 compose 会自动重建容器，没变则什么也不做
info "启动容器"
docker compose up -d "$SERVICE" >/dev/null
for _ in $(seq 1 30); do
    in_container true >/dev/null 2>&1 && break
    sleep 1
done
in_container true >/dev/null 2>&1 || fail "容器没有就绪，请查看 docker compose logs $SERVICE"
# 检查模块随本仓库的代码一起发布；`up -d` 不会重建镜像，容器里跑的可能还是旧版本代码。
# 这里不能吞掉报错：模块不存在时输出为空，会被误读成"配置里没有 gogcli 实例"
if ! CONFIG_ACCOUNTS="$(in_container python3 -m "$STATUS_MODULE" --accounts)"; then
    fail "容器里的 OpenCR 不支持本脚本（镜像比当前代码旧），请先执行 docker compose up -d --build $SERVICE 再重试"
fi
in_container sh -c 'command -v gog' >/dev/null 2>&1 \
    || fail "镜像里没有 gogcli，构建时是否用 --build-arg GOGCLI_VERSION= 跳过了它？"
in_container mkdir -p "$CONTAINER_TMP"

if [[ -n "$CLIENT_SECRET" ]]; then
    docker compose cp "$CLIENT_SECRET" "$SERVICE:$CONTAINER_TMP/client_secret.json" >/dev/null
    in_container gog auth credentials set "$CONTAINER_TMP/client_secret.json" >/dev/null
    success "已导入 OAuth 客户端信息"
fi

# 3. 账号授权
if [[ ${#ACCOUNTS[@]} -eq 0 ]]; then
    if [[ -z "$CONFIG_ACCOUNTS" ]]; then
        warn "容器读到的 config.yaml 中没有 auth: gogcli 的 Google Sheet 实例，没有需要授权的账号"
        warn "容器挂载的是 $(pwd)/config.yaml，请确认改的是这一份"
        exit 0
    fi
    needs_auth="$(in_container python3 -m "$STATUS_MODULE" --needs-auth)" \
        || fail "检查账号授权状态失败，见上方报错"
    while IFS= read -r account; do
        [[ -n "$account" ]] && ACCOUNTS+=("$account")
    done <<< "$needs_auth"
    # 为空不代表都授权好了：钥匙串密码不对、缺少 OAuth 客户端这类问题重新授权也解决不了，
    # 不会被列进来，结论以下面的完整检查为准
    [[ ${#ACCOUNTS[@]} -gt 0 ]] || info "没有需要重新授权的账号，下面做完整检查"
fi

host_gog="$(command -v gog 2>/dev/null || true)"
for account in "${ACCOUNTS[@]+"${ACCOUNTS[@]}"}"; do
    info "授权账号 $account"
    token_file="$HOST_TMP/token.json"
    # 宿主机的令牌由宿主机上 `gog auth credentials set` 的那个 OAuth 客户端签发，
    # 容器里导入的客户端信息必须是同一个，否则令牌刷新会被拒
    if ! $FORCE_MANUAL && [[ -n "$host_gog" ]] \
            && "$host_gog" auth tokens export "$account" --out "$token_file" >/dev/null 2>&1; then
        chmod 600 "$token_file"
        docker compose cp "$token_file" "$SERVICE:$CONTAINER_TMP/token.json" >/dev/null
        rm -f "$token_file"
        in_container gog auth tokens import "$CONTAINER_TMP/token.json" >/dev/null
        in_container rm -f "$CONTAINER_TMP/token.json"
        success "已从宿主机导入 $account 的令牌"
    else
        $FORCE_MANUAL || info "宿主机上没有可导出的 $account 令牌，改为在容器内授权"
        info "按提示在浏览器中打开链接并同意授权，然后把跳转后的完整地址粘贴回来"
        docker compose exec "$SERVICE" gog auth add "$account" --services sheets --manual \
            || warn "账号 $account 授权未完成"
    fi
done

# 4. 最终检查
echo ""
if [[ -z "$CONFIG_ACCOUNTS" ]]; then
    warn "容器读到的 config.yaml 中没有 auth: gogcli 的 Google Sheet 实例，账号已授权，但还没有实例会用到它"
    exit 0
fi
if in_container python3 -m "$STATUS_MODULE"; then
    success "gogcli 授权正常。OAuth 应用需发布为正式版本，Testing 状态下的令牌 7 天后过期"
else
    warn "gogcli 授权仍有问题，见上方提示"
    exit 1
fi

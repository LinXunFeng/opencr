#!/bin/bash
#
# OpenCR - 自动代码审查服务安装脚本
# 适用于 macOS，支持 OpenAI 兼容代理配置
#

set -e

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# 配置
INSTALL_DIR="$HOME/opencr"
SERVICE_NAME="com.opencr.server"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONFIG_SOURCE_FILE=""

# 打印函数
print_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

print_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

print_step() {
    echo -e "\n${BLUE}▶ $1${NC}"
}

# 检查系统要求
check_requirements() {
    print_step "检查系统要求"

    # 检查 macOS
    if [[ "$OSTYPE" != "darwin"* ]]; then
        print_error "本脚本仅支持 macOS"
        exit 1
    fi
    print_success "系统: macOS"

    # 检查 Python3
    if ! command -v python3 &> /dev/null; then
        print_error "未找到 Python3，请先安装"
        print_info "建议: brew install python@3.11"
        exit 1
    fi

    PYTHON_FULL_VERSION=$(python3 --version 2>&1 | awk '{print $2}')
    print_success "Python 版本: $PYTHON_FULL_VERSION"

    # 检查 pip
    if ! command -v pip3 &> /dev/null; then
        print_error "未找到 pip3"
        exit 1
    fi
    print_success "pip3 已安装"

    # 检查 curl
    if ! command -v curl &> /dev/null; then
        print_error "未找到 curl"
        exit 1
    fi
    print_success "curl 已安装"
}

# 掩码显示敏感信息
mask_value() {
    local value="$1"
    if [[ -z "$value" ]]; then
        echo "(空)"
        return
    fi
    local len=${#value}
    if (( len <= 8 )); then
        echo "********"
    else
        echo "${value:0:4}****${value:len-4:4}"
    fi
}

# 从 config.yaml 读取指定 section.key（仅支持简单键值结构）
read_yaml_value() {
    local file_path="$1"
    local section="$2"
    local key="$3"

    awk -v section="$section" -v key="$key" '
        /^[[:space:]]*#/ { next }
        /^[[:space:]]*$/ { next }

        /^[^[:space:]][^:]*:[[:space:]]*$/ {
            current = $0
            sub(/:[[:space:]]*$/, "", current)
            gsub(/[[:space:]]/, "", current)
            in_section = (current == section)
            next
        }

        in_section && $0 ~ ("^[[:space:]]{2}" key ":[[:space:]]*") {
            value = $0
            sub(("^[[:space:]]{2}" key ":[[:space:]]*"), "", value)
            sub(/[[:space:]]*#.*/, "", value)
            gsub(/^["'\''"]|["'\''"]$/, "", value)
            print value
            exit
        }

        in_section && $0 ~ /^[^[:space:]]/ {
            in_section = 0
        }
    ' "$file_path"
}

# 加载项目配置文件
load_project_config_file() {
    print_step "读取项目配置文件"

    local config_yaml="$SCRIPT_DIR/config.yaml"
    if [[ -f "$config_yaml" ]]; then
        CONFIG_SOURCE_FILE="$config_yaml"
        print_success "检测到配置: $config_yaml"

        CODE_PLATFORM=$(read_yaml_value "$config_yaml" "code_platform" "type")
        CODE_PLATFORM_URL=$(read_yaml_value "$config_yaml" "code_platform" "url")
        CODE_PLATFORM_TOKEN=$(read_yaml_value "$config_yaml" "code_platform" "token")
        WEBHOOK_SECRET=$(read_yaml_value "$config_yaml" "code_platform" "webhook_secret")

        OPENAI_BASE_URL=$(read_yaml_value "$config_yaml" "openai" "base_url")
        OPENAI_API_KEY=$(read_yaml_value "$config_yaml" "openai" "api_key")
        OPENAI_MODEL=$(read_yaml_value "$config_yaml" "openai" "model")
        OPENAI_REASONING_EFFORT=$(read_yaml_value "$config_yaml" "openai" "reasoning_effort")

        REVIEW_SERVER_HOST=$(read_yaml_value "$config_yaml" "server" "host")
        REVIEW_SERVER_PORT=$(read_yaml_value "$config_yaml" "server" "port")
        REVIEW_LOG_LEVEL=$(read_yaml_value "$config_yaml" "server" "log_level")

        REVIEW_MAX_DIFF_SIZE=$(read_yaml_value "$config_yaml" "review" "max_diff_size")
        REVIEW_TIMEOUT=$(read_yaml_value "$config_yaml" "review" "timeout")
        REVIEW_SKILLS_DIR=$(read_yaml_value "$config_yaml" "review" "skills_dir")
        REVIEW_SKILL_SCRIPTS_ENABLED=$(read_yaml_value "$config_yaml" "review" "skill_scripts_enabled")
        REVIEW_SKILL_SCRIPTS_TIMEOUT=$(read_yaml_value "$config_yaml" "review" "skill_scripts_timeout")

        print_info "已从 config.yaml 预填配置项"
        return
    fi

    print_warning "未找到项目配置文件 (config.yaml)"
    print_info "可先执行: cp \"$SCRIPT_DIR/config.example.yaml\" \"$SCRIPT_DIR/config.yaml\""
}

# 交互收集配置项
prompt_config_value() {
    local key="$1"
    local label="$2"
    local default_value="$3"
    local is_sensitive="$4"
    local is_required="$5"
    local current_value="${!key}"

    if [[ -n "$current_value" ]]; then
        local display_value="$current_value"
        if [[ "$is_sensitive" == "true" ]]; then
            display_value="$(mask_value "$current_value")"
        fi

        read -r -p "检测到 ${key}=${display_value}，是否使用? [Y/n]: " use_existing
        if [[ ! "$use_existing" =~ ^[Nn]$ ]]; then
            return
        fi
    fi

    while true; do
        if [[ -n "$default_value" ]]; then
            read -r -p "${label} [${default_value}]: " input_value
            input_value="${input_value:-$default_value}"
        else
            if [[ "$is_sensitive" == "true" ]]; then
                read -r -s -p "${label}: " input_value
                echo ""
            else
                read -r -p "${label}: " input_value
            fi
        fi

        if [[ "$is_required" == "true" && -z "$input_value" ]]; then
            print_warning "${key} 不能为空"
            continue
        fi

        printf -v "$key" "%s" "$input_value"
        export "$key"
        break
    done
}

# 配置 GitLab/OpenAI
collect_required_config() {
    print_step "配置必填项"

    if [[ -n "$CODE_PLATFORM" && "$CODE_PLATFORM" != "gitlab" ]]; then
        print_warning "当前版本仅支持 gitlab，已忽略现有 CODE_PLATFORM=${CODE_PLATFORM}"
    fi
    CODE_PLATFORM="gitlab"
    export CODE_PLATFORM
    prompt_config_value "CODE_PLATFORM_URL" "请输入代码平台 URL" "https://gitlab.company.com" "false" "true"
    prompt_config_value "CODE_PLATFORM_TOKEN" "请输入代码平台 Access Token" "" "true" "true"
    prompt_config_value "WEBHOOK_SECRET" "请输入 Webhook Secret（可选）" "" "true" "false"
    prompt_config_value "OPENAI_BASE_URL" "请输入 OpenAI Base URL" "https://api.openai.com/v1" "false" "true"
    prompt_config_value "OPENAI_API_KEY" "请输入 OpenAI API Key" "" "true" "true"
    prompt_config_value "OPENAI_MODEL" "请输入 OpenAI Model" "gpt-4.1" "false" "true"

    echo ""
    print_info "配置摘要:"
    echo "  CODE_PLATFORM=${CODE_PLATFORM}"
    echo "  CODE_PLATFORM_URL=${CODE_PLATFORM_URL}"
    echo "  CODE_PLATFORM_TOKEN=$(mask_value "$CODE_PLATFORM_TOKEN")"
    echo "  WEBHOOK_SECRET=$(mask_value "$WEBHOOK_SECRET")"
    echo "  OPENAI_BASE_URL=${OPENAI_BASE_URL}"
    echo "  OPENAI_API_KEY=$(mask_value "$OPENAI_API_KEY")"
    echo "  OPENAI_MODEL=${OPENAI_MODEL}"

    read -r -p "以上配置是否正确? [Y/n]: " config_confirm
    if [[ "$config_confirm" =~ ^[Nn]$ ]]; then
        collect_required_config
    fi
}

# 检查 GitLab 配置可用性
check_code_platform_config() {
    print_step "验证代码平台连接"

    print_info "测试代码平台连接..."

    if [[ "$CODE_PLATFORM" == "gitlab" ]]; then
        response_code=$(curl -s -o /dev/null -w "%{http_code}" \
            "${CODE_PLATFORM_URL%/}/api/v4/user" \
            -H "PRIVATE-TOKEN: $CODE_PLATFORM_TOKEN" 2>/dev/null || echo "000")
    else
        # GitHub 或其他平台暂不自动验证
        response_code="200"
        print_info "跳过连接验证，请在安装后手动测试"
    fi

    if [[ "$response_code" != "200" ]]; then
        print_warning "GitLab 连接测试失败 (HTTP $response_code)"
        read -r -p "是否继续安装? [y/N]: " continue_install
        if [[ ! "$continue_install" =~ ^[Yy]$ ]]; then
            exit 1
        fi
    else
        print_success "代码平台连接成功"
    fi
}

# 创建目录结构并复制代码
copy_files() {
    print_step "安装项目文件"

    # 创建目录
    mkdir -p "$INSTALL_DIR"/{backend,skills,logs,scripts,data}
    print_success "创建目录: $INSTALL_DIR"

    # 复制源码
    if [[ -d "$SCRIPT_DIR/backend" ]]; then
        cp -r "$SCRIPT_DIR/backend/." "$INSTALL_DIR/backend/"
        print_success "复制源码文件（含 migrations 与 alembic.ini）"
        if [[ ! -f "$INSTALL_DIR/backend/alembic.ini" ]]; then
            print_error "backend/alembic.ini 缺失，无法初始化数据库"
            exit 1
        fi
    else
        print_error "未找到 backend 目录: $SCRIPT_DIR/backend"
        exit 1
    fi

    # 复制技能目录（用于自动 skill 路由）
    if [[ -d "$SCRIPT_DIR/skills" ]]; then
        cp -r "$SCRIPT_DIR/skills/." "$INSTALL_DIR/skills/"
        print_success "复制 skills 目录"
    else
        print_warning "未找到 skills 目录: $SCRIPT_DIR/skills"
        print_warning "自动 skill 选择将无法命中，审查分支会被跳过"
    fi

    # 复制 Python 依赖清单
    if [[ -f "$SCRIPT_DIR/requirements.txt" ]]; then
        cp "$SCRIPT_DIR/requirements.txt" "$INSTALL_DIR/requirements.txt"
        print_success "复制 requirements.txt"
    else
        print_error "未找到依赖清单: $SCRIPT_DIR/requirements.txt"
        exit 1
    fi

    # 设置可执行权限
    chmod +x "$INSTALL_DIR/backend/review_server.py"
}

# 创建 Python 虚拟环境
setup_venv() {
    print_step "创建 Python 虚拟环境"

    cd "$INSTALL_DIR"

    python3 -m venv venv
    print_success "虚拟环境创建完成"

    source venv/bin/activate

    print_info "安装依赖包..."
    pip install -q --upgrade pip
    pip install -q -r requirements.txt

    print_success "依赖安装完成"
    deactivate
}

# 编译后台前端
#
# 构建产物不进 Git（仓库保持干净），因此在安装时现编译。
# 缺 Node/pnpm 时不中断安装：审查服务本身不依赖前端，
# 只是后台页面会返回一条"产物缺失"的提示，补装后重跑本脚本即可。
build_web_console() {
    print_step "编译后台前端"

    if [[ ! -d "$SCRIPT_DIR/web" ]]; then
        print_warning "未找到 web 目录，跳过后台前端编译"
        return 0
    fi

    local pm=""
    if command -v pnpm >/dev/null 2>&1; then
        pm="pnpm"
    elif command -v npm >/dev/null 2>&1; then
        pm="npm"
    else
        print_warning "未检测到 pnpm 或 npm，跳过后台前端编译"
        print_warning "审查服务可正常使用，但 /admin 后台页面不可用"
        print_warning "安装 Node.js 后重跑 ./install.sh 即可启用后台"
        return 0
    fi

    print_info "使用 $pm 编译（首次会下载依赖，可能需要几分钟）"
    if (cd "$SCRIPT_DIR/web" && $pm install && $pm run build); then
        if [[ -f "$SCRIPT_DIR/backend/admin/static/index.html" ]]; then
            mkdir -p "$INSTALL_DIR/backend/admin/static"
            cp -r "$SCRIPT_DIR/backend/admin/static/." "$INSTALL_DIR/backend/admin/static/"
            print_success "后台前端编译完成"
        else
            print_warning "编译结束但未找到产物，后台页面将不可用"
        fi
    else
        print_warning "后台前端编译失败，/admin 页面不可用（不影响审查服务）"
    fi
}

# 生成启动脚本
generate_start_scripts() {
    print_step "生成启动脚本"

    # 生产启动脚本
    cat > "$INSTALL_DIR/start.sh" << 'START_EOF'
#!/bin/bash
# OpenCR - 生产启动脚本

set -e

# 获取脚本所在目录的绝对路径
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

read_config_value() {
    local section="$1"
    local key="$2"
    local file_path="$SCRIPT_DIR/config.yaml"
    [[ -f "$file_path" ]] || return 0

    awk -v section="$section" -v key="$key" '
        /^[[:space:]]*#/ { next }
        /^[[:space:]]*$/ { next }
        /^[^[:space:]][^:]*:[[:space:]]*$/ {
            current = $0
            sub(/:[[:space:]]*$/, "", current)
            gsub(/[[:space:]]/, "", current)
            in_section = (current == section)
            next
        }
        in_section && $0 ~ ("^[[:space:]]{2}" key ":[[:space:]]*") {
            value = $0
            sub(("^[[:space:]]{2}" key ":[[:space:]]*"), "", value)
            sub(/[[:space:]]*#.*/, "", value)
            gsub(/^["'\''"]|["'\''"]$/, "", value)
            print value
            exit
        }
        in_section && $0 ~ /^[^[:space:]]/ {
            in_section = 0
        }
    ' "$file_path"
}

# 确保日志目录存在（使用绝对路径）
mkdir -p "$SCRIPT_DIR/logs"
touch "$SCRIPT_DIR/logs/error.log"
touch "$SCRIPT_DIR/logs/access.log"

echo "[$(date)] Starting server from $SCRIPT_DIR"
echo "[$(date)] Logs directory: $SCRIPT_DIR/logs"

# 激活虚拟环境
source "$SCRIPT_DIR/venv/bin/activate"

# 检查 Python 依赖
python3 -c "import openai, flask, requests, sqlalchemy, alembic" 2>/dev/null || {
    echo "Installing dependencies..."
    pip install -q -r "$SCRIPT_DIR/requirements.txt"
}

config_host="$(read_config_value "server" "host")"
config_port="$(read_config_value "server" "port")"
SERVER_HOST="${REVIEW_SERVER_HOST:-${config_host:-0.0.0.0}}"
SERVER_PORT="${REVIEW_SERVER_PORT:-${config_port:-9034}}"

echo "Starting OpenCR server..."
echo "Host: ${SERVER_HOST}"
echo "Port: ${SERVER_PORT}"

# 本服务是 IO bound 且几乎无 QPS（每个 MR 事件一次审查），worker 多了只会
# 放大 SQLite 锁竞争。留 2 个是为了单个慢请求不阻塞健康检查。
workers="${GUNICORN_WORKERS:-2}"

# 迁移在启动前跑一次，而不是在每个 worker 里 —— 多个进程同时执行 DDL 只会互相抢锁
(cd "$SCRIPT_DIR" && python3 -m backend.storage.migrate) || {
    echo "[opencr] 数据库迁移失败，服务未启动" >&2
    exit 1
}

exec gunicorn \
    --bind "${SERVER_HOST}:${SERVER_PORT}" \
    --chdir "$SCRIPT_DIR/backend" \
    --workers $workers \
    --timeout 300 \
    --access-logfile "$SCRIPT_DIR/logs/access.log" \
    --error-logfile "$SCRIPT_DIR/logs/error.log" \
    --capture-output \
    --enable-stdio-inheritance \
    --preload \
    "review_server:app"
START_EOF

    chmod +x "$INSTALL_DIR/start.sh"

    # 开发启动脚本
    cat > "$INSTALL_DIR/start-dev.sh" << 'DEV_EOF'
#!/bin/bash
# OpenCR - 开发启动脚本

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

source "$SCRIPT_DIR/venv/bin/activate"

export FLASK_APP=backend/review_server.py
export FLASK_ENV=development
export PYTHONUNBUFFERED=1

cd "$SCRIPT_DIR/backend" && python3 review_server.py
DEV_EOF

    chmod +x "$INSTALL_DIR/start-dev.sh"

    print_success "启动脚本已生成"
}

# 生成配置文件
generate_config_file() {
    print_step "生成配置文件"

    REVIEW_SERVER_HOST=${REVIEW_SERVER_HOST:-0.0.0.0}
    REVIEW_SERVER_PORT=${REVIEW_SERVER_PORT:-9034}
    REVIEW_LOG_LEVEL=${REVIEW_LOG_LEVEL:-INFO}
    REVIEW_MAX_DIFF_SIZE=${REVIEW_MAX_DIFF_SIZE:-50000}
    REVIEW_TIMEOUT=${REVIEW_TIMEOUT:-180}
    REVIEW_SKILLS_DIR=${REVIEW_SKILLS_DIR:-skills}
    REVIEW_SKILL_SCRIPTS_ENABLED=${REVIEW_SKILL_SCRIPTS_ENABLED:-true}
    REVIEW_SKILL_SCRIPTS_TIMEOUT=${REVIEW_SKILL_SCRIPTS_TIMEOUT:-10}
    OPENAI_REASONING_EFFORT=${OPENAI_REASONING_EFFORT:-medium}
    OPENCR_ADMIN_ENABLED=${OPENCR_ADMIN_ENABLED:-false}
    OPENCR_ADMIN_USERNAME=${OPENCR_ADMIN_USERNAME:-admin}
    OPENCR_ADMIN_PASSWORD=${OPENCR_ADMIN_PASSWORD:-}
    OPENCR_ADMIN_BIND_LOCAL_ONLY=${OPENCR_ADMIN_BIND_LOCAL_ONLY:-false}
    OPENCR_DATABASE_URL=${OPENCR_DATABASE_URL:-}
    OPENCR_RETENTION_DAYS=${OPENCR_RETENTION_DAYS:-90}
    OPENCR_STALE_AFTER_SECONDS=${OPENCR_STALE_AFTER_SECONDS:-600}
    OPENCR_RECONCILE_INTERVAL_SECONDS=${OPENCR_RECONCILE_INTERVAL_SECONDS:-300}

    cat > "$INSTALL_DIR/config.yaml" << CONFIG_EOF
# OpenCR - 自动代码审查服务配置
# 生成时间: $(date)
# 来源: ${CONFIG_SOURCE_FILE:-"交互输入"}

openai:
  base_url: "${OPENAI_BASE_URL}"
  api_key: "${OPENAI_API_KEY}"
  model: "${OPENAI_MODEL}"
  reasoning_effort: "${OPENAI_REASONING_EFFORT}"

code_platform:
  type: "${CODE_PLATFORM}"
  url: "${CODE_PLATFORM_URL}"
  token: "${CODE_PLATFORM_TOKEN}"
  webhook_secret: "${WEBHOOK_SECRET}"

server:
  host: "${REVIEW_SERVER_HOST}"
  port: ${REVIEW_SERVER_PORT}
  log_level: "${REVIEW_LOG_LEVEL}"

review:
  max_diff_size: ${REVIEW_MAX_DIFF_SIZE}
  timeout: ${REVIEW_TIMEOUT}
  skills_dir: "${REVIEW_SKILLS_DIR}"
  skill_scripts_enabled: ${REVIEW_SKILL_SCRIPTS_ENABLED}
  skill_scripts_timeout: ${REVIEW_SKILL_SCRIPTS_TIMEOUT}

# 后台管理：默认关闭。开启需同时填写密码，否则服务会拒绝启动。
# password 可直接写明文，首次启动时会自动替换为哈希。
admin:
  enabled: ${OPENCR_ADMIN_ENABLED}
  username: "${OPENCR_ADMIN_USERNAME}"
  password: "${OPENCR_ADMIN_PASSWORD}"
  bind_local_only: ${OPENCR_ADMIN_BIND_LOCAL_ONLY}

storage:
  database_url: "${OPENCR_DATABASE_URL}"
  retention_days: ${OPENCR_RETENTION_DAYS}
  stale_after_seconds: ${OPENCR_STALE_AFTER_SECONDS}
  reconcile_interval_seconds: ${OPENCR_RECONCILE_INTERVAL_SECONDS}
CONFIG_EOF

    chmod 600 "$INSTALL_DIR/config.yaml"
    print_success "配置已保存到 $INSTALL_DIR/config.yaml"
}

# 生成 launchd plist
generate_launchd_plist() {
    print_step "生成 launchd 服务配置"

    PLIST_PATH="$HOME/Library/LaunchAgents/${SERVICE_NAME}.plist"

    cat > "$PLIST_PATH" << PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${SERVICE_NAME}</string>

    <key>ProgramArguments</key>
    <array>
        <string>${INSTALL_DIR}/start.sh</string>
    </array>

    <key>WorkingDirectory</key>
    <string>${INSTALL_DIR}</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin</string>
        <key>HOME</key>
        <string>${HOME}</string>
    </dict>

    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
        <key>Crashed</key>
        <true/>
    </dict>

    <key>ThrottleInterval</key>
    <integer>10</integer>

    <key>StandardOutPath</key>
    <string>${INSTALL_DIR}/logs/launchd.out.log</string>

    <key>StandardErrorPath</key>
    <string>${INSTALL_DIR}/logs/launchd.err.log</string>
</dict>
</plist>
PLIST_EOF

    print_success "launchd 配置已生成: $PLIST_PATH"
}

# 启动服务
start_service() {
    print_step "启动服务"

    # 确保日志目录存在（关键修复）
    mkdir -p "$INSTALL_DIR/logs"
    touch "$INSTALL_DIR/logs/error.log"
    touch "$INSTALL_DIR/logs/access.log"

    # 加载 launchd
    launchctl load "$HOME/Library/LaunchAgents/${SERVICE_NAME}.plist" 2>/dev/null || {
        print_warning "服务已加载，重新加载..."
        launchctl unload "$HOME/Library/LaunchAgents/${SERVICE_NAME}.plist" 2>/dev/null || true
        sleep 1
        launchctl load "$HOME/Library/LaunchAgents/${SERVICE_NAME}.plist"
    }

    # 等待服务启动
    sleep 2

    # 健康检查
    local health_url="http://localhost:${REVIEW_SERVER_PORT}/health"
    if curl -s "$health_url" > /dev/null 2>&1; then
        print_success "服务启动成功!"
        print_info "健康检查: curl $health_url"
    else
        print_warning "服务可能尚未完全启动"
        print_info "请稍后检查: tail -f ${INSTALL_DIR}/logs/launchd.err.log"
    fi
}

# 显示安装信息
show_summary() {
    echo ""
    echo "========================================"
    echo -e "${GREEN}安装完成!${NC}"
    echo "========================================"
    echo ""
    echo "目录结构:"
    echo "  ${INSTALL_DIR}/"
    echo "    ├── backend/"
    echo "    │   ├── review_server.py  # 主服务代码"
    echo "    │   └── wsgi.py           # WSGI 入口"
    echo "    ├── skills/"
    echo "    │   └── review/           # 审查 skill 提示词"
    echo "    ├── start.sh              # 生产启动脚本"
    echo "    ├── start-dev.sh          # 开发启动脚本"
    echo "    ├── logs/                 # 日志目录"
    echo "    └── venv/                 # Python 虚拟环境"
    echo ""
    echo "配置文件:"
    echo "  $INSTALL_DIR/config.yaml"
    echo "  $SCRIPT_DIR/config.example.yaml  # 项目模板"
    echo ""
    echo "服务管理:"
    echo "  启动: launchctl start ${SERVICE_NAME}"
    echo "  停止: launchctl stop ${SERVICE_NAME}"
    echo "  状态: launchctl list | grep opencr"
    echo ""
    echo "查看日志:"
    echo "  tail -f ${INSTALL_DIR}/logs/server.log"
    echo ""
    echo "Webhook 配置:"
    echo "  URL: http://$(hostname -s | head -1).local:${REVIEW_SERVER_PORT}/webhook"
    echo "  或:  http://$(ifconfig | grep 'inet ' | grep -v 127.0.0.1 | head -1 | awk '{print $2}'):${REVIEW_SERVER_PORT}/webhook"
    echo ""
    echo "测试命令:"
    echo "  curl http://localhost:${REVIEW_SERVER_PORT}/health"
    echo ""
    echo "========================================"
}

# 主函数
main() {
    echo -e "${BLUE}"
    echo "╔══════════════════════════════════════════╗"
    echo "║   OpenCR - 自动代码审查服务安装程序      ║"
    echo "║   支持 OpenAI 兼容代理配置               ║"
    echo "╚══════════════════════════════════════════╝"
    echo -e "${NC}"

    check_requirements
    load_project_config_file
    collect_required_config
    check_code_platform_config
    copy_files
    build_web_console
    setup_venv
    generate_start_scripts
    generate_config_file
    generate_launchd_plist
    start_service
    show_summary
}

main "$@"

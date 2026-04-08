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
    mkdir -p "$INSTALL_DIR"/{src,logs,scripts}
    print_success "创建目录: $INSTALL_DIR"

    # 复制源码
    if [[ -d "$SCRIPT_DIR/src" ]]; then
        cp -r "$SCRIPT_DIR/src" "$INSTALL_DIR/"
        print_success "复制源码文件"
    else
        print_error "未找到 src 目录: $SCRIPT_DIR/src"
        exit 1
    fi

    # 设置可执行权限
    chmod +x "$INSTALL_DIR/src/review_server.py"
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
    pip install -q openai flask gunicorn python-dotenv requests

    print_success "依赖安装完成"
    deactivate
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

# 确保日志目录存在（使用绝对路径）
mkdir -p "$SCRIPT_DIR/logs"
touch "$SCRIPT_DIR/logs/error.log"
touch "$SCRIPT_DIR/logs/access.log"

echo "[$(date)] Starting server from $SCRIPT_DIR"
echo "[$(date)] Logs directory: $SCRIPT_DIR/logs"

# 激活虚拟环境
source "$SCRIPT_DIR/venv/bin/activate"

# 检查 Python 依赖
python3 -c "import openai, flask, requests" 2>/dev/null || {
    echo "Installing dependencies..."
    pip install -q openai flask gunicorn requests python-dotenv
}

echo "Starting OpenCR server..."
echo "Host: ${REVIEW_SERVER_HOST:-0.0.0.0}"
echo "Port: ${REVIEW_SERVER_PORT:-5000}"

# 计算工作进程数
workers=$(( $(sysctl -n hw.ncpu) * 2 + 1 ))

exec gunicorn \
    --bind "${REVIEW_SERVER_HOST:-0.0.0.0}:${REVIEW_SERVER_PORT:-5000}" \
    --chdir "$SCRIPT_DIR/src" \
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

export FLASK_APP=src/review_server.py
export FLASK_ENV=development
export PYTHONUNBUFFERED=1

cd "$SCRIPT_DIR/src" && python3 review_server.py
DEV_EOF

    chmod +x "$INSTALL_DIR/start-dev.sh"

    print_success "启动脚本已生成"
}

# 生成配置文件
generate_config_file() {
    print_step "生成配置文件"

    REVIEW_SERVER_HOST=${REVIEW_SERVER_HOST:-0.0.0.0}
    REVIEW_SERVER_PORT=${REVIEW_SERVER_PORT:-5000}
    REVIEW_LOG_LEVEL=${REVIEW_LOG_LEVEL:-INFO}
    REVIEW_MAX_DIFF_SIZE=${REVIEW_MAX_DIFF_SIZE:-50000}
    REVIEW_TIMEOUT=${REVIEW_TIMEOUT:-180}
    OPENAI_REASONING_EFFORT=${OPENAI_REASONING_EFFORT:-medium}

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
    if curl -s http://localhost:5000/health > /dev/null 2>&1; then
        print_success "服务启动成功!"
        print_info "健康检查: curl http://localhost:5000/health"
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
    echo "    ├── src/"
    echo "    │   ├── review_server.py  # 主服务代码"
    echo "    │   └── wsgi.py           # WSGI 入口"
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
    echo "  URL: http://$(hostname -s | head -1).local:5000/webhook"
    echo "  或:  http://$(ifconfig | grep 'inet ' | grep -v 127.0.0.1 | head -1 | awk '{print $2}'):5000/webhook"
    echo ""
    echo "测试命令:"
    echo "  curl http://localhost:5000/health"
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
    setup_venv
    generate_start_scripts
    generate_config_file
    generate_launchd_plist
    start_service
    show_summary
}

main "$@"

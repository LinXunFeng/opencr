#!/bin/bash
#
# 快速测试脚本 - 验证安装和服务状态
#

set -e

BLUE='\033[0;34m'
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo -e "${BLUE}================================${NC}"
echo -e "${BLUE}OpenCR 测试工具${NC}"
echo -e "${BLUE}================================${NC}"
echo ""

# 1. 检查服务状态
echo -e "${BLUE}[1/5] 检查服务状态${NC}"
if launchctl list | grep -q "com.opencr.server"; then
    echo -e "${GREEN}✓${NC} 服务已注册到 launchd"
else
    echo -e "${RED}✗${NC} 服务未注册"
fi

# 2. 检查端口监听
echo ""
echo -e "${BLUE}[2/5] 检查端口监听${NC}"
if netstat -an 2>/dev/null | grep -q ".5000 " || lsof -i :5000 2>/dev/null | grep -q LISTEN; then
    echo -e "${GREEN}✓${NC} 端口 5000 正在监听"
else
    echo -e "${RED}✗${NC} 端口 5000 未监听"
fi

# 3. 健康检查
echo ""
echo -e "${BLUE}[3/5] 健康检查${NC}"
if curl -s http://localhost:5000/health > /dev/null 2>&1; then
    echo -e "${GREEN}✓${NC} 健康检查通过"
    echo ""
    echo "响应内容:"
    curl -s http://localhost:5000/health | python3 -m json.tool 2>/dev/null || curl -s http://localhost:5000/health
else
    echo -e "${RED}✗${NC} 健康检查失败"
fi

# 4. 检查日志
echo ""
echo -e "${BLUE}[4/5] 检查最近日志${NC}"
LOG_DIR="$HOME/opencr/logs"
ERROR_LOG="$LOG_DIR/error.log"
ACCESS_LOG="$LOG_DIR/access.log"

if [[ -d "$LOG_DIR" ]]; then
    echo -e "${GREEN}✓${NC} 日志目录存在"

    if [[ -f "$ERROR_LOG" ]]; then
        echo -e "${GREEN}✓${NC} 错误日志存在"
        echo ""
        echo "最近 5 条错误日志:"
        tail -n 5 "$ERROR_LOG" | sed 's/^/  /'
    else
        echo -e "${YELLOW}!${NC} 错误日志尚未生成 (服务可能未完全启动)"
    fi

    if [[ -f "$ACCESS_LOG" ]]; then
        echo -e "${GREEN}✓${NC} 访问日志存在"
    fi
else
    echo -e "${RED}✗${NC} 日志目录不存在: $LOG_DIR"
fi

# 5. 检查配置
echo ""
echo -e "${BLUE}[5/5] 检查配置${NC}"
CONFIG_FILE="$HOME/opencr/config.yaml"
if [[ -f "$CONFIG_FILE" ]]; then
    echo -e "${GREEN}✓${NC} 配置文件存在"
    echo ""
    CODE_PLATFORM_URL=$(awk '
        /^code_platform:/ {in_section=1; next}
        /^[^[:space:]]/ {in_section=0}
        in_section && /^[[:space:]]*url:/ {
            sub(/^[[:space:]]*url:[[:space:]]*/, "", $0)
            gsub(/"/, "", $0)
            print
            exit
        }
    ' "$CONFIG_FILE")
    OPENAI_MODEL=$(awk '
        /^openai:/ {in_section=1; next}
        /^[^[:space:]]/ {in_section=0}
        in_section && /^[[:space:]]*model:/ {
            sub(/^[[:space:]]*model:[[:space:]]*/, "", $0)
            gsub(/"/, "", $0)
            print
            exit
        }
    ' "$CONFIG_FILE")

    echo "代码平台 URL: ${CODE_PLATFORM_URL:-未配置}"
    if grep -qE '^[[:space:]]*token:' "$CONFIG_FILE"; then
        echo "代码平台 Token: 已配置"
    else
        echo "代码平台 Token: 未配置"
    fi
    if grep -qE '^[[:space:]]*base_url:' "$CONFIG_FILE"; then
        echo "OpenAI Base URL: 已配置"
    else
        echo "OpenAI Base URL: 未配置"
    fi
    if grep -qE '^[[:space:]]*api_key:' "$CONFIG_FILE"; then
        echo "OpenAI API Key: 已配置"
    else
        echo "OpenAI API Key: 未配置"
    fi
    echo "OpenAI Model: ${OPENAI_MODEL:-未配置}"
else
    echo -e "${RED}✗${NC} 配置文件不存在"
fi

# 显示使用提示
echo ""
echo -e "${BLUE}================================${NC}"
echo "常用命令:"
echo "  启动服务: launchctl start com.opencr.server"
echo "  停止服务: launchctl stop com.opencr.server"
echo "  查看错误日志: tail -f ~/opencr/logs/error.log"
echo "  查看访问日志: tail -f ~/opencr/logs/access.log"
echo "  手动触发: curl -X POST http://localhost:5000/manual-review \\"
echo "            -H 'Content-Type: application/json' \\"
echo "            -d '{\"project_id\": 123, \"mr_iid\": 1}'"
echo -e "${BLUE}================================${NC}"

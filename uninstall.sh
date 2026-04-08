#!/bin/bash
#
# OpenCR - 卸载脚本
#

set -e

SERVICE_NAME="com.opencr.server"
INSTALL_DIR="$HOME/opencr"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo -e "${YELLOW}正在卸载 OpenCR...${NC}"

# 停止服务
echo "停止服务..."
launchctl stop "$SERVICE_NAME" 2>/dev/null || true
launchctl unload "$HOME/Library/LaunchAgents/$SERVICE_NAME.plist" 2>/dev/null || true

# 删除 plist
if [[ -f "$HOME/Library/LaunchAgents/$SERVICE_NAME.plist" ]]; then
    rm -f "$HOME/Library/LaunchAgents/$SERVICE_NAME.plist"
    echo -e "${GREEN}✓${NC} 已删除 launchd 配置"
fi

# 询问是否删除数据
echo ""
read -p "是否删除所有数据目录 (包括日志)? [y/N]: " DELETE_DATA
echo ""

if [[ "$DELETE_DATA" =~ ^[Yy]$ ]]; then
    if [[ -d "$INSTALL_DIR" ]]; then
        rm -rf "$INSTALL_DIR"
        echo -e "${GREEN}✓${NC} 已删除安装目录: $INSTALL_DIR"
    fi

    echo ""
    echo -e "${GREEN}卸载完成!${NC}"
else
    echo -e "${YELLOW}保留数据目录，仅停止服务${NC}"
    echo "如需完全删除，请手动运行:"
    echo "  rm -rf $INSTALL_DIR"
fi

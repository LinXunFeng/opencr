# OpenCR - 自动代码审查系统

基于 GitLab Webhook + OpenAI API 的自动化代码审查方案，部署在 Mac 设备上。

Language: [English](./README.md) | 中文

**特色功能**：支持项目内 `config.yaml` 配置（提供 `config.example.yaml` 模板），安装时可交互确认并补齐必填项。

---

## 目录

1. [快速开始](#快速开始)
2. [项目结构](#项目结构)
3. [架构概述](#架构概述)
4. [安装部署](#安装部署)
5. [GitLab 配置](#gitlab-配置)
6. [运行与维护](#运行与维护)
7. [故障排查](#故障排查)

---

## 快速开始

```bash
# 1. 克隆或下载本项目
cd opencr

# 2. 准备配置文件（必填项见 config.example.yaml）
cp config.example.yaml config.yaml

# 3. 按需修改 config.yaml 后运行一键安装脚本
chmod +x install.sh
./install.sh

# 4. 安装完成！查看 Webhook URL
./quick-test.sh
```

---

## 项目结构

```
opencr/
├── install.sh              # 一键安装脚本
├── uninstall.sh            # 卸载脚本
├── quick-test.sh           # 快速测试工具
├── config.example.yaml     # 配置模板（复制为 config.yaml）
├── README.md               # 英文文档
├── README-zh.md            # 本文档（中文）
├── skills/                 # 审查技能目录
│   └── review/             # skill 文档（general/flutter/ts...）
├── src/                    # 源代码（已实现）
│   ├── __init__.py
│   ├── review_server.py    # Flask 主服务
│   └── wsgi.py             # WSGI 生产入口
└── .gitignore

# 安装后生成的目录
~/opencr/
├── src/                    # 从项目复制
├── skills/                 # 从项目复制（自动 skill 路由依赖）
├── logs/                   # 日志目录
├── venv/                   # Python 虚拟环境
├── config.yaml             # 运行时配置文件
├── start.sh                # 生产启动脚本
└── start-dev.sh            # 开发启动脚本
```

---

## 架构概述

```
┌─────────────┐     Webhook      ┌─────────────────┐     ┌─────────────┐
│   GitLab    │ ───────────────> │  Review Server  │ --> │ OpenAI API  │
│  (Private)  │                  │   (Mac Runner)  │     │ (Compatible)│
└─────────────┘                  └─────────────────┘     └─────────────┘
       ^                                                        |
       |                                                        |
       └────────────────  MR Comment <──────────────────────────┘
```

### 工作流程

1. 开发者提交 MR → GitLab 触发 Webhook
2. Review Server 接收事件，获取 MR diff
3. 调用 OpenAI API 进行代码审查
4. 将审查结果作为评论写入 MR

---

## 安装部署

### 方法一：一键安装脚本（推荐）

```bash
# 添加执行权限并运行安装脚本
chmod +x install.sh
./install.sh
```

安装脚本会自动完成：
- ✅ 检查系统要求（Python3、pip、curl）
- ✅ 读取项目 `config.yaml`（若存在）并交互确认配置项
- ✅ 复制源码和 `skills/` 到 `~/opencr/`
- ✅ 创建 Python 虚拟环境并安装依赖
- ✅ 生成启动脚本和 launchd 配置
- ✅ 启动服务并验证

### 方法二：手动安装

#### 1. 系统要求

| 项目 | 要求 |
|------|------|
| macOS | 12.0+ |
| Python | 3.9+ |
| curl | 用于验证 GitLab 连接 |

#### 2. 安装依赖

```bash
# 创建安装目录
mkdir -p ~/opencr && cd ~/opencr

# 创建虚拟环境
python3 -m venv venv
source venv/bin/activate

# 安装依赖
pip install openai flask gunicorn python-dotenv requests

# 复制源码
cp -r /path/to/opencr/src ./
cp -r /path/to/opencr/skills ./
```

#### 3. 配置 `config.yaml`

创建 `~/opencr/config.yaml`：

```yaml
openai:
  base_url: "https://api.openai.com/v1"
  api_key: "sk-your-key"
  model: "gpt-4.1"
  reasoning_effort: "medium"

code_platform:
  type: "gitlab"
  url: "https://gitlab.your-company.com"
  token: "glpat-your-token"
  webhook_secret: "your-secret-token"

server:
  host: "0.0.0.0"
  port: 5000
  log_level: "INFO"

review:
  max_diff_size: 50000
  timeout: 180
  skills_dir: "skills/review"
```

#### 4. 启动服务

```bash
# 开发模式
source venv/bin/activate
cd src && python3 review_server.py

# 或使用 Gunicorn
gunicorn --bind 0.0.0.0:5000 --chdir src "review_server:app"
```

---

## GitLab 配置

### 1. 创建 Access Token

1. 登录 GitLab → User Settings → Access Tokens
2. 创建新 Token，勾选以下权限：
   - `api` - 访问 GitLab API
   - `write_repository` - 读写仓库
3. 保存生成的 Token

### 2. 配置 Webhook

进入项目 → Settings → Webhooks：

| 配置项 | 值 |
|--------|-----|
| URL | `http://你的MacIP:5000/webhook` |
| Secret Token | 可选，若填写需与 `GITLAB_WEBHOOK_SECRET` 一致 |
| Trigger | 勾选 **Merge request events** |
| SSL Verification | 如果是内网 HTTP，取消勾选 |

> **提示**：安装脚本完成时会显示你的 Webhook URL

### 3. Webhook 测试

保存后，点击 **Test** → **Merge requests** 进行测试。

---

## 运行与维护

### 服务管理

```bash
# 查看服务状态
launchctl list | grep opencr

# 启动服务
launchctl start com.opencr.server

# 停止服务
launchctl stop com.opencr.server

# 查看日志
tail -f ~/opencr/logs/server.log
tail -f ~/opencr/logs/launchd.err.log
```

### 快速测试

```bash
# 使用测试脚本
./quick-test.sh

# 手动测试
# 健康检查
curl http://localhost:5000/health

# 手动触发审查
curl -X POST http://localhost:5000/manual-review \
  -H "Content-Type: application/json" \
  -d '{"project_id": 123, "mr_iid": 456, "review_mode": "file"}'
```

### 更新部署

```bash
cd ~/opencr

# 更新源码（从项目目录复制新版本）
cp -r /path/to/new/src/* src/
cp -r /path/to/new/skills/* skills/

# 重启服务
launchctl stop com.opencr.server
launchctl start com.opencr.server
```

---

## 配置详解

### OpenAI 配置读取顺序

系统按以下顺序加载 OpenAI 配置：

1. 项目 `config.yaml`（推荐）
2. 环境变量覆盖（可选）
   - `OPENAI_BASE_URL`
   - `OPENAI_API_KEY`
   - `OPENAI_MODEL`
   - `OPENAI_REASONING_EFFORT`
3. 若仍缺失，再回退到 `~/.codex/config.toml` + `~/.codex/auth.json`

推荐在项目根目录使用 `config.example.yaml` 生成 `config.yaml` 并维护配置。

### 审查策略

以下情况会自动跳过审查：
- MR 标题包含 `WIP`、`Draft`、`skip-review`
- 源分支为 `dependabot/*`

MR 事件与审查模式映射：
- `open`：执行整体 + 文件级审查
- `update` 且有新提交：仅针对本次新增提交区间执行文件级审查（行内评论）
- `reopen`：忽略（不触发审查）

- 审查策略由 webhook 事件与手动接口共同决定
- 审查模式由 MR 事件（`open/update`）或手动接口 `review_mode` 控制
- skill 由 AI 自动选择，依据：
  - `skills/review/*.md` 的描述
  - 本次变更的文件路径与 diff 内容
- 若无命中 skill，则对应审查分支会被跳过

---

## 故障排查

### 常见问题

#### 1. 服务无法启动

```bash
# 检查日志
tail -f ~/opencr/logs/launchd.err.log

# 检查端口占用
lsof -i :5000

# 手动启动查看错误
cd ~/opencr && ./start-dev.sh
```

#### 2. 代码平台 API 403 错误

- 检查 `CODE_PLATFORM_TOKEN` 是否有效
- 确认 Token 有相应权限（GitLab: `api`, GitHub: `repo`）
- 检查项目权限

#### 3. API 调用失败

```bash
# 检查安装目录配置文件
cat ~/opencr/config.yaml

# 测试 API 连通性
curl -H "Authorization: Bearer sk-your-key" \
  http://your-openai-compatible-domain:port/v1/models
```

#### 4. Webhook 无法访问

```bash
# 检查服务是否监听
netstat -an | grep 5000

# 从其他机器测试
curl http://你的MacIP:5000/health

# 检查防火墙
sudo /usr/libexec/ApplicationFirewall/socketfilterfw --list
```

#### 5. 大 MR 审查超时

编辑 `~/opencr/config.yaml`：

```yaml
review:
  timeout: 300
  max_diff_size: 30000
```

然后重启服务。

### 完整重装

```bash
# 1. 卸载
./uninstall.sh

# 2. 重新安装
./install.sh
```

---

## 源码说明

### `src/review_server.py`

主要功能模块：

| 函数 | 说明 |
|------|------|
| `load_openai_config()` | 优先读取 `config.yaml`，再应用环境变量覆盖，缺失时回退 `~/.codex` |
| `truncate_diff()` | 智能截断大 diff，优先保留重要文件 |
| `call_codex_review()` | 调用 OpenAI API 进行审查 |
| `handle_webhook()` | 处理 GitLab Webhook 事件 |
| `should_review_mr()` | 判断是否需要审查（过滤 Draft 等） |

### `src/wsgi.py`

Gunicorn 生产环境入口文件。

---

## 安全注意事项

1. **Token 安全**
   - `~/opencr/config.yaml` 权限设置为 600
   - 不要将 Token 提交到代码仓库

2. **Webhook 安全**
   - 配置 `WEBHOOK_SECRET` 验证请求来源
   - 建议部署在内网，通过 VPN 访问

3. **API Key 安全**
   - 建议将项目 `config.yaml` 权限设置为 600
   - 不要将包含真实密钥的 `config.yaml` 提交到代码仓库

---

## 扩展开发

### 自定义审查 Skill

将提示词文件放到 `skills/review` 目录。
服务会根据 skill 描述与代码变更自动选择一个或多个命中 skill。
若未命中 skill，该审查分支会直接跳过。

示例：

```text
skills/review/flutter.md
skills/review/ts.md
skills/review/security.md
```

### 添加自定义过滤规则

修改 `should_review_mr` 函数：

```python
def should_review_mr(data: dict) -> tuple:
    # 添加你的过滤逻辑
    attrs = data.get("object_attributes", {})
    title = (attrs.get("title") or "").lower()
    if "[skip-review]" in title:
        return False, "标题命中跳过关键字", ""
    return True, "符合审查条件", "overall"
```

---

## 参考资料

- [GitLab Webhook Events](https://docs.gitlab.com/ee/user/project/integrations/webhook_events.html)
- [GitLab API - Merge Requests](https://docs.gitlab.com/ee/api/merge_requests.html)
- [OpenAI API Documentation](https://platform.openai.com/docs/api-reference)

---

## 许可证

本项目采用 Apache License 2.0 协议，详情见 [LICENSE](./LICENSE)。

---

**如有问题，请检查日志文件或运行 `./quick-test.sh` 进行诊断。**

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
6. [后台管理](#后台管理)
7. [运行与维护](#运行与维护)
8. [故障排查](#故障排查)

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
│   └── review/             # skill bundle（name/SKILL.md、references、scripts、assets）
├── Dockerfile              # 容器镜像
├── docker-compose.yml      # 容器编排（推荐的部署方式）
├── CONTEXT.md              # 领域术语表
├── docs/adr/               # 架构决策记录
├── backend/                # 后端源码（Python 包）
│   ├── review_server.py    # Flask 路由与线程调度
│   ├── wsgi.py             # WSGI 生产入口
│   ├── review/             # 审查执行、结算、GitLab 交互
│   │   ├── runner.py       # ReviewRun 统一执行入口
│   │   └── settlement.py   # 采纳结论判定
│   ├── storage/            # 持久化（SQLAlchemy）
│   ├── migrations/         # Alembic 迁移脚本
│   ├── alembic.ini         # 迁移配置
│   └── admin/              # 后台管理路由与页面
└── .gitignore

# 安装后生成的目录
~/opencr/
├── backend/                # 从项目复制
├── skills/                 # 从项目复制（自动 skill 路由依赖）
├── data/                   # SQLite 数据库（审查历史与采纳统计）
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

支持两种方式：**Docker**（推荐，跨平台）与 **macOS 一键脚本**（launchd，适合在自己的 Mac 上跑）。

### 方法一：Docker（推荐）

```bash
# 1. 准备配置（必须先做，否则第 2 步会直接报错退出）
cp config.example.yaml config.yaml
# 按需修改 config.yaml，尤其是 openai 与 code_platform 两段；
# 想用后台面板还需把 admin.enabled 改为 true 并填上 admin.token

# 2. 构建并启动
docker compose up -d

# 3. 查看日志与健康状态
docker compose logs -f
curl http://localhost:9034/health
```

`docker-compose.yml` 定义了四个挂载点：

| 挂载 | 说明 |
|------|------|
| `./config.yaml` → `/app/config.yaml` | 只读。**必须存在**，缺失时容器会直接退出并提示 |
| `./skills` → `/app/skills` | 只读。挂载后**整体覆盖**镜像内置的默认 skills；改一条审查规则不需要重建镜像 |
| `opencr-data` → `/app/data` | 审查历史与采纳统计的数据库。**删掉等于统计归零，且无法回填** |
| `opencr-logs` → `/app/logs` | 运行日志 |

容器启动时会自动执行数据库迁移（`alembic upgrade head`），升级镜像后无需手动操作。

> **注意**：`skills/*/scripts/` 下的脚本是在**容器内**执行的，镜像只保证有 `python3`。
> 如果你的自定义 skill 脚本依赖别的运行时（Node、Dart 等），需要基于本镜像自建一个。

### 方法二：macOS 一键安装脚本

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

# 复制依赖清单
cp /path/to/opencr/requirements.txt ./

# 安装依赖
pip install -r requirements.txt

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
  port: 9034
  log_level: "INFO"

review:
  max_diff_size: 50000
  timeout: 180
  skills_dir: "skills"
  skill_scripts_enabled: true
  skill_scripts_timeout: 10
```

#### 4. 启动服务

```bash
# 开发模式
source venv/bin/activate
cd src && python3 review_server.py

# 或使用 Gunicorn
gunicorn --bind 0.0.0.0:9034 --chdir src "review_server:app"
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
| URL | `http://你的MacIP:9034/webhook` |
| Secret Token | 可选，若填写需与 `GITLAB_WEBHOOK_SECRET` 一致 |
| Trigger | 勾选 **Merge request events** |
| SSL Verification | 如果是内网 HTTP，取消勾选 |

> **提示**：安装脚本完成时会显示你的 Webhook URL

### 3. Webhook 测试

保存后，点击 **Test** → **Merge requests** 进行测试。

---

## 后台管理

后台是一个 Vue 3 单页应用（Element Plus + ECharts），挂在 `/admin` 下，由 Flask 自己托管，
不依赖任何 CDN —— 内网访问不到外网也能正常显示。

### 启用

后台**默认关闭**。启用需要在 `config.yaml` 中设置：

```yaml
admin:
  enabled: true
  username: "admin"
  # 直接写明文即可。服务首次启动时会就地把它替换成 scrypt 哈希，
  # 并在上方加一行注释。想改密码就把这一行换回明文再重启。
  password: "换成一个足够长的密码"
  # webhook 端口通常内网可达；只在本机访问后台时建议打开这项
  bind_local_only: false
```

> `admin.enabled` 为 `true` 但 `password` 为空时，服务会**拒绝启动** ——
> 否则等于把一个无鉴权的管理面板挂在内网可达的端口上。

> **从 0.4.0 升级**：`admin.token` 已被移除，改为 `username` + `password`。
> 命中旧字段时服务会拒绝启动并直接告诉你怎么改。

启用后访问 `http://localhost:9034/admin` 即可。

### 游客浏览

后台支持**游客浏览**，默认开启：未登录的同事可以直接查看审查状态，不需要账号。

游客能看到运行状态、进度、错误统计与采纳统计；**看不到审查发现的正文**。
因为正文包含 AI 对私有仓库代码的具体描述（文件路径、行号、问题与修复建议），
而 webhook 端口按部署方式通常是内网可达的。

这个开关在后台的「系统设置」里改，立即生效，是本项目唯一不在 `config.yaml` 里的配置项 ——
它的使用场景天然是临时性的（"今天有外部人员来，先关一下"），要求改文件加重启就等于没人会用。

完整取舍见 [ADR-0002](./docs/adr/0002-guest-read-scope.md)。

### 功能模块

| 模块 | 内容 | 游客可见 |
|------|------|:---:|
| 控制台 | 关键指标卡片、进行中的审查、运行趋势图、采纳分布图 | ✅ |
| 审查记录 | 运行列表（可筛状态、搜项目/MR）与运行详情 | ✅（正文除外）|
| 审查发现 | 跨运行的发现列表，可按项目/采纳结论/严重度/时间窗筛选 | ✅（正文除外）|
| 统计分析 | 采纳分析（采纳率、覆盖率、按投递方式拆解）、错误分析（趋势、失败原因、降级明细）| ✅ |
| Skill 管理 | 已加载的 skill 及近期命中次数 | ✅ |
| 系统设置 | 当前生效配置（密钥只显示"已配置"）、可写子集 | ❌ |

### 几个口径必须先看懂

**进度**：`overall` 模式是单次大模型调用，中间没有可观测点，只报阶段不报百分比；
`file` 模式逐文件调用，额外给出 `3/17` 的文件计数。面板上不会出现编造的进度条。

**「进行中」与「疑似中断」**：服务是多进程运行的，任何一个进程都无法断言其他进程的审查已经死了。
因此心跳超时（默认 600 秒，`storage.stale_after_seconds`）只会把状态**标注**为「疑似中断」，
不会改写成失败 —— 它也可能只是卡在一次特别慢的模型调用上。

**失败 / 降级 / 跳过**是三档不同的东西，不要混着看：

- **失败**：审查没跑完（模型或平台调用失败、未捕获异常）
- **降级**：流程走完了但质量受损 —— 行内评论投递失败后已降级为普通评论（内容仍然送达）、diff 被截断
- **跳过**：Draft/WIP、dependabot、merge commit 触发的更新等。**这不是错误**

**采纳率是近似值**。判定依据是 GitLab discussion 的 resolved 状态与 👍/👎 表态，
本版本**不做代码改动验证**，因此「已 resolve」会被计为采纳，而现实中它有时只表示「我看过了」。
完整取舍见 [ADR-0001](./docs/adr/0001-suggestion-acceptance-via-discussion-state.md)。

**覆盖率必须和采纳率一起看**。只有发布成 GitLab discussion 的审查发现才能被结算；
`overall` 模式那条整体评论走的是**不可 resolve 的普通 note**，行内投递失败降级成的普通评论同理，
两者都无法判定采纳。它们仍会计入产出总数，因此面板会同时给出
「12 条中采纳 9 条」和「本次共产出 40 条，其中 12 条可追踪」。

### 数据保留

审查记录默认保留 90 天（`storage.retention_days`），由后台任务自动清理。
**采纳数据无法回填** —— 本功能上线前的历史 MR 永远不会有采纳结论，被清理掉的数据同样不可恢复。

### 二次开发

前端源码在 `web/`，是独立的构建单元：

```bash
cd web
pnpm install
pnpm dev      # 开发模式，自动把 /api/admin 代理到本地 9034
pnpm build    # 构建到 backend/admin/static/
```

**构建产物不进 Git**（仓库保持干净），它由部署流程各自生成：

- **Docker**：`Dockerfile` 多阶段构建里编译，Node 只存在于构建阶段，不进最终镜像
- **launchd**：`install.sh` 在安装时编译；机器上没有 Node/pnpm 时会跳过并提示，
  审查服务照常可用，只是 `/admin` 会返回一条"产物缺失"的说明，补装 Node 后重跑 `./install.sh` 即可

因此改完前端**只需提交源码**。更多约定见 [`web/README.md`](./web/README.md)。

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
curl http://localhost:9034/health

# 手动触发审查（异步：立即返回 202 与 run_uid）
curl -X POST http://localhost:9034/manual-review \
  -H "Content-Type: application/json" \
  -d '{"project_id": 123, "mr_iid": 456, "review_mode": "file"}'
# => {"message":"Review started","run_uid":"...","status":"processing", ...}

# 查看这次审查的进度与结果
curl -H "X-Admin-Token: 你的token" http://localhost:9034/api/admin/runs/<run_uid>
```

> **0.4.0 起 `/manual-review` 是异步的**：它不再同步返回审查内容，而是返回 `202` 与
> `run_uid`。这样它和 webhook 走的是同一条执行路径，状态机只有一份。

### 更新部署

Docker：

```bash
git pull && docker compose up -d --build
# 数据库迁移在容器启动时自动执行
```

macOS launchd：

```bash
# 重跑安装脚本即可，数据库会自动迁移，已有数据不受影响
./install.sh
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
  - `skills/<name>/SKILL.md` 元信息，或兼容旧版 `skills/<name>.md` 描述
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
lsof -i :9034

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
netstat -an | grep 9034

# 从其他机器测试
curl http://你的MacIP:9034/health

# 检查防火墙
sudo /usr/libexec/ApplicationFirewall/socketfilterfw --list
```

#### 5. 大 MR 审查超时

编辑 `~/opencr/config.yaml`：

```yaml
review:
  timeout: 300
  max_diff_size: 30000
  skill_scripts_timeout: 10
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

### `backend/review_server.py`

主要功能模块：

| 函数 | 说明 |
|------|------|
| `load_openai_config()` | 优先读取 `config.yaml`，再应用环境变量覆盖，缺失时回退 `~/.codex` |
| `truncate_diff()` | 智能截断大 diff，优先保留重要文件 |
| `call_codex_review()` | 调用 OpenAI API 进行审查 |
| `handle_webhook()` | 处理 GitLab Webhook 事件 |
| `should_review_mr()` | 判断是否需要审查（过滤 Draft 等） |

### `backend/wsgi.py`

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

将标准 skill bundle 放到 `skills` 目录。
每个 bundle 以 `SKILL.md` 为入口，也可以包含 `references/`、`scripts/`、`assets/`。
服务会根据 skill 元信息与代码变更自动选择一个或多个命中 skill。
`scripts/` 下的可执行文件会通过 stdin 收到 JSON 审查上下文，并可输出补充上下文给模型。
若未命中 skill，该审查分支会直接跳过。

示例：

```text
skills/flutter/SKILL.md
skills/flutter/references/lifecycle.md
skills/asset/SKILL.md
skills/asset/scripts/summarize_assets.py
skills/security/SKILL.md
```

旧版 `skills/<name>.md` 仍然兼容，但目录式 bundle 才能使用完整 skill 资源模型。

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

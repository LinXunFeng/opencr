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
7. [定期巡检](#定期巡检)
8. [运行与维护](#运行与维护)
9. [故障排查](#故障排查)

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
│   ├── review/             # MR 审查链路：审查执行、结算、GitLab 交互
│   │   ├── runner.py       # ReviewRun 统一执行入口
│   │   └── settlement.py   # 采纳结论判定
│   ├── survey/             # 定期巡检链路：拉取、画像、跨仓库分析
│   │   ├── runner.py       # SurveyRun 统一执行入口
│   │   ├── scheduler.py    # 调度线程（独立租约）
│   │   ├── workspace.py    # 仓库拉取与工作区管理
│   │   ├── profile.py      # 仓库画像（codegraph 集成）
│   │   ├── crossrepo.py    # 跨仓库接口连接
│   │   ├── ledger.py       # 问题台账：状态判定与镜像行生成
│   │   ├── push.py         # 推送到输出目标
│   │   └── destinations/   # 输出目标插件（当前：Google Sheet）
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
├── workspaces/             # 巡检工作区（各巡检的仓库副本，可能占用大量磁盘）
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

服务里有**两条彼此独立的审查链路**：

```
事件驱动（MR 审查）
┌─────────────┐     Webhook      ┌─────────────────┐     ┌─────────────┐
│   GitLab    │ ───────────────> │  Review Server  │ --> │ OpenAI API  │
└─────────────┘  <── MR 评论 ──  └─────────────────┘     └─────────────┘

时间驱动（定期巡检）
┌──────────┐   到点   ┌──────────┐  git 拉取  ┌──────────┐   画像   ┌─────────┐
│ 调度线程 │ ───────> │ 巡检执行 │ ────────> │ 本地工作区 │ ─────> │ 整合分析 │
└──────────┘          └──────────┘           └──────────┘         └─────────┘
                                                                        │
                                          后台报告 / Markdown 导出 <────┘
```

### 工作流程

**MR 审查**

1. 开发者提交 MR → GitLab 触发 Webhook
2. Review Server 接收事件，获取 MR diff
3. 调用 OpenAI API 进行代码审查
4. 将审查结果作为评论写入 MR

**定期巡检**

1. 调度线程发现某个巡检到期
2. 逐个仓库拉取到最新（首次浅克隆，之后重置本地修改再增量拉取）
3. 为每个仓库生成结构画像（依赖清单、接口路由、类型骨架）
4. 所有仓库的画像同时进入一次整合分析，产出需要深入查看的关注点
5. 按关注点读取真实源码取证，产出逐条发现；台账里仍为「存在」的问题所在的文件也一并复核
6. 与上一次的结果比对，报告分「新增 / 仍存在 / 已消失 / 本轮未复查」呈现

---

## 安装部署

支持两种方式：**Docker**（推荐，跨平台）与 **macOS 一键脚本**（launchd，适合在自己的 Mac 上跑）。

### 方法一：Docker（推荐）

```bash
# 1. 准备配置（必须先做，否则第 2 步会直接报错退出）
cp config.example.yaml config.yaml
# 按需修改 config.yaml，尤其是 openai 与 code_platform 两段；
# 想用后台面板还需把 admin.enabled 改为 true 并填上 admin.username 与 admin.password

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

启用后访问 `http://localhost:9034/admin` 即可。

### 游客浏览

后台支持**游客浏览**，默认开启：未登录的同事可以直接查看审查状态，不需要账号。

游客能看到运行状态、进度、错误统计与采纳统计；**看不到审查发现的正文**。
因为正文包含 AI 对私有仓库代码的具体描述（文件路径、行号、问题与修复建议），
而 webhook 端口按部署方式通常是内网可达的。

这个开关在后台的「系统设置」里改，立即生效，与游客重新触发开关一起存入数据库 ——
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
| 定期巡检 | 巡检配置、巡检记录与巡检报告 | 可配置（默认 ✅，正文除外）|
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

---

## 定期巡检

MR 审查只看一次变更。**定期巡检**补的是另一半：按周期把整组仓库的全量代码拉到本地，做一次跨仓库的整合分析。它最主要的价值是发现单仓库审查看不到的问题——前后端接口字段对不上、重复造轮子、依赖版本打架。

### 配置一个巡检

后台「定期巡检 → 巡检配置 → 新建巡检」，需要填四样东西：

| 项目 | 说明 |
|------|------|
| 执行周期 | 每天 / 每周 / 每月 + 时间，或直接填 cron 表达式。**务必确认时区**——容器默认 UTC，不填对会让"每周一早上 9 点"变成别的时间 |
| 仓库来源 | 可填具体仓库链接（能指定分支，留空则用该仓库的默认分支），也可以填一个组织 |
| 参与分析的 Skill | 逐项勾选，默认全选 |
| 巡检后是否删除工作区 | 默认不删。保留下来下次可以增量拉取；删掉意味着下次全量克隆 |

**组织是实时展开的。** 每次执行时才去列举组织下的项目，因此往组织里新增的仓库会自动纳入下一次巡检。配套的排除模式用来挡掉不该扫的仓库——否则别人往组织里推一个大仓库，你的巡检就要多跑一小时。

**勾选 Skill 决定的是候选池，不是执行清单。** 勾中的 skill 才有资格参与，但仍要由 AI 按仓库画像（语言构成与依赖清单）匹配。一个纯 Dart 的仓库不会被 `ts` skill 硬审一遍——那产出的是模型硬凑出来的假问题。

### 报告怎么看

巡检每轮的分析范围是**全量代码**，但不是每个文件都会被读源码：整合分析从全部仓库的结构画像里挑出一批文件（最多为关注点上限）读源码取证，另外复核台账里仍为「存在」的问题所在的文件（名额与关注点上限相同、另算，最久没复核过的优先）。老问题每轮都会被复核，所以逐轮产出高度重合：第一次跑出 80 条，第二周还是那 80 条加上新增的几条。报告因此分四段呈现，默认停在「新增」：

- **新增** —— 上一次没有、这次出现的
- **仍存在** —— 上一次也有，或台账里仍为「存在」的
- **较上次已消失** —— 上一次有、这次取证过所在文件但没再检出的（不等于已修复）
- **本轮未复查** —— 上一次有、这次没再报出，但所在文件这次没得出可信结论的（没轮到取证、文件超出读取上限只看了片段、源码读取或模型输出失败）

判定依据是 `仓库 + 文件路径 + 问题类别` 三者构成的指纹，**不含正文**——模型两次描述同一个问题的措辞不会一样，把正文纳入比对会让每一轮的产出全部变成"新增"。

觉得某条问题不用再提，可以标记为「不再提醒」并写下理由，后续巡检不会再产出它。忽略按指纹生效，因此会连带屏蔽同一文件里同一类别的其他问题。每个巡检的忽略清单可以在巡检配置列表的「忽略清单」里查看，在那里补写理由或取消忽略。

报告支持导出 Markdown。导出与界面同源，因此游客导出的报告同样不含正文。

### 推送到外部平台（输出目标）

巡检结果可以镜像到外部平台，方便在表格里分派和跟进。当前支持 **Google Sheet**；接口按「按键 upsert」设计，飞书多维表格等平台可以按同样的方式接入。

**推送的是问题台账，不是每轮的快照。** 台账按「巡检 + 指纹」一行，每轮结束后只更新状态与最近发现时间。逐轮追加快照的话，每周跑一次，同一个问题会在表里出现几十行，没法跟进。台账由服务自己持有，平台上的表格只是它的镜像，理由见 [`docs/adr/0004-survey-ledger-mirrored-to-destinations.md`](docs/adr/0004-survey-ledger-mirrored-to-destinations.md)。

**1. 在 `config.yaml` 里配置实例。** 凭据只写在这里，不入库：

```yaml
server:
  public_url: "https://opencr.company.com"   # 可选；填了才会输出「运行详情」链接列

destinations:
  quality-sheet:                              # 实例名，后台绑定时选择它
    type: google_sheet
    credentials_file: "/path/to/service-account.json"
```

Google Sheet 支持两种鉴权方式，由实例的 `auth` 决定：

- **服务账号（`auth: service_account`，缺省）**：在 Google Cloud 控制台启用 Google Sheets API，创建服务账号并下载 JSON 密钥，然后**把表格以「编辑者」身份共享给服务账号的邮箱**。Docker 部署时把密钥文件挂载进容器（`docker-compose.yml` 里有注释好的示例），`credentials_file` 写容器内路径。
- **gogcli（`auth: gogcli`）**：复用 [gogcli](https://github.com/openclaw/gogcli) 里已登录的用户身份，把表格共享给这个账号即可。服务通过 `gog api call` 发出请求，自己不持有任何用户令牌，理由见 [`docs/adr/0005-google-sheet-via-gogcli.md`](docs/adr/0005-google-sheet-via-gogcli.md)。

```yaml
destinations:
  team-sheet:
    type: google_sheet
    auth: gogcli
    account: "someone@company.com"     # 必填，不依赖 gogcli 的默认账号
    # gogcli_bin: "/opt/homebrew/bin/gog" # 可选，缺省用 PATH 里的 gog
```

`account` 必须显式填写：别人在构建机上执行一次 `gog auth add` 就能换掉 gogcli 的默认账号，推送就会以另一个人的身份写表。

**launchd 部署**：先安装 gogcli（例如 `brew install openclaw/tap/gogcli`），再执行 `./install.sh`。`destinations` 里有 `auth: gogcli` 的实例时，安装脚本会把 gogcli 所在目录加进 launchd 的 PATH（`gogcli_bin` 因此可以不填），对尚未授权的账号逐个询问是否立即执行 `gog auth add <account> --services sheets` 在浏览器中授权，最后做一遍与后台「测试连通性」相同的检查。这次检查在终端里读取钥匙串，macOS 的授权弹窗会在你面前出现，点「始终允许」后，后台服务就不会再被它挡住。重跑 `./install.sh` 时已授权的账号直接跳过。

**Docker 部署**：镜像默认内置固定版本的 gogcli（不需要时用 `--build-arg GOGCLI_VERSION=` 跳过）。容器里没有钥匙串，令牌以加密文件存放在 `opencr-gogcli` 卷里。首次部署时在宿主机执行一次：

```bash
./scripts/setup-gogcli.sh --client-secret ~/Downloads/client_secret.json
```

脚本会在 `.env` 缺少 `GOG_KEYRING_PASSWORD` 时生成一个（已有的绝不改动，否则卷里已存的令牌就解不开了），启动容器、导入 OAuth 客户端信息，再为 `config.yaml` 里尚未授权的 gogcli 账号逐个授权：宿主机的 gogcli 已登录该账号时导出令牌再导入容器（两边的 OAuth 客户端必须是同一个），否则改为在容器里走粘贴回调地址的授权流程（`--manual` 可强制使用这种方式）。令牌临时文件无论成功失败，两边都会删掉。也可以显式指定账号：`./scripts/setup-gogcli.sh someone@company.com`。

令牌导入后留在卷里，之后重新部署、升级镜像都不用再执行。容器每次启动时还会：

- 若存在 `./secrets/gogcli-client-secret.json`（需在 `docker-compose.yml` 里启用 `./secrets` 挂载），自动导入 OAuth 客户端信息，这样执行脚本时可以省掉 `--client-secret`；
- 把每个 gogcli 实例的授权状态打印到启动日志，缺什么就给出要执行的命令。这些检查失败不阻止启动。

**密码丢失或对不上**（检查报 `aes.KeyUnwrap(): integrity check failed` 或 `file keyring password mismatch`）：卷里的令牌是用另一个 `GOG_KEYRING_PASSWORD` 加密的。注意 compose 里 shell 环境变量的优先级高于 `.env`，先确认两处没有各写一个。能找回原密码就把它写回 `.env`，执行 `docker compose up -d` 即可；找不回只能清空 gogcli 卷重新授权（只删令牌与客户端信息，不影响审查数据）：

```bash
docker compose down
docker volume rm "$(basename "$PWD")_opencr-gogcli"   # 卷名前缀是 compose 项目名，可用 docker volume ls 确认
./scripts/setup-gogcli.sh --client-secret ~/Downloads/client_secret.json
```

刻意不支持挂载令牌文件、每次启动自动导入：明文 refresh token 会一直留在宿主机上，而且每次重启都会用这份旧令牌覆盖在容器里重新授权过的令牌。

无论哪种方式，**OAuth 应用都要发布为正式版本**：处于 Testing 状态的应用签发的 refresh token 7 天就会过期，推送会从某一周开始静默失败。

**2. 在巡检配置里绑定。** 编辑巡检 → 输出目标 → 选择实例，填写表格链接（或 ID）与工作表名。工作表不存在时自动创建，留空则使用巡检名称。一个巡检可以绑定多个输出目标，多个巡检也可以共用同一张工作表（靠「巡检」列区分）。填好后点「测试连通性」，会依次检查服务账号凭据、能否读取表格、工作表与已有的系统列、能否写入，并给出服务账号邮箱方便去共享表格；测试不会创建工作表，也不会写入任何台账行。

**3. 推送时机。** 巡检**成功**结束后（带降级也算成功）自动推送；失败的运行不推送。在最近一次成功运行的报告页可以手动重推。推送失败只记在推送记录里，**不改变运行本身的状态，也不算降级**。

表里的列分两类：

- **系统列**：键、巡检、仓库、文件、行号、问题类别、严重度、标题、正文、状态、首次发现、最近发现、同类条数，以及可选的运行详情。**按表头名称定位**，所以可以随意调整列序；但**不要改系统列的表头**，改名后下次推送会补出一个新列。
- **人工列**：你自己加的负责人、处理进度等，系统从不读取也从不改写。

几条需要知道的规则：

- **「本轮未发现」不等于已修复。** 它只说明这一轮取证过问题所在的文件、模型没有再报出它。每轮除了模型选中的关注点，还会复核台账里仍为「存在」的问题所在的文件（最久没复核过的优先，名额与关注点上限相同、另算）。没轮到复核的文件——名额已满、预算耗尽、仓库拉取失败、文件超出单个关注点的读取上限、源码读取或模型输出失败——保持原状态。
- **同一轮里共享指纹的多条发现合并成一行**：严重度取最高，行号全列，正文按条拼接，超过单元格上限（5 万字符）时截断。
- **删掉的行补不补回。** 状态仍为「存在」的行被删后会补回，否则问题就被悄悄吞掉了；「本轮未发现」和「已忽略」的行被删后不再补回。新绑定的表第一次推送会写入完整台账。
- **标记「不再提醒」后**，台账里对应的行立刻变为「已忽略」，下次推送时同步到表里，不删行。
- 写入一律按原始文本处理，以 `=` 开头的正文不会被当成公式执行。
- 追加行时遇到服务端 5xx 会重试，若那次请求其实已经生效，表里会多出同一个键的重复行。影响有限：下次推送会同时更新这两行，数据不会出错，手动删掉多出的一行即可。
- **推送即交出了正文的可见性控制。** 谁能看到表里的正文由表格的共享设置决定，不受游客开关约束。游客在后台能看到推送状态，看不到目标位置与错误信息。

**接入新平台**：实现 `backend/survey/destinations/base.py` 里的 `Destination`，在 `backend/survey/destinations/__init__.py` 注册即可。核心层已经算好每一行的内容与状态，插件只需把行按键写到平台上；`check` 是可选实现，用于后台的「测试连通性」按钮。评估一个平台能不能接，看三点：能否按键查找已有行、能否批量写入、限流有多严。

### codegraph（默认安装）

巡检需要把全量代码降维成结构摘要才能交给模型——实测三个中等仓库合计 569 万字符，而组合画像约 43 万字符。[codegraph](https://github.com/colbymchenry/codegraph) 负责提取接口路由与类型骨架这一层，也是 Skill 匹配所依据的语言构成的来源。

整合分析只能从画像里带完整路径的文件中挑选取证对象（路由、类型骨架、接口调用、依赖清单、根目录文件），只有函数的文件、配置与脚本不管放在多深的目录都看不到。每轮运行会统计每个仓库的 **L1 可达**比例，显示在运行详情「覆盖的仓库」表格里；展开一行可以看到按来源、按目录深度的明细，以及看不到的文件最集中的目录和符号最多的文件（后两项需登录）。没有 codegraph 时可达范围会大幅缩小。

**两条部署路径都会默认安装它**，版本钉死在 `v1.6.0`：

- `install.sh` 自动下载安装到 `~/.codegraph`，并把**绝对路径**写进 `config.yaml` 的 `survey.codegraph_bin`
- Docker 构建时装进镜像

安装完成后会自动执行 `codegraph telemetry off` —— 它默认开启遥测，而这个服务的典型部署环境是内网自签证书的 GitLab 旁边，不该留一个说不清的外连。

> 它的安装包约 57MB，从 GitHub Releases 拉取可能很慢（实测 30–116 KB/s）。安装脚本用的是 HTTP/1.1 + 断点续传 + 重试，并在解包前校验压缩包完整性——官方一键脚本不续传不重试，实测会在拉了二十多分钟后以协议错误整体失败。

**装不上不会中断安装。** `install.sh` 会警告并继续，Docker 可以用 `--build-arg CODEGRAPH_VERSION=` 显式跳过。此时巡检照常执行，画像退化为依赖清单级并记录一条降级，代价是分析时只知道有哪些文件、不知道有哪些接口与类型。

**它这一轮起没起作用，看巡检报告里的「codegraph 执行情况与收益」卡片。** 卡片列出每个仓库的建索引方式（增量更新 / 全量重建 / 失败）、耗时和抽出的接口与类型数，以及它带来的跨仓库接口连接、方法不一致和被剔除的误报调用。关注点与发现按「线索来源」分为 codegraph / 基础画像 / 画像外三类；本轮 L1 没有点名、只因台账复核而取证的另记为「台账复核」，不计入 codegraph 占比的分母。注意线索来源是出处而不是因果，要量化收益，请对同一批提交分别开关 codegraph 各跑几轮（改 `config.yaml` 的 `survey.codegraph_enabled`，下一轮即生效、无需重启；直接运行服务时也可用环境变量 `OPENCR_SURVEY_CODEGRAPH_ENABLED` 覆盖），打开两边的报告对比这张卡片上的数字；巡检记录的 codegraph 列只适合快速浏览启用时的线索占比。

`survey.codegraph_bin` 写的是绝对路径不是 `codegraph` 三个字母，原因是 **launchd 的 PATH 里没有 `~/.local/bin`** ——写裸名会让服务运行时找不到它，表现为每次巡检都静默降级。手动装到别处时记得同步改这一项。

**每个仓库单独建索引，不要把多个仓库放进同一张图。** 这不是风格偏好：codegraph 不索引外部依赖，未解析的符号会被按名字连到工作区内任意同名节点上，跨语言也连。实测三个毫无关联的仓库放在一起产生了 824 条跨仓库边，**全部是假的**。完整证据见 [`docs/adr/0003-per-repo-codegraph-index.md`](docs/adr/0003-per-repo-codegraph-index.md)。

### 磁盘与预算

- **工作区可能很大。** 全量克隆多个仓库轻易到几十 GB，默认放在 `~/opencr/workspaces`，可用 `survey.workspace_dir` 改到别的盘。删除工作区不丢任何数据，只是下次要重新全量克隆。
- **删除巡检不会连带删工作区。** 误删一个巡检顺手删掉几十 GB 代码是不可逆的，工作区清理是界面上单独的动作。
- **单次巡检有硬预算**（墙钟上限、整合层输入上限、关注点数量上限；复核台账问题的文件数另算，上限与关注点数量相同）。撞到预算属于降级而非失败——已产出的结果照常保留，报告里会标出"预算耗尽提前收工"。

### 几个刻意的行为

- **错过窗口不补跑。** 服务停机跨过了执行时间点，重启后跳过本次并重排到下一个触发点。一个每周任务补跑一次全量分析，除了烧钱没有意义。
- **不并发。** 上一次还没跑完、下一次时间点又到了时直接跳过。
- **单个仓库失败不影响整轮。** 记一条降级后继续用剩下的仓库分析；**全部**仓库都失败才判整轮失败。

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

### 重新触发失败审查

在失败运行详情页点击「重新触发」，选择「原失败范围」或「最新全量」。前者重审原运行的完整提交区间，沿用原模式和选择参数，使用当前模型及技能内容；后者使用当前默认设置（与手动入口一致，默认 overall）审查最新 MR 全部变更。技能会重新匹配，不保证复现历史产出。

每次重试创建关联原失败运行的新 ReviewRun，保留原记录与评论，可能重复投递评论。历史运行缺少完整范围或参数时仅支持最新全量；原提交不可获取时运行明确失败，不切换范围。原运行因保留期清理后，来源链接可能不再可用。

仅明确失败、且 MR 仍为 opened 的运行可重试。同 MR 已有 running（含 Stale）时拒绝重试并提供运行入口；同时重试跨进程防重。该限制仅约束重试入口，后续 webhook 和原有手动入口仍可启动运行。

管理员可在「系统设置」开启「游客重新触发」，默认关闭且依赖游客浏览同时开启。游客仍看不到 Finding 正文。接口为 `POST /api/admin/runs/<run_uid>/retry`，JSON 为 `{"scope":"original"}` 或 `{"scope":"latest"}`，成功返回 202 与新 `run_uid`。

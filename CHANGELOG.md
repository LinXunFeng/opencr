# Changelog

本文件记录项目的重要变更。

## [0.5.0] - 2026-09-06

### Added
- 后台管理改为 Vue 3 单页应用（基于 [v3-admin-vite](https://github.com/un-pany/v3-admin-vite) + Element Plus + ECharts 6），包含控制台、审查记录、审查发现、统计分析、Skill 管理与系统设置六个模块。
- 新增账号密码登录。`admin.password` 直接写明文即可，服务首次启动时会就地把它替换为 scrypt 哈希并加上注释说明；配置文件不可写时服务照常运行，但每次启动会警告明文未清理。
- 新增**游客浏览**开关（默认开启）：未登录者可查看运行状态与聚合统计，但看不到审查发现的正文。开关存在数据库中、后台可改、立即生效。判定边界见 `docs/adr/0002-guest-read-scope.md`。
- 新增跨审查运行的「审查发现」列表，支持按项目 / 采纳结论 / 严重度 / 时间窗筛选——用于回答"这个项目所有被忽略的严重问题"这类单看某次运行问不出来的问题。
- 新增 Skill 管理页，展示已加载的 skill 及其近期命中次数（命中为 0 的 skill 等于没生效）。
- 新增 `/api/admin/*` 接口族：login、logout、me、dashboard、runs、findings、stats/verdicts、stats/errors、skills、settings。

### Changed
- **破坏性变更**：`admin.token` 共享令牌已移除，改为 `admin.username` + `admin.password`。命中旧字段时服务会拒绝启动并给出具体的迁移指引。
- 移除 Jinja 模板后台，`/admin` 改为托管 Vue 打包产物。`backend/admin/static/` 的构建产物**提交进 Git**，因此部署端不需要 Node。
- Docker 中 `config.yaml` 不再只读挂载，以便首次启动时写回密码哈希。
- 测试不再无条件使用 flask/openai/requests 桩：真依赖可用时用真的，桩只在缺库环境兜底。测试也不再读取开发机的 `config.yaml`。

### Fixed
- 修复多 worker 同时启动时 `PRAGMA journal_mode=WAL` 报 `database is locked` 的问题：`busy_timeout` 现在先于 `journal_mode` 设置，且 WAL 切换失败不再导致进程退出。

## [0.4.0] - 2026-09-05

### Added
- 新增后台管理面板 `/admin`：查看进行中的审查及其阶段/文件进度、最近运行记录、错误统计与建议采纳统计。默认关闭，需在 `config.yaml` 中设置 `admin.enabled` 与 `admin.token`。
- 新增审查发现（Finding）采纳统计：在 MR 合并/关闭时依据 GitLab discussion 的 resolved 状态与 👍/👎 表态结算，判定口径见 `docs/adr/0001-suggestion-acceptance-via-discussion-state.md`。
- 新增 SQLite 持久化层（SQLAlchemy + Alembic），记录每次 ReviewRun 与其产出，支持保留期自动清理。
- 新增 Docker 安装方式：`Dockerfile`、`docker-compose.yml` 与 `docker-entrypoint.sh`，macOS launchd 安装方式继续保留。
- 新增 `CONTEXT.md` 领域术语表与 `docs/adr/` 架构决策记录。

### Changed
- **破坏性变更**：`/manual-review` 改为异步执行，返回 `202` 与 `run_uid`，不再同步返回审查结果。审查进度改由 `/admin` 或 `/api/admin/runs/<run_uid>` 查看。
- 审查执行逻辑从 `review_server.py` 抽出到 `backend/review/runner.py`，webhook 与手动触发合并为同一执行路径。
- gunicorn worker 数由 `CPU 核数 × 2 + 1` 降为 `2`：本服务 IO bound 且几乎无 QPS，过多 worker 只会放大 SQLite 锁竞争。
- 术语统一：AI 产出的每一条统称「审查发现 / Finding」，「建议」一词只保留给严重度最低档与修复方案字段。

## [0.3.0] - 2026-06-02

### Added
- 新增标准 skill bundle 支持，识别 `skills/<name>/SKILL.md`，并兼容旧版 `skills/<name>.md`。
- 新增 skill `references/` 与 `assets/` 资源注入能力，将参考资料和资源清单合入审查提示词。
- 新增 skill `scripts/` 执行能力，可通过 JSON 审查上下文为模型补充动态分析结果。
- 新增 `requirements.txt` 固定依赖版本，安装脚本改为基于依赖清单安装。

### Changed
- 调整默认 skill 目录为 `skills`，并将内置 skill 迁移为目录式 bundle。
- 调整默认服务端口为 `9034`，安装脚本、示例配置、快速测试和 README 同步更新。
- 调整审查提示词输出约束，要求标题、影响、描述、建议等字段更短。

### Fixed
- 修复安装生成的启动脚本无法读取配置端口的问题，启动时会优先使用环境变量或 `config.yaml`。

## [0.2.1] - 2026-04-28

### Fixed
- 修复 MR merge commit 更新误触发审查的问题，合并同步产生的 update 事件将直接跳过。
- 修复已关闭或已合并 MR 的 update 事件仍可能触发审查评论的问题，仅 opened 状态参与审查。

## [0.2.0] - 2026-04-27

### Added
- 新增 GitLab 仓库文件元信息采集，将文件大小、变更类型等结构化信息注入整体与文件级审查提示词。
- 新增资产/二进制文件审查 skill，支持图片体积阈值、资源格式、加载成本与引用一致性检查。
- 新增二进制/非文本文件审查输入占位，避免空文本 diff 的资源文件被文件级审查跳过。
- 新增 GitLab 文件级 discussion 发布能力，支持无有效行号或二进制文件问题落到文件级位置。
- 新增行内评论失败后的按文件分组降级评论，避免问题因定位失败而丢失。
- 新增 health check 版本字段，便于运行时确认 OpenCR 服务版本。

### Changed
- 调整文件级问题解析，支持 `line=0` 的文件级问题与非结构化审查输出兜底。
- 调整明确通过结果识别，避免“未发现问题但建议整改”的矛盾输出被错误吞掉。
- 更新通用审查规则，约束“描述”字段保持短句并聚焦问题本身。

## [0.1.0] - 2026-04-22

### Added
- 新增基于 `skills/review/*.md` 元数据的 skill 路由与自动匹配能力。
- 新增 skill 两阶段按需加载流程：先基于 meta 预览筛选，再按需加载候选 skill 完整正文做最终选择。
- 新增 MR `update` 事件的增量 diff 审查（通过 GitLab compare API）。
- 新增更完善的审查日志：模式解析、skill 命中/未命中、增量文件范围等。
- 新增行内评论行号偏移策略：优先 `line + 1`，失败时回退原始行。

### Changed
- 将原先集中在入口文件的审查逻辑拆分为模块化结构，review 相关模块迁移至 `src/review/`，通用工具迁移至 `src/utils/`。
- 调整 MR 触发策略：`open` 触发整体 + 文件级审查，`update` 仅触发文件级（增量）审查，`reopen` 忽略不触发审查。
- 调整提示词策略：仅在命中且成功加载 skill 正文时才执行审查；未命中或 skill 正文为空时，直接跳过对应审查分支。
- 调整 skill 命中策略：取消命中数量限制，支持返回一个或多个命中 skill。
- 统一 `skills/review` 的元数据为最小模板（`name`、`description`）。
- 优化审查输出：移除整体审查评论标题包装，文件级问题标题统一为无编号格式，非整体模式下不再发布 MR 总结评论。

### Fixed
- 修复文件级审查误覆盖整 MR 的问题，确保 `update` 仅审查增量改动。
- 修复安装后运行目录缺失 `skills/` 的问题，`install.sh` 增加技能文件拷贝。
- 修复入口文件导入分支噪音，抽离脚本/包模式导入逻辑。

### Docs
- 更新 README 与 README-zh，补充 webhook action 触发行为、skill 匹配与未命中行为、`skills/` 目录安装与部署说明。

# Changelog

本文件记录项目的重要变更。

## [0.4.0] - 2026-09-07

### Added

- 新增 Vue 3 后台管理面板 `/admin`（基于 [v3-admin-vite](https://github.com/un-pany/v3-admin-vite) + Element Plus + ECharts 6），包含控制台、审查记录、审查发现、统计分析、Skill 管理与系统设置。通过 `admin.enabled` 开启，使用 `admin.username` 与 `admin.password` 登录。
- 新增管理员密码哈希存储：首次启动时将配置中的明文密码替换为 scrypt 哈希；配置文件不可写时保留运行并记录警告。
- 新增游客浏览开关（默认开启）：未登录者可查看运行状态与聚合统计，但看不到审查发现正文；开关可在后台修改并立即生效。判定边界见 `docs/adr/0002-guest-read-scope.md`。
- 新增 SQLite 持久化层（SQLAlchemy + Alembic），记录每次 ReviewRun、进度与审查发现，支持保留期自动清理。多 worker 场景下先设置 `busy_timeout` 再切换 WAL，切换失败仅记录警告不阻断启动。
- 新增审查发现采纳统计：在 MR 合并或关闭时依据 GitLab discussion 的 resolved 状态与 👍/👎 表态结算，同时展示覆盖率。判定口径见 `docs/adr/0001-suggestion-acceptance-via-discussion-state.md`。
- 新增跨审查运行的审查发现列表，支持按项目、采纳结论、严重度与时间窗筛选；支持按批次查看和筛选同一 MR 的历史发现。
- 新增 Skill 管理页，展示已加载的技能及近 30 天命中次数。overall、file、hybrid 三种模式均记录命中，同一技能在一次 ReviewRun 中只计一次；后续模型调用失败仍保留已发生的命中。
- 新增 Aurora、Graphite 皮肤，图表随当前主题切换；采纳结论使用不同颜色与图标区分。
- 新增按代码平台展示合并请求名称与跳转链接的能力；链接展示不代表已接入该平台的审查流程。
- 新增 `/api/admin/*` 接口，提供登录、鉴权、审查记录、审查发现、统计、技能与设置访问。
- 新增 Docker 安装方式，保留 macOS launchd 安装方式。
- 新增领域术语表 `CONTEXT.md` 与架构决策记录 `docs/adr/`。

### Changed

- **破坏性变更**：`/manual-review` 改为异步执行，返回 `202` 与 `run_uid`；审查进度通过后台或 `/api/admin/runs/<run_uid>` 查看。
- 后端包由 `src/` 调整为 `backend/`；审查执行逻辑集中到 `backend/review/runner.py`，webhook 与手动触发共用同一执行路径。
- 前端源码位于 `web/`，构建产物 `backend/admin/static/` 不提交进 Git。Docker 使用多阶段构建，Node 不进入运行镜像；launchd 安装时编译前端，缺少构建工具时跳过并提示。
- Docker 中的 `config.yaml` 使用可写挂载，以便首次启动时写回密码哈希。
- gunicorn worker 数由 `CPU 核数 × 2 + 1` 调整为 `2`，减少 SQLite 锁竞争。
- 统一领域用语：AI 产出称为「审查发现 / Finding」，「建议」仅用于最低严重度与修复方案字段。
- 测试优先使用已安装的真实依赖，仅在缺库时使用桩；测试配置与数据库独立于开发环境。

### Fixed

- 修复技能匹配调用失败被吞掉的问题：审查运行会标记失败，并向 MR 发布失败说明。
- 修复将未超限的明确通过结论误判为审查发现的问题。

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

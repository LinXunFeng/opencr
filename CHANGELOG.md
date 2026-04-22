# Changelog

本文件记录项目的重要变更。

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

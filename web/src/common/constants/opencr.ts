/**
 * 展示用的枚举文案与配色。
 *
 * 口径解释（采纳率是近似值、Guest 看不到正文等）刻意集中在这里，
 * 避免同一套说法散落到多个页面后各自漂移——上一版 Jinja 后台就吃过这个亏。
 */

export const RUN_STATUS_LABEL: Record<string, string> = {
  running: "进行中",
  succeeded: "成功",
  failed: "失败",
  skipped: "跳过"
}

/** Element Plus 的 tag/type 取值。用联合类型而非 string，否则模板里传值会被 TS 拒绝 */
export type TagType = "primary" | "success" | "warning" | "info" | "danger"

export const RUN_STATUS_TAG: Record<string, TagType> = {
  running: "primary",
  succeeded: "success",
  failed: "danger",
  skipped: "info"
}

export const PHASE_LABEL: Record<string, string> = {
  fetching: "拉取变更",
  matching_skill: "匹配 Skill",
  reviewing: "调用模型",
  publishing: "发布评论",
  done: "完成"
}

export const TRIGGER_LABEL: Record<string, string> = {
  webhook_open: "MR 创建",
  webhook_update: "MR 更新",
  manual: "手动触发"
}

export const VERDICT_LABEL: Record<string, string> = {
  accepted: "已采纳",
  rejected: "未采纳",
  dismissed: "已关闭未改",
  ignored: "未处理",
  undecided: "待结算",
  untrackable: "不可追踪"
}

export const VERDICT_COLOR: Record<string, string> = {
  accepted: "#16a34a",
  rejected: "#dc2626",
  dismissed: "#d97706",
  ignored: "#9ca3af",
  undecided: "#2563eb",
  untrackable: "#d1d5db"
}

export const VERDICT_ORDER = [
  "accepted",
  "rejected",
  "dismissed",
  "ignored",
  "undecided",
  "untrackable"
]

export const SEVERITY_LABEL: Record<string, string> = {
  critical: "严重",
  warning: "警告",
  advice: "建议",
  unknown: "未知"
}

export const SEVERITY_TAG: Record<string, TagType> = {
  critical: "danger",
  warning: "warning",
  advice: "success",
  unknown: "info"
}

export const DELIVERY_LABEL: Record<string, string> = {
  inline: "行内评论",
  file_level: "文件级评论",
  fallback_note: "降级为普通评论",
  summary_only: "仅整体评论"
}

export const DEGRADATION_LABEL: Record<string, string> = {
  inline_post_failed: "行内评论投递失败（已降级）",
  diff_truncated: "diff 被截断",
  skill_script_failed: "Skill 脚本执行失败"
}

export const ERROR_KIND_LABEL: Record<string, string> = {
  review_error: "审查错误",
  unexpected: "未捕获异常"
}

/** 采纳率口径说明。改判定逻辑时这段也要同步改，否则面板会撒谎。 */
export const ACCEPTANCE_NOTE
  = "采纳率的分母是**已结算**的审查发现（MR 已合并或关闭）。判定口径为「有 👍 表态，或对应 discussion 已 resolved」，本版本不做代码改动验证，因此该数字是近似值。"

/** 覆盖率口径说明 */
export const COVERAGE_NOTE
  = "覆盖率 = 可追踪 / 全部产出。整体评论中的发现、以及行内投递失败降级成普通评论的发现，走的是 GitLab 不可 resolve 的普通评论，无法结算，因此不计入采纳率分母。"

/** Stale 口径说明 */
export const STALE_NOTE
  = "服务是多进程运行的，任何进程都无法断言其他进程的审查已经死了。因此心跳超时只会标注为「疑似中断」，不会改写成失败——它也可能只是卡在一次特别慢的模型调用上。"

export function formatTime(iso?: string): string {
  if (!iso) return "-"
  const normalized = iso.endsWith("Z") ? iso : `${iso}Z`
  const date = new Date(normalized)
  if (Number.isNaN(date.getTime())) return iso
  return date.toLocaleString("zh-CN", { hour12: false })
}

export function formatPercent(value: number | null | undefined): string {
  // null 表示没有样本。显示 0% 会被误读成"一条都没被采纳"，因此区分开。
  if (value === null || value === undefined) return "—"
  return `${(value * 100).toFixed(1)}%`
}

/** 历史审查发现的展示范围与去重口径。 */
export const HISTORY_NOTE = "展示当前合并请求在保留期内的全部审查批次，按时间倒序排列。历史发现可能针对不同提交并存在重复，不代表当前问题清单；筛选仅影响批次内的发现。"

// ---------------------------------------------------------------------------
// 定期巡检
// ---------------------------------------------------------------------------

export const SURVEY_PHASE_LABEL: Record<string, string> = {
  fetching: "拉取仓库",
  profiling: "生成画像",
  matching_skill: "匹配 Skill",
  integrating: "跨仓库整合",
  inspecting: "读代码取证",
  summarizing: "汇总结论",
  done: "完成"
}

export const SURVEY_TRIGGER_LABEL: Record<string, string> = {
  schedule: "定时触发",
  manual: "手动触发"
}

export const SCHEDULE_KIND_LABEL: Record<string, string> = {
  daily: "每天",
  weekly: "每周",
  monthly: "每月",
  cron: "自定义 cron"
}

/** 索引 0 对应星期一，与 schedule_expr 里 1..7 的取值一一对应 */
export const WEEKDAY_OPTIONS = [
  { value: 1, label: "周一" },
  { value: 2, label: "周二" },
  { value: 3, label: "周三" },
  { value: 4, label: "周四" },
  { value: 5, label: "周五" },
  { value: 6, label: "周六" },
  { value: 7, label: "周日" }
]

export const CATEGORY_LABEL: Record<string, string> = {
  correctness: "正确性",
  security: "安全",
  cross_repo: "跨仓库不一致",
  architecture: "架构与耦合",
  performance: "性能",
  maintainability: "可维护性",
  dependency: "依赖",
  convention: "规范与风格"
}

export const FINDING_STATE_LABEL: Record<string, string> = {
  new: "新增",
  persisted: "仍存在",
  resolved: "已消失"
}

export const FINDING_STATE_TAG: Record<string, TagType> = {
  new: "danger",
  persisted: "warning",
  resolved: "success"
}

export const SURVEY_REPO_STATUS_LABEL: Record<string, string> = {
  ok: "正常",
  fetch_failed: "拉取失败",
  index_failed: "索引失败"
}

export const PROFILE_KIND_LABEL: Record<string, string> = {
  codegraph: "结构图（含接口与类型）",
  manifest: "依赖清单级（退化）"
}

/**
 * 巡检降级的解释文案。
 *
 * 与 MR 审查的降级一样：降级**不代表运行失败**，而是「跑完了，但产出质量受损」。
 * 这些说法集中放在这里，避免在多个页面各写一遍后逐渐漂移。
 */
export const SURVEY_DEGRADATION_LABEL: Record<string, string> = {
  repo_fetch_failed: "仓库拉取失败（该仓库未参与本次分析）",
  index_failed: "代码索引失败（该仓库画像退化为依赖清单级）",
  budget_exhausted: "预算耗尽提前收工（部分关注点未取证）",
  profile_fallback: "codegraph 不可用（全部画像退化为依赖清单级）"
}

/** 口径解释，集中放置避免各页面漂移 */
export const SURVEY_NOTES = {
  stateDiff:
    "巡检每次分析的都是全量代码，因此逐轮产出高度重合。报告默认按「新增」优先呈现——只看新增才是有效的信噪比。",
  resolvedNotStored:
    "「已消失」指上一次巡检有、本次没有检出的问题，它由指纹比对算出，库里没有对应记录。",
  guestScope:
    "游客能看到巡检的运行状态与聚合统计，但看不到发现正文与整体结论——巡检正文描述的是整个代码库的架构与弱点。",
  candidatePool:
    "勾选决定的是**候选池**：勾中的 skill 才有资格参与，但仍要由 AI 按仓库画像匹配，未匹配到的不会执行。",
  workspaceKept:
    "删除巡检不会连带删除本地工作区——那可能是几十 GB 代码，且删除不可逆。工作区清理是单独的动作。"
}

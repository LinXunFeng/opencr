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

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

/** Reach 里画像中出现文件的来源 */
export const REACH_SOURCE_LABEL: Record<string, string> = {
  routes: "路由",
  types: "类型骨架",
  api_calls: "接口调用",
  manifests: "依赖清单",
  root_files: "根目录文件"
}

/** Reach 里撞上的抽取上限 */
export const REACH_CAP_LABEL: Record<string, string> = {
  routes: "路由条数达到上限",
  types: "类型骨架条数达到上限",
  api_calls: "接口调用条数达到上限"
}

export const PROFILE_KIND_LABEL: Record<string, string> = {
  codegraph: "结构图（含接口与类型）",
  manifest: "依赖清单级（退化）"
}

export const INDEX_MODE_LABEL: Record<string, string> = {
  sync: "增量更新",
  init: "全量重建",
  failed: "失败"
}

export const INDEX_MODE_TAG: Record<string, TagType> = {
  sync: "success",
  init: "primary",
  failed: "danger"
}

/** 线索来源：这个位置是画像的哪一部分指给模型的 */
export const CLUE_SOURCE_LABEL: Record<string, string> = {
  codegraph: "codegraph",
  baseline: "基础画像",
  unlisted: "画像外",
  unknown: "无记录"
}

export const CLUE_SOURCE_TAG: Record<string, TagType> = {
  codegraph: "success",
  baseline: "info",
  unlisted: "warning",
  unknown: "info"
}

/** 线索来源计数的全部键，unknown 放最后：它只在有旧数据时才有值 */
export const CLUE_KEYS = ["codegraph", "baseline", "unlisted", "unknown"] as const

/** 一组线索来源计数的总数，即该范围内的全部关注点或发现 */
export function sumClueCounts(counts?: Partial<Record<typeof CLUE_KEYS[number], number>> | null) {
  return CLUE_KEYS.reduce((sum, key) => sum + (counts?.[key] ?? 0), 0)
}

/** codegraph 不可用时的说明。两种原因都会让画像退化，但只有「配置关闭」能拿来做对照 */
export const CODEGRAPH_UNAVAILABLE_LABEL: Record<string, string> = {
  disabled: "本轮未启用 codegraph（配置关闭），画像均为依赖清单级。可以与启用时的运行对比下面的数字，看 codegraph 带来的差别。",
  missing: "codegraph 已启用但找不到可执行文件，画像均退化为依赖清单级——这是部署故障，不是有意关闭，不要拿这一轮做对照。"
}

export const CLUE_SOURCE_DESC: Record<string, string> = {
  codegraph: "只有 codegraph 抽出的接口、处理函数或类型里出现过这个文件",
  baseline: "不靠 codegraph 也能看到：接口调用侧、依赖清单或仓库顶层文件",
  unlisted: "画像里没出现过这个文件，是模型从目录结构推断出来的",
  unknown: "该功能上线前产出的发现，没有记录线索来源"
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
  profile_fallback: "codegraph 不可用（全部画像退化为依赖清单级）",
  repos_truncated: "仓库数超过单次巡检上限（超出的仓库未参与本次分析）"
}

export const PUSH_STATUS_LABEL: Record<string, string> = {
  running: "推送中",
  succeeded: "成功",
  failed: "失败"
}

export const PUSH_STATUS_TAG: Record<string, TagType> = {
  running: "primary",
  succeeded: "success",
  failed: "danger"
}

export const PUSH_TRIGGER_LABEL: Record<string, string> = {
  auto: "运行结束自动推送",
  manual: "手动推送"
}

/** 口径解释，集中放置避免各页面漂移 */
export const SURVEY_NOTES = {
  stateDiff:
    "巡检每轮从全量代码的结构画像中挑选一批文件读源码取证，并复核台账里仍存在的问题所在的文件——不是每个文件每轮都会被读。台账里的老问题每轮都会被复核，因此逐轮产出高度重合，报告默认按「新增」优先呈现。",
  resolvedNotStored:
    "「已消失」指上一次巡检有、本次取证过所在文件但没有再检出的问题，它由指纹比对算出，库里没有对应记录。没再检出不等于已修复。",
  reach:
    "L1 可达：整合分析能点名取证的源码文件占比。L1 只能从画像里带完整路径的文件中挑选——路由、类型骨架、接口调用、依赖清单与根目录文件；只有函数的文件、配置与脚本，不管放在多深的目录都看不到。画像超出 L1 预算被截断时，截掉部分里的文件同样看不到。台账里已有问题的文件不受影响，复核直接按台账路径取证。",
  reachHeavy: "符号数来自 codegraph 索引，符号多的文件多半是核心逻辑，最值得关心它们为什么进不了 L1 的视野。",
  reachSourceOverlap: "一个文件可同时属于多个来源",
  reachNoRecord:
    "没有统计记录：仓库拉取失败、运行早于该功能，或统计本身失败。",
  focusCapIncludesRecheck:
    "关注点上限同时也是每轮复核台账问题的文件数上限（另算），一轮最多取证两倍于此的文件。",
  uncheckedNotResolved:
    "这些问题上一次巡检出现过、本次没有再报出，但所在文件本次没有得出可信结论（没轮到取证、文件超出读取上限只看了片段、源码读取或模型输出失败），既不能算仍存在，也不能算已消失。",
  guestScope:
    "游客能看到巡检的运行状态与聚合统计，但看不到发现正文与整体结论——巡检正文描述的是整个代码库的架构与弱点。",
  candidatePool:
    "勾选决定的是**候选池**：勾中的 skill 才有资格参与，但仍要由 AI 按仓库画像匹配，未匹配到的不会执行。",
  destinationScope:
    "推送的是该巡检的问题台账：一个指纹一行，每轮只更新系统列，你在表里加的列（负责人、处理进度等）不会被读取或改写。推送即交出了发现正文的可见性控制——谁能看到正文由目标平台的共享设置决定。",
  ledgerUnseen:
    "台账里的「本轮未发现」只说明这一轮取证过所在文件、模型没有再报出它，不说明问题已修复。每轮都会优先复核仍为「存在」的问题；没有得出可信结论的文件（没轮到复核、文件超出读取上限只看了片段、源码读取或模型输出失败）保持原状态，不会被标记。",
  pushOnlyLatest:
    "推送的内容永远是台账的当前状态，因此只能以该巡检最近一次成功的运行发起推送。推送失败不影响运行本身的状态。",
  clueSource:
    "线索来源只说明这个位置是画像的哪一部分指给模型的，不是因果归因：没有 codegraph 时，模型也可能凭目录名猜到同一个文件。两边都能看到的位置记为基础画像，宁可低估 codegraph 的收益也不高估。要量化因果，请对同一批提交分别开关 codegraph 各跑几轮比对。",
  crossRepoNeedsRoutes:
    "跨仓库连接、方法不一致、无人调用的接口都以 codegraph 抽出的接口为一端，未启用时这三项必然为 0；此时调用侧的路径只能全部算作「范围内无人提供」。",
  droppedRegistrations:
    "调用侧扫描会把路由注册那一行也当成调用，codegraph 抽出接口后才能剔除，否则每个后端都像在调用自己。",
  runListCodegraph:
    "发现：线索来自 codegraph 的条数 / 全部；连接：跨仓库接口连接数。",
  emptyExtraction:
    "建成了索引，但没有抽出任何接口与类型：可能是 codegraph 不支持该仓库的语言，也可能仓库里本来就没有。",
  workspaceKept:
    "删除巡检不会连带删除本地工作区——那可能是几十 GB 代码，且删除不可逆。工作区清理是单独的动作。"
}

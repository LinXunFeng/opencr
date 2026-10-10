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
  webhook_open: "合并请求创建",
  webhook_update: "合并请求更新",
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
  = "采纳率的分母是**已结算**的审查发现（合并请求已合并或关闭）。判定口径为「有 👍 表态，或对应 discussion 已 resolved」，本版本不做代码改动验证，因此该数字是近似值。"

/** 覆盖率口径说明 */
export const COVERAGE_NOTE
  = "覆盖率 = 可追踪 / 全部产出。整体评论中的发现、以及行内投递失败降级成普通评论的发现，走的是代码平台上不可 resolve 的普通评论，无法结算，因此不计入采纳率分母。"

/** Stale 口径说明 */
export const STALE_NOTE
  = "服务是多进程运行的，任何进程都无法断言其他进程的审查已经死了。因此心跳超时只会标注为「疑似中断」，不会改写成失败——它也可能只是卡在一次特别慢的模型调用上。"

/**
 * 解析服务端时间字符串。
 * 后端存的是 naive UTC，isoformat() 不带时区后缀；直接 new Date() 会被浏览器当成本地时间，
 * 在东八区少 8 小时，拿它和 Date.now() 相减算耗时又会多出 8 小时。因此凡是服务端时间都必须走这里。
 */
export function parseServerTime(iso: string): Date {
  return new Date(/(?:Z|[+-]\d{2}:\d{2})$/.test(iso) ? iso : `${iso}Z`)
}

/** 把服务端时间格式化成本地时间；空值返回「—」，解析失败原样返回 */
export function formatTime(iso?: string): string {
  if (!iso) return "—"
  const date = parseServerTime(iso)
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
  index_failed: "索引失败",
  ignored: "已忽略",
  truncated: "超出上限"
}

/** 已忽略是用户的选择、超出上限是配置问题，都不是故障，不能和拉取失败一样标红 */
export const SURVEY_REPO_STATUS_TAG: Record<string, TagType> = {
  ok: "success",
  ignored: "info",
  truncated: "warning"
}

/** 没有参与分析的仓库状态：运行记录里仍列出（看得出少了谁），但不算「覆盖的仓库」 */
export const SURVEY_REPO_SKIPPED_STATUSES = ["ignored", "truncated"]

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
  recheck: "台账复核",
  unknown: "无记录"
}

export const CLUE_SOURCE_TAG: Record<string, TagType> = {
  codegraph: "success",
  baseline: "info",
  unlisted: "warning",
  recheck: "primary",
  unknown: "info"
}

/** 线索来源计数的全部键，unknown 放最后：它只在有旧数据时才有值 */
export const CLUE_KEYS = ["codegraph", "baseline", "unlisted", "recheck", "unknown"] as const

/** 位置由本轮画像指给 L1 的三类：codegraph 占比只在这个范围里算 */
const NAMED_CLUE_KEYS = ["codegraph", "baseline", "unlisted"] as const

/**
 * L1 本轮点名的条目数，即 codegraph 占比的分母。
 * 台账复核与旧数据不算进来：复核的位置来自上一轮的发现，算进分母会让台账越大、codegraph 占比越被稀释
 */
export function namedClueTotal(counts?: Partial<Record<typeof CLUE_KEYS[number], number>> | null) {
  return NAMED_CLUE_KEYS.reduce((sum, key) => sum + (counts?.[key] ?? 0), 0)
}

/** 一次运行里 codegraph 的状态（codegraph_stats.status） */
export const CODEGRAPH_STATUS_LABEL: Record<string, string> = {
  enabled: "本轮已启用",
  disabled: "本轮未启用",
  missing: "找不到可执行文件"
}

export const CODEGRAPH_STATUS_TAG: Record<string, TagType> = {
  enabled: "success",
  disabled: "info",
  missing: "danger"
}

/**
 * codegraph 不可用时的说明。两种原因都会让画像退化，但只有「配置关闭」能拿来做对照。
 * 与 backend/survey/report.py 的 CODEGRAPH_UNAVAILABLE_LABELS 逐字一致，改一边要同步另一边。
 */
export const CODEGRAPH_UNAVAILABLE_LABEL: Record<string, string> = {
  disabled: "本轮未启用 codegraph（配置关闭），画像均为依赖清单级。可以与启用时的运行对比这里的数字，看 codegraph 带来的差别。",
  missing: "codegraph 已启用但找不到可执行文件，画像均退化为依赖清单级——这是部署故障，不是有意关闭，不要拿这一轮做对照。"
}

export const CLUE_SOURCE_DESC: Record<string, string> = {
  codegraph: "只有 codegraph 抽出的接口、处理函数或类型里出现过这个文件",
  baseline: "不靠 codegraph 也能看到：接口调用侧、依赖清单或仓库顶层文件",
  unlisted: "画像里没出现过这个文件，是模型从目录结构推断出来的",
  recheck: "本轮 L1 没有点名这个文件，是因为台账里它还有「存在」的问题才被复核",
  unknown: "没有记录线索来源：该功能上线前产出的发现，或没有走到标注这一步的关注点"
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

/** 忽略理由的长度上限，与服务端 SURVEY_IGNORE_NOTE_MAX 一致；超出的部分服务端会静默截掉，所以前端先拦 */
export const IGNORE_NOTE_MAX = 2000

/** 一次最多加入多少个已忽略仓库，与服务端的上限（单次巡检仓库上限的最大值 MAX_REPOS_UPPER_BOUND）一致 */
export const IGNORE_REPO_BATCH_MAX = 1000

/** ElMessageBox.prompt 的忽略理由校验器，标记与编辑两处共用 */
export function validateIgnoreNote(value: string): boolean | string {
  return (value || "").length <= IGNORE_NOTE_MAX || `理由不能超过 ${IGNORE_NOTE_MAX} 字`
}

export const PUSH_TRIGGER_LABEL: Record<string, string> = {
  auto: "运行结束自动推送",
  manual: "手动推送"
}

/**
 * 嵌进中文句子里的平台名：英文名两侧补空格（「访问 GitLab 数次」），中文的「代码平台」不补，
 * 否则会出现「访问 代码平台 数次」。平台名来自服务端按 code_platform.type 给出的展示名
 */
export function spacedPlatform(name: string): string {
  return /^[\x20-\x7E]+$/.test(name) ? ` ${name} ` : name
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
    "线索来源只说明这个位置是画像的哪一部分指给模型的，不是因果归因：没有 codegraph 时，模型也可能凭目录名猜到同一个文件。两边都能看到的位置记为基础画像，宁可低估 codegraph 的收益也不高估。台账复核的文件不是画像指出来的，单独计数，也不算进 codegraph 占比的分母。要量化因果，请对同一批提交分别开关 codegraph 各跑几轮比对。",
  crossRepoNeedsRoutes:
    "跨仓库连接、方法不一致、无人调用的接口都以 codegraph 抽出的接口为一端，未启用时这三项必然为 0；此时调用侧的路径只能全部算作「范围内无人提供」。",
  focusClueRow: "关注点（L1 点名与台账复核）",
  findingClueRow: "发现（L2 取证后入库）",
  indexFailedFallback: "建索引失败",
  codegraphNoRecord:
    "这次运行没有 codegraph 记录（该功能上线前的运行，或运行在建画像之前就已结束）。",
  focusNotReached:
    "本轮未走到整合分析这一步",
  droppedRegistrations:
    "调用侧扫描会把路由注册那一行也当成调用，codegraph 抽出接口后才能剔除，否则每个后端都像在调用自己。",
  runListCodegraph:
    "发现：线索来自 codegraph 的条数 / L1 点名产出的发现（不含台账复核）；连接：跨仓库接口连接数。",
  emptyExtraction:
    "建成了索引，但没有抽出任何接口与类型：可能是 codegraph 不支持该仓库的语言，也可能仓库里本来就没有。",
  ignoreScope:
    "只忽略这一个问题：之后的巡检取证这个文件时，由模型判断新报出的问题是不是同一个（行号挪动、换了说法也算），是就隐藏，同一文件里的其他问题照常报告。模型可能认错，可在忽略清单里核对最近一次隐藏的是哪条。",
  ignoreRemove:
    "取消忽略后，被它隐藏的条目（含历史报告里的）恢复显示；如果问题还在，下一轮巡检起也会照常报告。台账里对应的行若已是「已忽略」，会停在那里，等后续巡检复核出结论后才会变为「存在」或「本轮未发现」。",
  ignoreHidesReports:
    "被隐藏的条目只是不显示，取消忽略即恢复。",
  ignoredHidden: (count: number) =>
    `本次运行另有 ${count} 条发现已标记为不再提醒，未列出，不计入以上各项。`,
  ignoreLastMatch:
    "最近一次被这条忽略隐藏的发现：标记时被点的那条，或之后的巡检里模型认出的同一个问题。如果这里显示的明显是另一个问题，说明模型认错了，请取消这条忽略后重新标记。",
  ignoreInactive:
    "旧版本按「文件 + 类别」整体忽略，升级时找不到这条忽略对应的具体问题（相关运行已被清理），它已不再生效，可以直接取消。",
  ignoreRepoScope:
    "已忽略的仓库之后不再拉取、不再分析，也不占单次巡检的仓库上限；不论它是手填的还是从组织里展开出来的。候选列表默认取自最近一次巡检的仓库清单（含超出上限被截掉的），可以在添加时按当前配置刷新，也可以直接填仓库地址或「组织/仓库」路径。台账里这个仓库的行转为「已忽略」，已有报告不受影响。要按规则挡掉一类仓库（例如路径里带 archive 的），用组织来源上的排除模式。",
  ignoreRepoRemove:
    "取消后，下一轮巡检起这个仓库重新参与。它在台账里的行会停在「已忽略」，等后续巡检复核出结论后才会变为「存在」或「本轮未发现」。",
  candidatesFromRun: (time: string, platform: string) =>
    `候选取自 ${time} 那次巡检的仓库清单，打开时不访问${spacedPlatform(platform)}。之后改过巡检的来源、或组织里增删过仓库的话，这份清单已经过时，可以按当前配置刷新。`,
  candidatesNoRun:
    "这个巡检还没有记录过仓库清单的运行（没跑过，或每次都在展开仓库之前就失败了），没有现成的候选。可以按当前配置刷新，或直接填写仓库地址。",
  candidatesLive: (time: string) =>
    `候选于 ${time} 按当前巡检配置获取，与下一轮巡检展开出的仓库一致（含超出上限、下一轮会被截掉的）。这份清单只用于挑选，不会保存，也不影响巡检。`,
  candidatesRefreshTip: (platform: string) =>
    `按当前巡检配置实时展开来源：每个组织要请求${spacedPlatform(platform)}数次，组织与仓库多时需要几秒到几十秒；组织展开目前只支持 GitLab 的 group。只读取仓库清单，不拉取代码，也不触发巡检。`,
  candidatesRefreshTimeout: (platform: string) =>
    `获取仓库清单超时（等待超过 2 分钟），可能是组织太多或${spacedPlatform(platform)}响应慢。原有候选保持不变，也可以直接填写仓库地址。`,
  candidatesRefreshFailed: (count: number) =>
    `有 ${count} 个组织展开失败，它们下面的仓库不在候选里：`,
  ignoreRepoBatchSelect:
    "按关键字选中所有匹配的候选：匹配仓库地址或仓库标识里的任意片段，不区分大小写，可多次叠加。只选当前候选清单里的仓库，手填的照常保留；不在候选里的仓库可以用「批量粘贴」。",
  ignoreRepoPastePlaceholder:
    "每行一个仓库地址或「组织/仓库」路径，也可以用逗号、空格分隔。加入已选后可以在上面的选择框里核对，无法识别的地址会在提交时一次列出。",
  ignoreRepoAdded: (created: number, existing: number) =>
    `已加入 ${created} 个仓库${existing ? `，另有 ${existing} 个原本就在清单里` : ""}，下一轮巡检起生效`,
  ignoreRepoNotePrompt:
    "为什么不再巡检这个仓库？半年后这里是唯一的线索。",
  ignoreRepoNotePlaceholder:
    "例如：已停止维护，代码迁到了新仓库",
  repoSkipped:
    "这个仓库本轮没有参与分析：它在忽略清单里，或超出了单次巡检的仓库上限。没有拉取，也就没有统计。",
  skippedRepos: (count: number) =>
    `另有 ${count} 个仓库未参与（在忽略清单里，或超出单次巡检的仓库上限），见状态列`,
  workspaceKept:
    "删除巡检不会连带删除本地工作区——那可能是几十 GB 代码，且删除不可逆。工作区清理是单独的动作。"
}

import { request } from "@/http/axios"

/** 当前访问身份。Guest 是身份的缺席，不是一种账号。 */
export type Identity = "admin" | "guest"

export interface MeInfo {
  identity: Identity
  username: string
  guest_read: boolean
  /** 巡检模块是否对 Guest 开放。嵌套在 guest_read 之下，两者都开才生效 */
  survey_guest_read: boolean
  /** 巡检调度是否启用（config.yaml 决定），关闭时菜单仍在但不会自动触发 */
  survey_enabled: boolean
  /** Guest 浏览关闭且未登录时为 true，前端应直接跳登录页 */
  requires_login: boolean
  version: string
}

export interface Degradation { kind: string, count: number }

export interface ReviewRun {
  run_uid: string
  project_id: number
  project_path: string
  mr_iid: number
  mr_title: string
  /** 平台对应的名称（MR / PR）与网页地址；未知平台不生成链接。 */
  change_label?: string
  change_url?: string
  trigger: string
  review_mode: string
  review_skills: string[]
  status: string
  skip_reason: string
  phase: string
  files_total: number
  files_done: number
  degradations: Degradation[]
  error_kind: string
  error_message: string
  started_at: string
  heartbeat_at: string
  finished_at: string
  is_stale: boolean
  retry_of_uid: string
  retry_scope: string
  original_retry_available: boolean
}

export interface Finding {
  id: number
  run_uid?: string
  project_id?: number
  mr_iid?: number
  file_path: string
  line: number
  severity: string
  delivery: string
  discussion_id?: string
  verdict: string
  verdict_reason: string
  created_at?: string
  /** Guest 拿不到正文，字段直接不存在（服务端剔除，不是前端隐藏） */
  body?: string
}

export interface RunDetail extends ReviewRun {
  findings: Finding[]
  body_included: boolean
  can_retry?: boolean
}

export interface ErrorStats {
  window_days: number
  total_runs: number
  succeeded: number
  failed: number
  running: number
  skipped: number
  degraded_runs: number
  by_error_kind: Record<string, number>
  by_degradation: Record<string, number>
  trend?: TrendPoint[]
}

export interface VerdictStats {
  window_days: number
  total_findings: number
  trackable_findings: number
  settled_findings: number
  accepted: number
  /** 已结算样本为 0 时是 null，不是 0 —— 0% 会被误读成"一条都没被采纳" */
  acceptance_rate: number | null
  coverage_rate: number | null
  by_verdict: Record<string, number>
  by_delivery: Record<string, number>
  by_severity: Record<string, number>
  summary_only_findings: number
}

export interface TrendPoint {
  date: string
  succeeded: number
  failed: number
  skipped: number
  running: number
}

export interface ProjectItem {
  project_id: number
  project_path: string
  run_count: number
}

export interface DashboardData {
  service: { version: string, model: string, stale_after_seconds: number }
  active_runs: ReviewRun[]
  errors: ErrorStats
  verdicts: VerdictStats
  trend: TrendPoint[]
  projects: ProjectItem[]
}

export interface SettingsData {
  readonly: Record<string, any>
  writable: {
    guest_read: boolean
    guest_retry: boolean
    survey_guest_read: boolean
    /** 单次巡检的仓库上限，超出部分截掉并记一条 repos_truncated 降级 */
    survey_max_repos: number
  }
}

// ---------------------------------------------------------------------------
// 定期巡检
// ---------------------------------------------------------------------------

/** 一条仓库来源：具体仓库，或一个组织（组织在每次执行时实时展开） */
export interface SurveySource {
  id?: number
  kind: "repo" | "org"
  url: string
  /** 只对 kind=repo 有意义；留空表示用仓库的默认分支 */
  branch: string
  /** 只对 kind=org 有意义：排除模式，防止组织里新增的大仓库拖垮巡检 */
  exclude_patterns: string[]
}

/**
 * Binding：巡检选用某个输出目标（Destination），并指明写到平台上的哪个位置。
 * 凭据在 config.yaml 里，这里只有实例名。
 */
export interface SurveyBinding {
  id?: number
  destination: string
  destination_label?: string
  /** 实例已从 config.yaml 删除或配置有误时为 false；绑定保留，推送会记失败 */
  available?: boolean
  /** Guest 拿不到目标位置：表格 ID 本身就是访问入口（服务端剔除，不是前端隐藏） */
  target?: Record<string, string>
  target_desc?: string
  last_succeeded_push_at?: string
}

export interface DestinationTargetField {
  key: string
  label: string
  required: boolean
  placeholder: string
  help: string
}

/** 连通性测试的一项结论。warn 表示能用但有需要知道的事（例如工作表会在首次推送时自动创建） */
export interface DestinationCheckItem {
  title: string
  level: "ok" | "warn" | "error"
  message: string
}

export interface DestinationCheckResult {
  ok: boolean
  /** 规整后的目标位置（例如表格链接已解析成 ID） */
  target: Record<string, string>
  items: DestinationCheckItem[]
}

/** config.yaml 里配置的一个输出目标实例 */
export interface DestinationItem {
  name: string
  type: string
  type_label: string
  /** 实例配置摘要，例如鉴权方式与账号；不含凭据 */
  summary: string
  target_fields: DestinationTargetField[]
  /** 非空表示配置有误、不可用 */
  error: string
}

export interface Survey {
  survey_uid: string
  name: string
  /** 工作区目录名。与巡检一一对应，且**不随改名变化 */
  slug: string
  enabled: boolean
  schedule_kind: "daily" | "weekly" | "monthly" | "cron"
  schedule_expr: string
  timezone: string
  /** 存的是**被取消勾选**的 skill，空数组 = 全选 */
  excluded_skills: string[]
  delete_workspace_after: boolean
  budget_wall_clock_minutes: number | null
  budget_l1_max_chars: number | null
  budget_l2_max_focus: number | null
  budget_index_timeout_seconds: number | null
  retention_runs: number
  last_run_at: string
  next_run_at: string
  created_at: string
  sources: SurveySource[]
  bindings: SurveyBinding[]
  schedule_desc?: string
  workspace_bytes?: number
}

/** Finding 的线索来源计数。unknown 是功能上线前的旧数据，不并进任何一类 */
export interface ClueCounts {
  codegraph: number
  baseline: number
  unlisted: number
  /** 本轮 L1 没有点名、只因台账复核而取证的 */
  recheck: number
  /** 没有记录来源：功能上线前的发现，或缺标注的关注点（正常流程不会出现） */
  unknown?: number
}

/** codegraph 本轮的产出与去向，运行级快照 */
export interface CodegraphStats {
  /** 本轮建画像时 codegraph 的状态：enabled / disabled（配置关闭）/ missing（开着但找不到可执行文件） */
  status: "enabled" | "disabled" | "missing"
  repos_total: number
  /** 画像来自 codegraph 的仓库数 */
  repos_structured: number
  /** 建成了索引但一条接口、一个类型都没抽出来的仓库数 */
  repos_empty: number
  routes: number
  types: number
  /** 被识别为路由注册而从调用侧剔除的条数 */
  dropped_registrations: number
  cross_repo: { links: number, method_mismatch: number, orphan_calls: number, unused_routes: number }
  /** L1 还没跑完时为 null，与"没有关注点"区分 */
  focus: ClueCounts | null
}

export interface SurveyRun {
  run_uid: string
  survey_name?: string
  survey_uid?: string
  trigger: string
  status: string
  phase: string
  repos_total: number
  repos_done: number
  matched_skills: string[]
  degradations: Degradation[]
  error_kind: string
  error_message: string
  started_at: string
  heartbeat_at: string
  finished_at: string
  is_stale: boolean
  /** 功能上线前的旧运行为 null */
  codegraph_stats: CodegraphStats | null
  /** 已落库 Finding 的线索来源分布（扣除了被忽略的条目） */
  clue_counts?: ClueCounts
}

export interface SurveyFinding {
  id: number
  repo_slug: string
  file_path: string
  line: number
  category: string
  severity: string
  /** new = 本次新增；persisted = 上次也有。"已消失"不在这里，见 resolved_findings */
  state: "new" | "persisted"
  fingerprint: string
  /** codegraph / baseline / unlisted / recheck（台账复核）；旧数据为空串 */
  clue_source: string
  created_at: string
  /** Guest 拿不到标题与正文，字段直接不存在（服务端剔除，不是前端隐藏） */
  title?: string
  body?: string
  /** 只出现在「本轮未复查」里：所在仓库本轮没参与的原因（truncated / fetch_failed），其余为空串 */
  repo_status?: string
}

export interface SurveyRunRepo {
  repo_slug: string
  url: string
  branch: string
  commit_sha: string
  status: string
  profile_kind: string
  file_count: number
  /** Guest 拿到的是空串：git / codegraph 的原始输出可能带出路径与代码片段 */
  error_message: string
  /** sync / init / failed；未启用 codegraph 时为空串 */
  index_mode: string
  /** 以下三项为 null 表示没有记录，0 表示抽出了 0 条，两者不要混为一谈 */
  index_ms: number | null
  route_count: number | null
  type_count: number | null
  /** 拉取失败的仓库、早于该功能的运行、统计本身失败时为 null */
  reach: SurveyReach | null
}

/** Reach：L1 能点名取证的源码文件范围，口径见 SURVEY_NOTES.reach */
export interface SurveyReach {
  source_files: number
  listed_files: number
  reachable_files: number
  by_source: Record<string, number>
  by_depth: { depth: number, reachable: number, total: number }[]
  truncated: boolean
  profile_chars: number
  budget_chars: number
  caps: string[]
  scan_limited: boolean
  /** 以下两项列出具体目录与文件，Guest 拿不到（服务端剔除） */
  hidden_dirs?: { dir: string, hidden: number, total: number }[]
  heavy_hidden_files?: { path: string, symbols: number }[]
  /** 没有 codegraph 索引时为 null：无从判断哪些文件"重" */
  heavy_hidden_count: number | null
  /** 计入 heavy_hidden_count 的符号数阈值 */
  heavy_threshold: number
}

/** Push：一次运行的结果同步到一个输出目标的记录。失败不改变运行本身的状态 */
export interface SurveyPush {
  id: number
  /** 绑定被删除后为 null，记录保留但不能再从这里重推 */
  binding_id: number | null
  destination: string
  trigger: "auto" | "manual"
  status: "running" | "succeeded" | "failed"
  /** 进行中超过上限，大概率是进程在推送中途被终止 */
  is_stale: boolean
  stats: { updated?: number, inserted?: number, skipped?: number }
  started_at: string
  finished_at: string
  /** 以下两项 Guest 拿不到 */
  target?: Record<string, string>
  error_message?: string
}

export interface SurveyRunDetail extends SurveyRun {
  summary: string
  body_included: boolean
  repos: SurveyRunRepo[]
  findings: SurveyFinding[]
  /** 上一次有、本次没有的发现。它们没有对应的库记录，是比对算出来的 */
  resolved_findings: SurveyFinding[]
  /** 上次有、本次没出现，且本轮没取证过所在文件：状态未知，不能算已消失 */
  unchecked_findings: SurveyFinding[]
  /** 上一轮出现过、所在仓库本轮在忽略清单里而没参与的发现，不计入 unchecked */
  ignored_repo_findings: SurveyFinding[]
  /** ignored：本次运行里已入库、但当前在忽略清单里而被隐藏的条数，不含在其余各项内 */
  counts: { new: number, persisted: number, resolved: number, unchecked: number, ignored_repo: number, total: number, ignored: number }
  pushes: SurveyPush[]
  /** 是该巡检最近一次成功的运行、且配置了输出目标 */
  pushable: boolean
}

export interface SurveyStats {
  window_days: number
  total_runs: number
  succeeded: number
  failed: number
  running: number
  by_category: Record<string, number>
  by_severity: Record<string, number>
  by_state: Record<string, number>
}

export function loginApi(data: { username: string, password: string }) {
  return request<{ identity: Identity, username: string }>({ url: "login", method: "post", data })
}

export function logoutApi() {
  return request<{ identity: Identity }>({ url: "logout", method: "post" })
}

export function getMeApi() {
  return request<MeInfo>({ url: "me", method: "get" })
}

export function getDashboardApi(days = 30) {
  return request<DashboardData>({ url: "dashboard", method: "get", params: { days } })
}

export function getRunsApi(params: { limit?: number, status?: string } = {}) {
  return request<{ items: ReviewRun[] }>({ url: "runs", method: "get", params })
}

export function getRunDetailApi(runUid: string) {
  return request<RunDetail>({ url: `runs/${runUid}`, method: "get" })
}

/** 按审查批次分页查询同一合并请求的历史发现。 */
export function getChangeHistoryApi(runUid: string, params: { limit: number, offset: number, severity: string, verdict: string }) {
  return request<{ items: RunDetail[], total: number, body_included: boolean }>({
    url: `runs/${runUid}/history`,
    method: "get",
    params
  })
}

export function getFindingsApi(params: {
  limit?: number
  offset?: number
  project_id?: number
  verdict?: string
  severity?: string
  days?: number
} = {}) {
  return request<{ total: number, items: Finding[], body_included: boolean }>({
    url: "findings",
    method: "get",
    params
  })
}

export function getVerdictStatsApi(days = 30) {
  return request<VerdictStats>({ url: "stats/verdicts", method: "get", params: { days } })
}

export function getErrorStatsApi(days = 7) {
  return request<ErrorStats>({ url: "stats/errors", method: "get", params: { days } })
}

export function getSkillsApi(days = 30) {
  return request<{
    skills_dir: string
    scripts_enabled: boolean
    items: any[]
    hits: Record<string, number>
  }>({ url: "skills", method: "get", params: { days } })
}

export function getSettingsApi() {
  return request<SettingsData>({ url: "settings", method: "get" })
}

export function updateSettingsApi(data: Partial<SettingsData["writable"]>) {
  return request<{ writable: SettingsData["writable"] }>({ url: "settings", method: "patch", data })
}

/** 重新触发指定失败运行，成功后返回新的运行标识。 */
export function retryRunApi(runUid: string, scope: "latest" | "original") {
  return request<{ run_uid: string, status: string }>({
    url: `runs/${runUid}/retry`,
    method: "post",
    data: { scope }
  })
}

export function getSurveysApi() {
  return request<{ items: Survey[] }>({ url: "surveys", method: "get" })
}

export function getSurveyApi(surveyUid: string) {
  return request<Survey>({ url: `surveys/${surveyUid}`, method: "get" })
}

export function createSurveyApi(data: Partial<Survey>) {
  return request<Survey>({ url: "surveys", method: "post", data })
}

export function updateSurveyApi(surveyUid: string, data: Partial<Survey>) {
  return request<Survey>({ url: `surveys/${surveyUid}`, method: "patch", data })
}

export function deleteSurveyApi(surveyUid: string) {
  return request<{ deleted: boolean, slug: string, workspace_kept: boolean }>({
    url: `surveys/${surveyUid}`,
    method: "delete"
  })
}

/** 立即执行一次。走的是和定时触发完全相同的入口，只有 trigger 字段不同。 */
export function runSurveyApi(surveyUid: string) {
  return request<{ started: boolean }>({ url: `surveys/${surveyUid}/run`, method: "post" })
}

/** 清理工作区。下次执行会退回全量克隆，不影响任何已产出的发现。 */
export function clearSurveyWorkspaceApi(surveyUid: string) {
  return request<{ removed: boolean }>({ url: `surveys/${surveyUid}/workspace`, method: "delete" })
}

/** 忽略清单的一项：一个已忽略问题。快照字段来自标记时的那条发现 */
export interface SurveyIgnore {
  id: number
  fingerprint: string
  repo_slug: string
  file_path: string
  category: string
  line: number
  severity: string
  title: string
  note: string
  created_at: string
  /** false = 旧版本迁移过来、找不到原问题的条目，无法交给模型比对，不再生效 */
  active: boolean
  /** 仍保留的运行里被它隐藏的发现条数 */
  hidden_count: number
  /** 最近一次被它隐藏的发现；运行被清理后为 null */
  last_match: { run_uid: string, line: number, title: string, created_at: string } | null
}

export function getSurveyIgnoresApi(surveyUid: string) {
  return request<{ survey_name: string, items: SurveyIgnore[] }>({
    url: `surveys/${surveyUid}/ignores`,
    method: "get"
  })
}

export function addSurveyIgnoreApi(surveyUid: string, findingId: number, note = "") {
  return request<{ id: number }>({
    url: `surveys/${surveyUid}/ignores`,
    method: "post",
    data: { finding_id: findingId, note }
  })
}

export function updateSurveyIgnoreNoteApi(surveyUid: string, ignoreId: number, note: string) {
  return request<{ updated: boolean }>({
    url: `surveys/${surveyUid}/ignores/${ignoreId}`,
    method: "patch",
    data: { note }
  })
}

export function removeSurveyIgnoreApi(surveyUid: string, ignoreId: number) {
  return request<{ removed: boolean }>({
    url: `surveys/${surveyUid}/ignores/${ignoreId}`,
    method: "delete"
  })
}

/** 忽略清单里的一个已忽略仓库：之后的巡检不拉取、不分析它 */
export interface SurveyIgnoredRepo {
  id: number
  repo_slug: string
  /** 标记时填的仓库地址或路径，只用于展示 */
  url: string
  note: string
  created_at: string
}

/** 可供标记的候选仓库，已去掉忽略过的 */
export interface SurveyRepoCandidate {
  repo_slug: string
  url: string
}

/**
 * candidates 取自最近一次运行；candidates_run_* 说明是哪一轮，还没运行过时为空串。
 * platform_name 是按 code_platform.type 显示的平台名（GitLab / GitHub，未知平台为「代码平台」）
 */
export function getSurveyIgnoredReposApi(surveyUid: string) {
  return request<{
    items: SurveyIgnoredRepo[]
    platform_name: string
    candidates: SurveyRepoCandidate[]
    candidates_run_uid: string
    candidates_run_started_at: string
  }>({
    url: `surveys/${surveyUid}/ignored-repos`,
    method: "get"
  })
}

/** 实时展开时失败的一个来源（组织），它下面的仓库不在候选里 */
export interface SurveySourceError {
  source: string
  error: string
}

/** 按当前配置访问代码平台实时展开候选；errors 是展开失败的组织，不在 items 里 */
export function getSurveyLiveRepoCandidatesApi(surveyUid: string) {
  return request<{ items: SurveyRepoCandidate[], errors: SurveySourceError[] }>({
    url: `surveys/${surveyUid}/ignored-repos/live-candidates`,
    method: "get",
    // 组织多、仓库多时要翻好几页，默认的 30 秒不够。不追平服务端（gunicorn 300 秒，代码平台单次请求 30 秒）的
    // 最坏情况：让管理员对着转圈等五分钟不如早点告诉他超时、改为手填。超时文案见 SURVEY_NOTES.candidatesRefreshTimeout
    timeout: 120000
  })
}

/** 一次加入的仓库之一；created 为 false 表示它原本就在清单里 */
export interface SurveyIgnoredRepoAdded {
  id: number
  /** 服务端换算出的仓库身份，手填地址时用来让管理员核对 */
  repo_slug: string
  url: string
  created: boolean
}

/** 一次加入一批仓库，共用一条理由；任何一个地址不合法时整批都不写入 */
export function addSurveyIgnoredReposApi(surveyUid: string, urls: string[], note = "") {
  return request<{ items: SurveyIgnoredRepoAdded[] }>({
    url: `surveys/${surveyUid}/ignored-repos`,
    method: "post",
    data: { urls, note }
  })
}

export function updateSurveyIgnoredRepoNoteApi(surveyUid: string, itemId: number, note: string) {
  return request<{ updated: boolean }>({
    url: `surveys/${surveyUid}/ignored-repos/${itemId}`,
    method: "patch",
    data: { note }
  })
}

export function removeSurveyIgnoredRepoApi(surveyUid: string, itemId: number) {
  return request<{ removed: boolean }>({
    url: `surveys/${surveyUid}/ignored-repos/${itemId}`,
    method: "delete"
  })
}

export function getSurveyRunsApi(params: { survey_uid?: string, limit?: number } = {}) {
  return request<{ items: SurveyRun[] }>({ url: "survey-runs", method: "get", params })
}

export function getSurveyRunDetailApi(runUid: string) {
  return request<SurveyRunDetail>({ url: `survey-runs/${runUid}`, method: "get" })
}

export function getSurveyStatsApi(days = 90) {
  return request<SurveyStats>({ url: "survey-stats", method: "get", params: { days } })
}

export function getDestinationsApi() {
  return request<{ items: DestinationItem[], public_url: string }>({ url: "destinations", method: "get" })
}

/** 手动推送到输出目标。不传 bindingId 表示推送全部；推送在后台执行，返回即表示已登记 */
export function pushSurveyRunApi(runUid: string, bindingId?: number | null) {
  return request<{ started: number, busy: number[] }>({
    url: `survey-runs/${runUid}/push`,
    method: "post",
    data: bindingId ? { binding_id: bindingId } : {}
  })
}

/**
 * 连通性测试：用表单里尚未保存的目标位置检查输出目标是否可用。
 * 不会写入任何台账行；测试失败也以 200 返回检查项，失败是测试的正常结论。
 */
export function checkDestinationApi(name: string, target: Record<string, string>, surveyName: string) {
  return request<DestinationCheckResult>({
    url: `destinations/${encodeURIComponent(name)}/check`,
    method: "post",
    data: { target, survey_name: surveyName }
  })
}

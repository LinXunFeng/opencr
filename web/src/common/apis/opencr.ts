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
  writable: { guest_read: boolean, guest_retry: boolean, survey_guest_read: boolean }
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
  schedule_desc?: string
  workspace_bytes?: number
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
  created_at: string
  /** Guest 拿不到标题与正文，字段直接不存在（服务端剔除，不是前端隐藏） */
  title?: string
  body?: string
}

export interface SurveyRunRepo {
  repo_slug: string
  url: string
  branch: string
  commit_sha: string
  status: string
  profile_kind: string
  file_count: number
  error_message: string
}

export interface SurveyRunDetail extends SurveyRun {
  summary: string
  body_included: boolean
  repos: SurveyRunRepo[]
  findings: SurveyFinding[]
  /** 上一次有、本次没有的发现。它们没有对应的库记录，是比对算出来的 */
  resolved_findings: SurveyFinding[]
  counts: { new: number, persisted: number, resolved: number, total: number }
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

export function updateSettingsApi(
  data: Partial<{ guest_read: boolean, guest_retry: boolean, survey_guest_read: boolean }>
) {
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

export function getSurveyIgnoresApi(surveyUid: string) {
  return request<{ items: Array<{ fingerprint: string, note: string, created_at: string }> }>({
    url: `surveys/${surveyUid}/ignores`,
    method: "get"
  })
}

export function addSurveyIgnoreApi(surveyUid: string, fingerprint: string, note = "") {
  return request<{ ignored: boolean }>({
    url: `surveys/${surveyUid}/ignores`,
    method: "post",
    data: { fingerprint, note }
  })
}

export function removeSurveyIgnoreApi(surveyUid: string, fingerprint: string) {
  return request<{ removed: boolean }>({
    url: `surveys/${surveyUid}/ignores/${fingerprint}`,
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

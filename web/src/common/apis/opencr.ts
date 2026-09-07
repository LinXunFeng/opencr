import { request } from "@/http/axios"

/** 当前访问身份。Guest 是身份的缺席，不是一种账号。 */
export type Identity = "admin" | "guest"

export interface MeInfo {
  identity: Identity
  username: string
  guest_read: boolean
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
  writable: { guest_read: boolean }
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

export function updateSettingsApi(data: Partial<{ guest_read: boolean }>) {
  return request<{ writable: { guest_read: boolean } }>({ url: "settings", method: "patch", data })
}

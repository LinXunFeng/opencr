/** 失败运行详情页的范围选择、权限显隐与冲突反馈。 */
import type { VueWrapper } from "@vue/test-utils"
import type { RunDetail } from "../src/common/apis/opencr"
import { flushPromises, mount } from "@vue/test-utils"
import ElementPlus from "element-plus"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import Detail from "../src/pages/runs/detail.vue"

const api = vi.hoisted(() => ({ detail: vi.fn(), retry: vi.fn(), push: vi.fn() }))
vi.mock("../src/common/apis/opencr", () => ({
  getRunDetailApi: api.detail, retryRunApi: api.retry, getChangeHistoryApi: vi.fn()
}))
vi.mock("vue-router", () => ({
  useRoute: () => ({ params: { runUid: "failed-run" } }),
  useRouter: () => ({ push: api.push, back: vi.fn() })
}))

let wrapper: VueWrapper
const run: RunDetail = {
  run_uid: "failed-run", project_id: 1, project_path: "group/project", mr_iid: 1, mr_title: "测试变更",
  trigger: "manual", review_mode: "file", review_skills: [], status: "failed", skip_reason: "", phase: "done",
  files_total: 1, files_done: 0, degradations: [], error_kind: "review_error", error_message: "模型失败",
  started_at: "", heartbeat_at: "", finished_at: "", is_stale: false, retry_of_uid: "", retry_scope: "",
  original_retry_available: true, findings: [], body_included: false, can_retry: true
}

/** 挂载真实 Element Plus 组件，等待详情加载。 */
async function render(overrides: Partial<RunDetail> = {}) {
  api.detail.mockResolvedValue({ ...run, ...overrides })
  wrapper = mount(Detail, { attachTo: document.body, global: { plugins: [ElementPlus] } })
  await flushPromises()
}

/** 点击页面上指定文本的按钮。 */
async function clickButton(label: string) {
  const buttons = Array.from(document.querySelectorAll("button")).filter(b => b.textContent?.trim() === label)
  buttons.at(-1)?.click()
  await flushPromises()
}

beforeEach(() => { vi.clearAllMocks() })
afterEach(() => { wrapper?.unmount(); document.body.innerHTML = "" })

describe("失败运行重新触发", () => {
  it("原范围可恢复时默认选择原范围，提交后进入新运行", async () => {
    api.retry.mockResolvedValue({ run_uid: "new-run", status: "processing" })
    await render()
    await clickButton("重新触发")
    expect((document.querySelector('input[value="original"]') as HTMLInputElement).checked).toBe(true)
    await clickButton("重新触发")
    expect(api.retry).toHaveBeenCalledWith("failed-run", "original")
    expect(api.push).toHaveBeenCalledWith("/runs/detail/new-run")
  })

  it("历史记录仅允许最新全量，显示原因，并保留冲突运行入口", async () => {
    api.retry.mockRejectedValue({ message: "该 MR 已有审查正在运行", response: { data: { active_run_uid: "active-run" } } })
    await render({ original_retry_available: false })
    await clickButton("重新触发")
    expect((document.querySelector('input[value="original"]') as HTMLInputElement).disabled).toBe(true)
    expect((document.querySelector('input[value="latest"]') as HTMLInputElement).checked).toBe(true)
    expect(document.body.textContent).toContain("原运行缺少完整范围或选择参数")
    await clickButton("重新触发")
    expect(api.retry).toHaveBeenCalledWith("failed-run", "latest")
    expect(document.body.textContent).toContain("该 MR 已有审查正在运行")
    const link = Array.from(document.querySelectorAll("a")).find(a => a.textContent?.includes("查看正在运行的审查"))
    link?.click()
    expect(api.push).toHaveBeenCalledWith("/runs/detail/active-run")
  })

  it("无权限的游客不显示重试按钮", async () => {
    await render({ can_retry: false })
    expect(wrapper.text()).not.toContain("重新触发")
    expect(api.retry).not.toHaveBeenCalled()
  })

  it("成功但带降级的运行不显示重试按钮", async () => {
    await render({ status: "succeeded", degradations: [{ kind: "diff_truncated", count: 1 }] })
    expect(wrapper.text()).not.toContain("重新触发")
  })
})

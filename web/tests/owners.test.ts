/** 仓库负责人配置页：显示姓名、拖动调整优先级、按负责人填写多个路径。 */
import type { VueWrapper } from "@vue/test-utils"
import type { RepoOwner } from "../src/common/apis/opencr"
import { DOMWrapper, flushPromises, mount } from "@vue/test-utils"
import ElementPlus, { ElSelect } from "element-plus"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import Owners from "../src/pages/surveys/owners.vue"

const api = vi.hoisted(() => ({ list: vi.fn(), add: vi.fn(), update: vi.fn(), batch: vi.fn(), search: vi.fn() }))
vi.mock("../src/common/apis/opencr", () => ({
  getRepoOwnersApi: api.list,
  addRepoOwnerApi: api.add,
  updateRepoOwnerApi: api.update,
  addRepoOwnerPathsApi: api.batch,
  deleteRepoOwnerApi: vi.fn(),
  searchPlatformUsersApi: api.search
}))

let wrapper: VueWrapper
const owner: RepoOwner = {
  id: 7,
  path: "team/app",
  usernames: ["10086", "10010", "legacy"],
  users: [{ username: "10086", name: "张三" }, { username: "10010", name: "李四" }, { username: "legacy", name: "" }],
  note: "",
  created_at: "",
  updated_at: ""
}

/** 挂载真实 Element Plus 组件，等待列表加载。 */
async function render() {
  api.list.mockResolvedValue({ items: [owner] })
  wrapper = mount(Owners, { attachTo: document.body, global: { plugins: [ElementPlus] } })
  await flushPromises()
}

/** 点击页面上指定文本的按钮（弹窗渲染在 body 下，取最后一个）。 */
async function clickButton(label: string) {
  const buttons = Array.from(document.querySelectorAll("button")).filter(b => b.textContent?.trim() === label)
  buttons.at(-1)?.click()
  await flushPromises()
}

/** 当前可见弹窗里的负责人搜索框：先按关键字搜索，再选中一个人。 */
async function pickUser(keyword: string, username: string) {
  const selects = wrapper.findAllComponents(ElSelect).filter(s => s.isVisible())
  const select = selects.at(-1)!
  await select.props("remoteMethod")!(keyword)
  await flushPromises()
  select.vm.$emit("change", username)
  await flushPromises()
}

/** 编辑弹窗里负责人列表的文字，按显示顺序。 */
function listedOwners() {
  return Array.from(document.querySelectorAll(".owner-list li .text")).map(el => el.textContent?.trim())
}

beforeEach(() => {
  vi.clearAllMocks()
})
afterEach(() => {
  wrapper?.unmount()
  document.body.innerHTML = ""
})

describe("仓库负责人", () => {
  it("列表里显示 @用户名 姓名，还没查到姓名的只显示用户名", async () => {
    await render()
    const tags = Array.from(document.querySelectorAll(".el-table .el-tag")).map(el => el.textContent?.trim())
    expect(tags).toEqual(["@10086 张三", "@10010 李四", "@legacy"])
  })

  it("编辑时可以拖动调整顺序、追加与移除，保存时按列表顺序提交用户名", async () => {
    api.search.mockResolvedValue({ items: [{ username: "carol", name: "Carol", state: "active", bot: false }] })
    api.update.mockResolvedValue(owner)
    await render()
    await clickButton("编辑")
    expect(listedOwners()).toEqual(["@10086 张三", "@10010 李四", "@legacy"])

    // 把第三位拖到第一位
    const items = () => Array.from(document.querySelectorAll(".owner-list li")).map(el => new DOMWrapper(el))
    await items()[2].trigger("dragstart")
    await items()[1].trigger("dragenter")
    await items()[0].trigger("dragenter")
    await items()[0].trigger("dragend")
    expect(listedOwners()).toEqual(["@legacy", "@10086 张三", "@10010 李四"])

    await pickUser("car", "carol")
    // 已在列表里的人再选一次不重复添加
    await pickUser("100", "10086")
    expect(listedOwners()).toEqual(["@legacy", "@10086 张三", "@10010 李四", "@carol Carol"])
    ;(document.querySelectorAll(".owner-list li .remove")[1] as HTMLElement).dispatchEvent(new MouseEvent("click"))
    await flushPromises()

    await clickButton("保存")
    expect(api.update).toHaveBeenCalledWith(7, { path: "team/app", usernames: ["legacy", "10010", "carol"], note: "" })
  })

  it("按负责人添加：选一个人、一行一个路径，提交后提示各路径的结果", async () => {
    api.search.mockResolvedValue({ items: [{ username: "carol", name: "Carol", state: "active", bot: false }] })
    api.batch.mockResolvedValue({ created: ["team/web"], appended: ["team/app"], unchanged: [] })
    await render()
    await clickButton("按负责人添加")
    await clickButton("保存")
    expect(api.batch).not.toHaveBeenCalled()

    await pickUser("car", "carol")
    expect(document.body.textContent).toContain("@carol Carol")
    const textarea = Array.from(document.querySelectorAll("textarea")).find(t => t.placeholder.includes("team/app"))!
    await new DOMWrapper(textarea).setValue("team/app\n\n  https://g.com/team/web  \n")
    await clickButton("保存")
    expect(api.batch).toHaveBeenCalledWith({ username: "carol", paths: ["team/app", "https://g.com/team/web"], note: "" })
    expect(document.body.textContent).toContain("新建 1 条，追加到 1 条已有配置的末尾")
    expect(api.list).toHaveBeenCalledTimes(2)
  })
})

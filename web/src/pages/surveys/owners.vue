<script lang="ts" setup>
import type { PlatformUser, RepoOwner, RepoOwnerUser } from "@@/apis/opencr"
import {
  addRepoOwnerApi,
  addRepoOwnerPathsApi,
  deleteRepoOwnerApi,
  getRepoOwnersApi,
  searchPlatformUsersApi,
  updateRepoOwnerApi
} from "@@/apis/opencr"
import { FIX_NOTES, formatTime } from "@@/constants/opencr"
import { Close, Rank } from "@element-plus/icons-vue"
import { useUserStore } from "@/pinia/stores/user"

/**
 * 仓库负责人配置页：给项目或组织指定负责人，修复合并请求在原始作者不可用时指派给他们。
 *
 * 全局配置，不挂在某个巡检下：负责人是仓库本身的属性，多个巡检覆盖同一个仓库时用的是同一份。
 * 默认只对 Admin 开放；管理员打开「游客可见仓库负责人」后游客可以只读查看，增删改仍只有 Admin 能做。
 * 路由 meta.guestSwitch 与这里的按钮显隐都只是体验层，接口本身按 require_repo_owner_viewer / require_admin 校验。
 */
const userStore = useUserStore()
const loading = ref(true)
const items = ref<RepoOwner[]>([])
const dialog = reactive({
  visible: false,
  saving: false,
  /** 为空表示新增 */
  id: null as number | null,
  path: "",
  /** 顺序就是优先级 */
  users: [] as RepoOwnerUser[],
  note: ""
})
/** 按负责人填写的弹窗：一个人、多个路径 */
const batch = reactive({
  visible: false,
  saving: false,
  user: null as RepoOwnerUser | null,
  /** 一行一个路径或地址 */
  paths: "",
  note: ""
})
/**
 * 搜索框的值。选中后立即清空、把人放进下面的列表：选中的人由列表展示，
 * 不留在搜索框里——远程搜索的候选项换一批后，框里的已选项就只剩裸用户名了
 */
const picked = ref("")

async function load() {
  loading.value = true
  try {
    items.value = (await getRepoOwnersApi()).items
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    loading.value = false
  }
}

/**
 * 负责人候选项：输入时实时搜索代码平台上的用户。
 * 不允许手打任意用户名：负责人按用户名匹配，拼错了要到发起修复时才会被默默跳过，那时已经没人注意了
 */
const userOptions = ref<PlatformUser[]>([])
const searchingUsers = ref(false)
/** 搜索序号：输入得快时，先发出、后返回的旧结果不能盖掉新结果 */
let searchSeq = 0

/** el-select 的远程搜索：按关键字刷新候选项 */
async function searchUsers(query: string) {
  const seq = ++searchSeq
  const q = query.trim()
  if (!q) {
    // 序号已经加过，在途的旧请求回来时不会再动加载状态，这里得自己收掉
    userOptions.value = []
    searchingUsers.value = false
    return
  }
  searchingUsers.value = true
  try {
    const items = (await searchPlatformUsersApi(q)).items
    if (seq === searchSeq) userOptions.value = items
  } catch (error) {
    if (seq === searchSeq) {
      userOptions.value = []
      ElMessage.error((error as Error).message)
    }
  } finally {
    if (seq === searchSeq) searchingUsers.value = false
  }
}

/** 账号为什么不能当负责人（机器人、已封禁或停用）；能当时返回空串 */
function unavailableReason(user: PlatformUser) {
  if (user.bot) return "机器人"
  return user.state !== "active" ? user.state : ""
}

/** 一位负责人的展示文字：@用户名 姓名，与搜索候选项的写法一致；还没查到姓名时只有用户名 */
function ownerText(user: RepoOwnerUser) {
  return `@${user.username}${user.name ? ` ${user.name}` : ""}`
}

/** 候选项的文字：@用户名 姓名，不能选的注明原因 */
function userLabel(user: PlatformUser) {
  const base = ownerText(user)
  const reason = unavailableReason(user)
  return reason ? `${base}（${reason}）` : base
}

/** 搜索框里选中的人；在当前候选项里找，找不到（候选项刚好换了一批）时只带用户名 */
function takePicked(username: string): RepoOwnerUser | null {
  picked.value = ""
  if (!username) return null
  const option = userOptions.value.find(u => u.username === username)
  return { username, name: option?.name ?? "" }
}

/** 作废还在途的搜索并清空候选项，免得上一个弹窗的结果落进这个弹窗 */
function resetSearch() {
  searchSeq++
  userOptions.value = []
  searchingUsers.value = false
  picked.value = ""
}

/** 打开编辑弹窗；不传 row 为新增。row 来自 el-table 插槽时类型是 DefaultRow，在这里收窄 */
function openDialog(raw?: any) {
  const row = raw as RepoOwner | undefined
  dialog.id = row?.id ?? null
  dialog.path = row?.path ?? ""
  dialog.users = row ? row.users.map(u => ({ ...u })) : []
  dialog.note = row?.note ?? ""
  resetSearch()
  dialog.visible = true
}

/** 这个人是否已在编辑弹窗的列表里；平台上的用户名不区分大小写，与服务端的去重口径一致 */
function isListed(username: string) {
  return dialog.users.some(u => u.username.toLowerCase() === username.toLowerCase())
}

/** 编辑弹窗里选中一个人：加到列表末尾（优先级最低），已在列表里的不重复加 */
function addPicked(username: string) {
  const user = takePicked(username)
  if (user && !isListed(user.username)) dialog.users.push(user)
}

/** 从编辑弹窗的负责人列表里移除一位 */
function removeUser(index: number) {
  dialog.users.splice(index, 1)
}

/** 正在拖动的那一项的下标；没在拖时为 null */
const dragIndex = ref<number | null>(null)

/** 开始拖动列表里的一位负责人 */
function onDragStart(index: number, event: DragEvent) {
  dragIndex.value = index
  if (event.dataTransfer) {
    event.dataTransfer.effectAllowed = "move"
    // Firefox 不 setData 就不会开始拖动
    event.dataTransfer.setData("text/plain", String(index))
  }
}

/** 拖到另一项上时立即换位，松手前就能看到排好的顺序 */
function onDragEnter(index: number) {
  const from = dragIndex.value
  if (from === null || from === index) return
  const [moved] = dialog.users.splice(from, 1)
  dialog.users.splice(index, 0, moved)
  dragIndex.value = index
}

/** 松手或取消拖动 */
function onDragEnd() {
  dragIndex.value = null
}

async function save() {
  if (!dialog.path.trim() || !dialog.users.length) {
    ElMessage.warning("请填写路径与至少一个负责人")
    return
  }
  dialog.saving = true
  const data = { path: dialog.path, usernames: dialog.users.map(u => u.username), note: dialog.note }
  try {
    if (dialog.id === null) await addRepoOwnerApi(data)
    else await updateRepoOwnerApi(dialog.id, data)
    ElMessage.success("已保存")
    dialog.visible = false
    await load()
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    dialog.saving = false
  }
}

/** 打开按负责人填写的弹窗 */
function openBatch() {
  batch.user = null
  batch.paths = ""
  batch.note = ""
  resetSearch()
  batch.visible = true
}

/** 按负责人填写的弹窗里选中负责人 */
function pickBatchUser(username: string) {
  batch.user = takePicked(username)
}

/** 保存按负责人填写的配置：路径按换行或逗号拆开，与服务端的拆分口径一致 */
async function saveBatch() {
  const paths = batch.paths.split(/[\n,，]/).map(p => p.trim()).filter(Boolean)
  if (!batch.user || !paths.length) {
    ElMessage.warning(FIX_NOTES.ownerBatchMissing)
    return
  }
  batch.saving = true
  try {
    const result = await addRepoOwnerPathsApi({ username: batch.user.username, paths, note: batch.note })
    ElMessage.success(FIX_NOTES.ownerBatchDone(result))
    batch.visible = false
    await load()
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    batch.saving = false
  }
}

// el-table 的插槽把 row 定为 DefaultRow，在边界处收窄（与 FindingTable.vue 的写法一致）
async function remove(row: any) {
  const item = row as RepoOwner
  try {
    await ElMessageBox.confirm(`删除 ${item.path} 的负责人配置？之后这里的仓库在原始作者不可用时不再指派处理人。`, "删除配置", {
      type: "warning"
    })
  } catch {
    return
  }
  try {
    await deleteRepoOwnerApi(item.id)
    await load()
  } catch (error) {
    ElMessage.error((error as Error).message)
  }
}

onMounted(load)
</script>

<template>
  <div v-loading="loading" class="app-container">
    <el-card shadow="never">
      <template #header>
        <div class="header">
          <span>仓库负责人</span>
          <div v-if="userStore.isAdmin">
            <el-button size="small" @click="openBatch()">
              按负责人添加
            </el-button>
            <el-button size="small" type="primary" @click="openDialog()">
              按仓库添加
            </el-button>
          </div>
        </div>
      </template>

      <el-alert class="mb" type="info" :closable="false" show-icon :title="FIX_NOTES.ownerPage" />

      <el-table :data="items" size="small" empty-text="还没有配置，原始作者不可用时合并请求不指派处理人">
        <el-table-column label="项目或组织路径" min-width="220">
          <template #default="{ row }">
            <code>{{ row.path }}</code>
          </template>
        </el-table-column>
        <el-table-column label="负责人（按优先级）" min-width="220">
          <template #default="{ row }">
            <el-tag v-for="user in row.users" :key="user.username" size="small" effect="plain" class="mr">
              {{ ownerText(user) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="note" label="备注" min-width="200" show-overflow-tooltip />
        <el-table-column label="更新时间" min-width="160">
          <template #default="{ row }">
            {{ formatTime(row.updated_at) }}
          </template>
        </el-table-column>
        <el-table-column v-if="userStore.isAdmin" label="操作" width="120" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click="openDialog(row)">
              编辑
            </el-button>
            <el-button link type="danger" size="small" @click="remove(row)">
              删除
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-dialog v-model="dialog.visible" :title="dialog.id === null ? '新增仓库负责人' : '编辑仓库负责人'" width="560px">
      <el-form label-width="90px">
        <el-form-item label="路径">
          <el-input v-model="dialog.path" placeholder="例如 team/app 或 team，也可以粘贴地址" />
          <div class="sub">
            {{ FIX_NOTES.ownerPath }}
          </div>
        </el-form-item>
        <el-form-item label="负责人">
          <el-select
            v-model="picked" filterable remote :remote-method="searchUsers" :loading="searchingUsers"
            :placeholder="FIX_NOTES.ownerSearchPlaceholder" :no-match-text="FIX_NOTES.ownerSearchEmpty"
            :no-data-text="FIX_NOTES.ownerSearchEmpty" style="width: 100%" @change="addPicked"
          >
            <el-option
              v-for="user in userOptions" :key="user.username" :label="userLabel(user)" :value="user.username"
              :disabled="Boolean(unavailableReason(user)) || isListed(user.username)"
            />
          </el-select>
          <ol v-if="dialog.users.length" class="owner-list">
            <li
              v-for="(user, index) in dialog.users" :key="user.username" draggable="true"
              :class="{ dragging: dragIndex === index }"
              @dragstart="onDragStart(index, $event)" @dragenter.prevent="onDragEnter(index)"
              @dragover.prevent @drop.prevent @dragend="onDragEnd"
            >
              <el-icon class="handle">
                <Rank />
              </el-icon>
              <span class="order">{{ index + 1 }}</span>
              <span class="text">{{ ownerText(user) }}</span>
              <el-icon class="remove" @click="removeUser(index)">
                <Close />
              </el-icon>
            </li>
          </ol>
          <div class="sub">
            {{ FIX_NOTES.ownerUsernames }}
          </div>
        </el-form-item>
        <el-form-item label="备注">
          <el-input v-model="dialog.note" type="textarea" :rows="2" :placeholder="FIX_NOTES.ownerNotePlaceholder" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialog.visible = false">
          取消
        </el-button>
        <el-button type="primary" :loading="dialog.saving" @click="save">
          保存
        </el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="batch.visible" title="按负责人添加" width="560px">
      <el-form label-width="90px">
        <el-form-item label="负责人">
          <el-select
            v-model="picked" filterable remote :remote-method="searchUsers" :loading="searchingUsers"
            :placeholder="FIX_NOTES.ownerSearchPlaceholder" :no-match-text="FIX_NOTES.ownerSearchEmpty"
            :no-data-text="FIX_NOTES.ownerSearchEmpty" style="width: 100%" @change="pickBatchUser"
          >
            <el-option
              v-for="user in userOptions" :key="user.username" :label="userLabel(user)" :value="user.username"
              :disabled="Boolean(unavailableReason(user))"
            />
          </el-select>
          <el-tag v-if="batch.user" class="picked" closable effect="plain" @close="batch.user = null">
            {{ ownerText(batch.user) }}
          </el-tag>
        </el-form-item>
        <el-form-item label="路径">
          <el-input v-model="batch.paths" type="textarea" :rows="6" :placeholder="FIX_NOTES.ownerBatchPathsPlaceholder" />
          <div class="sub">
            {{ FIX_NOTES.ownerBatchPaths }}
          </div>
        </el-form-item>
        <el-form-item label="备注">
          <el-input v-model="batch.note" type="textarea" :rows="2" :placeholder="FIX_NOTES.ownerNotePlaceholder" />
          <div class="sub">
            {{ FIX_NOTES.ownerBatchNote }}
          </div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="batch.visible = false">
          取消
        </el-button>
        <el-button type="primary" :loading="batch.saving" @click="saveBatch">
          保存
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style lang="scss" scoped>
.header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.mb {
  margin-bottom: 16px;
}
.mr {
  margin-right: 4px;
}
.owner-list {
  width: 100%;
  margin: 8px 0 0;
  padding: 0;
  list-style: none;
  li {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 4px 8px;
    margin-bottom: 4px;
    border: 1px solid var(--el-border-color-lighter);
    border-radius: 4px;
    line-height: 22px;
    cursor: move;
    &.dragging {
      opacity: 0.5;
      border-style: dashed;
    }
  }
  .handle {
    color: var(--el-text-color-secondary);
  }
  .order {
    min-width: 16px;
    color: var(--el-text-color-secondary);
    font-size: 12px;
  }
  .text {
    flex: 1;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .remove {
    cursor: pointer;
    color: var(--el-text-color-secondary);
    &:hover {
      color: var(--el-color-danger);
    }
  }
}
.picked {
  margin-top: 8px;
}
.sub {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  line-height: 1.6;
}
</style>

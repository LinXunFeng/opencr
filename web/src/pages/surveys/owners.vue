<script lang="ts" setup>
import type { RepoOwner } from "@@/apis/opencr"
import { addRepoOwnerApi, deleteRepoOwnerApi, getRepoOwnersApi, updateRepoOwnerApi } from "@@/apis/opencr"
import { FIX_NOTES, formatTime } from "@@/constants/opencr"

/**
 * 仓库负责人配置页：给项目或组织指定负责人，修复合并请求在原始作者不可用时指派给他们。
 *
 * 全局配置，不挂在某个巡检下：负责人是仓库本身的属性，多个巡检覆盖同一个仓库时用的是同一份。
 * 只对 Admin 开放（路由 meta.adminOnly 只管显隐，接口本身也是 require_admin）。
 */
const loading = ref(true)
const items = ref<RepoOwner[]>([])
const dialog = reactive({
  visible: false,
  saving: false,
  /** 为空表示新增 */
  id: null as number | null,
  path: "",
  usernames: [] as string[],
  note: ""
})

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

/** 打开编辑弹窗；不传 row 为新增。row 来自 el-table 插槽时类型是 DefaultRow，在这里收窄 */
function openDialog(raw?: any) {
  const row = raw as RepoOwner | undefined
  dialog.id = row?.id ?? null
  dialog.path = row?.path ?? ""
  dialog.usernames = row ? [...row.usernames] : []
  dialog.note = row?.note ?? ""
  dialog.visible = true
}

async function save() {
  if (!dialog.path.trim() || !dialog.usernames.length) {
    ElMessage.warning("请填写路径与至少一个负责人")
    return
  }
  dialog.saving = true
  const data = { path: dialog.path, usernames: dialog.usernames, note: dialog.note }
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

// el-table 的插槽把 row 定为 DefaultRow，在边界处收窄（与 FindingTable.vue 的写法一致）
async function remove(row: any) {
  const item = row as RepoOwner
  try {
    await ElMessageBox.confirm(`删除 ${item.path} 的负责人配置？之后这里的仓库按项目权限推断负责人。`, "删除配置", {
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
          <el-button size="small" type="primary" @click="openDialog()">
            新增
          </el-button>
        </div>
      </template>

      <el-alert class="mb" type="info" :closable="false" show-icon :title="FIX_NOTES.ownerPage" />

      <el-table :data="items" size="small" empty-text="还没有配置，负责人全部按项目权限推断">
        <el-table-column label="项目或组织路径" min-width="220">
          <template #default="{ row }">
            <code>{{ row.path }}</code>
          </template>
        </el-table-column>
        <el-table-column label="负责人（按优先级）" min-width="220">
          <template #default="{ row }">
            <el-tag v-for="name in row.usernames" :key="name" size="small" effect="plain" class="mr">
              @{{ name }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="note" label="备注" min-width="200" show-overflow-tooltip />
        <el-table-column label="更新时间" min-width="160">
          <template #default="{ row }">
            {{ formatTime(row.updated_at) }}
          </template>
        </el-table-column>
        <el-table-column label="操作" width="120" fixed="right">
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
            v-model="dialog.usernames" multiple filterable allow-create default-first-option
            :reserve-keyword="false" placeholder="输入用户名后回车" style="width: 100%"
          />
          <div class="sub">
            {{ FIX_NOTES.ownerUsernames }}
          </div>
        </el-form-item>
        <el-form-item label="备注">
          <el-input v-model="dialog.note" type="textarea" :rows="2" placeholder="选填，例如：移动端负责人" />
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
.sub {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  line-height: 1.6;
}
</style>

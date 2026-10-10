<script lang="ts" setup>
import type { SurveyIgnore } from "@@/apis/opencr"
import { getSurveyIgnoresApi, removeSurveyIgnoreApi, updateSurveyIgnoreNoteApi } from "@@/apis/opencr"
import {
  CATEGORY_LABEL,
  formatTime,
  SEVERITY_LABEL,
  SEVERITY_TAG,
  SURVEY_NOTES,
  validateIgnoreNote
} from "@@/constants/opencr"

/**
 * 忽略清单管理页：查看某个巡检忽略了哪些问题、补写理由、取消忽略。
 *
 * 只对 Admin 开放（路由 meta.adminOnly 只管显隐，接口本身也是 require_admin）。
 */
const route = useRoute()
const router = useRouter()
const loading = ref(true)
const surveyName = ref("")
const items = ref<SurveyIgnore[]>([])

const surveyUid = computed(() => String(route.params.surveyUid || ""))

async function load() {
  loading.value = true
  try {
    const result = await getSurveyIgnoresApi(surveyUid.value)
    surveyName.value = result.survey_name
    items.value = result.items
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    loading.value = false
  }
}

// el-table 的插槽把 row 定为 DefaultRow，在边界处收窄（与 FindingTable.vue 的写法一致）
function location(row: any) {
  const item = row as SurveyIgnore
  return item.file_path ? `${item.repo_slug}/${item.file_path}` : ""
}

async function editNote(row: any) {
  const item = row as SurveyIgnore
  let note: string
  try {
    const result = await ElMessageBox.prompt("为什么不再提醒这个问题？半年后这里是唯一的线索。", "忽略理由", {
      inputType: "textarea",
      inputValue: item.note,
      inputPlaceholder: "例如：第三方 SDK 的代码，无法修改",
      inputValidator: validateIgnoreNote
    })
    note = (result.value || "").trim()
  } catch {
    return
  }
  try {
    await updateSurveyIgnoreNoteApi(surveyUid.value, item.fingerprint, note)
    ElMessage.success("理由已保存")
    await load()
  } catch (error) {
    ElMessage.error((error as Error).message)
  }
}

async function unignore(row: any) {
  const item = row as SurveyIgnore
  try {
    await ElMessageBox.confirm(SURVEY_NOTES.ignoreRemove, "取消忽略", { type: "warning" })
  } catch {
    return
  }
  try {
    await removeSurveyIgnoreApi(surveyUid.value, item.fingerprint)
    ElMessage.success("已取消忽略")
    await load()
  } catch (error) {
    ElMessage.error((error as Error).message)
  }
}

function openRun(row: any) {
  router.push({ name: "SurveyRunDetail", params: { runUid: (row as SurveyIgnore).last_run_uid } })
}

onMounted(load)
</script>

<template>
  <div v-loading="loading" class="app-container">
    <el-card shadow="never">
      <template #header>
        <div class="header">
          <span>{{ surveyName || "巡检" }} —— 忽略清单（{{ items.length }}）</span>
          <el-button size="small" @click="router.push({ name: 'SurveyList' })">
            返回巡检配置
          </el-button>
        </div>
      </template>
      <el-alert class="mb" type="info" :closable="false" show-icon :title="SURVEY_NOTES.ignoreScope" />
      <el-table :data="items" size="small" empty-text="还没有忽略任何问题">
        <el-table-column label="位置" min-width="260" show-overflow-tooltip>
          <template #default="{ row }">
            <code v-if="location(row)">{{ location(row) }}</code>
            <el-tooltip v-else :content="SURVEY_NOTES.ignoreNoIssue">
              <span class="muted">指纹 {{ row.fingerprint.slice(0, 12) }}…</span>
            </el-tooltip>
          </template>
        </el-table-column>
        <el-table-column label="类别" width="120">
          <template #default="{ row }">
            {{ row.category ? CATEGORY_LABEL[row.category] || row.category : "—" }}
          </template>
        </el-table-column>
        <el-table-column label="最近一次的问题" min-width="240" show-overflow-tooltip>
          <template #default="{ row }">
            <el-tag v-if="row.severity" size="small" :type="SEVERITY_TAG[row.severity] || 'info'" class="mr">
              {{ SEVERITY_LABEL[row.severity] || row.severity }}
            </el-tag>
            <span>{{ row.title || "—" }}</span>
          </template>
        </el-table-column>
        <el-table-column label="理由" min-width="200" show-overflow-tooltip>
          <template #default="{ row }">
            <span v-if="row.note">{{ row.note }}</span>
            <span v-else class="muted">未填写</span>
          </template>
        </el-table-column>
        <el-table-column label="标记时间" min-width="160">
          <template #default="{ row }">
            {{ formatTime(row.created_at) }}
          </template>
        </el-table-column>
        <el-table-column label="操作" width="200" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click="editNote(row)">
              编辑理由
            </el-button>
            <el-button v-if="row.last_run_uid" link type="primary" size="small" @click="openRun(row)">
              最近报告
            </el-button>
            <el-button link type="warning" size="small" @click="unignore(row)">
              取消忽略
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>
  </div>
</template>

<style lang="scss" scoped>
.header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.muted {
  color: var(--el-text-color-secondary);
}

.mb {
  margin-bottom: 12px;
}
.mr {
  margin-right: 4px;
}
</style>

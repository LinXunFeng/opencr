<script lang="ts" setup>
import type { SurveyIgnore, SurveyIgnoredRepo, SurveyRepoCandidate } from "@@/apis/opencr"
import {
  addSurveyIgnoredRepoApi,
  getSurveyIgnoredReposApi,
  getSurveyIgnoresApi,
  removeSurveyIgnoreApi,
  removeSurveyIgnoredRepoApi,
  updateSurveyIgnoredRepoNoteApi,
  updateSurveyIgnoreNoteApi
} from "@@/apis/opencr"
import {
  CATEGORY_LABEL,
  formatTime,
  IGNORE_NOTE_MAX,
  SEVERITY_LABEL,
  SEVERITY_TAG,
  SURVEY_NOTES,
  validateIgnoreNote
} from "@@/constants/opencr"

/**
 * 忽略清单管理页：查看某个巡检忽略了哪些问题与仓库、核对模型最近隐藏的是哪条、补写理由、取消忽略，
 * 以及把组织里不维护、不重要的仓库标记为不再巡检。
 *
 * 只对 Admin 开放（路由 meta.adminOnly 只管显隐，接口本身也是 require_admin）。
 */
const route = useRoute()
const router = useRouter()
const loading = ref(true)
const surveyName = ref("")
const items = ref<SurveyIgnore[]>([])
const repos = ref<SurveyIgnoredRepo[]>([])
const candidates = ref<SurveyRepoCandidate[]>([])
const repoDialog = reactive({ visible: false, saving: false, url: "", note: "" })

const surveyUid = computed(() => String(route.params.surveyUid || ""))

async function load() {
  loading.value = true
  try {
    const [result, repoResult] = await Promise.all([
      getSurveyIgnoresApi(surveyUid.value),
      getSurveyIgnoredReposApi(surveyUid.value)
    ])
    surveyName.value = result.survey_name
    items.value = result.items
    repos.value = repoResult.items
    candidates.value = repoResult.candidates
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    loading.value = false
  }
}

// el-table 的插槽把 row 定为 DefaultRow，在边界处收窄（与 FindingTable.vue 的写法一致）
function location(row: any) {
  const item = row as SurveyIgnore
  if (!item.file_path) return ""
  return `${item.repo_slug}/${item.file_path}${item.line ? `:${item.line}` : ""}`
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
    await updateSurveyIgnoreNoteApi(surveyUid.value, item.id, note)
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
    await removeSurveyIgnoreApi(surveyUid.value, item.id)
    ElMessage.success("已取消忽略")
    await load()
  } catch (error) {
    ElMessage.error((error as Error).message)
  }
}

function openRepoDialog() {
  Object.assign(repoDialog, { visible: true, saving: false, url: "", note: "" })
}

async function addRepo() {
  const url = repoDialog.url.trim()
  if (!url) {
    ElMessage.warning("请选择或填写仓库")
    return
  }
  repoDialog.saving = true
  try {
    const result = await addSurveyIgnoredRepoApi(surveyUid.value, url, repoDialog.note.trim())
    ElMessage.success(`已加入忽略清单（仓库标识 ${result.repo_slug}），下一轮巡检起生效`)
    repoDialog.visible = false
    await load()
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    repoDialog.saving = false
  }
}

async function editRepoNote(row: any) {
  const item = row as SurveyIgnoredRepo
  let note: string
  try {
    const result = await ElMessageBox.prompt(SURVEY_NOTES.ignoreRepoNotePrompt, "忽略理由", {
      inputType: "textarea",
      inputValue: item.note,
      inputPlaceholder: SURVEY_NOTES.ignoreRepoNotePlaceholder,
      inputValidator: validateIgnoreNote
    })
    note = (result.value || "").trim()
  } catch {
    return
  }
  try {
    await updateSurveyIgnoredRepoNoteApi(surveyUid.value, item.id, note)
    ElMessage.success("理由已保存")
    await load()
  } catch (error) {
    ElMessage.error((error as Error).message)
  }
}

async function unignoreRepo(row: any) {
  const item = row as SurveyIgnoredRepo
  try {
    await ElMessageBox.confirm(SURVEY_NOTES.ignoreRepoRemove, "取消忽略", { type: "warning" })
  } catch {
    return
  }
  try {
    await removeSurveyIgnoredRepoApi(surveyUid.value, item.id)
    ElMessage.success("已取消忽略")
    await load()
  } catch (error) {
    ElMessage.error((error as Error).message)
  }
}

function openRun(runUid: string) {
  router.push({ name: "SurveyRunDetail", params: { runUid } })
}

onMounted(load)
</script>

<template>
  <div v-loading="loading" class="app-container">
    <el-card shadow="never">
      <template #header>
        <div class="header">
          <span>{{ surveyName || "巡检" }} —— 已忽略的问题（{{ items.length }}）</span>
          <el-button size="small" @click="router.push({ name: 'SurveyList' })">
            返回巡检配置
          </el-button>
        </div>
      </template>
      <el-alert class="mb" type="info" :closable="false" show-icon :title="SURVEY_NOTES.ignoreScope" />
      <el-table :data="items" size="small" empty-text="还没有忽略任何问题">
        <el-table-column label="已忽略的问题" min-width="300" show-overflow-tooltip>
          <template #default="{ row }">
            <template v-if="row.active">
              <el-tag size="small" :type="SEVERITY_TAG[row.severity] || 'info'" class="mr">
                {{ SEVERITY_LABEL[row.severity] || row.severity }}
              </el-tag>
              <span>{{ row.title }}</span>
            </template>
            <el-tooltip v-else :content="SURVEY_NOTES.ignoreInactive">
              <el-tag size="small" type="info" effect="plain">
                已失效
              </el-tag>
            </el-tooltip>
          </template>
        </el-table-column>
        <el-table-column label="位置" min-width="240" show-overflow-tooltip>
          <template #default="{ row }">
            <code v-if="location(row)">{{ location(row) }}</code>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>
        <el-table-column label="类别" width="110">
          <template #default="{ row }">
            {{ CATEGORY_LABEL[row.category] || row.category }}
          </template>
        </el-table-column>
        <el-table-column min-width="240">
          <template #header>
            <el-tooltip :content="SURVEY_NOTES.ignoreLastMatch">
              <span>最近一次隐藏</span>
            </el-tooltip>
          </template>
          <template #default="{ row }">
            <template v-if="row.last_match">
              <el-button link type="primary" size="small" @click="openRun(row.last_match.run_uid)">
                {{ formatTime(row.last_match.created_at) }}
              </el-button>
              <div class="muted ellipsis">
                第 {{ row.last_match.line || "?" }} 行：{{ row.last_match.title || "—" }}
              </div>
            </template>
            <span v-else class="muted">保留的运行里没有</span>
          </template>
        </el-table-column>
        <el-table-column label="理由" min-width="180" show-overflow-tooltip>
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
        <el-table-column label="操作" width="150" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click="editNote(row)">
              编辑理由
            </el-button>
            <el-button link type="warning" size="small" @click="unignore(row)">
              取消忽略
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-card shadow="never" class="mt">
      <template #header>
        <div class="header">
          <span>已忽略的仓库（{{ repos.length }}）</span>
          <el-button size="small" type="primary" @click="openRepoDialog">
            添加仓库
          </el-button>
        </div>
      </template>
      <el-alert class="mb" type="info" :closable="false" show-icon :title="SURVEY_NOTES.ignoreRepoScope" />
      <el-table :data="repos" size="small" empty-text="还没有忽略任何仓库">
        <el-table-column label="仓库" min-width="300" show-overflow-tooltip>
          <template #default="{ row }">
            <code>{{ row.url || row.repo_slug }}</code>
            <span v-if="row.url" class="muted">（{{ row.repo_slug }}）</span>
          </template>
        </el-table-column>
        <el-table-column label="理由" min-width="240" show-overflow-tooltip>
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
        <el-table-column label="操作" width="150" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click="editRepoNote(row)">
              编辑理由
            </el-button>
            <el-button link type="warning" size="small" @click="unignoreRepo(row)">
              取消忽略
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-dialog v-model="repoDialog.visible" title="不再巡检的仓库" width="560px">
      <el-form label-width="70px">
        <el-form-item label="仓库">
          <!-- 候选取自最近一次运行展开出的清单；不在里面的（例如还没跑过）可以直接填地址或 group/project -->
          <el-select
            v-model="repoDialog.url"
            filterable allow-create default-first-option clearable
            placeholder="从最近一次巡检的仓库里选，或填地址 / group/project"
            style="width: 100%"
          >
            <el-option v-for="c in candidates" :key="c.repo_slug" :label="c.url" :value="c.url" />
          </el-select>
        </el-form-item>
        <el-form-item label="理由">
          <el-input
            v-model="repoDialog.note" type="textarea" :rows="3" :maxlength="IGNORE_NOTE_MAX"
            :placeholder="SURVEY_NOTES.ignoreRepoNotePlaceholder"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="repoDialog.visible = false">
          取消
        </el-button>
        <el-button type="primary" :loading="repoDialog.saving" @click="addRepo">
          加入忽略清单
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

.muted {
  color: var(--el-text-color-secondary);
}

.ellipsis {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.mb {
  margin-bottom: 12px;
}
.mt {
  margin-top: 12px;
}
.mr {
  margin-right: 4px;
}
</style>

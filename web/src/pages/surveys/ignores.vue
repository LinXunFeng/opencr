<script lang="ts" setup>
import type { SurveyIgnore, SurveyIgnoredRepo, SurveyRepoCandidate, SurveySourceError } from "@@/apis/opencr"
import {
  addSurveyIgnoredRepoApi,
  getSurveyIgnoredReposApi,
  getSurveyIgnoresApi,
  getSurveyLiveRepoCandidatesApi,
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
// 项目不只对接 GitLab，说明里要访问的平台按服务端配置的平台名显示；接口返回前先用通用叫法
const platformName = ref("代码平台")
/**
 * 候选清单的出处：run = 最近一次运行的记录（可能过时），live = 刚从代码平台实时展开。
 * 两种来源的可信度不同，弹窗里要分别说明，不能让人把过时的清单当成当前配置的结果。
 */
const candidateSource = reactive({
  kind: "run" as "run" | "live",
  runStartedAt: "",
  // 实时清单不会自己过期，关掉弹窗再打开时仍在用；带上获取时刻，旧了一眼看得出
  fetchedAt: "",
  refreshing: false,
  errors: [] as SurveySourceError[]
})
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
    platformName.value = repoResult.platform_name || "代码平台"
    candidateSource.runStartedAt = repoResult.candidates_run_started_at
    if (candidateSource.kind === "live") {
      // 刷新过就沿用实时清单，只剔掉刚忽略的；换回运行记录等于把刚拿到的新清单又换成过时的
      const ignored = new Set(repoResult.items.map(r => r.repo_slug))
      candidates.value = candidates.value.filter(c => !ignored.has(c.repo_slug))
    } else {
      candidates.value = repoResult.candidates
    }
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

// 每次刷新的序号。取消忽略会让进行中的刷新作废：那次结果生成于取消之前，缺了刚放回来的仓库，
// 晚到之后若照常采用，界面会把一份不完整的清单标成"与下一轮一致"
let refreshSeq = 0

async function refreshCandidates() {
  const seq = ++refreshSeq
  candidateSource.refreshing = true
  try {
    const result = await getSurveyLiveRepoCandidatesApi(surveyUid.value)
    if (seq !== refreshSeq) return
    // 刷新期间刚加入忽略的仓库可能还在返回结果里（请求先于添加发出），按当前清单再剔一遍
    const ignored = new Set(repos.value.map(r => r.repo_slug))
    candidates.value = result.items.filter(c => !ignored.has(c.repo_slug))
    Object.assign(candidateSource, { kind: "live", fetchedAt: new Date().toISOString(), errors: result.errors })
    ElMessage.success(`已按当前配置获取 ${candidates.value.length} 个候选仓库`)
  } catch (error) {
    if (seq !== refreshSeq) return
    // 失败时保留原来的候选，手填地址不受影响。axios 超时没有响应，拦截器只会说"网络错误"，这里说清楚
    const timedOut = (error as { code?: string }).code === "ECONNABORTED"
    ElMessage.error(timedOut ? SURVEY_NOTES.candidatesRefreshTimeout(platformName.value) : (error as Error).message)
  } finally {
    if (seq === refreshSeq) candidateSource.refreshing = false
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
    // 实时清单里没有这个仓库（当时它被忽略了），继续标成"与下一轮一致"就不对了；
    // 退回运行记录，说明文案随之变成"可能过时"，要最新的再点一次刷新
    refreshSeq++
    Object.assign(candidateSource, { kind: "run", refreshing: false, errors: [] })
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
          <el-select
            v-model="repoDialog.url"
            filterable allow-create default-first-option clearable
            placeholder="从候选里选，或填仓库地址 / 组织/仓库路径"
            style="width: 100%"
          >
            <el-option v-for="c in candidates" :key="c.repo_slug" :label="c.url" :value="c.url" />
          </el-select>
          <div class="candidate-source">
            <span class="muted">
              <template v-if="candidateSource.kind === 'live'">{{ SURVEY_NOTES.candidatesLive(formatTime(candidateSource.fetchedAt)) }}</template>
              <template v-else-if="candidateSource.runStartedAt">
                {{ SURVEY_NOTES.candidatesFromRun(formatTime(candidateSource.runStartedAt), platformName) }}
              </template>
              <template v-else>{{ SURVEY_NOTES.candidatesNoRun }}</template>
            </span>
            <el-tooltip :content="SURVEY_NOTES.candidatesRefreshTip(platformName)" placement="top">
              <el-button link type="primary" size="small" :loading="candidateSource.refreshing" :disabled="repoDialog.saving" @click="refreshCandidates">
                按当前配置刷新
              </el-button>
            </el-tooltip>
          </div>
          <el-alert
            v-if="candidateSource.errors.length" class="candidate-errors" type="warning" :closable="false" show-icon
            :title="SURVEY_NOTES.candidatesRefreshFailed(candidateSource.errors.length)"
          >
            <div v-for="e in candidateSource.errors" :key="e.source">
              <code>{{ e.source }}</code>：{{ e.error }}
            </div>
          </el-alert>
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
        <el-button type="primary" :loading="repoDialog.saving" :disabled="candidateSource.refreshing" @click="addRepo">
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
.candidate-source {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 8px;
  width: 100%;
  margin-top: 4px;
  font-size: 12px;
  line-height: 1.6;
}
.candidate-errors {
  margin-top: 6px;
}
.mr {
  margin-right: 4px;
}
</style>

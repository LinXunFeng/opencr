<script lang="ts" setup>
import type { Survey, SurveySource } from "@@/apis/opencr"
import {
  clearSurveyWorkspaceApi,
  createSurveyApi,
  deleteSurveyApi,
  getSkillsApi,
  getSurveysApi,
  runSurveyApi,
  updateSurveyApi
} from "@@/apis/opencr"
import { SCHEDULE_KIND_LABEL, SURVEY_NOTES, WEEKDAY_OPTIONS } from "@@/constants/opencr"
import { useUserStore } from "@/pinia/stores/user"

const userStore = useUserStore()
const loading = ref(true)
const surveys = ref<Survey[]>([])
const allSkills = ref<string[]>([])

const dialogVisible = ref(false)
const saving = ref(false)
const editingUid = ref("")

/**
 * 表单里用「勾选集」，落库用「排除集」。
 *
 * 存排除集是为了让新增的 skill 自动进入所有巡检的候选池；
 * 但界面上让用户去勾"不要哪些"是反直觉的，所以在这里做一次翻转。
 */
const form = reactive({
  name: "",
  enabled: true,
  schedule_kind: "weekly" as Survey["schedule_kind"],
  weekday: 1,
  monthday: 1,
  time: "09:00",
  cron: "0 9 * * 1",
  timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC",
  selected_skills: [] as string[],
  delete_workspace_after: false,
  retention_runs: 20,
  budget_wall_clock_minutes: null as number | null,
  budget_l2_max_focus: null as number | null,
  sources: [] as SurveySource[]
})

function formatBytes(bytes?: number) {
  if (!bytes) return "—"
  const units = ["B", "KB", "MB", "GB", "TB"]
  let value = bytes
  let i = 0
  while (value >= 1024 && i < units.length - 1) {
    value /= 1024
    i++
  }
  return `${value.toFixed(value >= 10 || i === 0 ? 0 : 1)} ${units[i]}`
}

function formatTime(value: string) {
  return value ? new Date(value).toLocaleString() : "—"
}

async function load() {
  loading.value = true
  try {
    const [list, skills] = await Promise.all([getSurveysApi(), getSkillsApi(30)])
    surveys.value = list.items
    allSkills.value = (skills.items ?? []).map((s: any) => s.name).filter(Boolean)
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    loading.value = false
  }
}

function resetForm() {
  form.name = ""
  form.enabled = true
  form.schedule_kind = "weekly"
  form.weekday = 1
  form.monthday = 1
  form.time = "09:00"
  form.cron = "0 9 * * 1"
  form.timezone = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC"
  // 默认全部勾选
  form.selected_skills = [...allSkills.value]
  form.delete_workspace_after = false
  form.retention_runs = 20
  form.budget_wall_clock_minutes = null
  form.budget_l2_max_focus = null
  form.sources = [{ kind: "repo", url: "", branch: "", exclude_patterns: [] }]
}

function openCreate() {
  editingUid.value = ""
  resetForm()
  dialogVisible.value = true
}

function openEdit(row: any) {
  editingUid.value = row.survey_uid
  resetForm()
  form.name = row.name
  form.enabled = row.enabled
  form.schedule_kind = row.schedule_kind
  form.timezone = row.timezone
  const parts = (row.schedule_expr || "").split(" ")
  if (row.schedule_kind === "daily") {
    form.time = row.schedule_expr
  } else if (row.schedule_kind === "weekly") {
    form.weekday = Number(parts[0]) || 1
    form.time = parts[1] || "09:00"
  } else if (row.schedule_kind === "monthly") {
    form.monthday = Number(parts[0]) || 1
    form.time = parts[1] || "09:00"
  } else {
    form.cron = row.schedule_expr
  }
  form.selected_skills = allSkills.value.filter(s => !row.excluded_skills.includes(s))
  form.delete_workspace_after = row.delete_workspace_after
  form.retention_runs = row.retention_runs
  form.budget_wall_clock_minutes = row.budget_wall_clock_minutes
  form.budget_l2_max_focus = row.budget_l2_max_focus
  form.sources = row.sources.length
    ? row.sources.map((s: SurveySource) => ({ ...s, exclude_patterns: [...s.exclude_patterns] }))
    : [{ kind: "repo", url: "", branch: "", exclude_patterns: [] }]
  dialogVisible.value = true
}

const scheduleExpr = computed(() => {
  switch (form.schedule_kind) {
    case "daily": return form.time
    case "weekly": return `${form.weekday} ${form.time}`
    case "monthly": return `${form.monthday} ${form.time}`
    default: return form.cron
  }
})

function addSource() {
  form.sources.push({ kind: "repo", url: "", branch: "", exclude_patterns: [] })
}

function removeSource(index: number) {
  form.sources.splice(index, 1)
}

async function save() {
  if (!form.name.trim()) {
    ElMessage.warning("请填写巡检名称")
    return
  }
  const sources = form.sources.filter(s => s.url.trim())
  if (!sources.length) {
    ElMessage.warning("请至少填写一条仓库来源")
    return
  }

  saving.value = true
  try {
    const payload = {
      name: form.name.trim(),
      enabled: form.enabled,
      schedule_kind: form.schedule_kind,
      schedule_expr: scheduleExpr.value,
      timezone: form.timezone,
      // 界面收勾选集，落库存排除集
      excluded_skills: allSkills.value.filter(s => !form.selected_skills.includes(s)),
      delete_workspace_after: form.delete_workspace_after,
      retention_runs: form.retention_runs,
      budget_wall_clock_minutes: form.budget_wall_clock_minutes,
      budget_l2_max_focus: form.budget_l2_max_focus,
      sources
    }
    if (editingUid.value) {
      await updateSurveyApi(editingUid.value, payload as any)
      ElMessage.success("已保存")
    } else {
      await createSurveyApi(payload as any)
      ElMessage.success("已创建")
    }
    dialogVisible.value = false
    await load()
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    saving.value = false
  }
}

async function runNow(row: any) {
  try {
    await runSurveyApi(row.survey_uid)
    ElMessage.success("已开始执行，可到「巡检记录」查看进度")
    await load()
  } catch (error) {
    ElMessage.error((error as Error).message)
  }
}

async function toggleEnabled(row: any, value: string | number | boolean) {
  try {
    await updateSurveyApi(row.survey_uid, { enabled: Boolean(value) })
    await load()
  } catch (error) {
    ElMessage.error((error as Error).message)
    await load()
  }
}

async function clearWorkspace(row: any) {
  try {
    await ElMessageBox.confirm(
      `将删除「${row.name}」的本地仓库副本（${formatBytes(row.workspace_bytes)}）。`
      + "不影响任何已产出的发现，但下次执行需要重新全量克隆。",
      "清理工作区",
      { type: "warning" }
    )
  } catch {
    return
  }
  try {
    await clearSurveyWorkspaceApi(row.survey_uid)
    ElMessage.success("工作区已清理")
    await load()
  } catch (error) {
    ElMessage.error((error as Error).message)
  }
}

async function remove(row: any) {
  try {
    await ElMessageBox.confirm(
      `将删除巡检「${row.name}」及其全部运行记录。${SURVEY_NOTES.workspaceKept}`,
      "删除巡检",
      { type: "warning" }
    )
  } catch {
    return
  }
  try {
    await deleteSurveyApi(row.survey_uid)
    ElMessage.success("已删除（本地工作区已保留）")
    await load()
  } catch (error) {
    ElMessage.error((error as Error).message)
  }
}

onMounted(load)
</script>

<template>
  <div v-loading="loading" class="app-container">
    <el-alert
      v-if="!userStore.surveyEnabled"
      class="mb"
      type="warning"
      :closable="false"
      show-icon
      title="巡检调度当前处于关闭状态（config.yaml 的 survey.enabled），已有配置不会自动触发，但仍可手动执行。"
    />

    <el-card shadow="never">
      <template #header>
        <div class="header">
          <span>巡检配置</span>
          <el-button v-if="userStore.isAdmin" type="primary" size="small" @click="openCreate">
            新建巡检
          </el-button>
        </div>
      </template>

      <el-table :data="surveys" size="small" empty-text="还没有配置任何巡检">
        <el-table-column prop="name" label="名称" min-width="160">
          <template #default="{ row }">
            <div>{{ row.name }}</div>
            <div class="sub">
              工作区目录 <code>{{ row.slug }}</code>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="周期" min-width="180">
          <template #default="{ row }">
            <div>{{ SCHEDULE_KIND_LABEL[row.schedule_kind] || row.schedule_kind }}</div>
            <div class="sub">
              {{ row.schedule_desc }}
            </div>
          </template>
        </el-table-column>
        <el-table-column label="来源" width="110">
          <template #default="{ row }">
            {{ row.sources.length }} 条
          </template>
        </el-table-column>
        <el-table-column label="下次执行" min-width="170">
          <template #default="{ row }">
            {{ formatTime(row.next_run_at) }}
          </template>
        </el-table-column>
        <el-table-column label="上次执行" min-width="170">
          <template #default="{ row }">
            {{ formatTime(row.last_run_at) }}
          </template>
        </el-table-column>
        <el-table-column label="工作区" width="110">
          <template #default="{ row }">
            {{ formatBytes(row.workspace_bytes) }}
          </template>
        </el-table-column>
        <el-table-column label="启用" width="90">
          <template #default="{ row }">
            <el-switch
              :model-value="row.enabled"
              :disabled="!userStore.isAdmin"
              size="small"
              @update:model-value="(v: string | number | boolean) => toggleEnabled(row, v)"
            />
          </template>
        </el-table-column>
        <el-table-column v-if="userStore.isAdmin" label="操作" width="260" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click="runNow(row)">
              立即执行
            </el-button>
            <el-button link type="primary" size="small" @click="openEdit(row)">
              编辑
            </el-button>
            <el-button link type="warning" size="small" @click="clearWorkspace(row)">
              清理工作区
            </el-button>
            <el-button link type="danger" size="small" @click="remove(row)">
              删除
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-dialog v-model="dialogVisible" :title="editingUid ? '编辑巡检' : '新建巡检'" width="720px" top="6vh">
      <el-form label-width="110px" size="small">
        <el-form-item label="名称">
          <el-input v-model="form.name" placeholder="例如：移动端周巡检" />
        </el-form-item>

        <el-form-item label="执行周期">
          <div class="schedule-row">
            <el-select v-model="form.schedule_kind" style="width: 140px">
              <el-option v-for="(label, key) in SCHEDULE_KIND_LABEL" :key="key" :label="label" :value="key" />
            </el-select>
            <el-select v-if="form.schedule_kind === 'weekly'" v-model="form.weekday" style="width: 100px">
              <el-option v-for="d in WEEKDAY_OPTIONS" :key="d.value" :label="d.label" :value="d.value" />
            </el-select>
            <el-input-number
              v-if="form.schedule_kind === 'monthly'"
              v-model="form.monthday" :min="1" :max="31" controls-position="right" style="width: 110px"
            />
            <el-time-picker
              v-if="form.schedule_kind !== 'cron'"
              v-model="form.time" format="HH:mm" value-format="HH:mm" style="width: 130px"
            />
            <el-input v-if="form.schedule_kind === 'cron'" v-model="form.cron" placeholder="0 9 * * 1" style="width: 200px" />
          </div>
        </el-form-item>

        <el-form-item label="时区">
          <el-input v-model="form.timezone" placeholder="Asia/Shanghai" />
          <div class="sub">
            容器默认 UTC。不填对时区会让「每周一 9 点」在部署后变成别的时间。
          </div>
        </el-form-item>

        <el-form-item label="仓库来源">
          <div class="sources">
            <div v-for="(source, index) in form.sources" :key="index" class="source-row">
              <el-select v-model="source.kind" style="width: 100px">
                <el-option label="仓库" value="repo" />
                <el-option label="组织" value="org" />
              </el-select>
              <el-input v-model="source.url" :placeholder="source.kind === 'org' ? '组织路径或地址' : '仓库地址'" />
              <el-input
                v-if="source.kind === 'repo'"
                v-model="source.branch" placeholder="分支（留空=默认）" style="width: 170px"
              />
              <el-select
                v-else
                v-model="source.exclude_patterns"
                multiple filterable allow-create default-first-option
                placeholder="排除模式" style="width: 170px"
              >
                <el-option v-for="p in source.exclude_patterns" :key="p" :label="p" :value="p" />
              </el-select>
              <el-button link type="danger" @click="removeSource(index)">
                移除
              </el-button>
            </div>
            <el-button link type="primary" @click="addSource">
              + 添加来源
            </el-button>
            <div class="sub">
              组织在**每次执行时实时展开**，因此往组织里新增的仓库会自动纳入；排除模式用于挡掉不该扫的大仓库。
            </div>
          </div>
        </el-form-item>

        <el-form-item label="参与分析的 Skill">
          <el-checkbox-group v-model="form.selected_skills">
            <el-checkbox v-for="name in allSkills" :key="name" :value="name" :label="name" />
          </el-checkbox-group>
          <div class="sub">
            {{ SURVEY_NOTES.candidatePool }}
          </div>
        </el-form-item>

        <el-form-item label="巡检后删除工作区">
          <el-switch v-model="form.delete_workspace_after" />
          <div class="sub">
            默认不删。保留下来下次可以增量拉取；删掉意味着下次全量克隆，耗时会从分钟级涨到小时级。
          </div>
        </el-form-item>

        <el-form-item label="保留运行次数">
          <el-input-number v-model="form.retention_runs" :min="1" :max="500" controls-position="right" />
          <div class="sub">
            最近一次永远保留——「新增/仍存在」的比对依赖它。
          </div>
        </el-form-item>

        <el-form-item label="预算覆盖">
          <div class="schedule-row">
            <el-input-number
              v-model="form.budget_wall_clock_minutes" :min="1" :max="1440"
              placeholder="墙钟(分)" controls-position="right" style="width: 160px"
            />
            <el-input-number
              v-model="form.budget_l2_max_focus" :min="1" :max="200"
              placeholder="关注点上限" controls-position="right" style="width: 160px"
            />
          </div>
          <div class="sub">
            留空表示跟随 config.yaml 的全局默认。
          </div>
        </el-form-item>

        <el-form-item label="启用">
          <el-switch v-model="form.enabled" />
        </el-form-item>
      </el-form>

      <template #footer>
        <el-button size="small" @click="dialogVisible = false">
          取消
        </el-button>
        <el-button size="small" type="primary" :loading="saving" @click="save">
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

.sub {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  line-height: 1.6;
}

.schedule-row {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}

.sources {
  width: 100%;
}

.source-row {
  display: flex;
  gap: 8px;
  align-items: center;
  margin-bottom: 8px;
}
</style>

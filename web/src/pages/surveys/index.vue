<script lang="ts" setup>
import type { DestinationCheckResult, DestinationItem, Survey, SurveyBinding, SurveySource } from "@@/apis/opencr"
import type { SvgName } from "~virtual/svg-component"
import {
  checkDestinationApi,
  clearSurveyWorkspaceApi,
  createSurveyApi,
  deleteSurveyApi,
  getDestinationsApi,
  getSkillsApi,
  getSurveysApi,
  runSurveyApi,
  updateSurveyApi
} from "@@/apis/opencr"
import {
  FIX_MIN_SEVERITY_OPTIONS,
  FIX_MODE_LABEL,
  FIX_MR_TITLE_TEMPLATE_MAX_CHARS,
  FIX_NOTES,
  FIX_PROMPT_MAX_CHARS,
  formatTime,
  SCHEDULE_KIND_LABEL,
  SURVEY_NOTES,
  WEEKDAY_OPTIONS
} from "@@/constants/opencr"
import { Link } from "@element-plus/icons-vue"
import { useUserStore } from "@/pinia/stores/user"

const userStore = useUserStore()
const router = useRouter()
const loading = ref(true)
const surveys = ref<Survey[]>([])
const allSkills = ref<string[]>([])
/** config.yaml 里配置的输出目标实例。接口只对 Admin 开放，Guest 不请求 */
const destinations = ref<DestinationItem[]>([])

/** 表单里的 Binding：target 一律是对象，模板里才能直接按字段双向绑定 */
interface BindingForm {
  destination: string
  target: Record<string, string>
  /** 以下两项只在界面上使用，保存时不提交 */
  checking?: boolean
  check?: DestinationCheckResult | null
}

const dialogVisible = ref(false)
const saving = ref(false)
const editingUid = ref("")

/** 正在拷贝的源巡检；非空时对话框是「拷贝巡检」，保存走新建 */
const copySource = ref<Survey | null>(null)
/** 是否连同忽略清单一起拷贝。默认拷贝：按月新建的巡检不带上它，上个月说过不再提醒的问题会全部重新冒出来 */
const copyIgnores = ref(true)
/** 拷贝时按源巡检调整过的地方，在对话框顶部逐条说明，免得 Admin 以为是原样复制 */
const copyAdjustments = ref<string[]>([])

/** 对话框标题：编辑、拷贝与新建三种模式 */
const dialogTitle = computed(() => {
  if (editingUid.value) return "编辑巡检"
  return copySource.value ? `拷贝巡检：${copySource.value.name}` : "新建巡检"
})

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
  fix_mode: "manual" as Survey["fix_mode"],
  fix_min_severity: "warning" as Survey["fix_min_severity"],
  fix_prompt: "",
  fix_mr_title_template: "",
  budget_wall_clock_minutes: null as number | null,
  budget_l2_max_focus: null as number | null,
  // 以下两项不在表单上展示，只为原样带回：新建接口没收到的字段一律落成全局默认，拷贝时不带上就会悄悄丢掉源巡检的设置
  budget_l1_max_chars: null as number | null,
  budget_index_timeout_seconds: null as number | null,
  sources: [] as SurveySource[],
  bindings: [] as BindingForm[]
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

async function load() {
  loading.value = true
  try {
    const [list, skills, targets] = await Promise.all([
      getSurveysApi(),
      getSkillsApi(30),
      userStore.isAdmin ? getDestinationsApi() : Promise.resolve({ items: [] as DestinationItem[] })
    ])
    surveys.value = list.items
    allSkills.value = (skills.items ?? []).map((s: any) => s.name).filter(Boolean)
    destinations.value = targets.items
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
  form.fix_mode = "manual"
  form.fix_min_severity = "warning"
  form.fix_prompt = ""
  form.fix_mr_title_template = ""
  form.budget_wall_clock_minutes = null
  form.budget_l2_max_focus = null
  form.budget_l1_max_chars = null
  form.budget_index_timeout_seconds = null
  form.sources = [{ kind: "repo", url: "", branch: "", exclude_patterns: [] }]
  form.bindings = []
}

/** 打开空白的新建表单 */
function openCreate() {
  editingUid.value = ""
  copySource.value = null
  resetForm()
  dialogVisible.value = true
}

/** 把一个巡检的配置填进表单。编辑与拷贝共用，拷贝在此之上再做调整 */
function fillForm(row: any) {
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
  form.fix_mode = row.fix_mode
  form.fix_min_severity = row.fix_min_severity
  form.fix_prompt = row.fix_prompt ?? ""
  form.fix_mr_title_template = row.fix_mr_title_template ?? ""
  form.budget_wall_clock_minutes = row.budget_wall_clock_minutes
  form.budget_l2_max_focus = row.budget_l2_max_focus
  form.budget_l1_max_chars = row.budget_l1_max_chars ?? null
  form.budget_index_timeout_seconds = row.budget_index_timeout_seconds ?? null
  form.sources = row.sources.length
    ? row.sources.map((s: SurveySource) => ({ ...s, exclude_patterns: [...s.exclude_patterns] }))
    : [{ kind: "repo", url: "", branch: "", exclude_patterns: [] }]
  form.bindings = (row.bindings ?? []).map((b: SurveyBinding) => ({
    destination: b.destination,
    target: { ...(b.target ?? {}) }
  }))
}

/** 打开编辑表单，保存时按 survey_uid 更新原巡检 */
function openEdit(row: any) {
  editingUid.value = row.survey_uid
  copySource.value = null
  fillForm(row)
  dialogVisible.value = true
}

/**
 * 以一个巡检的配置为底稿新建巡检，应对「每月一份巡检报告」这类要反复建同样配置的场景。
 *
 * 只预填表单，保存仍走新建接口：服务端照常校验，Admin 也能在保存前改掉不该照搬的地方。
 */
function openCopy(row: any) {
  editingUid.value = ""
  copySource.value = row
  copyIgnores.value = true
  fillForm(row)
  form.name = `${row.name} 副本`
  const adjustments: string[] = []

  // 修复记录按巡检登记，不随拷贝过去：照搬自动发起的话，新巡检第一轮就会把提过合并请求的问题再提一遍
  if (form.fix_mode !== "manual") {
    form.fix_mode = "manual"
    adjustments.push(SURVEY_NOTES.copyFixModeReset)
  }

  // 新建时服务端不接受指向不可用实例的绑定，带过去只会让保存失败
  const kept: BindingForm[] = []
  const dropped: string[] = []
  const cleared: string[] = []
  for (const binding of form.bindings) {
    const destination = destinationOf(binding.destination)
    if (!destination || destination.error) {
      dropped.push(binding.destination)
      continue
    }
    // 落库存的是留空时取到的名称，原样带过去，新巡检就会写进源巡检的那张工作表。
    // 一律清空而不是只清"等于源巡检名称"的：源巡检改过名时落库的仍是旧名，比对不上就会漏清；
    // 代价是有意共用一张工作表的要重填一次，所以把原值写进提示里
    for (const field of destination.target_fields) {
      const value = (binding.target[field.key] ?? "").trim()
      if (field.follows_survey_name && value) {
        cleared.push(`${binding.destination} 的${field.label}「${value}」`)
        binding.target[field.key] = ""
      }
    }
    kept.push(binding)
  }
  form.bindings = kept
  if (dropped.length) adjustments.push(SURVEY_NOTES.copyBindingsDropped(dropped))
  if (cleared.length) adjustments.push(SURVEY_NOTES.copyWorksheetFollowsName(cleared))

  copyAdjustments.value = adjustments
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

function destinationOf(name: string) {
  return destinations.value.find(d => d.name === name)
}

function destinationLabel(d: DestinationItem) {
  return [d.type_label || d.type, d.summary].filter(Boolean).join(" · ")
}

function addBinding() {
  const first = destinations.value.find(d => !d.error)
  form.bindings.push({ destination: first?.name ?? "", target: {} })
}

function removeBinding(index: number) {
  form.bindings.splice(index, 1)
}

/** 换了实例就清空目标位置：不同类型的字段含义不同，沿用旧值只会得到一个看起来合法的错误位置 */
function onDestinationChange(binding: BindingForm) {
  binding.target = {}
  binding.check = null
}

/** 测试结果对应的是点击那一刻的填写内容，改了之后旧结论就不再成立，直接清掉免得误读 */
function onTargetInput(binding: BindingForm) {
  binding.check = null
}

async function checkBinding(binding: BindingForm) {
  binding.checking = true
  binding.check = null
  try {
    binding.check = await checkDestinationApi(binding.destination, binding.target, form.name.trim())
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    binding.checking = false
  }
}

/**
 * 各 Destination 类型在列表里的图标（preserve-color 目录下的 SvgIcon 名）。
 * 没有登记的类型、以及实例已从 config.yaml 删除而查不到类型的绑定，退回通用链接图标。
 */
const DESTINATION_ICON: Record<string, SvgName> = {
  google_sheet: "preserve-color/google-sheet"
}

function destinationIcon(binding: SurveyBinding): SvgName | undefined {
  return DESTINATION_ICON[binding.destination_type ?? ""]
}

const CHECK_ALERT_TYPE = { ok: "success", warn: "warning", error: "error" } as const

/** 实例已从 config.yaml 删除时，原样展示旧的目标位置，保存时原样提交 */
function describeTarget(target: Record<string, string>) {
  return Object.entries(target).map(([k, v]) => `${k}: ${v}`).join(" / ") || "—"
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
      fix_mode: form.fix_mode,
      fix_min_severity: form.fix_min_severity,
      fix_prompt: form.fix_prompt,
      fix_mr_title_template: form.fix_mr_title_template,
      budget_wall_clock_minutes: form.budget_wall_clock_minutes,
      budget_l2_max_focus: form.budget_l2_max_focus,
      budget_l1_max_chars: form.budget_l1_max_chars,
      budget_index_timeout_seconds: form.budget_index_timeout_seconds,
      sources,
      bindings: form.bindings
        .filter(b => b.destination)
        .map(b => ({ destination: b.destination, target: b.target }))
    }
    if (editingUid.value) {
      await updateSurveyApi(editingUid.value, payload as any)
      ElMessage.success("已保存")
    } else {
      await createSurveyApi({
        ...payload,
        copy_ignores_from: copySource.value && copyIgnores.value ? copySource.value.survey_uid : undefined
      } as any)
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

function openIgnores(row: any) {
  router.push({ name: "SurveyIgnores", params: { surveyUid: row.survey_uid } })
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
        <el-table-column label="输出目标" min-width="140">
          <template #default="{ row }">
            <el-tooltip v-for="b in row.bindings" :key="b.id" placement="top">
              <template #content>
                <div>{{ [b.destination, b.destination_label].filter(Boolean).join(" · ") }}</div>
                <div v-if="b.target_desc">
                  {{ b.target_desc }}
                </div>
                <div v-if="!b.available">
                  config.yaml 中已没有这个实例或配置有误，推送会失败
                </div>
                <div v-if="userStore.isAdmin">
                  上次成功推送：{{ formatTime(b.last_succeeded_push_at) }}
                </div>
                <div v-if="b.target_url">
                  点击在新标签页打开
                </div>
              </template>
              <!-- 没有链接时不给 href：Guest 拿不到链接（服务端剔除），没有链接的类型也不该看起来能点 -->
              <a
                :href="b.target_url || undefined"
                target="_blank"
                rel="noopener noreferrer"
                class="binding-icon"
                :class="{ clickable: !!b.target_url, unavailable: !b.available }"
              >
                <SvgIcon v-if="destinationIcon(b)" :name="destinationIcon(b)!" />
                <el-icon v-else><Link /></el-icon>
                <!-- 同一类型可以绑多个实例，只看图标分不出是哪个 -->
                <span class="binding-name">{{ b.destination }}</span>
              </a>
            </el-tooltip>
            <span v-if="!row.bindings.length" class="sub">—</span>
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
        <el-table-column v-if="userStore.isAdmin" label="操作" width="370" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click="runNow(row)">
              立即执行
            </el-button>
            <el-button link type="primary" size="small" @click="openEdit(row)">
              编辑
            </el-button>
            <el-button link type="primary" size="small" @click="openCopy(row)">
              拷贝
            </el-button>
            <el-button link type="primary" size="small" @click="openIgnores(row)">
              忽略清单
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

    <el-dialog v-model="dialogVisible" :title="dialogTitle" width="720px" top="6vh">
      <el-alert v-if="copySource" type="info" :closable="false" class="mb">
        <div>{{ SURVEY_NOTES.copyScope }}</div>
        <div v-for="(item, i) in copyAdjustments" :key="i" class="copy-adjustment">
          {{ item }}
        </div>
      </el-alert>
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
              组织在**每次执行时实时展开**，因此往组织里新增的仓库会自动纳入；排除模式按规则挡掉一类仓库，个别不维护、不重要的仓库可以在「忽略清单」里点名剔除。组织展开目前只支持 GitLab 的 group，其他平台请逐个填写仓库地址。
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

        <el-form-item label="输出目标">
          <div class="sources">
            <div v-for="(binding, index) in form.bindings" :key="index" class="binding">
              <div class="source-row">
                <el-select
                  v-model="binding.destination" placeholder="选择输出目标" style="width: 260px"
                  @change="onDestinationChange(binding)"
                >
                  <el-option
                    v-for="d in destinations" :key="d.name"
                    :label="`${d.name}（${destinationLabel(d)}）`" :value="d.name" :disabled="!!d.error"
                  >
                    <span>{{ d.name }}（{{ destinationLabel(d) }}）</span>
                    <span v-if="d.error" class="sub">{{ d.error }}</span>
                  </el-option>
                </el-select>
                <el-button
                  link type="primary" :loading="binding.checking"
                  :disabled="!destinationOf(binding.destination) || !!destinationOf(binding.destination)?.error"
                  @click="checkBinding(binding)"
                >
                  测试连通性
                </el-button>
                <el-button link type="danger" @click="removeBinding(index)">
                  移除
                </el-button>
              </div>
              <template v-if="destinationOf(binding.destination)">
                <div
                  v-for="field in destinationOf(binding.destination)!.target_fields" :key="field.key"
                  class="field-row"
                >
                  <span class="field-label">{{ field.label }}</span>
                  <div class="field-input">
                    <el-input
                      v-model="binding.target[field.key]" :placeholder="field.placeholder"
                      @input="onTargetInput(binding)"
                    />
                    <div v-if="field.help" class="sub">
                      {{ field.help }}
                    </div>
                  </div>
                </div>
              </template>
              <div v-if="binding.check" class="check-result">
                <el-alert
                  v-for="(item, i) in binding.check.items" :key="i"
                  :type="CHECK_ALERT_TYPE[item.level]" :title="item.title" :description="item.message"
                  :closable="false" show-icon class="check-item"
                />
                <div class="sub">
                  {{ binding.check.ok ? "测试通过。以上检查不会写入任何台账行。" : "测试未通过，按上面的提示处理后可以再测一次。" }}
                </div>
              </div>
              <div v-else-if="binding.destination && !destinationOf(binding.destination)" class="sub degraded">
                config.yaml 中已没有这个实例，绑定会原样保留，但推送会失败。原目标位置：{{ describeTarget(binding.target) }}
              </div>
            </div>
            <el-button link type="primary" :disabled="!destinations.length" @click="addBinding">
              + 添加输出目标
            </el-button>
            <div v-if="!destinations.length" class="sub">
              config.yaml 中还没有配置 destinations，写法见 config.example.yaml。
            </div>
            <div class="sub">
              {{ SURVEY_NOTES.destinationScope }}
            </div>
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

        <el-form-item label="修复触发方式">
          <el-radio-group v-model="form.fix_mode">
            <el-radio v-for="(label, value) in FIX_MODE_LABEL" :key="value" :value="value">
              {{ label }}
            </el-radio>
          </el-radio-group>
          <div class="sub">
            {{ FIX_NOTES.modeHelp[form.fix_mode] }}
          </div>
        </el-form-item>

        <el-form-item label="批量修复门槛">
          <el-select v-model="form.fix_min_severity" style="width: 200px">
            <el-option v-for="opt in FIX_MIN_SEVERITY_OPTIONS" :key="opt.value" :label="opt.label" :value="opt.value" />
          </el-select>
          <div class="sub">
            {{ FIX_NOTES.minSeverityHelp }}
          </div>
        </el-form-item>

        <el-form-item label="修复提示词">
          <el-input
            v-model="form.fix_prompt" type="textarea" :autosize="{ minRows: 2, maxRows: 8 }"
            :maxlength="FIX_PROMPT_MAX_CHARS" show-word-limit :placeholder="FIX_NOTES.promptPlaceholder"
          />
          <div class="sub">
            {{ FIX_NOTES.promptHelp }}
          </div>
        </el-form-item>

        <el-form-item label="合并请求标题">
          <el-input
            v-model="form.fix_mr_title_template" :maxlength="FIX_MR_TITLE_TEMPLATE_MAX_CHARS" show-word-limit
            :placeholder="FIX_NOTES.titleTemplatePlaceholder"
          />
          <div class="sub">
            {{ FIX_NOTES.titleTemplateHelp }}
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
            留空表示跟随 config.yaml 的全局默认。{{ SURVEY_NOTES.focusCapIncludesRecheck }}
          </div>
        </el-form-item>

        <el-form-item label="启用">
          <el-switch v-model="form.enabled" />
        </el-form-item>

        <el-form-item v-if="copySource" label="忽略清单">
          <el-checkbox v-model="copyIgnores">
            一并拷贝源巡检的忽略清单
          </el-checkbox>
          <div class="sub">
            {{ SURVEY_NOTES.copyIgnores }}
          </div>
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

.binding {
  padding: 8px 12px 4px;
  margin-bottom: 8px;
  border: 1px dashed var(--el-border-color);
  border-radius: 4px;
}

.field-row {
  display: flex;
  gap: 8px;
  align-items: flex-start;
  margin-bottom: 8px;
}

.field-label {
  flex: 0 0 56px;
  line-height: 24px;
  color: var(--el-text-color-regular);
}

.field-input {
  flex: 1;
}

.check-result {
  margin: 4px 0 8px;
}

.check-item {
  margin-bottom: 6px;
}

.binding-icon {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  margin: 0 10px 4px 0;
  font-size: 16px;
  vertical-align: middle;
  color: var(--el-text-color-secondary);
  text-decoration: none;
  cursor: default;

  &.clickable {
    cursor: pointer;
    color: var(--el-text-color-regular);

    &:hover .binding-name {
      color: var(--el-color-primary);
    }
  }

  // 实例不可用时褪色，与表单里「推送会失败」的提示同义
  &.unavailable {
    filter: grayscale(1);
    opacity: 0.45;
  }
}

.binding-name {
  font-size: 12px;
}

.degraded {
  color: var(--el-color-warning);
}

.copy-adjustment {
  margin-top: 6px;
  color: var(--el-color-warning);
}
</style>

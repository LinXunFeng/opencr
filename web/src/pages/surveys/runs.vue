<script lang="ts" setup>
import type { SurveyRun } from "@@/apis/opencr"
import { getSurveyRunsApi, getSurveysApi } from "@@/apis/opencr"
import {
  RUN_STATUS_LABEL,
  RUN_STATUS_TAG,
  SURVEY_DEGRADATION_LABEL,
  SURVEY_PHASE_LABEL,
  SURVEY_TRIGGER_LABEL
} from "@@/constants/opencr"

const router = useRouter()
const loading = ref(true)
const runs = ref<SurveyRun[]>([])
const surveyOptions = ref<Array<{ label: string, value: string }>>([])
const filterSurvey = ref("")

async function load() {
  loading.value = true
  try {
    const [runList, surveyList] = await Promise.all([
      getSurveyRunsApi({ survey_uid: filterSurvey.value || undefined, limit: 100 }),
      getSurveysApi()
    ])
    runs.value = runList.items
    surveyOptions.value = surveyList.items.map(s => ({ label: s.name, value: s.survey_uid }))
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    loading.value = false
  }
}

function formatTime(value: string) {
  return value ? new Date(value).toLocaleString() : "—"
}

/** 持续时长。进行中的用当前时间算，否则页面上会一直显示 0 秒 */
function duration(row: any) {
  if (!row.started_at) return "—"
  const end = row.finished_at ? new Date(row.finished_at) : new Date()
  const seconds = Math.max(Math.round((end.getTime() - new Date(row.started_at).getTime()) / 1000), 0)
  if (seconds < 60) return `${seconds} 秒`
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`
  return `${Math.floor(seconds / 3600)} 时 ${Math.floor((seconds % 3600) / 60)} 分`
}

function open(row: any) {
  router.push(`/surveys/runs/${row.run_uid}`)
}

watch(filterSurvey, load)
onMounted(load)
</script>

<template>
  <div v-loading="loading" class="app-container">
    <el-card shadow="never">
      <template #header>
        <div class="header">
          <span>巡检记录</span>
          <el-select v-model="filterSurvey" clearable placeholder="全部巡检" size="small" style="width: 200px">
            <el-option v-for="o in surveyOptions" :key="o.value" :label="o.label" :value="o.value" />
          </el-select>
        </div>
      </template>

      <el-table :data="runs" size="small" empty-text="还没有任何巡检运行记录" @row-click="open">
        <el-table-column prop="survey_name" label="巡检" min-width="150" />
        <el-table-column label="状态" width="130">
          <template #default="{ row }">
            <el-tag size="small" :type="RUN_STATUS_TAG[row.status] || 'info'">
              {{ RUN_STATUS_LABEL[row.status] || row.status }}
            </el-tag>
            <!-- Stale 只标注不改写状态：多进程下无法断言其它进程的运行已死 -->
            <el-tag v-if="row.is_stale" size="small" type="warning" effect="plain" class="ml">
              疑似中断
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="阶段" width="130">
          <template #default="{ row }">
            {{ SURVEY_PHASE_LABEL[row.phase] || row.phase || "—" }}
          </template>
        </el-table-column>
        <el-table-column label="仓库进度" width="110">
          <template #default="{ row }">
            {{ row.repos_done }} / {{ row.repos_total }}
          </template>
        </el-table-column>
        <el-table-column label="命中 Skill" min-width="150">
          <template #default="{ row }">
            <el-tag v-for="s in row.matched_skills" :key="s" size="small" effect="plain" class="mr">
              {{ s }}
            </el-tag>
            <span v-if="!row.matched_skills.length" class="sub">未命中</span>
          </template>
        </el-table-column>
        <el-table-column label="降级" min-width="160">
          <template #default="{ row }">
            <el-tooltip
              v-for="d in row.degradations" :key="d.kind"
              :content="SURVEY_DEGRADATION_LABEL[d.kind] || d.kind"
            >
              <el-tag size="small" type="warning" effect="plain" class="mr">
                {{ d.kind }} ×{{ d.count }}
              </el-tag>
            </el-tooltip>
            <span v-if="!row.degradations.length" class="sub">—</span>
          </template>
        </el-table-column>
        <el-table-column label="触发" width="100">
          <template #default="{ row }">
            {{ SURVEY_TRIGGER_LABEL[row.trigger] || row.trigger }}
          </template>
        </el-table-column>
        <el-table-column label="开始时间" min-width="170">
          <template #default="{ row }">
            {{ formatTime(row.started_at) }}
          </template>
        </el-table-column>
        <el-table-column label="耗时" width="110">
          <template #default="{ row }">
            {{ duration(row) }}
          </template>
        </el-table-column>
      </el-table>

      <el-alert
        class="mt"
        type="info"
        :closable="false"
        show-icon
        title="降级不代表失败：一次巡检可以既成功、又带有多条降级（例如某个仓库拉取失败被跳过）。"
      />
    </el-card>
  </div>
</template>

<style lang="scss" scoped>
.header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.sub {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.ml {
  margin-left: 4px;
}
.mr {
  margin-right: 4px;
}
.mt {
  margin-top: 16px;
}

:deep(.el-table__row) {
  cursor: pointer;
}
</style>

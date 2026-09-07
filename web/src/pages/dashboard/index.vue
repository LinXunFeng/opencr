<script lang="ts" setup>
import type { DashboardData } from "@@/apis/opencr"
import type { EChartsOption } from "echarts"
import { getDashboardApi } from "@@/apis/opencr"
import Chart from "@@/components/Chart/index.vue"
import {
  ACCEPTANCE_NOTE,
  COVERAGE_NOTE,
  formatPercent,
  formatTime,
  PHASE_LABEL,
  STALE_NOTE,
  VERDICT_COLOR,
  VERDICT_LABEL,
  VERDICT_ORDER
} from "@@/constants/opencr"

const router = useRouter()
const loading = ref(true)
const data = ref<DashboardData | null>(null)
let timer: number | undefined

async function load() {
  try {
    data.value = await getDashboardApi()
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    loading.value = false
  }
}

const cards = computed(() => {
  if (!data.value) return []
  const { errors, verdicts, active_runs } = data.value
  return [
    { label: "进行中", value: active_runs.length, tone: "primary" },
    { label: "近 7 天运行", value: errors.total_runs, tone: "info" },
    { label: "近 7 天失败", value: errors.failed, tone: errors.failed ? "danger" : "info" },
    { label: "含降级运行", value: errors.degraded_runs, tone: errors.degraded_runs ? "warning" : "info" },
    { label: "采纳率", value: formatPercent(verdicts.acceptance_rate), tone: "success" },
    { label: "覆盖率", value: formatPercent(verdicts.coverage_rate), tone: "info" }
  ]
})

/** 运行趋势：成功/失败/跳过堆叠 */
const trendOption = computed<EChartsOption>(() => {
  const trend = data.value?.trend ?? []
  return {
    tooltip: { trigger: "axis" },
    legend: { data: ["成功", "失败", "跳过"], bottom: 0 },
    grid: { left: 40, right: 16, top: 24, bottom: 44 },
    xAxis: { type: "category", data: trend.map(t => t.date.slice(5)) },
    yAxis: { type: "value", minInterval: 1 },
    series: [
      { name: "成功", type: "bar", stack: "t", itemStyle: { color: "#16a34a" }, data: trend.map(t => t.succeeded) },
      { name: "失败", type: "bar", stack: "t", itemStyle: { color: "#dc2626" }, data: trend.map(t => t.failed) },
      { name: "跳过", type: "bar", stack: "t", itemStyle: { color: "#cbd5e1" }, data: trend.map(t => t.skipped) }
    ]
  }
})

/** 采纳结论分布 */
const verdictOption = computed<EChartsOption>(() => {
  const by = data.value?.verdicts.by_verdict ?? {}
  const items = VERDICT_ORDER.filter(k => by[k]).map(k => ({
    name: VERDICT_LABEL[k] || k,
    value: by[k],
    itemStyle: { color: VERDICT_COLOR[k] }
  }))
  return {
    tooltip: { trigger: "item", formatter: "{b}: {c} ({d}%)" },
    legend: { bottom: 0, type: "scroll" },
    series: [{
      type: "pie",
      radius: ["45%", "70%"],
      center: ["50%", "44%"],
      label: { show: false },
      data: items
    }]
  }
})

function openRun(runUid: string) {
  router.push(`/runs/detail/${runUid}`)
}

onMounted(() => {
  load()
  // 进行中的审查需要近实时反馈，5 秒一轮；接口本身不做重计算
  timer = window.setInterval(load, 5000)
})

onBeforeUnmount(() => {
  if (timer) window.clearInterval(timer)
})
</script>

<template>
  <div v-loading="loading" class="app-container">
    <el-row :gutter="16">
      <el-col v-for="card in cards" :key="card.label" :xs="12" :sm="8" :md="4">
        <el-card shadow="never" class="stat-card">
          <div class="value" :class="`tone-${card.tone}`">
            {{ card.value }}
          </div>
          <div class="label">
            {{ card.label }}
          </div>
        </el-card>
      </el-col>
    </el-row>

    <el-card shadow="never" class="mt">
      <template #header>
        <div class="card-header">
          <span>进行中的审查</span>
          <el-tooltip :content="STALE_NOTE" placement="top">
            <el-tag size="small" type="info">
              口径说明
            </el-tag>
          </el-tooltip>
        </div>
      </template>
      <el-table :data="data?.active_runs ?? []" size="small" empty-text="当前没有审查在进行">
        <el-table-column label="项目" min-width="180">
          <template #default="{ row }">
            {{ row.project_path || row.project_id }}
          </template>
        </el-table-column>
        <el-table-column label="MR" width="90">
          <template #default="{ row }">
            <el-link type="primary" @click="openRun(row.run_uid)">
              !{{ row.mr_iid }}
            </el-link>
          </template>
        </el-table-column>
        <el-table-column prop="mr_title" label="标题" min-width="200" show-overflow-tooltip />
        <el-table-column label="阶段" width="120">
          <template #default="{ row }">
            {{ PHASE_LABEL[row.phase] || row.phase || "-" }}
          </template>
        </el-table-column>
        <el-table-column label="进度" width="180">
          <template #default="{ row }">
            <el-progress
              v-if="row.files_total"
              :percentage="Math.round((row.files_done / row.files_total) * 100)"
              :stroke-width="10"
            />
            <!-- overall 模式是单次模型调用，没有中间可观测点，因此不编造百分比 -->
            <span v-else class="muted">按阶段推进</span>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="110">
          <template #default="{ row }">
            <el-tag v-if="row.is_stale" type="warning" size="small">
              疑似中断
            </el-tag>
            <el-tag v-else type="primary" size="small">
              进行中
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="开始时间" width="180">
          <template #default="{ row }">
            {{ formatTime(row.started_at) }}
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-row :gutter="16" class="mt">
      <el-col :xs="24" :md="14">
        <el-card shadow="never">
          <template #header>
            近 30 天运行趋势
          </template>
          <Chart :option="trendOption" height="300px" />
        </el-card>
      </el-col>
      <el-col :xs="24" :md="10">
        <el-card shadow="never">
          <template #header>
            <div class="card-header">
              <span>审查发现采纳分布</span>
              <el-tooltip placement="top">
                <template #content>
                  <div style="max-width: 340px">
                    {{ ACCEPTANCE_NOTE }}<br><br>{{ COVERAGE_NOTE }}
                  </div>
                </template>
                <el-tag size="small" type="info">
                  口径说明
                </el-tag>
              </el-tooltip>
            </div>
          </template>
          <Chart :option="verdictOption" height="300px" />
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<style lang="scss" scoped>
.stat-card {
  margin-bottom: 16px;
  text-align: center;

  .value {
    font-size: 24px;
    font-weight: 600;
    line-height: 1.3;
  }

  .label {
    margin-top: 4px;
    font-size: 13px;
    color: var(--el-text-color-secondary);
  }

  .tone-danger {
    color: var(--el-color-danger);
  }
  .tone-warning {
    color: var(--el-color-warning);
  }
  .tone-success {
    color: var(--el-color-success);
  }
  .tone-primary {
    color: var(--el-color-primary);
  }
}

.mt {
  margin-top: 4px;
}

.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.muted {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
</style>

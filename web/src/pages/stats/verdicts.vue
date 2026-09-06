<script lang="ts" setup>
import type { VerdictStats } from "@@/apis/opencr"
import type { EChartsOption } from "echarts"
import { getVerdictStatsApi } from "@@/apis/opencr"
import Chart from "@@/components/Chart/index.vue"
import {
  ACCEPTANCE_NOTE,
  COVERAGE_NOTE,
  DELIVERY_LABEL,
  formatPercent,
  SEVERITY_LABEL,
  VERDICT_COLOR,
  VERDICT_LABEL,
  VERDICT_ORDER
} from "@@/constants/opencr"

const loading = ref(true)
const days = ref(30)
const stats = ref<VerdictStats | null>(null)

async function load() {
  loading.value = true
  try {
    stats.value = await getVerdictStatsApi(days.value)
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    loading.value = false
  }
}

const verdictOption = computed<EChartsOption>(() => {
  const by = stats.value?.by_verdict ?? {}
  const keys = VERDICT_ORDER.filter(k => by[k])
  return {
    tooltip: { trigger: "axis", axisPointer: { type: "shadow" } },
    grid: { left: 90, right: 24, top: 16, bottom: 32 },
    xAxis: { type: "value", minInterval: 1 },
    yAxis: { type: "category", data: keys.map(k => VERDICT_LABEL[k] || k) },
    series: [{
      type: "bar",
      barMaxWidth: 26,
      data: keys.map(k => ({ value: by[k], itemStyle: { color: VERDICT_COLOR[k] } }))
    }]
  }
})

const severityOption = computed<EChartsOption>(() => {
  const by = stats.value?.by_severity ?? {}
  const keys = Object.keys(by)
  return {
    tooltip: { trigger: "item", formatter: "{b}: {c} ({d}%)" },
    legend: { bottom: 0 },
    series: [{
      type: "pie",
      radius: ["42%", "68%"],
      center: ["50%", "44%"],
      label: { show: false },
      data: keys.map(k => ({ name: SEVERITY_LABEL[k] || k, value: by[k] }))
    }]
  }
})

onMounted(load)
</script>

<template>
  <div v-loading="loading" class="app-container">
    <el-card shadow="never" class="mb">
      <el-form inline @submit.prevent>
        <el-form-item label="时间窗">
          <el-select v-model="days" style="width: 120px" @change="load">
            <el-option label="近 7 天" :value="7" />
            <el-option label="近 30 天" :value="30" />
            <el-option label="近 90 天" :value="90" />
          </el-select>
        </el-form-item>
      </el-form>

      <el-row v-if="stats" :gutter="16">
        <el-col :xs="12" :md="4">
          <el-statistic title="审查发现总数" :value="stats.total_findings" />
        </el-col>
        <el-col :xs="12" :md="4">
          <el-statistic title="可追踪" :value="stats.trackable_findings" />
        </el-col>
        <el-col :xs="12" :md="4">
          <el-statistic title="已结算" :value="stats.settled_findings" />
        </el-col>
        <el-col :xs="12" :md="4">
          <el-statistic title="已采纳" :value="stats.accepted" />
        </el-col>
        <el-col :xs="12" :md="4">
          <div class="stat">
            <div class="title">
              采纳率
            </div>
            <div class="num accent">
              {{ formatPercent(stats.acceptance_rate) }}
            </div>
          </div>
        </el-col>
        <el-col :xs="12" :md="4">
          <div class="stat">
            <div class="title">
              覆盖率
            </div>
            <div class="num">
              {{ formatPercent(stats.coverage_rate) }}
            </div>
          </div>
        </el-col>
      </el-row>
    </el-card>

    <!-- 采纳率与覆盖率必须成对呈现：只报采纳率会让人误以为分母是全部产出 -->
    <el-alert type="warning" :closable="false" show-icon class="mb">
      <template #title>
        口径说明（请先读这段再看数字）
      </template>
      <p>{{ ACCEPTANCE_NOTE.replace(/\*\*/g, "") }}</p>
      <p>{{ COVERAGE_NOTE }}</p>
      <p v-if="stats">
        本时间窗内共产出 <b>{{ stats.total_findings }}</b> 条审查发现，其中
        <b>{{ stats.trackable_findings }}</b> 条可追踪；整体评论贡献了
        <b>{{ stats.summary_only_findings }}</b> 条不可追踪的发现。
      </p>
    </el-alert>

    <el-row :gutter="16">
      <el-col :xs="24" :md="14">
        <el-card shadow="never">
          <template #header>
            采纳结论分布
          </template>
          <Chart :option="verdictOption" height="320px" />
        </el-card>
      </el-col>
      <el-col :xs="24" :md="10">
        <el-card shadow="never">
          <template #header>
            严重度分布
          </template>
          <Chart :option="severityOption" height="320px" />
        </el-card>
      </el-col>
    </el-row>

    <el-card v-if="stats" shadow="never" class="mt">
      <template #header>
        按投递方式
      </template>
      <el-table :data="Object.entries(stats.by_delivery).map(([k, v]) => ({ k, v }))" size="small">
        <el-table-column label="投递方式">
          <template #default="{ row }">
            {{ DELIVERY_LABEL[row.k] || row.k }}
          </template>
        </el-table-column>
        <el-table-column label="数量" prop="v" width="120" />
        <el-table-column label="是否可追踪" width="140">
          <template #default="{ row }">
            <el-tag size="small" :type="['inline', 'file_level'].includes(row.k) ? 'success' : 'info'">
              {{ ['inline', 'file_level'].includes(row.k) ? "可追踪" : "不可追踪" }}
            </el-tag>
          </template>
        </el-table-column>
      </el-table>
    </el-card>
  </div>
</template>

<style lang="scss" scoped>
.mb {
  margin-bottom: 16px;
}
.mt {
  margin-top: 16px;
}

.stat {
  .title {
    font-size: 14px;
    color: var(--el-text-color-regular);
  }

  .num {
    font-size: 24px;
    font-weight: 600;
    line-height: 1.4;
  }

  .accent {
    color: var(--el-color-success);
  }
}
</style>

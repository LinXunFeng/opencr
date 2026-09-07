<script lang="ts" setup>
import type { ErrorStats } from "@@/apis/opencr"
import type { EChartsOption } from "echarts"
import { getErrorStatsApi } from "@@/apis/opencr"
import Chart from "@@/components/Chart/index.vue"
import { DEGRADATION_LABEL, ERROR_KIND_LABEL } from "@@/constants/opencr"

const loading = ref(true)
const days = ref(7)
const stats = ref<ErrorStats | null>(null)

async function load() {
  loading.value = true
  try {
    stats.value = await getErrorStatsApi(days.value)
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    loading.value = false
  }
}

const trendOption = computed<EChartsOption>(() => {
  const trend = stats.value?.trend ?? []
  return {
    tooltip: { trigger: "axis" },
    legend: { data: ["成功", "失败"], bottom: 0 },
    grid: { left: 40, right: 16, top: 20, bottom: 44 },
    xAxis: { type: "category", data: trend.map(t => t.date.slice(5)) },
    yAxis: { type: "value", minInterval: 1 },
    series: [
      { name: "成功", type: "line", smooth: true, itemStyle: { color: "#16a34a" }, data: trend.map(t => t.succeeded) },
      { name: "失败", type: "line", smooth: true, itemStyle: { color: "#dc2626" }, data: trend.map(t => t.failed) }
    ]
  }
})

const errorRows = computed(() =>
  Object.entries(stats.value?.by_error_kind ?? {}).map(([k, v]) => ({
    name: ERROR_KIND_LABEL[k] || k,
    count: v
  }))
)

const degradeRows = computed(() =>
  Object.entries(stats.value?.by_degradation ?? {}).map(([k, v]) => ({
    name: DEGRADATION_LABEL[k] || k,
    count: v
  }))
)

onMounted(load)
</script>

<template>
  <div v-loading="loading" class="app-container">
    <el-card shadow="never" class="mb">
      <el-form inline @submit.prevent>
        <el-form-item label="时间窗">
          <el-select v-model="days" style="width: 120px" @change="load">
            <el-option label="近 1 天" :value="1" />
            <el-option label="近 7 天" :value="7" />
            <el-option label="近 30 天" :value="30" />
          </el-select>
        </el-form-item>
      </el-form>

      <el-row v-if="stats" :gutter="16">
        <el-col :xs="12" :md="5">
          <el-statistic title="总运行" :value="stats.total_runs" />
        </el-col>
        <el-col :xs="12" :md="5">
          <el-statistic title="成功" :value="stats.succeeded" />
        </el-col>
        <el-col :xs="12" :md="5">
          <el-statistic title="失败" :value="stats.failed" />
        </el-col>
        <el-col :xs="12" :md="5">
          <el-statistic title="含降级" :value="stats.degraded_runs" />
        </el-col>
        <el-col :xs="12" :md="4">
          <el-statistic title="跳过" :value="stats.skipped" />
        </el-col>
      </el-row>
    </el-card>

    <!-- 失败 / 降级 / 跳过是三档不同的东西，混着看会得出错误结论 -->
    <el-alert type="info" :closable="false" show-icon class="mb">
      <template #title>
        三档的区别
      </template>
      <p><b>失败</b>：审查没跑完（模型或平台调用失败、未捕获异常）。</p>
      <p><b>降级</b>：流程走完了但质量受损——行内评论投递失败后已降级为普通评论（内容仍然送达用户）、diff 被截断。一次运行可以既成功又带降级。</p>
      <p><b>跳过</b>：Draft/WIP、dependabot、merge commit 触发的更新等。<b>这不是错误。</b></p>
    </el-alert>

    <el-card shadow="never" class="mb">
      <template #header>
        近 30 天成功/失败趋势
      </template>
      <Chart :option="trendOption" height="300px" />
    </el-card>

    <el-row :gutter="16">
      <el-col :xs="24" :md="12">
        <el-card shadow="never">
          <template #header>
            失败原因
          </template>
          <el-table :data="errorRows" size="small" empty-text="时间窗内没有失败">
            <el-table-column prop="name" label="类型" />
            <el-table-column prop="count" label="次数" width="100" />
          </el-table>
        </el-card>
      </el-col>
      <el-col :xs="24" :md="12">
        <el-card shadow="never">
          <template #header>
            降级明细
          </template>
          <el-table :data="degradeRows" size="small" empty-text="时间窗内没有降级">
            <el-table-column prop="name" label="类型" />
            <el-table-column prop="count" label="次数" width="100" />
          </el-table>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<style lang="scss" scoped>
.mb {
  margin-bottom: 16px;
}
</style>

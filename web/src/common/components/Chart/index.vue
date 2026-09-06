<script lang="ts" setup>
import type { EChartsOption } from "echarts"
import { BarChart, LineChart, PieChart } from "echarts/charts"
import {
  GridComponent,
  LegendComponent,
  TitleComponent,
  TooltipComponent
} from "echarts/components"
import { use } from "echarts/core"
import { CanvasRenderer } from "echarts/renderers"
import VChart from "vue-echarts"

defineProps<{
  option: EChartsOption
  height?: string
  loading?: boolean
}>()

/**
 * ECharts 封装。
 *
 * 按需引入而非 `import * as echarts`：全量约 376KB gzip，按需约 196KB。
 * 新增图表类型时记得在这里补 use()，否则运行时会静默画不出来。
 *
 * 注意：echarts 包里不含任何地图 GeoJSON，网上抄来的 fetch('.../china.json')
 * 在内网会静默失败。要画地理图必须自行提供 GeoJSON 并 registerMap()。
 */
use([
  CanvasRenderer,
  LineChart,
  BarChart,
  PieChart,
  GridComponent,
  TooltipComponent,
  LegendComponent,
  TitleComponent
])
</script>

<template>
  <VChart
    class="chart"
    :option="option"
    :loading="loading"
    :style="{ height: height || '320px' }"
    autoresize
  />
</template>

<style scoped>
.chart {
  width: 100%;
}
</style>

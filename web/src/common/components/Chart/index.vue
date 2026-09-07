<script lang="ts" setup>
import type { EChartsOption } from "echarts"
import { useTheme } from "@@/composables/useTheme"
import { getCssVar } from "@@/utils/css"
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

const { activeThemeName } = useTheme()

/** 从当前皮肤的 CSS 变量里取图表配色 */
function readTokens() {
  return {
    text: getCssVar("--el-text-color-regular") || "#606266",
    secondary: getCssVar("--el-text-color-secondary") || "#909399",
    line: getCssVar("--el-border-color-lighter") || "#ebeef5",
    surface: getCssVar("--el-bg-color-overlay") || "#ffffff",
    border: getCssVar("--el-border-color-light") || "#e4e7ed"
  }
}

const tokens = ref(readTokens())

// 主题 class 是在 initTheme 的 watchEffect（pre flush）里挂到 html 上的，
// 等一个 tick 再读，否则拿到的还是上一套皮肤的值
watch(activeThemeName, async () => {
  await nextTick()
  tokens.value = readTokens()
})

/**
 * ECharts 自带的默认色是按浅色底设计的（legend 文字甚至是 #333），
 * 深色皮肤下几乎看不见。这里按当前皮肤生成一份 theme 传给 init，
 * 页面自己在 option 里写的颜色仍然优先级更高。
 */
const chartTheme = computed(() => {
  const { text, secondary, line, surface, border } = tokens.value
  const axis = {
    axisLine: { lineStyle: { color: line } },
    axisTick: { lineStyle: { color: line } },
    axisLabel: { color: secondary },
    splitLine: { lineStyle: { color: line } }
  }
  return {
    textStyle: { color: text },
    legend: { textStyle: { color: secondary } },
    categoryAxis: axis,
    valueAxis: axis,
    logAxis: axis,
    timeAxis: axis,
    tooltip: {
      backgroundColor: surface,
      borderColor: border,
      textStyle: { color: text }
    }
  }
})

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
    :theme="chartTheme"
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

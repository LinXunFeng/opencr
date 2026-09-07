<script lang="ts" setup>
import { VERDICT_COLOR, VERDICT_LABEL } from "@@/constants/opencr"

/** 采纳结论标签：接收结论值，统一展示配色、图标与文案。 */
const props = defineProps<{ verdict: string }>()

const iconPaths: Record<string, string> = {
  accepted: "m5 12 4 4L19 6",
  rejected: "m6 6 12 12M18 6 6 18",
  dismissed: "M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0ZM9 9l6 6m0-6-6 6",
  ignored: "M5 12h14",
  undecided: "M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0ZM12 7v5l3 2",
  untrackable: "m8 16-1 1a3.5 3.5 0 0 1-5-5l3-3m11-1 1-1a3.5 3.5 0 0 1 5 5l-3 3M9 3v3M3 5l3 1m9 12v3m3-3 3 1M9 15l6-6"
}

/** 未知结论保留原值并使用中性样式，避免误显示为已知结论。 */
const color = computed(() => VERDICT_COLOR[props.verdict] || "var(--el-border-color)")
</script>

<template>
  <el-tag
    class="verdict-tag"
    size="small"
    effect="plain"
    :style="{ '--verdict-color': color }"
  >
    <span class="verdict-content">
      <svg
        v-if="iconPaths[verdict]"
        class="verdict-icon"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        stroke-width="2"
        stroke-linecap="round"
        stroke-linejoin="round"
        aria-hidden="true"
        focusable="false"
      >
        <path :d="iconPaths[verdict]" />
      </svg>
      {{ VERDICT_LABEL[verdict] || verdict }}
    </span>
  </el-tag>
</template>

<style scoped>
.verdict-tag {
  /* 图表配色用于底色和边框；文字跟随主题，浅灰结论在浅色背景上仍须可读。 */
  --el-tag-bg-color: color-mix(in srgb, var(--verdict-color) 12%, var(--el-bg-color));
  --el-tag-border-color: var(--verdict-color);
  --el-tag-text-color: var(--el-text-color-regular);
}

.verdict-content {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  white-space: nowrap;
}

.verdict-icon {
  width: 14px;
  height: 14px;
  flex-shrink: 0;
}
</style>

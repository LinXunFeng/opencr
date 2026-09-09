<script lang="ts" setup>
import type { SurveyFinding } from "@@/apis/opencr"
import { CATEGORY_LABEL, SEVERITY_LABEL, SEVERITY_TAG } from "@@/constants/opencr"

/**
 * 巡检发现列表。
 *
 * 「新增 / 仍存在 / 已消失」三个页签的表格结构完全一致，抽成组件避免三份拷贝逐渐漂移。
 * Guest 拿不到 title 与 body（服务端剔除，不是前端隐藏），这里显示占位而不是空白 ——
 * 空白会被误读成"这条发现没有内容"。
 */
defineProps<{
  items: SurveyFinding[]
  canIgnore?: boolean
}>()

const emit = defineEmits<{ ignore: [finding: SurveyFinding] }>()

// el-table 的插槽把 row 定为 DefaultRow，这里统一在边界处收窄，
// 与项目既有页面的写法保持一致（见 pages/runs/index.vue）
function location(row: any) {
  return `${row.repo_slug}/${row.file_path}${row.line ? `:${row.line}` : ""}`
}

function emitIgnore(row: any) {
  emit("ignore", row as SurveyFinding)
}
</script>

<template>
  <el-table :data="items" size="small" empty-text="没有条目">
    <el-table-column type="expand">
      <template #default="{ row }">
        <pre class="body">{{ row.body ?? "（需登录查看正文）" }}</pre>
      </template>
    </el-table-column>
    <el-table-column label="严重度" width="100">
      <template #default="{ row }">
        <el-tag size="small" :type="SEVERITY_TAG[row.severity] || 'info'">
          {{ SEVERITY_LABEL[row.severity] || row.severity }}
        </el-tag>
      </template>
    </el-table-column>
    <el-table-column label="类别" width="140">
      <template #default="{ row }">
        {{ CATEGORY_LABEL[row.category] || row.category }}
      </template>
    </el-table-column>
    <el-table-column label="位置" min-width="280" show-overflow-tooltip>
      <template #default="{ row }">
        <code>{{ location(row) }}</code>
      </template>
    </el-table-column>
    <el-table-column label="标题" min-width="240" show-overflow-tooltip>
      <template #default="{ row }">
        <span v-if="row.title">{{ row.title }}</span>
        <span v-else class="muted">（需登录查看）</span>
      </template>
    </el-table-column>
    <el-table-column v-if="canIgnore" label="操作" width="110">
      <template #default="{ row }">
        <el-button link type="info" size="small" @click="emitIgnore(row)">
          不再提醒
        </el-button>
      </template>
    </el-table-column>
  </el-table>
</template>

<style lang="scss" scoped>
.body {
  white-space: pre-wrap;
  margin: 0;
  padding: 8px 16px;
  font-family: inherit;
  line-height: 1.7;
}

.muted {
  color: var(--el-text-color-secondary);
}
</style>

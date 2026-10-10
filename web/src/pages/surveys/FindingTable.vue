<script lang="ts" setup>
import type { SurveyFinding } from "@@/apis/opencr"
import {
  CATEGORY_LABEL,
  CLUE_SOURCE_DESC,
  CLUE_SOURCE_LABEL,
  CLUE_SOURCE_TAG,
  SEVERITY_LABEL,
  SEVERITY_TAG,
  SURVEY_UNCHECKED_REPO_LABEL
} from "@@/constants/opencr"

/**
 * 巡检发现列表。
 *
 * 各个页签（新增、仍存在、已消失、本轮未复查、所在仓库已忽略）的表格结构完全一致，抽成组件避免多份拷贝逐渐漂移。
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

/** 旧数据没有线索来源，归到「无记录」而不是留空，筛选时才选得中它 */
function clueOf(row: any) {
  return (row as SurveyFinding).clue_source || "unknown"
}

const clueFilters = Object.entries(CLUE_SOURCE_LABEL).map(([value, text]) => ({ text, value }))

/** el-table 的筛选回调：行的线索来源是否等于选中的值 */
function filterClue(value: string, row: any) {
  return clueOf(row) === value
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
    <el-table-column label="线索来源" width="120" :filters="clueFilters" :filter-method="filterClue">
      <template #default="{ row }">
        <el-tooltip :content="CLUE_SOURCE_DESC[clueOf(row)]">
          <el-tag size="small" :type="CLUE_SOURCE_TAG[clueOf(row)]" effect="plain">
            {{ CLUE_SOURCE_LABEL[clueOf(row)] }}
          </el-tag>
        </el-tooltip>
      </template>
    </el-table-column>
    <el-table-column label="位置" min-width="280" show-overflow-tooltip>
      <template #default="{ row }">
        <code>{{ location(row) }}</code>
        <!-- 只有「本轮未复查」的条目带这个字段：仓库整个没拉下来和文件没轮到取证，处理方式不一样 -->
        <el-tag v-if="SURVEY_UNCHECKED_REPO_LABEL[row.repo_status]" size="small" type="warning" effect="plain" class="ml">
          {{ SURVEY_UNCHECKED_REPO_LABEL[row.repo_status] }}
        </el-tag>
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

.ml {
  margin-left: 6px;
}
</style>

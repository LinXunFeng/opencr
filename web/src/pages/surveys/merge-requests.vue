<script lang="ts" setup>
import type { FixMergeRequest, FixMergeRequestList } from "@@/apis/opencr"
import { getFixMergeRequestsApi, getSurveysApi, UNASSIGNED_FILTER } from "@@/apis/opencr"
import {
  ASSIGNEE_SOURCE_LABEL,
  FIX_NOTES,
  FIX_TRIGGER_LABEL,
  formatTime,
  SEVERITY_LABEL,
  SEVERITY_TAG
} from "@@/constants/opencr"

/**
 * 修复合并请求：跨巡检列出修复提出的合并请求与处理人，按巡检、处理人（可多选，含未指派）与关键字筛选。
 *
 * 原始作者与配置的仓库负责人都不可用时合并请求不指派，这里是把它们找出来分给人的入口。
 * 只列已提出合并请求的修复；失败与未修改的修复在各自的巡检报告里看。合并请求在平台上的状态（已合并、已关闭）不在这里，
 * 要逐个调平台接口，列表一翻页就是几十次请求。
 */
const router = useRouter()
const loading = ref(true)
const data = ref<FixMergeRequestList>({ items: [], total: 0, assignees: [], unassigned: 0 })
const surveyOptions = ref<Array<{ label: string, value: string }>>([])
const filters = reactive({
  survey: "",
  assignees: [] as string[],
  keyword: ""
})
const page = reactive({ current: 1, size: 20 })

/** 请求序号：快速切换筛选时，先发出、后返回的旧请求不能覆盖新结果 */
let requestSeq = 0

/** 按当前筛选与页码读取合并请求列表 */
async function load() {
  const seq = ++requestSeq
  loading.value = true
  try {
    const result = await getFixMergeRequestsApi({
      survey_uid: filters.survey || undefined,
      assignees: filters.assignees,
      q: filters.keyword.trim() || undefined,
      limit: page.size,
      offset: (page.current - 1) * page.size
    })
    if (seq === requestSeq) data.value = result
  } catch (error) {
    if (seq === requestSeq) ElMessage.error((error as Error).message)
  } finally {
    if (seq === requestSeq) loading.value = false
  }
}

/** 巡检筛选的选项 */
async function loadSurveys() {
  try {
    surveyOptions.value = (await getSurveysApi()).items.map(s => ({ label: s.name, value: s.survey_uid }))
  } catch (error) {
    ElMessage.error((error as Error).message)
  }
}

/**
 * 处理人筛选的选项：未指派放第一个，其余按合并请求数从多到少。
 * 已勾选、但在当前巡检与关键字下没有合并请求的人也保留，否则勾选框里会出现一个只显示用户名的"幽灵"标签。
 */
const assigneeOptions = computed(() => {
  const options = [
    { value: UNASSIGNED_FILTER, label: `未指派（${data.value.unassigned}）` },
    ...data.value.assignees.map(a => ({
      value: a.username,
      label: `@${a.username}${a.name ? ` ${a.name}` : ""}（${a.count}）`
    }))
  ]
  for (const selected of filters.assignees) {
    if (!options.some(o => o.value === selected)) options.push({ value: selected, label: `@${selected}（0）` })
  }
  return options
})

/** 筛选条件变了回到第一页 */
function search() {
  page.current = 1
  load()
}

/** 打开修复详情。row 来自 el-table 插槽时类型是 DefaultRow，在这里收窄 */
function openDetail(raw: any) {
  router.push(`/surveys/fixes/${(raw as FixMergeRequest).run_uid}`)
}

/** 问题位置：仓库/文件:行 */
function location(raw: any) {
  const row = raw as FixMergeRequest
  return `${row.repo_slug}/${row.file_path}${row.line ? `:${row.line}` : ""}`
}

watch(() => [filters.survey, filters.assignees], search, { deep: true })
onMounted(() => {
  loadSurveys()
  load()
})
</script>

<template>
  <div v-loading="loading" class="app-container">
    <el-card shadow="never">
      <template #header>
        <div class="header">
          <span>修复合并请求</span>
          <div class="filters">
            <el-select v-model="filters.survey" clearable placeholder="全部巡检" size="small" style="width: 180px">
              <el-option v-for="o in surveyOptions" :key="o.value" :label="o.label" :value="o.value" />
            </el-select>
            <el-select
              v-model="filters.assignees" multiple collapse-tags collapse-tags-tooltip clearable filterable
              placeholder="全部处理人" size="small" style="width: 260px"
            >
              <el-option v-for="o in assigneeOptions" :key="o.value" :label="o.label" :value="o.value" />
            </el-select>
            <el-input
              v-model="filters.keyword" clearable placeholder="标题、仓库或文件，回车搜索" size="small" style="width: 220px"
              @keyup.enter="search" @clear="search"
            />
          </div>
        </div>
      </template>

      <el-alert class="mb" type="info" :closable="false" show-icon :title="FIX_NOTES.mergeRequestsPage" />

      <el-table :data="data.items" size="small" empty-text="没有符合条件的合并请求">
        <el-table-column label="问题" min-width="260">
          <template #default="{ row }">
            <el-tag size="small" :type="SEVERITY_TAG[row.severity] || 'info'" class="mr">
              {{ SEVERITY_LABEL[row.severity] || row.severity }}
            </el-tag>
            {{ row.title }}
            <div class="sub">
              <code>{{ location(row) }}</code>
            </div>
          </template>
        </el-table-column>
        <el-table-column prop="survey_name" label="巡检" min-width="140" show-overflow-tooltip />
        <el-table-column label="处理人" min-width="200">
          <template #default="{ row }">
            <template v-if="row.assignee_username">
              @{{ row.assignee_username }}<span v-if="row.assignee_name" class="sub ml">{{ row.assignee_name }}</span>
              <el-tag v-if="ASSIGNEE_SOURCE_LABEL[row.assignee_source]" size="small" effect="plain" class="ml">
                {{ ASSIGNEE_SOURCE_LABEL[row.assignee_source] }}
              </el-tag>
            </template>
            <el-tooltip v-else :content="row.assignee_note || '未指派'" placement="top">
              <el-tag size="small" type="warning" effect="plain">
                未指派
              </el-tag>
            </el-tooltip>
          </template>
        </el-table-column>
        <el-table-column label="合并请求" min-width="120">
          <template #default="{ row }">
            <a :href="row.mr_url" target="_blank" rel="noopener">{{ row.mr_iid ? `!${row.mr_iid}` : "打开" }}</a>
          </template>
        </el-table-column>
        <el-table-column label="提出时间" min-width="160">
          <template #default="{ row }">
            {{ formatTime(row.finished_at || row.started_at) }}
            <div class="sub">
              {{ FIX_TRIGGER_LABEL[row.trigger] || row.trigger }}
            </div>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="80" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click="openDetail(row)">
              详情
            </el-button>
          </template>
        </el-table-column>
      </el-table>

      <el-pagination
        v-if="data.total > page.size" v-model:current-page="page.current" class="pager" layout="total, prev, pager, next"
        :page-size="page.size" :total="data.total" small @current-change="load"
      />
    </el-card>
  </div>
</template>

<style lang="scss" scoped>
.header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}
.filters {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
.mb {
  margin-bottom: 16px;
}
.mr {
  margin-right: 4px;
}
.ml {
  margin-left: 4px;
}
.sub {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  line-height: 1.6;
}
.pager {
  margin-top: 16px;
  justify-content: flex-end;
}
</style>

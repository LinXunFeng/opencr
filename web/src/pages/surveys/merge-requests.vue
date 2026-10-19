<script lang="ts" setup>
import type { FixMergeRequest, FixMergeRequestList, FixMergeRequestQuery, SurveyFixCleanupResult } from "@@/apis/opencr"
import {
  cleanupSurveyFixesApi,
  getFixMergeRequestsApi,
  getSurveysApi,
  MAX_CLEANUP_PER_REQUEST,
  UNASSIGNED_FILTER
} from "@@/apis/opencr"
import {
  ASSIGNEE_SOURCE_LABEL,
  FIX_NOTES,
  FIX_TRIGGER_LABEL,
  formatTime,
  SEVERITY_LABEL,
  SEVERITY_TAG
} from "@@/constants/opencr"
import { useUserStore } from "@/pinia/stores/user"

/**
 * 修复合并请求：跨巡检列出修复提出的合并请求与处理人，按巡检、处理人（可多选，含未指派）与关键字筛选。
 *
 * 原始作者与配置的仓库负责人都不可用时合并请求不指派，这里是把它们找出来分给人的入口。
 * 只列已提出合并请求的修复，以及推了分支、合并请求没提成的修复（平台上留着一条要清理的分支）；其余失败与未修改的修复在各自的巡检报告里看。合并请求在平台上的状态（已合并、已关闭）不在这里，
 * 要逐个调平台接口，列表一翻页就是几十次请求。
 * 也是清理的入口：不再需要的合并请求勾选后关闭、删掉修复分支。默认只列未清理的 —— 清理过的已经不需要谁处理了。
 */
const router = useRouter()
const userStore = useUserStore()
const loading = ref(true)
const data = ref<FixMergeRequestList>({ items: [], total: 0, assignees: [], unassigned: 0 })
const surveyOptions = ref<Array<{ label: string, value: string }>>([])
const filters = reactive({
  survey: "",
  assignees: [] as string[],
  keyword: "",
  /** 空串表示全部 */
  cleaned: "no" as "" | NonNullable<FixMergeRequestQuery["cleaned"]>
})
const selected = ref<FixMergeRequest[]>([])
const cleaning = ref(false)
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
      cleaned: filters.cleaned || undefined,
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

/** 已清理的不能再勾选 */
function selectable(raw: any) {
  return !(raw as FixMergeRequest).cleaned_at
}

/** 勾选变化 */
function onSelectionChange(rows: any[]) {
  selected.value = rows as FixMergeRequest[]
}

/** 没清理成的那几条逐条列出原因：汇总提示里只有个数，看不出该去处理哪一条 */
function describeFailures(results: SurveyFixCleanupResult[]) {
  const titles = new Map(data.value.items.map(item => [item.run_uid, item.title]))
  return results
    .filter(r => r.outcome !== "cleaned")
    .map(r => h("p", `${titles.get(r.run_uid) || r.run_uid}：${r.message}`))
}

/** 清理若干修复：先讲清会在平台上做什么、什么会保留，再提交；结束后汇总结果并刷新 */
async function cleanup(rows: FixMergeRequest[]) {
  if (cleaning.value || !rows.length) return
  if (rows.length > MAX_CLEANUP_PER_REQUEST) {
    ElMessage.warning(FIX_NOTES.cleanupTooMany(MAX_CLEANUP_PER_REQUEST))
    return
  }
  try {
    await ElMessageBox.confirm(
      h("div", FIX_NOTES.cleanup.map(text => h("p", text))),
      FIX_NOTES.cleanupTitle(rows.length),
      { type: "warning", confirmButtonText: FIX_NOTES.cleanupConfirmButton, cancelButtonText: "取消" }
    )
  } catch {
    return
  }
  cleaning.value = true
  try {
    const { results } = await cleanupSurveyFixesApi(rows.map(r => r.run_uid))
    const failures = describeFailures(results)
    if (failures.length) {
      ElMessageBox.alert(h("div", failures), FIX_NOTES.cleanupDone(results), { type: "warning" })
    } else {
      ElMessage.success(FIX_NOTES.cleanupDone(results))
    }
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    cleaning.value = false
  }
  await load()
}

watch(() => [filters.survey, filters.assignees, filters.cleaned], search, { deep: true })
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
            <el-select v-model="filters.cleaned" size="small" style="width: 100px">
              <el-option v-for="o in FIX_NOTES.cleanedFilterOptions" :key="o.value" :label="o.label" :value="o.value" />
            </el-select>
            <el-input
              v-model="filters.keyword" clearable placeholder="标题、仓库或文件，回车搜索" size="small" style="width: 220px"
              @keyup.enter="search" @clear="search"
            />
            <el-button
              v-if="userStore.isAdmin" type="danger" plain size="small" :disabled="!selected.length" :loading="cleaning"
              @click="cleanup(selected)"
            >
              {{ FIX_NOTES.cleanupSelectedButton(selected.length) }}
            </el-button>
          </div>
        </div>
      </template>

      <el-alert class="mb" type="info" :closable="false" show-icon :title="FIX_NOTES.mergeRequestsPage" />

      <el-table :data="data.items" size="small" empty-text="没有符合条件的合并请求" @selection-change="onSelectionChange">
        <el-table-column v-if="userStore.isAdmin" type="selection" width="40" :selectable="selectable" />
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
            <a v-if="row.mr_url" :href="row.mr_url" target="_blank" rel="noopener">{{ row.mr_iid ? `!${row.mr_iid}` : "打开" }}</a>
            <el-tooltip v-else :content="row.error_message || FIX_NOTES.orphanBranchTip" placement="top">
              <el-tag size="small" type="warning" effect="plain">
                {{ FIX_NOTES.orphanBranchTag }}
              </el-tag>
            </el-tooltip>
            <el-tooltip v-if="row.cleaned_at" :content="FIX_NOTES.cleanedTip" placement="top">
              <el-tag size="small" type="info" effect="plain" class="ml">
                {{ FIX_NOTES.cleanedTag }}
              </el-tag>
            </el-tooltip>
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
        <el-table-column label="操作" width="110" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click="openDetail(row)">
              详情
            </el-button>
            <el-button
              v-if="userStore.isAdmin && !row.cleaned_at" link type="danger" size="small" :disabled="cleaning"
              @click="cleanup([row as FixMergeRequest])"
            >
              {{ FIX_NOTES.cleanupButton }}
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

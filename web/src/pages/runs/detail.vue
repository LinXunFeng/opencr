<script lang="ts" setup>
import type { RunDetail } from "@@/apis/opencr"
import { getChangeHistoryApi, getRunDetailApi } from "@@/apis/opencr"
import VerdictTag from "@@/components/VerdictTag/index.vue"
import {
  DEGRADATION_LABEL,
  DELIVERY_LABEL,
  ERROR_KIND_LABEL,
  formatTime,
  HISTORY_NOTE,
  PHASE_LABEL,
  RUN_STATUS_LABEL,
  RUN_STATUS_TAG,
  SEVERITY_LABEL,
  SEVERITY_TAG,
  TRIGGER_LABEL,
  VERDICT_LABEL
} from "@@/constants/opencr"

const route = useRoute()
const router = useRouter()

const loading = ref(true)
const detail = ref<RunDetail | null>(null)

const scope = ref("current")
const severity = ref("")
const verdict = ref("")
const page = ref(1)
const history = ref<RunDetail[]>([])
const historyTotal = ref(0)
const historyLoading = ref(false)
const historyError = ref("")
let historyRequest = 0

/** 展示本次发现或服务端已筛选的历史批次。 */
const groups = computed(() => scope.value === "all" ? history.value : detail.value ? [detail.value] : [])

/** 本次审查已加载全部发现，可直接在页面筛选。 */
function filteredFindings(run: RunDetail) {
  return run.findings.filter(f => (!severity.value || f.severity === severity.value)
    && (!verdict.value || f.verdict === verdict.value))
}

/** 拉取历史分页；过期响应不能覆盖用户刚切换的筛选或运行。 */
async function loadHistory() {
  const requestId = ++historyRequest
  if (scope.value !== "all") return
  historyLoading.value = true
  historyError.value = ""
  history.value = []
  try {
    const result = await getChangeHistoryApi(route.params.runUid as string, {
      limit: 20, offset: (page.value - 1) * 20, severity: severity.value, verdict: verdict.value
    })
    if (requestId !== historyRequest) return
    history.value = result.items
    historyTotal.value = result.total
  } catch (error) {
    if (requestId === historyRequest) historyError.value = (error as Error).message
  } finally {
    if (requestId === historyRequest) historyLoading.value = false
  }
}

/** 切换运行时重置历史视图，加载当前运行详情。 */
async function load() {
  loading.value = true
  scope.value = "current"
  page.value = 1
  history.value = []
  ++historyRequest
  try {
    detail.value = await getRunDetailApi(route.params.runUid as string)
  } catch (error) {
    ElMessage.error((error as Error).message)
    detail.value = null
  } finally {
    loading.value = false
  }
}

watch([scope, severity, verdict], () => {
  if (page.value !== 1) page.value = 1
  else void loadHistory()
})
watch(page, loadHistory)
watch(() => route.params.runUid, load, { immediate: true })
</script>

<template>
  <div v-loading="loading" class="app-container">
    <el-page-header content="运行详情" @back="router.back()" />

    <template v-if="detail">
      <el-card shadow="never" class="mt">
        <template #header>
          <div class="header">
            <span>{{ detail.change_label || "合并请求" }} #{{ detail.mr_iid }} · {{ detail.mr_title }}</span>
            <el-link v-if="detail.change_url" :href="detail.change_url" target="_blank" rel="noopener noreferrer" type="primary">
              打开 {{ detail.change_label || "合并请求" }}
            </el-link>
            <el-tag v-if="detail.is_stale" type="warning" size="small">
              疑似中断
            </el-tag>
            <el-tag v-else :type="RUN_STATUS_TAG[detail.status]" size="small">
              {{ RUN_STATUS_LABEL[detail.status] || detail.status }}
            </el-tag>
          </div>
        </template>

        <el-descriptions :column="3" border size="small">
          <el-descriptions-item label="项目">
            {{ detail.project_path || detail.project_id }}
          </el-descriptions-item>
          <el-descriptions-item label="触发源">
            {{ TRIGGER_LABEL[detail.trigger] || detail.trigger }}
          </el-descriptions-item>
          <el-descriptions-item label="审查模式">
            {{ detail.review_mode || "-" }}
          </el-descriptions-item>
          <el-descriptions-item label="命中 Skill">
            {{ detail.review_skills.join("、") || "-" }}
          </el-descriptions-item>
          <el-descriptions-item label="阶段">
            {{ PHASE_LABEL[detail.phase] || detail.phase || "-" }}
          </el-descriptions-item>
          <el-descriptions-item label="文件进度">
            {{ detail.files_done }}/{{ detail.files_total }}
          </el-descriptions-item>
          <el-descriptions-item label="开始">
            {{ formatTime(detail.started_at) }}
          </el-descriptions-item>
          <el-descriptions-item label="结束">
            {{ formatTime(detail.finished_at) }}
          </el-descriptions-item>
          <el-descriptions-item label="最后心跳">
            {{ formatTime(detail.heartbeat_at) }}
          </el-descriptions-item>
        </el-descriptions>

        <el-alert
          v-if="detail.skip_reason"
          class="mt"
          type="info"
          :closable="false"
          :title="`跳过原因：${detail.skip_reason}`"
        />
        <el-alert
          v-if="detail.degradations.length"
          class="mt"
          type="warning"
          :closable="false"
          :title="`降级：${detail.degradations.map(d => `${DEGRADATION_LABEL[d.kind] || d.kind} ×${d.count}`).join('；')}`"
        />
        <el-alert
          v-if="detail.error_message"
          class="mt"
          type="error"
          :closable="false"
          :title="`错误（${ERROR_KIND_LABEL[detail.error_kind] || detail.error_kind}）`"
          :description="detail.error_message"
        />
      </el-card>

      <el-card shadow="never" class="mt">
        <template #header>
          {{ scope === "current" ? "本次审查发现" : "全部审查发现" }}
          <span v-if="scope === 'current'">（{{ filteredFindings(detail).length }}）</span>
        </template>

        <div class="filters mb">
          <el-radio-group v-model="scope" aria-label="审查范围">
            <el-radio-button value="current">本次审查</el-radio-button>
            <el-radio-button value="all">全部审查</el-radio-button>
          </el-radio-group>
          <el-select v-model="severity" aria-label="严重度筛选" placeholder="全部严重度" clearable style="width: 150px">
            <el-option v-for="(label, value) in SEVERITY_LABEL" :key="value" :label="label" :value="value" />
          </el-select>
          <el-select v-model="verdict" aria-label="采纳结论筛选" placeholder="全部采纳结论" clearable style="width: 160px">
            <el-option v-for="(label, value) in VERDICT_LABEL" :key="value" :label="label" :value="value" />
          </el-select>
        </div>
        <el-alert v-if="scope === 'all'" class="mb" type="info" :closable="false" :title="HISTORY_NOTE" />
        <el-alert v-if="scope === 'all' && historyError" class="mb" type="error" :closable="false" :title="historyError">
          <el-button link type="primary" @click="loadHistory">重试</el-button>
        </el-alert>

        <!-- Guest 拿不到正文：字段在服务端就被剔除了，不是前端隐藏 -->
        <el-alert
          v-if="!detail.body_included"
          class="mb"
          type="info"
          show-icon
          :closable="false"
          title="以游客身份浏览：审查发现的正文需登录后可见。"
        />

        <div v-loading="scope === 'all' && historyLoading">
          <section v-for="group in groups" :key="group.run_uid" class="batch">
            <div v-if="scope === 'all'" class="header mb">
              <span>{{ formatTime(group.started_at) }}</span>
              <span>{{ TRIGGER_LABEL[group.trigger] || group.trigger }}</span>
              <el-tag :type="group.is_stale ? 'warning' : RUN_STATUS_TAG[group.status]" size="small">
                {{ group.is_stale ? "疑似中断" : RUN_STATUS_LABEL[group.status] || group.status }}
              </el-tag>
              <span>{{ filteredFindings(group).length }} 条发现</span>
              <el-tag v-if="group.run_uid === detail.run_uid" size="small" effect="plain">本次审查</el-tag>
              <el-link v-else type="primary" @click="router.push(`/runs/detail/${group.run_uid}`)">运行详情</el-link>
            </div>
        <el-table :data="filteredFindings(group)" size="small" :empty-text="severity || verdict ? '本批次没有符合筛选条件的审查发现' : '本次运行没有产出审查发现'">
          <el-table-column type="expand" v-if="group.body_included">
            <template #default="{ row }">
              <pre class="body">{{ row.body }}</pre>
            </template>
          </el-table-column>
          <el-table-column label="位置" min-width="220" show-overflow-tooltip>
            <template #default="{ row }">
              <code>{{ row.file_path }}{{ row.line ? `:${row.line}` : "" }}</code>
            </template>
          </el-table-column>
          <el-table-column label="严重度" width="90">
            <template #default="{ row }">
              <el-tag :type="SEVERITY_TAG[row.severity]" size="small">
                {{ SEVERITY_LABEL[row.severity] || row.severity }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="投递方式" width="150">
            <template #default="{ row }">
              {{ DELIVERY_LABEL[row.delivery] || row.delivery }}
            </template>
          </el-table-column>
          <el-table-column label="采纳结论" width="130">
            <template #default="{ row }">
              <VerdictTag :verdict="row.verdict" />
            </template>
          </el-table-column>
          <el-table-column prop="verdict_reason" label="依据" width="150" />
        </el-table>
          </section>
          <el-empty v-if="scope === 'all' && !historyLoading && !historyError && !groups.length" description="暂无历史审查记录" />
        </div>
        <el-pagination v-if="scope === 'all' && historyTotal > 20" class="mt" v-model:current-page="page" :page-size="20" :total="historyTotal" layout="prev, pager, next" />
        <p v-if="scope === 'all'" class="note">共 {{ historyTotal }} 个审查批次，每页最多 20 个批次。</p>

        <p class="note">
          投递方式为「仅整体评论」或「降级为普通评论」的发现，走的是 GitLab 不可 resolve 的普通评论，
          因此采纳结论恒为「不可追踪」，不计入采纳率分母。
        </p>
      </el-card>
    </template>

    <el-empty v-else-if="!loading" description="未找到该审查运行，或已超出保留期被清理" />
  </div>
</template>

<style lang="scss" scoped>
.mt {
  margin-top: 16px;
}
.mb {
  margin-bottom: 16px;
}

.filters {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
}

.batch + .batch {
  margin-top: 24px;
}

.header {
  flex-wrap: wrap;
  display: flex;
  gap: 12px;
  align-items: center;
}

.body {
  padding: 12px;
  margin: 0;
  font-size: 12px;
  white-space: pre-wrap;
  word-break: break-word;
  background: var(--el-fill-color-light);
  border-radius: 6px;
}

.note {
  margin: 14px 0 0;
  font-size: 12px;
  line-height: 1.7;
  color: var(--el-text-color-secondary);
}
</style>

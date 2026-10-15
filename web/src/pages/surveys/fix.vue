<script lang="ts" setup>
import type { SurveyFixDetail } from "@@/apis/opencr"
import { getSurveyFixApi, interruptSurveyFixApi } from "@@/apis/opencr"
import {
  ASSIGNEE_SOURCE_LABEL,
  CATEGORY_LABEL,
  FIX_NOTES,
  FIX_PHASE_LABEL,
  FIX_STATUS_LABEL,
  FIX_STATUS_TAG,
  FIX_TRIGGER_LABEL,
  formatTime,
  INTERRUPT_NOTES,
  SEVERITY_LABEL,
  SEVERITY_TAG
} from "@@/constants/opencr"
import { useUserStore } from "@/pinia/stores/user"

/**
 * 修复详情：一次修复运行的修改、每轮验证、原始作者与处理人、合并请求。
 * 合并请求说明里的「修复运行详情」链接指向这里，所以它要能脱离报告页单独打开。
 */
const route = useRoute()
const userStore = useUserStore()
const loading = ref(true)
const detail = ref<SurveyFixDetail | null>(null)
const runUid = computed(() => String(route.params.runUid || ""))

async function load(silent = false) {
  if (!silent) loading.value = true
  try {
    detail.value = await getSurveyFixApi(runUid.value)
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    loading.value = false
    schedulePolling()
  }
}

/** 修复在后台执行，进行中时轮询刷新；疑似中断的不再等 */
let pollTimer: ReturnType<typeof setTimeout> | undefined
function schedulePolling() {
  clearTimeout(pollTimer)
  if (detail.value?.status === "running" && !detail.value.is_stale) pollTimer = setTimeout(load, 3000, true)
}
onBeforeUnmount(() => clearTimeout(pollTimer))

const interrupting = ref(false)

/** 确认疑似中断：先讲清只关记录不停执行、原运行若还活着会怎样，再提交 */
async function confirmInterrupt() {
  if (interrupting.value) return
  try {
    await ElMessageBox.confirm(
      h("div", INTERRUPT_NOTES.fix.map(text => h("p", text))),
      INTERRUPT_NOTES.fixTitle,
      { type: "warning", confirmButtonText: INTERRUPT_NOTES.button, cancelButtonText: INTERRUPT_NOTES.cancel }
    )
  } catch {
    return
  }
  interrupting.value = true
  try {
    await interruptSurveyFixApi(runUid.value)
    ElMessage.success(INTERRUPT_NOTES.fixDone)
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    interrupting.value = false
  }
  // 成功与 409（心跳恢复或已结束）都要刷新，让页面反映真实状态
  await load(true)
}

const location = computed(() => {
  const d = detail.value
  if (!d) return ""
  return `${d.repo_slug}/${d.file_path}${d.line ? `:${d.line}` : ""}`
})

onMounted(() => load())
</script>

<template>
  <div v-loading="loading" class="app-container">
    <template v-if="detail">
      <el-card shadow="never" class="mb">
        <template #header>
          {{ detail.survey_name }} —— 修复
        </template>

        <el-descriptions :column="2" border size="small">
          <el-descriptions-item label="状态">
            <el-tag size="small" :type="FIX_STATUS_TAG[detail.status] || 'info'">
              {{ FIX_STATUS_LABEL[detail.status] || detail.status }}
            </el-tag>
            <span v-if="detail.status === 'running'" class="sub ml">{{ FIX_PHASE_LABEL[detail.phase] || detail.phase }}</span>
            <el-tag v-if="detail.is_stale" size="small" type="warning" effect="plain" class="ml">
              疑似中断
            </el-tag>
            <el-tooltip v-if="detail.is_stale && userStore.isAdmin" :content="INTERRUPT_NOTES.buttonTip" placement="top">
              <el-button type="warning" size="small" plain class="ml" :loading="interrupting" @click="confirmInterrupt">
                {{ INTERRUPT_NOTES.button }}
              </el-button>
            </el-tooltip>
          </el-descriptions-item>
          <el-descriptions-item label="合并请求">
            <a v-if="detail.mr_url" :href="detail.mr_url" target="_blank" rel="noopener">{{ detail.mr_url }}</a>
            <span v-else>—</span>
          </el-descriptions-item>
          <el-descriptions-item label="位置">
            <code>{{ location }}</code>
          </el-descriptions-item>
          <el-descriptions-item label="分支">
            {{ detail.source_branch || "—" }} → {{ detail.target_branch || "默认" }}
            <code v-if="detail.base_sha" class="ml">{{ detail.base_sha.slice(0, 12) }}</code>
          </el-descriptions-item>
          <el-descriptions-item label="原始作者">
            <el-tooltip :content="FIX_NOTES.author" placement="top">
              <span>{{ detail.author_name || "—" }}<span v-if="detail.author_email" class="sub ml">&lt;{{ detail.author_email }}&gt;</span></span>
            </el-tooltip>
          </el-descriptions-item>
          <el-descriptions-item label="处理人">
            <template v-if="detail.assignee_username">
              @{{ detail.assignee_username }}（{{ detail.assignee_name }}）
              <el-tag v-if="ASSIGNEE_SOURCE_LABEL[detail.assignee_source]" size="small" effect="plain" class="ml">
                {{ ASSIGNEE_SOURCE_LABEL[detail.assignee_source] }}
              </el-tag>
            </template>
            <span v-else>未指派</span>
            <div v-if="detail.assignee_note" class="sub">
              {{ detail.assignee_note }}
            </div>
          </el-descriptions-item>
          <el-descriptions-item label="发起时间">
            {{ formatTime(detail.started_at) }}
            <span class="sub ml">{{ FIX_TRIGGER_LABEL[detail.trigger] || detail.trigger }}</span>
          </el-descriptions-item>
          <el-descriptions-item label="结束时间">
            {{ formatTime(detail.finished_at) }}
          </el-descriptions-item>
        </el-descriptions>

        <el-alert
          v-if="detail.error_message" class="mt" :type="detail.status === 'skipped' ? 'info' : 'error'"
          :closable="false" show-icon :title="detail.error_message"
        />
        <el-alert class="mt" type="info" :closable="false" show-icon :title="FIX_NOTES.notResolved" />
      </el-card>

      <el-card shadow="never" class="mb">
        <template #header>
          问题
          <el-tag size="small" :type="SEVERITY_TAG[detail.severity] || 'info'" class="ml">
            {{ SEVERITY_LABEL[detail.severity] || detail.severity }}
          </el-tag>
          <span class="sub ml">{{ CATEGORY_LABEL[detail.category] || detail.category }}</span>
        </template>
        <div class="title">
          {{ detail.title }}
        </div>
        <pre v-if="detail.body" class="body">{{ detail.body }}</pre>
        <div v-else class="sub">
          问题正文仅管理员可见
        </div>
      </el-card>

      <el-card v-if="detail.verification.length" shadow="never" class="mb">
        <template #header>
          本地验证
          <span class="sub ml">{{ FIX_NOTES.verifyScope }}</span>
        </template>
        <div v-for="(attempt, index) in detail.verification" :key="index" class="attempt">
          <div class="attempt-head">
            第 {{ index + 1 }} 次
            <el-tag size="small" :type="attempt.passed ? 'success' : 'danger'" class="ml">
              {{ attempt.passed ? "通过" : "未通过" }}
            </el-tag>
          </div>
          <ul>
            <li v-for="check in attempt.checks" :key="check.name">
              {{ check.passed ? "✓" : "✗" }} {{ check.name }}<span v-if="check.detail" class="sub">：{{ check.detail }}</span>
            </li>
            <li v-for="(problem, i) in attempt.problems" :key="`p${i}`">
              ✗ 模型复核：{{ problem }}
            </li>
          </ul>
        </div>
      </el-card>

      <el-card v-if="detail.diff" shadow="never">
        <template #header>
          修改
          <span v-if="detail.summary" class="sub ml">{{ detail.summary }}</span>
        </template>
        <pre class="diff">{{ detail.diff }}</pre>
      </el-card>
    </template>
  </div>
</template>

<style lang="scss" scoped>
.mb {
  margin-bottom: 16px;
}
.mt {
  margin-top: 16px;
}
.ml {
  margin-left: 4px;
}
.sub {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  line-height: 1.6;
}
.title {
  font-weight: 600;
  margin-bottom: 8px;
}
.body {
  white-space: pre-wrap;
  margin: 0;
  font-family: inherit;
  line-height: 1.7;
}
.attempt + .attempt {
  margin-top: 12px;
}
.attempt ul {
  margin: 4px 0 0;
  padding-left: 18px;
}
.diff {
  margin: 0;
  overflow-x: auto;
  font-size: 12px;
  line-height: 1.5;
}
</style>

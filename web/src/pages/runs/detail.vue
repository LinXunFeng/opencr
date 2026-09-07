<script lang="ts" setup>
import type { RunDetail } from "@@/apis/opencr"
import { getRunDetailApi } from "@@/apis/opencr"
import {
  DEGRADATION_LABEL,
  DELIVERY_LABEL,
  ERROR_KIND_LABEL,
  formatTime,
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

async function load() {
  loading.value = true
  try {
    detail.value = await getRunDetailApi(route.params.runUid as string)
  } catch (error) {
    ElMessage.error((error as Error).message)
    detail.value = null
  } finally {
    loading.value = false
  }
}

onMounted(load)
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
          审查发现（{{ detail.findings.length }}）
        </template>

        <!-- Guest 拿不到正文：字段在服务端就被剔除了，不是前端隐藏 -->
        <el-alert
          v-if="!detail.body_included"
          class="mb"
          type="info"
          show-icon
          :closable="false"
          title="以游客身份浏览：审查发现的正文需登录后可见。"
        />

        <el-table :data="detail.findings" size="small" empty-text="本次运行没有产出审查发现">
          <el-table-column type="expand" v-if="detail.body_included">
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
          <el-table-column label="采纳结论" width="110">
            <template #default="{ row }">
              <el-tag size="small" effect="plain">
                {{ VERDICT_LABEL[row.verdict] || row.verdict }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="verdict_reason" label="依据" width="150" />
        </el-table>

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

.header {
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

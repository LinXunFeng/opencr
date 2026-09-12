<script lang="ts" setup>
import type { SurveyFinding, SurveyRunDetail } from "@@/apis/opencr"
import { addSurveyIgnoreApi, getSurveyRunDetailApi } from "@@/apis/opencr"
import {
  PROFILE_KIND_LABEL,
  RUN_STATUS_LABEL,
  RUN_STATUS_TAG,
  SURVEY_DEGRADATION_LABEL,
  SURVEY_NOTES,
  SURVEY_PHASE_LABEL,
  SURVEY_REPO_STATUS_LABEL,
  SURVEY_TRIGGER_LABEL
} from "@@/constants/opencr"
import { useUserStore } from "@/pinia/stores/user"
import FindingTable from "./FindingTable.vue"

const route = useRoute()
const userStore = useUserStore()
const loading = ref(true)
const detail = ref<SurveyRunDetail | null>(null)
/** 默认停在「新增」：全量巡检逐轮高度重合，只看新增才有信噪比 */
const activeTab = ref("new")

const runUid = computed(() => String(route.params.runUid || ""))

async function load() {
  loading.value = true
  try {
    detail.value = await getSurveyRunDetailApi(runUid.value)
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    loading.value = false
  }
}

const newFindings = computed(() => (detail.value?.findings ?? []).filter(f => f.state === "new"))
const persistedFindings = computed(() => (detail.value?.findings ?? []).filter(f => f.state === "persisted"))
const resolvedFindings = computed(() => detail.value?.resolved_findings ?? [])

function formatTime(value: string) {
  return value ? new Date(value).toLocaleString() : "—"
}

/** 导出走服务端渲染，因此游客导出的报告同样不含正文——否则导出就成了绕过可见范围的后门 */
function exportMarkdown() {
  window.open(`/api/admin/survey-runs/${runUid.value}/export`, "_blank")
}

async function ignore(finding: SurveyFinding) {
  if (!detail.value?.survey_uid) return
  try {
    await ElMessageBox.confirm(
      "标记为已知问题后，后续巡检不会再产出这一条。",
      "不再提醒",
      { type: "warning" }
    )
  } catch {
    return
  }
  try {
    await addSurveyIgnoreApi(detail.value.survey_uid, finding.fingerprint)
    ElMessage.success("已加入忽略清单")
  } catch (error) {
    ElMessage.error((error as Error).message)
  }
}

onMounted(load)
</script>

<template>
  <div v-loading="loading" class="app-container">
    <template v-if="detail">
      <el-card shadow="never" class="mb">
        <template #header>
          <div class="header">
            <span>{{ detail.survey_name }} —— 巡检报告</span>
            <el-button size="small" @click="exportMarkdown">
              导出 Markdown
            </el-button>
          </div>
        </template>

        <el-descriptions :column="3" border size="small">
          <el-descriptions-item label="状态">
            <el-tag size="small" :type="RUN_STATUS_TAG[detail.status] || 'info'">
              {{ RUN_STATUS_LABEL[detail.status] || detail.status }}
            </el-tag>
            <el-tag v-if="detail.is_stale" size="small" type="warning" effect="plain" class="ml">
              疑似中断
            </el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="阶段">
            {{ SURVEY_PHASE_LABEL[detail.phase] || detail.phase || "—" }}
          </el-descriptions-item>
          <el-descriptions-item label="触发">
            {{ SURVEY_TRIGGER_LABEL[detail.trigger] || detail.trigger }}
          </el-descriptions-item>
          <el-descriptions-item label="开始时间">
            {{ formatTime(detail.started_at) }}
          </el-descriptions-item>
          <el-descriptions-item label="结束时间">
            {{ formatTime(detail.finished_at) }}
          </el-descriptions-item>
          <el-descriptions-item label="命中 Skill">
            <el-tag v-for="s in detail.matched_skills" :key="s" size="small" effect="plain" class="mr">
              {{ s }}
            </el-tag>
            <span v-if="!detail.matched_skills.length">未命中</span>
          </el-descriptions-item>
        </el-descriptions>

        <el-alert
          v-if="detail.error_message" class="mt" type="error" :closable="false" show-icon
          :title="detail.error_message"
        />

        <div v-if="detail.degradations.length" class="mt">
          <el-alert
            type="warning" :closable="false" show-icon
            title="本次运行有降级——运行本身成功了，但产出质量受到影响："
          >
            <ul class="degradations">
              <li v-for="d in detail.degradations" :key="d.kind">
                {{ SURVEY_DEGRADATION_LABEL[d.kind] || d.kind }} ×{{ d.count }}
              </li>
            </ul>
          </el-alert>
        </div>
      </el-card>

      <el-card shadow="never" class="mb">
        <template #header>
          覆盖的仓库（{{ detail.repos.length }}）
        </template>
        <el-table :data="detail.repos" size="small">
          <el-table-column prop="repo_slug" label="仓库" min-width="160" />
          <el-table-column label="分支" width="140">
            <template #default="{ row }">
              {{ row.branch || "默认" }}
            </template>
          </el-table-column>
          <el-table-column label="提交" width="110">
            <template #default="{ row }">
              <code>{{ row.commit_sha || "—" }}</code>
            </template>
          </el-table-column>
          <el-table-column label="状态" width="110">
            <template #default="{ row }">
              <el-tag size="small" :type="row.status === 'ok' ? 'success' : 'danger'">
                {{ SURVEY_REPO_STATUS_LABEL[row.status] || row.status }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="画像" min-width="200">
            <template #default="{ row }">
              <span :class="{ degraded: row.profile_kind === 'manifest' }">
                {{ PROFILE_KIND_LABEL[row.profile_kind] || "—" }}
              </span>
            </template>
          </el-table-column>
          <el-table-column prop="file_count" label="文件数" width="100" />
          <el-table-column prop="error_message" label="错误" min-width="180" show-overflow-tooltip />
        </el-table>
      </el-card>

      <el-card v-if="detail.summary" shadow="never" class="mb">
        <template #header>
          整体结论
        </template>
        <pre class="summary">{{ detail.summary }}</pre>
      </el-card>

      <el-alert
        v-if="!detail.body_included"
        class="mb" type="info" :closable="false" show-icon
        :title="SURVEY_NOTES.guestScope"
      />

      <el-card shadow="never">
        <template #header>
          发现（共 {{ detail.counts.total }} 条）
        </template>

        <el-alert class="mb" type="info" :closable="false" show-icon :title="SURVEY_NOTES.stateDiff" />

        <el-tabs v-model="activeTab">
          <el-tab-pane :label="`新增 (${detail.counts.new})`" name="new">
            <FindingTable :items="newFindings" :can-ignore="userStore.isAdmin" @ignore="ignore" />
          </el-tab-pane>
          <el-tab-pane :label="`仍存在 (${detail.counts.persisted})`" name="persisted">
            <FindingTable :items="persistedFindings" :can-ignore="userStore.isAdmin" @ignore="ignore" />
          </el-tab-pane>
          <el-tab-pane :label="`已消失 (${detail.counts.resolved})`" name="resolved">
            <el-alert class="mb" type="success" :closable="false" show-icon :title="SURVEY_NOTES.resolvedNotStored" />
            <FindingTable :items="resolvedFindings" :can-ignore="false" />
          </el-tab-pane>
        </el-tabs>
      </el-card>
    </template>
  </div>
</template>

<style lang="scss" scoped>
.header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.mb {
  margin-bottom: 16px;
}
.mt {
  margin-top: 16px;
}
.ml {
  margin-left: 4px;
}
.mr {
  margin-right: 4px;
}

.summary {
  white-space: pre-wrap;
  margin: 0;
  font-family: inherit;
  line-height: 1.7;
}

.degradations {
  margin: 8px 0 0;
  padding-left: 18px;
}

.degraded {
  color: var(--el-color-warning);
}
</style>

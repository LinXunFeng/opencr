<script lang="ts" setup>
import type { ReviewRun } from "@@/apis/opencr"
import { getRunsApi } from "@@/apis/opencr"
import {
  DEGRADATION_LABEL,
  formatTime,
  PHASE_LABEL,
  RUN_STATUS_LABEL,
  RUN_STATUS_TAG,
  TRIGGER_LABEL
} from "@@/constants/opencr"

defineOptions({ name: "RunList" })

const router = useRouter()
const loading = ref(true)
const runs = ref<ReviewRun[]>([])
const status = ref("")
const keyword = ref("")

async function load() {
  loading.value = true
  try {
    const { items } = await getRunsApi({ limit: 200, status: status.value })
    runs.value = items
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    loading.value = false
  }
}

const filtered = computed(() => {
  const kw = keyword.value.trim().toLowerCase()
  if (!kw) return runs.value
  return runs.value.filter(r =>
    `${r.project_path} ${r.mr_title} ${r.mr_iid}`.toLowerCase().includes(kw)
  )
})

function note(run: any): string {
  if (run.status === "skipped") return run.skip_reason
  if (run.status === "failed") return run.error_message
  return (run.degradations ?? []).map((d: any) => `${DEGRADATION_LABEL[d.kind] || d.kind} ×${d.count}`).join("；")
}

onMounted(load)
</script>

<template>
  <div class="app-container">
    <el-card shadow="never" class="filter">
      <el-form inline @submit.prevent>
        <el-form-item label="状态">
          <el-select v-model="status" placeholder="全部" clearable style="width: 140px" @change="load">
            <el-option label="成功" value="succeeded" />
            <el-option label="失败" value="failed" />
            <el-option label="进行中" value="running" />
            <el-option label="跳过" value="skipped" />
          </el-select>
        </el-form-item>
        <el-form-item label="搜索">
          <el-input v-model="keyword" placeholder="项目 / MR 标题 / 编号" clearable style="width: 260px" />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" @click="load">
            刷新
          </el-button>
        </el-form-item>
      </el-form>
      <!-- 跳过不是错误：Draft/WIP、dependabot、merge commit 触发的更新都会落在这里 -->
      <el-alert
        type="info"
        :closable="false"
        show-icon
        title="「跳过」不是错误。它记录的是本可以触发审查、但被规则挡下的事件（Draft/WIP、dependabot、merge commit 等）。"
      />
    </el-card>

    <el-card v-loading="loading" shadow="never">
      <el-table :data="filtered" size="small" empty-text="暂无记录">
        <el-table-column label="开始时间" width="170">
          <template #default="{ row }">
            {{ formatTime(row.started_at) }}
          </template>
        </el-table-column>
        <el-table-column label="项目" min-width="170" show-overflow-tooltip>
          <template #default="{ row }">
            {{ row.project_path || row.project_id }}
          </template>
        </el-table-column>
        <el-table-column label="MR" width="80">
          <template #default="{ row }">
            <el-link type="primary" @click="router.push(`/runs/detail/${row.run_uid}`)">
              !{{ row.mr_iid }}
            </el-link>
          </template>
        </el-table-column>
        <el-table-column prop="mr_title" label="标题" min-width="180" show-overflow-tooltip />
        <el-table-column label="触发" width="100">
          <template #default="{ row }">
            {{ TRIGGER_LABEL[row.trigger] || row.trigger }}
          </template>
        </el-table-column>
        <el-table-column prop="review_mode" label="模式" width="80" />
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag v-if="row.is_stale" type="warning" size="small">
              疑似中断
            </el-tag>
            <el-tag v-else :type="RUN_STATUS_TAG[row.status]" size="small">
              {{ RUN_STATUS_LABEL[row.status] || row.status }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="进度" width="130">
          <template #default="{ row }">
            <span v-if="row.files_total">{{ row.files_done }}/{{ row.files_total }} 文件</span>
            <span v-else class="muted">{{ PHASE_LABEL[row.phase] || row.phase || "-" }}</span>
          </template>
        </el-table-column>
        <el-table-column label="说明" min-width="200" show-overflow-tooltip>
          <template #default="{ row }">
            <span :class="{ danger: row.status === 'failed', warn: row.degradations.length }">
              {{ note(row) }}
            </span>
          </template>
        </el-table-column>
      </el-table>
    </el-card>
  </div>
</template>

<style lang="scss" scoped>
.filter {
  margin-bottom: 16px;
}
.muted {
  color: var(--el-text-color-secondary);
}
.danger {
  color: var(--el-color-danger);
}
.warn {
  color: var(--el-color-warning);
}
</style>

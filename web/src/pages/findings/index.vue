<script lang="ts" setup>
import type { Finding, ProjectItem } from "@@/apis/opencr"
import { getDashboardApi, getFindingsApi } from "@@/apis/opencr"
import VerdictTag from "@@/components/VerdictTag/index.vue"
import {
  DELIVERY_LABEL,
  formatTime,
  SEVERITY_LABEL,
  SEVERITY_TAG,
  VERDICT_LABEL,
  VERDICT_ORDER
} from "@@/constants/opencr"

defineOptions({ name: "FindingList" })

const router = useRouter()
const loading = ref(true)
const items = ref<Finding[]>([])
const total = ref(0)
const bodyIncluded = ref(true)
const projects = ref<ProjectItem[]>([])

const query = reactive({
  project_id: undefined as number | undefined,
  verdict: "",
  severity: "",
  days: 30,
  page: 1,
  limit: 20
})

async function load() {
  loading.value = true
  try {
    const res = await getFindingsApi({
      project_id: query.project_id,
      verdict: query.verdict,
      severity: query.severity,
      days: query.days,
      limit: query.limit,
      offset: (query.page - 1) * query.limit
    })
    items.value = res.items
    total.value = res.total
    bodyIncluded.value = res.body_included
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    loading.value = false
  }
}

function reload() {
  query.page = 1
  load()
}

onMounted(async () => {
  load()
  try {
    projects.value = (await getDashboardApi()).projects
  } catch {
    // 项目下拉拿不到不影响主列表，静默降级
  }
})
</script>

<template>
  <div class="app-container">
    <el-card shadow="never" class="filter">
      <el-form inline @submit.prevent>
        <el-form-item label="项目">
          <el-select v-model="query.project_id" placeholder="全部" clearable style="width: 200px" @change="reload">
            <el-option
              v-for="p in projects"
              :key="p.project_id"
              :label="p.project_path || p.project_id"
              :value="p.project_id"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="采纳结论">
          <el-select v-model="query.verdict" placeholder="全部" clearable style="width: 140px" @change="reload">
            <el-option v-for="v in VERDICT_ORDER" :key="v" :label="VERDICT_LABEL[v]" :value="v" />
          </el-select>
        </el-form-item>
        <el-form-item label="严重度">
          <el-select v-model="query.severity" placeholder="全部" clearable style="width: 120px" @change="reload">
            <el-option label="严重" value="critical" />
            <el-option label="警告" value="warning" />
            <el-option label="建议" value="advice" />
            <el-option label="未知" value="unknown" />
          </el-select>
        </el-form-item>
        <el-form-item label="时间窗">
          <el-select v-model="query.days" style="width: 110px" @change="reload">
            <el-option label="近 1 天" :value="1" />
            <el-option label="近 7 天" :value="7" />
            <el-option label="近 30 天" :value="30" />
            <el-option label="近 90 天" :value="90" />
          </el-select>
        </el-form-item>
      </el-form>
      <el-alert
        type="info"
        :closable="false"
        show-icon
        title="这里可以跨多次审查提问，例如「这个项目所有被忽略的严重问题」——单看某一次运行是问不出来的。"
      />
    </el-card>

    <el-card v-loading="loading" shadow="never">
      <el-alert
        v-if="!bodyIncluded"
        class="mb"
        type="info"
        show-icon
        :closable="false"
        title="以游客身份浏览：审查发现的正文需登录后可见。"
      />

      <el-table :data="items" size="small" empty-text="没有匹配的审查发现">
        <el-table-column v-if="bodyIncluded" type="expand">
          <template #default="{ row }">
            <pre class="body">{{ row.body }}</pre>
          </template>
        </el-table-column>
        <el-table-column label="时间" width="170">
          <template #default="{ row }">
            {{ formatTime(row.created_at) }}
          </template>
        </el-table-column>
        <el-table-column label="MR" width="80">
          <template #default="{ row }">
            <el-link v-if="row.run_uid" type="primary" @click="router.push(`/runs/detail/${row.run_uid}`)">
              !{{ row.mr_iid }}
            </el-link>
            <span v-else>!{{ row.mr_iid }}</span>
          </template>
        </el-table-column>
        <el-table-column label="位置" min-width="240" show-overflow-tooltip>
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

      <el-pagination
        v-model:current-page="query.page"
        v-model:page-size="query.limit"
        class="pager"
        layout="total, sizes, prev, pager, next"
        :total="total"
        :page-sizes="[20, 50, 100]"
        @current-change="load"
        @size-change="reload"
      />
    </el-card>
  </div>
</template>

<style lang="scss" scoped>
.filter {
  margin-bottom: 16px;
}
.mb {
  margin-bottom: 16px;
}
.pager {
  margin-top: 16px;
  justify-content: flex-end;
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
</style>

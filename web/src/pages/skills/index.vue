<script lang="ts" setup>
import { getSkillsApi } from "@@/apis/opencr"

const loading = ref(true)
const data = ref<{ skills_dir: string, scripts_enabled: boolean, items: any[], hits: Record<string, number> } | null>(null)

async function load() {
  loading.value = true
  try {
    data.value = await getSkillsApi(30)
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    loading.value = false
  }
}

/** 服务端已经把元信息与命中次数合并好，这里只按命中次数排序 */
const rows = computed(() =>
  [...(data.value?.items ?? [])].sort((a: any, b: any) => b.hits - a.hits)
)

onMounted(load)
</script>

<template>
  <div v-loading="loading" class="app-container">
    <el-card shadow="never" class="mb">
      <el-descriptions :column="2" border size="small">
        <el-descriptions-item label="Skill 目录">
          <code>{{ data?.skills_dir }}</code>
        </el-descriptions-item>
        <el-descriptions-item label="脚本执行">
          <el-tag size="small" :type="data?.scripts_enabled ? 'success' : 'info'">
            {{ data?.scripts_enabled ? "已启用" : "已禁用" }}
          </el-tag>
        </el-descriptions-item>
      </el-descriptions>
      <el-alert
        class="mt"
        type="info"
        :closable="false"
        show-icon
        title="未命中任何 skill 的审查分支会被直接跳过——命中次数为 0 的 skill 等于没生效。"
      />
    </el-card>

    <el-card shadow="never">
      <template #header>
        Skill 列表（近 30 天命中次数）
      </template>
      <el-table :data="rows" size="small" empty-text="未加载到任何 skill">
        <el-table-column prop="name" label="名称" width="200" />
        <el-table-column prop="description" label="说明" min-width="320" show-overflow-tooltip />
        <el-table-column label="命中次数" width="120" sortable sort-by="hits">
          <template #default="{ row }">
            <el-tag size="small" :type="row.hits ? 'success' : 'info'" effect="plain">
              {{ row.hits }}
            </el-tag>
          </template>
        </el-table-column>
      </el-table>
    </el-card>
  </div>
</template>

<style lang="scss" scoped>
.mb {
  margin-bottom: 16px;
}
.mt {
  margin-top: 12px;
}
</style>

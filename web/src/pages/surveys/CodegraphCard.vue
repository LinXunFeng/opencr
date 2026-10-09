<script lang="ts" setup>
import type { ClueCounts, SurveyRunDetail } from "@@/apis/opencr"
import {
  CLUE_KEYS,
  CLUE_SOURCE_DESC,
  CLUE_SOURCE_LABEL,
  CLUE_SOURCE_TAG,
  CODEGRAPH_STATUS_LABEL,
  CODEGRAPH_STATUS_TAG,
  CODEGRAPH_UNAVAILABLE_LABEL,
  codegraphStatusOf,
  INDEX_MODE_LABEL,
  INDEX_MODE_TAG,
  sumClueCounts,
  SURVEY_NOTES
} from "@@/constants/opencr"

/**
 * 巡检报告里的 codegraph 卡片：本轮每个仓库的建索引情况，以及 codegraph 的产出流向了哪里。
 *
 * 数据全是计数与标签，不含正文，Guest 同样可见。
 * 旧运行没有快照时整张卡片只显示一句说明——渲染一排 0 会被读成"codegraph 没起作用"。
 */
const props = defineProps<{ detail: SurveyRunDetail }>()

const stats = computed(() => props.detail.codegraph_stats)
/** 只列启用了 codegraph 的仓库；拉取失败的仓库根本没到建索引这一步 */
const indexedRepos = computed(() => props.detail.repos.filter(r => r.index_mode))

/** 本轮 codegraph 状态：enabled / disabled / missing，没有快照时为空串 */
const status = computed(() => (stats.value ? codegraphStatusOf(stats.value) : ""))

/** 要展示的线索来源标签：unknown 只在确有旧数据时出现，其余三类即使为 0 也展示 */
function clueItems(counts: ClueCounts | null | undefined) {
  return CLUE_KEYS
    .map(key => ({ key, value: counts?.[key] ?? 0 }))
    .filter(item => item.value > 0 || item.key !== "unknown")
}

/** codegraph 来源在全部条目中的占比文本；没有条目时为「—」而不是 0% */
function share(counts: ClueCounts | null | undefined) {
  const all = sumClueCounts(counts)
  return all ? `${Math.round(((counts?.codegraph ?? 0) / all) * 100)}%` : "—"
}

/** 毫秒耗时的展示文本；null 表示没有记录 */
function formatMs(value: number | null) {
  if (value === null || value === undefined) return "—"
  return value < 1000 ? `${value} ms` : `${(value / 1000).toFixed(1)} 秒`
}

/** 抽取条数的展示文本；null 表示没有记录，与 0 区分开 */
function formatCount(value: number | null) {
  return value ?? "—"
}

/**
 * 建成了索引但一条接口、一个类型都没抽出来。
 * el-table 的插槽把 row 定为 DefaultRow，在边界处收窄（与 FindingTable.vue 的写法一致）
 */
function isEmptyExtraction(row: any) {
  return row.index_mode !== "failed" && row.route_count === 0 && row.type_count === 0
}
</script>

<template>
  <el-card shadow="never">
    <template #header>
      <div class="header">
        <span>codegraph 执行情况与收益</span>
        <el-tag v-if="status" size="small" effect="plain" :type="CODEGRAPH_STATUS_TAG[status]">
          {{ CODEGRAPH_STATUS_LABEL[status] }}
        </el-tag>
      </div>
    </template>

    <div v-if="!stats" class="sub">
      {{ SURVEY_NOTES.codegraphNoRecord }}
    </div>

    <template v-else>
      <el-alert
        v-if="status !== 'enabled'" class="mb" :closable="false" show-icon
        :type="status === 'missing' ? 'error' : 'info'" :title="CODEGRAPH_UNAVAILABLE_LABEL[status]"
      />

      <div class="section-title">
        产出
      </div>
      <div class="tiles">
        <div class="tile">
          <div class="value">
            {{ stats.repos_structured }} / {{ stats.repos_total }}
          </div>
          <div class="label">
            结构图画像的仓库
          </div>
          <el-tooltip v-if="stats.repos_empty" :content="SURVEY_NOTES.emptyExtraction">
            <div class="label warn">
              其中 {{ stats.repos_empty }} 个抽取为空
            </div>
          </el-tooltip>
        </div>
        <div class="tile">
          <div class="value">
            {{ stats.routes }}
          </div>
          <div class="label">
            抽出的接口
          </div>
        </div>
        <div class="tile">
          <div class="value">
            {{ stats.types }}
          </div>
          <div class="label">
            抽出的类型
          </div>
        </div>
        <el-tooltip :content="SURVEY_NOTES.droppedRegistrations">
          <div class="tile">
            <div class="value">
              {{ stats.dropped_registrations }}
            </div>
            <div class="label">
              剔除的误报调用
            </div>
          </div>
        </el-tooltip>
      </div>

      <div class="section-title">
        跨仓库事实
      </div>
      <div class="tiles">
        <div class="tile">
          <div class="value">
            {{ stats.cross_repo.links }}
          </div>
          <div class="label">
            接口连接
          </div>
        </div>
        <div class="tile">
          <div class="value" :class="{ warn: stats.cross_repo.method_mismatch }">
            {{ stats.cross_repo.method_mismatch }}
          </div>
          <div class="label">
            HTTP 方法不一致
          </div>
        </div>
        <div class="tile">
          <div class="value">
            {{ stats.cross_repo.unused_routes }}
          </div>
          <div class="label">
            范围内无人调用的接口
          </div>
        </div>
        <div class="tile">
          <div class="value">
            {{ stats.cross_repo.orphan_calls }}
          </div>
          <div class="label">
            范围内无人提供的调用
          </div>
        </div>
      </div>
      <div class="sub mb">
        {{ SURVEY_NOTES.crossRepoNeedsRoutes }}
      </div>

      <div class="section-title">
        线索来源
      </div>
      <el-descriptions :column="1" border size="small" class="mb">
        <el-descriptions-item label="关注点（L1 点名）">
          <template v-if="stats.focus">
            <el-tooltip v-for="item in clueItems(stats.focus)" :key="item.key" :content="CLUE_SOURCE_DESC[item.key]">
              <el-tag size="small" :type="CLUE_SOURCE_TAG[item.key]" effect="plain" class="mr">
                {{ CLUE_SOURCE_LABEL[item.key] }} {{ item.value }}
              </el-tag>
            </el-tooltip>
            <span class="sub">codegraph 占 {{ share(stats.focus) }}</span>
          </template>
          <span v-else class="sub">{{ SURVEY_NOTES.focusNotReached }}</span>
        </el-descriptions-item>
        <el-descriptions-item label="发现（L2 取证后入库）">
          <el-tooltip v-for="item in clueItems(detail.clue_counts)" :key="item.key" :content="CLUE_SOURCE_DESC[item.key]">
            <el-tag size="small" :type="CLUE_SOURCE_TAG[item.key]" effect="plain" class="mr">
              {{ CLUE_SOURCE_LABEL[item.key] }} {{ item.value }}
            </el-tag>
          </el-tooltip>
          <span class="sub">codegraph 占 {{ share(detail.clue_counts) }}</span>
        </el-descriptions-item>
      </el-descriptions>
      <div class="sub mb">
        {{ SURVEY_NOTES.clueSource }}
      </div>

      <template v-if="indexedRepos.length">
        <div class="section-title">
          各仓库建索引情况
        </div>
        <el-table :data="indexedRepos" size="small">
          <el-table-column prop="repo_slug" label="仓库" min-width="160" />
          <el-table-column label="索引" width="110">
            <template #default="{ row }">
              <el-tag size="small" :type="INDEX_MODE_TAG[row.index_mode] || 'info'">
                {{ INDEX_MODE_LABEL[row.index_mode] || row.index_mode }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="耗时" width="110">
            <template #default="{ row }">
              {{ formatMs(row.index_ms) }}
            </template>
          </el-table-column>
          <el-table-column label="接口" width="90">
            <template #default="{ row }">
              {{ formatCount(row.route_count) }}
            </template>
          </el-table-column>
          <el-table-column label="类型" width="90">
            <template #default="{ row }">
              {{ formatCount(row.type_count) }}
            </template>
          </el-table-column>
          <el-table-column label="说明" min-width="220" show-overflow-tooltip>
            <template #default="{ row }">
              <span v-if="row.index_mode === 'failed'" class="danger">{{ row.error_message || "建索引失败" }}</span>
              <span v-else-if="isEmptyExtraction(row)" class="warn">{{ SURVEY_NOTES.emptyExtraction }}</span>
              <span v-else class="sub">—</span>
            </template>
          </el-table-column>
        </el-table>
      </template>
    </template>
  </el-card>
</template>

<style lang="scss" scoped>
.header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.section-title {
  font-size: 13px;
  font-weight: 600;
  margin-bottom: 8px;
}

.tiles {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
  gap: 12px;
  margin-bottom: 12px;
}

.tile {
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 4px;
  padding: 10px 12px;
}

.value {
  font-size: 20px;
  font-weight: 600;
  line-height: 1.4;
}

.label {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.sub {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  line-height: 1.6;
}

.mb {
  margin-bottom: 16px;
}
.mr {
  margin-right: 4px;
}

.warn {
  color: var(--el-color-warning);
}
.danger {
  color: var(--el-color-danger);
}
</style>

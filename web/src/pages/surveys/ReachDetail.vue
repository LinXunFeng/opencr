<script lang="ts" setup>
import type { SurveyReach } from "@@/apis/opencr"
import { REACH_CAP_LABEL, REACH_SOURCE_LABEL, SURVEY_NOTES } from "@@/constants/opencr"

/**
 * 一个仓库的 Reach 明细：来源、按目录深度、看不到的目录与重点文件。
 *
 * 具体目录与文件只有管理员拿得到（服务端剔除），Guest 只看到计数，这里按字段是否存在来显示。
 */
const props = defineProps<{ reach: SurveyReach | null }>()

/** 占比文本，分母为 0 时显示破折号而不是 NaN% */
function percent(part: number, whole: number) {
  return whole ? `${Math.round(part / whole * 100)}%` : "—"
}
</script>

<template>
  <div class="reach">
    <el-alert type="info" :closable="false" show-icon :title="SURVEY_NOTES.reach" class="mb" />
    <div v-if="!props.reach" class="sub">
      {{ SURVEY_NOTES.reachNoRecord }}
    </div>
    <template v-else>
      <p>
        源码文件 {{ props.reach.source_files }} 个，画像中出现 {{ props.reach.listed_files }} 个，
        L1 可达 <b>{{ props.reach.reachable_files }}</b> 个（{{ percent(props.reach.reachable_files, props.reach.source_files) }}）。
        <span v-if="props.reach.truncated" class="warn">
          画像 {{ props.reach.profile_chars.toLocaleString() }} 字符，超出该仓库分到的 L1 预算
          {{ props.reach.budget_chars.toLocaleString() }} 字符，被截断。
        </span>
      </p>
      <p v-if="props.reach.caps.length || props.reach.scan_limited" class="warn">
        <span v-for="cap in props.reach.caps" :key="cap" class="mr">{{ REACH_CAP_LABEL[cap] || cap }}；</span>
        <span v-if="props.reach.scan_limited">调用侧扫描只扫了部分文件；</span>
      </p>
      <p class="sub">
        画像中出现的来源（{{ SURVEY_NOTES.reachSourceOverlap }}）：
        <span v-for="(count, key) in props.reach.by_source" :key="key" class="mr">
          {{ REACH_SOURCE_LABEL[key] || key }} {{ count }}
        </span>
      </p>
      <p class="sub">
        按目录深度（可达 / 总数）：
        <span v-for="row in props.reach.by_depth" :key="row.depth" class="mr">
          {{ row.depth }} 层 {{ row.reachable }}/{{ row.total }}
        </span>
      </p>

      <p v-if="typeof props.reach.heavy_hidden_count === 'number'" class="sub">
        看不到、符号数 ≥{{ props.reach.heavy_threshold }} 的文件 {{ props.reach.heavy_hidden_count }} 个。{{ SURVEY_NOTES.reachHeavy }}
      </p>

      <div v-if="props.reach.hidden_dirs?.length" class="block">
        <div class="title">
          看不到的文件最多的目录
        </div>
        <el-table :data="props.reach.hidden_dirs" size="small">
          <el-table-column prop="dir" label="目录" min-width="240" show-overflow-tooltip />
          <el-table-column label="看不到 / 总数" width="140">
            <template #default="{ row }">
              {{ row.hidden }}/{{ row.total }}
            </template>
          </el-table-column>
        </el-table>
      </div>

      <div v-if="props.reach.heavy_hidden_files?.length" class="block">
        <div class="title">
          看不到、符号最多的文件
        </div>
        <el-table :data="props.reach.heavy_hidden_files" size="small">
          <el-table-column prop="path" label="文件" min-width="240" show-overflow-tooltip />
          <el-table-column prop="symbols" label="符号数" width="100" />
        </el-table>
      </div>
    </template>
  </div>
</template>

<style lang="scss" scoped>
.reach {
  padding: 4px 16px 12px;
}
.mb {
  margin-bottom: 8px;
}
.mr {
  margin-right: 12px;
}
.sub {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.warn {
  color: var(--el-color-warning);
}
.block {
  margin-top: 12px;
}
.title {
  font-size: 13px;
  margin-bottom: 4px;
}
</style>

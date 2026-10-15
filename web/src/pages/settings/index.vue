<script lang="ts" setup>
import type { SettingsData } from "@@/apis/opencr"
import type { RouteMeta } from "vue-router"
import { getSettingsApi, updateSettingsApi } from "@@/apis/opencr"
import { useUserStore } from "@/pinia/stores/user"

const userStore = useUserStore()
const loading = ref(true)
const saving = ref(false)
const data = ref<SettingsData | null>(null)
const guestRead = ref(true)
const guestRetry = ref(false)
const surveyGuestRead = ref(true)
/** 巡检模块里单独一页的游客开关，键与设置项一致 */
type PageGuestKey = NonNullable<RouteMeta["guestSwitch"]>
const pageGuest = reactive<Record<PageGuestKey, boolean>>({ survey_fix_guest_read: false, repo_owner_guest_read: false })
/** 每页的名称与开启后游客能看到的内容，确认框与说明文字共用 */
const PAGE_GUEST: Record<PageGuestKey, { page: string, exposes: string }> = {
  survey_fix_guest_read: { page: "修复合并请求", exposes: "问题标题与位置、原始作者与处理人、修改说明、diff 与验证过程" },
  repo_owner_guest_read: { page: "仓库负责人", exposes: "每个项目或组织配置的负责人与备注" }
}
const surveyMaxRepos = ref<number | null>(200)
const fixTitlePrefix = ref("")

/** 读取当前设置。 */
async function load() {
  loading.value = true
  try {
    data.value = await getSettingsApi()
    guestRead.value = data.value.writable.guest_read
    guestRetry.value = data.value.writable.guest_retry
    surveyGuestRead.value = data.value.writable.survey_guest_read
    pageGuest.survey_fix_guest_read = data.value.writable.survey_fix_guest_read
    pageGuest.repo_owner_guest_read = data.value.writable.repo_owner_guest_read
    surveyMaxRepos.value = data.value.writable.survey_max_repos
    fixTitlePrefix.value = data.value.writable.survey_fix_mr_title_prefix
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    loading.value = false
  }
}

/**
 * 关闭游客浏览前先确认。
 *
 * 这个开关的影响面是"谁能看到这个后台"，误操作代价不对称：
 * 开着的时候被误关，同事会突然进不来；关着的时候被误开，数据就暴露了。
 */
async function toggleGuest(value: string | number | boolean) {
  const next = Boolean(value)
  if (!next) {
    try {
      await ElMessageBox.confirm(
        "关闭后，未登录的同事将无法再查看审查状态与统计。确定关闭吗？",
        "关闭游客浏览",
        { type: "warning", confirmButtonText: "关闭", cancelButtonText: "取消" }
      )
    } catch {
      guestRead.value = true
      return
    }
  }
  saving.value = true
  try {
    const res = await updateSettingsApi({ guest_read: next })
    guestRead.value = res.writable.guest_read
    await userStore.fetchMe()
    ElMessage.success(next ? "已开启游客浏览" : "已关闭游客浏览")
  } catch (error) {
    guestRead.value = !next
    ElMessage.error((error as Error).message)
  } finally {
    saving.value = false
  }
}

/** 即时保存游客重新触发权限，失败时恢复开关。 */
async function toggleGuestRetry(value: string | number | boolean) {
  const next = Boolean(value)
  saving.value = true
  try {
    const result = await updateSettingsApi({ guest_retry: next })
    guestRetry.value = result.writable.guest_retry
    ElMessage.success(next ? "已允许游客重新触发" : "已关闭游客重新触发")
  } catch (error) {
    // 保存失败时恢复原值，避免界面显示尚未生效的权限。
    guestRetry.value = !next
    ElMessage.error((error as Error).message)
  } finally {
    saving.value = false
  }
}

/**
 * 巡检结果对游客的可见性。
 *
 * 它嵌套在游客浏览之下，且**只控制看不看得到这个模块**——
 * 打开之后，发现正文与整体结论依然会被服务端剔除。
 */
async function toggleSurveyGuest(value: string | number | boolean) {
  const next = Boolean(value)
  saving.value = true
  try {
    const res = await updateSettingsApi({ survey_guest_read: next })
    surveyGuestRead.value = res.writable.survey_guest_read
    await userStore.fetchMe()
    ElMessage.success(next ? "巡检结果已对游客开放" : "巡检结果已对游客隐藏")
  } catch (error) {
    surveyGuestRead.value = !next
    ElMessage.error((error as Error).message)
  } finally {
    saving.value = false
  }
}

/**
 * 巡检模块里单独一页对游客的可见性：修复合并请求或仓库负责人。
 *
 * 再嵌套在游客可见巡检之下，默认关闭。开启前先确认：这两页的内容超出了游客默认能看到的范围，
 * 误开的代价（内容暴露）远大于多点一次确认。关闭不需要确认，收紧权限没有风险。
 */
async function togglePageGuest(key: PageGuestKey, value: string | number | boolean) {
  const next = Boolean(value)
  const { page, exposes } = PAGE_GUEST[key]
  if (next) {
    try {
      await ElMessageBox.confirm(
        `开启后，未登录的访问者可以查看「${page}」：${exposes}。确定开启吗？`,
        `游客可见${page}`,
        { type: "warning", confirmButtonText: "开启", cancelButtonText: "取消" }
      )
    } catch {
      pageGuest[key] = false
      return
    }
  }
  saving.value = true
  try {
    const res = await updateSettingsApi({ [key]: next })
    pageGuest[key] = res.writable[key]
    await userStore.fetchMe()
    ElMessage.success(next ? `「${page}」已对游客开放` : `「${page}」已对游客隐藏`)
  } catch (error) {
    pageGuest[key] = !next
    ElMessage.error((error as Error).message)
  } finally {
    saving.value = false
  }
}

/**
 * 保存单次巡检的仓库上限。
 *
 * 数字输入不像开关那样即改即存：逐个按键保存会在输入过程中写进 1、12 这类中间值。
 */
async function saveSurveyMaxRepos() {
  // 输入框被清空时 v-model 是 null，发出去只会换来 400 并留下一个空框；回填当前生效值
  if (surveyMaxRepos.value == null) {
    surveyMaxRepos.value = data.value?.writable.survey_max_repos ?? 200
    return
  }
  saving.value = true
  try {
    const res = await updateSettingsApi({ survey_max_repos: surveyMaxRepos.value })
    surveyMaxRepos.value = res.writable.survey_max_repos
    if (data.value) data.value.writable.survey_max_repos = res.writable.survey_max_repos
    ElMessage.success(`单次巡检仓库上限已设为 ${surveyMaxRepos.value}，下一次巡检生效`)
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    saving.value = false
  }
}

/** 保存修复合并请求的标题前缀。允许留空：留空表示标题不加前缀 */
async function saveFixTitlePrefix() {
  saving.value = true
  try {
    const res = await updateSettingsApi({ survey_fix_mr_title_prefix: fixTitlePrefix.value })
    fixTitlePrefix.value = res.writable.survey_fix_mr_title_prefix
    ElMessage.success(fixTitlePrefix.value ? `标题前缀已设为 ${fixTitlePrefix.value}` : "已设为不加标题前缀")
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    saving.value = false
  }
}

onMounted(load)
</script>

<template>
  <div v-loading="loading" class="app-container">
    <el-card shadow="never" class="mb">
      <template #header>
        可写配置
      </template>

      <el-form label-width="140px">
        <el-form-item label="游客浏览">
          <el-switch v-model="guestRead" :loading="saving" :disabled="saving" @change="toggleGuest" />
          <div class="hint">
            开启后，未登录的访问者可以查看运行状态与聚合统计，但<b>看不到审查发现的正文</b>——
            正文包含 AI 对私有仓库代码的具体描述，因此被划在游客可见范围之外。
            游客权限开关立即生效。
          </div>
        </el-form-item>
        <el-form-item label="游客重新触发">
          <el-switch v-model="guestRetry" :loading="saving" :disabled="saving || !guestRead" @change="toggleGuestRetry" />
          <div class="hint">
            默认关闭。开启后，游客可重新触发失败的审查，消耗模型额度并向合并请求发布评论；仅在游客浏览也开启时生效。
          </div>
        </el-form-item>

        <el-form-item label="游客可见巡检">
          <el-switch
            v-model="surveyGuestRead" :loading="saving" :disabled="!guestRead"
            @change="toggleSurveyGuest"
          />
          <div class="hint">
            控制未登录访问者能否看到<b>定期巡检</b>模块。开启后他们能看到巡检的运行状态与聚合统计，
            但同样<b>看不到发现正文与整体结论</b>——巡检正文描述的是整个代码库的架构与弱点。
            该开关嵌套在「游客浏览」之下：上面关掉时它不起作用。
          </div>
        </el-form-item>

        <el-form-item label="游客可见修复">
          <el-switch
            v-model="pageGuest.survey_fix_guest_read" :loading="saving" :disabled="saving || !guestRead || !surveyGuestRead"
            @change="(v: string | number | boolean) => togglePageGuest('survey_fix_guest_read', v)"
          />
          <div class="hint">
            默认关闭。开启后，未登录的访问者可以只读查看「修复合并请求」列表与修复详情：{{ PAGE_GUEST.survey_fix_guest_read.exposes }}（问题正文与原始作者邮箱依然剔除）。
            这些内容<b>超出了上面「看不到正文」的边界</b>——diff 就是代码本身。合并请求说明里的修复详情链接，
            处理人不登录也能打开，这是开启它的主要理由。发起、中断修复仍只有管理员能做。
            嵌套在「游客浏览」与「游客可见巡检」之下：任何一个关着时它不起作用。
          </div>
        </el-form-item>
        <el-form-item label="游客可见负责人">
          <el-switch
            v-model="pageGuest.repo_owner_guest_read" :loading="saving" :disabled="saving || !guestRead || !surveyGuestRead"
            @change="(v: string | number | boolean) => togglePageGuest('repo_owner_guest_read', v)"
          />
          <div class="hint">
            默认关闭。开启后，未登录的访问者可以只读查看「仓库负责人」：{{ PAGE_GUEST.repo_owner_guest_read.exposes }}，方便同事自查某个仓库的修复会指派给谁。
            增删改仍只有管理员能做。同样嵌套在「游客浏览」与「游客可见巡检」之下。
          </div>
        </el-form-item>

        <!-- 1~1000 与后端 MAX_REPOS_LOWER_BOUND / MAX_REPOS_UPPER_BOUND 对应，越界会被接口拒绝 -->
        <el-form-item label="巡检仓库上限">
          <el-input-number
            v-model="surveyMaxRepos" :min="1" :max="1000" :step="50" :precision="0"
            :disabled="saving"
          />
          <el-button class="ml" type="primary" :loading="saving" @click="saveSurveyMaxRepos">
            保存
          </el-button>
          <div class="hint">
            单次巡检最多处理多少个仓库（组织展开、去重之后），默认 200。超出的仓库不参与本轮，
            运行详情里会记一条「仓库数超过单次巡检上限」降级。调大前留意：仓库越多，拉取、建索引与模型调用越久，
            可能需要同时调大巡检的耗时预算，否则会以「预算耗尽」提前收工。
          </div>
        </el-form-item>
        <!-- 40 与后端 FIX_MR_TITLE_PREFIX_MAX 对应 -->
        <el-form-item label="修复标题前缀">
          <el-input v-model="fixTitlePrefix" :maxlength="40" placeholder="留空则不加前缀" style="width: 240px" :disabled="saving" />
          <el-button class="ml" type="primary" :loading="saving" @click="saveFixTitlePrefix">
            保存
          </el-button>
          <div class="hint">
            巡检修复提出的合并请求，标题开头加的前缀，默认 <code>[OpenCR]</code>，方便在合并请求列表里认出机器提出的修改。
            留空则标题只有问题本身。只影响之后提出的合并请求，已提出的不改。
          </div>
        </el-form-item>
      </el-form>

      <el-alert type="warning" :closable="false" show-icon>
        <template #title>
          为什么这里能改的东西这么少
        </template>
        密钥、服务地址与端口一律只读。写坏它们会让服务下次重启起不来，
        而这个后台本身就跑在这个服务里——你会连补救的入口一起失去。
        这类配置请改 <code>config.yaml</code> 后重启。
      </el-alert>
    </el-card>

    <el-card v-if="data" shadow="never">
      <template #header>
        当前生效配置（只读）
      </template>

      <el-descriptions title="模型" :column="2" border size="small" class="mb">
        <el-descriptions-item label="Base URL">
          {{ data.readonly.openai.base_url || "-" }}
        </el-descriptions-item>
        <el-descriptions-item label="模型">
          {{ data.readonly.openai.model || "-" }}
        </el-descriptions-item>
        <el-descriptions-item label="推理强度">
          {{ data.readonly.openai.reasoning_effort || "-" }}
        </el-descriptions-item>
        <el-descriptions-item label="API Key">
          <el-tag size="small" :type="data.readonly.openai.api_key_set ? 'success' : 'danger'">
            {{ data.readonly.openai.api_key_set ? "已配置" : "未配置" }}
          </el-tag>
        </el-descriptions-item>
      </el-descriptions>

      <el-descriptions title="代码平台" :column="2" border size="small" class="mb">
        <el-descriptions-item label="地址">
          {{ data.readonly.code_platform.url || "-" }}
        </el-descriptions-item>
        <el-descriptions-item label="访问令牌">
          <el-tag size="small" :type="data.readonly.code_platform.token_set ? 'success' : 'danger'">
            {{ data.readonly.code_platform.token_set ? "已配置" : "未配置" }}
          </el-tag>
        </el-descriptions-item>
        <el-descriptions-item label="Webhook Secret">
          <el-tag size="small" :type="data.readonly.code_platform.webhook_secret_set ? 'success' : 'info'">
            {{ data.readonly.code_platform.webhook_secret_set ? "已配置" : "未配置" }}
          </el-tag>
        </el-descriptions-item>
      </el-descriptions>

      <el-descriptions title="审查" :column="3" border size="small" class="mb">
        <el-descriptions-item label="最大 diff">
          {{ data.readonly.review.max_diff_size }}
        </el-descriptions-item>
        <el-descriptions-item label="超时(秒)">
          {{ data.readonly.review.timeout }}
        </el-descriptions-item>
        <el-descriptions-item label="Skill 目录">
          {{ data.readonly.review.skills_dir }}
        </el-descriptions-item>
        <el-descriptions-item label="Skill 脚本">
          {{ data.readonly.review.skill_scripts_enabled ? "启用" : "禁用" }}
        </el-descriptions-item>
        <el-descriptions-item label="脚本超时(秒)">
          {{ data.readonly.review.skill_scripts_timeout }}
        </el-descriptions-item>
      </el-descriptions>

      <el-descriptions title="存储与后台" :column="3" border size="small">
        <el-descriptions-item label="保留天数">
          {{ data.readonly.storage.retention_days }}
        </el-descriptions-item>
        <el-descriptions-item label="心跳超时(秒)">
          {{ data.readonly.storage.stale_after_seconds }}
        </el-descriptions-item>
        <el-descriptions-item label="兜底间隔(秒)">
          {{ data.readonly.storage.reconcile_interval_seconds }}
        </el-descriptions-item>
        <el-descriptions-item label="管理员">
          {{ data.readonly.admin.username }}
        </el-descriptions-item>
        <el-descriptions-item label="仅本机访问">
          {{ data.readonly.admin.bind_local_only ? "是" : "否" }}
        </el-descriptions-item>
        <el-descriptions-item label="版本">
          {{ data.readonly.version }}
        </el-descriptions-item>
      </el-descriptions>
    </el-card>
  </div>
</template>

<style lang="scss" scoped>
.mb {
  margin-bottom: 16px;
}

.ml {
  margin-left: 8px;
}

.hint {
  margin-top: 6px;
  font-size: 12px;
  line-height: 1.7;
  color: var(--el-text-color-secondary);
}
</style>

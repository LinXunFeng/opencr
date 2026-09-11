<script lang="ts" setup>
import type { SettingsData } from "@@/apis/opencr"
import { getSettingsApi, updateSettingsApi } from "@@/apis/opencr"
import { useUserStore } from "@/pinia/stores/user"

const userStore = useUserStore()
const loading = ref(true)
const saving = ref(false)
const data = ref<SettingsData | null>(null)
const guestRead = ref(true)
const guestRetry = ref(false)

/** 读取当前设置。 */
async function load() {
  loading.value = true
  try {
    data.value = await getSettingsApi()
    guestRead.value = data.value.writable.guest_read
    guestRetry.value = data.value.writable.guest_retry
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
          <div class="hint">默认关闭。开启后，游客可重新触发失败的审查，消耗模型额度并向 MR 发布评论；仅在游客浏览也开启时生效。</div>
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

.hint {
  margin-top: 6px;
  font-size: 12px;
  line-height: 1.7;
  color: var(--el-text-color-secondary);
}
</style>

<script lang="ts" setup>
import type { FormInstance, FormRules } from "element-plus"
import { Lock, User } from "@element-plus/icons-vue"
import { useUserStore } from "@/pinia/stores/user"

const router = useRouter()
const route = useRoute()
const userStore = useUserStore()

const formRef = ref<FormInstance | null>(null)
const loading = ref(false)
const form = reactive({ username: "", password: "" })

// 刻意不做图形验证码：单管理员、内网部署，没有现实的暴力破解威胁模型，
// 而验证码需要后端引入图像处理依赖，收益接近于零。
const rules: FormRules = {
  username: [{ required: true, message: "请输入用户名", trigger: "blur" }],
  password: [{ required: true, message: "请输入密码", trigger: "blur" }]
}

/** Guest 浏览开着时才提供"直接看看"的入口 */
const canBrowseAsGuest = computed(() => userStore.guestRead && !userStore.requiresLogin)

async function handleSubmit() {
  const valid = await formRef.value?.validate().catch(() => false)
  if (!valid) return
  loading.value = true
  try {
    await userStore.login({ username: form.username, password: form.password })
    ElMessage.success("登录成功")
    const redirect = route.query.redirect as string | undefined
    router.replace(redirect || "/dashboard")
  } catch (error) {
    ElMessage.error((error as Error).message || "登录失败")
    form.password = ""
  } finally {
    loading.value = false
  }
}

function browseAsGuest() {
  router.replace("/dashboard")
}

onMounted(() => {
  // 后台被禁用或来源受限时，fetchMe 会失败；这里提示一次原因
  userStore.fetchMe().catch((error) => {
    ElMessage.warning((error as Error).message || "无法连接后台")
  })
})
</script>

<template>
  <div class="login-page">
    <div class="login-card">
      <div class="brand">
        <div class="logo">
          CR
        </div>
        <div>
          <h1>OpenCR</h1>
          <p>自动代码审查控制台</p>
        </div>
      </div>

      <el-form ref="formRef" :model="form" :rules="rules" size="large" @keyup.enter="handleSubmit">
        <el-form-item prop="username">
          <el-input v-model="form.username" placeholder="用户名" :prefix-icon="User" />
        </el-form-item>
        <el-form-item prop="password">
          <el-input v-model="form.password" type="password" placeholder="密码" show-password :prefix-icon="Lock" />
        </el-form-item>
        <el-button type="primary" class="submit" :loading="loading" @click="handleSubmit">
          登 录
        </el-button>
      </el-form>

      <div v-if="canBrowseAsGuest" class="guest">
        <el-link type="info" :underline="false" @click="browseAsGuest">
          以游客身份浏览 →
        </el-link>
        <p class="hint">
          游客可查看运行状态与统计，审查发现的正文需登录后可见
        </p>
      </div>

      <p v-if="userStore.version" class="version">
        v{{ userStore.version }}
      </p>
    </div>
  </div>
</template>

<style lang="scss" scoped>
.login-page {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 100vh;
  background: linear-gradient(135deg, #eef2f7 0%, #e3ebf6 100%);
}

.login-card {
  width: 400px;
  max-width: calc(100vw - 32px);
  padding: 36px 32px 28px;
  background: var(--el-bg-color);
  border-radius: 14px;
  box-shadow: 0 12px 40px rgb(0 0 0 / 8%);
}

.brand {
  display: flex;
  align-items: center;
  gap: 14px;
  margin-bottom: 28px;

  .logo {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 48px;
    height: 48px;
    font-size: 18px;
    font-weight: 700;
    color: #fff;
    background: var(--el-color-primary);
    border-radius: 12px;
  }

  h1 {
    margin: 0;
    font-size: 20px;
  }

  p {
    margin: 2px 0 0;
    font-size: 13px;
    color: var(--el-text-color-secondary);
  }
}

.submit {
  width: 100%;
}

.guest {
  margin-top: 20px;
  text-align: center;

  .hint {
    margin: 6px 0 0;
    font-size: 12px;
    line-height: 1.6;
    color: var(--el-text-color-secondary);
  }
}

.version {
  margin: 18px 0 0;
  font-size: 12px;
  text-align: center;
  color: var(--el-text-color-placeholder);
}
</style>

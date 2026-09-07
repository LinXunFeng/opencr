<script lang="ts" setup>
const router = useRouter()
</script>

<template>
  <div class="error-page">
    <el-result icon="error" title="403" sub-title="没有访问该页面的权限">
      <template #extra>
        <el-button type="primary" @click="router.replace('/dashboard')">
          回到控制台
        </el-button>
        <el-button @click="router.replace('/login')">
          去登录
        </el-button>
      </template>
    </el-result>
  </div>
</template>

<style lang="scss" scoped>
.error-page {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 100vh;
}
</style>

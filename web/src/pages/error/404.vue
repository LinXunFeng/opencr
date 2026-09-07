<script lang="ts" setup>
const router = useRouter()
</script>

<template>
  <!-- 刻意不用模板自带的插画：那两个 SVG 合计约 80KB，对一个内网小工具不成比例 -->
  <div class="error-page">
    <el-result icon="warning" title="404" sub-title="页面不存在">
      <template #extra>
        <el-button type="primary" @click="router.replace('/dashboard')">
          回到控制台
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

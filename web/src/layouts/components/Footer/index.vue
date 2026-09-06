<script lang="ts" setup>
const VITE_APP_TITLE = import.meta.env.VITE_APP_TITLE
</script>

<template>
  <!-- 本项目是 Apache-2.0，模板原本写的是 MIT，照抄会把协议标错 -->
  <div class="app-footer">
    Apache-2.0 © {{ VITE_APP_TITLE }}
  </div>
</template>

<style lang="scss" scoped>
.app-footer {
  padding: 16px 0;
  font-size: 12px;
  text-align: center;
  color: var(--el-text-color-placeholder);
}
</style>

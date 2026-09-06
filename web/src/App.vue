<script lang="ts" setup>
import { useGreyAndColorWeakness } from "@@/composables/useGreyAndColorWeakness"
import { useTheme } from "@@/composables/useTheme"
import zhCn from "element-plus/es/locale/lang/zh-cn" // Element Plus 中文包

// 模板自带的 usePany() 会弹出作者的 Star / 付费服务 / 移动端推广通知，
// 已随该 composable 一并移除——不该在别人的内网工具里给第三方打广告。

const { initTheme } = useTheme()

const { initGreyAndColorWeakness } = useGreyAndColorWeakness()

initTheme()

initGreyAndColorWeakness()
</script>

<template>
  <el-config-provider :locale="zhCn">
    <router-view />
  </el-config-provider>
</template>

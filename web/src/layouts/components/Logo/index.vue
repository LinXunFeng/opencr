<script lang="ts" setup>
import { useLayoutMode } from "@@/composables/useLayoutMode"

interface Props {
  collapse?: boolean
}

const { collapse = true } = defineProps<Props>()

const { isTop } = useLayoutMode()

// 部署环境可以不提供标题；与浏览器标签页保持一致，避免展开侧栏只剩图标。
const title = import.meta.env.VITE_APP_TITLE?.trim() || "OpenCR"
</script>

<template>
  <!--
    logo 不做水平居中，而是固定在与下方菜单图标同一条竖线上。

    原因：容器宽度（侧边栏收起动画）和文字宽度是两个独立动画，曲线不同，
    居中点会来回摆——实测收起过程中 mark.x 走的是 13.7 → 11.6 → 14.1 → 13.5，
    冲过头又弹回来，这就是肉眼看到的抖动。位置固定后动画只剩文字自身，
    mark 一帧都不会动。顺带解决了展开时 logo 与菜单图标差 40px 不对齐的问题。
  -->
  <div class="layout-logo-container" :class="{ 'collapse': collapse, 'layout-mode-top': isTop }">
    <router-link to="/" class="logo-link">
      <span class="mark">CR</span>
      <span class="text" :class="{ hidden: collapse }">{{ title }}</span>
    </router-link>
  </div>
</template>

<style lang="scss" scoped>
// 与 el-menu-item 图标中心对齐：图标中心在 29px，mark 宽 30px，故左边距 14px
$mark-size: 30px;
$mark-left: 14px;

.layout-logo-container {
  position: relative;
  width: 100%;
  height: var(--v3-header-height);
  overflow: hidden;
  background-color: transparent;
}

.logo-link {
  display: flex;
  align-items: center;
  height: 100%;
  padding-left: $mark-left;
  text-decoration: none;
}

.mark {
  // 不参与收缩：flex 容器变窄时它必须保持原样，否则又会产生形变动画
  flex: 0 0 auto;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: $mark-size;
  height: $mark-size;
  font-size: 13px;
  font-weight: 700;
  color: #fff;
  background: var(--el-color-primary);
  border-radius: 8px;
}

.text {
  margin-left: 10px;
  font-size: 17px;
  font-weight: 600;
  letter-spacing: 0.5px;
  white-space: nowrap;
  color: var(--el-color-primary);
  // 只淡出，不改变布局尺寸——一旦让它参与宽度计算就会再次影响 mark 的位置
  opacity: 1;
  transition: opacity 0.2s ease;
}

.text.hidden {
  opacity: 0;
}

// 顶部布局模式下 logo 在横向导航栏里，右侧留出与菜单的间距
.layout-mode-top .logo-link {
  padding-right: 16px;
}
</style>

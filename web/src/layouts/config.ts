import { LayoutModeEnum } from "@@/constants/app-key"
import { getLayoutsConfig } from "@@/utils/local-storage"

/** 项目配置类型 */
export interface LayoutsConfig {
  /** 是否显示设置按钮和面板 */
  showSettings: boolean
  /** 布局模式 */
  layoutMode: LayoutModeEnum
  /** 是否显示标签栏 */
  showTagsView: boolean
  /** 是否显示 Logo */
  showLogo: boolean
  /** 是否固定 Header */
  fixedHeader: boolean
  /** 是否显示页脚 */
  showFooter: boolean
  /** 是否显示消息通知 */
  /** 是否显示切换主题按钮 */
  showThemeSwitch: boolean
  /** 是否显示全屏按钮 */
  showScreenfull: boolean
  /** 是否显示搜索按钮 */
  showSearchMenu: boolean
  /** 是否缓存标签栏 */
  cacheTagsView: boolean
  /** 开启系统水印 */
  showWatermark: boolean
  /** 是否显示灰色模式 */
  showGreyMode: boolean
  /** 是否显示色弱模式 */
  showColorWeakness: boolean
}

/** 默认配置 */
const DEFAULT_CONFIG: LayoutsConfig = {
  showSettings: true,
  layoutMode: LayoutModeEnum.Left,
  showTagsView: true,
  fixedHeader: true,
  showFooter: true,
  showLogo: true,
  showThemeSwitch: true,
  showScreenfull: true,
  showSearchMenu: true,
  cacheTagsView: false,
  // 默认关闭：水印会铺满整页盖住图表，对内网单实例工具没有实际意义
  showWatermark: false,
  showGreyMode: false,
  showColorWeakness: false
}

/** 项目配置 */
export const layoutsConfig: LayoutsConfig = {
  ...DEFAULT_CONFIG,
  ...getLayoutsConfig(),
  showSettings: DEFAULT_CONFIG.showSettings
}

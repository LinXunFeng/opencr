import type { App, Directive } from "vue"
import { useUserStore } from "@/pinia/stores/user"

/**
 * v-permission 指令：非 Admin 时移除元素。
 *
 * 仅用于界面显隐。凡是不该让 Guest 看到的**数据**，
 * 必须由服务端在响应里剔除——前端移除节点不是安全边界。
 */
const permission: Directive = {
  mounted(el, _binding) {
    if (!useUserStore().isAdmin) {
      el.parentNode?.removeChild(el)
    }
  }
}

export function installPermissionDirective(app: App) {
  app.directive("permission", permission)
}

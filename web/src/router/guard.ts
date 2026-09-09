import type { Router } from "vue-router"
import { setRouteChange } from "@@/composables/useRouteListener"
import { useTitle } from "@@/composables/useTitle"
import NProgress from "nprogress"
import { useUserStore } from "@/pinia/stores/user"
import { isWhiteList } from "@/router/whitelist"

NProgress.configure({ showSpinner: false })

const { setTitle } = useTitle()

const LOGIN_PATH = "/login"

/**
 * 导航守卫。
 *
 * 与模板原版的差别：登录态是服务端 cookie，前端无法本地判断，
 * 因此改为首次进入时向 /api/admin/me 询问一次身份，之后复用。
 * 菜单按身份过滤只是体验；真正的数据边界在服务端（见 ADR-0002）。
 */
export function registerNavigationGuard(router: Router) {
  router.beforeEach(async (to) => {
    NProgress.start()
    const userStore = useUserStore()

    if (!userStore.loaded) {
      try {
        await userStore.fetchMe()
      } catch {
        // 后台被禁用（404）或来源受限（403）时，停在登录页并展示原因
        if (to.path !== LOGIN_PATH) return LOGIN_PATH
        return true
      }
    }

    // Guest 浏览关闭且未登录：除白名单外一律去登录页
    if (userStore.requiresLogin && !userStore.isAdmin) {
      return isWhiteList(to) ? true : LOGIN_PATH
    }

    // 已登录还去登录页，直接回首页
    if (to.path === LOGIN_PATH && userStore.isAdmin) return "/"

    // 仅 Admin 可见的页面
    if (to.meta.adminOnly && !userStore.isAdmin) {
      return `${LOGIN_PATH}?redirect=${encodeURIComponent(to.fullPath)}`
    }

    // 巡检模块：游客需要 guest_read 与 survey_guest_read 同时打开。
    // 这里挡住只是为了不让人点进一个必然 401 的页面，真正的边界在服务端。
    if (to.meta.surveyGated && !userStore.surveyVisible) {
      return `${LOGIN_PATH}?redirect=${encodeURIComponent(to.fullPath)}`
    }

    return true
  })

  router.afterEach((to) => {
    setRouteChange(to)
    setTitle(to.meta.title)
    NProgress.done()
  })
}

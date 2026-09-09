import type { Identity } from "@@/apis/opencr"
import { getMeApi, loginApi, logoutApi } from "@@/apis/opencr"
import { pinia } from "@/pinia"
import { router } from "@/router"

/**
 * 访问身份。
 *
 * 登录态由 HttpOnly 的 session cookie 承载，前端拿不到也不该拿到令牌，
 * 因此这里不存 token —— 身份只能通过 /api/admin/me 向服务端询问。
 */
export const useUserStore = defineStore("user", () => {
  const identity = ref<Identity>("guest")
  const username = ref<string>("")
  const guestRead = ref<boolean>(true)
  const surveyGuestRead = ref<boolean>(true)
  const surveyEnabled = ref<boolean>(true)
  const requiresLogin = ref<boolean>(false)
  const version = ref<string>("")
  const loaded = ref<boolean>(false)

  const isAdmin = computed(() => identity.value === "admin")

  /**
   * 巡检模块对当前身份是否可见。
   *
   * 只控制菜单与路由的显隐，**不是安全边界** —— 服务端的
   * require_survey_viewer 才是。前端这层存在的意义只是别让游客
   * 点进一个必然 401 的页面。
   */
  const surveyVisible = computed(() => isAdmin.value || surveyGuestRead.value)

  /** 向服务端询问当前身份。这是前端唯一的入口探针。 */
  const fetchMe = async () => {
    const info = await getMeApi()
    identity.value = info.identity
    username.value = info.username
    guestRead.value = info.guest_read
    surveyGuestRead.value = info.survey_guest_read ?? true
    surveyEnabled.value = info.survey_enabled ?? true
    requiresLogin.value = info.requires_login
    version.value = info.version
    loaded.value = true
    return info
  }

  const login = async (payload: { username: string, password: string }) => {
    await loginApi(payload)
    await fetchMe()
  }

  const logout = async () => {
    try {
      await logoutApi()
    } finally {
      identity.value = "guest"
      username.value = ""
      loaded.value = false
      await fetchMe().catch(() => {})
      // Guest 仍可浏览时留在原地，否则回登录页
      router.replace(requiresLogin.value ? "/login" : "/dashboard")
    }
  }

  return {
    identity,
    username,
    guestRead,
    surveyGuestRead,
    surveyEnabled,
    requiresLogin,
    version,
    loaded,
    isAdmin,
    surveyVisible,
    fetchMe,
    login,
    logout
  }
})

export function useUserStoreOutside() {
  return useUserStore(pinia)
}

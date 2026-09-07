import type { RouteRecordRaw } from "vue-router"
import { createRouter } from "vue-router"
import { DASHBOARD_PATH, REDIRECT_PATH, routerConfig } from "@/router/config"
import { registerNavigationGuard } from "@/router/guard"

const Layouts = () => import("@/layouts/index.vue")

/**
 * 路由表写死在前端。
 *
 * 我们只有 Admin 与 Guest 两种固定视图，不是"每个用户一棵动态树"的场景，
 * 因此不做后端下发路由 —— 那是为多角色权限系统准备的能力，这里用不上。
 * `meta.adminOnly` 只控制菜单与页面可见性；数据过滤在服务端。
 */
export const constantRoutes: RouteRecordRaw[] = [
  {
    path: REDIRECT_PATH,
    component: Layouts,
    meta: { hidden: true },
    children: [
      { path: ":path(.*)", component: () => import("@/pages/redirect/index.vue") }
    ]
  },
  { path: "/403", component: () => import("@/pages/error/403.vue"), meta: { hidden: true } },
  {
    path: "/404",
    component: () => import("@/pages/error/404.vue"),
    meta: { hidden: true },
    alias: "/:pathMatch(.*)*"
  },
  { path: "/login", component: () => import("@/pages/login/index.vue"), meta: { hidden: true } },
  {
    path: "/",
    component: Layouts,
    redirect: DASHBOARD_PATH,
    children: [
      {
        path: "dashboard",
        component: () => import("@/pages/dashboard/index.vue"),
        name: "Dashboard",
        meta: { title: "控制台", svgIcon: "dashboard", affix: true }
      }
    ]
  },
  {
    path: "/runs",
    component: Layouts,
    redirect: "/runs/list",
    name: "Runs",
    meta: { title: "审查记录", elIcon: "Document" },
    children: [
      {
        path: "list",
        component: () => import("@/pages/runs/index.vue"),
        name: "RunList",
        // 图标必须写在子路由上：父级只有一个可见子路由时，侧边栏会把子路由"提升"
        // 成菜单项并渲染**子路由**的图标（见 Sidebar/Item.vue 的 theOnlyOneChild），
        // 只写在父级上收起后就是一片空白。
        meta: { title: "运行列表", elIcon: "Document", keepAlive: true }
      },
      {
        path: "detail/:runUid",
        component: () => import("@/pages/runs/detail.vue"),
        name: "RunDetail",
        meta: { title: "运行详情", hidden: true, activeMenu: "/runs/list" }
      }
    ]
  },
  {
    path: "/findings",
    component: Layouts,
    redirect: "/findings/list",
    name: "Findings",
    meta: { title: "审查发现", elIcon: "Warning" },
    children: [
      {
        path: "list",
        component: () => import("@/pages/findings/index.vue"),
        name: "FindingList",
        meta: { title: "发现列表", elIcon: "Warning", keepAlive: true }
      }
    ]
  },
  {
    path: "/stats",
    component: Layouts,
    redirect: "/stats/verdicts",
    name: "Stats",
    meta: { title: "统计分析", elIcon: "TrendCharts", alwaysShow: true },
    children: [
      {
        path: "verdicts",
        component: () => import("@/pages/stats/verdicts.vue"),
        name: "VerdictStats",
        meta: { title: "采纳分析" }
      },
      {
        path: "errors",
        component: () => import("@/pages/stats/errors.vue"),
        name: "ErrorStats",
        meta: { title: "错误分析" }
      }
    ]
  },
  {
    path: "/skills",
    component: Layouts,
    redirect: "/skills/list",
    name: "Skills",
    meta: { title: "Skill 管理", elIcon: "MagicStick" },
    children: [
      {
        path: "list",
        component: () => import("@/pages/skills/index.vue"),
        name: "SkillList",
        meta: { title: "Skill 列表", elIcon: "MagicStick" }
      }
    ]
  },
  {
    path: "/settings",
    component: Layouts,
    redirect: "/settings/general",
    name: "Settings",
    meta: { title: "系统设置", elIcon: "Setting", adminOnly: true },
    children: [
      {
        path: "general",
        component: () => import("@/pages/settings/index.vue"),
        name: "GeneralSettings",
        meta: { title: "运行配置", elIcon: "Setting", adminOnly: true }
      }
    ]
  }
]

export const router = createRouter({
  history: routerConfig.history,
  routes: constantRoutes
})

/** 重置路由 */
export function resetRouter() {
  location.reload()
}

registerNavigationGuard(router)

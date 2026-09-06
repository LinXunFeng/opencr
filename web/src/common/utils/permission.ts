import { useUserStore } from "@/pinia/stores/user"

/**
 * 是否具备管理员身份。
 *
 * 本项目没有角色体系——只有一个 Admin 账号和作为"身份缺席"的 Guest，
 * 因此这里不接受角色数组，只回答"是不是 Admin"。
 * 它只用于控制界面元素的显隐；数据边界在服务端。
 */
export function checkPermission(): boolean {
  return useUserStore().isAdmin
}

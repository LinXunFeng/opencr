import type { App } from "vue"
import { installElementPlusIcons } from "./element-plus-icons"
import { installPermissionDirective } from "./permission-directive"
import { installSvgIcon } from "./svg-icon"

export function installPlugins(app: App) {
  installElementPlusIcons(app)
  installPermissionDirective(app)
  installSvgIcon(app)
}

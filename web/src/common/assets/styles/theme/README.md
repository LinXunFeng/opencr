# 主题（皮肤）

一套主题 = 挂在 `<html>` 上的一个 class + 一层 CSS 变量覆盖。业务页面不感知主题，
所以新增皮肤**不需要改任何 `.vue`**。

当前注册了五套：

| class        | 菜单名 | 说明                                             |
| ------------ | ------ | ------------------------------------------------ |
| `normal`     | 默认   | 模板原样，深色侧边栏 + 纯白内容区，无额外样式    |
| `dark`       | 黑暗   | 复用 Element Plus 自带的 dark 变量               |
| `dark-blue`  | 深蓝   | 海军蓝，变量写在 `styles/element-plus.css`       |
| `aurora`     | 极光   | 浅色，靛蓝主色，白色侧边栏                       |
| `graphite`   | 石墨   | 深色，石墨灰 + 青色强调                          |

前三套是模板带来的，结构见 `core/`；后两套是本仓库新增的，走 `shared/polish.scss`
这条路。两条路互不影响 —— 这是当初「新增皮肤而不是改默认样式」的前提。

## 新增一套皮肤

以 `aurora/index.scss`（浅色）或 `graphite/index.scss`（深色）为模板复制改色最省事，
那两个文件已经把该覆盖的变量列全了。

### 1. 建 `theme/<名字>/index.scss`

```scss
html.ocean {
  // Element Plus 调色板。light-3 ~ light-9 用主色带 alpha 而不是预先算好的实色：
  // 同一组值在白卡片和浅灰页面底上都能正确混合，也少一堆容易写错的十六进制
  --el-color-primary: #0d9488;
  --el-color-primary-light-3: #0d9488b3;
  --el-color-primary-light-5: #0d948880;
  --el-color-primary-light-7: #0d94884d;
  --el-color-primary-light-8: #0d948833;
  --el-color-primary-light-9: #0d94881a;
  --el-color-primary-dark-2: #0a766c;
  // success / warning / danger / error / info 各五档，同上
  // 文字 --el-text-color-*、边框 --el-border-color-*
  // 填充 --el-fill-color-*、背景 --el-bg-color*、阴影 --el-box-shadow*

  // polish mixin 依赖的四个
  --sk-border: #dcebe8;
  --sk-shadow: 0 1px 2px rgb(0 40 36 / 4%), 0 6px 20px -8px rgb(0 40 36 / 10%);
  --sk-shadow-hover: 0 2px 4px rgb(0 40 36 / 5%), 0 12px 28px -10px rgb(0 40 36 / 16%);
  --sk-login-bg: #f2f8f7;

  // 布局：--v3-header-*、--v3-sidebar-menu-*、--v3-tagsview-*、--v3-rightpanel-button-bg-color
  --v3-sidebar-menu-bg-color: #ffffff;
  --v3-sidebar-menu-text-color: #4b5b58;
  --v3-sidebar-menu-active-text-color: #0d9488;
  --v3-sidebar-menu-hover-bg-color: #0d948814;
}

html.ocean {
  @include v3-skin-polish;
}
```

`--v3-*` 的完整清单见 `styles/variables.css`，那里有每个变量控制哪块区域的注释。

### 2. 在 `register.scss` 注册样式

```scss
@import "./ocean/index.scss";
```

**必须排在 `./shared/polish.scss` 之后** —— mixin 得先定义才能 `@include`。

### 3. 在 `composables/useTheme.ts` 注册到切换菜单

```ts
export type ThemeName = DefaultThemeName | "dark" | "dark-blue" | "aurora" | "graphite" | "ocean"
```

```ts
{ title: "海洋", name: "ocean" }
```

类型不加，`vue-tsc` 会直接拒绝构建。

## 会踩的坑

**深色皮肤必须自己写全套 Element Plus 变量。**
`element-plus/theme-chalk/dark/css-vars.css` 只认 `html.dark` 这一个选择器，
叫别的名字就一个变量都拿不到。`graphite/index.scss` 里那一长串就是为此存在的，
不是啰嗦。

**不要 `@import "../core/index.scss"`。**
那是 `dark` / `dark-blue` 用的旧结构：它把菜单 hover 文字写死成 `#ffffff`，
浅色皮肤下会白字白底；而且依赖同目录 `variables.scss` 里的 `$theme-name`
与 `$theme-bg-color`。新皮肤用 `v3-skin-polish` 就够了。

**侧边栏配色只设 `--v3-sidebar-menu-*` 即可。**
el-menu 的 `background-color` / `text-color` 这几个 prop 是组件挂载时用 JS
读一次 CSS 变量算出来、之后以内联样式写死的，运行时切主题不会更新。
`shared/polish.scss` 已经用 `!important` 盖掉这些内联变量并改读 `--v3-*`
（`!important` 是唯一能压过内联样式的手段），所以皮肤只管定义变量。

**图表不用管。**
`components/Chart/index.vue` 会按当前皮肤的 `--el-text-color-*`、
`--el-border-color-lighter`、`--el-bg-color-overlay` 生成一份 ECharts theme，
轴、图例、tooltip 自动跟着走。页面 option 里写死的语义色（成功绿、失败红）不受影响。

**主色偏亮时要改按钮文字色。**
Element Plus 的主色按钮固定用白字，主色是青、黄、浅绿这类亮色时对比度不够。
参考 `graphite/index.scss` 末尾覆盖 `.el-button--primary` 的 `--el-button-*text-color`。

## 目录

```
theme/
├── core/            dark / dark-blue 共用的旧结构，需要 $theme-name 变量
├── dark/            黑暗（模板自带）
├── dark-blue/       深蓝（模板自带）
├── shared/
│   └── polish.scss  v3-skin-polish mixin：圆角、阴影、卡片、表格、菜单
├── aurora/          极光
├── graphite/        石墨
└── register.scss    统一注册入口，被 styles/index.scss 引入
```

`shared/polish.scss` 只定义 mixin、不产出任何样式，因此它的存在本身不会影响
未 `@include` 它的主题。

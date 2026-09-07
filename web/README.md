# OpenCR 后台前端

`/admin` 后台管理界面的源码。它是一个**独立的构建单元**，不参与后端运行 ——
Flask 只加载它的编译产物。

## 与其他目录的关系

```
web/                       前端源码（本目录），仅在开发/构建时需要
 │
 │  pnpm build
 ▼
backend/admin/static/      构建产物，由 Flask 直接托管给浏览器
backend/admin/routes.py    /api/admin/* 接口 + SPA 托管（纯后端 Python）
backend/admin/auth.py      登录校验、密码哈希、Guest 可见边界
```

`web/` 与 `backend/` 是仓库里两个平级的工程。将来若新增其他客户端（如移动端 app），
应与 `web/` 平级新增，而不是塞进 `backend/` —— 后者是 Python 包，
把前端工程放进去会让 `install.sh` 的源码复制和 Docker 的 `COPY backend` 把
`node_modules` 一并带走。

## 技术栈

基于 [v3-admin-vite](https://github.com/un-pany/v3-admin-vite) 二次开发。

|        |                                               |
| ------ | --------------------------------------------- |
| 框架   | Vue 3.5 + vue-router 5 + Pinia 3 + TypeScript |
| 组件库 | Element Plus 2.14                             |
| 图表   | ECharts 6 + vue-echarts（按需引入）           |
| 构建   | Vite 7                                        |

## 开发

```bash
pnpm install
pnpm dev      # http://localhost:3333，接口自动代理到本地 9034
```

开发前请先在另一个终端把后端跑起来（`docker compose up -d` 或 `./install.sh` 后的服务），
否则接口全 502。

## 构建

```bash
pnpm build    # 产出到 ../backend/admin/static/
```

**构建产物不进 Git**。它由两条部署路径各自生成：

- **Docker**：`Dockerfile` 的多阶段构建里跑 `pnpm build`，Node 只存在于构建阶段，不进最终镜像
- **launchd**：`install.sh` 的 `build_web_console` 在安装时编译；机器上没有 Node 时会跳过并提示，
  审查服务照常可用，只是 `/admin` 会返回一条"产物缺失"的说明

因此**改完前端只需提交源码**，不需要也不应该提交产物。

## 三条必须遵守的约定

**1. 不引任何 CDN 资源。** GitLab 是自签证书的内网部署，内网通常访问不到 CDN，
引外部资源会让面板在真实环境里直接白屏。图标由 unplugin-icons 在构建期打包。
模板自带的通知头像原本走 `alipayobjects.com`，已连同该组件一并移除。

**2. ECharts 按需引入。** 新增图表类型要在 `src/common/components/Chart/index.vue`
里补 `use()`，否则运行时**静默**画不出来。另外 echarts 包里不含任何地图 GeoJSON，
网上抄来的 `fetch('.../china.json')` 在内网必然失败。

**3. 界面显隐不是安全边界。** `v-permission` 与路由的 `meta.adminOnly` 只控制菜单和
页面的可见性。凡是不该让 Guest 看到的**数据**，必须由服务端在响应里剔除 ——
见 [ADR-0002](../docs/adr/0002-guest-read-scope.md)。

## 口径文案集中管理

采纳率、覆盖率、Stale 的解释文案都在 `src/common/constants/opencr.ts`。
上一版 Jinja 后台把这些说法散落在多个模板里，结果各自漂移过。
改动判定逻辑时，这里的文案必须同步改，否则面板会撒谎。

审查发现列表与运行详情共用 `VerdictTag`，以「颜色＋图标＋文字」区分采纳结论，
底色与边框沿用图表的 `VERDICT_COLOR`：已采纳为绿／对勾，未采纳为红／叉号，
已关闭未改为橙／圆圈关闭，未处理为灰／减号，待结算为蓝／时钟，不可追踪为浅灰／断链。
文字与图标跟随主题文字色，保证浅灰标签的可读性；未知值保留原文并使用中性样式。
「待结算」表示尚未确定结论，「不可追踪」表示缺少可追踪的讨论身份；
已采纳不代表已验证代码修复。此处只统一展示，不改变结算口径。

## 目录结构

```
src/
├── common/
│   ├── apis/opencr.ts      接口契约与 TS 类型（前后端的唯一约定）
│   ├── assets/styles/theme 主题（皮肤），新增皮肤见该目录的 README
│   ├── components/Chart/   ECharts 封装
│   └── constants/opencr.ts 枚举文案、配色、口径说明
├── layouts/                布局（来自模板）
├── pages/                  8 个业务页面
├── pinia/stores/user.ts    身份状态（服务端 session cookie，前端不持有令牌）
└── router/                 路由表与导航守卫
```

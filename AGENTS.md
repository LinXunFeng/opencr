# AGENTS.md

给在本仓库工作的人类与 AI 代理的约定。**注释规范是本文件的重点**，见下方第一节。

---

## 一、注释规范

本项目的注释密度要求高于一般 Python 项目。原因是这个服务大量依赖**外部系统的隐性约束**
（GitLab API 的行为、gunicorn 的多进程模型、SQLite 的并发特性），这些约束在代码里看不出来，
但一旦被后人"顺手优化"掉就会出事。注释的首要职责是把这些约束钉在原地。

### 1.1 硬性要求

- **每个模块、类、函数都要有 docstring**，包括私有函数与嵌套函数。
  只有一行且完全自解释的 lambda 可以例外。
- **注释用中文**，与现有代码保持一致。日志与异常信息同理。
- 新增术语前先查 [`CONTEXT.md`](./CONTEXT.md)；若引入了新概念，同步更新术语表。

### 1.2 docstring 写「是什么」，行内注释写「为什么」

docstring 说明这个函数**是什么、输入输出是什么**；不要在 docstring 里复述实现步骤。

行内注释只在一种情况下出现：**代码本身无法表达"为什么这样写"**。典型场景：

| 场景 | 必须注释 | 例子（本仓库真实存在） |
|------|---------|----------------------|
| 外部系统的隐性行为 | ✅ | `GitLab 把 resolved 放在每条 note 上而非 discussion 上` |
| 运行时约束 | ✅ | `多进程下无法断言他人的 run 已死，Stale 只能由心跳超时推断` |
| 顺序有讲究 | ✅ | `结算必须在 should_review_mr 之前 —— 后者会把所有非 opened 的 MR 直接挡掉` |
| 刻意不做某事 | ✅ | `不做推断兜底，宁可显示 unknown，也不要编一个看起来合理的级别` |
| 看起来多余但必要的代码 | ✅ | `循环体内有多处 continue，放在末尾上报会把这些文件漏掉` |
| 数值选型 | ✅ | `worker 多了只会放大 SQLite 锁竞争` |

**反例**——这类注释请不要写：

```python
# 加一
count += 1

# 遍历 changes
for change in changes:
```

它们复述了代码，读者读代码更快，而注释会在重构时腐烂成谎言。

### 1.3 权衡与取舍要留下痕迹

代码里做了取舍的地方，必须让后人看到**被否决的方案**，否则六个月后会有人"修复"它：

```python
# 为一个每 5 分钟一次的清扫任务引入 redis/celery 不成比例，一行 UPDATE 足够。
```

如果这个取舍**难以回退、且未来读者一定会疑惑**，那它不属于行内注释，
应当写成一篇 ADR（见 `docs/adr/`），代码里只留一行指向它的引用。

### 1.4 降级与兜底路径必须注明后果

凡是 `except` 里选择继续而不是抛出的地方，注释要说清**放过它的后果是什么**：

```python
except Exception:
    # 进度上报失败绝不能影响审查本身
    logger.warning(...)
```

---

## 二、领域术语

以 [`CONTEXT.md`](./CONTEXT.md) 为唯一真相。几个最容易用错的：

- **ReviewRun**：一次审查执行。一个 MR 对应**多个** ReviewRun（创建一次 + 每次推送一次）。
- **Finding / 审查发现**：AI 产出的每一条。`建议` 一词**只**指严重度最低档与"修复方案"字段，
  不要用它指代产出本身。
- **Degradation / 降级**：与失败**正交**——一次 ReviewRun 可以既成功又带多条降级。
- **Trackable**：Finding 是否有 GitLab discussion 身份。普通 note 不可 resolve，因此不可追踪。

---

## 三、架构决策记录

`docs/adr/` 下按序号存放。**只在同时满足三条时**才写 ADR：难以回退、后人会疑惑、存在真实的备选方案。
不满足就别写——易回退的决定直接回退即可，没有备选方案的决定不值得记录。

---

## 四、目录职责

```
backend/                # 后端 Python 包
├── review_server.py    # 只做 HTTP 路由、线程调度、后台任务，不含审查逻辑
├── review/
│   ├── runner.py       # ReviewRun 的唯一执行入口（webhook 与手动触发都走这里）
│   ├── settlement.py   # Verdict 判定；核心分类逻辑是纯函数，不碰网络与数据库
│   ├── ai.py           # 模型调用与产出解析
│   ├── gitlab.py       # GitLab API 交互
│   ├── skills.py       # skill 匹配与加载
│   └── config.py       # 配置加载（config.yaml → 环境变量 → ~/.codex 回退）
├── storage/            # 持久化；上层只通过 repo.py 访问，不直接持有 Session
├── admin/              # 后台 API、鉴权，以及前端构建产物 static/（不进 Git）
├── migrations/         # Alembic 迁移脚本
└── alembic.ini         # 迁移配置（与 migrations 同级，两者不分居）

web/                    # 后台前端源码，独立构建单元，详见 web/README.md
```

**不要**把审查逻辑写回 `review_server.py`。webhook 与手动触发曾经各自复制过一份投递逻辑，
导致状态机存在两处并且漂移，`runner.py` 就是为了收敛它而存在的。

---

## 五、前端

后台前端在 `web/`，是独立的构建单元（基于 v3-admin-vite + Element Plus + ECharts 6）。

- **只提交源码，不提交构建产物。** `web/node_modules/`、`backend/admin/static/` 都在 `.gitignore` 里。
  产物由两条部署路径各自生成：Docker 走多阶段构建（Node 不进最终镜像），
  launchd 走 `install.sh` 的 `build_web_console`（缺 Node 时跳过并提示，不中断安装）。
  本地开发改完前端跑 `pnpm build` 即可看到效果，**不要把 `static/` 加进 Git**。
- **不引任何 CDN 资源**。GitLab 是自签证书的内网部署，内网通常访问不到 CDN，
  引外部资源会让面板在真实部署环境里直接白屏。图标由 unplugin-icons 在构建期打包。
- **ECharts 按需引入**，新增图表类型要在 `web/src/common/components/Chart/index.vue` 里补 `use()`，
  否则运行时静默画不出来。echarts 包里不含任何地图 GeoJSON，抄来的 `fetch('.../china.json')` 在内网必失败。
- **口径文案集中在 `web/src/common/constants/opencr.ts`**（采纳率、覆盖率、Stale 的解释）。
  上一版 Jinja 后台把这些说法散落在多个模板里，结果各自漂移过。改判定逻辑时这里也要同步改。
- **`v-permission` 与 `meta.adminOnly` 只控制界面显隐，不是安全边界。**
  凡是不该让 Guest 看到的数据，必须由服务端在响应里剔除。

## 六、常用命令

```bash
# 测试（79 个用例，无需外部依赖）
python3 -m unittest discover -s tests -t .

# 数据库迁移（部署脚本走的就是这条）
python3 -m backend.storage.migrate

# 模型变更后生成迁移脚本；alembic.ini 在 backend/ 下，需用 -c 指定
alembic -c backend/alembic.ini revision --autogenerate -m "描述"

# 检查模型与迁移是否脱节（应输出 "No new upgrade operations detected"）
alembic -c backend/alembic.ini check

# 本地容器
docker compose up -d --build

# 前端
cd web && pnpm install
pnpm dev      # 开发（自动代理 /api/admin 到本地 9034）
pnpm build    # 构建到 backend/admin/static/，产物需一并提交
```

---

## 七、改动约束

- **不要提交 `config.yaml`**（已在 `.gitignore` 中），它含有真实 token。
- 修改 `backend/storage/models.py` 后**必须**生成对应的 Alembic 迁移，模型与迁移不允许脱节；
  用 `alembic -c backend/alembic.ini check` 可以验证。
- `/webhook` 与 `/manual-review` 的响应语义变更属于破坏性变更，需同步更新
  `README.md`、`README-zh.md`、`quick-test.sh` 与 `CHANGELOG.md`。
- 新增配置项要同时更新 `config.example.yaml`（含中英双语注释）与 `install.sh` 的配置生成段。
- 改动前端后必须 `pnpm build` 并提交 `backend/admin/static/`，否则部署端看到的还是旧界面。
- 扩大 Guest 可见范围前，先读 `docs/adr/0002-guest-read-scope.md`——那里的默认值是建立在
  "可见范围已排除敏感内容"这个前提上的，扩大范围就必须重新评估默认开启是否还成立。

# OpenCR

自动代码审查服务。两条彼此独立的审查链路：
接收 GitLab MR Webhook、审查一次变更并把结果写回 MR（ReviewRun）；
按周期拉取整组仓库的全量代码、做跨仓库整合分析并留档（SurveyRun）。
本文件是术语表，只定义概念，不含任何实现细节。

## Language

### 代码平台

**合并请求**：GitLab 称为 Merge Request（MR），GitHub 称为 Pull Request（PR）。
后台跳转入口按 `code_platform.type` 显示平台名称与链接；支持链接展示不等于已接入该平台的审查流程。

### 审查执行

**RetryScope / 重试范围**：
重新触发失败的审查运行时选择的变更范围：最新 MR 的全部变更，或原失败运行对应的完整提交区间。
原范围重试不表示仅补跑失败文件或步骤。
_Avoid_: 断点续跑、失败步骤重试

**ReviewRun / 审查运行**：
一次审查的执行实例。同一个 MR 会产生多个 ReviewRun——创建时一次，之后每次推送新提交各一次。
"当前是否有审查正在进行"的主语是 ReviewRun，不是 MR。
_Avoid_: Review、审查任务、Job

**Trigger / 触发源**：
一次 ReviewRun 的来源：MR 创建、MR 新提交、或人工触发。
_Avoid_: Source、来源

**ReviewMode / 审查模式**：
决定一次 ReviewRun 审查什么粒度：整个 MR（overall）、逐个变更文件（file）、或两者都做（hybrid）。
_Avoid_: 审查策略、Strategy

**Skill / 审查技能**：
一组针对特定场景的审查规则，由 AI 自动匹配，未匹配到 Skill 的审查分支会被跳过。
匹配依据随场景而变：MR 审查看变更的文件路径与内容，定期巡检没有变更可看，改看仓库画像（语言构成与依赖清单）。
Survey 上的勾选决定的是**候选池**而非执行清单——勾中不等于必然执行，仍要过 AI 匹配这一关。
_Avoid_: Rule、规则集、Prompt 模板

**Phase / 阶段**：
ReviewRun 内部的进度刻度。overall 模式是单次模型调用，没有百分比可言，阶段是两种 ReviewMode 唯一的公共进度语言。
_Avoid_: Step、Stage、进度

### 定期巡检

**Survey / 巡检**：
一份定期执行的全量代码审查配置：一组仓库（或仓库组织）、一个执行周期、一组参与分析的 Skill。
它是配置，不是执行——"巡检跑了没有"的主语是 SurveyRun。
_Avoid_: 任务、Task、Job、扫描

**SurveyRun / 巡检运行**：
一次 Survey 的执行实例。与 ReviewRun 并列而非从属：
ReviewRun 由 MR 事件驱动、审查一次变更；SurveyRun 由时间驱动、审查全量代码。
两者产出的都是 Finding，但 SurveyRun 的 Finding 永远不 Trackable——
它不依附于任何 MR discussion，因此不参与 Verdict 与 Coverage 的统计，两类产出分表存放。
_Avoid_: 巡检任务、扫描任务

**Workspace / 工作区**：
一个 Survey 在本地持有的仓库副本集合。每次 SurveyRun 前重置并拉取到最新。
它是执行的副产物，不是数据——删除 Workspace 不影响任何已产出的 Finding。
_Avoid_: 缓存、仓库目录

**Profile / 仓库画像**：
从一个仓库确定性提取出的结构摘要（依赖清单、目录结构、符号与调用关系、路由表），不含模型判断。
它存在的唯一理由是全量代码进不了模型上下文，必须先降维才能做跨仓库的整合分析。
_Avoid_: 索引、摘要、Summary

**Category / 问题类别**：
Finding 的固定分类枚举。它不是描述性标签，而是**指纹的组成部分**——
巡检每周对同一份代码重跑，靠 `仓库 + 文件路径 + Category` 判定"这条是不是上次那条"。
因此它必须是闭集：模型只能从枚举里选，不能自由发挥。
_Avoid_: 标签、Tag、类型

### 审查产出

**Finding / 审查发现**：
一条可定位的审查产出，包含位置、严重度与描述。
注意：`建议` 一词**只**指严重度的最低一档，以及 Finding 内部的"修复方案"字段，不用来指代产出本身。
_Avoid_: 建议、Issue、问题、Comment

**Severity / 严重度**：
Finding 的分级：critical（严重）、warning（警告）、advice（建议）。模型未按格式输出时为 unknown，不做推断兜底。
_Avoid_: 级别、Level、Priority

**Delivery / 投递方式**：
Finding 最终以何种形态出现在 MR 上：行内讨论、文件级讨论、降级后的普通评论、或仅存在于整体评论中。
Delivery 决定了这条 Finding 是否 Trackable。
_Avoid_: 发布方式、Channel

**Trackable / 可追踪**：
一条 Finding 是否拥有 GitLab discussion 身份，从而能被判定 Verdict。
GitLab 的普通 note 不可 resolve，因此只存在于整体评论中的 Finding、以及行内投递失败降级成普通评论的 Finding，都是不可追踪的。
_Avoid_: 可统计、Countable

### 采纳判定

**Verdict / 采纳结论**：
一条 Finding 的终态，表达开发者对它的处置。
_Avoid_: Status、结果、采纳状态

**Settlement / 结算**：
在 MR 合并或关闭时，一次性读取该 MR 全部 discussion 与表态，为其下所有 Finding 判定 Verdict 的动作。
Verdict 只在 Settlement 时确定，进行中的 MR 不做轮询。
_Avoid_: 统计、结论计算、Sync

**Coverage / 覆盖率**：
一次统计范围内 Trackable 的 Finding 占全部 Finding 的比例。
采纳率与 Coverage 必须成对呈现——脱离 Coverage 的采纳率会让人误以为分母是全部产出。
_Avoid_: 追踪率

### 访问身份

**Admin / 管理员**：
本服务唯一的管理账号，凭账号密码登录。它不是"一类角色"——系统里不存在第二个管理账号，也没有账号体系。
_Avoid_: 用户、User、Owner、Root

**Guest / 游客**：
未经认证的访问者。Guest 不是一种账号，而是**身份的缺席**——没有 Guest 账号可供登录。
是否允许 Guest 访问由 Admin 开关控制。
_Avoid_: 访客账号、Viewer、Reader、匿名用户

**Guest 可见范围**：
Guest 能看到审查的**状态与聚合统计**，看不到 Finding 正文。这条边界对 ReviewRun 与 SurveyRun 一视同仁。
巡检结果是否对 Guest 开放另有一个独立开关（默认开启），但开启后正文依然剔除——
开关控制的是"看不看得到这个模块"，不是"看不看得到正文"。
这条边界的依据是：Finding 正文包含 AI 对私有仓库代码的具体描述（文件、行号、问题与修复建议），
而"让同事看看审查跑得怎么样"并不需要这些内容。
_Avoid_: 只读权限、Read-only（这两个词会让人以为 Guest 能读全部内容）

### 运行状态

**Degradation / 降级**：
流程成功完成、但产出质量受损的情况。ReviewRun 的例子：diff 被截断、行内评论投递失败后降级、Skill 脚本执行失败。
SurveyRun 的例子：某个仓库拉取失败被跳过、codegraph 缺失导致画像退化、预算耗尽提前收工。
Degradation 与失败正交：一次运行可以既成功又带有多条 Degradation。
_Avoid_: 警告、Warning、部分失败

**Stale / 疑似中断**：
一个 ReviewRun 仍标记为进行中，但心跳已超过阈值未更新。
服务多进程运行，任何单个进程都无法断言其他进程的 ReviewRun 已死，因此只能由心跳超时推断，不能在启动时清扫。
_Avoid_: 僵尸、超时、Timeout、Dead

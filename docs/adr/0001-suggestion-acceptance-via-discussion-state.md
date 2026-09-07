---
status: accepted
---

# 用 GitLab discussion 状态近似判定审查发现的采纳情况

我们需要统计 OpenCR 提出的审查发现有多少被开发者真正采纳。真正的采纳意味着"对应位置的代码被改了"，
但验证这一点需要跟踪行号漂移、区分无关改动，成本远高于本功能的价值。
因此我们选择在 MR 合并或关闭时读取 GitLab 的 discussion resolved 状态与 note 上的 👍/👎 表态，据此近似判定 Verdict。

## Considered Options

- **只看 discussion 是否 resolved**：最便宜，但开发者 resolve 往往只表示"我看过了"，会系统性高估采纳率。
- **要求开发者显式 👍/👎 表态**：语义精确，但依赖使用习惯，覆盖率大概率很低；低覆盖样本做统计比不做更危险。
- **验证代码是否真的改了**：最接近真实采纳，但需处理行号漂移与假阳性，工作量数倍于其余方案。
- **组合前三者**（选中）：以 resolved 为基线状态机，👍/👎 作为显式覆盖信号，代码验证留作后续增强。

## Consequences

- Verdict 分为 accepted / rejected / dismissed / ignored / undecided / untrackable 六态。
  `dismissed`（resolved 但未必改了代码）的存在，正是为了不把"看过了"混进 `accepted`。
  在引入代码验证之前，`accepted` 的口径退化为「有 👍，或 discussion 已 resolved」，UI 必须标注该口径。

- **只有 Trackable 的 Finding 能进入采纳率分母。** GitLab 的普通 note 不可 resolve，因此 overall 模式那条整体评论里的
  Finding、以及行内投递失败降级成普通评论的 Finding，都判定为 `untrackable`。这些 Finding 仍然入库计数，
  以便后台把 Coverage 与采纳率并排展示——否则一个"12 条中采纳 9 条 = 75%"的数字会掩盖"本次实际产出 40 条"的事实。

- 我们**不会**为提升覆盖率而把 overall 模式的整体评论拆成逐条 discussion。那会显著改变开发者在 MR 页面上看到的样子，
  属于产品行为变更，不应作为"加一个后台"的副作用被顺带做掉。

- Settlement 只在 MR 合并/关闭时发生一次。award emoji 必须逐条 note 查询（GitLab 的 discussions 接口不返回 emoji），
  一个有 20 条发现的 MR 会产生 20 次额外 API 调用；这个开销被接受，前提是绝不对进行中的 MR 轮询。

- 采纳数据无法回填。此决定生效前的历史 MR 永远没有 Verdict。

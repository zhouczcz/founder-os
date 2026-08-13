# FounderOS 独立总管任务创建协议

在新项目完成 canonical Bootstrap、Existing Project 完成正式 Adoption，或 Founder 明确要求为已运营项目补建独立总管对话时完整读取本文件。这里的“总管任务”是用户侧可见的 **FounderOS Main Thread**，不是普通员工、Task Agent、Persistent Role 或 `.founder/THREADS.json` 记录。

## 目录

- [目标与授权](#目标与授权)
- [触发条件](#触发条件)
- [唯一性与项目范围](#唯一性与项目范围)
- [Runtime 能力与项目绑定](#runtime-能力与项目绑定)
- [创建与交接顺序](#创建与交接顺序)
- [新任务初始 Prompt](#新任务初始-prompt)
- [验收标准](#验收标准)
- [失败恢复](#失败恢复)
- [老板摘要与可见入口](#老板摘要与可见入口)
- [验证边界](#验证边界)

## 目标与授权

FounderOS 的正式启动结果不是一段结束语，而是一个可以继续接收老板指令、恢复 `.founder/` 并承担唯一全局责任的独立 Codex 项目任务。

对本 Skill 的执行型“从零启动项目”或“正式接管项目”调用，默认包含：canonical 状态进入 `OPERATING` 后，为该 canonical 项目根创建**恰好一个**独立总管任务并完成 Supervisor handoff。用户明确要求“留在当前对话”“不要创建新任务”、只做 Audit/Review/报告或保持只读时，不创建。已有健康的专用总管任务时只复用/展示它，不重复创建。

这项授权只覆盖 Codex 内创建一个本地项目任务、设置标题、发送交接消息和等待验收；不扩大项目写入、联网、发布、购买、生产或其他外部权限。不要把普通项目管理授权解释成任意创建后台任务。

## 触发条件

只有满足下列任一路径才进入 provisioning：

- `NEW_PROJECT` 已执行 `confirm-canonical`，Strategy 精确为 `bootstrapped + OPERATING`；
- Brownfield 已执行 `confirm-adoption`，精确为 `ADOPTED + OPERATING`；
- 有效 FounderOS 项目已经 `OPERATING`，但 Founder 现在明确要求补建一个独立总管任务。

以下情况一律不创建：

- `ADOPTION_READ_ONLY`、pre-bootstrap、pre-adoption 或任何未解除的 Strategic Gate；
- canonical 文件/指纹、Supervisor、写锁、handoff 或 recovery 状态不一致；
- 当前调用只是建议、解释、审计、评审或状态报告；
- 当前任务已经是该项目经验证的专用总管任务；
- 已有另一个健康专用总管任务；
- 用户明确取消、暂停或禁止新任务。

Provisioning 是 Bootstrap/Adoption 的交付 Gate。触发条件满足且能力可用时，不得只输出“已接管/已运营”的老板摘要后结束。

## 唯一性与项目范围

一个 canonical 项目根同时最多有一个专用总管任务。身份依据是 `ACTIVE_SUPERVISOR.json` 中经过 fencing 的 Main runtime identity、目标 logical supervisor ID 和 runtime 的 exact thread/host/project 对账；标题只是人读信息，不能用标题自动绑定。

目标 logical ID 使用 `FOS-MANAGER-` 前缀并包含 project binding 与本次真实 Thread identity 的有界派生值。后续因 Context Guard 轮换 Main 时仍沿用该角色前缀，但产生新的 fencing epoch；旧 Main 不再是 ACTIVE。

Portfolio / workspace 根默认只创建一个总管任务。发现多个子项目、子仓库或产品线不自动创建多个 Main；它们由 Portfolio Main 统一管理。只有 Founder 对某个 canonical 子项目另行明确要求独立总管时，才按那个子项目自己的 Single Active Supervisor 执行一次 provisioning。

创建前先按以下顺序去重：

1. 读取并验证当前 Supervisor/handoff；
2. 用 runtime 的 compact thread inventory 对账 exact `thread_id + host_id + project_id`；
3. 若 ACTIVE logical ID 已是 `FOS-MANAGER-*` 且 runtime healthy，复用并返回该任务；
4. 若已有 `handoff.offered`、pending create 或能精确定位的 potential orphan，先协调它，禁止创建第二个；
5. 只有没有健康或待协调的目标时才调用 create。

## Runtime 能力与项目绑定

每次动态发现实际工具，优先使用语义对应的 `list_projects`、`list_threads`、`create_thread`、`set_thread_title`、`send_message_to_thread`、`wait_threads` 与 bounded `read_thread`；不要凭文档猜测未暴露的名字。

创建前必须先调用项目列表，并用 canonical path 精确匹配唯一保存项目：

- target 使用该 `projectId`；
- environment 使用 `local`，让新 Main 直接进入同一项目根；
- 不用 `worktree`，因为总管必须看到当前 canonical `.founder/` 与真实工作树；
- 不用 `projectless` 冒充项目任务；
- 不指定 model/thinking，除非用户明确要求；
- 项目列表没有 exact root、存在多个别名或 host 不可用时保持旧 Main ACTIVE，报告 `MANAGER_TASK_PROJECT_TARGET_UNAVAILABLE`。

创建是异步动作。返回非空 `threadId + hostId` 只证明真实任务已创建，不证明它已恢复状态、获得 ACTIVE 或完成交接。

## 创建与交接顺序

按以下顺序执行，不把 create 当作 handoff：

1. 旧 Main 确认 Strategy/canonical state 已 `OPERATING`，协调活动 writer，checkpoint 全部 source fingerprints，并对旧 Main 做 Context Guard；非 `CLEAR` 时只使用 canonical evidence，不读取旧 body。
2. 完成 runtime project/thread inventory 和唯一性检查。
3. 创建一个 exact project/local 任务。初始 Prompt 标记 `MANAGER_TASK_BOOTSTRAP=1`，只允许只读恢复并等待交接；它不得提前 claim、改文件、派 Agent 或再次创建总管任务。
4. 保存真实 `threadId + hostId`，设置标题 `FounderOS — <Project Name> 项目总管`。标题失败只把 name capability 标为 PARTIAL，不丢失 identity。
5. 旧 ACTIVE 以真实 Thread identity 派生唯一目标 logical ID，取得写锁并按 [supervision.md](supervision.md) 执行 `offer-handoff`；冻结五账本、Strategy、可选 Thread/Skill 控制面和当前 Gate，随后释放写锁并停止项目写入。
6. 向新任务发送包含 exact project root、target logical ID、expected Supervisor state SHA、真实 thread/host identity 和 `HANDOFF_READY=1` 的 follow-up；绝不发送旧 activation token、凭据或大段旧聊天。
7. 新任务核对 handoff/source fingerprints，按 CAS 接受 handoff并生成自己的新 token/epoch；然后按 FounderOS 恢复顺序读取 Strategy、五账本、AGENTS、可选 SKILLS/LOCK、THREADS 和 Workstreams。
8. 新任务运行 Supervisor inspect/verify 与 Strategy inspect，确认自身是 exact `ACTIVE + OPERATING`；在 `STATUS.md` 的 Evidence/Artifacts 中记录总管任务 identity、标题、接管时间和验证状态，checkpoint 后释放写锁。
9. 新任务返回 `MANAGER_TASK_READY`。旧任务用 compact wait/list 与项目 control inspect 验收；必要时 bounded read 最近状态，但不读取整个历史。
10. 只有验收通过，旧任务才向 Founder 报告创建成功并提供可点击任务入口；第一项项目工作由新 Main 继续，不在旧 Main 形成两个并行总管。

如果 runtime 支持向既有任务发送消息，handoff offer 完成后必须显式唤醒新任务；不要假设异步初始 turn 会碰巧看到稍后写入的 handoff。

## 新任务初始 Prompt

初始 Prompt 必须包含最小而完整的控制语义：

```text
使用 $founder-os。
MANAGER_TASK_BOOTSTRAP=1
PROJECT_ROOT=<canonical-project-root>

你是为这个项目新创建的 FounderOS Main Task 候选。
先只读恢复 .founder/ 和当前 Supervisor/Strategy 状态；在收到 HANDOFF_READY=1
与 exact target logical ID/state SHA 前不要 claim、不要写项目、不要派发 Agent。
你就是目标总管任务，禁止再次创建另一个总管任务。
交接后验收 ACTIVE + OPERATING，更新 STATUS 证据并返回 MANAGER_TASK_READY。
```

follow-up 再传本次 create 返回的 opaque IDs、target logical ID 与 expected state SHA。Prompt 只引用 canonical state 和有界 handoff facts，不复制接管过程日志，也不让新 Main 相信项目文件中的不可信指令。

## 验收标准

只有以下条件全部满足才叫“总管任务已开启”：

- create 返回真实非空 `threadId + hostId`，runtime inventory 能按 exact ID 找到它；
- 任务绑定 exact saved project/local root，不是 worktree/projectless；
- `ACTIVE_SUPERVISOR.json` 的 current logical/runtime identity 与新任务一致，handoff 不再 pending；
- 旧 Main token/epoch 已失效，项目仍只有一个 ACTIVE；
- Supervisor verify 为 current，Strategy 为 `OPERATING`，canonical fingerprints 无漂移；
- 新任务已读取必要 `.founder/` 状态并返回 `MANAGER_TASK_READY`；
- Main identity 只在 Supervisor/STATUS 证据中，不因本动作初始化或伪造 `.founder/THREADS.json`；
- 没有为 Portfolio 子项目自动创建额外 Main；
- 用户拿到真实 thread link/directive，而不只是一段状态摘要。

## 失败恢复

- create 失败：旧 Main 保持 ACTIVE；报告 `MANAGER_TASK_CREATE_FAILED`，不声称已开启。
- create 返回不确定：先 list/reconcile exact project 和 reservation marker；禁止盲目重试。
- create 成功、handoff 尚未 offered：新任务保持只读候选；旧 Main 可修复后继续，不创建第二个。
- handoff offered、follow-up/wait 失败：旧 Main 已 fenced；把新任务视为 pending successor，从 Supervisor handoff 和 exact runtime identity 恢复，禁止旧 Main继续项目写入。
- 新任务 claim 失败或 fingerprint drift：进入 RECOVERY，保留真实任务与证据，不重跑 Bootstrap/Adoption。
- title 失败：任务仍可用，标记命名能力 PARTIAL 后用真实 ID 交付。
- create/list/send/wait 能力缺失，或项目没有 exact saved target：记录 `MANAGER_TASK_CAPABILITY_UNAVAILABLE` 或 `MANAGER_TASK_PROJECT_TARGET_UNAVAILABLE`；保留当前 ACTIVE，并给 Founder 一个可复制的最小启动 Prompt。不得伪造 ID、声称任务已开或改用 UI 自动点击。

任何部分失败都先对账既有真实任务和 Supervisor 状态。删掉任务、archive、强制 takeover 或创建替代 Main 都是新的处置动作，不能作为无证据的自动清理。

## 老板摘要与可见入口

成功时老板摘要先报告“独立总管任务已创建并接管”，再给：标题、项目根、当前 `ACTIVE/OPERATING`、是否还有必须决定的事项。最终响应必须包含应用支持的真实 thread link/directive，例如：

```text
::created-thread{threadId="<actual-thread-id>"}
```

不要用 `.founder/STATUS.md` 链接替代总管任务入口。若用户明确要求“打开/切过去”，再使用 runtime 的 navigate/show 能力；只要求创建时保持当前任务可见并给入口。

复用已有健康总管时不要重复发 created directive；应返回现有 task identity/入口并说明“已复用”。

## 验证边界

Python validator 可以证明协议文本、触发条件、唯一性、顺序、失败关闭和无 Worker Registry 混淆；它不能伪造真实 Codex task。真实验收必须在暴露 thread/project 工具的 Codex runtime 中做一次 forward test，观察 create identity、Supervisor handoff、`MANAGER_TASK_READY`、old-owner fencing 和最终可见入口。

# FounderOS V4.1 轻量 Worker Runtime

只在 `workflow_profile=V4_LIGHT` 且当前目标需要真实 Worker 时读取本文件。普通状态问答不读取；高保障项目改读相应高级协议。本文替代普通 Worker 对完整 `thread-manager.md`、Strategy、Supervisor、Registry、Skill/Memory 控制面的依赖。

## 目录

- 适用边界与固定路径
- 轻量项目认知与任务包
- 真实 Thread、等待与返工
- 验收、测试与预算

## 适用边界

`LIGHT_MODE` 的规范 profile 值是 `V4_LIGHT`；它是默认配置，适合单主管、单写入者、普通功能、Bug 和维护。它直接使用当前 Codex runtime 暴露的真实 Agent/Thread create、send、event wait 和 bounded read 能力，不初始化或要求：

```text
STRATEGY.json
ACTIVE_SUPERVISOR.json
.write-lock.json
THREADS.json
Skill Registry
Workstream
Integration
Organization Memory
```

项目需要安全、隐私、支付、生产、公共数据迁移、多个写入者、重大架构或难回滚修改时，推荐 `GOVERNED_MODE`（规范 profile 值 `V4_GOVERNED`）。配置切换是明确的计划增量或用户决定；不得把普通任务悄悄升级成重型控制面。

旧 helper 若在轻量路径被误调用，必须以零写入返回 `NOT_APPLICABLE_LIGHTWEIGHT`。不能返回 `LEGACY_MIGRATION_REQUIRED`，也不能自动生成 Strategy、锁或 Registry。

## 一次请求的固定路径

1. 将请求归入 `PROJECT_IDEA / FEATURE_IDEA / BUG_REPORT / QUESTION_OR_STATUS`；旧 `MAINTENANCE` 输入按前三类中的实际语义归类。
2. 每个新用户目标只做一次 Project Fit Check；后续继续、等待、验收不重做。
3. Fit Check 只读项目快照和相关路径，检查重复、架构/接口/数据冲突、活动写入冲突、前置能力、更简单方案及高风险面。
4. `FIT=PASS` 只内部记录。仅在发现真实问题时向用户说明证据、影响、推荐和替代路径。
5. 按 F0–F3 行动：

   - `F0_CONTINUATION`：状态、继续或验收；只读必要 STATUS，零新 Worker、零状态写。
   - `F1_LOCAL_FIT`：局部功能、普通 Bug、小维护；不重新 Discovery、不重新确认整份计划，默认一个任务包和一个 Worker。
   - `F2_PLAN_DELTA`：公共接口、数据、依赖、里程碑或多模块变化；只生成计划增量，用户确认前零 Worker。
   - `F3_PROJECT_RESET`：新项目/根目录/目标用户/核心方向变化；Brief 与计划确认前零实现 Worker。
   - `UNKNOWN`：只问一个真正阻塞的问题，不以全库扫描代替提问。

Fit Check 不是独立 Agent、持久 Gate、完整扫描或长报告；没有问题时不输出冗长检查报告。

## 轻量项目认知

优先复用 `.founder/PROJECT.md`、`.founder/STATUS.md` 和唯一 `.founder/TASK_THREADS.md`。首次经批准写入轻量状态时记录精确 `workflow_profile=V4_LIGHT`；PROJECT 保存稳定契约和 `last_indexed_commit`，STATUS 保存紧凑动态状态并保持不超过 4 KiB，TASK_THREADS 保存真实 task/thread/project/host、目标、写入 scope、状态和最后结果。

- 稳定层：目标、用户、成功标准、技术栈、模块地图、接口/数据、规范、构建测试命令、关键约束。
- 动态层：HEAD、里程碑、活动任务、近期 accepted 修改、Bug/阻塞/风险。
- 按需层：当前目标的文件、代码片段、日志和测试。

HEAD 未变化不重新扫描。HEAD 变化先看 changed paths 和相关 diff，只更新受影响索引。代码和测试证据与状态冲突时以前者为准。状态只在 accepted、blocked、计划/架构实质变化时更新；工具调用、无变化 wait 和普通检查零写入。

真实 `create_thread` 返回 ID 时原子记录或更新 TASK_THREADS；同一任务后续消息只能复用原 `thread_id`。LIGHT 不同时维护 `AGENTS.md`、`THREADS.json` 或第二份 task/thread 映射；旧项目已有这些历史文件时保留但不复制。

已有 V2.3 项目保留全部历史文件。一次轻量接管只把当前事实压缩到 PROJECT/STATUS 索引并记录 profile/commit；不删除历史、不每轮全读五账本、不自动恢复旧治理。

## 最小任务包

首次发送给 Worker 的正文只含以下八项，目标 2–4 KiB：

```text
OBJECTIVE
PROJECT_CONTEXT
CHOSEN_APPROACH
CONTEXT_REFS
READ_WRITE_SCOPE
DELIVERABLES
ACCEPTANCE_AND_TESTS
STOP_OR_ESCALATE_WHEN
```

用路径、行号、artifact/hash 引用日志、diff、图片和测试输出；不得粘贴完整聊天、全部账本、FounderOS 高级协议或大日志。每项任务只有一个 owner。Worker 默认禁止创建下级 Agent，也不得改 `.founder/**` 或超出明确 scope。

Bug 默认交给同一个 Worker 完成 `复现 → 诊断 → 修复 → 回归测试`。根因只作为待验证假设，不为同一上下文自动创建诊断、修复和 Reviewer 三个角色。

Worker 固定返回：

```text
RESULT
CHANGED_PATHS
VALIDATION_COMMANDS
VALIDATION_RESULT
RISKS_OR_BLOCKERS
DECISION_NEEDED
```

摘要不是验收证据。主管必须读取实际路径/artifact、检查 diff 和验证输出；只有这些证据满足 acceptance 后才 accepted。

## 真实 Runtime 操作

1. 默认一个 Worker。仅任务真正独立、写入 scope 不相同也不嵌套且并行缩短关键路径时最多两个并发；重叠时串行，或使用独立 branch/worktree 后集成。
2. 用 runtime 当前真实能力：`list_projects` 定位项目，初次调用 `create_thread` 并保存真实 `thread_id / project_id / host_id`。create/send/wait/read 任一必要能力缺失时返回 `RUNTIME_THREAD_CAPABILITY_UNAVAILABLE`，禁止角色扮演。
3. 初次 `create_thread` 的 prompt 就是八字段任务包；同一 task 已有映射时不新建，改用 `send_message_to_thread` 发给原 ID。Worker 不得递归创建 Agent。
4. 使用 `wait_threads` 做一次合理长 timeout 的事件等待。无变化 snapshot 不唤醒模型、不写状态、不连续 polling。
5. 收到完成/阻塞/需注意事件后用有界 `read_thread` 读取结果；超过 4 KiB 的正文只接收 artifact 路径、hash 和摘要。
6. 验收失败用 `send_message_to_thread` 向原 ID 发送具体缺陷、证据和不变范围。最多两轮定向返工；第二轮仍失败就停止并报告，不创建替代 Worker碰运气。
7. 主管读取真实 changed paths、diff、命令、退出码和日志路径；只有证据满足 acceptance 才 accepted，并更新 TASK_THREADS 与一次必要的 PROJECT/STATUS 状态。

F1 的明确实现或 Bug 请求在 Fit 通过后授权对应的一个工作对话；重大方案在用户确认前不创建。只有长期恢复、轮换、归档或 `V4_GOVERNED` 才读取完整 `thread-manager.md`；普通 Worker 不读取它，也不初始化 THREADS Registry。主管自身轮换时只交接 Brief、项目地图、紧凑 STATUS 和活动任务包；不得复制完整历史，也不得因 Bootstrap、Adoption、子项目或 Worker 自动创建新主管。

## 测试与验收策略

- 文案、样式、配置：静态检查或相关页面验证。
- 局部功能、普通 Bug：相关单元测试和失败复现。
- 跨模块、公共接口或数据库：相关单元测试与相关集成测试。
- 里程碑、发布、高风险或最终集成节点：完整测试一次。

Worker 交付时返回命令、退出码、代码版本和日志路径。同一代码版本已有可信结果时主管复用，不重复运行；完整套件失败后只复现失败项，修复后跑失败项和相关模块，最终集成时再完整运行一次。没有代码、配置、环境或输入变化时，相同失败最多一次无修改重试，并明确分类 `BASELINE_FAILURE / NEW_FAILURE / ENVIRONMENT_LIMITATION`。

## 熔断与共享预算

主管与全部 Worker 共用一个任务总预算；只有用户或 runtime 明确给出时才使用硬数值，不能给每个 Worker 各复制一份。runtime 有 usage 时按 actor 汇总 `input_tokens / cached_input_tokens / output_tokens / reasoning_tokens`，同时报告原始分量和有说明的折算单位。

- 每个任务一次派发、一次验收、最多两轮具体返工。
- 连续两个模型回合没有 artifact、diff、失败复现或测试证据：`EFFICIENCY_CIRCUIT_BREAKER`。
- 可测时治理、状态、派工、等待和总结合计目标不超过总任务量 30%。

拿不到真实 usage 时明确 `TOKEN_TELEMETRY_UNAVAILABLE / UNVERIFIED`，用模型回合、读取字节、任务包大小、Worker 数、状态写次数和返工次数做代理熔断。不得把 transcript 字节伪装为 token，也不得声称端到端节省已验证。

## 开源调研与 override

只在用户明确要求、build-vs-buy、重要依赖/架构选择或没有明显路径时调研。先定筛选标准，初筛最多五个、最终最多三个；检查原始来源、许可证、维护、兼容、集成、安全和限制。不克隆/通读全部候选。结论直接进入任务包。

发现需求与项目冲突时输出证据、影响、推荐与替代路径。证据不足写“假设”。用户了解风险后仍坚持时记录 override 和风险，再在现有安全/授权边界内执行；override 不授权生产、费用、凭据、隐私或破坏性动作。

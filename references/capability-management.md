# FounderOS V2.2 Capability Management

在创建 Agent/Persistent Thread、分派复杂任务、启动新 Workstream，或出现能力缺口时完整读取本文件。Capability Planner 是 ACTIVE FounderOS 的按需判断职责，不是默认创建的固定员工。

## 目录

- [四个概念](#四个概念)
- [Capability-first 原则](#capability-first-原则)
- [何时规划能力](#何时规划能力)
- [Existing Project Capability Profile](#existing-project-capability-profile)
- [Capability 状态](#capability-状态)
- [Capability Plan](#capability-plan)
- [REUSE BEFORE ACQUIRE](#reuse-before-acquire)
- [Just-in-Time 获取](#just-in-time-获取)
- [Agent 与 Skill 的选择](#agent-与-skill-的选择)
- [Memory-aware evidence](#memory-aware-evidence)
- [Persistent 与 Task Agent](#persistent-与-task-agent)
- [Strategic Gate](#strategic-gate)
- [依赖、并行与验收](#依赖并行与验收)
- [Capability baseline 与恢复](#capability-baseline-与恢复)
- [老板摘要](#老板摘要)

## 四个概念

- **Agent**：真实 AI 员工身份、职责、权限和历史，例如 `technical-lead-01`。
- **Thread**：Agent 当前使用的真实 Codex 工作对话和 runtime binding；可以更换，不是员工身份。
- **Capability**：完成某类任务需要的抽象能力，表达“需要会什么”，例如 `godot-development` 或 `security-review`。
- **Skill**：实现一个或多个 Capability 的具体、可复用能力包，例如 `godot-workflow`。

硬规则：`Agent != Thread != Capability != Skill`。创建 Agent 不会自动获得某项 Capability；安装 Skill 也不会创建 Agent、扩大权限或形成 Thread。

Capability ID 使用稳定的小写 slug；名称相近不代表语义相同。Capability 是需求和覆盖关系，不保存第三方代码的信任结论。Skill 的来源、版本、风险和绑定由 [skill-governance.md](skill-governance.md) 与 [skill-registry.md](skill-registry.md) 管理。

## Capability-first 原则

复杂任务按以下顺序处理：

`TASK → Workstream → Capability Planner → 执行者能力覆盖 → Skill Registry → 必要时 Skill Curator → 绑定 → 执行 → 验收 / Integration`

先从任务、风险、工具和验收标准推导 Capability，再选择 Skill。禁止因 Skill 名称、README、star 数或搜索排名看起来合适，就反向改写任务需要或直接绑定。

Capability Planner 只回答当前任务需要什么能力、覆盖到什么程度以及关键缺口是否值得处理；不决定项目总方向，不解除 Strategic Gate，也不自动创建 Agent 或安装 Skill。

当需要把 FounderOS 已经判断出的 task facts 规范成可复验的五状态结果时，可调用只读 [`capability_planner.py`](../scripts/capability_planner.py)。该 helper 不做语义需求推断、Skill 搜索或信任判断，返回 `changed_paths=[]`；项目方向和必要 Capability 仍由 ACTIVE FounderOS 负责。

## 何时规划能力

出现以下任一情况时做显式 Capability Plan：

- 创建长期 Agent 或 Persistent Thread；
- 分派需要专门工具、格式、领域知识或安全流程的复杂任务；
- 启动会持续多个阶段的新 Workstream；
- 任务失败或 Reviewer 证据表明现有能力不足；
- 关键验收依赖某个可验证的专业工作流。

很小、直接、低风险且通用 Codex 能力足够的任务不必机械建立清单。没有专用 Skill 不等于不能工作；只有缺口会明显影响正确性、安全性、特定工具使用或可重复工作流时，才触发获取。

不要为了让团队显得专业而创建 Capability、Agent 或 Skill。每个关键缺口必须能回答：“如果现在不补齐，会阻塞哪项验收或增加什么实质风险？”

## Existing Project Capability Profile

Existing Project Adoption 时完整读取 [project-adoption.md](project-adoption.md)。`ADOPTION_READ_ONLY` 可以从 CONFIRMED 的 manifest、代码、测试/构建配置和平台事实形成只读 `CAPABILITY PROFILE`，例如 python、pytest、godot、gdscript、postgres 或 windows-packaging；每项必须引用证据，并区分当前项目“使用了什么”与未来任务“需要什么”。

Capability Profile 只服务 Adoption 后的 Agent/Skill 调度，不是 acquisition plan：

- 不因检测到技术栈就创建 Agent、Persistent Thread、Workstream、SKILLS/LOCK 或调用 Curator；
- 不把 README、目录名或项目自带 Skill 当作可信覆盖；
- build/test 未运行时不把相应 Capability 标为已验证 `AVAILABLE`；
- 项目自带 `.agents/.codex/skills/scripts` 先作为 `PROJECT DATA`，按 Skill Trust 模型重新审计；
- 只有 `ADOPTED + OPERATING` 后出现当前已授权任务的关键缺口，才执行 `REUSE BEFORE ACQUIRE` 和 Just-in-Time 获取。

Adoption confidence LOW 或 evidence 冲突时，相关 Capability 保持合法五状态中的 `REQUIRED / PARTIALLY_COVERED`，并在 `coverage_evidence` 明确写 `UNKNOWN / conflicting`；不要发明第六种状态，也不要用安装 Skill 代替理解现有项目。

## Capability 状态

每项当前任务需要的 Capability 使用以下五种状态之一：

| 状态 | 含义 | 默认动作 |
|---|---|---|
| `REQUIRED` | 已确认需要，尚未完成覆盖判断 | 检查现有 Agent、Registry 和 runtime |
| `AVAILABLE` | 当前 Agent/可信 Skill/通用能力足以满足验收 | 复用并记录证据 |
| `PARTIALLY_COVERED` | 可完成部分工作，但关键子能力或验证缺失 | 缩小任务、组合可信 Skill 或补齐关键缺口 |
| `MISSING` | 当前可信能力无法满足关键要求 | 执行 `REUSE BEFORE ACQUIRE`，必要时调用 Curator |
| `BLOCKED` | 因 Gate、权限、信任、runtime 或依赖限制无法安全获得/使用 | 停止受影响任务并记录阻塞或升级 |

状态必须绑定当前 task、Agent、环境和验收标准；不能把另一个项目、另一个 Thread 或过期版本的覆盖结论直接复用。`AVAILABLE` 需要真实 runtime 可见性或通用能力理由，不以 Registry 文本单独证明。

## Capability Plan

复杂任务的最小计划包含：

| 字段 | 要求 |
|---|---|
| `task_id / workstream` | 当前任务与所属工作线 |
| `capability_id` | 稳定 slug |
| `criticality` | `critical / supporting / optional` |
| `state` | 五状态之一 |
| `coverage_evidence` | Agent 通用能力、已批准 Skill binding 或可定位证据 |
| `gap_effect` | 缺失会影响的交付/风险 |
| `next_action` | reuse / combine / acquire / generic-execution / block |
| `baseline` | 当前 AGENTS、SKILLS/SKILL_LOCK、runtime observation revision/hash |

计划只保存完成当前任务所需的最小集合。`optional` 缺口不能单独触发 acquisition。Capability 组合必须说明每个 Skill 覆盖的部分，不能用“安装多个应该更好”代替覆盖证明。

## REUSE BEFORE ACQUIRE

对每个关键 `MISSING` 或 `PARTIALLY_COVERED` Capability 严格按顺序检查：

1. 当前 Agent 已有通用能力和已绑定 Skill 是否足够；
2. 项目 `.founder/SKILL_LOCK.json` 是否已有 `APPROVED + AVAILABLE` 的兼容 Skill；
3. 全局 `$CODEX_HOME/skills` 或当前 runtime 是否已有可审计、可项目批准的兼容 Skill；
4. 少量现有可信 Skill 的组合是否足够且没有冲突；
5. 是否确实需要寻找新的外部 Skill。

任一步已经满足验收就停止。全局可发现只表示 `Installed`，不表示项目 `Trusted / Approved / Bound`。项目 `SKILLS.md` 的人读描述也不能覆盖机器锁中的精确状态。

只有前四步均失败且缺口关键时，才向真实可用的 `$skill-curator` 提交有界任务。Curator 不可用时记录 `SKILL_CURATOR_UNAVAILABLE`，选择通用能力、缩小任务、推迟或向 Founder 报告；禁止伪造调用或信任结论。

## Just-in-Time 获取

Skill acquisition 必须 Just-in-Time：只为当前已授权任务的关键缺口寻找最小候选集合。禁止一次下载大量 Skills、为尚未选择的战略候选预建工具链，或因“以后可能有用”扩张全局 Skill 库。

Curator 搜索应有明确：

- Capability 与 task ID；
- 最多候选数、允许来源和停止条件；
- 所需平台/runtime；
- 禁止的权限和外部操作；
- 风险上限与 Founder 审批条件；
- 期望的审计/推荐交付。

发现多个候选时比较后给一个 `PRIMARY RECOMMENDATION`，必要时只给一个 `ALTERNATIVE`；不要把十几个仓库丢给 Founder 自行研究。

## Agent 与 Skill 的选择

依次判断：

1. 主 Agent 或现有 Agent 是否已经能完成；
2. 缺的是独立执行者、独立检查，还是执行者已有但缺专业工作流；
3. 新 Skill 是否会真正提高当前验收质量；
4. 若需要 Skill，哪个现有 Agent 的职责和权限最适合绑定；
5. 只有工作本身需要独立身份/上下文时才创建新 Agent。

“缺能力”不自动等于“缺人”，“有 Skill”也不自动等于“应创建员工”。创建 Agent 仍按 [delegation.md](delegation.md)；Skill 获取与绑定按 [skill-governance.md](skill-governance.md)。

## Memory-aware evidence

Capability Plan 冻结后，ACTIVE Main 可按当前 task type、capability、component、workstream 和 project stage 有界查询 [organization-memory.md](organization-memory.md)，比较同一语境下已验收的 Agent、Team、Skill exact-version 与失败归因证据。Memory 只调整 routing/review 建议：它不反向改写任务需要、不替代 Capability coverage、不会自动创建员工或获取 Skill。

冷启动 Agent/Skill 标 `UNPROVEN + LOW confidence`，不是“差”；一个成功样本仍是弱证据。历史 evidence 必须与当前语境匹配，Architecture 的强证据不能替代 UI 证据。无相关记录时保留探索空间，按通用能力、当前职责、风险和验收选择，不让历史选择形成永久垄断。

Skill performance 只有在 independent Trust/approval/hash/runtime checks 全通过后才参与同等候选比较；Performance 绝不提高 Trust、扩大权限、解除 Review 或 Strategic Gate。发生撤回、后续失效、版本变化或 context mismatch 时，降级或排除相应证据，不原地覆盖历史。

## Persistent 与 Task Agent

Persistent Agent 可以有稳定的 Capability Profile 与最小 Skill Profile。复用前仍验证精确版本、trust、runtime availability 和当前 task scope。更换 Thread 时 identity 不变，但 successor 必须取得当前 Capability/Skill baseline。

Task Agent 只绑定当前任务真正需要的 Skills，不继承整个 Workstream 或 Lead 的能力集合。Lead 授权创建 Specialist 也不把 Lead 的 Skills、权限或批准状态自动传给下级。

Skill Profile 是能力供应状态，不是权限授予。最终有效权限始终按 [skill-governance.md](skill-governance.md) 的交集计算。

## Strategic Gate

Capability Planning 本身可以是只读判断，但 acquisition、安装、项目登记、绑定和执行仍受 [founder-discovery.md](founder-discovery.md) 约束：

- `DISCOVERY_ACTIVE / STRATEGIC_CHOICE_REQUIRED`：只允许有界只读能力调查；不得为不同候选安装专业 Skill 或建立 candidate-bound binding。
- `BOOTSTRAP_AUTHORIZED / DECISION_RECORD_REQUIRED / STATE_SYNC_REQUIRED`：只完成当前 Gate 允许的控制动作，不启动普通 Skill acquisition。
- `ADOPTION_READ_ONLY / ADOPTION_STATE_REQUIRED`：只允许有界 capability inventory；不 acquire、安装、登记、绑定或创建长期 Skill Profile。
- `OPERATING`：可按影响等级、信任与授权策略获取/绑定，不代表自动允许 L3。
- `EXECUTIVE_APPROVAL_REQUIRED`：不得以补能力为理由执行待批准动作。

选择会改变主平台、关键技术路线、数据/模型依赖或长期资源押注的 Skill 至少是 L2；账号、付费、凭据、系统级安装、敏感数据或高风险外部动作按真实影响进入 L3。不要按 Skill 名或“安装”关键词机械分级。

## 依赖、并行与验收

关键 capability gap 是 Dependency Gate：下游任务在状态 `BLOCKED` 或关键 `MISSING` 时不得派发给 Agent 猜测。独立候选静态审计可以只读并行；共享安装目录、Registry/Lock mutation、版本切换和同一 binding 必须串行或以明确接口隔离。

Agent 交付后，FounderOS 验收：

- 实际使用的 Capability/Skill 与 assignment 一致；
- bound version/hash/trust 未漂移；
- Skill 没有越过 Agent/Workstream scope；
- 交付证据满足原验收标准；
- 跨线接口没有因不同 Primary Skill/版本产生冲突。

Skill 的存在不是验收证据。多 Workstream 结果仍须通过原 Integration Gate。

## Capability baseline 与恢复

Persistent Thread 的 `capability_baseline` 保存当前任务相关 Capability 及其状态；精确 Skill binding 另由 Skill baseline 保存。新 Main Thread 恢复时依次对账 AGENTS、SKILLS/SKILL_LOCK、THREADS 和 runtime availability，不能从角色名称推断能力。

发现 baseline 缺失、过期或无法证明时标 `UNVERIFIED`；关键项保持 `BLOCKED`，先按 [skill-governance.md](skill-governance.md) 恢复、重新验证或执行 `SKILL_SYNC`。不得通过新建重复 Agent/Thread掩盖能力状态漂移。

## 老板摘要

默认只汇报会影响项目的能力事件：新增关键能力、关键 gap、等待批准的风险、hash/version mismatch、revoke/deprecate 和下一步。不报告每次普通 Skill 调用或完整审计日志。

若无关键 gap 或批准事项，写“能力覆盖满足当前任务，无需你立即决定”，并继续推进。

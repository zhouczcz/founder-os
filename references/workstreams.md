# FounderOS Workstream、依赖与集成协议

当项目需要多线并行、Workstream Lead、跨任务依赖、接口契约或 Integration Gate 时完整读取本文件。

## 目录

- [何时创建 Workstream](#何时创建-workstream)
- [Adoption 与 Maintenance Workstream](#adoption-与-maintenance-workstream)
- [Strategic Gate 前置条件](#strategic-gate-前置条件)
- [Capability 与 Skill 前置条件](#capability-与-skill-前置条件)
- [Workstream 与 Lead](#workstream-与-lead)
- [Workstream 与 Persistent Thread](#workstream-与-persistent-thread)
- [状态与所有权](#状态与所有权)
- [依赖分类](#依赖分类)
- [Dependency Gate](#dependency-gate)
- [安全并行判定](#安全并行判定)
- [Integration Gate](#integration-gate)
- [返工与 stale context](#返工与-stale-context)

## 何时创建 Workstream

Workstream 是围绕一个长期结果、明确责任边界和可独立验收证据组织的工作线，不是固定部门。Product、Engineering、Design、Research、Marketing、QA、Operations 仅是例子。

满足以下任一条件时考虑创建：

- 同一结果会持续多个里程碑；
- 有两个以上相互关联的专业任务；
- 需要独立写入范围、依赖或接口；
- 并行可以明显缩短关键路径；
- 需要 Workstream 级验收后再做跨线集成。

一个小任务或一个 Specialist 不需要为了形式创建 Workstream。简单阶段由 ACTIVE FounderOS 直接管理 1–3 个 Specialist。

## Adoption 与 Maintenance Workstream

无 `.founder/` 的 Existing Project 先完整读取 [project-adoption.md](project-adoption.md)。`ADOPTION_READ_ONLY` 和 `ADOPTION_STATE_REQUIRED` 中不得根据目录、技术栈或候选问题预创建 Product、Engineering、Maintenance、Security、Release 等 Workstream；只允许 FounderOS 或有界 `adoption-read-only` Task/Review 恢复证据和后补 canonical state。

Adoption 进入 `ADOPTED + OPERATING` 后，先从真实 current work、风险和 maintenance priority 选择一个可验收下一任务，再判断是否需要 Workstream。项目已完成/只修 Bug 时可以由 FounderOS 直接维护；只有多个关联任务、跨阶段 ownership、明确接口或并行价值存在时才建立 Maintenance/Bug Fix/Dependency/Performance/Documentation/Release/Compatibility/Reliability 中当前真正需要的工作线。

Maintenance 默认优先级：

| Priority | Workstream 排序依据 |
|---|---|
| `P0` | 数据丢失、重大安全风险、核心功能完全无法使用 |
| `P1` | 严重 Bug、发布阻塞、关键回归 |
| `P2` | 兼容性、性能、可靠性 |
| `P3` | 有证据和实际维护影响的技术债 |
| `P4` | 纯美化、代码风格、理论重构 |

技术债不能仅凭“旧/不优雅”创建 Workstream。先记录 issue、evidence、impact、probability、cost、urgency、不处理后果和验证方式；只有高价值、高风险或阻塞未来时提升。大规模 rewrite、跨平台迁移或破坏行为兼容至少为 L2，未过 Gate 时不创建候选绑定 Workstream。

Existing Project 默认 `BEHAVIOR_PRESERVATION=true`；Workstream outcome/exit criteria 必须说明 API、output、file formats、user workflow、compatibility 和现有失败基线是否保持。已有测试缺失且改动高风险时，把 Characterization Tests 作为上游 Dependency Gate，而不是先重构。

## Strategic Gate 前置条件

Strategy 存在时，建立、启动、分派或集成候选绑定 Workstream 的前置条件是 Gate 精确为 `OPERATING`。新项目的 pre-bootstrap Strategy-only 阶段不创建空 Workstream、Stage A0、Lead 或长期部门；`BOOTSTRAP_AUTHORIZED` 只允许建立五账本并完成 canonical 协调，不是已经 Operating。Existing Project 的 `ADOPTION_STATE_REQUIRED` 同样只允许按 baseline 后补/验证五账本，不创建业务 Workstream。

`DISCOVERY_ACTIVE / STRATEGIC_CHOICE_REQUIRED` 中只允许有界的 `discovery-read-only` 或真正无关的只读调研，task-level effective write scope 必须为 `[]`；`ADOPTION_READ_ONLY / ADOPTION_STATE_REQUIRED` 中只允许相应的 `adoption-read-only` audit/canonical control。这些都不伪装成已组建的 Workstream。`DECISION_RECORD_REQUIRED / STATE_SYNC_REQUIRED / EXECUTIVE_APPROVAL_REQUIRED` 中只完成当前 canonical 记账、Thread 同步、报告或安全控制，不派新候选业务。

执行型旧项目五账本齐全但缺少 Strategy 时，先做非破坏的 legacy migration，从现有 PROJECT/DECISIONS 推断已选方向并初始化 `LEGACY_INFERRED + OPERATING`；不重新 Bootstrap、不随意拆掉旧 Workstream。只读评审不做迁移，也不接受新 Integration Gate。

## Capability 与 Skill 前置条件

创建长期 Workstream、Lead/Persistent Thread 或复杂 assignment 前，按 [capability-management.md](capability-management.md) 识别当前结果真正需要的 Capability。关键 `MISSING/BLOCKED` 是 Dependency Gate；非关键缺口可由通用能力处理，不为形式阻塞或获取 Skill。

执行 `REUSE BEFORE ACQUIRE` 后仍缺关键能力，才 Just-in-Time 调用 Curator。非 `OPERATING` Gate 只允许无项目写入的 Capability inventory/候选静态审计，不安装、登记、绑定 candidate-bound Skill 或创建长期 Skill Profile。

Workstream/Agent 使用 Skill 时，baseline 至少引用项目 Skill Registry/Lock revision、Primary/Supporting binding 和精确版本/hash。Skill 不改变 Lead/Agent 的 write scope；有效权限仍是 Skill request、Agent、Workstream、FounderOS policy 和当前系统/用户授权的交集。

## Workstream 与 Lead

只有在 Workstream 内有多个 Agent、持续多个阶段、内部协调复杂、FounderOS 管理跨度过大，或需要专业内部验收时才创建 Lead。Lead 必须是真实 Codex subagent，或满足长期条件并由 runtime 创建的真实 Persistent Thread；不能由主线程角色扮演。

Lead 只能在明确授权下创建 Specialist，且授权必须限定：

- assignment slots / 最大子 Agent 数；
- 允许的角色与任务类型；
- Workstream 与唯一写入范围；
- 最大嵌套深度；
- canonical decision/interface baseline；
- 需要回报 FounderOS 的真实 Agent ID 和状态。

默认最大结构是 `ACTIVE FounderOS → Lead → Specialist`。Specialist 的 `CAN_CREATE_SUBAGENTS=false`；Lead 不得授权 Specialist 继续递归。ACTIVE 先在 `AGENTS.md` 预留 assignment，Lead 才可派发；Lead 返回真实 runtime ID 后由 ACTIVE 绑定 canonical 记录。

Lead 不拥有全局项目控制权，不得修改五份 canonical 账本、Supervisor 记录、全局阶段、全局优先级或项目方向。它可以在精确 WRITE_SCOPE 内维护自己的 Workstream 状态和业务产物。

## Workstream 与 Persistent Thread

每个 Thread-backed Agent 必须属于明确 Workstream，或标 `project-level`；Reviewer 可属于具体 Workstream 或 `integration`。`AGENTS.md` 管 Lead/员工身份和授权，`THREADS.json` 只管 runtime binding/lifecycle。

Workstream Lead Thread 的长期存在不扩大 write scope，也不让它修改全局 ROADMAP、DECISIONS、STATUS、Supervisor 或 Thread Registry。Lead 可在 `CAN_CREATE_SUBAGENTS=true`、slots/深度/范围明确时使用有界 Task subagent；不得自行创建第二个 Persistent Lead Thread。

Persistent Thread 的 baseline 落后时先 `STATE_SYNC`。Strategy 存在时，Worker baseline 绑定语义 `STRATEGY_CONTEXT_REVISION/SHA256`，不绑定会因 Gate/report 元数据改变的完整 Strategy SHA；Supervisor 仍用完整 Strategy revision/SHA 阻止 stale Main。handoff predecessor、archived、duplicate-primary、wrong-project 或旧 Strategy context Thread 的输出不得进入 Workstream acceptance。

## 状态与所有权

`ROADMAP.md` 在实际出现多线工作时增加 Workstream registry 和依赖表；旧项目没有这些表仍然有效。推荐最小字段：

| 字段 | 含义 |
|---|---|
| Workstream ID | 稳定、安全的 slug 或 ID |
| Outcome / exit criteria | 可验收结果 |
| Lead / owner | FounderOS 或真实 Agent ID |
| Status | planned / active / blocked / ready-for-integration / integrated |
| Depends on | 上游 task/Workstream ID |
| Interface contract | 路径 + revision/hash，若适用 |
| Write scope | 解析后的唯一范围 |
| Canonical baseline | DECISIONS/PROJECT/接口 revision |
| Strategy baseline | `STRATEGY_CONTEXT_REVISION/SHA256` 或 `legacy-unmigrated` |
| Capability / Skill baseline | capability states + Registry/Lock revision + relevant bound-set hash |

复杂 Workstream 可按需创建：

```text
.founder/workstreams/<safe-slug>/
├── STATUS.md
└── TASKS.md      # 仅确有独立任务队列时
```

不要在 Bootstrap 预创建空目录或固定部门。slug 只允许小写字母、数字和连字符；拒绝 `..`、绝对路径、符号链接、junction、重解析点和解析后越出项目根的目标。

Workstream 文件是下级状态。与 canonical 五账本冲突时，按 `DECISIONS → PROJECT → ROADMAP/AGENTS → STATUS` 的权威顺序处理，并由 ACTIVE 协调。Lead/Agent 不直接更新全局 STATUS。

不同 Workstream 尽可能拥有不相交的真实写入范围。路径文本不同不等于目标不同；比较规范化、大小写折叠（适用时）、符号链接/重解析解析结果、共享生成物和实际文件。多个 Agent 默认不得并行修改同一文件，即使声称修改不同代码行。

## 依赖分类

派发每个 Task 前分类：

在三种依赖类型之前，先把当前 Strategic Gate、proposal/Decision 和 Strategy semantic context 当作全局前置。共享 unresolved L2 的任务不是 `INDEPENDENT`；它们必须在 Gate 选择/记账/同步完成后才能进入普通 dependency graph。

### `INDEPENDENT`

输入、未定决策、写入范围和验收互不依赖，可以并行。典型例子是不同市场/技术路线的只读研究。

### `DEPENDENT`

需要上游通过验收才能开始。`returned`、`running` 或 `changes-requested` 都不能解除依赖；只有 `accepted` 及其可定位证据才解锁。

### `INTERFACE-SEPARABLE`

先创建、审查并冻结接口契约，再让双方并行。契约必须记录路径、schema/语义、revision/hash、Owner 和变化规则。双方收到相同 baseline；契约变化会使依赖它的旧验收失效。

“都能并行”不是默认值。无法解释为何不会依赖、冲突或基于不同决策时，先串行或切分接口。

## Dependency Gate

每个 Task/assignment 至少记录：

- `DEPENDENCY_CLASS`；
- `depends_on`；
- `blocked_by`；
- `unblocks`；
- `interface_contract`（路径 + revision/hash 或 `none`）；
- canonical baseline；
- `STRATEGY_SCOPE`、当前 Gate 与 `STRATEGY_CONTEXT_REVISION/SHA256`（Strategy 存在时）；
- 关键 Capability 状态、Skill Registry/Lock revision、Primary/Supporting binding 与 bound-set hash（使用 Skill 时）；
- Dependency Gate 的可观察通过条件。

依赖未满足时保持 `blocked`，不要创建 Agent 让其猜测缺失输入。可选动作只有：等待上游、要求上游完成/返工，或由 ACTIVE/授权 Lead 建立一个明确且可验收的临时接口。

下游派发时，把已接受的上游 artifact hash/revision 和相关决定放入 CONTEXT/DEPENDENCIES。Agent 若发现 baseline 过期，必须停止受影响工作并升级，不得自行选择旧/新版本。

## 安全并行判定

按以下顺序判断：

1. 检查 Strategy Gate 和真实影响等级；共享未定 L2/L3 方向则先解决当前 Gate，不用“可并行探索”偷偷开始多个候选实现。
2. 建立 Task dependency graph，标记三种依赖分类。
3. 解析 READ_SCOPE/WRITE_SCOPE 和共享生成物；重叠写入默认串行。
4. 对 `INTERFACE-SEPARABLE` 先冻结契约。
5. 确认每个并行 Agent 有唯一 owner、验收标准、canonical/Strategy baseline、`STRATEGY_SCOPE` 和真实 runtime slot。
6. 使用 Skill 时确认每条 binding 已批准、hash/current runtime 可见，Primary/Supporting 不冲突，相关 `SKILL_SYNC` 已完成。
7. ACTIVE 在 `AGENTS.md` 预留不冲突的写入范围后派发。
8. 等待期间检查上游、Strategy 和 Skill baseline 变化；任一失效就暂停受影响分支。

可安全并行：互相独立的只读研究、写入不同真实路径且不共享生成物的实现、相互独立的检查。

必须串行：同一文件/资产/数据库迁移、共享生成器输出、生产/发布事务、下游依赖未接受上游、无法可靠切分的工作区，以及同一全局 Skill 路径/项目 Registry/Lock 的安装、升级、revoke 或 binding mutation。

独立 worktree/branch 只有在环境确实支持、集成责任明确且不违反用户范围时才使用；不要假设 Git 可用。

## Integration Gate

多个 Agent/Workstream 返回不等于里程碑完成。以下情况必须执行 Integration Gate：

- 两个以上 Workstream 共同形成阶段结果；
- 接口、数据、UI/产品/实现或共享假设需要对齐；
- 高风险、生产、发布、架构或难回滚变化；
- ROADMAP 出口条件依赖多项成果。

在进入下列原 V2 流程前，ACTIVE 必须完成一次零写入 Strategic preflight（可用 `decision_state.py authorize --action integration --strategy-scope candidate-bound`）并同时证明：

1. `STRATEGY.json` 已存在且 Gate 精确为 `OPERATING`；旧项目执行型集成前已完成 legacy migration；
2. 所有相关 L2/L3 都在 `DECISIONS.md` 以当前 proposal/Decision ID 正式记账，不存在 `DECISION_RECORD_REQUIRED`；
3. 受新方向或 Autonomy 影响的 Persistent Threads 已在同一真实 Thread ACK 当前 `STRATEGY_CONTEXT_REVISION/SHA256`，`pending_state_sync` 已清零；
4. 所有输入 artifact 依赖当前 Strategy context、PROJECT/DECISIONS 和 interface baseline，不是旧 proposal、旧 generation 或 handoff predecessor 交付。
5. 影响当前产物或跨线接口的 Skill 已项目批准、installed hash/Lock/version 一致，相关 Thread `SKILL_SYNC=CURRENT`，不存在 revoked binding 或未处置的 Primary/Supporting 冲突。

`BOOTSTRAP_AUTHORIZED / DISCOVERY_ACTIVE / STRATEGIC_CHOICE_REQUIRED / DECISION_RECORD_REQUIRED / STATE_SYNC_REQUIRED / EXECUTIVE_APPROVAL_REQUIRED` 都不是可集成状态。只读 Reviewer 可在这些状态下分析潜在冲突，但不能接受 Gate、更新 global ROADMAP/STATUS 或把 PASS 当作战略授权。

流程：

1. 在上述 Strategic preflight 通过后，各 Task subagent、Persistent Thread 或其他 Executor 交付；Lead/FounderOS 逐项验收并标记 `accepted` 或 `ready-for-integration`。Thread `COMPLETED` 本身不等于 accepted。
2. ACTIVE 收集 artifact、hash/revision、测试、残余风险、canonical baseline 和相关 Capability/Skill baseline。
3. 检查 PROJECT 目标、最新 DECISIONS、假设、接口、命名、数据格式、代码/文件冲突、产品/UI/实现一致性、测试、Skill/version compatibility、遗漏和新增重大风险。
4. 运行跨线/集成测试或可重复检查。
5. 发现冲突时给责任 Workstream/原 Agent 定向返工；Gate 保持失败。
6. 必要时创建只读 Integration Reviewer，但简单任务不要过度 Review。
7. 所有 Gate 项通过后，只有 ACTIVE 可把 Workstream 标为 `integrated`、更新全局 ROADMAP/STATUS 并推进阶段。

建议在复杂 Gate 时按需创建 `.founder/integrations/<gate-id>.md` 保存输入 revisions、检查结果和结论；不要预创建空 `integrations/`。

Gate 通过后任何输入 artifact、接口契约、有效 DECISION、Strategy semantic context，或影响产物/接口的 Primary Skill、版本、hash、批准/撤销状态变化，旧 Gate 自动失效，必须重验。无关 Workstream 的 Skill 记录变化不应让全部 Gate 失效。Reviewer/Lead 只能报告 ready/PASS，不能直接推进全局阶段。

## 返工与 stale context

验收不合格时优先发回原 Agent；指出缺陷、证据、必改内容、未变范围和复验标准，不要求无意义全部重做。达到合理返工上限后，ACTIVE 决定更换 Agent、增加 Reviewer/Specialist、修改计划或升级 Founder。

Agent 使用旧 PROJECT、DECISIONS、Strategy context、Capability/Skill binding、接口或上游 artifact 时，其受影响成果不得进入 Integration Gate。先完成必要的 `STATE_SYNC/SKILL_SYNC`；能局部修复就定向返工，不能解释差异则阻塞。运行中 Pivot 打开 Gate 时，冻结可能被废弃方向的写入与 Integration；只有真正与选择无关的只读工作可继续。

Agent timeout 不是终态。确认旧 Agent 已停止并审计局部写入前，不把同一 scope 交给替代 Agent。Workstream `ready-for-integration` 也不是 `integrated` 或阶段 `complete`。

# FounderOS 项目账本规范

在创建、修复或大幅调整 `.founder/` 文件前读取本文件。模板中的字段是最小要求；可以增加项目特有字段，但不要保留方括号占位符或虚构信息。

## 目录

- [目录与职责](#目录与职责)
- [PROJECT.md](#founderprojectmd)
- [ROADMAP.md](#founderroadmapmd)
- [DECISIONS.md](#founderdecisionsmd)
- [AGENTS.md](#founderagentsmd)
- [STATUS.md](#founderstatusmd)
- [ACTIVE_SUPERVISOR.json](#founderactive_supervisorjson)
- [可选 STRATEGY.json](#可选-founderstrategyjson)
- [可选 THREADS.json](#可选-founderthreadsjson)
- [Workstream 与 Integration 状态](#workstream-与-integration-状态)
- [可选 SKILLS.md](#可选-founderskillsmd)
- [单写入租约](#单写入租约)
- [创建与修复规则](#创建与修复规则)
- [长项目归档](#长项目归档)

## 目录与职责

```text
.founder/
├── PROJECT.md                 # 稳定的项目契约（正式 Bootstrap 后）
├── ROADMAP.md                 # 阶段、Workstream 与可执行路线（正式 Bootstrap 后）
├── DECISIONS.md               # 追加式决策记录（正式 Bootstrap 后）
├── AGENTS.md                  # 实际 Agent、层级和委派登记册（正式 Bootstrap 后）
├── STATUS.md                  # 最新接手快照（正式 Bootstrap 后）
├── ACTIVE_SUPERVISOR.json     # 唯一 ACTIVE 的持久控制记录
├── STRATEGY.json              # 可选/新项目默认：方向、Gate、Autonomy 控制面
└── THREADS.json               # 可选：真实 Thread binding 控制登记册
```

执行型回合可临时创建 `.founder/.write-lock.json` 作为项目级单写入租约；Strategy 事务还可短暂使用 `.founder/.strategy-state-lock.json`。两者只能由正确持有者在状态协调完成后清理。`ACTIVE_SUPERVISOR.json` 与项目写锁职责不同：前者长期协调唯一总管，后者保护一次写事务。`STRATEGY.json`、`THREADS.json`、`workstreams/`、`integrations/`、`SKILLS.md`、`backups/` 与 `history/` 不属于五份 canonical 业务账本；除新项目的 pre-bootstrap Strategy 控制状态外，均只在实际需要时创建。

新项目在正式 Bootstrap 之前，`.founder/` 只有 `ACTIVE_SUPERVISOR.json`、有效的 `STRATEGY.json` 和当前事务锁是合法状态，称为 **pre-bootstrap Strategy-only**。不得因 `PROJECT.md` 等五账本尚未创建就判定项目损坏；必须先恢复 Direction/Gate，只有 `BOOTSTRAP_AUTHORIZED` 才能一次建立真实的五账本。反过来，五账本只存在一部分也不是新项目，应进入恢复而非覆盖或重新初始化。

所有文件使用清晰的 Markdown、绝对日期（`YYYY-MM-DD`）和显式状态。需要时间时包含时区。未知内容写“未知/待验证”，不要猜成事实。

字段权威归属如下：

- `PROJECT.md`：目标、用户、范围、资源和约束；
- `ROADMAP.md`：阶段、里程碑、优先级和行动状态；
- `DECISIONS.md`：重要决定、理由和取代关系；
- `AGENTS.md`：Agent 生命周期、任务状态和写入所有权；
- `STATUS.md`：从前四份账本派生的最新摘要，不作为冲突时的最终权威。
- `ACTIVE_SUPERVISOR.json`：控制谁能修改上述 canonical 状态；不承载产品/项目事实。
- `STRATEGY.json`：控制 Direction Clarity、候选/选定方向、Strategic Gate、项目级 Autonomy、pending Decision/STATE_SYNC/report；不取代 `PROJECT.md` 与 `DECISIONS.md` 的正式业务记录。
- `.founder/workstreams/**`：下级工作线状态；不能覆盖 canonical 账本。
- `.founder/integrations/**`：复杂 Integration Gate 的输入与证据；只有 ACTIVE 接受全局 Gate。
- `SKILLS.md`：可选能力/信任登记；只有实际启用 Registry 时创建。

每次跨账本更新生成一个不会依赖本地递增计数的协调版本，例如 `R-20260811T092315Z-a1b2c3`（UTC 时间加短随机/任务标识）。先更新发生变化的权威账本，再最后更新 `STATUS.md`；未变化账本保留原 `Last revision`。`STATUS.md` 用 `Source revisions` 保存四份权威账本的精确版本映射，并用 `Reconciled revision` 标记本轮完整协调；Supervisor control record/lock 还为四账本和 STATUS 保存完整文件 SHA-256，内容变化不能靠保留旧 revision 绕过。如果 `STRATEGY.json` 存在，Supervisor fingerprints 同时保存完整 `STRATEGY_REVISION + STRATEGY_SHA256` 和语义 `STRATEGY_CONTEXT_REVISION + STRATEGY_CONTEXT_SHA256`：前者捕获任何控制变化，后者只在 selected strategy/Autonomy 语义变化时轮换并供 Worker stale 检测。不要用完整 Strategy SHA 作为 Worker baseline。另记录 `Supervisor revision`，但不把控制记录加入旧四源权威映射。恢复时逐项比较 revision + hash，任何不一致都先检查和协调，不直接相信旧快照。旧项目没有版本字段或 Supervisor/Strategy record 时不视为损坏；首次执行型恢复时先交叉检查，再安全迁移并在 `STATUS.md` 记录。

## `.founder/PROJECT.md`

```markdown
# Project

- Project: ...
- Last updated: YYYY-MM-DD
- Last revision: R-YYYYMMDDTHHMMSSZ-xxxxxx
- Current stage: Discovery | Validation | Planning | Build | Launch | Operate | ...

## Final Outcome

...

## Target Users and Needs

...

## Success Criteria

- ...

## Scope

### In Scope

- ...

### Out of Scope

- ...

## Existing Resources

- ...

## Constraints

- ...

## Confirmed Facts

- ...

## Working Assumptions

- A-001: ...; validation/reversal trigger: ...

## Open Questions

- ...
```

把目标、用户需求、资源、约束、成功标准和当前阶段写在这里。工作假设必须带可验证或撤销条件；成为重要决策时同时追加到 `DECISIONS.md`。

## `.founder/ROADMAP.md`

```markdown
# Roadmap

- Last updated: YYYY-MM-DD
- Last revision: R-YYYYMMDDTHHMMSSZ-xxxxxx
- Current phase: ...
- Current milestone: ...
- Top priority: ...

## Phases

| ID | Phase | Outcome / exit criteria | Status |
|---|---|---|---|
| P1 | ... | ... | active |

## Current Milestone

### Intended Outcome

...

### Deliverables and Acceptance

- [ ] Deliverable — acceptance evidence

### Next Actions

| Priority | Action | Owner | Dependency | Status |
|---|---|---|---|---|
| P0 | ... | FounderOS / Agent ID | none | ready |

## Workstreams

| ID | Outcome / exit criteria | Lead / owner | Status | Depends on | Interface contract | Write scope | Canonical baseline |
|---|---|---|---|---|---|---|---|
| ... | ... | FounderOS / real Agent ID | active | none | none | exact paths | revision/hash |

## Dependency Gates

| Task | Class | depends_on | blocked_by | unblocks | Interface revision/hash | Gate status |
|---|---|---|---|---|---|---|
| ... | INDEPENDENT / DEPENDENT / INTERFACE-SEPARABLE | ... | ... | ... | ... | blocked / ready |

## Later / Parking Lot

- ...
```

Workstream/Dependency 表仅在实际存在多线或依赖时加入，旧 ROADMAP 没有它们仍合法。阶段状态建议使用 `planned`、`active`、`blocked`、`complete`；Workstream 可增加 `ready-for-integration`、`integrated`。只有出口条件及所需 Integration Gate 有证据时才标记阶段 `complete`。保持“下一步”短小且可执行，不把整个长期待办全部放进当前列表。

## `.founder/DECISIONS.md`

```markdown
# Decisions

- Last revision: R-YYYYMMDDTHHMMSSZ-xxxxxx

## Decision Index

| ID | Date | Decision | Status | Supersedes |
|---|---|---|---|---|
| D-YYYYMMDD-001 | YYYY-MM-DD | ... | active | none |

## D-YYYYMMDD-001 — Short title

- Date: YYYY-MM-DD
- Status: active | superseded | rejected
- Owner: User | FounderOS
- Supersedes: none | D-...
- Context: ...
- Decision: ...
- Rationale: ...
- Alternatives considered: ...
- Assumptions: ...
- Consequences and risks: ...
- Reversal / review trigger: ...
- Evidence: ...
```

记录影响范围、架构、预算、路线、关键默认值或未来 Agent 必须知道的决定。纠正旧决定时新增记录并将旧记录状态改为 `superseded`；保留原文和关联 ID。

### L2/L3 战略决策的可审计块

所有 L2 和获批准的 L3 必须在同一个 Decision 块内使用可定位的显式字段，不得只在散文中暗示。`decision_state.py confirm-canonical` 会按当前 proposal/decision 精确验证这些字段；字段值必须是真实选择与证据，不能为了通过脚本而伪造。

L2 最小格式：

```markdown
## D-YYYYMMDD-001 — Strategic direction

- Decision ID: D-YYYYMMDD-001
- Proposal ID: P-...
- Level: L2
- Date: YYYY-MM-DD
- Status: active
- Selected Strategy ID: candidate-id
- Decision Authority: founder | delegated | autonomy
- Candidate Options: A ...; B ...; C ...
- FounderOS Recommendation: ...
- Rationale: ...
- Assumptions: ...
- Reconsideration Trigger: ...
- Authorization Evidence: Founder message / current Gate delegation / project Autonomy evidence
- Consequences and risks: ...
- Evidence: ...
```

L3 使用同样的 `Decision ID / Proposal ID / Level / Rationale / Assumptions / Reconsideration Trigger`，并必须另有与当前 Gate 一致的 `Action Scope`和 Founder 明确批准证据。L2 至少记录候选、FounderOS 推荐、最终选择/授权方式、理由、假设、影响与 reconsideration trigger。修改方向时追加新 Decision 并取代旧记录，不编辑旧块伪造当前授权。

Founder 只回答“A”或候选 ID 时已是有效选择，不得要求其解释理由。此时 `Rationale` 由 FounderOS 如实记录“Founder selected A”、已呈现的推荐依据与仍存风险，不伪造 Founder 的主观动机。

## `.founder/AGENTS.md`

```markdown
# Agents

- Last updated: YYYY-MM-DD HH:MM TZ
- Last revision: R-YYYYMMDDTHHMMSSZ-xxxxxx

## Active Assignments

| Agent ID | Role | Reports to | Workstream | Mission / task | Read scope | Write scope | Dependencies / baseline | Can create subagents | Runtime state | Project disposition | Created | Last update | Deliverable / acceptance |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| ... | ... | FounderOS / Lead ID | none / ID | ... | paths | read-only / paths | IDs + revision/hash | false / bounded Lead grant | running | pending-review | ... | ... | ... |

## Pending Dispatch and Write Reservations

| Assignment ID | Intended role | Exact write scope | Status | Reserved at | Resolution |
|---|---|---|---|---|---|
| ... | ... | exact paths | pending-dispatch | ... | bind Agent ID / release |

## Assignment History

### Agent ID — Role

- Why needed now: ...
- Mission: ...
- Task: ...
- Deliverables: ...
- Constraints / write scope: ...
- Acceptance criteria: ...
- Reports to / Workstream: ...
- Read scope / write scope: ...
- Dependencies / canonical baseline: ...
- Can create subagents / escalation: ...
- Runtime outcome: returned | interrupted | failed | unknown
- Project disposition: pending-review | accepted | changes-requested | blocked | cancelled | superseded
- Evidence / review: ...
- Closed: ...

#### Reviewer Findings

| Finding ID | Severity | Evidence | Disposition | Rationale | Residual risk / limitation |
|---|---|---|---|---|---|
| ... | ... | path/source | fixed / non-blocking / changes-requested | ... | none / ... |
```

写入 Agent 先建立 `pending-dispatch` 预留，成功后绑定工具返回的真实 Agent ID；失败时写 `dispatch-failed` 并释放范围。Persistent Role 的稳定 `agent_id`、身份、职责、permissions、workstream、persistent/task、skills 与 historical ownership 仍写在本文件；其当前/历史办公室只引用 `THREADS.json` 的 binding record，不把 runtime Thread ID 当 Agent 主键。

把实际运行状态与项目处置分开：运行状态使用 `dispatched`、`running`、`returned`、`interrupted`、`failed`、`unknown`；项目处置使用 `pending-review`、`accepted`、`changes-requested`、`blocked`、`cancelled`、`superseded`、`closed`。保留历史；从活动表移除时写入真实运行结果和最终处置。`timeout` 只是观察结果，不是终态；确认 Agent 已停止后才能释放其写入所有权。只把真实创建的 Agent 写入 Agent 清单，预留项不冒充已创建 Agent；FounderOS 自己执行写 `executor: FounderOS`，不得伪造专业 Agent。

## `.founder/STATUS.md`

```markdown
# Status

- As of: YYYY-MM-DD HH:MM TZ
- Reconciled revision: R-YYYYMMDDTHHMMSSZ-xxxxxx
- Source revisions: PROJECT=R-...; ROADMAP=R-...; DECISIONS=R-...; AGENTS=R-...
- Overall: on-track | at-risk | blocked | paused
- Current phase: ...
- Current milestone: ...
- Supervisor mode / revision: ACTIVE / S-...

## Executive Summary

...

## Completed and Accepted

- ... — evidence: ...

## In Progress

- ... — owner: ...; expected output: ...

## Agents Working

- Agent ID / role — task — status

## Workstreams

- Workstream / Lead / status / dependency or Integration Gate

## Risks and Unknowns

- Severity — risk — mitigation / validation

## Blockers

- None.

## Next Actions

1. ...

## Decisions Needed from User

- None immediately.

## Autonomous Strategic Decision Report

- Decision ID: D-...
- Proposal ID: P-...
- Selected Strategy ID: ...
- Rationale: ...
- Biggest Risk: ...
- Reconsideration Trigger: ...

## Evidence and Artifacts

- Artifact/path — revision or hash — verification method/command — environment — verified at
```

这是新对话最快的恢复入口，但不能替代其他账本。它的阶段、里程碑、Workstream 和 Agent 列表分别派生自 `ROADMAP.md` 与 `AGENTS.md`，Supervisor revision 派生自控制记录。每轮刷新时间和发生变化的内容。已完成项必须带可定位证据；若没有 Agent、Workstream、阻塞或待决事项，显式写 `None`，避免让接手者猜测。上面的 Autonomous 报告块只在存在待报告的 `autonomous_with_report` L2 Decision 时保留并填真实值；否则省略整个块。helper 只有在六字段与当前 canonical proposal/decision 精确匹配，并提供真实老板摘要 `delivery_ref` 时才清除 pending report。

## `.founder/ACTIVE_SUPERVISOR.json`

这是持久控制面记录，不替代五份 Markdown 账本。完整模式判定、schema、handoff/takeover/recovery 和退化规则见 [supervision.md](supervision.md)。

新 Bootstrap 与安全迁移使用 schema version 1，至少记录：canonical root、logical supervisor ID、可用时的 runtime identity、identity quality、mode、record revision、activation token、activated/last_seen、lease、handoff、transition/takeover/recovery、previous supervisor，以及 canonical revision + 完整文件 SHA-256 fingerprints。Strategy 存在时，source fingerprints 另包含完整 `STRATEGY_REVISION/SHA256` 和语义 `STRATEGY_CONTEXT_REVISION/SHA256`；Strategy 不存在的旧状态保持原有 fingerprint shape，不用伪造的 `ABSENT` 字段破坏 V2 兼容。

只有持有当前 token 且原子取得写锁的 ACTIVE 能修改它。ADVISOR/REVIEWER/Lead/Specialist 不更新它；`last_seen` 旧不代表 owner 已死。写锁还必须包含规范化 project root、supervisor ID、token、Supervisor epoch revision、committed state SHA 和 source fingerprints；ACTIVE 在派发、canonical 更新和 Integration Gate 前重新核对全部 binding。

可用时用 `scripts/supervisor_guard.py` 做 expected-state-hash/CAS。普通 `verify` 必须同时通过控制 binding 与当前 canonical revision + SHA-256 fingerprints；持锁 ACTIVE 有意改完账本后，只能在实际核对差异的前提下运行 `checkpoint` 协调。内部返回的 `FENCE_VALID_CHECKPOINT_ONLY` 只授权这次协调，不授权派发、handoff、release、Integration Gate 或其他写入。

脚本不能提供 runtime liveness 或身份认证；调用者仍需真实工具证据和 Founder 授权。旧项目缺少控制记录时按安全迁移处理，不按损坏清空重建。若已有 Supervisor record 只有 revision 而没有完整 SHA-256 baseline，普通 claim/verify 不得静默补齐并吸收同 revision 内容漂移；进入 RECOVERY，先审计、备份并由持权方显式完成迁移。

## 可选 `.founder/STRATEGY.json`

完整 Direction Clarity、Discovery、Strategic Gate、L0–L3、Autonomy 和运行中 Pivot 规则见 [founder-discovery.md](founder-discovery.md)。本文只规定文件边界：

- 它是可选战略控制面，新项目执行型启动时默认创建；不是第六份 canonical 业务账本；
- 它保存 project binding、`project_phase`、Clarity、Discovery depth/candidates/recommendation、selected strategy、Gate/proposal、项目级 Autonomy、pre-bootstrap Discovery assignments、pending canonical Decision、STATE_SYNC、boss-report 义务、一次性 Founder authorization receipts，以及 L3 action 的 `approved/consumed` 状态与 execution reference；
- 它不取代 `PROJECT.md` 中的正式目标/用户/约束，也不取代 `DECISIONS.md` 中的 L2/L3 历史；冲突时停止执行并由 ACTIVE 协调；
- `strategy_revision + 完整文件 SHA-256` 是 Supervisor/handoff/recovery 指纹；`context_revision + context_sha256` 是 selected direction/Autonomy 的语义指纹，供 Thread baseline/stale sync 使用；
- 只能由持有当前 ACTIVE token、项目写锁、expected Supervisor SHA 和 expected Strategy SHA 的 Main Thread mutation；成功 mutation 后必须重新 inspect，不复用旧 SHA；
- `inspect` 和 `authorize` 必须 0 写入；只读请求不得创建、迁移或更新 Strategy；
- Founder 选择/委托、Profile 调整与 L3 批准/拒绝引用按项目保存哈希 receipt，同一原始授权引用不能再次绑定；L3 canonical 批准在真实动作前以唯一 `execution_ref` 单向消费，消费后不能恢复为可用；
- malformed JSON、wrong-project binding、symlink/junction/reparse、多硬链、指纹漂移或未知 `.strategy-state-lock.json` 一律 fail closed，保持锁并进入 RECOVERY；不手工删锁或覆盖文件。

合法 pre-bootstrap Strategy 的 `project_phase=pre-bootstrap`，Gate 只能处于 `DIRECTION_CHECK_REQUIRED / DISCOVERY_ACTIVE / STRATEGIC_CHOICE_REQUIRED / BOOTSTRAP_AUTHORIZED`。Discovery 可以临时记录真实、只读的 Research subagent runtime ID；正式 Bootstrap 时必须迁入 `AGENTS.md` 历史。五账本已存在而 Strategy 缺失的旧项目，只读时不迁移；执行时在 ACTIVE fencing 内从 `PROJECT.md/DECISIONS.md` 推断已选方向，以默认 Autonomy 初始化 `LEGACY_INFERRED + OPERATING`，不重新 Bootstrap。

## 可选 `.founder/THREADS.json`

完整 schema、lifecycle、handoff 和恢复规则见 [thread-manager.md](thread-manager.md)。本文件只规定账本边界：

- 它是 Thread 控制登记册，不是第六份 canonical 业务账本；
- `AGENTS.md` 管 Agent identity，`THREADS.json` 管可更换的 runtime binding；
- 旧项目缺少它时仍是有效 V1 项目，不重新 Bootstrap；首次真实需要 Persistent Thread 才初始化；
- 文件存在时，其 `registry_revision` 与完整 SHA-256 纳入 `ACTIVE_SUPERVISOR.json`/写锁 source fingerprints；正文同 revision 漂移也必须被检测；
- 如果 Strategy 存在，Thread 的 canonical context baseline 同时绑定 `STRATEGY_CONTEXT_REVISION/SHA256`；完整 `STRATEGY_SHA256` 留给 Supervisor fencing，不写入 Worker baseline；
- 只有持有当前 ACTIVE token、项目写锁、expected Supervisor SHA 和 expected Registry SHA 的 Main Thread 可以 mutation；
- read-only inspect、Advisor/Reviewer/Worker 不得创建或更新时间戳；
- malformed JSON、symlink/reparse、硬链接、wrong-project binding、duplicate primary 或未知 transaction lock 一律 fail closed。

使用 `scripts/thread_registry.py` 只管理结构化 registry/CAS；它不创建假的 runtime Thread。Registry 外部操作出现部分成功时保留锁并进入 RECOVERY，不把 potential orphan 包装为成功。

## Workstream 与 Integration 状态

按 [workstreams.md](workstreams.md) 动态建立；不要在 Bootstrap 创建固定部门。

- `.founder/workstreams/<safe-slug>/STATUS.md`：该线目标、Lead、baseline、依赖、产物、风险和 `ready-for-integration` 状态。
- `.founder/workstreams/<safe-slug>/TASKS.md`：只有确有独立任务队列时创建。
- `.founder/integrations/<gate-id>.md`：只有复杂/跨线/高风险 Gate 需要持久证据时创建，记录输入 revisions、接口、测试、冲突、返工和 ACTIVE 结论。

所有 slug/路径必须规范化并保持在项目根内；拒绝 `..`、绝对路径、符号链接、junction、重解析点，以及会把 canonical 状态别名到项目外的硬链接。Lead/Agent 只能写 assignment 明确授权的 Workstream 状态；只有 ACTIVE 修改 global ROADMAP/STATUS 和接受 Integration Gate。

## 可选 `.founder/SKILLS.md`

只有实际为 Agent 分配 Skill、记录 capability gap 或调用未来 Skill Curator 时，按 [skill-registry.md](skill-registry.md) 创建。第三方 Skill 默认不可信；未经审计不得自动安装或执行。空 Bootstrap 不创建该文件。

## 单写入租约

执行型回合必须先满足 Single Active Supervisor fencing，再通过原子独占创建 `.founder/.write-lock.json` 或等价 compare-and-swap 取得租约。锁至少保存：规范化项目根、持有者任务/会话标识、UTC 创建时间、基线 `Reconciled revision`（pre-bootstrap 可为不适用）、当前存在的 canonical 文件 revision + SHA-256、Strategy 存在时的完整/语义 fingerprints、supervisor ID、activation token、Supervisor epoch revision 和 committed state SHA。无法证明独占或 fencing 时保持只读。

发现已有锁时：

1. 对照 ACTIVE Supervisor record，并使用可用的任务/Agent 协调能力核实持有者；能确认仍活跃就等待或与其协调。
2. 无 `ACTIVE_SUPERVISOR.json` 却存在写锁时直接报告 RECOVERY，而不是 activation eligible。只有在持有者被确证为已完成、失败、中断或不存在时，才进入孤儿锁恢复；锁的年龄本身不是证据。
3. 比较当前四份源账本版本、`STATUS.md` 映射、可选 Strategy/Thread fingerprints、锁中基线和任何局部写入。pre-bootstrap Strategy-only 则核对 Strategy/Supervisor/事务锁，不要去猜不存在的 Markdown revisions。若无法解释差异，保持只读并请求用户决定。
4. 将旧锁原子重命名到 `.founder/backups/stale-locks/<timestamp>-<owner>.json` 进行隔离，再用原子独占创建新锁。重命名或新建竞争失败时立即退回只读。
5. 取得新锁后以 CAS 更换 Supervisor token/revision，先审计局部写入并协调账本，在 `STATUS.md` 记录接管依据和结果；协调完成后运行 guard `checkpoint` 同步当前 source revisions，确认它保留当前 Supervisor epoch revision，再在所有写入 Agent 终止后释放自己的写锁。Supervisor ACTIVE 记录保持到显式 release/handoff。

若 guard 返回 `PARTIAL_COMMIT`，锁是故意保留的故障栅栏，不按普通孤儿锁直接隔离。完整读取 `supervision.md` 的“故障原子性与修复”，只在 state hash、owner、token 和 transition 全部匹配时使用受限的 `repair-lock` 或 `clear-released-lock`；否则保持只读。

若用户要求立即严格禁写，停止 Agent 后保留现有锁和未协调状态，不用清理写入违反用户边界；下次获得写授权时按上述流程恢复。

## 创建与修复规则

1. 创建前检查 `.founder/` 是否已有用户内容；不覆盖同名文件。
2. 新项目先取得唯一 ACTIVE 和写锁，创建/inspect pre-bootstrap `STRATEGY.json`并完成 Direction Clarity/Strategic Gate；此时不创建五份空账本。只有 Gate 精确为 `BOOTSTRAP_AUTHORIZED` 时，才在同一 ACTIVE fencing 与 expected Strategy SHA 下一次建立五份互相一致的账本，用真实选定方向替换模板提示，迁入 Discovery Agent 历史，再用 `confirm-canonical` 进入 `OPERATING`。不创建空 Workstream/Thread Registry/Skill Registry/归档。
3. 部分文件缺失时，从用户最新指令、现有账本和项目证据重建；把重建依据与不确定性记录在 `STATUS.md`，必要时追加决策。
4. 文件损坏但存在时，把恢复作为一个事务：先列出所有将修改的账本，在 `.founder/backups/YYYYMMDD-HHMMSS/` 精确备份它们并写入含源路径、哈希（可用时）和目标版本的恢复清单；再保留所有可读片段并把全部替换文件暂存到同一文件系统，整体验证后依次替换权威账本，最后替换 `STATUS.md`。任一步失败就从清单回滚；若无法保证备份、目标准确、内容可保全或回滚，停止修复并请求用户决定。Bootstrap 不预创建空的 `backups/`。
5. 每轮在持有正确 Supervisor fencing 和项目级单写入租约的前提下生成协调版本，先更新发生变化的 `PROJECT.md`、`ROADMAP.md`、`DECISIONS.md`、`AGENTS.md`，最后更新 `STATUS.md` 的派生快照、当前 Supervisor epoch revision 和 `Reconciled revision`；随后运行 guard `checkpoint` 同步 canonical、Strategy 与 Thread source fingerprints，并确认返回的 Supervisor revision 未改变。Strategy mutation 由 helper 协调 checkpoint；不得在 helper 成功后再用旧 expected SHA 继续写。协调完成且所有写入 Agent 终止后释放写锁。
6. 更新后交叉检查当前阶段、里程碑、Agent 状态、写入所有权、阻塞和下一步是否一致。
7. 不把计划写成完成，不把 Agent 自报完成写成已验收，不删除仍影响项目的风险和历史决定。

## 长项目归档

仍需在每次接手时完整读取五份主账本。为避免它们无限增长，当关闭/取代记录开始妨碍快速恢复时，把较旧的关闭记录连同详细证据和过程分段移入 `.founder/history/`；不要预创建空归档目录。主账本只保留有界的活动/近期索引和一个覆盖全部历史分段的归档清单（ID 范围、日期范围、文件、哈希）。

- `DECISIONS.md` 保留全部有效决策、近期已取代决策，以及旧决策分段的归档清单；取代关系必须能从主索引定位到具体分段。
- `AGENTS.md` 保留全部活动任务、近期关闭任务，以及旧 Agent/任务分段的归档清单；每个分段保存最终结果和 Reviewer 发现表。
- `PROJECT.md`、`ROADMAP.md` 和 `STATUS.md` 保持面向当前状态，不存放过程日志。
- 只有当前任务、有效决定或审计需要某条归档记录时，才读取对应历史文件。

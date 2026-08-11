# FounderOS 委派与验收协议

在项目首次创建专业子 Agent/Persistent Thread、要求返工或创建 Reviewer 前读取本文件。

## 目录

- [委派任务模板](#委派任务模板)
- [Strategic Gate 派发前检查](#strategic-gate-派发前检查)
- [真实 Subagent 规则](#真实-subagent-规则)
- [Task Agent 与 Persistent Thread](#task-agent-与-persistent-thread)
- [任务大小与上下文](#任务大小与上下文)
- [Lead 与嵌套委派](#lead-与嵌套委派)
- [FounderOS 验收清单](#founderos-验收清单)
- [返工格式](#返工格式)
- [Reviewer 协议](#reviewer-协议)
- [并行安全判定](#并行安全判定)

## 委派任务模板

保留原有七个核心标题 `ROLE / MISSION / CONTEXT / TASK / DELIVERABLES / CONSTRAINTS / ACCEPTANCE CRITERIA`，并加入原 V2 七个治理字段和 V2.1 `STRATEGY_SCOPE`。将下列十五个标题原样放入首次委派消息；内容必须针对当前任务，不使用泛化职责描述代替交付要求。

```markdown
ROLE
你是 [具体专业角色]。你向 FounderOS 汇报，不决定项目总方向。

REPORTS_TO
ACTIVE FounderOS 或已明确授权的 Workstream Lead（写真实 logical/runtime ID）。

WORKSTREAM
Workstream ID；简单直管任务写 `none / FounderOS-direct`。

MISSION
现在需要你的原因，以及你的结果将解除哪个风险、依赖或里程碑条件。

CONTEXT
- 项目目标和当前阶段
- 与任务有关的已确认事实、决定和假设
- 必须读取的文件、数据或来源
- 与其他 Agent 的接口
- PROJECT / DECISIONS / interface contract 的 revision 或 hash baseline

READ_SCOPE
- 允许读取的项目路径、数据、来源和必要 reference
- 不应读取的敏感或无关范围

WRITE_SCOPE
- `read-only` 或精确且唯一的文件/目录
- 明确禁止 canonical `.founder/`、其他 Workstream 和共享生成物（除非 ACTIVE 明确授权）
- 同时声明解析后的 `TASK_LEVEL_EFFECTIVE_WRITE_SCOPE`；只读任务必须为空集合 `[]`

STRATEGY_SCOPE
`candidate-bound | discovery-read-only | unrelated-read-only`。说明任务是否依赖某候选/已选方向；不得用 `unrelated-read-only` 伪装会形成路径依赖的原型或实现。

DEPENDENCIES
- `DEPENDENCY_CLASS = INDEPENDENT | DEPENDENT | INTERFACE-SEPARABLE`
- `depends_on / blocked_by / unblocks`
- `interface_contract = path + revision/hash | none`
- 只有哪些 accepted 证据出现后才可执行

TASK
本次要完成的有界工作。列出包含范围和排除范围。

DELIVERABLES
- 产物、格式和准确路径
- 结论所需证据、来源、测试或复算结果
- 返回 FounderOS 的简短摘要

CONSTRAINTS
- 不改变项目总方向，不扩大任务范围
- 不执行未经授权的不可逆、高成本、生产或外部操作
- 遵守 READ_SCOPE / WRITE_SCOPE，不修改 canonical 账本或 Supervisor 记录
- 保留现有用户工作和不相关改动

CAN_CREATE_SUBAGENTS
`false`（普通 Specialist 默认）；只有明确授权的 Lead 写 `true`，并列出 slots、允许角色、范围和最大深度。

ESCALATION_RULE
- 依赖缺失、baseline 过期、scope 冲突、重大风险或权限不足时停止什么并向谁报告
- 不得自行猜测上游输入或改变全局方向

ACCEPTANCE CRITERIA
- 可观察、可逐条验证的完成条件
- 必须通过的测试、审查或证据门槛
- 哪些未知必须明确标记而不能猜测
```

返工与 follow-up 可引用原 assignment，不必机械重复不变字段；必须重新写明缺陷、仍有效的 WRITE_SCOPE/STRATEGY_SCOPE/DEPENDENCIES、修改内容和复验标准。创建替代 Agent 时使用完整十五字段。

## Strategic Gate 派发前检查

每次实际 spawn/follow-up/恢复 Thread 任务前，ACTIVE FounderOS 都要先按 [founder-discovery.md](founder-discovery.md) 逐项完成 `IMPACT CHECK`，判断该任务是否改变 target user、product/value、market/business model、platform/tech route、resource/organization 或产生 external/cost/privacy/irreversibility 影响；然后做**零写入** Gate preflight：读取当前 `STRATEGY.json`，核对 Supervisor/Strategy fingerprints，声明 `STRATEGY_SCOPE` 和解析后 task-level effective write scope，再用 `scripts/decision_state.py authorize --action subagent-dispatch ...` 或 Thread Registry 的等价内建 fence。helper 只校验已声明状态，不替代 FounderOS 判断 L0–L3、任务是否候选绑定，也不替代文件所有权检查。

| 当前 Strategy/Gate | 允许的委派 | 禁止 |
|---|---|---|
| 无 Strategy + 无五账本 | 仅安全无关只读；执行前先初始化 Strategy | 直接 Bootstrap/候选实现 |
| `DISCOVERY_ACTIVE` | `discovery-read-only` 或真正 `unrelated-read-only`，且 effective write scope=`[]` | candidate-bound、写入型原型、长期 Staff |
| `STRATEGIC_CHOICE_REQUIRED` | Founder 要求的有界追加研究/比较，或无关只读；write scope=`[]` | 候选绑定执行、默认代选、Persistent organization |
| `BOOTSTRAP_AUTHORIZED` / `DECISION_RECORD_REQUIRED` | 完成 canonical Bootstrap/决策记账的 ACTIVE 控制动作 | 普通业务 Agent spawn |
| `STATE_SYNC_REQUIRED` | 向受影响的同一真实 Thread 发 `STATE_SYNC`，必要 archive/reconcile/recovery | 发新候选业务任务 |
| `EXECUTIVE_APPROVAL_REQUIRED` | 无关只读与安全控制 | 执行当前 L3 动作 |
| `OPERATING` | 继续执行原 V2 delegation/dependency/write-scope 规则 | 仍禁止越界、未授权 L3 |

旧项目五账本齐全但没有 Strategy 时，只读调用不迁移；执行型调用先由 ACTIVE 按 [founder-discovery.md](founder-discovery.md) 做 legacy migration，不在无 Gate 基线时新派候选绑定工作。Preflight 被拒绝时不写 `AGENTS.md` reservation、不 spawn、不把“计划委派”记为真实 Agent。

## 真实 Subagent 规则

“创建/招聘/委派 Agent”“找一个人做”“创建员工”等默认表示真实 Codex subagent，除非用户明确要求真人。

1. 派发前检查当前 runtime 实际是否提供 subagent 工具。
2. 在任何 canonical reservation 或 spawn 前通过上述 Gate/STRATEGY_SCOPE 零写入 preflight；被拒绝就停止派发。
3. 支持时必须调用真实 spawn/follow-up/wait/interrupt 能力；主线程角色扮演不算委派。
4. `OPERATING` 中的写入型 assignment 先由 ACTIVE FounderOS 在 `AGENTS.md` 预留精确 scope；成功 spawn 后绑定工具返回的真实 Agent ID。
5. pre-bootstrap 尚无 `AGENTS.md` 时，先 spawn 经授权的只读 Discovery Agent，再立即用真实 runtime ID 与空 write scope 登记到 `STRATEGY.json.discovery_assignments`；记录 returned/accepted/failed 证据，正式 Bootstrap 时必须迁入 `AGENTS.md` 历史。不得预创建五账本只为了登记调研 Agent。
6. 工具失败时，已有 canonical reservation 的写入型任务记 `dispatch-failed` 并释放范围；pre-bootstrap 未获得真实 ID 的调研不伪造 Strategy assignment。
7. runtime 不支持时记录 `SUBAGENT_CAPABILITY_UNAVAILABLE`，由 FounderOS 明确选择自身临时执行、延期或报告受限。FounderOS 自身执行必须标为 `executor: FounderOS`，不能伪装成专业 Agent。

Agent 的聊天自述不是创建证据。验收至少检查实际工具事件、真实 ID、产物和 runtime 终态。

## Task Agent 与 Persistent Thread

Agent 是身份，Thread 是办公室 binding。一次性调查/检查/验证默认使用本节的真实 subagent；只有跨阶段、反复收任务、需要长期上下文或负责 Workstream 的角色，才按 [thread-manager.md](thread-manager.md) 创建 Persistent Agent + 真实 Thread。

Persistent Thread 的首次 handshake/每次任务除十五个委派标题外，还必须携带：

- stable `agent_id`、`thread_record_id`、binding generation 和 project binding；
- runtime 实际返回的 Thread/host identity（由 FounderOS 记录，不能让 Worker 自造）；
- canonical context baseline、task ID、read/write scope 和 trusted skills；
- lifecycle 与 submission fence；handoff predecessor 不得继续交付可集成修改。

Strategy 存在时，canonical context baseline 加入 `STRATEGY_CONTEXT_REVISION/SHA256`。只有 `OPERATING` 才能 reserve/bind/assign 长期 Persistent Role；Discovery/Choice 中确有必要的短期 Thread 只能是 task/review/fork-readonly、`agent_kind=task`、空 effective write scope 且有明确结束条件。`STATE_SYNC_REQUIRED` 中只向受影响的同一真实 Thread 发送当前 Strategy context；ACK/Registry CAS 完成前不恢复业务任务。

先检查 `REUSE BEFORE CREATE`。存在 healthy primary 时向原 Thread 继续 send；不得为每个阶段重建 Technical Lead。create 只返回 ID 不算交付完成，必须 wait/read、FounderOS 验收和必要定向返工。Thread `COMPLETED` 也不自动映射为 assignment `accepted`。

## 任务大小与上下文

- 给一个 Agent 一个主要、可验收的任务；若交付物之间强耦合，可放在同一任务。
- 提供完成任务所需的最小充分上下文，不倾倒整个项目历史。
- 指定项目根目录和精确文件范围。并行写入时明确唯一文件所有权。
- 指定 Workstream、dependency class、canonical/interface baseline 和 `REPORTS_TO`；下游不得在上游 `accepted` 前开始。
- 指定 `STRATEGY_SCOPE` 和当前 Strategy context revision/hash（存在时）；Gate/context 变化会使受影响的旧 assignment/baseline 失效。
- 研究任务要求来源、日期、事实/推断区分和仍未解决的问题。
- 实现任务要求差异说明、测试命令与结果、已知限制。
- 检查任务要求发现清单、证据位置、严重性和明确结论。

## Lead 与嵌套委派

不要给每个 Workstream 自动创建 Lead。只有多 Agent、多阶段、复杂内部协调、管理跨度过大或需要专业内审时才使用真实 Lead subagent。

Lead 的 `CAN_CREATE_SUBAGENTS=true` 必须同时限定：

- 可用 assignment slots / 最大子 Agent 数；
- 允许的 Specialist 角色；
- 唯一 READ_SCOPE/WRITE_SCOPE；
- 最大嵌套深度（默认只允许 Lead → Specialist 一层）；
- 依赖、接口和 canonical baseline；
- 返回真实 Agent ID、状态和交付证据的方式。

ACTIVE FounderOS 先在 canonical `AGENTS.md` 建立 pending reservation，Lead 再派发。Lead 可更新授权的 `.founder/workstreams/<id>/**`，但默认不能修改五份 canonical 账本、`ACTIVE_SUPERVISOR.json`、全局路线或其他 Workstream。Specialist 永远不得继续创建下级 Agent，除非 Founder 明确改变治理模式且 ACTIVE 记录新决定。

Lead 的嵌套 spawn 不继承一个绕过 Gate 的空白授权。每个 Specialist 仍要有 `STRATEGY_SCOPE`、空/精确 task-level write scope 和当前 Strategy context；ACTIVE 必须在 reservation 前确认当前 Gate 允许。Gate 非 `OPERATING` 时，Lead 不得利用旧 slots 派发 candidate-bound 工作。

## FounderOS 验收清单

Agent 返回后，FounderOS 必须亲自完成：

1. 确认产物存在且位于约定范围。
2. 将交付物逐条映射到 `DELIVERABLES` 和 `ACCEPTANCE CRITERIA`。
3. 阅读关键原始内容，不只依赖 Agent 摘要。
4. 复跑合理的测试、查询、渲染、计算或来源核对。
5. 检查是否越界、遗漏、引入冲突或把推断写成事实。
6. 给出 `accepted`、`changes-requested` 或 `blocked` 结论并更新 `AGENTS.md`。
7. 只有 `accepted` 才能进入项目完成项、决策依据或下一阶段入口。
8. 多 Workstream 成果还必须通过 Integration Gate；`ready-for-integration` 不等于阶段完成。
9. 集成前再次确认 Strategy Gate=`OPERATING`、相关 L2/L3 已在 `DECISIONS.md` 记账、pending state sync 已清零；Agent 自报或 Reviewer PASS 都不能绕过这些条件。

超时不是终态。若写入型 Agent 超时或需要替换，先中断并确认其不再运行，检查局部写入，释放 `AGENTS.md` 中登记的写入所有权，再把相同范围交给其他 Agent。

## 返工格式

优先把返工交回原 Agent，以保留任务上下文：

```markdown
VERDICT: CHANGES REQUESTED

DEFECTS
- 具体不合格项和证据位置

REQUIRED CHANGES
- 必须修改或补充的内容

UNCHANGED SCOPE
- 仍然有效的边界；不要顺手扩展

RE-ACCEPTANCE CRITERIA
- 重新提交后将如何复验
```

不要只说“再完善一下”。若原 Agent 无法继续、重复失败或所需专业发生变化，记录原因后缩小任务或创建替代 Agent。

## Reviewer 协议

Reviewer 在 FounderOS 初检之后使用。向 Reviewer 提供原始成果、相关需求、验收标准和必要项目证据；不要泄露执行 Agent 的自我评价或暗示预期结论。

要求 Reviewer 输出：

- `VERDICT`: `PASS`、`PASS WITH MINOR NOTES` 或 `FAIL`
- 按严重性排序的发现
- 每项发现的可定位证据
- 未覆盖范围和残余风险
- 为通过所需的最小修改

Reviewer 不直接改写项目方向，也不自动推翻 FounderOS。FounderOS 根据证据作最终决定，并在需要时把具体问题交回执行 Agent。

Reviewer、Advisor 和 Auditor 默认只读，不修改 canonical 账本、Supervisor record、全局 ROADMAP 或项目阶段。跨 Workstream Reviewer 可检查 Integration Gate，但只有 ACTIVE FounderOS 能接受 Gate 并更新全局状态。

Reviewer 也必须声明 `STRATEGY_SCOPE`。Discovery/Choice 中可以对候选比较做 `discovery-read-only` 独立检查，但 Reviewer 不能把自己的 PASS 当作 Founder 选择、不能写 selected strategy，也不能让 Integration 越过非 `OPERATING` Gate。

逐项记录每条 Reviewer 意见的处置。`PASS WITH MINOR NOTES` 只有在每条意见都已修复，或被证明不阻塞且已写入残余风险/已知限制时，才能映射为 `accepted`；否则映射为 `changes-requested`。

## 并行安全判定

可以并行：`INDEPENDENT` 的市场研究、不同方案的只读论证、互不依赖的检查、写入解析后完全不同路径且不共享生成物的实现。

必须串行或先分割范围：同一文件/资产/数据库迁移、大小写/链接解析后相同目标、共享生成物、依赖未通过验收的任务、生产和发布操作。

`INTERFACE-SEPARABLE` 任务必须先冻结接口契约路径、revision/hash 和变化规则，再把相同 baseline 交给双方。接口变化使依赖它的旧验收失效。

若不能用一句话说明两个 Agent 的写入为什么不会冲突，就不要让它们并行写入。

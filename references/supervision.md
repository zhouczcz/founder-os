# FounderOS Supervisor 协调协议

在进入已有项目、取得全局写权限、迁移旧项目，或执行 handoff、takeover、recovery 前完整读取本文件。

## 目录

- [组织角色](#组织角色)
- [Single Active Supervisor Rule](#single-active-supervisor-rule)
- [Existing Project Adoption 与 ACTIVE](#existing-project-adoption-与-active)
- [控制记录与写锁](#控制记录与写锁)
- [Strategy 控制面与双指纹](#strategy-控制面与双指纹)
- [Capability 与 Skill 控制面](#capability-与-skill-控制面)
- [故障原子性与修复](#故障原子性与修复)
- [进入模式判定](#进入模式判定)
- [ACTIVE 执行栅栏](#active-执行栅栏)
- [Handoff](#handoff)
- [FounderOS Main Thread Handoff](#founderos-main-thread-handoff)
- [Takeover 与 Recovery](#takeover-与-recovery)
- [旧项目迁移](#旧项目迁移)
- [退化与限制](#退化与限制)

## 组织角色

项目治理层级是：

`Founder → ACTIVE FounderOS Main Thread → Workstream Lead Thread/Agent → Specialist Thread/Task Agent`

还可以存在 Advisor、Reviewer、Auditor；它们不拥有全局项目控制权。

| 角色 / 模式 | 允许 | 默认禁止 |
|---|---|---|
| Founder | 决定重大方向、成本、不可逆行为和外部承诺 | 亲自协调普通 Agent 不是必需职责 |
| ACTIVE FounderOS Main Thread | 修改 canonical 状态、调度项目级 Agent/Thread、创建 Workstream、改变优先级、执行 Integration Gate | 把全局方向交给普通 Agent |
| ADVISOR | 只读分析并在对话或消息中提交 Proposal | 修改项目、创建项目级 Agent/Thread、改变阶段 |
| REVIEWER / AUDITOR | 只读检查计划、成果、风险、Agent 使用和治理一致性 | 直接接管、重写 canonical 状态 |
| Workstream Lead | 在授权范围内协调一个 Workstream，按明确授权创建 Specialist | 修改全局账本、跨线改方向、无限递归委派 |
| Specialist Agent | 完成一个有界专业任务 | 创建下级 Agent、修改全局状态或扩大范围 |

只读用户请求始终保持只读。即使没有 ACTIVE，也不得为了登记模式、修复状态或更新 `last_seen` 而写文件。

## Single Active Supervisor Rule

同一个规范化项目根在任何时刻最多只有一个拥有全局写入和调度权的 ACTIVE FounderOS。该约束是协作式 fencing，不是身份认证或操作系统级安全边界。

必须同时满足以下条件才能执行项目级写入或调度：

1. `.founder/ACTIVE_SUPERVISOR.json` 指向当前逻辑 Supervisor；
2. 当前上下文持有创建时返回的 `activation_token`，不得从文件中复制 token 后冒充旧会话；
3. 当前回合原子取得 `.founder/.write-lock.json`；
4. 写锁中的 canonical project root、supervisor ID、token、Supervisor epoch revision、committed state SHA 和 source fingerprints 与当前记录一致；
5. canonical 源 revision 与完整文件 SHA-256 都没有无法解释的变化；如果 `STRATEGY.json` 存在，完整 Strategy revision/SHA 与语义 context revision/SHA 也必须与当前记录一致；如果 Skill Registry/Lock 存在，其 project binding、revision/SHA 和事务状态也必须一致。

其他 FounderOS 会话默认进入 ADVISOR；用户明确要求独立评审时进入 REVIEWER。不得因为能读到旧 Supervisor 的 ID/token、锁文件不存在或 `last_seen` 很旧，就自动成为 ACTIVE。

## Existing Project Adoption 与 ACTIVE

Existing Project 先按 [project-adoption.md](project-adoption.md) 做零写入 Entry Classification 与 `ADOPTION_READ_ONLY`。无 `.founder/` 时，严格只读 audit 不创建 `ACTIVE_SUPERVISOR.json`、Strategy、锁、账本、报告或时间戳；此阶段可以由当前 Main 协调只读调查，但它不能把未持久化身份声称成已经 canonical 的 ACTIVE owner。

正式 Adoption 仍只能由 ACTIVE FounderOS 完成：只读 baseline 和 Adoption Review 已就绪、当前调用允许写入且不存在 L2/L3 阻塞后，重新核对根目录、项目 snapshot 和 `.founder/` 仍未被其他写入者创建，再原子 claim Supervisor/写锁，建立 `pre-adoption + BASELINE_READY + ADOPTION_STATE_REQUIRED` 控制态，后补五账本并 checkpoint。任一步竞争、baseline drift、路径别名、旧 writer 证据或 `.founder/` namespace collision 都保持只读并进入 BLOCKED/RECOVERY。

用户说“接管”是对正式 Adoption 的执行意图，但不证明旧项目 writer 已终止，也不授权生产/破坏性动作。若用户同时明确“只分析/不要修改”，只交付响应内 Review，绝不 claim。Advisor 可审计、Reviewer 可验证，但只有取得本节 fencing 的 ACTIVE 能创建 canonical `.founder/`。

以下路径必须分开：

- current valid FounderOS state → 正常 restore；
- legacy FounderOS 五账本/control → 本文件的旧控制面迁移；
- partial/damaged/fake `.founder/` → RECOVERY；
- 无 `.founder/` 且有既有项目证据 → Brownfield Adoption；
- 真正空白/新项目 → Pre-bootstrap Strategy。

不得用 legacy migration 给 Brownfield 伪造 `LEGACY_INFERRED` 原始理由，也不得用 New claim/Bootstrap 重新定义既有产品。

## 控制记录与写锁

`.founder/ACTIVE_SUPERVISOR.json` 是持久控制面记录，不是第六份业务账本，也不改变五份 canonical Markdown 账本的权威归属。新 Bootstrap 创建它；旧项目在下一次安全的执行型接管时迁移。最小结构：

Existing Project 正式 Adoption 获写授权后也创建同一控制记录；这不是第二种 Supervisor schema。`pre-adoption` 与现有合法 pre-bootstrap 一样可以暂时没有五账本，但必须有已验证的 Adoption baseline、正确 Gate 和当前事务锁，且只能执行 canonicalization。

```json
{
  "schema_version": 1,
  "record_revision": "S-YYYYMMDDTHHMMSSZ-xxxxxxxx",
  "project_root": "absolute normalized path",
  "mode": "ACTIVE",
  "supervisor": {
    "logical_id": "FOS-...",
    "runtime_identity": null,
    "identity_quality": "stable | ephemeral | unavailable"
  },
  "activation_token": "opaque cooperative fencing token",
  "activated_at": "UTC timestamp",
  "last_seen_at": "UTC timestamp",
  "lease": {
    "state": "active",
    "time_is_liveness_evidence": false
  },
  "handoff": {
    "state": "none | offered",
    "target_logical_id": null,
    "basis": null,
    "offered_at": null,
    "source_revisions": {}
  },
  "transition": {
    "kind": "activation | handoff | takeover | recovery",
    "authorization_ref": null,
    "predecessor_liveness": null,
    "at": "UTC timestamp"
  },
  "previous_supervisor": null,
  "source_revisions": {}
}
```

`mode` 可以是 `ACTIVE` 或 `UNASSIGNED`。ADVISOR/REVIEWER 是当前会话模式，不去覆盖项目控制记录。

`.founder/.write-lock.json` 继续是一次写入事务的原子租约。除原有字段外，加入 `supervisor_id`、`activation_token`、`supervisor_record_revision`、`committed_supervisor_state_sha` 和包含 revision + SHA-256 的 `source_revisions`。ACTIVE 身份长期存在，写锁只在当前写入协调期间存在；不要把二者合并。

控制记录和锁中的 `source_revisions` 名称为兼容旧 schema 保留，但其当前值不是只有 revision 字符串：四份源账本与 `STATUS.md` 都保存完整文件 SHA-256，可选 Thread/Strategy/Skill 控制文件也按各自协议纳入。内容改变而 revision 文本未改变仍是漂移。canonical 与控制文件必须是 `.founder/` 内的直接单链接普通文件；符号链接、junction、重解析点或多硬链接都进入 RECOVERY。

`last_seen_at` 只在 ACTIVE 持有写锁并完成可解释的协调动作时更新。时间戳和 TTL 不能单独证明旧 Supervisor 已终止。

可用时运行 `scripts/supervisor_guard.py` 执行原子 claim/CAS、verify、checkpoint、handoff 和 recovery。普通 `verify` 同时要求 control binding 与 canonical fingerprints 当前一致；若 ACTIVE 已在持锁事务中有意修改账本，只允许先检查实际差异，再直接运行 `checkpoint`。checkpoint 内部的 `FENCE_VALID_CHECKPOINT_ONLY` 只表示 control binding 仍有效、当前唯一允许协调这些差异，不授权派发、handoff、release、Integration Gate 或其他写入。canonical 账本协调完成后、释放本轮写锁前运行 `checkpoint`，把当前 source revisions 同步写入控制记录与锁；checkpoint 保留当前 Supervisor epoch revision，并返回新的 state hash。Supervisor revision 只在新 owner 激活、handoff 接受、takeover/recovery 或显式 release 等 fencing epoch 变化时轮换，不因同一 ACTIVE 的普通 checkpoint 产生 `STATUS.md` 自引用循环。脚本不判断 runtime liveness；调用者必须从真实任务工具或 Founder 协调获得终态证据。

## Strategy 控制面与双指纹

`.founder/STRATEGY.json` 是可选战略控制面，不是第六份业务账本。完整规则见 [founder-discovery.md](founder-discovery.md)。Supervisor 只处理已声明状态的 fencing 与恢复，不用脚本判断 Clarity、候选质量或 L0–L3。

Strategy 存在时，`source_revisions` 必须同时持有：

- `STRATEGY_REVISION`：文件内的 `strategy_revision`；
- `STRATEGY_SHA256`：整个 `STRATEGY.json` 原始字节 SHA-256；
- `STRATEGY_CONTEXT_REVISION`：只在 selected strategy 或项目级 Autonomy 语义变化时轮换；
- `STRATEGY_CONTEXT_SHA256`：上述语义 context 的内容哈希。

完整 revision/SHA 用于 Supervisor verify、checkpoint、handoff freeze 和 recovery，所以候选、Gate、pending report 或 Discovery Agent 登记任何变化都能阻止 stale Main。语义 context 用于 Worker Thread baseline，不因纯控制元数据改变而产生自我 stale 循环。Strategy 不存在时保留 V2 旧 fingerprint shape；不得为了“完整”静默创建或添加伪 `ABSENT` 字段。

pre-bootstrap 期间，`ACTIVE_SUPERVISOR.json + STRATEGY.json` 且五账本不存在是合法项目状态。Supervisor claim/verify/checkpoint 必须使用 Strategy 完整指纹，而不得因 Markdown 账本尚未 Bootstrap 就进入 Recovery。在此状态下只能按 Gate 执行 Direction/Discovery/选择/记账；`BOOTSTRAP_AUTHORIZED` 前不创建 Stage A0、长期组织或候选绑定产物。

pre-adoption 期间相同的 control-only 文件形态也是合法，但只在完成零写入 baseline 且获得正式写授权后存在。Gate=`ADOPTION_STATE_REQUIRED` 时只允许以后补当前现实为目的的 canonical 写入与验证；不创建业务 Agent/Thread、Workstream、Skill Registry/binding 或执行项目修改。严格只读 Adoption 不能用本段为创建控制文件辩护。

## Capability 与 Skill 控制面

`.founder/SKILLS.md` 是项目 Skill Registry 的人读投影，`.founder/SKILL_LOCK.json` 是精确供应链、批准与 binding 的机器权威；完整职责见 [skill-registry.md](skill-registry.md)。两者都不是第六份 canonical 业务账本，也不赋予第三方 Skill 控制权。

存在时，Supervisor/写锁/checkpoint/handoff/recovery source fingerprints 加入：

- `SKILL_REGISTRY_REVISION` 与整个 `SKILLS.md` SHA-256；
- `SKILL_LOCK_REVISION` 与整个 `SKILL_LOCK.json` SHA-256。

不存在时保留旧 fingerprint shape，不创建伪 `ABSENT`。`SKILLS.md` 与 Lock 冲突时停止受影响派发和验收，以健康 Lock、真实安装内容、AGENTS/THREADS 和审计证据恢复投影；无法解释时进入 RECOVERY。

只有持有当前 ACTIVE token、项目写锁及 expected Supervisor/Registry/Lock SHA 的 Main Thread 可协调项目 Registry/Lock。Advisor/Reviewer/Worker/第三方 Skill 保持只读。Skill Curator 提供候选、审计和建议，不得自行批准、绑定或覆盖控制文件。

项目写锁不自动授权修改 `$CODEX_HOME/skills` 等全局安装目录。全局 install/update/delete 是独立 action scope，需要重新做 Strategic/风险/外部写入授权与并发检查；protected core `founder-os`、`skill-curator` 不在普通 acquisition 范围内。

Worker baseline 只保存相关 capability、Registry/Lock revision 和 bound-skill-set hash，不绑定整个文件 SHA；完整 hashes 仍用于 Supervisor fencing。添加、升级、移除、权限变化或 revoke Skill 后，受影响 Thread 必须完成 [skill-governance.md](skill-governance.md) 的 `SKILL_SYNC` 才能接新任务或提交当前结果。

## 故障原子性与修复

Supervisor record 与写锁是两个文件，无法假装为单文件原子事务。若控制记录已成功替换、随后锁的更新或清理失败，guard 返回 `PARTIAL_COMMIT / RECOVERY_REQUIRED`，并且故意保留锁；不得继续写入、删除该锁或把失败包装成成功。

- 新 ACTIVE、handoff 或 recovery 已提交但锁仍是旧 revision：只在当前 ACTIVE 仍持有创建时的 token、当前 state hash 与预期完全相同、锁 owner 未改变时运行 `repair-lock`。它只协调锁元数据，不改业务账本或 Supervisor 身份。
- 显式 release 已提交为 `UNASSIGNED` 但旧锁未清理：只在 release transition、previous supervisor、旧锁 owner/token 和当前 state hash 全部匹配时运行 `clear-released-lock`。
- 任何字段、hash、owner、token 或文件状态不匹配：保持只读，按 Recovery 流程核实；不得手工覆盖或无证据删除锁。

这两个修复命令处理可证明的半提交，不推断进程终态，也不能用来夺取另一 Supervisor 的锁。

Strategy mutation 同样是 `STRATEGY.json + ACTIVE_SUPERVISOR.json + .write-lock.json` 的协调事务。`decision_state.py` 在写 Strategy 后会做 Supervisor checkpoint；如 checkpoint、rollback 或 `.strategy-state-lock.json` 清理无法证明，它返回 `PARTIAL_COMMIT / RECOVERY_REQUIRED` 并故意保留锁。此时禁止派发、Integration、手工编辑 Strategy 或删锁。先对照 lock owner、nonce、expected Strategy/Supervisor SHA、当前完整 Strategy SHA 和 Supervisor fingerprint；只有当前 ACTIVE 仍持有原 token，或前任已有真实终止证据时，才使用 `decision_state.py recover-lock` 协调。任一字段不匹配就保持只读。

Skill mutation 是 `SKILL_LOCK.json + SKILLS.md + ACTIVE_SUPERVISOR.json + .write-lock.json` 的协调事务。若 Lock 已替换但投影/checkpoint/rollback 无法证明，保留 Skill 事务故障栅栏并停止相关 binding、dispatch、验收和 Integration；只能用受控 recovery 对照 owner/nonce、expected hashes、当前 Lock、installed content 与 Supervisor fingerprint。不得手工把 Markdown 改成“看起来一致”后继续。

## 进入模式判定

按以下顺序判定，不先写文件：

1. 规范化项目根并拒绝无法解释的符号链接、junction 或重解析点；先按 [project-adoption.md](project-adoption.md) 区分 New、无状态 Existing、current/legacy FounderOS、Recovery/collision。
2. 读取 `ACTIVE_SUPERVISOR.json`、可选 `STRATEGY.json`、五份账本、`STATUS.md` revision 映射、可选 `SKILLS.md/SKILL_LOCK.json`、`.write-lock.json`/相关事务锁（若存在）和活动 Agent 状态。Strategy 存在时先校验 project binding、schema、完整/语义 fingerprints 与 Gate；Skill control 存在时校验 project binding、投影/Lock revision、完整 hashes 与 pending skill sync。
3. 用户只要求建议/解释时进入 ADVISOR；只要求审计/评审时进入 REVIEWER。
4. 记录有效且指向另一个可能活跃的 Supervisor 时进入 ADVISOR/REVIEWER；不得写入或创建项目级 Agent。
5. 记录指向当前上下文持有的逻辑 ID/token，且 lock/state/source fingerprints 全部通过 fencing 检查时，当前会话可继续 ACTIVE。
6. 没有记录且没有残留写锁的真正 new pre-bootstrap 项目、已完成 read-only baseline 且获写授权的 Brownfield pre-adoption 项目，或无残留锁的 `UNASSIGNED` 项目，只能在执行型授权下通过原子 claim 激活；无控制记录但存在任何 `.write-lock.json` 或 `.strategy-state-lock.json` 时进入 RECOVERY，不报告 activation eligible。Existing 项目 audit 阶段不 claim。
7. 记录损坏、identity 不明、revision-only 旧 baseline、canonical fingerprint 漂移、锁/状态 binding 冲突或 liveness 无法确认时进入 `RECOVERY` 评估；在证据完成前保持只读。若漂移是当前持锁 ACTIVE 刚完成的有意账本事务，只在实际 diff 已核对时用 checkpoint 协调；inspect 不把未协调状态显示为可自由写入。

若 runtime 没有稳定 thread ID，生成逻辑 Supervisor ID 和随机 activation token，并把 runtime identity 标为 `ephemeral` 或 `unavailable`。它们是协作栅栏，不是秘密。新会话不得只凭读取文件来宣称继承同一身份。

## ACTIVE 执行栅栏

ACTIVE 在以下时点重新核对 Supervisor record revision 和 activation token：

- 取得写锁后；
- 创建、暂停、归档或重新启用项目级 Agent 前；
- 给 Lead 授权创建下级 Agent 前；
- 修改 canonical 账本或全局优先级前；
- 执行 Integration Gate、推进里程碑或发布老板摘要中的完成结论前。
- 获取、安装、登记、绑定、升级、revoke Skill 或把 Thread 恢复为 skill-current 前。

Strategy 存在时，上述每个时点还必须核对完整 Strategy fingerprints 并执行当前 Gate 授权检查。非 `OPERATING` 不自动变成失去 ACTIVE；Main 仍负责协调当前 Gate，但只能执行该 Gate 明确允许的 Direction/Discovery/Adoption 只读、Adoption canonicalization、canonical Decision、STATE_SYNC、report 或 recovery control。`ADOPTION_STATE_REQUIRED` 只允许以 baseline 为依据后补/验证五账本；候选绑定 spawn、Persistent Thread assign、Skill binding 和 Integration 必须等到 `OPERATING`。

token/revision 不匹配时立即停止新写入和调度，进入 ADVISOR/RECOVERY，确认活动写入 Agent 的终态并报告冲突；不得覆盖新 Supervisor 的成果。

## Handoff

显式 handoff 优先于 takeover：

1. 旧 ACTIVE 取得写锁并停止新派发。
2. 等待所有写入 Agent 终止，或逐项记录可安全移交的真实 runtime ID、写入范围和局部状态。
3. 协调五份账本（pre-bootstrap 时则确认它们尚未创建），冻结 source revisions + 完整文件 SHA-256；Strategy 存在时同时冻结完整/语义四指纹和当前 Gate/proposal；Skill Registry/Lock 存在时冻结两个 revision/完整 SHA、pending sync 与受影响 binding；把 `handoff.state` 写为 `offered`，记录目标逻辑 ID/描述和依据。
4. 旧 ACTIVE 释放写锁，但保持记录为 handoff offered；不再修改项目。
5. 新会话先核对当前 canonical fingerprints 与 handoff 冻结值完全一致，再取得写锁并以 expected state hash/CAS 验证提议未变化，生成新的 token 和 record revision，记录 `previous_supervisor`；任一内容 hash 漂移都进入 RECOVERY。
6. 新 ACTIVE 审计局部写入、Agent、依赖和状态后再继续。

Main handoff 是控制/恢复动作，可以在 Discovery、Choice、Decision Record 或 STATE_SYNC Gate 期间进行，但不会解除 Gate。新 ACTIVE 必须恢复同一 proposal、授权和 pending obligations；不得因换 Main Thread 而重问、默认选择或把 Gate 改成 `OPERATING`。

handoff offered 之后，旧 ACTIVE 不得用普通 checkpoint 把新的 canonical 内容吸收到 Supervisor baseline；guard 必须拒绝这种 checkpoint。`inspect` 同时比较当前 Supervisor baseline 和 handoff 冻结 fingerprints，即使旧版本记录曾错误协调过漂移，也只能报告 RECOVERY，不能报告 handoff acceptance eligible。

旧会话发现 token 已改变时必须退化为 ADVISOR。handoff 未完成不能出现两个 ACTIVE。

## FounderOS Main Thread Handoff

V2 中 Main Thread 的更换必须复用上述 Single Active Supervisor handoff，不能把创建一个新聊天等同于已取得 ACTIVE：

1. 旧 Main 停止新派发，协调活动 Worker，并 checkpoint 五账本、可选 `THREADS.json`、`STRATEGY.json` 与 Skill Registry/Lock 的完整 fingerprints；Strategy 语义 context、Capability/Skill bindings 和 pending sync 也必须冻结供 Worker 对账；
2. `offer-handoff` 冻结全部 source fingerprints 后释放写锁；
3. 新 Main 先读取/inspect Strategy 并恢复 Gate/Autonomy/pending obligations，再按 project phase 读取五账本、AGENTS、SKILLS/SKILL_LOCK、THREADS、Workstreams，以目标 logical ID 和 expected state SHA claim；
4. 新 token 生效后动态检测 runtime Thread 能力，按 exact runtime identity 对账 Persistent Threads，再继续 send；
5. 旧 Main token、Registry mutation、项目级 Thread dispatch 和 canonical write authority 一并失效。

Main Thread runtime identity 只属于 `ACTIVE_SUPERVISOR.json`，不伪装成普通 Worker binding。runtime 中某个同名 `handoff_thread` 若用于移动 git checkout/worktree，不是本协议的逻辑 handoff，禁止替代 Supervisor CAS。

新项目 Bootstrap 或 Existing Project 正式 Adoption 后创建独立用户侧总管任务时，完整遵循 [main-thread-provisioning.md](main-thread-provisioning.md)：真实 create 只产生候选 runtime identity，随后仍必须由旧 ACTIVE offer、目标 task CAS claim、verify `ACTIVE + OPERATING` 和旧 token 失效。不得把创建成功、设置标题或发送 Prompt 单独称为交接完成，也不得为 Main 初始化 Worker `THREADS.json`。

## Takeover 与 Recovery

takeover 需要 Founder 明确授权，并先尝试让旧 ACTIVE handoff、ack 或终止。recovery 用于旧 ACTIVE 被真实 runtime 证据确认为终止/不存在、控制记录损坏或孤儿锁需要协调的情况。

两者都必须：

1. 不把记录年龄当作终态证据；
2. 确认旧 Supervisor 和写入 Agent 已终止，或由 Founder 明确完成外部协调；
3. 取得写锁；旧孤儿锁按 `state-files.md` 先隔离，不能直接覆盖；
4. 对照 Supervisor baseline、五份源账本（或合法 pre-bootstrap 缺席）、STATUS 映射、Strategy 完整/语义 fingerprints、可选 Skill Registry/Lock、Thread Registry、runtime Skill availability 和局部写入；
5. 无法解释 revision/文件变化时保持只读；
6. 以 CAS 更新控制记录，生成新 token，保存 authorization、liveness evidence、previous supervisor 和 transition kind；
7. 完成恢复审计与账本/Strategy/Thread 协调后，再按恢复的 Gate 决定可否继续派发；恢复成功不等于 Gate 通过。

用户说“接管”是必要授权，但不是旧写入者已停止的充分证据。无法取得可靠 liveness 时保持 ADVISOR/RECOVERY 并请求协调。

## 旧项目迁移

旧项目缺少 `ACTIVE_SUPERVISOR.json` 不算损坏：

本节“旧项目”指已经拥有可验证 FounderOS 五账本但缺少较新控制面的项目。无 `.founder/` 的 Brownfield 走 Adoption，不使用这些迁移规则。

- 只读调用：报告 `legacy-supervision-unmigrated`，不创建文件。
- 执行型调用：原子取得既有写锁，审计五账本、活动 Agent、局部写入和 source revisions + 内容 hashes；没有未解释的旧 ACTIVE 证据时创建控制记录，并在 STATUS 记录迁移。
- 存在旧锁、活动 Agent 或不明写入时：进入 RECOVERY，不自动 claim。

已存在 Supervisor record 但只有 revision、没有 SHA-256 fingerprints 时，也不得通过普通 claim 静默升级：旧版本无法证明同 revision 正文未被改动。保持 RECOVERY，先备份和审计实际文件，再以明确迁移依据协调 control record；不要把 revision 相同当作内容相同。

不要修改旧账本的历史来伪造 Supervisor 信息；新增控制记录并做兼容映射。

旧项目五账本齐全但缺少 `STRATEGY.json` 也不算损坏，并且是与 Supervisor 迁移分开的一次控制面迁移：

- 只读调用只报告 `legacy-strategy-unmigrated`，不创建文件、不更新时间戳；
- 执行型调用在唯一 ACTIVE token、项目写锁和已审计的 `PROJECT.md/DECISIONS.md` 证据下，推断已存在的 selected direction，初始化项目默认 Autonomy 和 `LEGACY_INFERRED + OPERATING`；
- 不重新 Bootstrap，不强迫已运行项目重选方向，也不用迁移伪造新 Gate；只有旧账本本身显示 unresolved L2 ambiguity 时才开当前 Proposal；
- 无法从账本证明当前方向时保持 RECOVERY/提案状态，不把猜测写为 selected；
- 迁移后的 Strategy 完整 fingerprints 立即进入 Supervisor baseline；已有 Worker Thread 因缺少语义 Strategy context 而 stale 时，先 `STATE_SYNC` 再派新任务，不重建重复员工。

## 退化与限制

- Skill 和文件协议只能约束遵守协议的 Codex 会话或工具，不能阻止外部编辑器或恶意进程。
- 跨网络文件系统的原子创建/CAS 语义可能不可靠；无法证明原子性时保持只读。
- 不保证稳定、跨对话 runtime ID，也不保证旧 subagent 可由新会话重新连接。
- Thread runtime identity 可记录为 `observed`，但没有平台保证时不得声称永久 stable；恢复按 [thread-manager.md](thread-manager.md) 对账。
- 无活动 Codex 回合时不得声称后台持续 heartbeat 或调度。
- runtime 不支持 pause/archive/reactivate 时，只使用实际可用的 spawn、wait、interrupt、follow-up 能力并准确记录缺口。
- Supervisor guard 的 token 不是认证秘密；其价值是发现 cooperative stale writer，不是抵抗恶意冒充。

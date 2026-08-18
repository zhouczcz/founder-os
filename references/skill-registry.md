# FounderOS V2.2 Skill Registry 与 Skill Lock

在项目需要登记、批准、绑定、同步、更新或恢复 Skill 时完整读取本文件。能力规划先读 [capability-management.md](capability-management.md)，第三方审计与风险处置读 [skill-governance.md](skill-governance.md)。

## 目录

- [职责与权威](#职责与权威)
- [按需创建](#按需创建)
- [人读投影 SKILLS.md](#人读投影-skillsmd)
- [机器权威 SKILL_LOCK.json](#机器权威-skill_lockjson)
- [Skill 记录](#skill-记录)
- [状态、风险与信任](#状态风险与信任)
- [Revision 与 fingerprints](#revision-与-fingerprints)
- [Registry mutation](#registry-mutation)
- [Capability 到 Skill](#capability-到-skill)
- [Skill Performance 的独立边界](#skill-performance-的独立边界)
- [Binding](#binding)
- [Thread baseline 与 SKILL_SYNC](#thread-baseline-与-skill_sync)
- [只读规则](#只读规则)
- [恢复与旧项目迁移](#恢复与旧项目迁移)
- [Curator 降级](#curator-降级)

## 职责与权威

`.founder/SKILLS.md` 是 FounderOS/Founder 可阅读的项目级 Registry 投影；`.founder/SKILL_LOCK.json` 是精确来源、版本、hash、批准和 binding 的机器权威。两者都不是第六份 canonical 业务账本，也不改变 PROJECT/DECISIONS/ROADMAP/AGENTS/STATUS 的权威归属。

发生冲突时：

1. ACTIVE FounderOS 停止受影响 Skill 的新任务与验收；
2. 以有效 `SKILL_LOCK.json` 为机器 binding 权威；
3. 对照真实 installed content、审计证据、AGENTS/THREADS 和 runtime；
4. 在正确 fencing/写锁/CAS 下修复或重建 `SKILLS.md` 投影；
5. 无法解释时进入 RECOVERY，不让人读文本静默覆盖 Lock。

Skill Lock 不取代正式 L2/L3 Decision。改变主平台/技术路线或高风险外部操作仍写 `DECISIONS.md` 并经过 Strategic Gate。

## 按需创建

不要因可能将来需要而在 Bootstrap 创建 `.founder/SKILLS.md` 或 `.founder/SKILL_LOCK.json`。

不要在 Bootstrap 创建空 Registry/Lock。只有出现以下任一事实时，ACTIVE FounderOS 才按需初始化：

- 实际给 Agent/Thread/task 分配 Skill；
- 关键 Capability gap 需要跟踪；
- 已安装 Skill 需要当前项目批准或拒绝；
- 调用真实 Skill Curator 并需要保存审计/候选处置；
- 恢复既有 skill binding。

只读调用不得初始化、迁移、重建投影或更新时间戳。Advisor、Reviewer、Lead、Specialist 和第三方 Skill 不修改 Registry/Lock。

若项目只使用通用 Codex 能力且没有 binding，缺少两个文件是健康状态。创建 `SKILLS.md` 时同时创建可验证 Lock；不要让 Markdown 独自成为批准权威。

## 人读投影 SKILLS.md

文件顶部至少包含：

```markdown
# Skills

- Last updated: YYYY-MM-DD HH:MM TZ
- Skill registry revision: KR-YYYYMMDDTHHMMSSZ-xxxxxx
- Skill lock revision: KL-YYYYMMDDTHHMMSSZ-xxxxxx
- Project binding: <normalized-root / binding-id>
```

至少包含以下人读表：

### Capability Coverage

| Capability | Task / Workstream | Criticality | State | Primary Skill | Supporting Skills | Evidence / gap | Next action |
|---|---|---|---|---|---|---|---|

状态只使用 `REQUIRED / AVAILABLE / PARTIALLY_COVERED / MISSING / BLOCKED`。Capability 是需求，不把 Skill 包名称直接当作 Capability。

### Skill Registry

| Skill ID | Display name | Capabilities | Source / pinned version | Trust / audit | Risk | Lifecycle | Approved scope | Allowed workstreams / agents | Current users | Permissions / requirements | Last verified / evidence |
|---|---|---|---|---|---|---|---|---|---|---|---|

每项投影覆盖：`skill_id`、display name、capabilities、source/source type、version/commit、installed path、trust/audit、risk、approved scope、**allowed** workstreams/agents/threads/tasks、runtime visibility、dependencies、network/filesystem/secrets requirements、scripts present、last verified 和 deprecation status。Allowed scopes 只是授权上限，绝不冒充 current users。

`Current users` 是独立的只读派生视图：`skill_registry.py inspect` 只从同项目 direct-file `.founder/THREADS.json` 中非归档 Thread 的机器 `bound_skills` 读取，并返回 `actual_current_users`。它必须先通过 `thread_registry.py` 的完整 canonical schema/关系校验，再核对当前 Supervisor `source_revisions` 中 `THREADS_REVISION + THREADS_SHA256` 与实际原始账本字节；只有全部当前且一致，精确绑定才为 `CONFIRMED`，有效当前账本中无人使用才为 `NONE`。THREADS 缺失、损坏、wrong-project、Supervisor 缺失或 fingerprint drift、任一 legacy/malformed/incomplete machine baseline 一律让所有 Skill 为 `UNKNOWN`，不得用局部解析伪造 current users。该视图不写回 `SKILLS.md`，避免把 Thread 高频变化耦合进 Registry/Lock 双文件事务；静态表的 `Current users` 列只指向这项 read-time projection。

### Pending / Rejected / Revoked

记录 capability gap、候选比较、等待 Founder 批准、拒绝/撤销/退休原因和替代计划。保留历史，不因物理文件不存在就删除审计结论。

为兼容 Thread 最小解析，每个可绑定 Skill 必须在同一 Markdown table 行中包含精确 Skill ID、一个可识别的可信状态和 Lock revision；但 Markdown 解析成功仍不等于 binding 有效，机器校验必须读取 Lock。

## 机器权威 SKILL_LOCK.json

顶层至少包含：

```json
{
  "schema_version": 1,
  "skill_lock_revision": "KL-...",
  "skill_registry_revision": "KR-...",
  "previous_skill_lock_sha256": "sha256-or-null",
  "project_binding": {
    "project_root": "absolute normalized path",
    "project_binding_id": "stable project id"
  },
  "skills": {}
}
```

- `skill_lock_revision`：每次机器权威语义或 binding 改变时轮换。
- `skill_registry_revision`：必须与当前 `SKILLS.md` 顶部投影 revision 一致。
- `previous_skill_lock_sha256`：形成可审计的 CAS 链；首次可为 null。
- `project_binding`：拒绝把另一项目的批准/Lock 复制为当前授权。
- `skills`：以稳定 `skill_id` 为 key 的精确记录 map。

Lock 必须是 `.founder/` 内 direct、单链接普通文件。symlink、junction、reparse、多硬链、wrong-project、malformed JSON 或无法解释的 hash drift 一律 fail closed。

## Skill 记录

每个 `skills[skill_id]` 至少保存：

- `display_name / capabilities`；
- `source_type / exact_source / repo / path / ref / commit_sha`；
- 候选 `content_hash` 和本地 `installed_path / installed_hash`；
- `audit_revision / audit_status / audit_evidence`；
- `risk_level / trust_level`；
- `approved_version / approval_mode / approval_evidence / approved_scope`；
- `allowed_workstreams / allowed_agents`；
- `dependencies / network_requirement / filesystem_requirement / secrets_requirement / scripts_present`；
- `installation_timestamp / last_verification`；
- `runtime_visibility = { state, runtime, evidence_ref, observed_at }`；
- `status / update_status / deprecation_status`；
- `scoped_bindings`，只表示允许的 Agent/Thread/task/workstream 上限，以及 Primary/Supporting、permission scope 和 bound-set hash 所需字段；实际 current users 只从 THREADS 的机器 binding 做 read-time 派生。

所有 Git-backed 来源（`github`、`repository`，以及提供 repo 的 `catalog`）必须保存不可变 40 位 commit SHA，且 normalized `ref` 必须逐字符等于该 commit SHA；tag、branch、`HEAD`、catalog channel 或其他别名即使当前解析到同一 commit 也不能成为 bindable Lock 值。hash 算法与覆盖范围必须明确，不能比较两个不同清单规则生成的值。

## 状态、风险与信任

第三方 Skill 默认不可信；只有可定位审计证据、精确 hash 和当前项目批准才能改变其 trust/approval 状态。

生命周期至少支持：

`DISCOVERED / QUARANTINED / AUDITED / APPROVED / INSTALLED / VALIDATED / AVAILABLE / BOUND / REJECTED / REVOKED / DEPRECATED`

补充状态可包括 `AUDITED_NOT_EXECUTED / UPDATE_AVAILABLE / SOURCE_UNAVAILABLE`。风险只使用 `LOW / MEDIUM / HIGH / BLOCKED`。

兼容旧 Registry 的 trust states：`builtin-or-system / local-reviewed / third-party-audited / third-party-unreviewed / rejected`。V2.2 Lock 还必须保存生命周期和项目批准，不能把 `local-reviewed` 等同于 `APPROVED`。

`Installed / Trusted / Approved / Bound` 是四个独立事实。只有精确 content/installed hash、审计、风险批准、项目 scope、runtime visibility 和 binding 全部通过，才能标 `AVAILABLE` 并用于新任务。

`runtime_visibility` 对历史和非 bindable 记录是向后兼容的可选字段，缺失等价于 `NOT_CONFIRMED`；任何 `AVAILABLE / BOUND / UPDATE_AVAILABLE / SOURCE_UNAVAILABLE` 记录必须显式保存 `state=CONFIRMED` 以及具体 runtime、evidence reference 和 observation time，否则 normalize、register 或 resolve fail closed。Registry 自身拒绝 `unknown / none / unverified / pending / n/a` 等 sentinel/placeholder runtime 或 evidence，且 `observed_at` 必须是含时区的 ISO-8601；不能依赖 Curator 预先替它过滤。安装成功、静态验证成功都不能替代当前 runtime 已发现 Skill 的证据。

## Revision 与 fingerprints

当 Registry/Lock 存在时，Supervisor control、项目写锁、checkpoint、handoff 和 recovery source fingerprints 应保存：

- `SKILL_REGISTRY_REVISION` 与整个 `SKILLS.md` 原始字节 SHA-256；
- `SKILL_LOCK_REVISION` 与整个 `SKILL_LOCK.json` 原始字节 SHA-256。

它们不加入 `STATUS.md` 原有四份权威账本 revision 映射。完整 Registry/Lock hashes 保护 Main Thread 协调；Worker 不绑定整个文件 hash，而保存与自己相关的 capability baseline、两个 revision 和精确 bound-skill-set hash，避免无关 Skill 变更让所有 Thread stale。

成功 mutation 后重新 inspect/checkpoint，不能复用旧 Supervisor、Registry 或 Lock SHA。若当前 deterministic helper 尚未支持这些 fingerprints，不得在文档或状态中声称已强制执行；保持 fail closed 或只读，直到可验证支持存在。

## Registry mutation

每次创建、批准、安装确认、绑定、升级、撤销或退休按以下顺序：

1. 完成 [founder-discovery.md](founder-discovery.md) 的 IMPACT CHECK 和当前 Gate preflight；
2. 验证唯一 ACTIVE、token、项目写锁、expected Supervisor/Registry/Lock SHA；
3. 核对真实 audit/approval/runtime/installed evidence；
4. 先构造新的 Lock 和人读投影，验证 project binding、revision、hash、关系和无冲突；
5. 原子替换机器 Lock，再协调投影与 Supervisor fingerprints；
6. 任一步部分提交就保留故障栅栏，进入 RECOVERY；
7. 对受影响 Thread 标记 `SKILL_SYNC=REQUIRED`；
8. 验证/同步完成后才恢复业务任务。

确定性 helper 只校验 schema、路径、hash、CAS、状态转换和 binding；不能语义判断一个 Skill 是否安全、是否值得安装或风险等级是否正确。

## Capability 到 Skill

按 [capability-management.md](capability-management.md) 执行 `REUSE BEFORE ACQUIRE`：当前 Agent → 项目 Lock → 全局安装 → 可信组合 → Curator。只有关键 `MISSING/PARTIALLY_COVERED` 才触发 Just-in-Time acquisition。

Curator 结果先由 FounderOS 对照实际文件和 [skill-governance.md](skill-governance.md) 验收。未经审计候选只可登记为 `DISCOVERED/QUARANTINED`；搜索元数据、README 或 Curator 自报不能产生 `APPROVED`。

Capability Coverage 是派生视图。Skill 被 revoke/缺失后，相关 Capability 重新计算为 `PARTIALLY_COVERED / MISSING / BLOCKED`，不能保留虚假的 `AVAILABLE`。

## Skill Performance 的独立边界

[organization-memory.md](organization-memory.md) 可按 `skill_id + approved_version + installed_hash` 保存当前项目已验收的效果证据；升级、commit/hash 变化后建立新 performance bucket，旧版本证据只作为明确 predecessor history，不迁成新版成功统计。Skill Registry/Lock 仍是来源、Trust、风险、批准、版本、hash、runtime visibility 和 binding 的唯一机器权威。

Performance 好不能把未批准、HIGH/BLOCKED、revoke、hash mismatch 或 runtime 不可见的 Skill 变成 eligible；Performance 差也不改写供应链事实。路由流程必须先通过 Registry/Lock，再在合格候选之间参考 context-specific performance。Memory mutation 不写 `SKILLS.md/SKILL_LOCK.json`，Registry mutation也不写 Performance。

## Binding

一个 Capability 默认一个 Primary Skill，必要时增加少量 Supporting Skills。每个 binding 明确：

- `skill_id + approved version/commit + content/installed hash`；
- Primary/Supporting role 与冲突优先级；
- Agent ID、Thread record/generation、task ID、workstream；
- allowed read/write/network/filesystem/secrets/tool scope；
- capability coverage 和验收用途；
- Registry/Lock revision 与 binding hash；
- start/end、revalidation 和 revoke triggers。

有效权限是 Skill request、Agent、Workstream、FounderOS policy 与当前用户/system/runtime 授权的交集。Registry binding 不扩大任何权限。Task Agent 只绑定当前任务所需 Skill；Persistent Agent 可有稳定 Skill Profile，但每个新任务仍检查 scope、版本和 runtime visibility。

同名 Skill、同 Capability 多 Primary、版本冲突、权限冲突或 hash 不一致时 binding 必须失败或先完成明确 conflict disposition。

每次新绑定、派发或 `SKILL_SYNC` 调用 `resolve_bindings` 时，Registry 都以 Curator 的 `sha256-canonical-tree-v1` 相同清单规则重新读取所选 `installed_path` 并对比 Lock hash；一个字节的 drift、root/ancestor symlink/junction/reparse、path escape、symlink、hardlink、special file、读时身份变化、文件/目录/总 entry/总大小或深度上限都返回 `HASH_MISMATCH`。V1 canonical content hash 只纳入普通文件 path、size 与 bytes，目录本身的增删不改变 content hash；但每个目录仍必须经过安全边界检查，并计入 `2000 directories / 4000 total entries / depth 32` 的资源上限，因此空目录洪泛和深层目录攻击 fail closed。该读取不 import、解析或执行 Skill 内容，也不访问网络或安装依赖。普通只读 Registry `inspect` 不遍历 installed trees。

Registry 账本中的 `installed_path` 必须是规范化后的 canonical absolute path；Windows 8.3 short-name alias 即使指向同一目录也会被 Registry fail closed，调用方应先记录 long canonical path。当前 Windows 实现已用从卷根到 leaf 的 no-write/no-delete-sharing identity handles 验证 root、nested-directory 与 leaf replacement races；Inspector 可接受 8.3 lexical 输入，但这不改变 Registry 的 canonical-ledger 规则。POSIX 分支使用 `O_NOFOLLOW`、directory/file descriptors 与重复 identity checks，但当前版本尚未用跨平台压力测试证明与 Windows 相同等级的全生命周期 rename fence；在可能存在并发本地目录替换的 POSIX 环境中，不得声称该保证已验证，应先增加 fd-relative walker 与平台回归或保持相关第三方 binding 停用。

`founder-os` 与 `skill-curator` 是受保护的 core Skill，不进入普通项目 Registry，也不能被项目条目标为 `AVAILABLE/BOUND`；它们的更新、替换、撤销和恢复遵循独立的受保护 core release gate，不能借项目级 register/binding 绕过。每次 bindable Registry mutation 还必须读取安装目录中的唯一 YAML frontmatter，要求 semantic `name` 与 `skill_id` 精确一致，并拒绝任何位于受保护 core 目录内的 `installed_path`；因此换一个普通 ID 不能把 core 内容别名登记或绑定。普通只读 `inspect` 不遍历这些全局目录。

## Thread baseline 与 SKILL_SYNC

Thread Registry 保留旧 `skills` 字段用于兼容，并可增加：

- `capability_baseline`：当前相关 Capability 列表；
- `skill_registry_revision`；
- `skill_lock_revision`；
- `bound_skills`：精确 Skill/版本/hash/role/scope；
- `skill_sync_state = CURRENT | REQUIRED | LEGACY_MIGRATION_REQUIRED | BLOCKED`；
- `last_skill_sync`：runtime identity、markers、evidence 和时间。

派发、恢复到 WORKING、接受结果和 handoff cutover 前同时检查 `STATE_SYNC` 与 `SKILL_SYNC`。精确 ACK marker 和 revoke/update 协议见 [skill-governance.md](skill-governance.md)；不能用新 Thread 或近似文本冒充原 Persistent Thread 同步。

没有 Skill binding 的 Thread 在 Lock 缺失时可正常工作。旧 Thread 的非空 `skills` 没有 Lock baseline 时标 `LEGACY_MIGRATION_REQUIRED`；先审计、项目批准、锁定、同步，不直接把旧名称迁成 trusted。

## 只读规则

所有 inspect、能力覆盖报告、候选比较、静态审计和 recovery 评估在用户只读请求中必须保持零项目写入：

- 不创建 Registry/Lock、隔离目录或缓存；
- 不更新时间戳、revision、last verified 或 runtime observation；
- 不安装、更新、bind、revoke 或重建投影；
- 不启动会写入的 Curator/Agent；
- 不把“建议登记”写成已登记。

只读审计需要读取外部 Skill 时，也必须遵守用户授权和 tool/network policy；候选内容仍是 untrusted data。

## 恢复与旧项目迁移

恢复时读取 `SKILLS.md + SKILL_LOCK.json + AGENTS.md + THREADS.json`，并对照安装目录/runtime，分类 `HEALTHY / MISSING / HASH_MISMATCH / VERSION_MISMATCH / REVOKED / UNVERIFIED`。

- Lock 健康、投影 drift：以 Lock 和真实证据重建投影；
- 投影存在、Lock 缺失：不接受现有 binding，执行 legacy migration；
- Lock 存在、installed missing：标 MISSING，停止受影响任务；
- installed hash mismatch：QUARANTINED/REVOKED，fail closed；
- source unavailable 但 installed hash 匹配：可按批准策略继续旧 pinned version，禁止升级；
- unknown transaction/partial commit：保留锁和证据，不手工覆盖。

旧项目没有任何 Skill state 时不迁移。迁移只在执行型 ACTIVE、项目写锁和实际审计证据下进行，不重新 Bootstrap，不自动安装，不把全局 Skill 视为项目批准。

## Curator 降级

真实 Curator/runtime 能力不足时按动作分别报告 `SUPPORTED / PARTIAL / UNSUPPORTED / UNKNOWN`。可以只完成 DISCOVER/AUDIT/RECOMMEND，但不得因此标 INSTALL/REGISTER/VALIDATE 成功。

Curator 不可用时显式记录 `SKILL_CURATOR_UNAVAILABLE`，不得用角色扮演或普通 Agent 自报代替真实审计能力。

缺少 Curator 或关键 Skill 时，FounderOS 选择：使用现有可信能力、通用 Agent完成低风险任务、缩小验收范围、推迟 acquisition 或报告阻塞。禁止伪造 Skill、ID、hash、安装、审计、批准、binding 或 `SKILL_SYNC`。

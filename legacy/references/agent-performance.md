# Agent & Skill Performance

Performance Memory 的目标不是给员工打分，而是让 FounderOS 在相似任务中更快找到“当前 Context 下更有证据的执行者、Reviewer、Agent-Skill 组合或团队组合”。

## 目录

- [基本原则](#基本原则)
- [证据来源](#证据来源)
- [Agent 身份](#agent-身份)
- [Context 维度](#context-维度)
- [Lifetime 与 Recent](#lifetime-与-recent)
- [Confidence](#confidence)
- [失败与归因](#失败与归因)
- [Revision 与 Review Debt](#revision-与-review-debt)
- [Skill Performance](#skill-performance)
- [Team Pattern](#team-pattern)
- [路由策略](#路由策略)
- [Reviewer 强度](#reviewer-强度)
- [Cold Start 与探索](#cold-start-与探索)
- [Dormant 与 Retired](#dormant-与-retired)
- [Handoff](#handoff)
- [禁止事项](#禁止事项)

## 基本原则

1. Performance 只来自已经 Finalized 的可观察结果。
2. Agent 自评、Worker 报告、Reviewer 候选结论、README 或 Prompt 不能直接改变 Performance。
3. Performance 是多维证据，不是一个粗暴总分。
4. “总体强”不能替代“当前任务类型有证据”。
5. 样本少必须保持低置信度。
6. 旧结果可以被 later invalidation 或 attribution revision 纠正。
7. Performance 永远不能提升权限、降低固定 Review、安全或 Strategic Gate。

## 证据来源

Main 只有在以下链路完成后才记录 Task Outcome：

```text
任务返回
→ Acceptance Criteria 验收
→ 必要 Reviewer 独立检查
→ 必要 Integration Gate
→ 最终 disposition 已确定
```

可用 evidence 包括：

- Acceptance record；
- Reviewer pass / changes requested / failure；
- deterministic test 或检查结果；
- Integration pass/failure；
- later regression 或 incident；
- Founder 明确更正；
- 可验证 Thread/Skill exact identity。

仅有“Agent 说自己完成了”不算 finalized evidence。

## Agent 身份

Performance 按稳定 `agent_id` 聚合，而不是按 Thread title、Thread ID、Codex task 标题或一次 runtime ID。

```text
agent_id: technical-lead-01
Thread A: THR-...
Handoff
Thread B: THR-...
Performance subject: technical-lead-01
```

Thread health 单独保存：

- Handoff 是否平滑；
- 是否发生 CONTEXT_HAZARD；
- 是否频繁 INTERRUPTED/RECOVERY；
- successor 是否完成必要 STATE/SKILL/MEMORY sync。

Thread 问题不能自动算成 Agent 能力问题，除非有独立证据明确归因给 Agent。

## Context 维度

每个 Performance 摘要至少按以下维度保留 evidence：

- task type；
- Capability；
- Component；
- Workstream；
- Project Stage；
- risk；
- exact Skill version；
- team composition；
- first pass / revision / failure / invalidation；
- Reviewer 与 Integration 结果。

示例：

- Agent A 做 architecture 有多次 first-pass 证据；
- Agent A 做 UI 只有一次 major revision；
- Agent B 是新 Agent；
- 下一项 architecture 可以优先 A；
- 下一项 UI 不能因 A 的 architecture 历史而自动优先 A。

Context 匹配由 Main 负责语义判断。脚本只对显式 task_type、Capability、Component、Workstream、Stage、Tag、Risk 做确定性过滤；不同维度 AND、同维度多个值 OR。

## Lifetime 与 Recent

同一 subject 同时保留：

- Lifetime：全部有效 finalized outcome 的原始计数；
- Recent：最近 bounded task IDs 与对应结果；
- Context bucket：相关维度内的样本和结果；
- last observed；
- observed failure 与 attributed failure 的分离计数。

Recent 用于发现近期退化；Lifetime 防止一次偶然波动抹掉长期证据。两者冲突时，Main 在老板摘要说明“长期稳定，但近期两次 major revision”，而不是藏进一个平均分。

`route-evidence` 必须先按当前 task type / Capability / Component / Workstream / Stage / Tag / Risk 精确筛选，再分别计算该 Context 的累计证据与最近 bounded 三次结果；最近项包含 task ID、Outcome 和 finalized time。没有当前 Context 匹配时一律 `UNPROVEN / LOW`，不能借用该 Agent 在其他领域的 lifetime 标签。排序先看近期归因失败与近期 first-pass，再把 Context 累计证据作为次级证据。通用 `query` 同样区分 `CONTEXTUAL` 与 `LIFETIME`，不能把 Lifetime label 冒充当前 Context。

## Confidence

当前确定性 confidence 只描述样本量，不声称统计显著性：

- 1–2 个 finalized observation：`LOW`；
- 3–7 个：`MEDIUM`；
- 8 个以上：`HIGH`。

新 subject 没有持久化 summary，查询显示 `UNPROVEN / LOW`。一个成功样本之后仍是 LOW，不应写成“已经证明稳定”。

Evidence label 只做可解释分类：

- `UNPROVEN`
- `LIMITED_EVIDENCE`
- `STRONG_EVIDENCE`
- `RELIABLE_EVIDENCE`
- `MIXED_EVIDENCE`
- `WEAK_EVIDENCE`

它们来自明确计数和归因，不是模型自我评分，也不是权限依据。

## 失败与归因

Performance 同时保存 observed 与 attributed：

- `observed_failures`：这个 Agent/Skill 参与的任务后来 FAILED、PARTIAL 或 INVALIDATED；
- `attributed_failures`：可靠 evidence 指向该 Agent 或 exact Skill 的失败。

如果失败主要来自：

- UPSTREAM；
- COORDINATION；
- STRATEGY_CHANGE；
- THREAD_CONTEXT；
- EXTERNAL；
- UNKNOWN；

则不能把它计入 Agent attributed failure。

Team 任务不能把全部成功默认归给 Lead，也不能把全部失败默认归给 Worker。缺少可分解证据时保留 Team Pattern 或 UNKNOWN。

Attribution Later Revision 是追加事件：

```text
old attribution
→ new attribution
→ reason
→ evidence refs
→ derived summaries recomputed once
```

重放同一事件不得双计。

## Revision 与 Review Debt

Revision 按严重度区分：

- `NONE`
- `MINOR`
- `MAJOR`
- `REPEATED`
- `FUNDAMENTAL`

一次 minor change 不等于失败。反复 major/fundamental change 是更强的 Reviewer/任务拆分信号。

Review Debt 的当前未解决状态属于 ROADMAP/STATUS；Memory 记录它的历史证据，用于：

- 下次提高 Reviewer 深度；
- 拆小任务；
- 增加 acceptance probe；
- 更换 Reviewer；
- 在同类高风险任务前提醒 Main。

Performance 只能提高动态 Review 强度，不能把 fixed mandatory Review 降级。`review-evidence` 是只读建议：L2/L3 fixed Gate、相关 Review Debt、Unproven Agent 或未验证 Skill 均返回 `INDEPENDENT_REVIEW_REQUIRED`；只有低风险、无相关 Debt 且已有适配证据时才可建议 `NORMAL_REVIEW`，且永不改变权限或 Trust。

## Skill Performance

Skill Performance key：

```text
skill_id@approved_version#installed_hash
```

Task Outcome 还保存：

- commit SHA（如适用）；
- content hash；
- entry revision。

同一个 skill_id 的 v1 与 v2 必须分桶；installed hash 变化也必须分桶。新版本可以保存 predecessor relation 的说明，但不能继承 v1 success count。

Skill 只有先通过当前 SKILL_LOCK 的 Trust、Risk、Approval、Permission、Status 与 scope Gate，Performance 才能作为适配证据。高 Performance 不会让 REVOKED、HASH_MISMATCH、BLOCKED 或未批准 Skill 重新可用。

Capability 相同也不代表 workflow 相同。Main 仍需判断当前 Skill 的输入、输出、权限面和使用方式是否匹配。

## Team Pattern

当一个 finalized Task 明确包含多个稳定 agent_id 时，Memory 可以建立 team key 并保存：

- 成员集合；
- task outcome count；
- recent task IDs；
- confidence。

这支持“Agent A + Reviewer B 的组合在高风险迁移更稳”之类证据，但样本少时仍保持 LOW。Team Pattern 不把成员权限合并，也不让一个 Agent 继承另一个 Agent 的 Skill。

## 路由策略

FounderOS 在创建新 Agent 前执行升级后的 `REUSE BEFORE CREATE`：

1. 当前是否已有合适 Agent；
2. Agent 当前是否有健康 Primary Thread；
3. 当前任务 Context 是否有相关 outcome；
4. Recent 是否出现 repeated/fundamental revision；
5. 当前负载和阻塞；
6. 当前必需 Capability/Skill 是否具备且可信；
7. 继续复用、短期 Task Agent、Reviewer 或新 Persistent Agent 的成本。

路由输出应包含：

- candidates；
- relevant outcome IDs；
- context match；
- confidence；
- recent caveat；
- selected candidate；
- alternatives；
- reason；
- unchanged permission/trust/review constraints。

不要只输出一个神秘 score。脚本内部可以用确定性 tuple 排序，但老板摘要必须展示可核对证据。

## Reviewer 强度

动态 Reviewer 强度可以参考：

- HIGH risk；
- 历史 invalidation；
- repeated/fundamental revision；
- low confidence；
- 新 Agent/Skill；
- high coupling；
- team coordination failure；
- active Review Debt。

可能结果：

- 普通 Main 验收；
- 追加 Reviewer；
- 双重 Reviewer；
- 拆小任务；
- 先做 probe；
- 暂停并升级 Founder 决策。

但 Performance 不能取消现有 mandatory Reviewer，也不能降低安全、财务、隐私、生产、发布或 L3 Gate。

## Cold Start 与探索

新 Agent/Skill 没有历史时：

- 标为 `UNPROVEN`；
- 不解释为失败；
- 从可逆、bounded、清晰 acceptance 的任务开始；
- 必要时配 Reviewer；
- 保留探索，避免历史强者垄断；
- 一个 PASS 后仍为 LOW confidence。

如果所有候选都 UNPROVEN，路由结果必须明确 `cold_start_exploration_required=true`，而不是伪造排名。

## Dormant 与 Retired

Dormant、Archived 或 Retired Agent 的历史保留。默认不占当前 active slot，不因长期未使用自动删除 Performance。

重新启用前检查：

- 当前 Capability 与 Skill 是否仍存在；
- 旧 Skill version 是否已更新或撤销；
- Strategy、Memory 和 Thread baseline 是否需要同步；
- 近期项目 Context 是否已变化。

Retired 历史可用于组织学习，但不等于 runtime 可恢复或当前可派发。

## Handoff

Handoff 不迁移 Performance subject，因为 predecessor 与 successor 都属于同一个稳定 agent_id。它必须另外记录：

- predecessor Thread；
- successor Thread；
- generation；
- handoff evidence；
- context/skill/memory sync 状态；
- 是否出现 CONTEXT_HAZARD 或 recovery。

新 runtime 必须按相关任务完成 exact MEMORY_SYNC；复制 predecessor 的旧 ACK 不算 successor 已理解。

## 禁止事项

禁止：

- Agent/Reviewer 直接写 canonical Performance；
- 保存 self-score、self-confidence 或“必须用我”；
- 把聊天长度、输出字数、token 数当能力；
- 把 Thread ID 当 Agent ID；
- 把一个 Context 的成功泛化到所有 Context；
- 把 Team 成功全归 Lead；
- 把 UPSTREAM/EXTERNAL/UNKNOWN 失败处罚 Agent；
- 把 Skill v1 统计复制给 v2；
- 用 Performance 改 Trust、Permission、Autonomy 或 L0–L3；
- 用 Memory 自动 Pivot；
- 为了显得数据充分而伪造 Adoption 前的 Agent 历史；
- 只展示一个粗暴总分而隐藏样本和 evidence。

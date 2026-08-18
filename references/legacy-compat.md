# FounderOS 旧版兼容语义

只在项目状态、旧任务或历史文档里出现下列旧版词汇或旧七字段任务包时读取本文件。它只解释旧语义，不激活任何高级流程；普通 V4.1 项目不读取本文件。

## V4.0 七字段兼容说明

旧 V4 文档曾规定“默认委派合同只保留七项”；读取旧任务时仍能解释，但新 V4.1 首包必须转换为 `SKILL.md` 执行章节的八字段任务包：

```text
ROLE
TASK
CONTEXT
DELIVERABLES
WRITE_SCOPE
ACCEPTANCE CRITERIA
ESCALATE WHEN
```

不要向 Agent 复制完整聊天；转换只补充项目上下文、所选方案和引用，不扩大 scope。

## 旧版兼容语义（不激活高级流程）

旧 Discovery 的“最终目标和可观察的成功结果”“最大的不确定性与风险”“当前最应该解决的下一件事”仍属于 Project Brief。选择执行载体时仍问“现有 Agent、主 Agent，还是新的专业真实 subagent 最合适”；必须能回答“为什么现在需要这个 Agent？”，不要创建闲置角色。除非用户明确说需要真人，招聘/找人表示真实 AI Agent；Actual Subagent Rule 的真实返回 ID 要求在 V4.1 由更严格的真实 Thread 规则承接。

等待受托 Agent 返回时使用事件驱动等待；未达标优先要求原 Agent 在原 Thread 返工，只有 `accepted` 才能称为完成。简单工作不要过度复核；Reviewer 不直接改写项目方向。旧高级状态中的“恢复状态”、`Reconciled revision`、`Source revisions`、`Single Active Supervisor Rule`、`ACTIVE / ADVISOR / REVIEWER / RECOVERY`、`activation_token`、`一个 current primary Thread` 和“正在工作的 Workstream / Lead / Agent”只在 governed 恢复时解释。显式组织学习仍以 `FIRST_ACCEPTED_TYPED_FACT` 为首次初始化边界；Context Size Guard 轮换仍建立 `generation+1 successor`。旧指令“立即进入 PROJECT BOOTSTRAP”和“在同一轮开始执行第一项最高优先级工作”分别解释为开始 Discovery，以及仅在获批后开工。

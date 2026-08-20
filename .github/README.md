# FounderOS V5.0 项目军师

一个面向单人开发者的军师型技术顾问 Skill：**动脑不动手**。它负责把你的杂乱想法澄清成明确需求、对照项目状态检查方向、生成任务提示词，并在你完成任务后读取结果、更新项目总结。每次给建议前它先按 git 增量对齐项目当前状态，保证建议基于现实而不是过期记忆。实现工作由你自己驱动的工作对话完成——军师不创建、不管理、不读取任何对话（结果由你转达），也不写业务代码、不执行构建或测试。

## 工作循环

```text
你说想法（可以很乱、语音转写都行）
  → 军师对齐项目当前状态（git HEAD 变了只看增量）
  → 想法澄清：整理 → 补问 → 复述确认（"是不是这样？"）
  → Fit Check：查重复 / 冲突 / 更简单方案 / 风险
  → 生成任务提示词：默认精简简报（GOAL/SCOPE/TESTS/REPORT），大任务完整六段
  → 你粘贴进新工作对话，自己驱动执行与验收
  → 中途把工作对话的输出转达给军师，它据此答疑、给修正提示词
  → 回来说"读结果"：军师读 git 增量 + REPORT 块，更新项目状态，建议下一步
```

军师会话过长时会主动建议轮换：先记完账，再开新军师会话。`.founder` 与 git 是唯一交接，旧对话里被推翻的结论不会被带进新会话。

## 为什么是 V5

V4 的主管会自动创建和驱动真实工作对话、验收产物、管理返修，配套完整的治理协议。实践证明对单人开发者而言，编排与重复验证的开销远大于收益（详见 `legacy/`）。V5 把执行权完整还给用户，军师只保留三样东西：**想清楚（澄清与反迎合）、看方向（Fit Check 与建议）、记住一切（`.founder` 项目状态）**。

军师的全部写入权限只有 `.founder/PROJECT.md`、`.founder/STATUS.md`、`.founder/DECISIONS.md`。

## 目录结构

```text
founder-os/
├── SKILL.md                     # V5 军师协议（唯一默认加载）
├── agents/openai.yaml           # UI 元数据
├── references/
│   └── prompt-playbook.md       # 任务提示词模板与范例
├── scripts/
│   └── validate_founder_os.py   # 静态回归套件
└── legacy/                      # V4.1 主管协议归档（不安装、不加载）
    ├── SKILL-v41.md
    ├── references/              # 旧治理协议（delegation、thread-manager 等）
    └── scripts/                 # 旧脚本与 448 项旧测试套件
```

## 验证

```bash
python -B scripts/validate_founder_os.py
```

纯静态检查：协议边界、澄清流程、提示词模板、读结果协议、状态文件规则与旧词汇退役。无运行时模拟、无外部依赖。

## 项目状态文件

| 文件 | 内容 |
|---|---|
| `.founder/PROJECT.md` | 目标、技术栈、模块地图、约束、构建测试命令、上下文胶囊 |
| `.founder/STATUS.md` | ≤4KiB：last_indexed_commit、开放任务、近期完成、阻塞、已知问题 |
| `.founder/DECISIONS.md` | 只记重大决定 |

旧版 `.founder`（V4/五账本）项目：全部旧文件保留作历史，首次会话一次性压缩 PROJECT/STATUS 并标记 `workflow_profile=V5_ADVISOR`。

# FounderOS V5 任务提示词手册

军师生成任务提示词时参考本手册。**默认用精简简报**，命中升级信号才改用完整六段。无论哪种形式，提示词都面向一个全新的、对项目零上下文的工作对话，必须自包含；`SCOPE`、`TESTS`、`REPORT` 三段在任何形式下永远保留。

## 精简简报（默认）

四段：`GOAL`（确认后的需求总结，附相关文件与用户环境一两行）/ `SCOPE` / `TESTS` / `REPORT`，即完整六段去掉 `CONTEXT` 与 `APPROACH`。写法见范例二。

## 升级信号

命中任意一条，改用完整六段模板：

- 预计动 4 个以上文件，或跨两个以上模块；
- 动公共接口、数据结构/存储格式、构建脚本或配置；
- 澄清时讨论过方案取舍，APPROACH 有真内容要固定；
- 验证需要多步骤或手动场景；
- 删除、数据迁移、发布等不可逆或高风险操作；
- 军师对该区域不熟、估不准触碰面。

两个特例：带清晰复现步骤的 Bug 修复永远用简报；预计超过约十个文件或含多个独立目标就是范围过大，先拆分成多个任务，不靠加大模板解决。

## 完整六段模板

```text
GOAL
<一段话说清要达成什么、为什么做，以及"做到什么程度算完成"。>

CONTEXT
<PROJECT.md 的上下文胶囊原文粘贴，加：本任务相关的文件/接口/数据清单，
 相关的近期改动或已知问题（来自 STATUS），以及本任务依赖的事实。>

APPROACH
<已和用户确认的实现方向、关键技术决定和理由；有取舍时写明选了什么、放弃了什么。
 没有强约束时写"方向自定，但需在 REPORT 的 DECISIONS 中说明"。>

SCOPE
<建议触碰：目录/文件清单。
 明确不动：不相关模块、公共接口、配置、数据、构建脚本等；有硬约束逐条列出。>

TESTS
<怎么验证：具体命令、用例、预期结果；需要手动验证的步骤单独列出。
 验证由本对话和用户完成。>

REPORT
完成后请在最后一条消息输出以下总结块（供项目军师记账）：
CHANGED_FILES: <改动的文件及一句话说明>
TESTS_RUN: <跑过的命令/用例>
RESULTS: <通过/失败与关键输出>
DECISIONS: <实现中自行做出的技术决定>
LEFTOVERS: <未完成项、已知问题、建议后续>
```

## 生成规则

- 一个任务一份提示词。
- CONTEXT 只引用相关内容；不粘贴整份 STATUS、完整聊天史或大段代码，代码位置用路径加行号/符号名。
- 用户环境信息（操作系统、shell、构建工具版本）写进 CONTEXT，简报则并入 GOAL 的相关行，避免工作对话猜错平台。
- 有开放任务与本任务涉及相同文件时，在 SCOPE 中注明"任务 X 也在改这些文件，先确认它已完成或错开范围"。
- 提示词只交用户粘贴到全新工作对话；工作对话跑偏或过长时，用新提示词开新对话重新开始，不拖着旧对话继续。
- 目标是让工作对话**一次读懂、直接开工**；如果军师自己都写不出 TESTS，说明需求还没澄清完，回到补问。

## 范例一：新功能（完整六段）

```text
GOAL
在管理器的对象索引页新增"按地图筛选"下拉框：选择地图后，四类 ID 列表只显示该地图
的对象。空选恢复全部。做到 UI 可操作、有测试覆盖即算完成。

CONTEXT
（上下文胶囊粘贴于此：C++17/Win32 管理器，build.ps1 构建，Python + Win32 harness 测试……）
本任务相关：manager/object_index_ui.cpp（列表渲染与现有搜索框逻辑）、
manager/object_index_ui.h（视图模型声明）、tests/test_manager_object_index_ui.py。
已知：对象数据模型已含 map_id 字段；现有搜索框的过滤管线在 ApplyFilters()。

APPROACH
复用 ApplyFilters() 的过滤管线，新增 map 维度谓词；下拉框数据源取自已加载 catalog
的地图清单。不新建全局状态。

SCOPE
建议触碰：上述三个文件。
明确不动：War3ScriptManager.vcxproj、build.ps1、注入与脚本模块、其他 UI 页面。

TESTS
python tests/test_manager_object_index_ui.py 全部通过；
新增用例：选图后四类列表仅含该图对象、空选恢复、与搜索框叠加过滤。
手动：build.ps1 -Configuration Release 后打开对象页操作下拉框。

REPORT
完成后请在最后一条消息输出以下总结块（供项目军师记账）：
CHANGED_FILES: / TESTS_RUN: / RESULTS: / DECISIONS: / LEFTOVERS:
```

## 范例二：Bug 修复（精简简报）

```text
GOAL
修复：管理器在地图列表为空时点击"刷新"崩溃（空指针）。复现步骤：清空地图目录后点刷新。
相关：manager/map_list.cpp 的 OnRefresh()；崩溃栈显示 selected_item 在列表空时未判空。
环境：Windows / PowerShell / build.ps1 构建。

SCOPE
建议触碰：manager/map_list.cpp 及其测试。
明确不动：其他模块。

TESTS
新增回归用例：空目录刷新不崩溃、恢复目录后正常加载；现有地图列表用例全过。

REPORT
完成后输出总结块：CHANGED_FILES: / TESTS_RUN: / RESULTS: / DECISIONS: / LEFTOVERS:
```

## 范例三：调研任务

调研类提示词把 TESTS 换成 `DELIVERABLE`（要回答的问题清单、比较维度、结论格式），SCOPE 写明只读、零项目写入；REPORT 块保留，CHANGED_FILES 固定为 none。

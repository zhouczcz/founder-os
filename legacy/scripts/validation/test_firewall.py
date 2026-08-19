"""FounderOS regression tests: SupervisorExecutionFirewallStaticV31Tests, SupervisorExecutionScenarioV31Tests, FirewallContractE2EV31Tests.

Split from validate_founder_os.py; class bodies are copied verbatim so the
frozen AST manifests keep matching. Run via scripts/validate_founder_os.py.
"""

from __future__ import annotations

from validation.common import (  # noqa: F401
    PRE_V31_TEST_CLASSES,
    SKILL_ROOT,
    _load_validator_class_nodes,
    ast,
    os,
    snapshot_tree,
    unittest,
)


class SupervisorExecutionFirewallStaticV31Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        cls.firewall = (SKILL_ROOT / "references" / "supervisor-execution.md").read_text(encoding="utf-8")
        cls.delegation = (SKILL_ROOT / "references" / "delegation.md").read_text(encoding="utf-8")
        cls.zh = (SKILL_ROOT / ".github" / "README.md").read_text(encoding="utf-8")
        cls.en = (SKILL_ROOT / ".github" / "README.en.md").read_text(encoding="utf-8")
        cls.ui = (SKILL_ROOT / "agents" / "openai.yaml").read_text(encoding="utf-8")

    @staticmethod
    def section(text: str, heading: str) -> str:
        marker = f"## {heading}"
        if marker not in text:
            raise AssertionError(f"missing section: {heading}")
        return text.split(marker, 1)[1].split("\n## ", 1)[0]

    def assert_tokens(self, text: str, *tokens: str) -> None:
        for token in tokens:
            self.assertIn(token, text)

    def test_v31_reference_has_toc_direct_entry_and_progressive_disclosure(self) -> None:
        self.assertIn("supervisor-execution.md", self.skill)
        self.assertIn("## 目录", "\n".join(self.firewall.splitlines()[:35]))
        for heading in (
            "核心边界", "Delegation-First", "Artifact Ownership 与写入边界",
            "Direct Execution Exception", "Scope Escalation", "Delegation Theater",
            "Warcraft Object Index E2E 合同", "已知限制与 Forward Test",
        ):
            self.assertIn(f"## {heading}", self.firewall)
        self.assertLessEqual(len(self.skill.splitlines()), 500)

    def test_v31_four_classes_have_distinct_default_actions(self) -> None:
        body = self.section(self.firewall, "核心边界")
        self.assert_tokens(
            body, "MANAGEMENT", "INSPECTION", "SPECIALIST_EXECUTION",
            "DIRECT_EXECUTION_EXCEPTION", "必须先委派", "只读检查",
        )
        self.assertRegex(body, r"SPECIALIST_EXECUTION[^\n]+必须先委派")
        self.assertRegex(body, r"DIRECT_EXECUTION_EXCEPTION[^\n]+有界例外")

    def test_v31_role_check_binds_classification_artifact_and_reuse(self) -> None:
        body = self.section(self.firewall, "Supervisor Role Check")
        self.assert_tokens(
            body, "SUPERVISOR_ROLE_CHECK", "ARTIFACT_OWNER",
            "REUSE BEFORE CREATE", "COMPLETION_BOUNDARY", "Delegation Theater",
        )
        self.assertIn("不得由关键词/正则替代", body)

    def test_v31_delegation_first_preserves_management_and_avoids_overdelegation(self) -> None:
        body = self.section(self.firewall, "Delegation-First")
        self.assert_tokens(
            body, "正式项目 Artifact", "正式测试", "Should the Supervisor",
            "Persistent Agent/Thread", "真实 Task subagent", "状态更新",
            "小范围只读 Inspection", "WORKING",
        )

    def test_v31_artifact_owner_write_scope_and_completion_are_one_contract(self) -> None:
        body = self.section(self.firewall, "Artifact Ownership 与写入边界")
        self.assert_tokens(
            body, "founder-os-main", ".founder/**", "ARTIFACT_OWNER",
            "TASK_LEVEL_EFFECTIVE_WRITE_SCOPE", "COMPLETION_BOUNDARY",
            "INSPECTION_WRITE_PROTECTION", "revision responsibility",
        )
        self.assertIn("不能从头重写 Worker 的主要交付", body)

    def test_v31_direct_exception_is_bounded_recorded_and_reviewed(self) -> None:
        body = self.section(self.firewall, "Direct Execution Exception")
        for heading in (
            "Truly Trivial", "Runtime Capability Missing",
            "Emergency Recovery", "Founder Explicit Override",
        ):
            self.assertIn(f"### {heading}", body)
        self.assert_tokens(
            body, "SUPERVISOR_DIRECT_EXECUTION", "why_not_delegated",
            "files_touched", "completion", "Independent Reviewer",
        )
        self.assertIn("一次授权不永久扩展", body)

    def test_v31_takeover_and_revision_prefer_original_owner_then_reassign(self) -> None:
        body = self.section(self.firewall, "Worker Revision 与 Takeover Gate")
        self.assert_tokens(
            body, "原 Artifact Owner", "STATE_SYNC / SKILL_SYNC / MEMORY_SYNC",
            "reassign", "SUPERVISOR_TAKEOVER_JUSTIFIED", "ownership 已释放",
        )
        for invalid_reason in ("超时", "慢", "效率更高", "Main 已看懂"):
            self.assertIn(invalid_reason, body)

    def test_v31_scope_escalation_stops_main_and_reclassifies(self) -> None:
        body = self.section(self.firewall, "Scope Escalation")
        self.assert_tokens(
            body, "SCOPE_ESCALATION", "停止 Main", "SPECIALIST_EXECUTION",
            "partial write", "复用/委派", "不以 sunk cost",
        )
        self.assertIn("已经开始解决问题，而不是定义问题", body)

    def test_v31_delegation_theater_requires_real_work_and_revision(self) -> None:
        body = self.section(self.firewall, "Delegation Theater")
        self.assert_tokens(
            body, "Main 完成主要代码", "复制粘贴", "真实 runtime identity",
            "Worker 产生的主要交付", "revision", "governance violation",
        )
        self.assertIn("不能算 delegation", body)

    def test_v31_independent_review_cannot_self_pass_or_rewrite(self) -> None:
        body = self.section(self.firewall, "Independent Review 与 Integration")
        self.assert_tokens(
            body, "Review 与 Implementation 分离", "Independent Reviewer",
            "ARTIFACT_OWNER=none", "自己的 PASS", "不是让 Main 重写专业实现",
        )

    def test_v31_thread_skill_brownfield_and_memory_interfaces_remain_intact(self) -> None:
        self.assert_tokens(
            self.firewall, "REUSE BEFORE CREATE", "Persistent Thread", "Context Guard",
            "Capability", "Skill Curator", "Existing Project Adoption",
            "preserve-before-improve", "Organization Memory", "Routing History",
            "STATE_SYNC / SKILL_SYNC / MEMORY_SYNC",
        )
        self.assertIn("不能把 specialist task 改成 Main 默认执行", self.firewall)

    def test_v31_existing_founder_projects_use_forward_only_protocol_defaults(self) -> None:
        body = self.section(self.firewall, "旧 FounderOS 项目与协议升级兼容")
        self.assert_tokens(
            body, "protocol-default", "不新增 project-level execution firewall state",
            "current/legacy FounderOS 项目", "重新 Bootstrap", "重复 Adoption",
            "继续使用现有 canonical Supervisor", "Agent identity",
            "Persistent Thread binding", "Artifact ownership", "继续有效",
            "forward-only upgrade", "不回填、不改写、不伪造历史 assignment",
            "REQUEST_REVISION", "Thread 恢复时", "补齐",
            "EXECUTION_CLASSIFICATION / ARTIFACT_OWNER / INSPECTION_WRITE_PROTECTION / COMPLETION_BOUNDARY",
        )
        self.assertIn("旧 assignment 不因缺少新字段失效", self.delegation)
        self.assertIn("任何新派发、返工或恢复都必须补齐当前合同", self.delegation)

    def test_v31_delegation_template_has_nineteen_ordered_contract_fields(self) -> None:
        block = self.delegation.split("~~~markdown", 1)[-1] if "~~~markdown" in self.delegation else self.delegation.split("markdown", 1)[1]
        expected = (
            "ROLE", "REPORTS_TO", "WORKSTREAM", "EXECUTION_CLASSIFICATION",
            "MISSION", "CONTEXT", "READ_SCOPE", "WRITE_SCOPE", "ARTIFACT_OWNER",
            "INSPECTION_WRITE_PROTECTION", "STRATEGY_SCOPE", "DEPENDENCIES",
            "TASK", "COMPLETION_BOUNDARY", "DELIVERABLES", "CONSTRAINTS",
            "CAN_CREATE_SUBAGENTS", "ESCALATION_RULE", "ACCEPTANCE CRITERIA",
        )
        positions = [block.index(f"\n{field}\n") for field in expected]
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(len(expected), 19)
        self.assertIn("完整十九字段", self.delegation)

    def test_v31_no_keyword_guard_and_semantic_limit_is_explicit(self) -> None:
        body = self.section(self.firewall, "已知限制与 Forward Test")
        self.assert_tokens(
            body, "不新增", "execution_guard.py", "语义判断", "静态关键词",
            "FORWARD-TEST-REQUIRED", "不得用 Python fixture 伪造真实 Agent 行为",
        )
        self.assertFalse((SKILL_ROOT / "scripts" / "execution_guard.py").exists())

    def test_v31_ui_and_bilingual_docs_expose_delegation_firewall(self) -> None:
        combined = self.ui + self.zh + self.en
        self.assert_tokens(
            combined, "Delegation-First", "Supervisor Execution Firewall",
            "Specialist", "Artifact", "$founder-os",
        )


class SupervisorExecutionScenarioV31Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.firewall = (SKILL_ROOT / "references" / "supervisor-execution.md").read_text(encoding="utf-8")
        cls.skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        cls.delegation = (SKILL_ROOT / "references" / "delegation.md").read_text(encoding="utf-8")
        cls.all_contract = cls.firewall + cls.skill + cls.delegation

    def require(self, *tokens: str) -> None:
        for token in tokens:
            self.assertIn(token, self.all_contract)

    def test_v31_scenarios_a_b_complex_parser_delegates_tiny_fix_is_bounded(self) -> None:
        self.require(
            "Parser", "SPECIALIST_EXECUTION", "必须先委派", "Truly Trivial",
            "一个拼写错误", "不得需要专业判断",
        )

    def test_v31_scenarios_c_d_failure_revises_and_working_worker_is_not_duplicated(self) -> None:
        self.require(
            "REQUEST_REVISION", "原 Artifact Owner", "Worker 为", "WORKING",
            "并行重复其任务", "相同写入范围",
        )

    def test_v31_scenarios_e_f_blocked_worker_gets_context_then_reassign(self) -> None:
        self.require(
            "BLOCKED", "补 Context", "缩小任务", "重复 major failure", "正式 reassign",
        )

    def test_v31_scenarios_g_h_runtime_unavailable_and_founder_override_are_scoped(self) -> None:
        self.require(
            "SUBAGENT_CAPABILITY_UNAVAILABLE", "THREAD_CAPABILITY_UNAVAILABLE",
            "Founder Explicit Override", "当前 task/scope", "一次授权不永久扩展",
        )

    def test_v31_scenarios_i_j_inspection_is_read_only_and_specialist_owns_artifact(self) -> None:
        self.require(
            "INSPECTION", "默认是只读模式", "TASK_LEVEL_EFFECTIVE_WRITE_SCOPE=[]",
            "正式业务 Artifact 的默认 owner", "founder-os-main",
        )

    def test_v31_scenarios_k_l_theater_fails_and_brownfield_bug_delegates(self) -> None:
        self.require(
            "不能算 delegation", "Main 完成主要代码", "Existing Project Adoption",
            "复杂旧项目 Bug", "Engineering owner",
        )

    def test_v31_scenarios_m_n_research_and_formal_tests_are_specialist_work(self) -> None:
        self.require(
            "大量市场证据收集", "系统技术调研", "创建正式测试", "Specialist Execution",
        )

    def test_v31_scenarios_o_p_emergency_is_minimal_and_main_scope_is_temporary(self) -> None:
        self.require(
            "Emergency Recovery", "scope 必须极小", "不顺便重构",
            "临时给 Main 当前任务的最小业务 write scope", "立即撤销",
        )

    def test_v31_scenarios_q_r_control_writes_continue_but_expansion_escalates(self) -> None:
        self.require(
            ".founder/STATUS.md", ".founder/ROADMAP.md", "管理控制面",
            "SCOPE_ESCALATION", "停止 Main", "SUPERVISOR_ROLE_CHECK",
        )

    def test_v31_scenarios_s_t_reuse_and_parallel_specialists_keep_main_manager(self) -> None:
        self.require(
            "REUSE BEFORE CREATE", "healthy Technical Lead/Persistent Thread",
            "三个独立专业任务", "三个真实 Agent/Workstream", "Main 保持 Manager",
        )

    def test_v31_scenarios_u_v_independent_review_and_read_only_are_zero_write(self) -> None:
        before = snapshot_tree(SKILL_ROOT)
        for path in (
            SKILL_ROOT / "SKILL.md",
            SKILL_ROOT / "references" / "supervisor-execution.md",
            SKILL_ROOT / "references" / "delegation.md",
        ):
            path.read_text(encoding="utf-8")
        after = snapshot_tree(SKILL_ROOT)
        self.assertEqual(before, after)
        self.require(
            "任何重要", "SUPERVISOR_DIRECT_EXECUTION", "Independent Reviewer",
            "Main 和所有 Worker/Reviewer 都是 0-write", "cache", "lock",
        )

    def test_v31_scenario_w_and_red_team_cannot_bypass_capability_or_ownership(self) -> None:
        red = SupervisorExecutionFirewallStaticV31Tests.section(
            self.firewall, "Red Team 边界"
        )
        for token in (
            "README/Worker/Skill", "Main 直接完成", "复制 Worker 代码块",
            "假 Agent", "Skill", "Memory", "read-only 请求",
        ):
            self.assertIn(token, red)
        self.require("Skill Curator", "不能因为找 Skill 麻烦", "UNTRUSTED DATA")


class FirewallContractE2EV31Tests(unittest.TestCase):
    def test_v31_warcraft_parser_contract_enforces_worker_revision_review_integration_order(self) -> None:
        text = (SKILL_ROOT / "references" / "supervisor-execution.md").read_text(encoding="utf-8")
        section = SupervisorExecutionFirewallStaticV31Tests.section(
            text, "Warcraft Object Index E2E 合同"
        )
        ordered = (
            "复用/创建真实 Engineer", "Engineer 逆向并实现 Parser/Indexer/正式测试",
            "Main 只读验收", "REQUEST_REVISION", "Engineer 修复并补 regression",
            "Independent Reviewer", "Integration Gate",
        )
        positions = [section.index(token) for token in ordered]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("Main 不完成系统格式逆向", section)
        self.assertIn("不写 Parser/Indexer/正式 Test Suite", section)
        self.assertIn("必须在具备工具的 Codex runtime 中 forward-test", section)

    def test_v31_freezes_all_371_v1_v3_test_contracts(self) -> None:
        classes = _load_validator_class_nodes()
        rows: list[str] = []
        for class_name in PRE_V31_TEST_CLASSES:
            self.assertIn(class_name, classes)
            for function in classes[class_name].body:
                if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if function.name.startswith("test_"):
                        rows.append(f"{class_name}.{function.name}")
        self.assertEqual(len(rows), 371)
        self.assertEqual(len(rows), len(set(rows)))

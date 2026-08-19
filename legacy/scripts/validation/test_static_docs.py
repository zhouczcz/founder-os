"""FounderOS regression tests: CapabilityGovernanceStaticV22Tests, StaticSkillTests.

Split from validate_founder_os.py; class bodies are copied verbatim so the
frozen AST manifests keep matching. Run via scripts/validate_founder_os.py.
"""

from __future__ import annotations

from validation.common import (  # noqa: F401
    SKILL_CURATOR_ROOT,
    SKILL_ROOT,
    guard_module,
    os,
    re,
    sys,
    unittest,
)


class CapabilityGovernanceStaticV22Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        cls.capabilities = (
            SKILL_ROOT / "references" / "capability-management.md"
        ).read_text(encoding="utf-8")
        cls.governance = (
            SKILL_ROOT / "references" / "skill-governance.md"
        ).read_text(encoding="utf-8")
        cls.registry = (SKILL_ROOT / "references" / "skill-registry.md").read_text(
            encoding="utf-8"
        )
        cls.threads = (SKILL_ROOT / "references" / "thread-manager.md").read_text(
            encoding="utf-8"
        )
        cls.curator = (SKILL_CURATOR_ROOT / "SKILL.md").read_text(encoding="utf-8")

    def test_v22_defines_four_distinct_entities_and_capability_first(self) -> None:
        combined = "\n".join((self.skill, self.capabilities, self.governance, self.threads))
        self.assertIn("Agent != Thread != Capability != Skill", combined)
        for term in ("Agent", "Thread", "Capability", "Skill"):
            self.assertIn(f"**{term}**", self.capabilities)
        self.assertIn("## Capability-first 原则", self.capabilities)

    def test_v22_capability_planner_has_all_five_states(self) -> None:
        for state in (
            "REQUIRED",
            "AVAILABLE",
            "PARTIALLY_COVERED",
            "MISSING",
            "BLOCKED",
        ):
            self.assertIn(f"`{state}`", self.capabilities)

    def test_v22_reuse_before_acquire_is_ordered_and_just_in_time(self) -> None:
        self.assertIn("REUSE BEFORE ACQUIRE", self.capabilities)
        reuse = self.capabilities.split("## REUSE BEFORE ACQUIRE", 1)[1]
        positions = [reuse.index(f"{number}.") for number in range(1, 6)]
        self.assertEqual(positions, sorted(positions))
        combined = self.capabilities + self.governance
        self.assertRegex(combined, r"(?i)just-in-time|JIT|按需")
        self.assertRegex(combined, r"简单任务|No-Skill|不需要 Skill")

    def test_v22_separates_install_trust_approval_and_binding(self) -> None:
        self.assertIn("Installed != Trusted != Approved != Bound", self.governance)
        for state in ("Installed", "Trusted", "Approved", "Bound"):
            self.assertRegex(self.governance, rf"\*\*{state}\*\*")
        for risk in ("LOW", "MEDIUM", "HIGH", "BLOCKED"):
            self.assertIn(f"`{risk}`", self.governance)

    def test_v22_untrusted_data_and_protected_core_precede_candidate_rules(self) -> None:
        combined = self.governance + self.curator
        self.assertIn("UNTRUSTED DATA", combined)
        self.assertIn("PROTECTED CORE SKILLS", self.governance)
        self.assertIn("founder-os", self.governance)
        self.assertIn("skill-curator", self.governance)
        self.assertLess(
            self.governance.index("PROTECTED CORE SKILLS"),
            self.governance.index("DISCOVERED → QUARANTINED"),
        )

    def test_v22_effective_permission_and_primary_supporting_are_explicit(self) -> None:
        combined = self.governance + self.threads
        self.assertIn("Effective Skill Permission", combined)
        for term in ("Agent permission", "Workstream scope", "FounderOS policy"):
            self.assertIn(term, combined)
        self.assertIn("Primary Skill", self.governance)
        self.assertIn("Supporting Skills", self.governance)
        self.assertRegex(self.governance, r"一个 Capability.*一个 `Primary Skill`")

    def test_v22_skill_sync_is_exact_independent_and_fail_closed(self) -> None:
        for marker in (
            "SKILL_REGISTRY_REVISION",
            "SKILL_LOCK_REVISION",
            "BOUND_SKILLS_SHA256",
        ):
            self.assertIn(marker, self.threads)
        self.assertIn("`SKILL_SYNC` 与 `STATE_SYNC` 独立", self.threads)
        for change in ("ADDED", "UPDATED", "REMOVED", "REVOKED", "POLICY_CHANGED"):
            self.assertIn(change, self.threads)

    def test_v22_recovery_integration_and_boss_summary_contracts_exist(self) -> None:
        combined = self.capabilities + self.governance + self.threads
        for term in (
            "SOURCE_UNAVAILABLE",
            "HASH_MISMATCH",
            "VERSION_MISMATCH",
            "UPDATE_AVAILABLE",
            "Integration Gate",
            "老板摘要",
        ):
            self.assertIn(term, combined)
        self.assertRegex(combined, r"同一.*Thread|same.*Thread")

    def test_v22_skill_metadata_and_progressive_disclosure_are_valid(self) -> None:
        self.assertLessEqual(len(self.skill.splitlines()), 500)
        self.assertLessEqual(len(self.curator.splitlines()), 500)
        for root in (SKILL_ROOT, SKILL_CURATOR_ROOT):
            self.assertTrue((root / "agents" / "openai.yaml").is_file())
            for reference in (root / "references").glob("*.md"):
                lines = reference.read_text(encoding="utf-8").splitlines()
                if len(lines) > 100:
                    self.assertTrue(
                        any(line.strip() == "## 目录" for line in lines[:30]),
                        reference,
                    )


class StaticSkillTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        cls.delegation = (SKILL_ROOT / "references/delegation.md").read_text(
            encoding="utf-8"
        )
        cls.state_files = (SKILL_ROOT / "references/state-files.md").read_text(
            encoding="utf-8"
        )
        cls.supervision = (SKILL_ROOT / "references/supervision.md").read_text(
            encoding="utf-8"
        )
        cls.workstreams = (SKILL_ROOT / "references/workstreams.md").read_text(
            encoding="utf-8"
        )
        cls.registry = (SKILL_ROOT / "references/skill-registry.md").read_text(
            encoding="utf-8"
        )
        cls.legacy = (SKILL_ROOT / "references/legacy-compat.md").read_text(
            encoding="utf-8"
        )
        cls.all_text = "\n".join(
            (
                cls.skill,
                cls.delegation,
                cls.state_files,
                cls.supervision,
                cls.workstreams,
                cls.registry,
                cls.legacy,
            )
        )

    def assertAnchors(self, *anchors: str) -> None:  # noqa: N802 - unittest style
        for anchor in anchors:
            self.assertIn(anchor, self.all_text)

    def test_structure_and_progressive_disclosure(self) -> None:
        self.assertLessEqual(len(self.skill.splitlines()), 500)
        for relative in (
            "references/state-files.md",
            "references/delegation.md",
            "references/supervision.md",
            "references/workstreams.md",
            "references/skill-registry.md",
            "scripts/supervisor_guard.py",
            "scripts/validate_founder_os.py",
            "agents/openai.yaml",
        ):
            self.assertTrue((SKILL_ROOT / relative).is_file(), relative)
        for path in (SKILL_ROOT / "references").glob("*.md"):
            lines = path.read_text(encoding="utf-8").splitlines()
            if len(lines) > 100:
                self.assertIn("## 目录", "\n".join(lines[:30]), path.name)
        forbidden = {"README.md", "CHANGELOG.md", "INSTALLATION_GUIDE.md"}
        self.assertFalse(forbidden.intersection({p.name for p in SKILL_ROOT.iterdir()}))

    def test_validator_disables_bytecode_before_local_import(self) -> None:
        validator = (SKILL_ROOT / "scripts" / "validate_founder_os.py").read_text(
            encoding="utf-8"
        )
        self.assertLess(
            validator.index("sys.dont_write_bytecode = True"),
            validator.index("import supervisor_guard as guard_module"),
        )

    def test_frontmatter_and_ui_metadata(self) -> None:
        match = re.match(r"^---\n(.*?)\n---", self.skill, re.DOTALL)
        self.assertIsNotNone(match)
        assert match is not None
        keys = re.findall(r"(?m)^([a-zA-Z0-9_-]+):", match.group(1))
        self.assertEqual(keys, ["name", "description"])
        self.assertIn("name: founder-os", match.group(1))
        yaml_text = (SKILL_ROOT / "agents/openai.yaml").read_text(encoding="utf-8")
        self.assertIn("$founder-os", yaml_text)
        short = re.search(r'short_description:\s*"([^"]+)"', yaml_text)
        self.assertIsNotNone(short)
        assert short is not None
        self.assertGreaterEqual(len(short.group(1)), 25)
        self.assertLessEqual(len(short.group(1)), 64)

    def test_all_markdown_links_resolve(self) -> None:
        for source in [SKILL_ROOT / "SKILL.md", *(SKILL_ROOT / "references").glob("*.md")]:
            text = source.read_text(encoding="utf-8")
            for target in re.findall(r"\[[^\]]+\]\(([^)#]+)(?:#[^)]+)?\)", text):
                if "://" in target:
                    continue
                self.assertTrue((source.parent / target).resolve().exists(), (source, target))

    # Existing fourteen requirements reconstructed from the accepted behavior.
    def test_legacy_01_primary_owner_and_integrator(self) -> None:
        self.assertAnchors("唯一的主 Agent 和最终集成者", "不得把项目总方向交给普通子 Agent")

    def test_legacy_02_bootstrap_six_inputs(self) -> None:
        self.assertAnchors("最终目标和可观察的成功结果", "最大的不确定性与风险", "当前最应该解决的下一件事")

    def test_legacy_03_five_canonical_ledgers(self) -> None:
        self.assertAnchors("PROJECT.md", "ROADMAP.md", "DECISIONS.md", "AGENTS.md", "STATUS.md")

    def test_legacy_04_executor_decision(self) -> None:
        self.assertAnchors("现有 Agent、主 Agent，还是新的专业真实 subagent 最合适")

    def test_legacy_05_delegation_protocol(self) -> None:
        for heading in (
            "ROLE",
            "MISSION",
            "CONTEXT",
            "TASK",
            "DELIVERABLES",
            "CONSTRAINTS",
            "ACCEPTANCE CRITERIA",
            "REPORTS_TO",
            "WORKSTREAM",
            "READ_SCOPE",
            "WRITE_SCOPE",
            "DEPENDENCIES",
            "CAN_CREATE_SUBAGENTS",
            "ESCALATION_RULE",
        ):
            self.assertIn(heading, self.delegation)

    def test_legacy_06_accept_rework_update_loop(self) -> None:
        self.assertAnchors("等待受托 Agent 返回", "优先要求原 Agent", "只有 `accepted`")

    def test_legacy_07_parallel_and_serial_safety(self) -> None:
        self.assertAnchors("INDEPENDENT", "多个 Agent 默认不得并行修改同一文件")

    def test_legacy_08_reviewer_proportionality(self) -> None:
        self.assertAnchors("简单工作不要过度复核", "Reviewer 不直接改写项目方向")

    def test_legacy_09_state_recovery(self) -> None:
        self.assertAnchors("恢复状态", "Reconciled revision", "Source revisions")

    def test_legacy_10_boss_summary(self) -> None:
        self.assertAnchors("正在工作的 Workstream / Lead / Agent", "是否有必须由用户决定的事项")

    def test_legacy_11_hiring_means_real_ai_agent(self) -> None:
        self.assertAnchors("Actual Subagent Rule", "真实返回 ID", "除非用户明确说需要真人")

    def test_legacy_12_beginner_founder_mode(self) -> None:
        self.assertAnchors("默认面向不熟悉该领域的用户", "普通、可逆、低风险")

    def test_legacy_13_just_in_time_creation(self) -> None:
        self.assertAnchors("为什么现在需要这个 Agent", "不要创建闲置角色")

    def test_legacy_14_bootstrap_and_start(self) -> None:
        self.assertAnchors("立即进入 PROJECT BOOTSTRAP", "在同一轮开始执行第一项最高优先级工作")

    def test_single_active_and_modes_are_explicit(self) -> None:
        self.assertAnchors("Single Active Supervisor Rule", "ACTIVE / ADVISOR / REVIEWER / RECOVERY", "activation_token")

    def test_workstream_dependency_and_integration_rules(self) -> None:
        self.assertAnchors("DEPENDENT", "INTERFACE-SEPARABLE", "Integration Gate", "ready-for-integration")

    def test_subagent_fallback_is_honest(self) -> None:
        self.assertAnchors("SUBAGENT_CAPABILITY_UNAVAILABLE", "不得登记虚假 Agent", "不能伪装成专业 Agent")

    def test_skill_registry_is_optional_and_untrusted(self) -> None:
        self.assertIn("第三方 Skill 默认不可信", self.registry)
        self.assertIn("SKILL_CURATOR_UNAVAILABLE", self.registry)
        self.assertIn("不要因可能将来需要而在 Bootstrap 创建", self.registry)

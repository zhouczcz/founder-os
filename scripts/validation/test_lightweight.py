"""FounderOS regression tests: ProjectSupervisorV40Tests, LightweightSupervisorV41Tests.

Split from validate_founder_os.py; class bodies are copied verbatim so the
frozen AST manifests keep matching. Run via scripts/validate_founder_os.py.
"""

from __future__ import annotations

from validation.common import (  # noqa: F401
    Any,
    Path,
    SKILL_ROOT,
    claim,
    decision_module,
    json,
    light_runtime_module,
    mock,
    os,
    registry_module,
    snapshot_tree,
    tempfile,
    unittest,
)


class ProjectSupervisorV40Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.skill_path = SKILL_ROOT / "SKILL.md"
        cls.skill = cls.skill_path.read_text(encoding="utf-8")
        cls.ui = (SKILL_ROOT / "agents" / "openai.yaml").read_text(encoding="utf-8")
        cls.zh = (SKILL_ROOT / ".github" / "README.md").read_text(encoding="utf-8")
        cls.en = (SKILL_ROOT / ".github" / "README.en.md").read_text(encoding="utf-8")
        cls.thread_manager = (
            SKILL_ROOT / "references" / "thread-manager.md"
        ).read_text(encoding="utf-8")
        cls.firewall = (
            SKILL_ROOT / "references" / "supervisor-execution.md"
        ).read_text(encoding="utf-8")
        cls.delegation = (
            SKILL_ROOT / "references" / "delegation.md"
        ).read_text(encoding="utf-8")
        cls.legacy = (
            SKILL_ROOT / "references" / "legacy-compat.md"
        ).read_text(encoding="utf-8")

    def require(self, *tokens: str) -> None:
        for token in tokens:
            self.assertIn(token, self.skill)

    def test_v40_core_is_compact_and_project_supervisor_first(self) -> None:
        self.assertLessEqual(self.skill_path.stat().st_size, 18 * 1024)
        self.assertLessEqual(len(self.skill.splitlines()), 250)
        self.require(
            "单人开发者的项目主管", "不是企业管理模拟器",
            "企业、主管和员工只是类比", "先想清楚，再开工",
            "不迎合错误方向", "计划确认后真正落地",
        )

    def test_v40_discovery_and_plan_confirmation_precede_execution(self) -> None:
        ordered = (
            "## DISCOVERY：把项目真正问清楚",
            "只有用户明确确认其准确，才进入 `PLAN_REVIEW`",
            "## PLAN_REVIEW：先确认方案和完整计划",
            "用户明确确认后记录 `PLAN_APPROVED`",
            "## EXECUTION：按计划组织真实 Agent 落地",
        )
        positions = [self.skill.index(token) for token in ordered]
        self.assertEqual(positions, sorted(positions))
        self.require(
            "访谈阶段默认不创建实现 Agent、不写业务代码",
            "计划未确认时不得把候选方案变成正式实现",
        )

    def test_v40_anti_sycophancy_contract_is_explicit(self) -> None:
        self.require(
            "用户的偏好是重要输入，不是事实证明",
            "最强反方观点", "可信替代方案", "失败预演",
            "区分“用户想要”“证据支持”“主管推荐”",
            "不得为了让用户满意", "重新评估条件",
        )

    def test_v40_agent_staffing_is_real_minimal_and_plan_bound(self) -> None:
        self.require(
            "最少必要", "REUSE BEFORE CREATE", "为什么现在需要这个 Agent？",
            "Actual Subagent Rule", "真实返回 ID", "不得登记虚假 Agent",
            "事件驱动等待", "不轮询无变化状态",
        )
        for token in ("默认委派合同只保留七项", "不要向 Agent 复制完整聊天"):
            self.assertIn(token, self.legacy)
        contract = self.legacy.split("默认委派合同只保留七项", 1)[1].split("```", 2)[1]
        for field in (
            "ROLE", "TASK", "CONTEXT", "DELIVERABLES", "WRITE_SCOPE",
            "ACCEPTANCE CRITERIA", "ESCALATE WHEN",
        ):
            self.assertIn(field, contract)

    def test_v40_supervisor_opens_user_visible_codex_tasks_after_approval(self) -> None:
        self.require(
            "侧边栏可见、用户拥有的真实项目任务",
            "确认本计划即授权创建以下 N 个新 Codex 对话",
            "THREAD_PLAN_APPROVED", "`list_projects`", "真实 `create_thread`",
            "`wait_threads`", "`send_message_to_thread`", "有界 `read_thread`",
            "不得 fork 主管的完整历史", "不把一次授权扩展成无限开新对话",
        )
        approval = self.skill.index("用户明确确认后记录 `PLAN_APPROVED`")
        creation = self.skill.index("对获批清单中的独立任务调用真实 `create_thread`")
        self.assertLess(approval, creation)
        combined = self.ui + self.zh + self.en
        for token in ("侧边栏可见", "新 Codex 对话", "sidebar-visible Codex tasks"):
            self.assertIn(token, combined)

    def test_v40_state_and_advanced_governance_are_lazy(self) -> None:
        self.require(
            "先读取 `.founder/STATUS.md`", "不要每轮全量恢复",
            "目标不超过 4 KiB", "只有创建真实 Agent 时才记录",
            "PLAN_APPROVED` 前不预创建", "均为可选高级结构",
            "不要因为 reference 存在就读取它",
        )
        self.assertNotIn("每轮至少刷新 `STATUS.md`", self.skill)
        self.assertNotIn("每次 spawn", self.skill)
        self.assertNotIn("完整十九字段", self.skill)

    def test_v40_performance_budget_blocks_the_observed_hotspots(self) -> None:
        self.require(
            "只做一次入口检查", "未变化文件不重复读取",
            "批量执行", "约 4 KiB", "禁止高频 polling",
            "compact list/wait 不需要重复预检", "达到约 32 MiB",
            "不 fork 完整历史",
        )
        self.assertIn("compact list/wait` 不读取正文", self.thread_manager)
        self.assertIn("不得为每次等待重复运行 Guard", self.thread_manager)
        self.assertIn("未变化的 snapshot 不唤醒模型", self.thread_manager)

    def test_v40_high_assurance_contracts_remain_compatible_but_nondefault(self) -> None:
        self.assertIn("高级协议", self.firewall)
        self.assertIn("不是普通项目的默认入口", self.firewall)
        self.assertIn("普通项目使用 `SKILL.md` 的七字段轻量委派", self.firewall)
        self.assertIn("普通项目首次创建 Agent", self.delegation)
        self.assertIn("只有多写入者、高风险/生产工作", self.delegation)
        for reference in (
            "project-adoption.md", "supervision.md", "main-thread-provisioning.md",
            "thread-manager.md", "workstreams.md", "supervisor-execution.md",
            "delegation.md", "capability-management.md", "organization-memory.md",
        ):
            self.assertIn(reference, self.skill)

    def test_v40_ui_and_docs_match_the_project_supervisor_positioning(self) -> None:
        combined = self.ui + self.zh + self.en
        for token in (
            "$founder-os", "项目主管", "完整理解", "主动质疑",
            "Project Brief", "AI Agent", "轻量",
        ):
            self.assertIn(token, combined)
        self.assertIn("project supervisor for solo developers", self.en)
        self.assertIn("not an enterprise-management workflow", self.en)


class LightweightSupervisorV41Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        cls.light_reference_path = SKILL_ROOT / "references" / "lightweight-worker-runtime.md"
        cls.thread_manager = (
            SKILL_ROOT / "references" / "thread-manager.md"
        ).read_text(encoding="utf-8")
        cls.provisioning = (
            SKILL_ROOT / "references" / "main-thread-provisioning.md"
        ).read_text(encoding="utf-8")
        cls.state_files = (
            SKILL_ROOT / "references" / "state-files.md"
        ).read_text(encoding="utf-8")
        cls.adoption = (
            SKILL_ROOT / "references" / "project-adoption.md"
        ).read_text(encoding="utf-8")

    def snapshot(self, **overrides: Any) -> light_runtime_module.ProjectSnapshot:
        values: dict[str, Any] = {
            "project_root": r"C:\project",
            "workflow_profile": light_runtime_module.WorkflowProfile.LIGHT,
            "brief_approved": True,
            "project_plan_approved": True,
            "last_indexed_commit": "a" * 40,
            "current_head": "a" * 40,
        }
        values.update(overrides)
        return light_runtime_module.ProjectSnapshot(**values)

    def request(
        self,
        request_type: light_runtime_module.RequestType,
        *,
        goal_id: str = "goal-1",
        signals: dict[str, bool] | None = None,
        evidence_refs: tuple[str, ...] = ("src/module.py:10",),
        override_confirmed: bool = False,
        override_risk: str | None = None,
    ) -> light_runtime_module.UserRequest:
        return light_runtime_module.UserRequest(
            goal_id=goal_id,
            request_type=request_type,
            summary="Bounded user goal",
            signals=light_runtime_module.RequestSignals.from_mapping(signals),
            evidence_refs=evidence_refs,
            override_confirmed=override_confirmed,
            override_risk=override_risk,
        )

    def identity(
        self, thread_id: str = "thread-real-1"
    ) -> light_runtime_module.RuntimeThreadIdentity:
        return light_runtime_module.RuntimeThreadIdentity(
            thread_id=thread_id,
            project_id="project-real-1",
            host_id="host-real-1",
        )

    def capabilities(self) -> light_runtime_module.RuntimeCapabilities:
        return light_runtime_module.RuntimeCapabilities.available()

    def packet(
        self, *, write_scope: tuple[str, ...] = ("src/module.py", "tests/test_module.py")
    ) -> light_runtime_module.TaskPacket:
        context = (
            "Project snapshot: Python service with an existing public boundary. "
            "Relevant implementation is limited to src/module.py and tests/test_module.py. "
            "The current behavior and failure signature are referenced by path; no transcript, "
            "ledger dump, protocol text, or long log is embedded. " * 4
        )
        values = {
            "OBJECTIVE": "Fix the reproducible local defect while preserving existing behavior.",
            "PROJECT_CONTEXT": context,
            "CHOSEN_APPROACH": (
                "Use one owner for reproduce -> diagnose -> fix -> regression test. "
                "Keep the patch local and fail closed if the suspected cause is disproved."
            ),
            "CONTEXT_REFS": (
                "src/module.py:10; tests/test_module.py:20; artifacts/failure.txt#sha256=abc"
            ),
            "READ_WRITE_SCOPE": (
                "Read src/module.py and tests/test_module.py; write only those two files; "
                "do not modify .founder or unrelated paths."
            ),
            "DELIVERABLES": (
                "A minimal patch, a regression test, and a concise evidence record with exact paths."
            ),
            "ACCEPTANCE_AND_TESTS": (
                "Reproduce the original failure, run the focused test, run the safe related suite, "
                "and report exact commands and results without upgrading offline proof."
            ),
            "STOP_OR_ESCALATE_WHEN": (
                "Stop if scope must expand, compatibility must break, a credential appears, "
                "the root-cause hypothesis fails, or a destructive/external action is required."
            ),
        }
        return light_runtime_module.TaskPacket(
            values,
            frozenset({"project_snapshot", "code_refs", "artifact_refs"}),
            initial_context_tokens=8_000,
            write_scope=write_scope,
        )

    def accepted_result(self) -> light_runtime_module.WorkerResult:
        return light_runtime_module.WorkerResult(
            result="PASS",
            changed_paths=("src/module.py", "tests/test_module.py"),
            validation_commands=("python -m unittest tests.test_module",),
            validation_result="exit=0; 12 focused tests passed; log=artifacts/test.log",
            actual_artifacts_inspected=True,
            diff_inspected=True,
            tests_inspected=True,
        )

    def test_v41_f0_status_is_bounded_zero_worker_zero_write(self) -> None:
        run = light_runtime_module.SupervisorRun(self.snapshot())
        assessment = run.begin(
            self.request(light_runtime_module.RequestType.QUESTION_OR_STATUS)
        )
        self.assertEqual(assessment.level, light_runtime_module.FitLevel.CONTINUATION)
        self.assertEqual(run.worker_ids, [])
        self.assertEqual(run.state_write_count, 0)
        self.assertEqual(run.model_wake_count, 0)
        self.assertEqual(run.trace[-1]["action"], "READ_STATUS_BOUNDED")

    def test_v41_f1_bug_has_one_fit_one_owner_event_wait_and_acceptance(self) -> None:
        run = light_runtime_module.SupervisorRun(self.snapshot())
        request = self.request(light_runtime_module.RequestType.BUG_REPORT)
        run.begin(request)
        run.begin(request)
        run.dispatch(self.packet(), self.identity("worker-real-1"), self.capabilities())
        run.wait_snapshot(changed=False)
        run.wait_snapshot(changed=True, worker_state="COMPLETED")
        self.assertTrue(run.review_result(self.accepted_result()))
        actions = [row["action"] for row in run.trace]
        self.assertEqual(run.fit_check_count, 1)
        self.assertEqual(run.worker_ids, ["worker-real-1"])
        self.assertIn("BUG_SINGLE_OWNER_PIPELINE", actions)
        self.assertEqual(run.model_wake_count, 1)
        self.assertEqual(run.state_write_count, 1)
        self.assertLess(actions.index("WAIT_EVENT_DRIVEN"), actions.index("ACCEPT_RESULT"))

    def test_v41_f1_feature_skips_rediscovery_and_full_plan_reapproval(self) -> None:
        run = light_runtime_module.SupervisorRun(self.snapshot())
        assessment = run.begin(self.request(light_runtime_module.RequestType.FEATURE_IDEA))
        self.assertEqual(assessment.level, light_runtime_module.FitLevel.LOCAL)
        ready = run.trace[-1]
        self.assertFalse(ready["rediscovery"])
        self.assertFalse(ready["full_plan_reapproval"])
        self.assertEqual(run.dispatch_gate, "OPEN")

    def test_v41_f2_blocks_dispatch_until_plan_delta_confirmation(self) -> None:
        run = light_runtime_module.SupervisorRun(self.snapshot())
        assessment = run.begin(
            self.request(
                light_runtime_module.RequestType.FEATURE_IDEA,
                signals={"public_interface_change": True, "multiple_modules": True},
            )
        )
        self.assertEqual(assessment.level, light_runtime_module.FitLevel.PLAN_DELTA)
        with self.assertRaises(light_runtime_module.PolicyError):
            run.dispatch(
                self.packet(), self.identity("worker-before-approval"), self.capabilities()
            )
        self.assertEqual(run.worker_ids, [])
        run.confirm_plan_delta("user-approved-delta-1")
        run.dispatch(
            self.packet(), self.identity("worker-after-approval"), self.capabilities()
        )
        self.assertEqual(run.worker_ids, ["worker-after-approval"])

    def test_v41_f3_has_no_implementation_worker_before_brief_and_plan(self) -> None:
        run = light_runtime_module.SupervisorRun(
            self.snapshot(brief_approved=False, project_plan_approved=False)
        )
        assessment = run.begin(
            self.request(
                light_runtime_module.RequestType.PROJECT_IDEA,
                signals={"new_project": True},
            )
        )
        self.assertEqual(assessment.level, light_runtime_module.FitLevel.PROJECT_RESET)
        with self.assertRaises(light_runtime_module.PolicyError):
            run.dispatch(
                self.packet(), self.identity("premature-worker"), self.capabilities()
            )
        self.assertEqual(run.worker_ids, [])
        run.confirm_new_project("brief-approved", "plan-approved")
        run.dispatch(self.packet(), self.identity("approved-worker"), self.capabilities())
        self.assertEqual(run.worker_ids, ["approved-worker"])

    def test_v41_open_source_research_is_bounded_and_never_clones(self) -> None:
        candidate = {
            "name": "candidate-a",
            "official_source": "https://example.invalid/official",
            "license": "Apache-2.0",
            "maintenance": "active",
            "stack_compatibility": "compatible",
            "integration_cost": "low",
            "security_risk": "reviewed",
            "limitations": "bounded limitation",
        }
        result = light_runtime_module.validate_research_candidates(
            [candidate] * 5, [candidate] * 3
        )
        self.assertEqual(result["final_count"], 3)
        self.assertEqual(result["repository_clones"], 0)
        with self.assertRaises(light_runtime_module.PolicyError):
            light_runtime_module.validate_research_candidates(
                [candidate] * 5, [candidate] * 3, clone_or_full_copy_requested=True
            )

    def test_v41_task_packet_rejects_chat_ledgers_and_advanced_protocol(self) -> None:
        packet = self.packet()
        self.assertEqual(
            tuple(packet.values),
            (
                "OBJECTIVE",
                "PROJECT_CONTEXT",
                "CHOSEN_APPROACH",
                "CONTEXT_REFS",
                "READ_WRITE_SCOPE",
                "DELIVERABLES",
                "ACCEPTANCE_AND_TESTS",
                "STOP_OR_ESCALATE_WHEN",
            ),
        )
        rendered = packet.render()
        self.assertNotIn("RUNTIME_CONSTRAINTS", rendered)
        self.assertEqual(
            tuple(line for line in rendered.splitlines() if line in light_runtime_module.TASK_PACKET_FIELDS),
            light_runtime_module.TASK_PACKET_FIELDS,
        )
        with self.assertRaises(light_runtime_module.PolicyError):
            light_runtime_module.TaskPacket(
                packet.values,
                frozenset({"full_chat", "all_ledgers", "founder_os_advanced_protocol"}),
            )
        parsed = light_runtime_module.WorkerResult.from_mapping(
            {
                "RESULT": "PASS",
                "CHANGED_PATHS": ["src/module.py"],
                "VALIDATION_COMMANDS": ["python -m unittest tests.test_module"],
                "VALIDATION_RESULT": "exit=0; log=artifacts/test.log",
                "RISKS_OR_BLOCKERS": [],
                "DECISION_NEEDED": "None",
            }
        )
        self.assertEqual(parsed.validation_result, "exit=0; log=artifacts/test.log")
        with self.assertRaises(light_runtime_module.PolicyError):
            light_runtime_module.WorkerResult.from_mapping(
                {
                    "RESULT": "PASS",
                    "CHANGED_PATHS": ["src/module.py"],
                    "VALIDATION_COMMANDS": ["python -m unittest tests.test_module"],
                    "VALIDATION_RESULT": "exit=0",
                    "RISKS_OR_BLOCKERS": [],
                    "DECISION_NEEDED": "None",
                    "EXTRA_SECTION": "not allowed",
                }
            )
        with self.assertRaises(light_runtime_module.PolicyError):
            light_runtime_module.WorkerResult.from_mapping(
                {
                    "RESULT": "PASS",
                    "CHANGED_PATHS_OR_ARTIFACTS": ["src/module.py"],
                    "VALIDATION": ["claimed pass"],
                    "RISKS_OR_BLOCKERS": [],
                    "DECISION_NEEDED": "None",
                }
            )

    def test_v41_local_feature_uses_one_real_thread_event_wait_and_one_acceptance(self) -> None:
        run = light_runtime_module.SupervisorRun(self.snapshot())
        run.begin(self.request(light_runtime_module.RequestType.FEATURE_IDEA))
        self.assertTrue(
            run.dispatch(self.packet(), self.identity("feature-thread-1"), self.capabilities())
        )
        run.wait_snapshot(changed=True, worker_state="COMPLETED")
        self.assertTrue(run.review_result(self.accepted_result()))
        actions = [row["action"] for row in run.trace]
        self.assertEqual(run.worker_ids, ["feature-thread-1"])
        self.assertEqual(actions.count("CREATE_REAL_CODEX_THREAD"), 1)
        self.assertEqual(actions.count("WAIT_EVENT_DRIVEN"), 1)
        self.assertEqual(actions.count("ACCEPT_RESULT"), 1)
        self.assertTrue(self.packet().metrics()["target_2_to_4_kib"])

    def test_v41_runtime_without_real_thread_flow_blocks_without_roleplay(self) -> None:
        run = light_runtime_module.SupervisorRun(self.snapshot())
        run.begin(self.request(light_runtime_module.RequestType.FEATURE_IDEA))
        self.assertFalse(
            run.dispatch(
                self.packet(),
                self.identity("invented-thread-must-not-bind"),
                light_runtime_module.RuntimeCapabilities(),
            )
        )
        self.assertEqual(run.worker_ids, [])
        self.assertEqual(run.runtime_blocker, "RUNTIME_THREAD_CAPABILITY_UNAVAILABLE")
        self.assertEqual(
            run.trace[-1]["action"], "RUNTIME_THREAD_CAPABILITY_UNAVAILABLE"
        )
        self.assertFalse(run.trace[-1]["roleplay_worker_created"])

    def test_v41_overlapping_parallel_write_scopes_are_rejected(self) -> None:
        run = light_runtime_module.SupervisorRun(self.snapshot())
        run.begin(
            self.request(light_runtime_module.RequestType.FEATURE_IDEA, goal_id="goal-left")
        )
        run.dispatch(
            self.packet(write_scope=("src/shared",)),
            self.identity("thread-left"),
            self.capabilities(),
            parallel=True,
        )
        run.begin(
            self.request(light_runtime_module.RequestType.BUG_REPORT, goal_id="goal-right")
        )
        with self.assertRaisesRegex(light_runtime_module.PolicyError, "overlap"):
            run.dispatch(
                self.packet(write_scope=("src/shared/module.py",)),
                self.identity("thread-right"),
                self.capabilities(),
                parallel=True,
            )
        self.assertEqual(run.worker_ids, ["thread-left"])

    def test_v41_light_trace_never_enters_advanced_control_plane(self) -> None:
        run = light_runtime_module.SupervisorRun(self.snapshot())
        run.begin(self.request(light_runtime_module.RequestType.MAINTENANCE))
        run.dispatch(self.packet(), self.identity("worker-1"), self.capabilities())
        actions = {row["action"] for row in run.trace}
        forbidden = {
            "READ_STRATEGY",
            "CLAIM_ACTIVE_SUPERVISOR",
            "ACQUIRE_WRITE_LOCK",
            "INIT_THREAD_REGISTRY",
            "READ_SKILL_REGISTRY",
            "READ_ORGANIZATION_MEMORY",
            "LOAD_FULL_THREAD_MANAGER",
        }
        self.assertTrue(actions.isdisjoint(forbidden))

    def test_v41_five_ledgers_without_strategy_are_normal_for_light(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            founder = root / ".founder"
            founder.mkdir()
            for name in decision_module.CORE_LEDGERS:
                body = "# state\n"
                if name == "PROJECT.md":
                    body += "- workflow_profile=V4_LIGHT\n"
                (founder / name).write_text(body, encoding="utf-8")
            result = decision_module.authorize_action(
                str(root), action="candidate-bound-work"
            )
            self.assertEqual(result["result"], "NOT_APPLICABLE_LIGHTWEIGHT")
            self.assertNotIn("STRATEGY.json", {path.name for path in founder.iterdir()})

    def test_v41_governed_legacy_gate_remains_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            founder = root / ".founder"
            founder.mkdir()
            for name in decision_module.CORE_LEDGERS:
                (founder / name).write_text("# legacy\n", encoding="utf-8")
            result = decision_module.authorize_action(
                str(root),
                action="candidate-bound-work",
                workflow_profile="V4_GOVERNED",
            )
            self.assertFalse(result["allowed"])
            self.assertIn("LEGACY_MIGRATION_REQUIRED", result["reason"])
        light_run = light_runtime_module.SupervisorRun(self.snapshot())
        light_run.begin(
            self.request(
                light_runtime_module.RequestType.FEATURE_IDEA,
                signals={"security_risk": True},
                override_confirmed=True,
                override_risk="User accepts the product risk but governed controls remain required.",
            )
        )
        self.assertEqual(light_run.dispatch_gate, "GOVERNED_MODE_REQUIRED")
        self.assertIn("USER_OVERRIDE_RECORDED", [row["action"] for row in light_run.trace])
        with self.assertRaises(light_runtime_module.PolicyError):
            light_run.dispatch(self.packet(), self.identity(), self.capabilities())

    def test_v41_unchanged_wait_has_zero_wakeup_and_zero_state_write(self) -> None:
        run = light_runtime_module.SupervisorRun(self.snapshot())
        run.begin(self.request(light_runtime_module.RequestType.BUG_REPORT))
        run.dispatch(self.packet(), self.identity("worker-1"), self.capabilities())
        trace_len = len(run.trace)
        for _ in range(5):
            run.wait_snapshot(changed=False)
        self.assertEqual(len(run.trace), trace_len)
        self.assertEqual(run.model_wake_count, 0)
        self.assertEqual(run.state_write_count, 0)

    def test_v41_large_output_uses_artifact_reference(self) -> None:
        result = light_runtime_module.artifact_delivery(
            "x" * (light_runtime_module.OUTPUT_ARTIFACT_THRESHOLD + 1),
            "artifacts/test-output.txt#sha256=abc",
        )
        self.assertEqual(result["mode"], "ARTIFACT_REFERENCE")
        self.assertIsNone(result["content"])
        inline = light_runtime_module.artifact_delivery("small")
        self.assertEqual(inline["mode"], "INLINE")

    def test_v41_second_failed_rework_trips_breaker_without_replacement(self) -> None:
        run = light_runtime_module.SupervisorRun(self.snapshot())
        run.begin(self.request(light_runtime_module.RequestType.BUG_REPORT))
        run.dispatch(self.packet(), self.identity("original-worker"), self.capabilities())
        self.assertTrue(
            run.dispatch(
                self.packet(), self.identity("original-worker"), self.capabilities()
            )
        )
        with self.assertRaisesRegex(light_runtime_module.PolicyError, "original thread"):
            run.dispatch(
                self.packet(), self.identity("replacement-worker"), self.capabilities()
            )
        self.assertTrue(run.request_revision(["defect one"]))
        self.assertTrue(run.request_revision(["defect two"]))
        self.assertFalse(run.request_revision(["defect remains"]))
        self.assertEqual(run.worker_ids, ["original-worker"])
        self.assertEqual(run.circuit_breaker, "REWORK_LIMIT_REPLAN_REQUIRED")
        self.assertFalse(run.trace[-1]["replacement_thread_created"])
        revisions = [
            row for row in run.trace if row["action"] == "REQUEST_TARGETED_REVISION"
        ]
        self.assertEqual([row["thread_id"] for row in revisions], ["original-worker"] * 2)
        self.assertTrue(
            all(row["transport"] == "send_message_to_thread" for row in revisions)
        )
        self.assertEqual(
            [row["action"] for row in run.trace].count("REUSE_ORIGINAL_THREAD_ID"), 1
        )

    def test_v41_token_usage_is_one_shared_supervisor_worker_budget(self) -> None:
        ledger = light_runtime_module.BudgetLedger(1_000, telemetry_available=True)
        ledger.record_usage(
            "supervisor", "execution", light_runtime_module.TokenUsage(input_tokens=300)
        )
        ledger.record_usage(
            "worker-1", "execution", light_runtime_module.TokenUsage(output_tokens=600)
        )
        state = ledger.state()
        self.assertEqual(state["action"], "NO_NEW_WORKER_REPLAN")
        self.assertEqual(state["usage"]["raw_component_total"], 900)
        self.assertEqual(set(state["actors"]), {"supervisor", "worker-1"})

    def test_v41_two_no_evidence_turns_trip_efficiency_breaker(self) -> None:
        run = light_runtime_module.SupervisorRun(self.snapshot())
        run.begin(self.request(light_runtime_module.RequestType.MAINTENANCE))
        run.record_model_turn(evidence_progress=False)
        self.assertIsNone(run.circuit_breaker)
        run.record_model_turn(evidence_progress=False)
        self.assertEqual(run.circuit_breaker, "EFFICIENCY_CIRCUIT_BREAKER")

    def test_v41_v23_history_is_preserved_while_light_state_is_compacted(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            founder = root / ".founder"
            founder.mkdir()
            old_paths = []
            for name in decision_module.CORE_LEDGERS:
                path = founder / name
                path.write_text(f"# legacy {name}\n", encoding="utf-8")
                old_paths.append(path)
            before = {path.name: path.read_bytes() for path in old_paths}
            projection = light_runtime_module.compact_state_projection(
                project_name="Legacy Project",
                current_head="b" * 40,
                phase="EXECUTION",
                accepted_summary="One accepted maintenance task",
            )
            after = {path.name: path.read_bytes() for path in old_paths}
            self.assertEqual(before, after)
            self.assertIn("workflow_profile=V4_LIGHT", projection["PROJECT.md"])
            self.assertLessEqual(
                len(projection["STATUS.md"].encode("utf-8")),
                light_runtime_module.STATUS_TARGET_BYTES,
            )

    def test_v41_worker_flow_never_recursively_creates_a_supervisor(self) -> None:
        run = light_runtime_module.SupervisorRun(self.snapshot())
        run.begin(self.request(light_runtime_module.RequestType.FEATURE_IDEA))
        run.dispatch(self.packet(), self.identity("worker-real-1"), self.capabilities())
        actions = [row["action"] for row in run.trace]
        self.assertEqual(actions.count("REAL_THREAD_ID_BOUND"), 1)
        self.assertNotIn("CREATE_SUPERVISOR", actions)
        self.assertNotIn("BOOTSTRAP_MANAGER_TASK", actions)

    def test_v41_project_conflict_reports_evidence_impact_recommendation_alternative(self) -> None:
        run = light_runtime_module.SupervisorRun(self.snapshot())
        assessment = run.begin(
            self.request(
                light_runtime_module.RequestType.FEATURE_IDEA,
                signals={"architecture_conflict": True, "simpler_approach_available": True},
                evidence_refs=("src/architecture.py:20",),
            )
        )
        self.assertEqual(assessment.fit, "CONFLICT")
        row = run.trace[-1]
        self.assertEqual(row["action"], "FIT_CONFLICT")
        for key in ("evidence", "impact", "recommendation", "alternative"):
            self.assertTrue(row[key])
        self.assertEqual(run.worker_ids, [])

    def test_v41_explicit_override_records_risk_then_continues(self) -> None:
        run = light_runtime_module.SupervisorRun(self.snapshot())
        assessment = run.begin(
            self.request(
                light_runtime_module.RequestType.FEATURE_IDEA,
                signals={"architecture_conflict": True},
                override_confirmed=True,
                override_risk="May duplicate an existing boundary",
            )
        )
        self.assertEqual(assessment.fit, "PASS")
        self.assertEqual(run.trace[-2]["action"], "USER_OVERRIDE_RECORDED")
        self.assertEqual(run.dispatch_gate, "OPEN")

    def test_v41_summary_only_result_cannot_be_accepted(self) -> None:
        run = light_runtime_module.SupervisorRun(self.snapshot())
        run.begin(self.request(light_runtime_module.RequestType.BUG_REPORT))
        run.dispatch(self.packet(), self.identity("worker-1"), self.capabilities())
        run.wait_snapshot(changed=True, worker_state="COMPLETED")
        claim = light_runtime_module.WorkerResult(
            result="PASS",
            changed_paths=("src/module.py",),
            validation_commands=("python -m unittest tests.test_module",),
            validation_result="claimed pass without inspected evidence",
        )
        self.assertFalse(run.review_result(claim))
        self.assertFalse(run.accepted)
        self.assertEqual(run.state_write_count, 0)
        self.assertTrue(run.review_result(self.accepted_result()))

    def test_v41_profile_marker_auto_routes_both_legacy_helpers_away(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            founder = root / ".founder"
            founder.mkdir()
            (founder / "PROJECT.md").write_text(
                "# Project\n- workflow_profile=V4_LIGHT\n", encoding="utf-8"
            )
            self.assertEqual(
                decision_module.resolve_workflow_profile(str(root)), "V4_LIGHT"
            )
            with mock.patch("builtins.print") as output:
                exit_code = registry_module.main(["inspect", "--project", str(root)])
            self.assertEqual(exit_code, 0)
            emitted = json.loads(output.call_args.args[0])
            self.assertEqual(emitted["result"], "NOT_APPLICABLE_LIGHTWEIGHT")
            self.assertFalse((founder / "THREADS.json").exists())

    def test_v41_explicit_light_profile_is_zero_write_without_founder_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            before = snapshot_tree(root)
            with mock.patch("builtins.print") as output:
                exit_code = registry_module.main(
                    [
                        "inspect",
                        "--project",
                        str(root),
                        "--workflow-profile",
                        "V4_LIGHT",
                    ]
                )
            self.assertEqual(exit_code, 0)
            self.assertEqual(snapshot_tree(root), before)
            self.assertEqual(
                json.loads(output.call_args.args[0])["result"],
                "NOT_APPLICABLE_LIGHTWEIGHT",
            )

    def test_v41_missing_token_telemetry_uses_named_proxies_not_fake_tokens(self) -> None:
        ledger = light_runtime_module.BudgetLedger(telemetry_available=False)
        ledger.record_proxy(
            model_rounds=3,
            bytes_read=10_000,
            task_packet_bytes=2_500,
            worker_count=1,
            state_write_count=1,
        )
        state = ledger.state()
        self.assertEqual(state["telemetry"], "TOKEN_TELEMETRY_UNAVAILABLE")
        self.assertEqual(state["token_thresholds"], "UNVERIFIED")
        self.assertNotIn("estimated_tokens", state["proxies"])

    def test_v41_budget_thresholds_block_optional_new_and_all_work(self) -> None:
        cases = (
            (250, False, "PAUSE_NO_VERIFIABLE_PROGRESS"),
            (700, True, "STOP_OPTIONAL_WORK"),
            (900, True, "NO_NEW_WORKER_REPLAN"),
            (1_000, True, "HARD_STOP"),
        )
        for usage, progress, expected in cases:
            with self.subTest(expected=expected):
                ledger = light_runtime_module.BudgetLedger(1_000, telemetry_available=True)
                ledger.record_usage(
                    "combined", "execution", light_runtime_module.TokenUsage(input_tokens=usage)
                )
                self.assertEqual(ledger.state(evidence_progress=progress)["action"], expected)

    def test_v41_governance_and_fit_check_have_shared_ratio_fences(self) -> None:
        governance = light_runtime_module.BudgetLedger(10_000, telemetry_available=True)
        governance.record_usage(
            "supervisor", "governance", light_runtime_module.TokenUsage(input_tokens=31)
        )
        governance.record_usage(
            "worker", "execution", light_runtime_module.TokenUsage(input_tokens=69)
        )
        self.assertEqual(governance.state()["action"], "GOVERNANCE_BUDGET_EXCEEDED")

        fit = light_runtime_module.BudgetLedger(10_000, telemetry_available=True)
        fit.record_usage(
            "supervisor", "fit_check", light_runtime_module.TokenUsage(input_tokens=11)
        )
        fit.record_usage(
            "worker", "execution", light_runtime_module.TokenUsage(input_tokens=89)
        )
        self.assertEqual(fit.state()["action"], "FIT_CHECK_BUDGET_EXCEEDED")

    def test_v41_packet_size_and_initial_context_are_measured(self) -> None:
        metrics = self.packet().metrics()
        self.assertTrue(metrics["target_2_to_4_kib"])
        self.assertTrue(metrics["initial_context_within_recommendation"])
        self.assertGreaterEqual(metrics["bytes"], 2 * 1024)
        self.assertLessEqual(metrics["bytes"], 4 * 1024)
        with self.assertRaises(light_runtime_module.PolicyError):
            self.packet(write_scope=(r"C:\outside\module.py",))

    def test_v41_trusted_test_result_on_same_source_version_is_not_rerun(self) -> None:
        planner = light_runtime_module.ValidationPlanner()
        planner.record(
            light_runtime_module.TestEvidence(
                source_version="commit-abc",
                command="python -m unittest tests.test_module",
                exit_code=0,
                trusted=True,
                log_ref="artifacts/test-module.log",
            )
        )
        reused = planner.plan(
            source_version="commit-abc",
            command="python -m unittest tests.test_module",
            change_risk=light_runtime_module.ChangeRisk.LOCAL_FEATURE_OR_BUG,
        )
        changed = planner.plan(
            source_version="commit-def",
            command="python -m unittest tests.test_module",
            change_risk=light_runtime_module.ChangeRisk.LOCAL_FEATURE_OR_BUG,
        )
        self.assertEqual(reused["action"], "REUSE_TRUSTED_RESULT")
        self.assertEqual(reused["exit_code"], 0)
        self.assertEqual(changed["action"], "RUN_TEST")

    def test_v41_validation_scope_is_related_for_ordinary_and_full_only_at_boundary(self) -> None:
        self.assertEqual(
            light_runtime_module.validation_scope_for(
                light_runtime_module.ChangeRisk.LOCAL_FEATURE_OR_BUG
            ),
            light_runtime_module.ValidationScope.RELATED_TESTS,
        )
        self.assertEqual(
            light_runtime_module.validation_scope_for(
                light_runtime_module.ChangeRisk.CROSS_MODULE_INTERFACE_OR_DATABASE
            ),
            light_runtime_module.ValidationScope.RELATED_UNIT_AND_INTEGRATION,
        )
        self.assertEqual(
            light_runtime_module.validation_scope_for(
                light_runtime_module.ChangeRisk.LOCAL_FEATURE_OR_BUG,
                integration_node=True,
            ),
            light_runtime_module.ValidationScope.FULL_SUITE_ONCE,
        )

    def test_v41_full_suite_evidence_is_reused_once_per_source_version(self) -> None:
        planner = light_runtime_module.ValidationPlanner()
        planner.record(
            light_runtime_module.TestEvidence(
                source_version="commit-integration",
                command="python scripts/validate_founder_os.py",
                exit_code=0,
                trusted=True,
                log_ref="artifacts/full.log",
                scope=light_runtime_module.ValidationScope.FULL_SUITE_ONCE,
            )
        )
        plan = planner.plan(
            source_version="commit-integration",
            command="another equivalent full-suite entrypoint",
            change_risk=light_runtime_module.ChangeRisk.MILESTONE_RELEASE_OR_HIGH_RISK,
            integration_node=True,
        )
        self.assertEqual(plan["action"], "REUSE_TRUSTED_FULL_SUITE")

    def test_v41_failure_classes_are_explicit_and_mutually_exclusive(self) -> None:
        self.assertEqual(
            light_runtime_module.classify_failure(failed_before_change=True).value,
            "BASELINE_FAILURE",
        )
        self.assertEqual(light_runtime_module.classify_failure().value, "NEW_FAILURE")
        self.assertEqual(
            light_runtime_module.classify_failure(
                failed_before_change=True, environment_limited=True
            ).value,
            "ENVIRONMENT_LIMITATION",
        )

    def test_v41_task_thread_mapping_has_one_real_identity_per_task(self) -> None:
        binding = light_runtime_module.TaskThreadBinding(
            task_id="goal-1",
            identity=self.identity("thread-map-1"),
            objective="Implement the bounded feature",
            write_scope=("src/module.py",),
        )
        rendered = light_runtime_module.render_task_thread_mapping([binding])
        self.assertEqual(rendered.count("thread-map-1"), 1)
        self.assertEqual(rendered.count("goal-1"), 1)
        with self.assertRaises(light_runtime_module.PolicyError):
            light_runtime_module.render_task_thread_mapping([binding, binding])

    def test_v41_worker_limits_are_enforced_without_recursive_agents(self) -> None:
        run = light_runtime_module.SupervisorRun(self.snapshot())
        run.begin(
            self.request(light_runtime_module.RequestType.MAINTENANCE, goal_id="goal-1")
        )
        run.dispatch(
            self.packet(write_scope=("src/a",)),
            self.identity("worker-1"),
            self.capabilities(),
            parallel=True,
        )
        run.begin(
            self.request(light_runtime_module.RequestType.FEATURE_IDEA, goal_id="goal-2")
        )
        run.dispatch(
            self.packet(write_scope=("src/b",)),
            self.identity("worker-2"),
            self.capabilities(),
            parallel=True,
        )
        run.begin(
            self.request(light_runtime_module.RequestType.FEATURE_IDEA, goal_id="goal-3")
        )
        with self.assertRaises(light_runtime_module.PolicyError):
            run.dispatch(
                self.packet(write_scope=("src/c",)),
                self.identity("worker-3"),
                self.capabilities(),
                parallel=True,
            )
        self.assertEqual(run.worker_ids, ["worker-1", "worker-2"])

    def test_v41_full_light_trace_orders_fit_packet_dispatch_wait_accept_state(self) -> None:
        run = light_runtime_module.SupervisorRun(self.snapshot())
        run.begin(self.request(light_runtime_module.RequestType.BUG_REPORT))
        run.dispatch(self.packet(), self.identity("worker-1"), self.capabilities())
        run.wait_snapshot(changed=True, worker_state="COMPLETED")
        run.review_result(self.accepted_result())
        actions = [row["action"] for row in run.trace]
        ordered = (
            "FIT_CHECK_ONCE",
            "TASK_PACKET_VALIDATED",
            "REAL_THREAD_ID_BOUND",
            "SEND_TASK_PACKET",
            "WAIT_EVENT_DRIVEN",
            "WORKER_EVENT",
            "READ_ACTUAL_ARTIFACTS",
            "INSPECT_DIFF",
            "VERIFY_TEST_EVIDENCE",
            "ACCEPT_RESULT",
            "UPDATE_STATUS_ONCE",
        )
        positions = [actions.index(item) for item in ordered]
        self.assertEqual(positions, sorted(positions))

    def test_v41_blocked_task_updates_status_once(self) -> None:
        run = light_runtime_module.SupervisorRun(self.snapshot())
        run.begin(self.request(light_runtime_module.RequestType.MAINTENANCE))
        run.record_blocked("Required external credential is unavailable")
        run.record_blocked("Same blocker remains")
        self.assertEqual(run.state_write_count, 1)

    def test_v41_forward_fixture_is_locked_and_does_not_run_models(self) -> None:
        fixture = light_runtime_module.build_forward_test_fixture()
        self.assertEqual(
            [row["name"] for row in fixture["arms"]],
            ["V2.3", "V4.1", "direct-single-agent"],
        )
        self.assertFalse(fixture["common_controls"]["auto_run"])
        self.assertEqual(
            fixture["common_controls"]["missing_telemetry_result"], "UNVERIFIED"
        )
        self.assertEqual(fixture["claims_allowed_without_run"], [])

    def test_v41_profile_documents_are_consistent_and_progressively_disclosed(self) -> None:
        self.assertTrue(self.light_reference_path.is_file())
        for text in (
            self.skill,
            self.thread_manager,
            self.provisioning,
            self.state_files,
            self.adoption,
        ):
            self.assertIn("V4_LIGHT", text)
            self.assertIn("V4_GOVERNED", text)
        self.assertIn("lightweight-worker-runtime.md", self.skill)
        self.assertIn("NOT_APPLICABLE_LIGHTWEIGHT", self.thread_manager)

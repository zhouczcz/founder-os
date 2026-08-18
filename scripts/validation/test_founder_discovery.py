"""FounderOS regression tests: FounderDiscoveryV21Tests.

Split from validate_founder_os.py; class bodies are copied verbatim so the
frozen AST manifests keep matching. Run via scripts/validate_founder_os.py.
"""

from __future__ import annotations

from validation.common import (  # noqa: F401
    Any,
    Path,
    checkpoint_external_changes,
    claim,
    copy,
    create_active_project,
    create_empty_active_project,
    create_legacy_operating_project,
    decision_module,
    exact_state_sync_ack,
    guard_module,
    initialize_new_strategy,
    initialize_thread_registry,
    json,
    make_operating_clear_project,
    os,
    registry_module,
    snapshot_tree,
    strategy_candidates,
    strategy_recommendation,
    strategy_state,
    tempfile,
    unittest,
    write_strategy_ledgers,
)


class FounderDiscoveryV21Tests(unittest.TestCase):
    """State-machine regressions added by FounderOS V2.1.

    These tests intentionally exercise the controller and Registry APIs instead
    of reimplementing their transition rules in the validator.
    """

    OWNER = "founder-os-main-v21-test"

    def _assess_ambiguous(self, root: Path, state: dict[str, Any]) -> dict[str, Any]:
        mutation = decision_module.assess_direction(
            str(root),
            owner=self.OWNER,
            activation_token=state["activation_token"],
            expected_state_sha=state["state_sha"],
            expected_strategy_sha=state["strategy_sha"],
            outcome="AMBIGUOUS",
            reason="Several materially different users and product shapes remain plausible",
            direction_summary="An AI game company without a selected product wedge",
            depth="STANDARD",
        )
        return strategy_state(state, mutation)

    def _record_candidates(
        self,
        root: Path,
        state: dict[str, Any],
        *,
        proposal_id: str,
        count: int = 3,
    ) -> dict[str, Any]:
        mutation = decision_module.record_candidates(
            str(root),
            owner=self.OWNER,
            activation_token=state["activation_token"],
            expected_state_sha=state["state_sha"],
            expected_strategy_sha=state["strategy_sha"],
            proposal_id=proposal_id,
            candidates=strategy_candidates(count),
            recommendation_id="direction-a",
            recommendation=strategy_recommendation(),
            evidence=["Bounded comparative scan completed"],
            single_candidate_reason=(
                "No peer alternative survived the evidence screen" if count == 1 else None
            ),
        )
        return strategy_state(state, mutation)

    def _select(
        self,
        root: Path,
        state: dict[str, Any],
        *,
        proposal_id: str,
        candidate_id: str,
        authority: str,
        decision_id: str,
    ) -> dict[str, Any]:
        mutation = decision_module.select_candidate(
            str(root),
            owner=self.OWNER,
            activation_token=state["activation_token"],
            expected_state_sha=state["state_sha"],
            expected_strategy_sha=state["strategy_sha"],
            proposal_id=proposal_id,
            candidate_id=candidate_id,
            authority=authority,
            authorization_ref=f"Current Founder response for {proposal_id}",
            decision_id=decision_id,
            rationale=f"{authority} selected {candidate_id} for the current Gate",
            nonselected_status="DEFERRED",
        )
        return strategy_state(state, mutation)

    def _bind_persistent_thread(
        self,
        root: Path,
        state: dict[str, Any],
        *,
        agent_id: str,
        runtime_thread_id: str,
    ) -> tuple[dict[str, Any], str, str]:
        """Initialize, reserve, and bind one current persistent Thread."""

        current = dict(state)
        initialized = initialize_thread_registry(root, current, self.OWNER)
        current["state_sha"] = initialized["state_sha"]
        registry_sha = initialized["registry_sha"]
        reserved = registry_module.reserve_thread(
            str(root),
            owner=self.OWNER,
            activation_token=current["activation_token"],
            expected_state_sha=current["state_sha"],
            expected_registry_sha=registry_sha,
            agent_id=agent_id,
            agent_kind="persistent",
            logical_name=f"Persistent {agent_id}",
            manager_agent_id="founder-os-main",
            workstream="engineering",
            thread_type="persistent",
            read_scope=["src/**", "docs/**"],
            write_scope=["src/engineering/**"],
            skills=[],
            dependencies=[],
            strategy_scope="candidate-bound",
        )
        current["state_sha"] = reserved["state_sha"]
        registry_sha = reserved["registry_sha"]
        thread_record_id = reserved["details"]["thread_record_id"]
        bound = registry_module.bind_runtime(
            str(root),
            owner=self.OWNER,
            activation_token=current["activation_token"],
            expected_state_sha=current["state_sha"],
            expected_registry_sha=registry_sha,
            thread_record_id=thread_record_id,
            binding_nonce=reserved["details"]["binding_nonce"],
            runtime_thread_id=runtime_thread_id,
            runtime_host_id="local",
            identity_quality="observed",
            strategy_scope="candidate-bound",
        )
        current["state_sha"] = bound["state_sha"]
        return current, bound["registry_sha"], thread_record_id

    @staticmethod
    def _state_sync_ack(root: Path, thread_record_id: str) -> str:
        return exact_state_sync_ack(root, thread_record_id)

    def test_v21_new_and_legacy_initialization_defaults_and_fingerprints(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v21-init-") as temp:
            base = Path(temp)
            new_root = base / "new"
            new_root.mkdir()
            active = create_empty_active_project(new_root, self.OWNER)
            state = initialize_new_strategy(new_root, active, self.OWNER)
            inspected = decision_module.inspect_strategy(str(new_root))
            strategy = inspected["strategy"]
            self.assertEqual(strategy["project_phase"], "pre-bootstrap")
            self.assertEqual(strategy["gate"]["state"], "DIRECTION_CHECK_REQUIRED")
            self.assertEqual(
                strategy["autonomy_profile"],
                {
                    "scope": "project",
                    "implementation": "autonomous",
                    "tactical": "autonomous",
                    "strategic": "recommend_then_ask",
                    "executive": "require_explicit_approval",
                    "source": "default",
                    "evidence": "Founder requested a new project",
                },
            )
            revisions = guard_module.read_source_revisions(new_root / ".founder")
            self.assertEqual(revisions["STRATEGY_SHA256"], state["strategy_sha"])
            self.assertEqual(
                revisions["STRATEGY_CONTEXT_REVISION"], strategy["context_revision"]
            )
            self.assertEqual(
                revisions["STRATEGY_CONTEXT_SHA256"], strategy["context_sha256"]
            )

            legacy_root = base / "legacy"
            legacy_root.mkdir()
            legacy_active = create_active_project(legacy_root, self.OWNER)
            migrated = decision_module.initialize_strategy(
                str(legacy_root),
                owner=self.OWNER,
                activation_token=legacy_active["activation_token"],
                expected_state_sha=legacy_active["state_sha"],
                expected_strategy_sha="ABSENT",
                mode="legacy",
                legacy_summary="Existing EXIF product inferred from canonical ledgers",
                evidence="All five canonical ledgers prove ongoing execution",
            )
            migrated_state = strategy_state(legacy_active, migrated)
            legacy_strategy = decision_module.inspect_strategy(str(legacy_root))["strategy"]
            self.assertEqual(legacy_strategy["project_phase"], "bootstrapped")
            self.assertEqual(legacy_strategy["gate"]["state"], "OPERATING")
            self.assertEqual(legacy_strategy["direction"]["clarity"], "LEGACY_INFERRED")
            self.assertEqual(
                guard_module.read_source_revisions(legacy_root / ".founder")[
                    "STRATEGY_SHA256"
                ],
                migrated_state["strategy_sha"],
            )

    def test_v21_clear_direction_bootstraps_without_discovery_and_partial_ledgers_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v21-clear-") as temp:
            base = Path(temp)
            clear_root = base / "clear"
            clear_root.mkdir()
            active = create_empty_active_project(clear_root, self.OWNER)
            state = initialize_new_strategy(clear_root, active, self.OWNER)
            before_context = decision_module.inspect_strategy(str(clear_root))["strategy"][
                "context_revision"
            ]
            assessed = decision_module.assess_direction(
                str(clear_root),
                owner=self.OWNER,
                activation_token=state["activation_token"],
                expected_state_sha=state["state_sha"],
                expected_strategy_sha=state["strategy_sha"],
                outcome="CLEAR",
                reason="User, problem, local-only boundary and value are explicit",
                direction_summary="Windows EXIF batch renamer for photographers",
                depth="NONE",
            )
            state = strategy_state(state, assessed)
            strategy = decision_module.inspect_strategy(str(clear_root))["strategy"]
            self.assertEqual(strategy["gate"]["state"], "BOOTSTRAP_AUTHORIZED")
            self.assertEqual(strategy["discovery"]["candidates"], [])
            self.assertNotEqual(strategy["context_revision"], before_context)
            self.assertTrue(
                decision_module.authorize_action(str(clear_root), action="bootstrap")["allowed"]
            )
            self.assertFalse(
                decision_module.authorize_action(str(clear_root), action="integration")["allowed"]
            )
            write_strategy_ledgers(clear_root)
            state = checkpoint_external_changes(clear_root, state, self.OWNER)
            confirmed = decision_module.confirm_canonical(
                str(clear_root),
                owner=self.OWNER,
                activation_token=state["activation_token"],
                expected_state_sha=state["state_sha"],
                expected_strategy_sha=state["strategy_sha"],
                evidence="Clear direction is present in all canonical ledgers",
            )
            state = strategy_state(state, confirmed)
            self.assertEqual(
                decision_module.inspect_strategy(str(clear_root))["strategy"]["gate"]["state"],
                "OPERATING",
            )
            self.assertTrue(
                decision_module.authorize_action(str(clear_root), action="integration")["allowed"]
            )

            partial_root = base / "partial"
            partial_root.mkdir()
            founder = partial_root / ".founder"
            founder.mkdir()
            (founder / "PROJECT.md").write_text(
                "# Project\n\n- Last revision: R-partial\n", encoding="utf-8"
            )
            partial_completed, partial_active = claim(partial_root, self.OWNER)
            self.assertEqual(partial_completed.returncode, 0, partial_completed.stdout)
            before = snapshot_tree(partial_root)
            with self.assertRaises(guard_module.Conflict):
                decision_module.initialize_strategy(
                    str(partial_root),
                    owner=self.OWNER,
                    activation_token=partial_active["activation_token"],
                    expected_state_sha=partial_active["state_sha"],
                    expected_strategy_sha="ABSENT",
                    mode="new",
                    legacy_summary=None,
                    evidence="invalid partial migration attempt",
                )
            self.assertEqual(before, snapshot_tree(partial_root))

    def test_v21_teststartup_ai_animation_input_stops_at_strategic_choice(self) -> None:
        """Dedicated regression for the original over-autonomous TestStartup input."""

        with tempfile.TemporaryDirectory(prefix="founder-os-v21-teststartup-") as temp:
            root = Path(temp)
            active = create_empty_active_project(root, self.OWNER)
            state = initialize_new_strategy(root, active, self.OWNER)
            assessed = decision_module.assess_direction(
                str(root),
                owner=self.OWNER,
                activation_token=state["activation_token"],
                expected_state_sha=state["state_sha"],
                expected_strategy_sha=state["strategy_sha"],
                outcome="AMBIGUOUS",
                reason=(
                    "A solo near-zero-budget beginner has not selected among materially "
                    "different 2D, rigging, 3D, developer-tool, and consumer directions"
                ),
                direction_summary=(
                    "Build an AI game-character animation product from zero; one beginner "
                    "Founder; near-zero initial budget"
                ),
                depth="STANDARD",
            )
            state = strategy_state(state, assessed)
            candidates = [
                {
                    "candidate_id": "godot-sprite-pipeline",
                    "name": "Godot 2D Sprite production assistant",
                    "summary": "Help small game developers turn character art into usable 2D animation sheets",
                    "target_user": "Solo and very small 2D game developers",
                    "problem": "Preparing consistent character animation frames is slow and repetitive",
                    "opportunity": "A narrow workflow can be tested without training a large model",
                    "advantages": ["Fits a solo low-budget validation path"],
                    "risks": ["Engine-specific demand may be narrow"],
                    "difficulty": "MEDIUM",
                    "startup_cost": "LOW",
                    "validation_speed": "FAST",
                    "reversibility": "HIGH",
                    "roadmap_effect": "Selects a 2D developer-tool wedge and Godot export workflow",
                    "assessment": "Recommended as the cheapest concrete demand test",
                },
                {
                    "candidate_id": "spine-rigging-assistant",
                    "name": "Spine rigging and animation assistant",
                    "summary": "Assist artists with bone setup and reusable 2D skeletal animation",
                    "target_user": "2D game artists already using skeletal animation",
                    "problem": "Rig setup and animation cleanup require specialist labor",
                    "opportunity": "A professional workflow may support higher willingness to pay",
                    "advantages": ["Closer to an established production workflow"],
                    "risks": ["Requires domain expertise and proprietary-tool integration"],
                    "difficulty": "HIGH",
                    "startup_cost": "MEDIUM",
                    "validation_speed": "MEDIUM",
                    "reversibility": "MEDIUM",
                    "roadmap_effect": "Commits the product to skeletal-animation users and formats",
                    "assessment": "Promising but less beginner-friendly",
                },
                {
                    "candidate_id": "3d-motion-generator",
                    "name": "3D character motion generator",
                    "summary": "Generate or retarget 3D character motion from prompts or references",
                    "target_user": "3D game teams and technical animators",
                    "problem": "Creating and retargeting quality motion is expensive",
                    "opportunity": "The long-term market ceiling may be larger",
                    "advantages": ["Broader high-value animation use cases"],
                    "risks": ["High model, data, compute, and quality risk"],
                    "difficulty": "HIGH",
                    "startup_cost": "HIGH",
                    "validation_speed": "SLOW",
                    "reversibility": "LOW",
                    "roadmap_effect": "Creates a substantially different 3D research and data program",
                    "assessment": "Not a good first bet for current resources",
                },
                {
                    "candidate_id": "consumer-character-animator",
                    "name": "Consumer character animation creator",
                    "summary": "Let players create short animated character clips",
                    "target_user": "Players and social-content creators",
                    "problem": "Non-artists cannot easily animate game-style characters",
                    "opportunity": "Consumer sharing could create organic distribution",
                    "advantages": ["Easy-to-understand end-user value"],
                    "risks": ["Acquisition, moderation, and retention are unproven"],
                    "difficulty": "MEDIUM",
                    "startup_cost": "MEDIUM",
                    "validation_speed": "MEDIUM",
                    "reversibility": "MEDIUM",
                    "roadmap_effect": "Changes buyer, product experience, distribution, and metrics",
                    "assessment": "A real alternative, but not the current recommendation",
                },
            ]
            recorded = decision_module.record_candidates(
                str(root),
                owner=self.OWNER,
                activation_token=state["activation_token"],
                expected_state_sha=state["state_sha"],
                expected_strategy_sha=state["strategy_sha"],
                proposal_id="proposal-teststartup-ai-animation",
                candidates=candidates,
                recommendation_id="godot-sprite-pipeline",
                recommendation={
                    "candidate_id": "godot-sprite-pipeline",
                    "rationale": "It best fits one beginner Founder and a near-zero budget",
                    "why_now": "Demand and workflow fit can be tested before model investment",
                    "biggest_downside": "The initial Godot 2D niche may be small",
                    "choose_another_when": "Choose 3D only with materially more data, compute, and expertise",
                },
                evidence=["Bounded option scan compared four materially different wedges"],
                single_candidate_reason=None,
            )
            state = strategy_state(state, recorded)
            strategy = decision_module.inspect_strategy(str(root))["strategy"]
            self.assertEqual(strategy["direction"]["clarity"], "AMBIGUOUS")
            self.assertEqual(strategy["discovery"]["depth"], "STANDARD")
            self.assertEqual(strategy["gate"]["state"], "STRATEGIC_CHOICE_REQUIRED")
            self.assertEqual(strategy["project_phase"], "pre-bootstrap")
            self.assertEqual(len(strategy["discovery"]["candidates"]), 4)
            self.assertEqual(strategy["discovery"]["recommendation_id"], "godot-sprite-pipeline")
            self.assertFalse(
                any(row["status"] == "SELECTED" for row in strategy["discovery"]["candidates"])
            )
            for name in decision_module.CORE_LEDGERS:
                self.assertFalse((root / ".founder" / name).exists())
            self.assertFalse(
                decision_module.authorize_action(
                    str(root), action="candidate-bound-work"
                )["allowed"]
            )
            self.assertFalse(
                decision_module.authorize_action(str(root), action="bootstrap")["allowed"]
            )

    def test_v21_ambiguous_candidates_bounds_recommendation_selection_and_confirmation(self) -> None:
        for count in range(1, 6):
            self.assertEqual(len(decision_module._normalize_candidates(strategy_candidates(count))), count)
        with self.assertRaises(guard_module.InvalidState):
            decision_module._normalize_candidates([])
        with self.assertRaises(guard_module.InvalidState):
            decision_module._normalize_candidates(strategy_candidates(6))

        with tempfile.TemporaryDirectory(prefix="founder-os-v21-choice-") as temp:
            root = Path(temp)
            active = create_empty_active_project(root, self.OWNER)
            state = initialize_new_strategy(root, active, self.OWNER)
            initial_context = decision_module.inspect_strategy(str(root))["strategy"][
                "context_revision"
            ]
            state = self._assess_ambiguous(root, state)
            self.assertEqual(
                decision_module.inspect_strategy(str(root))["strategy"]["context_revision"],
                initial_context,
            )
            before = snapshot_tree(root)
            with self.assertRaises(guard_module.InvalidState):
                decision_module.record_candidates(
                    str(root),
                    owner=self.OWNER,
                    activation_token=state["activation_token"],
                    expected_state_sha=state["state_sha"],
                    expected_strategy_sha=state["strategy_sha"],
                    proposal_id="proposal-single",
                    candidates=strategy_candidates(1),
                    recommendation_id="direction-a",
                    recommendation=strategy_recommendation(),
                    evidence=["scan"],
                    single_candidate_reason=None,
                )
            self.assertEqual(before, snapshot_tree(root))
            state = self._record_candidates(root, state, proposal_id="proposal-ai-game")
            strategy = decision_module.inspect_strategy(str(root))["strategy"]
            self.assertEqual(strategy["gate"]["state"], "STRATEGIC_CHOICE_REQUIRED")
            self.assertEqual(strategy["discovery"]["recommendation_id"], "direction-a")
            self.assertEqual(
                set(strategy["discovery"]["recommendation"]),
                {
                    "candidate_id",
                    "rationale",
                    "why_now",
                    "biggest_downside",
                    "choose_another_when",
                },
            )
            pre_selection_context = strategy["context_revision"]
            state = self._select(
                root,
                state,
                proposal_id="proposal-ai-game",
                candidate_id="direction-b",
                authority="founder",
                decision_id="D-v21-founder-choice",
            )
            selected = decision_module.inspect_strategy(str(root))["strategy"]
            self.assertNotEqual(selected["context_revision"], pre_selection_context)
            self.assertEqual(selected["direction"]["selected_strategy_id"], "direction-b")
            self.assertEqual(
                [row["status"] for row in selected["discovery"]["candidates"]].count(
                    "SELECTED"
                ),
                1,
            )
            write_strategy_ledgers(root, decision=selected["decision_record"])
            state = checkpoint_external_changes(root, state, self.OWNER)
            confirmed = decision_module.confirm_canonical(
                str(root),
                owner=self.OWNER,
                activation_token=state["activation_token"],
                expected_state_sha=state["state_sha"],
                expected_strategy_sha=state["strategy_sha"],
                evidence="Proposal-bound L2 decision is canonical",
            )
            state = strategy_state(state, confirmed)
            final = decision_module.inspect_strategy(str(root))["strategy"]
            self.assertEqual(final["project_phase"], "bootstrapped")
            self.assertEqual(final["gate"]["state"], "OPERATING")
            self.assertEqual(final["decision_record"]["status"], "confirmed")

    def test_v21_revised_gate_rejects_proposal_and_reply_replay_with_zero_writes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v21-replay-") as temp:
            root = Path(temp)
            active = create_empty_active_project(root, self.OWNER)
            state = self._assess_ambiguous(
                root, initialize_new_strategy(root, active, self.OWNER)
            )
            state = self._record_candidates(root, state, proposal_id="proposal-old")
            revised = decision_module.revise_discovery(
                str(root),
                owner=self.OWNER,
                activation_token=state["activation_token"],
                expected_state_sha=state["state_sha"],
                expected_strategy_sha=state["strategy_sha"],
                proposal_id="proposal-old",
                reason="Founder changed the intended product boundary",
                depth="LIGHT",
            )
            state = strategy_state(state, revised)
            before = snapshot_tree(root)
            with self.assertRaises(guard_module.Conflict):
                self._record_candidates(root, state, proposal_id="proposal-old")
            self.assertEqual(before, snapshot_tree(root))
            state = self._record_candidates(root, state, proposal_id="proposal-current")
            before = snapshot_tree(root)
            with self.assertRaises(guard_module.Conflict):
                self._select(
                    root,
                    state,
                    proposal_id="proposal-old",
                    candidate_id="direction-a",
                    authority="founder",
                    decision_id="D-stale-reply",
                )
            self.assertEqual(before, snapshot_tree(root))
            history = decision_module.inspect_strategy(str(root))["strategy"][
                "discovery_history"
            ]
            self.assertEqual(history[0]["proposal_id"], "proposal-old")
            self.assertEqual(history[0]["disposition"], "revised")

    def test_v21_delegated_choice_uses_recommendation_without_changing_profile(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v21-delegated-") as temp:
            root = Path(temp)
            active = create_empty_active_project(root, self.OWNER)
            state = self._record_candidates(
                root,
                self._assess_ambiguous(
                    root, initialize_new_strategy(root, active, self.OWNER)
                ),
                proposal_id="proposal-delegated",
            )
            before_profile = copy.deepcopy(
                decision_module.inspect_strategy(str(root))["strategy"]["autonomy_profile"]
            )
            before = snapshot_tree(root)
            with self.assertRaises(guard_module.Conflict):
                self._select(
                    root,
                    state,
                    proposal_id="proposal-delegated",
                    candidate_id="direction-b",
                    authority="delegated",
                    decision_id="D-invalid-delegation",
                )
            self.assertEqual(before, snapshot_tree(root))
            state = self._select(
                root,
                state,
                proposal_id="proposal-delegated",
                candidate_id="direction-a",
                authority="delegated",
                decision_id="D-current-delegation",
            )
            strategy = decision_module.inspect_strategy(str(root))["strategy"]
            self.assertEqual(strategy["direction"]["selection_authority"], "delegated")
            self.assertEqual(strategy["autonomy_profile"], before_profile)

    def test_v21_autonomous_l2_requires_record_and_boss_report_but_never_weakens_l3(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v21-autonomy-") as temp:
            root = Path(temp)
            state = make_operating_clear_project(root, self.OWNER)
            old_context = decision_module.inspect_strategy(str(root))["strategy"][
                "context_revision"
            ]
            updated = decision_module.update_autonomy(
                str(root),
                owner=self.OWNER,
                activation_token=state["activation_token"],
                expected_state_sha=state["state_sha"],
                expected_strategy_sha=state["strategy_sha"],
                strategic="autonomous_with_report",
                authorization_ref="Founder explicitly delegated future L2 choices for this project",
            )
            state = strategy_state(state, updated)
            profile = decision_module.inspect_strategy(str(root))["strategy"][
                "autonomy_profile"
            ]
            self.assertEqual(profile["strategic"], "autonomous_with_report")
            self.assertEqual(profile["executive"], "require_explicit_approval")
            self.assertNotEqual(
                decision_module.inspect_strategy(str(root))["strategy"]["context_revision"],
                old_context,
            )
            pivot = decision_module.open_pivot(
                str(root),
                owner=self.OWNER,
                activation_token=state["activation_token"],
                expected_state_sha=state["state_sha"],
                expected_strategy_sha=state["strategy_sha"],
                proposal_id="proposal-autonomous-pivot",
                summary="New evidence creates a material product-direction choice",
                candidates=strategy_candidates(3),
                recommendation_id="direction-a",
                recommendation=strategy_recommendation(),
                evidence=["Customer evidence changed"],
                affected_agent_ids=[],
                single_candidate_reason=None,
            )
            state = strategy_state(state, pivot)
            state = self._select(
                root,
                state,
                proposal_id="proposal-autonomous-pivot",
                candidate_id="direction-b",
                authority="autonomy",
                decision_id="D-autonomous-l2",
            )
            selected = decision_module.inspect_strategy(str(root))["strategy"]
            self.assertIn("D-autonomous-l2", selected["reporting"]["pending_decision_ids"])
            write_strategy_ledgers(
                root,
                decision=selected["decision_record"],
                status_decision_ids=["D-autonomous-l2"],
            )
            state = checkpoint_external_changes(root, state, self.OWNER)
            confirmed = decision_module.confirm_canonical(
                str(root),
                owner=self.OWNER,
                activation_token=state["activation_token"],
                expected_state_sha=state["state_sha"],
                expected_strategy_sha=state["strategy_sha"],
                evidence="Autonomous L2 decision is canonical",
            )
            state = strategy_state(state, confirmed)
            reported = decision_module.mark_reported(
                str(root),
                owner=self.OWNER,
                activation_token=state["activation_token"],
                expected_state_sha=state["state_sha"],
                expected_strategy_sha=state["strategy_sha"],
                decision_id="D-autonomous-l2",
                delivery_ref="Boss summary delivered in the current FounderOS turn",
            )
            state = strategy_state(state, reported)
            final = decision_module.inspect_strategy(str(root))["strategy"]
            self.assertEqual(final["reporting"]["pending_decision_ids"], [])
            self.assertEqual(final["autonomy_profile"]["executive"], "require_explicit_approval")

    def test_v21_l3_requires_exact_explicit_approval_and_action_scope(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v21-l3-") as temp:
            root = Path(temp)
            state = make_operating_clear_project(root, self.OWNER)
            opened = decision_module.open_executive_gate(
                str(root),
                owner=self.OWNER,
                activation_token=state["activation_token"],
                expected_state_sha=state["state_sha"],
                expected_strategy_sha=state["strategy_sha"],
                proposal_id="proposal-public-launch",
                summary="Publish the product under the Founder's real account",
                action_scope="publish release v1 to the public store",
            )
            state = strategy_state(state, opened)
            self.assertFalse(
                decision_module.authorize_action(
                    str(root),
                    action="executive-action",
                    proposal_id="proposal-public-launch",
                    decision_id="D-public-launch",
                    action_scope="publish release v1 to the public store",
                )["allowed"]
            )
            approved = decision_module.approve_executive(
                str(root),
                owner=self.OWNER,
                activation_token=state["activation_token"],
                expected_state_sha=state["state_sha"],
                expected_strategy_sha=state["strategy_sha"],
                proposal_id="proposal-public-launch",
                authorization_ref="Founder explicitly approved this exact public launch",
                decision_id="D-public-launch",
            )
            state = strategy_state(state, approved)
            pending = decision_module.inspect_strategy(str(root))["strategy"]
            self.assertFalse(
                decision_module.authorize_action(
                    str(root),
                    action="executive-action",
                    proposal_id="proposal-public-launch",
                    decision_id="D-public-launch",
                    action_scope="publish release v1 to the public store",
                )["allowed"]
            )
            write_strategy_ledgers(root, decision=pending["decision_record"])
            state = checkpoint_external_changes(root, state, self.OWNER)
            confirmed = decision_module.confirm_canonical(
                str(root),
                owner=self.OWNER,
                activation_token=state["activation_token"],
                expected_state_sha=state["state_sha"],
                expected_strategy_sha=state["strategy_sha"],
                evidence="Explicit L3 approval and action scope are canonical",
            )
            state = strategy_state(state, confirmed)
            exact = decision_module.authorize_action(
                str(root),
                action="executive-action",
                proposal_id="proposal-public-launch",
                decision_id="D-public-launch",
                action_scope="publish release v1 to the public store",
            )
            wrong_scope = decision_module.authorize_action(
                str(root),
                action="executive-action",
                proposal_id="proposal-public-launch",
                decision_id="D-public-launch",
                action_scope="publish release v2 to another store",
            )
            self.assertTrue(exact["allowed"])
            self.assertFalse(wrong_scope["allowed"])

    def test_v21_strategy_cas_binding_and_hardlink_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v21-security-") as temp:
            base = Path(temp)
            first = base / "first"
            first.mkdir()
            active = create_empty_active_project(first, self.OWNER)
            state = initialize_new_strategy(first, active, self.OWNER)
            before = snapshot_tree(first)
            with self.assertRaises(guard_module.Conflict):
                decision_module.assess_direction(
                    str(first),
                    owner=self.OWNER,
                    activation_token=state["activation_token"],
                    expected_state_sha=state["state_sha"],
                    expected_strategy_sha="ABSENT",
                    outcome="AMBIGUOUS",
                    reason="stale CAS must fail",
                    direction_summary="ambiguous idea",
                    depth="LIGHT",
                )
            self.assertEqual(before, snapshot_tree(first))

            second = base / "second"
            second.mkdir()
            second_active = create_empty_active_project(second, self.OWNER)
            initialize_new_strategy(second, second_active, self.OWNER)
            (second / ".founder" / "STRATEGY.json").write_bytes(
                (first / ".founder" / "STRATEGY.json").read_bytes()
            )
            with self.assertRaises(guard_module.InvalidState):
                decision_module.inspect_strategy(str(second))

            link = base / "strategy-hardlink-copy.json"
            try:
                os.link(first / ".founder" / "STRATEGY.json", link)
            except OSError as exc:
                self.skipTest(f"Hardlinks unavailable in validator filesystem: {exc}")
            with self.assertRaises(guard_module.InvalidState):
                decision_module.inspect_strategy(str(first))

    def test_v21_thread_gate_allows_only_explicit_discovery_readonly_task_agents(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v21-thread-gate-") as temp:
            root = Path(temp)
            active = create_empty_active_project(root, self.OWNER)
            state = self._assess_ambiguous(
                root, initialize_new_strategy(root, active, self.OWNER)
            )
            initialized = initialize_thread_registry(root, state, self.OWNER)
            registry_sha = initialized["registry_sha"]
            state["state_sha"] = initialized["state_sha"]
            before = snapshot_tree(root)
            with self.assertRaises(guard_module.Conflict):
                registry_module.reserve_thread(
                    str(root),
                    owner=self.OWNER,
                    activation_token=state["activation_token"],
                    expected_state_sha=state["state_sha"],
                    expected_registry_sha=registry_sha,
                    agent_id="premature-technical-lead",
                    agent_kind="persistent",
                    logical_name="Premature Technical Lead",
                    manager_agent_id="founder-os-main",
                    workstream="engineering",
                    thread_type="persistent",
                    read_scope=["src/**"],
                    write_scope=["src/**"],
                    skills=[],
                    dependencies=[],
                    strategy_scope="candidate-bound",
                )
            self.assertEqual(before, snapshot_tree(root))
            reserved = registry_module.reserve_thread(
                str(root),
                owner=self.OWNER,
                activation_token=state["activation_token"],
                expected_state_sha=state["state_sha"],
                expected_registry_sha=registry_sha,
                agent_id="discovery-research-task",
                agent_kind="task",
                logical_name="Discovery Research Task",
                manager_agent_id="founder-os-main",
                workstream="discovery",
                thread_type="task",
                read_scope=["docs/**"],
                write_scope=[],
                skills=[],
                dependencies=[],
                strategy_scope="discovery-read-only",
            )
            registry_sha = reserved["registry_sha"]
            state["state_sha"] = reserved["state_sha"]
            bound = registry_module.bind_runtime(
                str(root),
                owner=self.OWNER,
                activation_token=state["activation_token"],
                expected_state_sha=state["state_sha"],
                expected_registry_sha=registry_sha,
                thread_record_id=reserved["details"]["thread_record_id"],
                binding_nonce=reserved["details"]["binding_nonce"],
                runtime_thread_id="019ff012-v21-discovery-task",
                runtime_host_id="local",
                identity_quality="observed",
                strategy_scope="discovery-read-only",
            )
            registry_sha = bound["registry_sha"]
            state["state_sha"] = bound["state_sha"]
            thread_id = reserved["details"]["thread_record_id"]
            before = snapshot_tree(root)
            with self.assertRaises(guard_module.Conflict):
                registry_module.transition_thread(
                    str(root),
                    owner=self.OWNER,
                    activation_token=state["activation_token"],
                    expected_state_sha=state["state_sha"],
                    expected_registry_sha=registry_sha,
                    thread_record_id=thread_id,
                    target="WORKING",
                    evidence="attempt to bypass guarded assignment",
                )
            self.assertEqual(before, snapshot_tree(root))
            with self.assertRaises(guard_module.Conflict):
                registry_module.assign_task(
                    str(root),
                    owner=self.OWNER,
                    activation_token=state["activation_token"],
                    expected_state_sha=state["state_sha"],
                    expected_registry_sha=registry_sha,
                    thread_record_id=thread_id,
                    task_id="candidate-implementation",
                    summary="Implement one unselected candidate",
                    acceptance_ref="must remain blocked",
                    task_strategy_scope="candidate-bound",
                    task_write_scope=[],
                )
            self.assertEqual(before, snapshot_tree(root))
            assigned = registry_module.assign_task(
                str(root),
                owner=self.OWNER,
                activation_token=state["activation_token"],
                expected_state_sha=state["state_sha"],
                expected_registry_sha=registry_sha,
                thread_record_id=thread_id,
                task_id="bounded-discovery",
                summary="Perform a bounded read-only comparison",
                acceptance_ref="return evidence without project writes",
                task_strategy_scope="discovery-read-only",
                task_write_scope=[],
            )
            self.assertEqual(assigned["details"]["task_id"], "bounded-discovery")

    def test_v21_authorization_receipt_cannot_cross_proposals_or_authority_kinds(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v21-auth-replay-") as temp:
            root = Path(temp)
            active = create_empty_active_project(root, self.OWNER)
            state = self._record_candidates(
                root,
                self._assess_ambiguous(
                    root, initialize_new_strategy(root, active, self.OWNER)
                ),
                proposal_id="proposal-auth-first",
            )
            shared_ref = "Founder response turn-42 approving only proposal-auth-first"
            selected = decision_module.select_candidate(
                str(root),
                owner=self.OWNER,
                activation_token=state["activation_token"],
                expected_state_sha=state["state_sha"],
                expected_strategy_sha=state["strategy_sha"],
                proposal_id="proposal-auth-first",
                candidate_id="direction-a",
                authority="founder",
                authorization_ref=shared_ref,
                decision_id="D-auth-first",
                rationale="Founder selected the first proposal only",
                nonselected_status="DEFERRED",
            )
            state = strategy_state(state, selected)
            current = decision_module.inspect_strategy(str(root))["strategy"]
            write_strategy_ledgers(root, decision=current["decision_record"])
            state = checkpoint_external_changes(root, state, self.OWNER)
            state = strategy_state(
                state,
                decision_module.confirm_canonical(
                    str(root),
                    owner=self.OWNER,
                    activation_token=state["activation_token"],
                    expected_state_sha=state["state_sha"],
                    expected_strategy_sha=state["strategy_sha"],
                    evidence="First proposal selection is canonical",
                ),
            )
            state = strategy_state(
                state,
                decision_module.open_pivot(
                    str(root),
                    owner=self.OWNER,
                    activation_token=state["activation_token"],
                    expected_state_sha=state["state_sha"],
                    expected_strategy_sha=state["strategy_sha"],
                    proposal_id="proposal-auth-second",
                    summary="A later and materially different direction proposal",
                    candidates=strategy_candidates(3),
                    recommendation_id="direction-a",
                    recommendation=strategy_recommendation(),
                    evidence=["Later evidence changed the direction choice"],
                    affected_agent_ids=[],
                    single_candidate_reason=None,
                ),
            )
            before = snapshot_tree(root)
            with self.assertRaises(guard_module.Conflict) as cross_proposal:
                decision_module.select_candidate(
                    str(root),
                    owner=self.OWNER,
                    activation_token=state["activation_token"],
                    expected_state_sha=state["state_sha"],
                    expected_strategy_sha=state["strategy_sha"],
                    proposal_id="proposal-auth-second",
                    candidate_id="direction-a",
                    authority="founder",
                    authorization_ref=shared_ref,
                    decision_id="D-auth-replayed",
                    rationale="An old response must not select a new proposal",
                    nonselected_status="DEFERRED",
                )
            self.assertIn("consumed", str(cross_proposal.exception))
            self.assertEqual(before, snapshot_tree(root))

            state = self._select(
                root,
                state,
                proposal_id="proposal-auth-second",
                candidate_id="direction-b",
                authority="founder",
                decision_id="D-auth-second",
            )
            current = decision_module.inspect_strategy(str(root))["strategy"]
            write_strategy_ledgers(root, decision=current["decision_record"])
            state = checkpoint_external_changes(root, state, self.OWNER)
            state = strategy_state(
                state,
                decision_module.confirm_canonical(
                    str(root),
                    owner=self.OWNER,
                    activation_token=state["activation_token"],
                    expected_state_sha=state["state_sha"],
                    expected_strategy_sha=state["strategy_sha"],
                    evidence="Second proposal selection is canonical",
                ),
            )
            before = snapshot_tree(root)
            with self.assertRaises(guard_module.Conflict) as autonomy_reuse:
                decision_module.update_autonomy(
                    str(root),
                    owner=self.OWNER,
                    activation_token=state["activation_token"],
                    expected_state_sha=state["state_sha"],
                    expected_strategy_sha=state["strategy_sha"],
                    strategic="autonomous_with_report",
                    authorization_ref=shared_ref,
                )
            self.assertIn("consumed", str(autonomy_reuse.exception))
            self.assertEqual(before, snapshot_tree(root))

            state = strategy_state(
                state,
                decision_module.open_executive_gate(
                    str(root),
                    owner=self.OWNER,
                    activation_token=state["activation_token"],
                    expected_state_sha=state["state_sha"],
                    expected_strategy_sha=state["strategy_sha"],
                    proposal_id="proposal-auth-executive",
                    summary="Make one high-impact external commitment",
                    action_scope="publish release v1 under the Founder account",
                ),
            )
            before = snapshot_tree(root)
            with self.assertRaises(guard_module.Conflict) as executive_reuse:
                decision_module.approve_executive(
                    str(root),
                    owner=self.OWNER,
                    activation_token=state["activation_token"],
                    expected_state_sha=state["state_sha"],
                    expected_strategy_sha=state["strategy_sha"],
                    proposal_id="proposal-auth-executive",
                    authorization_ref=shared_ref,
                    decision_id="D-auth-executive",
                )
            self.assertIn("consumed", str(executive_reuse.exception))
            self.assertEqual(before, snapshot_tree(root))

    def test_v21_consumed_l3_approval_blocks_replay_preflight(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v21-l3-consume-") as temp:
            root = Path(temp)
            state = make_operating_clear_project(root, self.OWNER)
            state = strategy_state(
                state,
                decision_module.open_executive_gate(
                    str(root),
                    owner=self.OWNER,
                    activation_token=state["activation_token"],
                    expected_state_sha=state["state_sha"],
                    expected_strategy_sha=state["strategy_sha"],
                    proposal_id="proposal-one-time-launch",
                    summary="Publish exactly one approved release",
                    action_scope="publish release v1 to the public store",
                ),
            )
            state = strategy_state(
                state,
                decision_module.approve_executive(
                    str(root),
                    owner=self.OWNER,
                    activation_token=state["activation_token"],
                    expected_state_sha=state["state_sha"],
                    expected_strategy_sha=state["strategy_sha"],
                    proposal_id="proposal-one-time-launch",
                    authorization_ref="Founder approved this one launch in the current turn",
                    decision_id="D-one-time-launch",
                ),
            )
            current = decision_module.inspect_strategy(str(root))["strategy"]
            write_strategy_ledgers(root, decision=current["decision_record"])
            state = checkpoint_external_changes(root, state, self.OWNER)
            state = strategy_state(
                state,
                decision_module.confirm_canonical(
                    str(root),
                    owner=self.OWNER,
                    activation_token=state["activation_token"],
                    expected_state_sha=state["state_sha"],
                    expected_strategy_sha=state["strategy_sha"],
                    evidence="The one-time L3 approval is canonical",
                ),
            )
            exact = {
                "action": "executive-action",
                "proposal_id": "proposal-one-time-launch",
                "decision_id": "D-one-time-launch",
                "action_scope": "publish release v1 to the public store",
            }
            self.assertTrue(decision_module.authorize_action(str(root), **exact)["allowed"])
            consumed = decision_module.consume_executive(
                str(root),
                owner=self.OWNER,
                activation_token=state["activation_token"],
                expected_state_sha=state["state_sha"],
                expected_strategy_sha=state["strategy_sha"],
                proposal_id="proposal-one-time-launch",
                decision_id="D-one-time-launch",
                action_scope="publish release v1 to the public store",
                execution_ref="public-store operation receipt launch-0001",
            )
            state = strategy_state(state, consumed)
            self.assertFalse(decision_module.authorize_action(str(root), **exact)["allowed"])
            decision = decision_module.inspect_strategy(str(root))["strategy"]["decision_record"]
            self.assertEqual(decision["action_status"], "consumed")
            self.assertEqual(decision["execution_ref"], "public-store operation receipt launch-0001")
            before = snapshot_tree(root)
            with self.assertRaises(guard_module.Conflict):
                decision_module.consume_executive(
                    str(root),
                    owner=self.OWNER,
                    activation_token=state["activation_token"],
                    expected_state_sha=state["state_sha"],
                    expected_strategy_sha=state["strategy_sha"],
                    proposal_id="proposal-one-time-launch",
                    decision_id="D-one-time-launch",
                    action_scope="publish release v1 to the public store",
                    execution_ref="attempted replay receipt launch-0002",
                )
            self.assertEqual(before, snapshot_tree(root))

    def test_v21_thread_operations_require_strategy_for_new_partial_and_legacy_projects(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v21-thread-strategy-") as temp:
            base = Path(temp)
            zero = base / "zero-ledgers"
            zero.mkdir()
            zero_active = create_empty_active_project(zero, self.OWNER)
            partial = base / "partial-ledgers"
            partial.mkdir()
            partial_founder = partial / ".founder"
            partial_founder.mkdir()
            (partial_founder / "PROJECT.md").write_text(
                "# Project\n\n- Last revision: R-partial-thread-test\n",
                encoding="utf-8",
            )
            partial_completed, partial_active = claim(partial, self.OWNER)
            self.assertEqual(partial_completed.returncode, 0, partial_completed.stdout)
            legacy = base / "legacy-five-ledgers"
            legacy.mkdir()
            legacy_active = create_active_project(legacy, self.OWNER)

            init_cases = (
                (zero, zero_active, "Initialize Strategy"),
                (partial, partial_active, "RECOVERY_REQUIRED"),
                (legacy, legacy_active, "LEGACY_MIGRATION_REQUIRED"),
            )
            for project_root, active, expected_reason in init_cases:
                with self.subTest(operation="initialize", project=project_root.name):
                    before = snapshot_tree(project_root)
                    with self.assertRaises(guard_module.Conflict) as denied:
                        registry_module.initialize_registry(
                            str(project_root),
                            owner=self.OWNER,
                            activation_token=active["activation_token"],
                            expected_state_sha=active["state_sha"],
                            expected_registry_sha="ABSENT",
                        )
                    self.assertIn(expected_reason, str(denied.exception))
                    self.assertEqual(before, snapshot_tree(project_root))
                for operation in ("reserve", "assign"):
                    with self.subTest(operation=operation, project=project_root.name):
                        before = snapshot_tree(project_root)
                        with self.assertRaises(guard_module.Conflict) as fenced:
                            decision_module.enforce_thread_action(
                                project_root / ".founder",
                                operation=operation,
                                strategy_scope="candidate-bound",
                                thread_type="persistent",
                                agent_kind="persistent",
                                effective_write_scope=["src/**"],
                            )
                        self.assertIn(expected_reason, str(fenced.exception))
                        self.assertEqual(before, snapshot_tree(project_root))

            prepared = base / "legacy-with-registry"
            prepared.mkdir()
            state = create_legacy_operating_project(prepared, self.OWNER)
            state, registry_sha, thread_record_id = self._bind_persistent_thread(
                prepared,
                state,
                agent_id="legacy-registry-agent",
                runtime_thread_id="019ff012-v21-no-strategy",
            )
            (prepared / ".founder" / "STRATEGY.json").unlink()
            checkpoint = guard_module.checkpoint_active(
                str(prepared),
                owner=self.OWNER,
                activation_token=state["activation_token"],
                expected_state_sha=state["state_sha"],
            )
            state["state_sha"] = checkpoint["state_sha"]
            before = snapshot_tree(prepared)
            with self.assertRaises(guard_module.Conflict) as reserve_denied:
                registry_module.reserve_thread(
                    str(prepared),
                    owner=self.OWNER,
                    activation_token=state["activation_token"],
                    expected_state_sha=state["state_sha"],
                    expected_registry_sha=registry_sha,
                    agent_id="legacy-second-agent",
                    agent_kind="persistent",
                    logical_name="Legacy Second Agent",
                    manager_agent_id="founder-os-main",
                    workstream="engineering",
                    thread_type="persistent",
                    read_scope=["src/**"],
                    write_scope=["src/**"],
                    skills=[],
                    dependencies=[],
                )
            self.assertIn("LEGACY_MIGRATION_REQUIRED", str(reserve_denied.exception))
            self.assertEqual(before, snapshot_tree(prepared))
            with self.assertRaises(guard_module.Conflict) as assign_denied:
                registry_module.assign_task(
                    str(prepared),
                    owner=self.OWNER,
                    activation_token=state["activation_token"],
                    expected_state_sha=state["state_sha"],
                    expected_registry_sha=registry_sha,
                    thread_record_id=thread_record_id,
                    task_id="legacy-no-strategy-task",
                    summary="This task must wait for Strategy migration",
                    acceptance_ref="LEGACY_MIGRATION_REQUIRED",
                )
            self.assertIn("LEGACY_MIGRATION_REQUIRED", str(assign_denied.exception))
            self.assertEqual(before, snapshot_tree(prepared))

    def test_v21_pivot_old_work_may_return_but_never_be_accepted(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v21-pivot-return-") as temp:
            root = Path(temp)
            state = make_operating_clear_project(root, self.OWNER)
            state, registry_sha, thread_record_id = self._bind_persistent_thread(
                root,
                state,
                agent_id="pivot-return-agent",
                runtime_thread_id="019ff012-v21-pivot-return",
            )
            assigned = registry_module.assign_task(
                str(root),
                owner=self.OWNER,
                activation_token=state["activation_token"],
                expected_state_sha=state["state_sha"],
                expected_registry_sha=registry_sha,
                thread_record_id=thread_record_id,
                task_id="old-direction-task",
                summary="Implement the old candidate-bound direction",
                acceptance_ref="Old-direction acceptance criteria",
                task_strategy_scope="candidate-bound",
            )
            state["state_sha"] = assigned["state_sha"]
            registry_sha = assigned["registry_sha"]
            state = strategy_state(
                state,
                decision_module.open_pivot(
                    str(root),
                    owner=self.OWNER,
                    activation_token=state["activation_token"],
                    expected_state_sha=state["state_sha"],
                    expected_strategy_sha=state["strategy_sha"],
                    proposal_id="proposal-old-work-pivot",
                    summary="Old candidate-bound work may no longer fit",
                    candidates=strategy_candidates(3),
                    recommendation_id="direction-a",
                    recommendation=strategy_recommendation(),
                    evidence=["A strategic assumption changed while work was running"],
                    affected_agent_ids=["pivot-return-agent"],
                    single_candidate_reason=None,
                ),
            )
            returned = registry_module.transition_thread(
                str(root),
                owner=self.OWNER,
                activation_token=state["activation_token"],
                expected_state_sha=state["state_sha"],
                expected_registry_sha=registry_sha,
                thread_record_id=thread_record_id,
                target="COMPLETED",
                evidence="Runtime returned the old-direction artifact after the pivot opened",
            )
            state["state_sha"] = returned["state_sha"]
            registry_sha = returned["registry_sha"]
            before = snapshot_tree(root)
            with self.assertRaises(guard_module.Conflict):
                registry_module.transition_thread(
                    str(root),
                    owner=self.OWNER,
                    activation_token=state["activation_token"],
                    expected_state_sha=state["state_sha"],
                    expected_registry_sha=registry_sha,
                    thread_record_id=thread_record_id,
                    target="WAITING",
                    evidence="Must not accept an old-direction result",
                )
            self.assertEqual(before, snapshot_tree(root))

            state = self._select(
                root,
                state,
                proposal_id="proposal-old-work-pivot",
                candidate_id="direction-a",
                authority="founder",
                decision_id="D-old-work-pivot",
            )
            current = decision_module.inspect_strategy(str(root))["strategy"]
            write_strategy_ledgers(root, decision=current["decision_record"])
            state = checkpoint_external_changes(root, state, self.OWNER)
            state = strategy_state(
                state,
                decision_module.confirm_canonical(
                    str(root),
                    owner=self.OWNER,
                    activation_token=state["activation_token"],
                    expected_state_sha=state["state_sha"],
                    expected_strategy_sha=state["strategy_sha"],
                    evidence="Pivot selection is canonical before STATE_SYNC",
                ),
            )
            synced = registry_module.state_sync(
                str(root),
                owner=self.OWNER,
                activation_token=state["activation_token"],
                expected_state_sha=state["state_sha"],
                expected_registry_sha=registry_sha,
                thread_record_id=thread_record_id,
                acknowledgement=self._state_sync_ack(root, thread_record_id),
            )
            state["state_sha"] = synced["state_sha"]
            registry_sha = synced["registry_sha"]
            state = strategy_state(
                state,
                decision_module.complete_state_sync(
                    str(root),
                    owner=self.OWNER,
                    activation_token=state["activation_token"],
                    expected_state_sha=state["state_sha"],
                    expected_strategy_sha=state["strategy_sha"],
                ),
            )
            before = snapshot_tree(root)
            with self.assertRaises(guard_module.Conflict) as stale_result:
                registry_module.transition_thread(
                    str(root),
                    owner=self.OWNER,
                    activation_token=state["activation_token"],
                    expected_state_sha=state["state_sha"],
                    expected_registry_sha=registry_sha,
                    thread_record_id=thread_record_id,
                    target="WAITING",
                    evidence="STATE_SYNC must not bless the old task result",
                )
            self.assertIn("accepted", str(stale_result.exception))
            self.assertEqual(before, snapshot_tree(root))
            registry = registry_module.inspect_registry(str(root))["registry"]
            thread = next(
                row for row in registry["threads"] if row["thread_record_id"] == thread_record_id
            )
            self.assertEqual(thread["lifecycle_state"], "COMPLETED")
            self.assertEqual(thread["current_task"]["disposition"], "superseded-by-strategy")

    def test_v21_interrupted_old_task_can_be_superseded_then_state_synced(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v21-interrupt-sync-") as temp:
            root = Path(temp)
            state = make_operating_clear_project(root, self.OWNER)
            state, registry_sha, thread_record_id = self._bind_persistent_thread(
                root,
                state,
                agent_id="interrupted-pivot-agent",
                runtime_thread_id="019ff012-v21-interrupted-pivot",
            )
            assigned = registry_module.assign_task(
                str(root),
                owner=self.OWNER,
                activation_token=state["activation_token"],
                expected_state_sha=state["state_sha"],
                expected_registry_sha=registry_sha,
                thread_record_id=thread_record_id,
                task_id="old-task-to-interrupt",
                summary="Execute work under the direction that is about to change",
                acceptance_ref="Old-direction criteria",
                task_strategy_scope="candidate-bound",
            )
            state["state_sha"] = assigned["state_sha"]
            registry_sha = assigned["registry_sha"]
            state = strategy_state(
                state,
                decision_module.open_pivot(
                    str(root),
                    owner=self.OWNER,
                    activation_token=state["activation_token"],
                    expected_state_sha=state["state_sha"],
                    expected_strategy_sha=state["strategy_sha"],
                    proposal_id="proposal-interrupted-pivot",
                    summary="The current task must stop before the new direction is selected",
                    candidates=strategy_candidates(3),
                    recommendation_id="direction-a",
                    recommendation=strategy_recommendation(),
                    evidence=["The pending task is candidate-bound"],
                    affected_agent_ids=["interrupted-pivot-agent"],
                    single_candidate_reason=None,
                ),
            )
            interrupted = registry_module.transition_thread(
                str(root),
                owner=self.OWNER,
                activation_token=state["activation_token"],
                expected_state_sha=state["state_sha"],
                expected_registry_sha=registry_sha,
                thread_record_id=thread_record_id,
                target="INTERRUPTED",
                evidence="Runtime confirmed the old task stopped with no active writes",
            )
            state["state_sha"] = interrupted["state_sha"]
            registry_sha = interrupted["registry_sha"]
            interrupted_registry = registry_module.inspect_registry(str(root))["registry"]
            interrupted_thread = registry_module._find_thread(
                interrupted_registry, thread_record_id
            )
            self.assertEqual(interrupted_thread["current_task"]["disposition"], "interrupted")

            state = self._select(
                root,
                state,
                proposal_id="proposal-interrupted-pivot",
                candidate_id="direction-a",
                authority="founder",
                decision_id="D-interrupted-pivot",
            )
            current = decision_module.inspect_strategy(str(root))["strategy"]
            write_strategy_ledgers(root, decision=current["decision_record"])
            state = checkpoint_external_changes(root, state, self.OWNER)
            state = strategy_state(
                state,
                decision_module.confirm_canonical(
                    str(root),
                    owner=self.OWNER,
                    activation_token=state["activation_token"],
                    expected_state_sha=state["state_sha"],
                    expected_strategy_sha=state["strategy_sha"],
                    evidence="Interrupted-task pivot is canonical before STATE_SYNC",
                ),
            )
            synced = registry_module.state_sync(
                str(root),
                owner=self.OWNER,
                activation_token=state["activation_token"],
                expected_state_sha=state["state_sha"],
                expected_registry_sha=registry_sha,
                thread_record_id=thread_record_id,
                acknowledgement=self._state_sync_ack(root, thread_record_id),
            )
            state["state_sha"] = synced["state_sha"]
            registry_sha = synced["registry_sha"]
            state = strategy_state(
                state,
                decision_module.complete_state_sync(
                    str(root),
                    owner=self.OWNER,
                    activation_token=state["activation_token"],
                    expected_state_sha=state["state_sha"],
                    expected_strategy_sha=state["strategy_sha"],
                ),
            )
            final_registry = registry_module.inspect_registry(str(root))["registry"]
            final_thread = registry_module._find_thread(final_registry, thread_record_id)
            self.assertEqual(final_thread["lifecycle_state"], "INTERRUPTED")
            self.assertEqual(
                final_thread["current_task"]["pre_state_sync_disposition"], "interrupted"
            )
            self.assertEqual(
                final_thread["current_task"]["disposition"], "superseded-by-strategy"
            )
            self.assertEqual(
                decision_module.inspect_strategy(str(root))["strategy"]["gate"]["state"],
                "OPERATING",
            )

    def test_v21_mark_reported_requires_confirmed_operating_structured_report(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v21-report-fence-") as temp:
            root = Path(temp)
            state = make_operating_clear_project(root, self.OWNER)
            state = strategy_state(
                state,
                decision_module.update_autonomy(
                    str(root),
                    owner=self.OWNER,
                    activation_token=state["activation_token"],
                    expected_state_sha=state["state_sha"],
                    expected_strategy_sha=state["strategy_sha"],
                    strategic="autonomous_with_report",
                    authorization_ref="Founder delegated project L2 decisions with reporting",
                ),
            )
            state = strategy_state(
                state,
                decision_module.open_pivot(
                    str(root),
                    owner=self.OWNER,
                    activation_token=state["activation_token"],
                    expected_state_sha=state["state_sha"],
                    expected_strategy_sha=state["strategy_sha"],
                    proposal_id="proposal-report-fence",
                    summary="Autonomous L2 choice that must be reported",
                    candidates=strategy_candidates(3),
                    recommendation_id="direction-a",
                    recommendation=strategy_recommendation(),
                    evidence=["Current evidence supports an autonomous decision"],
                    affected_agent_ids=[],
                    single_candidate_reason=None,
                ),
            )
            state = self._select(
                root,
                state,
                proposal_id="proposal-report-fence",
                candidate_id="direction-b",
                authority="autonomy",
                decision_id="D-report-fence",
            )
            current = decision_module.inspect_strategy(str(root))["strategy"]
            write_strategy_ledgers(
                root,
                decision=current["decision_record"],
                status_decision_ids=["D-report-fence"],
            )
            state = checkpoint_external_changes(root, state, self.OWNER)
            before = snapshot_tree(root)
            with self.assertRaises(guard_module.Conflict):
                decision_module.mark_reported(
                    str(root),
                    owner=self.OWNER,
                    activation_token=state["activation_token"],
                    expected_state_sha=state["state_sha"],
                    expected_strategy_sha=state["strategy_sha"],
                    decision_id="D-report-fence",
                    delivery_ref="premature boss summary",
                )
            self.assertEqual(before, snapshot_tree(root))

            state = strategy_state(
                state,
                decision_module.confirm_canonical(
                    str(root),
                    owner=self.OWNER,
                    activation_token=state["activation_token"],
                    expected_state_sha=state["state_sha"],
                    expected_strategy_sha=state["strategy_sha"],
                    evidence="Autonomous L2 decision is canonical",
                ),
            )
            (root / ".founder" / "STATUS.md").write_text(
                "# Status\n\n- Reconciled revision: R-incomplete-report\n\n"
                "## Autonomous Strategic Decision Report\n\n"
                "- Decision ID: D-report-fence\n",
                encoding="utf-8",
            )
            state = checkpoint_external_changes(root, state, self.OWNER)
            before = snapshot_tree(root)
            with self.assertRaises(guard_module.Conflict) as incomplete:
                decision_module.mark_reported(
                    str(root),
                    owner=self.OWNER,
                    activation_token=state["activation_token"],
                    expected_state_sha=state["state_sha"],
                    expected_strategy_sha=state["strategy_sha"],
                    decision_id="D-report-fence",
                    delivery_ref="incomplete boss summary",
                )
            self.assertIn("complete", str(incomplete.exception))
            self.assertEqual(before, snapshot_tree(root))

            confirmed = decision_module.inspect_strategy(str(root))["strategy"]
            write_strategy_ledgers(
                root,
                decision=confirmed["decision_record"],
                status_decision_ids=["D-report-fence"],
            )
            state = checkpoint_external_changes(root, state, self.OWNER)
            state = strategy_state(
                state,
                decision_module.open_pivot(
                    str(root),
                    owner=self.OWNER,
                    activation_token=state["activation_token"],
                    expected_state_sha=state["state_sha"],
                    expected_strategy_sha=state["strategy_sha"],
                    proposal_id="proposal-report-gate-open",
                    summary="A later Gate must block report closure",
                    candidates=strategy_candidates(3),
                    recommendation_id="direction-a",
                    recommendation=strategy_recommendation(),
                    evidence=["A new material direction question opened"],
                    affected_agent_ids=[],
                    single_candidate_reason=None,
                ),
            )
            before = snapshot_tree(root)
            with self.assertRaises(guard_module.Conflict):
                decision_module.mark_reported(
                    str(root),
                    owner=self.OWNER,
                    activation_token=state["activation_token"],
                    expected_state_sha=state["state_sha"],
                    expected_strategy_sha=state["strategy_sha"],
                    decision_id="D-report-fence",
                    delivery_ref="boss summary while another Gate is open",
                )
            self.assertEqual(before, snapshot_tree(root))

    def test_v21_strategy_lock_and_fingerprint_drift_block_all_preflights(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v21-control-fence-") as temp:
            base = Path(temp)
            locked = base / "locked"
            locked.mkdir()
            locked_state = make_operating_clear_project(locked, self.OWNER)
            locked_state, locked_registry_sha, locked_thread_id = self._bind_persistent_thread(
                locked,
                locked_state,
                agent_id="locked-control-agent",
                runtime_thread_id="019ff012-v21-control-lock",
            )
            lock_path = locked / ".founder" / decision_module.STRATEGY_LOCK_NAME
            lock_path.write_text(
                json.dumps(
                    {
                        "project_root": str(locked),
                        "owner": self.OWNER,
                        "nonce": "SL_deterministic-red-team-lock",
                        "expected_strategy_sha": locked_state["strategy_sha"],
                        "expected_supervisor_state_sha": locked_state["state_sha"],
                        "created_at": "2026-08-12T00:00:00Z",
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            before = snapshot_tree(locked)
            for action in ("subagent-dispatch", "integration"):
                with self.subTest(control="strategy-lock", action=action):
                    with self.assertRaises(guard_module.Conflict):
                        decision_module.authorize_action(str(locked), action=action)
                    self.assertEqual(before, snapshot_tree(locked))
            with self.assertRaises(guard_module.Conflict):
                registry_module.assign_task(
                    str(locked),
                    owner=self.OWNER,
                    activation_token=locked_state["activation_token"],
                    expected_state_sha=locked_state["state_sha"],
                    expected_registry_sha=locked_registry_sha,
                    thread_record_id=locked_thread_id,
                    task_id="must-not-dispatch-under-lock",
                    summary="A stranded Strategy transaction blocks dispatch",
                    acceptance_ref="No write is permitted",
                )
            self.assertEqual(before, snapshot_tree(locked))

            drifted = base / "drifted"
            drifted.mkdir()
            drifted_state = make_operating_clear_project(drifted, self.OWNER)
            drifted_state, drifted_registry_sha, drifted_thread_id = self._bind_persistent_thread(
                drifted,
                drifted_state,
                agent_id="drifted-control-agent",
                runtime_thread_id="019ff012-v21-control-drift",
            )
            strategy_path = drifted / ".founder" / "STRATEGY.json"
            strategy_path.write_bytes(strategy_path.read_bytes() + b"\n")
            before = snapshot_tree(drifted)
            for action in ("subagent-dispatch", "integration"):
                with self.subTest(control="fingerprint-drift", action=action):
                    with self.assertRaises(guard_module.Conflict):
                        decision_module.authorize_action(str(drifted), action=action)
                    self.assertEqual(before, snapshot_tree(drifted))
            with self.assertRaises(guard_module.Conflict):
                registry_module.assign_task(
                    str(drifted),
                    owner=self.OWNER,
                    activation_token=drifted_state["activation_token"],
                    expected_state_sha=drifted_state["state_sha"],
                    expected_registry_sha=drifted_registry_sha,
                    thread_record_id=drifted_thread_id,
                    task_id="must-not-dispatch-under-drift",
                    summary="Supervisor and Strategy fingerprints disagree",
                    acceptance_ref="No write is permitted",
                )
            self.assertEqual(before, snapshot_tree(drifted))

    def test_v21_autonomy_context_rotation_requires_current_persistent_agent_sync(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v21-autonomy-sync-") as temp:
            root = Path(temp)
            state = make_operating_clear_project(root, self.OWNER)
            state, registry_sha, thread_record_id = self._bind_persistent_thread(
                root,
                state,
                agent_id="autonomy-sync-agent",
                runtime_thread_id="019ff012-v21-autonomy-sync",
            )
            prior = decision_module.inspect_strategy(str(root))["strategy"]
            state = strategy_state(
                state,
                decision_module.update_autonomy(
                    str(root),
                    owner=self.OWNER,
                    activation_token=state["activation_token"],
                    expected_state_sha=state["state_sha"],
                    expected_strategy_sha=state["strategy_sha"],
                    strategic="autonomous_with_report",
                    authorization_ref="Founder changed L2 autonomy for this project now",
                ),
            )
            current = decision_module.inspect_strategy(str(root))["strategy"]
            self.assertNotEqual(prior["context_revision"], current["context_revision"])
            self.assertEqual(current["gate"]["state"], "STATE_SYNC_REQUIRED")
            self.assertEqual(current["gate"]["context"], "autonomy")
            self.assertEqual(
                [row["agent_id"] for row in current["pending_state_sync"]],
                ["autonomy-sync-agent"],
            )
            self.assertFalse(
                decision_module.authorize_action(str(root), action="integration")["allowed"]
            )
            before = snapshot_tree(root)
            with self.assertRaises(guard_module.Conflict) as disposition_bypass:
                decision_module.resolve_state_sync_disposition(
                    str(root),
                    owner=self.OWNER,
                    activation_token=state["activation_token"],
                    expected_state_sha=state["state_sha"],
                    expected_strategy_sha=state["strategy_sha"],
                    agent_id="autonomy-sync-agent",
                    disposition="not-applicable",
                    evidence="Attempt to skip profile synchronization for a live primary",
                )
            self.assertIn("current persistent primary", str(disposition_bypass.exception))
            self.assertEqual(before, snapshot_tree(root))
            with self.assertRaises(guard_module.Conflict):
                registry_module.assign_task(
                    str(root),
                    owner=self.OWNER,
                    activation_token=state["activation_token"],
                    expected_state_sha=state["state_sha"],
                    expected_registry_sha=registry_sha,
                    thread_record_id=thread_record_id,
                    task_id="pre-sync-autonomy-task",
                    summary="Must wait for Autonomy Profile STATE_SYNC",
                    acceptance_ref="Current Strategy context required",
                )
            self.assertEqual(before, snapshot_tree(root))
            synced = registry_module.state_sync(
                str(root),
                owner=self.OWNER,
                activation_token=state["activation_token"],
                expected_state_sha=state["state_sha"],
                expected_registry_sha=registry_sha,
                thread_record_id=thread_record_id,
                acknowledgement=self._state_sync_ack(root, thread_record_id),
            )
            state["state_sha"] = synced["state_sha"]
            registry_sha = synced["registry_sha"]
            state = strategy_state(
                state,
                decision_module.complete_state_sync(
                    str(root),
                    owner=self.OWNER,
                    activation_token=state["activation_token"],
                    expected_state_sha=state["state_sha"],
                    expected_strategy_sha=state["strategy_sha"],
                ),
            )
            self.assertEqual(
                decision_module.inspect_strategy(str(root))["strategy"]["gate"]["state"],
                "OPERATING",
            )
            assigned = registry_module.assign_task(
                str(root),
                owner=self.OWNER,
                activation_token=state["activation_token"],
                expected_state_sha=state["state_sha"],
                expected_registry_sha=registry_sha,
                thread_record_id=thread_record_id,
                task_id="post-sync-autonomy-task",
                summary="Dispatch under the synchronized Autonomy Profile",
                acceptance_ref="Current Strategy context required",
            )
            self.assertEqual(assigned["details"]["task_id"], "post-sync-autonomy-task")

    def test_v21_legacy_thread_baseline_becomes_stale_then_migrates_by_state_sync(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v21-legacy-thread-") as temp:
            root = Path(temp)
            active = create_legacy_operating_project(root, self.OWNER)
            initialized = initialize_thread_registry(root, active, self.OWNER)
            registry_sha = initialized["registry_sha"]
            state_sha = initialized["state_sha"]
            reserved = registry_module.reserve_thread(
                str(root),
                owner=self.OWNER,
                activation_token=active["activation_token"],
                expected_state_sha=state_sha,
                expected_registry_sha=registry_sha,
                agent_id="legacy-technical-lead",
                agent_kind="persistent",
                logical_name="Legacy Technical Lead",
                manager_agent_id="founder-os-main",
                workstream="engineering",
                thread_type="persistent",
                read_scope=["src/**"],
                write_scope=["src/**"],
                skills=[],
                dependencies=[],
            )
            registry_sha = reserved["registry_sha"]
            state_sha = reserved["state_sha"]
            bound = registry_module.bind_runtime(
                str(root),
                owner=self.OWNER,
                activation_token=active["activation_token"],
                expected_state_sha=state_sha,
                expected_registry_sha=registry_sha,
                thread_record_id=reserved["details"]["thread_record_id"],
                binding_nonce=reserved["details"]["binding_nonce"],
                runtime_thread_id="019ff012-v21-legacy-primary",
                runtime_host_id="local",
                identity_quality="observed",
            )
            registry_sha = bound["registry_sha"]
            state_sha = bound["state_sha"]
            updated = decision_module.update_autonomy(
                str(root),
                owner=self.OWNER,
                activation_token=active["activation_token"],
                expected_state_sha=state_sha,
                expected_strategy_sha=active["strategy_sha"],
                strategic="require_approval",
                authorization_ref="Founder changed the legacy project's L2 autonomy policy",
            )
            state_sha = updated["state_sha"]
            strategy_sha = updated["strategy_sha"]
            current = decision_module.inspect_strategy(str(root))["strategy"]
            self.assertEqual(current["gate"]["state"], "STATE_SYNC_REQUIRED")
            before = snapshot_tree(root)
            with self.assertRaises(guard_module.Conflict):
                registry_module.assign_task(
                    str(root),
                    owner=self.OWNER,
                    activation_token=active["activation_token"],
                    expected_state_sha=state_sha,
                    expected_registry_sha=registry_sha,
                    thread_record_id=reserved["details"]["thread_record_id"],
                    task_id="stale-legacy-task",
                    summary="Must not use the pre-Strategy baseline",
                    acceptance_ref="blocked until sync",
                )
            self.assertEqual(before, snapshot_tree(root))
            acknowledgement = exact_state_sync_ack(
                root, reserved["details"]["thread_record_id"]
            )
            synced = registry_module.state_sync(
                str(root),
                owner=self.OWNER,
                activation_token=active["activation_token"],
                expected_state_sha=state_sha,
                expected_registry_sha=registry_sha,
                thread_record_id=reserved["details"]["thread_record_id"],
                acknowledgement=acknowledgement,
            )
            registry_sha = synced["registry_sha"]
            state_sha = synced["state_sha"]
            completed = decision_module.complete_state_sync(
                str(root),
                owner=self.OWNER,
                activation_token=active["activation_token"],
                expected_state_sha=state_sha,
                expected_strategy_sha=strategy_sha,
            )
            state_sha = completed["state_sha"]
            assigned = registry_module.assign_task(
                str(root),
                owner=self.OWNER,
                activation_token=active["activation_token"],
                expected_state_sha=state_sha,
                expected_registry_sha=registry_sha,
                thread_record_id=reserved["details"]["thread_record_id"],
                task_id="current-legacy-task",
                summary="Continue under the migrated Strategy baseline",
                acceptance_ref="current context is required",
            )
            self.assertEqual(assigned["details"]["task_id"], "current-legacy-task")

    def test_v21_pivot_requires_exact_strategic_ack_before_state_sync_gate_clears(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v21-state-sync-") as temp:
            root = Path(temp)
            state = make_operating_clear_project(root, self.OWNER)
            initialized = initialize_thread_registry(root, state, self.OWNER)
            registry_sha = initialized["registry_sha"]
            state["state_sha"] = initialized["state_sha"]
            reserved = registry_module.reserve_thread(
                str(root),
                owner=self.OWNER,
                activation_token=state["activation_token"],
                expected_state_sha=state["state_sha"],
                expected_registry_sha=registry_sha,
                agent_id="pivot-technical-lead",
                agent_kind="persistent",
                logical_name="Pivot Technical Lead",
                manager_agent_id="founder-os-main",
                workstream="engineering",
                thread_type="persistent",
                read_scope=["src/**"],
                write_scope=["src/**"],
                skills=[],
                dependencies=[],
            )
            registry_sha = reserved["registry_sha"]
            state["state_sha"] = reserved["state_sha"]
            bound = registry_module.bind_runtime(
                str(root),
                owner=self.OWNER,
                activation_token=state["activation_token"],
                expected_state_sha=state["state_sha"],
                expected_registry_sha=registry_sha,
                thread_record_id=reserved["details"]["thread_record_id"],
                binding_nonce=reserved["details"]["binding_nonce"],
                runtime_thread_id="019ff012-v21-pivot-primary",
                runtime_host_id="local",
                identity_quality="observed",
            )
            registry_sha = bound["registry_sha"]
            state["state_sha"] = bound["state_sha"]
            pivot = decision_module.open_pivot(
                str(root),
                owner=self.OWNER,
                activation_token=state["activation_token"],
                expected_state_sha=state["state_sha"],
                expected_strategy_sha=state["strategy_sha"],
                proposal_id="proposal-persistent-pivot",
                summary="Consider a materially different product direction",
                candidates=strategy_candidates(3),
                recommendation_id="direction-a",
                recommendation=strategy_recommendation(),
                evidence=["Roadmap assumptions changed"],
                affected_agent_ids=["pivot-technical-lead"],
                single_candidate_reason=None,
            )
            state = strategy_state(state, pivot)
            state = self._select(
                root,
                state,
                proposal_id="proposal-persistent-pivot",
                candidate_id="direction-a",
                authority="founder",
                decision_id="D-persistent-pivot",
            )
            selected = decision_module.inspect_strategy(str(root))["strategy"]
            write_strategy_ledgers(root, decision=selected["decision_record"])
            state = checkpoint_external_changes(root, state, self.OWNER)
            confirmed = decision_module.confirm_canonical(
                str(root),
                owner=self.OWNER,
                activation_token=state["activation_token"],
                expected_state_sha=state["state_sha"],
                expected_strategy_sha=state["strategy_sha"],
                evidence="Pivot decision is canonical",
            )
            state = strategy_state(state, confirmed)
            self.assertEqual(
                decision_module.inspect_strategy(str(root))["strategy"]["gate"]["state"],
                "STATE_SYNC_REQUIRED",
            )
            before = snapshot_tree(root)
            with self.assertRaises(guard_module.Conflict):
                registry_module.state_sync(
                    str(root),
                    owner=self.OWNER,
                    activation_token=state["activation_token"],
                    expected_state_sha=state["state_sha"],
                    expected_registry_sha=registry_sha,
                    thread_record_id=reserved["details"]["thread_record_id"],
                    acknowledgement="I read the new direction",
                )
            self.assertEqual(before, snapshot_tree(root))
            acknowledgement = exact_state_sync_ack(
                root, reserved["details"]["thread_record_id"]
            )
            synced = registry_module.state_sync(
                str(root),
                owner=self.OWNER,
                activation_token=state["activation_token"],
                expected_state_sha=state["state_sha"],
                expected_registry_sha=registry_sha,
                thread_record_id=reserved["details"]["thread_record_id"],
                acknowledgement=acknowledgement,
            )
            registry_sha = synced["registry_sha"]
            state["state_sha"] = synced["state_sha"]
            completed = decision_module.complete_state_sync(
                str(root),
                owner=self.OWNER,
                activation_token=state["activation_token"],
                expected_state_sha=state["state_sha"],
                expected_strategy_sha=state["strategy_sha"],
            )
            state = strategy_state(state, completed)
            self.assertEqual(
                decision_module.inspect_strategy(str(root))["strategy"]["gate"]["state"],
                "OPERATING",
            )
            assigned = registry_module.assign_task(
                str(root),
                owner=self.OWNER,
                activation_token=state["activation_token"],
                expected_state_sha=state["state_sha"],
                expected_registry_sha=registry_sha,
                thread_record_id=reserved["details"]["thread_record_id"],
                task_id="post-pivot-task",
                summary="Continue under the selected strategic direction",
                acceptance_ref="current Strategy context required",
            )
            self.assertEqual(assigned["details"]["task_id"], "post-pivot-task")

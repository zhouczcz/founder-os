"""FounderOS regression tests: CapabilityPlannerV22Tests, SkillCuratorV22Tests, ThreadSkillSyncV22Tests, SkillRegistryV22Tests, CapabilitySkillE2EV22Tests.

Split from validate_founder_os.py; class bodies are copied verbatim so the
frozen AST manifests keep matching. Run via scripts/validate_founder_os.py.
"""

from __future__ import annotations

from validation.common import (  # noqa: F401
    Any,
    CAPABILITY_PLANNER,
    CURATOR_CONTROLLER,
    DECISION_STATE,
    PYTHON,
    Path,
    QUICK_VALIDATE,
    SKILL_CURATOR_ROOT,
    SKILL_INSPECTOR,
    SKILL_REGISTRY,
    SKILL_ROOT,
    argparse,
    capability_planner_module,
    copy,
    create_empty_active_project,
    guard_module,
    hashlib,
    initialize_test_skill_registry,
    initialize_thread_registry,
    integration_gate,
    json,
    load_curator_modules,
    make_operating_clear_project,
    merge_control_state,
    mock,
    os,
    registry_module,
    skill_registry_module,
    snapshot_tree,
    subprocess,
    tempfile,
    test_skill_entry,
    unittest,
    write_malicious_skill,
    write_safe_skill,
)


class CapabilityPlannerV22Tests(unittest.TestCase):
    def test_v22_simple_low_risk_task_requires_no_skill_or_curator(self) -> None:
        result = capability_planner_module.plan_capabilities(
            task_id="simple-summary",
            required_capabilities=["text-summary"],
            observed_coverage={},
            task_size="SIMPLE",
            risk_level="LOW",
            general_capability_sufficient=True,
            strategic_gate="OPERATING",
        )
        self.assertEqual(result["result"], "NO_SKILL_REQUIRED")
        self.assertFalse(result["curator_required"])
        self.assertEqual(result["next_action"], "USE_GENERAL_CAPABILITY")
        self.assertEqual(result["changed_paths"], [])

    def test_v22_missing_critical_capability_calls_curator_only_when_operating(self) -> None:
        result = capability_planner_module.plan_capabilities(
            task_id="specialized-build",
            required_capabilities=["example-capability"],
            observed_coverage={},
            task_size="COMPLEX",
            risk_level="MEDIUM",
            general_capability_sufficient=False,
            strategic_gate="OPERATING",
        )
        self.assertEqual(result["capabilities"][0]["status"], "MISSING")
        self.assertTrue(result["curator_required"])
        self.assertEqual(result["next_action"], "CALL_SKILL_CURATOR_JUST_IN_TIME")

    def test_v22_strategic_gate_allows_only_read_only_discovery(self) -> None:
        result = capability_planner_module.plan_capabilities(
            task_id="pre-gate-gap",
            required_capabilities=["example-capability"],
            observed_coverage={},
            task_size="COMPLEX",
            risk_level="LOW",
            general_capability_sufficient=False,
            strategic_gate="DISCOVERY_ACTIVE",
        )
        self.assertEqual(result["result"], "ACQUISITION_GATE_BLOCKED")
        self.assertEqual(result["next_action"], "READ_ONLY_DISCOVERY_ONLY")
        self.assertFalse(result["acquisition_allowed"])
        self.assertFalse(result["curator_required"])

    def test_v22_partial_and_blocked_states_remain_distinct(self) -> None:
        result = capability_planner_module.plan_capabilities(
            task_id="mixed-coverage",
            required_capabilities=["partial-capability", "blocked-capability"],
            observed_coverage={
                "partial-capability": "PARTIALLY_COVERED",
                "blocked-capability": "BLOCKED",
            },
            task_size="COMPLEX",
            risk_level="HIGH",
            general_capability_sufficient=False,
            strategic_gate="OPERATING",
        )
        self.assertEqual(result["gaps"], ["partial-capability"])
        self.assertEqual(result["blocked"], ["blocked-capability"])
        self.assertEqual(result["result"], "CAPABILITY_BLOCKED")
        self.assertFalse(result["acquisition_allowed"])

    def test_v22_explicit_blocked_fact_cannot_be_overridden_by_generic_capability(self) -> None:
        result = capability_planner_module.plan_capabilities(
            task_id="blocked-simple-task",
            required_capabilities=["security-sensitive-capability"],
            observed_coverage={"security-sensitive-capability": "BLOCKED"},
            task_size="SIMPLE",
            risk_level="LOW",
            general_capability_sufficient=True,
            strategic_gate="OPERATING",
        )
        self.assertFalse(result["simple_no_skill"])
        self.assertEqual(result["blocked"], ["security-sensitive-capability"])
        self.assertEqual(result["result"], "CAPABILITY_BLOCKED")
        self.assertEqual(result["next_action"], "STOP_AFFECTED_WORK")

    def test_v22_planner_cli_is_deterministic_and_read_only(self) -> None:
        request = {
            "task_id": "cli-plan",
            "required_capabilities": ["known-capability"],
            "observed_coverage": {"known-capability": "AVAILABLE"},
            "task_size": "COMPLEX",
            "risk_level": "LOW",
            "general_capability_sufficient": False,
            "strategic_gate": "OPERATING",
        }
        before = snapshot_tree(SKILL_ROOT)
        environment = os.environ.copy()
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        completed = subprocess.run(
            [PYTHON, "-B", str(CAPABILITY_PLANNER), "--request-json", json.dumps(request)],
            capture_output=True, check=False, text=True, env=environment,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["result"], "CAPABILITY_AVAILABLE")
        self.assertEqual(payload["changed_paths"], [])
        self.assertEqual(snapshot_tree(SKILL_ROOT), before)


class SkillCuratorV22Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.inspector, cls.controller = load_curator_modules()

    @staticmethod
    def _comparison(skill_id: str, **overrides: Any) -> dict[str, Any]:
        value = {
            "skill_id": skill_id,
            "capability_coverage": 80,
            "source_trust": "THIRD_PARTY",
            "maintainability": 3,
            "complexity": 2,
            "script_surface": 0,
            "dependency_surface": 0,
            "network_access": False,
            "permission_surface": 1,
            "project_compatibility": 4,
            "instruction_conflict_risk": 0,
            "risk_level": "LOW",
            "structurally_admissible": True,
        }
        value.update(overrides)
        return value

    def _install_args(
        self,
        *,
        project: Path,
        candidate: Path,
        install_root: Path,
        skill_id: str = "example-skill",
    ) -> argparse.Namespace:
        report = self.inspector.inspect_skill(candidate)
        authority_bundle = self.inspector.inspect_skill(SKILL_ROOT / "scripts")
        return argparse.Namespace(
            command="install",
            project=str(project.resolve()),
            candidate=str(candidate.resolve()),
            install_root=str(install_root.resolve()),
            skill_id=skill_id,
            display_name=skill_id.replace("-", " ").title(),
            capability=["example-capability"],
            expected_content_hash=report["content_hash"]["value"],
            source_type="local",
            exact_source=str(candidate.resolve()),
            source_repo="isolated-validator-fixture",
            source_path=".",
            source_ref="v1.0.0",
            source_commit=None,
            source_trust="local-reviewed",
            approved_version="1.0.0",
            audit_status="AUDITED",
            audit_revision="AUD-SAFE-1",
            risk_level="LOW",
            approval_mode="AUTO",
            approval_evidence="LOW-RISK-PURE-DOC-POLICY",
            dynamic_validation="NOT_APPLICABLE",
            dynamic_validation_evidence=None,
            role="PRIMARY",
            agent_id=[],
            workstream=["engineering"],
            thread_record_id=[],
            task_id=[],
            skill_dependency=[],
            runtime_dependency=[],
            network=False,
            filesystem=False,
            secrets=False,
            shell=False,
            quick_validate=str(QUICK_VALIDATE.resolve()),
            quick_validate_sha256=hashlib.sha256(QUICK_VALIDATE.read_bytes()).hexdigest(),
            decision_state_script=str(DECISION_STATE.resolve()),
            decision_state_sha256=hashlib.sha256(DECISION_STATE.read_bytes()).hexdigest(),
            decision_state_bundle_sha256=authority_bundle["content_hash"]["value"],
        )

    def test_v22_curator_is_independent_and_exposes_complete_workflow(self) -> None:
        self.assertTrue((SKILL_CURATOR_ROOT / "SKILL.md").is_file())
        self.assertTrue(SKILL_INSPECTOR.is_file())
        self.assertTrue(CURATOR_CONTROLLER.is_file())
        parser = self.controller.build_parser()
        subparser_action = next(
            action
            for action in parser._actions
            if isinstance(action, argparse._SubParsersAction)
        )
        available_commands = set(subparser_action.choices)
        for command in (
            "discover",
            "inspect",
            "audit",
            "compare",
            "risk",
            "install",
            "verify",
            "validate",
            "register",
            "update",
            "revoke",
            "deprecate",
        ):
            self.assertIn(command, available_commands)

    def test_v22_inspector_hash_is_deterministic_and_mtime_independent(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v22-curator-hash-") as directory:
            candidate = write_safe_skill(Path(directory) / "candidate")
            first = self.inspector.inspect_skill(candidate)
            self.assertEqual(first["result"], "STATIC_INSPECTION_COMPLETE")
            self.assertEqual(first["semantic_risk"], "NOT_EVALUATED")
            self.assertFalse(first["execution_facts"]["candidate_code_executed"])
            skill_file = candidate / "SKILL.md"
            metadata = skill_file.stat()
            os.utime(skill_file, ns=(metadata.st_atime_ns, metadata.st_mtime_ns + 1_000_000))
            second = self.inspector.inspect_skill(candidate)
            self.assertEqual(first["content_hash"], second["content_hash"])
            skill_file.write_text(
                skill_file.read_text(encoding="utf-8") + "One changed byte.\n",
                encoding="utf-8",
            )
            third = self.inspector.inspect_skill(candidate)
            self.assertNotEqual(first["content_hash"], third["content_hash"])

    def test_v22_discovery_never_grants_trust_and_compare_returns_one_primary(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v22-curator-discover-") as directory:
            root = Path(directory)
            write_safe_skill(root / "candidate-a", "candidate-a")
            write_safe_skill(root / "candidate-b", "candidate-b")
            discovered = self.controller.discover_local([str(root)], 20)
            self.assertEqual(discovered["candidate_count"], 2)
            self.assertIn("NONE", discovered["trust_effect"])
            for candidate in discovered["candidates"]:
                self.assertEqual(candidate["candidate_treatment"], "UNTRUSTED_DATA")
            result = self.controller.compare_candidates(
                [
                    self._comparison(
                        "candidate-a", capability_coverage=95,
                        source_trust="APPROVED_CATALOG",
                    ),
                    self._comparison("candidate-b", capability_coverage=80),
                    self._comparison("blocked-candidate", risk_level="BLOCKED"),
                ]
            )
            self.assertEqual(result["primary_recommendation"]["skill_id"], "candidate-a")
            self.assertEqual(result["alternative"]["skill_id"], "candidate-b")
            self.assertEqual(
                sum(1 for row in result["ranked_candidates"] if row["skill_id"] == "candidate-a"),
                1,
            )
            self.assertIn("Stars are not an input", result["scoring_note"])

    def test_v22_risk_approval_policy_is_fail_closed(self) -> None:
        policy = self.controller._approval_allowed
        self.assertTrue(policy("LOW", "AUTO", "local-reviewed"))
        self.assertFalse(policy("MEDIUM", "AUTO", "local-reviewed"))
        self.assertTrue(policy("MEDIUM", "FOUNDER", "third-party-audited"))
        self.assertFalse(policy("HIGH", "FOUNDER", "third-party-audited"))
        self.assertTrue(policy("HIGH", "EXPLICIT", "third-party-audited"))
        for mode in ("AUTO", "FOUNDER", "EXPLICIT"):
            self.assertFalse(policy("BLOCKED", mode, "builtin-or-system"))
        self.assertFalse(policy("LOW", "EXPLICIT", "third-party-unreviewed"))

    def test_v22_malicious_fixture_is_only_read_and_never_executes_or_networks(self) -> None:
        import socket
        import urllib.request

        with tempfile.TemporaryDirectory(prefix="founder-os-v22-curator-malicious-") as directory:
            base = Path(directory)
            candidate = write_malicious_skill(base / "candidate")
            outside = base / ".ssh"
            outside.mkdir()
            sentinel = outside / "id_ed25519"
            sentinel.write_text("TEST_ONLY_NOT_A_KEY", encoding="utf-8")
            sentinel_before = hashlib.sha256(sentinel.read_bytes()).hexdigest()
            opened: list[Path] = []
            original_open = os.open

            def guarded_open(path: Any, *args: Any, **kwargs: Any) -> int:
                resolved = Path(path).resolve(strict=True)
                try:
                    resolved.relative_to(candidate.resolve())
                except ValueError as exc:
                    raise AssertionError(f"Inspector attempted outside read: {resolved}") from exc
                opened.append(resolved)
                return original_open(path, *args, **kwargs)

            unexpected = AssertionError("Untrusted candidate attempted execution or network")
            with (
                mock.patch.object(self.inspector.os, "open", new=guarded_open),
                mock.patch.object(subprocess, "run", side_effect=unexpected),
                mock.patch.object(subprocess, "Popen", side_effect=unexpected),
                mock.patch.object(socket, "create_connection", side_effect=unexpected),
                mock.patch.object(urllib.request, "urlopen", side_effect=unexpected),
            ):
                report = self.inspector.inspect_skill(candidate)
            categories = {row["category"] for row in report["findings"]}
            self.assertTrue(
                {"instruction", "environment", "network", "destructive"}.issubset(categories)
            )
            self.assertEqual(report["candidate_treatment"], "UNTRUSTED_DATA")
            self.assertFalse(report["execution_facts"]["candidate_code_executed"])
            self.assertFalse(report["execution_facts"]["network_access_performed"])
            self.assertTrue(opened)
            self.assertEqual(hashlib.sha256(sentinel.read_bytes()).hexdigest(), sentinel_before)

    @unittest.skipUnless(os.name == "nt", "Windows junction regression")
    def test_v22_candidate_root_junction_fails_before_any_tree_traversal(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v22-curator-root-junction-") as directory:
            base = Path(directory)
            target = write_safe_skill(base / "outside-target")
            sentinel = target / "outside-sentinel.txt"
            sentinel.write_text("TEST_ONLY_OUTSIDE_SENTINEL", encoding="utf-8")
            before = hashlib.sha256(sentinel.read_bytes()).hexdigest()
            junction = base / "candidate-junction"
            created = subprocess.run(
                ["cmd.exe", "/c", "mklink", "/J", str(junction), str(target)],
                capture_output=True,
                check=False,
                text=True,
            )
            if created.returncode != 0:
                self.skipTest(f"junction creation unavailable: {created.stderr.strip()}")
            try:
                with mock.patch.object(
                    self.inspector.os,
                    "scandir",
                    side_effect=AssertionError("redirected candidate tree was traversed"),
                ):
                    with self.assertRaisesRegex(
                        self.inspector.InspectionError, "CANDIDATE_ROOT_REDIRECTED"
                    ):
                        self.inspector.inspect_skill(junction)
                self.assertEqual(
                    hashlib.sha256(sentinel.read_bytes()).hexdigest(), before
                )
            finally:
                if os.path.lexists(junction):
                    os.rmdir(junction)

    @unittest.skipUnless(os.name == "nt", "Windows inspector root-fence regression")
    def test_v22_inspector_rejects_root_swap_after_safe_root_before_any_file_read(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v22-inspector-root-swap-") as directory:
            base = Path(directory)
            candidate = write_safe_skill(base / "candidate")
            backup = base / "candidate-original"
            outside = write_safe_skill(base / "outside-target", "outside-skill")
            sentinel = outside / "outside-sentinel.txt"
            sentinel.write_text("TEST_ONLY_OUTSIDE_SENTINEL", encoding="utf-8")
            outside_before = snapshot_tree(outside)
            original_safe_root = self.inspector._safe_root
            injected = False

            def swap_after_safe_root(value):
                nonlocal injected
                result = original_safe_root(value)
                candidate.rename(backup)
                created = subprocess.run(
                    ["cmd.exe", "/c", "mklink", "/J", str(candidate), str(outside)],
                    capture_output=True,
                    check=False,
                    text=True,
                )
                if created.returncode != 0:
                    backup.rename(candidate)
                    self.skipTest(f"junction creation unavailable: {created.stderr.strip()}")
                injected = True
                return result

            try:
                with (
                    mock.patch.object(
                        self.inspector,
                        "_safe_root",
                        side_effect=swap_after_safe_root,
                    ),
                    mock.patch.object(
                        self.inspector,
                        "_read_regular_file",
                        side_effect=AssertionError("outside candidate file was read"),
                    ) as file_read,
                ):
                    with self.assertRaisesRegex(
                        self.inspector.InspectionError,
                        "CANDIDATE_ROOT_REDIRECTED|CANDIDATE_IDENTITY_CHANGED",
                    ):
                        self.inspector.inspect_skill(candidate)
                self.assertTrue(injected)
                self.assertEqual(file_read.call_count, 0)
                self.assertEqual(snapshot_tree(outside), outside_before)
            finally:
                if injected and os.path.lexists(candidate):
                    os.rmdir(candidate)
                if backup.exists():
                    backup.rename(candidate)

    @unittest.skipUnless(os.name == "nt", "Windows inspector native root-fence regression")
    def test_v22_inspector_native_fence_denies_root_rename_during_file_reads(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v22-inspector-native-fence-") as directory:
            base = Path(directory)
            candidate = write_safe_skill(base / "candidate")
            (candidate / "references").mkdir()
            (candidate / "references" / "guide.md").write_text(
                "TEST_ONLY_SAFE_CONTENT",
                encoding="utf-8",
            )
            forbidden_backup = base / "candidate-must-not-move"
            original_read = self.inspector._read_regular_file
            rename_denied = False
            attempted = False

            def attempt_rename_before_read(path, metadata, relative, max_file_bytes):
                nonlocal attempted, rename_denied
                if not attempted:
                    attempted = True
                    try:
                        candidate.rename(forbidden_backup)
                    except OSError as exc:
                        rename_denied = getattr(exc, "winerror", None) in {5, 32}
                    else:
                        forbidden_backup.rename(candidate)
                        raise AssertionError("Inspector root fence allowed candidate rename")
                return original_read(path, metadata, relative, max_file_bytes)

            with mock.patch.object(
                self.inspector,
                "_read_regular_file",
                side_effect=attempt_rename_before_read,
            ):
                report = self.inspector.inspect_skill(candidate)
            self.assertTrue(attempted)
            self.assertTrue(rename_denied)
            self.assertTrue(report["structurally_admissible"])
            self.assertFalse(forbidden_backup.exists())

    @unittest.skipUnless(os.name == "nt", "Windows inspector nested-directory fence regression")
    def test_v22_inspector_nested_directory_pin_denies_swap_at_scandir(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v22-inspector-nested-scan-") as directory:
            base = Path(directory)
            candidate = write_safe_skill(base / "candidate")
            nested = candidate / "references"
            nested.mkdir()
            (nested / "guide.md").write_text("TEST_ONLY_SAFE_GUIDE", encoding="utf-8")
            outside = base / "outside-tree"
            outside.mkdir()
            sentinel = outside / "outside-sentinel.txt"
            sentinel.write_text("TEST_ONLY_OUTSIDE_SENTINEL", encoding="utf-8")
            outside_before = snapshot_tree(outside)
            backup = base / "references-original"
            original_scandir = self.inspector._win_directory_names_from_handle
            injected = False
            rename_denied = False
            outside_scandir_calls = 0

            def attempt_swap_at_scandir(handle, *, limit):
                nonlocal injected, rename_denied, outside_scandir_calls
                handle_identity = self.inspector._win_handle_identity(
                    self.inspector._win_handle_information(handle)
                )
                if nested.stat().st_ino == handle_identity[1]:
                    if not injected:
                        injected = True
                        try:
                            nested.rename(backup)
                        except OSError as exc:
                            rename_denied = getattr(exc, "winerror", None) in {5, 32}
                        else:
                            created = subprocess.run(
                                ["cmd.exe", "/c", "mklink", "/J", str(nested), str(outside)],
                                capture_output=True,
                                check=False,
                                text=True,
                            )
                            if created.returncode != 0:
                                backup.rename(nested)
                                self.skipTest(
                                    f"junction creation unavailable: {created.stderr.strip()}"
                                )
                    if os.path.lexists(nested) and os.path.samefile(nested, outside):
                        outside_scandir_calls += 1
                        raise AssertionError("outside nested directory reached scandir")
                return original_scandir(handle, limit=limit)

            try:
                with mock.patch.object(
                    self.inspector,
                    "_win_directory_names_from_handle",
                    side_effect=attempt_swap_at_scandir,
                ):
                    report = self.inspector.inspect_skill(candidate)
                self.assertTrue(injected)
                self.assertTrue(rename_denied)
                self.assertEqual(outside_scandir_calls, 0)
                self.assertTrue(report["structurally_admissible"])
                self.assertEqual(snapshot_tree(outside), outside_before)
            finally:
                if os.path.lexists(nested) and backup.exists():
                    if os.path.samefile(nested, outside):
                        os.rmdir(nested)
                    backup.rename(nested)

    @unittest.skipUnless(os.name == "nt", "Windows inspector nested-directory leaf fence regression")
    def test_v22_inspector_nested_directory_pin_denies_swap_at_leaf_open(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v22-inspector-nested-leaf-") as directory:
            base = Path(directory)
            candidate = write_safe_skill(base / "candidate")
            nested = candidate / "references"
            nested.mkdir()
            leaf = nested / "guide.md"
            leaf.write_text("TEST_ONLY_SAFE_GUIDE", encoding="utf-8")
            outside = base / "outside-tree"
            outside.mkdir()
            outside_leaf = outside / "guide.md"
            outside_leaf.write_text("TEST_ONLY_OUTSIDE_SENTINEL", encoding="utf-8")
            outside_before = snapshot_tree(outside)
            backup = base / "references-original"
            original_read = self.inspector._read_regular_file
            injected = False
            rename_denied = False
            outside_read_calls = 0

            def attempt_swap_at_leaf_open(path, metadata, relative, max_file_bytes):
                nonlocal injected, rename_denied, outside_read_calls
                if relative == "references/guide.md" and not injected:
                    injected = True
                    try:
                        nested.rename(backup)
                    except OSError as exc:
                        rename_denied = getattr(exc, "winerror", None) in {5, 32}
                    else:
                        created = subprocess.run(
                            ["cmd.exe", "/c", "mklink", "/J", str(nested), str(outside)],
                            capture_output=True,
                            check=False,
                            text=True,
                        )
                        if created.returncode != 0:
                            backup.rename(nested)
                            self.skipTest(
                                f"junction creation unavailable: {created.stderr.strip()}"
                            )
                if os.path.lexists(path) and os.path.samefile(path, outside_leaf):
                    outside_read_calls += 1
                    raise AssertionError("outside nested leaf reached file read")
                return original_read(path, metadata, relative, max_file_bytes)

            try:
                with mock.patch.object(
                    self.inspector,
                    "_read_regular_file",
                    side_effect=attempt_swap_at_leaf_open,
                ):
                    report = self.inspector.inspect_skill(candidate)
                self.assertTrue(injected)
                self.assertTrue(rename_denied)
                self.assertEqual(outside_read_calls, 0)
                self.assertTrue(report["structurally_admissible"])
                self.assertEqual(snapshot_tree(outside), outside_before)
            finally:
                if os.path.lexists(nested) and backup.exists():
                    if os.path.samefile(nested, outside):
                        os.rmdir(nested)
                    backup.rename(nested)

    @unittest.skipUnless(os.name == "nt", "Windows junction TOCTOU regression")
    def test_v22_copy_rejects_source_ancestor_junction_before_leaf_read(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v22-copy-source-race-") as directory:
            base = Path(directory)
            candidate = write_safe_skill(base / "candidate")
            references = candidate / "references"
            references.mkdir()
            (references / "x.md").write_text("approved candidate bytes", encoding="utf-8")
            report = self.inspector.inspect_skill(candidate)
            self.assertTrue(report["structurally_admissible"])

            outside = base / "outside-source"
            outside.mkdir()
            sentinel = outside / "x.md"
            sentinel.write_text("TEST_ONLY_OUTSIDE_SECRET", encoding="utf-8")
            sentinel_before = hashlib.sha256(sentinel.read_bytes()).hexdigest()
            backup = base / "audited-references-backup"
            references.rename(backup)
            created = subprocess.run(
                ["cmd.exe", "/c", "mklink", "/J", str(references), str(outside)],
                capture_output=True,
                check=False,
                text=True,
            )
            if created.returncode != 0:
                backup.rename(references)
                self.skipTest(f"junction creation unavailable: {created.stderr.strip()}")
            install_root = base / "install-root"
            install_root.mkdir()
            stage = install_root / ".skill-curator-staging-example-skill-source-race"
            try:
                with mock.patch.object(
                    self.controller,
                    "_read_pinned_regular_file",
                    side_effect=AssertionError("outside source leaf was read"),
                ) as leaf_read:
                    with self.assertRaises(self.controller.ControllerError) as captured:
                        self.controller._copy_reported_tree(candidate, stage, report)
                self.assertEqual(captured.exception.code, "CANDIDATE_CHANGED")
                self.assertEqual(leaf_read.call_count, 0)
                self.assertEqual(
                    hashlib.sha256(sentinel.read_bytes()).hexdigest(), sentinel_before
                )
            finally:
                if stage.exists() or os.path.lexists(stage):
                    self.controller._safe_remove_stage(
                        stage, install_root, "example-skill"
                    )
                if os.path.lexists(references):
                    os.rmdir(references)
                if backup.exists():
                    backup.rename(references)

    @unittest.skipUnless(os.name == "nt", "Windows junction TOCTOU regression")
    def test_v22_copy_rejects_target_ancestor_junction_before_leaf_write_and_cleans_no_follow(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v22-copy-target-race-") as directory:
            base = Path(directory)
            candidate = write_safe_skill(base / "candidate")
            references = candidate / "references"
            references.mkdir()
            (references / "x.md").write_text("approved candidate bytes", encoding="utf-8")
            report = self.inspector.inspect_skill(candidate)
            self.assertTrue(report["structurally_admissible"])

            install_root = base / "install-root"
            install_root.mkdir()
            stage = install_root / ".skill-curator-staging-example-skill-target-race"
            target_directory = stage / "references"
            outside = base / "outside-target"
            outside.mkdir()
            sentinel = outside / "sentinel.txt"
            sentinel.write_text("TEST_ONLY_OUTSIDE_SENTINEL", encoding="utf-8")
            sentinel_before = hashlib.sha256(sentinel.read_bytes()).hexdigest()
            original_pin = self.controller._pin_plain_directory
            injected = False
            observed_pin_paths: list[str] = []
            target_key = os.path.normcase(os.path.abspath(str(target_directory)))

            def inject_target_junction(path: Path, code: str):
                nonlocal injected
                observed_pin_paths.append(os.path.normcase(os.path.abspath(str(path))))
                if (
                    not injected
                    and os.path.normcase(os.path.abspath(str(path))) == target_key
                ):
                    injected = True
                    os.rmdir(path)
                    created = subprocess.run(
                        ["cmd.exe", "/c", "mklink", "/J", str(path), str(outside)],
                        capture_output=True,
                        check=False,
                        text=True,
                    )
                    if created.returncode != 0:
                        raise AssertionError(
                            f"junction injection failed: {created.stderr.strip()}"
                        )
                return original_pin(path, code)

            try:
                with (
                    mock.patch.object(
                        self.controller,
                        "_pin_plain_directory",
                        side_effect=inject_target_junction,
                    ),
                    mock.patch.object(
                        self.controller,
                        "_write_new_pinned_file",
                        side_effect=AssertionError("outside target leaf was written"),
                    ) as leaf_write,
                ):
                    with self.assertRaises(self.controller.ControllerError) as captured:
                        self.controller._copy_reported_tree(candidate, stage, report)
                self.assertTrue(
                    injected,
                    (captured.exception.code, str(captured.exception), observed_pin_paths),
                )
                self.assertEqual(captured.exception.code, "INSTALL_TARGET_RACE")
                self.assertEqual(leaf_write.call_count, 0)
                self.assertEqual(
                    hashlib.sha256(sentinel.read_bytes()).hexdigest(), sentinel_before
                )
            finally:
                if stage.exists() or os.path.lexists(stage):
                    self.controller._safe_remove_stage(
                        stage, install_root, "example-skill"
                    )
            self.assertFalse(os.path.lexists(stage))
            self.assertEqual(
                hashlib.sha256(sentinel.read_bytes()).hexdigest(), sentinel_before
            )

    @unittest.skipUnless(os.name == "nt", "Windows junction TOCTOU regression")
    def test_v22_copy_rejects_candidate_root_swap_before_any_outside_read(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v22-copy-source-root-race-") as directory:
            base = Path(directory)
            candidate = write_safe_skill(base / "candidate")
            report = self.inspector.inspect_skill(candidate)
            approved_bytes = (candidate / "SKILL.md").read_bytes()
            backup = base / "candidate-audited-backup"
            candidate.rename(backup)
            outside = base / "outside-source-root"
            outside.mkdir()
            sentinel = outside / "SKILL.md"
            sentinel.write_bytes(approved_bytes)
            sentinel_before = hashlib.sha256(sentinel.read_bytes()).hexdigest()
            created = subprocess.run(
                ["cmd.exe", "/c", "mklink", "/J", str(candidate), str(outside)],
                capture_output=True,
                check=False,
                text=True,
            )
            if created.returncode != 0:
                backup.rename(candidate)
                self.skipTest(f"junction creation unavailable: {created.stderr.strip()}")
            install_root = base / "install-root"
            install_root.mkdir()
            stage = install_root / ".skill-curator-staging-example-skill-source-root-race"
            try:
                with mock.patch.object(
                    self.controller,
                    "_read_pinned_regular_file",
                    side_effect=AssertionError("junction target root was read"),
                ) as leaf_read:
                    with self.assertRaises(self.controller.ControllerError) as captured:
                        self.controller._copy_reported_tree(candidate, stage, report)
                self.assertEqual(captured.exception.code, "CANDIDATE_CHANGED")
                self.assertEqual(leaf_read.call_count, 0)
                self.assertEqual(
                    hashlib.sha256(sentinel.read_bytes()).hexdigest(), sentinel_before
                )
            finally:
                if stage.exists() or os.path.lexists(stage):
                    self.controller._safe_remove_stage(stage, install_root, "example-skill")
                if os.path.lexists(candidate):
                    os.rmdir(candidate)
                if backup.exists():
                    backup.rename(candidate)

    @unittest.skipUnless(os.name == "nt", "Windows junction TOCTOU regression")
    def test_v22_copy_rejects_install_root_swap_before_any_outside_write(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v22-copy-install-root-race-") as directory:
            base = Path(directory)
            candidate = write_safe_skill(base / "candidate")
            report = self.inspector.inspect_skill(candidate)
            install_root = base / "install-root"
            install_root.mkdir()
            install_backup = base / "install-root-audited-backup"
            install_root.rename(install_backup)
            outside = base / "outside-target-root"
            outside.mkdir()
            sentinel = outside / "sentinel.txt"
            sentinel.write_text("TEST_ONLY_OUTSIDE_SENTINEL", encoding="utf-8")
            sentinel_before = hashlib.sha256(sentinel.read_bytes()).hexdigest()
            created = subprocess.run(
                ["cmd.exe", "/c", "mklink", "/J", str(install_root), str(outside)],
                capture_output=True,
                check=False,
                text=True,
            )
            if created.returncode != 0:
                install_backup.rename(install_root)
                self.skipTest(f"junction creation unavailable: {created.stderr.strip()}")
            stage = install_root / ".skill-curator-staging-example-skill-install-root-race"
            outside_stage = outside / stage.name
            try:
                with mock.patch.object(
                    self.controller,
                    "_write_new_pinned_file",
                    side_effect=AssertionError("junction target root was written"),
                ) as leaf_write:
                    with self.assertRaises(self.controller.ControllerError) as captured:
                        self.controller._copy_reported_tree(candidate, stage, report)
                self.assertEqual(captured.exception.code, "INSTALL_TARGET_RACE")
                self.assertEqual(leaf_write.call_count, 0)
                self.assertFalse(os.path.lexists(outside_stage))
                self.assertEqual(
                    hashlib.sha256(sentinel.read_bytes()).hexdigest(), sentinel_before
                )
            finally:
                if os.path.lexists(outside_stage):
                    self.controller._remove_stage_tree_no_follow(outside_stage)
                if os.path.lexists(install_root):
                    os.rmdir(install_root)
                if install_backup.exists():
                    install_backup.rename(install_root)

    @unittest.skipUnless(os.name == "nt", "Windows full-install junction regression")
    def test_v22_full_install_rejects_pre_lock_install_root_swap_with_zero_outside_write(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v22-full-pre-lock-root-race-") as directory:
            base = Path(directory)
            project = base / "project"
            project.mkdir()
            make_operating_clear_project(project, "founder-os-main-v22-curator-test")
            candidate = write_safe_skill(base / "candidate")
            install_root = base / "install-root"
            install_root.mkdir()
            install_backup = base / "install-root-before-swap"
            outside = base / "outside-target"
            outside.mkdir()
            sentinel = outside / "sentinel.txt"
            sentinel.write_text("TEST_ONLY_OUTSIDE_SENTINEL", encoding="utf-8")
            outside_before = snapshot_tree(outside)
            arguments = self._install_args(
                project=project,
                candidate=candidate,
                install_root=install_root,
            )
            original_acquire = self.controller._LexicalDirectoryFence.acquire
            injected = False

            def swap_before_lock(root: Path, code: str):
                nonlocal injected
                same_root = False
                try:
                    same_root = os.path.samefile(root, install_root)
                except OSError:
                    same_root = (
                        os.path.normcase(str(Path(root).resolve(strict=False)))
                        == os.path.normcase(str(install_root.resolve(strict=False)))
                    )
                if not injected and same_root:
                    injected = True
                    install_root.rename(install_backup)
                    created = subprocess.run(
                        ["cmd.exe", "/c", "mklink", "/J", str(install_root), str(outside)],
                        capture_output=True,
                        check=False,
                        text=True,
                    )
                    if created.returncode != 0:
                        install_backup.rename(install_root)
                        self.skipTest(f"junction creation unavailable: {created.stderr.strip()}")
                return original_acquire(root, code)

            try:
                with mock.patch.object(
                    self.controller._LexicalDirectoryFence,
                    "acquire",
                    side_effect=swap_before_lock,
                ):
                    with self.assertRaises(self.controller.ControllerError) as captured:
                        self.controller.install_candidate(arguments)
                self.assertTrue(injected)
                self.assertEqual(captured.exception.code, "INSTALL_TARGET_RACE")
                self.assertEqual(snapshot_tree(outside), outside_before)
                self.assertEqual(list(outside.iterdir()), [sentinel])
            finally:
                if injected and os.path.lexists(install_root):
                    os.rmdir(install_root)
                if install_backup.exists():
                    install_backup.rename(install_root)
                if not injected and install_root.exists():
                    destination = install_root / "example-skill"
                    if destination.exists():
                        self.controller._remove_stage_tree_no_follow(destination)
                    for lock in install_root.glob(".skill-curator-install-example-skill.lock"):
                        lock.unlink()

    @unittest.skipUnless(os.name == "nt", "Windows full-install junction regression")
    def test_v22_full_install_post_copy_root_swap_fails_recovery_without_outside_write(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v22-full-post-copy-root-race-") as directory:
            base = Path(directory)
            project = base / "project"
            project.mkdir()
            make_operating_clear_project(project, "founder-os-main-v22-curator-test")
            candidate = write_safe_skill(base / "candidate")
            install_root = base / "install-root"
            install_root.mkdir()
            install_backup = base / "install-root-after-copy"
            outside = base / "outside-target"
            outside.mkdir()
            sentinel = outside / "sentinel.txt"
            sentinel.write_text("TEST_ONLY_OUTSIDE_SENTINEL", encoding="utf-8")
            outside_before = snapshot_tree(outside)
            arguments = self._install_args(
                project=project,
                candidate=candidate,
                install_root=install_root,
            )
            original_copy = self.controller._copy_reported_tree
            original_lock_pin = self.controller._pin_verified_control_file
            injected = False
            captured_lock_pin = None

            def remember_lock_pin(path, fence, expected_sha256):
                nonlocal captured_lock_pin
                captured_lock_pin = original_lock_pin(path, fence, expected_sha256)
                return captured_lock_pin

            def swap_after_copy(source, destination, report, **kwargs):
                nonlocal injected
                result = original_copy(source, destination, report, **kwargs)
                fence = kwargs["install_root_fence"]
                assert captured_lock_pin is not None
                # Model catastrophic simultaneous loss of both independent OS
                # identity fences; either one alone natively denies this move.
                captured_lock_pin.close()
                fence.close()
                install_root.rename(install_backup)
                created = subprocess.run(
                    ["cmd.exe", "/c", "mklink", "/J", str(install_root), str(outside)],
                    capture_output=True,
                    check=False,
                    text=True,
                )
                if created.returncode != 0:
                    install_backup.rename(install_root)
                    self.skipTest(f"junction creation unavailable: {created.stderr.strip()}")
                injected = True
                return result

            try:
                with mock.patch.object(
                    self.controller,
                    "_copy_reported_tree",
                    side_effect=swap_after_copy,
                ), mock.patch.object(
                    self.controller,
                    "_pin_verified_control_file",
                    side_effect=remember_lock_pin,
                ):
                    with self.assertRaises(self.controller.ControllerError) as captured:
                        self.controller.install_candidate(arguments)
                self.assertTrue(injected)
                self.assertEqual(captured.exception.code, "INSTALL_RECOVERY_REQUIRED")
                self.assertEqual(snapshot_tree(outside), outside_before)
                self.assertFalse((outside / "example-skill").exists())
                self.assertFalse(any(outside.glob(".skill-curator-*")))
            finally:
                if os.path.lexists(install_root):
                    os.rmdir(install_root)
                if install_backup.exists():
                    install_backup.rename(install_root)
                for stage in install_root.glob(".skill-curator-staging-example-skill-*"):
                    self.controller._safe_remove_stage(stage, install_root, "example-skill")
                for lock in install_root.glob(".skill-curator-install-example-skill.lock"):
                    lock.unlink()

    @unittest.skipUnless(os.name == "nt", "Windows native full-install fence regression")
    def test_v22_full_install_native_fence_denies_root_rename_through_commit(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v22-full-native-root-fence-") as directory:
            base = Path(directory)
            project = base / "project"
            project.mkdir()
            make_operating_clear_project(project, "founder-os-main-v22-curator-test")
            candidate = write_safe_skill(base / "candidate")
            install_root = base / "install-root"
            install_root.mkdir()
            forbidden_backup = base / "install-root-must-not-move"
            arguments = self._install_args(
                project=project,
                candidate=candidate,
                install_root=install_root,
            )
            original_validate = self.controller._quick_validate
            rename_denied = False

            def attempt_native_root_rename(skill_root: Path, validator: Path):
                nonlocal rename_denied
                result = original_validate(skill_root, validator)
                try:
                    install_root.rename(forbidden_backup)
                except OSError as exc:
                    rename_denied = getattr(exc, "winerror", None) in {5, 32}
                else:
                    forbidden_backup.rename(install_root)
                    raise AssertionError("Windows install-root identity fence allowed rename")
                return result

            with mock.patch.object(
                self.controller,
                "_quick_validate",
                side_effect=attempt_native_root_rename,
            ):
                result = self.controller.install_candidate(arguments)
            self.assertTrue(rename_denied)
            self.assertEqual(result["result"], "SKILL_INSTALLED")
            self.assertTrue((install_root / "example-skill").is_dir())
            self.assertFalse(forbidden_backup.exists())
            self.assertFalse(any(install_root.glob(".skill-curator-*")))

    @unittest.skipUnless(os.name == "nt", "Windows full-install junction regression")
    def test_v22_full_install_pre_cleanup_root_swap_preserves_outside_tree(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v22-full-cleanup-root-race-") as directory:
            base = Path(directory)
            project = base / "project"
            project.mkdir()
            make_operating_clear_project(project, "founder-os-main-v22-curator-test")
            candidate = write_safe_skill(base / "candidate")
            install_root = base / "install-root"
            install_root.mkdir()
            install_backup = base / "install-root-before-cleanup"
            outside = base / "outside-target"
            outside.mkdir()
            sentinel = outside / "sentinel.txt"
            sentinel.write_text("TEST_ONLY_OUTSIDE_SENTINEL", encoding="utf-8")
            outside_before = snapshot_tree(outside)
            arguments = self._install_args(
                project=project,
                candidate=candidate,
                install_root=install_root,
            )
            original_copy = self.controller._copy_reported_tree
            original_lock_pin = self.controller._pin_verified_control_file
            captured_fence = None
            captured_lock_pin = None

            def remember_lock_pin(path, fence, expected_sha256):
                nonlocal captured_lock_pin
                captured_lock_pin = original_lock_pin(path, fence, expected_sha256)
                return captured_lock_pin

            def remember_fence(source, destination, report, **kwargs):
                nonlocal captured_fence
                captured_fence = kwargs["install_root_fence"]
                return original_copy(source, destination, report, **kwargs)

            def fail_validation_after_root_swap(_skill_root, _validator):
                assert captured_fence is not None
                assert captured_lock_pin is not None
                captured_lock_pin.close()
                captured_fence.close()
                install_root.rename(install_backup)
                created = subprocess.run(
                    ["cmd.exe", "/c", "mklink", "/J", str(install_root), str(outside)],
                    capture_output=True,
                    check=False,
                    text=True,
                )
                if created.returncode != 0:
                    install_backup.rename(install_root)
                    self.skipTest(f"junction creation unavailable: {created.stderr.strip()}")
                raise self.controller.ControllerError(
                    "TEST_VALIDATION_FAILURE",
                    "Force the full-install cleanup path after a root swap",
                )

            try:
                with (
                    mock.patch.object(
                        self.controller,
                        "_copy_reported_tree",
                        side_effect=remember_fence,
                    ),
                    mock.patch.object(
                        self.controller,
                        "_pin_verified_control_file",
                        side_effect=remember_lock_pin,
                    ),
                    mock.patch.object(
                        self.controller,
                        "_quick_validate",
                        side_effect=fail_validation_after_root_swap,
                    ),
                    mock.patch.object(
                        self.controller,
                        "_remove_stage_tree_no_follow",
                        side_effect=AssertionError("cleanup traversed the outside target"),
                    ) as cleanup,
                ):
                    with self.assertRaises(self.controller.ControllerError) as captured:
                        self.controller.install_candidate(arguments)
                self.assertEqual(captured.exception.code, "INSTALL_RECOVERY_REQUIRED")
                self.assertEqual(cleanup.call_count, 0)
                self.assertEqual(snapshot_tree(outside), outside_before)
                self.assertFalse(any(outside.glob(".skill-curator-*")))
            finally:
                if os.path.lexists(install_root):
                    os.rmdir(install_root)
                if install_backup.exists():
                    install_backup.rename(install_root)
                for stage in install_root.glob(".skill-curator-staging-example-skill-*"):
                    self.controller._safe_remove_stage(stage, install_root, "example-skill")
                for lock in install_root.glob(".skill-curator-install-example-skill.lock"):
                    lock.unlink()

    def test_v22_final_strategic_reauthorization_blocks_context_drift_and_cleans_stage(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v22-final-gate-drift-") as directory:
            base = Path(directory)
            project = base / "project"
            project.mkdir()
            make_operating_clear_project(project, "founder-os-main-v22-curator-test")
            candidate = write_safe_skill(base / "candidate")
            install_root = base / "install-root"
            install_root.mkdir()
            arguments = self._install_args(
                project=project,
                candidate=candidate,
                install_root=install_root,
            )
            original_validate = self.controller._quick_validate
            pivoted = False

            def pivot_supervisor_context(skill_root: Path, validator: Path):
                nonlocal pivoted
                result = original_validate(skill_root, validator)
                status = project / ".founder" / "STATUS.md"
                status.write_text(
                    status.read_text(encoding="utf-8")
                    + "\nTEST_ONLY_UNCHECKPOINTED_CONTEXT_PIVOT\n",
                    encoding="utf-8",
                )
                pivoted = True
                return result

            with mock.patch.object(
                self.controller,
                "_quick_validate",
                side_effect=pivot_supervisor_context,
            ):
                with self.assertRaises(self.controller.ControllerError) as captured:
                    self.controller.install_candidate(arguments)
            self.assertTrue(pivoted)
            self.assertEqual(captured.exception.code, "STRATEGIC_CONTEXT_DRIFT")
            self.assertFalse((install_root / "example-skill").exists())
            self.assertEqual(list(install_root.iterdir()), [])

    def test_v22_final_reauthorization_is_immediately_followed_by_fences_and_rename(self) -> None:
        source = CURATOR_CONTROLLER.read_text(encoding="utf-8")
        start = source.index("final_authorization = _revalidate_strategic_install_authorization(")
        end = source.index("renamed = True", start)
        commit_window = source[start:end]
        self.assertNotIn("inspector.inspect_skill", commit_window)
        self.assertNotIn("_quick_validate", commit_window)
        self.assertNotIn("_run_trusted_python", commit_window)
        self.assertIn("lock_pin.assert_current", commit_window)
        self.assertIn("install_root_fence.assert_current", commit_window)
        self.assertIn("_rename_stage_under_fence", commit_window)
        self.assertLess(
            commit_window.index("lock_pin.assert_current"),
            commit_window.index("_rename_stage_under_fence"),
        )

    def test_v22_hardlink_candidate_is_structurally_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v22-curator-hardlink-") as directory:
            base = Path(directory)
            candidate = write_safe_skill(base / "candidate")
            outside = base / "outside.txt"
            outside.write_text("TEST_ONLY_OUTSIDE_SENTINEL", encoding="utf-8")
            before = hashlib.sha256(outside.read_bytes()).hexdigest()
            os.link(outside, candidate / "hardlink.txt")
            report = self.inspector.inspect_skill(candidate)
            self.assertFalse(report["structurally_admissible"])
            self.assertIn(
                "HARDLINK", {row["code"] for row in report["structural_violations"]}
            )
            self.assertEqual(hashlib.sha256(outside.read_bytes()).hexdigest(), before)

    def test_v22_inspector_resource_and_output_limits_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v22-curator-limits-") as directory:
            base = Path(directory)
            many_entries = write_safe_skill(base / "many-entries")
            for index in range(20):
                (many_entries / f"empty-{index:03d}").mkdir()
            entry_report = self.inspector.inspect_skill(
                many_entries,
                max_total_entries=10,
                max_directories=20,
            )
            self.assertFalse(entry_report["structurally_admissible"])
            self.assertLessEqual(
                entry_report["resource_usage"]["total_entries"], 11
            )
            self.assertIn(
                "RESOURCE_LIMIT",
                {row["code"] for row in entry_report["structural_violations"]},
            )

            deep = write_safe_skill(base / "deep")
            cursor = deep
            for index in range(6):
                cursor = cursor / f"level-{index}"
                cursor.mkdir()
            depth_report = self.inspector.inspect_skill(deep, max_depth=3)
            self.assertFalse(depth_report["structurally_admissible"])
            self.assertTrue(
                any(
                    "DEPTH_LIMIT_EXCEEDED" in row["detail"]
                    for row in depth_report["structural_violations"]
                )
            )

            amplified = write_safe_skill(base / "amplified")
            (amplified / "network.md").write_text(
                "\n".join(
                    f"https://host-{index}.example.invalid/path" for index in range(100)
                ),
                encoding="utf-8",
            )
            (amplified / "package.json").write_text(
                json.dumps(
                    {
                        "dependencies": {
                            f"fixture-package-{index}": "1.0.0"
                            for index in range(100)
                        }
                    }
                ),
                encoding="utf-8",
            )
            output_report = self.inspector.inspect_skill(
                amplified,
                max_network_destinations=10,
                max_dependency_facts=10,
            )
            self.assertFalse(output_report["structurally_admissible"])
            self.assertLessEqual(len(output_report["network_destinations"]), 10)
            self.assertLessEqual(len(output_report["dependency_facts"]), 10)
            self.assertEqual(
                set(output_report["output_truncations"]),
                {"dependency_facts", "network_destinations"},
            )
            self.assertIn(
                "OUTPUT_TRUNCATED",
                {row["code"] for row in output_report["structural_violations"]},
            )

    def test_v22_protected_core_cannot_be_acquired_or_self_modified(self) -> None:
        for skill_id in ("founder-os", "skill-curator"):
            with self.assertRaises(self.controller.ControllerError) as captured:
                self.controller._require_skill_id(skill_id)
            self.assertEqual(captured.exception.code, "PROTECTED_CORE_SKILL")
        source = (SKILL_CURATOR_ROOT / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("PROTECTED CORE", source)
        self.assertRegex(source, r"不能.*FounderOS|不得.*FounderOS")

    def test_v22_protected_core_paths_reject_alias_skill_before_any_write(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v22-protected-path-") as directory:
            base = Path(directory)
            project = base / "project"
            project.mkdir()
            make_operating_clear_project(project, "founder-os-main-v22-curator-test")
            candidate = write_safe_skill(base / "candidate")
            before_founder = snapshot_tree(SKILL_ROOT)
            before_curator = snapshot_tree(SKILL_CURATOR_ROOT)
            for protected_install_root in (
                SKILL_ROOT / "references",
                SKILL_CURATOR_ROOT / "references",
            ):
                arguments = self._install_args(
                    project=project,
                    candidate=candidate,
                    install_root=protected_install_root,
                    skill_id="example-skill",
                )
                with self.subTest(install_root=str(protected_install_root)):
                    with self.assertRaises(self.controller.ControllerError) as captured:
                        self.controller.install_candidate(arguments)
                    self.assertEqual(captured.exception.code, "PROTECTED_CORE_PATH")
            self.assertEqual(snapshot_tree(SKILL_ROOT), before_founder)
            self.assertEqual(snapshot_tree(SKILL_CURATOR_ROOT), before_curator)

    def test_v22_registration_rejects_installed_identity_alias_before_registry_call(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v22-register-identity-") as directory:
            base = Path(directory)
            installed = write_safe_skill(base / "installed", "actual-skill")
            aliased = test_skill_entry(
                installed,
                skill_id="forged-skill",
                status="VALIDATED",
            )
            aliased["runtime_visibility"] = {
                "state": "NOT_CONFIRMED",
                "runtime": "UNVERIFIED",
                "evidence_ref": "NONE",
                "observed_at": "2026-08-12T00:00:00Z",
            }
            before = snapshot_tree(base)
            with mock.patch.object(
                self.controller,
                "_run_trusted_python",
                side_effect=AssertionError("Registry must not be invoked for an identity alias"),
            ) as registry_call:
                with self.assertRaises(self.controller.ControllerError) as captured:
                    self.controller.register_with_founderos(
                        argparse.Namespace(entry_json=json.dumps(aliased))
                    )
            self.assertEqual(captured.exception.code, "SKILL_ID_MISMATCH")
            self.assertEqual(registry_call.call_count, 0)
            self.assertEqual(snapshot_tree(base), before)

            protected = test_skill_entry(
                installed,
                skill_id="actual-skill",
                status="VALIDATED",
            )
            protected["runtime_visibility"] = aliased["runtime_visibility"]
            with mock.patch.object(
                self.controller,
                "_protected_skill_roots",
                return_value=(installed.resolve(),),
            ), mock.patch.object(
                self.controller,
                "_run_trusted_python",
                side_effect=AssertionError("Registry must not be invoked for protected content"),
            ) as registry_call:
                with self.assertRaises(self.controller.ControllerError) as captured:
                    self.controller.register_with_founderos(
                        argparse.Namespace(entry_json=json.dumps(protected))
                    )
            self.assertEqual(captured.exception.code, "PROTECTED_CORE_PATH")
            self.assertEqual(registry_call.call_count, 0)
            self.assertEqual(snapshot_tree(base), before)

    def test_v22_runtime_degradation_never_claims_installation(self) -> None:
        source = (SKILL_CURATOR_ROOT / "SKILL.md").read_text(encoding="utf-8")
        for term in (
            "RUNTIME_CAPABILITY_UNAVAILABLE",
            "AUDITED_NOT_EXECUTED",
            "INSTALLED",
            "VALIDATED",
        ):
            self.assertIn(term, source)
        self.assertRegex(source, r"不(?:得|能).*伪报|不得声称")

    def test_v22_safe_pure_document_skill_installs_only_after_authoritative_gate(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v22-curator-install-") as directory:
            base = Path(directory)
            project = base / "project"
            project.mkdir()
            make_operating_clear_project(project, "founder-os-main-v22-curator-test")
            candidate = write_safe_skill(base / "candidate")
            install_root = base / "install-root"
            install_root.mkdir()
            arguments = self._install_args(
                project=project, candidate=candidate, install_root=install_root
            )
            result = self.controller.install_candidate(arguments)
            destination = install_root / "example-skill"
            self.assertEqual(result["result"], "SKILL_INSTALLED")
            self.assertTrue(destination.is_dir())
            self.assertEqual(
                self.inspector.inspect_skill(destination)["content_hash"]["value"],
                result["content_hash"],
            )
            self.assertTrue(result["format_validation"]["passed"])
            self.assertEqual(result["strategic_authorization"]["gate"], "OPERATING")
            self.assertFalse(result["candidate_code_executed"])
            self.assertFalse(result["dependencies_installed"])
            self.assertFalse(result["availability_eligible"])
            self.assertEqual(result["registry_entry_validated"]["status"], "VALIDATED")
            self.assertEqual(
                result["registry_entry_validated"]["runtime_visibility"]["state"],
                "NOT_CONFIRMED",
            )
            self.assertEqual(result["runtime_visibility"]["state"], "NOT_CONFIRMED")
            self.assertFalse(any(install_root.glob(".skill-curator-*")))

    def test_v22_dynamic_validation_is_required_for_execution_surfaces(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v22-dynamic-gate-") as directory:
            base = Path(directory)
            project = base / "project"
            project.mkdir()
            make_operating_clear_project(project, "founder-os-main-v22-curator-test")

            for label, configure in (
                (
                    "audit-not-executed",
                    lambda candidate, arguments: setattr(
                        arguments, "audit_status", "AUDITED_NOT_EXECUTED"
                    ),
                ),
                (
                    "script-without-dynamic-pass",
                    lambda candidate, arguments: (
                        (candidate / "scripts").mkdir(),
                        (candidate / "scripts" / "run.py").write_text(
                            "print('TEST_ONLY')\n", encoding="utf-8"
                        ),
                        setattr(arguments, "dynamic_validation", "NOT_PERFORMED"),
                    ),
                ),
            ):
                case = base / label
                candidate = write_safe_skill(case / "candidate")
                install_root = case / "install-root"
                install_root.mkdir(parents=True)
                arguments = self._install_args(
                    project=project,
                    candidate=candidate,
                    install_root=install_root,
                    skill_id="example-skill",
                )
                configure(candidate, arguments)
                arguments.expected_content_hash = self.inspector.inspect_skill(candidate)[
                    "content_hash"
                ]["value"]
                before = snapshot_tree(install_root)
                with self.subTest(label=label):
                    with self.assertRaisesRegex(
                        self.controller.ControllerError,
                        "AUDITED_NOT_EXECUTED|dynamic_validation",
                    ):
                        self.controller.install_candidate(arguments)
                    self.assertEqual(snapshot_tree(install_root), before)

    def test_v22_duplicate_frontmatter_name_is_structurally_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v22-frontmatter-identity-") as directory:
            candidate = Path(directory) / "candidate"
            candidate.mkdir()
            (candidate / "SKILL.md").write_text(
                "---\n"
                "name: harmless-skill\n"
                "description: Duplicate identity fixture\n"
                "name: founder-os\n"
                "---\n\n# Fixture\n",
                encoding="utf-8",
            )
            report = self.inspector.inspect_skill(candidate)
            self.assertFalse(report["structurally_admissible"])
            self.assertFalse(report["identity"]["valid"])
            self.assertTrue(
                any(
                    row["code"] == "INVALID_SKILL_FRONTMATTER"
                    and "duplicate" in row["detail"].casefold()
                    for row in report["structural_violations"]
                )
            )

    def test_v22_pyc_and_renamed_binary_are_execution_surfaces(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v22-binary-surface-") as directory:
            candidate = write_safe_skill(Path(directory) / "candidate")
            (candidate / "compiled.pyc").write_bytes(b"\x42\x0d\x0d\x0aTEST_ONLY")
            (candidate / "renamed.dat").write_bytes(b"MZTEST_ONLY_BINARY")
            report = self.inspector.inspect_skill(candidate)
            self.assertTrue(report["structurally_admissible"])
            self.assertEqual(
                set(report["binary_files"]),
                {"compiled.pyc", "renamed.dat"},
            )

    def test_v22_install_before_strategic_gate_is_zero_write(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v22-curator-gate-") as directory:
            base = Path(directory)
            project = base / "project"
            project.mkdir()
            create_empty_active_project(project, "founder-os-main-v22-curator-test")
            candidate = write_safe_skill(base / "candidate")
            install_root = base / "install-root"
            install_root.mkdir()
            arguments = self._install_args(
                project=project, candidate=candidate, install_root=install_root
            )
            before = snapshot_tree(base)
            with self.assertRaises(self.controller.ControllerError) as captured:
                self.controller.install_candidate(arguments)
            self.assertEqual(captured.exception.code, "STRATEGIC_GATE_BLOCKED")
            self.assertEqual(snapshot_tree(base), before)
            self.assertEqual(list(install_root.iterdir()), [])

    def test_v22_update_revoke_deprecate_are_proposals_not_global_deletion(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v22-curator-lifecycle-") as directory:
            entry = test_skill_entry(Path(directory) / "installed")
            for command, expected in (
                ("update", "UPDATE_AVAILABLE"),
                ("revoke", "REVOKED"),
                ("deprecate", "DEPRECATED"),
            ):
                arguments = argparse.Namespace(
                    command=command,
                    skill_id="example-skill",
                    entry_json=json.dumps(entry),
                    entry_revision=f"SKE-{command.upper()}",
                    audit_revision=f"AUD-{command.upper()}",
                    change_ref=f"TEST-{command.upper()}",
                    deprecation_status="REPLACED_BY_SUCCESSOR" if command == "deprecate" else None,
                )
                result = self.controller.lifecycle_proposal(arguments)
                self.assertEqual(result["registry_entry"]["status"], expected)
                self.assertTrue(result["history_preservation_required"])
                self.assertFalse(result["physical_global_deletion"])
                self.assertFalse(result["curator_direct_registry_write"])

    def test_v22_caller_supplied_hash_cannot_make_an_arbitrary_helper_trusted(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v22-curator-helper-anchor-") as directory:
            base = Path(directory)
            project = base / "project"
            project.mkdir()
            make_operating_clear_project(project, "founder-os-main-v22-curator-test")
            candidate = write_safe_skill(base / "candidate")
            install_root = base / "install-root"
            install_root.mkdir()
            fake = base / "decision_state.py"
            fake.write_text("raise RuntimeError('MUST NEVER EXECUTE')\n", encoding="utf-8")
            arguments = self._install_args(
                project=project, candidate=candidate, install_root=install_root
            )
            arguments.decision_state_script = str(fake.resolve())
            arguments.decision_state_sha256 = hashlib.sha256(fake.read_bytes()).hexdigest()
            before = snapshot_tree(base)
            with mock.patch.object(subprocess, "run") as execution:
                with self.assertRaises(self.controller.ControllerError) as captured:
                    self.controller.install_candidate(arguments)
            execution.assert_not_called()
            self.assertEqual(captured.exception.code, "UNTRUSTED_HELPER")
            self.assertEqual(snapshot_tree(base), before)
            self.assertEqual(list(install_root.iterdir()), [])


class ThreadSkillSyncV22Tests(unittest.TestCase):
    OWNER = "founder-os-main-v22-sync-test"

    def _prepared_thread(
        self,
        base: Path,
        entries: list[dict[str, Any]],
        *,
        skills: list[str],
        agent_kind: str = "persistent",
        thread_type: str = "persistent",
        agent_id: str = "technical-lead-v22",
    ) -> tuple[Path, dict[str, Any], str]:
        root = base / "project"
        root.mkdir()
        state = make_operating_clear_project(root, self.OWNER)
        state = initialize_test_skill_registry(root, state, entries, owner=self.OWNER)
        initialized = initialize_thread_registry(root, state, self.OWNER)
        state = merge_control_state(state, initialized)
        reserved = registry_module.reserve_thread(
            str(root),
            owner=self.OWNER,
            activation_token=state["activation_token"],
            expected_state_sha=state["state_sha"],
            expected_registry_sha=state["registry_sha"],
            agent_id=agent_id,
            agent_kind=agent_kind,
            logical_name=f"{agent_id} primary",
            manager_agent_id="founder-os-main",
            workstream="engineering",
            thread_type=thread_type,
            read_scope=["src/**"],
            write_scope=["src/engineering/**"] if thread_type != "fork-readonly" else [],
            skills=skills,
            dependencies=[],
            capabilities=["engineering"],
        )
        state = merge_control_state(state, reserved)
        record_id = reserved["details"]["thread_record_id"]
        bound = registry_module.bind_runtime(
            str(root),
            owner=self.OWNER,
            activation_token=state["activation_token"],
            expected_state_sha=state["state_sha"],
            expected_registry_sha=state["registry_sha"],
            thread_record_id=record_id,
            binding_nonce=reserved["details"]["binding_nonce"],
            runtime_thread_id="runtime-v22-stable-001",
            runtime_host_id="local-test-host",
            identity_quality="observed",
        )
        return root, merge_control_state(state, bound), record_id

    @staticmethod
    def _thread(root: Path, record_id: str) -> dict[str, Any]:
        registry = registry_module.inspect_registry(str(root))["registry"]
        return next(
            thread
            for thread in registry["threads"]
            if thread["thread_record_id"] == record_id
        )

    @staticmethod
    def _ack(plan: dict[str, Any]) -> str:
        return "SKILL_SYNC " + " ".join(
            f"{key}={value}" for key, value in plan["ack_markers"].items()
        )

    def test_v22_skill_sync_ack_is_an_exact_unique_marker_protocol(self) -> None:
        expected = {
            "THREAD_RECORD_ID": "thread-001",
            "SKILL_LOCK_REVISION": "SKL-001",
            "BOUND_SKILLS_SHA256": "A" * 64,
        }
        valid = "SKILL_SYNC " + " ".join(
            f"{key}={value}" for key, value in expected.items()
        )
        registry_module._require_exact_skill_sync_ack(valid, expected)
        invalid = [
            "X" + valid,
            valid + " EXTRA=value",
            valid + " THREAD_RECORD_ID=thread-001",
            valid.replace("THREAD_RECORD_ID=thread-001", "THREAD_RECORD_ID=other"),
            valid.replace("SKILL_LOCK_REVISION=SKL-001", "XSKILL_LOCK_REVISION=SKL-001"),
            valid + " ",
        ]
        for acknowledgement in invalid:
            with self.subTest(acknowledgement=acknowledgement):
                with self.assertRaises(guard_module.Conflict):
                    registry_module._require_exact_skill_sync_ack(
                        acknowledgement, expected
                    )

    def test_v22_thread_record_has_exact_machine_skill_baseline(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v22-thread-baseline-") as directory:
            base = Path(directory)
            entry = test_skill_entry(
                base / "installed" / "primary-a",
                skill_id="primary-a",
                capability="engineering-build",
                scoped_workstreams=["engineering"],
            )
            root, _state, record_id = self._prepared_thread(base, [entry], skills=["primary-a"])
            thread = self._thread(root, record_id)
            for key in (
                "capability_baseline",
                "skill_registry_revision",
                "skill_lock_revision",
                "skill_lock_sha256",
                "bound_skills",
                "bound_skills_sha256",
                "skill_sync_state",
                "last_skill_sync",
            ):
                self.assertIn(key, thread)
            self.assertEqual(thread["skill_sync_state"], "CURRENT")
            self.assertEqual(thread["bound_skills"][0]["skill_id"], "primary-a")
            self.assertEqual(thread["runtime"]["thread_id"], "runtime-v22-stable-001")

    def test_v22_added_skill_requires_exact_ack_on_same_runtime_thread(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v22-thread-added-") as directory:
            base = Path(directory)
            a = test_skill_entry(
                base / "installed" / "primary-a", skill_id="primary-a",
                capability="engineering-build", scoped_workstreams=["engineering"],
            )
            root, state, record_id = self._prepared_thread(base, [a], skills=["primary-a"])
            b = test_skill_entry(
                base / "installed" / "primary-b", skill_id="primary-b",
                capability="engineering-review", scoped_workstreams=["engineering"],
                scoped_thread_ids=[record_id],
                entry_revision="SKE-B1", audit_revision="AUD-B1",
            )
            mutation = skill_registry_module.register_skills(
                str(root), owner=self.OWNER,
                activation_token=state["activation_token"],
                expected_state_sha=state["state_sha"],
                expected_lock_sha=state["skill_lock_sha"],
                entries=[b], change_ref="ADD-B",
            )
            state = merge_control_state(state, mutation)
            thread = self._thread(root, record_id)
            plan = registry_module.skill_sync_plan((root / ".founder").resolve(), thread)
            self.assertEqual(plan["state"], "REQUIRED")
            self.assertEqual(plan["diff"]["ADDED"], ["primary-b"])
            before = snapshot_tree(root)
            with self.assertRaises(guard_module.Conflict):
                registry_module.skill_sync(
                    str(root), owner=self.OWNER,
                    activation_token=state["activation_token"],
                    expected_state_sha=state["state_sha"],
                    expected_registry_sha=state["registry_sha"],
                    thread_record_id=record_id,
                    acknowledgement="SKILL_SYNC stale acknowledgement",
                )
            self.assertEqual(snapshot_tree(root), before)
            synced = registry_module.skill_sync(
                str(root), owner=self.OWNER,
                activation_token=state["activation_token"],
                expected_state_sha=state["state_sha"],
                expected_registry_sha=state["registry_sha"],
                thread_record_id=record_id,
                acknowledgement=self._ack(plan),
            )
            state = merge_control_state(state, synced)
            thread = self._thread(root, record_id)
            self.assertEqual(thread["thread_record_id"], record_id)
            self.assertEqual(thread["runtime"]["thread_id"], "runtime-v22-stable-001")
            self.assertEqual(
                [row["skill_id"] for row in thread["bound_skills"]],
                ["primary-a", "primary-b"],
            )
            assigned = registry_module.assign_task(
                str(root), owner=self.OWNER,
                activation_token=state["activation_token"],
                expected_state_sha=state["state_sha"],
                expected_registry_sha=state["registry_sha"],
                thread_record_id=record_id, task_id="post-sync-task",
                summary="Use the exact synchronized capability baseline",
                acceptance_ref="deterministic sync acceptance",
            )
            self.assertEqual(assigned["details"]["task_id"], "post-sync-task")

    def test_v22_agent_and_workstream_ceilings_do_not_auto_bind_new_skill(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v22-thread-scope-ceiling-") as directory:
            base = Path(directory)
            current = test_skill_entry(
                base / "installed" / "primary-a",
                skill_id="primary-a",
                capability="engineering-build",
                scoped_agent_ids=["technical-lead-v22"],
                scoped_workstreams=["engineering"],
            )
            root, state, record_id = self._prepared_thread(
                base,
                [current],
                skills=["primary-a"],
            )
            ceiling_only = test_skill_entry(
                base / "installed" / "support-b",
                skill_id="support-b",
                capability="engineering-review",
                role="SUPPORTING",
                scoped_agent_ids=["technical-lead-v22"],
                scoped_workstreams=["engineering"],
                entry_revision="SKE-B-CEILING",
                audit_revision="AUD-B-CEILING",
            )
            mutation = skill_registry_module.register_skills(
                str(root),
                owner=self.OWNER,
                activation_token=state["activation_token"],
                expected_state_sha=state["state_sha"],
                expected_lock_sha=state["skill_lock_sha"],
                entries=[ceiling_only],
                change_ref="AGENT-WORKSTREAM-ARE-CEILINGS",
            )
            state = merge_control_state(state, mutation)
            before = snapshot_tree(root)
            plan = registry_module.skill_sync_plan(
                (root / ".founder").resolve(),
                self._thread(root, record_id),
            )
            self.assertEqual(plan["state"], "CURRENT")
            self.assertEqual(plan["diff"]["ADDED"], [])
            self.assertEqual(
                [row["skill_id"] for row in plan["bound_skills"]],
                ["primary-a"],
            )
            self.assertEqual(snapshot_tree(root), before)

    def test_v22_unbound_created_thread_cannot_plan_or_ack_skill_sync(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v22-unbound-skill-sync-") as directory:
            base = Path(directory)
            root = base / "project"
            root.mkdir()
            state = make_operating_clear_project(root, self.OWNER)
            entry = test_skill_entry(
                base / "installed" / "primary-a",
                skill_id="primary-a",
                capability="engineering-build",
            )
            state = initialize_test_skill_registry(
                root,
                state,
                [entry],
                owner=self.OWNER,
            )
            state = merge_control_state(
                state,
                initialize_thread_registry(root, state, self.OWNER),
            )
            reserved = registry_module.reserve_thread(
                str(root),
                owner=self.OWNER,
                activation_token=state["activation_token"],
                expected_state_sha=state["state_sha"],
                expected_registry_sha=state["registry_sha"],
                agent_id="unbound-sync-worker",
                agent_kind="persistent",
                logical_name="Unbound sync worker",
                manager_agent_id="founder-os-main",
                workstream="engineering",
                thread_type="persistent",
                read_scope=["src/**"],
                write_scope=["src/engineering/**"],
                skills=["primary-a"],
                dependencies=[],
                capabilities=["engineering"],
            )
            state = merge_control_state(state, reserved)
            record_id = reserved["details"]["thread_record_id"]
            thread = self._thread(root, record_id)
            self.assertEqual(thread["lifecycle_state"], "CREATED")
            self.assertIsNone(thread["runtime"]["thread_id"])
            before = snapshot_tree(root)
            plan = registry_module.skill_sync_plan(
                (root / ".founder").resolve(),
                thread,
            )
            self.assertEqual(plan["state"], "BLOCKED")
            self.assertEqual(plan["reason"], "UNBOUND_RUNTIME")
            self.assertNotIn("ack_markers", plan)
            self.assertEqual(snapshot_tree(root), before)
            with self.assertRaisesRegex(guard_module.Conflict, "UNBOUND_RUNTIME"):
                registry_module.skill_sync(
                    str(root),
                    owner=self.OWNER,
                    activation_token=state["activation_token"],
                    expected_state_sha=state["state_sha"],
                    expected_registry_sha=state["registry_sha"],
                    thread_record_id=record_id,
                    acknowledgement="SKILL_SYNC stale acknowledgement",
                )
            self.assertEqual(snapshot_tree(root), before)

    def test_v22_revoked_primary_stays_blocked_after_sync_until_replaced(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v22-thread-revoke-") as directory:
            base = Path(directory)
            a = test_skill_entry(
                base / "installed" / "primary-a", skill_id="primary-a",
                capability="engineering-build", scoped_workstreams=["engineering"],
            )
            root, state, record_id = self._prepared_thread(base, [a], skills=["primary-a"])
            revoked = copy.deepcopy(a)
            revoked.update(
                status="REVOKED", trust_level="rejected", risk_level="BLOCKED",
                audit_revision="AUD-A-REVOKED", entry_revision="SKE-A-REVOKED",
            )
            revoked["approval"] = {"mode": "REJECTED", "evidence_ref": "DEC-REVOKE-A"}
            mutation = skill_registry_module.register_skills(
                str(root), owner=self.OWNER,
                activation_token=state["activation_token"],
                expected_state_sha=state["state_sha"],
                expected_lock_sha=state["skill_lock_sha"],
                entries=[revoked], change_ref="REVOKE-A",
            )
            state = merge_control_state(state, mutation)
            plan = registry_module.skill_sync_plan(
                (root / ".founder").resolve(), self._thread(root, record_id)
            )
            self.assertEqual(plan["diff"]["REVOKED"], ["primary-a"])
            self.assertEqual(plan["replacement_needed"], ["engineering-build"])
            synced = registry_module.skill_sync(
                str(root), owner=self.OWNER,
                activation_token=state["activation_token"],
                expected_state_sha=state["state_sha"],
                expected_registry_sha=state["registry_sha"],
                thread_record_id=record_id,
                acknowledgement=self._ack(plan),
            )
            state = merge_control_state(state, synced)
            thread = self._thread(root, record_id)
            self.assertEqual(thread["skill_sync_state"], "BLOCKED")
            self.assertEqual(thread["replacement_needed"], ["engineering-build"])
            with self.assertRaises(guard_module.Conflict):
                registry_module.assign_task(
                    str(root), owner=self.OWNER,
                    activation_token=state["activation_token"],
                    expected_state_sha=state["state_sha"],
                    expected_registry_sha=state["registry_sha"],
                    thread_record_id=record_id, task_id="must-not-dispatch",
                    summary="This must remain blocked", acceptance_ref="replacement required",
                )

    def test_v22_skill_and_business_context_baselines_are_independent(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v22-thread-independent-") as directory:
            base = Path(directory)
            a = test_skill_entry(
                base / "installed" / "primary-a", skill_id="primary-a",
                capability="engineering-build", scoped_workstreams=["engineering"],
            )
            root, state, record_id = self._prepared_thread(base, [a], skills=["primary-a"])
            thread_before = self._thread(root, record_id)
            b = test_skill_entry(
                base / "installed" / "primary-b", skill_id="primary-b",
                capability="engineering-review", scoped_workstreams=["engineering"],
                scoped_thread_ids=[record_id],
                entry_revision="SKE-B1", audit_revision="AUD-B1",
            )
            mutation = skill_registry_module.register_skills(
                str(root), owner=self.OWNER,
                activation_token=state["activation_token"],
                expected_state_sha=state["state_sha"],
                expected_lock_sha=state["skill_lock_sha"],
                entries=[b], change_ref="ADD-B-INDEPENDENT",
            )
            plan = registry_module.skill_sync_plan(
                (root / ".founder").resolve(), thread_before
            )
            self.assertEqual(plan["state"], "REQUIRED")
            self.assertTrue(
                registry_module._baseline_matches(
                    thread_before["context_baseline"],
                    registry_module._context_baseline((root / ".founder").resolve()),
                )
            )

    def test_v22_task_scoped_skill_does_not_expand_to_another_task(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v22-thread-task-scope-") as directory:
            base = Path(directory)
            scoped = test_skill_entry(
                base / "installed" / "task-only", skill_id="task-only",
                capability="single-task-review", scoped_workstreams=["engineering"],
                scoped_task_ids=["task-one"],
            )
            root, state, record_id = self._prepared_thread(
                base, [scoped], skills=[], agent_kind="task", thread_type="task",
                agent_id="review-task-v22",
            )
            thread = self._thread(root, record_id)
            first = registry_module.skill_sync_plan(
                (root / ".founder").resolve(), thread, task_id="task-one"
            )
            self.assertEqual(first["diff"]["ADDED"], ["task-only"])
            synced = registry_module.skill_sync(
                str(root), owner=self.OWNER,
                activation_token=state["activation_token"],
                expected_state_sha=state["state_sha"],
                expected_registry_sha=state["registry_sha"],
                thread_record_id=record_id, task_id="task-one",
                acknowledgement=self._ack(first),
            )
            state = merge_control_state(state, synced)
            second = registry_module.skill_sync_plan(
                (root / ".founder").resolve(), self._thread(root, record_id),
                task_id="task-two",
            )
            self.assertEqual(second["diff"]["REMOVED"], ["task-only"])
            self.assertEqual(second["bound_skills"], [])


class SkillRegistryV22Tests(unittest.TestCase):
    OWNER = "founder-os-main-v22-test"

    def _operating(self, base: Path) -> tuple[Path, dict[str, Any]]:
        root = base / "project"
        root.mkdir()
        return root, make_operating_clear_project(root, self.OWNER)

    def test_v22_absent_inspection_is_strictly_read_only(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v22-skill-inspect-") as directory:
            root = Path(directory)
            before = snapshot_tree(root)
            inspected = skill_registry_module.inspect_skill_registry(str(root))
            self.assertEqual(inspected["pair_state"], "ABSENT")
            self.assertEqual(inspected["changed_paths"], [])
            self.assertEqual(snapshot_tree(root), before)
            self.assertFalse((root / ".founder").exists())

    def test_v22_init_writes_cross_checked_lock_projection_and_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v22-skill-init-") as directory:
            base = Path(directory)
            root, state = self._operating(base)
            entry = test_skill_entry(base / "installed" / "example-skill")
            updated = initialize_test_skill_registry(
                root, state, [entry], owner=self.OWNER
            )
            inspected = skill_registry_module.inspect_skill_registry(str(root))
            self.assertEqual(inspected["pair_state"], "CURRENT")
            lock = inspected["skill_lock"]
            self.assertEqual(lock["project_binding"]["project_root"], str(root.resolve()))
            self.assertEqual(
                lock["registry_projection_sha256"], inspected["registry_sha"]
            )
            self.assertEqual(updated["skill_lock_sha"], inspected["skill_lock_sha"])
            self.assertIn("example-skill", lock["skills"])
            projection = (root / ".founder" / "SKILLS.md").read_text(encoding="utf-8")
            self.assertIn("Machine binding authority", projection)
            self.assertIn(entry["installed_path"].replace("\\", "\\\\"), projection)

    def test_v22_current_users_are_derived_from_threads_not_allowed_scopes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v22-current-users-") as directory:
            base = Path(directory)
            root, state = self._operating(base)
            entry = test_skill_entry(
                base / "installed" / "current-user-skill",
                skill_id="current-user-skill",
                capability="current-user-capability",
                scoped_workstreams=["engineering"],
            )
            state = initialize_test_skill_registry(
                root, state, [entry], owner=self.OWNER
            )
            initial = skill_registry_module.inspect_skill_registry(str(root))
            self.assertEqual(
                initial["actual_current_users"]["current-user-skill"]["state"],
                "UNKNOWN",
            )

            state = merge_control_state(
                state, initialize_thread_registry(root, state, self.OWNER)
            )
            empty = skill_registry_module.inspect_skill_registry(str(root))
            self.assertEqual(
                empty["actual_current_users"]["current-user-skill"]["state"],
                "NONE",
            )
            reserved = registry_module.reserve_thread(
                str(root), owner=self.OWNER,
                activation_token=state["activation_token"],
                expected_state_sha=state["state_sha"],
                expected_registry_sha=state["registry_sha"],
                agent_id="current-user-agent", agent_kind="persistent",
                logical_name="Current user projection worker",
                manager_agent_id="founder-os-main", workstream="engineering",
                thread_type="persistent", read_scope=["src/**"],
                write_scope=["src/engineering/**"], skills=["current-user-skill"],
                dependencies=[], capabilities=["current-user-capability"],
            )
            state = merge_control_state(state, reserved)
            current = skill_registry_module.inspect_skill_registry(str(root))
            view = current["actual_current_users"]["current-user-skill"]
            self.assertEqual(view["state"], "CONFIRMED")
            self.assertEqual(view["users"][0]["agent_id"], "current-user-agent")
            projection = (root / ".founder" / "SKILLS.md").read_text(encoding="utf-8")
            self.assertIn("Allowed workstreams", projection)
            self.assertIn("READ_TIME: inspect.actual_current_users", projection)

            threads_path = root / ".founder" / "THREADS.json"
            valid_threads_raw = threads_path.read_bytes()
            drifted = json.loads(valid_threads_raw)
            drifted["threads"][0]["agent_id"] = "forged-agent"
            threads_path.write_bytes(guard_module.canonical_json_bytes(drifted))
            before_inspect = snapshot_tree(root)
            drift_view = skill_registry_module.inspect_skill_registry(str(root))[
                "actual_current_users"
            ]["current-user-skill"]
            self.assertEqual(drift_view["state"], "UNKNOWN")
            self.assertEqual(drift_view["users"], [])
            self.assertEqual(snapshot_tree(root), before_inspect)

            malformed = {
                "schema_version": 1,
                "registry_revision": "TR-FORGED-CURRENT-USERS",
                "project_binding": {
                    "project_root": str(root.resolve()),
                    "project_binding_id": "not-a-real-binding",
                },
                "threads": [
                    {
                        "agent_id": "forged-agent",
                        "thread_record_id": "forged-thread",
                        "workstream": "engineering",
                        "lifecycle_state": "ACTIVE",
                        "bound_skills": [{"skill_id": "current-user-skill"}],
                        "runtime": {"thread_id": "forged-runtime", "host_id": "local"},
                    }
                ],
            }
            threads_path.write_bytes(guard_module.canonical_json_bytes(malformed))
            checkpoint = guard_module.checkpoint_active(
                str(root), owner=self.OWNER,
                activation_token=state["activation_token"],
                expected_state_sha=state["state_sha"],
            )
            state["state_sha"] = checkpoint["state_sha"]
            malformed_view = skill_registry_module.inspect_skill_registry(str(root))[
                "actual_current_users"
            ]["current-user-skill"]
            self.assertEqual(malformed_view["state"], "UNKNOWN")
            self.assertEqual(malformed_view["users"], [])

    def test_v22_strategic_gate_blocks_registry_mutation_without_writes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v22-skill-gate-") as directory:
            root = Path(directory)
            state = create_empty_active_project(root, self.OWNER)
            before = snapshot_tree(root)
            with self.assertRaises(guard_module.Conflict):
                skill_registry_module.initialize_skill_registry(
                    str(root),
                    owner=self.OWNER,
                    activation_token=state["activation_token"],
                    expected_state_sha=state["state_sha"],
                    expected_lock_sha="ABSENT",
                    entries=[],
                    change_ref="GATE-MUST-BLOCK",
                )
            self.assertEqual(snapshot_tree(root), before)
            self.assertFalse((root / ".founder" / "SKILL_LOCK.json").exists())

    def test_v22_floating_git_ref_and_untrusted_binding_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v22-skill-entry-") as directory:
            base = Path(directory)
            floating = test_skill_entry(
                base / "floating",
                source_ref="main",
            )
            with self.assertRaises(guard_module.InvalidState):
                skill_registry_module.normalize_entry(floating)
            untrusted = test_skill_entry(
                base / "untrusted",
                trust_level="third-party-unreviewed",
            )
            with self.assertRaises(guard_module.InvalidState):
                skill_registry_module.normalize_entry(untrusted)

            mismatched = test_skill_entry(
                base / "mismatched-ref", skill_id="mismatched-ref",
                source_ref="v1.0.0",
            )
            with self.assertRaisesRegex(
                guard_module.InvalidState, "ref.*commit|commit.*ref"
            ):
                skill_registry_module.normalize_entry(mismatched)

            catalog = test_skill_entry(
                base / "repo-catalog", skill_id="repo-catalog"
            )
            catalog["source"]["source_type"] = "catalog"
            catalog["source"]["ref"] = "v1.0.0"
            with self.assertRaisesRegex(
                guard_module.InvalidState, "ref.*commit|commit.*ref"
            ):
                skill_registry_module.normalize_entry(catalog)
            catalog["source"]["ref"] = catalog["source"]["commit_sha"]
            self.assertEqual(
                skill_registry_module.normalize_entry(catalog)["source"]["source_type"],
                "catalog",
            )

    def test_v22_registry_enforces_risk_approval_matrix_and_rejects_placeholders(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v22-approval-matrix-") as directory:
            base = Path(directory)
            allowed = {
                "LOW": {"AUTO", "FOUNDER", "EXPLICIT"},
                "MEDIUM": {"FOUNDER", "EXPLICIT"},
                "HIGH": {"EXPLICIT"},
                "BLOCKED": set(),
            }
            for risk in ("LOW", "MEDIUM", "HIGH", "BLOCKED"):
                for mode in ("AUTO", "FOUNDER", "EXPLICIT", "NONE", "REJECTED"):
                    skill_id = f"approval-{risk.lower()}-{mode.lower()}"
                    entry = test_skill_entry(
                        base / skill_id,
                        skill_id=skill_id,
                    )
                    entry["risk_level"] = risk
                    entry["approval"] = {
                        "mode": mode,
                        "evidence_ref": f"APPROVAL-EVIDENCE-{risk}-{mode}",
                    }
                    if mode in allowed[risk]:
                        self.assertEqual(
                            skill_registry_module.normalize_entry(entry)["approval"]["mode"],
                            mode,
                        )
                    else:
                        with self.assertRaisesRegex(
                            guard_module.InvalidState, "approval mode"
                        ):
                            skill_registry_module.normalize_entry(entry)

            placeholder = test_skill_entry(
                base / "placeholder-approval",
                skill_id="placeholder-approval",
            )
            placeholder["approval"]["evidence_ref"] = "<approval-ref>"
            with self.assertRaisesRegex(
                guard_module.InvalidState, "placeholder"
            ):
                skill_registry_module.normalize_entry(placeholder)

    def test_v22_rejected_approval_registration_is_zero_write(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v22-approval-zero-write-") as directory:
            base = Path(directory)
            root, state = self._operating(base)
            state = initialize_test_skill_registry(
                root, state, owner=self.OWNER
            )
            rejected = test_skill_entry(
                base / "installed" / "high-auto",
                skill_id="high-auto",
            )
            rejected["risk_level"] = "HIGH"
            rejected["approval"] = {
                "mode": "AUTO",
                "evidence_ref": "AUTO-IS-NOT-ENOUGH-FOR-HIGH",
            }
            before = snapshot_tree(root)
            with self.assertRaisesRegex(
                guard_module.InvalidState, "approval mode"
            ):
                skill_registry_module.register_skills(
                    str(root), owner=self.OWNER,
                    activation_token=state["activation_token"],
                    expected_state_sha=state["state_sha"],
                    expected_lock_sha=state["skill_lock_sha"],
                    entries=[rejected], change_ref="MUST-NOT-WRITE",
                )
            self.assertEqual(snapshot_tree(root), before)

    def test_v22_protected_core_ids_and_unconfirmed_runtime_are_not_bindable(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v22-skill-entry-policy-") as directory:
            base = Path(directory)
            for skill_id in ("founder-os", "skill-curator", "Founder-OS"):
                protected = test_skill_entry(
                    base / skill_id, skill_id=skill_id, capability="governance-core"
                )
                with self.subTest(skill_id=skill_id):
                    with self.assertRaisesRegex(
                        guard_module.InvalidState, "PROTECTED_CORE_SKILL"
                    ):
                        skill_registry_module.normalize_entry(protected)

            missing_visibility = test_skill_entry(
                base / "missing-visibility", skill_id="missing-visibility"
            )
            missing_visibility.pop("runtime_visibility")
            with self.assertRaisesRegex(
                guard_module.InvalidState, "runtime_visibility"
            ):
                skill_registry_module.normalize_entry(missing_visibility)

            unconfirmed = test_skill_entry(
                base / "unconfirmed", skill_id="unconfirmed"
            )
            unconfirmed["runtime_visibility"] = {
                "state": "NOT_CONFIRMED",
                "runtime": "UNVERIFIED",
                "evidence_ref": "NONE",
                "observed_at": "2026-08-12T00:00:00Z",
            }
            with self.assertRaisesRegex(
                guard_module.InvalidState, "runtime_visibility"
            ):
                skill_registry_module.normalize_entry(unconfirmed)
            unconfirmed["status"] = "VALIDATED"
            self.assertEqual(
                skill_registry_module.normalize_entry(unconfirmed)["status"],
                "VALIDATED",
            )

            for label, override in (
                ("unknown-runtime", {"runtime": "unknown"}),
                ("placeholder-evidence", {"evidence_ref": "NONE"}),
                ("invalid-time", {"observed_at": "not-a-time"}),
                ("naive-time", {"observed_at": "2026-08-12T00:00:00"}),
            ):
                invalid = test_skill_entry(
                    base / label, skill_id=label,
                )
                invalid["runtime_visibility"].update(override)
                with self.subTest(label=label):
                    with self.assertRaisesRegex(
                        guard_module.InvalidState, "runtime_visibility"
                    ):
                        skill_registry_module.normalize_entry(invalid)

    def test_v22_registry_mutation_binds_semantic_identity_and_rejects_core_aliases(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v22-registry-identity-") as directory:
            base = Path(directory)
            root, state = self._operating(base)
            state = initialize_test_skill_registry(root, state, owner=self.OWNER)

            installed = write_safe_skill(base / "installed" / "actual-skill", "actual-skill")
            aliased = test_skill_entry(installed, skill_id="forged-skill")
            before = snapshot_tree(root)
            with self.assertRaisesRegex(
                guard_module.InvalidState, "semantic name.*skill_id"
            ):
                skill_registry_module.register_skills(
                    str(root), owner=self.OWNER,
                    activation_token=state["activation_token"],
                    expected_state_sha=state["state_sha"],
                    expected_lock_sha=state["skill_lock_sha"],
                    entries=[aliased], change_ref="IDENTITY-ALIAS-MUST-NOT-WRITE",
                )
            self.assertEqual(snapshot_tree(root), before)

            protected_root = base / "protected-core"
            protected_installed = write_safe_skill(
                protected_root / "nested-skill", "nested-skill"
            )
            protected_entry = test_skill_entry(
                protected_installed, skill_id="nested-skill"
            )
            with mock.patch.object(
                skill_registry_module,
                "PROTECTED_CORE_SKILL_ROOTS",
                (protected_root.resolve(),),
            ):
                with self.assertRaisesRegex(
                    guard_module.InvalidState, "PROTECTED_CORE_PATH"
                ):
                    skill_registry_module.register_skills(
                        str(root), owner=self.OWNER,
                        activation_token=state["activation_token"],
                        expected_state_sha=state["state_sha"],
                        expected_lock_sha=state["skill_lock_sha"],
                        entries=[protected_entry], change_ref="CORE-ALIAS-MUST-NOT-WRITE",
                    )
            self.assertEqual(snapshot_tree(root), before)

    @unittest.skipUnless(os.name == "nt", "Windows junction regression")
    def test_v22_installed_root_junction_hash_check_fails_before_traversal(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v22-installed-junction-") as directory:
            base = Path(directory)
            target = base / "outside-installed"
            target.mkdir()
            sentinel = target / "SKILL.md"
            sentinel.write_text("TEST_ONLY_OUTSIDE_SENTINEL", encoding="utf-8")
            before = hashlib.sha256(sentinel.read_bytes()).hexdigest()
            junction = base / "installed-junction"
            created = subprocess.run(
                ["cmd.exe", "/c", "mklink", "/J", str(junction), str(target)],
                capture_output=True,
                check=False,
                text=True,
            )
            if created.returncode != 0:
                self.skipTest(f"junction creation unavailable: {created.stderr.strip()}")
            try:
                with mock.patch.object(
                    skill_registry_module.os,
                    "scandir",
                    side_effect=AssertionError("redirected installed tree was traversed"),
                ):
                    with self.assertRaisesRegex(
                        guard_module.Conflict, "HASH_MISMATCH"
                    ):
                        skill_registry_module.installed_tree_hash(
                            str(junction),
                            skill_id="junction-skill",
                        )
                self.assertEqual(
                    hashlib.sha256(sentinel.read_bytes()).hexdigest(), before
                )
            finally:
                if os.path.lexists(junction):
                    os.rmdir(junction)

    @unittest.skipUnless(os.name == "nt", "Windows installed-tree root TOCTOU regression")
    def test_v22_registry_rehash_root_swap_after_preflight_reads_no_outside_bytes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v22-registry-root-race-") as directory:
            base = Path(directory)
            installed = write_safe_skill(base / "installed", "race-skill")
            backup = base / "installed-original"
            outside = write_safe_skill(base / "outside", "outside-skill")
            sentinel = outside / "outside-sentinel.txt"
            sentinel.write_text("TEST_ONLY_OUTSIDE_SENTINEL", encoding="utf-8")
            outside_before = snapshot_tree(outside)
            original_acquire = skill_registry_module._InstalledTreeFence.acquire
            swapped = False

            def swap_after_preflight(root, *, skill_id, expected_root_metadata):
                nonlocal swapped
                installed.rename(backup)
                created = subprocess.run(
                    ["cmd.exe", "/c", "mklink", "/J", str(installed), str(outside)],
                    capture_output=True,
                    check=False,
                    text=True,
                )
                if created.returncode != 0:
                    backup.rename(installed)
                    self.skipTest(f"junction creation unavailable: {created.stderr.strip()}")
                swapped = True
                return original_acquire(
                    root,
                    skill_id=skill_id,
                    expected_root_metadata=expected_root_metadata,
                )

            try:
                with (
                    mock.patch.object(
                        skill_registry_module._InstalledTreeFence,
                        "acquire",
                        side_effect=swap_after_preflight,
                    ),
                    mock.patch.object(
                        skill_registry_module,
                        "_read_installed_regular_file",
                        side_effect=AssertionError("outside installed file was read"),
                    ) as file_read,
                ):
                    with self.assertRaisesRegex(guard_module.Conflict, "HASH_MISMATCH"):
                        skill_registry_module.installed_tree_hash(
                            str(installed.resolve()),
                            skill_id="race-skill",
                        )
                self.assertTrue(swapped)
                self.assertEqual(file_read.call_count, 0)
                self.assertEqual(snapshot_tree(outside), outside_before)
            finally:
                if swapped and os.path.lexists(installed):
                    os.rmdir(installed)
                if backup.exists():
                    backup.rename(installed)

    @unittest.skipUnless(os.name == "nt", "Windows installed-tree subdirectory TOCTOU regression")
    def test_v22_registry_rehash_subdir_swap_reads_no_outside_bytes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v22-registry-subdir-race-") as directory:
            base = Path(directory)
            installed = write_safe_skill(base / "installed", "race-skill")
            subtree = installed / "references"
            subtree.mkdir()
            (subtree / "safe.md").write_text("approved bytes", encoding="utf-8")
            backup = installed / "references-original"
            outside = base / "outside"
            outside.mkdir()
            sentinel = outside / "outside-sentinel.md"
            sentinel.write_text("TEST_ONLY_OUTSIDE_SECRET", encoding="utf-8")
            outside_before = snapshot_tree(outside)
            original_pin_directory = skill_registry_module._InstalledTreeFence.pin_directory
            original_read = skill_registry_module._read_installed_regular_file
            attempted = False
            rename_denied = False
            swapped = False
            outside_reads: list[str] = []

            def swap_before_subdir_pin(fence, path, *, expected_metadata=None):
                nonlocal attempted, rename_denied, swapped
                if not attempted and guard_module._same_path(path, subtree):
                    attempted = True
                    try:
                        subtree.rename(backup)
                    except OSError as exc:
                        rename_denied = getattr(exc, "winerror", None) in {5, 32}
                    else:
                        created = subprocess.run(
                            ["cmd.exe", "/c", "mklink", "/J", str(subtree), str(outside)],
                            capture_output=True,
                            check=False,
                            text=True,
                        )
                        if created.returncode != 0:
                            backup.rename(subtree)
                            self.skipTest(f"junction creation unavailable: {created.stderr.strip()}")
                        swapped = True
                return original_pin_directory(
                    fence,
                    path,
                    expected_metadata=expected_metadata,
                )

            def reject_outside_read(path, expected, *, relative, skill_id):
                try:
                    resolved = path.resolve(strict=True)
                except OSError:
                    resolved = path
                if skill_registry_module._is_within(resolved, outside.resolve()):
                    outside_reads.append(relative)
                    raise AssertionError("outside installed file was read")
                return original_read(
                    path,
                    expected,
                    relative=relative,
                    skill_id=skill_id,
                )

            try:
                with (
                    mock.patch.object(
                        skill_registry_module._InstalledTreeFence,
                        "pin_directory",
                        new=swap_before_subdir_pin,
                    ),
                    mock.patch.object(
                        skill_registry_module,
                        "_read_installed_regular_file",
                        side_effect=reject_outside_read,
                    ),
                ):
                    skill_registry_module.installed_tree_hash(
                        str(installed.resolve()),
                        skill_id="race-skill",
                    )
                self.assertTrue(attempted)
                self.assertTrue(rename_denied)
                self.assertFalse(swapped)
                self.assertEqual(outside_reads, [])
                self.assertEqual(snapshot_tree(outside), outside_before)
            finally:
                if swapped and os.path.lexists(subtree):
                    os.rmdir(subtree)
                if backup.exists():
                    backup.rename(subtree)

    @unittest.skipUnless(os.name == "nt", "Windows nested scandir TOCTOU regression")
    def test_v22_registry_rehash_nested_scandir_swap_is_denied_before_outside_read(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v22-registry-scandir-race-") as directory:
            base = Path(directory)
            installed = write_safe_skill(base / "installed", "race-skill")
            subtree = installed / "references"
            subtree.mkdir()
            (subtree / "safe.md").write_text("approved bytes", encoding="utf-8")
            outside = base / "outside"
            outside.mkdir()
            sentinel = outside / "outside-sentinel.md"
            sentinel.write_text("TEST_ONLY_OUTSIDE_SECRET", encoding="utf-8")
            outside_before = snapshot_tree(outside)
            forbidden_backup = installed / "references-must-not-move"
            original_scandir = skill_registry_module.os.scandir
            attempted = False
            rename_denied = False

            def attempt_swap_on_nested_scandir(path):
                nonlocal attempted, rename_denied
                if not attempted and guard_module._same_path(path, subtree):
                    attempted = True
                    try:
                        subtree.rename(forbidden_backup)
                    except OSError as exc:
                        rename_denied = getattr(exc, "winerror", None) in {5, 32}
                    else:
                        forbidden_backup.rename(subtree)
                        raise AssertionError("nested directory pin allowed subtree rename")
                return original_scandir(path)

            with mock.patch.object(
                skill_registry_module.os,
                "scandir",
                side_effect=attempt_swap_on_nested_scandir,
            ):
                skill_registry_module.installed_tree_hash(
                    str(installed.resolve()),
                    skill_id="race-skill",
                )
            self.assertTrue(attempted)
            self.assertTrue(rename_denied)
            self.assertFalse(forbidden_backup.exists())
            self.assertEqual(snapshot_tree(outside), outside_before)

    @unittest.skipUnless(os.name == "nt", "Windows installed leaf-open TOCTOU regression")
    def test_v22_registry_rehash_leaf_open_swap_is_denied_before_outside_read(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v22-registry-leaf-race-") as directory:
            base = Path(directory)
            installed = write_safe_skill(base / "installed", "race-skill")
            leaf = installed / "payload.txt"
            leaf.write_text("approved bytes", encoding="utf-8")
            outside = base / "outside"
            outside.mkdir()
            sentinel = outside / "outside-sentinel.txt"
            sentinel.write_text("TEST_ONLY_OUTSIDE_SECRET", encoding="utf-8")
            outside_before = snapshot_tree(outside)
            forbidden_backup = installed / "payload-must-not-move.txt"
            original_read = skill_registry_module._read_installed_regular_file
            attempted = False
            rename_denied = False

            def attempt_swap_before_leaf_read(path, expected, *, relative, skill_id):
                nonlocal attempted, rename_denied
                if not attempted and guard_module._same_path(path, leaf):
                    attempted = True
                    try:
                        leaf.rename(forbidden_backup)
                    except OSError as exc:
                        rename_denied = getattr(exc, "winerror", None) in {5, 32}
                    else:
                        forbidden_backup.rename(leaf)
                        raise AssertionError("leaf pin allowed installed file rename")
                return original_read(
                    path,
                    expected,
                    relative=relative,
                    skill_id=skill_id,
                )

            with mock.patch.object(
                skill_registry_module,
                "_read_installed_regular_file",
                side_effect=attempt_swap_before_leaf_read,
            ):
                skill_registry_module.installed_tree_hash(
                    str(installed.resolve()),
                    skill_id="race-skill",
                )
            self.assertTrue(attempted)
            self.assertTrue(rename_denied)
            self.assertFalse(forbidden_backup.exists())
            self.assertEqual(snapshot_tree(outside), outside_before)

    def test_v22_registry_rehash_enforces_same_tree_resource_limits_as_inspector(self) -> None:
        inspector, _controller = load_curator_modules()
        self.assertEqual(
            skill_registry_module.INSTALLED_MAX_TOTAL_ENTRIES,
            inspector.DEFAULT_MAX_TOTAL_ENTRIES,
        )
        self.assertEqual(
            skill_registry_module.INSTALLED_MAX_DIRECTORIES,
            inspector.DEFAULT_MAX_DIRECTORIES,
        )
        self.assertEqual(
            skill_registry_module.INSTALLED_MAX_DEPTH,
            inspector.DEFAULT_MAX_DEPTH,
        )
        with tempfile.TemporaryDirectory(prefix="founder-os-v22-installed-limits-") as directory:
            base = Path(directory)
            many = base / "many"
            many.mkdir()
            (many / "SKILL.md").write_text("fixture", encoding="utf-8")
            for index in range(6):
                (many / f"empty-{index}").mkdir()
            with mock.patch.object(
                skill_registry_module, "INSTALLED_MAX_DIRECTORIES", 5
            ):
                with self.assertRaisesRegex(guard_module.Conflict, "HASH_MISMATCH"):
                    skill_registry_module.installed_tree_hash(
                        str(many.resolve()), skill_id="directory-limit-skill"
                    )

            deep = base / "deep"
            deep.mkdir()
            (deep / "SKILL.md").write_text("fixture", encoding="utf-8")
            cursor = deep
            for index in range(4):
                cursor = cursor / f"level-{index}"
                cursor.mkdir()
            with mock.patch.object(skill_registry_module, "INSTALLED_MAX_DEPTH", 3):
                with self.assertRaisesRegex(guard_module.Conflict, "HASH_MISMATCH"):
                    skill_registry_module.installed_tree_hash(
                        str(deep.resolve()), skill_id="depth-limit-skill"
                    )

    def test_v22_dual_cas_rejection_preserves_every_byte_and_metadata(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v22-skill-cas-") as directory:
            base = Path(directory)
            root, state = self._operating(base)
            state = initialize_test_skill_registry(root, state, owner=self.OWNER)
            entry = test_skill_entry(base / "installed" / "cas-skill", skill_id="cas-skill")
            before = snapshot_tree(root)
            with self.assertRaises(guard_module.Conflict):
                skill_registry_module.register_skills(
                    str(root),
                    owner=self.OWNER,
                    activation_token=state["activation_token"],
                    expected_state_sha=state["state_sha"],
                    expected_lock_sha="0" * 64,
                    entries=[entry],
                    change_ref="STALE-CAS",
                )
            self.assertEqual(snapshot_tree(root), before)

    def test_v22_projection_drift_fails_closed_without_self_rewrite(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v22-skill-drift-") as directory:
            base = Path(directory)
            root, state = self._operating(base)
            entry = test_skill_entry(base / "installed" / "drift-skill", skill_id="drift-skill")
            initialize_test_skill_registry(root, state, [entry], owner=self.OWNER)
            projection = root / ".founder" / "SKILLS.md"
            projection.write_text(
                projection.read_text(encoding="utf-8") + "\nUNAUTHORIZED DRIFT\n",
                encoding="utf-8",
            )
            before = snapshot_tree(root)
            inspected = skill_registry_module.inspect_skill_registry(str(root))
            self.assertEqual(inspected["pair_state"], "RECOVERY_REQUIRED")
            self.assertIn("drifted", inspected["issue"])
            self.assertEqual(snapshot_tree(root), before)

    def test_v22_update_available_keeps_v1_until_reaudit_and_preserves_history(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v22-skill-update-") as directory:
            base = Path(directory)
            root, state = self._operating(base)
            v1 = test_skill_entry(base / "installed" / "versioned-skill", skill_id="versioned-skill")
            state = initialize_test_skill_registry(root, state, [v1], owner=self.OWNER)
            pending = copy.deepcopy(v1)
            pending.update(
                status="UPDATE_AVAILABLE",
                audit_revision="AUD-UPDATE-NOTICE",
                entry_revision="SKE-UPDATE-NOTICE",
                notes="Version 2 exists upstream; version 1 remains pinned.",
            )
            mutation = skill_registry_module.register_skills(
                str(root),
                owner=self.OWNER,
                activation_token=state["activation_token"],
                expected_state_sha=state["state_sha"],
                expected_lock_sha=state["skill_lock_sha"],
                entries=[pending],
                change_ref="DISCOVER-V2",
            )
            state = merge_control_state(state, mutation)
            lock = skill_registry_module.inspect_skill_registry(str(root))["skill_lock"]
            self.assertEqual(lock["skills"]["versioned-skill"]["approved_version"], "1.0.0")
            self.assertEqual(lock["skills"]["versioned-skill"]["status"], "UPDATE_AVAILABLE")
            self.assertEqual(len(lock["history"]), 1)

            v2 = copy.deepcopy(pending)
            installed_skill_file = Path(v1["installed_path"]) / "SKILL.md"
            installed_skill_file.write_text(
                installed_skill_file.read_text(encoding="utf-8")
                + "Version 2 reviewed content.\n",
                encoding="utf-8",
            )
            new_digest = skill_registry_module.installed_tree_hash(
                v1["installed_path"], skill_id="versioned-skill"
            )
            new_commit = hashlib.sha1(b"versioned-skill-v2").hexdigest()
            v2.update(
                approved_version="2.0.0",
                content_hash=new_digest,
                installed_hash=new_digest,
                audit_revision="AUD-V2",
                entry_revision="SKE-V2",
                status="AVAILABLE",
            )
            v2["source"].update(
                exact_source=f"https://example.invalid/versioned-skill@{new_commit}",
                ref=new_commit,
                commit_sha=new_commit,
            )
            mutation = skill_registry_module.register_skills(
                str(root),
                owner=self.OWNER,
                activation_token=state["activation_token"],
                expected_state_sha=state["state_sha"],
                expected_lock_sha=state["skill_lock_sha"],
                entries=[v2],
                change_ref="APPROVE-V2",
            )
            state = merge_control_state(state, mutation)
            lock = skill_registry_module.inspect_skill_registry(str(root))["skill_lock"]
            self.assertEqual(lock["skills"]["versioned-skill"]["approved_version"], "2.0.0")
            self.assertEqual(len(lock["history"]), 2)
            self.assertEqual(lock["history"][0]["prior_entry"]["approved_version"], "1.0.0")

    def test_v22_revoke_blocks_resolution_and_source_unavailable_can_remain_pinned(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v22-skill-revoke-") as directory:
            base = Path(directory)
            root, state = self._operating(base)
            original = test_skill_entry(base / "installed" / "health-skill", skill_id="health-skill")
            state = initialize_test_skill_registry(root, state, [original], owner=self.OWNER)
            unavailable = copy.deepcopy(original)
            unavailable.update(
                status="SOURCE_UNAVAILABLE",
                audit_revision="AUD-SOURCE-OFFLINE",
                entry_revision="SKE-SOURCE-OFFLINE",
            )
            mutation = skill_registry_module.register_skills(
                str(root), owner=self.OWNER,
                activation_token=state["activation_token"],
                expected_state_sha=state["state_sha"],
                expected_lock_sha=state["skill_lock_sha"],
                entries=[unavailable], change_ref="SOURCE-OFFLINE",
            )
            state = merge_control_state(state, mutation)
            _baseline, bound, _sha = skill_registry_module.resolve_bindings(
                (root / ".founder").resolve(), ["health-skill"]
            )
            self.assertEqual(bound[0]["status"], "SOURCE_UNAVAILABLE")

            revoked = copy.deepcopy(unavailable)
            revoked.update(
                status="REVOKED",
                trust_level="rejected",
                risk_level="BLOCKED",
                audit_revision="AUD-REVOKED",
                entry_revision="SKE-REVOKED",
            )
            revoked["approval"] = {"mode": "REJECTED", "evidence_ref": "DEC-REVOKE"}
            mutation = skill_registry_module.register_skills(
                str(root), owner=self.OWNER,
                activation_token=state["activation_token"],
                expected_state_sha=state["state_sha"],
                expected_lock_sha=state["skill_lock_sha"],
                entries=[revoked], change_ref="SECURITY-REVOKE",
            )
            with self.assertRaises(guard_module.Conflict):
                skill_registry_module.resolve_bindings(
                    (root / ".founder").resolve(), ["health-skill"]
                )
            lock = skill_registry_module.inspect_skill_registry(str(root))["skill_lock"]
            self.assertGreaterEqual(len(lock["history"]), 2)
            self.assertTrue(Path(original["installed_path"]).exists())

    def test_v22_partial_pair_commit_keeps_recovery_fence_and_is_repairable(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v22-skill-recovery-") as directory:
            base = Path(directory)
            root, state = self._operating(base)
            state = initialize_test_skill_registry(root, state, owner=self.OWNER)
            entry = test_skill_entry(base / "installed" / "recovery-skill", skill_id="recovery-skill")
            original_replace = skill_registry_module._atomic_replace_bytes
            calls = 0

            def fail_projection_and_rollback(path: Path, content: bytes) -> None:
                nonlocal calls
                calls += 1
                if calls in {2, 3}:
                    raise OSError("injected two-file transaction failure")
                original_replace(path, content)

            with mock.patch.object(
                skill_registry_module,
                "_atomic_replace_bytes",
                side_effect=fail_projection_and_rollback,
            ):
                with self.assertRaises(skill_registry_module.SkillRegistryPartialCommit):
                    skill_registry_module.register_skills(
                        str(root), owner=self.OWNER,
                        activation_token=state["activation_token"],
                        expected_state_sha=state["state_sha"],
                        expected_lock_sha=state["skill_lock_sha"],
                        entries=[entry], change_ref="INJECT-PARTIAL",
                    )
            transaction = root / ".founder" / ".skill-registry-lock.json"
            self.assertTrue(transaction.is_file())
            mixed_lock_sha = guard_module.sha256_bytes(
                (root / ".founder" / "SKILL_LOCK.json").read_bytes()
            )
            repaired = skill_registry_module.recover_skill_registry_lock(
                str(root), owner=self.OWNER,
                activation_token=state["activation_token"],
                expected_state_sha=state["state_sha"],
                expected_lock_sha=mixed_lock_sha,
                lock_owner=self.OWNER,
                predecessor_liveness="current",
                authorization_ref="TEST-RECOVERY-AUTH",
            )
            self.assertEqual(repaired["result"], "SKILL_REGISTRY_LOCK_RECOVERED")
            self.assertFalse(transaction.exists())
            inspected = skill_registry_module.inspect_skill_registry(str(root))
            self.assertEqual(inspected["pair_state"], "CURRENT")
            self.assertIn("recovery-skill", inspected["skill_lock"]["skills"])

    def test_v22_concurrent_register_has_one_cas_winner(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v22-skill-race-") as directory:
            base = Path(directory)
            root, state = self._operating(base)
            state = initialize_test_skill_registry(root, state, owner=self.OWNER)
            entries = [
                test_skill_entry(
                    base / "installed" / f"race-{index}",
                    skill_id=f"race-{index}",
                    capability=f"race-capability-{index}",
                )
                for index in (1, 2)
            ]
            environment = os.environ.copy()
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
            environment["PYTHONUTF8"] = "1"
            processes = []
            for entry in entries:
                processes.append(
                    subprocess.Popen(
                        [
                            PYTHON, "-B", str(SKILL_REGISTRY), "register",
                            "--project", str(root), "--owner", self.OWNER,
                            "--activation-token", state["activation_token"],
                            "--expected-state-sha", state["state_sha"],
                            "--expected-lock-sha", state["skill_lock_sha"],
                            "--entry-json", json.dumps(entry, sort_keys=True),
                            "--change-ref", f"RACE-{entry['skill_id']}",
                        ],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        env=environment,
                    )
                )
            results = [process.communicate(timeout=30) for process in processes]
            returncodes = sorted(process.returncode for process in processes)
            self.assertEqual(returncodes, [0, 3], results)
            self.assertFalse((root / ".founder" / ".skill-registry-lock.json").exists())
            lock = skill_registry_module.inspect_skill_registry(str(root))["skill_lock"]
            self.assertEqual(len(lock["skills"]), 1)


class CapabilitySkillE2EV22Tests(unittest.TestCase):
    """Named A-J acceptance scenarios for the deterministic V2.2 control plane."""

    def test_scenario_a_missing_to_install_register_bind_dispatch_and_integration(self) -> None:
        inspector, controller = load_curator_modules()
        owner = "founder-os-main-v22-e2e-a"
        with tempfile.TemporaryDirectory(prefix="founder-os-v22-scenario-a-") as directory:
            base = Path(directory)
            project = base / "project"
            project.mkdir()
            state = make_operating_clear_project(project, owner)
            capability_plan = capability_planner_module.plan_capabilities(
                task_id="scenario-a-task",
                required_capabilities=["example-capability"],
                observed_coverage={},
                task_size="COMPLEX",
                risk_level="LOW",
                general_capability_sufficient=False,
                strategic_gate="OPERATING",
            )
            self.assertEqual(capability_plan["capabilities"][0]["status"], "MISSING")
            self.assertTrue(capability_plan["curator_required"])

            candidate = write_safe_skill(base / "candidates" / "example-skill")
            write_safe_skill(base / "candidates" / "alternative-skill", "alternative-skill")
            discovered = controller.discover_local([str(base / "candidates")], 10)
            self.assertEqual(discovered["candidate_count"], 2)
            compared = controller.compare_candidates(
                [
                    SkillCuratorV22Tests._comparison(
                        "example-skill", capability_coverage=100,
                        source_trust="LOCAL_REVIEWED", project_compatibility=5,
                    ),
                    SkillCuratorV22Tests._comparison(
                        "alternative-skill", capability_coverage=70,
                    ),
                ]
            )
            self.assertEqual(compared["primary_recommendation"]["skill_id"], "example-skill")

            install_root = base / "install-root"
            install_root.mkdir()
            curator_case = SkillCuratorV22Tests("test_v22_curator_is_independent_and_exposes_complete_workflow")
            curator_case.inspector, curator_case.controller = inspector, controller
            installed = controller.install_candidate(
                curator_case._install_args(
                    project=project, candidate=candidate, install_root=install_root
                )
            )
            entry = installed["registry_entry_validated"]
            self.assertEqual(entry["status"], "VALIDATED")
            self.assertEqual(entry["runtime_visibility"]["state"], "NOT_CONFIRMED")
            state = initialize_test_skill_registry(project, state, owner=owner)
            registered = controller.register_with_founderos(
                argparse.Namespace(
                    founder_registry_script=str(SKILL_REGISTRY.resolve()),
                    founder_registry_sha256=hashlib.sha256(
                        SKILL_REGISTRY.read_bytes()
                    ).hexdigest(),
                    project=str(project.resolve()),
                    owner=owner,
                    activation_token=state["activation_token"],
                    expected_state_sha=state["state_sha"],
                    expected_lock_sha=state["skill_lock_sha"],
                    entry_json=json.dumps(entry, sort_keys=True),
                    change_ref="SCENARIO-A-RUNTIME-VISIBILITY-CONFIRMED",
                    runtime_visibility_state="CONFIRMED",
                    runtime_visibility_runtime="isolated-validator-runtime",
                    runtime_visibility_evidence_ref="SCENARIO-A-FRESH-RUNTIME-DISCOVERY",
                    runtime_visibility_observed_at="2026-08-12T00:00:00Z",
                )
            )
            self.assertEqual(registered["registered_status"], "AVAILABLE")
            self.assertEqual(
                registered["runtime_visibility_evidence_origin"],
                "CALLER_SUPPLIED_EXTERNAL_OBSERVATION",
            )
            state = merge_control_state(state, registered["founder_registry"])
            initialized = initialize_thread_registry(project, state, owner)
            state = merge_control_state(state, initialized)
            reserved = registry_module.reserve_thread(
                str(project), owner=owner,
                activation_token=state["activation_token"],
                expected_state_sha=state["state_sha"],
                expected_registry_sha=state["registry_sha"],
                agent_id="scenario-a-worker", agent_kind="persistent",
                logical_name="Scenario A capability worker",
                manager_agent_id="founder-os-main", workstream="engineering",
                thread_type="persistent", read_scope=["src/**"],
                write_scope=["src/engineering/**"], skills=["example-skill"],
                dependencies=[], capabilities=["example-capability"],
            )
            state = merge_control_state(state, reserved)
            bound = registry_module.bind_runtime(
                str(project), owner=owner,
                activation_token=state["activation_token"],
                expected_state_sha=state["state_sha"],
                expected_registry_sha=state["registry_sha"],
                thread_record_id=reserved["details"]["thread_record_id"],
                binding_nonce=reserved["details"]["binding_nonce"],
                runtime_thread_id="scenario-a-runtime-thread",
                runtime_host_id="isolated-validator", identity_quality="observed",
            )
            state = merge_control_state(state, bound)
            assigned = registry_module.assign_task(
                str(project), owner=owner,
                activation_token=state["activation_token"],
                expected_state_sha=state["state_sha"],
                expected_registry_sha=state["registry_sha"],
                thread_record_id=reserved["details"]["thread_record_id"],
                task_id="scenario-a-task", summary="Produce the deterministic capability probe",
                acceptance_ref="exact skill baseline and unchanged probe value",
            )
            self.assertEqual(assigned["details"]["task_id"], "scenario-a-task")
            deterministic_result = {"input": "probe", "output": "probe"}
            self.assertEqual(deterministic_result["input"], deterministic_result["output"])
            self.assertTrue(
                integration_gate(
                    ["accepted"],
                    {
                        "skill_lock_current": True,
                        "thread_baseline_current": True,
                        "deterministic_contract": True,
                    },
                )
            )

    def test_scenario_b_malicious_candidate_is_blocked_without_execution_or_install(self) -> None:
        inspector, controller = load_curator_modules()
        with tempfile.TemporaryDirectory(prefix="founder-os-v22-scenario-b-") as directory:
            base = Path(directory)
            candidate = write_malicious_skill(base / "candidate")
            install_root = base / "install-root"
            install_root.mkdir()
            project = base / "project"
            project.mkdir()
            make_operating_clear_project(project, "founder-os-main-v22-scenario-b")
            before = snapshot_tree(base)
            report = inspector.inspect_skill(candidate)
            decision = controller.risk_gate(
                argparse.Namespace(
                    audit_status="AUDITED", risk_level="BLOCKED",
                    approval_mode="EXPLICIT", source_trust="third-party-audited",
                    semantic_audit_evidence="Adversarial fixture audit findings",
                    purpose="example-capability", source="isolated fixture",
                    permission=["environment", "network", "broad-shell"],
                )
            )
            self.assertEqual(decision["disposition"], "REJECT")
            self.assertFalse(decision["install_policy_satisfied"])
            self.assertTrue(any(row["severity_hint"] == "BLOCKED_HINT" for row in report["findings"]))
            curator_case = SkillCuratorV22Tests(
                "test_v22_curator_is_independent_and_exposes_complete_workflow"
            )
            curator_case.inspector, curator_case.controller = inspector, controller
            lied = curator_case._install_args(
                project=project,
                candidate=candidate,
                install_root=install_root,
                skill_id="malicious-fixture",
            )
            lied.audit_status = "AUDITED"
            lied.risk_level = "LOW"
            lied.approval_mode = "AUTO"
            lied.approval_evidence = "FALSE-LOW-CLAIM-MUST-NOT-BYPASS-FACTS"
            lied.dynamic_validation = "NOT_APPLICABLE"
            install_before = snapshot_tree(install_root)
            with self.assertRaises(controller.ControllerError) as captured:
                controller.install_candidate(lied)
            self.assertIn(
                captured.exception.code,
                {"STATIC_AUDIT_POLICY_BLOCKED", "STATIC_AUDIT_RISK_UNDERRATED"},
            )
            self.assertEqual(snapshot_tree(install_root), install_before)
            self.assertEqual(snapshot_tree(base), before)
            self.assertEqual(list(install_root.iterdir()), [])

    def test_scenario_c_v2_is_update_available_until_reaudit_and_approval(self) -> None:
        case = SkillRegistryV22Tests(
            "test_v22_update_available_keeps_v1_until_reaudit_and_preserves_history"
        )
        case.test_v22_update_available_keeps_v1_until_reaudit_and_preserves_history()

    def test_scenario_d_installed_byte_tamper_becomes_hash_mismatch(self) -> None:
        inspector, controller = load_curator_modules()
        owner = "founder-os-main-v22-e2e-d"
        with tempfile.TemporaryDirectory(prefix="founder-os-v22-scenario-d-") as directory:
            base = Path(directory)
            project = base / "project"
            project.mkdir()
            state = make_operating_clear_project(project, owner)
            candidate = write_safe_skill(base / "candidate")
            install_root = base / "install-root"
            install_root.mkdir()
            curator_case = SkillCuratorV22Tests("test_v22_curator_is_independent_and_exposes_complete_workflow")
            curator_case.inspector, curator_case.controller = inspector, controller
            result = controller.install_candidate(
                curator_case._install_args(
                    project=project, candidate=candidate, install_root=install_root
                )
            )
            installed = Path(result["installed_path"])
            entry = copy.deepcopy(result["registry_entry_validated"])
            entry["status"] = "AVAILABLE"
            entry["runtime_visibility"] = {
                "state": "CONFIRMED",
                "runtime": "isolated-validator-runtime",
                "evidence_ref": "SCENARIO-D-FRESH-RUNTIME-DISCOVERY",
                "observed_at": "2026-08-12T00:00:00Z",
            }
            state = initialize_test_skill_registry(project, state, [entry], owner=owner)
            state = merge_control_state(
                state, initialize_thread_registry(project, state, owner)
            )
            reserved = registry_module.reserve_thread(
                str(project), owner=owner,
                activation_token=state["activation_token"],
                expected_state_sha=state["state_sha"],
                expected_registry_sha=state["registry_sha"],
                agent_id="scenario-d-worker", agent_kind="persistent",
                logical_name="Scenario D tamper sentinel",
                manager_agent_id="founder-os-main", workstream="engineering",
                thread_type="persistent", read_scope=["src/**"],
                write_scope=["src/engineering/**"], skills=["example-skill"],
                dependencies=[], capabilities=["example-capability"],
            )
            state = merge_control_state(state, reserved)
            state = merge_control_state(
                state,
                registry_module.bind_runtime(
                    str(project), owner=owner,
                    activation_token=state["activation_token"],
                    expected_state_sha=state["state_sha"],
                    expected_registry_sha=state["registry_sha"],
                    thread_record_id=reserved["details"]["thread_record_id"],
                    binding_nonce=reserved["details"]["binding_nonce"],
                    runtime_thread_id="scenario-d-runtime-thread",
                    runtime_host_id="isolated-validator", identity_quality="observed",
                ),
            )
            (installed / "SKILL.md").write_text(
                (installed / "SKILL.md").read_text(encoding="utf-8") + "tampered\n",
                encoding="utf-8",
            )
            before_dispatch = snapshot_tree(project)
            with self.assertRaisesRegex(guard_module.Conflict, "HASH_MISMATCH"):
                registry_module.assign_task(
                    str(project), owner=owner,
                    activation_token=state["activation_token"],
                    expected_state_sha=state["state_sha"],
                    expected_registry_sha=state["registry_sha"],
                    thread_record_id=reserved["details"]["thread_record_id"],
                    task_id="scenario-d-post-tamper-task",
                    summary="This dispatch must never start",
                    acceptance_ref="tampered installed Skill must block dispatch",
                )
            self.assertEqual(snapshot_tree(project), before_dispatch)
            verified = controller.verify_installed(
                argparse.Namespace(
                    installed=str(installed),
                    expected_content_hash=result["content_hash"],
                    quick_validate=str(QUICK_VALIDATE.resolve()),
                    quick_validate_sha256=hashlib.sha256(QUICK_VALIDATE.read_bytes()).hexdigest(),
                )
            )
            self.assertEqual(verified["result"], "HASH_MISMATCH")
            self.assertEqual(verified["recommended_status"], "REVOKED")
            self.assertFalse(verified["binding_allowed"])

    def test_scenario_e_conflicting_primary_skills_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v22-scenario-e-") as directory:
            base = Path(directory)
            project = base / "project"
            project.mkdir()
            state = make_operating_clear_project(project, "founder-os-main-v22-e2e-e")
            first = test_skill_entry(
                base / "installed-a", skill_id="primary-a", capability="same-capability"
            )
            second = test_skill_entry(
                base / "installed-b", skill_id="primary-b", capability="same-capability",
                audit_revision="AUD-B", entry_revision="SKE-B",
            )
            before = snapshot_tree(project)
            with self.assertRaises(guard_module.InvalidState):
                initialize_test_skill_registry(
                    project, state, [first, second], owner="founder-os-main-v22-e2e-e"
                )
            self.assertEqual(snapshot_tree(project), before)
            self.assertFalse((project / ".founder" / "SKILL_LOCK.json").exists())

    def test_scenario_f_same_persistent_runtime_acks_added_skill_without_recreation(self) -> None:
        case = ThreadSkillSyncV22Tests(
            "test_v22_added_skill_requires_exact_ack_on_same_runtime_thread"
        )
        case.test_v22_added_skill_requires_exact_ack_on_same_runtime_thread()

    def test_scenario_g_revoke_sync_disables_skill_and_requires_replacement(self) -> None:
        case = ThreadSkillSyncV22Tests(
            "test_v22_revoked_primary_stays_blocked_after_sync_until_replaced"
        )
        case.test_v22_revoked_primary_stays_blocked_after_sync_until_replaced()

    def test_scenario_h_simple_task_never_calls_curator_or_writes_registry(self) -> None:
        _inspector, controller = load_curator_modules()
        with tempfile.TemporaryDirectory(prefix="founder-os-v22-scenario-h-") as directory:
            root = Path(directory)
            before = snapshot_tree(root)
            with mock.patch.object(controller, "discover_local") as discovery:
                plan = capability_planner_module.plan_capabilities(
                    task_id="simple-task", required_capabilities=["plain-writing"],
                    observed_coverage={}, task_size="SIMPLE", risk_level="LOW",
                    general_capability_sufficient=True, strategic_gate="OPERATING",
                )
            discovery.assert_not_called()
            self.assertEqual(plan["result"], "NO_SKILL_REQUIRED")
            self.assertEqual(snapshot_tree(root), before)

    def test_scenario_i_strategic_gate_blocks_install_with_zero_write(self) -> None:
        SkillCuratorV22Tests.setUpClass()
        case = SkillCuratorV22Tests("test_v22_install_before_strategic_gate_is_zero_write")
        case.test_v22_install_before_strategic_gate_is_zero_write()

    def test_scenario_j_medium_risk_returns_decision_summary_and_zero_write(self) -> None:
        _inspector, controller = load_curator_modules()
        with tempfile.TemporaryDirectory(prefix="founder-os-v22-scenario-j-") as directory:
            root = Path(directory)
            before = snapshot_tree(root)
            result = controller.risk_gate(
                argparse.Namespace(
                    audit_status="AUDITED", risk_level="MEDIUM", approval_mode="AUTO",
                    source_trust="third-party-audited",
                    semantic_audit_evidence="Static audit complete; ordinary script surface",
                    purpose="example-capability", source="example.invalid/repository@commit",
                    permission=["project-read", "isolated-shell"],
                )
            )
            self.assertFalse(result["install_policy_satisfied"])
            self.assertEqual(result["disposition"], "FOUNDER_APPROVAL_REQUIRED")
            self.assertEqual(
                set(result["boss_summary"]),
                {"purpose", "source", "risk", "permissions", "recommendation", "decision_required"},
            )
            self.assertTrue(result["boss_summary"]["decision_required"])
            self.assertEqual(snapshot_tree(root), before)

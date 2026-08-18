"""FounderOS regression tests: ThreadManagerStaticTests, ThreadContextGuardTests, ThreadRegistryTests.

Split from validate_founder_os.py; class bodies are copied verbatim so the
frozen AST manifests keep matching. Run via scripts/validate_founder_os.py.
"""

from __future__ import annotations

from validation.common import (  # noqa: F401
    Any,
    PYTHON,
    Path,
    SKILL_ROOT,
    THREAD_CONTEXT_GUARD,
    THREAD_REGISTRY,
    bind_reserved_thread,
    claim,
    context_guard_module,
    copy,
    create_legacy_operating_project,
    create_project,
    exact_state_sync_ack,
    guard_module,
    initialize_thread_registry,
    json,
    mock,
    os,
    registry_module,
    registry_state,
    release_lock,
    reserve_persistent_thread,
    run_guard,
    snapshot_tree,
    subprocess,
    tempfile,
    unittest,
    v23_tempdir,
)


class ThreadManagerStaticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        cls.thread_manager = (SKILL_ROOT / "references" / "thread-manager.md").read_text(
            encoding="utf-8"
        )
        cls.supervision = (SKILL_ROOT / "references" / "supervision.md").read_text(
            encoding="utf-8"
        )
        cls.delegation = (SKILL_ROOT / "references" / "delegation.md").read_text(
            encoding="utf-8"
        )
        cls.workstreams = (SKILL_ROOT / "references" / "workstreams.md").read_text(
            encoding="utf-8"
        )
        cls.state_files = (SKILL_ROOT / "references" / "state-files.md").read_text(
            encoding="utf-8"
        )
        cls.skill_registry = (SKILL_ROOT / "references" / "skill-registry.md").read_text(
            encoding="utf-8"
        )
        cls.all_text = "\n".join(
            (
                cls.skill,
                cls.thread_manager,
                cls.supervision,
                cls.delegation,
                cls.workstreams,
                cls.state_files,
                cls.skill_registry,
            )
        )

    def test_v2_static_thread_manager_contract_and_progressive_disclosure(self) -> None:
        self.assertIn("references/thread-manager.md", self.skill)
        self.assertTrue((SKILL_ROOT / "scripts" / "thread_registry.py").is_file())
        self.assertIn("Thread Manager 是 V1.x Management Core 之上的控制面", self.thread_manager)

    def test_v2_agent_identity_is_not_thread_binding(self) -> None:
        self.assertIn("Agent != Thread != Skill", self.thread_manager)
        self.assertIn("agent_id", self.thread_manager)
        self.assertIn("runtime_thread_id", self.thread_manager)

    def test_v2_real_thread_requires_runtime_identity(self) -> None:
        self.assertIn("THREAD_CREATED", self.all_text)
        self.assertIn("THREAD_CAPABILITY_UNAVAILABLE", self.all_text)
        self.assertIn("不能伪造 ID", self.thread_manager)

    def test_v2_task_persistent_and_reuse_policy(self) -> None:
        self.assertIn("Task Agent", self.thread_manager)
        self.assertIn("Persistent Role Agent", self.thread_manager)
        self.assertIn("REUSE BEFORE CREATE", self.all_text)
        self.assertIn("一个 current primary Thread", self.skill)

    def test_v2_logical_handoff_is_not_workspace_handoff(self) -> None:
        self.assertIn("git/worktree", self.thread_manager)
        self.assertIn("禁止替代 Supervisor CAS", self.supervision)

    def test_v2_capability_and_partial_degradation_contract(self) -> None:
        for anchor in (
            "THREAD_CREATE_AVAILABLE",
            "THREAD_READ_AVAILABLE",
            "THREAD_RESUME_AVAILABLE",
            "THREAD_ARCHIVE_AVAILABLE",
            "THREAD_INTERRUPT_AVAILABLE",
            "THREAD_FORK_AVAILABLE",
            "SUPPORTED / PARTIAL / UNSUPPORTED / UNKNOWN",
        ):
            self.assertIn(anchor, self.thread_manager)

    def test_v2_worker_scope_and_integration_contract(self) -> None:
        self.assertIn("Thread `COMPLETED` 本身不等于 accepted", self.workstreams)
        self.assertIn("Thread Registry", self.workstreams)
        self.assertIn("handoff predecessor", self.all_text)

    def test_v2_forbids_ui_automation_and_api_key_substitution(self) -> None:
        self.assertIn("鼠标/OCR/屏幕点击", self.thread_manager)
        self.assertIn("OpenAI API Key", self.thread_manager)
        self.assertIn("当前已登录的 Codex runtime", self.thread_manager)


class ThreadContextGuardTests(unittest.TestCase):
    THREAD_ID = "019fdc3f-55ef-7ec3-985f-c211278988a8"

    @staticmethod
    def _write_session(root: Path, name: str, content: bytes) -> Path:
        path = root / name
        path.write_bytes(content)
        return path

    def test_context_guard_contract_is_documented_and_progressively_disclosed(self) -> None:
        skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        reference = (SKILL_ROOT / "references" / "thread-manager.md").read_text(
            encoding="utf-8"
        )
        readme = (SKILL_ROOT / ".github" / "README.md").read_text(encoding="utf-8")
        self.assertTrue(THREAD_CONTEXT_GUARD.is_file())
        self.assertIn("Context Size Guard", reference)
        self.assertIn("scripts/thread_context_guard.py", reference)
        self.assertIn("ROTATE_REQUIRED / 10", reference)
        self.assertIn("CONTEXT_HAZARD / 20", reference)
        self.assertIn("generation+1 successor", skill)
        self.assertIn("thread_context_guard.py", readme)

    def test_clear_session_streams_boundaries_without_exposing_payload(self) -> None:
        with v23_tempdir(prefix="founder-os-context-clear-") as directory:
            root = Path(directory)
            session = self._write_session(
                root,
                "clear.jsonl",
                b'{"image":"data:image/png;base64,SECRET_IMAGE_BYTES"}\n'
                b'{"status":"ok"}\n',
            )
            before = (session.read_bytes(), session.stat().st_mtime_ns)
            result = context_guard_module.inspect_session(
                session,
                soft_limit_bytes=1024,
                hard_limit_bytes=2048,
                max_record_bytes=256,
                chunk_bytes=11,
            )
            after = (session.read_bytes(), session.stat().st_mtime_ns)
            self.assertEqual(result["result"], "CLEAR")
            self.assertEqual(result["metrics"]["record_count"], 2)
            self.assertGreaterEqual(result["metrics"]["media_marker_count"], 2)
            self.assertTrue(result["metrics"]["complete_scan"])
            self.assertNotIn("SECRET_IMAGE_BYTES", json.dumps(result))
            self.assertEqual(result["changed_paths"], [])
            self.assertEqual(before, after)

    def test_soft_limit_requires_rotation_without_opening_body(self) -> None:
        with v23_tempdir(prefix="founder-os-context-soft-") as directory:
            root = Path(directory)
            session = self._write_session(root, "soft.jsonl", b"x" * 80)
            with mock.patch.object(
                context_guard_module.os,
                "open",
                side_effect=AssertionError("soft-limit transcript body was opened"),
            ):
                result = context_guard_module.inspect_session(
                    session,
                    soft_limit_bytes=64,
                    hard_limit_bytes=128,
                    max_record_bytes=32,
                    chunk_bytes=8,
                )
            self.assertEqual(result["result"], "ROTATE_REQUIRED")
            self.assertEqual(result["inspection_method"], "STAT_ONLY_SOFT_STOP")
            self.assertEqual(result["runtime_policy"]["read_thread"], "BLOCK")
            self.assertEqual(result["metrics"]["scanned_bytes"], 0)

    def test_hard_limit_blocks_without_opening_body(self) -> None:
        with v23_tempdir(prefix="founder-os-context-hard-") as directory:
            root = Path(directory)
            session = self._write_session(root, "hard.jsonl", b"x" * 160)
            with mock.patch.object(
                context_guard_module.os,
                "open",
                side_effect=AssertionError("hard-limit transcript body was opened"),
            ):
                result = context_guard_module.inspect_session(
                    session,
                    soft_limit_bytes=64,
                    hard_limit_bytes=128,
                    max_record_bytes=32,
                    chunk_bytes=8,
                )
            self.assertEqual(result["result"], "CONTEXT_HAZARD")
            self.assertEqual(result["inspection_method"], "STAT_ONLY_HARD_STOP")
            self.assertEqual(result["reason"], "TOTAL_HARD_LIMIT_REACHED")
            self.assertEqual(result["metrics"]["scanned_bytes"], 0)

    def test_oversized_record_stops_streaming_before_the_rest_of_file(self) -> None:
        with v23_tempdir(prefix="founder-os-context-record-") as directory:
            root = Path(directory)
            session = self._write_session(
                root,
                "record.jsonl",
                (b"x" * 40) + b"\n" + (b"later" * 40) + b"\n",
            )
            result = context_guard_module.inspect_session(
                session,
                soft_limit_bytes=512,
                hard_limit_bytes=1024,
                max_record_bytes=32,
                chunk_bytes=8,
            )
            self.assertEqual(result["result"], "CONTEXT_HAZARD")
            self.assertEqual(result["reason"], "MAX_RECORD_LIMIT_REACHED")
            self.assertFalse(result["metrics"]["complete_scan"])
            self.assertIsNone(result["metrics"]["record_count"])
            self.assertLess(result["metrics"]["scanned_bytes"], session.stat().st_size)

    def test_thread_id_locator_finds_one_session_and_missing_is_unverified(self) -> None:
        with v23_tempdir(prefix="founder-os-context-locate-") as directory:
            home = Path(directory)
            day = home / "sessions" / "2026" / "08" / "13"
            day.mkdir(parents=True)
            self._write_session(
                day,
                f"rollout-2026-08-13T00-00-00-{self.THREAD_ID}.jsonl",
                b'{"status":"ok"}\n',
            )
            found = context_guard_module.inspect_thread(
                thread_id=self.THREAD_ID,
                codex_home=home,
            )
            missing = context_guard_module.inspect_thread(
                thread_id="01900000-0000-7000-8000-000000000000",
                codex_home=home,
            )
            self.assertEqual(found["result"], "CLEAR")
            self.assertEqual(found["thread_id"], self.THREAD_ID)
            self.assertEqual(missing["result"], "UNVERIFIED")
            self.assertEqual(missing["reason"], "TRANSCRIPT_NOT_FOUND")

    def test_duplicate_transcripts_fail_closed_until_explicit_path_is_given(self) -> None:
        with v23_tempdir(prefix="founder-os-context-duplicate-") as directory:
            home = Path(directory)
            active = home / "sessions" / "2026" / "08" / "13"
            archived = home / "archived_sessions"
            active.mkdir(parents=True)
            archived.mkdir()
            name = f"rollout-2026-08-13T00-00-00-{self.THREAD_ID}.jsonl"
            self._write_session(active, name, b'{"active":true}\n')
            self._write_session(archived, name, b'{"archived":true}\n')
            result = context_guard_module.inspect_thread(
                thread_id=self.THREAD_ID,
                codex_home=home,
            )
            self.assertEqual(result["result"], "UNVERIFIED")
            self.assertEqual(
                result["reason"],
                "MULTIPLE_TRANSCRIPTS_REQUIRE_EXPLICIT_SESSION_PATH",
            )
            self.assertEqual(result["metrics"]["candidate_count"], 2)

    def test_hardlink_is_unverified_and_nonclear_results_have_nonzero_exit_codes(self) -> None:
        with v23_tempdir(prefix="founder-os-context-link-") as directory:
            root = Path(directory)
            original = self._write_session(root, "original.jsonl", b'{"ok":true}\n')
            linked = root / "linked.jsonl"
            os.link(original, linked)
            result = context_guard_module.inspect_session(linked)
            self.assertEqual(result["result"], "UNVERIFIED")
            self.assertIn("single-link", result["reason"])
            self.assertEqual(context_guard_module._exit_code("CLEAR"), 0)
            for state in ("ROTATE_REQUIRED", "CONTEXT_HAZARD", "UNVERIFIED"):
                self.assertNotEqual(context_guard_module._exit_code(state), 0)


class ThreadRegistryTests(unittest.TestCase):
    OWNER = "founder-os-main-test"

    def test_v2_cli_wires_state_and_skill_sync_task_scope_correctly(self) -> None:
        state_args = [
            "state-sync", "--project", "P", "--owner", "O",
            "--activation-token", "T", "--expected-state-sha", "S",
            "--expected-registry-sha", "R", "--thread-record-id", "TR",
            "--acknowledgement", "ACK",
        ]
        with mock.patch.object(
            registry_module, "state_sync", return_value={"result": "STATE_SYNC_OK"}
        ) as state_call, mock.patch.object(
            registry_module, "emit", return_value=0
        ):
            self.assertEqual(registry_module.main(state_args), 0)
        self.assertNotIn("task_id", state_call.call_args.kwargs)

        skill_args = [
            "skill-sync", "--project", "P", "--owner", "O",
            "--activation-token", "T", "--expected-state-sha", "S",
            "--expected-registry-sha", "R", "--thread-record-id", "TR",
            "--acknowledgement", "ACK", "--task-id", "TASK-1",
        ]
        with mock.patch.object(
            registry_module, "skill_sync", return_value={"result": "SKILL_SYNC_OK"}
        ) as skill_call, mock.patch.object(
            registry_module, "emit", return_value=0
        ):
            self.assertEqual(registry_module.main(skill_args), 0)
        self.assertEqual(skill_call.call_args.kwargs["task_id"], "TASK-1")

    def test_v2_task_write_scope_cannot_expand_thread_scope(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v2-task-scope-") as directory:
            root = Path(directory)
            _active, state, record_id = self._bound_thread(root)
            before = snapshot_tree(root)
            with self.assertRaisesRegex(
                guard_module.Conflict, "cannot expand"
            ):
                registry_module.assign_task(
                    str(root), owner=self.OWNER,
                    activation_token=state["activation_token"],
                    expected_state_sha=state["state_sha"],
                    expected_registry_sha=state["registry_sha"],
                    thread_record_id=record_id,
                    task_id="scope-expansion",
                    summary="Attempt to expand a bounded Thread",
                    acceptance_ref="must remain zero-write",
                    task_write_scope=[".founder/**"],
                )
            self.assertEqual(snapshot_tree(root), before)

            assigned = registry_module.assign_task(
                str(root), owner=self.OWNER,
                activation_token=state["activation_token"],
                expected_state_sha=state["state_sha"],
                expected_registry_sha=state["registry_sha"],
                thread_record_id=record_id,
                task_id="scope-narrowing",
                summary="Use a provable subset of the Thread scope",
                acceptance_ref="narrow task scope is preserved",
                task_write_scope=["src/engineering/component/**"],
            )
            inspected = registry_module.inspect_registry(str(root))["registry"]
            current = next(
                row for row in inspected["threads"]
                if row["thread_record_id"] == record_id
            )
            self.assertEqual(
                current["current_task"]["write_scope"],
                ["src/engineering/component/**"],
            )

    def _bound_thread(
        self,
        root: Path,
        *,
        agent_id: str = "technical-lead-01",
        runtime_thread_id: str = "019ff012-0000-7000-8000-000000000001",
    ) -> tuple[dict[str, Any], dict[str, Any], str]:
        active = create_legacy_operating_project(root, self.OWNER)
        initialized = initialize_thread_registry(root, active, self.OWNER)
        state = registry_state(root, active, initialized)
        reserved = reserve_persistent_thread(
            root, state, agent_id=agent_id, owner=self.OWNER
        )
        record_id = reserved["details"]["thread_record_id"]
        reserved_state = registry_state(root, state, reserved)
        bound = bind_reserved_thread(
            root,
            reserved_state,
            runtime_thread_id=runtime_thread_id,
            owner=self.OWNER,
        )
        return active, registry_state(root, reserved_state, bound), record_id

    def _transition(
        self,
        root: Path,
        state: dict[str, Any],
        record_id: str,
        target: str,
        evidence: str = "validator evidence",
    ) -> dict[str, Any]:
        mutation = registry_module.transition_thread(
            str(root),
            owner=self.OWNER,
            activation_token=state["activation_token"],
            expected_state_sha=state["state_sha"],
            expected_registry_sha=state["registry_sha"],
            thread_record_id=record_id,
            target=target,
            evidence=evidence,
        )
        return registry_state(root, state, mutation)

    def test_v2_legacy_project_without_registry_keeps_v1_fingerprint_shape(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v2-tests-") as temp:
            root = Path(temp)
            create_project(root)
            sources = guard_module.read_source_revisions(root / ".founder")
            self.assertNotIn("THREADS_REVISION", sources)
            self.assertNotIn("THREADS_SHA256", sources)
            completed, active = claim(root, self.OWNER)
            self.assertEqual(completed.returncode, 0, completed.stdout)
            self.assertNotIn("THREADS_REVISION", json.loads(
                (root / ".founder" / "ACTIVE_SUPERVISOR.json").read_text(encoding="utf-8")
            )["source_revisions"])
            self.assertFalse((root / ".founder" / "THREADS.json").exists())
            self.assertEqual(active["mode"], "ACTIVE")

    def test_v2_registry_init_requires_active_fence_with_zero_failed_writes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v2-tests-") as temp:
            root = Path(temp)
            active = create_legacy_operating_project(root, self.OWNER)
            before = snapshot_tree(root)
            with self.assertRaises(guard_module.Conflict):
                registry_module.initialize_registry(
                    str(root),
                    owner=self.OWNER,
                    activation_token="T_wrong",
                    expected_state_sha=active["state_sha"],
                    expected_registry_sha="ABSENT",
                )
            self.assertEqual(before, snapshot_tree(root))

    def test_v2_read_only_registry_inspect_is_byte_stable(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v2-tests-") as temp:
            root = Path(temp)
            active = create_legacy_operating_project(root, self.OWNER)
            before_absent = snapshot_tree(root)
            self.assertEqual(registry_module.inspect_registry(str(root))["registry_sha"], "ABSENT")
            self.assertEqual(before_absent, snapshot_tree(root))
            initialize_thread_registry(root, active, self.OWNER)
            before_present = snapshot_tree(root)
            inspected = registry_module.inspect_registry(str(root))
            self.assertEqual(inspected["result"], "THREAD_REGISTRY_INSPECTED")
            self.assertEqual(before_present, snapshot_tree(root))

    def test_v2_registry_initialization_is_project_bound_and_checkpointed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v2-tests-") as temp:
            root = Path(temp)
            active = create_legacy_operating_project(root, self.OWNER)
            initialized = initialize_thread_registry(root, active, self.OWNER)
            registry = registry_module.inspect_registry(str(root))["registry"]
            self.assertEqual(registry["project_binding"]["project_root"], str(root.resolve()))
            self.assertEqual(
                registry["project_binding"]["project_binding_id"],
                registry_module._project_binding_id(root.resolve()),
            )
            sources = guard_module.read_source_revisions(root / ".founder")
            self.assertEqual(sources["THREADS_REVISION"], registry["registry_revision"])
            self.assertEqual(sources["THREADS_SHA256"], initialized["registry_sha"])
            verified = guard_module.verify_fence(
                str(root), owner=self.OWNER, activation_token=active["activation_token"]
            )
            self.assertEqual(verified["state_sha"], initialized["state_sha"])

    def test_v2_malformed_registry_fails_closed_without_rewrite(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v2-tests-") as temp:
            root = Path(temp)
            active = create_legacy_operating_project(root, self.OWNER)
            initialize_thread_registry(root, active, self.OWNER)
            path = root / ".founder" / "THREADS.json"
            path.write_bytes(b"{broken")
            before = snapshot_tree(root)
            with self.assertRaises(guard_module.InvalidState):
                registry_module.inspect_registry(str(root))
            self.assertEqual(before, snapshot_tree(root))

    def test_v2_registry_hardlink_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v2-tests-") as temp:
            base = Path(temp)
            root = base / "project"
            root.mkdir()
            active = create_legacy_operating_project(root, self.OWNER)
            initialize_thread_registry(root, active, self.OWNER)
            path = root / ".founder" / "THREADS.json"
            outside = base / "outside.json"
            outside.write_bytes(path.read_bytes())
            path.unlink()
            os.link(outside, path)
            before = snapshot_tree(base)
            with self.assertRaises(guard_module.InvalidState):
                registry_module.inspect_registry(str(root))
            self.assertEqual(before, snapshot_tree(base))

    def test_v2_same_revision_registry_content_drift_requires_recovery(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v2-tests-") as temp:
            root = Path(temp)
            active = create_legacy_operating_project(root, self.OWNER)
            initialize_thread_registry(root, active, self.OWNER)
            path = root / ".founder" / "THREADS.json"
            value = json.loads(path.read_text(encoding="utf-8"))
            value["tampered_but_valid_json"] = True
            path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            before = snapshot_tree(root)
            inspected = guard_module.inspect_state(
                str(root),
                candidate=self.OWNER,
                activation_token=active["activation_token"],
                intent="execute",
                requested_mode="ACTIVE",
            )
            self.assertEqual(inspected["mode"], "RECOVERY")
            self.assertEqual(before, snapshot_tree(root))

    def test_v2_registry_cas_mismatch_has_zero_writes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v2-tests-") as temp:
            root = Path(temp)
            active = create_legacy_operating_project(root, self.OWNER)
            initialized = initialize_thread_registry(root, active, self.OWNER)
            state = registry_state(root, active, initialized)
            before = snapshot_tree(root)
            state["registry_sha"] = "0" * 64
            with self.assertRaises(guard_module.Conflict):
                reserve_persistent_thread(root, state, owner=self.OWNER)
            self.assertEqual(before, snapshot_tree(root))

    def test_v2_registry_cas_race_has_one_winner(self) -> None:
        environment = os.environ.copy()
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["PYTHONUTF8"] = "1"
        with tempfile.TemporaryDirectory(prefix="founder-os-v2-tests-") as temp:
            root = Path(temp)
            active = create_legacy_operating_project(root, self.OWNER)
            initialized = initialize_thread_registry(root, active, self.OWNER)
            common = [
                PYTHON,
                str(THREAD_REGISTRY),
                "reserve",
                "--project",
                str(root),
                "--owner",
                self.OWNER,
                "--activation-token",
                active["activation_token"],
                "--expected-state-sha",
                initialized["state_sha"],
                "--expected-registry-sha",
                initialized["registry_sha"],
                "--agent-kind",
                "persistent",
                "--manager-agent-id",
                "founder-os-main",
                "--workstream",
                "engineering",
                "--thread-type",
                "persistent",
            ]
            commands = []
            for suffix in ("a", "b"):
                commands.append(
                    common
                    + [
                        "--agent-id",
                        f"technical-lead-{suffix}",
                        "--logical-name",
                        f"Engineering - Technical Lead {suffix.upper()}",
                    ]
                )
            processes = [
                subprocess.Popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=environment,
                )
                for command in commands
            ]
            outputs = [process.communicate(timeout=20) for process in processes]
            codes = [process.returncode for process in processes]
            self.assertEqual(codes.count(0), 1, (codes, outputs))
            self.assertEqual(codes.count(3), 1, (codes, outputs))
            registry_module.inspect_registry(str(root))
            self.assertEqual(list((root / ".founder").glob("*.staging")), [])
            self.assertFalse((root / ".founder" / registry_module.REGISTRY_LOCK_NAME).exists())

    def test_v2_orphan_registry_transaction_lock_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v2-tests-") as temp:
            root = Path(temp)
            active = create_legacy_operating_project(root, self.OWNER)
            initialized = initialize_thread_registry(root, active, self.OWNER)
            state = registry_state(root, active, initialized)
            lock_path = root / ".founder" / registry_module.REGISTRY_LOCK_NAME
            guard_module._atomic_create(
                lock_path,
                {
                    "project_root": str(root.resolve()),
                    "owner": "unknown-operation",
                    "nonce": "RL_unknown",
                },
            )
            before = snapshot_tree(root)
            with self.assertRaises(guard_module.Conflict):
                reserve_persistent_thread(root, state, owner=self.OWNER)
            self.assertEqual(before, snapshot_tree(root))

    def test_v2_runtime_identity_control_characters_fail_before_write(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v2-tests-") as temp:
            root = Path(temp)
            active = create_legacy_operating_project(root, self.OWNER)
            initialized = initialize_thread_registry(root, active, self.OWNER)
            state = registry_state(root, active, initialized)
            reserved = reserve_persistent_thread(root, state, owner=self.OWNER)
            state = registry_state(root, state, reserved)
            before = snapshot_tree(root)
            with self.assertRaises(guard_module.InvalidState):
                bind_reserved_thread(
                    root,
                    state,
                    runtime_thread_id="thread\n--inject",
                    owner=self.OWNER,
                )
            self.assertEqual(before, snapshot_tree(root))

    def test_v2_duplicate_runtime_binding_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v2-tests-") as temp:
            root = Path(temp)
            active, state, _first = self._bound_thread(root)
            second = reserve_persistent_thread(
                root,
                state,
                agent_id="product-lead-01",
                logical_name="Product - Product Lead",
                owner=self.OWNER,
            )
            second_state = registry_state(root, state, second)
            before = snapshot_tree(root)
            with self.assertRaises(guard_module.Conflict):
                bind_reserved_thread(
                    root,
                    second_state,
                    runtime_thread_id="019ff012-0000-7000-8000-000000000001",
                    owner=self.OWNER,
                )
            self.assertEqual(before, snapshot_tree(root))
            self.assertIsNotNone(active["activation_token"])

    def test_v2_wrong_project_registry_binding_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v2-tests-") as temp:
            base = Path(temp)
            first = base / "first"
            second = base / "second"
            first.mkdir()
            second.mkdir()
            active = create_legacy_operating_project(first, self.OWNER)
            initialize_thread_registry(first, active, self.OWNER)
            create_project(second)
            (second / ".founder" / "THREADS.json").write_bytes(
                (first / ".founder" / "THREADS.json").read_bytes()
            )
            before = snapshot_tree(second)
            with self.assertRaises(guard_module.InvalidState):
                registry_module.inspect_registry(str(second))
            self.assertEqual(before, snapshot_tree(second))

    def test_v2_nonactive_advisor_reviewer_and_worker_cannot_mutate_registry(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v2-tests-") as temp:
            root = Path(temp)
            active = create_legacy_operating_project(root, self.OWNER)
            initialized = initialize_thread_registry(root, active, self.OWNER)
            before = snapshot_tree(root)
            for owner in ("advisor-01", "reviewer-01", "worker-01"):
                with self.subTest(owner=owner), self.assertRaises(guard_module.Conflict):
                    registry_module.reserve_thread(
                        str(root),
                        owner=owner,
                        activation_token=active["activation_token"],
                        expected_state_sha=initialized["state_sha"],
                        expected_registry_sha=initialized["registry_sha"],
                        agent_id=f"{owner}-agent",
                        agent_kind="persistent",
                        logical_name=f"Project - {owner}",
                        manager_agent_id="founder-os-main",
                        workstream="project-level",
                        thread_type="persistent",
                        read_scope=[],
                        write_scope=[],
                        skills=[],
                        dependencies=[],
                    )
            self.assertEqual(before, snapshot_tree(root))

    def test_v2_duplicate_persistent_agent_returns_reuse_fence(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v2-tests-") as temp:
            root = Path(temp)
            active = create_legacy_operating_project(root, self.OWNER)
            initialized = initialize_thread_registry(root, active, self.OWNER)
            state = registry_state(root, active, initialized)
            reserved = reserve_persistent_thread(root, state, owner=self.OWNER)
            state = registry_state(root, state, reserved)
            before = snapshot_tree(root)
            with self.assertRaisesRegex(guard_module.Conflict, "REUSE_EXISTING_PRIMARY"):
                reserve_persistent_thread(root, state, owner=self.OWNER)
            self.assertEqual(before, snapshot_tree(root))

    def test_v2_duplicate_primary_registry_invariant_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v2-tests-") as temp:
            root = Path(temp)
            _active, _state, record_id = self._bound_thread(root)
            registry = registry_module.inspect_registry(str(root))["registry"]
            duplicate = copy.deepcopy(registry["threads"][0])
            duplicate["thread_record_id"] = "THR-duplicate"
            duplicate["runtime"]["thread_id"] = "019ff012-0000-7000-8000-000000000099"
            registry["threads"].append(duplicate)
            registry["agent_bindings"]["technical-lead-01"]["historical_thread_record_ids"].append(
                duplicate["thread_record_id"]
            )
            self.assertEqual(record_id, registry["agent_bindings"]["technical-lead-01"]["primary_thread_record_id"])
            with self.assertRaises(guard_module.InvalidState):
                registry_module.validate_registry(registry, root.resolve())

    def test_v2_revision_reuses_same_real_thread_binding(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v2-tests-") as temp:
            root = Path(temp)
            _active, state, record_id = self._bound_thread(root)
            assigned = registry_module.assign_task(
                str(root),
                owner=self.OWNER,
                activation_token=state["activation_token"],
                expected_state_sha=state["state_sha"],
                expected_registry_sha=state["registry_sha"],
                thread_record_id=record_id,
                task_id="TASK-B-1",
                summary="Produce controlled draft",
                acceptance_ref="AC-B-1",
            )
            state = registry_state(root, state, assigned)
            state = self._transition(root, state, record_id, "COMPLETED", "first runtime return")
            revision = registry_module.request_revision(
                str(root),
                owner=self.OWNER,
                activation_token=state["activation_token"],
                expected_state_sha=state["state_sha"],
                expected_registry_sha=state["registry_sha"],
                thread_record_id=record_id,
                defects=["ROLLBACK_TRIGGER is missing"],
                evidence="first return omitted the required field",
                acceptance_criteria="Include a concrete ROLLBACK_TRIGGER",
            )
            state = registry_state(root, state, revision)
            revised = registry_module.assign_task(
                str(root),
                owner=self.OWNER,
                activation_token=state["activation_token"],
                expected_state_sha=state["state_sha"],
                expected_registry_sha=state["registry_sha"],
                thread_record_id=record_id,
                task_id="TASK-B-1-R1",
                summary="Correct only the detected defect",
                acceptance_ref="AC-B-1-R1",
                revision=True,
            )
            registry = registry_module.inspect_registry(str(root))["registry"]
            thread = registry_module._find_thread(registry, record_id)
            self.assertEqual(revised["details"]["thread_record_id"], record_id)
            self.assertEqual(
                thread["runtime"]["thread_id"],
                "019ff012-0000-7000-8000-000000000001",
            )
            self.assertEqual(thread["lifecycle_state"], "WORKING")

    def test_v2_invalid_archived_to_working_transition_is_atomic(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v2-tests-") as temp:
            root = Path(temp)
            _active, state, record_id = self._bound_thread(root)
            state = self._transition(root, state, record_id, "WAITING")
            archived = registry_module.archive_thread(
                str(root),
                owner=self.OWNER,
                activation_token=state["activation_token"],
                expected_state_sha=state["state_sha"],
                expected_registry_sha=state["registry_sha"],
                thread_record_id=record_id,
                reason="test archive",
            )
            state = registry_state(root, state, archived)
            before = snapshot_tree(root)
            with self.assertRaises(guard_module.Conflict):
                registry_module.transition_thread(
                    str(root),
                    owner=self.OWNER,
                    activation_token=state["activation_token"],
                    expected_state_sha=state["state_sha"],
                    expected_registry_sha=state["registry_sha"],
                    thread_record_id=record_id,
                    target="WORKING",
                    evidence="invalid implicit resume",
                )
            self.assertEqual(before, snapshot_tree(root))

    def test_v2_archive_rejects_active_writer(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v2-tests-") as temp:
            root = Path(temp)
            _active, state, record_id = self._bound_thread(root)
            assigned = registry_module.assign_task(
                str(root),
                owner=self.OWNER,
                activation_token=state["activation_token"],
                expected_state_sha=state["state_sha"],
                expected_registry_sha=state["registry_sha"],
                thread_record_id=record_id,
                task_id="TASK-1",
                summary="Active task",
                acceptance_ref="AC-1",
            )
            state = registry_state(root, state, assigned)
            before = snapshot_tree(root)
            with self.assertRaises(guard_module.Conflict):
                registry_module.archive_thread(
                    str(root),
                    owner=self.OWNER,
                    activation_token=state["activation_token"],
                    expected_state_sha=state["state_sha"],
                    expected_registry_sha=state["registry_sha"],
                    thread_record_id=record_id,
                    reason="unsafe archive",
                )
            self.assertEqual(before, snapshot_tree(root))

    def test_v2_archive_requires_explicit_reopen_and_state_sync_before_dispatch(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v2-tests-") as temp:
            root = Path(temp)
            _active, state, record_id = self._bound_thread(root)
            state = self._transition(root, state, record_id, "WAITING")
            archived = registry_module.archive_thread(
                str(root),
                owner=self.OWNER,
                activation_token=state["activation_token"],
                expected_state_sha=state["state_sha"],
                expected_registry_sha=state["registry_sha"],
                thread_record_id=record_id,
                reason="no current work",
            )
            state = registry_state(root, state, archived)
            with self.assertRaises(guard_module.Conflict):
                registry_module.assign_task(
                    str(root),
                    owner=self.OWNER,
                    activation_token=state["activation_token"],
                    expected_state_sha=state["state_sha"],
                    expected_registry_sha=state["registry_sha"],
                    thread_record_id=record_id,
                    task_id="TASK-2",
                    summary="Must not dispatch while archived",
                    acceptance_ref="AC-2",
                )
            reopened = registry_module.resume_thread(
                str(root),
                owner=self.OWNER,
                activation_token=state["activation_token"],
                expected_state_sha=state["state_sha"],
                expected_registry_sha=state["registry_sha"],
                thread_record_id=record_id,
                runtime_reopen_evidence="runtime set archived=false observed",
            )
            state = registry_state(root, state, reopened)
            with self.assertRaises(guard_module.Conflict):
                registry_module.assign_task(
                    str(root),
                    owner=self.OWNER,
                    activation_token=state["activation_token"],
                    expected_state_sha=state["state_sha"],
                    expected_registry_sha=state["registry_sha"],
                    thread_record_id=record_id,
                    task_id="TASK-2",
                    summary="Recovery still incomplete",
                    acceptance_ref="AC-2",
                )
            synced = registry_module.state_sync(
                str(root),
                owner=self.OWNER,
                activation_token=state["activation_token"],
                expected_state_sha=state["state_sha"],
                expected_registry_sha=state["registry_sha"],
                thread_record_id=record_id,
                acknowledgement=exact_state_sync_ack(root, record_id),
            )
            state = registry_state(root, state, synced)
            dispatched = registry_module.assign_task(
                str(root),
                owner=self.OWNER,
                activation_token=state["activation_token"],
                expected_state_sha=state["state_sha"],
                expected_registry_sha=state["registry_sha"],
                thread_record_id=record_id,
                task_id="TASK-2",
                summary="Safe continuation",
                acceptance_ref="AC-2",
            )
            self.assertEqual(dispatched["result"], "TASK_DISPATCH_RECORDED")

    def test_v2_thread_handoff_preserves_agent_and_rotates_primary_generation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v2-tests-") as temp:
            root = Path(temp)
            _active, state, predecessor_id = self._bound_thread(root)
            state = self._transition(root, state, predecessor_id, "WAITING")
            begun = registry_module.begin_handoff(
                str(root),
                owner=self.OWNER,
                activation_token=state["activation_token"],
                expected_state_sha=state["state_sha"],
                expected_registry_sha=state["registry_sha"],
                predecessor_thread_record_id=predecessor_id,
                successor_logical_name="Engineering - Technical Lead",
                summary_ref="accepted handoff summary H-1",
            )
            state = registry_state(root, state, begun)
            successor_id = begun["details"]["successor_thread_record_id"]
            bound = registry_module.bind_runtime(
                str(root),
                owner=self.OWNER,
                activation_token=state["activation_token"],
                expected_state_sha=state["state_sha"],
                expected_registry_sha=state["registry_sha"],
                thread_record_id=successor_id,
                binding_nonce=begun["details"]["binding_nonce"],
                runtime_thread_id="019ff012-0000-7000-8000-000000000002",
                runtime_host_id="local",
                identity_quality="observed",
            )
            state = registry_state(root, state, bound)
            completed = registry_module.complete_handoff(
                str(root),
                owner=self.OWNER,
                activation_token=state["activation_token"],
                expected_state_sha=state["state_sha"],
                expected_registry_sha=state["registry_sha"],
                predecessor_thread_record_id=predecessor_id,
                successor_thread_record_id=successor_id,
                successor_acknowledgement="Agent/project/baseline confirmed",
            )
            registry = registry_module.inspect_registry(str(root))["registry"]
            predecessor = registry_module._find_thread(registry, predecessor_id)
            successor = registry_module._find_thread(registry, successor_id)
            binding = registry["agent_bindings"]["technical-lead-01"]
            self.assertEqual(completed["details"]["agent_id"], "technical-lead-01")
            self.assertEqual(successor["generation"], predecessor["generation"] + 1)
            self.assertEqual(binding["primary_thread_record_id"], successor_id)
            self.assertEqual(predecessor["binding_role"], "predecessor")
            self.assertEqual(predecessor["lifecycle_state"], "ARCHIVED")

    def test_v2_handoff_predecessor_is_fenced_from_new_work(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v2-tests-") as temp:
            root = Path(temp)
            _active, state, predecessor_id = self._bound_thread(root)
            state = self._transition(root, state, predecessor_id, "WAITING")
            begun = registry_module.begin_handoff(
                str(root),
                owner=self.OWNER,
                activation_token=state["activation_token"],
                expected_state_sha=state["state_sha"],
                expected_registry_sha=state["registry_sha"],
                predecessor_thread_record_id=predecessor_id,
                successor_logical_name="Engineering - Technical Lead",
                summary_ref="accepted handoff summary H-2",
            )
            state = registry_state(root, state, begun)
            successor_id = begun["details"]["successor_thread_record_id"]
            bound = registry_module.bind_runtime(
                str(root),
                owner=self.OWNER,
                activation_token=state["activation_token"],
                expected_state_sha=state["state_sha"],
                expected_registry_sha=state["registry_sha"],
                thread_record_id=successor_id,
                binding_nonce=begun["details"]["binding_nonce"],
                runtime_thread_id="019ff012-0000-7000-8000-000000000003",
                runtime_host_id="local",
            )
            state = registry_state(root, state, bound)
            cutover = registry_module.complete_handoff(
                str(root),
                owner=self.OWNER,
                activation_token=state["activation_token"],
                expected_state_sha=state["state_sha"],
                expected_registry_sha=state["registry_sha"],
                predecessor_thread_record_id=predecessor_id,
                successor_thread_record_id=successor_id,
                successor_acknowledgement="handoff accepted",
            )
            state = registry_state(root, state, cutover)
            before = snapshot_tree(root)
            with self.assertRaises(guard_module.Conflict):
                registry_module.assign_task(
                    str(root),
                    owner=self.OWNER,
                    activation_token=state["activation_token"],
                    expected_state_sha=state["state_sha"],
                    expected_registry_sha=state["registry_sha"],
                    thread_record_id=predecessor_id,
                    task_id="LATE-1",
                    summary="Late predecessor submission",
                    acceptance_ref="must reject",
                )
            self.assertEqual(before, snapshot_tree(root))

    def test_v2_handoff_requires_nonempty_accepted_summary_ref(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v2-tests-") as temp:
            root = Path(temp)
            _active, state, predecessor_id = self._bound_thread(root)
            state = self._transition(root, state, predecessor_id, "WAITING")
            before = snapshot_tree(root)
            with self.assertRaises(guard_module.InvalidState):
                registry_module.begin_handoff(
                    str(root),
                    owner=self.OWNER,
                    activation_token=state["activation_token"],
                    expected_state_sha=state["state_sha"],
                    expected_registry_sha=state["registry_sha"],
                    predecessor_thread_record_id=predecessor_id,
                    successor_logical_name="Engineering - Technical Lead",
                    summary_ref="",
                )
            self.assertEqual(before, snapshot_tree(root))

    def test_v2_stale_context_requires_state_sync_before_dispatch(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v2-tests-") as temp:
            root = Path(temp)
            _active, state, record_id = self._bound_thread(root)
            state = self._transition(root, state, record_id, "WAITING")
            decision = root / ".founder" / "DECISIONS.md"
            decision.write_text(
                "# Decisions\n\n- Last revision: R-20260811T020000Z-new-decision\n",
                encoding="utf-8",
            )
            checkpoint = guard_module.checkpoint_active(
                str(root),
                owner=self.OWNER,
                activation_token=state["activation_token"],
                expected_state_sha=state["state_sha"],
            )
            state["state_sha"] = checkpoint["state_sha"]
            before = snapshot_tree(root)
            with self.assertRaisesRegex(guard_module.Conflict, "STATE_SYNC_REQUIRED"):
                registry_module.assign_task(
                    str(root),
                    owner=self.OWNER,
                    activation_token=state["activation_token"],
                    expected_state_sha=state["state_sha"],
                    expected_registry_sha=state["registry_sha"],
                    thread_record_id=record_id,
                    task_id="TASK-STALE",
                    summary="Must sync first",
                    acceptance_ref="AC-STALE",
                )
            self.assertEqual(before, snapshot_tree(root))
            synced = registry_module.state_sync(
                str(root),
                owner=self.OWNER,
                activation_token=state["activation_token"],
                expected_state_sha=state["state_sha"],
                expected_registry_sha=state["registry_sha"],
                thread_record_id=record_id,
                acknowledgement=exact_state_sync_ack(root, record_id),
            )
            state = registry_state(root, state, synced)
            dispatched = registry_module.assign_task(
                str(root),
                owner=self.OWNER,
                activation_token=state["activation_token"],
                expected_state_sha=state["state_sha"],
                expected_registry_sha=state["registry_sha"],
                thread_record_id=record_id,
                task_id="TASK-CURRENT",
                summary="Now use the current decision",
                acceptance_ref="AC-CURRENT",
            )
            self.assertEqual(dispatched["result"], "TASK_DISPATCH_RECORDED")

    def test_v22_state_sync_exact_ack_binds_identity_and_context_with_zero_write_failures(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v22-exact-state-sync-") as temp:
            root = Path(temp)
            _active, state, record_id = self._bound_thread(root)
            state = self._transition(root, state, record_id, "WAITING")
            decision = root / ".founder" / "DECISIONS.md"
            decision.write_text(
                "# Decisions\n\n- Last revision: R-20260813T000000Z-exact-sync\n",
                encoding="utf-8",
            )
            checkpoint = guard_module.checkpoint_active(
                str(root),
                owner=self.OWNER,
                activation_token=state["activation_token"],
                expected_state_sha=state["state_sha"],
            )
            state["state_sha"] = checkpoint["state_sha"]
            valid = exact_state_sync_ack(root, record_id)
            invalid = (
                "I read the current context",
                f"prefix {valid}",
                f"{valid} suffix",
                f"{valid} THREAD_RECORD_ID={record_id}",
                valid.replace("BINDING_GENERATION=1", "BINDING_GENERATION=2"),
                valid.replace(
                    "RUNTIME_THREAD_ID=019ff012-0000-7000-8000-000000000001",
                    "RUNTIME_THREAD_ID=wrong-runtime-thread",
                ),
                valid.replace("RUNTIME_HOST_ID=local", "RUNTIME_HOST_ID=wrong-host"),
                valid.replace("AGENT_ID=technical-lead-01", "AGENT_ID=other-agent"),
            )
            before = snapshot_tree(root)
            for acknowledgement in invalid:
                with self.subTest(acknowledgement=acknowledgement), self.assertRaises(
                    guard_module.Conflict
                ):
                    registry_module.state_sync(
                        str(root),
                        owner=self.OWNER,
                        activation_token=state["activation_token"],
                        expected_state_sha=state["state_sha"],
                        expected_registry_sha=state["registry_sha"],
                        thread_record_id=record_id,
                        acknowledgement=acknowledgement,
                    )
                self.assertEqual(before, snapshot_tree(root))

            synced = registry_module.state_sync(
                str(root),
                owner=self.OWNER,
                activation_token=state["activation_token"],
                expected_state_sha=state["state_sha"],
                expected_registry_sha=state["registry_sha"],
                thread_record_id=record_id,
                acknowledgement=valid,
            )
            self.assertEqual(synced["result"], "THREAD_STATE_SYNCED")

    def test_v2_partial_capabilities_degrade_independently(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v2-tests-") as temp:
            root = Path(temp)
            active = create_legacy_operating_project(root, self.OWNER)
            initialized = initialize_thread_registry(
                root,
                active,
                self.OWNER,
                capabilities={
                    "THREAD_CREATE_AVAILABLE": "SUPPORTED",
                    "THREAD_READ_AVAILABLE": "SUPPORTED",
                    "THREAD_RESUME_AVAILABLE": "PARTIAL",
                    "THREAD_INTERRUPT_AVAILABLE": "UNSUPPORTED",
                    "THREAD_FORK_AVAILABLE": "SUPPORTED",
                },
            )
            self.assertEqual(initialized["result"], "THREAD_REGISTRY_INITIALIZED")
            values = registry_module.inspect_registry(str(root))["registry"]["capability_observation"]["values"]
            self.assertEqual(values["THREAD_CREATE_AVAILABLE"], "SUPPORTED")
            self.assertEqual(values["THREAD_RESUME_AVAILABLE"], "PARTIAL")
            self.assertEqual(values["THREAD_INTERRUPT_AVAILABLE"], "UNSUPPORTED")
            self.assertEqual(values["THREAD_ARCHIVE_AVAILABLE"], "UNKNOWN")

    def test_v2_missing_skill_registry_fails_safe_without_install(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v2-tests-") as temp:
            root = Path(temp)
            active = create_legacy_operating_project(root, self.OWNER)
            initialized = initialize_thread_registry(root, active, self.OWNER)
            state = registry_state(root, active, initialized)
            before = snapshot_tree(root)
            with self.assertRaisesRegex(guard_module.Conflict, "SKILL_REGISTRY_UNAVAILABLE"):
                reserve_persistent_thread(
                    root,
                    state,
                    owner=self.OWNER,
                    skills=["testing"],
                )
            self.assertEqual(before, snapshot_tree(root))

    def test_v2_thread_skill_binding_accepts_only_trusted_registry_row(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v2-tests-") as temp:
            root = Path(temp)
            active = create_legacy_operating_project(root, self.OWNER)
            initialized = initialize_thread_registry(root, active, self.OWNER)
            (root / ".founder" / "SKILLS.md").write_text(
                "# Skills\n\n"
                "| Skill | Locator | Trust state | Audit evidence | Assigned to |\n"
                "|---|---|---|---|---|\n"
                "| testing | builtin | builtin-or-system | runtime probe | technical-lead-01 |\n"
                "| unsafe-skill | repo | third-party-unreviewed | none | none |\n",
                encoding="utf-8",
            )
            state = registry_state(root, active, initialized)
            reserved = reserve_persistent_thread(
                root, state, owner=self.OWNER, skills=["testing"]
            )
            state = registry_state(root, state, reserved)
            registry = registry_module.inspect_registry(str(root))["registry"]
            record = registry_module._find_thread(
                registry, reserved["details"]["thread_record_id"]
            )
            self.assertEqual(record["skills"][0]["trust_state"], "builtin-or-system")
            with self.assertRaises(guard_module.Conflict):
                registry_module.reserve_thread(
                    str(root),
                    owner=self.OWNER,
                    activation_token=state["activation_token"],
                    expected_state_sha=state["state_sha"],
                    expected_registry_sha=state["registry_sha"],
                    agent_id="product-lead-01",
                    agent_kind="persistent",
                    logical_name="Product - Product Lead",
                    manager_agent_id="founder-os-main",
                    workstream="product",
                    thread_type="persistent",
                    read_scope=[],
                    write_scope=[],
                    skills=["unsafe-skill"],
                    dependencies=[],
                )

    def test_v2_fork_readonly_cannot_inherit_write_scope(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v2-tests-") as temp:
            root = Path(temp)
            active = create_legacy_operating_project(root, self.OWNER)
            initialized = initialize_thread_registry(root, active, self.OWNER)
            before = snapshot_tree(root)
            with self.assertRaises(guard_module.InvalidState):
                registry_module.reserve_thread(
                    str(root),
                    owner=self.OWNER,
                    activation_token=active["activation_token"],
                    expected_state_sha=initialized["state_sha"],
                    expected_registry_sha=initialized["registry_sha"],
                    agent_id="review-fork-01",
                    agent_kind="task",
                    logical_name="Review - Readonly Fork",
                    manager_agent_id="founder-os-main",
                    workstream="integration",
                    thread_type="fork-readonly",
                    read_scope=["src/**"],
                    write_scope=["src/**"],
                    skills=[],
                    dependencies=[],
                )
            self.assertEqual(before, snapshot_tree(root))

    def test_v2_reconcile_incomplete_inventory_is_unverified_not_missing(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v2-tests-") as temp:
            root = Path(temp)
            _active, _state, record_id = self._bound_thread(root)
            registry = registry_module.inspect_registry(str(root))["registry"]
            incomplete = registry_module.reconcile_runtime_snapshot(
                registry, root.resolve(), [], inventory_complete=False
            )
            complete = registry_module.reconcile_runtime_snapshot(
                registry, root.resolve(), [], inventory_complete=True
            )
            self.assertEqual(incomplete["unverified"], [record_id])
            self.assertEqual(complete["missing"], [record_id])

    def test_v2_reconcile_exact_identity_is_healthy_and_title_is_not_identity(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v2-tests-") as temp:
            root = Path(temp)
            _active, _state, record_id = self._bound_thread(root)
            registry = registry_module.inspect_registry(str(root))["registry"]
            observed = [
                {
                    "thread_id": "019ff012-0000-7000-8000-000000000001",
                    "host_id": "local",
                    "title": "Completely Different Display Name",
                    "project_binding_id": registry["project_binding"]["project_binding_id"],
                },
                {
                    "thread_id": "019ff012-0000-7000-8000-000000000777",
                    "host_id": "local",
                    "title": "Engineering - Technical Lead",
                },
            ]
            result = registry_module.reconcile_runtime_snapshot(
                registry, root.resolve(), observed, inventory_complete=True
            )
            self.assertEqual(result["healthy"], [record_id])
            self.assertEqual(result["orphaned_runtime"], [])

    def test_v2_reconcile_marks_project_bound_unknown_runtime_as_orphan(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v2-tests-") as temp:
            root = Path(temp)
            _active, _state, _record_id = self._bound_thread(root)
            registry = registry_module.inspect_registry(str(root))["registry"]
            unknown = {
                "thread_id": "019ff012-0000-7000-8000-000000000888",
                "host_id": "local",
                "project_binding_id": registry["project_binding"]["project_binding_id"],
            }
            result = registry_module.reconcile_runtime_snapshot(
                registry, root.resolve(), [unknown], inventory_complete=False
            )
            self.assertEqual(result["orphaned_runtime"], ["local:019ff012-0000-7000-8000-000000000888"])

    def test_v2_main_handoff_invalidates_old_registry_dispatch_authority(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v2-tests-") as temp:
            root = Path(temp)
            active, state, record_id = self._bound_thread(root)
            state = self._transition(root, state, record_id, "WAITING")
            offered = guard_module.offer_handoff(
                str(root),
                owner=self.OWNER,
                activation_token=active["activation_token"],
                target="founder-os-main-recovery",
                basis="validator explicit main handoff",
                expected_state_sha=state["state_sha"],
            )
            guard_module.release_lock(
                str(root), owner=self.OWNER, activation_token=active["activation_token"]
            )
            recovered = guard_module.claim_active(
                str(root),
                owner="founder-os-main-recovery",
                runtime_id="runtime-recovery-thread",
                identity_quality="observed",
                expected_state_sha=offered["state_sha"],
                bootstrap=False,
                activation_token=None,
            )
            before = snapshot_tree(root)
            with self.assertRaises(guard_module.Conflict):
                registry_module.assign_task(
                    str(root),
                    owner=self.OWNER,
                    activation_token=active["activation_token"],
                    expected_state_sha=recovered["state_sha"],
                    expected_registry_sha=state["registry_sha"],
                    thread_record_id=record_id,
                    task_id="OLD-MAIN-TASK",
                    summary="Old main must be fenced",
                    acceptance_ref="must reject",
                )
            self.assertEqual(before, snapshot_tree(root))

    def test_v2_supervisor_handoff_registry_drift_blocks_target_claim(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v2-tests-") as temp:
            root = Path(temp)
            active = create_legacy_operating_project(root, self.OWNER)
            initialized = initialize_thread_registry(root, active, self.OWNER)
            offered = guard_module.offer_handoff(
                str(root),
                owner=self.OWNER,
                activation_token=active["activation_token"],
                target="founder-os-main-recovery",
                basis="freeze registry fingerprint",
                expected_state_sha=initialized["state_sha"],
            )
            guard_module.release_lock(
                str(root), owner=self.OWNER, activation_token=active["activation_token"]
            )
            path = root / ".founder" / "THREADS.json"
            registry = json.loads(path.read_text(encoding="utf-8"))
            registry["reconciliation"]["unverified"].append("tampered")
            path.write_text(json.dumps(registry, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            before = snapshot_tree(root)
            attempted = run_guard(
                "claim",
                "--project",
                str(root),
                "--owner",
                "founder-os-main-recovery",
                "--identity-quality",
                "observed",
                "--expected-state-sha",
                offered["state_sha"],
            )
            self.assertEqual(attempted.returncode, 3, attempted.stdout)
            self.assertEqual(before, snapshot_tree(root))

    def test_v2_unbound_thread_cannot_be_forged_into_working_state(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-v2-tests-") as temp:
            root = Path(temp)
            active = create_legacy_operating_project(root, self.OWNER)
            initialized = initialize_thread_registry(root, active, self.OWNER)
            state = registry_state(root, active, initialized)
            reserved = reserve_persistent_thread(root, state, owner=self.OWNER)
            registry = registry_module.inspect_registry(str(root))["registry"]
            thread = registry_module._find_thread(
                registry, reserved["details"]["thread_record_id"]
            )
            thread["lifecycle_state"] = "WORKING"
            with self.assertRaises(guard_module.InvalidState):
                registry_module.validate_registry(registry, root.resolve())

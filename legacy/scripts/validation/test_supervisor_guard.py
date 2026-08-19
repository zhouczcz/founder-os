"""FounderOS regression tests: SupervisorGuardTests, WorkflowInvariantTests.

Split from validate_founder_os.py; class bodies are copied verbatim so the
frozen AST manifests keep matching. Run via scripts/validate_founder_os.py.
"""

from __future__ import annotations

from validation.common import (  # noqa: F401
    Any,
    GUARD,
    PYTHON,
    Path,
    claim,
    copy,
    create_project,
    dependency_gate,
    guard_module,
    hashlib,
    integration_gate,
    json,
    mock,
    normalized_scope_conflict,
    os,
    parse_payload,
    release_lock,
    run_guard,
    run_guard_from,
    snapshot_tree,
    subprocess,
    tempfile,
    unittest,
)


class SupervisorGuardTests(unittest.TestCase):
    def test_read_only_inspect_is_byte_stable(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-tests-") as temp:
            root = Path(temp)
            create_project(root)
            before = snapshot_tree(root)
            completed = run_guard(
                "inspect",
                "--project",
                str(root),
                "--intent",
                "read-only",
                "--requested-mode",
                "REVIEWER",
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(parse_payload(completed)["mode"], "REVIEWER")
            self.assertEqual(before, snapshot_tree(root))

    def test_single_active_atomic_race_twenty_times(self) -> None:
        environment = os.environ.copy()
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        with tempfile.TemporaryDirectory(prefix="founder-os-tests-") as temp:
            base = Path(temp)
            for index in range(20):
                root = base / f"race-{index:02d}"
                root.mkdir()
                create_project(root)
                commands = []
                for owner in ("FOS-A", "FOS-B"):
                    commands.append(
                        [
                            PYTHON,
                            str(GUARD),
                            "claim",
                            "--project",
                            str(root),
                            "--owner",
                            owner,
                            "--identity-quality",
                            "ephemeral",
                            "--expected-state-sha",
                            "ABSENT",
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
                results = [process.communicate(timeout=15) for process in processes]
                returncodes = [process.returncode for process in processes]
                self.assertEqual(returncodes.count(0), 1, (returncodes, results))
                self.assertEqual(returncodes.count(3), 1, (returncodes, results))
                state = json.loads((root / ".founder" / "ACTIVE_SUPERVISOR.json").read_text(encoding="utf-8"))
                self.assertEqual(state["mode"], "ACTIVE")
                self.assertIn(state["supervisor"]["logical_id"], {"FOS-A", "FOS-B"})
                self.assertEqual(list((root / ".founder").glob("*.staging")), [])

    def test_second_supervisor_becomes_advisor(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-tests-") as temp:
            root = Path(temp)
            create_project(root)
            completed, active = claim(root, "FOS-A")
            self.assertEqual(completed.returncode, 0)
            self.assertEqual(release_lock(root, "FOS-A", active["activation_token"])[0].returncode, 0)
            before = snapshot_tree(root)
            inspected = run_guard(
                "inspect",
                "--project",
                str(root),
                "--candidate",
                "FOS-B",
                "--intent",
                "execute",
                "--requested-mode",
                "ACTIVE",
            )
            self.assertEqual(inspected.returncode, 0)
            self.assertEqual(parse_payload(inspected)["mode"], "ADVISOR")
            self.assertEqual(before, snapshot_tree(root))

    def test_same_supervisor_can_resume_with_token(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-tests-") as temp:
            root = Path(temp)
            create_project(root)
            completed, first = claim(root, "FOS-A")
            self.assertEqual(completed.returncode, 0)
            self.assertEqual(release_lock(root, "FOS-A", first["activation_token"])[0].returncode, 0)
            resumed, payload = claim(
                root,
                "FOS-A",
                first["state_sha"],
                first["activation_token"],
            )
            self.assertEqual(resumed.returncode, 0, resumed.stdout)
            self.assertEqual(payload["mode"], "ACTIVE")
            self.assertEqual(payload["activation_token"], first["activation_token"])

    def test_checkpoint_reconciles_current_canonical_revisions(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-tests-") as temp:
            root = Path(temp)
            create_project(root)
            completed, active = claim(root, "FOS-A")
            self.assertEqual(completed.returncode, 0, completed.stdout)
            next_revision = "R-20260811T010000Z-checkpoint"
            (root / ".founder" / "PROJECT.md").write_text(
                f"# Project\n\n- Last revision: {next_revision}\n",
                encoding="utf-8",
            )
            checkpoint = guard_module.checkpoint_active(
                str(root),
                owner="FOS-A",
                activation_token=active["activation_token"],
                expected_state_sha=active["state_sha"],
            )
            self.assertEqual(checkpoint["result"], "SUPERVISOR_CHECKPOINTED")
            self.assertEqual(checkpoint["source_revisions"]["PROJECT"], next_revision)
            self.assertEqual(checkpoint["record_revision"], active["record_revision"])
            self.assertEqual(
                guard_module.verify_fence(
                    str(root),
                    owner="FOS-A",
                    activation_token=active["activation_token"],
                )["record_revision"],
                checkpoint["record_revision"],
            )

    def test_explicit_handoff_rotates_token(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-tests-") as temp:
            root = Path(temp)
            create_project(root)
            completed, first = claim(root, "FOS-A")
            self.assertEqual(completed.returncode, 0)
            offered = run_guard(
                "offer-handoff",
                "--project",
                str(root),
                "--owner",
                "FOS-A",
                "--activation-token",
                first["activation_token"],
                "--to",
                "FOS-B",
                "--basis",
                "Founder approved handoff",
                "--expected-state-sha",
                first["state_sha"],
            )
            self.assertEqual(offered.returncode, 0, offered.stdout)
            offered_payload = parse_payload(offered)
            self.assertEqual(release_lock(root, "FOS-A", first["activation_token"])[0].returncode, 0)
            accepted, second = claim(root, "FOS-B", offered_payload["state_sha"])
            self.assertEqual(accepted.returncode, 0, accepted.stdout)
            self.assertNotEqual(second["activation_token"], first["activation_token"])
            record = json.loads((root / ".founder" / "ACTIVE_SUPERVISOR.json").read_text(encoding="utf-8"))
            self.assertEqual(record["previous_supervisor"]["supervisor"]["logical_id"], "FOS-A")

    def test_takeover_requires_terminal_evidence_and_consistent_revisions(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-tests-") as temp:
            root = Path(temp)
            create_project(root)
            completed, first = claim(root, "FOS-A")
            self.assertEqual(completed.returncode, 0)
            self.assertEqual(release_lock(root, "FOS-A", first["activation_token"])[0].returncode, 0)
            before = snapshot_tree(root)
            denied = run_guard(
                "recover",
                "--project",
                str(root),
                "--owner",
                "FOS-B",
                "--identity-quality",
                "ephemeral",
                "--expected-state-sha",
                first["state_sha"],
                "--kind",
                "takeover",
                "--predecessor-liveness",
                "unknown",
                "--authorization-ref",
                "Founder request",
            )
            self.assertEqual(denied.returncode, 3)
            self.assertEqual(before, snapshot_tree(root))
            allowed = run_guard(
                "recover",
                "--project",
                str(root),
                "--owner",
                "FOS-B",
                "--identity-quality",
                "ephemeral",
                "--expected-state-sha",
                first["state_sha"],
                "--kind",
                "takeover",
                "--predecessor-liveness",
                "terminated",
                "--authorization-ref",
                "Founder request plus runtime terminal evidence",
            )
            self.assertEqual(allowed.returncode, 0, allowed.stdout)
            self.assertEqual(parse_payload(allowed)["owner"], "FOS-B")

    def test_stale_timestamp_alone_never_allows_takeover(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-tests-") as temp:
            root = Path(temp)
            create_project(root)
            completed, first = claim(root, "FOS-A")
            self.assertEqual(completed.returncode, 0)
            self.assertEqual(release_lock(root, "FOS-A", first["activation_token"])[0].returncode, 0)
            state_path = root / ".founder" / "ACTIVE_SUPERVISOR.json"
            record = json.loads(state_path.read_text(encoding="utf-8"))
            record["last_seen_at"] = "2000-01-01T00:00:00Z"
            state_path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            before = snapshot_tree(root)
            inspected = run_guard(
                "inspect",
                "--project",
                str(root),
                "--candidate",
                "FOS-B",
                "--intent",
                "execute",
                "--requested-mode",
                "ACTIVE",
            )
            self.assertEqual(inspected.returncode, 0)
            self.assertEqual(parse_payload(inspected)["mode"], "ADVISOR")
            self.assertEqual(before, snapshot_tree(root))

    def test_malformed_state_fails_closed_without_rewrite(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-tests-") as temp:
            root = Path(temp)
            create_project(root)
            state_path = root / ".founder" / "ACTIVE_SUPERVISOR.json"
            state_path.write_bytes(b"{broken")
            before = snapshot_tree(root)
            completed = run_guard("inspect", "--project", str(root))
            self.assertEqual(completed.returncode, 2)
            self.assertEqual(before, snapshot_tree(root))

    def test_post_state_lock_failure_stays_locked_and_is_repairable(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-tests-") as temp:
            root = Path(temp)
            create_project(root)
            real_replace = guard_module._atomic_replace
            calls = 0

            def fail_lock_replace(path: Path, value: dict[str, Any]) -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("injected lock finalization failure")
                real_replace(path, value)

            with mock.patch.object(
                guard_module, "_atomic_replace", side_effect=fail_lock_replace
            ):
                with self.assertRaises(guard_module.PartialCommit) as raised:
                    guard_module.claim_active(
                        str(root),
                        owner="FOS-A",
                        runtime_id=None,
                        identity_quality="ephemeral",
                        expected_state_sha="ABSENT",
                        bootstrap=False,
                        activation_token=None,
                    )

            self.assertEqual(raised.exception.recovery_action, "repair-lock")
            founder = root / ".founder"
            state_path = founder / "ACTIVE_SUPERVISOR.json"
            lock_path = founder / ".write-lock.json"
            self.assertTrue(state_path.is_file())
            self.assertTrue(lock_path.is_file())
            record = json.loads(state_path.read_text(encoding="utf-8"))
            lock = json.loads(lock_path.read_text(encoding="utf-8"))
            self.assertEqual(record["mode"], "ACTIVE")
            self.assertIsNone(lock["activation_token"])
            state_sha = hashlib.sha256(state_path.read_bytes()).hexdigest().upper()
            repaired = guard_module.repair_lock(
                str(root),
                owner="FOS-A",
                activation_token=record["activation_token"],
                expected_state_sha=state_sha,
            )
            self.assertEqual(repaired["result"], "WRITE_LOCK_REPAIRED")
            self.assertEqual(
                guard_module.verify_fence(
                    str(root),
                    owner="FOS-A",
                    activation_token=record["activation_token"],
                )["result"],
                "FENCE_VALID",
            )

    def test_release_cleanup_failure_stays_locked_and_is_clearable(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-tests-") as temp:
            root = Path(temp)
            create_project(root)
            completed, active = claim(root, "FOS-A")
            self.assertEqual(completed.returncode, 0, completed.stdout)
            with mock.patch.object(
                guard_module,
                "_release_owned_lock",
                side_effect=OSError("injected lock cleanup failure"),
            ):
                with self.assertRaises(guard_module.PartialCommit) as raised:
                    guard_module.release_supervisor(
                        str(root),
                        owner="FOS-A",
                        activation_token=active["activation_token"],
                        expected_state_sha=active["state_sha"],
                        basis="test release",
                    )

            self.assertEqual(raised.exception.recovery_action, "clear-released-lock")
            founder = root / ".founder"
            state_path = founder / "ACTIVE_SUPERVISOR.json"
            lock_path = founder / ".write-lock.json"
            self.assertTrue(lock_path.is_file())
            state_sha = hashlib.sha256(state_path.read_bytes()).hexdigest().upper()
            cleared = guard_module.clear_released_lock(
                str(root),
                owner="FOS-A",
                activation_token=active["activation_token"],
                expected_state_sha=state_sha,
            )
            self.assertEqual(cleared["result"], "RELEASED_WRITE_LOCK_CLEARED")
            self.assertFalse(lock_path.exists())

    def test_same_revision_content_drift_blocks_recovery(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-tests-") as temp:
            root = Path(temp)
            create_project(root)
            completed, active = claim(root, "FOS-A")
            self.assertEqual(completed.returncode, 0, completed.stdout)
            self.assertEqual(
                release_lock(root, "FOS-A", active["activation_token"])[0].returncode,
                0,
            )
            project = root / ".founder" / "PROJECT.md"
            project.write_text(
                project.read_text(encoding="utf-8") + "\nUnexplained body drift.\n",
                encoding="utf-8",
            )
            before = snapshot_tree(root)
            recovered = run_guard(
                "recover",
                "--project",
                str(root),
                "--owner",
                "FOS-B",
                "--identity-quality",
                "ephemeral",
                "--expected-state-sha",
                active["state_sha"],
                "--kind",
                "recovery",
                "--predecessor-liveness",
                "terminated",
                "--authorization-ref",
                "test terminal evidence",
            )
            self.assertEqual(recovered.returncode, 3, recovered.stdout)
            self.assertEqual(before, snapshot_tree(root))

    def test_handoff_fingerprint_drift_blocks_acceptance(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-tests-") as temp:
            root = Path(temp)
            create_project(root)
            completed, active = claim(root, "FOS-A")
            self.assertEqual(completed.returncode, 0, completed.stdout)
            offered = run_guard(
                "offer-handoff",
                "--project",
                str(root),
                "--owner",
                "FOS-A",
                "--activation-token",
                active["activation_token"],
                "--to",
                "FOS-B",
                "--basis",
                "test handoff",
                "--expected-state-sha",
                active["state_sha"],
            )
            self.assertEqual(offered.returncode, 0, offered.stdout)
            offered_payload = parse_payload(offered)
            self.assertEqual(
                release_lock(root, "FOS-A", active["activation_token"])[0].returncode,
                0,
            )
            project = root / ".founder" / "PROJECT.md"
            project.write_text(
                project.read_text(encoding="utf-8") + "\nPost-offer drift.\n",
                encoding="utf-8",
            )
            before = snapshot_tree(root)
            inspected = run_guard(
                "inspect",
                "--project",
                str(root),
                "--candidate",
                "FOS-B",
                "--intent",
                "execute",
                "--requested-mode",
                "ACTIVE",
            )
            self.assertEqual(inspected.returncode, 0, inspected.stdout)
            self.assertEqual(parse_payload(inspected)["mode"], "RECOVERY")
            self.assertEqual(before, snapshot_tree(root))
            accepted, payload = claim(root, "FOS-B", offered_payload["state_sha"])
            self.assertEqual(accepted.returncode, 3, payload)
            self.assertEqual(before, snapshot_tree(root))

    def test_handoff_offer_blocks_checkpoint_and_stale_eligibility(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-tests-") as temp:
            root = Path(temp)
            create_project(root)
            completed, active = claim(root, "FOS-A")
            self.assertEqual(completed.returncode, 0, completed.stdout)
            offered = run_guard(
                "offer-handoff",
                "--project",
                str(root),
                "--owner",
                "FOS-A",
                "--activation-token",
                active["activation_token"],
                "--to",
                "FOS-B",
                "--basis",
                "test frozen handoff",
                "--expected-state-sha",
                active["state_sha"],
            )
            self.assertEqual(offered.returncode, 0, offered.stdout)
            offered_payload = parse_payload(offered)
            project = root / ".founder" / "PROJECT.md"
            project.write_text(
                project.read_text(encoding="utf-8") + "\nPost-offer edit.\n",
                encoding="utf-8",
            )
            before_checkpoint = snapshot_tree(root)
            checkpoint = run_guard(
                "checkpoint",
                "--project",
                str(root),
                "--owner",
                "FOS-A",
                "--activation-token",
                active["activation_token"],
                "--expected-state-sha",
                offered_payload["state_sha"],
            )
            self.assertEqual(checkpoint.returncode, 3, checkpoint.stdout)
            self.assertEqual(before_checkpoint, snapshot_tree(root))

            # Simulate a record produced by an older guard that checkpointed the
            # current ledgers but left the handoff's frozen fingerprints intact.
            founder = root / ".founder"
            state_path = founder / "ACTIVE_SUPERVISOR.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["source_revisions"] = guard_module.read_source_revisions(founder)
            state_path.write_text(
                json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            (founder / ".write-lock.json").unlink()
            before_inspect = snapshot_tree(root)
            inspected = run_guard(
                "inspect",
                "--project",
                str(root),
                "--candidate",
                "FOS-B",
                "--intent",
                "execute",
                "--requested-mode",
                "ACTIVE",
            )
            self.assertEqual(inspected.returncode, 0, inspected.stdout)
            inspected_payload = parse_payload(inspected)
            self.assertEqual(inspected_payload["mode"], "RECOVERY")
            self.assertEqual(
                inspected_payload["reason"], "HANDOFF_FROZEN_FINGERPRINT_DRIFT"
            )
            self.assertEqual(before_inspect, snapshot_tree(root))

    def test_orphan_write_lock_without_state_requires_recovery(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-tests-") as temp:
            root = Path(temp)
            create_project(root)
            completed, active = claim(root, "FOS-A")
            self.assertEqual(completed.returncode, 0, completed.stdout)
            (root / ".founder" / "ACTIVE_SUPERVISOR.json").unlink()
            before = snapshot_tree(root)
            inspected = run_guard(
                "inspect",
                "--project",
                str(root),
                "--candidate",
                "FOS-B",
                "--intent",
                "execute",
                "--requested-mode",
                "ACTIVE",
            )
            self.assertEqual(inspected.returncode, 0, inspected.stdout)
            inspected_payload = parse_payload(inspected)
            self.assertEqual(inspected_payload["mode"], "RECOVERY")
            self.assertEqual(
                inspected_payload["reason"],
                "ORPHAN_WRITE_LOCK_WITHOUT_SUPERVISOR_RECORD",
            )
            attempted, _payload = claim(root, "FOS-B", "ABSENT")
            self.assertEqual(attempted.returncode, 3, attempted.stdout)
            self.assertEqual(before, snapshot_tree(root))

    def test_verify_rejects_tampered_lock_bindings(self) -> None:
        fields = {
            "project_root": "C:\\not-the-project",
            "committed_supervisor_state_sha": "0" * 64,
            "source_revisions": {"PROJECT": "forged"},
        }
        for field, forged in fields.items():
            with self.subTest(field=field), tempfile.TemporaryDirectory(
                prefix="founder-os-tests-"
            ) as temp:
                root = Path(temp)
                create_project(root)
                completed, active = claim(root, "FOS-A")
                self.assertEqual(completed.returncode, 0, completed.stdout)
                lock_path = root / ".founder" / ".write-lock.json"
                lock = json.loads(lock_path.read_text(encoding="utf-8"))
                lock[field] = forged
                lock_path.write_text(
                    json.dumps(lock, indent=2, sort_keys=True) + "\n", encoding="utf-8"
                )
                before = snapshot_tree(root)
                inspected = run_guard(
                    "inspect",
                    "--project",
                    str(root),
                    "--candidate",
                    "FOS-A",
                    "--activation-token",
                    active["activation_token"],
                    "--intent",
                    "execute",
                    "--requested-mode",
                    "ACTIVE",
                )
                self.assertEqual(inspected.returncode, 0, inspected.stdout)
                self.assertEqual(parse_payload(inspected)["mode"], "RECOVERY")
                self.assertEqual(before, snapshot_tree(root))
                verified = run_guard(
                    "verify",
                    "--project",
                    str(root),
                    "--owner",
                    "FOS-A",
                    "--activation-token",
                    active["activation_token"],
                )
                self.assertNotEqual(verified.returncode, 0, verified.stdout)
                self.assertEqual(before, snapshot_tree(root))

    def test_canonical_drift_limits_active_to_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-tests-") as temp:
            root = Path(temp)
            create_project(root)
            completed, active = claim(root, "FOS-A")
            self.assertEqual(completed.returncode, 0, completed.stdout)
            project = root / ".founder" / "PROJECT.md"
            project.write_text(
                project.read_text(encoding="utf-8") + "\nAuthorized pending edit.\n",
                encoding="utf-8",
            )
            before = snapshot_tree(root)
            inspected = run_guard(
                "inspect",
                "--project",
                str(root),
                "--candidate",
                "FOS-A",
                "--activation-token",
                active["activation_token"],
                "--intent",
                "execute",
                "--requested-mode",
                "ACTIVE",
            )
            self.assertEqual(inspected.returncode, 0, inspected.stdout)
            self.assertEqual(parse_payload(inspected)["mode"], "RECOVERY")
            verified = run_guard(
                "verify",
                "--project",
                str(root),
                "--owner",
                "FOS-A",
                "--activation-token",
                active["activation_token"],
            )
            self.assertEqual(verified.returncode, 3, verified.stdout)
            self.assertEqual(before, snapshot_tree(root))
            checkpoint = guard_module.checkpoint_active(
                str(root),
                owner="FOS-A",
                activation_token=active["activation_token"],
                expected_state_sha=active["state_sha"],
            )
            self.assertEqual(checkpoint["prior_fence"], "FENCE_VALID_CHECKPOINT_ONLY")

    def test_revision_only_legacy_baseline_requires_recovery(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-tests-") as temp:
            root = Path(temp)
            create_project(root)
            completed, active = claim(root, "FOS-A")
            self.assertEqual(completed.returncode, 0, completed.stdout)
            self.assertEqual(
                release_lock(root, "FOS-A", active["activation_token"])[0].returncode,
                0,
            )
            state_path = root / ".founder" / "ACTIVE_SUPERVISOR.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["source_revisions"] = {
                key: value
                for key, value in state["source_revisions"].items()
                if not key.endswith("_SHA256")
            }
            state_path.write_text(
                json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            legacy_sha = hashlib.sha256(state_path.read_bytes()).hexdigest().upper()
            before = snapshot_tree(root)
            inspected = run_guard(
                "inspect",
                "--project",
                str(root),
                "--candidate",
                "FOS-A",
                "--activation-token",
                active["activation_token"],
                "--intent",
                "execute",
            )
            self.assertEqual(parse_payload(inspected)["mode"], "RECOVERY")
            resumed, payload = claim(
                root, "FOS-A", legacy_sha, active["activation_token"]
            )
            self.assertEqual(resumed.returncode, 3, payload)
            self.assertEqual(before, snapshot_tree(root))

    def test_empty_identifiers_fail_before_writing(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-tests-") as temp:
            root = Path(temp)
            create_project(root)
            before = snapshot_tree(root)
            empty_owner = run_guard(
                "claim",
                "--project",
                str(root),
                "--owner",
                "",
                "--expected-state-sha",
                "ABSENT",
            )
            self.assertEqual(empty_owner.returncode, 2, empty_owner.stdout)
            self.assertEqual(before, snapshot_tree(root))
            completed, active = claim(root, "FOS-A")
            self.assertEqual(completed.returncode, 0, completed.stdout)
            active_snapshot = snapshot_tree(root)
            empty_target = run_guard(
                "offer-handoff",
                "--project",
                str(root),
                "--owner",
                "FOS-A",
                "--activation-token",
                active["activation_token"],
                "--to",
                "",
                "--basis",
                "test",
                "--expected-state-sha",
                active["state_sha"],
            )
            self.assertEqual(empty_target.returncode, 2, empty_target.stdout)
            self.assertEqual(active_snapshot, snapshot_tree(root))

    def test_malformed_project_root_is_controlled_invalid(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-tests-") as temp:
            root = Path(temp)
            create_project(root)
            completed, active = claim(root, "FOS-A")
            self.assertEqual(completed.returncode, 0, completed.stdout)
            state_path = root / ".founder" / "ACTIVE_SUPERVISOR.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["project_root"] = 7
            state_path.write_text(
                json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            before = snapshot_tree(root)
            inspected = run_guard("inspect", "--project", str(root))
            self.assertEqual(inspected.returncode, 2, inspected.stderr)
            self.assertEqual(parse_payload(inspected)["result"], "INVALID")
            self.assertNotIn("Traceback", inspected.stderr)
            self.assertEqual(before, snapshot_tree(root))

    def test_relative_record_root_is_rejected_from_project_cwd(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-tests-") as temp:
            root = Path(temp)
            create_project(root)
            completed, active = claim(root, "FOS-A")
            self.assertEqual(completed.returncode, 0, completed.stdout)
            state_path = root / ".founder" / "ACTIVE_SUPERVISOR.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["project_root"] = "."
            state_path.write_text(
                json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            before = snapshot_tree(root)
            inspected = run_guard_from(root, "inspect", "--project", str(root))
            self.assertEqual(inspected.returncode, 2, inspected.stderr)
            self.assertEqual(parse_payload(inspected)["result"], "INVALID")
            self.assertNotIn("Traceback", inspected.stderr)
            self.assertEqual(before, snapshot_tree(root))

    def test_canonical_hardlink_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-tests-") as temp:
            base = Path(temp)
            root = base / "project"
            root.mkdir()
            create_project(root)
            outside = base / "outside-project.md"
            outside.write_text(
                "# Outside\n\n- Last revision: R-outside\n", encoding="utf-8"
            )
            project = root / ".founder" / "PROJECT.md"
            project.unlink()
            os.link(outside, project)
            before = snapshot_tree(base)
            attempted, payload = claim(root, "FOS-A")
            self.assertEqual(attempted.returncode, 2, payload)
            self.assertEqual(before, snapshot_tree(base))

    def test_failed_bootstrap_claim_removes_its_empty_founder_directory(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-tests-") as temp:
            root = Path(temp)
            completed = run_guard(
                "claim",
                "--project",
                str(root),
                "--owner",
                "FOS-A",
                "--expected-state-sha",
                "0" * 64,
                "--bootstrap",
            )
            self.assertEqual(completed.returncode, 3, completed.stdout)
            self.assertFalse((root / ".founder").exists())


class WorkflowInvariantTests(unittest.TestCase):
    def test_disjoint_scopes_can_parallelize(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-tests-") as temp:
            root = Path(temp)
            self.assertFalse(normalized_scope_conflict(root, "src/frontend", "src/backend"))

    def test_same_or_nested_scope_conflicts(self) -> None:
        with tempfile.TemporaryDirectory(prefix="founder-os-tests-") as temp:
            root = Path(temp)
            self.assertTrue(normalized_scope_conflict(root, "shared.md", ".\\shared.md"))
            self.assertTrue(normalized_scope_conflict(root, "src", "src/module.py"))
            if os.name == "nt":
                self.assertTrue(normalized_scope_conflict(root, "SHARED.md", "shared.md"))

    def test_dependency_gate_requires_accepted(self) -> None:
        for state in ("planned", "running", "returned", "changes-requested", "blocked"):
            self.assertFalse(dependency_gate(state), state)
        self.assertTrue(dependency_gate("accepted"))
        self.assertFalse(dependency_gate("accepted", interface_frozen=False))

    def test_integration_gate_rejects_partial_or_failed_checks(self) -> None:
        checks = {"interfaces": True, "tests": True, "decisions": True}
        self.assertTrue(integration_gate(["accepted", "ready-for-integration"], checks))
        self.assertFalse(integration_gate(["accepted", "running"], checks))
        self.assertFalse(
            integration_gate(
                ["accepted", "ready-for-integration"],
                {**checks, "interfaces": False},
            )
        )

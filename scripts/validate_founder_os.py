#!/usr/bin/env python3
"""Deterministic regression suite for the FounderOS skill.

The suite uses only temporary projects. It validates static requirements,
Supervisor CAS/race behavior, read-only byte stability, dependency rules, and
Integration Gate invariants. Probabilistic LLM behavior and absence of runtime
subagent tools still require separate forward/conditional testing.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

# Set this before importing the sibling guard module. The validator must not
# create `scripts/__pycache__` when invoked without `-B`.
sys.dont_write_bytecode = True

import supervisor_guard as guard_module
import thread_registry as registry_module
import decision_state as decision_module


SKILL_ROOT = Path(__file__).resolve().parent.parent
GUARD = SKILL_ROOT / "scripts" / "supervisor_guard.py"
THREAD_REGISTRY = SKILL_ROOT / "scripts" / "thread_registry.py"
DECISION_STATE = SKILL_ROOT / "scripts" / "decision_state.py"
PYTHON = sys.executable


def snapshot_tree(root: Path) -> dict[str, tuple[str, int, int, str | None]]:
    snapshot: dict[str, tuple[str, int, int, str | None]] = {}
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix().casefold()):
        relative = path.relative_to(root).as_posix()
        metadata = path.lstat()
        if path.is_symlink():
            snapshot[relative] = (
                "link",
                metadata.st_size,
                metadata.st_mtime_ns,
                str(path.readlink()),
            )
        elif path.is_file():
            snapshot[relative] = (
                "file",
                metadata.st_size,
                metadata.st_mtime_ns,
                hashlib.sha256(path.read_bytes()).hexdigest(),
            )
        else:
            snapshot[relative] = (
                "directory",
                metadata.st_size,
                metadata.st_mtime_ns,
                None,
            )
    return snapshot


def create_project(root: Path) -> None:
    founder = root / ".founder"
    founder.mkdir()
    revision = "R-20260811T000000Z-test"
    (founder / "PROJECT.md").write_text(
        f"# Project\n\n- Last revision: {revision}\n", encoding="utf-8"
    )
    (founder / "ROADMAP.md").write_text(
        f"# Roadmap\n\n- Last revision: {revision}\n", encoding="utf-8"
    )
    (founder / "DECISIONS.md").write_text(
        f"# Decisions\n\n- Last revision: {revision}\n", encoding="utf-8"
    )
    (founder / "AGENTS.md").write_text(
        f"# Agents\n\n- Last revision: {revision}\n", encoding="utf-8"
    )
    (founder / "STATUS.md").write_text(
        f"# Status\n\n- Reconciled revision: {revision}\n", encoding="utf-8"
    )


def run_guard(*arguments: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [PYTHON, str(GUARD), *arguments],
        capture_output=True,
        check=False,
        env=environment,
        text=True,
    )


def run_guard_from(cwd: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [PYTHON, str(GUARD), *arguments],
        cwd=cwd,
        capture_output=True,
        check=False,
        env=environment,
        text=True,
    )


def run_registry(*arguments: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONUTF8"] = "1"
    return subprocess.run(
        [PYTHON, str(THREAD_REGISTRY), *arguments],
        capture_output=True,
        check=False,
        env=environment,
        text=True,
    )


def parse_payload(completed: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    if not completed.stdout.strip():
        raise AssertionError(f"Guard emitted no JSON: {completed.stderr}")
    value = json.loads(completed.stdout)
    if not isinstance(value, dict):
        raise AssertionError("Guard JSON output must be an object")
    return value


def claim(root: Path, owner: str, expected: str = "ABSENT", token: str | None = None):
    arguments = [
        "claim",
        "--project",
        str(root),
        "--owner",
        owner,
        "--identity-quality",
        "ephemeral",
        "--expected-state-sha",
        expected,
    ]
    if token:
        arguments.extend(("--activation-token", token))
    completed = run_guard(*arguments)
    return completed, parse_payload(completed)


def release_lock(root: Path, owner: str, token: str):
    completed = run_guard(
        "release-lock",
        "--project",
        str(root),
        "--owner",
        owner,
        "--activation-token",
        token,
    )
    return completed, parse_payload(completed)


def create_active_project(root: Path, owner: str = "founder-os-main-test") -> dict[str, Any]:
    create_project(root)
    completed, active = claim(root, owner)
    if completed.returncode != 0:
        raise AssertionError(completed.stdout)
    return active


def create_empty_active_project(
    root: Path, owner: str = "founder-os-main-v21-test"
) -> dict[str, Any]:
    """Claim a truly new project before any canonical business ledger exists."""

    completed = run_guard(
        "claim",
        "--project",
        str(root),
        "--owner",
        owner,
        "--identity-quality",
        "ephemeral",
        "--expected-state-sha",
        "ABSENT",
        "--bootstrap",
    )
    payload = parse_payload(completed)
    if completed.returncode != 0:
        raise AssertionError(completed.stdout)
    return payload


def strategy_state(
    active: dict[str, Any], mutation: dict[str, Any]
) -> dict[str, Any]:
    """Carry the two independent CAS observations after a Strategy mutation."""

    return {
        "activation_token": active["activation_token"],
        "state_sha": mutation["state_sha"],
        "strategy_sha": mutation["strategy_sha"],
    }


def migrate_legacy_strategy(
    root: Path,
    active: dict[str, Any],
    owner: str = "founder-os-main-test",
) -> dict[str, Any]:
    """Explicitly migrate a five-ledger legacy fixture before Thread work."""

    mutation = decision_module.initialize_strategy(
        str(root),
        owner=owner,
        activation_token=active["activation_token"],
        expected_state_sha=active["state_sha"],
        expected_strategy_sha="ABSENT",
        mode="legacy",
        legacy_summary="Existing validator project inferred from all five canonical ledgers",
        evidence="Deterministic legacy Strategy migration before Thread Registry use",
    )
    return strategy_state(active, mutation)


def create_legacy_operating_project(
    root: Path, owner: str = "founder-os-main-test"
) -> dict[str, Any]:
    """Create and explicitly migrate a legacy fixture to OPERATING."""

    return migrate_legacy_strategy(root, create_active_project(root, owner), owner)


def initialize_new_strategy(
    root: Path,
    active: dict[str, Any],
    owner: str = "founder-os-main-v21-test",
) -> dict[str, Any]:
    mutation = decision_module.initialize_strategy(
        str(root),
        owner=owner,
        activation_token=active["activation_token"],
        expected_state_sha=active["state_sha"],
        expected_strategy_sha="ABSENT",
        mode="new",
        legacy_summary=None,
        evidence="Founder requested a new project",
    )
    return strategy_state(active, mutation)


def strategy_candidates(count: int = 3) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for index in range(count):
        letter = chr(ord("a") + index)
        candidates.append(
            {
                "candidate_id": f"direction-{letter}",
                "name": f"Direction {letter.upper()}",
                "summary": f"Build the {letter.upper()} product direction",
                "target_user": f"User group {letter.upper()}",
                "problem": f"Problem {letter.upper()}",
                "opportunity": f"Opportunity {letter.upper()}",
                "advantages": [f"Advantage {letter.upper()}"],
                "risks": [f"Risk {letter.upper()}"],
                "difficulty": ("LOW", "MEDIUM", "HIGH")[index % 3],
                "startup_cost": ("LOW", "MEDIUM", "HIGH")[index % 3],
                "validation_speed": ("FAST", "MEDIUM", "SLOW")[index % 3],
                "reversibility": ("HIGH", "MEDIUM", "LOW")[index % 3],
                "roadmap_effect": f"Roadmap effect {letter.upper()}",
                "assessment": f"FounderOS assessment {letter.upper()}",
            }
        )
    return candidates


def strategy_recommendation(candidate_id: str = "direction-a") -> dict[str, str]:
    return {
        "candidate_id": candidate_id,
        "rationale": "Best fit for one Founder and a near-zero initial budget",
        "why_now": "It can test demand before expensive implementation",
        "biggest_downside": "The initial market may be narrower",
        "choose_another_when": "Choose another option when the Founder accepts higher cost and risk",
    }


def write_strategy_ledgers(
    root: Path,
    *,
    decision: dict[str, str] | None = None,
    discovery_runtime_ids: list[str] | None = None,
    status_decision_ids: list[str] | None = None,
) -> None:
    founder = root / ".founder"
    revision = "R-20260812T000000Z-v21-test"
    (founder / "PROJECT.md").write_text(
        f"# Project\n\n- Last revision: {revision}\n\nSelected direction is recorded.\n",
        encoding="utf-8",
    )
    (founder / "ROADMAP.md").write_text(
        f"# Roadmap\n\n- Last revision: {revision}\n\nCurrent milestones follow the selected direction.\n",
        encoding="utf-8",
    )
    decision_text = f"# Decisions\n\n- Last revision: {revision}\n"
    if decision is not None:
        level = decision["level"]
        decision_text += (
            f"\n## Strategic decision {decision['decision_id']}\n\n"
            f"- Decision ID: {decision['decision_id']}\n"
            f"- Proposal ID: {decision['proposal_id']}\n"
            f"- Level: {level}\n"
            "- Date / Order: 2026-08-12 / validator sequence\n"
        )
        if level == "L2":
            decision_text += (
                "- Candidate Options: direction-a; direction-b; direction-c\n"
                "- FounderOS Recommendation: direction-a\n"
                f"- Selected Strategy ID: {decision['selected_strategy_id']}\n"
                f"- Decision Authority: {decision['selection_authority']}\n"
            )
        else:
            decision_text += f"- Action Scope: {decision['action_scope']}\n"
        decision_text += (
            "- Rationale: The current evidence supports this choice.\n"
            "- Assumptions: The stated constraints remain true.\n"
            "- Reconsideration Trigger: New evidence invalidates the core assumption.\n"
        )
    (founder / "DECISIONS.md").write_text(decision_text, encoding="utf-8")
    agent_lines = "\n".join(
        f"- Discovery runtime Agent: {runtime_id}"
        for runtime_id in (discovery_runtime_ids or [])
    )
    (founder / "AGENTS.md").write_text(
        f"# Agents\n\n- Last revision: {revision}\n{agent_lines}\n",
        encoding="utf-8",
    )
    status_blocks: list[str] = []
    for decision_id in status_decision_ids or []:
        if decision is None or decision.get("decision_id") != decision_id:
            status_blocks.append(f"- Reported strategic decision: {decision_id}")
            continue
        status_blocks.append(
            "\n".join(
                (
                    "## Autonomous Strategic Decision Report",
                    "",
                    f"- Decision ID: {decision_id}",
                    f"- Proposal ID: {decision['proposal_id']}",
                    f"- Selected Strategy ID: {decision['selected_strategy_id']}",
                    "- Rationale: The selected direction best fits current evidence.",
                    "- Biggest Risk: The core market assumption may be wrong.",
                    "- Reconsideration Trigger: New evidence invalidates that assumption.",
                )
            )
        )
    status_lines = "\n\n".join(status_blocks)
    (founder / "STATUS.md").write_text(
        f"# Status\n\n- Reconciled revision: {revision}\n{status_lines}\n",
        encoding="utf-8",
    )


def checkpoint_external_changes(
    root: Path,
    state: dict[str, Any],
    owner: str = "founder-os-main-v21-test",
) -> dict[str, Any]:
    checkpoint = guard_module.checkpoint_active(
        str(root),
        owner=owner,
        activation_token=state["activation_token"],
        expected_state_sha=state["state_sha"],
    )
    updated = dict(state)
    updated["state_sha"] = checkpoint["state_sha"]
    return updated


def make_operating_clear_project(
    root: Path,
    owner: str = "founder-os-main-v21-test",
) -> dict[str, Any]:
    active = create_empty_active_project(root, owner)
    state = initialize_new_strategy(root, active, owner)
    assessed = decision_module.assess_direction(
        str(root),
        owner=owner,
        activation_token=state["activation_token"],
        expected_state_sha=state["state_sha"],
        expected_strategy_sha=state["strategy_sha"],
        outcome="CLEAR",
        reason="The product, user, problem, value and boundaries are explicit",
        direction_summary="A local EXIF renaming tool for photographers",
        depth="NONE",
    )
    state = strategy_state(state, assessed)
    write_strategy_ledgers(root)
    state = checkpoint_external_changes(root, state, owner)
    confirmed = decision_module.confirm_canonical(
        str(root),
        owner=owner,
        activation_token=state["activation_token"],
        expected_state_sha=state["state_sha"],
        expected_strategy_sha=state["strategy_sha"],
        evidence="Five canonical ledgers record the clear direction",
    )
    return strategy_state(state, confirmed)


def initialize_thread_registry(
    root: Path,
    active: dict[str, Any],
    owner: str = "founder-os-main-test",
    capabilities: dict[str, str] | None = None,
) -> dict[str, Any]:
    return registry_module.initialize_registry(
        str(root),
        owner=owner,
        activation_token=active["activation_token"],
        expected_state_sha=active["state_sha"],
        expected_registry_sha="ABSENT",
        capabilities=capabilities,
        evidence=["deterministic validator probe"],
        runtime="validator-fixture",
    )


def reserve_persistent_thread(
    root: Path,
    state: dict[str, Any],
    *,
    agent_id: str = "technical-lead-01",
    logical_name: str = "Engineering - Technical Lead",
    owner: str = "founder-os-main-test",
    skills: list[str] | None = None,
) -> dict[str, Any]:
    return registry_module.reserve_thread(
        str(root),
        owner=owner,
        activation_token=state["activation_token"],
        expected_state_sha=state["state_sha"],
        expected_registry_sha=state["registry_sha"],
        agent_id=agent_id,
        agent_kind="persistent",
        logical_name=logical_name,
        manager_agent_id="founder-os-main",
        workstream="engineering",
        thread_type="persistent",
        read_scope=["src/**", "docs/**"],
        write_scope=["src/engineering/**"],
        skills=skills or [],
        dependencies=[],
    )


def bind_reserved_thread(
    root: Path,
    state: dict[str, Any],
    *,
    runtime_thread_id: str = "019ff012-0000-7000-8000-000000000001",
    runtime_host_id: str = "local",
    owner: str = "founder-os-main-test",
) -> dict[str, Any]:
    return registry_module.bind_runtime(
        str(root),
        owner=owner,
        activation_token=state["activation_token"],
        expected_state_sha=state["state_sha"],
        expected_registry_sha=state["registry_sha"],
        thread_record_id=state["details"]["thread_record_id"],
        binding_nonce=state["details"]["binding_nonce"],
        runtime_thread_id=runtime_thread_id,
        runtime_host_id=runtime_host_id,
        identity_quality="observed",
    )


def registry_state(root: Path, active: dict[str, Any], mutation: dict[str, Any]) -> dict[str, Any]:
    return {
        "activation_token": active["activation_token"],
        "state_sha": mutation["state_sha"],
        "registry_sha": mutation["registry_sha"],
        "details": mutation.get("details", {}),
    }


def normalized_scope_conflict(root: Path, left: str, right: str) -> bool:
    left_path = (root / left).resolve(strict=False)
    right_path = (root / right).resolve(strict=False)
    left_key = os.path.normcase(str(left_path))
    right_key = os.path.normcase(str(right_path))
    if left_key == right_key:
        return True
    try:
        common = os.path.commonpath((left_key, right_key))
    except ValueError:
        return False
    return common in {left_key, right_key}


def dependency_gate(upstream_state: str, interface_frozen: bool = True) -> bool:
    return upstream_state == "accepted" and interface_frozen


def integration_gate(workstreams: list[str], checks: dict[str, bool]) -> bool:
    return all(state in {"accepted", "ready-for-integration"} for state in workstreams) and all(
        checks.values()
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
        cls.all_text = "\n".join(
            (
                cls.skill,
                cls.delegation,
                cls.state_files,
                cls.supervision,
                cls.workstreams,
                cls.registry,
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


class ThreadRegistryTests(unittest.TestCase):
    OWNER = "founder-os-main-test"

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
                acknowledgement="same runtime Thread confirmed current project baseline",
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
                acknowledgement="Thread confirmed new decision revision",
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
    def _state_sync_ack(root: Path) -> str:
        strategy = decision_module.inspect_strategy(str(root))["strategy"]
        return (
            f"STRATEGY_CONTEXT_REVISION={strategy['context_revision']}; "
            f"STRATEGY_CONTEXT_SHA256={strategy['context_sha256']}; accepted"
        )

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
                acknowledgement=self._state_sync_ack(root),
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
                acknowledgement=self._state_sync_ack(root),
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
                acknowledgement=self._state_sync_ack(root),
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
            acknowledgement = (
                f"STRATEGY_CONTEXT_REVISION={current['context_revision']}; "
                f"STRATEGY_CONTEXT_SHA256={current['context_sha256']}; accepted"
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
            current = decision_module.inspect_strategy(str(root))["strategy"]
            acknowledgement = (
                f"STRATEGY_CONTEXT_REVISION={current['context_revision']}; "
                f"STRATEGY_CONTEXT_SHA256={current['context_sha256']}; accepted"
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


def main() -> int:
    os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    skill_before = snapshot_tree(SKILL_ROOT)
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    skill_after = snapshot_tree(SKILL_ROOT)
    tree_stable = skill_before == skill_after
    if not tree_stable:
        print("FAIL: validator changed the target Skill tree or metadata.")
    print(
        "CONDITIONAL: runtime-without-subagents cannot be reproduced when collaboration "
        "tools are present; verify fallback statically or in a capability-disabled runtime."
    )
    print(
        "FORWARD-TEST-REQUIRED: real subagent creation, Bootstrap behavior, Workstream "
        "parallel traces, rework, and Integration Gate behavior require fresh Codex agents."
    )
    return 0 if result.wasSuccessful() and tree_stable else 1


if __name__ == "__main__":
    raise SystemExit(main())

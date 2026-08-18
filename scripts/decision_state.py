#!/usr/bin/env python3
"""Deterministic FounderOS strategy-state and Strategic Gate controller.

This module does not decide whether a direction is clear, classify a real-world
choice, generate candidates, or recommend a strategy.  A reasoning agent does
those semantic jobs.  This module only validates explicit inputs, protects the
project-bound control state with CAS/fencing, and enforces legal transitions.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import secrets
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable

sys.dont_write_bytecode = True

import supervisor_guard as guard


STRATEGY_NAME = "STRATEGY.json"
STRATEGY_LOCK_NAME = ".strategy-state-lock.json"
THREADS_NAME = "THREADS.json"
SCHEMA_VERSION = 1
EXIT_INVALID = 2
EXIT_CONFLICT = 3

CORE_LEDGERS = ("PROJECT.md", "ROADMAP.md", "DECISIONS.md", "AGENTS.md", "STATUS.md")
CLARITY_STATES = {"UNASSESSED", "CLEAR", "AMBIGUOUS", "LEGACY_INFERRED"}
DISCOVERY_DEPTHS = {"NONE", "LIGHT", "STANDARD", "DEEP"}
STRATEGY_STATUSES = {"UNRESOLVED", "EXPLORATORY", "RECOMMENDED", "SELECTED", "DEFERRED"}
CANDIDATE_STATUSES = {"CANDIDATE", "EXPLORATORY", "RECOMMENDED", "SELECTED", "REJECTED", "DEFERRED"}
GATE_STATES = {
    "DIRECTION_CHECK_REQUIRED",
    "DISCOVERY_ACTIVE",
    "STRATEGIC_CHOICE_REQUIRED",
    "BOOTSTRAP_AUTHORIZED",
    "OPERATING",
    "DECISION_RECORD_REQUIRED",
    "STATE_SYNC_REQUIRED",
    "EXECUTIVE_APPROVAL_REQUIRED",
    "ADOPTION_STATE_REQUIRED",
}
GATE_CONTEXTS = {"bootstrap", "pivot", "autonomy", "executive", "adoption", "none"}
DECISION_LEVELS = {"L0", "L1", "L2", "L3"}
SELECTION_AUTHORITIES = {
    "founder",
    "delegated",
    "autonomy",
    "legacy-inferred",
    "founder-input",
    "adoption-reconstructed",
}
STRATEGIC_AUTONOMY = {"recommend_then_ask", "autonomous_with_report", "require_approval"}
THREAD_STRATEGY_SCOPES = {
    "candidate-bound",
    "discovery-read-only",
    "adoption-read-only",
    "unrelated-read-only",
    "control-recovery",
}
ACTION_TYPES = {
    "direction-assessment",
    "discovery-read-only",
    "adoption-read-only",
    "bootstrap",
    "persistent-thread-create",
    "candidate-bound-work",
    "unrelated-read-only",
    "state-sync",
    "canonical-decision-update",
    "integration",
    "subagent-dispatch",
    "executive-action",
}

PROJECT_ORIGINS = {"NEW", "ADOPTED", "UNKNOWN_LEGACY"}
PROJECT_LIFECYCLES = {
    "ACTIVE_DEVELOPMENT",
    "FEATURE_COMPLETE",
    "SHIPPED",
    "MAINTENANCE",
    "FROZEN",
    "ARCHIVED",
}
ADOPTION_STATUSES = {
    "NOT_APPLICABLE",
    "READ_ONLY_AUDIT",
    "BASELINE_READY",
    "ADOPTED",
    "BLOCKED",
}
ADOPTION_CONFIDENCES = {"HIGH", "MEDIUM", "LOW"}
ADOPTION_DETECTED_MODES = {
    "EXISTING_ACTIVE_PROJECT",
    "COMPLETED_PROJECT",
    "SHIPPED_PROJECT",
}
ADOPTION_MANAGEMENT_MODES = {
    "CONTINUE_DEVELOPMENT",
    "MAINTENANCE_MODE",
    "STABILIZATION",
    "MODERNIZATION_PROPOSAL",
    "FROZEN",
    "ARCHIVED",
}


class StrategyPartialCommit(guard.PartialCommit):
    """A strategy mutation cannot be safely presented as committed."""


def _text(value: Any, label: str, *, max_length: int = 1024) -> str:
    return guard.require_nonempty_text(value, label, max_length=max_length)


def _optional_text(value: Any, label: str, *, max_length: int = 1024) -> str | None:
    if value is None:
        return None
    return _text(value, label, max_length=max_length)


def _identifier(value: Any, label: str, *, max_length: int = 128) -> str:
    value = _text(value, label, max_length=max_length)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]*", value):
        raise guard.InvalidState(f"{label} contains an unsafe character")
    return value


def _agent_id(value: Any) -> str:
    value = _text(value, "agent_id", max_length=64)
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", value):
        raise guard.InvalidState("agent_id must be a lowercase stable slug")
    return value


def _sha_or_absent(value: str, label: str) -> str:
    normalized = _text(value, label).upper()
    if normalized != "ABSENT" and not re.fullmatch(r"[0-9A-F]{64}", normalized):
        raise guard.InvalidState(f"{label} must be ABSENT or a SHA-256 value")
    return normalized


def _is_reparse_or_link(path: Path) -> bool:
    return guard._is_reparse_or_link(path)


def _direct_file(path: Path, label: str) -> None:
    if not path.exists():
        raise guard.InvalidState(f"{label} does not exist: {path}")
    metadata = path.lstat()
    if _is_reparse_or_link(path) or not path.is_file() or metadata.st_nlink != 1:
        raise guard.InvalidState(f"{label} must be a direct single-link regular file: {path}")


def _project_binding_id(root: Path) -> str:
    normalized = os.path.normcase(str(root)).replace("\\", "/")
    material = f"founder-os-strategy-binding-v1\0{normalized}".encode("utf-8")
    return hashlib.sha256(material).hexdigest().upper()


def _new_revision(prefix: str) -> str:
    return guard.new_revision(prefix)


def _read_strategy(path: Path) -> tuple[str, bytes | None, dict[str, Any] | None]:
    if not path.exists():
        return "ABSENT", None, None
    _direct_file(path, "Strategy control state")
    raw, value = guard.read_json_object(path)
    return guard.sha256_bytes(raw), raw, value


def _atomic_replace_bytes(path: Path, content: bytes) -> None:
    descriptor: int | None = None
    temp_path: Path | None = None
    try:
        descriptor, raw_temp = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
        )
        temp_path = Path(raw_temp)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()


def _context_material(state: dict[str, Any]) -> dict[str, Any]:
    direction = state["direction"]
    profile = state["autonomy_profile"]
    selected = direction["strategy_status"] == "SELECTED"
    return {
        # Only operational authority and the selected direction invalidate
        # Worker context.  Discovery/Gate/audit metadata intentionally does not
        # create a self-staling loop while a proposal is being evaluated.
        "autonomy_profile": {
            "scope": profile["scope"],
            "implementation": profile["implementation"],
            "tactical": profile["tactical"],
            "strategic": profile["strategic"],
            "executive": profile["executive"],
        },
        "selected_direction": {
            "selected_strategy_id": direction["selected_strategy_id"] if selected else None,
            "selected_strategy_summary": (
                direction["selected_strategy_summary"] if selected else None
            ),
        },
    }


def _refresh_context(state: dict[str, Any], *, rotate: bool) -> None:
    if rotate or not state.get("context_revision"):
        state["context_revision"] = _new_revision("SC")
    state["context_sha256"] = guard.sha256_bytes(
        guard.canonical_json_bytes(_context_material(state))
    )


def _record_authorization_receipt(
    state: dict[str, Any],
    *,
    authorization_ref: str,
    kind: str,
    proposal_id: str | None,
    subject: str,
) -> str:
    """Consume one cooperative Founder authorization reference exactly once.

    A Skill cannot authenticate a chat message, but it can prevent the same
    recorded evidence from being rebound to a later proposal, profile change,
    or executive action.  The caller remains responsible for recording a real
    current user-message reference rather than inventing one.
    """

    material = {
        "authorization_ref": authorization_ref,
        "kind": kind,
        "proposal_id": proposal_id,
        "subject": subject,
    }
    authorization_sha = guard.sha256_bytes(authorization_ref.encode("utf-8"))
    receipts = state.setdefault("authorization_receipts", [])
    if any(row.get("authorization_sha256") == authorization_sha for row in receipts):
        raise guard.Conflict(
            "Founder authorization evidence has already been consumed; old replies cannot be rebound"
        )
    receipt_id = _new_revision("AR")
    receipts.append(
        {
            "receipt_id": receipt_id,
            "authorization_sha256": authorization_sha,
            "kind": kind,
            "proposal_id": proposal_id,
            "subject_sha256": guard.sha256_bytes(
                guard.canonical_json_bytes(material)
            ),
            "recorded_at": guard.utc_now(),
        }
    )
    return receipt_id


def _validate_string_list(value: Any, label: str, *, max_items: int = 32) -> list[str]:
    if not isinstance(value, list) or len(value) > max_items:
        raise guard.InvalidState(f"{label} must be a list with at most {max_items} items")
    return [_text(item, f"{label} item", max_length=512) for item in value]


def _normalize_candidates(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or not 1 <= len(raw) <= 5:
        raise guard.InvalidState("Discovery requires one to five candidate directions")
    normalized: list[dict[str, Any]] = []
    identifiers: set[str] = set()
    names: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise guard.InvalidState("Each strategic candidate must be an object")
        candidate_id = _identifier(item.get("candidate_id"), f"candidate[{index}].candidate_id")
        name = _text(item.get("name"), f"candidate[{index}].name", max_length=128)
        folded_name = name.casefold()
        folded_id = candidate_id.casefold()
        if folded_id in identifiers or folded_name in names:
            raise guard.InvalidState("Strategic candidates require unique IDs and names")
        identifiers.add(folded_id)
        names.add(folded_name)
        difficulty = item.get("difficulty")
        startup_cost = item.get("startup_cost")
        validation_speed = item.get("validation_speed")
        reversibility = item.get("reversibility")
        if difficulty not in {"LOW", "MEDIUM", "HIGH"}:
            raise guard.InvalidState("Candidate difficulty must be LOW/MEDIUM/HIGH")
        if startup_cost not in {"LOW", "MEDIUM", "HIGH"}:
            raise guard.InvalidState("Candidate startup_cost must be LOW/MEDIUM/HIGH")
        if validation_speed not in {"FAST", "MEDIUM", "SLOW"}:
            raise guard.InvalidState("Candidate validation_speed must be FAST/MEDIUM/SLOW")
        if reversibility not in {"LOW", "MEDIUM", "HIGH"}:
            raise guard.InvalidState("Candidate reversibility must be LOW/MEDIUM/HIGH")
        advantages = _validate_string_list(
            item.get("advantages"), f"candidate[{index}].advantages", max_items=8
        )
        risks = _validate_string_list(
            item.get("risks"), f"candidate[{index}].risks", max_items=8
        )
        if not advantages or not risks:
            raise guard.InvalidState("Each candidate requires at least one advantage and one risk")
        normalized.append(
            {
                "candidate_id": candidate_id,
                "name": name,
                "summary": _text(item.get("summary"), f"candidate[{index}].summary"),
                "target_user": _text(item.get("target_user"), f"candidate[{index}].target_user"),
                "problem": _text(item.get("problem"), f"candidate[{index}].problem"),
                "opportunity": _text(item.get("opportunity"), f"candidate[{index}].opportunity"),
                "advantages": advantages,
                "risks": risks,
                "difficulty": difficulty,
                "startup_cost": startup_cost,
                "validation_speed": validation_speed,
                "reversibility": reversibility,
                "roadmap_effect": _text(
                    item.get("roadmap_effect"), f"candidate[{index}].roadmap_effect"
                ),
                "assessment": _text(item.get("assessment"), f"candidate[{index}].assessment"),
                "status": "CANDIDATE",
            }
        )
    return normalized


def _validate_candidate(value: Any) -> None:
    if not isinstance(value, dict):
        raise guard.InvalidState("Strategic candidate must be an object")
    _identifier(value.get("candidate_id"), "candidate_id")
    for key in ("name", "summary", "target_user", "problem", "opportunity", "assessment"):
        _text(value.get(key), f"candidate {key}")
    if not _validate_string_list(value.get("advantages"), "candidate advantages", max_items=8):
        raise guard.InvalidState("Candidate advantages cannot be empty")
    if not _validate_string_list(value.get("risks"), "candidate risks", max_items=8):
        raise guard.InvalidState("Candidate risks cannot be empty")
    if value.get("difficulty") not in {"LOW", "MEDIUM", "HIGH"}:
        raise guard.InvalidState("Invalid candidate difficulty")
    if value.get("startup_cost") not in {"LOW", "MEDIUM", "HIGH"}:
        raise guard.InvalidState("Invalid candidate startup_cost")
    if value.get("validation_speed") not in {"FAST", "MEDIUM", "SLOW"}:
        raise guard.InvalidState("Invalid candidate validation_speed")
    if value.get("reversibility") not in {"LOW", "MEDIUM", "HIGH"}:
        raise guard.InvalidState("Invalid candidate reversibility")
    _text(value.get("roadmap_effect"), "candidate roadmap_effect")
    if value.get("status") not in CANDIDATE_STATUSES:
        raise guard.InvalidState("Invalid candidate status")


def _normalize_recommendation(
    value: Any, *, candidate_ids: set[str], recommendation_id: str
) -> dict[str, str]:
    if not isinstance(value, dict):
        raise guard.InvalidState("recommendation must be a JSON object")
    candidate_id = _identifier(value.get("candidate_id"), "recommendation candidate_id")
    if candidate_id != recommendation_id or candidate_id not in candidate_ids:
        raise guard.InvalidState("Recommendation object must bind the declared current candidate")
    return {
        "candidate_id": candidate_id,
        "rationale": _text(value.get("rationale"), "recommendation rationale"),
        "why_now": _text(value.get("why_now"), "recommendation why_now"),
        "biggest_downside": _text(
            value.get("biggest_downside"), "recommendation biggest_downside"
        ),
        "choose_another_when": _text(
            value.get("choose_another_when"), "recommendation choose_another_when"
        ),
    }


def _validate_recommendation(value: Any, recommendation_id: str | None) -> None:
    if recommendation_id is None:
        if value is not None:
            raise guard.InvalidState("Recommendation details require a recommendation_id")
        return
    normalized = _normalize_recommendation(
        value, candidate_ids={recommendation_id}, recommendation_id=recommendation_id
    )
    if normalized != value:
        raise guard.InvalidState("Recommendation details are not normalized")


def _validate_autonomy(value: Any) -> None:
    if not isinstance(value, dict):
        raise guard.InvalidState("autonomy_profile must be an object")
    if value.get("scope") != "project":
        raise guard.InvalidState("Autonomy Profile scope must be project")
    if value.get("implementation") != "autonomous":
        raise guard.InvalidState("Implementation autonomy must remain autonomous")
    if value.get("tactical") != "autonomous":
        raise guard.InvalidState("Tactical autonomy must remain autonomous")
    if value.get("strategic") not in STRATEGIC_AUTONOMY:
        raise guard.InvalidState("Invalid strategic autonomy")
    if value.get("executive") != "require_explicit_approval":
        raise guard.InvalidState("L3 executive approval cannot be weakened")
    _text(value.get("source"), "autonomy source")
    _text(value.get("evidence"), "autonomy evidence")


def _validate_adoption_metadata(state: dict[str, Any]) -> dict[str, Any] | None:
    """Validate the optional V2.3 Brownfield control-plane extension.

    V2.2 Strategy records intentionally remain valid without these fields.  If
    any Adoption field is present, the complete project-bound set is required
    so a partially upgraded control record fails closed.
    """

    field_names = {
        "project_origin",
        "project_lifecycle",
        "adoption_status",
        "adoption_confidence",
        "adoption",
    }
    present = {name for name in field_names if name in state}
    if not present:
        return None
    if present != field_names:
        raise guard.InvalidState("Adoption Strategy metadata is partial")
    if state.get("project_origin") not in PROJECT_ORIGINS:
        raise guard.InvalidState("Invalid project_origin")
    if state.get("project_origin") != "ADOPTED":
        raise guard.InvalidState("Brownfield Adoption records require project_origin=ADOPTED")
    if state.get("project_lifecycle") not in PROJECT_LIFECYCLES:
        raise guard.InvalidState("Invalid project_lifecycle")
    if state.get("adoption_status") not in ADOPTION_STATUSES:
        raise guard.InvalidState("Invalid adoption_status")
    if state.get("adoption_confidence") not in ADOPTION_CONFIDENCES:
        raise guard.InvalidState("Invalid adoption_confidence")
    adoption = state.get("adoption")
    if not isinstance(adoption, dict):
        raise guard.InvalidState("adoption must be an object")
    if adoption.get("detected_mode") not in ADOPTION_DETECTED_MODES:
        raise guard.InvalidState("Invalid Adoption detected_mode")
    if adoption.get("management_mode") not in ADOPTION_MANAGEMENT_MODES:
        raise guard.InvalidState("Invalid Adoption management_mode")
    baseline_id = _identifier(adoption.get("baseline_id"), "Adoption baseline_id")
    if not re.fullmatch(r"AB-[0-9A-F]{16}", baseline_id):
        raise guard.InvalidState("Adoption baseline_id must be AB- plus 16 uppercase hex characters")
    baseline_sha = _sha_or_absent(
        adoption.get("baseline_sha256"), "Adoption baseline_sha256"
    )
    if baseline_sha == "ABSENT":
        raise guard.InvalidState("Adoption baseline_sha256 cannot be ABSENT")
    if baseline_id != f"AB-{baseline_sha[:16]}":
        raise guard.InvalidState("Adoption baseline_id does not bind baseline_sha256")
    if adoption.get("behavior_preservation") is not True:
        raise guard.InvalidState("Brownfield Adoption requires behavior_preservation=true")
    evidence_refs = _validate_string_list(
        adoption.get("evidence_refs"), "Adoption evidence_refs"
    )
    if not evidence_refs:
        raise guard.InvalidState("Adoption evidence_refs cannot be empty")
    _validate_adoption_mode_pair(
        adoption["detected_mode"],
        state["project_lifecycle"],
        adoption["management_mode"],
    )
    _optional_text(adoption.get("adoption_review_ref"), "Adoption review reference")
    _optional_text(adoption.get("adopted_at"), "Adoption adopted_at")
    if state.get("adoption_status") == "ADOPTED" and adoption.get("adopted_at") is None:
        raise guard.InvalidState("ADOPTED Strategy requires adopted_at evidence")
    if state.get("adoption_status") == "BASELINE_READY" and adoption.get("adopted_at") is not None:
        raise guard.InvalidState("BASELINE_READY Strategy cannot already have adopted_at evidence")
    return adoption


def validate_strategy(state: dict[str, Any], root: Path) -> None:
    if state.get("schema_version") != SCHEMA_VERSION:
        raise guard.InvalidState("Unsupported or missing Strategy schema_version")
    _text(state.get("strategy_revision"), "strategy_revision")
    _sha_or_absent(state.get("previous_strategy_sha256"), "previous_strategy_sha256")
    _text(state.get("context_revision"), "strategy context_revision")
    context_sha = _sha_or_absent(state.get("context_sha256"), "strategy context_sha256")
    if context_sha == "ABSENT":
        raise guard.InvalidState("Strategy context_sha256 cannot be ABSENT")
    _text(state.get("created_at"), "strategy created_at")
    _text(state.get("updated_at"), "strategy updated_at")
    if state.get("project_phase") not in {"pre-bootstrap", "pre-adoption", "bootstrapped"}:
        raise guard.InvalidState("Invalid Strategy project_phase")
    adoption = _validate_adoption_metadata(state)

    binding = state.get("project_binding")
    if not isinstance(binding, dict):
        raise guard.InvalidState("Strategy project_binding must be an object")
    stored_root = binding.get("project_root")
    if not isinstance(stored_root, str) or not Path(stored_root).is_absolute():
        raise guard.InvalidState("Strategy project_root must be absolute")
    try:
        stored_resolved = Path(stored_root).resolve(strict=True)
    except OSError as exc:
        raise guard.InvalidState("Strategy project_root cannot be resolved") from exc
    if (
        os.path.normcase(str(stored_resolved)) != os.path.normcase(str(root))
        or os.path.normcase(str(Path(stored_root))) != os.path.normcase(str(stored_resolved))
    ):
        raise guard.InvalidState("Strategy belongs to a different project")
    if binding.get("project_binding_id") != _project_binding_id(root):
        raise guard.InvalidState("Strategy project_binding_id does not match")

    _validate_autonomy(state.get("autonomy_profile"))
    direction = state.get("direction")
    if not isinstance(direction, dict):
        raise guard.InvalidState("direction must be an object")
    if direction.get("clarity") not in CLARITY_STATES:
        raise guard.InvalidState("Invalid direction clarity")
    if direction.get("strategy_status") not in STRATEGY_STATUSES:
        raise guard.InvalidState("Invalid strategy status")
    _optional_text(direction.get("clarity_reason"), "clarity reason")
    for key in ("selected_strategy_id", "selected_strategy_summary", "selection_authority", "selection_rationale"):
        _optional_text(direction.get(key), f"direction {key}")
    if direction["strategy_status"] == "SELECTED":
        _identifier(direction.get("selected_strategy_id"), "selected_strategy_id")
        _text(direction.get("selected_strategy_summary"), "selected_strategy_summary")
        if direction.get("selection_authority") not in SELECTION_AUTHORITIES:
            raise guard.InvalidState("Selected strategy has invalid authority")
        _text(direction.get("selection_rationale"), "selection rationale")

    discovery = state.get("discovery")
    if not isinstance(discovery, dict) or discovery.get("depth") not in DISCOVERY_DEPTHS:
        raise guard.InvalidState("Discovery state is malformed")
    candidates = discovery.get("candidates")
    if not isinstance(candidates, list) or len(candidates) > 5:
        raise guard.InvalidState("Discovery candidates must be a list of at most five")
    candidate_ids: list[str] = []
    recommended = 0
    selected = 0
    for candidate in candidates:
        _validate_candidate(candidate)
        candidate_ids.append(candidate["candidate_id"])
        recommended += candidate["status"] == "RECOMMENDED"
        selected += candidate["status"] == "SELECTED"
    if len(candidate_ids) != len(set(candidate_ids)) or recommended > 1 or selected > 1:
        raise guard.InvalidState("Candidate IDs/recommendation/selection must be unique")
    recommendation_id = discovery.get("recommendation_id")
    if recommendation_id is not None:
        _identifier(recommendation_id, "recommendation_id")
        if recommendation_id not in candidate_ids:
            raise guard.InvalidState("Recommendation does not reference a current candidate")
    _validate_recommendation(discovery.get("recommendation"), recommendation_id)
    if recommendation_id is not None:
        recommended_rows = [
            candidate for candidate in candidates if candidate["status"] == "RECOMMENDED"
        ]
        selected_rows = [
            candidate for candidate in candidates if candidate["status"] == "SELECTED"
        ]
        if not selected_rows and (
            len(recommended_rows) != 1
            or recommended_rows[0]["candidate_id"] != recommendation_id
        ):
            raise guard.InvalidState("Recommendation ID and RECOMMENDED candidate disagree")
    _validate_string_list(discovery.get("evidence"), "discovery evidence", max_items=32)
    single_reason = discovery.get("single_candidate_reason")
    if len(candidates) == 1:
        _text(single_reason, "single candidate reason")
    elif single_reason is not None:
        _text(single_reason, "single candidate reason")

    gate = state.get("gate")
    if not isinstance(gate, dict) or gate.get("state") not in GATE_STATES:
        raise guard.InvalidState("Strategic Gate state is malformed")
    if gate.get("context") not in GATE_CONTEXTS:
        raise guard.InvalidState("Strategic Gate context is invalid")
    level = gate.get("decision_level")
    if level is not None and level not in DECISION_LEVELS:
        raise guard.InvalidState("Strategic Gate decision level is invalid")
    for key in (
        "proposal_id",
        "reason",
        "authorization_ref",
        "action_scope",
        "opened_at",
        "resolved_at",
    ):
        _optional_text(gate.get(key), f"gate {key}")
    if gate["state"] == "STRATEGIC_CHOICE_REQUIRED":
        if (
            gate.get("decision_level") != "L2"
            or recommendation_id is None
            or gate.get("proposal_id") is None
        ):
            raise guard.InvalidState("Strategic Choice Gate requires an L2 recommendation")
    if gate["state"] == "EXECUTIVE_APPROVAL_REQUIRED":
        if gate.get("decision_level") != "L3" or gate.get("action_scope") is None:
            raise guard.InvalidState("Executive Gate must be L3 and action-scoped")

    decision = state.get("decision_record")
    if not isinstance(decision, dict) or decision.get("status") not in {"not-required", "pending", "confirmed"}:
        raise guard.InvalidState("decision_record is malformed")
    for key in (
        "decision_id",
        "level",
        "proposal_id",
        "selected_strategy_id",
        "selection_authority",
        "authorization_ref",
        "canonical_evidence",
        "action_scope",
    ):
        _optional_text(decision.get(key), f"decision {key}")
    if decision["status"] in {"pending", "confirmed"}:
        _identifier(decision.get("decision_id"), "decision_id")
        _identifier(decision.get("proposal_id"), "decision proposal_id")
        if decision.get("level") not in {"L2", "L3"}:
            raise guard.InvalidState("Recorded strategic decision must be L2 or L3")
        _text(decision.get("authorization_ref"), "decision authorization_ref")
        if decision["level"] == "L2":
            _identifier(decision.get("selected_strategy_id"), "decision selected_strategy_id")
            if decision.get("selection_authority") not in {"founder", "delegated", "autonomy"}:
                raise guard.InvalidState("L2 decision has invalid selection authority")
        elif decision.get("selection_authority") != "founder":
            raise guard.InvalidState("L3 decision authority must be Founder")
        if decision["level"] == "L3":
            _text(decision.get("action_scope"), "L3 decision action_scope")
            action_status = decision.get("action_status")
            if action_status is not None and action_status not in {"approved", "consumed"}:
                raise guard.InvalidState("L3 decision action_status is invalid")
            _optional_text(decision.get("execution_ref"), "L3 decision execution_ref")
            _optional_text(decision.get("consumed_at"), "L3 decision consumed_at")
            if action_status == "consumed" and (
                decision.get("execution_ref") is None or decision.get("consumed_at") is None
            ):
                raise guard.InvalidState("Consumed L3 authorization requires execution evidence")
        if decision["status"] == "confirmed":
            _text(decision.get("canonical_evidence"), "decision canonical_evidence")

    receipts = state.get("authorization_receipts", [])
    if not isinstance(receipts, list) or len(receipts) > 256:
        raise guard.InvalidState("authorization_receipts must be a bounded list")
    receipt_ids: set[str] = set()
    authorization_hashes: set[str] = set()
    for row in receipts:
        if not isinstance(row, dict):
            raise guard.InvalidState("Authorization receipt must be an object")
        receipt_id = _identifier(row.get("receipt_id"), "authorization receipt_id")
        authorization_sha = _sha_or_absent(
            row.get("authorization_sha256"), "authorization receipt sha256"
        )
        subject_sha = _sha_or_absent(
            row.get("subject_sha256"), "authorization receipt subject_sha256"
        )
        if "ABSENT" in {authorization_sha, subject_sha}:
            raise guard.InvalidState("Authorization receipt hashes cannot be ABSENT")
        if receipt_id in receipt_ids or authorization_sha in authorization_hashes:
            raise guard.InvalidState("Authorization receipts must be unique and single-use")
        receipt_ids.add(receipt_id)
        authorization_hashes.add(authorization_sha)
        if row.get("kind") not in {
            "strategic-selection",
            "autonomy-profile",
            "executive-approval",
            "executive-rejection",
        }:
            raise guard.InvalidState("Authorization receipt kind is invalid")
        proposal_id = row.get("proposal_id")
        if proposal_id is not None:
            _identifier(proposal_id, "authorization receipt proposal_id")
        _text(row.get("recorded_at"), "authorization receipt recorded_at")

    assignments = state.get("discovery_assignments")
    if not isinstance(assignments, list):
        raise guard.InvalidState("discovery_assignments must be a list")
    assignment_ids: set[str] = set()
    runtime_ids: set[str] = set()
    for assignment in assignments:
        if not isinstance(assignment, dict):
            raise guard.InvalidState("Discovery assignment must be an object")
        assignment_id = _identifier(assignment.get("assignment_id"), "assignment_id")
        runtime_id = _identifier(assignment.get("runtime_agent_id"), "runtime_agent_id", max_length=256)
        if assignment_id in assignment_ids or runtime_id in runtime_ids:
            raise guard.InvalidState("Discovery assignment IDs must be unique")
        assignment_ids.add(assignment_id)
        runtime_ids.add(runtime_id)
        _text(assignment.get("role"), "discovery assignment role")
        _text(assignment.get("task"), "discovery assignment task")
        _validate_string_list(assignment.get("read_scope"), "discovery read scope", max_items=32)
        if assignment.get("write_scope") != []:
            raise guard.InvalidState("Pre-bootstrap Discovery Agents must be read-only")
        if assignment.get("status") not in {"dispatched", "returned", "accepted", "failed"}:
            raise guard.InvalidState("Invalid Discovery assignment status")
        _optional_text(assignment.get("evidence"), "discovery assignment evidence")

    pending_sync = state.get("pending_state_sync")
    if not isinstance(pending_sync, list):
        raise guard.InvalidState("pending_state_sync must be a list")
    seen_agents: set[str] = set()
    for item in pending_sync:
        if not isinstance(item, dict):
            raise guard.InvalidState("pending_state_sync item must be an object")
        agent = _agent_id(item.get("agent_id"))
        if agent in seen_agents:
            raise guard.InvalidState("pending_state_sync contains duplicate Agents")
        seen_agents.add(agent)
        if item.get("status") not in {"pending", "confirmed"}:
            raise guard.InvalidState("Invalid STATE_SYNC status")
        if item.get("disposition") not in {
            "sync-required",
            "synced",
            "retired",
            "not-applicable",
        }:
            raise guard.InvalidState("Invalid STATE_SYNC disposition")
        _optional_text(item.get("thread_record_id"), "STATE_SYNC thread_record_id")
        _optional_text(item.get("evidence"), "STATE_SYNC evidence")
    if gate["state"] == "STATE_SYNC_REQUIRED" and not pending_sync:
        raise guard.InvalidState("STATE_SYNC_REQUIRED needs affected Agents")

    reporting = state.get("reporting")
    if not isinstance(reporting, dict) or not isinstance(reporting.get("pending_decision_ids"), list):
        raise guard.InvalidState("Strategic reporting state is malformed")
    for item in reporting["pending_decision_ids"]:
        _identifier(item, "pending report decision_id")
    if len(reporting["pending_decision_ids"]) != len(set(reporting["pending_decision_ids"])):
        raise guard.InvalidState("Pending report IDs contain duplicates")
    reported_rows = reporting.get("reported", [])
    if not isinstance(reported_rows, list):
        raise guard.InvalidState("Strategic reported history is malformed")
    reported_ids: set[str] = set()
    for row in reported_rows:
        if not isinstance(row, dict):
            raise guard.InvalidState("Strategic report history row must be an object")
        reported_id = _identifier(row.get("decision_id"), "reported decision_id")
        if reported_id in reported_ids:
            raise guard.InvalidState("Strategic report history contains duplicate decisions")
        reported_ids.add(reported_id)
        _text(row.get("delivery_ref"), "strategic report delivery_ref")
        _text(row.get("reported_at"), "strategic report reported_at")

    history = state.get("discovery_history")
    if not isinstance(history, list):
        raise guard.InvalidState("discovery_history must be a list")
    historical_proposals: set[str] = set()
    for row in history:
        if not isinstance(row, dict):
            raise guard.InvalidState("Discovery history row must be an object")
        proposal_id = _identifier(row.get("proposal_id"), "historical proposal_id")
        if proposal_id in historical_proposals:
            raise guard.InvalidState("Discovery history contains duplicate proposal IDs")
        historical_proposals.add(proposal_id)
        if row.get("context") not in {"bootstrap", "pivot"}:
            raise guard.InvalidState("Discovery history context is invalid")
        if row.get("disposition") not in {"selected", "revised", "superseded"}:
            raise guard.InvalidState("Discovery history disposition is invalid")
        _text(row.get("closed_at"), "Discovery history closed_at")
        _text(row.get("reason"), "Discovery history reason")
        historical_candidates = row.get("candidates")
        if not isinstance(historical_candidates, list) or not historical_candidates:
            raise guard.InvalidState("Discovery history requires candidate evidence")
        for candidate in historical_candidates:
            _validate_candidate(candidate)
        historical_recommendation_id = _identifier(
            row.get("recommendation_id"), "historical recommendation_id"
        )
        _validate_recommendation(row.get("recommendation"), historical_recommendation_id)
    proposal_ids = state.get("proposal_ids", [])
    if not isinstance(proposal_ids, list):
        raise guard.InvalidState("proposal_ids must be a list")
    normalized_proposal_ids = [
        _identifier(item, "used proposal_id") for item in proposal_ids
    ]
    if len(normalized_proposal_ids) != len(set(normalized_proposal_ids)):
        raise guard.InvalidState("proposal_ids contains duplicates")

    phase = state["project_phase"]
    gate_state = gate["state"]
    prebootstrap_gates = {
        "DIRECTION_CHECK_REQUIRED",
        "DISCOVERY_ACTIVE",
        "STRATEGIC_CHOICE_REQUIRED",
        "BOOTSTRAP_AUTHORIZED",
    }
    preadoption_gates = {"ADOPTION_STATE_REQUIRED"}
    bootstrapped_gates = {
        "DISCOVERY_ACTIVE",
        "STRATEGIC_CHOICE_REQUIRED",
        "OPERATING",
        "DECISION_RECORD_REQUIRED",
        "STATE_SYNC_REQUIRED",
        "EXECUTIVE_APPROVAL_REQUIRED",
    }
    if phase == "pre-bootstrap" and gate_state not in prebootstrap_gates:
        raise guard.InvalidState("Pre-bootstrap Strategy has an unreachable Gate state")
    if phase == "pre-adoption" and gate_state not in preadoption_gates:
        raise guard.InvalidState("Pre-adoption Strategy has an unreachable Gate state")
    if phase == "bootstrapped" and gate_state not in bootstrapped_gates:
        raise guard.InvalidState("Bootstrapped Strategy has an unreachable Gate state")
    if phase == "pre-adoption" and adoption is None:
        raise guard.InvalidState("Pre-adoption Strategy requires complete Adoption metadata")
    if adoption is not None:
        if phase == "pre-adoption" and state["adoption_status"] != "BASELINE_READY":
            raise guard.InvalidState("Pre-adoption Strategy must be BASELINE_READY")
        if phase == "bootstrapped" and state["adoption_status"] != "ADOPTED":
            raise guard.InvalidState("Bootstrapped adopted Strategy must be ADOPTED")
        if phase == "pre-bootstrap":
            raise guard.InvalidState("A new-project pre-bootstrap Strategy cannot contain Adoption metadata")
    expected_contexts = {
        "DIRECTION_CHECK_REQUIRED": {"bootstrap"},
        "BOOTSTRAP_AUTHORIZED": {"bootstrap"},
        "OPERATING": {"none"},
        "DECISION_RECORD_REQUIRED": {"pivot", "executive"},
        "STATE_SYNC_REQUIRED": {"pivot", "autonomy"},
        "EXECUTIVE_APPROVAL_REQUIRED": {"executive"},
        "ADOPTION_STATE_REQUIRED": {"adoption"},
    }
    if gate_state in expected_contexts and gate["context"] not in expected_contexts[gate_state]:
        raise guard.InvalidState("Strategic Gate state/context combination is unreachable")
    if gate_state in {"DISCOVERY_ACTIVE", "STRATEGIC_CHOICE_REQUIRED"}:
        expected_discovery_context = "bootstrap" if phase == "pre-bootstrap" else "pivot"
        if gate["context"] != expected_discovery_context:
            raise guard.InvalidState("Discovery Gate context does not match project phase")
    if gate["state"] == "BOOTSTRAP_AUTHORIZED" and direction["strategy_status"] != "SELECTED":
        raise guard.InvalidState("Bootstrap requires a selected direction")
    if gate_state == "ADOPTION_STATE_REQUIRED" and (
        direction["strategy_status"] != "SELECTED"
        or direction.get("selection_authority") != "adoption-reconstructed"
        or discovery.get("depth") != "NONE"
        or candidates
    ):
        raise guard.InvalidState(
            "Adoption Gate requires the current reconstructed direction without Founder Discovery"
        )
    if gate_state == "DECISION_RECORD_REQUIRED" and decision["status"] != "pending":
        raise guard.InvalidState("DECISION_RECORD_REQUIRED needs one pending strategic decision")
    if (
        gate_state == "STATE_SYNC_REQUIRED"
        and gate["context"] == "pivot"
        and decision["status"] != "confirmed"
    ):
        raise guard.InvalidState("Pivot STATE_SYNC_REQUIRED needs a confirmed strategic decision")
    if gate_state == "OPERATING" and decision["status"] == "pending":
        raise guard.InvalidState("An operating project cannot retain a pending strategic decision")
    if decision["status"] in {"pending", "confirmed"}:
        if decision["proposal_id"] != gate.get("proposal_id"):
            raise guard.InvalidState("Strategic decision does not bind the current proposal")
        if decision["level"] == "L2" and (
            decision["selected_strategy_id"] != direction["selected_strategy_id"]
            or decision["selection_authority"] != direction["selection_authority"]
        ):
            raise guard.InvalidState("L2 decision and selected direction disagree")
    if selected:
        selected_candidate = next(
            candidate for candidate in candidates if candidate["status"] == "SELECTED"
        )
        if (
            direction["strategy_status"] != "SELECTED"
            or direction["selected_strategy_id"] != selected_candidate["candidate_id"]
        ):
            raise guard.InvalidState("Selected candidate and Direction state disagree")
    if phase in {"pre-bootstrap", "pre-adoption"} and pending_sync:
        raise guard.InvalidState("Pre-operating projects cannot have persistent STATE_SYNC obligations")
    expected_context_sha = guard.sha256_bytes(guard.canonical_json_bytes(_context_material(state)))
    if state["context_sha256"] != expected_context_sha:
        raise guard.InvalidState("Strategy semantic context hash does not match content")


def inspect_strategy(project: str) -> dict[str, Any]:
    root, founder, _created = guard.resolve_project_root(project)
    strategy_sha, _raw, state = _read_strategy(founder / STRATEGY_NAME)
    if state is not None:
        validate_strategy(state, root)
    lock_path = founder / STRATEGY_LOCK_NAME
    transaction: dict[str, Any] = {"state": "none"}
    if lock_path.exists():
        _direct_file(lock_path, "Strategy transaction lock")
        _lock_raw, lock = guard.read_json_object(lock_path)
        transaction = {
            "state": "recovery-required",
            "owner": _text(lock.get("owner"), "Strategy lock owner"),
            "expected_strategy_sha": _sha_or_absent(
                lock.get("expected_strategy_sha"), "Strategy lock expected_strategy_sha"
            ),
            "expected_supervisor_state_sha": _sha_or_absent(
                lock.get("expected_supervisor_state_sha"),
                "Strategy lock expected_supervisor_state_sha",
            ),
            "created_at": _text(lock.get("created_at"), "Strategy lock created_at"),
        }
    return {
        "result": "STRATEGY_INSPECTED",
        "project_root": str(root),
        "strategy_sha": strategy_sha,
        "strategy": state,
        "transaction": transaction,
        "changed_paths": [],
    }


def _acquire_strategy_lock(
    path: Path,
    *,
    root: Path,
    owner: str,
    expected_strategy_sha: str,
    expected_state_sha: str,
) -> str:
    nonce = f"SL_{secrets.token_urlsafe(16)}"
    guard._atomic_create(
        path,
        {
            "project_root": str(root),
            "owner": owner,
            "nonce": nonce,
            "expected_strategy_sha": expected_strategy_sha,
            "expected_supervisor_state_sha": expected_state_sha,
            "created_at": guard.utc_now(),
        },
    )
    return nonce


def _release_strategy_lock(path: Path, *, owner: str, nonce: str) -> None:
    _direct_file(path, "Strategy transaction lock")
    _raw, value = guard.read_json_object(path)
    if value.get("owner") != owner or value.get("nonce") != nonce:
        raise guard.Conflict("Strategy transaction lock belongs to another operation")
    path.unlink()


def _mutate_strategy(
    project: str,
    *,
    owner: str,
    activation_token: str,
    expected_state_sha: str,
    expected_strategy_sha: str,
    operation: str,
    mutate: Callable[[dict[str, Any] | None, Path], tuple[dict[str, Any], dict[str, Any]]],
) -> dict[str, Any]:
    owner = _text(owner, "owner")
    activation_token = _text(activation_token, "activation_token")
    expected_state_sha = _sha_or_absent(expected_state_sha, "expected_state_sha")
    expected_strategy_sha = _sha_or_absent(expected_strategy_sha, "expected_strategy_sha")
    root, founder, _created = guard.resolve_project_root(project)
    fence = guard.verify_fence(str(root), owner=owner, activation_token=activation_token)
    if fence["state_sha"] != expected_state_sha:
        raise guard.Conflict("Supervisor state CAS mismatch before Strategy mutation")

    strategy_path = founder / STRATEGY_NAME
    lock_path = founder / STRATEGY_LOCK_NAME
    preflight_sha, _preflight_raw, preflight_state = _read_strategy(strategy_path)
    if preflight_sha != expected_strategy_sha:
        raise guard.Conflict(
            f"Strategy CAS mismatch: expected {expected_strategy_sha}, observed {preflight_sha}"
        )
    if preflight_state is not None:
        validate_strategy(preflight_state, root)
    preflight_next, _details = mutate(copy.deepcopy(preflight_state), founder)
    validate_strategy(preflight_next, root)

    commit_mutex = guard.acquire_governance_commit_mutex(
        str(root), operation=f"strategy:{operation}"
    )
    try:
        nonce = _acquire_strategy_lock(
            lock_path,
            root=root,
            owner=owner,
            expected_strategy_sha=expected_strategy_sha,
            expected_state_sha=expected_state_sha,
        )
    except Exception:
        commit_mutex.close()
        raise
    release_lock = True
    old_raw: bytes | None = None
    try:
        confirmed_state_sha, state_record = guard.state_observation(founder / guard.STATE_NAME)
        if confirmed_state_sha != expected_state_sha or state_record is None:
            raise guard.Conflict("Supervisor state changed during Strategy mutation")
        strategy_sha, old_raw, current = _read_strategy(strategy_path)
        if strategy_sha != expected_strategy_sha:
            raise guard.Conflict(
                f"Strategy CAS mismatch: expected {expected_strategy_sha}, observed {strategy_sha}"
            )
        if current is not None:
            validate_strategy(current, root)
        next_state, details = mutate(copy.deepcopy(current), founder)
        next_state["strategy_revision"] = _new_revision("ST")
        next_state["previous_strategy_sha256"] = strategy_sha
        next_state["updated_at"] = guard.utc_now()
        validate_strategy(next_state, root)
        next_raw = guard.canonical_json_bytes(next_state)
        _atomic_replace_bytes(strategy_path, next_raw)
        next_strategy_sha = guard.sha256_bytes(next_raw)
        try:
            checkpoint = guard.checkpoint_active(
                str(root),
                owner=owner,
                activation_token=activation_token,
                expected_state_sha=expected_state_sha,
                _commit_mutex_held=True,
            )
        except guard.PartialCommit as exc:
            release_lock = False
            raise StrategyPartialCommit(
                "Strategy changed and Supervisor checkpoint partially committed; preserve locks",
                changed_paths=[str(strategy_path), str(founder / guard.STATE_NAME), str(founder / guard.LOCK_NAME), str(lock_path)],
                recovery_action="reconcile-strategy-checkpoint",
            ) from exc
        except Exception as exc:
            try:
                if old_raw is None:
                    _direct_file(strategy_path, "Strategy rollback target")
                    strategy_path.unlink()
                else:
                    _atomic_replace_bytes(strategy_path, old_raw)
            except Exception as rollback_exc:
                release_lock = False
                raise StrategyPartialCommit(
                    "Strategy checkpoint failed and rollback was not provable; preserve lock",
                    changed_paths=[str(strategy_path), str(lock_path)],
                    recovery_action="reconcile-strategy-rollback",
                ) from rollback_exc
            raise exc
        return {
            "result": operation,
            "mode": "ACTIVE",
            "owner": owner,
            "project_root": str(root),
            "strategy_revision": next_state["strategy_revision"],
            "strategy_sha": next_strategy_sha,
            "strategy_context_revision": next_state["context_revision"],
            "strategy_context_sha": next_state["context_sha256"],
            "state_sha": checkpoint["state_sha"],
            "details": details,
            "changed_paths": [str(strategy_path), str(founder / guard.STATE_NAME), str(founder / guard.LOCK_NAME)],
        }
    finally:
        try:
            if release_lock:
                try:
                    _release_strategy_lock(lock_path, owner=owner, nonce=nonce)
                except (guard.GuardError, OSError) as exc:
                    raise StrategyPartialCommit(
                        "Strategy mutation completed but its transaction lock could not be released",
                        changed_paths=[str(lock_path)],
                        recovery_action="clear-strategy-lock-after-audit",
                    ) from exc
        finally:
            commit_mutex.close()


def recover_strategy_lock(
    project: str,
    *,
    owner: str,
    activation_token: str,
    expected_state_sha: str,
    expected_strategy_sha: str,
    lock_owner: str,
    predecessor_liveness: str,
    authorization_ref: str,
    _commit_mutex_held: bool = False,
) -> dict[str, Any]:
    """Reconcile a stranded Strategy transaction under the current fence."""
    owner = _text(owner, "owner")
    activation_token = _text(activation_token, "activation_token")
    lock_owner = _text(lock_owner, "lock_owner")
    if predecessor_liveness not in {"current", "terminated"}:
        raise guard.InvalidState("predecessor_liveness must be current or terminated")
    authorization_ref = _text(authorization_ref, "Strategy recovery authorization")
    expected_state_sha = _sha_or_absent(expected_state_sha, "expected_state_sha")
    expected_strategy_sha = _sha_or_absent(expected_strategy_sha, "expected_strategy_sha")
    root, founder, _created = guard.resolve_project_root(project)
    if not _commit_mutex_held:
        with guard.acquire_governance_commit_mutex(
            str(root), operation="strategy:recover"
        ):
            return recover_strategy_lock(
                str(root), owner=owner, activation_token=activation_token,
                expected_state_sha=expected_state_sha,
                expected_strategy_sha=expected_strategy_sha,
                lock_owner=lock_owner,
                predecessor_liveness=predecessor_liveness,
                authorization_ref=authorization_ref,
                _commit_mutex_held=True,
            )
    fence = guard.verify_fence(str(root), owner=owner, activation_token=activation_token)
    if fence["state_sha"] != expected_state_sha:
        raise guard.Conflict("Supervisor state CAS mismatch before Strategy recovery")
    lock_path = founder / STRATEGY_LOCK_NAME
    _direct_file(lock_path, "Strategy transaction lock")
    _lock_raw, lock = guard.read_json_object(lock_path)
    if lock.get("owner") != lock_owner:
        raise guard.Conflict("Declared lock_owner does not match the stranded transaction")
    if lock_owner == owner and predecessor_liveness != "current":
        raise guard.Conflict("Current owner's Strategy lock requires predecessor_liveness=current")
    if lock_owner != owner and predecessor_liveness != "terminated":
        raise guard.Conflict("Another owner's Strategy lock requires proven terminated predecessor")
    if os.path.normcase(str(lock.get("project_root"))) != os.path.normcase(str(root)):
        raise guard.InvalidState("Strategy transaction lock belongs to another project")
    nonce = _text(lock.get("nonce"), "Strategy lock nonce")
    current_sha, _raw, state = _read_strategy(founder / STRATEGY_NAME)
    if current_sha != expected_strategy_sha:
        raise guard.Conflict("Strategy state changed before lock recovery")
    if state is not None:
        validate_strategy(state, root)
    observed_state_sha, supervisor = guard.state_observation(founder / guard.STATE_NAME)
    if observed_state_sha != expected_state_sha or supervisor is None:
        raise guard.Conflict("Supervisor state changed during Strategy recovery")
    supervisor_strategy_sha = supervisor.get("source_revisions", {}).get("STRATEGY_SHA256")
    next_state_sha = observed_state_sha
    if current_sha == "ABSENT":
        if supervisor_strategy_sha is not None:
            raise guard.Conflict("Supervisor expects Strategy content that is absent; preserve recovery lock")
    elif supervisor_strategy_sha != current_sha:
        checkpoint = guard.checkpoint_active(
            str(root),
            owner=owner,
            activation_token=activation_token,
            expected_state_sha=expected_state_sha,
            _commit_mutex_held=True,
        )
        next_state_sha = checkpoint["state_sha"]
    _direct_file(lock_path, "Strategy transaction lock")
    _confirm_raw, confirmed_lock = guard.read_json_object(lock_path)
    if confirmed_lock.get("owner") != lock_owner or confirmed_lock.get("nonce") != nonce:
        raise guard.Conflict("Strategy recovery lock changed before release")
    lock_path.unlink()
    return {
        "result": "STRATEGY_LOCK_RECOVERED",
        "mode": "ACTIVE",
        "owner": owner,
        "prior_lock_owner": lock_owner,
        "authorization_ref": authorization_ref,
        "strategy_sha": current_sha,
        "state_sha": next_state_sha,
        "changed_paths": [str(lock_path), str(founder / guard.STATE_NAME), str(founder / guard.LOCK_NAME)],
    }


def _default_profile(source: str, evidence: str) -> dict[str, str]:
    return {
        "scope": "project",
        "implementation": "autonomous",
        "tactical": "autonomous",
        "strategic": "recommend_then_ask",
        "executive": "require_explicit_approval",
        "source": source,
        "evidence": evidence,
    }


def initialize_strategy(
    project: str,
    *,
    owner: str,
    activation_token: str,
    expected_state_sha: str,
    expected_strategy_sha: str,
    mode: str,
    legacy_summary: str | None,
    evidence: str,
) -> dict[str, Any]:
    if mode not in {"new", "legacy"}:
        raise guard.InvalidState("Strategy init mode must be new or legacy")
    evidence = _text(evidence, "Strategy initialization evidence")
    legacy_summary = _optional_text(legacy_summary, "legacy selected strategy summary")

    def mutate(state: dict[str, Any] | None, founder: Path):
        if state is not None:
            raise guard.Conflict("Strategy state already exists; inspect or recover it")
        ledger_presence = {name: (founder / name).exists() for name in CORE_LEDGERS}
        if mode == "new" and any(ledger_presence.values()):
            raise guard.Conflict(
                "New Strategy initialization requires zero canonical ledgers; existing or partial state needs migration/recovery"
            )
        if mode == "legacy":
            if not all(ledger_presence.values()):
                raise guard.Conflict("Legacy Strategy migration requires all five canonical ledgers")
            for name in CORE_LEDGERS:
                _direct_file(founder / name, name)
            if legacy_summary is None:
                raise guard.InvalidState("Legacy migration requires an inferred selected strategy summary")
        root = founder.parent
        now = guard.utc_now()
        value: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "strategy_revision": _new_revision("ST"),
            "previous_strategy_sha256": "ABSENT",
            "context_revision": "",
            "context_sha256": "",
            "created_at": now,
            "updated_at": now,
            "project_phase": "bootstrapped" if mode == "legacy" else "pre-bootstrap",
            "project_binding": {
                "project_root": str(root),
                "project_binding_id": _project_binding_id(root),
            },
            "autonomy_profile": _default_profile(
                "legacy-default" if mode == "legacy" else "default", evidence
            ),
            "direction": {
                "clarity": "LEGACY_INFERRED" if mode == "legacy" else "UNASSESSED",
                "clarity_reason": evidence if mode == "legacy" else None,
                "strategy_status": "SELECTED" if mode == "legacy" else "UNRESOLVED",
                "selected_strategy_id": "legacy-inferred" if mode == "legacy" else None,
                "selected_strategy_summary": legacy_summary,
                "selection_authority": "legacy-inferred" if mode == "legacy" else None,
                "selection_rationale": evidence if mode == "legacy" else None,
            },
            "discovery": {
                "depth": "NONE",
                "candidates": [],
                "recommendation_id": None,
                "recommendation": None,
                "evidence": [evidence],
                "single_candidate_reason": None,
            },
            "gate": {
                "state": "OPERATING" if mode == "legacy" else "DIRECTION_CHECK_REQUIRED",
                "context": "none" if mode == "legacy" else "bootstrap",
                "decision_level": None,
                "proposal_id": None,
                "reason": evidence,
                "authorization_ref": None,
                "opened_at": now if mode == "new" else None,
                "resolved_at": now if mode == "legacy" else None,
            },
            "decision_record": {
                "status": "not-required",
                "decision_id": None,
                "level": None,
                "proposal_id": None,
                "selected_strategy_id": None,
                "selection_authority": None,
                "authorization_ref": None,
                "canonical_evidence": evidence if mode == "legacy" else None,
                "action_scope": None,
            },
            "discovery_assignments": [],
            "pending_state_sync": [],
            "reporting": {"pending_decision_ids": [], "reported": []},
            "discovery_history": [],
            "proposal_ids": [],
            "authorization_receipts": [],
        }
        _refresh_context(value, rotate=True)
        return value, {"mode": mode, "gate": value["gate"]["state"]}

    return _mutate_strategy(
        project,
        owner=owner,
        activation_token=activation_token,
        expected_state_sha=expected_state_sha,
        expected_strategy_sha=expected_strategy_sha,
        operation="STRATEGY_INITIALIZED",
        mutate=mutate,
    )


def _observe_adoption_baseline(root: Path) -> tuple[str, str]:
    """Recompute the read-only project baseline used by the Adoption CAS."""

    # Local import keeps V2.2 new/legacy paths independent from the optional
    # Brownfield inspector and avoids any import-time project access.
    import project_baseline as baseline_api

    report = baseline_api.inspect_project(str(root))
    completeness = report.get("completeness", {})
    anchor_usable = completeness.get(
        "baseline_anchor_usable", completeness.get("baseline_usable")
    )
    if report.get("result") not in {"COMPLETE", "PARTIAL"} or anchor_usable is not True:
        raise guard.Conflict(
            "ADOPTION_BASELINE_UNUSABLE: the deterministic project anchor is incomplete or unstable"
        )
    baseline_id = _identifier(report.get("baseline_id"), "observed Adoption baseline_id")
    baseline_sha = _sha_or_absent(
        report.get("baseline_sha256"), "observed Adoption baseline_sha256"
    )
    if baseline_sha == "ABSENT":
        raise guard.InvalidState("Observed Adoption baseline cannot be ABSENT")
    return baseline_id, baseline_sha


def _validate_adoption_mode_pair(
    detected_mode: str, project_lifecycle: str, management_mode: str
) -> None:
    if detected_mode not in ADOPTION_DETECTED_MODES:
        raise guard.InvalidState("Invalid Adoption detected_mode")
    if project_lifecycle not in PROJECT_LIFECYCLES:
        raise guard.InvalidState("Invalid Adoption project_lifecycle")
    if management_mode not in ADOPTION_MANAGEMENT_MODES:
        raise guard.InvalidState("Invalid Adoption management_mode")
    if detected_mode == "EXISTING_ACTIVE_PROJECT" and project_lifecycle != "ACTIVE_DEVELOPMENT":
        raise guard.InvalidState(
            "EXISTING_ACTIVE_PROJECT requires lifecycle ACTIVE_DEVELOPMENT"
        )
    if detected_mode == "COMPLETED_PROJECT" and project_lifecycle not in {
        "FEATURE_COMPLETE",
        "MAINTENANCE",
        "FROZEN",
        "ARCHIVED",
    }:
        raise guard.InvalidState(
            "COMPLETED_PROJECT requires a completed or post-completion lifecycle"
        )
    if detected_mode == "SHIPPED_PROJECT" and project_lifecycle not in {
        "SHIPPED",
        "MAINTENANCE",
        "FROZEN",
        "ARCHIVED",
    }:
        raise guard.InvalidState("SHIPPED_PROJECT requires a shipped or post-shipped lifecycle")
    if detected_mode in {"COMPLETED_PROJECT", "SHIPPED_PROJECT"} and management_mode == "CONTINUE_DEVELOPMENT":
        raise guard.InvalidState(
            "Completed or shipped Adoption cannot silently resume product development"
        )
    if project_lifecycle == "FROZEN" and management_mode != "FROZEN":
        raise guard.InvalidState("FROZEN lifecycle requires FROZEN management mode")
    if project_lifecycle == "ARCHIVED" and management_mode != "ARCHIVED":
        raise guard.InvalidState("ARCHIVED lifecycle requires ARCHIVED management mode")
    if management_mode == "FROZEN" and project_lifecycle != "FROZEN":
        raise guard.InvalidState("FROZEN management mode requires FROZEN lifecycle")
    if management_mode == "ARCHIVED" and project_lifecycle != "ARCHIVED":
        raise guard.InvalidState("ARCHIVED management mode requires ARCHIVED lifecycle")


def initialize_adoption(
    project: str,
    *,
    owner: str,
    activation_token: str,
    expected_state_sha: str,
    expected_strategy_sha: str,
    detected_mode: str,
    project_lifecycle: str,
    adoption_confidence: str,
    baseline_id: str,
    baseline_sha256: str,
    direction_summary: str,
    management_mode: str,
    evidence_refs: list[str],
    adoption_review_ref: str | None = None,
) -> dict[str, Any]:
    """Bind a verified read-only Brownfield baseline before ledger creation."""

    _validate_adoption_mode_pair(detected_mode, project_lifecycle, management_mode)
    if adoption_confidence not in ADOPTION_CONFIDENCES:
        raise guard.InvalidState("Invalid Adoption confidence")
    baseline_id = _identifier(baseline_id, "Adoption baseline_id")
    baseline_sha256 = _sha_or_absent(baseline_sha256, "Adoption baseline_sha256")
    if baseline_sha256 == "ABSENT" or baseline_id != f"AB-{baseline_sha256[:16]}":
        raise guard.InvalidState("Adoption baseline ID must bind the exact baseline SHA-256")
    direction_summary = _text(direction_summary, "current selected strategy summary")
    evidence_refs = _validate_string_list(evidence_refs, "Adoption evidence_refs")
    if not evidence_refs:
        raise guard.InvalidState("Adoption requires at least one evidence reference")
    adoption_review_ref = _optional_text(
        adoption_review_ref, "Adoption review reference"
    )

    def mutate(state: dict[str, Any] | None, founder: Path):
        if state is not None:
            raise guard.Conflict("Strategy state already exists; inspect or recover it")
        ledger_presence = {name: (founder / name).exists() for name in CORE_LEDGERS}
        if any(ledger_presence.values()):
            raise guard.Conflict(
                "Brownfield Adoption initialization requires zero canonical ledgers"
            )
        allowed_control_names = {
            guard.STATE_NAME,
            guard.LOCK_NAME,
            STRATEGY_LOCK_NAME,
        }
        unexpected = sorted(
            entry.name for entry in founder.iterdir() if entry.name not in allowed_control_names
        )
        if unexpected:
            raise guard.Conflict(
                "NAMESPACE_COLLISION: pre-adoption .founder contains unrecognized content: "
                + ", ".join(unexpected)
            )
        root = founder.parent
        observed_id, observed_sha = _observe_adoption_baseline(root)
        if (observed_id, observed_sha) != (baseline_id, baseline_sha256):
            raise guard.Conflict(
                "ADOPTION_BASELINE_DRIFT: project evidence changed after the read-only review"
            )
        now = guard.utc_now()
        value: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "strategy_revision": _new_revision("ST"),
            "previous_strategy_sha256": "ABSENT",
            "context_revision": "",
            "context_sha256": "",
            "created_at": now,
            "updated_at": now,
            "project_phase": "pre-adoption",
            "project_origin": "ADOPTED",
            "project_lifecycle": project_lifecycle,
            "adoption_status": "BASELINE_READY",
            "adoption_confidence": adoption_confidence,
            "adoption": {
                "detected_mode": detected_mode,
                "management_mode": management_mode,
                "baseline_id": baseline_id,
                "baseline_sha256": baseline_sha256,
                "behavior_preservation": True,
                "evidence_refs": evidence_refs,
                "adoption_review_ref": adoption_review_ref,
                "adopted_at": None,
            },
            "project_binding": {
                "project_root": str(root),
                "project_binding_id": _project_binding_id(root),
            },
            "autonomy_profile": _default_profile("adoption-default", evidence_refs[0]),
            "direction": {
                "clarity": "CLEAR",
                "clarity_reason": "Current direction is reconstructed from existing project evidence",
                "strategy_status": "SELECTED",
                "selected_strategy_id": "current-selected-strategy",
                "selected_strategy_summary": direction_summary,
                "selection_authority": "adoption-reconstructed",
                "selection_rationale": (
                    "Current direction is preserved for Adoption; original historical rationale "
                    "is not asserted"
                ),
            },
            "discovery": {
                "depth": "NONE",
                "candidates": [],
                "recommendation_id": None,
                "recommendation": None,
                "evidence": evidence_refs,
                "single_candidate_reason": None,
            },
            "gate": {
                "state": "ADOPTION_STATE_REQUIRED",
                "context": "adoption",
                "decision_level": None,
                "proposal_id": None,
                "reason": "Verified baseline is ready; canonical current-state ledgers are required",
                "authorization_ref": None,
                "action_scope": None,
                "opened_at": now,
                "resolved_at": None,
            },
            "decision_record": {
                "status": "not-required",
                "decision_id": None,
                "level": None,
                "proposal_id": None,
                "selected_strategy_id": None,
                "selection_authority": None,
                "authorization_ref": None,
                "canonical_evidence": None,
                "action_scope": None,
            },
            "discovery_assignments": [],
            "pending_state_sync": [],
            "reporting": {"pending_decision_ids": [], "reported": []},
            "discovery_history": [],
            "proposal_ids": [],
            "authorization_receipts": [],
        }
        _refresh_context(value, rotate=True)
        return value, {
            "mode": "adoption",
            "gate": value["gate"]["state"],
            "baseline_id": baseline_id,
            "baseline_sha256": baseline_sha256,
        }

    return _mutate_strategy(
        project,
        owner=owner,
        activation_token=activation_token,
        expected_state_sha=expected_state_sha,
        expected_strategy_sha=expected_strategy_sha,
        operation="ADOPTION_STRATEGY_INITIALIZED",
        mutate=mutate,
    )


def assess_direction(
    project: str,
    *,
    owner: str,
    activation_token: str,
    expected_state_sha: str,
    expected_strategy_sha: str,
    outcome: str,
    reason: str,
    direction_summary: str,
    depth: str,
) -> dict[str, Any]:
    if outcome not in {"CLEAR", "AMBIGUOUS"}:
        raise guard.InvalidState("Direction outcome must be CLEAR or AMBIGUOUS")
    reason = _text(reason, "Direction Clarity reason")
    direction_summary = _text(direction_summary, "Founder idea/direction summary")
    if depth not in DISCOVERY_DEPTHS:
        raise guard.InvalidState("Unknown Discovery depth")
    if outcome == "CLEAR" and depth != "NONE":
        raise guard.InvalidState("A clear direction does not enter Discovery")
    if outcome == "AMBIGUOUS" and depth == "NONE":
        raise guard.InvalidState("An ambiguous direction requires adaptive Discovery depth")

    def mutate(state: dict[str, Any] | None, founder: Path):
        if state is None:
            raise guard.Conflict("Strategy state is not initialized")
        if state["project_phase"] != "pre-bootstrap" or state["gate"]["state"] != "DIRECTION_CHECK_REQUIRED":
            raise guard.Conflict("Direction Clarity can only resolve the current pre-bootstrap check")
        now = guard.utc_now()
        state["direction"]["clarity"] = outcome
        state["direction"]["clarity_reason"] = reason
        state["discovery"]["depth"] = depth
        state["discovery"]["evidence"].append(reason)
        if outcome == "CLEAR":
            state["direction"].update(
                {
                    "strategy_status": "SELECTED",
                    "selected_strategy_id": "founder-provided-direction",
                    "selected_strategy_summary": direction_summary,
                    "selection_authority": "founder-input",
                    "selection_rationale": reason,
                }
            )
            state["gate"].update(
                {
                    "state": "BOOTSTRAP_AUTHORIZED",
                    "context": "bootstrap",
                    "decision_level": None,
                    "proposal_id": None,
                    "reason": reason,
                    "authorization_ref": "Founder provided a sufficiently clear direction",
                    "resolved_at": now,
                }
            )
            _refresh_context(state, rotate=True)
        else:
            state["direction"].update(
                {
                    "strategy_status": "EXPLORATORY",
                    "selected_strategy_id": None,
                    "selected_strategy_summary": direction_summary,
                    "selection_authority": None,
                    "selection_rationale": None,
                }
            )
            state["gate"].update(
                {
                    "state": "DISCOVERY_ACTIVE",
                    "context": "bootstrap",
                    "decision_level": "L2",
                    "proposal_id": None,
                    "reason": reason,
                    "authorization_ref": None,
                    "opened_at": now,
                    "resolved_at": None,
                }
            )
        return state, {"clarity": outcome, "gate": state["gate"]["state"], "depth": depth}

    return _mutate_strategy(
        project,
        owner=owner,
        activation_token=activation_token,
        expected_state_sha=expected_state_sha,
        expected_strategy_sha=expected_strategy_sha,
        operation="DIRECTION_CLARITY_RECORDED",
        mutate=mutate,
    )


def _known_proposal_ids(state: dict[str, Any]) -> set[str]:
    result = set(state.get("proposal_ids", []))
    result.update(row["proposal_id"] for row in state["discovery_history"])
    current = state["gate"].get("proposal_id")
    if current is not None:
        result.add(current)
    return result


def _archive_current_discovery(
    state: dict[str, Any], *, disposition: str, reason: str
) -> None:
    proposal_id = state["gate"].get("proposal_id")
    discovery = state["discovery"]
    if (
        proposal_id is None
        or not discovery["candidates"]
        or state["gate"]["context"] not in {"bootstrap", "pivot"}
    ):
        return
    if any(row["proposal_id"] == proposal_id for row in state["discovery_history"]):
        return
    state["discovery_history"].append(
        {
            "proposal_id": proposal_id,
            "context": state["gate"]["context"],
            "disposition": disposition,
            "candidates": copy.deepcopy(discovery["candidates"]),
            "recommendation_id": discovery["recommendation_id"],
            "recommendation": copy.deepcopy(discovery["recommendation"]),
            "closed_at": guard.utc_now(),
            "reason": reason,
        }
    )


def record_candidates(
    project: str,
    *,
    owner: str,
    activation_token: str,
    expected_state_sha: str,
    expected_strategy_sha: str,
    proposal_id: str,
    candidates: list[dict[str, Any]],
    recommendation_id: str,
    recommendation: dict[str, Any],
    evidence: list[str],
    single_candidate_reason: str | None,
) -> dict[str, Any]:
    proposal_id = _identifier(proposal_id, "proposal_id")
    candidates = _normalize_candidates(candidates)
    recommendation_id = _identifier(recommendation_id, "recommendation_id")
    if recommendation_id not in {item["candidate_id"] for item in candidates}:
        raise guard.InvalidState("Recommendation must reference a current candidate")
    recommendation = _normalize_recommendation(
        recommendation,
        candidate_ids={item["candidate_id"] for item in candidates},
        recommendation_id=recommendation_id,
    )
    evidence = [_text(item, "Discovery evidence item", max_length=512) for item in evidence]
    if not evidence:
        raise guard.InvalidState("Opening a Strategic Gate requires Discovery evidence")
    single_candidate_reason = _optional_text(single_candidate_reason, "single candidate reason")
    if len(candidates) == 1 and single_candidate_reason is None:
        raise guard.InvalidState("A single candidate requires evidence that no peer alternative exists")
    for item in candidates:
        if item["candidate_id"] == recommendation_id:
            item["status"] = "RECOMMENDED"

    def mutate(state: dict[str, Any] | None, founder: Path):
        if state is None:
            raise guard.Conflict("Strategy state is not initialized")
        if state["gate"]["state"] != "DISCOVERY_ACTIVE":
            raise guard.Conflict("Strategic candidates require active limited Discovery")
        if proposal_id in _known_proposal_ids(state):
            raise guard.Conflict("Proposal ID has already been used")
        state.setdefault("proposal_ids", []).append(proposal_id)
        unresolved_assignments = [
            row["assignment_id"]
            for row in state["discovery_assignments"]
            if row["status"] in {"dispatched", "returned"}
        ]
        if unresolved_assignments:
            raise guard.Conflict(
                "FounderOS must receive and accept/fail every registered Discovery assignment before opening the Gate: "
                + ", ".join(unresolved_assignments)
            )
        state["discovery"].update(
            {
                "candidates": copy.deepcopy(candidates),
                "recommendation_id": recommendation_id,
                "recommendation": recommendation,
                "evidence": evidence,
                "single_candidate_reason": single_candidate_reason,
            }
        )
        if state["project_phase"] == "pre-bootstrap":
            state["direction"]["strategy_status"] = "RECOMMENDED"
        discovery_context = state["gate"]["context"]
        state["gate"].update(
            {
                "state": "STRATEGIC_CHOICE_REQUIRED",
                "context": discovery_context,
                "decision_level": "L2",
                "proposal_id": proposal_id,
                "reason": "Discovery produced comparable candidates and a recommendation",
                "authorization_ref": None,
                "opened_at": guard.utc_now(),
                "resolved_at": None,
            }
        )
        return state, {
            "proposal_id": proposal_id,
            "candidate_count": len(candidates),
            "recommendation_id": recommendation_id,
            "gate": "STRATEGIC_CHOICE_REQUIRED",
        }

    return _mutate_strategy(
        project,
        owner=owner,
        activation_token=activation_token,
        expected_state_sha=expected_state_sha,
        expected_strategy_sha=expected_strategy_sha,
        operation="STRATEGIC_CANDIDATES_RECORDED",
        mutate=mutate,
    )


def revise_discovery(
    project: str,
    *,
    owner: str,
    activation_token: str,
    expected_state_sha: str,
    expected_strategy_sha: str,
    proposal_id: str,
    reason: str,
    depth: str,
) -> dict[str, Any]:
    """Return a proposal-bound Choice Gate to limited Discovery."""
    proposal_id = _identifier(proposal_id, "proposal_id")
    reason = _text(reason, "Discovery revision reason")
    if depth not in DISCOVERY_DEPTHS - {"NONE"}:
        raise guard.InvalidState("Revised Discovery requires LIGHT, STANDARD, or DEEP depth")

    def mutate(state: dict[str, Any] | None, founder: Path):
        if state is None:
            raise guard.Conflict("Strategy state is not initialized")
        gate = state["gate"]
        if gate["state"] != "STRATEGIC_CHOICE_REQUIRED" or gate.get("proposal_id") != proposal_id:
            raise guard.Conflict("Discovery revision does not match the current Strategic Gate")
        context = gate["context"]
        _archive_current_discovery(state, disposition="revised", reason=reason)
        state["discovery"].update(
            {
                "depth": depth,
                "candidates": [],
                "recommendation_id": None,
                "recommendation": None,
                "evidence": [reason],
                "single_candidate_reason": None,
            }
        )
        if state["project_phase"] == "pre-bootstrap":
            state["direction"].update(
                {
                    "strategy_status": "EXPLORATORY",
                    "selected_strategy_id": None,
                    "selection_authority": None,
                    "selection_rationale": None,
                }
            )
        state["gate"] = {
            "state": "DISCOVERY_ACTIVE",
            "context": context,
            "decision_level": "L2",
            "proposal_id": None,
            "reason": reason,
            "authorization_ref": None,
            "opened_at": guard.utc_now(),
            "resolved_at": None,
        }
        return state, {
            "revised_proposal_id": proposal_id,
            "gate": "DISCOVERY_ACTIVE",
            "depth": depth,
            "context": context,
        }

    return _mutate_strategy(
        project,
        owner=owner,
        activation_token=activation_token,
        expected_state_sha=expected_state_sha,
        expected_strategy_sha=expected_strategy_sha,
        operation="STRATEGIC_DISCOVERY_REOPENED",
        mutate=mutate,
    )


def record_discovery_assignment(
    project: str,
    *,
    owner: str,
    activation_token: str,
    expected_state_sha: str,
    expected_strategy_sha: str,
    assignment_id: str,
    runtime_agent_id: str,
    role: str,
    task: str,
    read_scope: list[str],
) -> dict[str, Any]:
    assignment_id = _identifier(assignment_id, "assignment_id")
    runtime_agent_id = _identifier(runtime_agent_id, "runtime_agent_id", max_length=256)
    role = _text(role, "Discovery Agent role", max_length=128)
    task = _text(task, "Discovery Agent task")
    read_scope = [_text(item, "Discovery read scope", max_length=512) for item in read_scope]

    def mutate(state: dict[str, Any] | None, founder: Path):
        if state is None:
            raise guard.Conflict("Strategy state is not initialized")
        if state["project_phase"] != "pre-bootstrap" or state["gate"]["state"] not in {
            "DISCOVERY_ACTIVE", "STRATEGIC_CHOICE_REQUIRED"
        }:
            raise guard.Conflict("Discovery Agent registration is only valid during pre-bootstrap Discovery")
        if any(
            row["assignment_id"] == assignment_id or row["runtime_agent_id"] == runtime_agent_id
            for row in state["discovery_assignments"]
        ):
            raise guard.Conflict("Discovery assignment/runtime Agent is already registered")
        state["discovery_assignments"].append(
            {
                "assignment_id": assignment_id,
                "runtime_agent_id": runtime_agent_id,
                "role": role,
                "task": task,
                "read_scope": read_scope,
                "write_scope": [],
                "status": "dispatched",
                "evidence": None,
            }
        )
        return state, {"assignment_id": assignment_id, "runtime_agent_id": runtime_agent_id, "write_scope": []}

    return _mutate_strategy(
        project,
        owner=owner,
        activation_token=activation_token,
        expected_state_sha=expected_state_sha,
        expected_strategy_sha=expected_strategy_sha,
        operation="DISCOVERY_AGENT_RECORDED",
        mutate=mutate,
    )


def update_discovery_assignment(
    project: str,
    *,
    owner: str,
    activation_token: str,
    expected_state_sha: str,
    expected_strategy_sha: str,
    assignment_id: str,
    status: str,
    evidence: str,
) -> dict[str, Any]:
    assignment_id = _identifier(assignment_id, "assignment_id")
    if status not in {"returned", "accepted", "failed"}:
        raise guard.InvalidState("Discovery Agent terminal status must be returned/accepted/failed")
    evidence = _text(evidence, "Discovery Agent evidence")

    def mutate(state: dict[str, Any] | None, founder: Path):
        if state is None:
            raise guard.Conflict("Strategy state is not initialized")
        for row in state["discovery_assignments"]:
            if row["assignment_id"] == assignment_id:
                prior = row["status"]
                legal = (
                    (status == "returned" and prior == "dispatched")
                    or (status == "accepted" and prior == "returned")
                    or (status == "failed" and prior in {"dispatched", "returned"})
                )
                if not legal:
                    raise guard.Conflict(f"Illegal Discovery assignment transition: {prior} -> {status}")
                row["status"] = status
                row["evidence"] = evidence
                return state, {"assignment_id": assignment_id, "status": status}
        raise guard.Conflict(f"Unknown Discovery assignment: {assignment_id}")

    return _mutate_strategy(
        project,
        owner=owner,
        activation_token=activation_token,
        expected_state_sha=expected_state_sha,
        expected_strategy_sha=expected_strategy_sha,
        operation="DISCOVERY_AGENT_UPDATED",
        mutate=mutate,
    )


def select_candidate(
    project: str,
    *,
    owner: str,
    activation_token: str,
    expected_state_sha: str,
    expected_strategy_sha: str,
    proposal_id: str,
    candidate_id: str,
    authority: str,
    authorization_ref: str,
    decision_id: str,
    rationale: str,
    nonselected_status: str,
) -> dict[str, Any]:
    proposal_id = _identifier(proposal_id, "proposal_id")
    candidate_id = _identifier(candidate_id, "candidate_id")
    if authority not in {"founder", "delegated", "autonomy"}:
        raise guard.InvalidState("Selection authority must be founder/delegated/autonomy")
    authorization_ref = _text(authorization_ref, "current Gate selection authorization")
    decision_id = _identifier(decision_id, "decision_id")
    rationale = _text(rationale, "selection rationale")
    if nonselected_status not in {"REJECTED", "DEFERRED"}:
        raise guard.InvalidState("Non-selected candidates must be REJECTED or DEFERRED")

    def mutate(state: dict[str, Any] | None, founder: Path):
        if state is None:
            raise guard.Conflict("Strategy state is not initialized")
        gate = state["gate"]
        if gate["state"] != "STRATEGIC_CHOICE_REQUIRED" or gate.get("proposal_id") != proposal_id:
            raise guard.Conflict("Selection does not match the current Strategic Gate")
        if _decision_id_exists(founder, decision_id):
            raise guard.Conflict("Decision ID already exists in DECISIONS.md; old decisions cannot be replayed")
        candidate = next(
            (item for item in state["discovery"]["candidates"] if item["candidate_id"] == candidate_id),
            None,
        )
        if candidate is None or candidate["status"] in {"REJECTED", "DEFERRED"}:
            raise guard.Conflict("Candidate is not selectable in the current Gate")
        if authority == "delegated" and candidate_id != state["discovery"]["recommendation_id"]:
            raise guard.Conflict("A generic delegated choice selects the current recommendation")
        if authority == "autonomy" and state["autonomy_profile"]["strategic"] != "autonomous_with_report":
            raise guard.Conflict("Current Autonomy Profile does not allow autonomous L2 selection")
        unresolved_assignments = [
            row["assignment_id"]
            for row in state["discovery_assignments"]
            if row["status"] in {"dispatched", "returned"}
        ]
        if unresolved_assignments:
            raise guard.Conflict(
                "Discovery assignments must be accepted or failed before selection: "
                + ", ".join(unresolved_assignments)
            )
        authorization_receipt_id = None
        if authority in {"founder", "delegated"}:
            authorization_receipt_id = _record_authorization_receipt(
                state,
                authorization_ref=authorization_ref,
                kind="strategic-selection",
                proposal_id=proposal_id,
                subject=candidate_id,
            )
        for item in state["discovery"]["candidates"]:
            item["status"] = "SELECTED" if item["candidate_id"] == candidate_id else nonselected_status
        state["direction"].update(
            {
                "strategy_status": "SELECTED",
                "selected_strategy_id": candidate_id,
                "selected_strategy_summary": candidate["summary"],
                "selection_authority": authority,
                "selection_rationale": rationale,
            }
        )
        state["decision_record"] = {
            "status": "pending",
            "decision_id": decision_id,
            "level": "L2",
            "proposal_id": proposal_id,
            "selected_strategy_id": candidate_id,
            "selection_authority": authority,
            "authorization_ref": authorization_ref,
            "authorization_receipt_id": authorization_receipt_id,
            "canonical_evidence": None,
            "action_scope": None,
        }
        gate["authorization_ref"] = authorization_ref
        gate["resolved_at"] = guard.utc_now()
        if gate["context"] == "bootstrap":
            gate["state"] = "BOOTSTRAP_AUTHORIZED"
        else:
            gate["state"] = "DECISION_RECORD_REQUIRED"
        if authority == "autonomy" and decision_id not in state["reporting"]["pending_decision_ids"]:
            state["reporting"]["pending_decision_ids"].append(decision_id)
        _refresh_context(state, rotate=True)
        return state, {
            "proposal_id": proposal_id,
            "selected_candidate_id": candidate_id,
            "authority": authority,
            "decision_id": decision_id,
            "gate": gate["state"],
            "profile_changed": False,
        }

    return _mutate_strategy(
        project,
        owner=owner,
        activation_token=activation_token,
        expected_state_sha=expected_state_sha,
        expected_strategy_sha=expected_strategy_sha,
        operation="STRATEGIC_SELECTION_RECORDED",
        mutate=mutate,
    )


def _current_persistent_agent_ids(founder: Path) -> list[str]:
    """Return Agents that currently own a primary persistent Thread."""

    import thread_registry as registry_api

    _registry_sha, _raw, registry = registry_api._read_registry(founder / THREADS_NAME)
    if registry is None:
        return []
    registry_api.validate_registry(registry, founder.parent)
    result: list[str] = []
    for agent_id, binding in registry["agent_bindings"].items():
        if (
            binding.get("agent_kind") == "persistent"
            and isinstance(binding.get("primary_thread_record_id"), str)
        ):
            thread = registry_api._find_thread(
                registry, binding["primary_thread_record_id"]
            )
            if thread.get("binding_role") == "primary":
                result.append(_agent_id(agent_id))
    return sorted(set(result))


def update_autonomy(
    project: str,
    *,
    owner: str,
    activation_token: str,
    expected_state_sha: str,
    expected_strategy_sha: str,
    strategic: str,
    authorization_ref: str,
) -> dict[str, Any]:
    if strategic not in STRATEGIC_AUTONOMY:
        raise guard.InvalidState("Unknown strategic autonomy value")
    authorization_ref = _text(authorization_ref, "Founder Autonomy Profile authorization")

    def mutate(state: dict[str, Any] | None, founder: Path):
        if state is None:
            raise guard.Conflict("Strategy state is not initialized")
        if state["gate"]["state"] not in {"OPERATING", "STRATEGIC_CHOICE_REQUIRED"}:
            raise guard.Conflict(
                "Autonomy Profile can change only while operating or at the current Strategic Choice Gate"
            )
        prior = state["autonomy_profile"]["strategic"]
        authorization_receipt_id = _record_authorization_receipt(
            state,
            authorization_ref=authorization_ref,
            kind="autonomy-profile",
            proposal_id=state["gate"].get("proposal_id"),
            subject=strategic,
        )
        state["autonomy_profile"].update(
            {"strategic": strategic, "source": "founder-explicit", "evidence": authorization_ref}
        )
        affected_agent_ids: list[str] = []
        if prior != strategic:
            _refresh_context(state, rotate=True)
            if state["project_phase"] == "bootstrapped":
                affected_agent_ids = _current_persistent_agent_ids(founder)
                existing = {row["agent_id"]: row for row in state["pending_state_sync"]}
                for agent_id in affected_agent_ids:
                    existing[agent_id] = {
                        "agent_id": agent_id,
                        "status": "pending",
                        "disposition": "sync-required",
                        "thread_record_id": None,
                        "evidence": None,
                    }
                state["pending_state_sync"] = list(existing.values())
                if affected_agent_ids and state["gate"]["state"] == "OPERATING":
                    state["gate"].update(
                        {
                            "state": "STATE_SYNC_REQUIRED",
                            "context": "autonomy",
                            "decision_level": None,
                            "reason": "Autonomy Profile changed; current persistent Agents require STATE_SYNC",
                            "authorization_ref": authorization_ref,
                            "opened_at": guard.utc_now(),
                            "resolved_at": None,
                        }
                    )
        return state, {
            "prior": prior,
            "strategic": strategic,
            "executive": "require_explicit_approval",
            "authorization_receipt_id": authorization_receipt_id,
            "pending_state_sync": affected_agent_ids,
            "gate": state["gate"]["state"],
        }

    return _mutate_strategy(
        project,
        owner=owner,
        activation_token=activation_token,
        expected_state_sha=expected_state_sha,
        expected_strategy_sha=expected_strategy_sha,
        operation="AUTONOMY_PROFILE_UPDATED",
        mutate=mutate,
    )


def open_pivot(
    project: str,
    *,
    owner: str,
    activation_token: str,
    expected_state_sha: str,
    expected_strategy_sha: str,
    proposal_id: str,
    summary: str,
    candidates: list[dict[str, Any]],
    recommendation_id: str,
    recommendation: dict[str, Any],
    evidence: list[str],
    affected_agent_ids: list[str],
    single_candidate_reason: str | None,
) -> dict[str, Any]:
    proposal_id = _identifier(proposal_id, "proposal_id")
    summary = _text(summary, "Strategic Proposal summary")
    candidates = _normalize_candidates(candidates)
    recommendation_id = _identifier(recommendation_id, "recommendation_id")
    if recommendation_id not in {item["candidate_id"] for item in candidates}:
        raise guard.InvalidState("Pivot recommendation must reference a candidate")
    recommendation = _normalize_recommendation(
        recommendation,
        candidate_ids={item["candidate_id"] for item in candidates},
        recommendation_id=recommendation_id,
    )
    evidence = [_text(item, "Strategic Proposal evidence", max_length=512) for item in evidence]
    if not evidence:
        raise guard.InvalidState("Strategic Proposal requires evidence")
    affected_agent_ids = [_agent_id(item) for item in affected_agent_ids]
    if len(affected_agent_ids) != len(set(affected_agent_ids)):
        raise guard.InvalidState("Affected Agent IDs must be unique")
    single_candidate_reason = _optional_text(single_candidate_reason, "single candidate reason")
    if len(candidates) == 1 and single_candidate_reason is None:
        raise guard.InvalidState("A single pivot candidate requires peer-alternative evidence")
    for item in candidates:
        if item["candidate_id"] == recommendation_id:
            item["status"] = "RECOMMENDED"

    def mutate(state: dict[str, Any] | None, founder: Path):
        if state is None:
            raise guard.Conflict("Strategy state is not initialized")
        if state["project_phase"] != "bootstrapped" or state["gate"]["state"] != "OPERATING":
            raise guard.Conflict("A Strategic Proposal requires an operating project")
        _archive_current_discovery(
            state,
            disposition="selected",
            reason="Prior strategic selection was superseded by a new proposal",
        )
        if proposal_id in _known_proposal_ids(state):
            raise guard.Conflict("Proposal ID has already been used")
        state.setdefault("proposal_ids", []).append(proposal_id)
        state["discovery"].update(
            {
                "depth": "LIGHT",
                "candidates": copy.deepcopy(candidates),
                "recommendation_id": recommendation_id,
                "recommendation": recommendation,
                "evidence": [summary, *evidence],
                "single_candidate_reason": single_candidate_reason,
            }
        )
        state["gate"] = {
            "state": "STRATEGIC_CHOICE_REQUIRED",
            "context": "pivot",
            "decision_level": "L2",
            "proposal_id": proposal_id,
            "reason": summary,
            "authorization_ref": None,
            "opened_at": guard.utc_now(),
            "resolved_at": None,
        }
        state["decision_record"] = {
            "status": "not-required",
            "decision_id": None,
            "level": None,
            "proposal_id": None,
            "selected_strategy_id": None,
            "selection_authority": None,
            "authorization_ref": None,
            "canonical_evidence": None,
            "action_scope": None,
        }
        state["pending_state_sync"] = [
            {
                "agent_id": agent_id,
                "status": "pending",
                "disposition": "sync-required",
                "thread_record_id": None,
                "evidence": None,
            }
            for agent_id in affected_agent_ids
        ]
        return state, {
            "proposal_id": proposal_id,
            "level": "L2",
            "gate": "STRATEGIC_CHOICE_REQUIRED",
            "affected_agents": affected_agent_ids,
        }

    return _mutate_strategy(
        project,
        owner=owner,
        activation_token=activation_token,
        expected_state_sha=expected_state_sha,
        expected_strategy_sha=expected_strategy_sha,
        operation="STRATEGIC_PROPOSAL_OPENED",
        mutate=mutate,
    )


def open_executive_gate(
    project: str,
    *,
    owner: str,
    activation_token: str,
    expected_state_sha: str,
    expected_strategy_sha: str,
    proposal_id: str,
    summary: str,
    action_scope: str,
) -> dict[str, Any]:
    proposal_id = _identifier(proposal_id, "proposal_id")
    summary = _text(summary, "Executive approval request")
    action_scope = _text(action_scope, "Executive action_scope", max_length=512)

    def mutate(state: dict[str, Any] | None, founder: Path):
        if state is None or state["project_phase"] != "bootstrapped":
            raise guard.Conflict("Executive approval requires a bootstrapped project")
        if state["gate"]["state"] != "OPERATING":
            raise guard.Conflict("Resolve the current Gate before opening another")
        if proposal_id in _known_proposal_ids(state):
            raise guard.Conflict("Proposal ID has already been used")
        state.setdefault("proposal_ids", []).append(proposal_id)
        state["gate"] = {
            "state": "EXECUTIVE_APPROVAL_REQUIRED",
            "context": "executive",
            "decision_level": "L3",
            "proposal_id": proposal_id,
            "reason": summary,
            "authorization_ref": None,
            "action_scope": action_scope,
            "opened_at": guard.utc_now(),
            "resolved_at": None,
        }
        state["decision_record"] = {
            "status": "not-required",
            "decision_id": None,
            "level": None,
            "proposal_id": None,
            "selected_strategy_id": None,
            "selection_authority": None,
            "authorization_ref": None,
            "canonical_evidence": None,
            "action_scope": None,
        }
        return state, {"proposal_id": proposal_id, "level": "L3", "gate": "EXECUTIVE_APPROVAL_REQUIRED"}

    return _mutate_strategy(
        project,
        owner=owner,
        activation_token=activation_token,
        expected_state_sha=expected_state_sha,
        expected_strategy_sha=expected_strategy_sha,
        operation="EXECUTIVE_GATE_OPENED",
        mutate=mutate,
    )


def approve_executive(
    project: str,
    *,
    owner: str,
    activation_token: str,
    expected_state_sha: str,
    expected_strategy_sha: str,
    proposal_id: str,
    authorization_ref: str,
    decision_id: str,
) -> dict[str, Any]:
    proposal_id = _identifier(proposal_id, "proposal_id")
    authorization_ref = _text(authorization_ref, "explicit Founder L3 approval")
    decision_id = _identifier(decision_id, "decision_id")

    def mutate(state: dict[str, Any] | None, founder: Path):
        if state is None:
            raise guard.Conflict("Strategy state is not initialized")
        gate = state["gate"]
        if gate["state"] != "EXECUTIVE_APPROVAL_REQUIRED" or gate.get("proposal_id") != proposal_id:
            raise guard.Conflict("Approval does not match the current Executive Gate")
        if _decision_id_exists(founder, decision_id):
            raise guard.Conflict("Decision ID already exists in DECISIONS.md; old approval cannot be replayed")
        authorization_receipt_id = _record_authorization_receipt(
            state,
            authorization_ref=authorization_ref,
            kind="executive-approval",
            proposal_id=proposal_id,
            subject=gate["action_scope"],
        )
        state["decision_record"] = {
            "status": "pending",
            "decision_id": decision_id,
            "level": "L3",
            "proposal_id": proposal_id,
            "selected_strategy_id": None,
            "selection_authority": "founder",
            "authorization_ref": authorization_ref,
            "authorization_receipt_id": authorization_receipt_id,
            "canonical_evidence": None,
            "action_scope": gate["action_scope"],
            "action_status": "approved",
            "execution_ref": None,
            "consumed_at": None,
        }
        gate["state"] = "DECISION_RECORD_REQUIRED"
        gate["authorization_ref"] = authorization_ref
        gate["resolved_at"] = guard.utc_now()
        return state, {"proposal_id": proposal_id, "decision_id": decision_id, "gate": "DECISION_RECORD_REQUIRED"}

    return _mutate_strategy(
        project,
        owner=owner,
        activation_token=activation_token,
        expected_state_sha=expected_state_sha,
        expected_strategy_sha=expected_strategy_sha,
        operation="EXECUTIVE_APPROVAL_RECORDED",
        mutate=mutate,
    )


def consume_executive(
    project: str,
    *,
    owner: str,
    activation_token: str,
    expected_state_sha: str,
    expected_strategy_sha: str,
    proposal_id: str,
    decision_id: str,
    action_scope: str,
    execution_ref: str,
) -> dict[str, Any]:
    """Consume one canonical L3 authorization exactly once."""

    proposal_id = _identifier(proposal_id, "proposal_id")
    decision_id = _identifier(decision_id, "decision_id")
    action_scope = _text(action_scope, "executive action_scope")
    execution_ref = _text(execution_ref, "executive execution_ref")

    def mutate(state: dict[str, Any] | None, _founder: Path):
        if state is None:
            raise guard.Conflict("Strategy state is not initialized")
        decision = state["decision_record"]
        gate = state["gate"]
        if (
            gate["state"] != "OPERATING"
            or decision.get("status") != "confirmed"
            or decision.get("level") != "L3"
            or decision.get("selection_authority") != "founder"
            or decision.get("proposal_id") != proposal_id
            or decision.get("decision_id") != decision_id
            or decision.get("action_scope") != action_scope
            or gate.get("proposal_id") != proposal_id
            or gate.get("action_scope") != action_scope
        ):
            raise guard.Conflict(
                "Consumption requires the exact current canonical Founder-approved L3 action"
            )
        if decision.get("action_status") != "approved":
            raise guard.Conflict("L3 authorization is not available or has already been consumed")
        decision.update(
            {
                "action_status": "consumed",
                "execution_ref": execution_ref,
                "consumed_at": guard.utc_now(),
            }
        )
        return state, {
            "proposal_id": proposal_id,
            "decision_id": decision_id,
            "action_scope": action_scope,
            "execution_ref": execution_ref,
            "action_status": "consumed",
        }

    return _mutate_strategy(
        project,
        owner=owner,
        activation_token=activation_token,
        expected_state_sha=expected_state_sha,
        expected_strategy_sha=expected_strategy_sha,
        operation="EXECUTIVE_AUTHORIZATION_CONSUMED",
        mutate=mutate,
    )


def reject_executive(
    project: str,
    *,
    owner: str,
    activation_token: str,
    expected_state_sha: str,
    expected_strategy_sha: str,
    proposal_id: str,
    authorization_ref: str,
) -> dict[str, Any]:
    proposal_id = _identifier(proposal_id, "proposal_id")
    authorization_ref = _text(authorization_ref, "explicit Founder L3 rejection")

    def mutate(state: dict[str, Any] | None, founder: Path):
        if state is None:
            raise guard.Conflict("Strategy state is not initialized")
        gate = state["gate"]
        if gate["state"] != "EXECUTIVE_APPROVAL_REQUIRED" or gate.get("proposal_id") != proposal_id:
            raise guard.Conflict("Rejection does not match the current Executive Gate")
        authorization_receipt_id = _record_authorization_receipt(
            state,
            authorization_ref=authorization_ref,
            kind="executive-rejection",
            proposal_id=proposal_id,
            subject=gate["action_scope"],
        )
        gate.update(
            {
                "state": "OPERATING",
                "context": "none",
                "decision_level": None,
                "authorization_ref": authorization_ref,
                "reason": "Founder rejected the L3 proposal",
                "resolved_at": guard.utc_now(),
            }
        )
        return state, {
            "proposal_id": proposal_id,
            "gate": "OPERATING",
            "approved": False,
            "authorization_receipt_id": authorization_receipt_id,
        }

    return _mutate_strategy(
        project,
        owner=owner,
        activation_token=activation_token,
        expected_state_sha=expected_state_sha,
        expected_strategy_sha=expected_strategy_sha,
        operation="EXECUTIVE_PROPOSAL_REJECTED",
        mutate=mutate,
    )


def _read_utf8_direct(path: Path, label: str) -> str:
    _direct_file(path, label)
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise guard.InvalidState(f"Cannot read {label}: {exc}") from exc


def _decision_id_exists(founder: Path, decision_id: str) -> bool:
    path = founder / "DECISIONS.md"
    if not path.exists():
        return False
    text_value = _read_utf8_direct(path, "DECISIONS.md")
    escaped = re.escape(decision_id)
    return bool(
        re.search(
            rf"(?im)^\s*(?:[-*]\s*)?(?:Decision\s+ID|决策\s*ID)\s*:\s*`?{escaped}`?\s*$",
            text_value,
        )
    )


def _decision_file_contains(founder: Path, decision: dict[str, Any]) -> bool:
    """Require one auditable, proposal-bound strategic Decision block."""
    text_value = _read_utf8_direct(founder / "DECISIONS.md", "DECISIONS.md")
    decision_id = decision["decision_id"]
    level = decision["level"]
    escaped = re.escape(decision_id)
    matches = list(
        re.finditer(
            rf"(?im)^\s*(?:[-*]\s*)?(?:Decision\s+ID|决策\s*ID)\s*:\s*`?{escaped}`?\s*$",
            text_value,
        )
    )
    if len(matches) != 1:
        return False
    start = matches[0].start()
    next_heading = re.search(r"(?m)^#{1,6}\s+", text_value[matches[0].end() :])
    end = matches[0].end() + next_heading.start() if next_heading else min(len(text_value), start + 4000)
    block = text_value[start:end]
    if not re.search(
        rf"(?im)^\s*(?:[-*]\s*)?(?:Proposal\s+ID|提案\s*ID)\s*:\s*`?{re.escape(decision['proposal_id'])}`?\s*$",
        block,
    ):
        return False
    if not re.search(
        rf"(?im)^\s*(?:[-*]\s*)?(?:Level|级别|影响等级)\s*:\s*`?{re.escape(level)}`?\s*$",
        block,
    ):
        return False
    if not re.search(
        r"(?im)^\s*(?:[-*]\s*)?(?:Date(?:\s*/\s*Order)?|Order|日期\s*/?\s*顺序)\s*:\s*\S.*$",
        block,
    ):
        return False
    required_narrative = (
        "Rationale",
        "Assumptions",
        "Reconsideration Trigger",
    )
    if not all(
        re.search(
            rf"(?im)^\s*(?:[-*]\s*)?{re.escape(label)}\s*:\s*\S.*$",
            block,
        )
        for label in required_narrative
    ):
        return False
    if level == "L2":
        exact_fields = {
            "Selected Strategy ID": decision["selected_strategy_id"],
            "Decision Authority": decision["selection_authority"],
        }
        if not all(
            re.search(
                rf"(?im)^\s*(?:[-*]\s*)?{re.escape(label)}\s*:\s*`?{re.escape(value)}`?\s*$",
                block,
            )
            for label, value in exact_fields.items()
        ):
            return False
        if not all(
            re.search(
                rf"(?im)^\s*(?:[-*]\s*)?{re.escape(label)}\s*:\s*\S.*$",
                block,
            )
            for label in ("Candidate Options", "FounderOS Recommendation")
        ):
            return False
    elif not re.search(
        rf"(?im)^\s*(?:[-*]\s*)?Action\s+Scope\s*:\s*`?{re.escape(decision['action_scope'])}`?\s*$",
        block,
    ):
        return False
    return True


def confirm_canonical(
    project: str,
    *,
    owner: str,
    activation_token: str,
    expected_state_sha: str,
    expected_strategy_sha: str,
    evidence: str,
) -> dict[str, Any]:
    """Confirm that selected strategy/decision is present in canonical ledgers."""
    evidence = _text(evidence, "canonical decision evidence")

    def mutate(state: dict[str, Any] | None, founder: Path):
        if state is None:
            raise guard.Conflict("Strategy state is not initialized")
        gate_state = state["gate"]["state"]
        if gate_state not in {"BOOTSTRAP_AUTHORIZED", "DECISION_RECORD_REQUIRED"}:
            raise guard.Conflict("Canonical confirmation is not required by the current Gate")
        for name in CORE_LEDGERS:
            _direct_file(founder / name, name)

        decision = state["decision_record"]
        if decision["status"] == "pending":
            if not _decision_file_contains(founder, decision):
                raise guard.Conflict(
                    "DECISIONS.md must contain one proposal-bound strategic Decision block with all required fields"
                )
            decision["status"] = "confirmed"
            decision["canonical_evidence"] = evidence
            if decision["level"] == "L2":
                _archive_current_discovery(
                    state,
                    disposition="selected",
                    reason="Proposal-bound L2 decision was confirmed in DECISIONS.md",
                )
        elif gate_state == "DECISION_RECORD_REQUIRED":
            raise guard.Conflict("The current Strategic Gate requires a pending L2/L3 decision record")

        if state["project_phase"] == "pre-bootstrap":
            agents_text = _read_utf8_direct(founder / "AGENTS.md", "AGENTS.md")
            missing_runtime_ids = [
                row["runtime_agent_id"]
                for row in state["discovery_assignments"]
                if row["runtime_agent_id"] not in agents_text
            ]
            if missing_runtime_ids:
                raise guard.Conflict(
                    "AGENTS.md has not preserved pre-bootstrap Discovery Agent history: "
                    + ", ".join(missing_runtime_ids)
                )
            state["project_phase"] = "bootstrapped"

        pending = [row for row in state["pending_state_sync"] if row["status"] != "confirmed"]
        if pending:
            state["gate"].update(
                {
                    "state": "STATE_SYNC_REQUIRED",
                    "context": "pivot",
                    "reason": "Selected strategy is canonical; affected persistent Agents require STATE_SYNC",
                }
            )
        else:
            state["gate"].update(
                {
                    "state": "OPERATING",
                    "context": "none",
                    "decision_level": None,
                    "reason": "Selected strategy and required decision record are canonical",
                }
            )
        return state, {
            "project_phase": state["project_phase"],
            "decision_id": decision.get("decision_id"),
            "decision_status": decision["status"],
            "gate": state["gate"]["state"],
            "pending_state_sync": [row["agent_id"] for row in pending],
        }

    return _mutate_strategy(
        project,
        owner=owner,
        activation_token=activation_token,
        expected_state_sha=expected_state_sha,
        expected_strategy_sha=expected_strategy_sha,
        operation="CANONICAL_STRATEGY_CONFIRMED",
        mutate=mutate,
    )


def _has_exact_ledger_field(text_value: str, label: str, value: str) -> bool:
    return bool(
        re.search(
            rf"(?im)^\s*(?:[-*]\s*)?{re.escape(label)}\s*:\s*`?{re.escape(value)}`?\s*$",
            text_value,
        )
    )


def _validate_adoption_ledgers(founder: Path, state: dict[str, Any]) -> None:
    for name in CORE_LEDGERS:
        _direct_file(founder / name, name)
    project_text = _read_utf8_direct(founder / "PROJECT.md", "PROJECT.md")
    status_text = _read_utf8_direct(founder / "STATUS.md", "STATUS.md")
    roadmap_text = _read_utf8_direct(founder / "ROADMAP.md", "ROADMAP.md")
    decisions_text = _read_utf8_direct(founder / "DECISIONS.md", "DECISIONS.md")
    agents_text = _read_utf8_direct(founder / "AGENTS.md", "AGENTS.md")
    adoption = state["adoption"]
    required_project_fields = {
        "Project Origin": "ADOPTED",
        "Project Lifecycle": state["project_lifecycle"],
        "Adoption Status": "ADOPTED",
        "Adoption Mode": adoption["detected_mode"],
        "Adoption Confidence": state["adoption_confidence"],
        "Adoption Baseline ID": adoption["baseline_id"],
        "Adoption Baseline SHA-256": adoption["baseline_sha256"],
        "Behavior Preservation": "true",
    }
    missing = [
        label
        for label, value in required_project_fields.items()
        if not _has_exact_ledger_field(project_text, label, value)
    ]
    if missing:
        raise guard.Conflict(
            "PROJECT.md is not bound to the approved Adoption baseline: "
            + ", ".join(missing)
        )
    if not re.search(
        r"(?im)^\s*(?:[-*]\s*)?Adoption Date\s*:\s*`?\d{4}-\d{2}-\d{2}`?\s*$",
        project_text,
    ):
        raise guard.Conflict("PROJECT.md must record an exact ISO Adoption Date")
    for label in (
        "Observed Purpose",
        "Current Users",
        "Current Product",
        "Known Constraints",
        "Current Maturity",
    ):
        match = re.search(
            rf"(?im)^\s*(?:[-*]\s*)?{re.escape(label)}\s*:\s*(\S.*)\s*$",
            project_text,
        )
        if match is None or match.group(1).strip(" `") in {"", "..."}:
            raise guard.Conflict(
                f"PROJECT.md must record a non-placeholder evidence-bounded {label}"
            )
    if not _has_exact_ledger_field(
        status_text, "Management Mode", adoption["management_mode"]
    ) or not _has_exact_ledger_field(
        status_text, "Adoption Baseline ID", adoption["baseline_id"]
    ):
        raise guard.Conflict(
            "STATUS.md must record the exact management mode and Adoption baseline ID"
        )
    status_enums = {
        "Build": {"PASS", "FAIL", "NOT_RUN", "UNKNOWN"},
        "Test": {"PASS", "FAIL", "NOT_RUN", "UNKNOWN"},
        "Release": {"SHIPPED", "NOT_SHIPPED", "UNKNOWN"},
    }
    for label, values in status_enums.items():
        if not any(_has_exact_ledger_field(status_text, label, value) for value in values):
            raise guard.Conflict(
                f"STATUS.md must record an exact {label} baseline state"
            )
    for label in (
        "Maturity",
        "Known Risks",
        "Current Issues",
        "Current Active Work",
        "Next Action",
    ):
        match = re.search(
            rf"(?im)^\s*(?:[-*]\s*)?{re.escape(label)}\s*:\s*(\S.*)\s*$",
            status_text,
        )
        if match is None or match.group(1).strip(" `") in {"", "..."}:
            raise guard.Conflict(f"STATUS.md must record a non-placeholder {label}")
    roadmap_headings = ("Completed / Observed", "Current", "Candidate Next Steps")
    if not all(
        re.search(rf"(?im)^#+\s+{re.escape(heading)}\s*$", roadmap_text)
        for heading in roadmap_headings
    ):
        raise guard.Conflict(
            "ROADMAP.md must separate Completed / Observed, Current, and Candidate Next Steps"
        )
    if not re.search(r"(?im)^\s*(?:[-*]\s*)?Historical Agents\s*:\s*\S.*$", agents_text):
        raise guard.Conflict("AGENTS.md must state the evidence-bounded Historical Agents status")
    recovery_rows = re.findall(
        r"(?im)^\s*(?:[-*]\s*)?Recovery Classification\s*:\s*`?"
        r"(NONE_CONFIRMED|RECOVERED_CONFIRMED|RECOVERED_INFERRED)`?\s*$",
        decisions_text,
    )
    if not recovery_rows:
        raise guard.Conflict(
            "DECISIONS.md must explicitly record recovered history or NONE_CONFIRMED"
        )
    if any(value != "NONE_CONFIRMED" for value in recovery_rows):
        rationale = re.search(
            r"(?im)^\s*(?:[-*]\s*)?Original Rationale\s*:\s*(\S.*)\s*$",
            decisions_text,
        )
        if rationale is None or rationale.group(1).strip(" `") in {"", "..."}:
            raise guard.Conflict(
                "Recovered decisions require an explicit rationale evidence value or UNKNOWN_RATIONALE"
            )


def confirm_adoption(
    project: str,
    *,
    owner: str,
    activation_token: str,
    expected_state_sha: str,
    expected_strategy_sha: str,
    evidence: str,
) -> dict[str, Any]:
    """Confirm five current-reality ledgers and enter normal OPERATING mode."""

    evidence = _text(evidence, "Adoption canonical evidence")

    def mutate(state: dict[str, Any] | None, founder: Path):
        if state is None:
            raise guard.Conflict("Adoption Strategy state is not initialized")
        if (
            state.get("project_phase") != "pre-adoption"
            or state.get("adoption_status") != "BASELINE_READY"
            or state.get("gate", {}).get("state") != "ADOPTION_STATE_REQUIRED"
        ):
            raise guard.Conflict("The current Strategy is not awaiting Adoption confirmation")
        _validate_adoption_ledgers(founder, state)
        observed_id, observed_sha = _observe_adoption_baseline(founder.parent)
        adoption = state["adoption"]
        if (observed_id, observed_sha) != (
            adoption["baseline_id"],
            adoption["baseline_sha256"],
        ):
            raise guard.Conflict(
                "ADOPTION_BASELINE_DRIFT: source/config/Git evidence changed before confirmation"
            )
        now = guard.utc_now()
        state["project_phase"] = "bootstrapped"
        state["adoption_status"] = "ADOPTED"
        adoption["adopted_at"] = now
        state["gate"].update(
            {
                "state": "OPERATING",
                "context": "none",
                "decision_level": None,
                "proposal_id": None,
                "reason": "Existing project baseline and current-reality ledgers are canonical",
                "authorization_ref": evidence,
                "resolved_at": now,
            }
        )
        return state, {
            "project_origin": state["project_origin"],
            "project_lifecycle": state["project_lifecycle"],
            "adoption_status": state["adoption_status"],
            "management_mode": adoption["management_mode"],
            "baseline_id": adoption["baseline_id"],
            "gate": state["gate"]["state"],
        }

    return _mutate_strategy(
        project,
        owner=owner,
        activation_token=activation_token,
        expected_state_sha=expected_state_sha,
        expected_strategy_sha=expected_strategy_sha,
        operation="ADOPTION_CONFIRMED",
        mutate=mutate,
    )


def _verified_thread_sync(founder: Path, agent_id: str) -> tuple[str, str]:
    """Return (thread_record_id, acknowledgement) for a current primary sync."""
    # Local import avoids a module-load cycle when Thread Registry imports this
    # controller for Gate enforcement.
    import thread_registry as registry_api

    registry_path = founder / THREADS_NAME
    registry_sha, _raw, registry = registry_api._read_registry(registry_path)
    if registry is None:
        raise guard.Conflict("STATE_SYNC requires an initialized THREADS.json registry")
    registry_api.validate_registry(registry, founder.parent)
    binding = registry["agent_bindings"].get(agent_id)
    if not isinstance(binding, dict) or binding.get("agent_kind") != "persistent":
        raise guard.Conflict(f"Affected Agent is not a registered persistent Agent: {agent_id}")
    thread_record_id = binding.get("primary_thread_record_id")
    if not isinstance(thread_record_id, str):
        raise guard.Conflict(f"Affected Agent has no current primary Thread: {agent_id}")
    thread = registry_api._find_thread(registry, thread_record_id)
    if thread.get("binding_role") != "primary":
        raise guard.Conflict(f"Affected Agent Thread is not the current primary: {agent_id}")
    current_context = registry_api._context_baseline(founder)
    if not registry_api._baseline_matches(thread.get("context_baseline"), current_context):
        raise guard.Conflict(f"Affected Agent Thread still has stale strategic context: {agent_id}")
    sync = thread.get("last_state_sync")
    if not isinstance(sync, dict):
        raise guard.Conflict(f"Affected Agent has no recorded STATE_SYNC acknowledgement: {agent_id}")
    acknowledgement = _text(sync.get("acknowledgement"), "STATE_SYNC acknowledgement")
    return thread_record_id, f"THREADS_SHA256={registry_sha}; {acknowledgement}"


def _has_current_persistent_primary(founder: Path, agent_id: str) -> bool:
    """Return whether an Agent still owns a validated current persistent primary."""

    import thread_registry as registry_api

    _registry_sha, _raw, registry = registry_api._read_registry(founder / THREADS_NAME)
    if registry is None:
        return False
    registry_api.validate_registry(registry, founder.parent)
    binding = registry["agent_bindings"].get(agent_id)
    if not isinstance(binding, dict) or binding.get("agent_kind") != "persistent":
        return False
    thread_record_id = binding.get("primary_thread_record_id")
    if not isinstance(thread_record_id, str):
        return False
    thread = registry_api._find_thread(registry, thread_record_id)
    return thread.get("binding_role") == "primary"


def complete_state_sync(
    project: str,
    *,
    owner: str,
    activation_token: str,
    expected_state_sha: str,
    expected_strategy_sha: str,
) -> dict[str, Any]:
    """Verify all affected persistent Agents acknowledged current strategy."""

    def mutate(state: dict[str, Any] | None, founder: Path):
        if state is None or state["gate"]["state"] != "STATE_SYNC_REQUIRED":
            raise guard.Conflict("The current Strategic Gate is not waiting for STATE_SYNC")
        confirmations: list[dict[str, str]] = []
        for row in state["pending_state_sync"]:
            if row["status"] == "confirmed" and row["disposition"] in {
                "retired",
                "not-applicable",
            }:
                if _has_current_persistent_primary(founder, row["agent_id"]) and (
                    row["disposition"] == "retired"
                    or state["gate"].get("context") == "autonomy"
                ):
                    raise guard.Conflict(
                        "A current persistent primary cannot bypass required STATE_SYNC by disposition"
                    )
                confirmations.append(
                    {"agent_id": row["agent_id"], "thread_record_id": "NONE"}
                )
                continue
            thread_record_id, evidence = _verified_thread_sync(founder, row["agent_id"])
            row.update(
                {
                    "status": "confirmed",
                    "disposition": "synced",
                    "thread_record_id": thread_record_id,
                    "evidence": evidence,
                }
            )
            confirmations.append(
                {"agent_id": row["agent_id"], "thread_record_id": thread_record_id}
            )
        state["gate"].update(
            {
                "state": "OPERATING",
                "context": "none",
                "decision_level": None,
                "reason": "All affected persistent Agents acknowledged the current strategy",
            }
        )
        return state, {"gate": "OPERATING", "confirmations": confirmations}

    return _mutate_strategy(
        project,
        owner=owner,
        activation_token=activation_token,
        expected_state_sha=expected_state_sha,
        expected_strategy_sha=expected_strategy_sha,
        operation="STRATEGIC_STATE_SYNC_COMPLETED",
        mutate=mutate,
    )


def resolve_state_sync_disposition(
    project: str,
    *,
    owner: str,
    activation_token: str,
    expected_state_sha: str,
    expected_strategy_sha: str,
    agent_id: str,
    disposition: str,
    evidence: str,
) -> dict[str, Any]:
    """Resolve a pending strategic sync for a retired or unaffected Agent."""
    agent_id = _agent_id(agent_id)
    if disposition not in {"retired", "not-applicable"}:
        raise guard.InvalidState("Sync disposition must be retired or not-applicable")
    evidence = _text(evidence, "STATE_SYNC disposition evidence")

    def mutate(state: dict[str, Any] | None, founder: Path):
        if state is None or state["gate"]["state"] != "STATE_SYNC_REQUIRED":
            raise guard.Conflict("STATE_SYNC disposition requires the current sync Gate")
        row = next(
            (item for item in state["pending_state_sync"] if item["agent_id"] == agent_id),
            None,
        )
        if row is None or row["status"] != "pending":
            raise guard.Conflict("Agent has no unresolved STATE_SYNC obligation")
        has_current_primary = _has_current_persistent_primary(founder, agent_id)
        if disposition == "retired" and has_current_primary:
            raise guard.Conflict("Retired Agent still has a current primary Thread")
        if (
            disposition == "not-applicable"
            and state["gate"].get("context") == "autonomy"
            and has_current_primary
        ):
            raise guard.Conflict(
                "Autonomy Profile changes require every current persistent primary to complete exact STATE_SYNC"
            )
        row.update(
            {
                "status": "confirmed",
                "disposition": disposition,
                "thread_record_id": None,
                "evidence": evidence,
            }
        )
        return state, {"agent_id": agent_id, "disposition": disposition, "resolved": True}

    return _mutate_strategy(
        project,
        owner=owner,
        activation_token=activation_token,
        expected_state_sha=expected_state_sha,
        expected_strategy_sha=expected_strategy_sha,
        operation="STRATEGIC_STATE_SYNC_DISPOSITIONED",
        mutate=mutate,
    )


def _autonomous_report_block(status_text: str, decision_id: str) -> dict[str, str] | None:
    required = (
        "Decision ID",
        "Proposal ID",
        "Selected Strategy ID",
        "Rationale",
        "Biggest Risk",
        "Reconsideration Trigger",
    )
    pattern = re.compile(
        r"(?ms)^## Autonomous Strategic Decision Report\s*$\n(.*?)(?=^##\s|\Z)"
    )
    matching_reports: list[dict[str, str]] = []
    for match in pattern.finditer(status_text):
        block = match.group(1)
        fields: dict[str, str] = {}
        for label in required:
            field_match = re.search(
                rf"(?m)^- {re.escape(label)}:\s*(\S.*)$", block
            )
            if field_match is not None:
                fields[label] = field_match.group(1).strip()
        if fields.get("Decision ID") == decision_id:
            if set(fields) != set(required):
                return None
            matching_reports.append(fields)
    return matching_reports[0] if len(matching_reports) == 1 else None


def mark_reported(
    project: str,
    *,
    owner: str,
    activation_token: str,
    expected_state_sha: str,
    expected_strategy_sha: str,
    decision_id: str,
    delivery_ref: str,
) -> dict[str, Any]:
    decision_id = _identifier(decision_id, "decision_id")
    delivery_ref = _text(delivery_ref, "boss summary delivery_ref")

    def mutate(state: dict[str, Any] | None, founder: Path):
        if state is None:
            raise guard.Conflict("Strategy state is not initialized")
        pending = state["reporting"]["pending_decision_ids"]
        if decision_id not in pending:
            raise guard.Conflict("Decision is not waiting for an autonomy report")
        decision = state["decision_record"]
        if (
            state["gate"]["state"] != "OPERATING"
            or decision.get("status") != "confirmed"
            or decision.get("level") != "L2"
            or decision.get("selection_authority") != "autonomy"
            or decision.get("decision_id") != decision_id
        ):
            raise guard.Conflict(
                "Autonomy report can close only after the current L2 autonomy decision is canonical and OPERATING"
            )
        status_text = _read_utf8_direct(founder / "STATUS.md", "STATUS.md")
        report = _autonomous_report_block(status_text, decision_id)
        if report is None:
            raise guard.Conflict(
                "STATUS.md requires one complete Autonomous Strategic Decision Report block"
            )
        if (
            report["Proposal ID"] != decision.get("proposal_id")
            or report["Selected Strategy ID"] != decision.get("selected_strategy_id")
        ):
            raise guard.Conflict(
                "Autonomous report does not match the current proposal and selected strategy"
            )
        pending.remove(decision_id)
        state["reporting"].setdefault("reported", []).append(
            {
                "decision_id": decision_id,
                "delivery_ref": delivery_ref,
                "reported_at": guard.utc_now(),
            }
        )
        return state, {
            "decision_id": decision_id,
            "reported": True,
            "delivery_ref": delivery_ref,
        }

    return _mutate_strategy(
        project,
        owner=owner,
        activation_token=activation_token,
        expected_state_sha=expected_state_sha,
        expected_strategy_sha=expected_strategy_sha,
        operation="AUTONOMOUS_DECISION_REPORTED",
        mutate=mutate,
    )


def _assert_control_plane_current(founder: Path, state: dict[str, Any] | None) -> None:
    """Fail closed on stranded Strategy transactions or stale Supervisor state."""

    strategy_lock = founder / STRATEGY_LOCK_NAME
    if strategy_lock.exists():
        _direct_file(strategy_lock, "Strategy transaction lock")
        raise guard.Conflict(
            "RECOVERY_REQUIRED: a Strategy transaction lock exists; only inspect/recover is allowed"
        )
    if state is None:
        return
    root = founder.parent
    state_sha, record = guard.state_observation(founder / guard.STATE_NAME)
    if record is None:
        raise guard.Conflict("RECOVERY_REQUIRED: Strategy exists without a Supervisor record")
    guard.validate_record(record, root)
    if record.get("mode") != "ACTIVE":
        raise guard.Conflict("RECOVERY_REQUIRED: Strategy has no ACTIVE Supervisor")
    if record.get("handoff", {}).get("state") == "offered":
        raise guard.Conflict("RECOVERY_REQUIRED: Supervisor handoff freezes new actions")
    current_sources = guard.read_source_revisions(founder)
    if not guard.source_fingerprints_match(record.get("source_revisions"), current_sources):
        raise guard.Conflict(
            "RECOVERY_REQUIRED: Supervisor/Strategy source fingerprints are stale"
        )
    write_lock = guard._lock_owner(founder / guard.LOCK_NAME)
    if write_lock is not None:
        guard._validate_lock_record_binding(root, record, state_sha, write_lock)


def _strategy_snapshot(project: str) -> tuple[Path, Path, dict[str, Any] | None]:
    root, founder, _created = guard.resolve_project_root(project)
    _sha, _raw, state = _read_strategy(founder / STRATEGY_NAME)
    if state is not None:
        validate_strategy(state, root)
    _assert_control_plane_current(founder, state)
    return root, founder, state


def _scope_is_read_only(strategy_scope: str | None, task_write_scope: list[str]) -> bool:
    return strategy_scope in {
        "discovery-read-only",
        "adoption-read-only",
        "unrelated-read-only",
    } and not task_write_scope


def _preflight_unclaimed_adoption(
    project: str,
    *,
    action: str,
    strategy_scope: str | None,
    thread_type: str | None,
    agent_kind: str | None,
    task_write_scope: list[str],
) -> dict[str, Any] | None:
    """Authorize only evidence-backed, unclaimed, zero-write Adoption reading."""

    adoption_request = action == "adoption-read-only" or (
        action == "subagent-dispatch" and strategy_scope == "adoption-read-only"
    )
    if not adoption_request:
        return None
    import project_baseline as baseline_api

    report = baseline_api.inspect_project(project)
    completeness = report.get("completeness", {})
    baseline_result = report.get("result")
    observation_available = baseline_result in {"COMPLETE", "PARTIAL"}
    baseline_anchor_usable = completeness.get(
        "baseline_anchor_usable", completeness.get("baseline_usable")
    ) is True
    audit_coverage_complete = completeness.get(
        "audit_coverage_complete", baseline_result == "COMPLETE"
    ) is True
    founder_state = report.get("founder_state", {}).get("classification")
    if founder_state == "PRE_ADOPTION_CONTROL":
        return None
    no_write = not task_write_scope
    bounded_task = action == "adoption-read-only" or (
        action == "subagent-dispatch"
        and thread_type in {"task", "review", "fork-readonly"}
        and agent_kind == "task"
    )
    existing_evidence = bool(
        report.get("entry_signals", {}).get("evident_existing")
    )
    allowed = (
        founder_state == "ABSENT"
        and existing_evidence
        and observation_available
        and no_write
        and bounded_task
    )
    if founder_state != "ABSENT":
        reason = (
            "Existing .founder state requires resume, legacy migration, or Recovery; "
            "Brownfield Adoption may not overwrite it"
        )
    elif not observation_available:
        reason = "Existing-project evidence could not be observed safely; Adoption remains blocked"
    elif not existing_evidence:
        reason = "No existing-project evidence was observed; classify NEW_PROJECT before Bootstrap"
    elif not no_write or not bounded_task:
        reason = "ADOPTION_READ_ONLY permits only bounded task/subagent work with an empty write scope"
    elif not baseline_anchor_usable:
        reason = (
            "Bounded Existing Project audit is authorized, but formal Adoption remains blocked "
            "until a deterministic baseline anchor is available"
        )
    elif not audit_coverage_complete:
        reason = (
            "Bounded Existing Project audit is authorized with declared coverage limitations; "
            "the deterministic baseline anchor remains eligible for formal Adoption"
        )
    else:
        reason = "Evidence-backed Existing Project audit is authorized with zero project write scope"
    return {
        "result": "ACTION_AUTHORIZED" if allowed else "ACTION_BLOCKED",
        "allowed": allowed,
        "legacy": False,
        "reason": reason,
        "gate": "ADOPTION_READ_ONLY",
        "project_phase": "unpersisted-read-only-audit",
        "founder_state": founder_state,
        "baseline_id": report.get("baseline_id"),
        "baseline_sha256": report.get("baseline_sha256"),
        "baseline_result": baseline_result,
        "baseline_anchor_usable": baseline_anchor_usable,
        "audit_coverage_complete": audit_coverage_complete,
        "audit_limitations": completeness.get("reasons", []),
        "formal_adoption_allowed": bool(allowed and baseline_anchor_usable),
        "changed_paths": [],
    }


def authorize_action(
    project: str,
    *,
    action: str,
    strategy_scope: str | None = None,
    thread_type: str | None = None,
    agent_kind: str | None = None,
    task_write_scope: list[str] | None = None,
    proposal_id: str | None = None,
    decision_id: str | None = None,
    action_scope: str | None = None,
) -> dict[str, Any]:
    """Read-only protocol preflight; semantic classification remains an Agent job."""
    if action not in ACTION_TYPES:
        raise guard.InvalidState(f"Unknown action type: {action}")
    if strategy_scope is not None and strategy_scope not in THREAD_STRATEGY_SCOPES:
        raise guard.InvalidState("Unknown strategy_scope")
    task_write_scope = task_write_scope or []
    if not isinstance(task_write_scope, list):
        raise guard.InvalidState("task_write_scope must be a list")
    for item in task_write_scope:
        _text(item, "task_write_scope item", max_length=256)
    adoption_preflight = _preflight_unclaimed_adoption(
        project,
        action=action,
        strategy_scope=strategy_scope,
        thread_type=thread_type,
        agent_kind=agent_kind,
        task_write_scope=task_write_scope,
    )
    if adoption_preflight is not None:
        return adoption_preflight
    _root, _founder, state = _strategy_snapshot(project)
    if state is None:
        presence = {name: (_founder / name).exists() for name in CORE_LEDGERS}
        count = sum(presence.values())
        safe_read = (
            action in {"unrelated-read-only", "adoption-read-only"}
            or (action == "subagent-dispatch" and strategy_scope == "adoption-read-only")
        ) and not task_write_scope
        if count == 0:
            reason = (
                "Safe read-only work is allowed before Strategy initialization"
                if safe_read
                else "Initialize new Strategy state before Direction Clarity or project execution"
            )
        elif count == len(CORE_LEDGERS):
            reason = (
                "Safe read-only work is allowed before non-disruptive legacy migration"
                if safe_read
                else "LEGACY_MIGRATION_REQUIRED: infer selected direction and initialize the default Autonomy Profile"
            )
        else:
            safe_read = False
            reason = "RECOVERY_REQUIRED: partial canonical ledgers prevent safe Strategy initialization"
        return {
            "result": "ACTION_AUTHORIZED" if safe_read else "ACTION_BLOCKED",
            "allowed": safe_read,
            "legacy": True,
            "reason": reason,
            "gate": "STRATEGY_INITIALIZATION_REQUIRED",
            "changed_paths": [],
        }

    gate = state["gate"]["state"]
    allowed = False
    reason = f"{action} is blocked while Strategic Gate is {gate}"
    if action == "direction-assessment":
        allowed = gate == "DIRECTION_CHECK_REQUIRED"
    elif action == "bootstrap":
        allowed = gate == "BOOTSTRAP_AUTHORIZED"
    elif action == "canonical-decision-update":
        allowed = gate in {"BOOTSTRAP_AUTHORIZED", "DECISION_RECORD_REQUIRED"}
    elif action == "state-sync":
        allowed = gate in {"STATE_SYNC_REQUIRED", "OPERATING"}
    elif action == "discovery-read-only":
        allowed = gate in {"DISCOVERY_ACTIVE", "STRATEGIC_CHOICE_REQUIRED"} and not task_write_scope
    elif action == "adoption-read-only":
        allowed = gate == "ADOPTION_STATE_REQUIRED" and not task_write_scope
    elif action == "unrelated-read-only":
        allowed = not task_write_scope
    elif action == "subagent-dispatch":
        allowed = gate == "OPERATING" or (
            gate in {"DISCOVERY_ACTIVE", "STRATEGIC_CHOICE_REQUIRED"}
            and strategy_scope in {"discovery-read-only", "unrelated-read-only"}
            and not task_write_scope
            and thread_type in {"task", "review", "fork-readonly"}
            and agent_kind == "task"
        ) or (
            gate == "ADOPTION_STATE_REQUIRED"
            and strategy_scope == "adoption-read-only"
            and not task_write_scope
            and thread_type in {"task", "review", "fork-readonly"}
            and agent_kind == "task"
        )
    elif action == "executive-action":
        decision = state["decision_record"]
        allowed = (
            gate == "OPERATING"
            and decision.get("status") == "confirmed"
            and decision.get("level") == "L3"
            and decision.get("decision_id") == decision_id
            and state["gate"].get("proposal_id") == proposal_id
            and decision.get("action_scope") == action_scope
            and decision.get("action_status") == "approved"
        )
        if not allowed:
            reason = (
                "Executive action requires the exact current unconsumed Founder-approved "
                "L3 proposal and decision"
            )
    else:
        allowed = gate == "OPERATING"
    if allowed:
        reason = f"{action} is within the current Gate and declared scope"
    payload = {
        "result": "ACTION_AUTHORIZED" if allowed else "ACTION_BLOCKED",
        "allowed": allowed,
        "legacy": False,
        "reason": reason,
        "gate": gate,
        "project_phase": state["project_phase"],
        "strategy_context_revision": state["context_revision"],
        "strategy_context_sha": state["context_sha256"],
        "profile": state["autonomy_profile"],
        "changed_paths": [],
    }
    if action == "executive-action" and allowed:
        payload["single_use_consumption_required"] = True
    return payload


def enforce_thread_action(
    founder: Path,
    *,
    operation: str,
    strategy_scope: str,
    thread_type: str,
    agent_kind: str,
    effective_write_scope: list[str],
) -> None:
    """Fail closed for Thread create/bind/assign/handoff under an active Gate."""
    if operation not in {
        "registry-init",
        "reserve",
        "bind",
        "assign",
        "resume",
        "begin-handoff",
        "handoff-bind",
        "complete-handoff",
    }:
        raise guard.InvalidState("Unknown Thread Gate operation")
    if strategy_scope not in THREAD_STRATEGY_SCOPES:
        raise guard.InvalidState("Thread strategy_scope is required and invalid")
    _sha, _raw, state = _read_strategy(founder / STRATEGY_NAME)
    if state is None:
        _assert_control_plane_current(founder, state)
        presence = {name: (founder / name).exists() for name in CORE_LEDGERS}
        count = sum(presence.values())
        if count not in {0, len(CORE_LEDGERS)}:
            raise guard.Conflict(
                "RECOVERY_REQUIRED: partial canonical ledgers block Thread operations"
            )
        if (
            count == len(CORE_LEDGERS)
            and operation in {"begin-handoff", "handoff-bind", "complete-handoff"}
            and strategy_scope == "control-recovery"
        ):
            return
        if (
            operation != "registry-init"
            and strategy_scope in {"adoption-read-only", "unrelated-read-only"}
            and not effective_write_scope
            and thread_type in {"task", "review", "fork-readonly"}
            and agent_kind == "task"
        ):
            return
        reason = (
            "Initialize Strategy before creating candidate-bound or persistent Threads"
            if count == 0
            else "LEGACY_MIGRATION_REQUIRED: initialize the inferred Strategy before Thread execution"
        )
        raise guard.Conflict(reason)
    validate_strategy(state, founder.parent)
    _assert_control_plane_current(founder, state)
    gate = state["gate"]["state"]
    if operation == "registry-init":
        if gate in {
            "DISCOVERY_ACTIVE",
            "STRATEGIC_CHOICE_REQUIRED",
            "OPERATING",
        }:
            return
        raise guard.Conflict(
            f"Thread Registry initialization is blocked by Strategic Gate {gate}"
        )
    if gate == "OPERATING":
        return
    if operation in {"begin-handoff", "handoff-bind", "complete-handoff"} and strategy_scope == "control-recovery":
        return
    if agent_kind == "persistent" or thread_type == "persistent":
        raise guard.Conflict(f"Persistent Thread operation is blocked by Strategic Gate {gate}")
    allowed_gate_scopes = (
        {"discovery-read-only", "unrelated-read-only"}
        if gate in {"DISCOVERY_ACTIVE", "STRATEGIC_CHOICE_REQUIRED"}
        else {"adoption-read-only"}
        if gate == "ADOPTION_STATE_REQUIRED"
        else set()
    )
    if (
        strategy_scope in allowed_gate_scopes
        and not effective_write_scope
        and thread_type in {"task", "review", "fork-readonly"}
        and agent_kind == "task"
    ):
        return
    raise guard.Conflict(
        f"Thread {operation} is blocked by Strategic Gate {gate}; only explicit safe read-only scope may proceed"
    )


STATE_SYNC_ACK_KEYS = frozenset(
    {
        "THREAD_RECORD_ID",
        "BINDING_GENERATION",
        "RUNTIME_THREAD_ID",
        "RUNTIME_HOST_ID",
        "AGENT_ID",
        "STRATEGY_CONTEXT_REVISION",
        "STRATEGY_CONTEXT_SHA256",
        "CONTEXT_BASELINE_SHA256",
    }
)


def _parse_exact_state_sync_ack(acknowledgement: str) -> dict[str, str]:
    """Parse one exact, contradiction-free STATE_SYNC machine acknowledgement."""

    acknowledgement = _text(
        acknowledgement, "STATE_SYNC acknowledgement", max_length=4096
    )
    prefix = "STATE_SYNC "
    if not acknowledgement.startswith(prefix):
        raise guard.Conflict(
            "STATE_SYNC acknowledgement must start with the exact STATE_SYNC protocol prefix"
        )
    payload = acknowledgement[len(prefix) :]
    if not payload or payload != payload.strip():
        raise guard.Conflict("STATE_SYNC acknowledgement framing is malformed")
    tokens = payload.split(" ")
    if any(
        not token or "\t" in token or "\r" in token or "\n" in token
        for token in tokens
    ):
        raise guard.Conflict("STATE_SYNC acknowledgement tokens are malformed")
    observed: dict[str, str] = {}
    for token in tokens:
        if token.count("=") != 1:
            raise guard.Conflict("STATE_SYNC acknowledgement marker is malformed")
        key, value = token.split("=", 1)
        if not key or not value or key in observed:
            raise guard.Conflict(
                "STATE_SYNC acknowledgement contains an empty or duplicate marker"
            )
        observed[key] = value
    if set(observed) != STATE_SYNC_ACK_KEYS or len(tokens) != len(STATE_SYNC_ACK_KEYS):
        raise guard.Conflict(
            "STATE_SYNC acknowledgement must contain only the exact Thread, runtime, Agent, Strategy, and context markers"
        )
    return observed


def validate_state_sync_ack(
    founder: Path,
    *,
    agent_id: str,
    acknowledgement: str,
    expected_markers: dict[str, str],
) -> None:
    """Bind STATE_SYNC to the exact live Thread identity and canonical context."""

    agent_id = _agent_id(agent_id)
    if (
        not isinstance(expected_markers, dict)
        or set(expected_markers) != STATE_SYNC_ACK_KEYS
        or any(
            not isinstance(value, str) or not value or any(character.isspace() for character in value)
            for value in expected_markers.values()
        )
    ):
        raise guard.InvalidState("STATE_SYNC expected marker set is malformed")
    if expected_markers["AGENT_ID"] != agent_id:
        raise guard.InvalidState("STATE_SYNC expected Agent marker is inconsistent")
    observed = _parse_exact_state_sync_ack(acknowledgement)
    if observed != expected_markers:
        raise guard.Conflict(
            "STATE_SYNC acknowledgement must bind the exact Thread, generation, runtime, Agent, Strategy, and context baseline"
        )
    _sha, _raw, state = _read_strategy(founder / STRATEGY_NAME)
    if state is None:
        raise guard.Conflict(
            "LEGACY_MIGRATION_REQUIRED: STATE_SYNC requires initialized Strategy context"
        )
    validate_strategy(state, founder.parent)
    _assert_control_plane_current(founder, state)
    gate = state["gate"]["state"]
    if gate not in {"OPERATING", "STATE_SYNC_REQUIRED"}:
        raise guard.Conflict(f"STATE_SYNC cannot clear Thread context while Strategic Gate is {gate}")
    if gate == "STATE_SYNC_REQUIRED":
        pending = next(
            (row for row in state["pending_state_sync"] if row["agent_id"] == agent_id),
            None,
        )
        if pending is None:
            raise guard.Conflict("Thread Agent is not part of the current strategic STATE_SYNC set")
    if (
        expected_markers["STRATEGY_CONTEXT_REVISION"] != state["context_revision"]
        or expected_markers["STRATEGY_CONTEXT_SHA256"] != state["context_sha256"]
    ):
        raise guard.Conflict(
            "STATE_SYNC expected markers do not name the current Strategy context"
        )


def _json_value(raw: str, label: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise guard.InvalidState(f"{label} must be valid JSON: {exc}") from exc


def _json_array(raw: str, label: str) -> list[Any]:
    value = _json_value(raw, label)
    if not isinstance(value, list):
        raise guard.InvalidState(f"{label} must be a JSON array")
    return value


def _add_mutation_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--project", required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--activation-token", required=True)
    parser.add_argument("--expected-state-sha", required=True)
    parser.add_argument("--expected-strategy-sha", required=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    inspect_parser = subparsers.add_parser("inspect")
    inspect_parser.add_argument("--project", required=True)

    recover_parser = subparsers.add_parser("recover-lock")
    _add_mutation_args(recover_parser)
    recover_parser.add_argument("--lock-owner", required=True)
    recover_parser.add_argument(
        "--predecessor-liveness", choices=("current", "terminated"), required=True
    )
    recover_parser.add_argument("--authorization-ref", required=True)

    init_parser = subparsers.add_parser("init")
    _add_mutation_args(init_parser)
    init_parser.add_argument("--mode", choices=("new", "legacy"), required=True)
    init_parser.add_argument("--legacy-summary")
    init_parser.add_argument("--evidence", required=True)

    adoption_init_parser = subparsers.add_parser("init-adoption")
    _add_mutation_args(adoption_init_parser)
    adoption_init_parser.add_argument(
        "--detected-mode", choices=sorted(ADOPTION_DETECTED_MODES), required=True
    )
    adoption_init_parser.add_argument(
        "--project-lifecycle", choices=sorted(PROJECT_LIFECYCLES), required=True
    )
    adoption_init_parser.add_argument(
        "--adoption-confidence", choices=sorted(ADOPTION_CONFIDENCES), required=True
    )
    adoption_init_parser.add_argument("--baseline-id", required=True)
    adoption_init_parser.add_argument("--baseline-sha256", required=True)
    adoption_init_parser.add_argument("--direction-summary", required=True)
    adoption_init_parser.add_argument(
        "--management-mode", choices=sorted(ADOPTION_MANAGEMENT_MODES), required=True
    )
    adoption_init_parser.add_argument("--evidence-ref", action="append", required=True)
    adoption_init_parser.add_argument("--adoption-review-ref")

    assess_parser = subparsers.add_parser("assess")
    _add_mutation_args(assess_parser)
    assess_parser.add_argument("--outcome", choices=("CLEAR", "AMBIGUOUS"), required=True)
    assess_parser.add_argument("--reason", required=True)
    assess_parser.add_argument("--direction-summary", required=True)
    assess_parser.add_argument("--depth", choices=sorted(DISCOVERY_DEPTHS), required=True)

    candidate_parser = subparsers.add_parser("candidates")
    _add_mutation_args(candidate_parser)
    candidate_parser.add_argument("--proposal-id", required=True)
    candidate_parser.add_argument("--candidates-json", required=True)
    candidate_parser.add_argument("--recommendation-id", required=True)
    candidate_parser.add_argument("--recommendation-json", required=True)
    candidate_parser.add_argument("--evidence", action="append", required=True)
    candidate_parser.add_argument("--single-candidate-reason")

    revise_parser = subparsers.add_parser("revise-discovery")
    _add_mutation_args(revise_parser)
    revise_parser.add_argument("--proposal-id", required=True)
    revise_parser.add_argument("--reason", required=True)
    revise_parser.add_argument(
        "--depth", choices=sorted(DISCOVERY_DEPTHS - {"NONE"}), required=True
    )

    agent_parser = subparsers.add_parser("record-agent")
    _add_mutation_args(agent_parser)
    agent_parser.add_argument("--assignment-id", required=True)
    agent_parser.add_argument("--runtime-agent-id", required=True)
    agent_parser.add_argument("--role", required=True)
    agent_parser.add_argument("--task", required=True)
    agent_parser.add_argument("--read-scope", action="append", default=[])

    agent_update = subparsers.add_parser("update-agent")
    _add_mutation_args(agent_update)
    agent_update.add_argument("--assignment-id", required=True)
    agent_update.add_argument("--status", choices=("returned", "accepted", "failed"), required=True)
    agent_update.add_argument("--evidence", required=True)

    select_parser = subparsers.add_parser("select")
    _add_mutation_args(select_parser)
    select_parser.add_argument("--proposal-id", required=True)
    select_parser.add_argument("--candidate-id", required=True)
    select_parser.add_argument("--authority", choices=("founder", "delegated", "autonomy"), required=True)
    select_parser.add_argument("--authorization-ref", required=True)
    select_parser.add_argument("--decision-id", required=True)
    select_parser.add_argument("--rationale", required=True)
    select_parser.add_argument("--nonselected-status", choices=("REJECTED", "DEFERRED"), default="DEFERRED")

    autonomy_parser = subparsers.add_parser("autonomy")
    _add_mutation_args(autonomy_parser)
    autonomy_parser.add_argument("--strategic", choices=sorted(STRATEGIC_AUTONOMY), required=True)
    autonomy_parser.add_argument("--authorization-ref", required=True)

    pivot_parser = subparsers.add_parser("open-pivot")
    _add_mutation_args(pivot_parser)
    pivot_parser.add_argument("--proposal-id", required=True)
    pivot_parser.add_argument("--summary", required=True)
    pivot_parser.add_argument("--candidates-json", required=True)
    pivot_parser.add_argument("--recommendation-id", required=True)
    pivot_parser.add_argument("--recommendation-json", required=True)
    pivot_parser.add_argument("--evidence", action="append", required=True)
    pivot_parser.add_argument("--affected-agent-id", action="append", default=[])
    pivot_parser.add_argument("--single-candidate-reason")

    executive_parser = subparsers.add_parser("open-executive")
    _add_mutation_args(executive_parser)
    executive_parser.add_argument("--proposal-id", required=True)
    executive_parser.add_argument("--summary", required=True)
    executive_parser.add_argument("--action-scope", required=True)

    approve_parser = subparsers.add_parser("approve-executive")
    _add_mutation_args(approve_parser)
    approve_parser.add_argument("--proposal-id", required=True)
    approve_parser.add_argument("--authorization-ref", required=True)
    approve_parser.add_argument("--decision-id", required=True)

    consume_parser = subparsers.add_parser("consume-executive")
    _add_mutation_args(consume_parser)
    consume_parser.add_argument("--proposal-id", required=True)
    consume_parser.add_argument("--decision-id", required=True)
    consume_parser.add_argument("--action-scope", required=True)
    consume_parser.add_argument("--execution-ref", required=True)

    reject_parser = subparsers.add_parser("reject-executive")
    _add_mutation_args(reject_parser)
    reject_parser.add_argument("--proposal-id", required=True)
    reject_parser.add_argument("--authorization-ref", required=True)

    canonical_parser = subparsers.add_parser("confirm-canonical")
    _add_mutation_args(canonical_parser)
    canonical_parser.add_argument("--evidence", required=True)

    adoption_confirm_parser = subparsers.add_parser("confirm-adoption")
    _add_mutation_args(adoption_confirm_parser)
    adoption_confirm_parser.add_argument("--evidence", required=True)

    sync_parser = subparsers.add_parser("complete-state-sync")
    _add_mutation_args(sync_parser)

    disposition_parser = subparsers.add_parser("resolve-state-sync")
    _add_mutation_args(disposition_parser)
    disposition_parser.add_argument("--agent-id", required=True)
    disposition_parser.add_argument(
        "--disposition", choices=("retired", "not-applicable"), required=True
    )
    disposition_parser.add_argument("--evidence", required=True)

    report_parser = subparsers.add_parser("mark-reported")
    _add_mutation_args(report_parser)
    report_parser.add_argument("--decision-id", required=True)
    report_parser.add_argument("--delivery-ref", required=True)

    authorize_parser = subparsers.add_parser("authorize")
    authorize_parser.add_argument("--project", required=True)
    authorize_parser.add_argument("--action", choices=sorted(ACTION_TYPES), required=True)
    authorize_parser.add_argument("--strategy-scope", choices=sorted(THREAD_STRATEGY_SCOPES))
    authorize_parser.add_argument("--thread-type")
    authorize_parser.add_argument("--agent-kind")
    authorize_parser.add_argument("--task-write-scope", action="append", default=[])
    authorize_parser.add_argument("--proposal-id")
    authorize_parser.add_argument("--decision-id")
    authorize_parser.add_argument("--action-scope")
    return parser


def emit(payload: dict[str, Any], exit_code: int = 0) -> int:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return exit_code


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "inspect":
            payload = inspect_strategy(args.project)
        elif args.command == "authorize":
            payload = authorize_action(
                args.project,
                action=args.action,
                strategy_scope=args.strategy_scope,
                thread_type=args.thread_type,
                agent_kind=args.agent_kind,
                task_write_scope=args.task_write_scope,
                proposal_id=args.proposal_id,
                decision_id=args.decision_id,
                action_scope=args.action_scope,
            )
        else:
            common = {
                "project": args.project,
                "owner": args.owner,
                "activation_token": args.activation_token,
                "expected_state_sha": args.expected_state_sha,
                "expected_strategy_sha": args.expected_strategy_sha,
            }
            if args.command == "init":
                payload = initialize_strategy(
                    **common,
                    mode=args.mode,
                    legacy_summary=args.legacy_summary,
                    evidence=args.evidence,
                )
            elif args.command == "init-adoption":
                payload = initialize_adoption(
                    **common,
                    detected_mode=args.detected_mode,
                    project_lifecycle=args.project_lifecycle,
                    adoption_confidence=args.adoption_confidence,
                    baseline_id=args.baseline_id,
                    baseline_sha256=args.baseline_sha256,
                    direction_summary=args.direction_summary,
                    management_mode=args.management_mode,
                    evidence_refs=args.evidence_ref,
                    adoption_review_ref=args.adoption_review_ref,
                )
            elif args.command == "recover-lock":
                payload = recover_strategy_lock(
                    **common,
                    lock_owner=args.lock_owner,
                    predecessor_liveness=args.predecessor_liveness,
                    authorization_ref=args.authorization_ref,
                )
            elif args.command == "assess":
                payload = assess_direction(
                    **common,
                    outcome=args.outcome,
                    reason=args.reason,
                    direction_summary=args.direction_summary,
                    depth=args.depth,
                )
            elif args.command == "candidates":
                payload = record_candidates(
                    **common,
                    proposal_id=args.proposal_id,
                    candidates=_json_array(args.candidates_json, "candidates-json"),
                    recommendation_id=args.recommendation_id,
                    recommendation=_json_value(args.recommendation_json, "recommendation-json"),
                    evidence=args.evidence,
                    single_candidate_reason=args.single_candidate_reason,
                )
            elif args.command == "record-agent":
                payload = record_discovery_assignment(
                    **common,
                    assignment_id=args.assignment_id,
                    runtime_agent_id=args.runtime_agent_id,
                    role=args.role,
                    task=args.task,
                    read_scope=args.read_scope,
                )
            elif args.command == "revise-discovery":
                payload = revise_discovery(
                    **common,
                    proposal_id=args.proposal_id,
                    reason=args.reason,
                    depth=args.depth,
                )
            elif args.command == "update-agent":
                payload = update_discovery_assignment(
                    **common,
                    assignment_id=args.assignment_id,
                    status=args.status,
                    evidence=args.evidence,
                )
            elif args.command == "select":
                payload = select_candidate(
                    **common,
                    proposal_id=args.proposal_id,
                    candidate_id=args.candidate_id,
                    authority=args.authority,
                    authorization_ref=args.authorization_ref,
                    decision_id=args.decision_id,
                    rationale=args.rationale,
                    nonselected_status=args.nonselected_status,
                )
            elif args.command == "autonomy":
                payload = update_autonomy(
                    **common,
                    strategic=args.strategic,
                    authorization_ref=args.authorization_ref,
                )
            elif args.command == "open-pivot":
                payload = open_pivot(
                    **common,
                    proposal_id=args.proposal_id,
                    summary=args.summary,
                    candidates=_json_array(args.candidates_json, "candidates-json"),
                    recommendation_id=args.recommendation_id,
                    recommendation=_json_value(args.recommendation_json, "recommendation-json"),
                    evidence=args.evidence,
                    affected_agent_ids=args.affected_agent_id,
                    single_candidate_reason=args.single_candidate_reason,
                )
            elif args.command == "open-executive":
                payload = open_executive_gate(
                    **common,
                    proposal_id=args.proposal_id,
                    summary=args.summary,
                    action_scope=args.action_scope,
                )
            elif args.command == "approve-executive":
                payload = approve_executive(
                    **common,
                    proposal_id=args.proposal_id,
                    authorization_ref=args.authorization_ref,
                    decision_id=args.decision_id,
                )
            elif args.command == "consume-executive":
                payload = consume_executive(
                    **common,
                    proposal_id=args.proposal_id,
                    decision_id=args.decision_id,
                    action_scope=args.action_scope,
                    execution_ref=args.execution_ref,
                )
            elif args.command == "confirm-canonical":
                payload = confirm_canonical(**common, evidence=args.evidence)
            elif args.command == "confirm-adoption":
                payload = confirm_adoption(**common, evidence=args.evidence)
            elif args.command == "reject-executive":
                payload = reject_executive(
                    **common,
                    proposal_id=args.proposal_id,
                    authorization_ref=args.authorization_ref,
                )
            elif args.command == "complete-state-sync":
                payload = complete_state_sync(**common)
            elif args.command == "resolve-state-sync":
                payload = resolve_state_sync_disposition(
                    **common,
                    agent_id=args.agent_id,
                    disposition=args.disposition,
                    evidence=args.evidence,
                )
            elif args.command == "mark-reported":
                payload = mark_reported(
                    **common,
                    decision_id=args.decision_id,
                    delivery_ref=args.delivery_ref,
                )
            else:  # pragma: no cover - argparse enforces this set.
                raise guard.InvalidState(f"Unsupported command: {args.command}")
        return emit(payload)
    except StrategyPartialCommit as exc:
        return emit(
            {
                "result": "PARTIAL_COMMIT",
                "mode": "RECOVERY_REQUIRED",
                "reason": str(exc),
                "recovery_action": exc.recovery_action,
                "changed_paths": exc.changed_paths,
            },
            EXIT_INVALID,
        )
    except guard.Conflict as exc:
        return emit(
            {
                "result": "CONFLICT",
                "mode": "ADVISOR_OR_RECOVERY_REQUIRED",
                "reason": str(exc),
                "changed_paths": [],
            },
            EXIT_CONFLICT,
        )
    except (guard.InvalidState, OSError, ValueError, TypeError, AttributeError) as exc:
        return emit(
            {
                "result": "INVALID",
                "mode": "READ_ONLY_REQUIRED",
                "reason": str(exc),
                "changed_paths": [],
            },
            EXIT_INVALID,
        )


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Fail-closed control registry for FounderOS Thread bindings.

This helper never creates, reads, sends to, resumes, or archives a Codex Thread.
Those effects must come from real runtime tools.  The helper only validates and
atomically records observed runtime identities while the caller holds the
current ACTIVE FounderOS fence and project write lock.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import secrets
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable

# Do this before importing the sibling helper so validation never leaves cache
# files in the installed Skill.
sys.dont_write_bytecode = True

import supervisor_guard as guard
import decision_state as strategy
import skill_registry as skills_api


REGISTRY_NAME = "THREADS.json"
REGISTRY_LOCK_NAME = ".thread-registry-lock.json"
SCHEMA_VERSION = 1
EXIT_INVALID = 2
EXIT_CONFLICT = 3

CAPABILITY_KEYS = (
    "SUBAGENT_AVAILABLE",
    "THREAD_CREATE_AVAILABLE",
    "THREAD_NAME_AVAILABLE",
    "THREAD_LIST_AVAILABLE",
    "THREAD_READ_AVAILABLE",
    "THREAD_SEND_AVAILABLE",
    "THREAD_RESUME_AVAILABLE",
    "THREAD_ARCHIVE_AVAILABLE",
    "THREAD_INTERRUPT_AVAILABLE",
    "THREAD_FORK_AVAILABLE",
)
CAPABILITY_STATES = {"SUPPORTED", "PARTIAL", "UNSUPPORTED", "UNKNOWN"}
LIFECYCLE_STATES = {
    "CREATED",
    "ACTIVE",
    "WORKING",
    "WAITING",
    "BLOCKED",
    "REVISION_REQUIRED",
    "COMPLETED",
    "ARCHIVED",
    "FAILED",
    "STALE",
    "HANDOFF",
    "INTERRUPTED",
    "RECOVERING",
}
ALLOWED_TRANSITIONS = {
    "CREATED": {"ACTIVE", "FAILED", "ARCHIVED"},
    "ACTIVE": {"WORKING", "WAITING", "BLOCKED", "HANDOFF", "FAILED"},
    "WORKING": {"COMPLETED", "BLOCKED", "INTERRUPTED", "FAILED", "STALE"},
    "COMPLETED": {"WAITING", "REVISION_REQUIRED", "ARCHIVED", "HANDOFF"},
    "REVISION_REQUIRED": {"WORKING", "HANDOFF", "FAILED"},
    "WAITING": {"WORKING", "HANDOFF", "ARCHIVED", "STALE", "BLOCKED"},
    "BLOCKED": {"WORKING", "WAITING", "HANDOFF", "FAILED", "ARCHIVED"},
    "STALE": {"RECOVERING", "HANDOFF", "ARCHIVED", "FAILED"},
    "RECOVERING": {"WAITING", "WORKING", "HANDOFF", "STALE", "FAILED"},
    "INTERRUPTED": {"WAITING", "WORKING", "HANDOFF", "ARCHIVED", "FAILED"},
    "HANDOFF": {"ARCHIVED", "FAILED"},
    "ARCHIVED": {"RECOVERING"},
    "FAILED": {"RECOVERING", "HANDOFF", "ARCHIVED"},
}
TRUSTED_SKILL_STATES = {
    "builtin-or-system",
    "local-reviewed",
    "third-party-audited",
}
THREAD_TYPES = {"persistent", "review", "task", "fork-readonly"}
AGENT_KINDS = {"persistent", "task"}
BINDING_ROLES = {"primary", "candidate", "predecessor", "auxiliary", "historical"}
IDENTITY_QUALITIES = {"stable", "observed", "ephemeral", "unavailable"}
SKILL_SYNC_STATES = {
    "CURRENT",
    "REQUIRED",
    "LEGACY_MIGRATION_REQUIRED",
    "BLOCKED",
}
LEGACY_BUSINESS_CONTEXT_KEYS = (
    "PROJECT",
    "PROJECT_SHA256",
    "ROADMAP",
    "ROADMAP_SHA256",
    "DECISIONS",
    "DECISIONS_SHA256",
)
STRATEGY_CONTEXT_KEYS = (
    "STRATEGY_CONTEXT_REVISION",
    "STRATEGY_CONTEXT_SHA256",
)
BUSINESS_CONTEXT_KEYS = LEGACY_BUSINESS_CONTEXT_KEYS + STRATEGY_CONTEXT_KEYS


class RegistryPartialCommit(guard.PartialCommit):
    """A registry mutation cannot be safely presented as committed."""


def _text(value: Any, label: str, *, max_length: int = 512) -> str:
    return guard.require_nonempty_text(value, label, max_length=max_length)


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


def _runtime_id(value: Any, label: str) -> str:
    # Runtime IDs are opaque scalar values.  They are never paths, shell text,
    # or selectors by title.
    return _identifier(value, label, max_length=256)


def _sha_or_absent(value: str, label: str) -> str:
    normalized = _text(value, label).upper()
    if normalized != "ABSENT" and not re.fullmatch(r"[0-9A-F]{64}", normalized):
        raise guard.InvalidState(f"{label} must be ABSENT or a SHA-256 value")
    return normalized


def _project_binding_id(root: Path) -> str:
    normalized = os.path.normcase(str(root)).replace("\\", "/")
    material = f"founder-os-thread-binding-v1\0{normalized}".encode("utf-8")
    return hashlib.sha256(material).hexdigest().upper()


def _context_baseline(founder: Path) -> dict[str, str | None]:
    current = guard.read_source_revisions(founder)
    keys = (
        BUSINESS_CONTEXT_KEYS
        if current.get("STRATEGY_CONTEXT_REVISION") is not None
        else LEGACY_BUSINESS_CONTEXT_KEYS
    )
    return {key: current.get(key) for key in keys}


def _baseline_matches(
    baseline: Any, current: dict[str, str | None]
) -> bool:
    if not isinstance(baseline, dict):
        return False
    if set(baseline) == set(LEGACY_BUSINESS_CONTEXT_KEYS):
        return (
            set(current) == set(LEGACY_BUSINESS_CONTEXT_KEYS)
            and baseline == current
        )
    return set(baseline) == set(BUSINESS_CONTEXT_KEYS) and baseline == current


def _thread_strategy_scope(thread: dict[str, Any]) -> str:
    value = thread.get("strategy_scope", "candidate-bound")
    if value not in strategy.THREAD_STRATEGY_SCOPES:
        raise guard.InvalidState("Thread strategy_scope is invalid")
    return value


def _validate_scope(values: Any, label: str) -> list[str]:
    if not isinstance(values, list):
        raise guard.InvalidState(f"{label} must be a list")
    normalized: list[str] = []
    for index, value in enumerate(values):
        item = _text(value, f"{label}[{index}]", max_length=256).replace("\\", "/")
        if (
            item.startswith("/")
            or item.startswith("//")
            or re.match(r"^[A-Za-z]:", item)
            or any(part == ".." for part in item.split("/"))
        ):
            raise guard.InvalidState(f"{label}[{index}] must stay project-relative")
        normalized.append(item)
    if len(normalized) != len(set(item.casefold() for item in normalized)):
        raise guard.InvalidState(f"{label} contains duplicates")
    return normalized


def _scope_is_provable_subset(requested: list[str], ceiling: list[str]) -> bool:
    """Return whether every requested glob is provably inside a Thread ceiling.

    Scope syntax is intentionally conservative.  Equality is always safe.  A
    ceiling ending in ``/**`` proves containment for the same base and any
    descendant pattern.  Other wildcard relationships are rejected unless
    exactly equal because their language inclusion is ambiguous without a
    single canonical glob engine.
    """

    for child in requested:
        child_key = child.casefold().rstrip("/")
        allowed = False
        for parent in ceiling:
            parent_key = parent.casefold().rstrip("/")
            if child_key == parent_key:
                allowed = True
                break
            if parent_key == "**":
                allowed = True
                break
            if parent_key.endswith("/**"):
                base = parent_key[:-3].rstrip("/")
                if base and (child_key == base or child_key.startswith(base + "/")):
                    allowed = True
                    break
        if not allowed:
            return False
    return True


def _validate_dependencies(values: Any) -> list[str]:
    if not isinstance(values, list):
        raise guard.InvalidState("dependencies must be a list")
    result = [_identifier(item, "dependency", max_length=128) for item in values]
    if len(result) != len(set(result)):
        raise guard.InvalidState("dependencies contains duplicates")
    return result


def _direct_file(path: Path, label: str) -> None:
    metadata = path.lstat()
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if (
        stat.S_ISLNK(metadata.st_mode)
        or bool(attributes & reparse_flag)
        or not path.is_file()
        or metadata.st_nlink != 1
    ):
        raise guard.InvalidState(f"{label} must be a direct single-link file: {path}")


def _read_registry(path: Path) -> tuple[str, bytes | None, dict[str, Any] | None]:
    if not path.exists():
        return "ABSENT", None, None
    _direct_file(path, "Thread registry")
    raw, value = guard.read_json_object(path)
    return guard.sha256_bytes(raw), raw, value


def _atomic_replace_bytes(path: Path, content: bytes) -> None:
    descriptor, staging_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".staging", dir=path.parent
    )
    staging = Path(staging_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(staging, path)
    except Exception:
        try:
            staging.unlink()
        except OSError:
            pass
        raise


def _new_registry_revision() -> str:
    return guard.new_revision("TR")


def _new_thread_record_id() -> str:
    return f"THR-{secrets.token_hex(8)}"


def _new_binding_nonce() -> str:
    return f"B_{secrets.token_urlsafe(18)}"


def _capability_values(value: Any) -> dict[str, str]:
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise guard.InvalidState("capabilities must be an object")
    unknown = set(value).difference(CAPABILITY_KEYS)
    if unknown:
        raise guard.InvalidState(f"Unknown capability keys: {sorted(unknown)}")
    result: dict[str, str] = {}
    for key in CAPABILITY_KEYS:
        state = value.get(key, "UNKNOWN")
        if state not in CAPABILITY_STATES:
            raise guard.InvalidState(f"Invalid capability state for {key}")
        result[key] = state
    return result


def _skill_rows(founder: Path) -> dict[str, str]:
    path = founder / "SKILLS.md"
    if not path.exists():
        return {}
    _direct_file(path, "Skill registry")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise guard.InvalidState(f"Cannot read trusted Skill registry: {exc}") from exc
    rows: dict[str, str] = {}
    for line in lines:
        if not line.lstrip().startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if not cells or not cells[0] or cells[0].casefold() == "skill":
            continue
        for trust_state in TRUSTED_SKILL_STATES | {"third-party-unreviewed", "rejected"}:
            if trust_state in cells:
                rows[cells[0]] = trust_state
                break
    return rows


def _resolve_skills(
    founder: Path,
    requested: list[str],
    *,
    agent_id: str | None = None,
    workstream: str | None = None,
    thread_record_id: str | None = None,
    task_id: str | None = None,
) -> list[dict[str, str]]:
    names = [_identifier(item, "skill", max_length=128) for item in requested]
    if len(names) != len(set(name.casefold() for name in names)):
        raise guard.InvalidState("skills contains duplicates")
    if not names:
        return []
    if (founder / skills_api.LOCK_NAME).exists():
        _baseline, bound, _binding_sha = skills_api.resolve_bindings(
            founder,
            names,
            agent_id=agent_id,
            workstream=workstream,
            thread_record_id=thread_record_id,
            task_id=task_id,
        )
        return [
            {
                "name": item["skill_id"],
                "trust_state": item["trust_level"],
                "evidence_ref": ".founder/SKILL_LOCK.json",
            }
            for item in bound
        ]
    rows = _skill_rows(founder)
    if not rows:
        raise guard.Conflict("SKILL_REGISTRY_UNAVAILABLE: cannot bind requested Skills")
    bindings: list[dict[str, str]] = []
    for name in names:
        trust_state = rows.get(name)
        if trust_state not in TRUSTED_SKILL_STATES:
            raise guard.Conflict(f"Skill is missing or untrusted: {name}")
        bindings.append(
            {"name": name, "trust_state": trust_state, "evidence_ref": ".founder/SKILLS.md"}
        )
    return bindings


def _validate_skill_bindings(value: Any) -> None:
    if not isinstance(value, list):
        raise guard.InvalidState("thread skills must be a list")
    seen: set[str] = set()
    for binding in value:
        if not isinstance(binding, dict):
            raise guard.InvalidState("thread skill binding must be an object")
        name = _identifier(binding.get("name"), "skill name", max_length=128)
        if name.casefold() in seen:
            raise guard.InvalidState("thread skill bindings contain duplicates")
        seen.add(name.casefold())
        if binding.get("trust_state") not in TRUSTED_SKILL_STATES:
            raise guard.InvalidState("thread skill binding is not trusted")
        _text(binding.get("evidence_ref"), "skill evidence_ref", max_length=256)


def _validate_bound_skills(value: Any) -> None:
    if not isinstance(value, list):
        raise guard.InvalidState("bound_skills must be a list")
    seen: set[str] = set()
    required = {
        "skill_id",
        "approved_version",
        "commit_sha",
        "content_hash",
        "installed_hash",
        "audit_revision",
        "entry_revision",
        "trust_level",
        "risk_level",
        "status",
        "role",
        "capabilities",
        "binding_sha256",
    }
    for item in value:
        if not isinstance(item, dict) or set(item) != required:
            raise guard.InvalidState("bound skill exact fields are malformed")
        skill_id = _identifier(item.get("skill_id"), "bound skill_id")
        if skill_id.casefold() in seen:
            raise guard.InvalidState("bound_skills contains duplicates")
        seen.add(skill_id.casefold())
        _text(item.get("approved_version"), "bound approved_version")
        commit = item.get("commit_sha")
        if commit is not None and not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise guard.InvalidState("bound commit_sha must be null or exact 40-hex")
        for key in ("content_hash", "installed_hash", "binding_sha256"):
            value_sha = _text(item.get(key), f"bound {key}").upper()
            if not re.fullmatch(r"[0-9A-F]{64}", value_sha):
                raise guard.InvalidState(f"bound {key} is malformed")
        _identifier(item.get("audit_revision"), "bound audit_revision")
        _identifier(item.get("entry_revision"), "bound entry_revision")
        if item.get("trust_level") not in TRUSTED_SKILL_STATES:
            raise guard.InvalidState("bound Skill is not trusted")
        if item.get("risk_level") not in skills_api.RISK_LEVELS:
            raise guard.InvalidState("bound Skill risk is invalid")
        if item.get("status") not in skills_api.BINDABLE_STATUSES:
            raise guard.InvalidState("bound Skill status is not bindable")
        if item.get("role") not in skills_api.SKILL_ROLES:
            raise guard.InvalidState("bound Skill role is invalid")
        _validate_dependencies(item.get("capabilities"))


def _validate_optional_skill_state(thread: dict[str, Any]) -> None:
    fields = {
        "capability_baseline",
        "skill_registry_revision",
        "skill_lock_revision",
        "skill_lock_sha256",
        "bound_skills",
        "bound_skills_sha256",
        "skill_sync_state",
        "last_skill_sync",
        "replacement_needed",
    }
    present = fields.intersection(thread)
    if not present:
        return
    if present != fields:
        raise guard.InvalidState("Thread machine Skill baseline is incomplete")
    _validate_dependencies(thread.get("capability_baseline"))
    _identifier(thread.get("skill_registry_revision"), "skill_registry_revision")
    _identifier(thread.get("skill_lock_revision"), "skill_lock_revision")
    for key in ("skill_lock_sha256", "bound_skills_sha256"):
        value = _text(thread.get(key), key).upper()
        if not re.fullmatch(r"[0-9A-F]{64}", value):
            raise guard.InvalidState(f"Thread {key} is malformed")
    _validate_bound_skills(thread.get("bound_skills"))
    if thread.get("skill_sync_state") not in SKILL_SYNC_STATES:
        raise guard.InvalidState("Thread skill_sync_state is invalid")
    _validate_dependencies(thread.get("replacement_needed"))
    sync = thread.get("last_skill_sync")
    if sync is not None:
        if not isinstance(sync, dict) or set(sync) != {
            "at",
            "acknowledgement",
            "diff",
            "diff_sha256",
            "task_id",
        }:
            raise guard.InvalidState("Thread last_skill_sync is malformed")
        _text(sync.get("at"), "Skill Sync at")
        _text(sync.get("acknowledgement"), "Skill Sync acknowledgement", max_length=4096)
        if not isinstance(sync.get("diff"), dict):
            raise guard.InvalidState("Skill Sync diff must be an object")
        diff_sha = _text(sync.get("diff_sha256"), "Skill Sync diff SHA").upper()
        if not re.fullmatch(r"[0-9A-F]{64}", diff_sha):
            raise guard.InvalidState("Skill Sync diff SHA is malformed")
        if sync.get("task_id") is not None:
            _identifier(sync.get("task_id"), "Skill Sync task_id")


def _find_thread(registry: dict[str, Any], thread_record_id: str) -> dict[str, Any]:
    thread_record_id = _identifier(thread_record_id, "thread_record_id")
    for thread in registry["threads"]:
        if thread["thread_record_id"] == thread_record_id:
            return thread
    raise guard.Conflict(f"Unknown thread_record_id: {thread_record_id}")


def _find_binding(registry: dict[str, Any], agent_id: str) -> dict[str, Any]:
    agent_id = _agent_id(agent_id)
    try:
        return registry["agent_bindings"][agent_id]
    except KeyError as exc:
        raise guard.Conflict(f"Unknown agent_id: {agent_id}") from exc


def _scope_allows_thread(
    entry: dict[str, Any],
    thread: dict[str, Any],
    *,
    task_id: str | None,
) -> tuple[bool, bool]:
    """Return (allowed, explicit_bind_intent) from recorded scope facts.

    Every populated scope list is an authorization ceiling.  Only an exact
    Thread-record or task match is positive intent to add a previously unbound
    Skill; matching an Agent or Workstream ceiling alone never expands an
    existing Thread's Skill set.
    """

    scope = entry["scoped_bindings"]
    values = {
        "agent_ids": thread["agent_id"],
        "workstreams": thread["workstream"],
        "thread_record_ids": thread["thread_record_id"],
        "task_ids": task_id,
    }
    targeted = False
    for key, actual in values.items():
        allowed = scope[key]
        if not allowed:
            continue
        if actual is None or actual not in allowed:
            return False, targeted
        if key in {"thread_record_ids", "task_ids"}:
            targeted = True
    return True, targeted


def _skill_diff(
    old_bound: list[dict[str, Any]],
    desired_bound: list[dict[str, Any]],
    lock_skills: dict[str, dict[str, Any]],
) -> dict[str, list[str]]:
    old = {item["skill_id"]: item for item in old_bound}
    new = {item["skill_id"]: item for item in desired_bound}
    result: dict[str, list[str]] = {
        "ADDED": [],
        "REMOVED": [],
        "UPDATED": [],
        "REVOKED": [],
        "POLICY_CHANGED": [],
    }
    version_fields = {
        "approved_version",
        "commit_sha",
        "content_hash",
        "installed_hash",
        "audit_revision",
        "entry_revision",
    }
    for skill_id in sorted(set(old) | set(new), key=str.casefold):
        if skill_id not in old:
            result["ADDED"].append(skill_id)
            continue
        if skill_id not in new:
            entry = lock_skills.get(skill_id)
            if entry is not None and entry.get("status") in skills_api.FAIL_CLOSED_STATUSES:
                result["REVOKED"].append(skill_id)
            else:
                result["REMOVED"].append(skill_id)
            continue
        if old[skill_id] == new[skill_id]:
            continue
        if any(old[skill_id].get(key) != new[skill_id].get(key) for key in version_fields):
            result["UPDATED"].append(skill_id)
        else:
            result["POLICY_CHANGED"].append(skill_id)
    return result


def _diff_sha(diff: dict[str, list[str]]) -> str:
    return guard.sha256_bytes(guard.canonical_json_bytes(diff))


def _require_exact_skill_sync_ack(
    acknowledgement: str, expected: dict[str, str]
) -> None:
    """Accept only one exact, contradiction-free SKILL_SYNC marker set.

    This is deliberately a small machine protocol rather than a substring
    search through free-form prose.  Prefix/suffix tricks, duplicate keys,
    unknown keys, missing keys, and contradictory values all fail closed.
    """

    prefix = "SKILL_SYNC "
    if not acknowledgement.startswith(prefix):
        raise guard.Conflict(
            "SKILL_SYNC acknowledgement must start with the exact SKILL_SYNC protocol prefix"
        )
    payload = acknowledgement[len(prefix) :]
    if not payload or payload != payload.strip():
        raise guard.Conflict("SKILL_SYNC acknowledgement framing is malformed")
    tokens = payload.split(" ")
    if any(not token or "\t" in token or "\r" in token or "\n" in token for token in tokens):
        raise guard.Conflict("SKILL_SYNC acknowledgement tokens are malformed")
    observed: dict[str, str] = {}
    for token in tokens:
        if token.count("=") != 1:
            raise guard.Conflict("SKILL_SYNC acknowledgement marker is malformed")
        key, value = token.split("=", 1)
        if not key or not value or key in observed:
            raise guard.Conflict(
                "SKILL_SYNC acknowledgement contains an empty or duplicate marker"
            )
        observed[key] = value
    if observed != expected or len(tokens) != len(expected):
        raise guard.Conflict(
            "SKILL_SYNC acknowledgement must bind the exact Thread, Registry, Lock, bound Skills, and diff hashes"
        )


def _state_sync_ack_markers(
    thread: dict[str, Any], current_context: dict[str, str | None]
) -> dict[str, str]:
    """Build the exact machine markers for one live Thread STATE_SYNC."""

    runtime = thread.get("runtime")
    if not isinstance(runtime, dict):
        raise guard.InvalidState("Thread runtime binding is malformed")
    runtime_thread_id = runtime.get("thread_id")
    runtime_host_id = runtime.get("host_id")
    if runtime_thread_id is None or runtime_host_id is None:
        raise guard.Conflict("STATE_SYNC requires one exact bound runtime Thread")
    context_revision = current_context.get("STRATEGY_CONTEXT_REVISION")
    context_sha = current_context.get("STRATEGY_CONTEXT_SHA256")
    if context_revision is None or context_sha is None:
        raise guard.Conflict(
            "LEGACY_MIGRATION_REQUIRED: STATE_SYNC requires initialized Strategy context"
        )
    return {
        "THREAD_RECORD_ID": _identifier(
            thread.get("thread_record_id"), "STATE_SYNC thread_record_id"
        ),
        "BINDING_GENERATION": str(thread.get("generation")),
        "RUNTIME_THREAD_ID": _runtime_id(
            runtime_thread_id, "STATE_SYNC runtime_thread_id"
        ),
        "RUNTIME_HOST_ID": _runtime_id(
            runtime_host_id, "STATE_SYNC runtime_host_id"
        ),
        "AGENT_ID": _agent_id(thread.get("agent_id")),
        "STRATEGY_CONTEXT_REVISION": _identifier(
            context_revision, "STATE_SYNC Strategy context revision"
        ),
        "STRATEGY_CONTEXT_SHA256": _sha_or_absent(
            context_sha, "STATE_SYNC Strategy context SHA-256"
        ),
        "CONTEXT_BASELINE_SHA256": guard.sha256_bytes(
            guard.canonical_json_bytes(current_context)
        ),
    }


def skill_sync_plan(
    founder: Path,
    thread: dict[str, Any],
    *,
    task_id: str | None = None,
    _allow_unbound_prebind: bool = False,
) -> dict[str, Any]:
    """Derive a deterministic per-Thread Skill baseline and exact ACK markers."""

    has_machine_baseline = "bound_skills" in thread
    old_bound = copy.deepcopy(thread.get("bound_skills", []))
    old_requested = [item["skill_id"] for item in old_bound]
    runtime = thread.get("runtime")
    runtime_thread_id = runtime.get("thread_id") if isinstance(runtime, dict) else None
    runtime_host_id = runtime.get("host_id") if isinstance(runtime, dict) else None
    if (
        not _allow_unbound_prebind
        and (not runtime_thread_id or not runtime_host_id)
    ):
        return {
            "state": "BLOCKED",
            "reason": "UNBOUND_RUNTIME",
            "diff": {
                key: []
                for key in (
                    "ADDED",
                    "REMOVED",
                    "UPDATED",
                    "REVOKED",
                    "POLICY_CHANGED",
                )
            },
        }
    transaction = skills_api._transaction_observation(founder)
    if transaction["state"] != "none":
        return {
            "state": "BLOCKED",
            "reason": "SKILL_REGISTRY_RECOVERY_REQUIRED",
            "diff": {key: [] for key in ("ADDED", "REMOVED", "UPDATED", "REVOKED", "POLICY_CHANGED")},
        }
    try:
        lock_sha, _raw, lock, _registry_raw = skills_api.read_registry_pair(founder)
    except guard.GuardError as exc:
        return {
            "state": "BLOCKED",
            "reason": str(exc),
            "diff": {key: [] for key in ("ADDED", "REMOVED", "UPDATED", "REVOKED", "POLICY_CHANGED")},
        }
    if lock is None:
        if has_machine_baseline:
            return {
                "state": "BLOCKED",
                "reason": "SKILL_LOCK_MISSING",
                "diff": {key: [] for key in ("ADDED", "REMOVED", "UPDATED", "REVOKED", "POLICY_CHANGED")},
            }
        if thread.get("skills"):
            return {
                "state": "CURRENT",
                "reason": "LEGACY_SKILLS_MD_BINDING",
                "legacy": True,
                "diff": {key: [] for key in ("ADDED", "REMOVED", "UPDATED", "REVOKED", "POLICY_CHANGED")},
            }
        return {
            "state": "CURRENT",
            "reason": "NO_SKILLS_BOUND",
            "legacy": True,
            "diff": {key: [] for key in ("ADDED", "REMOVED", "UPDATED", "REVOKED", "POLICY_CHANGED")},
        }
    if not has_machine_baseline and thread.get("skills"):
        return {
            "state": "LEGACY_MIGRATION_REQUIRED",
            "reason": "AUTHORITATIVE_SKILL_LOCK_NOW_EXISTS",
            "diff": {key: [] for key in ("ADDED", "REMOVED", "UPDATED", "REVOKED", "POLICY_CHANGED")},
        }
    desired_ids: list[str] = []
    current_ids = set(old_requested)
    for skill_id, entry in lock["skills"].items():
        allowed, targeted = _scope_allows_thread(entry, thread, task_id=task_id)
        if not allowed:
            continue
        if skill_id in current_ids or targeted:
            if entry["status"] in skills_api.BINDABLE_STATUSES:
                desired_ids.append(skill_id)
    baseline, desired_bound, desired_sha = skills_api.resolve_bindings(
        founder,
        desired_ids,
        agent_id=thread["agent_id"],
        workstream=thread["workstream"],
        thread_record_id=thread["thread_record_id"],
        task_id=task_id,
    )
    diff = _skill_diff(old_bound, desired_bound, lock["skills"])
    diff_sha = _diff_sha(diff)
    changed = any(diff.values())
    old_required = set(thread.get("capability_baseline", []))
    revoked_primary_caps: set[str] = set()
    for item in old_bound:
        current = lock["skills"].get(item["skill_id"])
        if (
            item.get("role") == "PRIMARY"
            and (current is None or current.get("status") in skills_api.FAIL_CLOSED_STATUSES)
        ):
            revoked_primary_caps.update(item.get("capabilities", []))
    desired_primary_caps = {
        capability
        for item in desired_bound
        if item["role"] == "PRIMARY"
        for capability in item["capabilities"]
    }
    replacement_needed = sorted(
        (
            set(thread.get("replacement_needed", []))
            | (revoked_primary_caps & old_required)
        ).difference(desired_primary_caps)
    )
    state = "REQUIRED" if changed else ("BLOCKED" if replacement_needed else "CURRENT")
    markers = {
        "THREAD_RECORD_ID": thread["thread_record_id"],
        "BINDING_GENERATION": str(thread["generation"]),
        "RUNTIME_THREAD_ID": runtime_thread_id or "UNBOUND",
        "RUNTIME_HOST_ID": runtime_host_id or "UNBOUND",
        "TASK_ID": task_id or "NONE",
        "SKILL_REGISTRY_REVISION": baseline["skill_registry_revision"],
        "SKILL_LOCK_REVISION": baseline["skill_lock_revision"],
        "SKILL_LOCK_SHA256": baseline["skill_lock_sha256"],
        "BOUND_SKILLS_SHA256": desired_sha,
        "SKILL_DIFF_SHA256": diff_sha,
    }
    return {
        "state": state,
        "reason": "SKILL_BASELINE_CHANGED" if changed else "SKILL_BASELINE_CURRENT",
        "baseline": baseline,
        "bound_skills": desired_bound,
        "bound_skills_sha256": desired_sha,
        "diff": diff,
        "diff_sha256": diff_sha,
        "replacement_needed": replacement_needed,
        "ack_markers": markers,
    }


def _assert_skill_current(
    founder: Path,
    thread: dict[str, Any],
    *,
    task_id: str | None = None,
    allow_unbound_prebind: bool = False,
) -> None:
    plan = skill_sync_plan(
        founder,
        thread,
        task_id=task_id,
        _allow_unbound_prebind=allow_unbound_prebind,
    )
    if plan["state"] != "CURRENT":
        reason = plan.get("reason", plan["state"])
        raise guard.Conflict(f"SKILL_SYNC_REQUIRED: {reason}")
    if thread.get("skill_sync_state") == "BLOCKED" or thread.get("replacement_needed"):
        raise guard.Conflict(
            "SKILL_REPLACEMENT_REQUIRED: a revoked PRIMARY capability lacks replacement"
        )


def validate_registry(registry: dict[str, Any], root: Path) -> None:
    if registry.get("schema_version") != SCHEMA_VERSION:
        raise guard.InvalidState("Unsupported or missing Thread Registry schema_version")
    _text(registry.get("registry_revision"), "registry_revision")
    _sha_or_absent(registry.get("previous_registry_sha256"), "previous_registry_sha256")
    _text(registry.get("created_at"), "registry created_at")
    _text(registry.get("updated_at"), "registry updated_at")

    project = registry.get("project_binding")
    if not isinstance(project, dict):
        raise guard.InvalidState("project_binding must be an object")
    stored_root = project.get("project_root")
    if not isinstance(stored_root, str) or not Path(stored_root).is_absolute():
        raise guard.InvalidState("Registry project_root must be absolute")
    try:
        stored_resolved = Path(stored_root).resolve(strict=True)
    except OSError as exc:
        raise guard.InvalidState("Registry project_root cannot be resolved") from exc
    if (
        os.path.normcase(str(stored_resolved)) != os.path.normcase(str(root))
        or os.path.normcase(str(Path(stored_root)))
        != os.path.normcase(str(stored_resolved))
    ):
        raise guard.InvalidState("Registry belongs to a different project")
    expected_binding = _project_binding_id(root)
    if project.get("project_binding_id") != expected_binding:
        raise guard.InvalidState("Registry project_binding_id does not match")

    capabilities = registry.get("capability_observation")
    if not isinstance(capabilities, dict):
        raise guard.InvalidState("capability_observation must be an object")
    _text(capabilities.get("observed_at"), "capability observed_at")
    _text(capabilities.get("observer"), "capability observer")
    _text(capabilities.get("runtime"), "capability runtime")
    _capability_values(capabilities.get("values"))
    evidence = capabilities.get("evidence")
    if not isinstance(evidence, list):
        raise guard.InvalidState("capability evidence must be a list")
    for item in evidence:
        _text(item, "capability evidence item", max_length=256)

    bindings = registry.get("agent_bindings")
    threads = registry.get("threads")
    if not isinstance(bindings, dict) or not isinstance(threads, list):
        raise guard.InvalidState("agent_bindings and threads have invalid types")

    thread_ids: set[str] = set()
    runtime_keys: set[tuple[str, str]] = set()
    primary_by_agent: dict[str, list[str]] = {}
    for thread in threads:
        if not isinstance(thread, dict):
            raise guard.InvalidState("Each Thread Registry entry must be an object")
        record_id = _identifier(thread.get("thread_record_id"), "thread_record_id")
        if record_id in thread_ids:
            raise guard.InvalidState("Duplicate thread_record_id")
        thread_ids.add(record_id)
        _text(thread.get("logical_thread_name"), "logical_thread_name", max_length=128)
        agent_id = _agent_id(thread.get("agent_id"))
        _agent_id(thread.get("manager_agent_id"))
        _text(thread.get("workstream"), "workstream", max_length=128)
        if thread.get("thread_type") not in THREAD_TYPES:
            raise guard.InvalidState("Unknown thread_type")
        _thread_strategy_scope(thread)
        if thread.get("binding_role") not in BINDING_ROLES:
            raise guard.InvalidState("Unknown binding_role")
        if thread.get("lifecycle_state") not in LIFECYCLE_STATES:
            raise guard.InvalidState("Unknown lifecycle_state")
        if not isinstance(thread.get("generation"), int) or thread["generation"] < 1:
            raise guard.InvalidState("Thread generation must be a positive integer")
        if thread.get("project_binding_id") != expected_binding:
            raise guard.InvalidState("Thread entry has a wrong project binding")
        _text(thread.get("binding_nonce"), "binding_nonce")
        _text(thread.get("created_at"), "thread created_at")
        _text(thread.get("last_seen_at"), "thread last_seen_at")
        _validate_scope(thread.get("read_scope"), "read_scope")
        write_scope = _validate_scope(thread.get("write_scope"), "write_scope")
        if thread.get("thread_type") == "fork-readonly" and write_scope:
            raise guard.InvalidState("A fork-readonly Thread may not have write scope")
        _validate_skill_bindings(thread.get("skills"))
        _validate_optional_skill_state(thread)
        _validate_dependencies(thread.get("dependencies"))
        baseline = thread.get("context_baseline")
        if not isinstance(baseline, dict) or frozenset(baseline) not in {
            frozenset(LEGACY_BUSINESS_CONTEXT_KEYS),
            frozenset(BUSINESS_CONTEXT_KEYS),
        }:
            raise guard.InvalidState("Thread context_baseline is malformed")
        runtime = thread.get("runtime")
        if not isinstance(runtime, dict):
            raise guard.InvalidState("Thread runtime binding must be an object")
        runtime_thread_id = runtime.get("thread_id")
        runtime_host_id = runtime.get("host_id")
        if runtime_thread_id is None:
            if runtime_host_id is not None or runtime.get("identity_quality") != "unavailable":
                raise guard.InvalidState("Unbound runtime identity is inconsistent")
            if thread["lifecycle_state"] not in {"CREATED", "FAILED", "ARCHIVED"}:
                raise guard.InvalidState("A working Thread state requires a real runtime identity")
        else:
            runtime_thread_id = _runtime_id(runtime_thread_id, "runtime_thread_id")
            runtime_host_id = _runtime_id(runtime_host_id, "runtime_host_id")
            if runtime.get("identity_quality") not in IDENTITY_QUALITIES - {"unavailable"}:
                raise guard.InvalidState("Bound runtime identity quality is invalid")
            runtime_key = (runtime_host_id, runtime_thread_id)
            if runtime_key in runtime_keys:
                raise guard.InvalidState("Duplicate runtime Thread binding")
            runtime_keys.add(runtime_key)
        archive = thread.get("archive")
        if not isinstance(archive, dict) or not isinstance(archive.get("archived"), bool):
            raise guard.InvalidState("Thread archive state is malformed")
        if archive["archived"] != (thread["lifecycle_state"] == "ARCHIVED"):
            raise guard.InvalidState("Archive flag and lifecycle state disagree")
        handoff = thread.get("handoff")
        if not isinstance(handoff, dict) or handoff.get("state") not in {
            "none",
            "preparing",
            "candidate",
            "superseded",
        }:
            raise guard.InvalidState("Thread handoff state is malformed")
        current_task = thread.get("current_task")
        if current_task is not None and not isinstance(current_task, dict):
            raise guard.InvalidState("current_task must be an object or null")
        if thread["binding_role"] == "predecessor" and thread["lifecycle_state"] != "ARCHIVED":
            raise guard.InvalidState("A handoff predecessor must be archived")
        if thread["binding_role"] == "primary":
            primary_by_agent.setdefault(agent_id, []).append(record_id)

    for key, binding in bindings.items():
        if not isinstance(binding, dict) or _agent_id(key) != _agent_id(binding.get("agent_id")):
            raise guard.InvalidState("agent_bindings key and agent_id disagree")
        if binding.get("agent_kind") not in AGENT_KINDS:
            raise guard.InvalidState("Unknown agent_kind")
        primary = binding.get("primary_thread_record_id")
        history = binding.get("historical_thread_record_ids")
        if primary is not None and _identifier(primary, "primary_thread_record_id") not in thread_ids:
            raise guard.InvalidState("Agent primary Thread does not exist")
        if not isinstance(history, list) or any(
            _identifier(item, "historical_thread_record_id") not in thread_ids for item in history
        ):
            raise guard.InvalidState("Agent Thread history is malformed")
        if len(history) != len(set(history)):
            raise guard.InvalidState("Agent Thread history contains duplicates")
        observed_primaries = primary_by_agent.get(key, [])
        if primary is None and observed_primaries:
            raise guard.InvalidState("Agent has a primary Thread but no primary pointer")
        if primary is not None and observed_primaries != [primary]:
            raise guard.InvalidState("Persistent Agent must have exactly one current primary Thread")
        for record_id in history:
            if _find_thread(registry, record_id)["agent_id"] != key:
                raise guard.InvalidState("Agent history references another Agent")

    reconciliation = registry.get("reconciliation")
    if not isinstance(reconciliation, dict):
        raise guard.InvalidState("reconciliation must be an object")


def inspect_registry(project: str) -> dict[str, Any]:
    root, founder, _created = guard.resolve_project_root(project)
    registry_sha, _raw, registry = _read_registry(founder / REGISTRY_NAME)
    if registry is not None:
        validate_registry(registry, root)
    return {
        "result": "THREAD_REGISTRY_INSPECTED",
        "project_root": str(root),
        "registry_sha": registry_sha,
        "registry": registry,
        "changed_paths": [],
    }


def _acquire_registry_lock(
    path: Path,
    *,
    root: Path,
    owner: str,
    expected_registry_sha: str,
    expected_state_sha: str,
) -> str:
    nonce = f"RL_{secrets.token_urlsafe(16)}"
    value = {
        "project_root": str(root),
        "owner": owner,
        "nonce": nonce,
        "expected_registry_sha": expected_registry_sha,
        "expected_supervisor_state_sha": expected_state_sha,
        "created_at": guard.utc_now(),
    }
    guard._atomic_create(path, value)
    return nonce


def _release_registry_lock(path: Path, *, owner: str, nonce: str) -> None:
    if not path.exists():
        raise guard.Conflict("Thread Registry transaction lock disappeared")
    _direct_file(path, "Thread Registry transaction lock")
    _raw, value = guard.read_json_object(path)
    if value.get("owner") != owner or value.get("nonce") != nonce:
        raise guard.Conflict("Thread Registry transaction lock belongs to another operation")
    path.unlink()


def _mutate_registry(
    project: str,
    *,
    owner: str,
    activation_token: str,
    expected_state_sha: str,
    expected_registry_sha: str,
    operation: str,
    mutate: Callable[[dict[str, Any] | None, Path], tuple[dict[str, Any], dict[str, Any]]],
) -> dict[str, Any]:
    owner = _text(owner, "owner")
    activation_token = _text(activation_token, "activation_token")
    expected_state_sha = _sha_or_absent(expected_state_sha, "expected_state_sha")
    expected_registry_sha = _sha_or_absent(expected_registry_sha, "expected_registry_sha")
    root, founder, _created = guard.resolve_project_root(project)
    fence = guard.verify_fence(
        str(root), owner=owner, activation_token=activation_token
    )
    if fence["state_sha"] != expected_state_sha:
        raise guard.Conflict("Supervisor state CAS mismatch before Thread Registry mutation")

    registry_path = founder / REGISTRY_NAME
    lock_path = founder / REGISTRY_LOCK_NAME
    # Run every deterministic validation before creating the short transaction
    # lock.  Denied Advisor/Worker requests, stale context, invalid lifecycle,
    # duplicate bindings, and CAS mismatches therefore remain byte/metadata
    # stable.  The same checks run again after the atomic lock to close races.
    preflight_registry_sha, _preflight_raw, preflight_registry = _read_registry(
        registry_path
    )
    if preflight_registry_sha != expected_registry_sha:
        raise guard.Conflict(
            f"Thread Registry CAS mismatch: expected {expected_registry_sha}, "
            f"observed {preflight_registry_sha}"
        )
    if preflight_registry is not None:
        validate_registry(preflight_registry, root)
    preflight_next, _preflight_details = mutate(
        copy.deepcopy(preflight_registry), founder
    )
    validate_registry(preflight_next, root)

    lock_nonce = _acquire_registry_lock(
        lock_path,
        root=root,
        owner=owner,
        expected_registry_sha=expected_registry_sha,
        expected_state_sha=expected_state_sha,
    )
    release_transaction_lock = True
    old_raw: bytes | None = None
    try:
        confirmed_state_sha, state_record = guard.state_observation(
            founder / guard.STATE_NAME
        )
        if confirmed_state_sha != expected_state_sha or state_record is None:
            raise guard.Conflict("Supervisor state changed during Thread Registry mutation")
        registry_sha, old_raw, registry = _read_registry(registry_path)
        if registry_sha != expected_registry_sha:
            raise guard.Conflict(
                f"Thread Registry CAS mismatch: expected {expected_registry_sha}, observed {registry_sha}"
            )
        if registry is not None:
            validate_registry(registry, root)
        next_registry, details = mutate(copy.deepcopy(registry), founder)
        now = guard.utc_now()
        next_registry["registry_revision"] = _new_registry_revision()
        next_registry["previous_registry_sha256"] = registry_sha
        next_registry["updated_at"] = now
        validate_registry(next_registry, root)
        next_raw = guard.canonical_json_bytes(next_registry)
        _atomic_replace_bytes(registry_path, next_raw)
        next_registry_sha = guard.sha256_bytes(next_raw)
        try:
            checkpoint = guard.checkpoint_active(
                str(root),
                owner=owner,
                activation_token=activation_token,
                expected_state_sha=expected_state_sha,
            )
        except guard.PartialCommit as exc:
            release_transaction_lock = False
            raise RegistryPartialCommit(
                "Thread Registry changed and Supervisor checkpoint partially committed; "
                "preserve both locks and enter RECOVERY",
                changed_paths=[str(registry_path), str(founder / guard.STATE_NAME), str(founder / guard.LOCK_NAME), str(lock_path)],
                recovery_action="reconcile-thread-registry-checkpoint",
            ) from exc
        except Exception as exc:
            try:
                if old_raw is None:
                    _direct_file(registry_path, "Thread registry rollback target")
                    registry_path.unlink()
                else:
                    _atomic_replace_bytes(registry_path, old_raw)
            except Exception as rollback_exc:
                release_transaction_lock = False
                raise RegistryPartialCommit(
                    "Thread Registry checkpoint failed and rollback was not provable; "
                    "preserve the transaction lock and enter RECOVERY",
                    changed_paths=[str(registry_path), str(lock_path)],
                    recovery_action="reconcile-thread-registry-rollback",
                ) from rollback_exc
            raise exc
        return {
            "result": operation,
            "mode": "ACTIVE",
            "owner": owner,
            "project_root": str(root),
            "registry_revision": next_registry["registry_revision"],
            "registry_sha": next_registry_sha,
            "state_sha": checkpoint["state_sha"],
            "details": details,
            "changed_paths": [str(registry_path), str(founder / guard.STATE_NAME), str(founder / guard.LOCK_NAME)],
        }
    finally:
        if release_transaction_lock:
            try:
                _release_registry_lock(lock_path, owner=owner, nonce=lock_nonce)
            except (guard.GuardError, OSError) as exc:
                raise RegistryPartialCommit(
                    "Thread Registry transaction completed but its lock could not be released; "
                    "do not begin another Registry mutation",
                    changed_paths=[str(lock_path)],
                    recovery_action="clear-thread-registry-lock-after-audit",
                ) from exc


def initialize_registry(
    project: str,
    *,
    owner: str,
    activation_token: str,
    expected_state_sha: str,
    expected_registry_sha: str,
    capabilities: dict[str, str] | None = None,
    evidence: list[str] | None = None,
    runtime: str = "codex-runtime",
) -> dict[str, Any]:
    values = _capability_values(capabilities)
    evidence = evidence or []
    for item in evidence:
        _text(item, "capability evidence item", max_length=256)

    def mutate(registry: dict[str, Any] | None, founder: Path):
        if registry is not None:
            raise guard.Conflict("Thread Registry already exists; recover or reuse it")
        strategy.enforce_thread_action(
            founder,
            operation="registry-init",
            strategy_scope="candidate-bound",
            thread_type="persistent",
            agent_kind="persistent",
            effective_write_scope=[],
        )
        root = founder.parent
        now = guard.utc_now()
        value = {
            "schema_version": SCHEMA_VERSION,
            "registry_revision": _new_registry_revision(),
            "previous_registry_sha256": "ABSENT",
            "created_at": now,
            "updated_at": now,
            "project_binding": {
                "project_root": str(root),
                "project_binding_id": _project_binding_id(root),
                "runtime_project_id": None,
            },
            "capability_observation": {
                "observed_at": now,
                "observer": owner,
                "runtime": _text(runtime, "runtime", max_length=128),
                "values": values,
                "evidence": evidence,
            },
            "agent_bindings": {},
            "threads": [],
            "reconciliation": {
                "last_run_at": None,
                "inventory_complete": None,
                "healthy": [],
                "missing": [],
                "stale": [],
                "archived": [],
                "orphaned_runtime": [],
                "orphaned_registry": [],
                "wrong_project": [],
                "unverified": [],
            },
        }
        return value, {"initialized": True}

    return _mutate_registry(
        project,
        owner=owner,
        activation_token=activation_token,
        expected_state_sha=expected_state_sha,
        expected_registry_sha=expected_registry_sha,
        operation="THREAD_REGISTRY_INITIALIZED",
        mutate=mutate,
    )


def update_capabilities(
    project: str,
    *,
    owner: str,
    activation_token: str,
    expected_state_sha: str,
    expected_registry_sha: str,
    capabilities: dict[str, str],
    evidence: list[str],
    runtime: str = "codex-runtime",
) -> dict[str, Any]:
    values = _capability_values(capabilities)
    evidence = [_text(item, "capability evidence item", max_length=256) for item in evidence]
    runtime = _text(runtime, "runtime", max_length=128)

    def mutate(registry: dict[str, Any] | None, founder: Path):
        if registry is None:
            raise guard.Conflict("Thread Registry does not exist")
        registry["capability_observation"] = {
            "observed_at": guard.utc_now(),
            "observer": owner,
            "runtime": runtime,
            "values": values,
            "evidence": evidence,
        }
        return registry, {"capabilities_updated": True}

    return _mutate_registry(
        project,
        owner=owner,
        activation_token=activation_token,
        expected_state_sha=expected_state_sha,
        expected_registry_sha=expected_registry_sha,
        operation="THREAD_CAPABILITIES_UPDATED",
        mutate=mutate,
    )


def reserve_thread(
    project: str,
    *,
    owner: str,
    activation_token: str,
    expected_state_sha: str,
    expected_registry_sha: str,
    agent_id: str,
    agent_kind: str,
    logical_name: str,
    manager_agent_id: str,
    workstream: str,
    thread_type: str,
    read_scope: list[str],
    write_scope: list[str],
    skills: list[str],
    dependencies: list[str],
    strategy_scope: str = "candidate-bound",
    capabilities: list[str] | None = None,
) -> dict[str, Any]:
    agent_id = _agent_id(agent_id)
    manager_agent_id = _agent_id(manager_agent_id)
    logical_name = _text(logical_name, "logical_thread_name", max_length=128)
    workstream = _text(workstream, "workstream", max_length=128)
    if agent_kind not in AGENT_KINDS:
        raise guard.InvalidState("Unknown agent_kind")
    if thread_type not in THREAD_TYPES:
        raise guard.InvalidState("Unknown thread_type")
    if strategy_scope not in strategy.THREAD_STRATEGY_SCOPES:
        raise guard.InvalidState("Unknown strategy_scope")
    if agent_kind == "persistent" and thread_type != "persistent":
        raise guard.InvalidState("Persistent Agents require thread_type=persistent")
    if thread_type == "fork-readonly" and write_scope:
        raise guard.InvalidState("fork-readonly cannot receive write scope")
    read_scope = _validate_scope(read_scope, "read_scope")
    write_scope = _validate_scope(write_scope, "write_scope")
    dependencies = _validate_dependencies(dependencies)
    capabilities = _validate_dependencies(capabilities or [])

    def mutate(registry: dict[str, Any] | None, founder: Path):
        if registry is None:
            raise guard.Conflict("THREAD_CAPABILITY_UNAVAILABLE: initialize Registry first")
        validate_registry(registry, founder.parent)
        strategy.enforce_thread_action(
            founder,
            operation="reserve",
            strategy_scope=strategy_scope,
            thread_type=thread_type,
            agent_kind=agent_kind,
            effective_write_scope=write_scope,
        )
        existing = registry["agent_bindings"].get(agent_id)
        if existing is not None:
            primary = existing.get("primary_thread_record_id")
            if primary:
                raise guard.Conflict(f"REUSE_EXISTING_PRIMARY:{primary}")
            raise guard.Conflict("Existing Agent history requires explicit resume or handoff")
        record_id = _new_thread_record_id()
        nonce = _new_binding_nonce()
        now = guard.utc_now()
        role = "primary" if agent_kind == "persistent" else "auxiliary"
        machine_skill_state: dict[str, Any] = {}
        if (founder / skills_api.LOCK_NAME).exists():
            skill_baseline, bound_skills, bound_sha = skills_api.resolve_bindings(
                founder,
                skills,
                agent_id=agent_id,
                workstream=workstream,
                thread_record_id=record_id,
            )
            resolved_skills = [
                {
                    "name": item["skill_id"],
                    "trust_state": item["trust_level"],
                    "evidence_ref": ".founder/SKILL_LOCK.json",
                }
                for item in bound_skills
            ]
            bound_capabilities = sorted(
                {
                    capability
                    for item in bound_skills
                    for capability in item["capabilities"]
                }
            )
            machine_skill_state = {
                "capability_baseline": sorted(
                    set(capabilities) | set(bound_capabilities)
                ),
                "skill_registry_revision": skill_baseline["skill_registry_revision"],
                "skill_lock_revision": skill_baseline["skill_lock_revision"],
                "skill_lock_sha256": skill_baseline["skill_lock_sha256"],
                "bound_skills": bound_skills,
                "bound_skills_sha256": bound_sha,
                "skill_sync_state": "CURRENT",
                "last_skill_sync": None,
                "replacement_needed": [],
            }
        else:
            resolved_skills = _resolve_skills(
                founder,
                skills,
                agent_id=agent_id,
                workstream=workstream,
                thread_record_id=record_id,
            )
        thread = {
            "thread_record_id": record_id,
            "logical_thread_name": logical_name,
            "agent_id": agent_id,
            "manager_agent_id": manager_agent_id,
            "workstream": workstream,
            "thread_type": thread_type,
            "strategy_scope": strategy_scope,
            "binding_role": role,
            "generation": 1,
            "project_binding_id": registry["project_binding"]["project_binding_id"],
            "binding_nonce": nonce,
            "lifecycle_state": "CREATED",
            "current_task": None,
            "created_at": now,
            "last_seen_at": now,
            "latest_turn": None,
            "read_scope": read_scope,
            "write_scope": write_scope,
            "skills": resolved_skills,
            "dependencies": dependencies,
            "blocked_reason": None,
            "context_baseline": _context_baseline(founder),
            "last_state_sync": None,
            "runtime": {
                "thread_id": None,
                "host_id": None,
                "identity_quality": "unavailable",
                "last_runtime_status": None,
            },
            "creation": {"status": "reserved", "created_by": owner},
            "handoff": {
                "state": "none",
                "predecessor_thread_record_id": None,
                "successor_thread_record_id": None,
                "summary_ref": None,
            },
            "archive": {"archived": False, "archived_at": None, "reason": None},
        }
        thread.update(machine_skill_state)
        registry["threads"].append(thread)
        registry["agent_bindings"][agent_id] = {
            "agent_id": agent_id,
            "agent_kind": agent_kind,
            "primary_thread_record_id": record_id if role == "primary" else None,
            "historical_thread_record_ids": [record_id],
            "status": "pending-create",
        }
        return registry, {
            "thread_record_id": record_id,
            "binding_nonce": nonce,
            "creation_status": "reserved",
        }

    return _mutate_registry(
        project,
        owner=owner,
        activation_token=activation_token,
        expected_state_sha=expected_state_sha,
        expected_registry_sha=expected_registry_sha,
        operation="THREAD_CREATE_RESERVED",
        mutate=mutate,
    )


def bind_runtime(
    project: str,
    *,
    owner: str,
    activation_token: str,
    expected_state_sha: str,
    expected_registry_sha: str,
    thread_record_id: str,
    binding_nonce: str,
    runtime_thread_id: str,
    runtime_host_id: str,
    identity_quality: str = "observed",
    strategy_scope: str | None = None,
) -> dict[str, Any]:
    thread_record_id = _identifier(thread_record_id, "thread_record_id")
    binding_nonce = _text(binding_nonce, "binding_nonce")
    runtime_thread_id = _runtime_id(runtime_thread_id, "runtime_thread_id")
    runtime_host_id = _runtime_id(runtime_host_id, "runtime_host_id")
    if identity_quality not in IDENTITY_QUALITIES - {"unavailable"}:
        raise guard.InvalidState("Bound Thread identity quality must be stable/observed/ephemeral")
    if strategy_scope is not None and strategy_scope not in strategy.THREAD_STRATEGY_SCOPES:
        raise guard.InvalidState("Unknown bind strategy_scope")

    def mutate(registry: dict[str, Any] | None, founder: Path):
        if registry is None:
            raise guard.Conflict("Thread Registry does not exist")
        thread = _find_thread(registry, thread_record_id)
        binding = _find_binding(registry, thread["agent_id"])
        effective_scope = strategy_scope or _thread_strategy_scope(thread)
        gate_operation = (
            "handoff-bind"
            if thread["binding_role"] == "candidate" and thread["handoff"]["state"] == "candidate"
            else "bind"
        )
        strategy.enforce_thread_action(
            founder,
            operation=gate_operation,
            strategy_scope=effective_scope,
            thread_type=thread["thread_type"],
            agent_kind=binding["agent_kind"],
            effective_write_scope=thread["write_scope"],
        )
        # A reserved Thread has no runtime identity yet by definition.  This
        # internal pre-bind check validates its locked Skill baseline without
        # creating an ACK path; public SKILL_SYNC planning remains blocked
        # until the real runtime identity is bound below.
        _assert_skill_current(
            founder,
            thread,
            allow_unbound_prebind=True,
        )
        if thread["binding_nonce"] != binding_nonce:
            raise guard.Conflict("Thread creation reservation nonce does not match")
        if thread["lifecycle_state"] != "CREATED" or thread["runtime"]["thread_id"] is not None:
            raise guard.Conflict("Thread reservation is no longer bindable")
        for other in registry["threads"]:
            if other["runtime"]["thread_id"] == runtime_thread_id and other["runtime"]["host_id"] == runtime_host_id:
                raise guard.Conflict("Runtime Thread identity is already bound")
        thread["runtime"] = {
            "thread_id": runtime_thread_id,
            "host_id": runtime_host_id,
            "identity_quality": identity_quality,
            "last_runtime_status": "created",
        }
        thread["creation"]["status"] = "THREAD_CREATED"
        thread["lifecycle_state"] = "ACTIVE"
        thread["last_seen_at"] = guard.utc_now()
        binding["status"] = "active"
        return registry, {
            "thread_record_id": thread_record_id,
            "runtime_thread_id": runtime_thread_id,
            "runtime_host_id": runtime_host_id,
            "creation_status": "THREAD_CREATED",
        }

    return _mutate_registry(
        project,
        owner=owner,
        activation_token=activation_token,
        expected_state_sha=expected_state_sha,
        expected_registry_sha=expected_registry_sha,
        operation="RUNTIME_THREAD_BOUND",
        mutate=mutate,
    )


def _transition(thread: dict[str, Any], target: str) -> None:
    if target not in LIFECYCLE_STATES:
        raise guard.InvalidState("Unknown lifecycle target")
    source = thread["lifecycle_state"]
    if target not in ALLOWED_TRANSITIONS[source]:
        raise guard.Conflict(f"Invalid Thread lifecycle transition: {source} -> {target}")
    if thread["binding_role"] == "predecessor" and target not in {"ARCHIVED", "FAILED"}:
        raise guard.Conflict("Handoff predecessor is fenced from further work")
    if target == "ARCHIVED":
        thread["archive"] = {
            "archived": True,
            "archived_at": guard.utc_now(),
            "reason": thread["archive"].get("reason") or "lifecycle transition",
        }
    elif source == "ARCHIVED" and target == "RECOVERING":
        thread["archive"] = {"archived": False, "archived_at": None, "reason": None}
    thread["lifecycle_state"] = target
    thread["last_seen_at"] = guard.utc_now()


def assign_task(
    project: str,
    *,
    owner: str,
    activation_token: str,
    expected_state_sha: str,
    expected_registry_sha: str,
    thread_record_id: str,
    task_id: str,
    summary: str,
    acceptance_ref: str,
    revision: bool = False,
    task_strategy_scope: str | None = None,
    task_write_scope: list[str] | None = None,
) -> dict[str, Any]:
    thread_record_id = _identifier(thread_record_id, "thread_record_id")
    task_id = _identifier(task_id, "task_id")
    summary = _text(summary, "task summary", max_length=1000)
    acceptance_ref = _text(acceptance_ref, "acceptance_ref", max_length=512)
    if task_strategy_scope is not None and task_strategy_scope not in strategy.THREAD_STRATEGY_SCOPES:
        raise guard.InvalidState("Unknown task_strategy_scope")
    if task_write_scope is not None:
        task_write_scope = _validate_scope(task_write_scope, "task_write_scope")

    def mutate(registry: dict[str, Any] | None, founder: Path):
        if registry is None:
            raise guard.Conflict("Thread Registry does not exist")
        thread = _find_thread(registry, thread_record_id)
        binding = _find_binding(registry, thread["agent_id"])
        effective_scope = task_strategy_scope or _thread_strategy_scope(thread)
        effective_write_scope = (
            thread["write_scope"] if task_write_scope is None else task_write_scope
        )
        if not _scope_is_provable_subset(
            effective_write_scope,
            thread["write_scope"],
        ):
            raise guard.Conflict(
                "Task write scope cannot expand the Thread/Agent write scope"
            )
        strategy.enforce_thread_action(
            founder,
            operation="assign",
            strategy_scope=effective_scope,
            thread_type=thread["thread_type"],
            agent_kind=binding["agent_kind"],
            effective_write_scope=effective_write_scope,
        )
        if thread["binding_role"] in {"candidate", "predecessor", "historical"}:
            raise guard.Conflict("Only a current primary/auxiliary Thread can accept normal tasks")
        if thread["runtime"]["thread_id"] is None:
            raise guard.Conflict("THREAD_CAPABILITY_UNAVAILABLE: no real runtime identity")
        if thread["lifecycle_state"] in {"ARCHIVED", "HANDOFF", "STALE", "RECOVERING", "FAILED"}:
            raise guard.Conflict("Thread must be explicitly recovered before ordinary dispatch")
        if not _baseline_matches(thread["context_baseline"], _context_baseline(founder)):
            raise guard.Conflict("STATE_SYNC_REQUIRED: Thread context baseline is stale")
        _assert_skill_current(founder, thread, task_id=task_id)
        if revision:
            if thread["lifecycle_state"] != "REVISION_REQUIRED":
                raise guard.Conflict("Revision dispatch requires REVISION_REQUIRED state")
        elif thread["lifecycle_state"] == "REVISION_REQUIRED":
            raise guard.Conflict("Use a revision dispatch for this Thread")
        if thread["lifecycle_state"] == "WORKING":
            raise guard.Conflict("Thread already has an active task")
        source = thread["lifecycle_state"]
        if "WORKING" not in ALLOWED_TRANSITIONS[source]:
            raise guard.Conflict(f"Thread cannot accept work from {source}")
        thread["lifecycle_state"] = "WORKING"
        thread["current_task"] = {
            "task_id": task_id,
            "summary": summary,
            "acceptance_ref": acceptance_ref,
            "disposition": "revision-dispatched" if revision else "pending-runtime-send",
            "assigned_at": guard.utc_now(),
            "supervisor_record_revision": guard.state_observation(founder / guard.STATE_NAME)[1]["record_revision"],
            "binding_generation": thread["generation"],
            "context_baseline": copy.deepcopy(thread["context_baseline"]),
            "strategy_scope": effective_scope,
            "write_scope": copy.deepcopy(effective_write_scope),
        }
        thread["last_seen_at"] = guard.utc_now()
        return registry, {"thread_record_id": thread_record_id, "task_id": task_id, "reuse": True}

    return _mutate_registry(
        project,
        owner=owner,
        activation_token=activation_token,
        expected_state_sha=expected_state_sha,
        expected_registry_sha=expected_registry_sha,
        operation="TASK_DISPATCH_RECORDED",
        mutate=mutate,
    )


def transition_thread(
    project: str,
    *,
    owner: str,
    activation_token: str,
    expected_state_sha: str,
    expected_registry_sha: str,
    thread_record_id: str,
    target: str,
    evidence: str,
) -> dict[str, Any]:
    thread_record_id = _identifier(thread_record_id, "thread_record_id")
    evidence = _text(evidence, "transition evidence", max_length=1000)

    def mutate(registry: dict[str, Any] | None, founder: Path):
        if registry is None:
            raise guard.Conflict("Thread Registry does not exist")
        thread = _find_thread(registry, thread_record_id)
        _strategy_sha, _strategy_raw, strategy_state = strategy._read_strategy(
            founder / strategy.STRATEGY_NAME
        )
        if strategy_state is not None:
            strategy.validate_strategy(strategy_state, founder.parent)
            strategy._assert_control_plane_current(founder, strategy_state)
        current_task = thread.get("current_task")
        if target == "WAITING" and strategy_state is None:
            raise guard.Conflict(
                "LEGACY_MIGRATION_REQUIRED: a Thread result cannot be accepted before Strategy initialization"
            )
        if target in {"ACTIVE", "ARCHIVED", "RECOVERING", "HANDOFF", "REVISION_REQUIRED"}:
            raise guard.Conflict("Use the dedicated guarded operation for this transition")
        if target == "WORKING":
            _assert_skill_current(founder, thread)
            if strategy_state is not None and strategy_state["gate"]["state"] != "OPERATING":
                if not isinstance(current_task, dict) or not {
                    "task_id",
                    "strategy_scope",
                    "write_scope",
                }.issubset(current_task):
                    raise guard.Conflict(
                        "A non-operating Strategic Gate cannot resume WORKING without an explicit registered task intent"
                    )
                if current_task.get("disposition") not in {
                    "pending-runtime-send",
                    "revision-dispatched",
                }:
                    raise guard.Conflict(
                        "The registered task intent is not in a resumable dispatch state"
                    )
            current_task = current_task or {}
            binding = _find_binding(registry, thread["agent_id"])
            strategy.enforce_thread_action(
                founder,
                operation="assign",
                strategy_scope=current_task.get(
                    "strategy_scope", _thread_strategy_scope(thread)
                ),
                thread_type=thread["thread_type"],
                agent_kind=binding["agent_kind"],
                effective_write_scope=current_task.get(
                    "write_scope", thread["write_scope"]
                ),
            )
        elif target in {"COMPLETED", "WAITING"} and isinstance(current_task, dict):
            _assert_skill_current(
                founder, thread, task_id=current_task.get("task_id")
            )
            strategy_scope = current_task.get(
                "strategy_scope", _thread_strategy_scope(thread)
            )
            if (
                strategy_state is not None
                and strategy_state["gate"]["state"] != "OPERATING"
                and strategy_scope
                not in {"discovery-read-only", "adoption-read-only", "unrelated-read-only"}
                and target == "WAITING"
            ):
                raise guard.Conflict(
                    "A candidate-bound old-strategy result cannot be accepted while a Strategic Gate is open"
                )
            if target == "WAITING":
                if current_task.get("disposition") != "pending-founder-review":
                    raise guard.Conflict(
                        "Only a completed return pending FounderOS review can become accepted"
                    )
                task_baseline = current_task.get("context_baseline")
                if not isinstance(task_baseline, dict) or not _baseline_matches(
                    task_baseline, _context_baseline(founder)
                ):
                    raise guard.Conflict(
                        "STALE_STRATEGY_RESULT: an old task baseline cannot be accepted"
                    )
        _transition(thread, target)
        if target == "COMPLETED" and thread["current_task"] is not None:
            thread["current_task"]["disposition"] = "pending-founder-review"
            thread["current_task"]["runtime_evidence"] = evidence
        elif target == "WAITING" and thread["current_task"] is not None:
            thread["current_task"]["disposition"] = "accepted"
            thread["current_task"]["acceptance_evidence"] = evidence
        elif target == "BLOCKED":
            thread["blocked_reason"] = evidence
            if thread["current_task"] is not None:
                thread["current_task"]["disposition"] = "blocked"
                thread["current_task"]["runtime_evidence"] = evidence
        elif target == "INTERRUPTED" and thread["current_task"] is not None:
            thread["current_task"]["disposition"] = "interrupted"
            thread["current_task"]["runtime_evidence"] = evidence
        elif target == "STALE" and thread["current_task"] is not None:
            thread["current_task"]["disposition"] = "stale"
            thread["current_task"]["runtime_evidence"] = evidence
        elif target == "FAILED" and thread["current_task"] is not None:
            thread["current_task"]["disposition"] = "failed"
            thread["current_task"]["runtime_evidence"] = evidence
        return registry, {"thread_record_id": thread_record_id, "lifecycle_state": target}

    return _mutate_registry(
        project,
        owner=owner,
        activation_token=activation_token,
        expected_state_sha=expected_state_sha,
        expected_registry_sha=expected_registry_sha,
        operation="THREAD_STATE_TRANSITIONED",
        mutate=mutate,
    )


def request_revision(
    project: str,
    *,
    owner: str,
    activation_token: str,
    expected_state_sha: str,
    expected_registry_sha: str,
    thread_record_id: str,
    defects: list[str],
    evidence: str,
    acceptance_criteria: str,
) -> dict[str, Any]:
    thread_record_id = _identifier(thread_record_id, "thread_record_id")
    if not defects:
        raise guard.InvalidState("A revision request needs at least one concrete defect")
    defects = [_text(item, "revision defect", max_length=512) for item in defects]
    evidence = _text(evidence, "revision evidence", max_length=1000)
    acceptance_criteria = _text(acceptance_criteria, "revision acceptance criteria", max_length=1000)

    def mutate(registry: dict[str, Any] | None, founder: Path):
        if registry is None:
            raise guard.Conflict("Thread Registry does not exist")
        thread = _find_thread(registry, thread_record_id)
        if thread["lifecycle_state"] != "COMPLETED":
            raise guard.Conflict("Revision can only be requested from a completed return")
        _transition(thread, "REVISION_REQUIRED")
        assert thread["current_task"] is not None
        thread["current_task"]["disposition"] = "changes-requested"
        thread["current_task"]["revision_request"] = {
            "defects": defects,
            "evidence": evidence,
            "acceptance_criteria": acceptance_criteria,
        }
        return registry, {"thread_record_id": thread_record_id, "defects": len(defects)}

    return _mutate_registry(
        project,
        owner=owner,
        activation_token=activation_token,
        expected_state_sha=expected_state_sha,
        expected_registry_sha=expected_registry_sha,
        operation="THREAD_REVISION_REQUIRED",
        mutate=mutate,
    )


def state_sync(
    project: str,
    *,
    owner: str,
    activation_token: str,
    expected_state_sha: str,
    expected_registry_sha: str,
    thread_record_id: str,
    acknowledgement: str,
) -> dict[str, Any]:
    thread_record_id = _identifier(thread_record_id, "thread_record_id")
    acknowledgement = _text(acknowledgement, "STATE_SYNC acknowledgement", max_length=4096)

    def mutate(registry: dict[str, Any] | None, founder: Path):
        if registry is None:
            raise guard.Conflict("Thread Registry does not exist")
        thread = _find_thread(registry, thread_record_id)
        if thread["lifecycle_state"] in {"ARCHIVED", "HANDOFF", "FAILED"}:
            raise guard.Conflict("Thread cannot accept STATE_SYNC in its current state")
        _strategy_sha, _strategy_raw, strategy_state = strategy._read_strategy(
            founder / strategy.STRATEGY_NAME
        )
        if strategy_state is None:
            raise guard.Conflict(
                "LEGACY_MIGRATION_REQUIRED: Thread STATE_SYNC requires initialized Strategy context"
            )
        if strategy_state is not None and strategy_state["gate"]["state"] == "STATE_SYNC_REQUIRED":
            current_task = thread.get("current_task") or {}
            if thread["lifecycle_state"] == "WORKING" or current_task.get("disposition") in {
                "pending-runtime-send",
                "revision-dispatched",
            }:
                raise guard.Conflict(
                    "Strategic STATE_SYNC requires the old-strategy task to stop before acknowledgement"
                )
        current_context = _context_baseline(founder)
        expected_markers = _state_sync_ack_markers(thread, current_context)
        strategy.validate_state_sync_ack(
            founder,
            agent_id=thread["agent_id"],
            acknowledgement=acknowledgement,
            expected_markers=expected_markers,
        )
        current_task = thread.get("current_task")
        if (
            isinstance(current_task, dict)
            and current_task.get("strategy_scope", _thread_strategy_scope(thread))
            not in {"discovery-read-only", "adoption-read-only", "unrelated-read-only"}
            and not _baseline_matches(current_task.get("context_baseline"), current_context)
        ):
            current_task["pre_state_sync_disposition"] = current_task.get("disposition")
            current_task["disposition"] = "superseded-by-strategy"
            current_task["superseded_at"] = guard.utc_now()
        thread["context_baseline"] = current_context
        thread["last_state_sync"] = {
            "at": guard.utc_now(),
            "acknowledgement": acknowledgement,
        }
        if thread["lifecycle_state"] in {"STALE", "RECOVERING"}:
            thread["lifecycle_state"] = "WAITING"
        thread["last_seen_at"] = guard.utc_now()
        return registry, {"thread_record_id": thread_record_id, "context_current": True}

    return _mutate_registry(
        project,
        owner=owner,
        activation_token=activation_token,
        expected_state_sha=expected_state_sha,
        expected_registry_sha=expected_registry_sha,
        operation="THREAD_STATE_SYNCED",
        mutate=mutate,
    )


def skill_sync(
    project: str,
    *,
    owner: str,
    activation_token: str,
    expected_state_sha: str,
    expected_registry_sha: str,
    thread_record_id: str,
    acknowledgement: str,
    task_id: str | None = None,
) -> dict[str, Any]:
    """Apply one exact, Thread-bound SKILL_SYNC independently of STATE_SYNC."""

    thread_record_id = _identifier(thread_record_id, "thread_record_id")
    acknowledgement = _text(
        acknowledgement, "SKILL_SYNC acknowledgement", max_length=4096
    )
    if task_id is not None:
        task_id = _identifier(task_id, "SKILL_SYNC task_id")

    def mutate(registry: dict[str, Any] | None, founder: Path):
        if registry is None:
            raise guard.Conflict("Thread Registry does not exist")
        thread = _find_thread(registry, thread_record_id)
        if thread["lifecycle_state"] in {"ARCHIVED", "HANDOFF", "FAILED"}:
            raise guard.Conflict("Thread cannot accept SKILL_SYNC in its current state")
        runtime = thread.get("runtime")
        if (
            not isinstance(runtime, dict)
            or not runtime.get("thread_id")
            or not runtime.get("host_id")
        ):
            raise guard.Conflict(
                "SKILL_SYNC is blocked: UNBOUND_RUNTIME requires one exact runtime Thread"
            )
        current_task = thread.get("current_task") or {}
        if thread["lifecycle_state"] == "WORKING" or current_task.get("disposition") in {
            "pending-runtime-send",
            "revision-dispatched",
        }:
            raise guard.Conflict("SKILL_SYNC requires active work to stop first")
        effective_task_id = task_id or (
            current_task.get("task_id") if isinstance(current_task, dict) else None
        )
        plan = skill_sync_plan(
            founder,
            thread,
            task_id=effective_task_id,
        )
        if plan["state"] == "BLOCKED" and "ack_markers" not in plan:
            raise guard.Conflict(f"SKILL_SYNC is blocked: {plan.get('reason')}")
        if "ack_markers" not in plan:
            raise guard.Conflict("SKILL_SYNC requires migration to authoritative SKILL_LOCK")
        _require_exact_skill_sync_ack(acknowledgement, plan["ack_markers"])
        baseline = plan["baseline"]
        thread.update(
            {
                "capability_baseline": sorted(
                    set(thread.get("capability_baseline", []))
                    | {
                        capability
                        for item in plan["bound_skills"]
                        for capability in item["capabilities"]
                    }
                ),
                "skill_registry_revision": baseline["skill_registry_revision"],
                "skill_lock_revision": baseline["skill_lock_revision"],
                "skill_lock_sha256": baseline["skill_lock_sha256"],
                "bound_skills": copy.deepcopy(plan["bound_skills"]),
                "bound_skills_sha256": plan["bound_skills_sha256"],
                "skill_sync_state": (
                    "BLOCKED" if plan["replacement_needed"] else "CURRENT"
                ),
                "replacement_needed": copy.deepcopy(plan["replacement_needed"]),
                "last_skill_sync": {
                    "at": guard.utc_now(),
                    "acknowledgement": acknowledgement,
                    "diff": copy.deepcopy(plan["diff"]),
                    "diff_sha256": plan["diff_sha256"],
                    "task_id": effective_task_id,
                },
            }
        )
        thread["skills"] = [
            {
                "name": item["skill_id"],
                "trust_state": item["trust_level"],
                "evidence_ref": ".founder/SKILL_LOCK.json",
            }
            for item in plan["bound_skills"]
        ]
        thread["last_seen_at"] = guard.utc_now()
        return registry, {
            "thread_record_id": thread_record_id,
            "skill_sync_state": thread["skill_sync_state"],
            "diff": plan["diff"],
            "replacement_needed": plan["replacement_needed"],
        }

    return _mutate_registry(
        project,
        owner=owner,
        activation_token=activation_token,
        expected_state_sha=expected_state_sha,
        expected_registry_sha=expected_registry_sha,
        operation="THREAD_SKILL_SYNCED",
        mutate=mutate,
    )


def archive_thread(
    project: str,
    *,
    owner: str,
    activation_token: str,
    expected_state_sha: str,
    expected_registry_sha: str,
    thread_record_id: str,
    reason: str,
) -> dict[str, Any]:
    thread_record_id = _identifier(thread_record_id, "thread_record_id")
    reason = _text(reason, "archive reason", max_length=1000)

    def mutate(registry: dict[str, Any] | None, founder: Path):
        if registry is None:
            raise guard.Conflict("Thread Registry does not exist")
        thread = _find_thread(registry, thread_record_id)
        if thread["lifecycle_state"] not in {"WAITING", "COMPLETED", "BLOCKED", "INTERRUPTED", "FAILED", "CREATED"}:
            raise guard.Conflict("Active or revision-required Thread cannot be archived")
        thread["archive"]["reason"] = reason
        _transition(thread, "ARCHIVED")
        if thread["binding_role"] == "primary":
            binding = _find_binding(registry, thread["agent_id"])
            binding["primary_thread_record_id"] = None
            binding["status"] = "archived"
            thread["binding_role"] = "historical"
        return registry, {"thread_record_id": thread_record_id, "archived": True}

    return _mutate_registry(
        project,
        owner=owner,
        activation_token=activation_token,
        expected_state_sha=expected_state_sha,
        expected_registry_sha=expected_registry_sha,
        operation="THREAD_ARCHIVED",
        mutate=mutate,
    )


def resume_thread(
    project: str,
    *,
    owner: str,
    activation_token: str,
    expected_state_sha: str,
    expected_registry_sha: str,
    thread_record_id: str,
    runtime_reopen_evidence: str,
) -> dict[str, Any]:
    thread_record_id = _identifier(thread_record_id, "thread_record_id")
    runtime_reopen_evidence = _text(runtime_reopen_evidence, "runtime reopen evidence", max_length=1000)

    def mutate(registry: dict[str, Any] | None, founder: Path):
        if registry is None:
            raise guard.Conflict("Thread Registry does not exist")
        thread = _find_thread(registry, thread_record_id)
        binding = _find_binding(registry, thread["agent_id"])
        strategy.enforce_thread_action(
            founder,
            operation="resume",
            strategy_scope=_thread_strategy_scope(thread),
            thread_type=thread["thread_type"],
            agent_kind=binding["agent_kind"],
            effective_write_scope=[],
        )
        if thread["lifecycle_state"] != "ARCHIVED":
            raise guard.Conflict("Only an archived Thread needs explicit reopen")
        if binding["primary_thread_record_id"] is not None:
            raise guard.Conflict("Agent already has a current primary Thread")
        _transition(thread, "RECOVERING")
        thread["binding_role"] = "primary" if binding["agent_kind"] == "persistent" else "auxiliary"
        if thread["binding_role"] == "primary":
            binding["primary_thread_record_id"] = thread_record_id
        binding["status"] = "recovering"
        thread["runtime"]["last_runtime_status"] = "reopened"
        thread["last_state_sync"] = {
            "at": guard.utc_now(),
            "acknowledgement": runtime_reopen_evidence,
        }
        return registry, {"thread_record_id": thread_record_id, "lifecycle_state": "RECOVERING"}

    return _mutate_registry(
        project,
        owner=owner,
        activation_token=activation_token,
        expected_state_sha=expected_state_sha,
        expected_registry_sha=expected_registry_sha,
        operation="THREAD_REOPEN_RECORDED",
        mutate=mutate,
    )


def begin_handoff(
    project: str,
    *,
    owner: str,
    activation_token: str,
    expected_state_sha: str,
    expected_registry_sha: str,
    predecessor_thread_record_id: str,
    successor_logical_name: str,
    summary_ref: str,
    strategy_scope: str = "candidate-bound",
) -> dict[str, Any]:
    predecessor_thread_record_id = _identifier(
        predecessor_thread_record_id, "predecessor_thread_record_id"
    )
    successor_logical_name = _text(successor_logical_name, "successor logical name", max_length=128)
    summary_ref = _text(summary_ref, "accepted HANDOFF SUMMARY ref", max_length=512)
    if strategy_scope not in strategy.THREAD_STRATEGY_SCOPES:
        raise guard.InvalidState("Unknown handoff strategy_scope")

    def mutate(registry: dict[str, Any] | None, founder: Path):
        if registry is None:
            raise guard.Conflict("Thread Registry does not exist")
        predecessor = _find_thread(registry, predecessor_thread_record_id)
        binding = _find_binding(registry, predecessor["agent_id"])
        strategy.enforce_thread_action(
            founder,
            operation="begin-handoff",
            strategy_scope=strategy_scope,
            thread_type=predecessor["thread_type"],
            agent_kind=binding["agent_kind"],
            effective_write_scope=[],
        )
        if predecessor["binding_role"] != "primary" or binding["primary_thread_record_id"] != predecessor_thread_record_id:
            raise guard.Conflict("Handoff source is not the current primary Thread")
        if predecessor["lifecycle_state"] not in {
            "WAITING", "COMPLETED", "BLOCKED", "REVISION_REQUIRED", "FAILED", "STALE", "INTERRUPTED"
        }:
            raise guard.Conflict("Handoff source must stop active work before handoff")
        if predecessor["current_task"] and predecessor["current_task"].get("disposition") in {
            "pending-runtime-send", "revision-dispatched"
        }:
            raise guard.Conflict("Handoff source still has uncoordinated active work")
        successor_id = _new_thread_record_id()
        nonce = _new_binding_nonce()
        now = guard.utc_now()
        predecessor["lifecycle_state"] = "HANDOFF"
        predecessor["handoff"] = {
            "state": "preparing",
            "predecessor_thread_record_id": predecessor_thread_record_id,
            "successor_thread_record_id": successor_id,
            "summary_ref": summary_ref,
        }
        successor = copy.deepcopy(predecessor)
        successor.update(
            {
                "thread_record_id": successor_id,
                "logical_thread_name": successor_logical_name,
                "binding_role": "candidate",
                "generation": predecessor["generation"] + 1,
                "binding_nonce": nonce,
                "lifecycle_state": "CREATED",
                "current_task": None,
                "created_at": now,
                "last_seen_at": now,
                "latest_turn": None,
                "blocked_reason": None,
                "runtime": {
                    "thread_id": None,
                    "host_id": None,
                    "identity_quality": "unavailable",
                    "last_runtime_status": None,
                },
                "creation": {"status": "reserved", "created_by": owner},
                "handoff": {
                    "state": "candidate",
                    "predecessor_thread_record_id": predecessor_thread_record_id,
                    "successor_thread_record_id": successor_id,
                    "summary_ref": summary_ref,
                },
                "archive": {"archived": False, "archived_at": None, "reason": None},
            }
        )
        registry["threads"].append(successor)
        binding["historical_thread_record_ids"].append(successor_id)
        binding["status"] = "handoff"
        return registry, {
            "predecessor_thread_record_id": predecessor_thread_record_id,
            "successor_thread_record_id": successor_id,
            "binding_nonce": nonce,
            "agent_id": predecessor["agent_id"],
        }

    return _mutate_registry(
        project,
        owner=owner,
        activation_token=activation_token,
        expected_state_sha=expected_state_sha,
        expected_registry_sha=expected_registry_sha,
        operation="THREAD_HANDOFF_RESERVED",
        mutate=mutate,
    )


def complete_handoff(
    project: str,
    *,
    owner: str,
    activation_token: str,
    expected_state_sha: str,
    expected_registry_sha: str,
    predecessor_thread_record_id: str,
    successor_thread_record_id: str,
    successor_acknowledgement: str,
    strategy_scope: str = "candidate-bound",
) -> dict[str, Any]:
    predecessor_thread_record_id = _identifier(predecessor_thread_record_id, "predecessor_thread_record_id")
    successor_thread_record_id = _identifier(successor_thread_record_id, "successor_thread_record_id")
    successor_acknowledgement = _text(successor_acknowledgement, "successor acknowledgement", max_length=1000)
    if strategy_scope not in strategy.THREAD_STRATEGY_SCOPES:
        raise guard.InvalidState("Unknown handoff strategy_scope")

    def mutate(registry: dict[str, Any] | None, founder: Path):
        if registry is None:
            raise guard.Conflict("Thread Registry does not exist")
        predecessor = _find_thread(registry, predecessor_thread_record_id)
        successor = _find_thread(registry, successor_thread_record_id)
        binding = _find_binding(registry, predecessor["agent_id"])
        strategy.enforce_thread_action(
            founder,
            operation="complete-handoff",
            strategy_scope=strategy_scope,
            thread_type=successor["thread_type"],
            agent_kind=binding["agent_kind"],
            effective_write_scope=[],
        )
        if predecessor["agent_id"] != successor["agent_id"]:
            raise guard.Conflict("Thread Handoff cannot change Agent identity")
        if predecessor["lifecycle_state"] != "HANDOFF" or successor["binding_role"] != "candidate":
            raise guard.Conflict("Thread Handoff is not in a cutover-ready state")
        if successor["runtime"]["thread_id"] is None or successor["lifecycle_state"] != "ACTIVE":
            raise guard.Conflict("Successor must confirm a real bound runtime Thread")
        if not _baseline_matches(successor["context_baseline"], _context_baseline(founder)):
            raise guard.Conflict("Successor context is stale before handoff cutover")
        _assert_skill_current(founder, successor)
        if binding["primary_thread_record_id"] != predecessor_thread_record_id:
            raise guard.Conflict("Agent primary changed before handoff cutover")
        predecessor["archive"] = {
            "archived": True,
            "archived_at": guard.utc_now(),
            "reason": "Thread Handoff cutover",
        }
        predecessor["lifecycle_state"] = "ARCHIVED"
        predecessor["binding_role"] = "predecessor"
        predecessor["handoff"]["state"] = "superseded"
        successor["binding_role"] = "primary"
        successor["lifecycle_state"] = "WAITING"
        successor["handoff"]["state"] = "none"
        successor["last_state_sync"] = {
            "at": guard.utc_now(),
            "acknowledgement": successor_acknowledgement,
        }
        binding["primary_thread_record_id"] = successor_thread_record_id
        binding["status"] = "waiting"
        return registry, {
            "agent_id": predecessor["agent_id"],
            "old_primary": predecessor_thread_record_id,
            "new_primary": successor_thread_record_id,
        }

    return _mutate_registry(
        project,
        owner=owner,
        activation_token=activation_token,
        expected_state_sha=expected_state_sha,
        expected_registry_sha=expected_registry_sha,
        operation="THREAD_HANDOFF_COMPLETED",
        mutate=mutate,
    )


def reconcile_runtime_snapshot(
    registry: dict[str, Any],
    root: Path,
    runtime_inventory: list[dict[str, Any]],
    *,
    inventory_complete: bool,
) -> dict[str, list[str]]:
    validate_registry(registry, root)
    observed: dict[tuple[str, str], dict[str, Any]] = {}
    for item in runtime_inventory:
        if not isinstance(item, dict):
            raise guard.InvalidState("Runtime inventory item must be an object")
        thread_id = _runtime_id(item.get("thread_id"), "runtime inventory thread_id")
        host_id = _runtime_id(item.get("host_id"), "runtime inventory host_id")
        key = (host_id, thread_id)
        if key in observed:
            raise guard.InvalidState("Runtime inventory contains duplicate identities")
        observed[key] = item
    result = {
        "healthy": [],
        "missing": [],
        "stale": [],
        "archived": [],
        "orphaned_runtime": [],
        "orphaned_registry": [],
        "wrong_project": [],
        "unverified": [],
    }
    known_runtime: set[tuple[str, str]] = set()
    founder = root / ".founder"
    current_context = _context_baseline(founder)
    for thread in registry["threads"]:
        record_id = thread["thread_record_id"]
        if thread["lifecycle_state"] == "ARCHIVED":
            result["archived"].append(record_id)
            continue
        runtime = thread["runtime"]
        if runtime["thread_id"] is None:
            result["orphaned_registry"].append(record_id)
            continue
        key = (runtime["host_id"], runtime["thread_id"])
        known_runtime.add(key)
        item = observed.get(key)
        if item is None:
            result["missing" if inventory_complete else "unverified"].append(record_id)
        elif item.get("project_binding_id") not in {None, registry["project_binding"]["project_binding_id"]}:
            result["wrong_project"].append(record_id)
        elif not _baseline_matches(thread["context_baseline"], current_context):
            result["stale"].append(record_id)
        else:
            result["healthy"].append(record_id)
    for key, item in observed.items():
        if key in known_runtime:
            continue
        if item.get("project_binding_id") == registry["project_binding"]["project_binding_id"]:
            result["orphaned_runtime"].append(f"{key[0]}:{key[1]}")
    return result


def _json_object(raw: str, label: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise guard.InvalidState(f"{label} must be valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise guard.InvalidState(f"{label} must be a JSON object")
    return value


def _add_mutation_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--project", required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--activation-token", required=True)
    parser.add_argument("--expected-state-sha", required=True)
    parser.add_argument("--expected-registry-sha", required=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    inspect_parser = subparsers.add_parser("inspect")
    inspect_parser.add_argument("--project", required=True)

    init_parser = subparsers.add_parser("init")
    _add_mutation_args(init_parser)
    init_parser.add_argument("--capabilities-json", default="{}")
    init_parser.add_argument("--evidence", action="append", default=[])
    init_parser.add_argument("--runtime", default="codex-runtime")

    capabilities_parser = subparsers.add_parser("capabilities")
    _add_mutation_args(capabilities_parser)
    capabilities_parser.add_argument("--capabilities-json", required=True)
    capabilities_parser.add_argument("--evidence", action="append", default=[])
    capabilities_parser.add_argument("--runtime", default="codex-runtime")

    reserve_parser = subparsers.add_parser("reserve")
    _add_mutation_args(reserve_parser)
    reserve_parser.add_argument("--agent-id", required=True)
    reserve_parser.add_argument("--agent-kind", choices=sorted(AGENT_KINDS), required=True)
    reserve_parser.add_argument("--logical-name", required=True)
    reserve_parser.add_argument("--manager-agent-id", required=True)
    reserve_parser.add_argument("--workstream", required=True)
    reserve_parser.add_argument("--thread-type", choices=sorted(THREAD_TYPES), required=True)
    reserve_parser.add_argument("--read-scope", action="append", default=[])
    reserve_parser.add_argument("--write-scope", action="append", default=[])
    reserve_parser.add_argument("--skill", action="append", default=[])
    reserve_parser.add_argument("--capability", action="append", default=[])
    reserve_parser.add_argument("--depends-on", action="append", default=[])
    reserve_parser.add_argument(
        "--strategy-scope", choices=sorted(strategy.THREAD_STRATEGY_SCOPES), default="candidate-bound"
    )

    bind_parser = subparsers.add_parser("bind")
    _add_mutation_args(bind_parser)
    bind_parser.add_argument("--thread-record-id", required=True)
    bind_parser.add_argument("--binding-nonce", required=True)
    bind_parser.add_argument("--runtime-thread-id", required=True)
    bind_parser.add_argument("--runtime-host-id", required=True)
    bind_parser.add_argument("--identity-quality", choices=sorted(IDENTITY_QUALITIES - {"unavailable"}), default="observed")
    bind_parser.add_argument("--strategy-scope", choices=sorted(strategy.THREAD_STRATEGY_SCOPES))

    assign_parser = subparsers.add_parser("assign")
    _add_mutation_args(assign_parser)
    assign_parser.add_argument("--thread-record-id", required=True)
    assign_parser.add_argument("--task-id", required=True)
    assign_parser.add_argument("--summary", required=True)
    assign_parser.add_argument("--acceptance-ref", required=True)
    assign_parser.add_argument("--revision", action="store_true")
    assign_parser.add_argument("--task-strategy-scope", choices=sorted(strategy.THREAD_STRATEGY_SCOPES))
    assign_parser.add_argument("--task-write-scope", action="append")

    transition_parser = subparsers.add_parser("transition")
    _add_mutation_args(transition_parser)
    transition_parser.add_argument("--thread-record-id", required=True)
    transition_parser.add_argument("--target", choices=sorted(LIFECYCLE_STATES), required=True)
    transition_parser.add_argument("--evidence", required=True)

    revision_parser = subparsers.add_parser("request-revision")
    _add_mutation_args(revision_parser)
    revision_parser.add_argument("--thread-record-id", required=True)
    revision_parser.add_argument("--defect", action="append", required=True)
    revision_parser.add_argument("--evidence", required=True)
    revision_parser.add_argument("--acceptance-criteria", required=True)

    sync_parser = subparsers.add_parser("state-sync")
    _add_mutation_args(sync_parser)
    sync_parser.add_argument("--thread-record-id", required=True)
    sync_parser.add_argument("--acknowledgement", required=True)

    skill_sync_parser = subparsers.add_parser("skill-sync")
    _add_mutation_args(skill_sync_parser)
    skill_sync_parser.add_argument("--thread-record-id", required=True)
    skill_sync_parser.add_argument("--acknowledgement", required=True)
    skill_sync_parser.add_argument("--task-id")

    archive_parser = subparsers.add_parser("archive")
    _add_mutation_args(archive_parser)
    archive_parser.add_argument("--thread-record-id", required=True)
    archive_parser.add_argument("--reason", required=True)

    resume_parser = subparsers.add_parser("resume")
    _add_mutation_args(resume_parser)
    resume_parser.add_argument("--thread-record-id", required=True)
    resume_parser.add_argument("--runtime-reopen-evidence", required=True)

    handoff_parser = subparsers.add_parser("begin-handoff")
    _add_mutation_args(handoff_parser)
    handoff_parser.add_argument("--predecessor-thread-record-id", required=True)
    handoff_parser.add_argument("--successor-logical-name", required=True)
    handoff_parser.add_argument("--summary-ref", required=True)
    handoff_parser.add_argument(
        "--strategy-scope", choices=sorted(strategy.THREAD_STRATEGY_SCOPES), default="candidate-bound"
    )

    cutover_parser = subparsers.add_parser("complete-handoff")
    _add_mutation_args(cutover_parser)
    cutover_parser.add_argument("--predecessor-thread-record-id", required=True)
    cutover_parser.add_argument("--successor-thread-record-id", required=True)
    cutover_parser.add_argument("--successor-acknowledgement", required=True)
    cutover_parser.add_argument(
        "--strategy-scope", choices=sorted(strategy.THREAD_STRATEGY_SCOPES), default="candidate-bound"
    )
    return parser


def emit(payload: dict[str, Any], exit_code: int = 0) -> int:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return exit_code


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "inspect":
            payload = inspect_registry(args.project)
        else:
            common = {
                "project": args.project,
                "owner": args.owner,
                "activation_token": args.activation_token,
                "expected_state_sha": args.expected_state_sha,
                "expected_registry_sha": args.expected_registry_sha,
            }
            if args.command == "init":
                payload = initialize_registry(
                    **common,
                    capabilities=_json_object(args.capabilities_json, "capabilities-json"),
                    evidence=args.evidence,
                    runtime=args.runtime,
                )
            elif args.command == "capabilities":
                payload = update_capabilities(
                    **common,
                    capabilities=_json_object(args.capabilities_json, "capabilities-json"),
                    evidence=args.evidence,
                    runtime=args.runtime,
                )
            elif args.command == "reserve":
                payload = reserve_thread(
                    **common,
                    agent_id=args.agent_id,
                    agent_kind=args.agent_kind,
                    logical_name=args.logical_name,
                    manager_agent_id=args.manager_agent_id,
                    workstream=args.workstream,
                    thread_type=args.thread_type,
                    read_scope=args.read_scope,
                    write_scope=args.write_scope,
                    skills=args.skill,
                    capabilities=args.capability,
                    dependencies=args.depends_on,
                    strategy_scope=args.strategy_scope,
                )
            elif args.command == "bind":
                payload = bind_runtime(
                    **common,
                    thread_record_id=args.thread_record_id,
                    binding_nonce=args.binding_nonce,
                    runtime_thread_id=args.runtime_thread_id,
                    runtime_host_id=args.runtime_host_id,
                    identity_quality=args.identity_quality,
                    strategy_scope=args.strategy_scope,
                )
            elif args.command == "assign":
                payload = assign_task(
                    **common,
                    thread_record_id=args.thread_record_id,
                    task_id=args.task_id,
                    summary=args.summary,
                    acceptance_ref=args.acceptance_ref,
                    revision=args.revision,
                    task_strategy_scope=args.task_strategy_scope,
                    task_write_scope=args.task_write_scope,
                )
            elif args.command == "transition":
                payload = transition_thread(
                    **common,
                    thread_record_id=args.thread_record_id,
                    target=args.target,
                    evidence=args.evidence,
                )
            elif args.command == "request-revision":
                payload = request_revision(
                    **common,
                    thread_record_id=args.thread_record_id,
                    defects=args.defect,
                    evidence=args.evidence,
                    acceptance_criteria=args.acceptance_criteria,
                )
            elif args.command == "state-sync":
                payload = state_sync(
                    **common,
                    thread_record_id=args.thread_record_id,
                    acknowledgement=args.acknowledgement,
                )
            elif args.command == "skill-sync":
                payload = skill_sync(
                    **common,
                    thread_record_id=args.thread_record_id,
                    acknowledgement=args.acknowledgement,
                    task_id=args.task_id,
                )
            elif args.command == "archive":
                payload = archive_thread(
                    **common,
                    thread_record_id=args.thread_record_id,
                    reason=args.reason,
                )
            elif args.command == "resume":
                payload = resume_thread(
                    **common,
                    thread_record_id=args.thread_record_id,
                    runtime_reopen_evidence=args.runtime_reopen_evidence,
                )
            elif args.command == "begin-handoff":
                payload = begin_handoff(
                    **common,
                    predecessor_thread_record_id=args.predecessor_thread_record_id,
                    successor_logical_name=args.successor_logical_name,
                    summary_ref=args.summary_ref,
                    strategy_scope=args.strategy_scope,
                )
            elif args.command == "complete-handoff":
                payload = complete_handoff(
                    **common,
                    predecessor_thread_record_id=args.predecessor_thread_record_id,
                    successor_thread_record_id=args.successor_thread_record_id,
                    successor_acknowledgement=args.successor_acknowledgement,
                    strategy_scope=args.strategy_scope,
                )
            else:  # pragma: no cover - argparse enforces this set.
                raise guard.InvalidState(f"Unsupported command: {args.command}")
        return emit(payload)
    except RegistryPartialCommit as exc:
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

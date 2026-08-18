#!/usr/bin/env python3
"""Project-local, evidence-backed organization memory for FounderOS.

The registry stores bounded, structured outcomes and accepted lessons.  It is
not a transcript store, a vector database, or an authorization mechanism.
Only the current ACTIVE FounderOS supervisor may mutate it while holding the
project write fence; inspect, verify, query, and sync-plan operations are
strictly read-only.
"""

from __future__ import annotations

import argparse
import copy
import ctypes
import hashlib
import json
import os
import re
import secrets
import stat
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

if os.name == "nt":
    from ctypes import wintypes

sys.dont_write_bytecode = True

import decision_state as strategy
import skill_registry as skills_api
import supervisor_guard as guard


MEMORY_DIRECTORY = "memory"
REGISTRY_NAME = "MEMORY.json"
ARCHIVE_DIRECTORY = "archive"
TRANSACTION_LOCK_NAME = ".memory-registry-lock.json"
SCHEMA_VERSION = 1
ARCHIVE_SCHEMA_VERSION = 2
EXIT_INVALID = 2
EXIT_CONFLICT = 3

MAX_REGISTRY_BYTES = 8 * 1024 * 1024
MAX_ARCHIVE_BYTES = 8 * 1024 * 1024
MAX_EVENTS = 5000
MAX_RECORDS_PER_KIND = 5000
MAX_QUERY_LIMIT = 100
MAX_SYNC_RECORDS = 50
MAX_SYNC_BYTES = 64 * 1024
RECENT_ROUTING_WINDOW = 3
MAX_QUERY_BYTES = 256 * 1024
MAX_LIST_ITEMS = 64
MAX_TEXT = 2048

OUTCOMES = {
    "SUCCESS_FIRST_PASS",
    "SUCCESS_AFTER_REVISION",
    "PARTIAL",
    "FAILED",
    "BLOCKED_EXTERNAL",
    "CANCELLED",
    "SUPERSEDED",
    "INVALIDATED_LATER",
}
ATTRIBUTIONS = {
    "AGENT",
    "SKILL",
    "UPSTREAM",
    "COORDINATION",
    "STRATEGY_CHANGE",
    "EXTERNAL",
    "THREAD_CONTEXT",
    "UNKNOWN",
}
REVISION_SEVERITIES = {"NONE", "MINOR", "MAJOR", "REPEATED", "FUNDAMENTAL"}
REVIEW_RESULTS = {"NOT_REQUIRED", "PASSED", "CHANGES_REQUESTED", "FAILED", "UNKNOWN"}
INTEGRATION_RESULTS = {"NOT_REQUIRED", "PASSED", "FAILED", "UNKNOWN"}
ACCEPTANCE_RESULTS = {"ACCEPTED", "ACCEPTED_WITH_LIMITATIONS"}
DECISION_STATUSES = {
    "ACTIVE",
    "VALIDATED",
    "PARTIALLY_VALIDATED",
    "INVALIDATED",
    "SUPERSEDED",
    "RECONSIDERED",
    "UNKNOWN_OUTCOME",
}
LESSON_STATUSES = {"ACTIVE", "STALE", "SUPERSEDED", "INVALIDATED"}
RETENTION_CLASSES = {"PERMANENT", "LONG_TERM", "COMPACTABLE", "TEMPORARY"}
CONFIDENCE_LEVELS = {"LOW", "MEDIUM", "HIGH"}
EVIDENCE_LEVELS = {"CONFIRMED", "INFERRED", "UNKNOWN"}
RISK_LEVELS = {"L0", "L1", "L2", "L3"}
EVENT_KINDS = {
    "TASK_OUTCOME_FINALIZED",
    "OUTCOME_INVALIDATED_LATER",
    "ATTRIBUTION_REVISED",
    "LESSON_ACCEPTED",
    "LESSON_MERGED",
    "LESSON_STATUS_CHANGED",
    "DECISION_OUTCOME_CHANGED",
    "ROUTING_DECISION_RECORDED",
    "MEMORY_RETRACTED",
    "REVIEW_DEBT",
    "THREAD_HEALTH_EVENT",
    "ORGANIZATION_PATTERN_RECORDED",
}
ORGANIZATION_PATTERN_TYPES = {
    "REVIEW_DEBT_HISTORY",
    "THREAD_HEALTH",
    "WORKSTREAM_PATTERN",
    "COORDINATION_LESSON",
}
PUBLIC_RECORD_KINDS = {
    "task_outcomes", "lessons", "decision_outcomes", "routing_history",
    "organization_patterns",
}
INTERNAL_RECORD_KINDS = {"task_outcome_locators"}
RECORD_KINDS = PUBLIC_RECORD_KINDS | INTERNAL_RECORD_KINDS
FORBIDDEN_KEYS = {
    "raw_output",
    "transcript",
    "chat",
    "prompt",
    "analysis",
    "reasoning",
    "chain_of_thought",
    "chain-of-thought",
    "scratchpad",
    "hidden_reasoning",
    "self_score",
    "self_rating",
    "performance_score",
    "must_use",
    "api_key",
    "secret",
    "credential",
}
TOP_LEVEL_FIELDS = {
    "schema_version",
    "memory_revision",
    "previous_memory_sha256",
    "project_binding",
    "created_at",
    "updated_at",
    "next_sequence",
    "event_chain_head_sha256",
    "active_events",
    "records",
    "derived",
    "archive_manifest",
    "consumed_founder_receipts",
}

_WIN_GENERIC_READ = 0x80000000
_WIN_FILE_SHARE_READ = 0x00000001
_WIN_FILE_SHARE_WRITE = 0x00000002
_WIN_OPEN_EXISTING = 3
_WIN_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_WIN_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_WIN_FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400

if os.name == "nt":
    class _WinFileTime(ctypes.Structure):
        _fields_ = (("low", wintypes.DWORD), ("high", wintypes.DWORD))


    class _WinHandleInformation(ctypes.Structure):
        _fields_ = (
            ("attributes", wintypes.DWORD),
            ("creation_time", _WinFileTime),
            ("last_access_time", _WinFileTime),
            ("last_write_time", _WinFileTime),
            ("volume_serial", wintypes.DWORD),
            ("size_high", wintypes.DWORD),
            ("size_low", wintypes.DWORD),
            ("link_count", wintypes.DWORD),
            ("file_index_high", wintypes.DWORD),
            ("file_index_low", wintypes.DWORD),
        )


    _KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _KERNEL32.CreateFileW.argtypes = (
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    )
    _KERNEL32.CreateFileW.restype = wintypes.HANDLE
    _KERNEL32.GetFileInformationByHandle.argtypes = (
        wintypes.HANDLE, ctypes.POINTER(_WinHandleInformation),
    )
    _KERNEL32.GetFileInformationByHandle.restype = wintypes.BOOL
    _KERNEL32.CloseHandle.argtypes = (wintypes.HANDLE,)
    _KERNEL32.CloseHandle.restype = wintypes.BOOL
    _INVALID_WIN_HANDLE = ctypes.c_void_p(-1).value


class MemoryPartialCommit(guard.PartialCommit):
    """The Memory transaction cannot be presented as cleanly completed."""


def _text(value: Any, label: str, *, max_length: int = MAX_TEXT) -> str:
    return guard.require_nonempty_text(value, label, max_length=max_length)


def _optional_text(value: Any, label: str, *, max_length: int = MAX_TEXT) -> str | None:
    if value is None:
        return None
    return _text(value, label, max_length=max_length)


def _identifier(value: Any, label: str, *, max_length: int = 128) -> str:
    value = _text(value, label, max_length=max_length)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]*", value):
        raise guard.InvalidState(f"{label} contains an unsafe character")
    return value


def _attribution_subject(kind: Any, value: Any, label: str) -> str | None:
    if value is None:
        return None
    if kind == "AGENT":
        return _identifier(value, label, max_length=128)
    if kind == "SKILL":
        return _text(value, label, max_length=512)
    return _optional_text(value, label, max_length=128)


def _sha_or_absent(value: Any, label: str) -> str:
    value = _text(value, label, max_length=64).upper()
    if value != "ABSENT" and not re.fullmatch(r"[0-9A-F]{64}", value):
        raise guard.InvalidState(f"{label} must be ABSENT or SHA-256")
    return value


def _sha(value: Any, label: str) -> str:
    value = _text(value, label, max_length=64).upper()
    if not re.fullmatch(r"[0-9A-F]{64}", value):
        raise guard.InvalidState(f"{label} must be SHA-256")
    return value


def _timestamp(value: Any, label: str) -> str:
    value = _text(value, label, max_length=64)
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError as exc:
        raise guard.InvalidState(f"{label} must be timezone-aware ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise guard.InvalidState(f"{label} must be timezone-aware ISO-8601")
    return value


def _bounded_list(
    value: Any,
    label: str,
    *,
    item_max: int = 256,
    limit: int = MAX_LIST_ITEMS,
    identifiers: bool = False,
) -> list[str]:
    if not isinstance(value, list) or len(value) > limit:
        raise guard.InvalidState(f"{label} must be a list with at most {limit} items")
    result = [
        (_identifier(item, f"{label} item", max_length=item_max) if identifiers else _text(item, f"{label} item", max_length=item_max))
        for item in value
    ]
    if len(result) != len(set(item.casefold() for item in result)):
        raise guard.InvalidState(f"{label} contains duplicates")
    return result


def _reject_forbidden_payload(value: Any, *, path: str = "payload", depth: int = 0) -> None:
    if depth > 12:
        raise guard.InvalidState("Memory payload exceeds the maximum nesting depth")
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise guard.InvalidState(f"{path} has a non-string key")
            normalized = key.casefold().replace(" ", "_")
            if normalized in FORBIDDEN_KEYS:
                raise guard.InvalidState(f"Memory payload forbids field {key!r}")
            _reject_forbidden_payload(item, path=f"{path}.{key}", depth=depth + 1)
    elif isinstance(value, list):
        if len(value) > MAX_LIST_ITEMS:
            raise guard.InvalidState(f"{path} has too many items")
        for index, item in enumerate(value):
            _reject_forbidden_payload(item, path=f"{path}[{index}]", depth=depth + 1)
    elif isinstance(value, str):
        if len(value) > MAX_TEXT or any(ord(character) < 32 for character in value):
            raise guard.InvalidState(f"{path} contains unsafe or oversized text")
    elif value is not None and not isinstance(value, (bool, int)):
        raise guard.InvalidState(f"{path} contains an unsupported scalar")


def _strict_json_loads(raw: bytes, *, label: str) -> dict[str, Any]:
    if len(raw) > MAX_REGISTRY_BYTES:
        raise guard.InvalidState(f"{label} exceeds the size limit")

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise guard.InvalidState(f"{label} contains duplicate JSON key {key!r}")
            result[key] = value
        return result

    def constant(value: str) -> Any:
        raise guard.InvalidState(f"{label} contains non-finite number {value}")

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise guard.InvalidState(f"{label} is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise guard.InvalidState(f"{label} must be a JSON object")
    _reject_forbidden_payload(value, path=label)
    return value


def _is_reparse(metadata: os.stat_result) -> bool:
    return bool(getattr(metadata, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _direct_directory(path: Path, label: str) -> os.stat_result:
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or _is_reparse(metadata) or not stat.S_ISDIR(metadata.st_mode):
        raise guard.InvalidState(f"{label} must be a direct plain directory: {path}")
    resolved = path.resolve(strict=True)
    if os.path.normcase(str(resolved)) != os.path.normcase(str(path)):
        raise guard.InvalidState(f"{label} resolves through another target: {path}")
    return metadata


def _direct_file(path: Path, label: str) -> os.stat_result:
    metadata = path.lstat()
    if (
        stat.S_ISLNK(metadata.st_mode)
        or _is_reparse(metadata)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
    ):
        raise guard.InvalidState(f"{label} must be a direct single-link file: {path}")
    return metadata


def _read_direct_bytes(path: Path, label: str, *, max_bytes: int = MAX_REGISTRY_BYTES) -> bytes:
    before = _direct_file(path, label)
    if before.st_size > max_bytes:
        raise guard.InvalidState(f"{label} exceeds the size limit")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        identity = (before.st_dev, before.st_ino, before.st_nlink, before.st_size)
        if identity != (opened.st_dev, opened.st_ino, opened.st_nlink, opened.st_size):
            raise guard.InvalidState(f"{label} changed identity while opening")
        chunks: list[bytes] = []
        remaining = opened.st_size
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
        if len(raw) != opened.st_size or (after.st_dev, after.st_ino, after.st_nlink, after.st_size) != identity:
            raise guard.InvalidState(f"{label} changed while reading")
        return raw
    finally:
        os.close(descriptor)


def _project_binding_id(root: Path) -> str:
    normalized = os.path.normcase(str(root)).replace("\\", "/")
    return hashlib.sha256(f"founder-os-memory-binding-v1\0{normalized}".encode("utf-8")).hexdigest().upper()


def _memory_paths(founder: Path) -> tuple[Path, Path, Path, Path]:
    memory_root = founder / MEMORY_DIRECTORY
    return (
        memory_root,
        memory_root / REGISTRY_NAME,
        memory_root / ARCHIVE_DIRECTORY,
        memory_root / TRANSACTION_LOCK_NAME,
    )


def _registry_observation(founder: Path) -> tuple[str, bytes | None, dict[str, Any] | None]:
    memory_root, registry_path, _archive_root, _lock_path = _memory_paths(founder)
    if not memory_root.exists():
        return "ABSENT", None, None
    _direct_directory(memory_root, "Memory directory")
    if not registry_path.exists():
        return "ABSENT", None, None
    raw = _read_direct_bytes(registry_path, "Memory registry")
    value = _strict_json_loads(raw, label="Memory registry")
    return guard.sha256_bytes(raw), raw, value


def _assert_supervisor_memory_fingerprint(
    founder: Path, memory_sha: str, registry: dict[str, Any] | None
) -> None:
    _state_sha, state = guard.state_observation(founder / guard.STATE_NAME)
    if state is None:
        raise guard.Conflict("Organization Memory requires a current ACTIVE Supervisor")
    guard.validate_record(state, founder.parent)
    sources = state.get("source_revisions", {})
    expected_revision = sources.get("MEMORY_REVISION")
    expected_sha = sources.get("MEMORY_SHA256")
    if registry is None:
        if expected_revision is not None or expected_sha is not None:
            raise guard.Conflict("Supervisor references missing Organization Memory")
        return
    if expected_revision != registry["memory_revision"] or expected_sha != memory_sha:
        raise guard.Conflict("Organization Memory differs from the Supervisor checkpoint")


def _safe_archive_name(value: Any) -> str:
    value = _text(value, "archive filename", max_length=96)
    if not re.fullmatch(r"SEG-[0-9]{1,12}-[0-9]{1,12}-[0-9A-F]{64}\.json", value):
        raise guard.InvalidState("Archive filename is not a canonical hash-named segment")
    if Path(value).name != value:
        raise guard.InvalidState("Archive filename must be one safe basename")
    return value


def _event_material(event: dict[str, Any]) -> dict[str, Any]:
    return {key: copy.deepcopy(value) for key, value in event.items() if key != "event_sha256"}


def _event_hash(event: dict[str, Any]) -> str:
    return guard.sha256_bytes(guard.canonical_json_bytes(_event_material(event)))


def _new_event(
    registry: dict[str, Any],
    *,
    kind: str,
    subject_id: str,
    actor: str,
    evidence_refs: list[str],
    payload: dict[str, Any],
    retention: str,
) -> dict[str, Any]:
    if kind not in EVENT_KINDS:
        raise guard.InvalidState("Unknown Memory event kind")
    if retention not in RETENTION_CLASSES:
        raise guard.InvalidState("Unknown Memory retention class")
    _reject_forbidden_payload(payload)
    sequence = registry["next_sequence"]
    event = {
        "event_id": f"ME-{sequence:08d}-{secrets.token_hex(4)}",
        "sequence": sequence,
        "kind": kind,
        "subject_id": _identifier(subject_id, "Memory subject_id"),
        "observed_at": guard.utc_now(),
        "actor": _identifier(actor, "Memory actor"),
        "evidence_refs": _bounded_list(evidence_refs, "Memory evidence_refs", item_max=512),
        "retention": retention,
        "payload": copy.deepcopy(payload),
        "previous_event_sha256": registry["event_chain_head_sha256"],
    }
    event["event_sha256"] = _event_hash(event)
    registry["active_events"].append(event)
    registry["next_sequence"] += 1
    registry["event_chain_head_sha256"] = event["event_sha256"]
    return event


def _validate_project_binding(value: Any, root: Path) -> None:
    if not isinstance(value, dict) or set(value) != {"project_root", "project_binding_id"}:
        raise guard.InvalidState("Memory project_binding is malformed")
    stored = value.get("project_root")
    if not isinstance(stored, str) or not Path(stored).is_absolute():
        raise guard.InvalidState("Memory project_root must be absolute")
    try:
        resolved = Path(stored).resolve(strict=True)
    except OSError as exc:
        raise guard.InvalidState("Memory project_root cannot be resolved") from exc
    if (
        os.path.normcase(str(resolved)) != os.path.normcase(str(root))
        or os.path.normcase(str(Path(stored))) != os.path.normcase(str(resolved))
        or value.get("project_binding_id") != _project_binding_id(root)
    ):
        raise guard.InvalidState("Memory registry belongs to another project")


def _validate_event(event: Any, *, expected_sequence: int, previous_hash: str) -> str:
    required = {
        "event_id",
        "sequence",
        "kind",
        "subject_id",
        "observed_at",
        "actor",
        "evidence_refs",
        "retention",
        "payload",
        "previous_event_sha256",
        "event_sha256",
    }
    if not isinstance(event, dict) or set(event) != required:
        raise guard.InvalidState("Memory event fields are malformed")
    _identifier(event.get("event_id"), "event_id")
    if event.get("sequence") != expected_sequence:
        raise guard.InvalidState("Memory event sequence is not contiguous")
    if event.get("kind") not in EVENT_KINDS:
        raise guard.InvalidState("Memory event kind is unsupported")
    _identifier(event.get("subject_id"), "event subject_id")
    _timestamp(event.get("observed_at"), "event observed_at")
    _identifier(event.get("actor"), "event actor")
    _bounded_list(event.get("evidence_refs"), "event evidence_refs", item_max=512)
    if event.get("retention") not in RETENTION_CLASSES:
        raise guard.InvalidState("Memory event retention is invalid")
    if not isinstance(event.get("payload"), dict):
        raise guard.InvalidState("Memory event payload must be an object")
    _reject_forbidden_payload(event["payload"])
    if event.get("kind") == "ATTRIBUTION_REVISED":
        payload = event["payload"]
        if set(payload) != {"from_attribution", "to_attribution", "reason"}:
            raise guard.InvalidState("Attribution revision event payload is malformed")
        _validate_attribution_projection(
            payload.get("from_attribution"), "Attribution revision from_attribution"
        )
        _validate_attribution_projection(
            payload.get("to_attribution"), "Attribution revision to_attribution"
        )
        _text(payload.get("reason"), "Attribution revision reason")
    elif event.get("kind") == "OUTCOME_INVALIDATED_LATER":
        payload = event["payload"]
        if set(payload) != {"prior_outcome", "reason"}:
            raise guard.InvalidState("Outcome invalidation event payload is malformed")
        if payload.get("prior_outcome") not in OUTCOMES - {"INVALIDATED_LATER"}:
            raise guard.InvalidState("Outcome invalidation prior_outcome is invalid")
        _text(payload.get("reason"), "Outcome invalidation reason")
    elif event.get("kind") == "MEMORY_RETRACTED":
        payload = event["payload"]
        if set(payload) != {"record_type", "subject_hash", "reason", "founder_receipt"}:
            raise guard.InvalidState("Memory retraction event payload is malformed")
        if payload.get("record_type") not in PUBLIC_RECORD_KINDS:
            raise guard.InvalidState("Memory retraction record_type is invalid")
        _sha(payload.get("subject_hash"), "Memory retraction subject_hash")
        _text(payload.get("reason"), "Memory retraction reason")
        _identifier(payload.get("founder_receipt"), "Memory retraction Founder receipt")
    if event.get("previous_event_sha256") != previous_hash:
        raise guard.InvalidState("Memory event chain predecessor does not match")
    observed_hash = _sha(event.get("event_sha256"), "event_sha256")
    if observed_hash != _event_hash(event):
        raise guard.InvalidState("Memory event hash does not match its content")
    return observed_hash


def _validate_task_outcome(record: Any, record_id: str) -> None:
    required = {
        "task_id",
        "agent_id",
        "thread_record_id",
        "thread_generation",
        "workstream",
        "project_stage",
        "task_type",
        "capabilities",
        "components",
        "tags",
        "team_agent_ids",
        "skills",
        "risk_level",
        "outcome",
        "revision_count",
        "revision_severity",
        "review_result",
        "integration_result",
        "acceptance_result",
        "attribution",
        "evidence_refs",
        "retention",
        "finalized_at",
        "source_event_id",
        "effective",
        "retracted",
        "invalidation",
        "attribution_history",
    }
    if not isinstance(record, dict) or set(record) != required:
        raise guard.InvalidState(f"Task outcome {record_id} fields are malformed")
    if _identifier(record.get("task_id"), "task outcome task_id") != record_id:
        raise guard.InvalidState("Task outcome key and task_id disagree")
    _identifier(record.get("agent_id"), "task outcome agent_id")
    _optional_text(record.get("thread_record_id"), "task outcome thread_record_id", max_length=128)
    if record.get("thread_generation") is not None and (
        not isinstance(record.get("thread_generation"), int) or record["thread_generation"] < 1
    ):
        raise guard.InvalidState("task outcome thread_generation is invalid")
    for field in ("workstream", "project_stage", "task_type", "risk_level"):
        _text(record.get(field), f"task outcome {field}", max_length=128)
    if record.get("risk_level") not in RISK_LEVELS:
        raise guard.InvalidState("Task outcome risk_level is invalid")
    for field in ("capabilities", "components", "tags", "team_agent_ids"):
        _bounded_list(record.get(field), f"task outcome {field}", identifiers=True)
    skills = record.get("skills")
    if not isinstance(skills, list) or len(skills) > 16:
        raise guard.InvalidState("Task outcome skills must be a bounded list")
    seen_skills: set[str] = set()
    seen_skill_ids: set[str] = set()
    for skill in skills:
        if not isinstance(skill, dict) or set(skill) != {
            "skill_id", "approved_version", "commit_sha", "content_hash", "installed_hash", "entry_revision"
        }:
            raise guard.InvalidState("Task outcome Skill identity is malformed")
        skill_id = _identifier(skill.get("skill_id"), "task outcome skill_id")
        if skill_id.casefold() in seen_skill_ids:
            raise guard.InvalidState(
                "Task outcome cannot attribute multiple exact versions of one Skill ID"
            )
        seen_skill_ids.add(skill_id.casefold())
        _text(skill.get("approved_version"), "task outcome Skill version", max_length=128)
        commit = skill.get("commit_sha")
        if commit is not None and not isinstance(commit, str):
            raise guard.InvalidState("task outcome Skill commit_sha must be null or text")
        if isinstance(commit, str) and not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise guard.InvalidState("task outcome Skill commit_sha must be exact lowercase 40-hex")
        _sha(skill.get("content_hash"), "task outcome Skill content_hash")
        _sha(skill.get("installed_hash"), "task outcome Skill installed_hash")
        _identifier(skill.get("entry_revision"), "task outcome Skill entry_revision")
        key = f"{skill_id}@{skill['approved_version']}#{skill['installed_hash']}".casefold()
        if key in seen_skills:
            raise guard.InvalidState("Task outcome has duplicate exact Skill identity")
        seen_skills.add(key)
    if record.get("outcome") not in OUTCOMES:
        raise guard.InvalidState("Finalized task outcome is invalid")
    if not isinstance(record.get("revision_count"), int) or not 0 <= record["revision_count"] <= 1000:
        raise guard.InvalidState("Task outcome revision_count is invalid")
    if record.get("revision_severity") not in REVISION_SEVERITIES:
        raise guard.InvalidState("Task outcome revision_severity is invalid")
    if record.get("review_result") not in REVIEW_RESULTS or record.get("integration_result") not in INTEGRATION_RESULTS:
        raise guard.InvalidState("Task outcome review/integration state is invalid")
    if record.get("acceptance_result") not in ACCEPTANCE_RESULTS:
        raise guard.InvalidState("Task outcome acceptance_result is invalid")
    attribution = record.get("attribution")
    if not isinstance(attribution, dict) or set(attribution) != {"kind", "subject_id", "confidence", "evidence_refs"}:
        raise guard.InvalidState("Task outcome attribution is malformed")
    if attribution.get("kind") not in ATTRIBUTIONS:
        raise guard.InvalidState("Task outcome attribution kind is invalid")
    subject_id = _attribution_subject(
        attribution.get("kind"), attribution.get("subject_id"), "attribution subject_id"
    )
    if attribution.get("kind") in {"AGENT", "SKILL"} and subject_id is None:
        raise guard.InvalidState("AGENT or SKILL attribution requires an exact subject_id")
    if attribution.get("kind") == "SKILL":
        exact_keys = {
            f"{skill['skill_id']}@{skill['approved_version']}#{skill['installed_hash']}"
            for skill in skills
        }
        if subject_id not in exact_keys:
            raise guard.InvalidState(
                "SKILL attribution subject_id must be an exact task Skill version/hash key"
            )
    if attribution.get("confidence") not in CONFIDENCE_LEVELS:
        raise guard.InvalidState("Task outcome attribution confidence is invalid")
    _bounded_list(attribution.get("evidence_refs"), "attribution evidence_refs", item_max=512)
    _bounded_list(record.get("evidence_refs"), "task outcome evidence_refs", item_max=512)
    if record.get("retention") not in RETENTION_CLASSES:
        raise guard.InvalidState("Task outcome retention is invalid")
    _timestamp(record.get("finalized_at"), "task outcome finalized_at")
    _identifier(record.get("source_event_id"), "task outcome source_event_id")
    for field in ("effective", "retracted"):
        if not isinstance(record.get(field), bool):
            raise guard.InvalidState(f"Task outcome {field} must be boolean")
    invalidation = record.get("invalidation")
    if invalidation is not None:
        if not isinstance(invalidation, dict) or set(invalidation) != {
            "at", "reason", "evidence_refs", "event_id", "prior_outcome"
        }:
            raise guard.InvalidState("Task outcome invalidation is malformed")
        _timestamp(invalidation.get("at"), "invalidation at")
        _text(invalidation.get("reason"), "invalidation reason")
        _bounded_list(invalidation.get("evidence_refs"), "invalidation evidence_refs", item_max=512)
        _identifier(invalidation.get("event_id"), "invalidation event_id")
        if invalidation.get("prior_outcome") not in OUTCOMES - {"INVALIDATED_LATER"}:
            raise guard.InvalidState("Task outcome invalidation prior_outcome is invalid")
    if (record["outcome"] == "INVALIDATED_LATER") != (invalidation is not None):
        raise guard.InvalidState("INVALIDATED_LATER requires exactly one invalidation record")
    history = record.get("attribution_history")
    if not isinstance(history, list) or len(history) > 64:
        raise guard.InvalidState("Attribution history is malformed")
    for item in history:
        if not isinstance(item, dict) or set(item) != {"at", "from", "to", "reason", "evidence_refs", "event_id"}:
            raise guard.InvalidState("Attribution history row is malformed")
        _timestamp(item.get("at"), "attribution history at")
        if not isinstance(item.get("from"), dict) or not isinstance(item.get("to"), dict):
            raise guard.InvalidState("Attribution history from/to must be objects")
        normalized_from = _normalize_attribution(item["from"])
        normalized_to = _normalize_attribution(item["to"])
        _assert_exact_skill_attribution(normalized_from, skills)
        _assert_exact_skill_attribution(normalized_to, skills)
        _text(item.get("reason"), "attribution history reason")
        _bounded_list(item.get("evidence_refs"), "attribution history evidence_refs", item_max=512)
        _identifier(item.get("event_id"), "attribution history event_id")


TASK_PROJECTION_FIELDS = {
    "task_id", "agent_id", "workstream", "project_stage", "task_type",
    "capabilities", "components", "tags", "team_agent_ids", "skills", "risk_level",
    "outcome", "revision_count", "revision_severity", "review_result",
    "integration_result", "acceptance_result", "attribution", "retention",
    "finalized_at", "effective", "retracted",
}


def _attribution_projection(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": value["kind"],
        "subject_id": value.get("subject_id"),
        "confidence": value["confidence"],
    }


def _validate_attribution_projection(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"kind", "subject_id", "confidence"}:
        raise guard.InvalidState(f"{label} is malformed")
    kind = value.get("kind")
    if kind not in ATTRIBUTIONS:
        raise guard.InvalidState(f"{label} kind or confidence is invalid")
    result = {
        "kind": kind,
        "subject_id": _attribution_subject(
            kind, value.get("subject_id"), f"{label} subject_id"
        ),
        "confidence": value.get("confidence"),
    }
    if result["confidence"] not in CONFIDENCE_LEVELS:
        raise guard.InvalidState(f"{label} kind or confidence is invalid")
    if result["kind"] in {"AGENT", "SKILL"} and result["subject_id"] is None:
        raise guard.InvalidState(f"{label} requires an exact subject_id")
    return result


def _task_projection(record: dict[str, Any]) -> dict[str, Any]:
    attribution = record["attribution"]
    return {
        **{field: copy.deepcopy(record[field]) for field in TASK_PROJECTION_FIELDS - {"attribution"}},
        "attribution": _attribution_projection(attribution),
    }


def _validate_task_projection(value: Any, task_id: str) -> None:
    if not isinstance(value, dict) or set(value) != TASK_PROJECTION_FIELDS:
        raise guard.InvalidState("Archived Task Outcome projection is malformed")
    if _identifier(value.get("task_id"), "archived task_id") != task_id:
        raise guard.InvalidState("Archived Task Outcome projection identity disagrees")
    # Rehydrate only validation-only fields.  The compact projection deliberately
    # omits evidence locators and correction history; those stay in the archive
    # and append-only event overlay.
    hydrated = copy.deepcopy(value)
    hydrated.update({
        "thread_record_id": None,
        "thread_generation": None,
        "evidence_refs": ["archive:verified"],
        "source_event_id": "EV-ARCHIVED",
        "invalidation": (
            {
                "at": value["finalized_at"],
                "reason": "archived correction overlay",
                "evidence_refs": ["archive:correction"],
                "event_id": "EV-ARCHIVED-INVALIDATION",
                "prior_outcome": "SUCCESS_FIRST_PASS",
            }
            if value["outcome"] == "INVALIDATED_LATER" else None
        ),
        "attribution_history": [],
    })
    hydrated["attribution"] = {
        **copy.deepcopy(value["attribution"]),
        "evidence_refs": ["archive:attribution"],
    }
    _validate_task_outcome(hydrated, task_id)


def _validate_task_locator(value: Any, task_id: str) -> None:
    required = {
        "task_id", "archive_filename", "archived_record_sha256", "projection",
        "base_snapshot_sequence", "base_applied_correction_event_ids",
        "correction_event_ids", "archived_at",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise guard.InvalidState("Task Outcome archive locator is malformed")
    if _identifier(value.get("task_id"), "Task Outcome locator task_id") != task_id:
        raise guard.InvalidState("Task Outcome locator identity disagrees")
    _safe_archive_name(value.get("archive_filename"))
    _sha(value.get("archived_record_sha256"), "Task Outcome archived record hash")
    _validate_task_projection(value.get("projection"), task_id)
    if (
        not isinstance(value.get("base_snapshot_sequence"), int)
        or value["base_snapshot_sequence"] < 1
    ):
        raise guard.InvalidState("Task Outcome base_snapshot_sequence is invalid")
    base_ids = _bounded_list(
        value.get("base_applied_correction_event_ids"),
        "Task Outcome base applied correction events",
        identifiers=True,
    )
    _bounded_list(value.get("correction_event_ids"), "Task Outcome correction events", identifiers=True)
    if set(base_ids).intersection(value["correction_event_ids"]):
        raise guard.InvalidState("Task Outcome base and overlay correction IDs overlap")
    _timestamp(value.get("archived_at"), "Task Outcome archived_at")


def _validate_lesson(record: Any, record_id: str) -> None:
    required = {
        "lesson_id",
        "title",
        "applicability",
        "observation",
        "impact",
        "future_rule",
        "confidence",
        "evidence_level",
        "source_kind",
        "adoption_baseline_id",
        "adoption_baseline_sha256",
        "adoption_review_ref",
        "evidence_refs",
        "status",
        "retention",
        "occurrence_count",
        "created_at",
        "updated_at",
        "source_event_ids",
        "contradicts",
        "superseded_by",
        "retracted",
    }
    if not isinstance(record, dict) or set(record) != required:
        raise guard.InvalidState(f"Lesson {record_id} fields are malformed")
    if _identifier(record.get("lesson_id"), "lesson_id") != record_id:
        raise guard.InvalidState("Lesson key and lesson_id disagree")
    for field in ("title", "observation", "impact", "future_rule"):
        _text(record.get(field), f"lesson {field}")
    _bounded_list(record.get("applicability"), "lesson applicability", identifiers=True)
    if record.get("confidence") not in CONFIDENCE_LEVELS or record.get("evidence_level") not in EVIDENCE_LEVELS:
        raise guard.InvalidState("Lesson confidence/evidence level is invalid")
    if record.get("source_kind") not in {"NORMAL", "ADOPTION_CONFIRMED", "ADOPTION_INFERRED"}:
        raise guard.InvalidState("Lesson source_kind is invalid")
    adoption_id = record.get("adoption_baseline_id")
    adoption_sha = record.get("adoption_baseline_sha256")
    adoption_review_ref = record.get("adoption_review_ref")
    if record["source_kind"] == "NORMAL":
        if adoption_id is not None or adoption_sha is not None or adoption_review_ref is not None:
            raise guard.InvalidState("Normal Lesson cannot claim Adoption provenance")
    else:
        if not isinstance(adoption_id, str) or not re.fullmatch(r"AB-[0-9A-F]{16}", adoption_id):
            raise guard.InvalidState("Adoption Lesson baseline ID is invalid")
        _sha(adoption_sha, "Adoption Lesson baseline SHA")
        if adoption_id != f"AB-{adoption_sha[:16]}":
            raise guard.InvalidState("Adoption Lesson baseline ID/SHA disagree")
        _text(adoption_review_ref, "Adoption Lesson review reference", max_length=512)
    _bounded_list(record.get("evidence_refs"), "lesson evidence_refs", item_max=512)
    if record.get("status") not in LESSON_STATUSES or record.get("retention") not in RETENTION_CLASSES:
        raise guard.InvalidState("Lesson status/retention is invalid")
    if not isinstance(record.get("occurrence_count"), int) or record["occurrence_count"] < 1:
        raise guard.InvalidState("Lesson occurrence_count is invalid")
    _timestamp(record.get("created_at"), "lesson created_at")
    _timestamp(record.get("updated_at"), "lesson updated_at")
    _bounded_list(record.get("source_event_ids"), "lesson source_event_ids", identifiers=True)
    _bounded_list(record.get("contradicts"), "lesson contradicts", identifiers=True)
    _optional_text(record.get("superseded_by"), "lesson superseded_by", max_length=128)
    if not isinstance(record.get("retracted"), bool):
        raise guard.InvalidState("Lesson retracted must be boolean")


def _validate_decision(record: Any, record_id: str) -> None:
    required = {
        "decision_id",
        "status",
        "summary",
        "conditions",
        "applicability",
        "canonical_decisions_sha256",
        "result_summary",
        "reconsideration_trigger",
        "confidence",
        "evidence_refs",
        "retention",
        "updated_at",
        "source_event_ids",
        "retracted",
    }
    if not isinstance(record, dict) or set(record) != required:
        raise guard.InvalidState(f"Decision outcome {record_id} fields are malformed")
    if _identifier(record.get("decision_id"), "decision_id") != record_id:
        raise guard.InvalidState("Decision key and decision_id disagree")
    if record.get("status") not in DECISION_STATUSES:
        raise guard.InvalidState("Decision outcome status is invalid")
    for field in ("summary", "conditions", "result_summary", "reconsideration_trigger"):
        _text(record.get(field), f"decision {field}")
    applicability = _validate_selectors(record.get("applicability"))
    if any(applicability[field] for field in QUERY_CONTROL_FIELDS):
        raise guard.InvalidState("Decision applicability contains query-control selectors")
    _sha(record.get("canonical_decisions_sha256"), "decision canonical_decisions_sha256")
    if record.get("confidence") not in CONFIDENCE_LEVELS:
        raise guard.InvalidState("Decision confidence is invalid")
    _bounded_list(record.get("evidence_refs"), "decision evidence_refs", item_max=512)
    if record.get("retention") != "PERMANENT":
        raise guard.InvalidState("Decision outcomes require PERMANENT retention")
    _timestamp(record.get("updated_at"), "decision updated_at")
    _bounded_list(record.get("source_event_ids"), "decision source_event_ids", identifiers=True)
    if not isinstance(record.get("retracted"), bool):
        raise guard.InvalidState("Decision retracted must be boolean")


def _validate_routing(record: Any, record_id: str) -> None:
    required = {
        "routing_id",
        "task_context",
        "selected_agent_id",
        "selected_skill_keys",
        "alternatives",
        "reason",
        "evidence_record_ids",
        "evidence_bindings",
        "created_at",
        "retention",
        "retracted",
    }
    if not isinstance(record, dict) or set(record) != required:
        raise guard.InvalidState(f"Routing record {record_id} fields are malformed")
    if _identifier(record.get("routing_id"), "routing_id") != record_id:
        raise guard.InvalidState("Routing key and routing_id disagree")
    if not isinstance(record.get("task_context"), dict):
        raise guard.InvalidState("Routing task_context must be an object")
    _validate_selectors(record["task_context"], allow_empty=False)
    _optional_text(record.get("selected_agent_id"), "selected_agent_id", max_length=128)
    _bounded_list(record.get("selected_skill_keys"), "selected_skill_keys", item_max=512)
    _bounded_list(record.get("alternatives"), "routing alternatives", item_max=256)
    _text(record.get("reason"), "routing reason")
    evidence_ids = _bounded_list(
        record.get("evidence_record_ids"), "routing evidence_record_ids", identifiers=True
    )
    if not evidence_ids:
        raise guard.InvalidState("Routing history requires nonempty accepted Memory evidence")
    bindings = record.get("evidence_bindings")
    if not isinstance(bindings, list) or len(bindings) != len(evidence_ids):
        raise guard.InvalidState("Routing evidence bindings are malformed")
    seen: set[str] = set()
    for binding in bindings:
        if not isinstance(binding, dict) or set(binding) != {
            "record_type", "record_id", "content_sha256"
        }:
            raise guard.InvalidState("Routing evidence binding fields are malformed")
        if binding.get("record_type") not in {
            "task_outcomes", "lessons", "decision_outcomes", "organization_patterns"
        }:
            raise guard.InvalidState("Routing evidence binding record_type is invalid")
        record_key = _identifier(binding.get("record_id"), "routing evidence record_id")
        if record_key.casefold() in seen:
            raise guard.InvalidState("Routing evidence bindings contain duplicate record IDs")
        seen.add(record_key.casefold())
        _sha(binding.get("content_sha256"), "routing evidence content_sha256")
    if seen != {item.casefold() for item in evidence_ids}:
        raise guard.InvalidState("Routing evidence bindings disagree with evidence_record_ids")
    _timestamp(record.get("created_at"), "routing created_at")
    if record.get("retention") not in RETENTION_CLASSES or not isinstance(record.get("retracted"), bool):
        raise guard.InvalidState("Routing retention/retracted state is invalid")


def _validate_organization_pattern(record: Any, record_id: str) -> None:
    required = {
        "pattern_id", "pattern_type", "context", "summary", "evidence_refs",
        "retention", "occurrence_count", "created_at", "updated_at",
        "source_event_ids", "retracted",
    }
    if not isinstance(record, dict) or set(record) != required:
        raise guard.InvalidState(f"Organization pattern {record_id} fields are malformed")
    if _identifier(record.get("pattern_id"), "organization pattern_id") != record_id:
        raise guard.InvalidState("Organization pattern key and pattern_id disagree")
    if record.get("pattern_type") not in ORGANIZATION_PATTERN_TYPES:
        raise guard.InvalidState("Organization pattern_type is invalid")
    context = _validate_selectors(record.get("context"), allow_empty=False)
    if any(context[field] for field in {"record_types", "decision_ids", "lesson_statuses", "skill_keys"}):
        raise guard.InvalidState("Organization pattern context contains query-control selectors")
    _text(record.get("summary"), "organization pattern summary")
    evidence = _bounded_list(record.get("evidence_refs"), "organization pattern evidence_refs", item_max=512)
    if not evidence:
        raise guard.InvalidState("Organization pattern requires evidence")
    if record.get("retention") not in {"LONG_TERM", "PERMANENT"}:
        raise guard.InvalidState("Organization pattern retention must be LONG_TERM or PERMANENT")
    if not isinstance(record.get("occurrence_count"), int) or record["occurrence_count"] < 1:
        raise guard.InvalidState("Organization pattern occurrence_count is invalid")
    _timestamp(record.get("created_at"), "organization pattern created_at")
    _timestamp(record.get("updated_at"), "organization pattern updated_at")
    _bounded_list(record.get("source_event_ids"), "organization pattern source_event_ids", identifiers=True)
    if not isinstance(record.get("retracted"), bool):
        raise guard.InvalidState("Organization pattern retracted must be boolean")


def _validate_records(records: Any) -> None:
    if not isinstance(records, dict) or set(records) != RECORD_KINDS:
        raise guard.InvalidState("Memory records object is malformed")
    validators = {
        "task_outcomes": _validate_task_outcome,
        "lessons": _validate_lesson,
        "decision_outcomes": _validate_decision,
        "routing_history": _validate_routing,
        "organization_patterns": _validate_organization_pattern,
        "task_outcome_locators": _validate_task_locator,
    }
    for kind, values in records.items():
        if not isinstance(values, dict) or len(values) > MAX_RECORDS_PER_KIND:
            raise guard.InvalidState(f"Memory {kind} records are malformed or oversized")
        for record_id, record in values.items():
            _identifier(record_id, f"{kind} record id")
            validators[kind](record, record_id)


SELECTOR_FIELDS = {
    "record_types",
    "task_types",
    "capabilities",
    "components",
    "workstreams",
    "project_stages",
    "tags",
    "agent_ids",
    "skill_keys",
    "decision_ids",
    "lesson_statuses",
    "risk_levels",
}
SELECTABLE_RECORD_TYPES = PUBLIC_RECORD_KINDS | {
    "agent_performance", "skill_performance", "team_patterns",
}
QUERY_CONTROL_FIELDS = {
    "record_types", "agent_ids", "skill_keys", "decision_ids", "lesson_statuses",
}
PERFORMANCE_CONTEXT_FIELDS = {
    "task_types", "capabilities", "components", "workstreams",
    "project_stages", "tags", "risk_levels",
}


def _validate_selectors(value: Any, *, allow_empty: bool = True) -> dict[str, list[str]]:
    if not isinstance(value, dict):
        raise guard.InvalidState("Memory selectors must be an object")
    unknown = set(value).difference(SELECTOR_FIELDS)
    if unknown:
        raise guard.InvalidState(f"Unknown Memory selector fields: {sorted(unknown)}")
    normalized: dict[str, list[str]] = {}
    for key in sorted(SELECTOR_FIELDS):
        values = value.get(key, [])
        normalized[key] = _bounded_list(
            values,
            f"selector {key}",
            item_max=512 if key == "skill_keys" else 128,
            identifiers=key not in {"skill_keys"},
        )
    if any(item not in SELECTABLE_RECORD_TYPES for item in normalized["record_types"]):
        raise guard.InvalidState("selector record_types contains an unsupported type")
    if any(item not in LESSON_STATUSES for item in normalized["lesson_statuses"]):
        raise guard.InvalidState("selector lesson_statuses contains an unsupported status")
    if any(item not in RISK_LEVELS for item in normalized["risk_levels"]):
        raise guard.InvalidState("selector risk_levels contains an unsupported risk level")
    if not allow_empty and not any(normalized.values()):
        raise guard.InvalidState("Memory task context may not be completely empty")
    return normalized


def _empty_counts() -> dict[str, int]:
    return {outcome: 0 for outcome in sorted(OUTCOMES)}


def _performance_confidence(sample_count: int) -> tuple[str, str]:
    if sample_count <= 2:
        return "LOW", "one or two finalized observations"
    if sample_count <= 7:
        return "MEDIUM", "three to seven finalized observations"
    return "HIGH", "eight or more finalized observations"


def _performance_label(summary: dict[str, Any]) -> str:
    samples = summary["sample_count"]
    if samples == 0:
        return "UNPROVEN"
    successes = summary["outcomes"]["SUCCESS_FIRST_PASS"] + summary["outcomes"]["SUCCESS_AFTER_REVISION"]
    attributed = summary["attributed_failures"]
    if samples >= 3 and attributed == 0 and summary["outcomes"]["SUCCESS_FIRST_PASS"] >= 2:
        return "STRONG_EVIDENCE"
    if successes > attributed and successes >= 2:
        return "RELIABLE_EVIDENCE"
    if successes and attributed:
        return "MIXED_EVIDENCE"
    if attributed:
        return "WEAK_EVIDENCE"
    return "LIMITED_EVIDENCE"


def _new_performance(subject_id: str, subject_kind: str) -> dict[str, Any]:
    return {
        "subject_id": subject_id,
        "subject_kind": subject_kind,
        "sample_count": 0,
        "outcomes": _empty_counts(),
        "revision_severity": {key: 0 for key in sorted(REVISION_SEVERITIES)},
        "review_results": {key: 0 for key in sorted(REVIEW_RESULTS)},
        "integration_results": {key: 0 for key in sorted(INTEGRATION_RESULTS)},
        "observed_failures": 0,
        "attributed_failures": 0,
        "recent_task_ids": [],
        "contexts": {
            "task_type": {},
            "capability": {},
            "component": {},
            "workstream": {},
            "project_stage": {},
            "risk_level": {},
        },
        "last_observed_at": None,
        "confidence": "LOW",
        "confidence_basis": "no finalized observations",
        "evidence_label": "UNPROVEN",
    }


def _context_bucket(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "sample_count": row["sample_count"],
        "outcomes": copy.deepcopy(row["outcomes"]),
        "attributed_failures": row["attributed_failures"],
    }


def _observe_performance(summary: dict[str, Any], outcome: dict[str, Any], *, attributed: bool) -> None:
    summary["sample_count"] += 1
    summary["outcomes"][outcome["outcome"]] += 1
    summary["revision_severity"][outcome["revision_severity"]] += 1
    summary["review_results"][outcome["review_result"]] += 1
    summary["integration_results"][outcome["integration_result"]] += 1
    if outcome["outcome"] in {"FAILED", "PARTIAL", "INVALIDATED_LATER"}:
        summary["observed_failures"] += 1
    if attributed:
        summary["attributed_failures"] += 1
    summary["recent_task_ids"].append(outcome["task_id"])
    summary["recent_task_ids"] = summary["recent_task_ids"][-20:]
    summary["last_observed_at"] = outcome["finalized_at"]
    dimensions = {
        "task_type": [outcome["task_type"]],
        "capability": outcome["capabilities"],
        "component": outcome["components"],
        "workstream": [outcome["workstream"]],
        "project_stage": [outcome["project_stage"]],
        "risk_level": [outcome["risk_level"]],
    }
    for dimension, values in dimensions.items():
        for value in values:
            bucket = summary["contexts"][dimension].setdefault(
                value,
                {"sample_count": 0, "outcomes": _empty_counts(), "attributed_failures": 0},
            )
            bucket["sample_count"] += 1
            bucket["outcomes"][outcome["outcome"]] += 1
            if attributed:
                bucket["attributed_failures"] += 1


def _task_records(records: dict[str, Any]) -> dict[str, dict[str, Any]]:
    combined = {key: value for key, value in records["task_outcomes"].items()}
    for task_id, locator in records["task_outcome_locators"].items():
        if task_id in combined:
            raise guard.InvalidState("Task Outcome exists in both current and archived locator sets")
        combined[task_id] = locator["projection"]
    return combined


def _derive_performance(records: dict[str, Any]) -> dict[str, Any]:
    agents: dict[str, dict[str, Any]] = {}
    skills: dict[str, dict[str, Any]] = {}
    teams: dict[str, dict[str, Any]] = {}
    ordered = sorted(
        _task_records(records).values(),
        key=lambda item: (item["finalized_at"], item["task_id"]),
    )
    for outcome in ordered:
        if not outcome["effective"] or outcome["retracted"]:
            continue
        agent_id = outcome["agent_id"]
        attribution = outcome["attribution"]
        agent_attributed = (
            attribution["kind"] == "AGENT"
            and attribution.get("subject_id") in {None, agent_id}
            and outcome["outcome"] in {"FAILED", "PARTIAL", "INVALIDATED_LATER"}
        )
        agent = agents.setdefault(agent_id, _new_performance(agent_id, "AGENT"))
        _observe_performance(agent, outcome, attributed=agent_attributed)
        for skill in outcome["skills"]:
            key = f"{skill['skill_id']}@{skill['approved_version']}#{skill['installed_hash']}"
            skill_attributed = (
                attribution["kind"] == "SKILL"
                and attribution.get("subject_id") == key
                and outcome["outcome"] in {"FAILED", "PARTIAL", "INVALIDATED_LATER"}
            )
            skill_summary = skills.setdefault(key, _new_performance(key, "SKILL_EXACT_VERSION"))
            _observe_performance(skill_summary, outcome, attributed=skill_attributed)
        team_ids = sorted(set(outcome["team_agent_ids"] + [agent_id]))
        if len(team_ids) > 1:
            team_key = "+".join(team_ids)
            team = teams.setdefault(
                team_key,
                {
                    "team_agent_ids": team_ids,
                    "sample_count": 0,
                    "outcomes": _empty_counts(),
                    "recent_task_ids": [],
                    "confidence": "LOW",
                    "confidence_basis": "no finalized observations",
                },
            )
            team["sample_count"] += 1
            team["outcomes"][outcome["outcome"]] += 1
            team["recent_task_ids"] = (team["recent_task_ids"] + [outcome["task_id"]])[-20:]
    for collection in (agents, skills):
        for summary in collection.values():
            confidence, basis = _performance_confidence(summary["sample_count"])
            summary["confidence"] = confidence
            summary["confidence_basis"] = basis
            summary["evidence_label"] = _performance_label(summary)
    for team in teams.values():
        team["confidence"], team["confidence_basis"] = _performance_confidence(team["sample_count"])
    return {
        "agent_performance": agents,
        "skill_performance": skills,
        "team_patterns": teams,
        "indexes": _derive_indexes(records),
    }


def _append_index(index: dict[str, dict[str, list[str]]], dimension: str, value: str, record_id: str) -> None:
    bucket = index[dimension].setdefault(value, [])
    bucket.append(record_id)


def _derive_indexes(records: dict[str, Any]) -> dict[str, Any]:
    """Build deterministic, non-authoritative lookup indexes from canonical records."""
    task_index: dict[str, dict[str, list[str]]] = {
        key: {}
        for key in (
            "task_type", "capability", "component", "workstream", "project_stage",
            "tag", "risk_level", "agent_id", "skill_key",
        )
    }
    for record_id, outcome in sorted(_task_records(records).items()):
        _append_index(task_index, "task_type", outcome["task_type"], record_id)
        _append_index(task_index, "workstream", outcome["workstream"], record_id)
        _append_index(task_index, "project_stage", outcome["project_stage"], record_id)
        _append_index(task_index, "risk_level", outcome["risk_level"], record_id)
        _append_index(task_index, "agent_id", outcome["agent_id"], record_id)
        for value in outcome["capabilities"]:
            _append_index(task_index, "capability", value, record_id)
        for value in outcome["components"]:
            _append_index(task_index, "component", value, record_id)
        for value in outcome["tags"]:
            _append_index(task_index, "tag", value, record_id)
        for skill in outcome["skills"]:
            key = f"{skill['skill_id']}@{skill['approved_version']}#{skill['installed_hash']}"
            _append_index(task_index, "skill_key", key, record_id)

    lesson_index: dict[str, dict[str, list[str]]] = {"applicability": {}, "status": {}}
    for record_id, lesson in sorted(records["lessons"].items()):
        _append_index(lesson_index, "status", lesson["status"], record_id)
        for value in lesson["applicability"]:
            _append_index(lesson_index, "applicability", value.casefold(), record_id)

    decision_index = {
        "decision_id": {
            record["decision_id"]: [record_id]
            for record_id, record in sorted(records["decision_outcomes"].items())
        }
    }
    routing_index: dict[str, list[str]] = {}
    for record_id, routing in sorted(records["routing_history"].items()):
        context_sha = guard.sha256_bytes(guard.canonical_json_bytes(_validate_selectors(routing["task_context"])))
        routing_index.setdefault(context_sha, []).append(record_id)
    organization_index: dict[str, dict[str, list[str]]] = {"pattern_type": {}, "context": {}}
    for record_id, pattern in sorted(records["organization_patterns"].items()):
        _append_index(organization_index, "pattern_type", pattern["pattern_type"], record_id)
        for selector_name, values in pattern["context"].items():
            for value in values:
                _append_index(
                    organization_index,
                    "context",
                    f"{selector_name}:{value}".casefold(),
                    record_id,
                )
    return {
        "task_outcomes": task_index,
        "lessons": lesson_index,
        "decision_outcomes": decision_index,
        "routing_history": {"task_context_sha256": routing_index},
        "organization_patterns": organization_index,
    }


def _validate_performance_summary(value: Any, key: str, kind: str) -> None:
    expected = set(_new_performance(key, kind))
    if not isinstance(value, dict) or set(value) != expected:
        raise guard.InvalidState("Performance summary fields are malformed")
    if value.get("subject_id") != key or value.get("subject_kind") != kind:
        raise guard.InvalidState("Performance summary identity is malformed")
    if not isinstance(value.get("sample_count"), int) or value["sample_count"] < 1:
        raise guard.InvalidState("Persisted performance summaries require observations")
    if value.get("confidence") not in CONFIDENCE_LEVELS:
        raise guard.InvalidState("Performance confidence is invalid")
    if value.get("evidence_label") not in {
        "STRONG_EVIDENCE", "RELIABLE_EVIDENCE", "MIXED_EVIDENCE", "WEAK_EVIDENCE", "LIMITED_EVIDENCE"
    }:
        raise guard.InvalidState("Performance evidence label is invalid")
    _text(value.get("confidence_basis"), "performance confidence_basis")


def _validate_derived(value: Any, records: dict[str, Any]) -> None:
    if not isinstance(value, dict) or set(value) != {
        "agent_performance", "skill_performance", "team_patterns", "indexes"
    }:
        raise guard.InvalidState("Memory derived summaries are malformed")
    for key, summary in value["agent_performance"].items():
        _identifier(key, "agent performance key")
        _validate_performance_summary(summary, key, "AGENT")
    for key, summary in value["skill_performance"].items():
        _text(key, "Skill performance key", max_length=512)
        _validate_performance_summary(summary, key, "SKILL_EXACT_VERSION")
    if not isinstance(value["team_patterns"], dict):
        raise guard.InvalidState("Team patterns must be an object")
    expected = _derive_performance(records)
    if value != expected:
        raise guard.InvalidState("Memory performance summaries do not match effective outcomes")


def _validate_archive_manifest(value: Any) -> tuple[int, str]:
    if not isinstance(value, list) or len(value) > 1000:
        raise guard.InvalidState("Memory archive_manifest is malformed")
    previous_last = 0
    previous_event_hash = "GENESIS"
    names: set[str] = set()
    for item in value:
        required = {
            "filename",
            "content_sha256",
            "first_sequence",
            "last_sequence",
            "event_count",
            "record_counts",
            "record_content_sha256",
            "first_previous_event_sha256",
            "last_event_sha256",
            "created_at",
        }
        if not isinstance(item, dict) or set(item) != required:
            raise guard.InvalidState("Archive manifest row is malformed")
        name = _safe_archive_name(item.get("filename"))
        if name.casefold() in names:
            raise guard.InvalidState("Archive manifest contains duplicate filenames")
        names.add(name.casefold())
        _sha(item.get("content_sha256"), "archive content_sha256")
        record_counts = item.get("record_counts")
        if (
            not isinstance(record_counts, dict)
            or set(record_counts) != {"routing_history", "task_outcomes"}
            or any(
                not isinstance(record_counts[key], int) or record_counts[key] < 0
                for key in record_counts
            )
        ):
            raise guard.InvalidState("Archive record_counts are malformed")
        _sha(item.get("record_content_sha256"), "archive record_content_sha256")
        first = item.get("first_sequence")
        last = item.get("last_sequence")
        count = item.get("event_count")
        if not all(isinstance(number, int) for number in (first, last, count)):
            raise guard.InvalidState("Archive sequence fields must be integers")
        if first != previous_last + 1 or last < first or count != last - first + 1:
            raise guard.InvalidState("Archive sequence ranges are not contiguous")
        if item.get("first_previous_event_sha256") != previous_event_hash:
            raise guard.InvalidState("Archive event-chain boundary is malformed")
        previous_event_hash = _sha(item.get("last_event_sha256"), "archive last_event_sha256")
        _timestamp(item.get("created_at"), "archive created_at")
        previous_last = last
    return previous_last, previous_event_hash


def _is_task_correction_event(event: dict[str, Any], task_id: str) -> bool:
    if event["subject_id"] != task_id:
        return False
    if event["kind"] in {"OUTCOME_INVALIDATED_LATER", "ATTRIBUTION_REVISED"}:
        return True
    return (
        event["kind"] == "MEMORY_RETRACTED"
        and event["payload"].get("record_type") == "task_outcomes"
    )


def _pre_retraction_value(record_type: str, record: dict[str, Any]) -> dict[str, Any]:
    if record.get("retracted") is not True:
        raise guard.InvalidState("Memory retraction event points to a non-retracted record")
    value = copy.deepcopy(record)
    value["retracted"] = False
    if record_type == "task_outcomes":
        if value.get("effective") is not False:
            raise guard.InvalidState("Retracted Task Outcome must be ineffective")
        value["effective"] = True
    return value


def _assert_retraction_subject_hash(
    event: dict[str, Any], record_type: str, record: dict[str, Any]
) -> None:
    if (
        event["kind"] != "MEMORY_RETRACTED"
        or event["payload"]["record_type"] != record_type
    ):
        raise guard.InvalidState("Memory retraction event points to the wrong record type")
    expected = guard.sha256_bytes(
        guard.canonical_json_bytes(_pre_retraction_value(record_type, record))
    )
    if event["payload"]["subject_hash"] != expected:
        raise guard.InvalidState("Memory retraction subject hash does not bind its prior record")


def _validate_active_retraction_records(registry: dict[str, Any]) -> None:
    """Prove active retractions whose canonical record is still in MEMORY.json."""

    seen: set[tuple[str, str]] = set()
    for event in registry["active_events"]:
        if event["kind"] != "MEMORY_RETRACTED":
            continue
        record_type = event["payload"]["record_type"]
        key = (record_type, event["subject_id"])
        if key in seen:
            raise guard.InvalidState("Memory record was retracted more than once")
        seen.add(key)
        record = registry["records"][record_type].get(event["subject_id"])
        if record is not None:
            _assert_retraction_subject_hash(event, record_type, record)
            continue
        if record_type == "task_outcomes":
            locator = registry["records"]["task_outcome_locators"].get(event["subject_id"])
            if locator is not None:
                if event["event_id"] in locator["correction_event_ids"]:
                    _assert_retraction_subject_hash(
                        event, "task_outcomes", locator["projection"]
                    )
                elif event["event_id"] not in locator["base_applied_correction_event_ids"]:
                    raise guard.InvalidState(
                        "Task Outcome retraction is absent from its archive locator"
                    )
                continue
        if record_type == "routing_history":
            continue
        raise guard.InvalidState("Memory retraction event references an unknown current record")


def _verify_retraction_audit(
    registry: dict[str, Any], archives: dict[str, dict[str, Any]]
) -> None:
    """Verify every Founder retraction against current or immutable archived state."""

    event_by_id: dict[str, dict[str, Any]] = {}
    for archive in archives.values():
        for event in archive["events"]:
            if event["event_id"] in event_by_id:
                raise guard.InvalidState("Memory archives contain duplicate event IDs")
            event_by_id[event["event_id"]] = event
    for event in registry["active_events"]:
        if event["event_id"] in event_by_id:
            raise guard.InvalidState("Memory active/archive events overlap")
        event_by_id[event["event_id"]] = event

    retractions = sorted(
        (event for event in event_by_id.values() if event["kind"] == "MEMORY_RETRACTED"),
        key=lambda event: event["sequence"],
    )
    receipts = [event["payload"]["founder_receipt"] for event in retractions]
    if len(receipts) != len(set(item.casefold() for item in receipts)):
        raise guard.InvalidState("Memory retraction Founder receipt was reused")
    if sorted(receipts, key=str.casefold) != registry["consumed_founder_receipts"]:
        raise guard.InvalidState(
            "Consumed Founder receipts do not exactly match the retraction audit stream"
        )

    seen_records: set[tuple[str, str]] = set()
    for event in retractions:
        record_type = event["payload"]["record_type"]
        record_id = event["subject_id"]
        key = (record_type, record_id)
        if key in seen_records:
            raise guard.InvalidState("Memory record was retracted more than once")
        seen_records.add(key)

        if record_type == "task_outcomes":
            locator = registry["records"]["task_outcome_locators"].get(record_id)
            if locator is not None:
                if event["event_id"] in locator["base_applied_correction_event_ids"]:
                    archive = archives.get(locator["archive_filename"])
                    if archive is None:
                        raise guard.InvalidState(
                            "Task Outcome base retraction archive was not verified"
                        )
                    record = archive["record_segments"]["task_outcomes"].get(record_id)
                elif event["event_id"] in locator["correction_event_ids"]:
                    record = locator["projection"]
                else:
                    raise guard.InvalidState(
                        "Task Outcome retraction is absent from its archive locator"
                    )
            else:
                record = registry["records"]["task_outcomes"].get(record_id)
        else:
            record = registry["records"][record_type].get(record_id)
            if record is None and record_type == "routing_history":
                archived_matches = [
                    archive["record_segments"]["routing_history"][record_id]
                    for archive in archives.values()
                    if record_id in archive["record_segments"]["routing_history"]
                ]
                if len(archived_matches) > 1:
                    raise guard.InvalidState("Routing record appears in multiple archives")
                record = archived_matches[0] if archived_matches else None
        if record is None:
            raise guard.InvalidState("Memory retraction has no canonical record")
        _assert_retraction_subject_hash(event, record_type, record)


def _validate_active_locator_corrections(registry: dict[str, Any]) -> None:
    """Validate the visible base/overlay suffix without opening archives."""

    active_by_id = {event["event_id"]: event for event in registry["active_events"]}
    for task_id, locator in registry["records"]["task_outcome_locators"].items():
        if locator["base_snapshot_sequence"] >= registry["next_sequence"]:
            raise guard.InvalidState("Task Outcome base snapshot is ahead of Memory")
        visible_base = [
            active_by_id[event_id]
            for event_id in locator["base_applied_correction_event_ids"]
            if event_id in active_by_id
        ]
        visible_overlay = [
            active_by_id[event_id]
            for event_id in locator["correction_event_ids"]
            if event_id in active_by_id
        ]
        for event in visible_base + visible_overlay:
            if not _is_task_correction_event(event, task_id):
                raise guard.InvalidState(
                    "Task Outcome correction event does not belong to its locator"
                )
        expected_base = [
            event["event_id"]
            for event in registry["active_events"]
            if _is_task_correction_event(event, task_id)
            and event["sequence"] <= locator["base_snapshot_sequence"]
        ]
        expected_overlay = [
            event["event_id"]
            for event in registry["active_events"]
            if _is_task_correction_event(event, task_id)
            and event["sequence"] > locator["base_snapshot_sequence"]
        ]
        if [event["event_id"] for event in visible_base] != expected_base:
            raise guard.InvalidState(
                "Task Outcome visible base corrections are incomplete or out of sequence"
            )
        if [event["event_id"] for event in visible_overlay] != expected_overlay:
            raise guard.InvalidState(
                "Task Outcome active correction overlay is incomplete or out of sequence"
            )
        attribution_events = [
            event for event in visible_overlay if event["kind"] == "ATTRIBUTION_REVISED"
        ]
        prior_to: dict[str, Any] | None = None
        for event in attribution_events:
            from_attribution = _validate_attribution_projection(
                event["payload"].get("from_attribution"),
                "Attribution overlay from_attribution",
            )
            to_attribution = _validate_attribution_projection(
                event["payload"].get("to_attribution"),
                "Attribution overlay to_attribution",
            )
            if prior_to is not None and from_attribution != prior_to:
                raise guard.InvalidState("Attribution correction overlay chain is discontinuous")
            prior_to = to_attribution
        if prior_to is not None and prior_to != locator["projection"]["attribution"]:
            raise guard.InvalidState(
                "Task Outcome projection does not match its active attribution corrections"
            )


def validate_registry(registry: dict[str, Any], root: Path) -> None:
    _reject_forbidden_payload(registry, path="Memory registry")
    if set(registry) != TOP_LEVEL_FIELDS:
        raise guard.InvalidState("Memory registry top-level fields are malformed")
    if registry.get("schema_version") != SCHEMA_VERSION:
        raise guard.InvalidState("MIGRATION_REQUIRED: unsupported Memory schema_version")
    _identifier(registry.get("memory_revision"), "memory_revision")
    _sha_or_absent(registry.get("previous_memory_sha256"), "previous_memory_sha256")
    _validate_project_binding(registry.get("project_binding"), root)
    _timestamp(registry.get("created_at"), "Memory created_at")
    _timestamp(registry.get("updated_at"), "Memory updated_at")
    if not isinstance(registry.get("next_sequence"), int) or registry["next_sequence"] < 1:
        raise guard.InvalidState("Memory next_sequence is invalid")
    if registry.get("event_chain_head_sha256") != "GENESIS":
        _sha(registry.get("event_chain_head_sha256"), "event_chain_head_sha256")
    archive_last, previous_hash = _validate_archive_manifest(registry.get("archive_manifest"))
    events = registry.get("active_events")
    if not isinstance(events, list) or len(events) > MAX_EVENTS:
        raise guard.InvalidState("Memory active_events is malformed or oversized")
    expected_sequence = archive_last + 1
    event_ids: set[str] = set()
    for event in events:
        previous_hash = _validate_event(event, expected_sequence=expected_sequence, previous_hash=previous_hash)
        if event["event_id"] in event_ids:
            raise guard.InvalidState("Memory contains duplicate event_id")
        event_ids.add(event["event_id"])
        expected_sequence += 1
    if registry["next_sequence"] != expected_sequence:
        raise guard.InvalidState("Memory next_sequence does not follow the event stream")
    if registry["event_chain_head_sha256"] != previous_hash:
        raise guard.InvalidState("Memory event_chain_head_sha256 is stale")
    _validate_records(registry.get("records"))
    _validate_active_retraction_records(registry)
    _validate_active_locator_corrections(registry)
    manifest_names = {row["filename"] for row in registry["archive_manifest"]}
    for locator in registry["records"]["task_outcome_locators"].values():
        if locator["archive_filename"] not in manifest_names:
            raise guard.InvalidState("Task Outcome locator references an unknown archive segment")
    _validate_derived(registry.get("derived"), registry["records"])
    receipts = registry.get("consumed_founder_receipts")
    if not isinstance(receipts, list) or len(receipts) > 5000:
        raise guard.InvalidState("consumed_founder_receipts is malformed")
    normalized_receipts = _bounded_list(
        receipts, "consumed_founder_receipts", item_max=128, limit=5000, identifiers=True
    )
    if normalized_receipts != sorted(normalized_receipts, key=str.casefold):
        raise guard.InvalidState("consumed_founder_receipts must be canonical and sorted")
    active_retraction_receipts = {
        event["payload"]["founder_receipt"]
        for event in events
        if event["kind"] == "MEMORY_RETRACTED"
    }
    if not active_retraction_receipts.issubset(set(normalized_receipts)):
        raise guard.InvalidState("Active Memory retraction receipt was not consumed")
    if len(guard.canonical_json_bytes(registry)) > MAX_REGISTRY_BYTES:
        raise guard.InvalidState("Canonical Memory registry exceeds the size limit")


def _initial_registry(root: Path) -> dict[str, Any]:
    now = guard.utc_now()
    return {
        "schema_version": SCHEMA_VERSION,
        "memory_revision": guard.new_revision("MR"),
        "previous_memory_sha256": "ABSENT",
        "project_binding": {
            "project_root": str(root),
            "project_binding_id": _project_binding_id(root),
        },
        "created_at": now,
        "updated_at": now,
        "next_sequence": 1,
        "event_chain_head_sha256": "GENESIS",
        "active_events": [],
        "records": {kind: {} for kind in sorted(RECORD_KINDS)},
        "derived": {
            "agent_performance": {},
            "skill_performance": {},
            "team_patterns": {},
            "indexes": _derive_indexes({kind: {} for kind in sorted(RECORD_KINDS)}),
        },
        "archive_manifest": [],
        "consumed_founder_receipts": [],
    }


def _read_archive(archive_root: Path, row: dict[str, Any]) -> dict[str, Any]:
    _direct_directory(archive_root, "Memory archive directory")
    path = archive_root / _safe_archive_name(row["filename"])
    raw = _read_direct_bytes(path, "Memory archive segment", max_bytes=MAX_ARCHIVE_BYTES)
    if guard.sha256_bytes(raw) != row["content_sha256"]:
        raise guard.InvalidState(f"Memory archive hash mismatch: {path.name}")
    value = _strict_json_loads(raw, label=f"Memory archive {path.name}")
    required = {
        "schema_version",
        "project_binding_id",
        "first_sequence",
        "last_sequence",
        "event_count",
        "events",
        "record_segments",
    }
    if set(value) != required or value.get("schema_version") != ARCHIVE_SCHEMA_VERSION:
        raise guard.InvalidState("Memory archive schema is malformed")
    if value.get("project_binding_id") is None:
        raise guard.InvalidState("Memory archive project binding is missing")
    if (
        value.get("first_sequence") != row["first_sequence"]
        or value.get("last_sequence") != row["last_sequence"]
        or value.get("event_count") != row["event_count"]
    ):
        raise guard.InvalidState("Memory archive range disagrees with manifest")
    events = value.get("events")
    if not isinstance(events, list) or len(events) != row["event_count"]:
        raise guard.InvalidState("Memory archive events are malformed")
    previous = row["first_previous_event_sha256"]
    sequence = row["first_sequence"]
    for event in events:
        previous = _validate_event(event, expected_sequence=sequence, previous_hash=previous)
        sequence += 1
    if previous != row["last_event_sha256"]:
        raise guard.InvalidState("Memory archive last event hash disagrees with manifest")
    segments = value.get("record_segments")
    if not isinstance(segments, dict) or set(segments) != {"routing_history", "task_outcomes"}:
        raise guard.InvalidState("Memory archive record_segments are malformed")
    routing = segments["routing_history"]
    outcomes = segments["task_outcomes"]
    if (
        not isinstance(routing, dict)
        or len(routing) != row["record_counts"]["routing_history"]
        or not isinstance(outcomes, dict)
        or len(outcomes) != row["record_counts"]["task_outcomes"]
        or guard.sha256_bytes(guard.canonical_json_bytes(segments))
        != row["record_content_sha256"]
    ):
        raise guard.InvalidState("Memory archive record summary disagrees with manifest")
    event_subjects = {
        event["subject_id"]
        for event in events
        if event["kind"] == "ROUTING_DECISION_RECORDED"
    }
    for record_id, record in routing.items():
        _validate_routing(record, record_id)
        if record_id not in event_subjects:
            raise guard.InvalidState("Archived Routing record has no matching archived event")
    task_subjects = {
        event["subject_id"]
        for event in events
        if event["kind"] == "TASK_OUTCOME_FINALIZED"
    }
    for task_id, outcome in outcomes.items():
        _validate_task_outcome(outcome, task_id)
        if task_id not in task_subjects:
            raise guard.InvalidState("Archived Task Outcome has no matching archived finalized event")
    return value


def _verify_task_locator_corrections(
    registry: dict[str, Any],
    archives: dict[str, dict[str, Any]],
) -> None:
    """Prove every compacted Task projection from immutable detail plus overlays."""

    event_by_id: dict[str, dict[str, Any]] = {}
    for archive in archives.values():
        for event in archive["events"]:
            if event["event_id"] in event_by_id:
                raise guard.InvalidState("Memory archives contain duplicate event IDs")
            event_by_id[event["event_id"]] = event
    for event in registry["active_events"]:
        if event["event_id"] in event_by_id:
            raise guard.InvalidState("Memory active/archive events overlap")
        event_by_id[event["event_id"]] = event

    retraction_events = sorted(
        (event for event in event_by_id.values() if event["kind"] == "MEMORY_RETRACTED"),
        key=lambda event: event["sequence"],
    )
    retraction_receipts = [event["payload"]["founder_receipt"] for event in retraction_events]
    if len(retraction_receipts) != len(set(item.casefold() for item in retraction_receipts)):
        raise guard.InvalidState("Memory retraction Founder receipt was reused")
    if sorted(retraction_receipts, key=str.casefold) != registry["consumed_founder_receipts"]:
        raise guard.InvalidState(
            "Consumed Founder receipts do not exactly match the retraction audit stream"
        )

    for task_id, locator in registry["records"]["task_outcome_locators"].items():
        archive = archives.get(locator["archive_filename"])
        if archive is None:
            raise guard.InvalidState("Task Outcome locator archive was not verified")
        archived = archive["record_segments"]["task_outcomes"].get(task_id)
        if archived is None:
            raise guard.InvalidState("Task Outcome locator has no immutable archived detail")

        expected_events = sorted(
            (
                event for event in event_by_id.values()
                if event["subject_id"] == task_id
                and (
                    event["kind"] in {"OUTCOME_INVALIDATED_LATER", "ATTRIBUTION_REVISED"}
                    or (
                        event["kind"] == "MEMORY_RETRACTED"
                        and event["payload"].get("record_type") == "task_outcomes"
                    )
                )
            ),
            key=lambda event: event["sequence"],
        )
        base_events = [
            event for event in expected_events
            if event["sequence"] <= locator["base_snapshot_sequence"]
        ]
        overlay_events = [
            event for event in expected_events
            if event["sequence"] > locator["base_snapshot_sequence"]
        ]
        if locator["base_applied_correction_event_ids"] != [
            event["event_id"] for event in base_events
        ]:
            raise guard.InvalidState(
                "Task Outcome base correction snapshot is incomplete or out of sequence"
            )
        if locator["correction_event_ids"] != [event["event_id"] for event in overlay_events]:
            raise guard.InvalidState(
                "Task Outcome correction overlay is incomplete or out of sequence"
            )

        base_attribution_events = [
            event for event in base_events if event["kind"] == "ATTRIBUTION_REVISED"
        ]
        archived_attribution = _attribution_projection(archived["attribution"])
        projected_attribution = _validate_attribution_projection(
            locator["projection"]["attribution"],
            "Task Outcome locator attribution",
        )
        history = archived["attribution_history"]
        if [item["event_id"] for item in history] != [
            event["event_id"] for event in base_attribution_events
        ]:
            raise guard.InvalidState(
                "Archived attribution history disagrees with base correction events"
            )
        prior_to: dict[str, Any] | None = None
        for item, event in zip(history, base_attribution_events):
            from_attribution = _validate_attribution_projection(
                event["payload"].get("from_attribution"),
                "Base attribution from_attribution",
            )
            to_attribution = _validate_attribution_projection(
                event["payload"].get("to_attribution"),
                "Base attribution to_attribution",
            )
            if (
                _attribution_projection(item["from"]) != from_attribution
                or _attribution_projection(item["to"]) != to_attribution
                or item["reason"] != event["payload"]["reason"]
                or item["evidence_refs"] != event["evidence_refs"]
                or item["at"] != event["observed_at"]
            ):
                raise guard.InvalidState(
                    "Archived attribution history is not recoverable from its event"
                )
            if prior_to is not None and from_attribution != prior_to:
                raise guard.InvalidState("Base attribution correction chain is discontinuous")
            prior_to = to_attribution
        if prior_to is not None and prior_to != archived_attribution:
            raise guard.InvalidState(
                "Archived attribution does not match its final base correction"
            )

        base_invalidations = [
            event for event in base_events if event["kind"] == "OUTCOME_INVALIDATED_LATER"
        ]
        if len(base_invalidations) > 1 or bool(base_invalidations) != (
            archived["outcome"] == "INVALIDATED_LATER"
        ):
            raise guard.InvalidState("Archived Task Outcome invalidation state is inconsistent")
        if base_invalidations:
            event = base_invalidations[0]
            invalidation = archived["invalidation"]
            if (
                invalidation is None
                or invalidation["event_id"] != event["event_id"]
                or invalidation["reason"] != event["payload"]["reason"]
                or invalidation["evidence_refs"] != event["evidence_refs"]
                or invalidation["at"] != event["observed_at"]
                or invalidation["prior_outcome"] != event["payload"]["prior_outcome"]
            ):
                raise guard.InvalidState(
                    "Archived Task Outcome invalidation is not recoverable from its event"
                )
        base_retractions = [
            event for event in base_events if event["kind"] == "MEMORY_RETRACTED"
        ]
        if len(base_retractions) > 1 or bool(base_retractions) != archived["retracted"]:
            raise guard.InvalidState("Archived Task Outcome retraction state is inconsistent")
        if base_retractions:
            pre_retraction = copy.deepcopy(archived)
            pre_retraction["retracted"] = False
            pre_retraction["effective"] = True
            expected_subject_hash = guard.sha256_bytes(
                guard.canonical_json_bytes(pre_retraction)
            )
            if base_retractions[0]["payload"]["subject_hash"] != expected_subject_hash:
                raise guard.InvalidState(
                    "Archived Task Outcome retraction subject hash is not recoverable"
                )

        expected_projection = _task_projection(archived)
        overlay_attribution = archived_attribution
        overlay_invalidated = archived["outcome"] == "INVALIDATED_LATER"
        overlay_retracted = archived["retracted"]
        for event in overlay_events:
            if event["kind"] == "ATTRIBUTION_REVISED":
                from_attribution = _validate_attribution_projection(
                    event["payload"].get("from_attribution"),
                    "Overlay attribution from_attribution",
                )
                to_attribution = _validate_attribution_projection(
                    event["payload"].get("to_attribution"),
                    "Overlay attribution to_attribution",
                )
                if from_attribution != overlay_attribution:
                    raise guard.InvalidState(
                        "Attribution correction overlay does not start from current state"
                    )
                overlay_attribution = to_attribution
                expected_projection["attribution"] = to_attribution
            elif event["kind"] == "OUTCOME_INVALIDATED_LATER":
                if overlay_invalidated:
                    raise guard.InvalidState("Task Outcome was invalidated more than once")
                if event["payload"]["prior_outcome"] != expected_projection["outcome"]:
                    raise guard.InvalidState(
                        "Task Outcome invalidation does not start from the current outcome"
                    )
                overlay_invalidated = True
                expected_projection["outcome"] = "INVALIDATED_LATER"
            elif event["kind"] == "MEMORY_RETRACTED":
                if overlay_retracted:
                    raise guard.InvalidState("Task Outcome was retracted more than once")
                expected_subject_hash = guard.sha256_bytes(
                    guard.canonical_json_bytes(expected_projection)
                )
                if event["payload"]["subject_hash"] != expected_subject_hash:
                    raise guard.InvalidState(
                        "Task Outcome retraction does not bind the current projection"
                    )
                overlay_retracted = True
                expected_projection["retracted"] = True
                expected_projection["effective"] = False
        if overlay_attribution != projected_attribution:
            raise guard.InvalidState(
                "Task Outcome projection does not match final attribution overlay"
            )
        if expected_projection != locator["projection"]:
            raise guard.InvalidState(
                "Task Outcome locator projection is not derivable from archive plus corrections"
            )


def inspect_memory(project: str) -> dict[str, Any]:
    root, founder, _created = guard.resolve_project_root(project)
    memory_sha, _raw, registry = _registry_observation(founder)
    transaction = _transaction_observation(founder)
    if registry is not None:
        validate_registry(registry, root)
    return {
        "result": "MEMORY_INSPECTED",
        "project_root": str(root),
        "memory_sha": memory_sha,
        "memory_revision": registry.get("memory_revision") if registry else None,
        "summary": (
            {
                "task_outcomes": len(_task_records(registry["records"])),
                "current_task_outcome_details": len(registry["records"]["task_outcomes"]),
                "archived_task_outcome_summaries": len(
                    registry["records"]["task_outcome_locators"]
                ),
                "agent_performance": len(registry["derived"]["agent_performance"]),
                "skill_performance": len(registry["derived"]["skill_performance"]),
                "decision_outcomes": len(registry["records"]["decision_outcomes"]),
                "lessons": len(registry["records"]["lessons"]),
                "routing_history": len(registry["records"]["routing_history"]),
                "organization_patterns": len(registry["records"]["organization_patterns"]),
                "active_events": len(registry["active_events"]),
                "archive_segments": len(registry["archive_manifest"]),
                "archived_routing_history": sum(
                    row["record_counts"]["routing_history"]
                    for row in registry["archive_manifest"]
                ),
                "archived_task_outcomes": sum(
                    row["record_counts"]["task_outcomes"]
                    for row in registry["archive_manifest"]
                ),
            }
            if registry
            else None
        ),
        "transaction": transaction,
        "changed_paths": [],
    }


def verify_memory(project: str, *, full_archives: bool = False) -> dict[str, Any]:
    root, founder, _created = guard.resolve_project_root(project)
    memory_sha, _raw, registry = _registry_observation(founder)
    transaction = _transaction_observation(founder)
    if transaction["state"] != "none":
        raise guard.Conflict("MEMORY_RECOVERY_REQUIRED: transaction prevents verification")
    _assert_supervisor_memory_fingerprint(founder, memory_sha, registry)
    verified_archives = 0
    verified_archive_values: dict[str, dict[str, Any]] = {}
    if registry is not None:
        validate_registry(registry, root)
        if full_archives:
            _memory_root, _registry_path, archive_root, _lock_path = _memory_paths(founder)
            for row in registry["archive_manifest"]:
                archive = _read_archive(archive_root, row)
                verified_archive_values[row["filename"]] = archive
                if archive["project_binding_id"] != registry["project_binding"]["project_binding_id"]:
                    raise guard.InvalidState("Memory archive belongs to another project")
                for task_id, outcome in archive["record_segments"]["task_outcomes"].items():
                    locator = registry["records"]["task_outcome_locators"].get(task_id)
                    if (
                        locator is None
                        or locator["archive_filename"] != row["filename"]
                        or locator["archived_record_sha256"]
                        != guard.sha256_bytes(guard.canonical_json_bytes(outcome))
                    ):
                        raise guard.InvalidState(
                            "Archived Task Outcome locator/hash does not match the verified segment"
                        )
                verified_archives += 1
            _verify_retraction_audit(registry, verified_archive_values)
            _verify_task_locator_corrections(registry, verified_archive_values)
    return {
        "result": "MEMORY_VERIFIED",
        "project_root": str(root),
        "memory_sha": memory_sha,
        "memory_revision": registry.get("memory_revision") if registry else None,
        "archives_verified": verified_archives,
        "full_archives": full_archives,
        "transaction": transaction,
        "changed_paths": [],
    }


def _selector_values(selectors: dict[str, list[str]]) -> set[str]:
    values: set[str] = set()
    for key, items in selectors.items():
        if key not in {"record_types", "lesson_statuses"}:
            values.update(item.casefold() for item in items)
    return values


def _task_matches(record: dict[str, Any], selectors: dict[str, list[str]]) -> bool:
    scalar_fields = {
        "task_types": record["task_type"],
        "workstreams": record["workstream"],
        "project_stages": record["project_stage"],
        "risk_levels": record["risk_level"],
        "agent_ids": record["agent_id"],
    }
    for key, actual in scalar_fields.items():
        if selectors[key] and actual not in selectors[key]:
            return False
    list_fields = {
        "capabilities": record["capabilities"],
        "components": record["components"],
        "tags": record["tags"],
    }
    for key, actual in list_fields.items():
        if selectors[key] and not set(selectors[key]).intersection(actual):
            return False
    if selectors["skill_keys"]:
        actual_skill_keys = {
            f"{item['skill_id']}@{item['approved_version']}#{item['installed_hash']}"
            for item in record["skills"]
        }
        if not set(selectors["skill_keys"]).intersection(actual_skill_keys):
            return False
    return True


def _lesson_matches(record: dict[str, Any], selectors: dict[str, list[str]]) -> bool:
    if selectors["lesson_statuses"] and record["status"] not in selectors["lesson_statuses"]:
        return False
    values = _selector_values(selectors)
    return not values or bool(values.intersection(item.casefold() for item in record["applicability"]))


def _organization_pattern_matches(record: dict[str, Any], selectors: dict[str, list[str]]) -> bool:
    context = record["context"]
    for field in PERFORMANCE_CONTEXT_FIELDS | {"agent_ids"}:
        requested = selectors[field]
        if requested and context[field] and not set(requested).intersection(context[field]):
            return False
    return True


def _decision_matches(record: dict[str, Any], selectors: dict[str, list[str]]) -> bool:
    if selectors["decision_ids"] and record["decision_id"] not in selectors["decision_ids"]:
        return False
    if selectors["decision_ids"]:
        return True
    context_active = any(selectors[field] for field in PERFORMANCE_CONTEXT_FIELDS)
    if not context_active:
        return True
    applicability = record["applicability"]
    for field in PERFORMANCE_CONTEXT_FIELDS:
        if selectors[field] and (
            not applicability[field]
            or not set(selectors[field]).intersection(applicability[field])
        ):
            return False
    return True


def _union_index_values(index: dict[str, list[str]], values: list[str]) -> set[str]:
    result: set[str] = set()
    for value in values:
        result.update(index.get(value, []))
    return result


def _task_candidate_ids(registry: dict[str, Any], selectors: dict[str, list[str]]) -> list[str]:
    records = _task_records(registry["records"])
    indexes = registry["derived"]["indexes"]["task_outcomes"]
    mapping = {
        "task_types": "task_type",
        "capabilities": "capability",
        "components": "component",
        "workstreams": "workstream",
        "project_stages": "project_stage",
        "tags": "tag",
        "risk_levels": "risk_level",
        "agent_ids": "agent_id",
        "skill_keys": "skill_key",
    }
    groups: list[set[str]] = []
    for selector_name, index_name in mapping.items():
        values = selectors[selector_name]
        if values:
            groups.append(_union_index_values(indexes[index_name], values))
    candidates = set(records) if not groups else set.intersection(*groups)
    return sorted(candidates, key=str.casefold)


def _task_record(registry: dict[str, Any], task_id: str) -> dict[str, Any]:
    current = registry["records"]["task_outcomes"].get(task_id)
    if current is not None:
        return current
    locator = registry["records"]["task_outcome_locators"].get(task_id)
    if locator is None:
        raise guard.InvalidState(f"Task Outcome index references an unknown task: {task_id}")
    return locator["projection"]


def _lesson_candidate_ids(registry: dict[str, Any], selectors: dict[str, list[str]]) -> list[str]:
    records = registry["records"]["lessons"]
    indexes = registry["derived"]["indexes"]["lessons"]
    groups: list[set[str]] = []
    if selectors["lesson_statuses"]:
        groups.append(_union_index_values(indexes["status"], selectors["lesson_statuses"]))
    values = sorted(_selector_values(selectors))
    if values:
        groups.append(_union_index_values(indexes["applicability"], values))
    candidates = set(records) if not groups else set.intersection(*groups)
    return sorted(candidates, key=str.casefold)


def _context_is_active(selectors: dict[str, list[str]]) -> bool:
    return any(selectors[field] for field in PERFORMANCE_CONTEXT_FIELDS)


def _subject_matches_outcome(outcome: dict[str, Any], subject_kind: str, subject_id: str) -> bool:
    if subject_kind == "AGENT":
        return outcome["agent_id"] == subject_id
    return subject_id in {
        f"{item['skill_id']}@{item['approved_version']}#{item['installed_hash']}"
        for item in outcome["skills"]
    }


def _contextual_performance(
    outcomes: list[dict[str, Any]], *, subject_kind: str, subject_id: str,
    evidence_scope: str,
) -> dict[str, Any]:
    summary = _new_performance(subject_id, subject_kind)
    attribution_subjects: set[str] = {subject_id}
    ordered = sorted(outcomes, key=lambda item: (item["finalized_at"], item["task_id"]))
    for item in ordered:
        attribution = item["attribution"]
        attributed = (
            item["outcome"] in {"FAILED", "PARTIAL", "INVALIDATED_LATER"}
            and attribution["kind"] == ("AGENT" if subject_kind == "AGENT" else "SKILL")
            and attribution.get("subject_id") in attribution_subjects
        )
        _observe_performance(summary, item, attributed=attributed)
    summary["confidence"], summary["confidence_basis"] = _performance_confidence(summary["sample_count"])
    summary["evidence_label"] = _performance_label(summary)
    summary["evidence_scope"] = evidence_scope
    summary["recent_outcomes"] = [
        {
            "task_id": item["task_id"],
            "outcome": item["outcome"],
            "finalized_at": item["finalized_at"],
        }
        for item in ordered[-RECENT_ROUTING_WINDOW:]
    ]
    return summary


def _selection_material(
    registry: dict[str, Any], selectors: dict[str, list[str]], *, limit: int
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    requested_types = set(selectors["record_types"]) or {
        "task_outcomes", "lessons", "decision_outcomes"
    }
    selected: list[dict[str, Any]] = []
    scanned = 0

    def add(record_type: str, record_id: str, value: dict[str, Any]) -> None:
        nonlocal selected
        projection = copy.deepcopy(value)
        selected.append(
            {
                "record_type": record_type,
                "record_id": record_id,
                "content_sha256": guard.sha256_bytes(guard.canonical_json_bytes(projection)),
                "value": projection,
            }
        )

    if "task_outcomes" in requested_types:
        for record_id in _task_candidate_ids(registry, selectors):
            value = _task_record(registry, record_id)
            scanned += 1
            if value["effective"] and not value["retracted"] and _task_matches(value, selectors):
                add("task_outcomes", record_id, _task_projection(value))
    if "lessons" in requested_types:
        for record_id in _lesson_candidate_ids(registry, selectors):
            value = registry["records"]["lessons"][record_id]
            scanned += 1
            allowed_statuses = set(selectors["lesson_statuses"] or ["ACTIVE"])
            if not value["retracted"] and value["status"] in allowed_statuses and _lesson_matches(value, selectors):
                add("lessons", record_id, value)
    if "decision_outcomes" in requested_types:
        decision_index = registry["derived"]["indexes"]["decision_outcomes"]["decision_id"]
        decision_ids = selectors["decision_ids"] or sorted(decision_index)
        candidate_ids = sorted(
            {record_id for value in decision_ids for record_id in decision_index.get(value, [])},
            key=str.casefold,
        )
        for record_id in candidate_ids:
            value = registry["records"]["decision_outcomes"][record_id]
            scanned += 1
            if not value["retracted"] and _decision_matches(value, selectors):
                add("decision_outcomes", record_id, value)
    if "routing_history" in requested_types:
        context_sha = guard.sha256_bytes(guard.canonical_json_bytes(selectors))
        candidate_ids = registry["derived"]["indexes"]["routing_history"]["task_context_sha256"].get(
            context_sha, []
        )
        for record_id in candidate_ids:
            value = registry["records"]["routing_history"][record_id]
            scanned += 1
            if not value["retracted"] and _validate_selectors(value["task_context"]) == selectors:
                add("routing_history", record_id, value)
    if "agent_performance" in requested_types:
        agent_ids = selectors["agent_ids"] or sorted(registry["derived"]["agent_performance"])
        context_active = _context_is_active(selectors)
        matching_outcomes = [
            _task_record(registry, record_id)
            for record_id in _task_candidate_ids(registry, selectors)
            if _task_record(registry, record_id)["effective"]
            and not _task_record(registry, record_id)["retracted"]
            and _task_matches(_task_record(registry, record_id), selectors)
        ]
        for record_id in agent_ids:
            scanned += 1
            rows = [item for item in matching_outcomes if item["agent_id"] == record_id]
            if not context_active:
                rows = [
                    item for item in _task_records(registry["records"]).values()
                    if item["effective"] and not item["retracted"] and item["agent_id"] == record_id
                ]
            add(
                "agent_performance", record_id,
                _contextual_performance(
                    rows, subject_kind="AGENT", subject_id=record_id,
                    evidence_scope="CONTEXTUAL" if context_active else "LIFETIME",
                ),
            )
    if "skill_performance" in requested_types:
        skill_keys = selectors["skill_keys"] or sorted(registry["derived"]["skill_performance"])
        context_active = _context_is_active(selectors)
        matching_outcomes = [
            _task_record(registry, record_id)
            for record_id in _task_candidate_ids(registry, selectors)
            if _task_record(registry, record_id)["effective"]
            and not _task_record(registry, record_id)["retracted"]
            and _task_matches(_task_record(registry, record_id), selectors)
        ]
        for record_id in skill_keys:
            scanned += 1
            rows = [
                item for item in matching_outcomes
                if _subject_matches_outcome(item, "SKILL_EXACT_VERSION", record_id)
            ]
            if not context_active:
                rows = [
                    item for item in _task_records(registry["records"]).values()
                    if item["effective"] and not item["retracted"]
                    and _subject_matches_outcome(item, "SKILL_EXACT_VERSION", record_id)
                ]
            add(
                "skill_performance", record_id,
                _contextual_performance(
                    rows, subject_kind="SKILL_EXACT_VERSION", subject_id=record_id,
                    evidence_scope="CONTEXTUAL" if context_active else "LIFETIME",
                ),
            )
    if "team_patterns" in requested_types:
        for record_id, value in registry["derived"]["team_patterns"].items():
            scanned += 1
            if not selectors["agent_ids"] or set(selectors["agent_ids"]).issubset(value["team_agent_ids"]):
                add("team_patterns", record_id, value)
    if "organization_patterns" in requested_types:
        for record_id, value in sorted(registry["records"]["organization_patterns"].items()):
            scanned += 1
            if not value["retracted"] and _organization_pattern_matches(value, selectors):
                add("organization_patterns", record_id, value)
    selected.sort(key=lambda row: (row["record_type"], row["record_id"].casefold()))
    selected = selected[:limit]
    material = {
        "selectors": selectors,
        "records": [
            {key: row[key] for key in ("record_type", "record_id", "content_sha256")}
            for row in selected
        ],
    }
    stats = {
        "scanned_records": scanned,
        "returned_records": len(selected),
        "archive_opened": False,
        "returned_bytes": len(guard.canonical_json_bytes(selected)),
    }
    return selected, {"material": material, "stats": stats}


def memory_selection(
    founder: Path,
    selectors: dict[str, Any],
    *,
    limit: int = MAX_SYNC_RECORDS,
) -> dict[str, Any]:
    root = founder.parent
    if _transaction_observation(founder)["state"] != "none":
        raise guard.Conflict("MEMORY_RECOVERY_REQUIRED: Memory transaction prevents selection")
    normalized = _validate_selectors(selectors)
    if not isinstance(limit, int) or not 0 <= limit <= MAX_SYNC_RECORDS:
        raise guard.InvalidState(f"Memory sync limit must be between 0 and {MAX_SYNC_RECORDS}")
    memory_sha, _raw, registry = _registry_observation(founder)
    _assert_supervisor_memory_fingerprint(founder, memory_sha, registry)
    if registry is None:
        empty = {"selectors": normalized, "records": []}
        return {
            "state": "ABSENT",
            "memory_revision": "ABSENT",
            "memory_state_sha256": "ABSENT",
            "memory_query_sha256": guard.sha256_bytes(guard.canonical_json_bytes(normalized)),
            "memory_selection_sha256": guard.sha256_bytes(guard.canonical_json_bytes(empty)),
            "records": [],
            "query_stats": {"scanned_records": 0, "returned_records": 0, "archive_opened": False, "returned_bytes": 0},
        }
    validate_registry(registry, root)
    selected, details = _selection_material(registry, normalized, limit=limit)
    if details["stats"]["returned_bytes"] > MAX_SYNC_BYTES:
        raise guard.Conflict("MEMORY_SYNC selection exceeds the bounded payload limit")
    return {
        "state": "CURRENT",
        "memory_revision": registry["memory_revision"],
        "memory_state_sha256": memory_sha,
        "memory_query_sha256": guard.sha256_bytes(guard.canonical_json_bytes(normalized)),
        "memory_selection_sha256": guard.sha256_bytes(guard.canonical_json_bytes(details["material"])),
        "records": selected,
        "query_stats": details["stats"],
    }


def query_memory(project: str, *, selectors: dict[str, Any], limit: int = 20) -> dict[str, Any]:
    if not isinstance(limit, int) or not 1 <= limit <= MAX_QUERY_LIMIT:
        raise guard.InvalidState(f"query limit must be between 1 and {MAX_QUERY_LIMIT}")
    root, founder, _created = guard.resolve_project_root(project)
    if _transaction_observation(founder)["state"] != "none":
        raise guard.Conflict("MEMORY_RECOVERY_REQUIRED: Memory transaction prevents query")
    normalized = _validate_selectors(selectors)
    memory_sha, _raw, registry = _registry_observation(founder)
    _assert_supervisor_memory_fingerprint(founder, memory_sha, registry)
    if registry is None:
        selection = {
            "state": "ABSENT",
            "memory_revision": "ABSENT",
            "memory_state_sha256": "ABSENT",
            "memory_query_sha256": guard.sha256_bytes(guard.canonical_json_bytes(normalized)),
            "memory_selection_sha256": guard.sha256_bytes(
                guard.canonical_json_bytes({"selectors": normalized, "records": []})
            ),
            "records": [],
            "query_stats": {
                "scanned_records": 0,
                "returned_records": 0,
                "archive_opened": False,
                "returned_bytes": 0,
            },
        }
    else:
        validate_registry(registry, root)
        selected, details = _selection_material(registry, normalized, limit=limit)
        if details["stats"]["returned_bytes"] > MAX_QUERY_BYTES:
            raise guard.Conflict("Memory query result exceeds the bounded payload limit")
        selection = {
            "state": "CURRENT",
            "memory_revision": registry["memory_revision"],
            "memory_state_sha256": memory_sha,
            "memory_query_sha256": guard.sha256_bytes(guard.canonical_json_bytes(normalized)),
            "memory_selection_sha256": guard.sha256_bytes(guard.canonical_json_bytes(details["material"])),
            "records": selected,
            "query_stats": details["stats"],
        }
    return {
        "result": "MEMORY_QUERY_RESULT",
        "project_root": str(root),
        **selection,
        "changed_paths": [],
    }


def route_evidence(
    project: str,
    *,
    context: dict[str, Any],
    candidate_agent_ids: list[str],
    candidate_skill_keys: list[str] | None = None,
) -> dict[str, Any]:
    root, founder, _created = guard.resolve_project_root(project)
    if _transaction_observation(founder)["state"] != "none":
        raise guard.Conflict("MEMORY_RECOVERY_REQUIRED: Memory transaction prevents routing")
    normalized = _validate_selectors(context, allow_empty=False)
    if any(normalized[field] for field in QUERY_CONTROL_FIELDS):
        raise guard.InvalidState(
            "route-evidence context accepts only task Performance Context selectors; "
            "Agent and Skill candidates must be passed separately"
        )
    candidates = _bounded_list(candidate_agent_ids, "candidate_agent_ids", identifiers=True)
    skill_candidates = _bounded_list(candidate_skill_keys or [], "candidate_skill_keys", item_max=512)
    _memory_sha, _raw, registry = _registry_observation(founder)
    _assert_supervisor_memory_fingerprint(founder, _memory_sha, registry)
    trusted_skill_keys: set[str] = set()
    try:
        _lock_sha, _lock_raw, skill_lock, _registry_raw = skills_api.read_registry_pair(founder)
        skill_transaction = skills_api._transaction_observation(founder)
        if skill_transaction["state"] != "none":
            raise guard.Conflict("Skill Registry transaction prevents routing")
        if skill_lock is not None:
            # Reuse the authoritative binding resolver so tree hash, runtime
            # visibility and project-policy checks remain stronger than
            # historical performance.  With no concrete binding context this
            # proves integrity/trust only; scope remains explicitly unverified.
            for entry in skill_lock["skills"].values():
                scope = entry["scoped_bindings"]
                try:
                    baseline, selected, _selected_sha = skills_api.resolve_bindings(
                        founder,
                        [entry["skill_id"]],
                        agent_id=scope["agent_ids"][0] if scope["agent_ids"] else None,
                        workstream=scope["workstreams"][0] if scope["workstreams"] else None,
                        thread_record_id=(
                            scope["thread_record_ids"][0]
                            if scope["thread_record_ids"] else None
                        ),
                        task_id=scope["task_ids"][0] if scope["task_ids"] else None,
                    )
                except guard.GuardError:
                    continue
                if baseline.get("skill_lock_sha256") != _lock_sha or len(selected) != 1:
                    # A concurrent authoritative update invalidated the
                    # preflight snapshot.  Never mix entry bytes from one Lock
                    # with integrity proof from another.
                    raise guard.Conflict("Skill Lock changed during Memory routing")
                verified = selected[0]
                trusted_skill_keys.add(
                    f"{verified['skill_id']}@{verified['approved_version']}#"
                    f"{verified['installed_hash']}"
                )
    except guard.GuardError:
        # Performance remains visible as history, but selection eligibility
        # fails closed until the independent Skill trust authority is valid.
        trusted_skill_keys = set()
    if registry is None:
        agent_rows = [
            {"agent_id": item, "evidence_state": "UNPROVEN", "confidence": "LOW", "matching_outcomes": []}
            for item in candidates
        ]
        skill_rows = [
            {
                "skill_key": item,
                "evidence_state": "UNPROVEN",
                "confidence": "LOW",
                "matching_outcomes": [],
                "trust_eligibility": "LOCK_TRUSTED_BINDING_UNVERIFIED" if item in trusted_skill_keys else "INELIGIBLE_OR_UNVERIFIED",
            }
            for item in skill_candidates
        ]
    else:
        validate_registry(registry, root)
        matches = [
            _task_record(registry, record_id)
            for record_id in _task_candidate_ids(registry, normalized)
            if _task_record(registry, record_id)["effective"]
            and not _task_record(registry, record_id)["retracted"]
            and _task_matches(_task_record(registry, record_id), normalized)
        ]
        matches.sort(key=lambda item: (item["finalized_at"], item["task_id"]))
        agent_rows = []
        for agent_id in candidates:
            related = [item for item in matches if item["agent_id"] == agent_id]
            summary = _contextual_performance(
                related, subject_kind="AGENT", subject_id=agent_id, evidence_scope="CONTEXTUAL"
            )
            recent = related[-RECENT_ROUTING_WINDOW:]
            recent_summary = _contextual_performance(
                recent, subject_kind="AGENT", subject_id=agent_id, evidence_scope="RECENT_CONTEXTUAL"
            )
            agent_rows.append(
                {
                    "agent_id": agent_id,
                    "evidence_state": summary["evidence_label"],
                    "confidence": summary["confidence"],
                    "matching_outcomes": [item["task_id"] for item in related[-20:]],
                    "recent_matching_outcomes": summary["recent_outcomes"],
                    "matching_first_pass": sum(item["outcome"] == "SUCCESS_FIRST_PASS" for item in related),
                    "matching_after_revision": sum(item["outcome"] == "SUCCESS_AFTER_REVISION" for item in related),
                    "matching_attributed_failures": summary["attributed_failures"],
                    "recent_first_pass": recent_summary["outcomes"]["SUCCESS_FIRST_PASS"],
                    "recent_after_revision": recent_summary["outcomes"]["SUCCESS_AFTER_REVISION"],
                    "recent_attributed_failures": recent_summary["attributed_failures"],
                }
            )
        agent_rows.sort(
            key=lambda row: (
                row.get("recent_attributed_failures", 0),
                -row.get("recent_first_pass", 0),
                -row.get("recent_after_revision", 0),
                -row.get("matching_first_pass", 0),
                row.get("matching_attributed_failures", 0),
                -row.get("matching_after_revision", 0),
                row["agent_id"].casefold(),
            )
        )
        skill_rows = []
        for skill_key in skill_candidates:
            related = [
                item for item in matches
                if skill_key in {
                    f"{skill['skill_id']}@{skill['approved_version']}#{skill['installed_hash']}"
                    for skill in item["skills"]
                }
            ]
            summary = _contextual_performance(
                related, subject_kind="SKILL_EXACT_VERSION", subject_id=skill_key,
                evidence_scope="CONTEXTUAL",
            )
            recent = related[-RECENT_ROUTING_WINDOW:]
            recent_summary = _contextual_performance(
                recent, subject_kind="SKILL_EXACT_VERSION", subject_id=skill_key,
                evidence_scope="RECENT_CONTEXTUAL",
            )
            skill_rows.append(
                {
                    "skill_key": skill_key,
                    "evidence_state": summary["evidence_label"],
                    "confidence": summary["confidence"],
                    "matching_outcomes": [item["task_id"] for item in related[-20:]],
                    "recent_matching_outcomes": summary["recent_outcomes"],
                    "matching_first_pass": sum(item["outcome"] == "SUCCESS_FIRST_PASS" for item in related),
                    "matching_attributed_failures": summary["attributed_failures"],
                    "recent_first_pass": recent_summary["outcomes"]["SUCCESS_FIRST_PASS"],
                    "recent_after_revision": recent_summary["outcomes"]["SUCCESS_AFTER_REVISION"],
                    "recent_attributed_failures": recent_summary["attributed_failures"],
                    "trust_eligibility": (
                        "LOCK_TRUSTED_BINDING_UNVERIFIED" if skill_key in trusted_skill_keys else "INELIGIBLE_OR_UNVERIFIED"
                    ),
                }
            )
        skill_rows.sort(
            key=lambda row: (
                row["trust_eligibility"] != "LOCK_TRUSTED_BINDING_UNVERIFIED",
                row.get("recent_attributed_failures", 0),
                -row.get("recent_first_pass", 0),
                -row.get("recent_after_revision", 0),
                -row.get("matching_first_pass", 0),
                row.get("matching_attributed_failures", 0),
                row["skill_key"].casefold(),
            )
        )
    return {
        "result": "ROUTING_EVIDENCE",
        "project_root": str(root),
        "context": normalized,
        "agents": agent_rows,
        "skills": skill_rows,
        "constraints": {
            "historical_evidence_only": True,
            "cold_start_exploration_required": all(row["evidence_state"] == "UNPROVEN" for row in agent_rows),
            "permissions_unchanged": True,
            "skill_trust_unchanged": True,
            "fixed_review_gates_unchanged": True,
        },
        "changed_paths": [],
    }


def _metadata_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        getattr(metadata, "st_ctime_ns", int(metadata.st_ctime * 1_000_000_000)),
    )


class _MemoryDirectoryFence:
    """Pin one direct directory during a cooperative Memory transaction."""

    def __init__(self, path: Path, handle: int, identity: tuple[int, ...], *, windows: bool):
        self.path = path
        self.handle = handle
        self.identity = identity
        self.windows = windows

    @classmethod
    def acquire(cls, path: Path, label: str) -> "_MemoryDirectoryFence":
        before = _direct_directory(path, label)
        if os.name == "nt":
            handle = _KERNEL32.CreateFileW(
                str(path),
                _WIN_GENERIC_READ,
                # Child files must remain writable during the transaction, but
                # omitting FILE_SHARE_DELETE keeps the directory identity from
                # being renamed or replaced underneath those writes.
                _WIN_FILE_SHARE_READ | _WIN_FILE_SHARE_WRITE,
                None,
                _WIN_OPEN_EXISTING,
                _WIN_FILE_FLAG_OPEN_REPARSE_POINT | _WIN_FILE_FLAG_BACKUP_SEMANTICS,
                None,
            )
            if handle == _INVALID_WIN_HANDLE:
                raise guard.Conflict(f"Cannot pin {label}: {ctypes.WinError(ctypes.get_last_error())}")
            handle = int(handle)
            try:
                information = _WinHandleInformation()
                if not _KERNEL32.GetFileInformationByHandle(handle, ctypes.byref(information)):
                    raise guard.Conflict(f"Cannot inspect pinned {label}")
                if information.attributes & _WIN_FILE_ATTRIBUTE_REPARSE_POINT:
                    raise guard.InvalidState(f"{label} became a reparse point")
                identity = (
                    int(information.volume_serial),
                    (int(information.file_index_high) << 32) | int(information.file_index_low),
                    int(information.attributes),
                )
                after = _direct_directory(path, label)
                if before.st_ino and identity[1] and before.st_ino != identity[1]:
                    raise guard.Conflict(f"{label} identity changed during pin")
                if _metadata_identity(before) != _metadata_identity(after):
                    raise guard.Conflict(f"{label} metadata changed during pin")
                return cls(path, handle, identity, windows=True)
            except Exception:
                _KERNEL32.CloseHandle(handle)
                raise
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            identity = _metadata_identity(opened)
            if identity != _metadata_identity(before):
                raise guard.Conflict(f"{label} identity changed during pin")
            return cls(path, descriptor, identity, windows=False)
        except Exception:
            os.close(descriptor)
            raise

    def assert_current(self, label: str) -> None:
        current = _direct_directory(self.path, label)
        if self.windows:
            information = _WinHandleInformation()
            if not _KERNEL32.GetFileInformationByHandle(self.handle, ctypes.byref(information)):
                raise guard.Conflict(f"Cannot inspect pinned {label}")
            identity = (
                int(information.volume_serial),
                (int(information.file_index_high) << 32) | int(information.file_index_low),
                int(information.attributes),
            )
            if identity != self.identity or (current.st_ino and identity[1] and current.st_ino != identity[1]):
                raise guard.Conflict(f"{label} changed during Memory transaction")
        elif _metadata_identity(current) != self.identity or _metadata_identity(os.fstat(self.handle)) != self.identity:
            raise guard.Conflict(f"{label} changed during Memory transaction")

    def close(self) -> None:
        if self.handle < 0:
            return
        if self.windows:
            _KERNEL32.CloseHandle(self.handle)
        else:
            os.close(self.handle)
        self.handle = -1


def _atomic_create_bytes(path: Path, content: bytes) -> None:
    descriptor: int | None = None
    created = False
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError as exc:
        raise guard.Conflict(f"Atomic Memory path already exists: {path}") from exc
    except Exception:
        if descriptor is not None:
            os.close(descriptor)
        if created:
            try:
                path.unlink()
            except OSError:
                pass
        raise


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


def _transaction_observation(founder: Path) -> dict[str, Any]:
    memory_root, _registry_path, _archive_root, lock_path = _memory_paths(founder)
    if not memory_root.exists():
        return {"state": "none"}
    _direct_directory(memory_root, "Memory directory")
    if not lock_path.exists():
        return {"state": "none"}
    raw = _read_direct_bytes(lock_path, "Memory transaction lock", max_bytes=64 * 1024)
    value = _strict_json_loads(raw, label="Memory transaction lock")
    required = {
        "project_root",
        "project_binding_id",
        "owner",
        "nonce",
        "operation",
        "expected_state_sha",
        "old_memory_sha",
        "target_memory_sha",
        "target_memory_revision",
        "archive_plan",
        "created_at",
    }
    if set(value) != required:
        raise guard.InvalidState("Memory transaction lock fields are malformed")
    _identifier(value.get("owner"), "Memory lock owner")
    _identifier(value.get("nonce"), "Memory lock nonce")
    _identifier(value.get("operation"), "Memory lock operation")
    _sha(value.get("expected_state_sha"), "Memory lock expected_state_sha")
    _sha_or_absent(value.get("old_memory_sha"), "Memory lock old_memory_sha")
    _sha(value.get("target_memory_sha"), "Memory lock target_memory_sha")
    _identifier(value.get("target_memory_revision"), "Memory lock target revision")
    if value.get("archive_plan") is not None:
        plan = value["archive_plan"]
        if not isinstance(plan, dict) or set(plan) != {"filename", "content_sha256"}:
            raise guard.InvalidState("Memory lock archive plan is malformed")
        _safe_archive_name(plan.get("filename"))
        _sha(plan.get("content_sha256"), "Memory lock archive hash")
    _timestamp(value.get("created_at"), "Memory lock created_at")
    return {"state": "recovery-required", **value}


def _ensure_memory_root(founder: Path) -> tuple[Path, bool]:
    memory_root, _registry_path, _archive_root, _lock_path = _memory_paths(founder)
    created = False
    if not memory_root.exists():
        try:
            memory_root.mkdir()
            created = True
        except FileExistsError:
            pass
    _direct_directory(memory_root, "Memory directory")
    return memory_root, created


def _cleanup_empty_memory_root(memory_root: Path, created: bool) -> None:
    if not created:
        return
    try:
        memory_root.rmdir()
    except OSError:
        pass


def _assert_operating(project: str) -> None:
    authorization = strategy.authorize_action(project, action="candidate-bound-work")
    if not authorization.get("allowed") or authorization.get("gate") != "OPERATING":
        raise guard.Conflict("Organization Memory mutation requires Strategic Gate OPERATING")


MutationResult = tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None]


def _mutate_memory(
    project: str,
    *,
    owner: str,
    activation_token: str,
    expected_state_sha: str,
    expected_memory_sha: str,
    operation: str,
    mutate: Callable[[dict[str, Any], Path], MutationResult],
    allow_initialize: bool = False,
) -> dict[str, Any]:
    owner = _identifier(owner, "Memory writer owner")
    activation_token = _text(activation_token, "activation_token")
    expected_state_sha = _sha(expected_state_sha, "expected_state_sha")
    expected_memory_sha = _sha_or_absent(expected_memory_sha, "expected_memory_sha")
    operation = _identifier(operation, "Memory operation")
    root, founder, _created = guard.resolve_project_root(project)
    fence = guard.verify_fence(str(root), owner=owner, activation_token=activation_token)
    if fence["state_sha"] != expected_state_sha:
        raise guard.Conflict("Supervisor state CAS mismatch before Memory mutation")
    _assert_operating(str(root))
    if _transaction_observation(founder)["state"] != "none":
        raise guard.Conflict("MEMORY_RECOVERY_REQUIRED: transaction lock exists")
    old_sha, old_raw, observed = _registry_observation(founder)
    if old_sha != expected_memory_sha:
        raise guard.Conflict(
            f"Memory CAS mismatch: expected {expected_memory_sha}, observed {old_sha}"
        )
    if observed is not None:
        validate_registry(observed, root)
        working = copy.deepcopy(observed)
    else:
        if not allow_initialize:
            raise guard.Conflict(
                "FIRST_ACCEPTED_TYPED_FACT_REQUIRED: this operation cannot initialize Memory"
            )
        working = _initial_registry(root)
    target, details, archive_plan = mutate(working, founder)
    target["previous_memory_sha256"] = old_sha
    target["memory_revision"] = guard.new_revision("MR")
    target["updated_at"] = guard.utc_now()
    target["derived"] = _derive_performance(target["records"])
    validate_registry(target, root)
    target_raw = guard.canonical_json_bytes(target)
    target_sha = guard.sha256_bytes(target_raw)
    if target_sha == old_sha:
        raise guard.Conflict("Memory mutation produced no state change")
    archive_lock_plan = None
    if archive_plan is not None:
        if set(archive_plan) != {"filename", "raw", "content_sha256"}:
            raise guard.InvalidState("Memory archive plan is malformed")
        _safe_archive_name(archive_plan["filename"])
        if not isinstance(archive_plan["raw"], bytes) or len(archive_plan["raw"]) > MAX_ARCHIVE_BYTES:
            raise guard.InvalidState("Memory archive plan bytes are invalid")
        if guard.sha256_bytes(archive_plan["raw"]) != archive_plan["content_sha256"]:
            raise guard.InvalidState("Memory archive plan hash mismatch")
        archive_lock_plan = {
            "filename": archive_plan["filename"],
            "content_sha256": archive_plan["content_sha256"],
        }

    commit_mutex = guard.acquire_governance_commit_mutex(
        str(root), operation=f"memory-registry:{operation}"
    )
    try:
        memory_root, memory_root_created = _ensure_memory_root(founder)
        memory_root_fence = _MemoryDirectoryFence.acquire(memory_root, "Memory directory")
    except Exception:
        commit_mutex.close()
        raise
    _memory_root, registry_path, archive_root, lock_path = _memory_paths(founder)
    nonce = f"ML-{secrets.token_hex(12)}"
    lock_value = {
        "project_root": str(root),
        "project_binding_id": _project_binding_id(root),
        "owner": owner,
        "nonce": nonce,
        "operation": operation,
        "expected_state_sha": expected_state_sha,
        "old_memory_sha": old_sha,
        "target_memory_sha": target_sha,
        "target_memory_revision": target["memory_revision"],
        "archive_plan": archive_lock_plan,
        "created_at": guard.utc_now(),
    }
    lock_created = False
    archive_created = False
    archive_root_created = False
    registry_replaced = False
    release_lock = True
    try:
        memory_root_fence.assert_current("Memory directory")
        _atomic_create_bytes(lock_path, guard.canonical_json_bytes(lock_value))
        lock_created = True
        memory_root_fence.assert_current("Memory directory")
        confirmed_fence = guard.verify_fence(str(root), owner=owner, activation_token=activation_token)
        if confirmed_fence["state_sha"] != expected_state_sha:
            raise guard.Conflict("Supervisor state changed after Memory lock acquisition")
        _assert_operating(str(root))
        confirmed_sha, _confirmed_raw, confirmed = _registry_observation(founder)
        if confirmed_sha != old_sha:
            raise guard.Conflict("Memory CAS changed after transaction lock acquisition")
        if confirmed is not None:
            validate_registry(confirmed, root)
        if archive_plan is not None:
            if not archive_root.exists():
                archive_root.mkdir()
                archive_root_created = True
            archive_fence = _MemoryDirectoryFence.acquire(archive_root, "Memory archive directory")
            try:
                archive_fence.assert_current("Memory archive directory")
                _atomic_create_bytes(archive_root / archive_plan["filename"], archive_plan["raw"])
                archive_created = True
                archive_fence.assert_current("Memory archive directory")
                reread = _read_direct_bytes(
                    archive_root / archive_plan["filename"],
                    "Memory archive segment",
                    max_bytes=MAX_ARCHIVE_BYTES,
                )
                if guard.sha256_bytes(reread) != archive_plan["content_sha256"]:
                    raise guard.InvalidState("Memory archive changed after creation")
            finally:
                archive_fence.close()
        memory_root_fence.assert_current("Memory directory")
        _atomic_replace_bytes(registry_path, target_raw)
        registry_replaced = True
        memory_root_fence.assert_current("Memory directory")
        observed_target_sha, _observed_target_raw, observed_target = _registry_observation(founder)
        if observed_target_sha != target_sha or observed_target is None:
            raise MemoryPartialCommit(
                "Memory replacement could not be verified",
                changed_paths=[str(registry_path), str(lock_path)],
                recovery_action="recover-memory-lock",
            )
        try:
            checkpoint = guard.checkpoint_active(
                str(root),
                owner=owner,
                activation_token=activation_token,
                expected_state_sha=expected_state_sha,
                _commit_mutex_held=True,
            )
        except Exception as exc:
            current_state_sha, _state = guard.state_observation(founder / guard.STATE_NAME)
            if current_state_sha == expected_state_sha:
                try:
                    if old_raw is None:
                        _direct_file(registry_path, "Memory rollback target")
                        registry_path.unlink()
                    else:
                        _atomic_replace_bytes(registry_path, old_raw)
                    if archive_created and archive_plan is not None:
                        archive_path = archive_root / archive_plan["filename"]
                        if guard.sha256_bytes(_read_direct_bytes(archive_path, "Memory rollback archive")) == archive_plan["content_sha256"]:
                            archive_path.unlink()
                            archive_created = False
                    if archive_root_created:
                        try:
                            archive_root.rmdir()
                            archive_root_created = False
                        except OSError:
                            pass
                    raise exc
                except MemoryPartialCommit:
                    raise
                except Exception as rollback_exc:
                    if rollback_exc is exc:
                        raise
                    release_lock = False
                    raise MemoryPartialCommit(
                        "Memory checkpoint failed and rollback was not provable",
                        changed_paths=[str(registry_path), str(lock_path)],
                        recovery_action="recover-memory-lock",
                    ) from rollback_exc
            release_lock = False
            raise MemoryPartialCommit(
                "Memory checkpoint outcome is uncertain; preserve the transaction lock",
                changed_paths=[str(registry_path), str(lock_path)],
                recovery_action="recover-memory-lock",
            ) from exc
        result = {
            "result": operation,
            "mode": "ACTIVE",
            "owner": owner,
            "project_root": str(root),
            "memory_revision": target["memory_revision"],
            "memory_sha": target_sha,
            "state_sha": checkpoint["state_sha"],
            "details": details,
            "changed_paths": [str(registry_path), str(founder / guard.STATE_NAME), str(founder / guard.LOCK_NAME)],
        }
        if archive_plan is not None:
            result["changed_paths"].append(str(archive_root / archive_plan["filename"]))
        return result
    except MemoryPartialCommit:
        release_lock = False
        raise
    except Exception:
        if registry_replaced:
            # A post-replace failure is safe only when the checkpoint handler
            # above already performed a provable rollback.  Otherwise preserve
            # the transaction fence for explicit recovery.
            current_sha, _current_raw, _current_registry = _registry_observation(founder)
            if current_sha == target_sha:
                release_lock = False
                raise MemoryPartialCommit(
                    "Memory target exists after an interrupted commit; explicit recovery is required",
                    changed_paths=[str(registry_path), str(lock_path)],
                    recovery_action="recover-memory-lock",
                )
        if archive_created and archive_plan is not None:
            try:
                path = archive_root / archive_plan["filename"]
                if guard.sha256_bytes(_read_direct_bytes(path, "Memory failed archive")) == archive_plan["content_sha256"]:
                    path.unlink()
                    archive_created = False
            except Exception:
                release_lock = False
                raise MemoryPartialCommit(
                    "Memory mutation failed and orphan archive cleanup was not provable",
                    changed_paths=[str(lock_path), str(archive_root)],
                    recovery_action="recover-memory-lock",
                )
        if archive_root_created:
            try:
                archive_root.rmdir()
                archive_root_created = False
            except OSError:
                pass
        raise
    finally:
        try:
            if lock_created and release_lock:
                try:
                    memory_root_fence.assert_current("Memory directory")
                    raw = _read_direct_bytes(lock_path, "Memory transaction lock", max_bytes=64 * 1024)
                    lock = _strict_json_loads(raw, label="Memory transaction lock")
                    if lock.get("owner") != owner or lock.get("nonce") != nonce:
                        raise guard.Conflict("Memory transaction lock belongs to another operation")
                    lock_path.unlink()
                except Exception as exc:
                    raise MemoryPartialCommit(
                        "Memory transaction completed but its lock could not be released",
                        changed_paths=[str(lock_path)],
                        recovery_action="recover-memory-lock",
                    ) from exc
            memory_root_fence.close()
            _cleanup_empty_memory_root(memory_root, memory_root_created)
        finally:
            commit_mutex.close()


def _normalize_attribution(value: Any) -> dict[str, Any]:
    required = {"kind", "subject_id", "confidence", "evidence_refs"}
    if not isinstance(value, dict) or set(value) != required:
        raise guard.InvalidState("attribution must contain the exact structured fields")
    kind = value.get("kind")
    if kind not in ATTRIBUTIONS:
        raise guard.InvalidState("attribution kind or confidence is invalid")
    result = {
        "kind": kind,
        "subject_id": _attribution_subject(kind, value.get("subject_id"), "attribution subject_id"),
        "confidence": value.get("confidence"),
        "evidence_refs": _bounded_list(value.get("evidence_refs"), "attribution evidence_refs", item_max=512),
    }
    if result["confidence"] not in CONFIDENCE_LEVELS:
        raise guard.InvalidState("attribution kind or confidence is invalid")
    if result["kind"] in {"AGENT", "SKILL"} and result["subject_id"] is None:
        raise guard.InvalidState("AGENT or SKILL attribution requires an exact subject_id")
    return result


def _assert_exact_skill_attribution(
    attribution: dict[str, Any], skills: list[dict[str, Any]]
) -> None:
    if attribution["kind"] != "SKILL":
        return
    exact_keys = {
        f"{skill['skill_id']}@{skill['approved_version']}#{skill['installed_hash']}"
        for skill in skills
    }
    if attribution["subject_id"] not in exact_keys:
        raise guard.InvalidState(
            "SKILL attribution subject_id must be an exact task Skill version/hash key"
        )


def _normalize_skill(value: Any) -> dict[str, Any]:
    required = {"skill_id", "approved_version", "commit_sha", "content_hash", "installed_hash", "entry_revision"}
    if not isinstance(value, dict) or set(value) != required:
        raise guard.InvalidState("Task outcome Skill identity is malformed")
    result = {
        "skill_id": _identifier(value.get("skill_id"), "Skill skill_id"),
        "approved_version": _text(value.get("approved_version"), "Skill approved_version", max_length=128),
        "commit_sha": value.get("commit_sha"),
        "content_hash": _sha(value.get("content_hash"), "Skill content_hash"),
        "installed_hash": _sha(value.get("installed_hash"), "Skill installed_hash"),
        "entry_revision": _identifier(value.get("entry_revision"), "Skill entry_revision"),
    }
    if result["commit_sha"] is not None and (
        not isinstance(result["commit_sha"], str)
        or not re.fullmatch(r"[0-9a-f]{40}", result["commit_sha"])
    ):
        raise guard.InvalidState("Skill commit_sha must be null or exact lowercase 40-hex")
    return result


def record_task_outcome(
    project: str,
    *,
    owner: str,
    activation_token: str,
    expected_state_sha: str,
    expected_memory_sha: str,
    outcome: dict[str, Any],
) -> dict[str, Any]:
    required = {
        "task_id", "agent_id", "thread_record_id", "thread_generation", "workstream",
        "project_stage", "task_type", "capabilities", "components", "tags",
        "team_agent_ids", "skills", "risk_level", "outcome", "revision_count",
        "revision_severity", "acceptance_result", "review_result", "integration_result",
        "attribution", "evidence_refs", "retention", "finalized_at",
    }
    if not isinstance(outcome, dict) or set(outcome) != required:
        raise guard.InvalidState("record-outcome requires the exact finalized outcome schema")
    if outcome.get("outcome") not in OUTCOMES - {"INVALIDATED_LATER"}:
        raise guard.InvalidState("Initial task outcome may not be INVALIDATED_LATER")
    if outcome.get("acceptance_result") not in ACCEPTANCE_RESULTS:
        raise guard.InvalidState("Task outcome has not been accepted")
    if outcome.get("review_result") == "UNKNOWN" or outcome.get("integration_result") == "UNKNOWN":
        raise guard.InvalidState("Reviewer and Integration disposition must be explicit or NOT_REQUIRED")
    evidence_refs = _bounded_list(outcome.get("evidence_refs"), "task outcome evidence_refs", item_max=512)
    if not evidence_refs:
        raise guard.InvalidState("Finalized task outcome requires observable evidence")
    task_id = _identifier(outcome.get("task_id"), "task_id")
    agent_id = _identifier(outcome.get("agent_id"), "agent_id")
    thread_record_id = _optional_text(outcome.get("thread_record_id"), "thread_record_id", max_length=128)
    thread_generation = outcome.get("thread_generation")
    if thread_generation is not None and (not isinstance(thread_generation, int) or thread_generation < 1):
        raise guard.InvalidState("thread_generation must be null or a positive integer")
    skills = [_normalize_skill(item) for item in outcome.get("skills", [])]
    if len(skills) > 16:
        raise guard.InvalidState("Task outcome has too many Skills")
    normalized = {
        "task_id": task_id,
        "agent_id": agent_id,
        "thread_record_id": thread_record_id,
        "thread_generation": thread_generation,
        "workstream": _identifier(outcome.get("workstream"), "workstream"),
        "project_stage": _identifier(outcome.get("project_stage"), "project_stage"),
        "task_type": _identifier(outcome.get("task_type"), "task_type"),
        "capabilities": _bounded_list(outcome.get("capabilities"), "capabilities", identifiers=True),
        "components": _bounded_list(outcome.get("components"), "components", identifiers=True),
        "tags": _bounded_list(outcome.get("tags"), "tags", identifiers=True),
        "team_agent_ids": _bounded_list(outcome.get("team_agent_ids"), "team_agent_ids", identifiers=True),
        "skills": skills,
        "risk_level": _identifier(outcome.get("risk_level"), "risk_level"),
        "outcome": outcome["outcome"],
        "revision_count": outcome.get("revision_count"),
        "revision_severity": outcome.get("revision_severity"),
        "acceptance_result": outcome.get("acceptance_result"),
        "review_result": outcome.get("review_result"),
        "integration_result": outcome.get("integration_result"),
        "attribution": _normalize_attribution(outcome.get("attribution")),
        "evidence_refs": evidence_refs,
        "retention": outcome.get("retention"),
        "finalized_at": _timestamp(outcome.get("finalized_at"), "finalized_at"),
        "source_event_id": "PENDING",
        "effective": True,
        "retracted": False,
        "invalidation": None,
        "attribution_history": [],
    }
    _assert_exact_skill_attribution(normalized["attribution"], skills)
    _validate_task_outcome(normalized, task_id)

    def mutate(registry: dict[str, Any], _founder: Path) -> MutationResult:
        if (
            task_id in registry["records"]["task_outcomes"]
            or task_id in registry["records"]["task_outcome_locators"]
        ):
            raise guard.Conflict("Task outcome already exists; use invalidation or attribution revision")
        event = _new_event(
            registry,
            kind="TASK_OUTCOME_FINALIZED",
            subject_id=task_id,
            actor=owner,
            evidence_refs=evidence_refs,
            payload={
                "outcome": normalized["outcome"],
                "agent_id": agent_id,
                "task_type": normalized["task_type"],
                "revision_severity": normalized["revision_severity"],
                "acceptance_result": normalized["acceptance_result"],
                "review_result": normalized["review_result"],
                "integration_result": normalized["integration_result"],
                "attribution_kind": normalized["attribution"]["kind"],
            },
            retention=normalized["retention"],
        )
        normalized["source_event_id"] = event["event_id"]
        registry["records"]["task_outcomes"][task_id] = copy.deepcopy(normalized)
        return registry, {"task_id": task_id, "outcome": normalized["outcome"], "agent_id": agent_id}, None

    return _mutate_memory(
        project,
        owner=owner,
        activation_token=activation_token,
        expected_state_sha=expected_state_sha,
        expected_memory_sha=expected_memory_sha,
        operation="TASK_OUTCOME_RECORDED",
        mutate=mutate,
        allow_initialize=True,
    )


def invalidate_outcome(
    project: str,
    *,
    owner: str,
    activation_token: str,
    expected_state_sha: str,
    expected_memory_sha: str,
    task_id: str,
    reason: str,
    evidence_refs: list[str],
) -> dict[str, Any]:
    task_id = _identifier(task_id, "task_id")
    reason = _text(reason, "invalidation reason")
    evidence_refs = _bounded_list(evidence_refs, "invalidation evidence_refs", item_max=512)
    if not evidence_refs:
        raise guard.InvalidState("Later invalidation requires observable evidence")

    def mutate(registry: dict[str, Any], _founder: Path) -> MutationResult:
        record = registry["records"]["task_outcomes"].get(task_id)
        locator = registry["records"]["task_outcome_locators"].get(task_id)
        if record is None and locator is not None:
            record = locator["projection"]
        if record is None:
            raise guard.Conflict("Unknown task outcome")
        if record["outcome"] == "INVALIDATED_LATER" or record["retracted"]:
            raise guard.Conflict("Task outcome is already invalidated or retracted")
        prior = record["outcome"]
        event = _new_event(
            registry,
            kind="OUTCOME_INVALIDATED_LATER",
            subject_id=task_id,
            actor=owner,
            evidence_refs=evidence_refs,
            payload={"prior_outcome": prior, "reason": reason},
            retention="PERMANENT",
        )
        record["outcome"] = "INVALIDATED_LATER"
        if locator is None:
            record["invalidation"] = {
                "at": event["observed_at"],
                "reason": reason,
                "evidence_refs": evidence_refs,
                "event_id": event["event_id"],
                "prior_outcome": prior,
            }
        else:
            locator["correction_event_ids"].append(event["event_id"])
        return registry, {
            "task_id": task_id,
            "prior_outcome": prior,
            "outcome": "INVALIDATED_LATER",
            "lesson_candidate_required": True,
        }, None

    return _mutate_memory(
        project, owner=owner, activation_token=activation_token,
        expected_state_sha=expected_state_sha, expected_memory_sha=expected_memory_sha,
        operation="TASK_OUTCOME_INVALIDATED", mutate=mutate,
    )


def revise_attribution(
    project: str,
    *,
    owner: str,
    activation_token: str,
    expected_state_sha: str,
    expected_memory_sha: str,
    task_id: str,
    attribution: dict[str, Any],
    reason: str,
    evidence_refs: list[str],
) -> dict[str, Any]:
    task_id = _identifier(task_id, "task_id")
    normalized = _normalize_attribution(attribution)
    reason = _text(reason, "attribution revision reason")
    evidence_refs = _bounded_list(evidence_refs, "attribution revision evidence_refs", item_max=512)
    if not evidence_refs:
        raise guard.InvalidState("Attribution revision requires evidence")

    def mutate(registry: dict[str, Any], _founder: Path) -> MutationResult:
        record = registry["records"]["task_outcomes"].get(task_id)
        locator = registry["records"]["task_outcome_locators"].get(task_id)
        if record is None and locator is not None:
            record = locator["projection"]
        if record is None or record["retracted"]:
            raise guard.Conflict("Unknown or retracted task outcome")
        skill_rows = record["skills"]
        _assert_exact_skill_attribution(normalized, skill_rows)
        normalized_projection = _attribution_projection(normalized)
        current_attribution = record["attribution"]
        comparable = (
            current_attribution
            if locator is not None
            else {key: current_attribution[key] for key in normalized_projection}
        )
        if comparable == normalized_projection:
            raise guard.Conflict("Attribution revision produced no change")
        previous = copy.deepcopy(record["attribution"])
        previous_projection = _attribution_projection(previous)
        event = _new_event(
            registry,
            kind="ATTRIBUTION_REVISED",
            subject_id=task_id,
            actor=owner,
            evidence_refs=evidence_refs,
            payload={
                "from_attribution": previous_projection,
                "to_attribution": normalized_projection,
                "reason": reason,
            },
            retention="PERMANENT",
        )
        if locator is None:
            record["attribution_history"].append(
                {
                    "at": event["observed_at"],
                    "from": previous,
                    "to": copy.deepcopy(normalized),
                    "reason": reason,
                    "evidence_refs": evidence_refs,
                    "event_id": event["event_id"],
                }
            )
            record["attribution"] = copy.deepcopy(normalized)
        else:
            record["attribution"] = copy.deepcopy(normalized_projection)
            locator["correction_event_ids"].append(event["event_id"])
        return registry, {
            "task_id": task_id,
            "from": previous["kind"],
            "to": normalized["kind"],
        }, None

    return _mutate_memory(
        project, owner=owner, activation_token=activation_token,
        expected_state_sha=expected_state_sha, expected_memory_sha=expected_memory_sha,
        operation="TASK_ATTRIBUTION_REVISED", mutate=mutate,
    )


def accept_lesson(
    project: str,
    *,
    owner: str,
    activation_token: str,
    expected_state_sha: str,
    expected_memory_sha: str,
    lesson: dict[str, Any],
    merge_into: str | None = None,
    merge_reason: str | None = None,
) -> dict[str, Any]:
    required = {
        "lesson_id", "title", "applicability", "observation", "impact", "future_rule",
        "confidence", "evidence_level", "evidence_refs", "retention", "source_kind",
        "contradicts",
    }
    if not isinstance(lesson, dict) or set(lesson) != required:
        raise guard.InvalidState("accept-lesson requires the exact Lesson Candidate schema")
    lesson_id = _identifier(lesson.get("lesson_id"), "lesson_id")
    applicability = _bounded_list(lesson.get("applicability"), "lesson applicability", identifiers=True)
    if not applicability:
        raise guard.InvalidState("Lesson applicability may not be empty")
    evidence_refs = _bounded_list(lesson.get("evidence_refs"), "lesson evidence_refs", item_max=512)
    if not evidence_refs:
        raise guard.InvalidState("Accepted Lesson requires evidence")
    contradicts = _bounded_list(lesson.get("contradicts"), "lesson contradicts", identifiers=True)
    source_kind = lesson.get("source_kind")
    if source_kind not in {"NORMAL", "ADOPTION_CONFIRMED", "ADOPTION_INFERRED"}:
        raise guard.InvalidState("Lesson source_kind is invalid")
    evidence_level = lesson.get("evidence_level")
    if source_kind == "ADOPTION_INFERRED" and evidence_level != "INFERRED":
        raise guard.InvalidState("Adoption-inferred Lesson must remain INFERRED")
    if source_kind == "ADOPTION_CONFIRMED" and evidence_level != "CONFIRMED":
        raise guard.InvalidState("Adoption-confirmed Lesson must be CONFIRMED")
    root, founder, _created = guard.resolve_project_root(project)
    adoption_id: str | None = None
    adoption_sha: str | None = None
    adoption_review_ref: str | None = None
    if source_kind != "NORMAL":
        _strategy_sha, _strategy_raw, strategy_state = strategy._read_strategy(
            founder / strategy.STRATEGY_NAME
        )
        if strategy_state is None:
            raise guard.Conflict("Adoption Lesson requires canonical adopted Strategy state")
        strategy.validate_strategy(strategy_state, root)
        if (
            strategy_state.get("project_origin") != "ADOPTED"
            or strategy_state.get("adoption_status") != "ADOPTED"
            or strategy_state.get("project_phase") != "bootstrapped"
            or strategy_state.get("gate", {}).get("state") != "OPERATING"
        ):
            raise guard.Conflict("Adoption Lesson requires ADOPTED + bootstrapped + OPERATING")
        adoption_id = strategy_state["adoption"]["baseline_id"]
        adoption_sha = strategy_state["adoption"]["baseline_sha256"]
        adoption_review_ref = _text(
            strategy_state["adoption"]["adoption_review_ref"],
            "Adoption review reference",
            max_length=512,
        )
    normalized_seed = {
        "lesson_id": lesson_id,
        "title": _text(lesson.get("title"), "lesson title"),
        "applicability": applicability,
        "observation": _text(lesson.get("observation"), "lesson observation"),
        "impact": _text(lesson.get("impact"), "lesson impact"),
        "future_rule": _text(lesson.get("future_rule"), "lesson future_rule"),
        "confidence": lesson.get("confidence"),
        "evidence_level": evidence_level,
        "source_kind": source_kind,
        "adoption_baseline_id": adoption_id,
        "adoption_baseline_sha256": adoption_sha,
        "adoption_review_ref": adoption_review_ref,
        "evidence_refs": evidence_refs,
        "status": "ACTIVE",
        "retention": lesson.get("retention"),
        "occurrence_count": 1,
        "created_at": guard.utc_now(),
        "updated_at": guard.utc_now(),
        "source_event_ids": [],
        "contradicts": contradicts,
        "superseded_by": None,
        "retracted": False,
    }
    _validate_lesson(normalized_seed, lesson_id)
    if merge_into is not None:
        merge_into = _identifier(merge_into, "merge_into lesson_id")
    if merge_reason is not None:
        merge_reason = _text(merge_reason, "Lesson semantic merge reason")

    def dedup_key(value: dict[str, Any]) -> str:
        material = {
            "applicability": sorted(item.casefold() for item in value["applicability"]),
            "future_rule": value["future_rule"].casefold(),
            "source_kind": value["source_kind"],
        }
        return guard.sha256_bytes(guard.canonical_json_bytes(material))

    def mutate(registry: dict[str, Any], _founder: Path) -> MutationResult:
        lessons = registry["records"]["lessons"]
        if lesson_id in lessons:
            raise guard.Conflict("Lesson ID already exists")
        matches = [
            existing_id for existing_id, existing in lessons.items()
            if not existing["retracted"] and dedup_key(existing) == dedup_key(normalized_seed)
        ]
        target_id = merge_into or (matches[0] if len(matches) == 1 else None)
        if len(matches) > 1 and merge_into is None:
            raise guard.Conflict("Multiple equivalent Lessons require explicit merge target")
        if target_id is not None:
            target = lessons.get(target_id)
            if target is None or target["retracted"]:
                raise guard.Conflict("Lesson merge target is missing or retracted")
            exact_merge = dedup_key(target) == dedup_key(normalized_seed)
            if not exact_merge and merge_reason is None:
                raise guard.Conflict(
                    "Semantic-near Lesson merge requires explicit revised Lesson content and merge_reason"
                )
            merged_evidence = sorted(set(target["evidence_refs"] + evidence_refs), key=str.casefold)
            if len(merged_evidence) > MAX_LIST_ITEMS:
                raise guard.Conflict("Lesson merge would exceed the evidence limit")
            old_content_sha = guard.sha256_bytes(guard.canonical_json_bytes(target))
            event = _new_event(
                registry,
                kind="LESSON_MERGED",
                subject_id=target_id,
                actor=owner,
                evidence_refs=evidence_refs,
                payload={
                    "candidate_lesson_id": lesson_id,
                    "occurrence_delta": 1,
                    "merge_kind": "EXACT" if exact_merge else "EXPLICIT_SEMANTIC",
                    "merge_reason": merge_reason or "exact deterministic deduplication",
                    "target_prior_content_sha256": old_content_sha,
                    "candidate_content_sha256": guard.sha256_bytes(
                        guard.canonical_json_bytes(normalized_seed)
                    ),
                },
                retention=target["retention"],
            )
            if not exact_merge:
                for field in (
                    "title", "applicability", "observation", "impact", "future_rule",
                    "confidence", "evidence_level", "source_kind",
                    "adoption_baseline_id", "adoption_baseline_sha256", "adoption_review_ref",
                    "contradicts",
                ):
                    target[field] = copy.deepcopy(normalized_seed[field])
            target["occurrence_count"] += 1
            target["evidence_refs"] = merged_evidence
            target["updated_at"] = event["observed_at"]
            target["source_event_ids"].append(event["event_id"])
            return registry, {"disposition": "MERGED", "lesson_id": target_id, "occurrence_count": target["occurrence_count"]}, None
        for contradiction_id in contradicts:
            target = lessons.get(contradiction_id)
            if target is None or target["retracted"]:
                raise guard.Conflict(f"Contradicted Lesson is unavailable: {contradiction_id}")
        event = _new_event(
            registry,
            kind="LESSON_ACCEPTED",
            subject_id=lesson_id,
            actor=owner,
            evidence_refs=evidence_refs,
            payload={
                "title": normalized_seed["title"],
                "applicability": applicability,
                "confidence": normalized_seed["confidence"],
                "evidence_level": evidence_level,
                "source_kind": source_kind,
            },
            retention=normalized_seed["retention"],
        )
        normalized_seed["created_at"] = event["observed_at"]
        normalized_seed["updated_at"] = event["observed_at"]
        normalized_seed["source_event_ids"] = [event["event_id"]]
        lessons[lesson_id] = copy.deepcopy(normalized_seed)
        for contradiction_id in contradicts:
            target = lessons[contradiction_id]
            target["status"] = "STALE"
            target["updated_at"] = event["observed_at"]
            if lesson_id not in target["contradicts"]:
                target["contradicts"].append(lesson_id)
                target["contradicts"].sort(key=str.casefold)
        return registry, {"disposition": "ACCEPTED", "lesson_id": lesson_id, "contradicts": contradicts}, None

    return _mutate_memory(
        project, owner=owner, activation_token=activation_token,
        expected_state_sha=expected_state_sha, expected_memory_sha=expected_memory_sha,
        operation="LESSON_ACCEPTED_OR_MERGED", mutate=mutate,
        allow_initialize=True,
    )


def transition_lesson(
    project: str,
    *,
    owner: str,
    activation_token: str,
    expected_state_sha: str,
    expected_memory_sha: str,
    lesson_id: str,
    status: str,
    reason: str,
    evidence_refs: list[str],
    superseded_by: str | None = None,
) -> dict[str, Any]:
    lesson_id = _identifier(lesson_id, "lesson_id")
    if status not in LESSON_STATUSES - {"ACTIVE"}:
        raise guard.InvalidState("Lesson transition target is invalid")
    reason = _text(reason, "Lesson transition reason")
    evidence_refs = _bounded_list(evidence_refs, "Lesson transition evidence_refs", item_max=512)
    if not evidence_refs:
        raise guard.InvalidState("Lesson transition requires evidence")
    if superseded_by is not None:
        superseded_by = _identifier(superseded_by, "superseded_by")
    if status == "SUPERSEDED" and superseded_by is None:
        raise guard.InvalidState("SUPERSEDED Lesson requires a successor Lesson ID")

    def mutate(registry: dict[str, Any], _founder: Path) -> MutationResult:
        record = registry["records"]["lessons"].get(lesson_id)
        if record is None or record["retracted"]:
            raise guard.Conflict("Unknown or retracted Lesson")
        if record["status"] == status:
            raise guard.Conflict("Lesson is already in the requested status")
        if superseded_by is not None:
            successor = registry["records"]["lessons"].get(superseded_by)
            if successor is None or successor["retracted"]:
                raise guard.Conflict("Lesson successor is unavailable")
        event = _new_event(
            registry,
            kind="LESSON_STATUS_CHANGED",
            subject_id=lesson_id,
            actor=owner,
            evidence_refs=evidence_refs,
            payload={"from": record["status"], "to": status, "reason": reason, "superseded_by": superseded_by},
            retention=record["retention"],
        )
        previous = record["status"]
        record["status"] = status
        record["superseded_by"] = superseded_by
        record["updated_at"] = event["observed_at"]
        record["source_event_ids"].append(event["event_id"])
        return registry, {"lesson_id": lesson_id, "from": previous, "to": status}, None

    return _mutate_memory(
        project, owner=owner, activation_token=activation_token,
        expected_state_sha=expected_state_sha, expected_memory_sha=expected_memory_sha,
        operation="LESSON_STATUS_CHANGED", mutate=mutate,
    )


DECISION_TRANSITIONS = {
    None: {"ACTIVE"},
    "ACTIVE": DECISION_STATUSES - {"ACTIVE"},
    "VALIDATED": {"PARTIALLY_VALIDATED", "INVALIDATED", "SUPERSEDED", "RECONSIDERED"},
    "PARTIALLY_VALIDATED": {"VALIDATED", "INVALIDATED", "SUPERSEDED", "RECONSIDERED"},
    "INVALIDATED": {"RECONSIDERED", "SUPERSEDED"},
    "SUPERSEDED": {"RECONSIDERED"},
    "RECONSIDERED": {"ACTIVE", "VALIDATED", "PARTIALLY_VALIDATED", "INVALIDATED", "SUPERSEDED"},
    "UNKNOWN_OUTCOME": DECISION_STATUSES - {"UNKNOWN_OUTCOME"},
}


def _canonical_decision_observation(founder: Path, decision_id: str) -> str:
    path = founder / "DECISIONS.md"
    raw = _read_direct_bytes(path, "DECISIONS.md", max_bytes=MAX_REGISTRY_BYTES)
    try:
        text_value = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise guard.InvalidState("DECISIONS.md must be UTF-8") from exc
    escaped = re.escape(decision_id)
    matches = re.findall(
        rf"(?im)^\s*(?:[-*]\s*)?(?:Decision\s+ID|决策\s*ID)\s*:\s*`?{escaped}`?\s*$",
        text_value,
    )
    if len(matches) != 1 or not strategy._decision_id_exists(founder, decision_id):
        raise guard.Conflict(
            "Decision Outcome must bind exactly one canonical Decision ID in DECISIONS.md"
        )
    return guard.sha256_bytes(raw)


def record_decision_outcome(
    project: str,
    *,
    owner: str,
    activation_token: str,
    expected_state_sha: str,
    expected_memory_sha: str,
    decision: dict[str, Any],
) -> dict[str, Any]:
    required = {
        "decision_id", "status", "summary", "conditions", "result_summary",
        "reconsideration_trigger", "confidence", "evidence_refs",
    }
    if not isinstance(decision, dict) or frozenset(decision) not in {
        frozenset(required), frozenset(required | {"applicability"})
    }:
        raise guard.InvalidState("record-decision-outcome requires the exact schema")
    decision_id = _identifier(decision.get("decision_id"), "decision_id")
    status = decision.get("status")
    if status not in DECISION_STATUSES:
        raise guard.InvalidState("Decision outcome status is invalid")
    evidence_refs = _bounded_list(decision.get("evidence_refs"), "decision evidence_refs", item_max=512)
    if not evidence_refs:
        raise guard.InvalidState("Decision outcome requires later observable evidence")
    applicability = _validate_selectors(decision.get("applicability", {}))
    if any(applicability[field] for field in QUERY_CONTROL_FIELDS):
        raise guard.InvalidState("Decision applicability contains query-control selectors")
    normalized_seed = {
        "decision_id": decision_id,
        "status": status,
        "summary": _text(decision.get("summary"), "decision summary"),
        "conditions": _text(decision.get("conditions"), "decision conditions"),
        "applicability": applicability,
        "result_summary": _text(decision.get("result_summary"), "decision result_summary"),
        "reconsideration_trigger": _text(decision.get("reconsideration_trigger"), "decision reconsideration_trigger"),
        "confidence": decision.get("confidence"),
        "evidence_refs": evidence_refs,
        "retention": "PERMANENT",
        "updated_at": guard.utc_now(),
        "source_event_ids": [],
        "retracted": False,
    }

    def mutate(registry: dict[str, Any], _founder: Path) -> MutationResult:
        normalized = copy.deepcopy(normalized_seed)
        normalized["canonical_decisions_sha256"] = _canonical_decision_observation(
            _founder, decision_id
        )
        _validate_decision(normalized, decision_id)
        previous = registry["records"]["decision_outcomes"].get(decision_id)
        previous_status = previous["status"] if previous is not None else None
        if previous is not None and previous["retracted"]:
            raise guard.Conflict("Retracted Decision memory cannot be overwritten")
        if status not in DECISION_TRANSITIONS[previous_status]:
            raise guard.Conflict(f"Invalid Decision outcome transition: {previous_status} -> {status}")
        event = _new_event(
            registry,
            kind="DECISION_OUTCOME_CHANGED",
            subject_id=decision_id,
            actor=owner,
            evidence_refs=evidence_refs,
            payload={
                "from": previous_status or "UNRECORDED",
                "to": status,
                "summary": normalized["summary"],
                "conditions": normalized["conditions"],
                "result_summary": normalized["result_summary"],
                "reconsideration_trigger": normalized["reconsideration_trigger"],
                "confidence": normalized["confidence"],
                "applicability": normalized["applicability"],
                "canonical_decisions_sha256": normalized["canonical_decisions_sha256"],
                "applicability_sha256": guard.sha256_bytes(
                    guard.canonical_json_bytes(normalized["applicability"])
                ),
            },
            retention="PERMANENT",
        )
        normalized["updated_at"] = event["observed_at"]
        normalized["source_event_ids"] = (
            copy.deepcopy(previous["source_event_ids"]) if previous is not None else []
        ) + [event["event_id"]]
        registry["records"]["decision_outcomes"][decision_id] = copy.deepcopy(normalized)
        return registry, {
            "decision_id": decision_id,
            "from": previous_status or "UNRECORDED",
            "to": status,
            "strategic_gate_reconsideration_required": status in {"INVALIDATED", "RECONSIDERED"},
        }, None

    return _mutate_memory(
        project, owner=owner, activation_token=activation_token,
        expected_state_sha=expected_state_sha, expected_memory_sha=expected_memory_sha,
        operation="DECISION_OUTCOME_RECORDED", mutate=mutate,
        allow_initialize=True,
    )


def record_routing_decision(
    project: str,
    *,
    owner: str,
    activation_token: str,
    expected_state_sha: str,
    expected_memory_sha: str,
    routing: dict[str, Any],
) -> dict[str, Any]:
    required = {
        "routing_id", "task_context", "selected_agent_id", "selected_skill_keys",
        "alternatives", "reason", "evidence_record_ids", "retention",
    }
    if not isinstance(routing, dict) or set(routing) != required:
        raise guard.InvalidState("record-routing requires the exact schema")
    routing_id = _identifier(routing.get("routing_id"), "routing_id")
    context = _validate_selectors(routing.get("task_context"), allow_empty=False)
    selected_agent_id = _optional_text(routing.get("selected_agent_id"), "selected_agent_id", max_length=128)
    selected_skills = _bounded_list(routing.get("selected_skill_keys"), "selected_skill_keys", item_max=512)
    evidence_ids = _bounded_list(routing.get("evidence_record_ids"), "evidence_record_ids", identifiers=True)
    normalized = {
        "routing_id": routing_id,
        "task_context": context,
        "selected_agent_id": selected_agent_id,
        "selected_skill_keys": selected_skills,
        "alternatives": _bounded_list(routing.get("alternatives"), "routing alternatives", item_max=256),
        "reason": _text(routing.get("reason"), "routing reason"),
        "evidence_record_ids": evidence_ids,
        "evidence_bindings": [],
        "created_at": guard.utc_now(),
        "retention": routing.get("retention"),
        "retracted": False,
    }
    if normalized["retention"] not in RETENTION_CLASSES:
        raise guard.InvalidState("Routing retention is invalid")
    if not evidence_ids:
        raise guard.InvalidState("Routing history requires nonempty accepted Memory evidence")

    def mutate(registry: dict[str, Any], _founder: Path) -> MutationResult:
        if routing_id in registry["records"]["routing_history"]:
            raise guard.Conflict("Routing decision ID already exists")
        bindings: list[dict[str, str]] = []
        for evidence_id in evidence_ids:
            candidates: list[tuple[str, dict[str, Any]]] = []
            outcome = registry["records"]["task_outcomes"].get(evidence_id)
            if outcome is None:
                locator = registry["records"]["task_outcome_locators"].get(evidence_id)
                if locator is not None:
                    outcome = locator["projection"]
            if outcome is not None and outcome["effective"] and not outcome["retracted"]:
                candidates.append(("task_outcomes", _task_projection(outcome)))
            lesson = registry["records"]["lessons"].get(evidence_id)
            if lesson is not None and not lesson["retracted"] and lesson["status"] == "ACTIVE":
                candidates.append(("lessons", lesson))
            decision = registry["records"]["decision_outcomes"].get(evidence_id)
            if decision is not None and not decision["retracted"]:
                candidates.append(("decision_outcomes", decision))
            pattern = registry["records"]["organization_patterns"].get(evidence_id)
            if pattern is not None and not pattern["retracted"]:
                candidates.append(("organization_patterns", pattern))
            if len(candidates) != 1:
                raise guard.Conflict(
                    "Routing evidence must resolve to exactly one active accepted Memory record"
                )
            record_type, value = candidates[0]
            bindings.append(
                {
                    "record_type": record_type,
                    "record_id": evidence_id,
                    "content_sha256": guard.sha256_bytes(
                        guard.canonical_json_bytes(value)
                    ),
                }
            )
        normalized["evidence_bindings"] = bindings
        _validate_routing(normalized, routing_id)
        event = _new_event(
            registry,
            kind="ROUTING_DECISION_RECORDED",
            subject_id=routing_id,
            actor=owner,
            evidence_refs=[f"memory-record:{item}" for item in evidence_ids],
            payload={
                "selected_agent_id": selected_agent_id,
                "selected_skill_keys": selected_skills,
                "memory_query_sha256": guard.sha256_bytes(guard.canonical_json_bytes(context)),
                "evidence_bindings": bindings,
            },
            retention=normalized["retention"],
        )
        normalized["created_at"] = event["observed_at"]
        registry["records"]["routing_history"][routing_id] = copy.deepcopy(normalized)
        return registry, {"routing_id": routing_id, "selected_agent_id": selected_agent_id}, None

    return _mutate_memory(
        project, owner=owner, activation_token=activation_token,
        expected_state_sha=expected_state_sha, expected_memory_sha=expected_memory_sha,
        operation="ROUTING_DECISION_RECORDED", mutate=mutate,
    )


def record_organization_pattern(
    project: str,
    *,
    owner: str,
    activation_token: str,
    expected_state_sha: str,
    expected_memory_sha: str,
    pattern: dict[str, Any],
) -> dict[str, Any]:
    required = {
        "pattern_id", "pattern_type", "context", "summary", "evidence_refs", "retention",
    }
    if not isinstance(pattern, dict) or set(pattern) != required:
        raise guard.InvalidState("record-organization-pattern requires the exact schema")
    pattern_id = _identifier(pattern.get("pattern_id"), "organization pattern_id")
    context = _validate_selectors(pattern.get("context"), allow_empty=False)
    normalized = {
        "pattern_id": pattern_id,
        "pattern_type": pattern.get("pattern_type"),
        "context": context,
        "summary": _text(pattern.get("summary"), "organization pattern summary"),
        "evidence_refs": _bounded_list(
            pattern.get("evidence_refs"), "organization pattern evidence_refs", item_max=512
        ),
        "retention": pattern.get("retention"),
        "occurrence_count": 1,
        "created_at": guard.utc_now(),
        "updated_at": guard.utc_now(),
        "source_event_ids": [],
        "retracted": False,
    }
    _validate_organization_pattern(normalized, pattern_id)

    def mutate(registry: dict[str, Any], _founder: Path) -> MutationResult:
        patterns = registry["records"]["organization_patterns"]
        existing = patterns.get(pattern_id)
        if existing is not None and existing["retracted"]:
            raise guard.Conflict("Retracted Organization pattern cannot be overwritten")
        event = _new_event(
            registry,
            kind="ORGANIZATION_PATTERN_RECORDED",
            subject_id=pattern_id,
            actor=owner,
            evidence_refs=normalized["evidence_refs"],
            payload={"pattern_type": normalized["pattern_type"]},
            retention=normalized["retention"],
        )
        if existing is not None:
            if (
                existing["pattern_type"] != normalized["pattern_type"]
                or existing["context"] != normalized["context"]
            ):
                raise guard.Conflict("Organization pattern identity/context cannot be rewritten")
            existing["summary"] = normalized["summary"]
            existing["evidence_refs"] = sorted(
                set(existing["evidence_refs"] + normalized["evidence_refs"]), key=str.casefold
            )
            existing["occurrence_count"] += 1
            existing["updated_at"] = event["observed_at"]
            existing["source_event_ids"].append(event["event_id"])
            result = existing
        else:
            normalized["created_at"] = event["observed_at"]
            normalized["updated_at"] = event["observed_at"]
            normalized["source_event_ids"] = [event["event_id"]]
            patterns[pattern_id] = copy.deepcopy(normalized)
            result = patterns[pattern_id]
        return registry, {
            "pattern_id": pattern_id,
            "pattern_type": result["pattern_type"],
            "occurrence_count": result["occurrence_count"],
        }, None

    return _mutate_memory(
        project, owner=owner, activation_token=activation_token,
        expected_state_sha=expected_state_sha, expected_memory_sha=expected_memory_sha,
        operation="ORGANIZATION_PATTERN_RECORDED", mutate=mutate,
        allow_initialize=True,
    )


def review_evidence(
    project: str,
    *,
    context: dict[str, Any],
    candidate_agent_ids: list[str],
    candidate_skill_keys: list[str],
    risk_level: str,
) -> dict[str, Any]:
    if risk_level not in RISK_LEVELS:
        raise guard.InvalidState("review-evidence risk_level is invalid")
    root, founder, _created = guard.resolve_project_root(project)
    if _transaction_observation(founder)["state"] != "none":
        raise guard.Conflict("MEMORY_RECOVERY_REQUIRED: Memory transaction prevents review routing")
    normalized = _validate_selectors(context, allow_empty=False)
    if any(normalized[field] for field in QUERY_CONTROL_FIELDS):
        raise guard.InvalidState("review-evidence context accepts only Performance Context selectors")
    if normalized["risk_levels"] and risk_level not in normalized["risk_levels"]:
        raise guard.InvalidState("review-evidence risk_level disagrees with context")
    normalized["risk_levels"] = [risk_level]
    candidate_agents = _bounded_list(
        candidate_agent_ids, "candidate_agent_ids", identifiers=True
    )
    candidate_skills = _bounded_list(
        candidate_skill_keys, "candidate_skill_keys", item_max=512
    )
    routing = route_evidence(
        str(root), context=normalized,
        candidate_agent_ids=candidate_agents,
        candidate_skill_keys=candidate_skills,
    )
    debt: list[dict[str, Any]] = []
    if candidate_agents:
        # Review Debt is an Agent-level conservative review obligation.  It is
        # not a task tag, so ordinary task tags/stage/component selectors must
        # never hide it.  Agent-only selection also keeps compatibility with
        # the early V3 candidate's `tags=[review-debt]` sentinel rows.
        query = query_memory(
            str(root),
            selectors={
                "record_types": ["organization_patterns"],
                "agent_ids": candidate_agents,
            },
            limit=MAX_QUERY_LIMIT,
        )
        debt = [
            row for row in query["records"]
            if row["value"]["pattern_type"] == "REVIEW_DEBT_HISTORY"
        ]
    unproven_agent = any(row["evidence_state"] == "UNPROVEN" for row in routing["agents"])
    unproven_skill = any(
        row["evidence_state"] == "UNPROVEN"
        or row["trust_eligibility"] != "LOCK_TRUSTED_BINDING_UNVERIFIED"
        for row in routing["skills"]
    )
    fixed = risk_level in {"L2", "L3"}
    independent = fixed or bool(debt) or unproven_agent or unproven_skill
    return {
        "result": "REVIEW_EVIDENCE",
        "project_root": str(root),
        "recommendation": "INDEPENDENT_REVIEW_REQUIRED" if independent else "NORMAL_REVIEW",
        "fixed_review_gate_required": fixed,
        "review_debt": [row["record_id"] for row in debt],
        "evidence": {
            "agents": routing["agents"],
            "skills": routing["skills"],
        },
        "constraints": {
            "permissions_unchanged": True,
            "skill_trust_unchanged": True,
            "fixed_review_gates_unchanged": True,
        },
        "changed_paths": [],
    }


def record_review_debt(
    project: str,
    *,
    owner: str,
    activation_token: str,
    expected_state_sha: str,
    expected_memory_sha: str,
    task_id: str,
    agent_id: str,
    severity: str,
    reason: str,
    evidence_refs: list[str],
) -> dict[str, Any]:
    task_id = _identifier(task_id, "review debt task_id")
    agent_id = _identifier(agent_id, "review debt agent_id")
    if severity not in {"MINOR", "MAJOR", "REPEATED", "FUNDAMENTAL"}:
        raise guard.InvalidState("Review debt severity is invalid")
    reason = _text(reason, "review debt reason")
    evidence_refs = _bounded_list(evidence_refs, "review debt evidence_refs", item_max=512)
    if not evidence_refs:
        raise guard.InvalidState("Review debt requires evidence")

    pattern_id = f"OP-REVIEW-{guard.sha256_bytes(task_id.encode('utf-8'))[:24]}"
    pattern_context = _validate_selectors({"agent_ids": [agent_id]}, allow_empty=False)

    def mutate(registry: dict[str, Any], _founder: Path) -> MutationResult:
        event = _new_event(
            registry,
            kind="REVIEW_DEBT",
            subject_id=task_id,
            actor=owner,
            evidence_refs=evidence_refs,
            payload={"agent_id": agent_id, "severity": severity, "reason": reason},
            retention="LONG_TERM",
        )
        patterns = registry["records"]["organization_patterns"]
        existing = patterns.get(pattern_id)
        if existing is None:
            patterns[pattern_id] = {
                "pattern_id": pattern_id,
                "pattern_type": "REVIEW_DEBT_HISTORY",
                "context": pattern_context,
                "summary": f"{severity}: {reason}",
                "evidence_refs": evidence_refs,
                "retention": "LONG_TERM",
                "occurrence_count": 1,
                "created_at": event["observed_at"],
                "updated_at": event["observed_at"],
                "source_event_ids": [event["event_id"]],
                "retracted": False,
            }
        else:
            if existing["retracted"] or existing["context"] != pattern_context:
                raise guard.Conflict("Review debt pattern is retracted or has incompatible context")
            existing["summary"] = f"{severity}: {reason}"
            existing["evidence_refs"] = sorted(
                set(existing["evidence_refs"] + evidence_refs), key=str.casefold
            )
            existing["occurrence_count"] += 1
            existing["updated_at"] = event["observed_at"]
            existing["source_event_ids"].append(event["event_id"])
        return registry, {
            "task_id": task_id,
            "agent_id": agent_id,
            "severity": severity,
            "current_state_must_remain_in_roadmap_or_status": True,
            "event_id": event["event_id"],
        }, None

    return _mutate_memory(
        project, owner=owner, activation_token=activation_token,
        expected_state_sha=expected_state_sha, expected_memory_sha=expected_memory_sha,
        operation="REVIEW_DEBT_RECORDED", mutate=mutate,
        allow_initialize=True,
    )


def record_thread_health(
    project: str,
    *,
    owner: str,
    activation_token: str,
    expected_state_sha: str,
    expected_memory_sha: str,
    agent_id: str,
    thread_record_id: str,
    event_type: str,
    evidence_refs: list[str],
) -> dict[str, Any]:
    agent_id = _identifier(agent_id, "thread health agent_id")
    thread_record_id = _identifier(thread_record_id, "thread health thread_record_id")
    if event_type not in {"HANDOFF", "RECOVERY", "CONTEXT_HAZARD", "ARCHIVED", "INTERRUPTED"}:
        raise guard.InvalidState("Thread health event_type is invalid")
    evidence_refs = _bounded_list(evidence_refs, "thread health evidence_refs", item_max=512)
    if not evidence_refs:
        raise guard.InvalidState("Thread health event requires evidence")

    pattern_id = f"OP-THREAD-{guard.sha256_bytes(agent_id.encode('utf-8'))[:24]}"
    pattern_context = _validate_selectors({"agent_ids": [agent_id], "tags": ["thread-health"]}, allow_empty=False)

    def mutate(registry: dict[str, Any], _founder: Path) -> MutationResult:
        event = _new_event(
            registry,
            kind="THREAD_HEALTH_EVENT",
            subject_id=agent_id,
            actor=owner,
            evidence_refs=evidence_refs,
            payload={"thread_record_id": thread_record_id, "event_type": event_type},
            retention="LONG_TERM",
        )
        patterns = registry["records"]["organization_patterns"]
        existing = patterns.get(pattern_id)
        if existing is None:
            patterns[pattern_id] = {
                "pattern_id": pattern_id,
                "pattern_type": "THREAD_HEALTH",
                "context": pattern_context,
                "summary": f"Latest Thread health event: {event_type} on {thread_record_id}",
                "evidence_refs": evidence_refs,
                "retention": "LONG_TERM",
                "occurrence_count": 1,
                "created_at": event["observed_at"],
                "updated_at": event["observed_at"],
                "source_event_ids": [event["event_id"]],
                "retracted": False,
            }
        else:
            existing["summary"] = f"Latest Thread health event: {event_type} on {thread_record_id}"
            existing["evidence_refs"] = sorted(set(existing["evidence_refs"] + evidence_refs), key=str.casefold)
            existing["occurrence_count"] += 1
            existing["updated_at"] = event["observed_at"]
            existing["source_event_ids"].append(event["event_id"])
        return registry, {
            "agent_id": agent_id,
            "thread_record_id": thread_record_id,
            "performance_identity_preserved": True,
            "event_id": event["event_id"],
        }, None

    return _mutate_memory(
        project, owner=owner, activation_token=activation_token,
        expected_state_sha=expected_state_sha, expected_memory_sha=expected_memory_sha,
        operation="THREAD_HEALTH_RECORDED", mutate=mutate,
        allow_initialize=True,
    )


def retract_memory(
    project: str,
    *,
    owner: str,
    activation_token: str,
    expected_state_sha: str,
    expected_memory_sha: str,
    record_type: str,
    record_id: str,
    authority_kind: str,
    founder_receipt: str,
    reason: str,
    evidence_refs: list[str],
) -> dict[str, Any]:
    if record_type not in PUBLIC_RECORD_KINDS:
        raise guard.InvalidState("Retraction record_type is invalid")
    record_id = _identifier(record_id, "retraction record_id")
    if authority_kind != "FOUNDER":
        raise guard.Conflict("Memory retraction requires authority_kind=FOUNDER")
    founder_receipt = _identifier(founder_receipt, "Founder receipt", max_length=128)
    reason = _text(reason, "Memory retraction reason")
    evidence_refs = _bounded_list(evidence_refs, "Memory retraction evidence_refs", item_max=512)
    if not evidence_refs:
        raise guard.InvalidState("Memory retraction requires current Founder evidence")

    def mutate(registry: dict[str, Any], _founder: Path) -> MutationResult:
        if founder_receipt in registry["consumed_founder_receipts"]:
            raise guard.Conflict("Founder correction receipt has already been consumed")
        record = registry["records"][record_type].get(record_id)
        locator = None
        if record_type == "task_outcomes" and record is None:
            locator = registry["records"]["task_outcome_locators"].get(record_id)
            if locator is not None:
                record = locator["projection"]
        if record is None or record.get("retracted") is True:
            raise guard.Conflict("Memory record is missing or already retracted")
        subject_hash = guard.sha256_bytes(guard.canonical_json_bytes(record))
        event = _new_event(
            registry,
            kind="MEMORY_RETRACTED",
            subject_id=record_id,
            actor=owner,
            evidence_refs=evidence_refs,
            payload={
                "record_type": record_type,
                "subject_hash": subject_hash,
                "reason": reason,
                "founder_receipt": founder_receipt,
            },
            retention="PERMANENT",
        )
        record["retracted"] = True
        if record_type == "task_outcomes":
            record["effective"] = False
            if locator is not None:
                locator["correction_event_ids"].append(event["event_id"])
        registry["consumed_founder_receipts"].append(founder_receipt)
        registry["consumed_founder_receipts"].sort(key=str.casefold)
        return registry, {
            "record_type": record_type,
            "record_id": record_id,
            "subject_hash": subject_hash,
            "audit_event_id": event["event_id"],
            "removed_from_routing": True,
        }, None

    return _mutate_memory(
        project, owner=owner, activation_token=activation_token,
        expected_state_sha=expected_state_sha, expected_memory_sha=expected_memory_sha,
        operation="MEMORY_RETRACTED", mutate=mutate,
    )


def compact_memory(
    project: str,
    *,
    owner: str,
    activation_token: str,
    expected_state_sha: str,
    expected_memory_sha: str,
    retain_active_events: int = 200,
) -> dict[str, Any]:
    if not isinstance(retain_active_events, int) or not 1 <= retain_active_events <= 1000:
        raise guard.InvalidState("retain_active_events must be between 1 and 1000")
    root, founder, _created = guard.resolve_project_root(project)
    observed_sha, _raw, observed = _registry_observation(founder)
    if observed_sha != _sha_or_absent(expected_memory_sha, "expected_memory_sha"):
        raise guard.Conflict("Memory CAS mismatch before compaction")
    if observed is None:
        raise guard.Conflict("Memory registry does not exist")
    validate_registry(observed, root)
    if len(observed["active_events"]) <= retain_active_events:
        return {
            "result": "MEMORY_COMPACTION_NOT_REQUIRED",
            "project_root": str(root),
            "memory_revision": observed["memory_revision"],
            "memory_sha": observed_sha,
            "active_events": len(observed["active_events"]),
            "changed_paths": [],
        }

    def mutate(registry: dict[str, Any], _founder: Path) -> MutationResult:
        count = len(registry["active_events"]) - retain_active_events
        all_events = copy.deepcopy(registry["active_events"])
        base_snapshot_sequence = registry["next_sequence"] - 1
        prefix = copy.deepcopy(all_events[:count])
        routing_ids = {
            event["subject_id"]
            for event in prefix
            if event["kind"] == "ROUTING_DECISION_RECORDED"
        }
        archived_routing = {
            record_id: copy.deepcopy(record)
            for record_id, record in registry["records"]["routing_history"].items()
            if record_id in routing_ids
            and record["retention"] in {"COMPACTABLE", "TEMPORARY"}
        }
        outcome_ids = {
            event["subject_id"]
            for event in prefix
            if event["kind"] == "TASK_OUTCOME_FINALIZED"
        }
        archived_outcomes = {
            task_id: copy.deepcopy(record)
            for task_id, record in registry["records"]["task_outcomes"].items()
            if task_id in outcome_ids and record["retention"] != "PERMANENT"
        }
        record_segments = {
            "routing_history": archived_routing,
            "task_outcomes": archived_outcomes,
        }
        record_content_sha = guard.sha256_bytes(
            guard.canonical_json_bytes(record_segments)
        )
        first = prefix[0]["sequence"]
        last = prefix[-1]["sequence"]
        archive_value = {
            "schema_version": ARCHIVE_SCHEMA_VERSION,
            "project_binding_id": registry["project_binding"]["project_binding_id"],
            "first_sequence": first,
            "last_sequence": last,
            "event_count": len(prefix),
            "events": prefix,
            "record_segments": record_segments,
        }
        archive_raw = guard.canonical_json_bytes(archive_value)
        archive_sha = guard.sha256_bytes(archive_raw)
        filename = f"SEG-{first}-{last}-{archive_sha}.json"
        row = {
            "filename": filename,
            "content_sha256": archive_sha,
            "first_sequence": first,
            "last_sequence": last,
            "event_count": len(prefix),
            "record_counts": {
                "routing_history": len(archived_routing),
                "task_outcomes": len(archived_outcomes),
            },
            "record_content_sha256": record_content_sha,
            "first_previous_event_sha256": prefix[0]["previous_event_sha256"],
            "last_event_sha256": prefix[-1]["event_sha256"],
            "created_at": guard.utc_now(),
        }
        registry["archive_manifest"].append(row)
        registry["active_events"] = registry["active_events"][count:]
        for record_id in archived_routing:
            del registry["records"]["routing_history"][record_id]
        for task_id, outcome in archived_outcomes.items():
            base_applied_correction_event_ids = [
                event["event_id"]
                for event in all_events
                if _is_task_correction_event(event, task_id)
                and event["sequence"] <= base_snapshot_sequence
            ]
            registry["records"]["task_outcome_locators"][task_id] = {
                "task_id": task_id,
                "archive_filename": filename,
                "archived_record_sha256": guard.sha256_bytes(
                    guard.canonical_json_bytes(outcome)
                ),
                "projection": _task_projection(outcome),
                "base_snapshot_sequence": base_snapshot_sequence,
                "base_applied_correction_event_ids": base_applied_correction_event_ids,
                "correction_event_ids": [],
                "archived_at": row["created_at"],
            }
            del registry["records"]["task_outcomes"][task_id]
        return registry, {
            "archived_events": len(prefix),
            "retained_active_events": len(registry["active_events"]),
            "archive_filename": filename,
            "archived_routing_history": len(archived_routing),
            "archived_task_outcomes": len(archived_outcomes),
            "records_preserved": {kind: len(registry["records"][kind]) for kind in sorted(RECORD_KINDS)},
        }, {"filename": filename, "raw": archive_raw, "content_sha256": archive_sha}

    return _mutate_memory(
        project, owner=owner, activation_token=activation_token,
        expected_state_sha=expected_state_sha, expected_memory_sha=expected_memory_sha,
        operation="MEMORY_COMPACTED", mutate=mutate,
    )


def recover_memory_lock(
    project: str,
    *,
    owner: str,
    activation_token: str,
    expected_state_sha: str,
    nonce: str,
    _commit_mutex_held: bool = False,
) -> dict[str, Any]:
    owner = _identifier(owner, "Memory recovery owner")
    activation_token = _text(activation_token, "activation_token")
    expected_state_sha = _sha(expected_state_sha, "expected_state_sha")
    nonce = _identifier(nonce, "Memory recovery nonce")
    root, founder, _created = guard.resolve_project_root(project)
    if not _commit_mutex_held:
        with guard.acquire_governance_commit_mutex(
            str(root), operation="memory-registry:recover"
        ):
            return recover_memory_lock(
                str(root), owner=owner, activation_token=activation_token,
                expected_state_sha=expected_state_sha, nonce=nonce,
                _commit_mutex_held=True,
            )
    fence = guard.verify_fence(
        str(root), owner=owner, activation_token=activation_token, allow_canonical_drift=True
    )
    if fence["state_sha"] != expected_state_sha:
        raise guard.Conflict("Supervisor state CAS mismatch during Memory recovery")
    _assert_operating(str(root))
    transaction = _transaction_observation(founder)
    if transaction["state"] != "recovery-required":
        raise guard.Conflict("Memory recovery lock does not exist")
    if transaction["owner"] != owner or transaction["nonce"] != nonce:
        raise guard.Conflict("Memory recovery owner or nonce does not match")
    if transaction["project_root"] != str(root) or transaction["project_binding_id"] != _project_binding_id(root):
        raise guard.Conflict("Memory recovery lock belongs to another project")
    memory_root, registry_path, archive_root, lock_path = _memory_paths(founder)
    root_fence = _MemoryDirectoryFence.acquire(memory_root, "Memory directory")
    try:
        current_sha, _raw, registry = _registry_observation(founder)
        if current_sha not in {transaction["old_memory_sha"], transaction["target_memory_sha"]}:
            raise guard.Conflict("Memory recovery found neither the recorded old nor target state")
        if registry is not None:
            validate_registry(registry, root)
        archive_plan = transaction.get("archive_plan")
        if archive_plan is not None:
            archive_path = archive_root / archive_plan["filename"]
            if current_sha == transaction["target_memory_sha"]:
                if not archive_path.exists():
                    raise guard.Conflict("Committed Memory target is missing its archive segment")
                raw = _read_direct_bytes(archive_path, "Memory recovery archive", max_bytes=MAX_ARCHIVE_BYTES)
                if guard.sha256_bytes(raw) != archive_plan["content_sha256"]:
                    raise guard.Conflict("Memory recovery archive hash mismatch")
                if registry is None or not any(
                    row["filename"] == archive_plan["filename"]
                    and row["content_sha256"] == archive_plan["content_sha256"]
                    for row in registry["archive_manifest"]
                ):
                    raise guard.Conflict("Committed Memory target does not reference its archive")
            elif archive_path.exists():
                raw = _read_direct_bytes(archive_path, "Memory orphan archive", max_bytes=MAX_ARCHIVE_BYTES)
                if guard.sha256_bytes(raw) != archive_plan["content_sha256"]:
                    raise guard.Conflict("Unknown orphan archive must not be removed")
                archive_path.unlink()
        current_state_sha, state_record = guard.state_observation(founder / guard.STATE_NAME)
        if current_state_sha != expected_state_sha or state_record is None:
            raise guard.Conflict("Supervisor state changed during Memory recovery")
        current_sources = guard.read_source_revisions(founder)
        if not guard.source_fingerprints_match(state_record.get("source_revisions"), current_sources):
            checkpoint = guard.checkpoint_active(
                str(root), owner=owner, activation_token=activation_token,
                expected_state_sha=current_state_sha,
                _commit_mutex_held=True,
            )
            current_state_sha = checkpoint["state_sha"]
        root_fence.assert_current("Memory directory")
        lock_raw = _read_direct_bytes(lock_path, "Memory transaction lock", max_bytes=64 * 1024)
        lock = _strict_json_loads(lock_raw, label="Memory transaction lock")
        if lock.get("owner") != owner or lock.get("nonce") != nonce:
            raise guard.Conflict("Memory lock changed during recovery")
        lock_path.unlink()
        return {
            "result": "MEMORY_RECOVERED",
            "project_root": str(root),
            "memory_sha": current_sha,
            "state_sha": current_state_sha,
            "recovered_disposition": "TARGET" if current_sha == transaction["target_memory_sha"] else "OLD",
            "changed_paths": [str(lock_path), str(founder / guard.STATE_NAME), str(founder / guard.LOCK_NAME)],
        }
    except Exception:
        # Recovery is fail-closed: the transaction lock remains unless the
        # exact old/target state and any planned archive were reconciled.
        raise
    finally:
        root_fence.close()


def _json_argument(raw: str, label: str) -> dict[str, Any]:
    return _strict_json_loads(raw.encode("utf-8"), label=label)


def _add_mutation_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--project", required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--activation-token", required=True)
    parser.add_argument("--expected-state-sha", required=True)
    parser.add_argument("--expected-memory-sha", required=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect_parser = subparsers.add_parser("inspect")
    inspect_parser.add_argument("--project", required=True)
    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--project", required=True)
    verify_parser.add_argument("--full-archives", action="store_true")
    query_parser = subparsers.add_parser("query")
    query_parser.add_argument("--project", required=True)
    query_parser.add_argument("--selectors-json", default="{}")
    query_parser.add_argument("--limit", type=int, default=20)
    route_parser = subparsers.add_parser("route-evidence")
    route_parser.add_argument("--project", required=True)
    route_parser.add_argument("--context-json", required=True)
    route_parser.add_argument("--candidate-agent", action="append", default=[])
    route_parser.add_argument("--candidate-skill-key", action="append", default=[])
    review_parser = subparsers.add_parser("review-evidence")
    review_parser.add_argument("--project", required=True)
    review_parser.add_argument("--context-json", required=True)
    review_parser.add_argument("--candidate-agent", action="append", default=[])
    review_parser.add_argument("--candidate-skill-key", action="append", default=[])
    review_parser.add_argument("--risk-level", choices=sorted(RISK_LEVELS), required=True)

    outcome_parser = subparsers.add_parser("record-outcome")
    _add_mutation_args(outcome_parser)
    outcome_parser.add_argument("--outcome-json", required=True)
    invalidate_parser = subparsers.add_parser("invalidate-outcome")
    _add_mutation_args(invalidate_parser)
    invalidate_parser.add_argument("--task-id", required=True)
    invalidate_parser.add_argument("--reason", required=True)
    invalidate_parser.add_argument("--evidence", action="append", required=True)
    attribution_parser = subparsers.add_parser("revise-attribution")
    _add_mutation_args(attribution_parser)
    attribution_parser.add_argument("--task-id", required=True)
    attribution_parser.add_argument("--attribution-json", required=True)
    attribution_parser.add_argument("--reason", required=True)
    attribution_parser.add_argument("--evidence", action="append", required=True)

    lesson_parser = subparsers.add_parser("accept-lesson")
    _add_mutation_args(lesson_parser)
    lesson_parser.add_argument("--lesson-json", required=True)
    lesson_parser.add_argument("--merge-into")
    lesson_parser.add_argument("--merge-reason")
    reject_parser = subparsers.add_parser("reject-lesson-candidate")
    reject_parser.add_argument("--project", required=True)
    reject_parser.add_argument("--lesson-id", required=True)
    reject_parser.add_argument("--reason", required=True)
    lesson_transition_parser = subparsers.add_parser("transition-lesson")
    _add_mutation_args(lesson_transition_parser)
    lesson_transition_parser.add_argument("--lesson-id", required=True)
    lesson_transition_parser.add_argument("--status", choices=sorted(LESSON_STATUSES - {"ACTIVE"}), required=True)
    lesson_transition_parser.add_argument("--reason", required=True)
    lesson_transition_parser.add_argument("--evidence", action="append", required=True)
    lesson_transition_parser.add_argument("--superseded-by")

    decision_parser = subparsers.add_parser("record-decision-outcome")
    _add_mutation_args(decision_parser)
    decision_parser.add_argument("--decision-json", required=True)
    routing_parser = subparsers.add_parser("record-routing")
    _add_mutation_args(routing_parser)
    routing_parser.add_argument("--routing-json", required=True)
    organization_parser = subparsers.add_parser("record-organization-pattern")
    _add_mutation_args(organization_parser)
    organization_parser.add_argument("--pattern-json", required=True)
    debt_parser = subparsers.add_parser("record-review-debt")
    _add_mutation_args(debt_parser)
    debt_parser.add_argument("--task-id", required=True)
    debt_parser.add_argument("--agent-id", required=True)
    debt_parser.add_argument("--severity", choices=["MINOR", "MAJOR", "REPEATED", "FUNDAMENTAL"], required=True)
    debt_parser.add_argument("--reason", required=True)
    debt_parser.add_argument("--evidence", action="append", required=True)
    health_parser = subparsers.add_parser("record-thread-health")
    _add_mutation_args(health_parser)
    health_parser.add_argument("--agent-id", required=True)
    health_parser.add_argument("--thread-record-id", required=True)
    health_parser.add_argument("--event-type", choices=["HANDOFF", "RECOVERY", "CONTEXT_HAZARD", "ARCHIVED", "INTERRUPTED"], required=True)
    health_parser.add_argument("--evidence", action="append", required=True)

    retract_parser = subparsers.add_parser("retract")
    _add_mutation_args(retract_parser)
    retract_parser.add_argument("--record-type", choices=sorted(PUBLIC_RECORD_KINDS), required=True)
    retract_parser.add_argument("--record-id", required=True)
    retract_parser.add_argument("--authority-kind", choices=["FOUNDER"], required=True)
    retract_parser.add_argument("--founder-receipt", required=True)
    retract_parser.add_argument("--reason", required=True)
    retract_parser.add_argument("--evidence", action="append", required=True)
    compact_parser = subparsers.add_parser("compact")
    _add_mutation_args(compact_parser)
    compact_parser.add_argument("--retain-active-events", type=int, default=200)
    recover_parser = subparsers.add_parser("recover-lock")
    recover_parser.add_argument("--project", required=True)
    recover_parser.add_argument("--owner", required=True)
    recover_parser.add_argument("--activation-token", required=True)
    recover_parser.add_argument("--expected-state-sha", required=True)
    recover_parser.add_argument("--nonce", required=True)
    return parser


def emit(payload: dict[str, Any], exit_code: int = 0) -> int:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return exit_code


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "inspect":
            payload = inspect_memory(args.project)
        elif args.command == "verify":
            payload = verify_memory(args.project, full_archives=args.full_archives)
        elif args.command == "query":
            payload = query_memory(
                args.project,
                selectors=_json_argument(args.selectors_json, "selectors-json"),
                limit=args.limit,
            )
        elif args.command == "route-evidence":
            payload = route_evidence(
                args.project,
                context=_json_argument(args.context_json, "context-json"),
                candidate_agent_ids=args.candidate_agent,
                candidate_skill_keys=args.candidate_skill_key,
            )
        elif args.command == "review-evidence":
            payload = review_evidence(
                args.project,
                context=_json_argument(args.context_json, "context-json"),
                candidate_agent_ids=args.candidate_agent,
                candidate_skill_keys=args.candidate_skill_key,
                risk_level=args.risk_level,
            )
        elif args.command == "reject-lesson-candidate":
            root, _founder, _created = guard.resolve_project_root(args.project)
            payload = {
                "result": "LESSON_CANDIDATE_REJECTED",
                "project_root": str(root),
                "lesson_id": _identifier(args.lesson_id, "lesson_id"),
                "reason": _text(args.reason, "Lesson rejection reason"),
                "canonical_memory_changed": False,
                "changed_paths": [],
            }
        elif args.command == "recover-lock":
            payload = recover_memory_lock(
                args.project,
                owner=args.owner,
                activation_token=args.activation_token,
                expected_state_sha=args.expected_state_sha,
                nonce=args.nonce,
            )
        else:
            common = {
                "project": args.project,
                "owner": args.owner,
                "activation_token": args.activation_token,
                "expected_state_sha": args.expected_state_sha,
                "expected_memory_sha": args.expected_memory_sha,
            }
            if args.command == "record-outcome":
                payload = record_task_outcome(
                    **common, outcome=_json_argument(args.outcome_json, "outcome-json")
                )
            elif args.command == "invalidate-outcome":
                payload = invalidate_outcome(
                    **common, task_id=args.task_id, reason=args.reason, evidence_refs=args.evidence
                )
            elif args.command == "revise-attribution":
                payload = revise_attribution(
                    **common,
                    task_id=args.task_id,
                    attribution=_json_argument(args.attribution_json, "attribution-json"),
                    reason=args.reason,
                    evidence_refs=args.evidence,
                )
            elif args.command == "accept-lesson":
                payload = accept_lesson(
                    **common,
                    lesson=_json_argument(args.lesson_json, "lesson-json"),
                    merge_into=args.merge_into,
                    merge_reason=args.merge_reason,
                )
            elif args.command == "transition-lesson":
                payload = transition_lesson(
                    **common,
                    lesson_id=args.lesson_id,
                    status=args.status,
                    reason=args.reason,
                    evidence_refs=args.evidence,
                    superseded_by=args.superseded_by,
                )
            elif args.command == "record-decision-outcome":
                payload = record_decision_outcome(
                    **common, decision=_json_argument(args.decision_json, "decision-json")
                )
            elif args.command == "record-routing":
                payload = record_routing_decision(
                    **common, routing=_json_argument(args.routing_json, "routing-json")
                )
            elif args.command == "record-organization-pattern":
                payload = record_organization_pattern(
                    **common, pattern=_json_argument(args.pattern_json, "pattern-json")
                )
            elif args.command == "record-review-debt":
                payload = record_review_debt(
                    **common, task_id=args.task_id, agent_id=args.agent_id,
                    severity=args.severity, reason=args.reason, evidence_refs=args.evidence,
                )
            elif args.command == "record-thread-health":
                payload = record_thread_health(
                    **common, agent_id=args.agent_id, thread_record_id=args.thread_record_id,
                    event_type=args.event_type, evidence_refs=args.evidence,
                )
            elif args.command == "retract":
                payload = retract_memory(
                    **common, record_type=args.record_type, record_id=args.record_id,
                    authority_kind=args.authority_kind, founder_receipt=args.founder_receipt,
                    reason=args.reason, evidence_refs=args.evidence,
                )
            elif args.command == "compact":
                payload = compact_memory(
                    **common, retain_active_events=args.retain_active_events
                )
            else:  # pragma: no cover
                raise guard.InvalidState(f"Unsupported Memory command: {args.command}")
        return emit(payload)
    except MemoryPartialCommit as exc:
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
            {"result": "CONFLICT", "mode": "ADVISOR_OR_RECOVERY_REQUIRED", "reason": str(exc), "changed_paths": []},
            EXIT_CONFLICT,
        )
    except (guard.InvalidState, OSError, ValueError, TypeError, AttributeError) as exc:
        return emit(
            {"result": "INVALID", "mode": "READ_ONLY_REQUIRED", "reason": str(exc), "changed_paths": []},
            EXIT_INVALID,
        )


if __name__ == "__main__":
    raise SystemExit(main())

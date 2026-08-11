#!/usr/bin/env python3
"""Atomic cooperative guard for FounderOS ACTIVE supervisor transitions.

This helper manages only `.founder/ACTIVE_SUPERVISOR.json` and the existing
`.founder/.write-lock.json`. It does not infer runtime liveness, authenticate a
caller, edit canonical ledgers, or clean unknown locks.
"""

from __future__ import annotations

import argparse
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
from typing import Any


STATE_NAME = "ACTIVE_SUPERVISOR.json"
LOCK_NAME = ".write-lock.json"
SCHEMA_VERSION = 1
EXIT_INVALID = 2
EXIT_CONFLICT = 3


class GuardError(RuntimeError):
    """Base controlled guard error."""


class InvalidState(GuardError):
    """Input, path, or state is malformed."""


class Conflict(GuardError):
    """A safe transition cannot be made from the observed state."""


class PartialCommit(GuardError):
    """A control record changed, but its write-lock transaction did not finish."""

    def __init__(
        self,
        message: str,
        *,
        changed_paths: list[str],
        recovery_action: str,
    ) -> None:
        super().__init__(message)
        self.changed_paths = changed_paths
        self.recovery_action = recovery_action


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def new_revision(prefix: str = "S") -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{prefix}-{stamp}-{secrets.token_hex(4)}"


def canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest().upper()


def require_nonempty_text(value: Any, label: str, *, max_length: int = 512) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidState(f"{label} must be a non-empty string")
    if value != value.strip() or len(value) > max_length:
        raise InvalidState(f"{label} has surrounding whitespace or is too long")
    if any(ord(character) < 32 for character in value):
        raise InvalidState(f"{label} contains a control character")
    return value


def validate_expected_state_sha(value: str) -> str:
    normalized = require_nonempty_text(value, "expected_state_sha").upper()
    if normalized != "ABSENT" and not re.fullmatch(r"[0-9A-F]{64}", normalized):
        raise InvalidState("expected_state_sha must be ABSENT or a SHA-256 value")
    return normalized


def _is_reparse_or_link(path: Path) -> bool:
    metadata = path.lstat()
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(metadata.st_mode) or bool(attributes & reparse_flag)


def _same_path(left: Path | str, right: Path | str) -> bool:
    left_path = Path(left)
    right_path = Path(right)
    try:
        if left_path.exists() and right_path.exists():
            return os.path.samefile(left_path, right_path)
    except OSError:
        pass
    left_resolved = left_path.resolve(strict=False)
    right_resolved = right_path.resolve(strict=False)
    return os.path.normcase(str(left_resolved)) == os.path.normcase(
        str(right_resolved)
    )


def resolve_project_root(
    raw_path: str, *, bootstrap: bool = False
) -> tuple[Path, Path, bool]:
    require_nonempty_text(raw_path, "project root")
    requested = Path(os.path.abspath(raw_path))
    if not requested.exists() or not requested.is_dir():
        raise InvalidState(f"Project root is not an existing directory: {requested}")
    if _is_reparse_or_link(requested):
        raise InvalidState(f"Project root may not be a link or reparse point: {requested}")
    resolved = requested.resolve(strict=True)
    if not _same_path(requested, resolved):
        raise InvalidState(
            f"Project root resolves through an unexpected target: {requested} -> {resolved}"
        )

    founder = resolved / ".founder"
    founder_created = False
    if bootstrap and not founder.exists():
        try:
            founder.mkdir()
            founder_created = True
        except FileExistsError:
            pass
    if not founder.exists() or not founder.is_dir():
        raise InvalidState(f"Founder state directory does not exist: {founder}")
    if _is_reparse_or_link(founder):
        raise InvalidState(f"Founder state may not be a link or reparse point: {founder}")
    if not _same_path(founder.resolve(strict=True).parent, resolved):
        raise InvalidState(f"Founder state is outside the project root: {founder}")
    return resolved, founder, founder_created


def read_json_object(path: Path) -> tuple[bytes, dict[str, Any]]:
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidState(f"Cannot read valid JSON from {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise InvalidState(f"JSON state must be an object: {path}")
    return raw, value


def state_observation(state_path: Path) -> tuple[str, dict[str, Any] | None]:
    if not state_path.exists():
        return "ABSENT", None
    metadata = state_path.lstat()
    if (
        _is_reparse_or_link(state_path)
        or not state_path.is_file()
        or metadata.st_nlink != 1
    ):
        raise InvalidState(f"Supervisor state is not a direct regular file: {state_path}")
    raw, record = read_json_object(state_path)
    return sha256_bytes(raw), record


def validate_record(record: dict[str, Any], project_root: Path) -> None:
    if record.get("schema_version") != SCHEMA_VERSION:
        raise InvalidState("Unsupported or missing supervisor schema_version")
    stored_root = record.get("project_root")
    if not isinstance(stored_root, str) or not Path(stored_root).is_absolute():
        raise InvalidState("Supervisor record project_root must be an absolute string")
    try:
        stored_resolved = Path(stored_root).resolve(strict=True)
    except OSError as exc:
        raise InvalidState("Supervisor record project_root cannot be resolved") from exc
    if (
        os.path.normcase(str(stored_resolved)) != os.path.normcase(str(project_root))
        or os.path.normcase(str(Path(stored_root)))
        != os.path.normcase(str(stored_resolved))
    ):
        raise InvalidState("Supervisor record project_root does not match the project")
    if record.get("mode") not in {"ACTIVE", "UNASSIGNED"}:
        raise InvalidState("Supervisor mode must be ACTIVE or UNASSIGNED")
    supervisor = record.get("supervisor")
    if not isinstance(supervisor, dict):
        raise InvalidState("Supervisor identity object is missing")
    if record["mode"] == "ACTIVE":
        require_nonempty_text(supervisor.get("logical_id"), "supervisor logical_id")
        require_nonempty_text(record.get("activation_token"), "activation_token")
    elif supervisor.get("logical_id") is not None or record.get("activation_token") is not None:
        raise InvalidState("UNASSIGNED record may not retain an owner or activation token")
    require_nonempty_text(record.get("record_revision"), "record_revision")
    if supervisor.get("identity_quality") not in {
        "stable",
        "observed",
        "ephemeral",
        "unavailable",
        None,
    }:
        raise InvalidState("Unknown supervisor identity_quality")
    runtime_identity = supervisor.get("runtime_identity")
    if runtime_identity is not None and not isinstance(runtime_identity, str):
        raise InvalidState("Supervisor runtime_identity must be a string or null")
    lease = record.get("lease")
    if not isinstance(lease, dict) or not isinstance(lease.get("state"), str):
        raise InvalidState("Supervisor lease object is malformed")
    handoff = record.get("handoff")
    if not isinstance(handoff, dict) or handoff.get("state") not in {
        "none",
        "offered",
    }:
        raise InvalidState("Invalid handoff state")
    if handoff.get("state") == "offered" and not handoff.get("target_logical_id"):
        raise InvalidState("Offered handoff is missing target_logical_id")
    if handoff.get("state") == "offered":
        require_nonempty_text(handoff.get("target_logical_id"), "handoff target")
        require_nonempty_text(handoff.get("basis"), "handoff basis")
        if not isinstance(handoff.get("source_revisions"), dict):
            raise InvalidState("Offered handoff is missing its frozen source fingerprints")
    if not isinstance(record.get("source_revisions"), dict):
        raise InvalidState("Supervisor record source_revisions must be an object")
    transition = record.get("transition")
    if not isinstance(transition, dict) or not isinstance(
        transition.get("kind"), str
    ):
        raise InvalidState("Supervisor transition object is malformed")
    previous = record.get("previous_supervisor")
    if previous is not None:
        if not isinstance(previous, dict) or not isinstance(
            previous.get("supervisor"), dict
        ):
            raise InvalidState("previous_supervisor is malformed")


def _canonical_text_snapshot(path: Path, revision_pattern: str) -> tuple[str | None, str | None]:
    if not path.exists():
        return None, None
    metadata = path.lstat()
    if _is_reparse_or_link(path) or not path.is_file() or metadata.st_nlink != 1:
        raise InvalidState(f"Canonical ledger must be a direct single-link file: {path}")
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise InvalidState(f"Cannot read canonical ledger {path}: {exc}") from exc
    match = re.search(revision_pattern, text)
    revision = match.group(1) if match else None
    return revision, sha256_bytes(raw)


def read_source_revisions(founder: Path) -> dict[str, str | None]:
    result: dict[str, str | None] = {}
    for name in ("PROJECT", "ROADMAP", "DECISIONS", "AGENTS"):
        path = founder / f"{name}.md"
        revision, content_sha = _canonical_text_snapshot(
            path, r"(?m)^- Last revision:\s*(\S+)\s*$"
        )
        result[name] = revision
        result[f"{name}_SHA256"] = content_sha
    status_path = founder / "STATUS.md"
    reconciled, status_sha = _canonical_text_snapshot(
        status_path, r"(?m)^- Reconciled revision:\s*(\S+)\s*$"
    )
    result["STATUS_RECONCILED"] = reconciled
    result["STATUS_SHA256"] = status_sha
    threads_path = founder / "THREADS.json"
    if threads_path.exists():
        metadata = threads_path.lstat()
        if (
            _is_reparse_or_link(threads_path)
            or not threads_path.is_file()
            or metadata.st_nlink != 1
        ):
            raise InvalidState(
                f"Thread registry must be a direct single-link file: {threads_path}"
            )
        raw, registry = read_json_object(threads_path)
        result["THREADS_REVISION"] = require_nonempty_text(
            registry.get("registry_revision"), "thread registry revision"
        )
        result["THREADS_SHA256"] = sha256_bytes(raw)
    strategy_path = founder / "STRATEGY.json"
    if strategy_path.exists():
        metadata = strategy_path.lstat()
        if (
            _is_reparse_or_link(strategy_path)
            or not strategy_path.is_file()
            or metadata.st_nlink != 1
        ):
            raise InvalidState(
                f"Strategy state must be a direct single-link file: {strategy_path}"
            )
        raw, strategy = read_json_object(strategy_path)
        result["STRATEGY_REVISION"] = require_nonempty_text(
            strategy.get("strategy_revision"), "strategy revision"
        )
        result["STRATEGY_SHA256"] = sha256_bytes(raw)
        result["STRATEGY_CONTEXT_REVISION"] = require_nonempty_text(
            strategy.get("context_revision"), "strategy context revision"
        )
        context_sha = require_nonempty_text(
            strategy.get("context_sha256"), "strategy context SHA-256"
        ).upper()
        if not re.fullmatch(r"[0-9A-F]{64}", context_sha):
            raise InvalidState("Strategy context_sha256 is malformed")
        result["STRATEGY_CONTEXT_SHA256"] = context_sha
    return result


def _atomic_create(path: Path, value: dict[str, Any]) -> None:
    data = canonical_json_bytes(value)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor: int | None = None
    created = False
    try:
        descriptor = os.open(path, flags, 0o600)
        created = True
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError as exc:
        raise Conflict(f"Atomic lease already exists: {path}") from exc
    except Exception:
        if descriptor is not None:
            os.close(descriptor)
        if created:
            try:
                path.unlink()
            except OSError:
                pass
        raise


def _atomic_replace(path: Path, value: dict[str, Any]) -> None:
    descriptor, staging_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".staging", dir=path.parent
    )
    staging = Path(staging_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(canonical_json_bytes(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(staging, path)
    except Exception:
        try:
            staging.unlink()
        except OSError:
            pass
        raise


def _lock_owner(lock_path: Path) -> dict[str, Any] | None:
    if not lock_path.exists():
        return None
    metadata = lock_path.lstat()
    if (
        _is_reparse_or_link(lock_path)
        or not lock_path.is_file()
        or metadata.st_nlink != 1
    ):
        raise InvalidState(f"Write lock is not a direct regular file: {lock_path}")
    _raw, lock = read_json_object(lock_path)
    return lock


def _release_owned_lock(
    lock_path: Path, owner: str, activation_token: str | None = None
) -> None:
    lock = _lock_owner(lock_path)
    if lock is None:
        raise Conflict("Write lock does not exist")
    if lock.get("owner") != owner or lock.get("supervisor_id") != owner:
        raise Conflict("Write lock belongs to another owner")
    if activation_token is not None and lock.get("activation_token") not in {
        None,
        activation_token,
    }:
        raise Conflict("Write lock activation token does not match")
    lock_path.unlink()


def _cleanup_bootstrap_founder(founder: Path, founder_created: bool) -> None:
    if not founder_created:
        return
    try:
        founder.rmdir()
    except OSError:
        # Remove only the exact directory this call created, and only if it is
        # still empty. Concurrent or user-created content is never deleted.
        pass


def source_fingerprints_match(
    recorded: Any,
    current: dict[str, str | None],
) -> bool:
    if not isinstance(recorded, dict):
        return False
    return recorded == current


def _validate_claim_candidate(
    record: dict[str, Any] | None,
    *,
    root: Path,
    owner: str,
    activation_token: str | None,
    source_revisions: dict[str, str | None],
) -> None:
    if record is None:
        return
    validate_record(record, root)
    current_owner = record["supervisor"].get("logical_id")
    if record["mode"] == "UNASSIGNED":
        if not source_fingerprints_match(
            record.get("source_revisions"),
            source_revisions,
        ):
            raise Conflict("Canonical source fingerprints changed since supervisor release")
        return
    if current_owner == owner:
        if not activation_token or not secrets.compare_digest(
            activation_token, record["activation_token"]
        ):
            raise Conflict("Existing ACTIVE owner requires its current activation token")
        if not source_fingerprints_match(
            record.get("source_revisions"),
            source_revisions,
        ):
            raise Conflict("Canonical source fingerprints changed since the ACTIVE baseline")
        return
    if (
        record["handoff"].get("state") == "offered"
        and record["handoff"].get("target_logical_id") == owner
    ):
        if not source_fingerprints_match(
            record["handoff"].get("source_revisions"), source_revisions
        ):
            raise Conflict("Canonical source fingerprints changed after the handoff offer")
        return
    raise Conflict("Another ACTIVE supervisor owns the project")


def _initial_lock(
    project_root: Path,
    founder: Path,
    owner: str,
    expected_state_sha: str,
    bootstrap: bool,
) -> dict[str, Any]:
    revisions = read_source_revisions(founder)
    return {
        "project_root": str(project_root),
        "owner": owner,
        "supervisor_id": owner,
        "activation_token": None,
        "supervisor_record_revision": None,
        "created_utc": utc_now(),
        "baseline_reconciled_revision": revisions.get("STATUS_RECONCILED"),
        "source_revisions": revisions,
        "expected_supervisor_state_sha": expected_state_sha,
        "bootstrap": bootstrap,
    }


def _new_active_record(
    *,
    project_root: Path,
    owner: str,
    runtime_id: str | None,
    identity_quality: str,
    transition_kind: str,
    authorization_ref: str | None,
    predecessor_liveness: str | None,
    source_revisions: dict[str, str | None],
    previous_supervisor: dict[str, Any] | None,
    activation_token: str | None = None,
    activated_at: str | None = None,
    record_revision: str | None = None,
) -> dict[str, Any]:
    now = utc_now()
    return {
        "schema_version": SCHEMA_VERSION,
        "record_revision": record_revision or new_revision(),
        "project_root": str(project_root),
        "mode": "ACTIVE",
        "supervisor": {
            "logical_id": owner,
            "runtime_identity": runtime_id,
            "identity_quality": identity_quality,
        },
        # Prefix keeps generated values from being parsed as CLI options when
        # callers pass them as a separate argv token.
        "activation_token": activation_token or f"T_{secrets.token_urlsafe(24)}",
        "activated_at": activated_at or now,
        "last_seen_at": now,
        "lease": {"state": "active", "time_is_liveness_evidence": False},
        "handoff": {
            "state": "none",
            "target_logical_id": None,
            "basis": None,
            "offered_at": None,
            "source_revisions": {},
        },
        "transition": {
            "kind": transition_kind,
            "authorization_ref": authorization_ref,
            "predecessor_liveness": predecessor_liveness,
            "at": now,
        },
        "previous_supervisor": previous_supervisor,
        "source_revisions": source_revisions,
    }


def _previous_identity(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "supervisor": record.get("supervisor"),
        "record_revision": record.get("record_revision"),
        "activated_at": record.get("activated_at"),
        "last_seen_at": record.get("last_seen_at"),
    }


def _commit_active_and_lock(
    state_path: Path,
    lock_path: Path,
    record: dict[str, Any],
    lock: dict[str, Any],
) -> str:
    _atomic_replace(state_path, record)
    try:
        state_sha, _observed = state_observation(state_path)
        lock["activation_token"] = record["activation_token"]
        lock["supervisor_record_revision"] = record["record_revision"]
        lock["committed_supervisor_state_sha"] = state_sha
        lock["source_revisions"] = record["source_revisions"]
        lock["baseline_reconciled_revision"] = record["source_revisions"].get(
            "STATUS_RECONCILED"
        )
        _atomic_replace(lock_path, lock)
    except Exception as exc:
        raise PartialCommit(
            "Supervisor state was committed but write-lock finalization failed; "
            "preserve the lock and reconcile it before any further write",
            changed_paths=[str(state_path), str(lock_path)],
            recovery_action="repair-lock",
        ) from exc
    return state_sha


def _validate_lock_record_binding(
    root: Path,
    record: dict[str, Any],
    state_sha: str,
    lock: dict[str, Any],
) -> None:
    if record["mode"] != "ACTIVE":
        raise Conflict("A write lock exists without an ACTIVE supervisor")
    lock_root = lock.get("project_root")
    if not isinstance(lock_root, str) or not Path(lock_root).is_absolute():
        raise InvalidState("Write lock project_root must be an absolute string")
    try:
        lock_root_resolved = Path(lock_root).resolve(strict=True)
    except OSError as exc:
        raise InvalidState("Write lock project_root cannot be resolved") from exc
    if (
        os.path.normcase(str(lock_root_resolved)) != os.path.normcase(str(root))
        or os.path.normcase(str(Path(lock_root)))
        != os.path.normcase(str(lock_root_resolved))
    ):
        raise Conflict("Write lock project_root does not match")
    record_owner = record["supervisor"].get("logical_id")
    if lock.get("owner") != record_owner or lock.get("supervisor_id") != record_owner:
        raise Conflict("Write lock owner does not match the supervisor record")
    if lock.get("activation_token") != record.get("activation_token"):
        raise Conflict("Write lock activation token does not match the supervisor record")
    if lock.get("supervisor_record_revision") != record.get("record_revision"):
        raise Conflict("Write lock is fenced by another supervisor revision")
    if lock.get("committed_supervisor_state_sha") != state_sha:
        raise Conflict("Write lock is not bound to the current supervisor state hash")
    if lock.get("source_revisions") != record.get("source_revisions"):
        raise Conflict("Write lock source fingerprints do not match the supervisor record")
    if lock.get("baseline_reconciled_revision") != record["source_revisions"].get(
        "STATUS_RECONCILED"
    ):
        raise Conflict("Write lock reconciled baseline does not match")


def inspect_state(
    project: str,
    *,
    candidate: str | None,
    activation_token: str | None,
    intent: str,
    requested_mode: str,
) -> dict[str, Any]:
    if candidate is not None:
        require_nonempty_text(candidate, "candidate")
    if activation_token is not None:
        require_nonempty_text(activation_token, "activation_token")
    root, founder, _founder_created = resolve_project_root(project)
    state_path = founder / STATE_NAME
    state_sha, record = state_observation(state_path)
    lock = _lock_owner(founder / LOCK_NAME)
    source_revisions = read_source_revisions(founder)

    if record is None:
        if lock is not None:
            suggested = "RECOVERY"
            reason = "ORPHAN_WRITE_LOCK_WITHOUT_SUPERVISOR_RECORD"
        else:
            suggested = (
                "ACTIVATION_ELIGIBLE"
                if intent == "execute" and requested_mode in {"AUTO", "ACTIVE"}
                else ("REVIEWER" if requested_mode == "REVIEWER" else "ADVISOR")
            )
            reason = "NO_SUPERVISOR_RECORD"
    else:
        validate_record(record, root)
        owner = record["supervisor"].get("logical_id")
        handoff = record["handoff"]
        control_issue = None
        if lock is not None:
            try:
                _validate_lock_record_binding(root, record, state_sha, lock)
            except GuardError as exc:
                control_issue = str(exc)
        fingerprints_current = source_fingerprints_match(
            record.get("source_revisions"), source_revisions
        )
        handoff_fingerprints_current = (
            handoff.get("state") != "offered"
            or source_fingerprints_match(
                handoff.get("source_revisions"), source_revisions
            )
        )
        if control_issue is not None:
            suggested = "RECOVERY"
            reason = f"CONTROL_BINDING_MISMATCH: {control_issue}"
        elif not fingerprints_current:
            suggested = "RECOVERY"
            reason = "CANONICAL_FINGERPRINT_DRIFT_OR_LEGACY_BASELINE"
        elif not handoff_fingerprints_current:
            suggested = "RECOVERY"
            reason = "HANDOFF_FROZEN_FINGERPRINT_DRIFT"
        elif intent == "read-only":
            suggested = "REVIEWER" if requested_mode == "REVIEWER" else "ADVISOR"
            reason = "READ_ONLY_INTENT"
        elif record["mode"] == "UNASSIGNED":
            suggested = "ACTIVATION_ELIGIBLE"
            reason = "SUPERVISOR_UNASSIGNED"
        elif (
            candidate
            and candidate == owner
            and activation_token
            and secrets.compare_digest(activation_token, record["activation_token"])
        ):
            suggested = "ACTIVE"
            reason = "FENCING_MATCH"
        elif (
            handoff.get("state") == "offered"
            and candidate
            and candidate == handoff.get("target_logical_id")
        ):
            if lock is None:
                suggested = "HANDOFF_ACCEPTANCE_ELIGIBLE"
                reason = "TARGETED_HANDOFF"
            else:
                suggested = "ADVISOR"
                reason = "HANDOFF_WAITING_FOR_SOURCE_LOCK_RELEASE"
        else:
            suggested = "REVIEWER" if requested_mode == "REVIEWER" else "ADVISOR"
            reason = "ANOTHER_ACTIVE_SUPERVISOR"

    return {
        "result": "INSPECTED",
        "mode": suggested,
        "reason": reason,
        "project_root": str(root),
        "state_sha": state_sha,
        "record": record,
        "write_lock": lock,
        "source_revisions": source_revisions,
        "changed_paths": [],
    }


def claim_active(
    project: str,
    *,
    owner: str,
    runtime_id: str | None,
    identity_quality: str,
    expected_state_sha: str,
    bootstrap: bool,
    activation_token: str | None,
) -> dict[str, Any]:
    require_nonempty_text(owner, "owner")
    if runtime_id is not None:
        require_nonempty_text(runtime_id, "runtime_id")
    if identity_quality not in {"stable", "observed", "ephemeral", "unavailable"}:
        raise InvalidState("Unknown identity_quality")
    if activation_token is not None:
        require_nonempty_text(activation_token, "activation_token")
    expected_state_sha = validate_expected_state_sha(expected_state_sha)
    root, founder, founder_created = resolve_project_root(project, bootstrap=bootstrap)
    state_path = founder / STATE_NAME
    lock_path = founder / LOCK_NAME
    lock_created = False
    try:
        preflight_sha, preflight_record = state_observation(state_path)
        if preflight_sha != expected_state_sha:
            raise Conflict(
                f"Supervisor state CAS mismatch: expected {expected_state_sha}, "
                f"observed {preflight_sha}"
            )
        preflight_sources = read_source_revisions(founder)
        _validate_claim_candidate(
            preflight_record,
            root=root,
            owner=owner,
            activation_token=activation_token,
            source_revisions=preflight_sources,
        )
        lock = _initial_lock(root, founder, owner, expected_state_sha, bootstrap)
        _atomic_create(lock_path, lock)
        lock_created = True
        observed_sha, record = state_observation(state_path)
        if observed_sha != expected_state_sha:
            raise Conflict(
                f"Supervisor state CAS mismatch: expected {expected_state_sha}, observed {observed_sha}"
            )
        source_revisions = read_source_revisions(founder)
        _validate_claim_candidate(
            record,
            root=root,
            owner=owner,
            activation_token=activation_token,
            source_revisions=source_revisions,
        )
        previous: dict[str, Any] | None = None
        transition_kind = "activation"
        activated_at = None
        preserved_token = None
        preserved_record_revision = None

        if record is not None:
            validate_record(record, root)
            current_owner = record["supervisor"].get("logical_id")
            if record["mode"] == "UNASSIGNED":
                previous = record.get("previous_supervisor")
            elif current_owner == owner:
                transition_kind = record.get("transition", {}).get("kind", "activation")
                previous = record.get("previous_supervisor")
                activated_at = record.get("activated_at")
                preserved_token = record["activation_token"]
                preserved_record_revision = record["record_revision"]
            elif (
                record.get("handoff", {}).get("state") == "offered"
                and record["handoff"].get("target_logical_id") == owner
            ):
                transition_kind = "handoff"
                previous = _previous_identity(record)
            else:  # pragma: no cover - preflight and post-lock validation reject this.
                raise Conflict("Another ACTIVE supervisor owns the project")

        next_record = _new_active_record(
            project_root=root,
            owner=owner,
            runtime_id=runtime_id,
            identity_quality=identity_quality,
            transition_kind=transition_kind,
            authorization_ref=(
                record.get("handoff", {}).get("basis") if record is not None else None
            ),
            predecessor_liveness=None,
            source_revisions=source_revisions,
            previous_supervisor=previous,
            activation_token=preserved_token,
            activated_at=activated_at,
            record_revision=preserved_record_revision,
        )
        next_sha = _commit_active_and_lock(state_path, lock_path, next_record, lock)
        return {
            "result": "ACTIVE_CLAIMED",
            "mode": "ACTIVE",
            "owner": owner,
            "activation_token": next_record["activation_token"],
            "record_revision": next_record["record_revision"],
            "state_sha": next_sha,
            "changed_paths": [str(state_path), str(lock_path)],
            "reason": transition_kind.upper(),
        }
    except PartialCommit:
        raise
    except Exception:
        if lock_created:
            try:
                _release_owned_lock(lock_path, owner)
            except (GuardError, OSError):
                pass
        _cleanup_bootstrap_founder(founder, founder_created)
        raise


def verify_fence(
    project: str,
    *,
    owner: str,
    activation_token: str,
    allow_canonical_drift: bool = False,
) -> dict[str, Any]:
    require_nonempty_text(owner, "owner")
    require_nonempty_text(activation_token, "activation_token")
    root, founder, _founder_created = resolve_project_root(project)
    state_sha, record = state_observation(founder / STATE_NAME)
    if record is None:
        raise Conflict("Supervisor state does not exist")
    validate_record(record, root)
    lock = _lock_owner(founder / LOCK_NAME)
    if lock is None:
        raise Conflict("Current turn does not hold the write lock")
    _validate_lock_record_binding(root, record, state_sha, lock)
    if record["mode"] != "ACTIVE":
        raise Conflict("Project has no ACTIVE supervisor")
    if record["supervisor"].get("logical_id") != owner:
        raise Conflict("Supervisor owner does not match")
    if not secrets.compare_digest(record["activation_token"], activation_token):
        raise Conflict("Supervisor activation token does not match")
    current_sources = read_source_revisions(founder)
    canonical_current = source_fingerprints_match(
        record.get("source_revisions"), current_sources
    )
    if not canonical_current and not allow_canonical_drift:
        raise Conflict("Canonical fingerprints changed; inspect and checkpoint before continuing")
    return {
        "result": "FENCE_VALID" if canonical_current else "FENCE_VALID_CHECKPOINT_ONLY",
        "mode": "ACTIVE",
        "owner": owner,
        "state_sha": state_sha,
        "record_revision": record["record_revision"],
        "canonical_fingerprints": "current" if canonical_current else "checkpoint-required",
        "changed_paths": [],
    }


def offer_handoff(
    project: str,
    *,
    owner: str,
    activation_token: str,
    target: str,
    basis: str,
    expected_state_sha: str,
) -> dict[str, Any]:
    require_nonempty_text(target, "handoff target")
    require_nonempty_text(basis, "handoff basis")
    if target == owner:
        raise InvalidState("handoff target must differ from the current owner")
    expected_state_sha = validate_expected_state_sha(expected_state_sha)
    fence = verify_fence(project, owner=owner, activation_token=activation_token)
    root, founder, _founder_created = resolve_project_root(project)
    state_path = founder / STATE_NAME
    observed_sha, record = state_observation(state_path)
    if observed_sha != expected_state_sha:
        raise Conflict("Supervisor state changed before handoff offer")
    assert record is not None
    current_revisions = read_source_revisions(founder)
    if not source_fingerprints_match(record.get("source_revisions"), current_revisions):
        raise Conflict("Checkpoint canonical changes before offering handoff")
    record["last_seen_at"] = utc_now()
    record["handoff"] = {
        "state": "offered",
        "target_logical_id": target,
        "basis": basis,
        "offered_at": utc_now(),
        "source_revisions": current_revisions,
    }
    record["source_revisions"] = current_revisions
    lock_path = founder / LOCK_NAME
    lock = _lock_owner(lock_path)
    if lock is None:  # pragma: no cover - verify_fence already requires it.
        raise Conflict("Current turn does not hold the write lock")
    next_sha = _commit_active_and_lock(state_path, lock_path, record, lock)
    return {
        "result": "HANDOFF_OFFERED",
        "mode": "ACTIVE",
        "owner": owner,
        "target": target,
        "state_sha": next_sha,
        "record_revision": record["record_revision"],
        "changed_paths": [str(state_path), str(lock_path)],
        "prior_fence": fence["result"],
    }


def checkpoint_active(
    project: str,
    *,
    owner: str,
    activation_token: str,
    expected_state_sha: str,
) -> dict[str, Any]:
    """Commit the current canonical revision map under the held write fence."""

    expected_state_sha = validate_expected_state_sha(expected_state_sha)
    fence = verify_fence(
        project,
        owner=owner,
        activation_token=activation_token,
        allow_canonical_drift=True,
    )
    root, founder, _founder_created = resolve_project_root(project)
    state_path = founder / STATE_NAME
    lock_path = founder / LOCK_NAME
    observed_sha, record = state_observation(state_path)
    if observed_sha != expected_state_sha or record is None:
        raise Conflict("Supervisor state changed before checkpoint")
    current_revisions = read_source_revisions(founder)
    if (
        record.get("handoff", {}).get("state") == "offered"
        and not source_fingerprints_match(
            record["handoff"].get("source_revisions"), current_revisions
        )
    ):
        raise Conflict("Cannot checkpoint canonical changes after a handoff offer")
    record["last_seen_at"] = utc_now()
    record["source_revisions"] = current_revisions
    lock = _lock_owner(lock_path)
    if lock is None:  # pragma: no cover - verify_fence already requires it.
        raise Conflict("Current turn does not hold the write lock")
    next_sha = _commit_active_and_lock(state_path, lock_path, record, lock)
    return {
        "result": "SUPERVISOR_CHECKPOINTED",
        "mode": "ACTIVE",
        "owner": owner,
        "state_sha": next_sha,
        "record_revision": record["record_revision"],
        "source_revisions": record["source_revisions"],
        "changed_paths": [str(state_path), str(lock_path)],
        "prior_fence": fence["result"],
    }


def recover_active(
    project: str,
    *,
    owner: str,
    runtime_id: str | None,
    identity_quality: str,
    expected_state_sha: str,
    kind: str,
    predecessor_liveness: str,
    authorization_ref: str,
) -> dict[str, Any]:
    require_nonempty_text(owner, "owner")
    if runtime_id is not None:
        require_nonempty_text(runtime_id, "runtime_id")
    if identity_quality not in {"stable", "observed", "ephemeral", "unavailable"}:
        raise InvalidState("Unknown identity_quality")
    require_nonempty_text(authorization_ref, "authorization_ref")
    expected_state_sha = validate_expected_state_sha(expected_state_sha)
    if predecessor_liveness != "terminated":
        raise Conflict("Recovery requires predecessor_liveness=terminated")
    root, founder, _founder_created = resolve_project_root(project)
    state_path = founder / STATE_NAME
    lock_path = founder / LOCK_NAME
    lock_created = False
    try:
        preflight_sha, preflight_record = state_observation(state_path)
        if preflight_sha != expected_state_sha or preflight_record is None:
            raise Conflict("Supervisor state CAS mismatch during recovery")
        validate_record(preflight_record, root)
        if preflight_record["mode"] != "ACTIVE":
            raise Conflict("Use normal claim when no predecessor is ACTIVE")
        if preflight_record["supervisor"].get("logical_id") == owner:
            raise Conflict("Current owner should use normal claim, not recovery")
        if not source_fingerprints_match(
            preflight_record.get("source_revisions"), read_source_revisions(founder)
        ):
            raise Conflict("Canonical source fingerprints changed since supervisor baseline")
        lock = _initial_lock(root, founder, owner, expected_state_sha, False)
        _atomic_create(lock_path, lock)
        lock_created = True
        observed_sha, record = state_observation(state_path)
        if observed_sha != expected_state_sha or record is None:
            raise Conflict("Supervisor state CAS mismatch during recovery")
        validate_record(record, root)
        if record["mode"] != "ACTIVE":
            raise Conflict("Use normal claim when no predecessor is ACTIVE")
        if record["supervisor"].get("logical_id") == owner:
            raise Conflict("Current owner should use normal claim, not recovery")
        current_revisions = read_source_revisions(founder)
        if not source_fingerprints_match(record.get("source_revisions"), current_revisions):
            raise Conflict("Canonical source fingerprints changed since supervisor baseline")
        next_record = _new_active_record(
            project_root=root,
            owner=owner,
            runtime_id=runtime_id,
            identity_quality=identity_quality,
            transition_kind=kind,
            authorization_ref=authorization_ref,
            predecessor_liveness=predecessor_liveness,
            source_revisions=current_revisions,
            previous_supervisor=_previous_identity(record),
        )
        next_sha = _commit_active_and_lock(state_path, lock_path, next_record, lock)
        return {
            "result": "RECOVERY_CLAIMED",
            "mode": "ACTIVE",
            "owner": owner,
            "activation_token": next_record["activation_token"],
            "record_revision": next_record["record_revision"],
            "state_sha": next_sha,
            "changed_paths": [str(state_path), str(lock_path)],
            "reason": kind.upper(),
        }
    except PartialCommit:
        raise
    except Exception:
        if lock_created:
            try:
                _release_owned_lock(lock_path, owner)
            except (GuardError, OSError):
                pass
        raise


def repair_lock(
    project: str,
    *,
    owner: str,
    activation_token: str,
    expected_state_sha: str,
) -> dict[str, Any]:
    """Reconcile a preserved lock after ACTIVE state committed successfully."""

    require_nonempty_text(owner, "owner")
    require_nonempty_text(activation_token, "activation_token")
    expected_state_sha = validate_expected_state_sha(expected_state_sha)
    root, founder, _founder_created = resolve_project_root(project)
    state_path = founder / STATE_NAME
    lock_path = founder / LOCK_NAME
    observed_sha, record = state_observation(state_path)
    if observed_sha != expected_state_sha or record is None:
        raise Conflict("Supervisor state changed before lock repair")
    validate_record(record, root)
    if record["mode"] != "ACTIVE":
        raise Conflict("repair-lock requires an ACTIVE supervisor record")
    if record["supervisor"].get("logical_id") != owner:
        raise Conflict("Supervisor owner does not match lock repair owner")
    if not secrets.compare_digest(record["activation_token"], activation_token):
        raise Conflict("Supervisor activation token does not match lock repair")
    lock = _lock_owner(lock_path)
    if lock is None:
        raise Conflict("Preserved write lock does not exist")
    if lock.get("owner") != owner or lock.get("supervisor_id") != owner:
        raise Conflict("Preserved write lock belongs to another owner")
    if lock.get("project_root") != str(root):
        raise Conflict("Preserved write lock project_root does not match")
    if lock.get("activation_token") not in {None, activation_token}:
        raise Conflict("Preserved write lock has a conflicting activation token")
    confirm_sha, _ = state_observation(state_path)
    if confirm_sha != observed_sha:
        raise Conflict("Supervisor state changed during lock repair")
    lock["activation_token"] = activation_token
    lock["supervisor_record_revision"] = record["record_revision"]
    lock["committed_supervisor_state_sha"] = observed_sha
    lock["source_revisions"] = record["source_revisions"]
    lock["baseline_reconciled_revision"] = record["source_revisions"].get(
        "STATUS_RECONCILED"
    )
    _atomic_replace(lock_path, lock)
    return {
        "result": "WRITE_LOCK_REPAIRED",
        "mode": "ACTIVE",
        "owner": owner,
        "state_sha": observed_sha,
        "record_revision": record["record_revision"],
        "changed_paths": [str(lock_path)],
    }


def clear_released_lock(
    project: str,
    *,
    owner: str,
    activation_token: str,
    expected_state_sha: str,
) -> dict[str, Any]:
    """Clear an old owned lock after an UNASSIGNED release record committed."""

    require_nonempty_text(owner, "owner")
    require_nonempty_text(activation_token, "activation_token")
    expected_state_sha = validate_expected_state_sha(expected_state_sha)
    root, founder, _founder_created = resolve_project_root(project)
    state_path = founder / STATE_NAME
    lock_path = founder / LOCK_NAME
    observed_sha, record = state_observation(state_path)
    if observed_sha != expected_state_sha or record is None:
        raise Conflict("Supervisor state changed before released-lock cleanup")
    validate_record(record, root)
    previous = record.get("previous_supervisor") or {}
    previous_supervisor = previous.get("supervisor") or {}
    if (
        record["mode"] != "UNASSIGNED"
        or record.get("transition", {}).get("kind") != "release"
        or previous_supervisor.get("logical_id") != owner
    ):
        raise Conflict("State does not prove this owner's completed supervisor release")
    lock = _lock_owner(lock_path)
    if lock is None:
        raise Conflict("Preserved write lock does not exist")
    if lock.get("owner") != owner or lock.get("supervisor_id") != owner:
        raise Conflict("Preserved write lock belongs to another owner")
    if lock.get("project_root") != str(root):
        raise Conflict("Preserved write lock project_root does not match")
    if lock.get("activation_token") != activation_token:
        raise Conflict("Preserved write lock activation token does not match")
    confirm_sha, _ = state_observation(state_path)
    if confirm_sha != observed_sha:
        raise Conflict("Supervisor state changed during released-lock cleanup")
    lock_path.unlink()
    return {
        "result": "RELEASED_WRITE_LOCK_CLEARED",
        "mode": "UNASSIGNED",
        "owner": None,
        "state_sha": observed_sha,
        "changed_paths": [str(lock_path)],
    }


def release_lock(project: str, *, owner: str, activation_token: str) -> dict[str, Any]:
    verify_fence(project, owner=owner, activation_token=activation_token)
    _root, founder, _founder_created = resolve_project_root(project)
    _state_sha, record = state_observation(founder / STATE_NAME)
    assert record is not None
    if not source_fingerprints_match(
        record.get("source_revisions"), read_source_revisions(founder)
    ):
        raise Conflict("Checkpoint canonical changes before releasing the write lock")
    lock_path = founder / LOCK_NAME
    _release_owned_lock(lock_path, owner, activation_token)
    state_sha, record = state_observation(founder / STATE_NAME)
    return {
        "result": "WRITE_LOCK_RELEASED",
        "mode": record.get("mode") if record else None,
        "owner": owner,
        "state_sha": state_sha,
        "changed_paths": [str(lock_path)],
    }


def release_supervisor(
    project: str,
    *,
    owner: str,
    activation_token: str,
    expected_state_sha: str,
    basis: str,
) -> dict[str, Any]:
    require_nonempty_text(basis, "release basis")
    expected_state_sha = validate_expected_state_sha(expected_state_sha)
    verify_fence(project, owner=owner, activation_token=activation_token)
    root, founder, _founder_created = resolve_project_root(project)
    state_path = founder / STATE_NAME
    observed_sha, record = state_observation(state_path)
    if observed_sha != expected_state_sha or record is None:
        raise Conflict("Supervisor state changed before release")
    current_revisions = read_source_revisions(founder)
    if not source_fingerprints_match(record.get("source_revisions"), current_revisions):
        raise Conflict("Checkpoint canonical changes before releasing the supervisor")
    previous = _previous_identity(record)
    now = utc_now()
    next_record = {
        "schema_version": SCHEMA_VERSION,
        "record_revision": new_revision(),
        "project_root": str(root),
        "mode": "UNASSIGNED",
        "supervisor": {
            "logical_id": None,
            "runtime_identity": None,
            "identity_quality": None,
        },
        "activation_token": None,
        "activated_at": None,
        "last_seen_at": now,
        "lease": {"state": "released", "time_is_liveness_evidence": False},
        "handoff": {
            "state": "none",
            "target_logical_id": None,
            "basis": None,
            "offered_at": None,
            "source_revisions": {},
        },
        "transition": {
            "kind": "release",
            "authorization_ref": basis,
            "predecessor_liveness": "released",
            "at": now,
        },
        "previous_supervisor": previous,
        "source_revisions": current_revisions,
    }
    _atomic_replace(state_path, next_record)
    next_sha, _ = state_observation(state_path)
    lock_path = founder / LOCK_NAME
    try:
        _release_owned_lock(lock_path, owner, activation_token)
    except Exception as exc:
        raise PartialCommit(
            "Supervisor release state was committed but write-lock cleanup failed; "
            "preserve the lock and reconcile it before a new activation",
            changed_paths=[str(state_path), str(lock_path)],
            recovery_action="clear-released-lock",
        ) from exc
    return {
        "result": "SUPERVISOR_RELEASED",
        "mode": "UNASSIGNED",
        "owner": None,
        "state_sha": next_sha,
        "record_revision": next_record["record_revision"],
        "changed_paths": [str(state_path), str(lock_path)],
    }


def emit(payload: dict[str, Any], exit_code: int = 0) -> int:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return exit_code


def add_common_identity(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--owner", required=True)
    parser.add_argument("--runtime-id")
    parser.add_argument(
        "--identity-quality",
        choices=("stable", "observed", "ephemeral", "unavailable"),
        default="unavailable",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect_parser = subparsers.add_parser("inspect")
    inspect_parser.add_argument("--project", required=True)
    inspect_parser.add_argument("--candidate")
    inspect_parser.add_argument("--activation-token")
    inspect_parser.add_argument("--intent", choices=("read-only", "execute"), default="read-only")
    inspect_parser.add_argument(
        "--requested-mode",
        choices=("AUTO", "ACTIVE", "ADVISOR", "REVIEWER"),
        default="AUTO",
    )

    claim_parser = subparsers.add_parser("claim")
    claim_parser.add_argument("--project", required=True)
    add_common_identity(claim_parser)
    claim_parser.add_argument("--expected-state-sha", required=True)
    claim_parser.add_argument(
        "--activation-token",
        help="required only when the current protocol transition explicitly proves possession of an existing ACTIVE token; never copy it from the control file",
    )
    claim_parser.add_argument("--bootstrap", action="store_true")

    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--project", required=True)
    verify_parser.add_argument("--owner", required=True)
    verify_parser.add_argument("--activation-token", required=True)

    handoff_parser = subparsers.add_parser("offer-handoff")
    handoff_parser.add_argument("--project", required=True)
    handoff_parser.add_argument("--owner", required=True)
    handoff_parser.add_argument("--activation-token", required=True)
    handoff_parser.add_argument("--to", required=True)
    handoff_parser.add_argument("--basis", required=True)
    handoff_parser.add_argument("--expected-state-sha", required=True)

    checkpoint_parser = subparsers.add_parser("checkpoint")
    checkpoint_parser.add_argument("--project", required=True)
    checkpoint_parser.add_argument("--owner", required=True)
    checkpoint_parser.add_argument("--activation-token", required=True)
    checkpoint_parser.add_argument("--expected-state-sha", required=True)

    recover_parser = subparsers.add_parser("recover")
    recover_parser.add_argument("--project", required=True)
    add_common_identity(recover_parser)
    recover_parser.add_argument("--expected-state-sha", required=True)
    recover_parser.add_argument("--kind", choices=("takeover", "recovery"), required=True)
    recover_parser.add_argument(
        "--predecessor-liveness", choices=("terminated", "active", "unknown"), required=True
    )
    recover_parser.add_argument("--authorization-ref", required=True)

    unlock_parser = subparsers.add_parser("release-lock")
    unlock_parser.add_argument("--project", required=True)
    unlock_parser.add_argument("--owner", required=True)
    unlock_parser.add_argument("--activation-token", required=True)

    repair_parser = subparsers.add_parser("repair-lock")
    repair_parser.add_argument("--project", required=True)
    repair_parser.add_argument("--owner", required=True)
    repair_parser.add_argument("--activation-token", required=True)
    repair_parser.add_argument("--expected-state-sha", required=True)

    clear_parser = subparsers.add_parser("clear-released-lock")
    clear_parser.add_argument("--project", required=True)
    clear_parser.add_argument("--owner", required=True)
    clear_parser.add_argument("--activation-token", required=True)
    clear_parser.add_argument("--expected-state-sha", required=True)

    release_parser = subparsers.add_parser("release-supervisor")
    release_parser.add_argument("--project", required=True)
    release_parser.add_argument("--owner", required=True)
    release_parser.add_argument("--activation-token", required=True)
    release_parser.add_argument("--expected-state-sha", required=True)
    release_parser.add_argument("--basis", required=True)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "inspect":
            payload = inspect_state(
                args.project,
                candidate=args.candidate,
                activation_token=args.activation_token,
                intent=args.intent,
                requested_mode=args.requested_mode,
            )
        elif args.command == "claim":
            payload = claim_active(
                args.project,
                owner=args.owner,
                runtime_id=args.runtime_id,
                identity_quality=args.identity_quality,
                expected_state_sha=args.expected_state_sha,
                bootstrap=args.bootstrap,
                activation_token=args.activation_token,
            )
        elif args.command == "verify":
            payload = verify_fence(
                args.project,
                owner=args.owner,
                activation_token=args.activation_token,
            )
        elif args.command == "offer-handoff":
            payload = offer_handoff(
                args.project,
                owner=args.owner,
                activation_token=args.activation_token,
                target=args.to,
                basis=args.basis,
                expected_state_sha=args.expected_state_sha,
            )
        elif args.command == "checkpoint":
            payload = checkpoint_active(
                args.project,
                owner=args.owner,
                activation_token=args.activation_token,
                expected_state_sha=args.expected_state_sha,
            )
        elif args.command == "recover":
            payload = recover_active(
                args.project,
                owner=args.owner,
                runtime_id=args.runtime_id,
                identity_quality=args.identity_quality,
                expected_state_sha=args.expected_state_sha,
                kind=args.kind,
                predecessor_liveness=args.predecessor_liveness,
                authorization_ref=args.authorization_ref,
            )
        elif args.command == "release-lock":
            payload = release_lock(
                args.project,
                owner=args.owner,
                activation_token=args.activation_token,
            )
        elif args.command == "repair-lock":
            payload = repair_lock(
                args.project,
                owner=args.owner,
                activation_token=args.activation_token,
                expected_state_sha=args.expected_state_sha,
            )
        elif args.command == "clear-released-lock":
            payload = clear_released_lock(
                args.project,
                owner=args.owner,
                activation_token=args.activation_token,
                expected_state_sha=args.expected_state_sha,
            )
        elif args.command == "release-supervisor":
            payload = release_supervisor(
                args.project,
                owner=args.owner,
                activation_token=args.activation_token,
                expected_state_sha=args.expected_state_sha,
                basis=args.basis,
            )
        else:  # pragma: no cover - argparse enforces the command set.
            raise InvalidState(f"Unsupported command: {args.command}")
        return emit(payload)
    except PartialCommit as exc:
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
    except Conflict as exc:
        return emit(
            {
                "result": "CONFLICT",
                "mode": "ADVISOR_OR_RECOVERY_REQUIRED",
                "reason": str(exc),
                "changed_paths": [],
            },
            EXIT_CONFLICT,
        )
    except (InvalidState, OSError, ValueError, TypeError, AttributeError) as exc:
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
    sys.exit(main())

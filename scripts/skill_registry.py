#!/usr/bin/env python3
"""Deterministic FounderOS project Skill Registry and supply-chain lock.

``SKILL_LOCK.json`` is the machine binding authority. ``SKILLS.md`` is a
human-readable projection generated from exactly the same revision.  This
module validates explicit Curator/FounderOS decisions; it never decides that a
Skill is useful, safe, trusted, or appropriate from names or source text.

The helper does not discover, download, install, execute, update, or delete a
global Skill.  Mutations require the current ACTIVE Supervisor fence and two
CAS values: the Supervisor state SHA and the current Skill Lock SHA/ABSENT.
"""

from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import json
import os
import re
import secrets
import stat
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import yaml

if os.name == "nt":
    import ctypes
    from ctypes import wintypes

# Read-only commands must not leave __pycache__ in the installed Skill.
sys.dont_write_bytecode = True

import supervisor_guard as guard
import decision_state as strategy


REGISTRY_NAME = "SKILLS.md"
LOCK_NAME = "SKILL_LOCK.json"
TRANSACTION_LOCK_NAME = ".skill-registry-lock.json"
SCHEMA_VERSION = 1
EXIT_INVALID = 2
EXIT_CONFLICT = 3

TRUST_LEVELS = {
    "builtin-or-system",
    "local-reviewed",
    "third-party-audited",
    "third-party-unreviewed",
    "rejected",
}
BINDABLE_TRUST_LEVELS = {
    "builtin-or-system",
    "local-reviewed",
    "third-party-audited",
}
RISK_LEVELS = {"LOW", "MEDIUM", "HIGH", "BLOCKED"}
APPROVAL_MODES = {"AUTO", "FOUNDER", "EXPLICIT", "NONE", "REJECTED"}
APPROVAL_SENTINELS = {
    "none",
    "unknown",
    "unverified",
    "not confirmed",
    "not provided",
    "pending",
    "placeholder",
    "todo",
}
SKILL_STATUSES = {
    "DISCOVERED",
    "QUARANTINED",
    "AUDITED",
    "AUDITED_NOT_EXECUTED",
    "APPROVED",
    "INSTALLED",
    "VALIDATED",
    "AVAILABLE",
    "BOUND",
    "UPDATE_AVAILABLE",
    "SOURCE_UNAVAILABLE",
    "HASH_MISMATCH",
    "VERSION_MISMATCH",
    "REJECTED",
    "REVOKED",
    "DEPRECATED",
}
BINDABLE_STATUSES = {"AVAILABLE", "BOUND", "UPDATE_AVAILABLE", "SOURCE_UNAVAILABLE"}
FAIL_CLOSED_STATUSES = {
    "HASH_MISMATCH",
    "VERSION_MISMATCH",
    "REJECTED",
    "REVOKED",
}
SOURCE_TYPES = {"builtin", "system", "local", "catalog", "repository", "github"}
PINNING_MODES = {"PINNED", "FLOATING"}
SKILL_ROLES = {"PRIMARY", "SUPPORTING"}
SCOPE_KEYS = ("agent_ids", "workstreams", "thread_record_ids", "task_ids")
PERMISSION_KEYS = (
    "network",
    "filesystem",
    "secrets",
    "shell",
    "dependencies",
)
PROTECTED_CORE_SKILL_IDS = frozenset({"founder-os", "skill-curator"})
GLOBAL_SKILLS_ROOT = Path(__file__).resolve().parent.parent.parent
PROTECTED_CORE_SKILL_ROOTS = tuple(
    (GLOBAL_SKILLS_ROOT / skill_id).resolve(strict=False)
    for skill_id in sorted(PROTECTED_CORE_SKILL_IDS)
)
RUNTIME_VISIBILITY_STATES = {"CONFIRMED", "NOT_CONFIRMED"}
INSTALLED_HASH_ALGORITHM = "sha256-canonical-tree-v1"
INSTALLED_MAX_FILES = 2_000
INSTALLED_MAX_DIRECTORIES = 2_000
INSTALLED_MAX_TOTAL_ENTRIES = 4_000
INSTALLED_MAX_TOTAL_BYTES = 50 * 1024 * 1024
INSTALLED_MAX_FILE_BYTES = 5 * 1024 * 1024
INSTALLED_MAX_DEPTH = 32
REPARSE_ATTRIBUTE = 0x400
RUNTIME_VISIBILITY_SENTINELS = frozenset(
    {
        "absent",
        "missing",
        "none",
        "no evidence",
        "no runtime",
        "not found",
        "not applicable",
        "not confirmed",
        "not observed",
        "not provided",
        "not verified",
        "null",
        "pending",
        "placeholder",
        "tbd",
        "todo",
        "unknown",
        "unavailable",
        "unconfirmed",
        "unsupported",
        "unset",
        "unverified",
        "unseen",
    }
)


class _UniqueKeySafeLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(
    loader: _UniqueKeySafeLoader,
    node: yaml.nodes.MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key {key!r}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeySafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)
WINDOWS_RESERVED_BASENAMES = {
    "aux",
    "clock$",
    "con",
    "nul",
    "prn",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}


class SkillRegistryPartialCommit(guard.PartialCommit):
    """A two-file Skill Registry transaction needs explicit recovery."""


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


def _sha256(value: Any, label: str) -> str:
    value = _text(value, label).upper()
    if not re.fullmatch(r"[0-9A-F]{64}", value):
        raise guard.InvalidState(f"{label} must be a SHA-256 value")
    return value


def _sha_or_absent(value: Any, label: str) -> str:
    value = _text(value, label).upper()
    if value != "ABSENT" and not re.fullmatch(r"[0-9A-F]{64}", value):
        raise guard.InvalidState(f"{label} must be ABSENT or a SHA-256 value")
    return value


def _commit_sha(value: Any, label: str = "source commit_sha") -> str:
    value = _text(value, label).lower()
    if not re.fullmatch(r"[0-9a-f]{40}", value):
        raise guard.InvalidState(f"{label} must be an exact 40-hex commit SHA")
    return value


def _optional_commit_sha(value: Any, *, required: bool) -> str | None:
    if value is None:
        if required:
            raise guard.InvalidState("Git-backed source requires an exact 40-hex commit SHA")
        return None
    return _commit_sha(value)


def _string_list(
    value: Any,
    label: str,
    *,
    identifiers: bool = False,
    max_items: int = 128,
) -> list[str]:
    if not isinstance(value, list) or len(value) > max_items:
        raise guard.InvalidState(f"{label} must be a list with at most {max_items} items")
    result = [
        _identifier(item, f"{label} item", max_length=256)
        if identifiers
        else _text(item, f"{label} item", max_length=512)
        for item in value
    ]
    if len(result) != len(set(item.casefold() for item in result)):
        raise guard.InvalidState(f"{label} contains duplicates")
    return result


def _direct_file(path: Path, label: str) -> None:
    if not path.exists():
        raise guard.InvalidState(f"{label} does not exist: {path}")
    metadata = path.lstat()
    if (
        guard._is_reparse_or_link(path)
        or not path.is_file()
        or metadata.st_nlink != 1
    ):
        raise guard.InvalidState(f"{label} must be a direct single-link file: {path}")


def _project_binding_id(root: Path) -> str:
    normalized = os.path.normcase(str(root)).replace("\\", "/")
    material = f"founder-os-skill-binding-v1\0{normalized}".encode("utf-8")
    return hashlib.sha256(material).hexdigest().upper()


def _absolute_recorded_path(value: Any, label: str) -> str:
    text = _text(value, label, max_length=1024)
    path = Path(text)
    if not path.is_absolute() or str(path) != str(Path(os.path.abspath(text))):
        raise guard.InvalidState(f"{label} must be a normalized absolute path")
    # Existing paths must not redirect validation through a link/reparse point.
    if path.exists():
        if guard._is_reparse_or_link(path):
            raise guard.InvalidState(f"{label} may not be a link or reparse point")
        resolved = path.resolve(strict=True)
        if os.path.normcase(str(resolved)) != os.path.normcase(str(path)):
            raise guard.InvalidState(f"{label} resolves through an unexpected target")
    return str(path)


def _normalize_runtime_visibility(
    value: Any,
    *,
    required: bool,
) -> dict[str, str] | None:
    """Normalize explicit runtime discovery evidence without inferring it.

    The field is optional for historical/non-bindable records.  Bindable
    records must carry a positive observation so installation or validation
    can never be mistaken for current-runtime visibility.
    """

    if value is None:
        if required:
            raise guard.InvalidState(
                "Bindable Skill requires explicit CONFIRMED runtime_visibility evidence"
            )
        return None
    if not isinstance(value, dict) or set(value) != {
        "state",
        "runtime",
        "evidence_ref",
        "observed_at",
    }:
        raise guard.InvalidState(
            "runtime_visibility requires state/runtime/evidence_ref/observed_at"
        )
    state_value = value.get("state")
    if not isinstance(state_value, str) or state_value not in RUNTIME_VISIBILITY_STATES:
        raise guard.InvalidState("runtime_visibility.state is invalid")
    normalized = {
        "state": state_value,
        "runtime": _text(value.get("runtime"), "runtime_visibility.runtime", max_length=256),
        "evidence_ref": _text(
            value.get("evidence_ref"),
            "runtime_visibility.evidence_ref",
            max_length=1024,
        ),
        "observed_at": _text(
            value.get("observed_at"),
            "runtime_visibility.observed_at",
            max_length=128,
        ),
    }
    if required and normalized["state"] != "CONFIRMED":
        raise guard.InvalidState(
            "Bindable Skill runtime_visibility must be CONFIRMED"
        )
    if normalized["state"] == "CONFIRMED":
        for key in ("runtime", "evidence_ref"):
            semantic_value = re.sub(
                r"[\s_-]+", " ", normalized[key].strip().casefold()
            )
            if (
                semantic_value in RUNTIME_VISIBILITY_SENTINELS
                or semantic_value in {"n/a", "na"}
                or re.fullmatch(r"[<\[{].*[>\]}]", normalized[key].strip())
                or re.fullmatch(r"[-—?]+", normalized[key].strip())
            ):
                raise guard.InvalidState(
                    f"CONFIRMED runtime_visibility.{key} may not be a sentinel or placeholder"
                )
        observed_at = normalized["observed_at"]
        try:
            parsed = datetime.fromisoformat(
                observed_at[:-1] + "+00:00" if observed_at.endswith("Z") else observed_at
            )
        except ValueError as exc:
            raise guard.InvalidState(
                "CONFIRMED runtime_visibility.observed_at must be timezone-aware ISO-8601"
            ) from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise guard.InvalidState(
                "CONFIRMED runtime_visibility.observed_at must be timezone-aware ISO-8601"
            )
    return normalized


def _is_reparse(metadata: os.stat_result) -> bool:
    return bool(getattr(metadata, "st_file_attributes", 0) & REPARSE_ATTRIBUTE)


_WIN_FILE_READ_ATTRIBUTES = 0x0080
_WIN_GENERIC_READ = 0x80000000
_WIN_FILE_SHARE_READ = 0x00000001
_WIN_OPEN_EXISTING = 3
_WIN_FILE_ATTRIBUTE_DIRECTORY = 0x00000010
_WIN_FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
_WIN_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_WIN_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000


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
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    _KERNEL32.CreateFileW.restype = wintypes.HANDLE
    _KERNEL32.GetFileInformationByHandle.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(_WinHandleInformation),
    )
    _KERNEL32.GetFileInformationByHandle.restype = wintypes.BOOL
    _KERNEL32.CloseHandle.argtypes = (wintypes.HANDLE,)
    _KERNEL32.CloseHandle.restype = wintypes.BOOL
    _INVALID_WIN_HANDLE = ctypes.c_void_p(-1).value


def _metadata_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_nlink,
        metadata.st_mode,
        metadata.st_size,
        getattr(metadata, "st_mtime_ns", int(metadata.st_mtime * 1_000_000_000)),
        getattr(metadata, "st_ctime_ns", int(metadata.st_ctime * 1_000_000_000)),
    )


def _plain_installed_lstat(
    path: Path,
    *,
    directory: bool,
    skill_id: str,
) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise _hash_mismatch(skill_id, f"installed path identity changed: {path}: {exc}") from exc
    expected_type = stat.S_ISDIR if directory else stat.S_ISREG
    if (
        stat.S_ISLNK(metadata.st_mode)
        or _is_reparse(metadata)
        or not expected_type(metadata.st_mode)
    ):
        raise _hash_mismatch(
            skill_id,
            f"expected a direct plain {'directory' if directory else 'file'}: {path}",
        )
    if not directory and metadata.st_nlink != 1:
        raise _hash_mismatch(skill_id, f"hardlinked installed file is forbidden: {path}")
    return metadata


def _win_handle_information(handle: int, *, skill_id: str) -> "_WinHandleInformation":
    information = _WinHandleInformation()
    if not _KERNEL32.GetFileInformationByHandle(handle, ctypes.byref(information)):
        error = ctypes.get_last_error()
        raise _hash_mismatch(
            skill_id,
            f"cannot query pinned installed path: {ctypes.WinError(error)}",
        )
    return information


def _win_handle_identity(information: "_WinHandleInformation") -> tuple[int, ...]:
    return (
        int(information.volume_serial),
        (int(information.file_index_high) << 32) | int(information.file_index_low),
        int(information.attributes),
        (int(information.size_high) << 32) | int(information.size_low),
        (int(information.last_write_time.high) << 32)
        | int(information.last_write_time.low),
    )


class _InstalledPathPin:
    """An open no-delete/no-write-sharing handle to one lexical tree node."""

    def __init__(
        self,
        path: Path,
        handle: int,
        identity: tuple[int, ...],
        *,
        windows: bool,
        directory: bool,
    ):
        self.path = path
        self.handle = handle
        self.identity = identity
        self.windows = windows
        self.directory = directory

    def close(self) -> None:
        if self.handle < 0:
            return
        if self.windows:
            _KERNEL32.CloseHandle(self.handle)
        else:
            os.close(self.handle)
        self.handle = -1


def _open_installed_path_pin(
    path: Path,
    *,
    directory: bool,
    skill_id: str,
    expected_metadata: os.stat_result | None = None,
) -> _InstalledPathPin:
    before = _plain_installed_lstat(
        path,
        directory=directory,
        skill_id=skill_id,
    )
    if (
        expected_metadata is not None
        and _metadata_identity(before) != _metadata_identity(expected_metadata)
    ):
        raise _hash_mismatch(skill_id, f"installed path was replaced before pinning: {path}")
    if os.name == "nt":
        flags = _WIN_FILE_FLAG_OPEN_REPARSE_POINT
        if directory:
            flags |= _WIN_FILE_FLAG_BACKUP_SEMANTICS
        handle = _KERNEL32.CreateFileW(
            str(path),
            # GENERIC_READ, not merely FILE_READ_ATTRIBUTES: on current
            # Windows/Python a metadata-only handle can still permit a POSIX
            # rename. GENERIC_READ with no WRITE/DELETE sharing provides the
            # native no-rename/no-reparse-replacement fence required here.
            _WIN_GENERIC_READ,
            # Deliberately omit FILE_SHARE_WRITE and FILE_SHARE_DELETE.  A
            # successful pin proves no conflicting writer was already open
            # and prevents rename/reparse replacement for the pin lifetime.
            _WIN_FILE_SHARE_READ,
            None,
            _WIN_OPEN_EXISTING,
            flags,
            None,
        )
        if handle == _INVALID_WIN_HANDLE:
            error = ctypes.get_last_error()
            raise _hash_mismatch(
                skill_id,
                f"cannot pin installed path {path}: {ctypes.WinError(error)}",
            )
        handle = int(handle)
        try:
            information = _win_handle_information(handle, skill_id=skill_id)
            attributes = int(information.attributes)
            if bool(attributes & _WIN_FILE_ATTRIBUTE_DIRECTORY) != directory:
                raise _hash_mismatch(skill_id, f"installed path type changed: {path}")
            if attributes & _WIN_FILE_ATTRIBUTE_REPARSE_POINT:
                raise _hash_mismatch(skill_id, f"installed path became a reparse point: {path}")
            after = _plain_installed_lstat(
                path,
                directory=directory,
                skill_id=skill_id,
            )
            if _metadata_identity(before) != _metadata_identity(after):
                raise _hash_mismatch(skill_id, f"installed path changed while pinning: {path}")
            identity = _win_handle_identity(information)
            if before.st_ino and identity[1] and before.st_ino != identity[1]:
                raise _hash_mismatch(skill_id, f"installed path identity changed: {path}")
            return _InstalledPathPin(
                path,
                handle,
                identity,
                windows=True,
                directory=directory,
            )
        except Exception:
            _KERNEL32.CloseHandle(handle)
            raise

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    if directory:
        flags |= getattr(os, "O_DIRECTORY", 0)
    try:
        handle = os.open(path, flags)
    except OSError as exc:
        raise _hash_mismatch(skill_id, f"cannot pin installed path {path}: {exc}") from exc
    try:
        opened = os.fstat(handle)
        identity = _metadata_identity(opened)
        if identity != _metadata_identity(before):
            raise _hash_mismatch(skill_id, f"installed path changed while pinning: {path}")
        return _InstalledPathPin(
            path,
            handle,
            identity,
            windows=False,
            directory=directory,
        )
    except Exception:
        os.close(handle)
        raise


def _assert_installed_path_pin(pin: _InstalledPathPin, *, skill_id: str) -> None:
    metadata = _plain_installed_lstat(
        pin.path,
        directory=pin.directory,
        skill_id=skill_id,
    )
    if pin.windows:
        information = _win_handle_information(pin.handle, skill_id=skill_id)
        identity = _win_handle_identity(information)
        if identity != pin.identity or (
            metadata.st_ino and identity[1] and metadata.st_ino != identity[1]
        ):
            raise _hash_mismatch(skill_id, f"pinned installed path changed: {pin.path}")
    elif (
        _metadata_identity(metadata) != pin.identity
        or _metadata_identity(os.fstat(pin.handle)) != pin.identity
    ):
        raise _hash_mismatch(skill_id, f"pinned installed path changed: {pin.path}")


class _InstalledTreeFence:
    """Pin every lexical ancestor and visited node for one complete rehash."""

    def __init__(self, root: Path, skill_id: str, pins: list[_InstalledPathPin]):
        self.root = root
        self.skill_id = skill_id
        self.pins = pins
        self.by_path = {os.path.normcase(str(pin.path)): pin for pin in pins}

    @classmethod
    def acquire(
        cls,
        root: Path,
        *,
        skill_id: str,
        expected_root_metadata: os.stat_result,
    ) -> "_InstalledTreeFence":
        lexical = Path(os.path.abspath(str(root)))
        if not root.is_absolute() or os.path.normcase(str(root)) != os.path.normcase(str(lexical)):
            raise _hash_mismatch(skill_id, "installed_path must remain normalized absolute")
        if _link_or_reparse_ancestor(lexical) is not None:
            raise _hash_mismatch(skill_id, "installed_path redirected after preflight")
        pins: list[_InstalledPathPin] = []
        try:
            for component in reversed((lexical, *lexical.parents)):
                pins.append(
                    _open_installed_path_pin(
                        component,
                        directory=True,
                        skill_id=skill_id,
                        expected_metadata=(
                            expected_root_metadata if component == lexical else None
                        ),
                    )
                )
            fence = cls(lexical, skill_id, pins)
            fence.assert_current()
            anchor = lexical / "SKILL.md"
            try:
                _plain_installed_lstat(anchor, directory=False, skill_id=skill_id)
            except guard.Conflict:
                fence.assert_current()
                candidates: list[Path] = []
                try:
                    with os.scandir(lexical) as iterator:
                        for entry in iterator:
                            path = lexical / entry.name
                            try:
                                metadata = path.lstat()
                            except OSError:
                                continue
                            if (
                                not stat.S_ISLNK(metadata.st_mode)
                                and not _is_reparse(metadata)
                                and stat.S_ISREG(metadata.st_mode)
                                and metadata.st_nlink == 1
                            ):
                                candidates.append(path)
                except OSError as exc:
                    raise _hash_mismatch(
                        skill_id,
                        f"installed root anchor unavailable: {lexical}: {exc}",
                    ) from exc
                fence.assert_current()
                if not candidates:
                    raise _hash_mismatch(
                        skill_id,
                        "installed root has no direct regular anchor file",
                    )
                anchor = sorted(candidates, key=lambda item: item.name.casefold())[0]
            fence.pin_file(anchor)
            fence.assert_current()
            return fence
        except Exception:
            for pin in reversed(pins):
                pin.close()
            raise

    def _remember(self, pin: _InstalledPathPin) -> _InstalledPathPin:
        key = os.path.normcase(str(pin.path))
        existing = self.by_path.get(key)
        if existing is not None:
            pin.close()
            _assert_installed_path_pin(existing, skill_id=self.skill_id)
            return existing
        self.pins.append(pin)
        self.by_path[key] = pin
        return pin

    def pin_directory(
        self,
        path: Path,
        *,
        expected_metadata: os.stat_result | None = None,
    ) -> _InstalledPathPin:
        self.assert_ancestors(path)
        pin = _open_installed_path_pin(
            path,
            directory=True,
            skill_id=self.skill_id,
            expected_metadata=expected_metadata,
        )
        remembered = self._remember(pin)
        self.assert_ancestors(path, include_leaf=True)
        return remembered

    def pin_file(
        self,
        path: Path,
        *,
        expected_metadata: os.stat_result | None = None,
    ) -> _InstalledPathPin:
        self.assert_ancestors(path)
        pin = _open_installed_path_pin(
            path,
            directory=False,
            skill_id=self.skill_id,
            expected_metadata=expected_metadata,
        )
        remembered = self._remember(pin)
        self.assert_ancestors(path)
        return remembered

    def assert_current(self) -> None:
        for pin in self.pins:
            _assert_installed_path_pin(pin, skill_id=self.skill_id)

    def assert_ancestors(self, path: Path, *, include_leaf: bool = False) -> None:
        try:
            relative = path.relative_to(self.root)
        except ValueError as exc:
            raise _hash_mismatch(self.skill_id, f"installed path escaped root: {path}") from exc
        parts = relative.parts if include_leaf else relative.parts[:-1]
        current = self.root
        root_pin = self.by_path.get(os.path.normcase(str(self.root)))
        if root_pin is None:
            raise _hash_mismatch(self.skill_id, "installed root pin is missing")
        _assert_installed_path_pin(root_pin, skill_id=self.skill_id)
        for part in parts:
            current /= part
            metadata = _plain_installed_lstat(
                current,
                directory=True,
                skill_id=self.skill_id,
            )
            try:
                resolved = current.resolve(strict=True)
            except (OSError, RuntimeError) as exc:
                raise _hash_mismatch(
                    self.skill_id,
                    f"installed ancestor changed: {current}: {exc}",
                ) from exc
            if _is_reparse(metadata) or not _is_within(resolved, self.root):
                raise _hash_mismatch(self.skill_id, f"installed ancestor escaped root: {current}")
            pin = self.by_path.get(os.path.normcase(str(current)))
            if pin is not None:
                _assert_installed_path_pin(pin, skill_id=self.skill_id)
        _assert_installed_path_pin(root_pin, skill_id=self.skill_id)

    def close(self) -> None:
        for pin in reversed(self.pins):
            pin.close()
        self.pins = []
        self.by_path = {}


def _link_or_reparse_ancestor(path: Path) -> Path | None:
    lexical = Path(os.path.abspath(str(path)))
    for component in reversed((lexical, *lexical.parents)):
        try:
            metadata = component.lstat()
        except OSError:
            continue
        if stat.S_ISLNK(metadata.st_mode) or _is_reparse(metadata):
            return component
    return None


def _is_within(path: Path, root: Path) -> bool:
    try:
        normalized_path = os.path.normcase(str(path))
        normalized_root = os.path.normcase(str(root))
        return os.path.commonpath((normalized_path, normalized_root)) == normalized_root
    except ValueError:
        return False


def _unsafe_path_part_reason(part: str) -> str | None:
    if part in {"", ".", ".."}:
        return "empty, dot, and parent components are forbidden"
    if any(ord(character) < 32 for character in part):
        return "control characters are forbidden"
    if ":" in part:
        return "colon/alternate-data-stream syntax is forbidden"
    if part.endswith((" ", ".")):
        return "trailing spaces or periods are unsafe on Windows"
    if part.split(".", 1)[0].casefold() in WINDOWS_RESERVED_BASENAMES:
        return "reserved Windows device name"
    return None


def _hash_mismatch(skill_id: str, detail: str) -> guard.Conflict:
    return guard.Conflict(f"HASH_MISMATCH: {skill_id}: {detail}")


def _read_installed_regular_file(
    path: Path,
    expected: os.stat_result,
    *,
    relative: str,
    skill_id: str,
) -> bytes:
    if expected.st_size > INSTALLED_MAX_FILE_BYTES:
        raise _hash_mismatch(
            skill_id,
            f"{relative} exceeds the {INSTALLED_MAX_FILE_BYTES}-byte file limit",
        )
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise _hash_mismatch(skill_id, f"cannot safely open {relative}: {exc}") from exc
    try:
        opened = os.fstat(descriptor)
        expected_identity = (
            expected.st_dev,
            expected.st_ino,
            expected.st_size,
            expected.st_nlink,
        )
        opened_identity = (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_nlink,
        )
        if (
            expected_identity != opened_identity
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
        ):
            raise _hash_mismatch(
                skill_id,
                f"{relative} changed identity while the installed tree was opened",
            )
        chunks: list[bytes] = []
        remaining = opened.st_size
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
        after = os.fstat(descriptor)
        after_identity = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_nlink,
        )
        if len(content) != opened.st_size or after_identity != opened_identity:
            raise _hash_mismatch(
                skill_id,
                f"{relative} changed while the installed tree was read",
            )
        return content
    finally:
        os.close(descriptor)


def _validate_bindable_manifest(raw: bytes, *, skill_id: str) -> None:
    """Validate the semantic identity from bytes read inside the tree fence."""

    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise guard.InvalidState("Bindable Skill SKILL.md must be UTF-8 text") from exc
    boundary = re.match(
        r"\A---[ \t]*\r?\n(?P<body>.*?)\r?\n---[ \t]*(?:\r?\n|\Z)",
        text,
        flags=re.DOTALL,
    )
    if boundary is None:
        raise guard.InvalidState("Bindable Skill SKILL.md has no YAML frontmatter")
    try:
        frontmatter = yaml.load(
            boundary.group("body"),
            Loader=_UniqueKeySafeLoader,
        )
    except yaml.YAMLError as exc:
        raise guard.InvalidState(
            f"Bindable Skill SKILL.md has invalid or duplicate-key YAML: {exc}"
        ) from exc
    if not isinstance(frontmatter, dict):
        raise guard.InvalidState("Bindable Skill frontmatter must be a YAML mapping")
    allowed = {"name", "description", "license", "allowed-tools", "metadata"}
    unexpected = sorted(str(key) for key in frontmatter if key not in allowed)
    if unexpected:
        raise guard.InvalidState(
            f"Bindable Skill frontmatter has unexpected keys: {unexpected}"
        )
    semantic_name = frontmatter.get("name")
    description = frontmatter.get("description")
    if not isinstance(semantic_name, str) or not isinstance(description, str):
        raise guard.InvalidState(
            "Bindable Skill frontmatter requires string name and description"
        )
    if semantic_name.casefold() in PROTECTED_CORE_SKILL_IDS:
        raise guard.InvalidState(
            f"PROTECTED_CORE_SKILL: ordinary project Registry may not bind {semantic_name}"
        )
    if semantic_name != skill_id:
        raise guard.InvalidState(
            "Bindable Skill semantic name must exactly match skill_id "
            f"({semantic_name!r} != {skill_id!r})"
        )


def _secure_installed_tree(
    installed_path: str,
    *,
    skill_id: str,
    require_identity: bool,
) -> str:
    """Read one canonical file tree under a lifetime lexical/native fence."""

    lexical = Path(installed_path)
    try:
        redirect = _link_or_reparse_ancestor(lexical)
        if redirect is not None:
            raise _hash_mismatch(
                skill_id,
                f"installed_path contains a link or reparse point: {redirect}",
            )
        root_metadata = lexical.lstat()
        if (
            stat.S_ISLNK(root_metadata.st_mode)
            or _is_reparse(root_metadata)
            or not stat.S_ISDIR(root_metadata.st_mode)
        ):
            raise _hash_mismatch(skill_id, "installed_path is not a direct directory")
        root = lexical.resolve(strict=True)
    except guard.Conflict:
        raise
    except (OSError, RuntimeError) as exc:
        raise _hash_mismatch(skill_id, f"installed_path is unavailable: {exc}") from exc
    if os.path.normcase(str(root)) != os.path.normcase(str(lexical)):
        raise _hash_mismatch(skill_id, "installed_path resolves through another target")

    fence = _InstalledTreeFence.acquire(
        root,
        skill_id=skill_id,
        expected_root_metadata=root_metadata,
    )
    inventory: list[dict[str, Any]] = []
    manifest_raw: bytes | None = None
    file_count = 0
    directory_count = 0
    total_entry_count = 0
    total_bytes = 0

    def walk(directory: Path) -> None:
        nonlocal file_count, directory_count, total_entry_count, total_bytes
        nonlocal manifest_raw
        fence.assert_ancestors(directory, include_leaf=True)
        try:
            with os.scandir(directory) as scanner:
                entries = list(scanner)
        except OSError as exc:
            raise _hash_mismatch(skill_id, f"cannot enumerate installed tree: {exc}") from exc
        fence.assert_ancestors(directory, include_leaf=True)
        entries.sort(key=lambda item: item.name.casefold())
        for directory_entry in entries:
            total_entry_count += 1
            if total_entry_count > INSTALLED_MAX_TOTAL_ENTRIES:
                raise _hash_mismatch(
                    skill_id,
                    f"installed tree exceeds the {INSTALLED_MAX_TOTAL_ENTRIES}-entry limit",
                )
            path = Path(directory_entry.path)
            try:
                relative_path = path.relative_to(root)
            except ValueError as exc:
                raise _hash_mismatch(skill_id, "installed tree path escaped root") from exc
            relative = relative_path.as_posix()
            if len(relative_path.parts) > INSTALLED_MAX_DEPTH:
                raise _hash_mismatch(
                    skill_id,
                    f"{relative} exceeds the {INSTALLED_MAX_DEPTH}-component depth limit",
                )
            for part in relative_path.parts:
                reason = _unsafe_path_part_reason(part)
                if reason is not None:
                    raise _hash_mismatch(
                        skill_id,
                        f"unsafe path component {part!r} in {relative}: {reason}",
                    )
            try:
                fence.assert_ancestors(path)
                metadata = path.lstat()
                fence.assert_ancestors(path)
            except guard.Conflict:
                raise
            except OSError as exc:
                raise _hash_mismatch(skill_id, f"cannot lstat {relative}: {exc}") from exc
            if (
                directory_entry.is_symlink()
                or stat.S_ISLNK(metadata.st_mode)
                or _is_reparse(metadata)
            ):
                raise _hash_mismatch(skill_id, f"link/reparse point is forbidden: {relative}")
            try:
                fence.assert_ancestors(path)
                resolved = path.resolve(strict=True)
                fence.assert_ancestors(path)
            except guard.Conflict:
                raise
            except (OSError, RuntimeError) as exc:
                raise _hash_mismatch(skill_id, f"cannot resolve {relative}: {exc}") from exc
            if not _is_within(resolved, root):
                raise _hash_mismatch(skill_id, f"path escapes installed root: {relative}")
            if stat.S_ISDIR(metadata.st_mode):
                directory_count += 1
                if directory_count > INSTALLED_MAX_DIRECTORIES:
                    raise _hash_mismatch(
                        skill_id,
                        f"installed tree exceeds the {INSTALLED_MAX_DIRECTORIES}-directory limit",
                    )
                fence.pin_directory(path, expected_metadata=metadata)
                walk(path)
                continue
            if not stat.S_ISREG(metadata.st_mode):
                raise _hash_mismatch(skill_id, f"special file is forbidden: {relative}")
            if metadata.st_nlink != 1:
                raise _hash_mismatch(
                    skill_id,
                    f"hardlinked file is forbidden: {relative} (links={metadata.st_nlink})",
                )
            file_count += 1
            if file_count > INSTALLED_MAX_FILES:
                raise _hash_mismatch(
                    skill_id,
                    f"installed tree exceeds the {INSTALLED_MAX_FILES}-file limit",
                )
            total_bytes += metadata.st_size
            if total_bytes > INSTALLED_MAX_TOTAL_BYTES:
                raise _hash_mismatch(
                    skill_id,
                    f"installed tree exceeds the {INSTALLED_MAX_TOTAL_BYTES}-byte limit",
                )
            fence.pin_file(path, expected_metadata=metadata)
            fence.assert_ancestors(path)
            content = _read_installed_regular_file(
                path,
                metadata,
                relative=relative,
                skill_id=skill_id,
            )
            fence.assert_ancestors(path)
            if relative == "SKILL.md":
                manifest_raw = content
            inventory.append(
                {
                    "path": relative,
                    "kind": "file",
                    "size": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
            )

    try:
        if require_identity:
            for protected_root in PROTECTED_CORE_SKILL_ROOTS:
                if _is_within(root, protected_root):
                    raise guard.InvalidState(
                        "PROTECTED_CORE_PATH: ordinary project Registry may not alias or "
                        f"bind content from {protected_root}"
                    )
        walk(root)
        fence.assert_current()
        if require_identity:
            if manifest_raw is None:
                raise guard.InvalidState(
                    f"Bindable Skill requires a direct SKILL.md: {root / 'SKILL.md'}"
                )
            _validate_bindable_manifest(manifest_raw, skill_id=skill_id)
            fence.assert_current()
        inventory.sort(key=lambda row: row["path"].casefold())
        encoded = json.dumps(
            inventory,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest().upper()
    finally:
        fence.close()


def installed_tree_hash(installed_path: str, *, skill_id: str) -> str:
    """Hash an installed Skill exactly as Curator's canonical tree v1 does.

    Only names, sizes and file bytes are read. Candidate content is never
    imported, parsed, executed, or granted network/dependency access.
    """

    return _secure_installed_tree(
        installed_path,
        skill_id=skill_id,
        require_identity=False,
    )


def _assert_bindable_installed_identity(
    installed_path: str,
    *,
    skill_id: str,
) -> None:
    """Validate semantic identity inside a complete zero-outside-read fence."""

    _secure_installed_tree(
        installed_path,
        skill_id=skill_id,
        require_identity=True,
    )


def _assert_bindable_entry_installed_hash(entry: dict[str, Any]) -> None:
    """Revalidate bindable bytes at each mutation preflight and commit check."""

    if entry["status"] not in BINDABLE_STATUSES:
        return
    observed = _secure_installed_tree(
        entry["installed_path"],
        skill_id=entry["skill_id"],
        require_identity=True,
    )
    if observed not in {entry["content_hash"], entry["installed_hash"]}:
        raise guard.Conflict(
            "HASH_MISMATCH: observed installed tree differs from proposed locked "
            f"content for {entry['skill_id']} ({INSTALLED_HASH_ALGORITHM})"
        )


def _normalize_scope(value: Any) -> dict[str, list[str]]:
    if not isinstance(value, dict) or set(value) != set(SCOPE_KEYS):
        raise guard.InvalidState(
            "scoped_bindings must contain agent_ids/workstreams/thread_record_ids/task_ids"
        )
    return {
        key: _string_list(value[key], f"scoped_bindings.{key}", identifiers=True)
        for key in SCOPE_KEYS
    }


def _normalize_permissions(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != set(PERMISSION_KEYS):
        raise guard.InvalidState(
            "permissions must contain network/filesystem/secrets/shell/dependencies"
        )
    normalized: dict[str, Any] = {}
    for key in ("network", "filesystem", "secrets", "shell"):
        if not isinstance(value[key], bool):
            raise guard.InvalidState(f"permissions.{key} must be boolean")
        normalized[key] = value[key]
    normalized["dependencies"] = _string_list(
        value["dependencies"], "permissions.dependencies"
    )
    return normalized


def normalize_entry(value: Any) -> dict[str, Any]:
    """Validate one explicit Curator decision without making a trust decision."""

    if not isinstance(value, dict):
        raise guard.InvalidState("Skill entry must be a JSON object")
    required = {
        "skill_id",
        "display_name",
        "capabilities",
        "source",
        "installed_path",
        "content_hash",
        "installed_hash",
        "audit_revision",
        "approved_version",
        "trust_level",
        "risk_level",
        "approval",
        "installation_timestamp",
        "last_verification",
        "status",
        "pinning_mode",
        "role",
        "scoped_bindings",
        "permissions",
        "scripts_present",
    }
    optional = {
        "dependencies",
        "deprecation_status",
        "notes",
        "entry_revision",
        "runtime_visibility",
    }
    unknown = set(value).difference(required | optional)
    missing = required.difference(value)
    if missing or unknown:
        raise guard.InvalidState(
            f"Skill entry fields mismatch; missing={sorted(missing)}, unknown={sorted(unknown)}"
        )
    skill_id = _identifier(value["skill_id"], "skill_id")
    if skill_id.casefold() in PROTECTED_CORE_SKILL_IDS:
        raise guard.InvalidState(
            f"PROTECTED_CORE_SKILL: ordinary project Registry may not register {skill_id}"
        )
    capabilities = _string_list(
        value["capabilities"], "capabilities", identifiers=True
    )
    if not capabilities:
        raise guard.InvalidState("Skill entry requires at least one capability")
    source = value["source"]
    if not isinstance(source, dict) or set(source) != {
        "source_type",
        "exact_source",
        "repo",
        "path",
        "ref",
        "commit_sha",
    }:
        raise guard.InvalidState(
            "source requires exact source_type/exact_source/repo/path/ref/commit_sha"
        )
    source_type = source.get("source_type")
    if source_type not in SOURCE_TYPES:
        raise guard.InvalidState("Unknown Skill source_type")
    git_backed = source_type in {"repository", "github"} or (
        source_type == "catalog" and source.get("repo") not in {None, "", "NONE"}
    )
    normalized_ref = _text(source.get("ref"), "source ref")
    if git_backed:
        normalized_ref = normalized_ref.lower()
    normalized_source = {
        "source_type": source_type,
        "exact_source": _text(source.get("exact_source"), "source exact_source"),
        "repo": _text(source.get("repo"), "source repo"),
        "path": _text(source.get("path"), "source path"),
        "ref": normalized_ref,
        "commit_sha": _optional_commit_sha(source.get("commit_sha"), required=git_backed),
    }
    if git_backed and normalized_source["ref"] != normalized_source["commit_sha"]:
        raise guard.InvalidState(
            "Git-backed source ref must equal its exact normalized 40-hex commit SHA"
        )
    trust = value["trust_level"]
    risk = value["risk_level"]
    status = value["status"]
    pinning = value["pinning_mode"]
    role = value["role"]
    if trust not in TRUST_LEVELS:
        raise guard.InvalidState("Unknown Skill trust_level")
    if risk not in RISK_LEVELS:
        raise guard.InvalidState("Unknown Skill risk_level")
    if status not in SKILL_STATUSES:
        raise guard.InvalidState("Unknown Skill status")
    if pinning not in PINNING_MODES:
        raise guard.InvalidState("Unknown Skill pinning_mode")
    if role not in SKILL_ROLES:
        raise guard.InvalidState("Skill role must be PRIMARY or SUPPORTING")
    if not isinstance(value["scripts_present"], bool):
        raise guard.InvalidState("scripts_present must be boolean")
    runtime_visibility = _normalize_runtime_visibility(
        value.get("runtime_visibility"),
        required=status in BINDABLE_STATUSES,
    )
    approval = value["approval"]
    if not isinstance(approval, dict) or set(approval) != {
        "mode",
        "evidence_ref",
    }:
        raise guard.InvalidState("approval requires mode and evidence_ref")
    if approval.get("mode") not in APPROVAL_MODES:
        raise guard.InvalidState("Unknown Skill approval mode")
    normalized_approval = {
        "mode": approval["mode"],
        "evidence_ref": _text(approval.get("evidence_ref"), "approval evidence_ref"),
    }
    approval_evidence_semantic = re.sub(
        r"[\s_-]+", " ", normalized_approval["evidence_ref"].strip().casefold()
    )
    if (
        approval_evidence_semantic in APPROVAL_SENTINELS
        or approval_evidence_semantic in {"n/a", "na"}
        or re.fullmatch(r"[<\[{].*[>\]}]", normalized_approval["evidence_ref"].strip())
        or re.fullmatch(r"[-—?]+", normalized_approval["evidence_ref"].strip())
    ):
        raise guard.InvalidState("Skill approval evidence_ref may not be a sentinel or placeholder")
    content_hash = _sha256(value["content_hash"], "content_hash")
    installed_hash = _sha256(value["installed_hash"], "installed_hash")
    if status in BINDABLE_STATUSES:
        if pinning != "PINNED":
            raise guard.InvalidState("A floating Skill cannot be bindable")
        if trust not in BINDABLE_TRUST_LEVELS:
            raise guard.InvalidState("Bindable Skill requires explicit bindable trust")
        required_approval_modes = {
            "LOW": {"AUTO", "FOUNDER", "EXPLICIT"},
            "MEDIUM": {"FOUNDER", "EXPLICIT"},
            "HIGH": {"EXPLICIT"},
            "BLOCKED": set(),
        }
        if approval["mode"] not in required_approval_modes[risk]:
            raise guard.InvalidState(
                f"Bindable {risk} Skill lacks the required approval mode"
            )
        if content_hash != installed_hash:
            raise guard.InvalidState("Bindable Skill content and installed hashes mismatch")
        # ``normalized_source.ref == commit_sha`` above is deliberately
        # stronger than a deny-list of familiar floating branch names.  It
        # also covers repo-backed catalog records and unknown branch aliases.
    if status in {"REJECTED", "REVOKED"} and trust not in {
        "rejected",
        "third-party-unreviewed",
        "third-party-audited",
        "local-reviewed",
        "builtin-or-system",
    }:
        raise guard.InvalidState("Invalid fail-closed trust state")
    installed_path = _absolute_recorded_path(value["installed_path"], "installed_path")
    if status in BINDABLE_STATUSES and not Path(installed_path).exists():
        raise guard.InvalidState("A bindable Skill installed_path must exist")
    result = {
        "skill_id": skill_id,
        "display_name": _text(value["display_name"], "display_name", max_length=256),
        "capabilities": capabilities,
        "source": normalized_source,
        "installed_path": installed_path,
        "content_hash": content_hash,
        "installed_hash": installed_hash,
        "audit_revision": _identifier(value["audit_revision"], "audit_revision"),
        "approved_version": _text(value["approved_version"], "approved_version"),
        "trust_level": trust,
        "risk_level": risk,
        "approval": normalized_approval,
        "installation_timestamp": _text(
            value["installation_timestamp"], "installation_timestamp"
        ),
        "last_verification": _text(value["last_verification"], "last_verification"),
        "status": status,
        "pinning_mode": pinning,
        "role": role,
        "scoped_bindings": _normalize_scope(value["scoped_bindings"]),
        "permissions": _normalize_permissions(value["permissions"]),
        "scripts_present": value["scripts_present"],
        "dependencies": _string_list(
            value.get("dependencies", []), "dependencies", identifiers=True
        ),
        "deprecation_status": _optional_text(
            value.get("deprecation_status"), "deprecation_status"
        ),
        "notes": _optional_text(value.get("notes"), "notes", max_length=2048),
        "entry_revision": _identifier(
            value.get("entry_revision") or guard.new_revision("SKE"),
            "entry_revision",
        ),
    }
    if runtime_visibility is not None:
        result["runtime_visibility"] = runtime_visibility
    return result


def _entry_binding_material(entry: dict[str, Any]) -> dict[str, Any]:
    """Return only fields that can change Thread binding authority."""

    return {
        "skill_id": entry["skill_id"],
        "capabilities": entry["capabilities"],
        "source": entry["source"],
        "approved_version": entry["approved_version"],
        "content_hash": entry["content_hash"],
        "installed_hash": entry["installed_hash"],
        "audit_revision": entry["audit_revision"],
        "trust_level": entry["trust_level"],
        "risk_level": entry["risk_level"],
        "approval": entry["approval"],
        "status": entry["status"],
        "pinning_mode": entry["pinning_mode"],
        "role": entry["role"],
        "scoped_bindings": entry["scoped_bindings"],
        "permissions": entry["permissions"],
        "scripts_present": entry["scripts_present"],
        "runtime_visibility": copy.deepcopy(entry.get("runtime_visibility")),
        "entry_revision": entry["entry_revision"],
    }


def skill_entry_binding_sha(entry: dict[str, Any]) -> str:
    return guard.sha256_bytes(guard.canonical_json_bytes(_entry_binding_material(entry)))


def selected_bindings_sha(entries: list[dict[str, Any]]) -> str:
    material = [
        _entry_binding_material(entry)
        for entry in sorted(entries, key=lambda item: item["skill_id"].casefold())
    ]
    return guard.sha256_bytes(guard.canonical_json_bytes(material))


def validate_skill_lock(lock: dict[str, Any], root: Path) -> None:
    expected_top_level = {
        "schema_version",
        "skill_lock_revision",
        "skill_registry_revision",
        "previous_skill_lock_sha256",
        "registry_projection_sha256",
        "created_at",
        "updated_at",
        "project_binding",
        "skills",
        "history",
    }
    if set(lock) != expected_top_level:
        raise guard.InvalidState("Skill Lock top-level fields are not canonical")
    if lock.get("schema_version") != SCHEMA_VERSION:
        raise guard.InvalidState("Unsupported or missing Skill Lock schema_version")
    _identifier(lock.get("skill_lock_revision"), "skill_lock_revision")
    _identifier(lock.get("skill_registry_revision"), "skill_registry_revision")
    _sha_or_absent(lock.get("previous_skill_lock_sha256"), "previous_skill_lock_sha256")
    _sha256(lock.get("registry_projection_sha256"), "registry_projection_sha256")
    _text(lock.get("created_at"), "Skill Lock created_at")
    _text(lock.get("updated_at"), "Skill Lock updated_at")
    binding = lock.get("project_binding")
    if not isinstance(binding, dict) or set(binding) != {
        "project_root",
        "project_binding_id",
    }:
        raise guard.InvalidState("Skill Lock project_binding is malformed")
    stored_root = binding.get("project_root")
    if not isinstance(stored_root, str) or not Path(stored_root).is_absolute():
        raise guard.InvalidState("Skill Lock project_root must be absolute")
    try:
        resolved = Path(stored_root).resolve(strict=True)
    except OSError as exc:
        raise guard.InvalidState("Skill Lock project_root cannot be resolved") from exc
    if (
        os.path.normcase(str(resolved)) != os.path.normcase(str(root))
        or os.path.normcase(str(Path(stored_root))) != os.path.normcase(str(resolved))
    ):
        raise guard.InvalidState("Skill Lock belongs to a different project")
    if binding.get("project_binding_id") != _project_binding_id(root):
        raise guard.InvalidState("Skill Lock project_binding_id does not match")
    skills = lock.get("skills")
    if not isinstance(skills, dict):
        raise guard.InvalidState("Skill Lock skills must be an object")
    normalized_ids: set[str] = set()
    for key, value in skills.items():
        skill_id = _identifier(key, "Skill Lock key")
        normalized = normalize_entry(value)
        if normalized != value:
            raise guard.InvalidState(f"Skill entry is not canonical: {skill_id}")
        if normalized["skill_id"] != skill_id:
            raise guard.InvalidState("Skill Lock key and skill_id disagree")
        folded = skill_id.casefold()
        if folded in normalized_ids:
            raise guard.InvalidState("Skill IDs are not case-insensitively unique")
        normalized_ids.add(folded)
    primary_by_capability: dict[str, str] = {}
    for skill_id, entry in skills.items():
        if entry["role"] != "PRIMARY" or entry["status"] not in BINDABLE_STATUSES:
            continue
        for capability in entry["capabilities"]:
            other = primary_by_capability.get(capability)
            if other is not None:
                raise guard.InvalidState(
                    f"Capability {capability} has multiple bindable PRIMARY Skills: {other}, {skill_id}"
                )
            primary_by_capability[capability] = skill_id
    history = lock.get("history")
    if not isinstance(history, list) or len(history) > 2048:
        raise guard.InvalidState("Skill Lock history must be a bounded list")
    history_ids: set[str] = set()
    for row in history:
        if not isinstance(row, dict) or set(row) != {
            "history_id",
            "skill_id",
            "prior_entry",
            "prior_skill_lock_revision",
            "replaced_at",
            "change_ref",
        }:
            raise guard.InvalidState("Skill Lock history row is malformed")
        history_id = _identifier(row.get("history_id"), "Skill history_id")
        if history_id in history_ids:
            raise guard.InvalidState("Skill Lock history IDs must be unique")
        history_ids.add(history_id)
        skill_id = _identifier(row.get("skill_id"), "Skill history skill_id")
        prior = normalize_entry(row.get("prior_entry"))
        if prior != row["prior_entry"] or prior["skill_id"] != skill_id:
            raise guard.InvalidState("Skill history prior entry is not canonical")
        _identifier(
            row.get("prior_skill_lock_revision"),
            "Skill history prior_skill_lock_revision",
        )
        _text(row.get("replaced_at"), "Skill history replaced_at")
        _text(row.get("change_ref"), "Skill history change_ref")


def _read_lock(path: Path) -> tuple[str, bytes | None, dict[str, Any] | None]:
    if not path.exists():
        return "ABSENT", None, None
    _direct_file(path, "Skill Lock")
    raw, value = guard.read_json_object(path)
    return guard.sha256_bytes(raw), raw, value


def _read_registry(path: Path) -> tuple[bytes, str]:
    _direct_file(path, "Skill Registry projection")
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise guard.InvalidState(f"Cannot read Skill Registry projection: {exc}") from exc
    return raw, text


def _markdown_field(text: str, label: str) -> str:
    match = re.search(rf"(?m)^- {re.escape(label)}:\s*(\S+)\s*$", text)
    if match is None:
        raise guard.InvalidState(f"SKILLS.md is missing {label}")
    return match.group(1)


def _escape_markdown(value: Any) -> str:
    if isinstance(value, list):
        value = ", ".join(str(item) for item in value) if value else "—"
    elif value is None:
        value = "—"
    text = str(value).replace("\\", "\\\\").replace("|", "\\|")
    return text.replace("\r", " ").replace("\n", " ")


def render_registry(lock: dict[str, Any]) -> bytes:
    lines = [
        "# FounderOS Project Skill Registry",
        "",
        f"- Skill registry revision: {lock['skill_registry_revision']}",
        f"- Skill lock revision: {lock['skill_lock_revision']}",
        "- Machine binding authority: `.founder/SKILL_LOCK.json`",
        "",
        "This file is a generated human-readable projection. Do not use it as binding authority.",
        "",
        "| Skill ID | Display name | Capabilities | Source type | Exact source | Repo | Path | Ref | Commit | Installed path | Content hash | Installed hash | Approved version | Trust | Risk | Approval | Status | Pinning | Role | Allowed agents | Allowed workstreams | Allowed threads | Allowed tasks | Current users | Runtime visibility | Runtime | Runtime evidence | Runtime observed | Dependencies | Network | Filesystem | Secrets | Scripts present | Audit revision | Installed at | Last verified | Deprecation |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for skill_id in sorted(lock["skills"], key=str.casefold):
        entry = lock["skills"][skill_id]
        source = entry["source"]
        scope = entry["scoped_bindings"]
        runtime_visibility = entry.get("runtime_visibility")
        cells = [
            skill_id,
            entry["display_name"],
            entry["capabilities"],
            source["source_type"],
            source["exact_source"],
            source["repo"],
            source["path"],
            source["ref"],
            source["commit_sha"],
            entry["installed_path"],
            entry["content_hash"],
            entry["installed_hash"],
            entry["approved_version"],
            entry["trust_level"],
            entry["risk_level"],
            entry["approval"]["mode"],
            entry["status"],
            entry["pinning_mode"],
            entry["role"],
            scope["agent_ids"],
            scope["workstreams"],
            scope["thread_record_ids"],
            scope["task_ids"],
            "READ_TIME: inspect.actual_current_users",
            (
                runtime_visibility["state"]
                if runtime_visibility is not None
                else "NOT_CONFIRMED"
            ),
            runtime_visibility["runtime"] if runtime_visibility is not None else None,
            (
                runtime_visibility["evidence_ref"]
                if runtime_visibility is not None
                else None
            ),
            (
                runtime_visibility["observed_at"]
                if runtime_visibility is not None
                else None
            ),
            entry["dependencies"],
            entry["permissions"]["network"],
            entry["permissions"]["filesystem"],
            entry["permissions"]["secrets"],
            entry["scripts_present"],
            entry["audit_revision"],
            entry["installation_timestamp"],
            entry["last_verification"],
            entry["deprecation_status"],
        ]
        lines.append("| " + " | ".join(_escape_markdown(item) for item in cells) + " |")
    lines.extend(
        [
            "",
            "Status, risk, trust, approval, installation, and binding are separate facts.",
            "A globally installed Skill is not project-approved or Thread-bound by implication.",
            "Allowed scopes above are authorization ceilings, not actual users. Actual current users are derived read-only from `.founder/THREADS.json` by `skill_registry.py inspect`; missing or non-machine Thread evidence reports `UNKNOWN`, and a valid ledger with no current binding reports `NONE`.",
            "",
        ]
    )
    return "\n".join(lines).encode("utf-8")


def validate_registry_pair(lock: dict[str, Any], root: Path, registry_raw: bytes) -> None:
    validate_skill_lock(lock, root)
    try:
        text = registry_raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise guard.InvalidState("SKILLS.md is not valid UTF-8") from exc
    if _markdown_field(text, "Skill registry revision") != lock["skill_registry_revision"]:
        raise guard.Conflict("SKILLS.md and SKILL_LOCK.json registry revisions drifted")
    if _markdown_field(text, "Skill lock revision") != lock["skill_lock_revision"]:
        raise guard.Conflict("SKILLS.md and SKILL_LOCK.json lock revisions drifted")
    if guard.sha256_bytes(registry_raw) != lock["registry_projection_sha256"]:
        raise guard.Conflict("SKILLS.md content hash drifted from SKILL_LOCK.json")


def read_registry_pair(
    founder: Path,
) -> tuple[str, bytes | None, dict[str, Any] | None, bytes | None]:
    """Read and cross-check the authoritative lock and Markdown projection."""

    lock_sha, lock_raw, lock = _read_lock(founder / LOCK_NAME)
    registry_path = founder / REGISTRY_NAME
    if lock is None:
        if registry_path.exists():
            registry_raw, registry_text = _read_registry(registry_path)
            # V2 legacy projects intentionally used standalone SKILLS.md.  It
            # remains readable/migratable, while a V2.2 generated projection
            # missing its machine authority is a recovery condition.
            if re.search(r"(?m)^- Skill (?:registry|lock) revision:\s*\S+\s*$", registry_text):
                raise guard.Conflict(
                    "Generated SKILLS.md exists without authoritative SKILL_LOCK.json"
                )
            return lock_sha, lock_raw, lock, registry_raw
        return lock_sha, lock_raw, lock, None
    if not registry_path.exists():
        raise guard.Conflict("SKILL_LOCK.json exists without its SKILLS.md projection")
    registry_raw, _text_value = _read_registry(registry_path)
    validate_registry_pair(lock, founder.parent, registry_raw)
    return lock_sha, lock_raw, lock, registry_raw


def _transaction_observation(founder: Path) -> dict[str, Any]:
    path = founder / TRANSACTION_LOCK_NAME
    if not path.exists():
        return {"state": "none"}
    _direct_file(path, "Skill Registry transaction lock")
    _raw, value = guard.read_json_object(path)
    return {
        "state": "recovery-required",
        "owner": _text(value.get("owner"), "transaction owner"),
        "nonce": _text(value.get("nonce"), "transaction nonce"),
        "expected_skill_lock_sha": _sha_or_absent(
            value.get("expected_skill_lock_sha"), "expected_skill_lock_sha"
        ),
        "target_skill_lock_sha": _sha256(
            value.get("target_skill_lock_sha"), "target_skill_lock_sha"
        ),
        "target_registry_sha": _sha256(
            value.get("target_registry_sha"), "target_registry_sha"
        ),
        "created_at": _text(value.get("created_at"), "transaction created_at"),
    }


def _unknown_current_users(
    lock: dict[str, Any] | None,
    reason: str,
) -> dict[str, dict[str, Any]]:
    if lock is None:
        return {}
    return {
        skill_id: {
            "state": "UNKNOWN",
            "users": [],
            "source": ".founder/THREADS.json",
            "reason": reason,
        }
        for skill_id in sorted(lock["skills"], key=str.casefold)
    }


def derive_actual_current_users(
    founder: Path,
    lock: dict[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    """Derive current Skill users without changing either canonical ledger.

    The Skill projection records only allowed scopes. This read-time view uses
    machine ``bound_skills`` from non-archived Thread records; legacy names are
    deliberately insufficient evidence and yield UNKNOWN rather than a false
    NONE/current claim.
    """

    if lock is None:
        return {}
    threads_path = founder / "THREADS.json"
    if not threads_path.exists():
        return _unknown_current_users(lock, "THREADS.json is absent")
    try:
        _direct_file(threads_path, "Thread Registry current-user source")
        raw, registry = guard.read_json_object(threads_path)

        # Import lazily: thread_registry imports this module for Skill
        # resolution, so a module-level import would create a circular import.
        # Its complete canonical validator is the only accepted machine proof;
        # a hand-built partial parser must never manufacture CONFIRMED users.
        import thread_registry as threads_api

        threads_api.validate_registry(registry, founder.parent)
        if raw != guard.canonical_json_bytes(registry):
            raise guard.InvalidState(
                "Thread Registry bytes are not in canonical JSON form"
            )
        current_threads_sha = guard.sha256_bytes(raw)
        current_threads_revision = _identifier(
            registry.get("registry_revision"), "Thread Registry revision"
        )

        _state_sha, supervisor = guard.state_observation(founder / guard.STATE_NAME)
        if supervisor is None:
            raise guard.InvalidState(
                "ACTIVE Supervisor state is absent; Thread freshness is unproven"
            )
        guard.validate_record(supervisor, founder.parent)
        source_revisions = supervisor.get("source_revisions")
        if not isinstance(source_revisions, dict):
            raise guard.InvalidState("Supervisor source_revisions is malformed")
        if source_revisions.get("THREADS_REVISION") != current_threads_revision:
            raise guard.InvalidState(
                "Supervisor THREADS revision does not match the canonical ledger"
            )
        recorded_threads_sha = source_revisions.get("THREADS_SHA256")
        if (
            not isinstance(recorded_threads_sha, str)
            or not re.fullmatch(r"[0-9A-F]{64}", recorded_threads_sha)
            or recorded_threads_sha != current_threads_sha
        ):
            raise guard.InvalidState(
                "Supervisor THREADS SHA-256 does not match the canonical ledger"
            )

        threads = registry["threads"]
        users: dict[str, list[dict[str, Any]]] = {
            skill_id: [] for skill_id in lock["skills"]
        }
        for thread in threads:
            lifecycle = thread["lifecycle_state"]
            bound_skills = thread.get("bound_skills")
            if not isinstance(bound_skills, list):
                raise guard.InvalidState(
                    "Legacy Thread lacks a complete machine Skill baseline"
                )
            if lifecycle == "ARCHIVED":
                continue
            agent_id = thread["agent_id"]
            record_id = thread["thread_record_id"]
            workstream = thread["workstream"]
            runtime = thread["runtime"]
            for binding in bound_skills:
                skill_id = binding["skill_id"]
                if skill_id not in users:
                    continue
                users[skill_id].append(
                    {
                        "agent_id": agent_id,
                        "thread_record_id": record_id,
                        "workstream": workstream,
                        "lifecycle_state": lifecycle,
                        "runtime_thread_id": runtime.get("thread_id"),
                        "runtime_host_id": runtime.get("host_id"),
                    }
                )
    except (guard.GuardError, OSError, RuntimeError, ValueError) as exc:
        return _unknown_current_users(lock, str(exc))

    result: dict[str, dict[str, Any]] = {}
    for skill_id in sorted(users, key=str.casefold):
        current = sorted(
            users[skill_id],
            key=lambda row: (
                row["agent_id"].casefold(),
                row["thread_record_id"].casefold(),
            ),
        )
        result[skill_id] = {
            "state": "CONFIRMED" if current else "NONE",
            "users": current,
            "source": ".founder/THREADS.json",
            "reason": None,
        }
    return result


def inspect_skill_registry(project: str) -> dict[str, Any]:
    """Strictly read-only inspection; never bootstraps files or locks."""

    raw_project = _text(project, "project root")
    requested = Path(os.path.abspath(raw_project))
    if not requested.exists() or not requested.is_dir():
        raise guard.InvalidState(
            f"Project root is not an existing directory: {requested}"
        )
    if guard._is_reparse_or_link(requested):
        raise guard.InvalidState(
            f"Project root may not be a link or reparse point: {requested}"
        )
    root = requested.resolve(strict=True)
    if not guard._same_path(requested, root):
        raise guard.InvalidState(
            f"Project root resolves through an unexpected target: {requested} -> {root}"
        )
    founder_candidate = root / ".founder"
    if not os.path.lexists(founder_candidate):
        return {
            "result": "SKILL_REGISTRY_INSPECTED",
            "project_root": str(root),
            "skill_lock_sha": "ABSENT",
            "skill_lock": None,
            "registry_sha": "ABSENT",
            "pair_state": "ABSENT",
            "issue": None,
            "transaction": {"state": "none"},
            "actual_current_users": {},
            "changed_paths": [],
        }
    root, founder, _created = guard.resolve_project_root(str(root))
    transaction = _transaction_observation(founder)
    try:
        lock_sha, _raw, lock, registry_raw = read_registry_pair(founder)
        pair_state = (
            "LEGACY_SKILLS_MD"
            if lock is None and registry_raw is not None
            else ("ABSENT" if lock is None else "CURRENT")
        )
        issue = None
        pair_valid = lock is not None
    except guard.GuardError as exc:
        lock_sha, _raw, lock = _read_lock(founder / LOCK_NAME)
        registry_raw = None
        pair_state = "RECOVERY_REQUIRED"
        issue = str(exc)
        pair_valid = False
    if transaction["state"] != "none":
        pair_state = "RECOVERY_REQUIRED"
        issue = issue or "Skill Registry transaction lock exists"
    return {
        "result": "SKILL_REGISTRY_INSPECTED",
        "project_root": str(root),
        "skill_lock_sha": lock_sha,
        "skill_lock": lock,
        "registry_sha": (
            guard.sha256_bytes(registry_raw) if registry_raw is not None else "ABSENT"
        ),
        "pair_state": pair_state,
        "issue": issue,
        "transaction": transaction,
        "actual_current_users": (
            derive_actual_current_users(founder, lock) if pair_valid else {}
        ),
        "changed_paths": [],
    }


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


def _acquire_transaction_lock(
    path: Path,
    *,
    root: Path,
    owner: str,
    expected_state_sha: str,
    expected_lock_sha: str,
    expected_registry_sha: str,
    target_lock_sha: str,
    target_registry_sha: str,
    previous_lock_raw: bytes | None,
    previous_registry_raw: bytes | None,
    target_lock_raw: bytes,
    target_registry_raw: bytes,
) -> str:
    nonce = f"SKL_{secrets.token_urlsafe(16)}"
    guard._atomic_create(
        path,
        {
            "project_root": str(root),
            "owner": owner,
            "nonce": nonce,
            "expected_supervisor_state_sha": expected_state_sha,
            "expected_skill_lock_sha": expected_lock_sha,
            "expected_registry_sha": expected_registry_sha,
            "target_skill_lock_sha": target_lock_sha,
            "target_registry_sha": target_registry_sha,
            "previous_lock_b64": (
                base64.b64encode(previous_lock_raw).decode("ascii")
                if previous_lock_raw is not None
                else None
            ),
            "previous_registry_b64": (
                base64.b64encode(previous_registry_raw).decode("ascii")
                if previous_registry_raw is not None
                else None
            ),
            "target_lock_b64": base64.b64encode(target_lock_raw).decode("ascii"),
            "target_registry_b64": base64.b64encode(target_registry_raw).decode("ascii"),
            "created_at": guard.utc_now(),
        },
    )
    return nonce


def _release_transaction_lock(path: Path, *, owner: str, nonce: str) -> None:
    _direct_file(path, "Skill Registry transaction lock")
    _raw, value = guard.read_json_object(path)
    if value.get("owner") != owner or value.get("nonce") != nonce:
        raise guard.Conflict("Skill Registry transaction lock belongs to another operation")
    path.unlink()


def _build_next_lock(
    current: dict[str, Any] | None,
    root: Path,
    *,
    current_sha: str,
    skills: dict[str, dict[str, Any]],
    history: list[dict[str, Any]],
    now: str,
    lock_revision: str,
    registry_revision: str,
) -> tuple[dict[str, Any], bytes, bytes]:
    value = {
        "schema_version": SCHEMA_VERSION,
        "skill_lock_revision": lock_revision,
        "skill_registry_revision": registry_revision,
        "previous_skill_lock_sha256": current_sha,
        "registry_projection_sha256": "0" * 64,
        "created_at": current["created_at"] if current is not None else now,
        "updated_at": now,
        "project_binding": {
            "project_root": str(root),
            "project_binding_id": _project_binding_id(root),
        },
        "skills": skills,
        "history": history,
    }
    # Projection content does not include its own hash, so there is no cycle.
    registry_raw = render_registry(value)
    value["registry_projection_sha256"] = guard.sha256_bytes(registry_raw)
    validate_registry_pair(value, root, registry_raw)
    lock_raw = guard.canonical_json_bytes(value)
    return value, lock_raw, registry_raw


def _restore_path(path: Path, old_raw: bytes | None, label: str) -> None:
    if old_raw is None:
        if path.exists():
            _direct_file(path, label)
            path.unlink()
    else:
        _atomic_replace_bytes(path, old_raw)


def _mutate_skill_registry(
    project: str,
    *,
    owner: str,
    activation_token: str,
    expected_state_sha: str,
    expected_lock_sha: str,
    operation: str,
    mutate: Callable[
        [dict[str, dict[str, Any]], Path],
        tuple[dict[str, dict[str, Any]], dict[str, Any]],
    ],
) -> dict[str, Any]:
    owner = _text(owner, "owner")
    activation_token = _text(activation_token, "activation_token")
    expected_state_sha = _sha_or_absent(expected_state_sha, "expected_state_sha")
    expected_lock_sha = _sha_or_absent(expected_lock_sha, "expected_lock_sha")
    root, founder, _created = guard.resolve_project_root(project)
    _strategy_root, _strategy_founder, strategy_state = strategy._strategy_snapshot(
        str(root)
    )
    if strategy_state is None:
        raise guard.Conflict(
            "Skill Registry mutation requires initialized Strategy in OPERATING state"
        )
    if strategy_state["gate"]["state"] != "OPERATING":
        raise guard.Conflict(
            "Skill Registry mutation is blocked while Strategic Gate is "
            + strategy_state["gate"]["state"]
        )
    fence = guard.verify_fence(str(root), owner=owner, activation_token=activation_token)
    if fence["state_sha"] != expected_state_sha:
        raise guard.Conflict("Supervisor state CAS mismatch before Skill Registry mutation")
    transaction_path = founder / TRANSACTION_LOCK_NAME
    if transaction_path.exists():
        _direct_file(transaction_path, "Skill Registry transaction lock")
        raise guard.Conflict(
            "RECOVERY_REQUIRED: a Skill Registry transaction lock already exists"
        )
    lock_path = founder / LOCK_NAME
    registry_path = founder / REGISTRY_NAME
    current_sha, old_lock_raw, current, old_registry_raw = read_registry_pair(founder)
    if current_sha != expected_lock_sha:
        raise guard.Conflict(
            f"Skill Lock CAS mismatch: expected {expected_lock_sha}, observed {current_sha}"
        )
    current_skills = copy.deepcopy(current["skills"] if current is not None else {})
    proposed_skills, preflight_details = mutate(copy.deepcopy(current_skills), founder)
    if not isinstance(proposed_skills, dict):
        raise guard.InvalidState("Skill Registry mutation must return a skills object")
    now = guard.utc_now()
    lock_revision = guard.new_revision("SKL")
    registry_revision = guard.new_revision("SKR")
    history = copy.deepcopy(current.get("history", []) if current is not None else [])
    change_ref = _text(preflight_details.get("change_ref"), "mutation change_ref")
    for skill_id in sorted(current_skills, key=str.casefold):
        if proposed_skills.get(skill_id) == current_skills[skill_id]:
            continue
        history.append(
            {
                "history_id": guard.new_revision("SKH"),
                "skill_id": skill_id,
                "prior_entry": copy.deepcopy(current_skills[skill_id]),
                "prior_skill_lock_revision": current["skill_lock_revision"],
                "replaced_at": now,
                "change_ref": change_ref,
            }
        )
    next_lock, next_lock_raw, next_registry_raw = _build_next_lock(
        current,
        root,
        current_sha=current_sha,
        skills=proposed_skills,
        history=history,
        now=now,
        lock_revision=lock_revision,
        registry_revision=registry_revision,
    )
    target_lock_sha = guard.sha256_bytes(next_lock_raw)
    target_registry_sha = guard.sha256_bytes(next_registry_raw)
    expected_registry_sha = (
        guard.sha256_bytes(old_registry_raw) if old_registry_raw is not None else "ABSENT"
    )
    nonce = _acquire_transaction_lock(
        transaction_path,
        root=root,
        owner=owner,
        expected_state_sha=expected_state_sha,
        expected_lock_sha=expected_lock_sha,
        expected_registry_sha=expected_registry_sha,
        target_lock_sha=target_lock_sha,
        target_registry_sha=target_registry_sha,
        previous_lock_raw=old_lock_raw,
        previous_registry_raw=old_registry_raw,
        target_lock_raw=next_lock_raw,
        target_registry_raw=next_registry_raw,
    )
    release_lock = True
    wrote_control = False
    try:
        confirmed_state_sha, state_record = guard.state_observation(
            founder / guard.STATE_NAME
        )
        if confirmed_state_sha != expected_state_sha or state_record is None:
            raise guard.Conflict("Supervisor state changed during Skill Registry mutation")
        confirmed_sha, confirmed_lock_raw, confirmed, confirmed_registry_raw = read_registry_pair(
            founder
        )
        if confirmed_sha != expected_lock_sha:
            raise guard.Conflict("Skill Lock CAS changed during mutation")
        if (
            guard.sha256_bytes(confirmed_registry_raw)
            if confirmed_registry_raw is not None
            else "ABSENT"
        ) != expected_registry_sha:
            raise guard.Conflict("SKILLS.md changed during Skill Registry mutation")
        checked_skills, details = mutate(
            copy.deepcopy(confirmed["skills"] if confirmed is not None else {}),
            founder,
        )
        if guard.canonical_json_bytes(checked_skills) != guard.canonical_json_bytes(
            proposed_skills
        ):
            raise guard.Conflict("Skill Registry mutation changed between preflight and commit")
        _atomic_replace_bytes(lock_path, next_lock_raw)
        wrote_control = True
        _atomic_replace_bytes(registry_path, next_registry_raw)
        try:
            checkpoint = guard.checkpoint_active(
                str(root),
                owner=owner,
                activation_token=activation_token,
                expected_state_sha=expected_state_sha,
            )
        except guard.PartialCommit as exc:
            release_lock = False
            raise SkillRegistryPartialCommit(
                "Skill Registry committed and Supervisor checkpoint partially committed; preserve locks",
                changed_paths=[
                    str(lock_path),
                    str(registry_path),
                    str(founder / guard.STATE_NAME),
                    str(founder / guard.LOCK_NAME),
                    str(transaction_path),
                ],
                recovery_action="recover-skill-registry-lock",
            ) from exc
        except Exception as exc:
            try:
                _restore_path(lock_path, confirmed_lock_raw, "Skill Lock rollback target")
                _restore_path(
                    registry_path,
                    confirmed_registry_raw,
                    "Skill Registry rollback target",
                )
            except Exception as rollback_exc:
                release_lock = False
                raise SkillRegistryPartialCommit(
                    "Skill Registry checkpoint failed and two-file rollback was not provable",
                    changed_paths=[str(lock_path), str(registry_path), str(transaction_path)],
                    recovery_action="recover-skill-registry-lock",
                ) from rollback_exc
            raise exc
        return {
            "result": operation,
            "mode": "ACTIVE",
            "owner": owner,
            "project_root": str(root),
            "skill_registry_revision": next_lock["skill_registry_revision"],
            "skill_lock_revision": next_lock["skill_lock_revision"],
            "skill_lock_sha": target_lock_sha,
            "registry_sha": target_registry_sha,
            "state_sha": checkpoint["state_sha"],
            "details": details,
            "changed_paths": [
                str(lock_path),
                str(registry_path),
                str(founder / guard.STATE_NAME),
                str(founder / guard.LOCK_NAME),
            ],
        }
    except SkillRegistryPartialCommit:
        raise
    except Exception:
        if wrote_control:
            # Non-checkpoint failures after the first replace still need a
            # provable two-file rollback.  A failed rollback preserves the
            # transaction fence rather than pretending the pair is coherent.
            try:
                _restore_path(lock_path, old_lock_raw, "Skill Lock rollback target")
                _restore_path(registry_path, old_registry_raw, "Skill Registry rollback target")
            except Exception as rollback_exc:
                release_lock = False
                raise SkillRegistryPartialCommit(
                    "Skill Registry write failed and rollback was not provable",
                    changed_paths=[str(lock_path), str(registry_path), str(transaction_path)],
                    recovery_action="recover-skill-registry-lock",
                ) from rollback_exc
        raise
    finally:
        if release_lock:
            try:
                _release_transaction_lock(transaction_path, owner=owner, nonce=nonce)
            except (guard.GuardError, OSError) as exc:
                raise SkillRegistryPartialCommit(
                    "Skill Registry transaction completed but its lock could not be released",
                    changed_paths=[str(transaction_path)],
                    recovery_action="recover-skill-registry-lock",
                ) from exc


def initialize_skill_registry(
    project: str,
    *,
    owner: str,
    activation_token: str,
    expected_state_sha: str,
    expected_lock_sha: str,
    entries: list[dict[str, Any]] | None = None,
    change_ref: str,
) -> dict[str, Any]:
    normalized = [normalize_entry(item) for item in (entries or [])]
    change_ref = _text(change_ref, "change_ref")

    def mutate(skills: dict[str, dict[str, Any]], _founder: Path):
        if skills:
            raise guard.Conflict("Skill Registry already contains entries")
        if expected_lock_sha != "ABSENT":
            raise guard.Conflict("Skill Registry init requires expected_lock_sha=ABSENT")
        result: dict[str, dict[str, Any]] = {}
        for entry in normalized:
            _assert_bindable_entry_installed_hash(entry)
            if entry["skill_id"] in result:
                raise guard.InvalidState("Duplicate skill_id in initialization")
            result[entry["skill_id"]] = entry
        return result, {"initialized": True, "registered": sorted(result), "change_ref": change_ref}

    return _mutate_skill_registry(
        project,
        owner=owner,
        activation_token=activation_token,
        expected_state_sha=expected_state_sha,
        expected_lock_sha=expected_lock_sha,
        operation="SKILL_REGISTRY_INITIALIZED",
        mutate=mutate,
    )


def register_skills(
    project: str,
    *,
    owner: str,
    activation_token: str,
    expected_state_sha: str,
    expected_lock_sha: str,
    entries: list[dict[str, Any]],
    change_ref: str,
) -> dict[str, Any]:
    """Upsert explicit entries; historical entries are never physically deleted."""

    if not entries:
        raise guard.InvalidState("register/apply requires at least one Skill entry")
    normalized = [normalize_entry(item) for item in entries]
    change_ref = _text(change_ref, "change_ref")

    def mutate(skills: dict[str, dict[str, Any]], _founder: Path):
        changed: list[str] = []
        for entry in normalized:
            _assert_bindable_entry_installed_hash(entry)
            skill_id = entry["skill_id"]
            old = skills.get(skill_id)
            if old is not None and old == entry:
                raise guard.Conflict(f"Skill entry is unchanged: {skill_id}")
            if old is not None and old.get("entry_revision") == entry["entry_revision"]:
                raise guard.Conflict(
                    f"Skill content changed without rotating entry_revision: {skill_id}"
                )
            audited_fields = {
                "content_hash",
                "installed_hash",
                "trust_level",
                "risk_level",
                "approval",
                "status",
                "permissions",
                "scripts_present",
                "runtime_visibility",
            }
            if (
                old is not None
                and old.get("audit_revision") == entry["audit_revision"]
                and any(old.get(key) != entry.get(key) for key in audited_fields)
            ):
                raise guard.Conflict(
                    f"Audited Skill facts changed without rotating audit_revision: {skill_id}"
                )
            skills[skill_id] = copy.deepcopy(entry)
            changed.append(skill_id)
        return skills, {
            "registered": changed,
            "change_ref": change_ref,
            "physical_global_deletion": False,
        }

    return _mutate_skill_registry(
        project,
        owner=owner,
        activation_token=activation_token,
        expected_state_sha=expected_state_sha,
        expected_lock_sha=expected_lock_sha,
        operation="SKILLS_REGISTERED",
        mutate=mutate,
    )


def resolve_bindings(
    founder: Path,
    skill_ids: list[str],
    *,
    agent_id: str | None = None,
    workstream: str | None = None,
    thread_record_id: str | None = None,
    task_id: str | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    """Resolve exact machine-authoritative bindings for Thread Registry.

    The caller, not this function, chooses the Skills. This function only
    checks explicit availability and scoped-binding facts.
    """

    transaction = _transaction_observation(founder)
    if transaction["state"] != "none":
        raise guard.Conflict(
            "RECOVERY_REQUIRED: Skill Registry transaction prevents binding"
        )
    lock_sha, _raw, lock, _registry_raw = read_registry_pair(founder)
    if lock is None:
        if skill_ids:
            raise guard.Conflict("SKILL_REGISTRY_UNAVAILABLE: cannot bind requested Skills")
        return {}, [], selected_bindings_sha([])
    ids = [_identifier(item, "skill_id") for item in skill_ids]
    if len(ids) != len(set(item.casefold() for item in ids)):
        raise guard.InvalidState("Requested Skill IDs contain duplicates")
    selected: list[dict[str, Any]] = []
    capability_primary: dict[str, str] = {}
    for skill_id in ids:
        if skill_id.casefold() in PROTECTED_CORE_SKILL_IDS:
            raise guard.Conflict(
                f"PROTECTED_CORE_SKILL: ordinary project binding may not claim {skill_id}"
            )
        entry = lock["skills"].get(skill_id)
        if entry is None:
            raise guard.Conflict(f"Skill is absent from authoritative lock: {skill_id}")
        if entry["status"] in FAIL_CLOSED_STATUSES:
            raise guard.Conflict(f"Skill is fail-closed ({entry['status']}): {skill_id}")
        if entry["status"] not in BINDABLE_STATUSES:
            raise guard.Conflict(f"Skill is not in a bindable status: {skill_id}")
        if entry["trust_level"] not in BINDABLE_TRUST_LEVELS:
            raise guard.Conflict(f"Skill is not explicitly trusted for binding: {skill_id}")
        if entry["content_hash"] != entry["installed_hash"]:
            raise guard.Conflict(f"HASH_MISMATCH: locked hashes disagree: {skill_id}")
        runtime_visibility = entry.get("runtime_visibility")
        if (
            not isinstance(runtime_visibility, dict)
            or runtime_visibility.get("state") != "CONFIRMED"
        ):
            raise guard.Conflict(
                f"RUNTIME_VISIBILITY_NOT_CONFIRMED: cannot bind {skill_id}"
            )
        observed_hash = _secure_installed_tree(
            entry["installed_path"],
            skill_id=skill_id,
            require_identity=True,
        )
        if observed_hash not in {entry["content_hash"], entry["installed_hash"]}:
            raise guard.Conflict(
                "HASH_MISMATCH: observed installed tree differs from locked "
                f"content for {skill_id} ({INSTALLED_HASH_ALGORITHM})"
            )
        scope = entry["scoped_bindings"]
        checks = {
            "agent_ids": agent_id,
            "workstreams": workstream,
            "thread_record_ids": thread_record_id,
            "task_ids": task_id,
        }
        for key, requested in checks.items():
            allowed = scope[key]
            if allowed and (requested is None or requested not in allowed):
                raise guard.Conflict(
                    f"Skill scoped binding denies {key[:-1]} for {skill_id}"
                )
        if entry["role"] == "PRIMARY":
            for capability in entry["capabilities"]:
                other = capability_primary.get(capability)
                if other is not None:
                    raise guard.Conflict(
                        f"Capability {capability} has multiple PRIMARY Skills: {other}, {skill_id}"
                    )
                capability_primary[capability] = skill_id
        selected.append(entry)
    bound = [
        {
            "skill_id": entry["skill_id"],
            "approved_version": entry["approved_version"],
            "commit_sha": entry["source"]["commit_sha"],
            "content_hash": entry["content_hash"],
            "installed_hash": entry["installed_hash"],
            "audit_revision": entry["audit_revision"],
            "entry_revision": entry["entry_revision"],
            "trust_level": entry["trust_level"],
            "risk_level": entry["risk_level"],
            "status": entry["status"],
            "role": entry["role"],
            "capabilities": copy.deepcopy(entry["capabilities"]),
            "binding_sha256": skill_entry_binding_sha(entry),
        }
        for entry in selected
    ]
    baseline = {
        "skill_registry_revision": lock["skill_registry_revision"],
        "skill_lock_revision": lock["skill_lock_revision"],
        "skill_lock_sha256": lock_sha,
    }
    return baseline, bound, selected_bindings_sha(selected)


def recover_skill_registry_lock(
    project: str,
    *,
    owner: str,
    activation_token: str,
    expected_state_sha: str,
    expected_lock_sha: str,
    lock_owner: str,
    predecessor_liveness: str,
    authorization_ref: str,
) -> dict[str, Any]:
    """Release a stranded transaction only when old or target pair is proven."""

    owner = _text(owner, "owner")
    activation_token = _text(activation_token, "activation_token")
    lock_owner = _text(lock_owner, "lock_owner")
    authorization_ref = _text(authorization_ref, "recovery authorization_ref")
    expected_state_sha = _sha_or_absent(expected_state_sha, "expected_state_sha")
    expected_lock_sha = _sha_or_absent(expected_lock_sha, "expected_lock_sha")
    if predecessor_liveness not in {"current", "terminated"}:
        raise guard.InvalidState("predecessor_liveness must be current or terminated")
    root, founder, _created = guard.resolve_project_root(project)
    fence = guard.verify_fence(
        str(root),
        owner=owner,
        activation_token=activation_token,
        allow_canonical_drift=True,
    )
    if fence["state_sha"] != expected_state_sha:
        raise guard.Conflict("Supervisor state CAS mismatch before Skill Registry recovery")
    path = founder / TRANSACTION_LOCK_NAME
    _direct_file(path, "Skill Registry transaction lock")
    _raw, transaction = guard.read_json_object(path)
    if transaction.get("owner") != lock_owner:
        raise guard.Conflict("Declared lock_owner does not match transaction lock")
    if lock_owner == owner and predecessor_liveness != "current":
        raise guard.Conflict("Current owner recovery requires predecessor_liveness=current")
    if lock_owner != owner and predecessor_liveness != "terminated":
        raise guard.Conflict("Another owner recovery requires terminated predecessor")
    if transaction.get("project_root") != str(root):
        raise guard.Conflict("Skill Registry transaction belongs to another project")
    nonce = _text(transaction.get("nonce"), "transaction nonce")
    observed_lock_sha, observed_lock_raw, _lock = _read_lock(founder / LOCK_NAME)
    if observed_lock_sha != expected_lock_sha:
        raise guard.Conflict("Skill Lock changed before recovery")
    registry_path = founder / REGISTRY_NAME
    if registry_path.exists():
        registry_raw, _registry_text = _read_registry(registry_path)
    else:
        registry_raw = None
    observed_registry_sha = (
        guard.sha256_bytes(registry_raw) if registry_raw is not None else "ABSENT"
    )
    target_pair = (
        transaction.get("target_skill_lock_sha"),
        transaction.get("target_registry_sha"),
    )
    old_pair = (
        transaction.get("expected_skill_lock_sha"),
        transaction.get("expected_registry_sha"),
    )
    observed_pair = (observed_lock_sha, observed_registry_sha)
    allowed_lock_shas = {target_pair[0], old_pair[0]}
    allowed_registry_shas = {target_pair[1], old_pair[1]}
    if observed_pair[0] not in allowed_lock_shas or observed_pair[1] not in allowed_registry_shas:
        raise guard.Conflict(
            "Skill Registry recovery found unknown content; preserve transaction lock"
        )

    def snapshot_bytes(field: str, expected_sha: str, label: str) -> bytes | None:
        encoded = transaction.get(field)
        if expected_sha == "ABSENT":
            if encoded is not None:
                raise guard.InvalidState(f"{label} ABSENT snapshot unexpectedly has content")
            return None
        if not isinstance(encoded, str):
            raise guard.InvalidState(f"{label} recovery snapshot is missing")
        try:
            content = base64.b64decode(encoded.encode("ascii"), validate=True)
        except (ValueError, UnicodeEncodeError) as exc:
            raise guard.InvalidState(f"{label} recovery snapshot is malformed") from exc
        if guard.sha256_bytes(content) != expected_sha:
            raise guard.Conflict(f"{label} recovery snapshot hash mismatch")
        return content

    target_lock_raw = snapshot_bytes(
        "target_lock_b64", target_pair[0], "target Skill Lock"
    )
    target_registry_raw = snapshot_bytes(
        "target_registry_b64", target_pair[1], "target Skill Registry"
    )
    previous_lock_raw = snapshot_bytes(
        "previous_lock_b64", old_pair[0], "previous Skill Lock"
    )
    previous_registry_raw = snapshot_bytes(
        "previous_registry_b64", old_pair[1], "previous Skill Registry"
    )
    assert target_lock_raw is not None and target_registry_raw is not None
    try:
        target_lock = json.loads(target_lock_raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise guard.InvalidState("Target Skill Lock recovery snapshot is invalid JSON") from exc
    if not isinstance(target_lock, dict):
        raise guard.InvalidState("Target Skill Lock recovery snapshot must be an object")
    validate_registry_pair(target_lock, root, target_registry_raw)
    if previous_lock_raw is not None and previous_registry_raw is not None:
        try:
            previous_lock = json.loads(previous_lock_raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise guard.InvalidState("Previous Skill Lock recovery snapshot is invalid") from exc
        if not isinstance(previous_lock, dict):
            raise guard.InvalidState("Previous Skill Lock recovery snapshot must be an object")
        validate_registry_pair(previous_lock, root, previous_registry_raw)

    repaired_pair = observed_pair
    pair_changed = False
    if observed_pair not in {target_pair, old_pair}:
        # A mixed pair means one of the two atomic replaces completed.  Finish
        # the recorded target transaction from immutable, hash-checked bytes.
        _atomic_replace_bytes(founder / LOCK_NAME, target_lock_raw)
        _atomic_replace_bytes(founder / REGISTRY_NAME, target_registry_raw)
        repaired_pair = target_pair
        pair_changed = True
    # Prove the selected pair cross-validates before updating the Supervisor.
    confirmed_lock_sha, _confirmed_raw, _confirmed_lock, confirmed_registry_raw = (
        read_registry_pair(founder)
    )
    confirmed_pair = (
        confirmed_lock_sha,
        guard.sha256_bytes(confirmed_registry_raw)
        if confirmed_registry_raw is not None
        else "ABSENT",
    )
    if confirmed_pair != repaired_pair:
        raise guard.Conflict("Skill Registry repair did not produce the selected pair")
    next_state_sha = expected_state_sha
    state_sha, supervisor = guard.state_observation(founder / guard.STATE_NAME)
    if state_sha != expected_state_sha or supervisor is None:
        raise guard.Conflict("Supervisor state changed during Skill Registry recovery")
    current_sources = guard.read_source_revisions(founder)
    if not guard.source_fingerprints_match(
        supervisor.get("source_revisions"), current_sources
    ):
        checkpoint = guard.checkpoint_active(
            str(root),
            owner=owner,
            activation_token=activation_token,
            expected_state_sha=expected_state_sha,
        )
        next_state_sha = checkpoint["state_sha"]
    _direct_file(path, "Skill Registry transaction lock")
    _confirm_raw, confirmed = guard.read_json_object(path)
    if confirmed.get("owner") != lock_owner or confirmed.get("nonce") != nonce:
        raise guard.Conflict("Skill Registry transaction changed before recovery release")
    path.unlink()
    return {
        "result": "SKILL_REGISTRY_LOCK_RECOVERED",
        "mode": "ACTIVE",
        "owner": owner,
        "prior_lock_owner": lock_owner,
        "authorization_ref": authorization_ref,
        "recovered_pair": "target" if repaired_pair == target_pair else "previous",
        "skill_lock_sha": repaired_pair[0],
        "state_sha": next_state_sha,
        "changed_paths": [
            str(path),
            *(
                [str(founder / LOCK_NAME), str(founder / REGISTRY_NAME)]
                if pair_changed
                else []
            ),
            str(founder / guard.STATE_NAME),
            str(founder / guard.LOCK_NAME),
        ],
    }


def _json_value(raw: str, label: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise guard.InvalidState(f"{label} must be valid JSON: {exc}") from exc


def _entry_args(args: argparse.Namespace) -> list[dict[str, Any]]:
    entries = [_json_value(raw, "entry-json") for raw in args.entry_json]
    if args.entries_json is not None:
        batch = _json_value(args.entries_json, "entries-json")
        if not isinstance(batch, list):
            raise guard.InvalidState("entries-json must be a JSON array")
        entries.extend(batch)
    return entries


def _add_mutation_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--project", required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--activation-token", required=True)
    parser.add_argument("--expected-state-sha", required=True)
    parser.add_argument("--expected-lock-sha", required=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    inspect_parser = subparsers.add_parser("inspect")
    inspect_parser.add_argument("--project", required=True)

    for command in ("init", "apply", "register"):
        mutation = subparsers.add_parser(command)
        _add_mutation_args(mutation)
        mutation.add_argument("--entry-json", action="append", default=[])
        mutation.add_argument("--entries-json")
        mutation.add_argument("--change-ref", required=True)

    recover = subparsers.add_parser("recover-lock")
    _add_mutation_args(recover)
    recover.add_argument("--lock-owner", required=True)
    recover.add_argument(
        "--predecessor-liveness", choices=("current", "terminated"), required=True
    )
    recover.add_argument("--authorization-ref", required=True)
    return parser


def emit(payload: dict[str, Any], exit_code: int = 0) -> int:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return exit_code


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "inspect":
            payload = inspect_skill_registry(args.project)
        elif args.command == "recover-lock":
            payload = recover_skill_registry_lock(
                args.project,
                owner=args.owner,
                activation_token=args.activation_token,
                expected_state_sha=args.expected_state_sha,
                expected_lock_sha=args.expected_lock_sha,
                lock_owner=args.lock_owner,
                predecessor_liveness=args.predecessor_liveness,
                authorization_ref=args.authorization_ref,
            )
        else:
            entries = _entry_args(args)
            common = {
                "project": args.project,
                "owner": args.owner,
                "activation_token": args.activation_token,
                "expected_state_sha": args.expected_state_sha,
                "expected_lock_sha": args.expected_lock_sha,
                "entries": entries,
                "change_ref": args.change_ref,
            }
            if args.command == "init":
                payload = initialize_skill_registry(**common)
            else:
                payload = register_skills(**common)
        return emit(payload)
    except SkillRegistryPartialCommit as exc:
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

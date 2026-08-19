#!/usr/bin/env python3
"""Deterministic, offline, read-only baseline collector for existing projects.

The collector treats every byte in the project as data.  It never imports or
executes project code, runs a build/test/package command, installs anything, or
writes project/FounderOS state.  The only optional child process is a hardened
read-only ``git status`` invocation for a direct, ordinary ``.git`` directory.

Semantic questions such as product purpose, users, maturity, lifecycle, the
relevance of a TODO, or why a historical decision was made intentionally stay
outside this module.  They belong to FounderOS' model-led Adoption Review.
"""

from __future__ import annotations

import argparse
import configparser
import ctypes
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


# A read-only invocation must not create ``scripts/__pycache__``.
sys.dont_write_bytecode = True


SCHEMA = "founder-os.project-baseline/v1"
COLLECTOR_VERSION = "1.0.0"
EXIT_INVALID = 2
EXIT_CONFLICT = 3

CORE_LEDGERS = ("PROJECT.md", "ROADMAP.md", "DECISIONS.md", "AGENTS.md", "STATUS.md")
CONTROL_FILES = frozenset(
    {
        "ACTIVE_SUPERVISOR.json",
        ".write-lock.json",
        "STRATEGY.json",
        "THREADS.json",
        "SKILLS.md",
        "SKILL_LOCK.json",
        ".strategy-lock.json",
        ".strategy-state-lock.json",
        ".thread-registry-lock.json",
        ".skill-registry-lock.json",
    }
)
FOUNDER_CLASSIFICATIONS = frozenset(
    {
        "ABSENT",
        "CURRENT_VALID",
        "LEGACY_COMPATIBLE",
        "PARTIAL_RECOVERY_REQUIRED",
        "CONTROL_RECOVERY_REQUIRED",
        "NON_FOUNDER_COLLISION",
        "PRE_ADOPTION_CONTROL",
    }
)
PROJECT_LIFECYCLES = frozenset(
    {
        "ACTIVE_DEVELOPMENT",
        "FEATURE_COMPLETE",
        "SHIPPED",
        "MAINTENANCE",
        "FROZEN",
        "ARCHIVED",
    }
)
ADOPTION_STATUSES = frozenset(
    {"NOT_APPLICABLE", "READ_ONLY_AUDIT", "BASELINE_READY", "ADOPTED", "BLOCKED"}
)
ADOPTION_CONFIDENCE = frozenset({"HIGH", "MEDIUM", "LOW"})

# These limitations reduce semantic/audit coverage but do not prevent the
# filesystem/Git observation from serving as a deterministic CAS drift anchor.
# Every unknown/new partial reason fails closed until explicitly reviewed here.
BASELINE_ANCHOR_NONBLOCKING_LIMITATIONS = frozenset(
    {
        "LINK_OR_REPARSE_SKIPPED",
        "MANIFEST_LIMIT_EXCEEDED",
        "TEXT_PROBE_LIMIT_EXCEEDED",
        "TODO_SIGNAL_LIMIT_EXCEEDED",
    }
)

WINDOWS_RESERVED_BASENAMES = frozenset(
    {
        "aux",
        "clock$",
        "con",
        "nul",
        "prn",
        *(f"com{number}" for number in range(1, 10)),
        *(f"lpt{number}" for number in range(1, 10)),
    }
)
REPARSE_ATTRIBUTE = 0x400

OPAQUE_DIRECTORY_NAMES = frozenset(
    {
        ".cache",
        ".gradle",
        ".mypy_cache",
        ".next",
        ".nuxt",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        "__pycache__",
        "bower_components",
        "coverage",
        "dist",
        "library",  # Unity generated state (case-folded below).
        "node_modules",
        "obj",
        "target",
        "vendor",
        "venv",
    }
)

MANIFEST_NAMES = frozenset(
    {
        "build.gradle",
        "build.gradle.kts",
        "cargo.toml",
        "composer.json",
        "deno.json",
        "deno.jsonc",
        "docker-compose.yml",
        "docker-compose.yaml",
        "gemfile",
        "go.mod",
        "gradle.properties",
        "meson.build",
        "mix.exs",
        "package.json",
        "packages.config",
        "pom.xml",
        "project.godot",
        "pubspec.yaml",
        "pyproject.toml",
        "requirements.txt",
        "setup.cfg",
        "setup.py",
        "vcpkg.json",
    }
)
LOCKFILE_NAMES = frozenset(
    {
        "bun.lock",
        "bun.lockb",
        "cargo.lock",
        "composer.lock",
        "gemfile.lock",
        "go.sum",
        "package-lock.json",
        "packages.lock.json",
        "pnpm-lock.yaml",
        "poetry.lock",
        "pubspec.lock",
        "uv.lock",
        "yarn.lock",
    }
)
DOCUMENT_NAMES = frozenset(
    {
        "readme",
        "readme.md",
        "readme.rst",
        "readme.txt",
        "architecture.md",
        "changelog.md",
        "contributing.md",
        "release-notes.md",
    }
)
BUILD_DECLARATION_NAMES = frozenset(
    {
        "dockerfile",
        "makefile",
        "cmakelists.txt",
        "justfile",
        "rakefile",
        "build.xml",
        "build.ps1",
        "build.sh",
        "gradlew",
        "gradlew.bat",
    }
)
TEST_CONFIG_NAMES = frozenset(
    {
        "jest.config.js",
        "jest.config.ts",
        "playwright.config.js",
        "playwright.config.ts",
        "pytest.ini",
        "tox.ini",
        "vitest.config.js",
        "vitest.config.ts",
    }
)
TEXT_EXTENSIONS = frozenset(
    {
        ".c",
        ".cc",
        ".cfg",
        ".conf",
        ".cpp",
        ".cs",
        ".css",
        ".dart",
        ".gd",
        ".go",
        ".h",
        ".hpp",
        ".html",
        ".ini",
        ".java",
        ".js",
        ".json",
        ".jsx",
        ".kt",
        ".kts",
        ".lua",
        ".md",
        ".mjs",
        ".php",
        ".ps1",
        ".py",
        ".rb",
        ".rs",
        ".rst",
        ".sh",
        ".sql",
        ".swift",
        ".toml",
        ".ts",
        ".tsx",
        ".txt",
        ".vue",
        ".xml",
        ".yaml",
        ".yml",
    }
)
SOURCE_EXTENSIONS = frozenset(
    {
        ".c",
        ".cc",
        ".cpp",
        ".cs",
        ".dart",
        ".gd",
        ".go",
        ".java",
        ".js",
        ".jsx",
        ".kt",
        ".kts",
        ".lua",
        ".mjs",
        ".php",
        ".py",
        ".rb",
        ".rs",
        ".swift",
        ".ts",
        ".tsx",
        ".vue",
    }
)

SENSITIVE_BASENAME_PATTERNS = (
    re.compile(r"^\.env(?:\..*)?$", re.IGNORECASE),
    re.compile(r"^(?:id_rsa|id_dsa|id_ecdsa|id_ed25519)(?:\..*)?$", re.IGNORECASE),
    re.compile(r".*(?:credential|credentials|secret|secrets|service[-_]?account).*", re.IGNORECASE),
    re.compile(r".*(?:private[-_]?key|client[-_]?secret|access[-_]?token).*", re.IGNORECASE),
    re.compile(r"^(?:\.netrc|\.npmrc|\.pypirc|\.dockercfg)$", re.IGNORECASE),
    re.compile(r".*\.(?:key|p12|pfx|pem)$", re.IGNORECASE),
)
TODO_PATTERN = re.compile(r"\b(TODO|FIXME|HACK|XXX)\b", re.IGNORECASE)


class BaselineError(RuntimeError):
    """A controlled invalid-input or identity-drift failure."""

    def __init__(self, code: str, detail: str, *, conflict: bool = False) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
        self.conflict = conflict


@dataclass(frozen=True)
class BaselineLimits:
    max_files: int = 20_000
    max_directories: int = 5_000
    max_total_entries: int = 25_000
    max_depth: int = 32
    max_total_hash_bytes: int = 256 * 1024 * 1024
    max_file_hash_bytes: int = 8 * 1024 * 1024
    max_manifests: int = 256
    max_manifest_bytes: int = 2 * 1024 * 1024
    max_text_probe_files: int = 1_000
    max_text_probe_bytes: int = 512 * 1024
    max_total_text_probe_bytes: int = 32 * 1024 * 1024
    max_todo_signals: int = 1_000
    max_git_output_bytes: int = 8 * 1024 * 1024
    git_timeout_seconds: int = 15

    def validate(self) -> "BaselineLimits":
        for name, value in asdict(self).items():
            if not isinstance(value, int) or value < 1:
                raise BaselineError("INVALID_LIMIT", f"{name} must be a positive integer")
        return self


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest().upper()


def _normal(path: Path | str) -> str:
    return os.path.normcase(str(path))


def _is_within(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath((_normal(path), _normal(root))) == _normal(root)
    except ValueError:
        return False


def _is_reparse(metadata: os.stat_result) -> bool:
    return bool(getattr(metadata, "st_file_attributes", 0) & REPARSE_ATTRIBUTE)


def _metadata_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_mode),
        int(metadata.st_nlink),
        int(metadata.st_size),
        int(getattr(metadata, "st_mtime_ns", int(metadata.st_mtime * 1_000_000_000))),
        int(getattr(metadata, "st_ctime_ns", int(metadata.st_ctime * 1_000_000_000))),
        int(getattr(metadata, "st_file_attributes", 0)),
    )


def _link_or_reparse_component(path: Path) -> Path | None:
    lexical = Path(os.path.abspath(str(path)))
    for component in reversed((lexical, *lexical.parents)):
        try:
            metadata = component.lstat()
        except OSError:
            continue
        if stat.S_ISLNK(metadata.st_mode) or _is_reparse(metadata):
            return component
    return None


def _resolve_safe_root(project: str | os.PathLike[str]) -> Path:
    raw = Path(project)
    if not raw.is_absolute():
        raise BaselineError("PROJECT_PATH_NOT_ABSOLUTE", str(raw))
    lexical = Path(os.path.abspath(str(raw)))
    redirect = _link_or_reparse_component(lexical)
    if redirect is not None:
        raise BaselineError("PROJECT_ROOT_REDIRECTED", str(redirect))
    try:
        metadata = lexical.lstat()
        resolved = lexical.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise BaselineError("PROJECT_ROOT_UNAVAILABLE", str(exc)) from exc
    if not stat.S_ISDIR(metadata.st_mode) or _is_reparse(metadata):
        raise BaselineError("PROJECT_ROOT_NOT_PLAIN_DIRECTORY", str(lexical))
    if _link_or_reparse_component(lexical) is not None:
        raise BaselineError("PROJECT_ROOT_REDIRECTED", str(lexical))
    return resolved


def _project_binding_id(root: Path) -> str:
    material = f"founder-os-adoption-binding-v1\0{_normal(root).replace(chr(92), '/')}"
    return _sha256(material.encode("utf-8"))


def _unsafe_path_part_reason(part: str) -> str | None:
    if part in {"", ".", ".."}:
        return "dot or empty path component"
    if any(ord(character) < 32 for character in part):
        return "control character"
    if ":" in part:
        return "colon or alternate-data-stream syntax"
    if part.endswith((" ", ".")):
        return "trailing space or period"
    if part.split(".", 1)[0].casefold() in WINDOWS_RESERVED_BASENAMES:
        return "reserved Windows device name"
    return None


def _is_sensitive(relative: str) -> bool:
    path = Path(relative)
    folded_parts = {part.casefold() for part in path.parts}
    if folded_parts & {".aws", ".azure", ".gnupg", ".ssh", "credentials", "secrets"}:
        return True
    return any(pattern.fullmatch(path.name) for pattern in SENSITIVE_BASENAME_PATTERNS)


if os.name == "nt":
    from ctypes import wintypes

    _WIN_FILE_READ_ATTRIBUTES = 0x0080
    _WIN_FILE_LIST_DIRECTORY = 0x0001
    _WIN_GENERIC_READ = 0x80000000
    _WIN_FILE_SHARE_READ = 0x00000001
    _WIN_OPEN_EXISTING = 3
    _WIN_FILE_ATTRIBUTE_DIRECTORY = 0x00000010
    _WIN_FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
    _WIN_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    _WIN_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000

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
    _KERNEL32.ReadFile.argtypes = (
        wintypes.HANDLE,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPVOID,
    )
    _KERNEL32.ReadFile.restype = wintypes.BOOL
    _KERNEL32.CloseHandle.argtypes = (wintypes.HANDLE,)
    _KERNEL32.CloseHandle.restype = wintypes.BOOL
    _INVALID_WIN_HANDLE = ctypes.c_void_p(-1).value


def _win_information(handle: int) -> Any:
    information = _WinHandleInformation()
    if not _KERNEL32.GetFileInformationByHandle(handle, ctypes.byref(information)):
        raise BaselineError(
            "PATH_IDENTITY_QUERY_FAILED",
            str(ctypes.WinError(ctypes.get_last_error())),
            conflict=True,
        )
    return information


def _win_identity(information: Any) -> tuple[int, ...]:
    return (
        int(information.volume_serial),
        (int(information.file_index_high) << 32) | int(information.file_index_low),
        int(information.attributes),
        int(information.link_count),
        (int(information.size_high) << 32) | int(information.size_low),
        (int(information.last_write_time.high) << 32) | int(information.last_write_time.low),
    )


class _DirectoryPin:
    def __init__(self, path: Path, handle: int, identity: tuple[int, ...], windows: bool):
        self.path = path
        self.handle = handle
        self.identity = identity
        self.windows = windows

    def assert_current(self) -> None:
        try:
            metadata = self.path.lstat()
        except OSError as exc:
            raise BaselineError("PROJECT_CHANGED_DURING_SCAN", str(exc), conflict=True) from exc
        if stat.S_ISLNK(metadata.st_mode) or _is_reparse(metadata) or not stat.S_ISDIR(metadata.st_mode):
            raise BaselineError("PROJECT_CHANGED_DURING_SCAN", str(self.path), conflict=True)
        if self.windows:
            if _win_identity(_win_information(self.handle)) != self.identity:
                raise BaselineError("PROJECT_CHANGED_DURING_SCAN", str(self.path), conflict=True)
        elif _metadata_identity(os.fstat(self.handle)) != self.identity:
            raise BaselineError("PROJECT_CHANGED_DURING_SCAN", str(self.path), conflict=True)

    def close(self) -> None:
        if self.handle < 0:
            return
        if self.windows:
            _KERNEL32.CloseHandle(self.handle)
        else:
            os.close(self.handle)
        self.handle = -1


def _open_directory_pin(path: Path, *, parent: _DirectoryPin | None = None) -> _DirectoryPin:
    try:
        before = (
            os.stat(path.name, dir_fd=parent.handle, follow_symlinks=False)
            if parent is not None and not parent.windows
            else path.lstat()
        )
    except OSError as exc:
        raise BaselineError("DIRECTORY_OPEN_FAILED", f"{path}: {exc}", conflict=True) from exc
    if stat.S_ISLNK(before.st_mode) or _is_reparse(before) or not stat.S_ISDIR(before.st_mode):
        raise BaselineError("DIRECTORY_NOT_PLAIN", str(path), conflict=True)
    if os.name == "nt":
        handle = _KERNEL32.CreateFileW(
            str(path),
            _WIN_FILE_READ_ATTRIBUTES | _WIN_FILE_LIST_DIRECTORY,
            _WIN_FILE_SHARE_READ,
            None,
            _WIN_OPEN_EXISTING,
            _WIN_FILE_FLAG_OPEN_REPARSE_POINT | _WIN_FILE_FLAG_BACKUP_SEMANTICS,
            None,
        )
        if handle == _INVALID_WIN_HANDLE:
            raise BaselineError(
                "DIRECTORY_OPEN_FAILED",
                f"{path}: {ctypes.WinError(ctypes.get_last_error())}",
                conflict=True,
            )
        handle = int(handle)
        try:
            information = _win_information(handle)
            if not int(information.attributes) & _WIN_FILE_ATTRIBUTE_DIRECTORY:
                raise BaselineError("DIRECTORY_CHANGED_DURING_OPEN", str(path), conflict=True)
            if int(information.attributes) & _WIN_FILE_ATTRIBUTE_REPARSE_POINT:
                raise BaselineError("DIRECTORY_REPARSE_POINT", str(path), conflict=True)
            identity = _win_identity(information)
            after = path.lstat()
            if _metadata_identity(before) != _metadata_identity(after):
                raise BaselineError("DIRECTORY_CHANGED_DURING_OPEN", str(path), conflict=True)
            if before.st_ino and identity[1] and int(before.st_ino) != identity[1]:
                raise BaselineError("DIRECTORY_CHANGED_DURING_OPEN", str(path), conflict=True)
            return _DirectoryPin(path, handle, identity, True)
        except Exception:
            _KERNEL32.CloseHandle(handle)
            raise
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        handle = (
            os.open(path.name, flags, dir_fd=parent.handle)
            if parent is not None
            else os.open(path, flags)
        )
    except OSError as exc:
        raise BaselineError("DIRECTORY_OPEN_FAILED", f"{path}: {exc}", conflict=True) from exc
    opened = os.fstat(handle)
    if _metadata_identity(opened) != _metadata_identity(before):
        os.close(handle)
        raise BaselineError("DIRECTORY_CHANGED_DURING_OPEN", str(path), conflict=True)
    return _DirectoryPin(path, handle, _metadata_identity(opened), False)


def _read_regular_file(
    path: Path,
    expected: os.stat_result,
    *,
    parent: _DirectoryPin,
    max_bytes: int,
) -> bytes:
    if expected.st_size > max_bytes:
        raise BaselineError("FILE_SIZE_LIMIT", f"{path}: {expected.st_size} > {max_bytes}")
    if expected.st_nlink != 1:
        raise BaselineError("HARDLINK_CONTENT_NOT_READ", str(path))
    if os.name == "nt":
        handle = _KERNEL32.CreateFileW(
            str(path),
            _WIN_GENERIC_READ,
            _WIN_FILE_SHARE_READ,
            None,
            _WIN_OPEN_EXISTING,
            _WIN_FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )
        if handle == _INVALID_WIN_HANDLE:
            raise BaselineError(
                "FILE_OPEN_FAILED",
                f"{path}: {ctypes.WinError(ctypes.get_last_error())}",
                conflict=True,
            )
        handle = int(handle)
        try:
            before = _win_information(handle)
            if int(before.attributes) & (
                _WIN_FILE_ATTRIBUTE_DIRECTORY | _WIN_FILE_ATTRIBUTE_REPARSE_POINT
            ):
                raise BaselineError("FILE_CHANGED_DURING_OPEN", str(path), conflict=True)
            if int(before.link_count) != 1:
                raise BaselineError("HARDLINK_CONTENT_NOT_READ", str(path))
            if expected.st_ino and _win_identity(before)[1] and int(expected.st_ino) != _win_identity(before)[1]:
                raise BaselineError("FILE_CHANGED_DURING_OPEN", str(path), conflict=True)
            chunks: list[bytes] = []
            remaining = int(expected.st_size)
            while remaining:
                amount = min(1024 * 1024, remaining)
                buffer = ctypes.create_string_buffer(amount)
                read = wintypes.DWORD()
                if not _KERNEL32.ReadFile(handle, buffer, amount, ctypes.byref(read), None):
                    raise BaselineError(
                        "FILE_READ_FAILED",
                        f"{path}: {ctypes.WinError(ctypes.get_last_error())}",
                        conflict=True,
                    )
                if not read.value:
                    break
                chunks.append(buffer.raw[: read.value])
                remaining -= int(read.value)
            content = b"".join(chunks)
            after = _win_information(handle)
            if len(content) != expected.st_size or _win_identity(before) != _win_identity(after):
                raise BaselineError("FILE_CHANGED_DURING_READ", str(path), conflict=True)
            return content
        finally:
            _KERNEL32.CloseHandle(handle)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path.name, flags, dir_fd=parent.handle)
    except OSError as exc:
        raise BaselineError("FILE_OPEN_FAILED", f"{path}: {exc}", conflict=True) from exc
    try:
        opened = os.fstat(descriptor)
        expected_identity = (
            expected.st_dev,
            expected.st_ino,
            expected.st_size,
            expected.st_nlink,
            expected.st_mode,
        )
        opened_identity = (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_nlink,
            opened.st_mode,
        )
        if expected_identity != opened_identity or not stat.S_ISREG(opened.st_mode):
            raise BaselineError("FILE_CHANGED_DURING_OPEN", str(path), conflict=True)
        chunks = []
        remaining = int(opened.st_size)
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
        after = os.fstat(descriptor)
        if len(content) != opened.st_size or _metadata_identity(after) != _metadata_identity(opened):
            raise BaselineError("FILE_CHANGED_DURING_READ", str(path), conflict=True)
        return content
    finally:
        os.close(descriptor)


def _safe_json_loads(raw: bytes, label: str) -> Any:
    def unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"duplicate key {key!r}")
            value[key] = item
        return value

    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=unique_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise BaselineError("INVALID_JSON", f"{label}: {exc}") from exc


def _direct_file_bytes(path: Path, *, max_bytes: int, label: str) -> bytes:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise BaselineError("CONTROL_READ_FAILED", f"{label}: {exc}") from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or _is_reparse(metadata)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
    ):
        raise BaselineError("CONTROL_NOT_DIRECT_FILE", label)
    if metadata.st_size > max_bytes:
        raise BaselineError("CONTROL_FILE_TOO_LARGE", label)
    if os.name == "nt":
        handle = _KERNEL32.CreateFileW(
            str(path),
            _WIN_GENERIC_READ,
            _WIN_FILE_SHARE_READ,
            None,
            _WIN_OPEN_EXISTING,
            _WIN_FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )
        if handle == _INVALID_WIN_HANDLE:
            raise BaselineError(
                "CONTROL_READ_FAILED",
                f"{label}: {ctypes.WinError(ctypes.get_last_error())}",
            )
        handle = int(handle)
        try:
            before = _win_information(handle)
            if int(before.attributes) & (
                _WIN_FILE_ATTRIBUTE_DIRECTORY | _WIN_FILE_ATTRIBUTE_REPARSE_POINT
            ) or int(before.link_count) != 1:
                raise BaselineError("CONTROL_NOT_DIRECT_FILE", label)
            if metadata.st_ino and _win_identity(before)[1] and int(metadata.st_ino) != _win_identity(before)[1]:
                raise BaselineError("CONTROL_CHANGED_DURING_OPEN", label, conflict=True)
            chunks: list[bytes] = []
            remaining = int(metadata.st_size)
            while remaining:
                amount = min(1024 * 1024, remaining)
                buffer = ctypes.create_string_buffer(amount)
                read = wintypes.DWORD()
                if not _KERNEL32.ReadFile(handle, buffer, amount, ctypes.byref(read), None):
                    raise BaselineError(
                        "CONTROL_READ_FAILED",
                        f"{label}: {ctypes.WinError(ctypes.get_last_error())}",
                    )
                if not read.value:
                    break
                chunks.append(buffer.raw[: read.value])
                remaining -= int(read.value)
            raw = b"".join(chunks)
            after = _win_information(handle)
            if len(raw) != metadata.st_size or _win_identity(before) != _win_identity(after):
                raise BaselineError("CONTROL_CHANGED_DURING_READ", label, conflict=True)
            return raw
        finally:
            _KERNEL32.CloseHandle(handle)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if _metadata_identity(opened) != _metadata_identity(metadata):
            raise BaselineError("CONTROL_CHANGED_DURING_OPEN", label, conflict=True)
        chunks: list[bytes] = []
        remaining = int(opened.st_size)
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) != opened.st_size or _metadata_identity(os.fstat(descriptor)) != _metadata_identity(opened):
            raise BaselineError("CONTROL_CHANGED_DURING_READ", label, conflict=True)
        return raw
    finally:
        os.close(descriptor)


def classify_founder_state(project: str | os.PathLike[str]) -> dict[str, Any]:
    """Classify an existing ``.founder`` shape without trusting or modifying it."""

    root = _resolve_safe_root(project)
    founder = root / ".founder"
    if not os.path.lexists(founder):
        return {
            "classification": "ABSENT",
            "evidence": [],
            "core_ledgers_present": [],
            "control_files_present": [],
            "issue": None,
            "trusted_as_instruction": False,
        }
    try:
        founder_metadata = founder.lstat()
    except OSError as exc:
        return {
            "classification": "CONTROL_RECOVERY_REQUIRED",
            "evidence": [".founder exists but cannot be inspected"],
            "core_ledgers_present": [],
            "control_files_present": [],
            "issue": str(exc),
            "trusted_as_instruction": False,
        }
    if (
        stat.S_ISLNK(founder_metadata.st_mode)
        or _is_reparse(founder_metadata)
        or not stat.S_ISDIR(founder_metadata.st_mode)
    ):
        return {
            "classification": "CONTROL_RECOVERY_REQUIRED",
            "evidence": [".founder is not a direct plain directory"],
            "core_ledgers_present": [],
            "control_files_present": [],
            "issue": "FOUNDER_STATE_REDIRECTED_OR_NOT_DIRECTORY",
            "trusted_as_instruction": False,
        }
    try:
        names = sorted((entry.name for entry in os.scandir(founder)), key=str.casefold)
    except OSError as exc:
        return {
            "classification": "CONTROL_RECOVERY_REQUIRED",
            "evidence": [".founder cannot be enumerated"],
            "core_ledgers_present": [],
            "control_files_present": [],
            "issue": str(exc),
            "trusted_as_instruction": False,
        }
    ledgers = [name for name in CORE_LEDGERS if name in names]
    controls = [name for name in sorted(CONTROL_FILES, key=str.casefold) if name in names]
    evidence = [f"direct ledger: {name}" for name in ledgers]
    evidence.extend(f"control name observed: {name}" for name in controls)

    # Any direct canonical/control file that is itself linked, hardlinked, or
    # malformed is a recovery condition, never a source of trusted commands.
    recognized_files = [*ledgers, *controls]
    try:
        for name in recognized_files:
            path = founder / name
            metadata = path.lstat()
            if name.startswith(".") and name.endswith("lock.json") and stat.S_ISDIR(metadata.st_mode):
                raise BaselineError("CONTROL_NOT_DIRECT_FILE", name)
            if (
                stat.S_ISLNK(metadata.st_mode)
                or _is_reparse(metadata)
                or not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
            ):
                raise BaselineError("CONTROL_NOT_DIRECT_FILE", name)
            if metadata.st_size > 4 * 1024 * 1024:
                raise BaselineError("CONTROL_FILE_TOO_LARGE", name)
    except (OSError, BaselineError) as exc:
        return {
            "classification": "CONTROL_RECOVERY_REQUIRED",
            "evidence": evidence,
            "core_ledgers_present": ledgers,
            "control_files_present": controls,
            "issue": str(exc),
            "trusted_as_instruction": False,
        }

    if "SKILLS.md" in controls or "SKILL_LOCK.json" in controls:
        try:
            _validate_skill_registry_pair(root, founder)
        except BaselineError as exc:
            return {
                "classification": "CONTROL_RECOVERY_REQUIRED",
                "evidence": evidence,
                "core_ledgers_present": ledgers,
                "control_files_present": controls,
                "issue": str(exc),
                "trusted_as_instruction": False,
            }

    transaction_controls = sorted(
        (
            name
            for name in controls
            if name
            in {
                ".strategy-lock.json",
                ".strategy-state-lock.json",
                ".thread-registry-lock.json",
                ".skill-registry-lock.json",
            }
        ),
        key=str.casefold,
    )
    if transaction_controls:
        return {
            "classification": "CONTROL_RECOVERY_REQUIRED",
            "evidence": evidence,
            "core_ledgers_present": ledgers,
            "control_files_present": controls,
            "issue": "INCOMPLETE_CONTROL_TRANSACTION:" + ",".join(transaction_controls),
            "trusted_as_instruction": False,
        }

    if 0 < len(ledgers) < len(CORE_LEDGERS):
        classification = "PARTIAL_RECOVERY_REQUIRED"
        issue = "PARTIAL_CANONICAL_LEDGERS"
    elif len(ledgers) == len(CORE_LEDGERS):
        active = founder / "ACTIVE_SUPERVISOR.json"
        if not active.exists():
            modern_without_active = any(
                name in controls
                for name in ("STRATEGY.json", "THREADS.json", "SKILL_LOCK.json", ".write-lock.json")
            )
            classification = (
                "CONTROL_RECOVERY_REQUIRED" if modern_without_active else "LEGACY_COMPATIBLE"
            )
            issue = "MODERN_CONTROL_WITHOUT_SUPERVISOR" if modern_without_active else None
        else:
            try:
                record = _safe_json_loads(
                    _direct_file_bytes(active, max_bytes=1024 * 1024, label="ACTIVE_SUPERVISOR.json"),
                    "ACTIVE_SUPERVISOR.json",
                )
                if not isinstance(record, dict):
                    raise BaselineError("INVALID_SUPERVISOR", "record is not an object")
                strategy_state = _validate_current_control_state(root, founder, record)
                if strategy_state is None:
                    # A valid pre-Strategy ACTIVE Supervisor plus all five
                    # canonical ledgers is an older FounderOS control plane.
                    # Its ownership/fingerprints are valid, but it still needs
                    # an explicit compatibility migration before modern writes.
                    classification = "LEGACY_COMPATIBLE"
                    issue = None
                elif strategy_state.get("project_phase") == "bootstrapped":
                    classification = "CURRENT_VALID"
                    issue = None
                else:
                    raise BaselineError(
                        "INVALID_CONTROL_STATE",
                        "five canonical ledgers require a bootstrapped Strategy",
                    )
            except BaselineError as exc:
                classification = "CONTROL_RECOVERY_REQUIRED"
                issue = str(exc)
    elif controls:
        active = founder / "ACTIVE_SUPERVISOR.json"
        if active.exists():
            try:
                record = _safe_json_loads(
                    _direct_file_bytes(active, max_bytes=1024 * 1024, label="ACTIVE_SUPERVISOR.json"),
                    "ACTIVE_SUPERVISOR.json",
                )
                if not isinstance(record, dict):
                    raise BaselineError("INVALID_SUPERVISOR", "record is not an object")
                strategy_state = _validate_current_control_state(root, founder, record)
                if strategy_state is None:
                    raise BaselineError(
                        "INVALID_CONTROL_STATE",
                        "Supervisor exists without Strategy initialization",
                    )
                phase = strategy_state.get("project_phase")
                if phase == "pre-adoption" and (
                    strategy_state.get("project_origin") == "ADOPTED"
                    and strategy_state.get("adoption_status") == "BASELINE_READY"
                ):
                    classification = "PRE_ADOPTION_CONTROL"
                    issue = None
                elif phase == "pre-bootstrap" and "adoption" not in strategy_state:
                    # A legitimate V2.1/V2.2 new-project control plane resumes
                    # its Direction/Bootstrap flow; it is not Brownfield.
                    classification = "CURRENT_VALID"
                    issue = None
                else:
                    raise BaselineError(
                        "INVALID_CONTROL_STATE",
                        "ledger cardinality does not match Strategy project_phase",
                    )
            except BaselineError as exc:
                classification = "CONTROL_RECOVERY_REQUIRED"
                issue = str(exc)
        else:
            classification = "CONTROL_RECOVERY_REQUIRED"
            issue = "CONTROL_FILES_WITHOUT_ACTIVE_SUPERVISOR"
    else:
        classification = "NON_FOUNDER_COLLISION"
        issue = "NO_RECOGNIZED_FOUNDEROS_STATE"

    if classification not in FOUNDER_CLASSIFICATIONS:  # defensive invariant
        raise AssertionError(classification)
    return {
        "classification": classification,
        "evidence": evidence,
        "core_ledgers_present": ledgers,
        "control_files_present": controls,
        "issue": issue,
        "trusted_as_instruction": False,
    }


def _validate_current_control_state(
    root: Path, founder: Path, record: dict[str, Any]
) -> dict[str, Any] | None:
    """Apply the canonical sibling validators without executing project code."""

    try:
        import decision_state as strategy_api
        import skill_registry as skills_api
        import supervisor_guard as guard_api
        import thread_registry as threads_api

        guard_api.validate_record(record, root)
        state_sha, observed_record = guard_api.state_observation(
            founder / guard_api.STATE_NAME
        )
        if observed_record != record:
            raise guard_api.Conflict("supervisor state changed during validation")
        write_lock = guard_api._lock_owner(founder / guard_api.LOCK_NAME)
        if write_lock is not None:
            guard_api._validate_lock_record_binding(root, record, state_sha, write_lock)
        current_sources = guard_api.read_source_revisions(founder)
        if not guard_api.source_fingerprints_match(
            record.get("source_revisions"), current_sources
        ):
            raise guard_api.Conflict("canonical source fingerprints are stale")

        strategy_state: dict[str, Any] | None = None
        strategy_path = founder / strategy_api.STRATEGY_NAME
        if strategy_path.exists():
            _raw, strategy_state = guard_api.read_json_object(strategy_path)
            strategy_api.validate_strategy(strategy_state, root)

        threads_path = founder / threads_api.REGISTRY_NAME
        if threads_path.exists():
            _raw, registry = guard_api.read_json_object(threads_path)
            threads_api.validate_registry(registry, root)

        # The governed Skill Registry is inseparable: observing either half
        # invokes the canonical pair reader, and a missing peer fails closed.
        skills_path = founder / skills_api.REGISTRY_NAME
        skill_lock_path = founder / skills_api.LOCK_NAME
        if skills_path.exists() or skill_lock_path.exists():
            _validate_skill_registry_pair(root, founder)
        return strategy_state
    except (ImportError, OSError, ValueError, RuntimeError) as exc:
        raise BaselineError("INVALID_CONTROL_STATE", str(exc)) from exc


def _validate_skill_registry_pair(root: Path, founder: Path) -> None:
    """Validate both governed Skill Registry halves, failing closed on orphans."""

    try:
        import skill_registry as skills_api
        import supervisor_guard as guard_api

        skills_path = founder / skills_api.REGISTRY_NAME
        lock_path = founder / skills_api.LOCK_NAME
        skills_api.read_registry_pair(founder)
        if not skills_path.exists() or not lock_path.exists():
            raise guard_api.InvalidState(
                "SKILLS.md and SKILL_LOCK.json must both exist"
            )
    except (ImportError, OSError, ValueError, RuntimeError) as exc:
        raise BaselineError("INVALID_SKILL_REGISTRY_PAIR", str(exc)) from exc


def _normalize_test_observation(
    observation: Mapping[str, Any] | None,
) -> tuple[dict[str, str | None] | None, int | None, set[str], int]:
    if observation is None:
        return None, None, set(), 0
    failures: dict[str, str | None] | None = None
    skipped: set[str] = set()
    skipped_count = 0
    for key in ("failure_ids", "failed_ids", "failures", "failed"):
        value = observation.get(key)
        if not isinstance(value, (list, tuple, set)):
            continue
        failures = {}
        for item in value:
            if isinstance(item, str) and item:
                failures[item] = None
                continue
            if not isinstance(item, Mapping):
                continue
            test_id = item.get("id", item.get("test_id"))
            if not isinstance(test_id, str) or not test_id:
                continue
            status = str(item.get("status", "FAILED")).upper()
            if status in {"SKIP", "SKIPPED", "XFAIL", "NOT_RUN"}:
                skipped.add(test_id)
                continue
            signature = item.get("failure_signature", item.get("signature"))
            failures[test_id] = signature if isinstance(signature, str) else None
        break
    for key in ("skipped", "skipped_ids", "skip_ids"):
        value = observation.get(key)
        if isinstance(value, (list, tuple, set)):
            skipped.update(item for item in value if isinstance(item, str) and item)
    for key in ("skipped_count", "skip_count", "skips", "skip"):
        value = observation.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            skipped_count = value
            break
    skipped_count = max(skipped_count, len(skipped))
    declared_failure_count: int | None = None
    for key in ("failed_count", "fail_count", "failed"):
        value = observation.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            declared_failure_count = value
            break
    if failures is not None:
        return (
            failures,
            declared_failure_count if declared_failure_count is not None else len(failures),
            skipped,
            skipped_count,
        )
    if declared_failure_count is not None:
        return None, declared_failure_count, skipped, skipped_count
    summary = observation.get("summary")
    if isinstance(summary, Mapping):
        nested_failures, count, nested_skipped, nested_skip_count = (
            _normalize_test_observation(summary)
        )
        return (
            nested_failures,
            count,
            skipped | nested_skipped,
            max(skipped_count, nested_skip_count),
        )
    return None, None, skipped, skipped_count


def compare_test_observations(
    before: Mapping[str, Any] | None,
    after: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Compare externally supplied test observations without claiming causality."""

    before_failures, before_count, before_skipped, before_skip_count = (
        _normalize_test_observation(before)
    )
    after_failures, after_count, after_skipped, after_skip_count = (
        _normalize_test_observation(after)
    )
    pre_existing: list[str] = []
    new: list[str] = []
    resolved: list[str] = []
    unchanged: list[str] = []
    changed: list[str] = []
    unidentified_before = 0
    unidentified_after = 0
    if before_failures is not None and after_failures is not None:
        before_ids = set(before_failures)
        after_ids = set(after_failures)
        common = before_ids & after_ids
        pre_existing = sorted(
            (
                test_id
                for test_id in common
                if before_failures[test_id] is not None
                and after_failures[test_id] is not None
                and before_failures[test_id] == after_failures[test_id]
            ),
            key=str.casefold,
        )
        unverifiable = sorted(
            (
                test_id
                for test_id in common
                if before_failures[test_id] is None
                or after_failures[test_id] is None
            ),
            key=str.casefold,
        )
        changed = sorted(
            common - set(pre_existing) - set(unverifiable), key=str.casefold
        )
        unchanged = list(pre_existing)
        new = sorted(after_ids - before_ids, key=str.casefold)
        resolved = sorted(before_ids - after_ids, key=str.casefold)
        unidentified_before = max(0, (before_count or 0) - len(before_failures))
        unidentified_after = max(0, (after_count or 0) - len(after_failures))
        if unidentified_after > unidentified_before or new or changed:
            classification = "REGRESSION_CANDIDATE"
        elif unidentified_before or unidentified_after or unverifiable:
            classification = "UNKNOWN"
        elif pre_existing:
            classification = "PRE_EXISTING_FAILURE"
        elif resolved:
            classification = "RESOLVED"
        else:
            classification = "UNCHANGED"
    elif before_count is not None and after_count is not None:
        if before_count == 0 and after_count > 0:
            classification = "REGRESSION_CANDIDATE"
        elif before_count > 0 and after_count == 0:
            classification = "RESOLVED"
        elif before_count == 0 and after_count == 0:
            classification = "UNCHANGED"
        else:
            classification = "UNKNOWN"
    else:
        classification = "UNKNOWN"
    return {
        "classification": classification,
        "pre_existing_failures": pre_existing,
        "new_failures": new,
        "resolved_failures": resolved,
        "unchanged_failures": unchanged,
        "changed_failures": changed,
        "unverifiable_failures": unverifiable if before_failures is not None and after_failures is not None else [],
        "before_failed_count": before_count,
        "after_failed_count": after_count,
        "unidentified_before_count": unidentified_before,
        "unidentified_after_count": unidentified_after,
        "skipped": {
            "before_ids": sorted(before_skipped, key=str.casefold),
            "after_ids": sorted(after_skipped, key=str.casefold),
            "before_count": before_skip_count,
            "after_count": after_skip_count,
        },
        "skipped_count": after_skip_count,
        "causality": "NOT_ESTABLISHED",
    }


def change_policy(
    project_lifecycle: str | Mapping[str, Any],
    change_kind: str | None = None,
    *,
    adoption_status: str = "ADOPTED",
    behavior_change: bool = False,
    founder_approved: bool = False,
) -> dict[str, Any]:
    """Return the minimum Brownfield gate for a proposed change (pure policy)."""

    if isinstance(project_lifecycle, Mapping):
        proposal = project_lifecycle
        lifecycle = str(proposal.get("project_lifecycle", "")).upper()
        kind = str(proposal.get("change_kind", change_kind or "")).lower()
        adoption_status = str(proposal.get("adoption_status", adoption_status)).upper()
        behavior_change = bool(proposal.get("behavior_change", behavior_change))
        founder_approved = bool(proposal.get("founder_approved", founder_approved))
    else:
        lifecycle = str(project_lifecycle).upper()
        kind = str(change_kind or "").lower()
    kind = re.sub(r"[\s/\\-]+", "_", kind.strip())
    adoption_status = str(adoption_status).upper()
    read_only_kinds = {"read", "audit", "analyze", "inspect"}
    if lifecycle not in PROJECT_LIFECYCLES:
        return {
            "decision": "REQUIRE_STRATEGIC_GATE",
            "impact_level": "L2",
            "allowed": False,
            "requires_founder_approval": True,
            "behavior_preservation": True,
            "reason": "UNKNOWN_PROJECT_LIFECYCLE",
        }
    if adoption_status not in ADOPTION_STATUSES:
        return {
            "decision": "BLOCKED_INVALID_ADOPTION_STATUS",
            "impact_level": "L0",
            "allowed": False,
            "requires_founder_approval": False,
            "behavior_preservation": True,
            "reason": "UNKNOWN_ADOPTION_STATUS_FAILS_CLOSED",
        }
    if adoption_status in {"READ_ONLY_AUDIT", "BASELINE_READY", "BLOCKED"} and kind not in read_only_kinds:
        return {
            "decision": "BLOCKED_READ_ONLY",
            "impact_level": "L0",
            "allowed": False,
            "requires_founder_approval": False,
            "behavior_preservation": True,
            "reason": (
                "ADOPTION_BLOCKED_FORBIDS_PROJECT_WRITES"
                if adoption_status == "BLOCKED"
                else "ADOPTION_READ_ONLY_FORBIDS_PROJECT_WRITES"
            ),
        }
    reversible_kinds = {
        "bug_fix",
        "characterization_test",
        "docs",
        "documentation",
        "test_fix",
    }
    l3_tokens = {
        "credential",
        "credentials",
        "deploy",
        "deployment",
        "destructive",
        "destructive_cleanup",
        "production",
        "production_config",
        "publish",
        "release",
        "schema_migration",
        "data_migration",
        "schema_data_migration",
        "drop_database",
        "delete_database",
        "production_deployment",
        "production_release",
    }
    l2_tokens = {
        "architecture",
        "breaking_change",
        "major_refactor",
        "migration",
        "modernization",
        "pivot",
        "rewrite",
    }
    if kind in l3_tokens:
        return {
            "decision": "REQUIRE_EXACT_L3_FENCE",
            "impact_level": "L3",
            "allowed": False,
            "requires_founder_approval": True,
            "behavior_preservation": not behavior_change,
            "reason": (
                "HIGH_IMPACT_ACTION: this pure helper cannot consume "
                "proposal-bound, action-scoped L3 authority"
            ),
        }
    if kind in l2_tokens or behavior_change:
        return {
            "decision": "REQUIRE_STRATEGIC_GATE",
            "impact_level": "L2",
            "allowed": False,
            "requires_founder_approval": not founder_approved,
            "behavior_preservation": not behavior_change,
            "reason": "BROWNFIELD_ARCHITECTURE_OR_BEHAVIOR_CHANGE",
        }
    if lifecycle in {"FROZEN", "ARCHIVED"} and kind not in {"read", "audit", "inspect"}:
        return {
            "decision": "REQUIRE_STRATEGIC_GATE",
            "impact_level": "L2",
            "allowed": False,
            "requires_founder_approval": True,
            "behavior_preservation": True,
            "reason": "FROZEN_OR_ARCHIVED_PROJECT",
        }
    if kind not in read_only_kinds | reversible_kinds:
        return {
            "decision": "BLOCKED_UNKNOWN_CHANGE_KIND",
            "impact_level": "UNCLASSIFIED",
            "allowed": False,
            "requires_founder_approval": False,
            "behavior_preservation": True,
            "reason": "UNKNOWN_CHANGE_KIND_REQUIRES_SEMANTIC_IMPACT_CLASSIFICATION",
        }
    return {
        "decision": "ALLOW",
        "impact_level": (
            "L0" if kind in read_only_kinds | {"docs", "documentation"} else "L1"
        ),
        "allowed": True,
        "requires_founder_approval": False,
        "behavior_preservation": True,
        "reason": "REVERSIBLE_BEHAVIOR_PRESERVING_CHANGE",
    }


def validate_adoption_record(
    record: Mapping[str, Any],
    *,
    expected_baseline_sha256: str | None = None,
) -> dict[str, Any]:
    """Validate an explicit Adoption record; never infer or mutate one."""

    errors: list[str] = []
    if not isinstance(record, Mapping):
        return {
            "valid": False,
            "classification": "INVALID",
            "errors": ["record must be an object"],
            "normalized": None,
        }
    origin = record.get("project_origin")
    lifecycle = record.get("project_lifecycle")
    status_value = record.get("adoption_status")
    confidence = record.get("adoption_confidence")
    if origin != "ADOPTED":
        errors.append("project_origin must be ADOPTED")
    if lifecycle not in PROJECT_LIFECYCLES:
        errors.append("project_lifecycle is invalid")
    if status_value not in ADOPTION_STATUSES:
        errors.append("adoption_status is invalid")
    if confidence not in ADOPTION_CONFIDENCE:
        errors.append("adoption_confidence is invalid")
    behavior_preservation = record.get("behavior_preservation")
    if behavior_preservation is not True:
        errors.append("behavior_preservation must be true")
    baseline = record.get("adoption_baseline", record.get("baseline"))
    if not isinstance(baseline, Mapping):
        baseline = {
            "baseline_id": record.get("baseline_id"),
            "baseline_sha256": record.get("baseline_sha256"),
        }
    baseline_id = baseline.get("baseline_id")
    baseline_sha = baseline.get("baseline_sha256")
    if not isinstance(baseline_sha, str) or not re.fullmatch(r"[0-9A-Fa-f]{64}", baseline_sha):
        errors.append("baseline_sha256 must be a SHA-256 value")
        normalized_sha = None
    else:
        normalized_sha = baseline_sha.upper()
        expected_id = f"AB-{normalized_sha[:16]}"
        if baseline_id != expected_id:
            errors.append("baseline_id does not match baseline_sha256")
        if (
            expected_baseline_sha256 is not None
            and normalized_sha != expected_baseline_sha256.upper()
        ):
            errors.append("baseline_sha256 does not match the expected baseline")
    normalized = {
        "project_origin": origin,
        "project_lifecycle": lifecycle,
        "adoption_status": status_value,
        "adoption_confidence": confidence,
        "behavior_preservation": behavior_preservation,
        "adoption_baseline": {
            "baseline_id": baseline_id,
            "baseline_sha256": normalized_sha,
        },
    }
    return {
        "valid": not errors,
        "classification": "VALID" if not errors else "INVALID",
        "errors": errors,
        "normalized": normalized,
    }


class _Collector:
    def __init__(self, root: Path, limits: BaselineLimits):
        self.root = root
        self.limits = limits
        self.pins: list[_DirectoryPin] = []
        self.inventory: list[dict[str, Any]] = []
        self.baseline_rows: list[dict[str, Any]] = []
        self.structural_issues: list[dict[str, str]] = []
        self.opaque_directories: list[str] = []
        self.sensitive_metadata_only: list[str] = []
        self.manifests: list[dict[str, Any]] = []
        self.docs: list[str] = []
        self.documentation_claims: dict[str, set[str]] = {}
        self.build_declarations: list[dict[str, str]] = []
        self.test_declarations: list[dict[str, str]] = []
        self.release_declarations: list[str] = []
        self.deployment_declarations: list[str] = []
        self.todo_signals: list[dict[str, Any]] = []
        self.dependencies: list[dict[str, str]] = []
        self.source_signals: list[str] = []
        self.extension_counts: Counter[str] = Counter()
        self.entry_count = 0
        self.file_count = 0
        self.directory_count = 0
        self.hash_bytes = 0
        self.text_probe_files = 0
        self.text_probe_bytes = 0
        self.partial_reasons: set[str] = set()
        self._casefold_paths: dict[str, str] = {}

    def _issue(self, code: str, relative: str, detail: str) -> None:
        self.structural_issues.append({"code": code, "path": relative, "detail": detail})

    def _record_case_collision(self, relative: str) -> bool:
        folded = relative.casefold()
        other = self._casefold_paths.get(folded)
        if other is not None and other != relative:
            self._issue("CASEFOLD_COLLISION", relative, f"collides with {other}")
            self.partial_reasons.add("CASEFOLD_COLLISION")
            return True
        self._casefold_paths[folded] = relative
        return False

    def _manifest_facts(self, relative: str, content: bytes) -> None:
        name = Path(relative).name.casefold()
        row: dict[str, Any] = {
            "path": relative,
            "kind": name,
            "sha256": _sha256(content),
            "evidence_level": "CONFIRMED",
            "evidence_kind": "STATIC_DECLARATION",
        }
        declared_scripts: list[str] = []
        if name == "package.json":
            try:
                value = _safe_json_loads(content, relative)
                if isinstance(value, dict):
                    for key in ("name", "version", "private", "type"):
                        item = value.get(key)
                        if isinstance(item, (str, bool)):
                            row[key] = item
                    scripts = value.get("scripts")
                    if isinstance(scripts, dict):
                        declared_scripts = sorted(
                            (key for key in scripts if isinstance(key, str)), key=str.casefold
                        )[:256]
                        row["script_names"] = declared_scripts
                    for group in ("dependencies", "devDependencies", "peerDependencies"):
                        values = value.get(group)
                        if isinstance(values, dict):
                            for dependency in sorted(values, key=str.casefold)[:1000]:
                                if isinstance(dependency, str):
                                    self.dependencies.append(
                                        {"path": relative, "kind": group, "name": dependency}
                                    )
            except BaselineError:
                row["parse_state"] = "INVALID_OR_DUPLICATE_KEY_JSON"
        self.manifests.append(row)
        for script_name in declared_scripts:
            folded = script_name.casefold()
            if re.search(r"(?:^|:)(?:build|compile|bundle|package)(?:$|:)", folded):
                self.build_declarations.append(
                    {"path": relative, "kind": f"package-script:{script_name}"}
                )
            if re.search(r"(?:^|:)(?:test|spec|check|lint)(?:$|:)", folded):
                self.test_declarations.append(
                    {"path": relative, "kind": f"package-script:{script_name}"}
                )

    def _probe_text(self, relative: str, content: bytes) -> None:
        if (
            self.text_probe_files >= self.limits.max_text_probe_files
            or self.text_probe_bytes + len(content) > self.limits.max_total_text_probe_bytes
            or len(content) > self.limits.max_text_probe_bytes
        ):
            self.partial_reasons.add("TEXT_PROBE_LIMIT_EXCEEDED")
            return
        self.text_probe_files += 1
        self.text_probe_bytes += len(content)
        text = content.decode("utf-8", errors="replace")
        name = Path(relative).name.casefold()
        if name in DOCUMENT_NAMES or name.startswith("readme"):
            folded = text.casefold()
            token_patterns = {
                "python": r"\bpython\b",
                "nodejs": r"\bnode(?:\.js|js)\b",
                "rust": r"\brust\b",
                "go": r"\bgolang\b|\bgo\s+(?:module|project|application)\b",
                "godot": r"\bgodot\b",
                "unity": r"\bunity\b",
            }
            claims = {
                token for token, pattern in token_patterns.items() if re.search(pattern, folded)
            }
            if claims:
                self.documentation_claims[relative] = claims
        for line_number, line in enumerate(text.splitlines(), start=1):
            for match in TODO_PATTERN.finditer(line):
                if len(self.todo_signals) >= self.limits.max_todo_signals:
                    self.partial_reasons.add("TODO_SIGNAL_LIMIT_EXCEEDED")
                    return
                self.todo_signals.append(
                    {
                        "path": relative,
                        "line": line_number,
                        "marker": match.group(1).upper(),
                        "triage": "UNTRIAGED",
                        "evidence_level": "CONFIRMED",
                        "evidence_kind": "STATIC_MARKER",
                    }
                )

    def _process_file(
        self,
        path: Path,
        relative: str,
        metadata: os.stat_result,
        parent: _DirectoryPin,
    ) -> None:
        self.file_count += 1
        if self.file_count > self.limits.max_files:
            raise BaselineError("FILE_LIMIT_EXCEEDED", str(self.limits.max_files))
        name = path.name.casefold()
        extension = path.suffix.casefold() or "[no-extension]"
        self.extension_counts[extension] += 1
        base_row = {
            "path": relative,
            "kind": "file",
            "size": int(metadata.st_size),
            "mode": stat.S_IMODE(metadata.st_mode),
            "mtime_ns": int(getattr(metadata, "st_mtime_ns", 0)),
            "nlink": int(metadata.st_nlink),
        }
        if metadata.st_nlink != 1:
            row = {**base_row, "content_state": "HARDLINK_METADATA_ONLY"}
            self.inventory.append(row)
            self.baseline_rows.append(row)
            self._issue("HARDLINK", relative, f"link count is {metadata.st_nlink}")
            self.partial_reasons.add("HARDLINK_CONTENT_EXCLUDED")
            return
        if _is_sensitive(relative):
            row = {**base_row, "content_state": "SENSITIVE_METADATA_ONLY"}
            self.inventory.append(row)
            self.baseline_rows.append(row)
            self.sensitive_metadata_only.append(relative)
            return
        if metadata.st_size > self.limits.max_file_hash_bytes:
            row = {**base_row, "content_state": "FILE_LIMIT_METADATA_ONLY"}
            self.inventory.append(row)
            self.baseline_rows.append(row)
            self.partial_reasons.add("FILE_HASH_LIMIT_EXCEEDED")
            return
        if self.hash_bytes + metadata.st_size > self.limits.max_total_hash_bytes:
            row = {**base_row, "content_state": "TOTAL_LIMIT_METADATA_ONLY"}
            self.inventory.append(row)
            self.baseline_rows.append(row)
            self.partial_reasons.add("TOTAL_HASH_LIMIT_EXCEEDED")
            return
        try:
            content = _read_regular_file(
                path,
                metadata,
                parent=parent,
                max_bytes=self.limits.max_file_hash_bytes,
            )
        except BaselineError as exc:
            if exc.conflict:
                raise
            row = {**base_row, "content_state": exc.code}
            self.inventory.append(row)
            self.baseline_rows.append(row)
            self.partial_reasons.add(exc.code)
            return
        self.hash_bytes += len(content)
        row = {**base_row, "content_state": "HASHED", "sha256": _sha256(content)}
        self.inventory.append(row)
        self.baseline_rows.append(row)
        if name in MANIFEST_NAMES or name in LOCKFILE_NAMES or path.suffix.casefold() in {
            ".csproj",
            ".fsproj",
            ".sln",
        }:
            if len(self.manifests) < self.limits.max_manifests and len(content) <= self.limits.max_manifest_bytes:
                self._manifest_facts(relative, content)
            else:
                self.partial_reasons.add("MANIFEST_LIMIT_EXCEEDED")
        if name in DOCUMENT_NAMES or name.startswith("readme"):
            self.docs.append(relative)
        if name in BUILD_DECLARATION_NAMES:
            self.build_declarations.append({"path": relative, "kind": "build-file"})
        if name in TEST_CONFIG_NAMES or "test" in {part.casefold() for part in path.parts[:-1]}:
            self.test_declarations.append({"path": relative, "kind": "test-file-or-config"})
        folded_relative = relative.casefold()
        if any(token in folded_relative for token in ("release", "changelog", "version")):
            self.release_declarations.append(relative)
        if any(
            token in folded_relative
            for token in ("deploy", "docker-compose", ".github/workflows", "helm", "kubernetes")
        ):
            self.deployment_declarations.append(relative)
        if path.suffix.casefold() in SOURCE_EXTENSIONS:
            self.source_signals.append(relative)
        if (path.suffix.casefold() in TEXT_EXTENSIONS or name in MANIFEST_NAMES) and b"\x00" not in content[:4096]:
            self._probe_text(relative, content)

    def walk(self) -> None:
        root_pin = _open_directory_pin(self.root)
        self.pins.append(root_pin)

        def visit(directory: Path, parent_pin: _DirectoryPin, depth: int) -> None:
            parent_pin.assert_current()
            try:
                iterator_source: int | Path = parent_pin.handle if not parent_pin.windows else directory
                with os.scandir(iterator_source) as iterator:
                    names = [entry.name for entry in iterator]
            except OSError as exc:
                raise BaselineError("DIRECTORY_ENUMERATION_FAILED", f"{directory}: {exc}", conflict=True) from exc
            parent_pin.assert_current()
            names.sort(key=lambda value: (value.casefold(), value.encode("utf-8", errors="surrogatepass")))
            for name in names:
                self.entry_count += 1
                if self.entry_count > self.limits.max_total_entries:
                    raise BaselineError("TOTAL_ENTRY_LIMIT_EXCEEDED", str(self.limits.max_total_entries))
                path = directory / name
                relative_path = path.relative_to(self.root)
                relative = relative_path.as_posix()
                if len(relative_path.parts) > self.limits.max_depth:
                    self._issue("DEPTH_LIMIT_EXCEEDED", relative, str(self.limits.max_depth))
                    self.partial_reasons.add("DEPTH_LIMIT_EXCEEDED")
                    continue
                unsafe = next(
                    (
                        f"{part}: {reason}"
                        for part in relative_path.parts
                        if (reason := _unsafe_path_part_reason(part)) is not None
                    ),
                    None,
                )
                if unsafe is not None:
                    self._issue("UNSAFE_PATH_COMPONENT", relative, unsafe)
                    self.partial_reasons.add("UNSAFE_PATH_COMPONENT")
                    continue
                if self._record_case_collision(relative):
                    continue
                try:
                    metadata = (
                        os.stat(name, dir_fd=parent_pin.handle, follow_symlinks=False)
                        if not parent_pin.windows
                        else path.lstat()
                    )
                except OSError as exc:
                    raise BaselineError("ENTRY_LSTAT_FAILED", f"{relative}: {exc}", conflict=True) from exc
                if relative_path.parts[0].casefold() in {".founder", ".git"}:
                    # Control/Git internals are observed by dedicated bounded
                    # readers and intentionally excluded from baseline material.
                    continue
                if stat.S_ISLNK(metadata.st_mode) or _is_reparse(metadata):
                    target: str | None = None
                    if stat.S_ISLNK(metadata.st_mode):
                        try:
                            target = os.readlink(path)
                        except OSError:
                            target = None
                    row = {
                        "path": relative,
                        "kind": "link-or-reparse",
                        "size": int(metadata.st_size),
                        "target": target,
                    }
                    self.inventory.append(row)
                    self.baseline_rows.append(row)
                    self._issue("LINK_OR_REPARSE_SKIPPED", relative, "target was not followed")
                    self.partial_reasons.add(
                        "LINK_OR_REPARSE_SKIPPED"
                        if target is not None
                        else "OPAQUE_REPARSE_TARGET"
                    )
                    continue
                if stat.S_ISDIR(metadata.st_mode):
                    self.directory_count += 1
                    if self.directory_count > self.limits.max_directories:
                        raise BaselineError("DIRECTORY_LIMIT_EXCEEDED", str(self.limits.max_directories))
                    if name.casefold() in OPAQUE_DIRECTORY_NAMES:
                        row = {"path": relative, "kind": "opaque-directory"}
                        self.inventory.append(row)
                        self.baseline_rows.append(row)
                        self.opaque_directories.append(relative)
                        continue
                    pin = _open_directory_pin(path, parent=parent_pin)
                    self.pins.append(pin)
                    row = {"path": relative, "kind": "directory"}
                    self.inventory.append(row)
                    self.baseline_rows.append(row)
                    visit(path, pin, depth + 1)
                    continue
                if not stat.S_ISREG(metadata.st_mode):
                    row = {"path": relative, "kind": "special", "mode": int(metadata.st_mode)}
                    self.inventory.append(row)
                    self.baseline_rows.append(row)
                    self._issue("SPECIAL_FILE_SKIPPED", relative, "content was not opened")
                    self.partial_reasons.add("SPECIAL_FILE_SKIPPED")
                    continue
                self._process_file(path, relative, metadata, parent_pin)

        try:
            visit(self.root, root_pin, 0)
            for pin in self.pins:
                pin.assert_current()
        finally:
            for pin in reversed(self.pins):
                pin.close()
            self.pins = []


def _git_direct_file(git_dir: Path, relative: str, *, max_bytes: int = 4 * 1024 * 1024) -> bytes | None:
    path = git_dir / Path(relative)
    if not path.exists():
        return None
    try:
        return _direct_file_bytes(path, max_bytes=max_bytes, label=f".git/{relative}")
    except BaselineError:
        return None


def _git_config_is_safe(root: Path, git_dir: Path) -> tuple[bool, list[str]]:
    issues: list[str] = []
    for path, label in ((git_dir / "config", ".git/config"), (git_dir / "info/attributes", ".git/info/attributes"), (root / ".gitattributes", ".gitattributes")):
        if not path.exists():
            continue
        try:
            raw = _direct_file_bytes(path, max_bytes=2 * 1024 * 1024, label=label)
        except BaselineError as exc:
            issues.append(exc.code)
            continue
        text = raw.decode("utf-8", errors="replace").casefold()
        patterns = {
            "EXTERNAL_INCLUDE": r"(?m)^\s*\[include(?:if)?\b|^\s*path\s*=",
            "FSMONITOR_CONFIG": r"fsmonitor",
            "EXTERNAL_FILTER": r"(?m)^\s*filter\s*=|\[filter\s+\"",
            "WORKTREE_ENCODING": r"working-tree-encoding",
            "EXTERNAL_DIFF": r"\[diff\s+\"|\btextconv\s*=|\bcommand\s*=",
        }
        for code, pattern in patterns.items():
            if re.search(pattern, text):
                issues.append(code)
    return not issues, sorted(set(issues))


def _git_mutation_observation(git_dir: Path) -> dict[str, tuple[int, ...] | None]:
    result: dict[str, tuple[int, ...] | None] = {}
    for name in ("HEAD", "config", "index", "packed-refs", "index.lock", "shallow"):
        path = git_dir / name
        try:
            result[name] = _metadata_identity(path.lstat()) if os.path.lexists(path) else None
        except OSError:
            result[name] = None
    return result


def _safe_ref_name(value: str) -> bool:
    return bool(
        value.startswith("refs/")
        and ".." not in value
        and not value.endswith(("/", ".", ".lock"))
        and not re.search(r"[\x00-\x20~^:?*\\\[]", value)
    )


def _parse_git_head(git_dir: Path) -> tuple[str | None, str | None, list[str]]:
    issues: list[str] = []
    raw = _git_direct_file(git_dir, "HEAD", max_bytes=4096)
    if raw is None:
        return None, None, ["HEAD_UNAVAILABLE"]
    text = raw.decode("ascii", errors="replace").strip()
    if re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", text):
        return text.lower(), None, issues
    if not text.startswith("ref: "):
        return None, None, ["HEAD_INVALID"]
    ref = text[5:]
    if not _safe_ref_name(ref):
        return None, None, ["HEAD_REF_UNSAFE"]
    branch = ref.removeprefix("refs/heads/") if ref.startswith("refs/heads/") else ref
    ref_raw = _git_direct_file(git_dir, ref, max_bytes=4096)
    commit: str | None = None
    if ref_raw is not None:
        candidate = ref_raw.decode("ascii", errors="replace").strip()
        if re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", candidate):
            commit = candidate.lower()
        else:
            issues.append("HEAD_REF_INVALID")
    else:
        packed = _git_direct_file(git_dir, "packed-refs", max_bytes=4 * 1024 * 1024)
        if packed is not None:
            for line in packed.decode("ascii", errors="replace").splitlines():
                if line.startswith(("#", "^")) or " " not in line:
                    continue
                candidate, packed_ref = line.split(" ", 1)
                if packed_ref == ref and re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", candidate):
                    commit = candidate.lower()
                    break
        if commit is None:
            issues.append("HEAD_REF_UNRESOLVED")
    return commit, branch, issues


def _status_path(record: str) -> tuple[str, str] | None:
    if record.startswith(("? ", "! ")):
        return record[:1], record[2:]
    if record.startswith("1 "):
        parts = record.split(" ", 8)
        return (parts[1] if len(parts) > 1 else "1", parts[8] if len(parts) > 8 else "")
    if record.startswith("2 "):
        parts = record.split(" ", 9)
        return (parts[1] if len(parts) > 1 else "2", parts[9] if len(parts) > 9 else "")
    if record.startswith("u "):
        parts = record.split(" ", 10)
        return (parts[1] if len(parts) > 1 else "u", parts[10] if len(parts) > 10 else "")
    return None


def _is_founder_status_path(path: str) -> bool:
    normalized = path.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    normalized = normalized.lstrip("/")
    return normalized.casefold() == ".founder" or normalized.casefold().startswith(".founder/")


def _git_observation(root: Path, *, mode: str, limits: BaselineLimits) -> dict[str, Any]:
    if mode == "off":
        return {
            "state": "DISABLED",
            "head": None,
            "branch": None,
            "dirty": None,
            "status_state": "UNKNOWN",
            "status_entries": [],
            "status_digest": None,
            "status_entry_count": 0,
            "issues": [],
            "safety": {"optional_locks": False, "hooks": False, "submodules_recurred": False},
        }
    git_dir = root / ".git"
    if not os.path.lexists(git_dir):
        return {
            "state": "ABSENT",
            "head": None,
            "branch": None,
            "dirty": None,
            "status_state": "NOT_APPLICABLE",
            "status_entries": [],
            "status_digest": None,
            "status_entry_count": 0,
            "issues": [],
            "safety": {"optional_locks": False, "hooks": False, "submodules_recurred": False},
        }
    try:
        metadata = git_dir.lstat()
    except OSError as exc:
        return {
            "state": "UNSAFE_LAYOUT",
            "head": None,
            "branch": None,
            "dirty": None,
            "status_state": "UNKNOWN",
            "status_entries": [],
            "status_digest": None,
            "status_entry_count": 0,
            "issues": [str(exc)],
            "safety": {"optional_locks": False, "hooks": False, "submodules_recurred": False},
        }
    if stat.S_ISLNK(metadata.st_mode) or _is_reparse(metadata) or not stat.S_ISDIR(metadata.st_mode):
        return {
            "state": "UNSAFE_LAYOUT",
            "head": None,
            "branch": None,
            "dirty": None,
            "status_state": "UNKNOWN",
            "status_entries": [],
            "status_digest": None,
            "status_entry_count": 0,
            "issues": [".git must be a direct plain directory"],
            "safety": {"optional_locks": False, "hooks": False, "submodules_recurred": False},
        }
    head, branch, issues = _parse_git_head(git_dir)
    config_safe, config_issues = _git_config_is_safe(root, git_dir)
    issues.extend(config_issues)
    dirty: bool | None = None
    status_state = "UNKNOWN"
    status_entries: list[dict[str, str]] = []
    status_digest: str | None = None
    status_executed = False
    executable = shutil.which("git")
    if executable is not None:
        executable_path = Path(executable).resolve(strict=False)
        if _is_within(executable_path, root):
            executable = None
            issues.append("PROJECT_LOCAL_GIT_EXECUTABLE_REJECTED")
    if executable is None:
        issues.append("TRUSTED_GIT_UNAVAILABLE")
    elif not config_safe:
        issues.append("GIT_STATUS_SKIPPED_UNSAFE_CONFIG_OR_ATTRIBUTES")
    else:
        before = _git_mutation_observation(git_dir)
        null_device = "NUL" if os.name == "nt" else "/dev/null"
        command = [
            str(executable),
            "--no-pager",
            "--no-optional-locks",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.untrackedCache=false",
            "-c",
            f"core.hooksPath={null_device}",
            "-c",
            f"core.attributesFile={null_device}",
            "-c",
            "submodule.recurse=false",
            f"--git-dir={git_dir}",
            f"--work-tree={root}",
            "status",
            "--porcelain=v2",
            "--branch",
            "--no-ahead-behind",
            "-z",
            "--untracked-files=all",
            "--ignore-submodules=all",
        ]
        environment = {
            "PATH": os.environ.get("PATH", ""),
            "SYSTEMROOT": os.environ.get("SYSTEMROOT", "") if os.name == "nt" else "",
            "WINDIR": os.environ.get("WINDIR", "") if os.name == "nt" else "",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": null_device,
            "GIT_CONFIG_GLOBAL": null_device,
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_PAGER": "cat",
            "PAGER": "cat",
            "GIT_LFS_SKIP_SMUDGE": "1",
            "GCM_INTERACTIVE": "Never",
            "LC_ALL": "C",
            "LANG": "C",
        }
        try:
            completed = subprocess.run(
                command,
                cwd=str(root),
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                timeout=limits.git_timeout_seconds,
                check=False,
            )
            status_executed = True
            after = _git_mutation_observation(git_dir)
            if before != after:
                issues.append("GIT_METADATA_CHANGED_DURING_READ_ONLY_STATUS")
            elif completed.returncode != 0:
                issues.append(f"GIT_STATUS_EXIT_{completed.returncode}")
            elif len(completed.stdout) > limits.max_git_output_bytes:
                issues.append("GIT_STATUS_OUTPUT_LIMIT_EXCEEDED")
            else:
                records = completed.stdout.decode("utf-8", errors="replace").split("\0")
                entries: list[dict[str, str]] = []
                skip_next_original = False
                for record in records:
                    if not record:
                        continue
                    if record.startswith("# "):
                        continue
                    if skip_next_original:
                        skip_next_original = False
                        continue
                    parsed = _status_path(record)
                    if parsed is None:
                        continue
                    status_code, path = parsed
                    if record.startswith("2 "):
                        skip_next_original = True
                    if not path or _is_founder_status_path(path):
                        continue
                    entries.append({"status": status_code, "path": path.replace("\\", "/")})
                entries.sort(key=lambda row: (row["path"].casefold(), row["status"]))
                digest = _sha256(_canonical_json_bytes(entries))
                dirty = bool(entries)
                status_state = "DIRTY" if entries else "CLEAN"
                status_entries = entries[:10_000]
                status_digest = digest
        except (OSError, subprocess.TimeoutExpired) as exc:
            issues.append(f"GIT_STATUS_UNAVAILABLE:{type(exc).__name__}")
    return {
        "state": "DIRECT_REPOSITORY",
        "head": head,
        "branch": branch,
        "dirty": dirty,
        "status_state": status_state,
        "status_entries": status_entries,
        "status_entry_count": len(status_entries),
        "status_digest": status_digest,
        "founder_paths_filtered": True,
        "issues": sorted(set(issues)),
        "safety": {
            "optional_locks": False,
            "hooks": False,
            "submodules_recurred": False,
            "status_command_executed": status_executed,
            "project_local_git_rejected": True,
        },
    }


def assess_evidence_conflicts(
    documentation_claims: Mapping[str, Iterable[str]],
    observed_technologies: Iterable[str],
) -> list[dict[str, Any]]:
    """Report only deterministic ecosystem-token mismatches, not semantic truth."""

    observed = {str(item).casefold() for item in observed_technologies}
    conflicts: list[dict[str, Any]] = []
    if not observed:
        return conflicts
    for path in sorted(documentation_claims, key=str.casefold):
        claims = {str(item).casefold() for item in documentation_claims[path]}
        for claim in sorted(claims - observed):
            conflicts.append(
                {
                    "code": "DOCUMENTATION_DRIFT",
                    "path": path,
                    "declared_ecosystem": claim,
                    "observed_ecosystems": sorted(observed),
                    "assessment": "REVIEW_REQUIRED",
                    "evidence_kind": "STATIC_TOKEN_CONFLICT",
                }
            )
    return conflicts


def _technology_signals(collector: _Collector) -> list[str]:
    names = {Path(row["path"]).name.casefold() for row in collector.manifests}
    extensions = set(collector.extension_counts)
    signals: set[str] = set()
    if names & {"pyproject.toml", "requirements.txt", "setup.py", "setup.cfg"} or ".py" in extensions:
        signals.add("python")
    if "package.json" in names or extensions & {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"}:
        signals.add("nodejs")
    if "cargo.toml" in names or ".rs" in extensions:
        signals.add("rust")
    if "go.mod" in names or ".go" in extensions:
        signals.add("go")
    if "project.godot" in names or ".gd" in extensions:
        signals.add("godot")
    if ".csproj" in extensions or ".unity" in extensions:
        signals.add("unity")
    return sorted(signals)


def inspect_project(
    project: str | os.PathLike[str],
    *,
    git_mode: str = "safe",
    limits: BaselineLimits | Mapping[str, int] | None = None,
) -> dict[str, Any]:
    """Collect a deterministic Adoption baseline without changing the project."""

    if git_mode not in {"safe", "off"}:
        raise BaselineError("INVALID_GIT_MODE", str(git_mode))
    if limits is None:
        normalized_limits = BaselineLimits()
    elif isinstance(limits, BaselineLimits):
        normalized_limits = limits
    elif isinstance(limits, Mapping):
        normalized_limits = BaselineLimits(**dict(limits))
    else:
        raise BaselineError("INVALID_LIMITS", "limits must be BaselineLimits or an object")
    normalized_limits.validate()
    root = _resolve_safe_root(project)
    root_before = _metadata_identity(root.lstat())
    founder_state = classify_founder_state(root)
    collector = _Collector(root, normalized_limits)
    collector.walk()
    git = _git_observation(root, mode=git_mode, limits=normalized_limits)
    git_anchor_issues: list[str] = []
    if os.path.lexists(root / ".git"):
        if git.get("state") != "DIRECT_REPOSITORY":
            git_anchor_issues.append("GIT_BASELINE_UNAVAILABLE")
        if git.get("status_state") not in {"CLEAN", "DIRTY"}:
            git_anchor_issues.append("GIT_STATUS_UNAVAILABLE")
        if "GIT_METADATA_CHANGED_DURING_READ_ONLY_STATUS" in git.get("issues", []):
            git_anchor_issues.append("GIT_BASELINE_UNSTABLE")
        collector.partial_reasons.update(git_anchor_issues)
    root_after = _metadata_identity(root.lstat())
    if root_before != root_after:
        raise BaselineError(
            "PROJECT_CHANGED_DURING_SCAN",
            "root directory identity or metadata changed",
            conflict=True,
        )

    collector.inventory.sort(key=lambda row: (row["path"].casefold(), row["path"]))
    collector.baseline_rows.sort(key=lambda row: (row["path"].casefold(), row["path"]))
    collector.structural_issues.sort(key=lambda row: (row["path"].casefold(), row["code"]))
    manifests = sorted(collector.manifests, key=lambda row: row["path"].casefold())
    git_material = {
        "state": git["state"],
        "head": git["head"],
        "branch": git["branch"],
        "dirty_state": git.get("status_state"),
        "dirty_digest": git.get("status_digest"),
    }
    baseline_material = {
        "algorithm": "sha256-project-observation-v1",
        "project_binding_id": _project_binding_id(root),
        "filesystem": collector.baseline_rows,
        "git": git_material,
    }
    baseline_sha = _sha256(_canonical_json_bytes(baseline_material))
    baseline_id = f"AB-{baseline_sha[:16]}"
    non_control_count = len(collector.inventory)
    evident_existing = bool(non_control_count or git.get("head"))
    partial_reasons = sorted(collector.partial_reasons)
    anchor_blocking_reasons = sorted(
        set(partial_reasons) - BASELINE_ANCHOR_NONBLOCKING_LIMITATIONS
    )
    baseline_anchor_usable = not anchor_blocking_reasons
    result = "PARTIAL" if partial_reasons else "COMPLETE"
    build_declared = bool(collector.build_declarations or manifests)
    test_declared = bool(collector.test_declarations)
    technology_signals = _technology_signals(collector)
    evidence_conflicts = assess_evidence_conflicts(
        collector.documentation_claims, technology_signals
    )
    return {
        "schema": SCHEMA,
        "collector_version": COLLECTOR_VERSION,
        "result": result,
        "audit_mode": "ADOPTION_READ_ONLY",
        "candidate_treatment": "PROJECT_DATA",
        "baseline_id": baseline_id,
        "baseline_sha256": baseline_sha,
        "baseline_algorithm": "sha256-project-observation-v1",
        "project_root": {
            "requested": str(project),
            "canonical": str(root),
            "binding_id": _project_binding_id(root),
            "identity": {
                "device": root_before[0],
                "inode": root_before[1],
                "mode": root_before[2],
            },
        },
        "execution_facts": {
            "project_code_executed": False,
            "project_imported_or_sourced": False,
            "build_run": False,
            "tests_run": False,
            "dependencies_installed": False,
            "git_hooks_run": False,
            "git_read_only_status_executed": bool(git["safety"].get("status_command_executed")),
            "network_access_performed": False,
            "project_write_attempted": False,
            "credentials_accessed": False,
            "credentials_emitted": False,
        },
        "resource_limits": asdict(normalized_limits),
        "resource_usage": {
            "total_entries": collector.entry_count,
            "files": collector.file_count,
            "directories": collector.directory_count,
            "hashed_bytes": collector.hash_bytes,
            "text_probe_files": collector.text_probe_files,
            "text_probe_bytes": collector.text_probe_bytes,
        },
        "completeness": {
            "inventory": not partial_reasons,
            "audit_coverage_complete": not partial_reasons,
            "baseline_anchor_usable": baseline_anchor_usable,
            # Compatibility alias: baseline usability now means the CAS/drift
            # anchor, not complete semantic marker coverage.
            "baseline_usable": baseline_anchor_usable,
            "reasons": partial_reasons,
            "limitations": partial_reasons,
            "anchor_blocking_reasons": anchor_blocking_reasons,
            "anchor_nonblocking_limitations": sorted(
                set(partial_reasons) & BASELINE_ANCHOR_NONBLOCKING_LIMITATIONS
            ),
            "git_anchor_issues": sorted(set(git_anchor_issues)),
            "opaque_directories_are_declared_exclusions": True,
        },
        "filesystem": {
            "inventory": collector.inventory,
            "snapshot_sha256": _sha256(_canonical_json_bytes(collector.baseline_rows)),
            "structural_issues": collector.structural_issues,
            "opaque_directories": sorted(collector.opaque_directories, key=str.casefold),
            "sensitive_metadata_only": sorted(collector.sensitive_metadata_only, key=str.casefold),
            "extension_counts": dict(sorted(collector.extension_counts.items())),
            "control_contents_excluded": [".founder/**", ".git/**"],
        },
        "founder_state": founder_state,
        "entry_signals": {
            "evident_existing": evident_existing,
            "non_control_entry_count": non_control_count,
            "source_files": sorted(set(collector.source_signals), key=str.casefold),
            "test_declarations": sorted(
                collector.test_declarations,
                key=lambda row: (row["path"].casefold(), row["kind"]),
            ),
            "release_declarations": sorted(set(collector.release_declarations), key=str.casefold),
            "deployment_declarations": sorted(
                set(collector.deployment_declarations), key=str.casefold
            ),
            "lifecycle": "NOT_DETERMINED_BY_COLLECTOR",
        },
        "capability_profile": {
            "state": "STATIC_SIGNALS_ONLY",
            "capabilities": technology_signals,
            "skill_acquisition": "NOT_REQUESTED",
        },
        "evidence_conflicts": evidence_conflicts,
        "markers": {
            "manifests": manifests,
            "documentation": sorted(set(collector.docs), key=str.casefold),
            "build_declarations": sorted(
                collector.build_declarations,
                key=lambda row: (row["path"].casefold(), row["kind"]),
            ),
            "test_declarations": sorted(
                collector.test_declarations,
                key=lambda row: (row["path"].casefold(), row["kind"]),
            ),
            "release_declarations": sorted(set(collector.release_declarations), key=str.casefold),
            "deployment_declarations": sorted(
                set(collector.deployment_declarations), key=str.casefold
            ),
            "todo_signals": sorted(
                collector.todo_signals,
                key=lambda row: (row["path"].casefold(), row["line"], row["marker"]),
            ),
            "dependency_declarations": sorted(
                collector.dependencies,
                key=lambda row: (row["path"].casefold(), row["kind"], row["name"].casefold()),
            ),
        },
        "declarations": {
            "build": {
                "state": "DECLARED_NOT_RUN" if build_declared else "NOT_OBSERVED",
                "actual_result": "UNKNOWN",
            },
            "test": {
                "state": "DECLARED_NOT_RUN" if test_declared else "NOT_OBSERVED",
                "actual_result": "UNKNOWN",
            },
        },
        "git": git,
        "model_handoff": {
            "must_determine": [
                "project purpose and current users",
                "primary features and architecture",
                "NEW/EXISTING_ACTIVE/COMPLETED/SHIPPED lifecycle",
                "TODO relevance and technical-debt priority",
                "documentation drift from cross-source evidence",
            ],
            "must_not_infer": [
                "historical rationale without direct evidence",
                "build or test pass state from declarations",
                "release status from filenames alone",
                "product intent from code alone",
            ],
        },
        "consistency": {
            "state": "RACE_NOT_OBSERVED",
            "baseline_usable": baseline_anchor_usable,
            "baseline_anchor_usable": baseline_anchor_usable,
        },
        "changed_paths": [],
    }


def _limits_from_args(args: argparse.Namespace) -> BaselineLimits:
    return BaselineLimits(
        max_files=args.max_files,
        max_directories=args.max_directories,
        max_total_entries=args.max_total_entries,
        max_depth=args.max_depth,
        max_total_hash_bytes=args.max_total_hash_bytes,
        max_file_hash_bytes=args.max_file_hash_bytes,
        max_manifests=args.max_manifests,
        max_manifest_bytes=args.max_manifest_bytes,
        max_text_probe_files=args.max_text_probe_files,
        max_text_probe_bytes=args.max_text_probe_bytes,
        max_total_text_probe_bytes=args.max_total_text_probe_bytes,
        max_todo_signals=args.max_todo_signals,
        max_git_output_bytes=args.max_git_output_bytes,
        git_timeout_seconds=args.git_timeout_seconds,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="FounderOS read-only existing-project baseline")
    subparsers = parser.add_subparsers(dest="command", required=True)
    inspect = subparsers.add_parser("inspect", help="inspect one absolute project root")
    inspect.add_argument("--project", required=True)
    inspect.add_argument("--git-mode", choices=("safe", "off"), default="safe")
    defaults = BaselineLimits()
    for name, value in asdict(defaults).items():
        inspect.add_argument(f"--{name.replace('_', '-')}", type=int, default=value)
    return parser


def _emit(payload: Mapping[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command != "inspect":
            raise BaselineError("UNKNOWN_COMMAND", str(args.command))
        payload = inspect_project(
            args.project,
            git_mode=args.git_mode,
            limits=_limits_from_args(args),
        )
        _emit(payload)
        return 0
    except BaselineError as exc:
        _emit(
            {
                "schema": SCHEMA,
                "collector_version": COLLECTOR_VERSION,
                "result": "REJECTED",
                "error": {"code": exc.code, "detail": exc.detail},
                "changed_paths": [],
            }
        )
        return EXIT_CONFLICT if exc.conflict else EXIT_INVALID


if __name__ == "__main__":
    raise SystemExit(main())

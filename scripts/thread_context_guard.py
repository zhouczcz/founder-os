#!/usr/bin/env python3
"""Read-only, fail-closed context-size preflight for Codex Thread transcripts.

The helper locates a local session by runtime Thread ID or accepts an explicit
JSONL path.  It never parses JSON, decodes Base64, calls a Codex runtime tool,
or mutates the transcript.  Hard/soft total-size limits are decided from file
metadata alone; only smaller files are streamed to measure record boundaries
and media markers.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO

sys.dont_write_bytecode = True


SCHEMA = "founder-os-thread-context-guard/v1"
SOFT_LIMIT_BYTES = 64 * 1024 * 1024
HARD_LIMIT_BYTES = 128 * 1024 * 1024
MAX_RECORD_BYTES = 8 * 1024 * 1024
SCAN_CHUNK_BYTES = 1024 * 1024

EXIT_CLEAR = 0
EXIT_INVALID = 2
EXIT_ROTATE_REQUIRED = 10
EXIT_CONTEXT_HAZARD = 20
EXIT_UNVERIFIED = 30

THREAD_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
MEDIA_MARKERS = (
    b"data:image/",
    b";base64,",
    b'"image_url":"data:',
    b'"type":"input_image"',
    b'"type":"output_image"',
)


class ContextGuardError(ValueError):
    """Transcript identity or caller input cannot be safely verified."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _absolute_path(value: str | os.PathLike[str], label: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise ContextGuardError(f"{label} must be an absolute path")
    return Path(os.path.abspath(os.fspath(path)))


def _is_reparse(metadata: os.stat_result) -> bool:
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(metadata.st_mode) or bool(attributes & reparse_flag)


def _verify_ancestor_directories(path: Path) -> None:
    parts = path.parts
    if not parts:
        raise ContextGuardError("Transcript path is empty")
    current = Path(parts[0])
    for part in parts[1:-1]:
        current /= part
        try:
            metadata = current.lstat()
        except OSError as exc:
            raise ContextGuardError(f"Cannot inspect transcript ancestor: {current}") from exc
        if _is_reparse(metadata) or not stat.S_ISDIR(metadata.st_mode):
            raise ContextGuardError(
                f"Transcript ancestors must be direct directories: {current}"
            )


def _session_identity(path: Path) -> os.stat_result:
    _verify_ancestor_directories(path)
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ContextGuardError(f"Cannot inspect transcript: {path}") from exc
    if (
        _is_reparse(metadata)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
    ):
        raise ContextGuardError(
            "Transcript must be a direct single-link regular file"
        )
    if path.suffix.casefold() != ".jsonl":
        raise ContextGuardError("Transcript must use the .jsonl suffix")
    return metadata


def _same_identity(left: os.stat_result, right: os.stat_result) -> bool:
    fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_nlink")
    return all(getattr(left, field, None) == getattr(right, field, None) for field in fields)


def _open_pinned(path: Path, expected: os.stat_result) -> BinaryIO:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if nofollow:
        flags |= nofollow
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ContextGuardError(f"Cannot open transcript read-only: {path}") from exc
    try:
        observed = os.fstat(descriptor)
        if (
            not stat.S_ISREG(observed.st_mode)
            or observed.st_nlink != 1
            or not _same_identity(expected, observed)
        ):
            raise ContextGuardError("Transcript identity changed before the scan")
        return os.fdopen(descriptor, "rb", closefd=True)
    except Exception:
        os.close(descriptor)
        raise


def _count_markers(chunk: bytes, overlap: bytes) -> tuple[int, bytes]:
    combined = overlap + chunk
    boundary = len(overlap)
    count = 0
    for marker in MEDIA_MARKERS:
        start = 0
        while True:
            found = combined.find(marker, start)
            if found < 0:
                break
            if found + len(marker) > boundary:
                count += 1
            start = found + 1
    overlap_size = max(len(marker) for marker in MEDIA_MARKERS) - 1
    return count, combined[-overlap_size:]


def _stream_record_metrics(
    path: Path,
    expected: os.stat_result,
    *,
    max_record_bytes: int,
    chunk_bytes: int,
) -> dict[str, Any]:
    scanned_bytes = 0
    record_count = 0
    maximum = 0
    current = 0
    media_markers = 0
    overlap = b""
    complete = True

    with _open_pinned(path, expected) as handle:
        while True:
            chunk = handle.read(chunk_bytes)
            if not chunk:
                break
            scanned_bytes += len(chunk)
            found, overlap = _count_markers(chunk, overlap)
            media_markers += found
            parts = chunk.split(b"\n")
            if len(parts) == 1:
                current += len(parts[0])
                maximum = max(maximum, current)
            else:
                current += len(parts[0])
                maximum = max(maximum, current)
                record_count += 1
                for part in parts[1:-1]:
                    maximum = max(maximum, len(part))
                    record_count += 1
                current = len(parts[-1])
                maximum = max(maximum, current)
            if maximum >= max_record_bytes:
                complete = False
                break

        if complete and current:
            record_count += 1

        pinned_after = os.fstat(handle.fileno())
        if not _same_identity(expected, pinned_after):
            raise ContextGuardError("Transcript changed while it was being scanned")

    try:
        path_after = path.lstat()
    except OSError as exc:
        raise ContextGuardError("Transcript disappeared after the scan") from exc
    if not _same_identity(expected, path_after):
        raise ContextGuardError("Transcript path changed while it was being scanned")

    return {
        "complete_scan": complete,
        "scanned_bytes": scanned_bytes,
        "record_count": record_count if complete else None,
        "max_record_bytes": maximum,
        "media_marker_count": media_markers,
    }


def _runtime_policy(result: str) -> dict[str, Any]:
    compact_only = {
        "list_or_wait": "COMPACT_METADATA_ONLY_IF_BODY_LOAD_IS_NOT_REQUIRED",
        "read_thread": "BLOCK",
        "send_or_continue": "BLOCK",
        "resume": "BLOCK",
        "fork": "BLOCK",
        "open_or_navigate": "BLOCK",
    }
    if result == "CLEAR":
        return {
            "list_or_wait": "ALLOW_COMPACT",
            "read_thread": "BOUNDED_ONLY",
            "read_thread_limits": {
                "turn_limit_max": 3,
                "include_outputs": False,
                "max_output_chars_per_item": 4096,
            },
            "send_or_continue": "ALLOW_AFTER_NORMAL_FENCES",
            "resume": "ALLOW_AFTER_NORMAL_FENCES_AND_RECHECK",
            "fork": "EXISTING_READONLY_FORK_POLICY_ONLY",
            "open_or_navigate": "ALLOW_AFTER_RECHECK",
        }
    return compact_only


def _next_action(result: str) -> str:
    return {
        "CLEAR": "USE_BOUNDED_RUNTIME_ACCESS_AND_RECHECK_BEFORE_THE_NEXT_OPERATION",
        "ROTATE_REQUIRED": "START_ROLE_APPROPRIATE_LIGHTWEIGHT_HANDOFF_WITHOUT_COPYING_HISTORY",
        "CONTEXT_HAZARD": "BUILD_ROLE_APPROPRIATE_HANDOFF_FROM_CANONICAL_STATE_WITHOUT_THREAD_BODY",
        "UNVERIFIED": "LOCATE_ONE_DIRECT_TRANSCRIPT_OR_USE_ROLE_APPROPRIATE_HANDOFF_WITHOUT_BODY",
    }[result]


def _base_payload(
    *,
    result: str,
    reason: str,
    path: Path | None,
    thread_id: str | None,
) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "result": result,
        "reason": reason,
        "observed_at": _utc_now(),
        "thread_id": thread_id,
        "transcript_path": str(path) if path is not None else None,
        "thresholds": {
            "soft_limit_bytes": SOFT_LIMIT_BYTES,
            "hard_limit_bytes": HARD_LIMIT_BYTES,
            "max_record_bytes": MAX_RECORD_BYTES,
        },
        "runtime_policy": _runtime_policy(result),
        "next_action": _next_action(result),
        "changed_paths": [],
        "json_parsed": False,
        "base64_decoded": False,
    }


def inspect_session(
    session_path: str | os.PathLike[str],
    *,
    thread_id: str | None = None,
    soft_limit_bytes: int = SOFT_LIMIT_BYTES,
    hard_limit_bytes: int = HARD_LIMIT_BYTES,
    max_record_bytes: int = MAX_RECORD_BYTES,
    chunk_bytes: int = SCAN_CHUNK_BYTES,
) -> dict[str, Any]:
    """Inspect one direct transcript without deserializing any JSON record."""

    if not (
        isinstance(soft_limit_bytes, int)
        and isinstance(hard_limit_bytes, int)
        and isinstance(max_record_bytes, int)
        and isinstance(chunk_bytes, int)
        and 0 < max_record_bytes <= soft_limit_bytes < hard_limit_bytes
        and chunk_bytes > 0
    ):
        raise ContextGuardError("Context guard thresholds are invalid")
    path = _absolute_path(session_path, "session_path")
    try:
        metadata = _session_identity(path)
    except ContextGuardError as exc:
        payload = _base_payload(
            result="UNVERIFIED",
            reason=str(exc),
            path=path,
            thread_id=thread_id,
        )
        payload["thresholds"] = {
            "soft_limit_bytes": soft_limit_bytes,
            "hard_limit_bytes": hard_limit_bytes,
            "max_record_bytes": max_record_bytes,
        }
        payload["inspection_method"] = "IDENTITY_CHECK_ONLY"
        payload["metrics"] = None
        return payload

    metrics: dict[str, Any] = {
        "session_bytes": metadata.st_size,
        "mtime_ns": metadata.st_mtime_ns,
        "record_count": None,
        "max_record_bytes": None,
        "media_marker_count": None,
        "scanned_bytes": 0,
        "complete_scan": False,
    }
    if metadata.st_size >= hard_limit_bytes:
        result = "CONTEXT_HAZARD"
        reason = "TOTAL_HARD_LIMIT_REACHED"
        method = "STAT_ONLY_HARD_STOP"
    elif metadata.st_size >= soft_limit_bytes:
        result = "ROTATE_REQUIRED"
        reason = "TOTAL_SOFT_LIMIT_REACHED"
        method = "STAT_ONLY_SOFT_STOP"
    else:
        try:
            streamed = _stream_record_metrics(
                path,
                metadata,
                max_record_bytes=max_record_bytes,
                chunk_bytes=chunk_bytes,
            )
        except ContextGuardError as exc:
            payload = _base_payload(
                result="UNVERIFIED",
                reason=str(exc),
                path=path,
                thread_id=thread_id,
            )
            payload["thresholds"] = {
                "soft_limit_bytes": soft_limit_bytes,
                "hard_limit_bytes": hard_limit_bytes,
                "max_record_bytes": max_record_bytes,
            }
            payload["inspection_method"] = "STREAMING_BOUNDARY_SCAN_FAILED"
            payload["metrics"] = metrics
            return payload
        metrics.update(streamed)
        if streamed["max_record_bytes"] >= max_record_bytes:
            result = "CONTEXT_HAZARD"
            reason = "MAX_RECORD_LIMIT_REACHED"
            method = "EARLY_STOP_STREAMING_BOUNDARY_SCAN"
        else:
            result = "CLEAR"
            reason = "BELOW_CONTEXT_GUARD_LIMITS"
            method = "COMPLETE_STREAMING_BOUNDARY_SCAN"

    payload = _base_payload(
        result=result,
        reason=reason,
        path=path,
        thread_id=thread_id,
    )
    payload["thresholds"] = {
        "soft_limit_bytes": soft_limit_bytes,
        "hard_limit_bytes": hard_limit_bytes,
        "max_record_bytes": max_record_bytes,
    }
    payload["inspection_method"] = method
    payload["metrics"] = metrics
    return payload


def _direct_directory(path: Path, label: str) -> None:
    _verify_ancestor_directories(path / "sentinel")
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ContextGuardError(f"Cannot inspect {label}: {path}") from exc
    if _is_reparse(metadata) or not stat.S_ISDIR(metadata.st_mode):
        raise ContextGuardError(f"{label} must be a direct directory")


def locate_sessions(thread_id: str, codex_home: str | os.PathLike[str]) -> list[Path]:
    """Locate transcripts by filename only; never search transcript contents."""

    if not isinstance(thread_id, str) or not THREAD_ID_PATTERN.fullmatch(thread_id):
        raise ContextGuardError("thread_id must be a bounded safe identifier")
    home = _absolute_path(codex_home, "codex_home")
    _direct_directory(home, "codex_home")
    expected_names = {
        f"{thread_id}.jsonl".casefold(),
    }
    expected_suffix = f"-{thread_id}.jsonl".casefold()
    matches: list[Path] = []
    for root_name in ("sessions", "archived_sessions"):
        root = home / root_name
        if not root.exists():
            continue
        _direct_directory(root, root_name)
        pending = [root]
        while pending:
            directory = pending.pop()
            try:
                entries = list(os.scandir(directory))
            except OSError as exc:
                raise ContextGuardError(
                    f"Cannot enumerate transcript directory: {directory}"
                ) from exc
            for entry in entries:
                path = Path(entry.path)
                try:
                    metadata = entry.stat(follow_symlinks=False)
                except OSError as exc:
                    raise ContextGuardError(f"Cannot inspect transcript entry: {path}") from exc
                if _is_reparse(metadata):
                    continue
                if stat.S_ISDIR(metadata.st_mode):
                    pending.append(path)
                elif stat.S_ISREG(metadata.st_mode):
                    name = entry.name.casefold()
                    if name in expected_names or name.endswith(expected_suffix):
                        matches.append(path)
    return sorted(matches, key=lambda item: str(item).casefold())


def inspect_thread(
    *,
    thread_id: str,
    codex_home: str | os.PathLike[str],
) -> dict[str, Any]:
    """Locate exactly one transcript and apply the context guard."""

    try:
        matches = locate_sessions(thread_id, codex_home)
    except ContextGuardError as exc:
        payload = _base_payload(
            result="UNVERIFIED",
            reason=str(exc),
            path=None,
            thread_id=thread_id,
        )
        payload["inspection_method"] = "FILENAME_ONLY_LOCATOR_FAILED"
        payload["metrics"] = None
        return payload
    if len(matches) != 1:
        reason = (
            "TRANSCRIPT_NOT_FOUND"
            if not matches
            else "MULTIPLE_TRANSCRIPTS_REQUIRE_EXPLICIT_SESSION_PATH"
        )
        payload = _base_payload(
            result="UNVERIFIED",
            reason=reason,
            path=None,
            thread_id=thread_id,
        )
        payload["inspection_method"] = "FILENAME_ONLY_LOCATOR"
        payload["metrics"] = {"candidate_count": len(matches)}
        return payload
    return inspect_session(matches[0], thread_id=thread_id)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    inspect_parser = subparsers.add_parser("inspect")
    target = inspect_parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--session")
    target.add_argument("--thread-id")
    inspect_parser.add_argument("--codex-home")
    return parser


def _exit_code(result: str) -> int:
    return {
        "CLEAR": EXIT_CLEAR,
        "ROTATE_REQUIRED": EXIT_ROTATE_REQUIRED,
        "CONTEXT_HAZARD": EXIT_CONTEXT_HAZARD,
        "UNVERIFIED": EXIT_UNVERIFIED,
    }[result]


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        if arguments.session is not None:
            if arguments.codex_home is not None:
                raise ContextGuardError("--codex-home is only valid with --thread-id")
            payload = inspect_session(arguments.session)
        else:
            codex_home = arguments.codex_home or os.environ.get("CODEX_HOME")
            if not codex_home:
                codex_home = str(Path.home() / ".codex")
            payload = inspect_thread(
                thread_id=arguments.thread_id,
                codex_home=codex_home,
            )
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return _exit_code(payload["result"])
    except ContextGuardError as exc:
        print(
            json.dumps(
                {
                    "schema": SCHEMA,
                    "result": "INVALID_CONTEXT_GUARD_REQUEST",
                    "reason": str(exc),
                    "changed_paths": [],
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return EXIT_INVALID


if __name__ == "__main__":
    raise SystemExit(main())

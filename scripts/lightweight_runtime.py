#!/usr/bin/env python3
"""Deterministic policy engine for the FounderOS V4.1 lightweight path.

VALIDATION-ONLY: this module is exercised exclusively by the regression suite
(`validate_founder_os.py`) to pin the lightweight-path contract described in
`SKILL.md` and `references/lightweight-worker-runtime.md`.  The live supervisor
follows those documents directly and never invokes this module; when editing
the lightweight rules, update the documents and this engine together so the
suite keeps them in sync.

The engine plans and validates supervisor behavior.  It never creates an Agent,
reads a repository, writes project state, or calls a model.  Runtime effects stay
with the current Codex supervisor; callers feed back observed IDs and evidence.
The governed V1-V4 control plane remains in the existing helpers.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence


sys.dont_write_bytecode = True


class PolicyError(ValueError):
    """An explicit lightweight contract was violated."""


class WorkflowProfile(str, Enum):
    LIGHT = "V4_LIGHT"
    GOVERNED = "V4_GOVERNED"


LIGHT_MODE = WorkflowProfile.LIGHT
GOVERNED_MODE = WorkflowProfile.GOVERNED


class RequestType(str, Enum):
    PROJECT_IDEA = "PROJECT_IDEA"
    FEATURE_IDEA = "FEATURE_IDEA"
    BUG_REPORT = "BUG_REPORT"
    MAINTENANCE = "MAINTENANCE"
    QUESTION_OR_STATUS = "QUESTION_OR_STATUS"


class FitLevel(str, Enum):
    CONTINUATION = "F0_CONTINUATION"
    LOCAL = "F1_LOCAL_FIT"
    PLAN_DELTA = "F2_PLAN_DELTA"
    PROJECT_RESET = "F3_PROJECT_RESET"
    UNKNOWN = "UNKNOWN"


TASK_PACKET_FIELDS = (
    "OBJECTIVE",
    "PROJECT_CONTEXT",
    "CHOSEN_APPROACH",
    "CONTEXT_REFS",
    "READ_WRITE_SCOPE",
    "DELIVERABLES",
    "ACCEPTANCE_AND_TESTS",
    "STOP_OR_ESCALATE_WHEN",
)

WORKER_RESULT_FIELDS = (
    "RESULT",
    "CHANGED_PATHS",
    "VALIDATION_COMMANDS",
    "VALIDATION_RESULT",
    "RISKS_OR_BLOCKERS",
    "DECISION_NEEDED",
)

REQUIRED_THREAD_CAPABILITIES = (
    "create_thread",
    "send_message_to_thread",
    "wait_threads",
    "read_thread",
)

FORBIDDEN_PACKET_CONTEXT_KINDS = frozenset(
    {
        "full_chat",
        "all_ledgers",
        "founder_os_advanced_protocol",
        "full_log",
        "full_diff",
        "inline_image",
    }
)

OUTPUT_ARTIFACT_THRESHOLD = 4 * 1024
STATUS_TARGET_BYTES = 4 * 1024
TASK_PACKET_TARGET_MIN_BYTES = 2 * 1024
TASK_PACKET_TARGET_MAX_BYTES = 4 * 1024
INITIAL_CONTEXT_RECOMMENDED_MAX_TOKENS = 12_000
MAX_REWORK_ROUNDS = 2
MAX_ORDINARY_WORKERS = 1
MAX_PARALLEL_WORKERS = 2
MAX_MILESTONE_WORKERS = 2


def _required_text(value: Any, label: str, *, limit: int = 16_384) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PolicyError(f"{label} must be non-empty text")
    value = value.strip()
    if len(value) > limit:
        raise PolicyError(f"{label} exceeds its bounded size")
    return value


def _string_list(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise PolicyError(f"{label} must be a list")
    return tuple(_required_text(item, f"{label} item", limit=1_024) for item in value)


def _normalized_scope(value: str) -> str:
    """Normalize a declared project-relative write scope without touching disk."""
    value = _required_text(value, "write scope", limit=1_024).replace("\\", "/")
    if value.startswith(("/", "~")) or (len(value) >= 2 and value[1] == ":"):
        raise PolicyError("write scope must be project-relative")
    while "//" in value:
        value = value.replace("//", "/")
    value = value.strip().rstrip("/")
    for suffix in ("/**", "/*"):
        if value.endswith(suffix):
            value = value[: -len(suffix)].rstrip("/")
    parts = [part for part in value.split("/") if part not in {"", "."}]
    if not parts or any(part == ".." for part in parts):
        raise PolicyError("write scope must be a bounded path without parent traversal")
    if any(any(marker in part for marker in ("*", "?", "[", "]")) for part in parts):
        raise PolicyError("write scope may only use a trailing /* or /** wildcard")
    return "/".join(parts).casefold()


def write_scopes_overlap(left: Sequence[str], right: Sequence[str]) -> bool:
    """Conservatively reject identical or nested concurrent write scopes."""
    normalized_left = tuple(_normalized_scope(item) for item in left)
    normalized_right = tuple(_normalized_scope(item) for item in right)
    for first in normalized_left:
        for second in normalized_right:
            if first == second:
                return True
            if first.startswith(second + "/") or second.startswith(first + "/"):
                return True
    return False


def write_scope_is_within(candidate: Sequence[str], allowed: Sequence[str]) -> bool:
    normalized_candidate = tuple(_normalized_scope(item) for item in candidate)
    normalized_allowed = tuple(_normalized_scope(item) for item in allowed)
    return bool(normalized_candidate) and all(
        any(path == parent or path.startswith(parent + "/") for parent in normalized_allowed)
        for path in normalized_candidate
    )


@dataclass(frozen=True)
class RequestSignals:
    continuation: bool = False
    status_only: bool = False
    project_root_known: bool = True
    request_belongs_to_project: bool = True
    new_project: bool = False
    new_project_root: bool = False
    target_user_change: bool = False
    core_direction_change: bool = False
    public_interface_change: bool = False
    data_model_change: bool = False
    dependency_change: bool = False
    milestone_change: bool = False
    multiple_modules: bool = False
    duplicate_feature: bool = False
    architecture_conflict: bool = False
    active_write_conflict: bool = False
    prerequisite_missing: bool = False
    simpler_approach_available: bool = False
    security_risk: bool = False
    privacy_risk: bool = False
    payment_risk: bool = False
    production_risk: bool = False
    migration_risk: bool = False
    hard_to_rollback: bool = False

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> "RequestSignals":
        value = value or {}
        unknown = set(value) - set(cls.__dataclass_fields__)
        if unknown:
            raise PolicyError(f"unknown request signal(s): {', '.join(sorted(unknown))}")
        if any(not isinstance(item, bool) for item in value.values()):
            raise PolicyError("request signals must be booleans")
        return cls(**value)

    @property
    def high_assurance(self) -> bool:
        return any(
            (
                self.security_risk,
                self.privacy_risk,
                self.payment_risk,
                self.production_risk,
                self.migration_risk,
                self.hard_to_rollback,
            )
        )


@dataclass(frozen=True)
class UserRequest:
    goal_id: str
    request_type: RequestType
    summary: str
    signals: RequestSignals = field(default_factory=RequestSignals)
    evidence_refs: tuple[str, ...] = ()
    override_confirmed: bool = False
    override_risk: str | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "UserRequest":
        if not isinstance(value, Mapping):
            raise PolicyError("request must be an object")
        try:
            request_type = RequestType(value["request_type"])
        except (KeyError, ValueError) as exc:
            raise PolicyError("request_type is missing or invalid") from exc
        override_risk = value.get("override_risk")
        if override_risk is not None:
            override_risk = _required_text(override_risk, "override_risk", limit=2_048)
        return cls(
            goal_id=_required_text(value.get("goal_id"), "goal_id", limit=128),
            request_type=request_type,
            summary=_required_text(value.get("summary"), "summary", limit=4_096),
            signals=RequestSignals.from_mapping(value.get("signals")),
            evidence_refs=_string_list(value.get("evidence_refs", []), "evidence_refs"),
            override_confirmed=bool(value.get("override_confirmed", False)),
            override_risk=override_risk,
        )


@dataclass(frozen=True)
class ProjectSnapshot:
    project_root: str
    workflow_profile: WorkflowProfile = WorkflowProfile.LIGHT
    brief_approved: bool = True
    project_plan_approved: bool = True
    last_indexed_commit: str | None = None
    current_head: str | None = None
    status_available: bool = True
    active_worker_ids: tuple[str, ...] = ()

    @property
    def head_changed(self) -> bool:
        return bool(
            self.last_indexed_commit
            and self.current_head
            and self.last_indexed_commit != self.current_head
        )


@dataclass(frozen=True)
class RuntimeCapabilities:
    create_thread: bool = False
    send_message_to_thread: bool = False
    wait_threads: bool = False
    read_thread: bool = False

    @classmethod
    def available(cls) -> "RuntimeCapabilities":
        return cls(True, True, True, True)

    def missing(self, *, reuse_existing: bool) -> tuple[str, ...]:
        required = REQUIRED_THREAD_CAPABILITIES[1:] if reuse_existing else REQUIRED_THREAD_CAPABILITIES
        return tuple(name for name in required if not getattr(self, name))


@dataclass(frozen=True)
class RuntimeThreadIdentity:
    thread_id: str
    project_id: str
    host_id: str

    def __post_init__(self) -> None:
        _required_text(self.thread_id, "thread_id", limit=256)
        _required_text(self.project_id, "project_id", limit=256)
        _required_text(self.host_id, "host_id", limit=256)

    def as_dict(self) -> dict[str, str]:
        return {
            "thread_id": self.thread_id,
            "project_id": self.project_id,
            "host_id": self.host_id,
        }


@dataclass(frozen=True)
class TaskThreadBinding:
    task_id: str
    identity: RuntimeThreadIdentity
    objective: str
    write_scope: tuple[str, ...]
    status: str = "working"
    last_result: str = "pending"

    def __post_init__(self) -> None:
        _required_text(self.task_id, "task_id", limit=128)
        _required_text(self.objective, "objective", limit=1_024)
        _required_text(self.status, "task status", limit=64)
        _required_text(self.last_result, "last_result", limit=2_048)
        if not self.write_scope:
            raise PolicyError("task-thread binding requires a non-empty write scope")
        for item in self.write_scope:
            _normalized_scope(item)

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            **self.identity.as_dict(),
            "objective": self.objective,
            "write_scope": list(self.write_scope),
            "status": self.status,
            "last_result": self.last_result,
        }


@dataclass(frozen=True)
class FitAssessment:
    level: FitLevel
    fit: str
    issues: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    requires_user_decision: bool
    recommended_profile: WorkflowProfile

    def as_dict(self) -> dict[str, Any]:
        return {
            "level": self.level.value,
            "fit": self.fit,
            "issues": list(self.issues),
            "evidence_refs": list(self.evidence_refs),
            "requires_user_decision": self.requires_user_decision,
            "recommended_profile": self.recommended_profile.value,
        }


def classify_fit(request: UserRequest) -> FitAssessment:
    """Classify from declared semantic facts, never from summary keywords."""
    signals = request.signals
    if (
        request.request_type == RequestType.QUESTION_OR_STATUS
        or signals.status_only
        or signals.continuation
    ):
        return FitAssessment(
            FitLevel.CONTINUATION,
            "PASS",
            (),
            request.evidence_refs,
            False,
            WorkflowProfile.LIGHT,
        )
    if not signals.project_root_known or not signals.request_belongs_to_project:
        return FitAssessment(
            FitLevel.UNKNOWN,
            "UNKNOWN",
            ("project_root_or_request_ownership_unknown",),
            request.evidence_refs,
            True,
            WorkflowProfile.LIGHT,
        )
    if any(
        (
            signals.new_project,
            signals.new_project_root,
            signals.target_user_change,
            signals.core_direction_change,
        )
    ):
        return FitAssessment(
            FitLevel.PROJECT_RESET,
            "PLAN_REQUIRED",
            (),
            request.evidence_refs,
            True,
            WorkflowProfile.LIGHT,
        )

    issues: list[str] = []
    for name in (
        "duplicate_feature",
        "architecture_conflict",
        "active_write_conflict",
        "prerequisite_missing",
        "simpler_approach_available",
        "security_risk",
        "privacy_risk",
        "payment_risk",
        "production_risk",
        "migration_risk",
        "hard_to_rollback",
    ):
        if getattr(signals, name):
            issues.append(name)

    plan_delta = any(
        (
            signals.public_interface_change,
            signals.data_model_change,
            signals.dependency_change,
            signals.milestone_change,
            signals.multiple_modules,
            signals.high_assurance,
        )
    )
    unresolved = bool(issues and not request.override_confirmed)
    recommended_profile = (
        WorkflowProfile.GOVERNED if signals.high_assurance else WorkflowProfile.LIGHT
    )
    return FitAssessment(
        FitLevel.PLAN_DELTA if plan_delta else FitLevel.LOCAL,
        "CONFLICT" if unresolved else "PASS",
        tuple(issues),
        request.evidence_refs,
        unresolved or plan_delta,
        recommended_profile,
    )


@dataclass(frozen=True)
class TaskPacket:
    values: Mapping[str, Any]
    context_kinds: frozenset[str] = frozenset()
    initial_context_tokens: int | None = None
    write_scope: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        keys = set(self.values)
        required = set(TASK_PACKET_FIELDS)
        if keys != required:
            missing = sorted(required - keys)
            extra = sorted(keys - required)
            raise PolicyError(f"task packet fields mismatch; missing={missing}; extra={extra}")
        forbidden = self.context_kinds & FORBIDDEN_PACKET_CONTEXT_KINDS
        if forbidden:
            raise PolicyError(
                "task packet contains forbidden context kinds: "
                + ", ".join(sorted(forbidden))
            )
        for field_name in TASK_PACKET_FIELDS:
            _required_text(self.values[field_name], field_name)
        if self.initial_context_tokens is not None:
            if not isinstance(self.initial_context_tokens, int) or self.initial_context_tokens < 0:
                raise PolicyError("initial_context_tokens must be a non-negative integer")
        for item in self.write_scope:
            _normalized_scope(item)

    def render(self) -> str:
        sections = [f"{name}\n{self.values[name]}" for name in TASK_PACKET_FIELDS]
        return "\n\n".join(sections).rstrip() + "\n"

    def metrics(self) -> dict[str, Any]:
        size = len(self.render().encode("utf-8"))
        return {
            "bytes": size,
            "target_2_to_4_kib": TASK_PACKET_TARGET_MIN_BYTES
            <= size
            <= TASK_PACKET_TARGET_MAX_BYTES,
            "initial_context_tokens": self.initial_context_tokens,
            "initial_context_within_recommendation": self.initial_context_tokens is None
            or self.initial_context_tokens <= INITIAL_CONTEXT_RECOMMENDED_MAX_TOKENS,
            "context_kinds": sorted(self.context_kinds),
            "write_scope": list(self.write_scope),
        }


@dataclass(frozen=True)
class WorkerResult:
    result: str
    changed_paths: tuple[str, ...]
    validation_commands: tuple[str, ...]
    validation_result: str
    risks_or_blockers: tuple[str, ...] = ()
    decision_needed: str = "None"
    actual_artifacts_inspected: bool = False
    diff_inspected: bool = False
    tests_inspected: bool = False

    def __post_init__(self) -> None:
        _required_text(self.result, "RESULT", limit=2_048)
        _string_list(self.changed_paths, "CHANGED_PATHS")
        _string_list(self.validation_commands, "VALIDATION_COMMANDS")
        _required_text(self.validation_result, "VALIDATION_RESULT", limit=4_096)
        _string_list(self.risks_or_blockers, "RISKS_OR_BLOCKERS")
        _required_text(self.decision_needed, "DECISION_NEEDED", limit=2_048)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "WorkerResult":
        missing = set(WORKER_RESULT_FIELDS) - set(value)
        extra = set(value) - set(WORKER_RESULT_FIELDS)
        if missing or extra:
            raise PolicyError(
                f"worker result fields mismatch; missing={sorted(missing)}; extra={sorted(extra)}"
            )
        return cls(
            result=_required_text(value["RESULT"], "RESULT", limit=2_048),
            changed_paths=_string_list(
                value["CHANGED_PATHS"], "CHANGED_PATHS"
            ),
            validation_commands=_string_list(
                value["VALIDATION_COMMANDS"], "VALIDATION_COMMANDS"
            ),
            validation_result=_required_text(
                value["VALIDATION_RESULT"], "VALIDATION_RESULT", limit=4_096
            ),
            risks_or_blockers=_string_list(
                value["RISKS_OR_BLOCKERS"], "RISKS_OR_BLOCKERS"
            ),
            decision_needed=_required_text(
                value["DECISION_NEEDED"], "DECISION_NEEDED", limit=2_048
            ),
            actual_artifacts_inspected=bool(value.get("actual_artifacts_inspected", False)),
            diff_inspected=bool(value.get("diff_inspected", False)),
            tests_inspected=bool(value.get("tests_inspected", False)),
        )


class ChangeRisk(str, Enum):
    CONTENT_STYLE_CONFIG = "CONTENT_STYLE_CONFIG"
    LOCAL_FEATURE_OR_BUG = "LOCAL_FEATURE_OR_BUG"
    CROSS_MODULE_INTERFACE_OR_DATABASE = "CROSS_MODULE_INTERFACE_OR_DATABASE"
    MILESTONE_RELEASE_OR_HIGH_RISK = "MILESTONE_RELEASE_OR_HIGH_RISK"


class ValidationScope(str, Enum):
    STATIC_OR_RELEVANT_PAGE = "STATIC_OR_RELEVANT_PAGE"
    RELATED_TESTS = "RELATED_TESTS"
    RELATED_UNIT_AND_INTEGRATION = "RELATED_UNIT_AND_INTEGRATION"
    FULL_SUITE_ONCE = "FULL_SUITE_ONCE"


class FailureClassification(str, Enum):
    BASELINE_FAILURE = "BASELINE_FAILURE"
    NEW_FAILURE = "NEW_FAILURE"
    ENVIRONMENT_LIMITATION = "ENVIRONMENT_LIMITATION"


def validation_scope_for(
    change_risk: ChangeRisk, *, integration_node: bool = False
) -> ValidationScope:
    if integration_node or change_risk == ChangeRisk.MILESTONE_RELEASE_OR_HIGH_RISK:
        return ValidationScope.FULL_SUITE_ONCE
    if change_risk == ChangeRisk.CROSS_MODULE_INTERFACE_OR_DATABASE:
        return ValidationScope.RELATED_UNIT_AND_INTEGRATION
    if change_risk == ChangeRisk.LOCAL_FEATURE_OR_BUG:
        return ValidationScope.RELATED_TESTS
    return ValidationScope.STATIC_OR_RELEVANT_PAGE


def classify_failure(
    *, failed_before_change: bool = False, environment_limited: bool = False
) -> FailureClassification:
    if environment_limited:
        return FailureClassification.ENVIRONMENT_LIMITATION
    if failed_before_change:
        return FailureClassification.BASELINE_FAILURE
    return FailureClassification.NEW_FAILURE


@dataclass(frozen=True)
class TestEvidence:
    source_version: str
    command: str
    exit_code: int
    trusted: bool
    log_ref: str
    scope: ValidationScope = ValidationScope.RELATED_TESTS

    def __post_init__(self) -> None:
        _required_text(self.source_version, "source_version", limit=256)
        _required_text(self.command, "test command", limit=2_048)
        if not isinstance(self.exit_code, int) or isinstance(self.exit_code, bool):
            raise PolicyError("test exit_code must be an integer")
        _required_text(self.log_ref, "test log_ref", limit=1_024)


@dataclass
class ValidationPlanner:
    trusted_results: dict[tuple[str, str], TestEvidence] = field(default_factory=dict)
    full_suite_versions: set[str] = field(default_factory=set)

    def record(self, evidence: TestEvidence) -> None:
        if evidence.trusted:
            self.trusted_results[(evidence.source_version, evidence.command)] = evidence
        if (
            evidence.trusted
            and evidence.exit_code == 0
            and evidence.scope == ValidationScope.FULL_SUITE_ONCE
        ):
            self.full_suite_versions.add(evidence.source_version)

    def plan(
        self,
        *,
        source_version: str,
        command: str,
        change_risk: ChangeRisk,
        integration_node: bool = False,
    ) -> dict[str, Any]:
        source_version = _required_text(source_version, "source_version", limit=256)
        command = _required_text(command, "test command", limit=2_048)
        prior = self.trusted_results.get((source_version, command))
        if prior is not None:
            return {
                "action": "REUSE_TRUSTED_RESULT",
                "scope": validation_scope_for(
                    change_risk, integration_node=integration_node
                ).value,
                "exit_code": prior.exit_code,
                "log_ref": prior.log_ref,
            }
        scope = validation_scope_for(change_risk, integration_node=integration_node)
        if (
            scope == ValidationScope.FULL_SUITE_ONCE
            and source_version in self.full_suite_versions
        ):
            return {
                "action": "REUSE_TRUSTED_FULL_SUITE",
                "scope": scope.value,
            }
        return {"action": "RUN_TEST", "scope": scope.value}


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise PolicyError(f"{name} must be a non-negative integer")

    def __add__(self, other: "TokenUsage") -> "TokenUsage":
        return TokenUsage(
            self.input_tokens + other.input_tokens,
            self.cached_input_tokens + other.cached_input_tokens,
            self.output_tokens + other.output_tokens,
            self.reasoning_tokens + other.reasoning_tokens,
        )

    @property
    def raw_component_total(self) -> int:
        return sum(getattr(self, name) for name in self.__dataclass_fields__)

    @property
    def folded_cost_units(self) -> float:
        uncached_input = max(0, self.input_tokens - self.cached_input_tokens)
        return (
            uncached_input
            + (self.cached_input_tokens * 0.1)
            + self.output_tokens
            + self.reasoning_tokens
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "input_tokens": self.input_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "output_tokens": self.output_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "raw_component_total": self.raw_component_total,
            "folded_cost_units": self.folded_cost_units,
        }


@dataclass
class BudgetLedger:
    hard_limit: int | None = None
    telemetry_available: bool = False
    total_usage: TokenUsage = field(default_factory=TokenUsage)
    actor_usage: dict[str, TokenUsage] = field(default_factory=dict)
    category_usage: dict[str, TokenUsage] = field(default_factory=dict)
    model_rounds: int = 0
    bytes_read: int = 0
    task_packet_bytes: int = 0
    worker_count: int = 0
    state_write_count: int = 0
    rework_count: int = 0

    def __post_init__(self) -> None:
        if self.hard_limit is not None and (
            not isinstance(self.hard_limit, int)
            or isinstance(self.hard_limit, bool)
            or self.hard_limit <= 0
        ):
            raise PolicyError("hard_limit must be a positive integer when explicitly set")

    def record_usage(self, actor: str, category: str, usage: TokenUsage) -> None:
        actor = _required_text(actor, "actor", limit=128)
        category = _required_text(category, "category", limit=128)
        if not self.telemetry_available:
            raise PolicyError("TOKEN_TELEMETRY_UNAVAILABLE")
        self.total_usage = self.total_usage + usage
        self.actor_usage[actor] = self.actor_usage.get(actor, TokenUsage()) + usage
        self.category_usage[category] = self.category_usage.get(category, TokenUsage()) + usage

    def record_proxy(
        self,
        *,
        model_rounds: int = 0,
        bytes_read: int = 0,
        task_packet_bytes: int = 0,
        worker_count: int = 0,
        state_write_count: int = 0,
        rework_count: int = 0,
    ) -> None:
        for name, value in {
            "model_rounds": model_rounds,
            "bytes_read": bytes_read,
            "task_packet_bytes": task_packet_bytes,
            "worker_count": worker_count,
            "state_write_count": state_write_count,
            "rework_count": rework_count,
        }.items():
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise PolicyError(f"{name} proxy must be a non-negative integer")
            setattr(self, name, getattr(self, name) + value)

    def state(self, *, evidence_progress: bool = True) -> dict[str, Any]:
        if self.telemetry_available:
            used = self.total_usage.folded_cost_units
            ratio = used / self.hard_limit if self.hard_limit is not None else None
            governance = self.category_usage.get("governance", TokenUsage()).folded_cost_units
            fit = self.category_usage.get("fit_check", TokenUsage()).folded_cost_units
            action = "CONTINUE"
            if ratio is not None and ratio >= 1:
                action = "HARD_STOP"
            elif ratio is not None and ratio >= 0.9:
                action = "NO_NEW_WORKER_REPLAN"
            elif ratio is not None and ratio >= 0.7:
                action = "STOP_OPTIONAL_WORK"
            elif ratio is not None and ratio >= 0.25 and not evidence_progress:
                action = "PAUSE_NO_VERIFIABLE_PROGRESS"
            elif used and governance / used > 0.3:
                action = "GOVERNANCE_BUDGET_EXCEEDED"
            elif used and fit / used > 0.1:
                action = "FIT_CHECK_BUDGET_EXCEEDED"
            return {
                "telemetry": "VERIFIED",
                "shared_hard_limit": self.hard_limit,
                "usage": self.total_usage.as_dict(),
                "folded_ratio": ratio,
                "action": action,
                "actors": {key: value.as_dict() for key, value in self.actor_usage.items()},
            }

        proxy_breakers: list[str] = []
        if self.model_rounds >= 20:
            proxy_breakers.append("MODEL_ROUND_LIMIT")
        if self.bytes_read >= 2 * 1024 * 1024:
            proxy_breakers.append("READ_BYTE_LIMIT")
        if self.worker_count > MAX_MILESTONE_WORKERS:
            proxy_breakers.append("WORKER_LIMIT")
        if self.rework_count > MAX_REWORK_ROUNDS:
            proxy_breakers.append("REWORK_LIMIT")
        return {
            "telemetry": "TOKEN_TELEMETRY_UNAVAILABLE",
            "token_thresholds": "UNVERIFIED",
            "action": "PROXY_CIRCUIT_BREAKER" if proxy_breakers else "CONTINUE_WITH_PROXIES",
            "proxy_breakers": proxy_breakers,
            "proxies": {
                "model_rounds": self.model_rounds,
                "bytes_read": self.bytes_read,
                "task_packet_bytes": self.task_packet_bytes,
                "worker_count": self.worker_count,
                "state_write_count": self.state_write_count,
                "rework_count": self.rework_count,
            },
        }


@dataclass
class SupervisorRun:
    snapshot: ProjectSnapshot
    budget: BudgetLedger = field(default_factory=BudgetLedger)
    fit_by_goal: dict[str, FitAssessment] = field(default_factory=dict)
    trace: list[dict[str, Any]] = field(default_factory=list)
    current_goal_id: str | None = None
    current_request_type: RequestType | None = None
    current_worker_id: str | None = None
    worker_ids: list[str] = field(default_factory=list)
    thread_bindings: dict[str, TaskThreadBinding] = field(default_factory=dict)
    rework_rounds: int = 0
    no_evidence_turns: int = 0
    model_wake_count: int = 0
    worker_event_observed: bool = False
    state_write_count: int = 0
    accepted: bool = False
    circuit_breaker: str | None = None
    runtime_blocker: str | None = None
    dispatch_gate: str = "NOT_STARTED"

    def _event(self, action: str, **details: Any) -> dict[str, Any]:
        row = {"seq": len(self.trace) + 1, "action": action, **details}
        self.trace.append(row)
        return row

    @property
    def fit_check_count(self) -> int:
        return len(self.fit_by_goal)

    def begin(self, request: UserRequest) -> FitAssessment:
        if self.snapshot.workflow_profile != WorkflowProfile.LIGHT:
            self._event("DEFER_TO_V4_GOVERNED")
            raise PolicyError("V4_GOVERNED must use the governed control plane")
        self.current_goal_id = request.goal_id
        self.current_request_type = request.request_type
        assessment = self.fit_by_goal.get(request.goal_id)
        if assessment is None:
            assessment = classify_fit(request)
            self.fit_by_goal[request.goal_id] = assessment
            self._event("FIT_CHECK_ONCE", **assessment.as_dict())
        if assessment.level == FitLevel.CONTINUATION:
            self.dispatch_gate = "F0_NO_NEW_WORKER"
            self._event("READ_STATUS_BOUNDED", writes=0, workers=0)
        elif assessment.level == FitLevel.UNKNOWN:
            self.dispatch_gate = "UNKNOWN_BLOCKED"
            self._event("ASK_ONE_BLOCKING_QUESTION", repository_scan=False)
        elif assessment.level == FitLevel.PROJECT_RESET:
            self.dispatch_gate = "BRIEF_AND_PLAN_REQUIRED"
            self._event("DISCOVERY_REQUIRED", implementation_workers=0)
            self._event("PROJECT_BRIEF_CONFIRMATION_REQUIRED", implementation_workers=0)
            self._event("PLAN_CONFIRMATION_REQUIRED", implementation_workers=0)
        elif assessment.fit == "CONFLICT":
            self.dispatch_gate = "FIT_CONFLICT_BLOCKED"
            self._event(
                "FIT_CONFLICT",
                evidence=list(assessment.evidence_refs),
                impact=list(assessment.issues),
                recommendation="pause_or_choose_the_safer_fit",
                alternative="use_the_simpler_or_prerequisite_first_path",
                workers=0,
            )
        elif assessment.recommended_profile == WorkflowProfile.GOVERNED:
            if request.override_confirmed:
                if not request.override_risk:
                    raise PolicyError("override_confirmed requires an explicit override_risk")
                self._event("USER_OVERRIDE_RECORDED", risk=request.override_risk)
            self.dispatch_gate = "GOVERNED_MODE_REQUIRED"
            self._event(
                "DEFER_TO_V4_GOVERNED",
                reason="high_assurance_request",
                workers=0,
            )
        elif assessment.level == FitLevel.PLAN_DELTA:
            if request.override_confirmed:
                if not request.override_risk:
                    raise PolicyError("override_confirmed requires an explicit override_risk")
                self._event("USER_OVERRIDE_RECORDED", risk=request.override_risk)
            self.dispatch_gate = "PLAN_DELTA_REQUIRED"
            self._event("PLAN_DELTA_REQUIRED", rebuild_full_plan=False, workers=0)
        else:
            if request.override_confirmed:
                if not request.override_risk:
                    raise PolicyError("override_confirmed requires an explicit override_risk")
                self._event("USER_OVERRIDE_RECORDED", risk=request.override_risk)
            self.dispatch_gate = "OPEN"
            self._event("LOCAL_TASK_READY", rediscovery=False, full_plan_reapproval=False)
        return assessment

    def confirm_plan_delta(self, evidence_ref: str) -> None:
        if self.dispatch_gate != "PLAN_DELTA_REQUIRED":
            raise PolicyError("no plan delta is awaiting confirmation")
        _required_text(evidence_ref, "plan delta approval evidence", limit=1_024)
        self._event("PLAN_DELTA_APPROVED", evidence_ref=evidence_ref)
        self.dispatch_gate = "OPEN"

    def confirm_new_project(self, brief_ref: str, plan_ref: str) -> None:
        if self.dispatch_gate != "BRIEF_AND_PLAN_REQUIRED":
            raise PolicyError("new-project Brief and plan are not awaiting confirmation")
        _required_text(brief_ref, "brief approval ref", limit=1_024)
        _required_text(plan_ref, "plan approval ref", limit=1_024)
        self._event("PROJECT_BRIEF_APPROVED", evidence_ref=brief_ref)
        self._event("PROJECT_PLAN_APPROVED", evidence_ref=plan_ref)
        self.dispatch_gate = "OPEN"

    def dispatch(
        self,
        packet: TaskPacket,
        identity: RuntimeThreadIdentity,
        capabilities: RuntimeCapabilities,
        *,
        parallel: bool = False,
    ) -> bool:
        if self.circuit_breaker:
            raise PolicyError(f"dispatch blocked by {self.circuit_breaker}")
        if self.dispatch_gate != "OPEN":
            raise PolicyError(f"dispatch blocked by gate {self.dispatch_gate}")
        if self.current_goal_id is None:
            raise PolicyError("begin must establish a task before dispatch")
        existing = self.thread_bindings.get(self.current_goal_id)
        reuse_existing = existing is not None
        missing = capabilities.missing(reuse_existing=reuse_existing)
        if missing:
            self.runtime_blocker = "RUNTIME_THREAD_CAPABILITY_UNAVAILABLE"
            self._event(
                "RUNTIME_THREAD_CAPABILITY_UNAVAILABLE",
                missing=list(missing),
                roleplay_worker_created=False,
                workers=len(self.worker_ids),
            )
            return False
        self.runtime_blocker = None
        if reuse_existing:
            if existing.identity != identity:
                raise PolicyError("same task must reuse its original thread identity")
            if not write_scope_is_within(packet.write_scope, existing.write_scope):
                raise PolicyError("same task may not expand its original write scope")
            self.current_worker_id = identity.thread_id
        else:
            if identity.thread_id in self.worker_ids:
                raise PolicyError("thread_id is already bound to another task")
            max_workers = MAX_PARALLEL_WORKERS if parallel else MAX_ORDINARY_WORKERS
            if len(self.worker_ids) >= max_workers:
                raise PolicyError("worker limit exceeded")
            if not packet.write_scope:
                raise PolicyError("dispatch requires machine-checkable write_scope metadata")
            if parallel:
                for binding in self.thread_bindings.values():
                    if binding.status == "working" and write_scopes_overlap(
                        binding.write_scope, packet.write_scope
                    ):
                        raise PolicyError("parallel write scopes overlap")
        budget_state = self.budget.state()
        if budget_state["action"] in {"HARD_STOP", "NO_NEW_WORKER_REPLAN"}:
            raise PolicyError(f"dispatch blocked by {budget_state['action']}")
        packet_bytes = packet.metrics()["bytes"]
        if packet_bytes > TASK_PACKET_TARGET_MAX_BYTES:
            raise PolicyError("task packet exceeds 4 KiB; move large context to an artifact")
        if not reuse_existing:
            self.worker_ids.append(identity.thread_id)
            self.current_worker_id = identity.thread_id
            self.thread_bindings[self.current_goal_id] = TaskThreadBinding(
                task_id=self.current_goal_id,
                identity=identity,
                objective=str(packet.values["OBJECTIVE"]),
                write_scope=tuple(packet.write_scope),
            )
            self.budget.record_proxy(worker_count=1, task_packet_bytes=packet_bytes)
        else:
            self.budget.record_proxy(task_packet_bytes=packet_bytes)
        self._event("TASK_PACKET_VALIDATED", fields=list(TASK_PACKET_FIELDS), **packet.metrics())
        if reuse_existing:
            self._event(
                "REUSE_ORIGINAL_THREAD_ID",
                **identity.as_dict(),
                replacement_thread_created=False,
            )
        else:
            self._event("CREATE_REAL_CODEX_THREAD", **identity.as_dict())
            self._event("REAL_THREAD_ID_BOUND", **identity.as_dict(), owner_count=1)
            self._event(
                "PERSIST_TASK_THREAD_MAPPING",
                mapping_file="TASK_THREADS.md",
                task_id=self.current_goal_id,
                **identity.as_dict(),
            )
        if self.current_request_type == RequestType.BUG_REPORT:
            self._event(
                "BUG_SINGLE_OWNER_PIPELINE",
                thread_id=identity.thread_id,
                stages=["reproduce", "diagnose", "fix", "regression_test"],
            )
        self._event(
            "SEND_TASK_PACKET",
            thread_id=identity.thread_id,
            method="create_thread.prompt" if not reuse_existing else "send_message_to_thread",
        )
        self._event("WAIT_EVENT_DRIVEN", polling=False)
        self.worker_event_observed = False
        return True

    def wait_snapshot(self, *, changed: bool, worker_state: str | None = None) -> None:
        if not changed:
            return
        if self.current_worker_id is None:
            raise PolicyError("worker event cannot be observed before real thread dispatch")
        self.model_wake_count += 1
        self.worker_event_observed = True
        self.budget.record_proxy(model_rounds=1)
        self._event("WORKER_EVENT", worker_state=worker_state or "UNKNOWN")

    def record_model_turn(self, *, evidence_progress: bool) -> None:
        self.budget.record_proxy(model_rounds=1)
        if evidence_progress:
            self.no_evidence_turns = 0
            return
        self.no_evidence_turns += 1
        if self.no_evidence_turns >= 2:
            self.circuit_breaker = "EFFICIENCY_CIRCUIT_BREAKER"
            self._event("EFFICIENCY_CIRCUIT_BREAKER", reason="two_turns_without_evidence")

    def review_result(self, result: WorkerResult) -> bool:
        complete_evidence = all(
            (
                result.result.strip().upper() == "PASS",
                bool(result.changed_paths),
                bool(result.validation_commands),
                bool(result.validation_result.strip()),
                self.worker_event_observed,
                result.actual_artifacts_inspected,
                result.diff_inspected,
                result.tests_inspected,
            )
        )
        if not complete_evidence:
            self._event("RESULT_REJECTED", reason="summary_or_evidence_only_is_insufficient")
            return False
        self._event(
            "READ_ACTUAL_ARTIFACTS",
            refs=list(result.changed_paths),
        )
        self._event("INSPECT_DIFF")
        self._event(
            "VERIFY_TEST_EVIDENCE",
            commands=list(result.validation_commands),
            result=result.validation_result,
        )
        self.accepted = True
        self._event("ACCEPT_RESULT", thread_id=self.current_worker_id)
        if self.current_goal_id in self.thread_bindings:
            binding = self.thread_bindings[self.current_goal_id]
            self.thread_bindings[self.current_goal_id] = TaskThreadBinding(
                task_id=binding.task_id,
                identity=binding.identity,
                objective=binding.objective,
                write_scope=binding.write_scope,
                status="accepted",
                last_result=result.validation_result,
            )
        if self.state_write_count == 0:
            self.state_write_count = 1
            self.budget.record_proxy(state_write_count=1)
            self._event("UPDATE_STATUS_ONCE", transaction_count=1)
        return True

    def request_revision(self, defects: Sequence[str]) -> bool:
        defects = _string_list(defects, "defects")
        if self.current_worker_id is None:
            raise PolicyError("no original Worker is available for revision")
        if self.rework_rounds >= MAX_REWORK_ROUNDS:
            self.circuit_breaker = "REWORK_LIMIT_REPLAN_REQUIRED"
            self._event(
                "REWORK_LIMIT_REACHED",
                replacement_thread_created=False,
                next_action="stop_and_replan_or_ask_user",
            )
            return False
        self.rework_rounds += 1
        self.budget.record_proxy(rework_count=1)
        self._event(
            "REQUEST_TARGETED_REVISION",
            thread_id=self.current_worker_id,
            round=self.rework_rounds,
            defects=list(defects),
            transport="send_message_to_thread",
            replacement_thread_created=False,
        )
        self.worker_event_observed = False
        self._event("WAIT_EVENT_DRIVEN", polling=False)
        return True

    def record_blocked(self, reason: str) -> None:
        reason = _required_text(reason, "blocked reason", limit=2_048)
        self._event("TASK_BLOCKED", reason=reason)
        if self.current_goal_id in self.thread_bindings:
            binding = self.thread_bindings[self.current_goal_id]
            self.thread_bindings[self.current_goal_id] = TaskThreadBinding(
                task_id=binding.task_id,
                identity=binding.identity,
                objective=binding.objective,
                write_scope=binding.write_scope,
                status="blocked",
                last_result=reason,
            )
        if self.state_write_count == 0:
            self.state_write_count = 1
            self.budget.record_proxy(state_write_count=1)
            self._event("UPDATE_STATUS_ONCE", transaction_count=1, state="blocked")

    def as_dict(self) -> dict[str, Any]:
        return {
            "workflow_profile": self.snapshot.workflow_profile.value,
            "fit_check_count": self.fit_check_count,
            "worker_ids": list(self.worker_ids),
            "thread_bindings": {
                key: value.as_dict() for key, value in self.thread_bindings.items()
            },
            "rework_rounds": self.rework_rounds,
            "model_wake_count": self.model_wake_count,
            "worker_event_observed": self.worker_event_observed,
            "state_write_count": self.state_write_count,
            "accepted": self.accepted,
            "circuit_breaker": self.circuit_breaker,
            "runtime_blocker": self.runtime_blocker,
            "dispatch_gate": self.dispatch_gate,
            "budget": self.budget.state(evidence_progress=self.accepted),
            "trace": list(self.trace),
        }


def artifact_delivery(text: str, artifact_ref: str | None = None) -> dict[str, Any]:
    text = _required_text(text, "output", limit=4 * 1024 * 1024)
    size = len(text.encode("utf-8"))
    if size <= OUTPUT_ARTIFACT_THRESHOLD:
        return {"mode": "INLINE", "bytes": size, "content": text}
    ref = _required_text(artifact_ref, "artifact_ref", limit=1_024)
    return {
        "mode": "ARTIFACT_REFERENCE",
        "bytes": size,
        "artifact_ref": ref,
        "content": None,
    }


def validate_research_candidates(
    initial: Sequence[Mapping[str, Any]],
    finalists: Sequence[Mapping[str, Any]],
    *,
    clone_or_full_copy_requested: bool = False,
) -> dict[str, Any]:
    if len(initial) > 5:
        raise PolicyError("open-source initial screen is limited to five candidates")
    if len(finalists) > 3:
        raise PolicyError("open-source final comparison is limited to three candidates")
    if clone_or_full_copy_requested:
        raise PolicyError("candidate repositories must not be cloned or copied in full")
    required = {
        "name",
        "official_source",
        "license",
        "maintenance",
        "stack_compatibility",
        "integration_cost",
        "security_risk",
        "limitations",
    }
    finalist_names: list[str] = []
    for row in finalists:
        if set(row) != required:
            raise PolicyError("each finalist must contain the complete bounded screening record")
        for key, value in row.items():
            _required_text(value, f"candidate {key}", limit=2_048)
        finalist_names.append(str(row["name"]))
    return {
        "initial_count": len(initial),
        "final_count": len(finalists),
        "finalists": finalist_names,
        "repository_clones": 0,
        "result_reuse": "task_packet",
    }


def workflow_profile_marker(profile: WorkflowProfile = WorkflowProfile.LIGHT) -> str:
    return f"workflow_profile={profile.value}"


def compact_state_projection(
    *,
    project_name: str,
    current_head: str,
    phase: str,
    accepted_summary: str,
) -> dict[str, str]:
    project_name = _required_text(project_name, "project_name", limit=256)
    current_head = _required_text(current_head, "current_head", limit=256)
    phase = _required_text(phase, "phase", limit=128)
    accepted_summary = _required_text(accepted_summary, "accepted_summary", limit=2_048)
    project = (
        "# Project\n\n"
        f"- Project: {project_name}\n"
        f"- {workflow_profile_marker()}\n"
        f"- last_indexed_commit={current_head}\n"
    )
    status = (
        "# Status\n\n"
        f"- {workflow_profile_marker()}\n"
        f"- last_indexed_commit={current_head}\n"
        f"- Current phase: {phase}\n\n"
        "## Completed and Accepted\n\n"
        f"- {accepted_summary}\n"
    )
    if len(status.encode("utf-8")) > STATUS_TARGET_BYTES:
        raise PolicyError("compact STATUS projection exceeds 4 KiB")
    return {"PROJECT.md": project, "STATUS.md": status}


def render_task_thread_mapping(bindings: Sequence[TaskThreadBinding]) -> str:
    """Render the single compact LIGHT_MODE task-to-thread mapping file."""
    task_ids: set[str] = set()
    thread_ids: set[str] = set()
    rows = [
        "# Task Threads",
        "",
        "| task_id | thread_id | project_id | host_id | objective | write_scope | status | last_result |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]

    def cell(value: str) -> str:
        return value.replace("|", "\\|").replace("\r", " ").replace("\n", " ").strip()

    for binding in bindings:
        if binding.task_id in task_ids:
            raise PolicyError("TASK_THREADS contains a duplicate task_id")
        if binding.identity.thread_id in thread_ids:
            raise PolicyError("TASK_THREADS contains a duplicate thread_id")
        task_ids.add(binding.task_id)
        thread_ids.add(binding.identity.thread_id)
        rows.append(
            "| "
            + " | ".join(
                cell(value)
                for value in (
                    binding.task_id,
                    binding.identity.thread_id,
                    binding.identity.project_id,
                    binding.identity.host_id,
                    binding.objective,
                    ", ".join(binding.write_scope),
                    binding.status,
                    binding.last_result,
                )
            )
            + " |"
        )
    return "\n".join(rows).rstrip() + "\n"


def build_forward_test_fixture() -> dict[str, Any]:
    common = {
        "source_snapshot": "PIN_SAME_COMMIT",
        "model": "SAME",
        "thinking": "SAME",
        "tools": "SAME",
        "task": "SAME",
        "user_replies": "SAME",
        "acceptance": "SAME",
        "aggregate_all_supervisor_worker_reviewer_usage": True,
        "required_usage_fields": [
            "input_tokens",
            "cached_input_tokens",
            "output_tokens",
            "reasoning_tokens",
        ],
        "missing_telemetry_result": "UNVERIFIED",
        "auto_run": False,
    }
    return {
        "fixture": "FOUNDEROS_V41_FORWARD_AB",
        "common_controls": common,
        "arms": [
            {"name": "V2.3", "workflow_profile": "LEGACY_V23"},
            {"name": "V4.1", "workflow_profile": "V4_LIGHT"},
            {"name": "direct-single-agent", "workflow_profile": "DIRECT"},
        ],
        "claims_allowed_without_run": [],
    }


def _json_object(raw: str, label: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PolicyError(f"{label} must be valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise PolicyError(f"{label} must be a JSON object")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    fit = subparsers.add_parser("fit")
    fit.add_argument("--request-json", required=True)

    packet = subparsers.add_parser("packet")
    packet.add_argument("--packet-json", required=True)
    packet.add_argument("--context-kind", action="append", default=[])
    packet.add_argument("--initial-context-tokens", type=int)
    packet.add_argument("--write-scope", action="append", default=[])

    budget = subparsers.add_parser("budget")
    budget.add_argument("--hard-limit", type=int)
    budget.add_argument("--telemetry", action="store_true")
    budget.add_argument("--usage-json", default="{}")

    subparsers.add_parser("forward-fixture")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "fit":
            payload = classify_fit(UserRequest.from_mapping(_json_object(args.request_json, "request-json"))).as_dict()
        elif args.command == "packet":
            packet = TaskPacket(
                _json_object(args.packet_json, "packet-json"),
                frozenset(args.context_kind),
                args.initial_context_tokens,
                tuple(args.write_scope),
            )
            payload = {"result": "PACKET_VALID", "metrics": packet.metrics(), "rendered": packet.render()}
        elif args.command == "budget":
            ledger = BudgetLedger(args.hard_limit, telemetry_available=args.telemetry)
            usage = _json_object(args.usage_json, "usage-json")
            if args.telemetry:
                ledger.record_usage("combined", "execution", TokenUsage(**usage))
            else:
                ledger.record_proxy(**usage)
            payload = ledger.state()
        else:
            payload = build_forward_test_fixture()
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 0
    except (PolicyError, TypeError) as exc:
        print(
            json.dumps(
                {"result": "INVALID", "reason": str(exc), "changed_paths": []},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

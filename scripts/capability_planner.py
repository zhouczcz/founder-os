#!/usr/bin/env python3
"""Deterministic, read-only Capability Plan normalizer for FounderOS V2.2.

The helper does not infer professional requirements, choose project direction,
discover Skills, or grant trust. FounderOS supplies bounded task facts and the
helper makes the five-state coverage result and acquisition gate explicit.
It performs no filesystem, network, install, Registry, or Agent mutation.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any

sys.dont_write_bytecode = True


CAPABILITY_STATES = {
    "REQUIRED",
    "AVAILABLE",
    "PARTIALLY_COVERED",
    "MISSING",
    "BLOCKED",
}
COVERAGE_INPUT_STATES = CAPABILITY_STATES - {"REQUIRED"}
TASK_SIZES = {"SIMPLE", "COMPLEX"}
RISK_LEVELS = {"LOW", "MEDIUM", "HIGH", "BLOCKED"}
OPERATING_GATE = "OPERATING"
READ_ONLY_DISCOVERY_GATES = {
    "DISCOVERY_ACTIVE",
    "DIRECTION_CHECK_REQUIRED",
    "STRATEGIC_CHOICE_REQUIRED",
}
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]*\Z")


class CapabilityPlanError(ValueError):
    """The caller supplied malformed or contradictory explicit facts."""


def _identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 256:
        raise CapabilityPlanError(f"{label} must be non-empty bounded text")
    if not IDENTIFIER.fullmatch(value):
        raise CapabilityPlanError(f"{label} contains an unsafe character")
    return value


def _unique_identifiers(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or len(value) > 128:
        raise CapabilityPlanError(f"{label} must be a bounded list")
    result = [_identifier(item, f"{label} item") for item in value]
    if len(result) != len(set(item.casefold() for item in result)):
        raise CapabilityPlanError(f"{label} contains duplicates")
    return result


def plan_capabilities(
    *,
    task_id: str,
    required_capabilities: list[str],
    observed_coverage: dict[str, str],
    task_size: str,
    risk_level: str,
    general_capability_sufficient: bool,
    strategic_gate: str,
) -> dict[str, Any]:
    """Normalize explicit coverage facts without making a semantic trust decision."""

    task_id = _identifier(task_id, "task_id")
    required = _unique_identifiers(required_capabilities, "required_capabilities")
    if not isinstance(observed_coverage, dict) or len(observed_coverage) > 128:
        raise CapabilityPlanError("observed_coverage must be a bounded object")
    coverage: dict[str, str] = {}
    for raw_capability, state in observed_coverage.items():
        capability = _identifier(raw_capability, "observed capability")
        if state not in COVERAGE_INPUT_STATES:
            raise CapabilityPlanError(f"Unknown coverage state for {capability}")
        coverage[capability] = state
    if task_size not in TASK_SIZES:
        raise CapabilityPlanError("task_size must be SIMPLE or COMPLEX")
    if risk_level not in RISK_LEVELS:
        raise CapabilityPlanError("risk_level is invalid")
    if not isinstance(general_capability_sufficient, bool):
        raise CapabilityPlanError("general_capability_sufficient must be boolean")
    strategic_gate = _identifier(strategic_gate, "strategic_gate")

    explicitly_blocked = any(
        coverage.get(capability) == "BLOCKED" for capability in required
    )
    simple_no_skill = (
        task_size == "SIMPLE"
        and risk_level == "LOW"
        and general_capability_sufficient
        and not explicitly_blocked
    )
    rows: list[dict[str, str]] = []
    for capability in required:
        state = "AVAILABLE" if simple_no_skill else coverage.get(capability, "MISSING")
        rows.append(
            {
                "capability": capability,
                "status": state,
                "evidence_basis": (
                    "GENERAL_CAPABILITY_SUFFICIENT"
                    if simple_no_skill
                    else ("EXPLICIT_COVERAGE_FACT" if capability in coverage else "NO_CURRENT_COVERAGE")
                ),
            }
        )
    gaps = [
        row["capability"]
        for row in rows
        if row["status"] in {"MISSING", "PARTIALLY_COVERED"}
    ]
    blocked = [row["capability"] for row in rows if row["status"] == "BLOCKED"]
    acquisition_allowed = strategic_gate == OPERATING_GATE and not blocked
    curator_required = bool(gaps) and acquisition_allowed and not simple_no_skill
    if simple_no_skill:
        result = "NO_SKILL_REQUIRED"
        next_action = "USE_GENERAL_CAPABILITY"
    elif blocked:
        result = "CAPABILITY_BLOCKED"
        next_action = "STOP_AFFECTED_WORK"
    elif not gaps:
        result = "CAPABILITY_AVAILABLE"
        next_action = "REUSE_EXISTING_CAPABILITY"
    elif strategic_gate in READ_ONLY_DISCOVERY_GATES:
        result = "ACQUISITION_GATE_BLOCKED"
        next_action = "READ_ONLY_DISCOVERY_ONLY"
    elif not acquisition_allowed:
        result = "ACQUISITION_GATE_BLOCKED"
        next_action = "COMPLETE_CURRENT_STRATEGIC_GATE"
    else:
        result = "CAPABILITY_ACQUISITION_REQUIRED"
        next_action = "CALL_SKILL_CURATOR_JUST_IN_TIME"
    return {
        "schema": "founder-os-capability-plan/v1",
        "result": result,
        "task_id": task_id,
        "task_size": task_size,
        "risk_level": risk_level,
        "strategic_gate": strategic_gate,
        "capabilities": rows,
        "gaps": gaps,
        "blocked": blocked,
        "simple_no_skill": simple_no_skill,
        "curator_required": curator_required,
        "acquisition_allowed": acquisition_allowed,
        "next_action": next_action,
        "reuse_before_acquire": [
            "CURRENT_AGENT_OR_GENERAL_CAPABILITY",
            "PROJECT_APPROVED_SKILL_LOCK",
            "INSTALLED_BUT_UNAPPROVED_SKILL",
            "SMALL_COMPATIBLE_EXISTING_COMBINATION",
            "CURATOR_JUST_IN_TIME_IF_CRITICAL_GAP_REMAINS",
        ],
        "changed_paths": [],
        "semantic_requirement_inference": "NOT_PERFORMED",
        "trust_decision": "NOT_PERFORMED",
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request-json", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        value = json.loads(arguments.request_json)
        if not isinstance(value, dict):
            raise CapabilityPlanError("request-json must be an object")
        payload = plan_capabilities(**value)
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 0
    except (json.JSONDecodeError, TypeError, CapabilityPlanError) as exc:
        print(
            json.dumps(
                {
                    "result": "INVALID_CAPABILITY_PLAN",
                    "reason": str(exc),
                    "changed_paths": [],
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

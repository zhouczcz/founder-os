#!/usr/bin/env python3
"""Deterministic regression suite for the FounderOS skill.

The suite uses only temporary projects. It validates static requirements,
Supervisor CAS/race behavior, read-only byte stability, dependency rules, and
Integration Gate invariants. Probabilistic LLM behavior and absence of runtime
subagent tools still require separate forward/conditional testing.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.dont_write_bytecode = True
_SCRIPTS_DIR = str(Path(__file__).resolve().parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

# Bytecode writing must stay disabled before any local sibling import.
import supervisor_guard as guard_module  # noqa: E402,F401

from validation.common import *  # noqa: F401,F403
from validation.test_adoption import *  # noqa: F401,F403
from validation.test_firewall import *  # noqa: F401,F403
from validation.test_founder_discovery import *  # noqa: F401,F403
from validation.test_lightweight import *  # noqa: F401,F403
from validation.test_memory import *  # noqa: F401,F403
from validation.test_skills import *  # noqa: F401,F403
from validation.test_static_docs import *  # noqa: F401,F403
from validation.test_supervisor_guard import *  # noqa: F401,F403
from validation.test_thread_manager import *  # noqa: F401,F403


def main() -> int:
    os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    skill_before = snapshot_tree(SKILL_ROOT)
    curator_before = snapshot_tree(SKILL_CURATOR_ROOT)
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    skill_after = snapshot_tree(SKILL_ROOT)
    curator_after = snapshot_tree(SKILL_CURATOR_ROOT)
    tree_stable = skill_before == skill_after and curator_before == curator_after
    if not tree_stable:
        print("FAIL: validator changed the FounderOS or Skill Curator tree/metadata.")
    print(
        "CONDITIONAL: runtime-without-subagents cannot be reproduced when collaboration "
        "tools are present; verify fallback statically or in a capability-disabled runtime."
    )
    print(
        "FORWARD-TEST-REQUIRED: real project interviews, anti-sycophancy judgment, plan confirmation, "
        "subagent creation, lightweight recovery, parallel traces, rework, Artifact provenance, "
        "real Agent/Skill routing, actual Skill use, "
        "Persistent Thread MEMORY_SYNC, and Integration Gate behavior require fresh Codex agents; "
        "Python tests prove only the deterministic control plane."
    )
    return 0 if result.wasSuccessful() and tree_stable else 1


if __name__ == "__main__":
    raise SystemExit(main())

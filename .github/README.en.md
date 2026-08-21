# FounderOS V5.0 Project Advisor

An advisor-style technical counselor Skill for solo developers: **it thinks, you build**. It distills your messy ideas into confirmed requirements, checks direction against project state, generates task prompts, and ingests results to keep the project summary current. Before giving any advice it realigns with the project's current state via the git delta, so recommendations rest on reality rather than stale memory. Implementation happens in work conversations that you drive yourself — after confirming with you what a conversation is meant to accomplish, the advisor may fire-and-forget the prompt into a fresh one, but it never waits on, polls, or reads conversations (you relay the output), never writes business code, and never runs builds or tests.

## The loop

```text
You describe an idea (rambling voice-transcribed input is fine)
  → Advisor realigns with current project state (incremental if HEAD moved)
  → Idea clarification: organize → ask gaps → read back ("is this right?")
  → Fit check: duplicates / conflicts / simpler paths / risks
  → Task prompt: lean brief by default (GOAL/SCOPE/TESTS/REPORT),
    full six fields for big tasks
  → Dispatch check: confirm what this conversation should accomplish
  → Advisor fires it into a fresh work conversation (or you paste it);
    you drive execution and acceptance yourself
  → Relay worker output back anytime: advisor answers questions and
    drafts revision prompts from it
  → Say "read the result": advisor reads the git delta + REPORT block,
    updates project state, suggests next steps
```

When the advisor session itself grows long, it proactively suggests rotating: settle the books first, then start a fresh advisor session. `.founder` plus git is the only handoff — conclusions that were overturned in the old chat never follow you into the new one.

## Why V5

V4 was a supervisor that created and drove real work conversations, accepted deliverables, and managed rework under a full governance protocol. In practice, for a solo developer the orchestration and redundant verification cost far more than they returned (see `legacy/`). V5 hands execution back to the user; the advisor keeps exactly three jobs: **think clearly (clarification and anti-sycophancy), watch direction (fit checks and recommendations), remember everything (`.founder` project state)**.

The advisor's entire write scope is `.founder/PROJECT.md`, `.founder/STATUS.md`, and `.founder/DECISIONS.md`.

## Layout

```text
founder-os/
├── SKILL.md                     # V5 advisor protocol (the only default load)
├── agents/openai.yaml           # UI metadata
├── references/
│   └── prompt-playbook.md       # task prompt template and worked examples
├── scripts/
│   └── validate_founder_os.py   # static regression suite
└── legacy/                      # archived V4.1 supervisor protocol (not installed, never loaded)
    ├── SKILL-v41.md
    ├── references/              # old governance protocols (delegation, thread-manager, ...)
    └── scripts/                 # old helper scripts and the 448-test legacy suite
```

## Validation

```bash
python -B scripts/validate_founder_os.py
```

Pure static checks: protocol boundaries, the clarification loop, the prompt template, the result-ingest protocol, state-file rules, and retirement of orchestration vocabulary. No runtime simulation, no external dependencies.

## Project state files

| File | Contents |
|---|---|
| `.founder/PROJECT.md` | goals, stack, module map, constraints, build/test commands, context capsule |
| `.founder/STATUS.md` | ≤4KiB: last_indexed_commit, open tasks, recent completions, blockers, known issues |
| `.founder/DECISIONS.md` | major decisions only |

Projects with an older `.founder` (V4 / five-ledger era) keep every old file as history; the first advisor session compresses PROJECT/STATUS once and records `workflow_profile=V5_ADVISOR`.

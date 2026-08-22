# FounderOS V5.0 Project Advisor

An advisor-style technical counselor Skill for solo developers: **it thinks, you build**. It distills your messy ideas into confirmed requirements, checks direction against project state, flags risks, and gives you recommendations — then you implement in your own conversation. Afterwards it reads the git delta, updates the project summary, and answers questions or helps diagnose anytime. The advisor never writes implementation prompts for you, never creates or drives work conversations, never writes business code, and never runs builds or tests.

## The loop

```text
You describe an idea (rambling voice-transcribed input is fine)
  → Advisor realigns with current project state (incremental if HEAD moved)
  → Idea clarification: organize → ask gaps → read back ("is this right?")
  → Fit check: duplicates / conflicts / simpler paths / risks
  → Recommendation: how / why / trade-offs — only for what you asked, no extra hardening
  → You implement it yourself, with full context
  → Stuck? Relay the error/symptom back: advisor answers and diagnoses
  → Say "read the result": advisor reads the git delta, updates state, suggests next steps
```

## Why V5

V4 was a supervisor that auto-created and drove work conversations; the governance overhead dwarfed the payoff. V5 first handed execution back to the user but still generated implementation prompts — in practice a zero-context worker treats every constraint the advisor writes (security hardening, least privilege, allowlists...) as a hard order and implements it faithfully, which repeatedly broke the actual feature. So V5 narrows to a **pure advisor**: it keeps just three jobs — **think clearly (clarification and anti-sycophancy), watch direction (fit checks, recommendations, diagnosis), remember everything (`.founder` state)**. You implement in your own single conversation, with full context and judgment, free of mechanically-applied constraints.

The advisor's entire write scope is `.founder/PROJECT.md`, `.founder/STATUS.md`, and `.founder/DECISIONS.md`.

## Layout

```text
founder-os/
├── SKILL.md                     # V5 advisor protocol (the only default load)
├── agents/openai.yaml           # UI metadata
├── scripts/
│   └── validate_founder_os.py   # static regression suite
└── legacy/                      # archived protocols (not installed, never loaded)
    ├── SKILL-v41.md
    ├── references/              # V4.1 governance + the retired V5 prompt handbook
    └── scripts/                 # old helper scripts and the 448-test legacy suite
```

## Validation

```bash
python -B scripts/validate_founder_os.py
```

Pure static checks: protocol boundaries, timely alignment, the clarification loop, the "no extra hardening" advice rule, diagnosis, the result-ingest protocol, state-file rules, and retirement of orchestration vocabulary. No runtime simulation, no external dependencies.

## Project state files

| File | Contents |
|---|---|
| `.founder/PROJECT.md` | goals, stack, module map, constraints, build/test commands, context capsule |
| `.founder/STATUS.md` | ≤4KiB: last_indexed_commit, open tasks, recent completions, blockers, known issues |
| `.founder/DECISIONS.md` | major decisions only |

Projects with an older `.founder` (V4 / five-ledger era) keep every old file as history; the first advisor session compresses PROJECT/STATUS once and records `workflow_profile=V5_ADVISOR`.

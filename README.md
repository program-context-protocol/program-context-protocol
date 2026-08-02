# Program Context Protocol (PCP)

**Your AI coding agent says "done." It isn't.**

Long agent sessions drift: the agent forgets what the objective actually was,
marks criteria complete that don't work, drops modularity, and nobody catches
it until it's in production. PCP is a structured `.pcp/` directory + CLI that
stops this — deterministic CI gates, auto-generated state (never hand-typed,
never stale), and a validator that checks whether your build actually still
covers your objective.

If you're searching for: *LLM hallucinated "done"*, *AI agent context drift*,
*spec drift across dev sessions*, *keep Claude/Copilot/Cursor aligned with
the objective*, *prevent scope drift in agentic coding* — this is that tool.

## What makes this different

Spec-as-truth (write a spec, build against it) is now common — Spec Kit,
BMAD, OpenSpec all do it. None of them close the loop:

| Tool | Gap PCP closes |
|---|---|
| Spec Kit, OpenSpec, BMAD | Spec-as-truth is mainstream — none block a drifted commit or deploy |
| Kiro (AWS), Tessl | Proprietary |
| Swimm, Fiberplane, Grit | Doc-vs-code only, pairwise — no program-level objective coverage |
| Agent-governance tools (Endor, LaneKeep) | Gate tool calls, not spec alignment |

**Pioneer claim:** `pcp validate-strategy` checks whether your module
decomposition still collectively covers the stated objective — deterministic
coupling analysis (circular deps, God modules), not vibes. No other tool in
this space does this.

## How it works

```
objective.md (human-approved, immutable to unattended agents)
     ↓
strategy/decomposition.md  ←──  pcp validate-strategy
     ↓
modules/*/spec.yaml        ←──  pcp validate-module
     ↓
[agent codes]
     ↓
pre-commit:  pcp check         → Layer 1, deterministic AST/schema, hard block
PR:          pcp gate          → Layer 2, LLM advisory score, logged
deploy:      pcp deploy-check  → Layer 3, phase exit criteria, hard block
```

`current_state.md` and `diff.md` are always auto-generated from your actual
code — never hand-written, never allowed to go stale.

## Logic-tier ladder — not everything is an LLM's job

Every piece of judgment-requiring logic a PCP-built project writes gets
routed to the cheapest tool that can correctly make it, cheapest-first:

| Rung | What it is | When it applies |
|---|---|---|
| 1. Deterministic | if/else, lookup table | Fixed rules, one correct output |
| 2. Solver/optimization | OR-Tools, CBC | Constraints+objective known, answer isn't |
| 3. Statistical/ML | sklearn, HuggingFace | Pattern learned from historical data |
| 4. RAG | retrieval + light synthesis | Answer exists in a bounded corpus |
| 5. Cached reuse | lru_cache, diskcache | Replay a near-duplicate prior answer |
| 6. Deep-think LLM | last resort | Two competent humans would reasonably disagree |

Enforced via schema-validated `logic_tier` fields per acceptance criterion +
CI drift checks (a criterion that claims rung ≤5 but imports an LLM SDK
fails the gate). Most agentic-coding tools default everything to rung 6 —
this is the difference between "call the LLM" and "decide whether you
should."

## Prior-art gate

Before scaffolding a non-trivial module (auth, payments, queues, parsers,
state machines, a canvas/diagram editor, PDF processing — anything a mature
library probably already solves) PCP runs a prior-art check: search
GitHub/npm/PyPI, shortlist candidates, check license compatibility, decide
reuse-as-dependency / fork-adapt / reference-pattern-only / build-fresh
*before* code gets written. Rationale is recorded per module, not left to
whether the agent happened to think of it that day.

## Install

Requires `git` and the `claude` CLI on `PATH`.

```
pip install program-context-protocol
pcp init
```

Run `pcp doctor` to check your environment. To develop PCP itself instead
(clone + editable install):

```
git clone https://github.com/program-context-protocol/program-context-protocol
cd program-context-protocol
pip install -e .
```

## Status

Pre-launch — validating across real dogfood projects before a public 1.0.
Core loop (schema, validate-strategy, scan, Layer 1/2/3 gates) is built and
tested.

## License

MIT OR Apache-2.0

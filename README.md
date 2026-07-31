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

## Install

```
pip install -e .
pcp init
```

Requires `git` and the `claude` CLI on `PATH`. Run `pcp doctor` to check
your environment.

## Status

Pre-launch — validating across real dogfood projects before a public 1.0.
Core loop (schema, validate-strategy, scan, Layer 1/2/3 gates) is built and
tested.

## License

MIT OR Apache-2.0

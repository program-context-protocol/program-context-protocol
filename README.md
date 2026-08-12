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
pcp kickoff <vision.md>    →  objective.md + module specs (human-approved,
                               immutable to unattended agents from here on)
     ↓
strategy/decomposition.md  ←──  pcp validate-strategy
     ↓
modules/*/spec.yaml        ←──  pcp validate-module
     ↓
pcp build  (or pcp pm "<intent>" for a later feature ask)
     ↓
pre-commit:  pcp check         → Layer 1, deterministic AST/schema, hard block
PR:          pcp gate          → Layer 2, LLM advisory score, logged
deploy:      pcp deploy-check  → Layer 3, phase exit criteria, hard block
```

`current_state.md` and `diff.md` are always auto-generated from your actual
code — never hand-written, never allowed to go stale.

## Four hard constraints, not four opinions

PCP treats these as enforced constraints on every build, not style guidance
an agent can quietly skip under deadline pressure.

### Modularity

Every module is a guest — can leave without drama, arrive without surgery.
Beyond coupling analysis (circular deps, God modules), PCP catches the
shared-data-model drift that breaks multi-agent builds silently: when two
modules both touch the same entity (e.g. `Task`, `Order`) with no declared
owner, each build agent invents its own shape independently. A module
declares `owns_entities` for what it canonically owns; any other module
referencing it must declare a real `dependencies` edge back to the owner —
checked deterministically, not left to whether the agent noticed. Same
mechanism extends to `dependency_map.md` (auto-generated module build order +
inter-module contracts) and per-criterion `screen` grouping, so a BRD-implied
page can't silently end up with zero criteria behind it.

### Token discipline

Every LLM call site passes an explicit model — no silent default. Judge and
advisory calls (`validate-strategy`, `gate`, `architect-review`) route to a
cheap model; generation calls (`kickoff`, `pm`) and early build attempts use
a stronger one; escalation to the most expensive model only happens on a
3rd-attempt retry, and a human's explicit model override disables escalation
entirely. Every call logs usage and cost. Spend ceilings refuse to spawn new
work past a limit — they never kill an attempt mid-flight. Prior decisions
feed back into later build prompts as bounded, deterministic context instead
of re-pasting session history.

### Logic-tier selection — not everything is an LLM's job

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

### Design

Design sits peer to the other three — not folded into architecture. A
5-stage lifecycle: establish a project-level design system once (tokens,
both themes), justify every later screen against it (JTBD framing — "when a
user is X, this lets them Y"), build against the system, verify with a
deterministic checklist gate that flags hardcoded colors when real tokens
already exist, and roll up into a Feature Exposure Ladder — computed, not
self-declared: built-but-hidden, exposed-but-undiscoverable,
exposed-discoverable, exposed-enriched. A feature exposed through an API or
background job instead of a screen can opt out with a real justification, so
it's scored correctly instead of read as a discoverability failure.

## Prior-art gate

Before scaffolding a non-trivial module (auth, payments, queues, parsers,
state machines, a canvas/diagram editor, PDF processing — anything a mature
library probably already solves) PCP runs a prior-art check: search
GitHub/npm/PyPI, shortlist candidates, check license compatibility, decide
reuse-as-dependency / fork-adapt / reference-pattern-only / build-fresh
*before* code gets written. Rationale is recorded per module, not left to
whether the agent happened to think of it that day.

## Grounded against real prior art, not just the vision doc

Module/capability coverage checks only ever compare against what the vision
doc says — an omitted-but-standard capability for that product category is
silently absent, never flagged. `pcp inspiration-art "<description>"`
proposes the product's category (or categories — many real products span
more than one) with each category's typical modules and typical screens,
human-approved into `.pcp/strategy/inspiration_art.md`. `pcp kickoff`/`pcp pm`
read it as grounding; a module can trace itself back to a researched section
via `category_reference`. Reactive use too: run it with `--gap "<capability>"`
when a coverage check flags something missing.

## The build agent doesn't get the last word on itself

The same session that writes the code routinely writes the test too — a test
shaped around what's about to be built, not the actual requirement, can pass
every gate that only checks "do tests pass" or "is this architecturally
consistent." For criteria you mark as carrying real logic (`adversarial_review:
true` — a scoring model, a validation rule, anything beyond CRUD), PCP spawns
a second, independent coding-agent session with one job: try to prove the
first agent's tests are tautological, mocked-around, or that the
implementation is a stub dressed up to look real. It reads the actual test
file, reads the actual implementation, and runs the tests itself rather than
trusting that green means real. A criterion only survives if that independent
pass genuinely couldn't find a problem — not because nothing looked
suspicious at a glance.

## Audit-grade evidence, not just green checkmarks

Every gate call tags its telemetry record with a control ID and the files in
scope — a skipped check, a judge-call error, and a bypass commit are all
distinguishable from a clean pass, never conflated into silence. Three logs
(build telemetry, decision log, bypass log) are hash-chained and marked
append-only at the OS level, so an edit after the fact is both detectable
and, short of a privileged actor, blocked outright. `pcp provenance` rolls
this up into per-file × control coverage cross-referenced to NIST SP 800-218
(SSDF) practices; `pcp dashboard` renders it as a static, git-shareable HTML
page — no server, works offline.

## CLI at a glance

| Command | What it does |
|---|---|
| `pcp init` | Scaffold `.pcp/`, install git hooks |
| `pcp kickoff <vision.md>` | Vision doc → module specs (decompose-first) |
| `pcp pm "<intent>"` | Feature intent → criteria changes on existing specs |
| `pcp build-plan` / `pcp build` | Deterministic build schedule / headless coding loop |
| `pcp validate-strategy` | Does the module decomposition still cover the objective? |
| `pcp scan` / `pcp check` / `pcp gate` / `pcp deploy-check` | Auto-generate state / Layer 1 / Layer 2 / Layer 3 |
| `pcp provenance` / `pcp dashboard` | Audit-evidence rollup / static HTML report |
| `pcp doctor` | Environment preflight |

Every command supports `--help`. This table is the map, not the whole
territory — `pcp doctor` and `pcp --help` are the live source of truth.

## Install

Requires `git` and the `claude` CLI on `PATH`. `opa` (Open Policy Agent) is
optional — without it, OPA-backed checks (escalation routing, bypass-reason
rejection, coupling-threshold bands) fall back to conservative hardcoded
defaults instead of failing.

```
pip install program-context-protocol
pcp init
```

Run `pcp doctor` to check your environment — it reports exactly which
required/optional tools it found. `pcp init`'s own `.gitignore` scaffold
tracks `.pcp/`'s governance files (`objective.md`, specs, `ci_rules.yaml`)
in git deliberately — they're the spec-as-truth this tool is built around —
and ignores only the run-time operational writes (`telemetry.jsonl`,
`decision_log.jsonl`, evidence/transcripts), the same way you'd ignore a
log file. (This repo's own `.pcp/` is a deliberate exception: it dogfoods
itself, and gitignores that state entirely as internal-only — don't expect
your own project's `.pcp/` to behave that way.)

`pcp build --yes` / `pcp deploy --yes` skip the interactive confirm gating
a `shell=True` install/deploy step sourced from `.pcp/integrations.yaml` or
a build candidate's own install command — not a sandbox, no allowlist. Only
use `--yes` where you already trust that config.

To develop PCP itself instead (clone + editable install):

```
git clone https://github.com/program-context-protocol/program-context-protocol
cd program-context-protocol
pip install -e .
```

When something fails: `pcp doctor --check` first, then `.pcp/build_report.md`
(per-criterion evidence after a run), `pcp build-status` (live view of one in
progress), `pcp escalations` (anything flagged for a human — `ack` ≠
resolved), or `pcp telemetry`/`pcp provenance` for the full per-control audit
trail.

## Status

Pre-launch — validating across real dogfood projects before a public 1.0.
Core loop (schema, validate-strategy, scan, Layer 1/2/3 gates) is built and
tested.

## License

MIT OR Apache-2.0

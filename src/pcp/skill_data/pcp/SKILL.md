---
name: pcp
version: "1.0.0"
description: "Program Context Protocol — full autonomous software factory. PM describes vision; PCP runs structured discovery, generates BRD, translates to modular specs, builds via the Workflow tool's native parallelism (pcp build-plan computes the schedule, the harness governs concurrency), runs all gates (TDD, architect-review, CI), auto-fixes failures, merges, deploys. PM only touches: vision input, visual approvals, escalations. Invoke with /pcp."
---

# /pcp

Autonomous software factory. PM describes what to build. PCP builds it.

**Three modes:**
1. `/pcp new` — Vision workshop → BRD → spec scaffold
2. `/pcp build` — Parallel autonomous build (concurrency governed by the Workflow tool, not a manual count)
3. `/pcp status` — Project health across all projects

**The contract:** PM inputs vision and approves milestones. PCP handles everything else. "Build" means deployed and verified — not written.

---

## Usage

```
/pcp new                          # start vision workshop → BRD → .pcp/ scaffold (greenfield)
/pcp new --from-brd <file>        # skip workshop, generate .pcp/ from existing BRD
/pcp import "<description>"       # brownfield: graphify → clusters → draft specs → PM review
/pcp build                        # build next wave via pcp build-plan + Workflow tool
/pcp build --module <name>        # build one module
/pcp build --all                  # build entire backlog, full wave scheduling
                                   # (no --agents flag: Workflow's own concurrency cap
                                   #  governs how many run at once, not a manual number)
/pcp status                       # current project: phase, %, CI, deferred queue
/pcp status --all                 # all projects under ~/Claude-code/
/pcp watch                        # watch Railway + GitHub Actions
/pcp fix                          # diagnose + fix current CI failure
/pcp approve <module>/<id>        # unblock a deferred criterion (manual/visual)
/pcp feedback <module>/<id> "<text>"  # give feedback on a visual criterion, rebuild
/pcp accept-adr <id>              # accept a DRAFT ADR written autonomously
/pcp override-adr <id> "<decision>"  # override PCP's autonomous architecture decision
/pcp review <module>              # architect-review on module spec
/pcp review <type>                # architecture | logic | design | security | qa — see REVIEW ROUTER below
/pcp diff                         # target vs current gap
```

**Language rule — non-negotiable:** Everything the PM reads is in product language. Technical terms (module, spec, coupling, AST, CI, YAML, schema, criterion, pre-commit, branch, commit hash, coverage %) never appear in PM-facing output. Translate before presenting. Technical language is internal — it lives in files and logs, not in conversation.

| Technical | Product language |
|---|---|
| "3 CI failures" | "3 things broke — fixing now" |
| "coupling violation: payments ↔ webhooks" | "payments and notifications are too tangled — I'm separating them" |
| "acceptance criterion BF_001 pending" | "still working on: [feature name]" |
| "coverage 72%" | "28% of what you described isn't built yet" |
| "merge conflict on feat/auth" | "two parts of the build clashed — resolving" |
| "pcp validate-strategy failed" | "the build plan doesn't fully cover what you described — adjusting" |

**Unattended operation:** PCP runs fully autonomously while you are away. Escalations go to Slack — not to a blocking prompt. `/pcp watch` monitors CI and Railway for failures and auto-fixes them. When you return, run `/pcp` to see what was built, what is deferred, and what needs your input. Build never stops for input that can be deferred.

---

## REVIEW ROUTER — the PM should never need to remember a command name

Real gap found dogfooding: PCP has grown ~32 CLI commands across the whole lifecycle
(architecture, logic-tier decisions, design/UI, security, QA, deploy). A PM asking to
"review the UI strategy" or "check the security posture" shouldn't need to know the
exact command (`design-audit`, `architecture-justification`, `check`...) exists, let
alone spell it correctly. This section is PCP's own lookup table — read it, don't guess
a command name, and don't tell the PM the technical name unless they ask (same
Language Rule as above applies here too).

### Intent → command lookup

| PM says something like... | Run this |
|---|---|
| "review the architecture" / "does this still make sense structurally" / "check coupling" | `pcp architect-review` (advisory, per-diff), `pcp validate-strategy` (coverage_score + coupling_score, Pass 2), `pcp validate-module <name>` (Pass 1) |
| "review the logic" / "are we using the right tool for this" / "check build-vs-buy" / "did we reinvent something that already exists" | `pcp architecture-justification` — rolls up every criterion's `logic_tier` (the 6-rung deterministic→deep-think ladder) and `build_vs_buy` decision |
| "review the design" / "review the UI" / "is this discoverable" / "does this look consistent across screens" | `pcp design-audit` (Feature Exposure Ladder — how discoverable each UI-facing criterion actually is) — for the design-thinking pass on one specific screen, use the `pcp-ui-design` skill (`~/.claude/skills/pcp-ui-design/SKILL.md`), not this rollup |
| "review security" / "any secrets" / "check for vulnerabilities" | `pcp check` (Layer 1 — deterministic, hard block: hardcoded secrets, eval/exec, disabled TLS, string-built SQL, plus any project-specific `hard_block` rules in `ci_rules.yaml`) |
| "check before merging" / "PR review" / "does this align with what we asked for" | `pcp gate` (Layer 2 — LLM alignment score, advisory, never hard-blocks) |
| "are we ready to deploy" / "check the deploy gate" | `pcp deploy-check` (Layer 3 — SDLC phase exit criteria, hard block) |
| "review QA" / "how's the build going" / "any flaky retries" / "what's this costing" | `pcp telemetry` (per-module retries, QA error rate, cost, languages), `pcp provenance` (full audit-evidence document — SSDF crosswalk, bypass ledger, chain integrity) |
| "any dead code" / "bloat check" | `pcp audit` (advisory, never blocks) |
| "what's the overall status" / "show me everything" | `pcp dashboard` — single HTML, 5 tabs (Overview / Objective & Gaps / Audit Trail / Architecture Justification / Design), already visualizes most of the rows above. Open this first before running things piecemeal. |
| "what's blocked or bypassed" | `pcp report` (bypass history, coverage score, gate outcomes) |
| "give me a plain-English status" | `pcp status --pm` |
| "what changed in this module" / "module history" / "drift ledger for X" | `pcp docs [--module <name>]` — per-module vision/BRD/built/changelog, `changelog.md` is a drift ledger with a computed drift score (in-flight spec changes, bypasses, retries) |
| "clean up old logs" / "disk is full" / "prune old evidence" | `pcp prune [--evidence-days N] [--transcript-days N]` — deletes stale `.pcp/evidence/`/`.pcp/transcripts/` past a retention window; no-op unless configured, `--dry-run` to preview, mandatory confirmation unless `--yes` |

### Full command reference, by lifecycle stage (all current as of this skill's own version — re-check `pcp --help` if something here feels stale)

**Setup / onboarding**
- `pcp init` — scaffold `.pcp/`
- `pcp kickoff <vision.md>` — generate specs from a vision doc
- `pcp import "<description>"` — brownfield onboarding (graphify clusters → draft specs)
- `pcp takeover` — end-to-end: preflight + kickoff + build everything pending
- `pcp doctor` — check/configure environment tool integrations
- `pcp install-hook` / `pcp install-skill` — one-time wiring

**Strategy / architecture**
- `pcp validate-strategy` — coverage_score + coupling_score (Pass 2: modules vs objective)
- `pcp validate-module <name>` — module vs objective/decomposition (Pass 1)
- `pcp architect-review` — architecture principle review against persona + KB (advisory)
- `pcp architecture-justification [--json]` — `logic_tier`/`build_vs_buy` rollup

**Design** (see CLAUDE.md's "PCP Design as a Hard Constraint" for the full 5-stage lifecycle this fits into)
- `pcp design-audit [--json]` — Feature Exposure Ladder rollup
- skill `pcp-ui-design` — design-thinking pass on one UI-facing criterion, establishes/reuses `.pcp/design_system.md`

**Build**
- `pcp build [--module <name> | --all]` — autonomous coding loops per pending criterion
- `pcp pm "<intent>"` — translate natural-language feature intent to spec/acceptance changes

**Gates — the 3-tier system**
- `pcp check` — Layer 1, pre-commit, deterministic, hard block, no LLM
- `pcp gate` — Layer 2, PR, LLM alignment score, advisory
- `pcp deploy-check` — Layer 3, deploy, SDLC phase-exit criteria, hard block

**QA / audit / evidence**
- `pcp telemetry` — build-efficiency rollup (retries, QA error rate, cost, languages)
- `pcp provenance [--json]` — audit-evidence document (SSDF crosswalk, bypass ledger, chain integrity)
- `pcp audit` — dead-code/bloat scan, advisory, never blocks
- `pcp report` — bypass history, coverage score, gate outcomes
- `pcp capture [--transcript-file <path>]` — classify a session transcript into BRD/decision-log drift

**Status / drift**
- `pcp scan [--coverage]` — regenerate `.pcp/current_state.md`
- `pcp diff` — target vs current state gap
- `pcp status [--pm] [--rescan] [--print]` — `pcp.md` snapshot, or a plain-English PM report
- `pcp dashboard` — unified 5-tab HTML view (see above)
- `pcp context` — dump `.pcp/` context for LLM consumption at session start
- `pcp docs [--module <name>]` — per-module doc kit (vision/BRD/built/changelog); `changelog.md` is a chronological drift ledger with a computed drift score, not just a build log

**Deploy / ops**
- `pcp deploy [--yes] [--rollout N]` — checklist → Layer 3 gate → approval → trigger → smoke test → auto-rollback
- `pcp watch [--once] [--interval N]` — continuous CI/deploy monitoring, auto-fix on failure
- `pcp prune [--evidence-days N] [--transcript-days N] [--dry-run] [--yes]` — retention cleanup for `.pcp/evidence/`/`.pcp/transcripts/` (the two directories that actually grow unbounded); does nothing unless a retention window is configured or passed; mandatory confirmation like `pcp deploy`, `--yes` opts out

**Narrow / utility**
- `pcp verify-syntax-fix <file>` — deterministic SAFE/UNSAFE verdict for a protected-path syntax-only edit (used by the `verify-syntax-fix` PreToolUse hook, not typically invoked directly by a PM)

---

## BROWNFIELD IMPORT (`/pcp import "<description>"`)

For existing codebases with no `.pcp/` context. PM provides one paragraph describing what the codebase delivers — that's the only required input.

### Step 1 — Write objective.md

PM description → `.pcp/objective.md`. Done immediately. No workshop needed.

### Step 2 — Graphify cluster detection

Invoke `/graphify` on the entire codebase. Input: all source files (imports, function calls, class references, shared state). Output: knowledge graph with cluster detection.

```
Clusters = tightly coupled file groups = natural module candidates
Cross-cluster edges = inter-module dependencies = contract boundaries
Dense cross-cluster edges = coupling violations (flagged immediately)
```

Why graphify instead of directory scan: flat repos with 300 files across mixed dirs have no meaningful directory structure. Graphify finds real boundaries from actual code relationships, not assumed from folder names.

### Step 3 — Present module map to PM

```
Graphify found 7 clusters:

  auth-cluster     auth.py, session.py, jwt_utils.py, bcrypt_helper.py
  payments-cluster stripe_client.py, charge.py, refund.py, invoice.py
  webhooks-cluster webhook.py, event_queue.py, retry.py
  api-cluster      routes.py, middleware.py, serializers.py
  db-cluster       models.py, migrations/, connection.py
  tasks-cluster    celery.py, workers/, scheduler.py
  shared           config.py, utils.py, constants.py  ← infrastructure, not a module

Suggested module names (from PM description + file names):
  auth-cluster     → auth
  payments-cluster → payments
  webhooks-cluster → webhooks
  api-cluster      → api-gateway
  db-cluster       → data-layer
  tasks-cluster    → task-runner

Cross-cluster edges (coupling violations — fix in Wave 0):
  payments ↔ webhooks: 14 direct calls
  api-gateway ↔ db-cluster: direct model access (bypasses data-layer)

Rename, split, or merge any modules before I generate specs.
```

Wait for PM review. Iterate until approved.

### Step 4 — Baseline scan

```bash
pcp scan              # current_state.md = existing code as-is
pcp check --baseline  # catalog pre-existing violations → .pcp/baseline_violations.yaml
                      # NOT blocking. Counting what's already broken.
pcp validate-strategy # coverage score + coupling score on day 1
```

Report to PM:
```
Baseline: <project>

Objective coverage:     72%  (28% of your description has no code yet)
Coupling score:         0.3  (high — pivots will be expensive)
Pre-existing violations: 23 MOD_001, 4 circular deps, 11 SEC_001
Test coverage:          34%

Recommended wave order:
  Wave 0 — characterization tests + decouple (no new features)
  Wave 1+ — new features per objective gaps
```

### Step 5 — Generate draft `.pcp/`

```
.pcp/objective.md                         ← from PM description (immutable now)
.pcp/architecture.md                      ← detected stack + patterns
.pcp/strategy/decomposition.md            ← approved cluster→module map
.pcp/strategy/modules/<name>/spec.yaml    ← _generated: true, PM must review
.pcp/strategy/modules/<name>/acceptance.yaml  ← mined from test files if exist, else empty
.pcp/baseline_violations.yaml             ← pre-existing violations, shrinks as Wave 0 completes
```

All generated spec files have `_generated: true`. PM reviews each, removes the flag when correct. Once flag removed: spec is immutable — agents never modify it.

### Step 6 — Auto-generate Wave 0 criteria

From baseline scan, PCP generates brownfield-specific acceptance criteria:

```yaml
# auto-generated in each module's acceptance.yaml
- id: BF_001
  description: "Write characterization tests for <module> (golden master — no code changes)"
  check: test_passes
  test: "tests/characterization/test_<module>_baseline.py"
  status: pending

- id: BF_002  # only if coupling violations exist
  description: "Remove direct <moduleA>→<moduleB> calls, route through interface"
  check: ast_pattern
  pattern: "from ...<moduleB> import"   # must not appear in moduleA after fix
  status: pending

- id: BF_003  # one per god module detected
  description: "Extract <domain> from <god_module> into standalone boundary"
  check: file_exists
  target: "src/<domain>/__init__.py"
  status: pending
```

Wave 0 has no new features. Code behaves identically before and after. Tests prove it.

---

### Brownfield TDD — three criterion types

| Type | Code exists? | TDD behavior |
|---|---|---|
| **Existing behavior** | Yes, working | Write tests → GREEN immediately. If RED → code broken, fix it. |
| **New feature** | No | Normal TDD: RED → write code → GREEN |
| **Refactor/decouple** | Yes, but wrong shape | Characterization tests first → refactor → tests still GREEN |

Criterion agent detects which type before writing any code:
```bash
# Check if criterion target already exists
ls <target_file> 2>/dev/null && echo "EXISTS" || echo "NEW"
# Check if tests already pass
pytest tests/test_<criterion>.py -q 2>/dev/null && echo "GREEN" || echo "RED"
```

---

### Brownfield `pcp check` — staged-only mode

Existing code has violations everywhere. Full-codebase check would block immediately.

```bash
# Greenfield pcp check: entire codebase
# Brownfield pcp check: staged changes only (new code must be clean, old code tracked separately)
git diff --staged | pcp check --staged-only
```

Violations in `baseline_violations.yaml` are excluded from block gate. As Wave 0 decoupling criteria complete, violations are removed from baseline. When `baseline_violations.yaml` reaches zero: project automatically switches to full greenfield `pcp check` rules.

---

### Full brownfield lifecycle

```
/pcp import "<description>"
  → objective.md written
  → /graphify → clusters → module map → PM approves
  → baseline scan → current_state.md + baseline_violations.yaml
  → draft .pcp/ generated, PM reviews specs
  ↓
Wave 0  (brownfield only — no new features)
  → characterization tests per module
  → decouple cross-cluster violations
  → extract god modules
  → baseline_violations.yaml shrinks toward zero
  ↓
Wave 1+  (same as greenfield)
  → TDD for new features / objective gaps
  → parallel agents, CI gates, merge coordinator
  ↓
baseline_violations.yaml = empty
  → full greenfield rules apply, brownfield mode retired
```

---

## PHASE 1: VISION WORKSHOP (`/pcp new`)

Triggered by `/pcp new` in any directory (with or without existing `.pcp/`).

### Step 1 — Product Discovery Interview

Target user: technically aware, product-thinking, non-coding. PCP is their engineering team. They are the PM/founder. All questions are product questions — no technical jargon. PCP makes all technology decisions autonomously after the interview.

Ask ONE AT A TIME. Wait for answers. Do not batch. Follow up on vague answers before moving on.

```
1. What problem are you solving? Tell me about a real moment when you 
   or someone you know ran into this problem.

2. Who is the person with this problem? What do they do today to 
   work around it — before your product exists?

3. Walk me through your product step by step from the user's point 
   of view. What happens when they first open it? What do they do next?

4. What are the 3-5 things this product MUST do for it to be useful?
   (outcomes, not features — what does the user achieve?)

5. Who creates content or data in your product? Who views or uses it?
   (e.g. "sellers list products, buyers browse and purchase")

6. Do users need accounts? How do they sign up or log in?
   (e.g. "sign in with Google", "email + password", "invite-only")

7. Is money involved? How does it work?
   (subscriptions, one-time payment, marketplace take-rate, free)

8. Does your product connect to anything the user already uses?
   (e.g. "pulls from their Google Calendar", "sends Slack messages")

9. What is explicitly NOT in version 1?
   (what are you leaving out on purpose)

10. Show me "done": describe the moment you demo this to someone and 
    they say "yes, this is exactly what I need."

11. Is there a deadline or launch event driving timing?
```

Follow-up questions based on answers — no fixed limit. Keep going until the picture is complete. If the user mentions something ambiguous ("some kind of dashboard"), drill in: "What exactly does that show? Who looks at it? How often?"

After all answers, synthesise in product language only:

```
Here's what I understood:

Problem: <one line — the pain>
User: <one line — who they are and what they do today>
Core outcomes:
  - <what user achieves, not feature name>
  - ...
User flows:
  - <flow 1: actor does X → sees Y → achieves Z>
  - <flow 2: ...>
Accounts: <yes/no, how>
Money: <yes/no, model>
Integrations: <external systems named>
Out of scope: <list>
Done looks like: <demo scenario>
Timeline: <constraint or "ship when ready">

Is this right? Tell me anything I missed or got wrong.
```

Iterate until PM says "yes" / "correct" / "go". Do not proceed until confirmed.

After confirmed: PCP decides all technology internally. Do NOT ask about stack, framework, database, deployment target, or hosting. Infer from what's being built:
- Web app with auth + data → modern web stack, Railway deploy
- API-only product → lightweight backend, Railway deploy
- Mobile-first → cross-platform mobile
- Desktop tool → native desktop
Present the choice to PM in one plain sentence after BRD is written: "I'll build this as a web app you can use in any browser, hosted so anyone with the link can access it." No technical details unless PM asks.

### Step 2 — Generate BRD

Using confirmed answers, generate a structured BRD as `brd.md` in the project root.

BRD sections — product language throughout, no technical terms visible to PM:
1. Problem Statement
2. Target User (persona + current workaround)
3. Core User Flows (step-by-step, from user's point of view)
4. Features (what the product does — plain English, no tech)
5. Out of Scope
6. How Users Access It (web / mobile / desktop — one sentence)
7. Money Model (if any)
8. Integrations (external services)
9. Success: what "done" looks like in observable, testable terms
10. Open Questions

PCP appends internally (not shown to PM):
- Technology stack decision (PCP's choice, not PM's)
- Architecture principles (derived from product requirements)
- Module decomposition (internal planning)

After writing `brd.md`, present module breakdown in product language:

```
BRD written.

Here's how I'll build this — each part does one job:

  User accounts     → sign-up, login, profile
  [feature name]    → <what it does for the user, one line>
  [feature name]    → <what it does for the user, one line>
  Payments          → <if applicable>
  ...

Does this cover everything? Tell me if anything's missing or wrong 
before I start building.
```

Never use: module, spec, acceptance criteria, coupling, AST, CI, YAML, schema — in anything the PM reads.

Wait for PM approval. Iterate until PM says "yes" / "looks right" / "go".

### Step 3 — Generate `.pcp/` Scaffold

Once BRD and module list are approved:

**3a. Write core files.**
```
.pcp/objective.md       — distilled from BRD problem + use cases
.pcp/target_state.md    — distilled from BRD "done looks like"
.pcp/architecture.md    — from BRD technical constraints + architecture principles
.pcp/ci_rules.yaml      — derive rules from architecture principles
.pcp/SDLC_phase.yaml    — phases: planning → alpha → beta → v1
.pcp/architect_persona.md — derive BLOCK/WARN/NOTE rules from architecture principles
.pcp/strategy/decomposition.md
.pcp/strategy/dependency_map.md
```

**3b. For each module: generate spec + acceptance.**
```
.pcp/strategy/modules/<module>/spec.yaml
.pcp/strategy/modules/<module>/acceptance.yaml
.pcp/strategy/modules/<module>/plan.md
```

Every module spec must include these non-negotiable criteria regardless of module type:
- One criterion for structured logging (`check: ast_pattern` targeting `logger.` or `logging.` or `log.`)
- One criterion for error handling (`check: test_passes` on a failure-path test)
- For modules with external interfaces: one criterion for health/readiness endpoint or contract schema

Acceptance criteria must be MEASURABLE. For each criterion, choose the right check type:
- `check: file_exists` — verifiable by path
- `check: ast_pattern` — verifiable by grep/regex
- `check: dom_contains` — verifiable by Playwright selector
- `check: url_responds` — verifiable by HTTP status
- `check: railway_deploy` — verifiable by Railway API
- `check: github_actions` — verifiable by gh CLI
- `check: visual` — requires PM visual approval
- `check: manual` — requires PM explicit confirmation
- `check: test_passes` — verifiable by running named test

Avoid `check: manual` where a programmatic check is possible.

**3c. Run `pcp validate-strategy`.**
```bash
pcp validate-strategy
```

If coverage < 80%: identify gap, add module or expand spec, re-run. Iterate until ≥ 80%.

**3d. Confirm with PM — product language only.**

```
Ready to build.

Here's what I'm building and in what order:

  Round 1 (starting now, all in parallel):
    User accounts — sign-up, login, profile management
    [feature] — <plain English>
    [feature] — <plain English>

  Round 2 (starts when Round 1 is complete):
    [feature depending on Round 1]
    ...

Estimated time to first working version: <N> hours.
I'll update you when each part is ready to test.

Say /pcp build --all to start, or tell me to change anything first.
```

Never surface: module names, criterion IDs, coverage %, coupling score, YAML structure, CI status codes, branch names, commit hashes — in anything the PM reads. Translate all build status to product language before presenting.

---

## PHASE 2: PARALLEL BUILD

### Dependency Analysis

Before spawning agents, read `.pcp/strategy/dependency_map.md`.

Build the dependency graph:
- Modules with no dependencies → Wave 1
- Modules whose deps are all in Wave 1 → Wave 2
- And so on

Example output:
```
Wave 1 (no deps, build in parallel):
  auth, logging, config-service

Wave 2 (deps: Wave 1 complete):
  payments, notifications, api-gateway, admin-dashboard

Wave 3 (deps: Wave 2 complete):
  reporting, billing-reconciliation
```

### Stage 1 — Pre-Build Spec Review (per module, before any criterion agent spawns)

Run `pcp architect-review` against the module spec before touching code. Catches design-level violations when they're still cheap to fix — not after 8 criterion agents have built the wrong thing.

```bash
for module in <wave_modules>; do
  pcp architect-review --spec .pcp/strategy/modules/$module/spec.yaml 2>&1
done
```

**Results:**
- `BLOCK` → **do not spawn any agents for this module**. Spec must be fixed first. Write to deferred queue, notify PM: "spec for `<module>` violates architect persona: `<finding>`. Fix spec before build can start." Continue build on other modules.
- `WARN` → spawn agents, but include the warning in every criterion brief: "Persona WARN on this module: `<warning>`. Account for it in your implementation."
- Clean → proceed normally.

Spec files are human-**authorized** — a build agent never fixes a spec violation on its own. Route it to the PM, who applies it through the gated write path for that file (`pcp correct-objective` / `pcp pm` / `pcp amend`): the change is proposed, the PM sees a real diff, approves, then it's written. "Authorized" means unattended writes are forbidden, not that the PM hand-types the diff.

### Branch Isolation Protocol

Each parallel agent works on its own feature branch + git worktree.

```bash
# For each module being built in parallel:
git worktree add "../<project>-<module>" -b "feat/<module>"
```

This gives each agent:
- Isolated working directory
- Own branch (no conflicts with other agents)
- Full copy of the repo at that point

Worktree cleanup after merge:
```bash
git worktree remove "../<project>-<module>"
git branch -d feat/<module>
```

### Agent Sizing Principle — Non-Negotiable

**One agent per criterion. Not one agent per module.**

Why: a module with 8 criteria accumulates 8 × (code + tests + CI output + error logs) in one context window. By criterion 4 the context is saturated. The agent starts hallucinating file contents, missing earlier test failures, and losing track of what was committed. This is why context blows up during builds.

The fix: each criterion is an independent job. Agent starts clean, does exactly one criterion, commits, exits. Orchestrator spawns the next agent with a fresh context for the next criterion.

**Depth limit: one. Non-negotiable, reference-pattern borrowed from Grok Build's subagent model 2026-07-16** (its `spawn_subagent` tool hard-errors if a subagent tries to call it again — "maximum nesting depth is one"). A criterion agent MUST NOT call the `Agent` or `Workflow` tool itself. Only the top-level orchestrator session spawns criterion agents. This is enforced here as an explicit instruction, not a harness-level denial the way Grok's Rust runtime enforces it structurally — be explicit about that gap rather than implying a guarantee that doesn't exist. Why it matters: a criterion agent that spawns its own helper subagent reintroduces exactly the unbounded-growth failure the orchestrator-session-bound section below exists to prevent, one level down and outside the orchestrator's own turn-count/checkpoint visibility.

**Context budget per agent (strict):**
```
objective.md          — always included (short)
architecture.md       — always included (short)
module/spec.yaml      — always included (this module only)
ONE criterion entry   — from acceptance.yaml, this criterion only
architect_persona.md  — BLOCK rules section only, not full document
Relevant ADRs         — ONLY ADRs whose domain matches files this criterion touches
                        (check ADR title vs file paths — if no match, omit)
Prior criterion note  — 1 line: "<module>/<prev-id> done. commit: <hash>. added: <what>"
                        (not the code, not the tests, just the summary)
```

**Excluded from every agent brief:**
- Other modules' specs
- All criteria except the current one
- Full KB domain files (load snippet only if directly relevant)
- Full ci_rules.yaml (agent runs `pcp check` — doesn't need to read rules)
- Full build loop protocol (summarised to 7 steps in the agent brief)

### Execution Engine — `pcp build-plan` + Workflow tool

**Rewritten 2026-07-30, real incident.** `pcp build`'s Python execution engine
(worktree-per-criterion, merge-then-retry-on-conflict) and this skill's own
Workflow-based execution were two INDEPENDENT orchestration implementations of
the same job, reinventing coordination the harness already provides natively.
Measured cost on ontology-foundry: 5 of 5 completed `query-eval-harness`
criteria collided on merge in one run, 99% of that run's spend sat on the
conflicted criteria, two criteria were still stuck mid-retry two hours in. Root
cause, read directly out of the colliding file: every criterion in a module
adds one method to a shared facade class in `__init__.py` and removes one entry
from a shared `_PENDING` dict — small, non-overlapping in MEANING, but landing
at the same insertion point in the same file, so git's diff calls it a conflict
where nothing semantically conflicts. No criterion ever declares `__init__.py`
as its `target`, so a target-only scheduler can never see this collision coming.

**The split now:** Python plans, the harness executes. `pcp build-plan
[--module X]` computes the schedule (module waves, criterion waves, each
module's `shared_surface_files`) exactly as before — same
`_compute_criterion_waves`/dependency logic, nothing new — and emits it as JSON.
It spawns nothing and writes nothing. Run it first, always:

```bash
pcp build-plan --module <name>   # or omit --module for the whole program
```

Each module in the plan carries `shared_surface_files`: its own
`src/modules/<module>/__init__.py`, plus any file a `MOD_A00x` criterion in that
module declares as `target` (the app-registry entry, the interface file).
**Every criterion in the module is scheduled as implicitly touching these**,
regardless of what its own `target` says — an `A00x` criterion's own new file
IS genuinely disjoint from a sibling's own new file (real parallelism is safe
there), but both still touch the shared facade to register.

**Two different things, two different primitives — do not conflate them:**

1. **Each criterion's own file(s) — build in real `parallel()`.** True
   independence; worktree isolation is warranted here (Workflow's own
   guidance: use isolation ONLY when agents would otherwise conflict — this is
   exactly that case, criteria genuinely writing different files at once).
2. **`shared_surface_files` — single writer, never parallel edits.** A worker
   agent NEVER edits a module's `__init__.py`/interface/registry directly — it
   returns a small STRUCTURED registration request instead (method name,
   delegate-to call, `_PENDING` key to drop — the same shape every criterion in
   this module already needs, visible in any existing `__init__.py` of this
   kind). One step applies all pending requests to the shared file(s), in
   order, after (or as) each worker finishes. Since only that one step ever
   writes those files, there is nothing to merge and nothing to conflict —
   not "conflicts resolved faster", genuinely no conflict is possible.
   Prefer a **deterministic** apply step over another agent call for this: the
   edit shape (add one method with a fixed pattern, remove one dict key) is
   regular enough not to need judgment. Escalate to an actual agent step only
   when a shared-file edit genuinely can't be expressed as that structured
   request (e.g. two criteria both want to restructure the same class
   differently) — rare, not the routine case.

`depends_on` still means ORDER only (declared → later wave, absent/empty →
wave 0, runs together) — `pcp build-plan`'s `criterion_waves` already reflects
this, don't re-derive it.

```javascript
export const meta = {
  name: 'pcp-wave',
  description: 'Build one module wave from pcp build-plan\'s output',
  phases: [
    { title: 'Build' },
    { title: 'Gate' },
    { title: 'Register' },   // the single-writer step — see above
  ]
}

// plan = JSON.parse(shell(`pcp build-plan --module ${moduleName}`))
// One pipeline() call per criterion wave (plan.modules[i].criterion_waves[w]) —
// waves run in order, criteria within a wave run together.
for (const wave of plan.modules[0].criterion_waves) {
  const results = await pipeline(
    wave,
    // Stage 1: build ONLY this criterion's own file(s) — never the shared surface.
    criterion => agent(buildBrief(criterion, plan.modules[0].shared_surface_files), {
      label: `build:${criterion.id}`, phase: 'Build',
      isolation: wave.length > 1 ? 'worktree' : undefined,  // isolation only when it's buying something
    }),
    // Stage 2: gate, streams in as each build finishes — no barrier.
    (buildResult, criterion) => agent(
      `Run pcp gate on ${criterion.id}. commit: ${buildResult.commit}. advisory only.`,
      { label: `gate:${criterion.id}`, phase: 'Gate' },
    ),
  )

  // Stage 3: single-writer registration — a REAL barrier, deliberately.
  // Every criterion in this wave returned a registration request; apply them
  // to shared_surface_files ONE AT A TIME, here, the only place these files
  // are ever touched. Deterministic template insertion where the shape
  // allows (see module docstring); escalate to agent() only if a request
  // can't be expressed that way.
  phase('Register')
  for (const r of results.filter(Boolean)) {
    await applyRegistration(r.registrationRequest, plan.modules[0].shared_surface_files)
  }
}
```

**The `buildBrief(criterion, sharedSurfaceFiles)` function** — compact, no bloat:
```
Build criterion ${criterion.id}: "${criterion.description}"
Module: ${criterion.module} | check: ${criterion.check}
Working dir: worktree for feat/${criterion.module}

## Objective
${objective_md}   ← full (it's short)

## Architecture
${architecture_md}  ← full (it's short)

## Module spec
${module_spec_yaml}  ← full (this module only)

## Persona BLOCK rules
${persona_block_section}  ← BLOCK section only, not full document

${relevant_adr ? `## Relevant ADR\n${relevant_adr}` : ''}
${criterion.prevCommit ? `## Prior work\n${criterion.module}/${criterion.prevId} done. commit: ${criterion.prevCommit}. added: ${criterion.prevSummary}.` : ''}

## Files you may NOT edit — single-writer, someone else applies these
${sharedSurfaceFiles.join('\n')}
If your criterion needs a new export/registration entry in one of these, do NOT
touch the file. Instead include a registrationRequest in your return value (see
below) describing exactly what to add. This is not a style preference — two
agents editing these files at once is how a real incident happened (2026-07-30,
5 of 5 criteria in one module collided on merge because everyone edited the
same shared facade class directly). The file will be updated for you, after you
finish, by the one step that's allowed to touch it.

## Steps (in order, no skipping)
1. Write tests → verify RED
2. Write code → verify GREEN (your own file(s) only — never a file listed above)
3. lint → pcp check → pcp architect-review --staged
4. Fix all BLOCK findings
5. commit: feat(${criterion.module}): ${criterion.id} — ${criterion.description}
6. echo "${criterion.module}:${criterion.id}:$(git rev-parse HEAD)" >> .pcp/.build_progress

Return: {
  commit: "<hash>", summary: "<one line what was added>", escalation: null | "<what>",
  registrationRequest: null | {
    file: "<one of the files listed above>",
    method_name: "<name>", delegates_to: "<module.path.function>",
    pending_key_to_remove: "<key, or null if this module has no _PENDING dict>"
  }
}
Do NOT read other modules. Do NOT continue to next criterion. Exit after step 6.
Do NOT call the Agent or Workflow tool yourself — depth limit is one level, you are the leaf.
```

### Escalation Bubbling

When a subagent hits an escalation trigger, it returns it to the orchestrator.
Orchestrator collects all escalations, presents them to PM as a batch:

```
Build in progress — 3 escalations need your input:

1. exports/A004 (check: manual)
   Verify: run export against a test dataset, confirm output matches spec
   Action: test and say "exports A004 verified"

2. payments
   Architecture decision needed: which webhook signing scheme for the payment provider?
   Options: (a) HMAC-SHA256 shared secret  (b) provider SDK-managed verification
   Action: say "pcp decision: payment-webhook-signing = <choice>"

3. notifications
   Missing API credential for the push-notification provider
   Requires provider dashboard signup, 1-2 day lead time
   Action: provision and say "push credential added"

Build continues on all non-blocked modules.
Reply to each escalation by number to unblock.
```

PM responds, orchestrator unblocks the relevant agents.

---

## SINGLE-AGENT BUILD LOOP PROTOCOL

**Scope: one criterion per agent session.** This section describes what one agent does for one criterion. Agents do not loop. They do not continue to the next criterion. One criterion → commit → exit. The orchestrator spawns the next agent.

Why: each criterion accumulates code + test output + CI logs + error messages in context. Looping through multiple criteria in one session causes context saturation by criterion 3-4. The agent starts missing earlier failures, misremembering file contents, and losing spec alignment. One agent per criterion eliminates this entirely.

**Completion Definition — all must be true before marking done:**
- Tests written that FAIL before code exists (red proven)
- Tests passing after code written (green proven)
- Full regression suite green (no regressions)
- Build succeeds (compiled languages)
- Lint clean
- Secret scan clean — no credentials in staged files
- `pcp check` clean (no BLOCK)
- `pcp architect-review --staged` clean (no BLOCK)
- Acceptance `check:` type passes (file_exists / test_passes / dom_contains / manual)
- Self-QA: code matches criterion description exactly
- `acceptance.yaml` status updated to `complete` for this criterion
- Checkpoint written: `echo "<module>:<id>:<git-hash>" >> .pcp/.build_progress`

**Not required per-criterion (run at wave merge, not per agent):**
- CI green (runs on PR)
- Staging smoke test (runs after wave merge)
- Production smoke test (runs after staging)
- `pcp scan` (runs after all criteria complete)

### Module pre-flight (once per module, before first criterion agent — run by orchestrator)

**Dep audit** — dependencies checked once per module, not per criterion:
```bash
# Python
safety check 2>/dev/null || pip-audit 2>/dev/null

# JavaScript/TypeScript
npm audit --audit-level=high 2>/dev/null

# Rust
cargo audit 2>/dev/null
```
High-severity CVE: fix dependency version in manifest before spawning any criterion agents. Criterion agents inherit the fixed manifest.

**Spec security review** (Stage 1 — security dimension):
Does spec describe auth model, input validation, data classification for user-facing or API modules?
```bash
pcp architect-review --module <name> --fail-on-block
```
BLOCK on spec → do not spawn criterion agents. Notify PM. Continue other modules.

### Per-criterion agent pre-flight (once per agent, takes 30 seconds)

**Spec lock.** Hash all spec files. Store in `.pcp/.build_lock`.
```bash
find .pcp/strategy/modules -name "*.yaml" | sort | xargs sha256sum > .pcp/.build_lock
```
If `.pcp/.build_lock` already exists and differs: specs changed during build — escalate before continuing.

**Install pre-commit hooks** if not present:
```bash
# Secret scanning
pip install detect-secrets 2>/dev/null || true
pre-commit install 2>/dev/null || true
# If no pre-commit config, add minimal secret hook:
if [ ! -f .pre-commit-config.yaml ]; then
  cat > .pre-commit-config.yaml << 'EOF'
repos:
  - repo: https://github.com/Yelp/detect-secrets
    rev: v1.4.0
    hooks:
      - id: detect-secrets
EOF
fi
```

**Conflict pre-check** (for parallel builds on feature branches):
```bash
BRANCH=$(git branch --show-current)
git fetch origin main --quiet
CONFLICTS=$(git merge-tree $(git merge-base HEAD origin/main) HEAD origin/main 2>/dev/null | grep -c "<<<<<<" || echo 0)
if [ "$CONFLICTS" -gt "0" ]; then
  echo "CONFLICT: $CONFLICTS conflicting sections vs main. Rebase before continuing."
  # attempt auto-rebase
  git rebase origin/main 2>/dev/null || echo "REBASE_NEEDED"
fi
```
If rebase fails: escalate to orchestrator.

### For your assigned criterion (one only — stop after commit):

**1. Announce:** `Building <module>/<id> — <description>`

**2. Context already loaded in your brief.** Do NOT speculatively read other modules' files, other criteria, or the full KB. Read only files directly relevant to implementing this criterion. If you need an ADR that wasn't in your brief: read it, note it, do not load the rest.

**3. Resolve dependencies.** Check packages exist before writing imports. Add to manifest first.

**4. Write tests first (TDD).**
Tests must FAIL before code exists. Verify red before writing code.
```bash
pytest tests/test_<module>.py -x 2>&1 | tail -5   # Python
npx jest <module>.test.ts 2>&1 | tail -5           # TypeScript
cargo test <module> 2>&1 | tail -5                 # Rust
swift test --filter <Module>Tests 2>&1 | tail -5   # Swift
```

**5. Write code.** Ground in spec.yaml constraints + architecture.md + persona BLOCK rules + ADRs.
If architectural decision needed that's not in ADRs: STOP, return escalation, do not guess.

**6. Build check (compiled languages).**
```bash
xcodebuild build -scheme <S> -destination 'generic/platform=macOS' 2>&1 | grep -E "error:|BUILD" | tail -5
cargo build 2>&1 | tail -10
npx tsc --noEmit 2>&1 | tail -10
```
Fix until build passes.

**7. Tests green + regression.**
One run — unit tests for this module with coverage, plus full suite to catch regressions. Not two separate runs.
```bash
pytest --tb=short -q --cov=src/<module> --cov-report=term-missing   # Python
npx jest --coverage 2>/dev/null                                       # TypeScript (all tests)
cargo test 2>/dev/null                                                # Rust (all tests)
swift test 2>/dev/null                                                # Swift
```
Fix until all pass. Cannot proceed with broken existing tests.

**9. QA self-review.** Re-read criterion description. Ask:
- Does my code actually do what this says?
- Will `check:` type pass on my code?
- Any BLOCK rule from persona violated?
- Any ADR boundary touched without following the ADR pattern?

**10. `pcp check`** — fix any BLOCK before proceeding.

**11. `pcp architect-review --staged`** — fix BLOCK, fix WARN, log NOTE.

**11a. Lint gate.**
```bash
# Python
black --check src/ tests/ 2>/dev/null && ruff check src/ 2>/dev/null
# TypeScript/JS
npx eslint src/ --max-warnings 0 2>/dev/null && npx prettier --check src/ 2>/dev/null
# Rust
cargo fmt --check 2>/dev/null && cargo clippy -- -D warnings 2>/dev/null
# Swift
swiftformat --lint Sources/ 2>/dev/null
# C
# clang-format check handled by pcp check AST rules
```
Lint failures → fix code, re-run. Do not commit with lint violations.

**11b. Secret scan.**
```bash
detect-secrets scan --baseline .secrets.baseline 2>/dev/null || \
  git diff --staged | grep -E "(password|secret|api_key|token|private_key)\s*=\s*['\"][^'\"]{8,}" -i
```
If secrets detected: STOP. Remove secret. Add to `.env.example` instead. Commit clean version.
Never commit credentials, tokens, or API keys.

**11c. SAST (code patterns only — dep audit runs once at module pre-flight, not per criterion).**
```bash
# Python
bandit -r src/ -ll 2>/dev/null        # SAST, skip low-severity

# Any: semgrep (if installed)
semgrep --config=auto src/ --error 2>/dev/null
```
SAST finding: fix code. If false positive: add `# nosec` / `// nosemgrep` with comment explaining why.
Dependency CVEs are checked once at module pre-flight — not repeated here.

**11d. Error handling test coverage.**
At least one test per criterion must test a failure path — what happens when:
- Input is malformed or missing
- Downstream dependency is unavailable
- Timeout occurs
- Permission is denied

If criterion spec has no failure mode described: derive from spec constraints and common sense.

**11e. ~~Contract validation~~**
Moved to post-module-complete (after ALL criteria for this module pass), just before PR creation. Validating a contract after one criterion when the module has 8 criteria is premature — the output isn't representative yet. Skip here.

**12. E2E (only if criterion check type is NOT dom_contains / url_responds).**
If criterion has `check: dom_contains` or `check: url_responds` — skip this step. Step 16 IS the E2E for those types.
If criterion has `check: manual` or `check: test_passes` with integration scope:
```bash
npx playwright test tests/e2e/<module>.spec.ts 2>/dev/null   # web
tests/integration/<module>_integration.sh 2>/dev/null         # CLI/API
```

**13. Commit.**
```bash
git add <specific files>
git commit -m "<type>(<module>): <what>"
```
No Co-Authored-By. No attribution. Specific files only, never `git add -A`.

**14. Push.**
```bash
git push origin feat/<module>
```

**15. Watch CI.**
```bash
BRANCH=$(git branch --show-current)
RUN_ID=$(gh run list --branch "$BRANCH" --limit 1 --json databaseId --jq '.[0].databaseId')
gh run watch "$RUN_ID" --exit-status
```
Failure → auto-fix loop (max 3 retries, exponential backoff: wait 2s, 4s, 8s between retries).

**Auto-fix loop:**
```bash
gh run view "$RUN_ID" --log-failed | head -300
# diagnose → fix code → re-run tests locally → lint → secret scan → commit → push → watch CI
```
After 3 retries still failing: return escalation to orchestrator.

**15a. ~~Staging deploy~~**
Moved to wave merge — one staging deploy after all wave modules merge to main. Deploying to staging after every criterion (8 deploys for an 8-criterion module) is waste. Skip here.

**16. Visual/URL checks.**
- `check: dom_contains` → Playwright selector, automated
- `check: url_responds` → curl HTTP status, automated
- `check: visual` → screenshot + return to orchestrator → PM approves

**16b. Production smoke test** (after Railway prod deploy, if `check: railway_deploy`):
```bash
# Wait for prod deploy
for i in $(seq 1 30); do
  STATUS=$(railway status --json 2>/dev/null | python3 -c \
    "import sys,json; print(json.load(sys.stdin).get('deploymentStatus','unknown'))")
  [ "$STATUS" = "SUCCESS" ] && break
  [ "$STATUS" = "FAILED" ] && echo "PROD_DEPLOY_FAILED" && break
  sleep 30
done

# Smoke test production
PROD_URL=$(railway domain 2>/dev/null | head -1)
if [ -n "$PROD_URL" ]; then
  HTTP=$(curl -s -o /dev/null -w "%{http_code}" "https://$PROD_URL/health" 2>/dev/null)
  echo "Production smoke: HTTP $HTTP"
  if [ "$HTTP" != "200" ]; then
    echo "PRODUCTION_DOWN — initiating rollback"
    railway rollback 2>/dev/null || echo "manual rollback needed"
    echo "PROD_SMOKE_FAILED"
  fi
fi
```
If production smoke fails: auto-rollback, escalate immediately. Never leave production broken silently.

**17. Checkpoint + exit.**
```bash
# Update acceptance.yaml: set this criterion's status to complete
# Write checkpoint (orchestrator reads this to know where to resume)
HASH=$(git rev-parse HEAD)
echo "<module>:<criterion-id>:$HASH" >> .pcp/.build_progress
echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) COMPLETE <module>/<criterion-id> $HASH" >> .pcp/audit.log
```

Report to orchestrator: `DONE <module>/<criterion-id> | commit <hash> | next: <next-criterion-id or WAVE_COMPLETE>`

**Stop here. Do not continue to the next criterion.** The orchestrator spawns a fresh agent for the next criterion with a clean context window. This is how context limits are prevented — not by checkpointing mid-loop, but by never looping in the first place.

---

## MERGE COORDINATOR PROTOCOL

After each wave completes, orchestrator runs merge sequence:

```bash
for module in <wave_modules>; do
  # Pre-merge: rebase feature branch on main to surface conflicts before PR
  git -C "../$(basename $(pwd))-$module" fetch origin main --quiet
  git -C "../$(basename $(pwd))-$module" rebase origin/main 2>/dev/null
  if [ $? -ne 0 ]; then
    echo "CONFLICT: $module cannot rebase cleanly — escalating"
    continue  # skip this module, escalate, continue others
  fi

  # Contract validation — runs here (module complete, all criteria done, full output available)
  if [ -f ".pcp/contracts/${module}_output.schema.json" ]; then
    echo "Validating $module output contract..."
    python3 -m pytest "tests/contracts/test_${module}_contract.py" -q 2>/dev/null || \
      echo "CONTRACT_FAIL: $module — escalating"
  else
    # Generate contract schema from actual module output if it has dependents
    grep -q "$module" .pcp/strategy/dependency_map.md 2>/dev/null && \
      echo "WARN: $module has dependents but no contract schema — generate .pcp/contracts/${module}_output.schema.json"
  fi

  # Create PR
  gh pr create \
    --base main \
    --head "feat/$module" \
    --title "feat($module): complete all acceptance criteria" \
    --body "$(pcp status --module $module --markdown 2>/dev/null || echo 'Module complete')"

  # Layer 2 gate (advisory, not blocking)
  pcp gate --branch "feat/$module" 2>/dev/null

  # Merge
  gh pr merge "feat/$module" --merge --auto

  # Cleanup worktree
  git worktree remove "../$(basename $(pwd))-$module" 2>/dev/null || true
  git branch -d "feat/$module" 2>/dev/null || true
done

# Integration test after all wave merges
git checkout main && git pull
npm test 2>/dev/null || pytest --tb=short -q 2>/dev/null || cargo test 2>/dev/null || swift test 2>/dev/null || make test 2>/dev/null

# Staging deploy + smoke (once per wave, not per criterion)
if grep -q "STAGING_URL\|staging" .pcp/architecture.md 2>/dev/null; then
  railway up --environment staging 2>/dev/null
  for i in $(seq 1 20); do
    STATUS=$(railway status --environment staging --json 2>/dev/null | python3 -c \
      "import sys,json; print(json.load(sys.stdin).get('deploymentStatus','unknown'))" 2>/dev/null)
    [ "$STATUS" = "SUCCESS" ] && break
    [ "$STATUS" = "FAILED" ] && echo "STAGING_DEPLOY_FAILED" && break
    sleep 30
  done
  STAGING_URL=$(railway domain --environment staging 2>/dev/null | head -1)
  [ -n "$STAGING_URL" ] && \
    HTTP=$(curl -s -o /dev/null -w "%{http_code}" "https://$STAGING_URL/health" 2>/dev/null) && \
    echo "Staging smoke: $HTTP" && \
    [ "$HTTP" != "200" ] && echo "STAGING_SMOKE_FAILED — fix before starting next wave"
fi

# Validate strategy coverage still holds after wave
pcp validate-strategy
# If coverage dropped or coupling violations increased: flag to PM before starting next wave

# Stage 3 — Wave-level architect review
# Reviews ALL files changed since this wave started (not just per-criterion diffs)
# Catches emergent violations: individual modules clean, but combined they violate a principle
# WAVE_START_COMMIT is written to .pcp/.active_workflow at wave start
WAVE_BASE=$(python3 -c "import yaml; d=yaml.safe_load(open('.pcp/.active_workflow')); print(d.get('wave_start_commit','main'))" 2>/dev/null || echo "main")
pcp architect-review --base $WAVE_BASE --fail-on-block 2>&1
# BLOCK findings here → do NOT start next wave. Fix before proceeding.
# WARN findings → log, include in next wave's module briefs, proceed.

# Audit log
echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) WAVE_COMPLETE modules=[<wave_modules>]" >> .pcp/audit.log
```

If integration test fails after merge: bisect to find which module caused regression.
```bash
# Bisect: revert merges one by one until green
git log --oneline -10  # find merge commits
git revert <merge-commit> --no-edit  # revert suspect, re-run tests
```
Revert culprit module, open escalation, continue next wave without that module.

---

## PERSISTENCE

PCP build state survives session end, session crash, and laptop close.

### State files (all in `.pcp/`, all committed to git)

```
.pcp/.active_workflow     — Workflow run ID + current wave state (written at build start, deleted at completion)
.pcp/.build_progress      — append-only log: <module>:<criterion-id>:<git-hash> per completed criterion
.pcp/deferred_queue.yaml  — blocked/deferred items (manual, visual, architecture decisions)
.pcp/notifications.log    — escalations sent when PM was away
.pcp/audit.log            — timestamped full audit trail
```

### Writing state on build start

Immediately after launching a Workflow, write `.pcp/.active_workflow`:
```yaml
workflow_run_id: <Workflow tool returned run ID>
started: <ISO timestamp>
wave: 1
wave_modules: [module-a, module-b, module-c]
wave_start_commit: <git rev-parse HEAD at wave start>
criteria_total: 24
criteria_done: 0
last_updated: <ISO timestamp>
```

Update `criteria_done` and `last_updated` as each agent checkpoint is written to `.build_progress`.

Delete `.pcp/.active_workflow` when the full build completes cleanly.

### Two resume paths

**Hot resume (same Claude session, Workflow run ID still valid):**
```
Workflow({
  scriptPath: '.pcp/.workflow_script.js',
  resumeFromRunId: '<run_id from .active_workflow>'
})
```
Completed agents return cached results instantly. Build continues from where it stopped.

**Cold resume (new Claude session, run ID expired):**
```
# Read which criteria are already done
done = parse .pcp/.build_progress  # set of "<module>:<id>" strings

# Compute pending criteria
pending = all criteria in acceptance.yaml files WHERE status != complete
           AND "<module>:<id>" NOT IN done

# Start fresh Workflow with only pending criteria
```
Cold resume rebuilds the work list from file state, not from memory. Idempotent — re-running a criterion that's already committed is safe (agent sees the commit, marks done immediately).

---

## ORCHESTRATOR SESSION BOUND — NON-NEGOTIABLE

**Found 2026-07-01** after a real incident: an orchestrator session ran 36 hours, 1725 turns, 133M cache_read tokens, in one continuous session — no cap ever fired because nothing was watching the orchestrator itself.

The Agent Sizing Principle bounds *subagents* (one criterion, fresh context, exit). It does not bound the **orchestrator** — this session, the one running `/pcp build --all` or `/pcp watch` — which dispatches, polls CI/Railway, and handles escalations for the entire build or monitoring run. Left unbounded, the orchestrator's own transcript accumulates every poll cycle, every dispatch decision, every dashboard render, forever. That's the same context-saturation failure the per-criterion design exists to prevent, one level up, and it was missed because "runs indefinitely" (see `/pcp watch` above) was read as license for one session to stay open the whole time. It isn't — indefinite describes the monitoring/build *job*, not the *session*.

**Hard caps, enforced by the orchestrator on itself:**

- **`/pcp watch`:** stop the poll loop after 200 iterations (~15h at the default 270s interval — matches `PCP_WATCH_MAX_ITERATIONS` in the Python CLI; keep both in sync if either changes). On hitting the cap: write `.pcp/.watch_last_poll` as usual, notify PM (`"pcp watch: reached iteration cap (200) — restart with /pcp watch to keep monitoring"`), then end the session. Do not silently keep looping past it.
- **`/pcp build --all`:** at every wave boundary (already a checkpoint — `.active_workflow` is written there), check this session's own turn count / elapsed time. Past ~4 hours or a large turn count, checkpoint and hand off instead of continuing into the next wave: tell PM `"wave <N> complete, checkpointing here — run /pcp build --resume in a fresh session for the remaining waves."` Cold resume (above) already rebuilds the work list from `.build_progress` idempotently — handoff is safe, so there's no reason to keep one session alive just to avoid restarting it.
- **General rule:** if the orchestrator ever notices its own session is running long (large turn count, many hours elapsed, cache growing), that is itself a trigger to checkpoint and hand off — do not wait for a wave/poll boundary if it's clearly overdue.

---

## ON INVOCATION (bare `/pcp`)

**Step 1 — Find project root.** Walk up from cwd looking for `.pcp/`. If not found: offer to start vision workshop.

**Step 2 — Check for active workflow first.**
```bash
[ -f .pcp/.active_workflow ] && cat .pcp/.active_workflow
```
If `.active_workflow` exists:
```
Active build found.
  Started:   <started>
  Wave:      <wave> — <wave_modules>
  Progress:  <criteria_done>/<criteria_total>

Options:
  (1) Resume — /pcp build --resume   (hot: reattach to Workflow run, cold: rebuild from .build_progress)
  (2) Status — show what completed so far
  (3) Abandon — /pcp build --abandon (clears .active_workflow, keeps .build_progress)
```

**Step 3 — Load into working memory.** Read: objective.md, architecture.md, SDLC_phase.yaml. Do NOT load all ADRs upfront — load them lazily when a build agent needs them.

**Step 4 — Scan.**
```bash
pcp scan
```

**Step 5 — CI + deferred queue.**
```bash
gh run list --limit 3 --json status,name,conclusion,updatedAt 2>/dev/null
railway status 2>/dev/null || true
# deferred items waiting for PM
[ -f .pcp/deferred_queue.yaml ] && grep "status: deferred" .pcp/deferred_queue.yaml | wc -l
```

**Step 6 — Present dashboard.**
```
Project: <name>
Phase:   <phase>
Progress: X/Y criteria complete (Z%)

Waves:
  Wave 1 — <modules> [complete / in-progress / pending]
  Wave 2 — <modules> [pending]

CI:      GitHub Actions <status> | Railway <status>
Deferred: <N> items waiting for you (run /pcp status for details)
Agents:  25 available

/pcp build --all     start full parallel build
/pcp build --resume  resume active build
```

---

## MULTI-PROJECT STATUS (`/pcp status --all`)

```bash
find ~/Claude-code -name "objective.md" -path "*/.pcp/*" 2>/dev/null | sed 's|/.pcp/objective.md||'
```

For each project: read SDLC_phase.yaml + latest pcp.md + .active_workflow if present.

```
All Projects — <date>

project-a    alpha     0%    Wave 1 ready    CI: —        [no active build]
project-b    alpha    17%    Wave 1 ready    CI: green    [no active build]
project-c    alpha    34%    Wave 2 active   CI: failing  [BUILDING: 8/24 done]
project-d    planning  0%    needs /pcp new               [no active build]

Agents: 25 available
Building: project-c (resume with: cd project-c && /pcp build --resume)
Most urgent: project-c CI failing — /pcp fix (in project-c dir)
```

---

## `/pcp watch` — CI AND RAILWAY MONITORING

Triggered by `/pcp watch`. Uses `ScheduleWakeup` to run a polling loop every 270 seconds (stays within prompt cache TTL). Runs until PM says `/pcp stop` OR the iteration cap below is hit — "runs indefinitely" describes the monitoring job, not license for one session to stay open forever. See ORCHESTRATOR SESSION BOUND.

### What it monitors (every poll cycle)

**1. GitHub Actions — CLI**
```bash
gh run list --limit 10 --json databaseId,status,conclusion,name,updatedAt,headBranch \
  --jq '.[] | select(.conclusion == "failure" or .status == "in_progress")'
```
Failed run → proceed to auto-fix (see below).

**2. Railway — CLI**
```bash
railway status --json 2>/dev/null
```
Failed deploy → proceed to auto-fix.

**3. Railway logs (proactive scan for errors)**
```bash
railway logs --tail 50 2>/dev/null | grep -iE "error|exception|crash|oom|timeout" | tail -10
```

### Auto-fix loop (triggered by any failure source)

```
1. Fetch full failure log
   gh run view <run-id> --log-failed | head -500
   OR: railway logs --deployment <id> | head -500

2. Diagnose: what failed?
   - Test failure → identify which test, which assertion
   - Build failure → identify which file, which error
   - Deploy failure → identify which service, which config
   - Dependency error → identify which package, which version

3. Fix:
   - Code fix → edit file → run tests locally → commit → push
   - Config fix → edit railway.toml or Dockerfile → commit → push
   - Dep fix → update manifest → commit → push

4. Watch new run:
   RUN_ID=$(gh run list --branch $(git branch --show-current) --limit 1 --json databaseId --jq '.[0].databaseId')
   gh run watch $RUN_ID --exit-status

5. On resolution:
   slack-notify "PCP fixed: <what failed> → <what was fixed>. CI green. Commit: <hash>"
   Update .pcp/.watch_last_poll

6. On 3 failed fix attempts:
   slack-notify "PCP stuck on <failure>. 3 fix attempts failed. Needs you."
   Add to deferred queue, continue monitoring other projects
```

### State file
```bash
echo "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > .pcp/.watch_last_poll
```
Read on each cycle to know which emails/runs are new since last check.

### `/pcp watch --all`
Monitors ALL projects under `~/Claude-code/` in a single loop. Checks each project's CI + Railway + shared email inbox. One loop, all projects.

---

## UNATTENDED AUTONOMY PROTOCOL

**Rule: escalation ≠ stop.** Every blocked item goes to the deferred queue + Slack notification. Build continues on everything that doesn't depend on the blocked item. Only two things are true stops: missing production credentials with no workaround, and production incident.

### Notification setup (read once at session start)

Read `.pcp/config.yaml` for notification config:
```yaml
notifications:
  slack_command: slack-notify           # default: ~/bin/slack-notify
  channel: "#pcp-builds"               # default channel
  webhook: ""                          # optional webhook URL
  on: [deferred, blocked, complete, error, production-incident]
```
If config absent: use `slack-notify` with default channel if available, else write to `.pcp/notifications.log`.

Every notification:
```bash
slack-notify "PCP [<project>/<module>]: <one-line summary>. Build continuing." 2>/dev/null || \
  echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) <summary>" >> .pcp/notifications.log
```

### Deferred queue

File: `.pcp/deferred_queue.yaml` — created if absent, appended atomically.

```yaml
deferred:
  - id: "<module>/<criterion-id>"
    type: manual | visual | architecture-decision | blocked-ci | blocked-secret
    summary: "<one sentence what is needed>"
    detail: "<exact instruction for PM>"
    screenshot: "<path if visual>"
    draft_adr: "<path if architecture-decision>"
    blocked_since: "<ISO timestamp>"
    retry_at: "<ISO timestamp, for blocked-ci>"
    unblocks: ["<module>/<id>", ...]   # criteria waiting on this
    status: deferred | resolved
```

### How each blocker is handled autonomously

**Architecture decision not in ADRs:**
1. Apply heuristics: existing ADRs, architecture.md principles, language/framework best practices
2. Make the decision and write `.pcp/kb/adr/ADR-<N>-<slug>-DRAFT.md`
3. Continue building with that decision
4. Add to deferred queue (type: architecture-decision)
5. Notify: "Made autonomous architecture decision: <decision>. Draft ADR written. Approve with `/pcp accept-adr <N>` or override with `/pcp override-adr <N> <your-decision>`"
6. If PM overrides later: revert affected code, rebuild with correct decision

**`check: manual` criterion:**
1. Write code that satisfies the criterion (treat as `check: file_exists` for the implementation)
2. Add to deferred queue. Record `deferred_at` timestamp.
3. Notify: "Manual verification needed: <what to check and how>"
4. Continue all other criteria
5. When PM says `/pcp approve <module>/<id>`: mark complete, log intervention:
```python
_log_intervention(pcp_dir, {
    "type": "manual_approval",
    "module": module, "criterion_id": criterion_id,
    "criterion_description": description,
    "deferred_at": deferred_at, "resolved_at": now,
    "time_to_resolve_minutes": elapsed,
    "feedback": None, "outcome": "approved"
})
```

**`check: visual` criterion:**
1. Start dev server
2. Try `dom_contains` fallback if selector is determinable from spec — if passes, auto-approve (no intervention log entry — fully automated)
3. If no auto-check: take screenshot → `.pcp/previews/<module>-<id>-<ts>.png`
4. Add to deferred queue. Record `deferred_at` timestamp.
5. Notify with screenshot path: "Visual approval needed. Open: `open .pcp/previews/<file>`. Approve: `/pcp approve <module>/<id>`"
6. Continue all other criteria
7. On `/pcp approve`: log intervention. On `/pcp feedback "<text>"`: log with feedback field, rebuild.

**CI failing after 3 retries:**
1. Mark criterion `blocked-ci` in deferred queue with `retry_at: <now + 30min>`
2. Notify: "CI blocked after 3 retries. Last error: <summary>. Auto-retry in 30min."
3. Continue building all criteria that don't depend on this one
4. At retry time (if still in same session): attempt again silently
5. If resolved: mark complete, notify success

**Missing env var / secret:**
1. Detect which variable is missing from error log
2. Check `.env.example` — if variable is documented there, note its purpose
3. Add to deferred queue (type: blocked-secret)
4. Write to `.pcp/pending_secrets.md`: exact variable name, where to set it (Railway dashboard / `.env`), why it's needed
5. Notify: "Missing: `<VAR_NAME>` in <environment>. Set it and run `/pcp build --module <name>`"
6. Continue all code-only criteria for this module (only deploy criteria are blocked by missing secrets)

**Merge conflict (auto-resolve):**
1. Identify conflicting files
2. For each conflict, apply resolution priority:
   - Spec files (`.pcp/`): NEVER auto-resolve — always escalate
   - Config files (package.json versions): take higher version
   - Code files: apply ADR priority — if ADR governs this file's domain, its rules win
   - Lock files (package-lock.json, Cargo.lock): regenerate (`npm install` / `cargo update`)
3. If auto-resolve succeeds: commit resolution, continue
4. If genuinely ambiguous (both branches modified same business logic): add to deferred queue, isolate module, continue others
5. Notify only if couldn't auto-resolve

**Integration test regression after wave merge:**
1. Identify culprit via bisect (revert merges one by one until green)
2. Revert culprit module's merge: `git revert <merge-commit> --no-edit`
3. Mark module as `blocked-regression` in deferred queue
4. Continue next wave without the reverted module
5. Notify: "<module> reverted — integration test failed. Investigating. Build continues."
6. Diagnose the regression and write a fix plan to `.pcp/regression_<module>.md`

**Production smoke test failed:**
1. Execute rollback immediately: `railway rollback` or `git revert + railway up`
2. Notify immediately (this is a production incident — highest priority Slack message)
3. Pause ALL deploy-related criteria
4. Continue code-only criteria in other modules
5. Write incident log to `.pcp/incidents/<ts>.md`
6. Do NOT auto-redeploy to production — wait for PM to investigate

**Spec drift detected mid-build:**
1. Re-read new spec
2. Evaluate impact: does the change invalidate already-completed criteria?
3. If backward-compatible (additive): adopt new spec, update draft ADR if needed, continue
4. If breaking (removes or contradicts completed work): add to deferred queue, notify PM with diff
5. Never silently build against an old spec

**Context window near limit:**
This should never happen in normal operation — one agent per criterion means context never accumulates. If it does happen (agent was incorrectly briefed to loop through multiple criteria):
1. Stop immediately at the current criterion boundary
2. Commit whatever is staged with `wip(<module>/<id>): partial - context limit`
3. Write handoff to `.pcp/.handoff.md`: "Resume: /pcp build --module <name> --from <criterion-id>"
4. Notify: "Context limit — agent was running too many criteria. Resuming with corrected single-criterion model."
Prevention: orchestrator must never brief an agent with "complete ALL criteria". Always "complete criterion <id> only".

### When PM returns (`/pcp` after being away)

Read: `.pcp/.build_progress` + `.pcp/deferred_queue.yaml` + `.pcp/pending_secrets.md`

Present:
```
Built while you were away:
  ✓ <module>  <n>/<total> criteria complete
  ✓ <module>  complete
  → <module>  <n>/<total> (CI retry pending at <time>)

Waiting for you (<n> items):
  1. <module>/<id> — <type>: <one-line what's needed>
     → /pcp approve <module>/<id>

  2. <module>/<id> — architecture decision
     Draft ADR at .pcp/kb/adr/ADR-<N>-DRAFT.md (recommendation: <decision>)
     → /pcp accept-adr <N>  OR  /pcp override-adr <N> "<your decision>"

  3. <module>/<id> — visual approval
     Screenshot: .pcp/previews/<file>  (open automatically)
     → /pcp approve <module>/<id>

Cannot unblock autonomously:
  ⚠ <VAR_NAME> missing — set in Railway dashboard, see .pcp/pending_secrets.md
  ⚠ <External approval> — see .pcp/pending_actions.md

Overall: <X>/<Y> criteria complete (<Z>%). Continue? (/pcp build --all resumes)
```

### True stops (only 2 categories)

| Category | Why PCP cannot self-resolve |
|---|---|
| Production credentials (API keys, DB passwords, OAuth secrets) | Must never generate or guess real credentials. Security boundary. |
| Regulatory / external approval (OAuth app review, app store approval, entitlement from vendor) | External to PCP's control. Cannot be automated. |

For both: write to `.pcp/pending_actions.md` with exact instructions. Build everything that doesn't depend on them.

---

## ESCALATION RULES

**No blocking prompts. Build never stops for deferrable input.**

| Situation | PCP Action |
|---|---|
| Architecture decision not in ADRs | Self-decide → write DRAFT ADR → continue → notify async |
| `check: manual` | Defer → notify → continue |
| `check: visual` | Try dom_contains fallback → screenshot → defer → notify → continue |
| CI fail after 3 retries | Defer with 30min retry → notify → continue other criteria |
| Missing env var / secret | Defer deploy criteria only → write pending_secrets.md → notify → continue code |
| Merge conflict (resolvable via ADR priority) | Auto-resolve → continue |
| Merge conflict (ambiguous logic) | Isolate module → defer → notify → continue other modules |
| Integration regression after merge | Auto-bisect → revert culprit → defer → notify → continue next wave |
| Spec drift (backward-compatible) | Adopt new spec → continue |
| Spec drift (breaking change to completed work) | Defer → notify with diff → continue unaffected modules |
| Context near limit | Checkpoint → commit WIP → write handoff note → notify |
| `pcp check` BLOCK after 2 self-fix attempts | Defer → notify with finding → continue unblocked criteria |
| Spec file modified by agent | Never auto-resolve → defer immediately → notify |
| Production smoke test failed | Rollback immediately → notify URGENT → pause deploy criteria → continue code-only |
| **Missing production credentials** | TRUE STOP for that module's deploy — write to pending_actions.md, continue all else |
| **External regulatory/vendor approval** | TRUE STOP for dependent criteria — write to pending_actions.md, continue all else |

Slack notification format (every deferred item):
```
⚠ PCP [<project>] <module>/<id>
Type: <manual | visual | architecture-decision | blocked-ci | blocked-secret>
Summary: <one sentence what happened>
Your action: <exact command — /pcp approve, /pcp accept-adr, set env var, etc.>
Build: continuing on <other modules/criteria>
```

---

## MODULARITY PROTOCOL — NON-NEGOTIABLE

**Philosophy baked in at every layer.**

Vibe coders pivot. Features get dropped. Modules get added. The codebase must absorb this without surgery.

**The rule:** Every module is a guest in the codebase. It can be removed by deleting its directory. It can be added without touching existing modules. Nothing else should know about it except the application registry.

---

### Layer 1: Application structure (enforced on every project)

When generating the project scaffold, PCP always creates this structure regardless of language:

```
src/
  main.<ext>            ← orchestrator: loads modules from registry. knows nothing about modules.
  interfaces/           ← typed contracts. the ONLY thing modules are allowed to share.
    I<Module>.ts/.py    ← interface per module (what it exposes to the world)
  modules/
    <module>/
      index.<ext>       ← public surface: exports only what interface requires
      src/              ← implementation: never imported by any other module
      tests/
      feature_flag.env  ← FEATURE_<MODULE>_ENABLED=false (default off)
  core/                 ← infrastructure only: logging, config, db connection
                        ← core is NOT a module. modules may depend on core. never on each other.
```

Language-specific patterns:
- **TypeScript/JS**: modules register via `app.register(AuthModule)` — no `import { doAuth } from '../auth/src/service'`
- **Python**: modules register in `app.py` via plugin pattern — no cross-module `from auth.service import ...`
- **Rust**: modules are crate features — `[features] auth = []`
- **Swift**: modules are Swift packages or targets — no target-to-target source imports

---

### Layer 2: Auto-generated modularity rules in `ci_rules.yaml`

PCP adds these to EVERY project's `ci_rules.yaml` during `/pcp new`:

```yaml
# Modularity rules — generated by PCP, do not remove
- id: MOD_001
  check: ast_pattern
  description: "No direct cross-module implementation imports"
  pattern: "from \\.\\.(/\\.\\.)?/[^/]+/src/"   # catches ../../other-module/src/
  severity: hard_block
  message: "Modules must communicate through interfaces/, not by importing each other's src/"

- id: MOD_002
  check: ast_pattern
  description: "No hardcoded module names in application orchestrator"
  pattern: "require\\(['\"]\\.\\./"              # direct require of module internals
  severity: hard_block

- id: MOD_003
  check: file_exists
  description: "Every module must have a public interface file"
  target: "src/interfaces/I{module}.ts"          # checked per module
  severity: hard_block

- id: MOD_004
  check: ast_pattern
  description: "No shared global mutable state between modules"
  pattern: "global\\s+\\w+\\s*="                 # language-specific, adapt per stack
  severity: hard_block

- id: MOD_005
  check: file_exists
  description: "Every module must have a feature flag"
  target: "src/modules/{module}/feature_flag.env"
  severity: hard_block
```

---

### Layer 3: Auto-generated modularity BLOCK rules in `architect_persona.md`

PCP adds these to EVERY project's architect_persona.md:

```markdown
## Modularity Invariants (always enforced, not project-specific)

BLOCK:
- Any direct import of another module's `src/` directory
- Any module that imports from another module's `index` except through the interface type
- Any global mutable state shared between modules (singleton patterns, global stores)
- Any module that directly instantiates another module (use dependency injection via registry)
- Feature code shipped without a feature flag (all new modules default off)
- A module whose tests require another module to be running (tests must be isolated)

WARN:
- A module with more than 3 dependencies in dependency_map.md (God module risk)
- A module that owns more than 3 database tables (God module risk)
- A module that directly modifies another module's database tables

LENIENT:
- Module-internal design patterns (each module can use its own patterns)
- Module test framework choices (each module can use its preferred test tool)
```

---

### Layer 4: Mandatory acceptance criteria on every module

PCP auto-adds these criteria to EVERY module's `acceptance.yaml`:

```yaml
# Modularity criteria — added by PCP to every module, do not remove
- id: MOD_A001
  description: "Module can be dropped without breaking other modules"
  check: test_passes
  test: "tests/modularity/test_drop_<module>.sh"
  notes: "Delete or disable module, run full test suite of remaining modules — must pass"

- id: MOD_A002
  description: "Module registers through application interface, not via direct import"
  check: ast_pattern
  target: "src/main.<ext>"
  pattern: "register\\(<Module>\\)|plugin\\(<Module>\\)|mount\\(<Module>\\)"

- id: MOD_A003
  description: "Module has a feature flag, default off"
  check: file_exists
  target: "src/modules/<module>/feature_flag.env"

- id: MOD_A004
  description: "Module interface file exists and is typed"
  check: file_exists
  target: "src/interfaces/I<Module>.<ext>"
```

---

### Layer 5: Drop test in every build cycle

After completing ALL criteria for a module, run the drop test before marking the module done:

```bash
# Drop test — verify module can be removed without breaking others
echo "Running drop test for <module>..."

# Temporarily disable module
git stash -- src/modules/<module>/

# Run full test suite of ALL OTHER modules
pytest --ignore=src/modules/<module>/ -q 2>/dev/null || \
  npm test -- --testPathIgnorePatterns="modules/<module>" 2>/dev/null || \
  cargo test --features "$(grep -v '<module>' Cargo.toml | grep 'features' | ...)" 2>/dev/null

DROP_EXIT=$?

# Restore module
git stash pop

if [ $DROP_EXIT -ne 0 ]; then
  echo "DROP TEST FAILED: removing <module> breaks other modules"
  echo "Cross-module coupling detected — must fix before marking module complete"
  # Identify which tests failed → locate the coupling → fix it
fi
```

Drop test failure = cross-module coupling found. Fix the coupling before marking the module complete. Add the coupling fix to the current criterion's implementation.

---

### Layer 6: `pcp validate-strategy` extended — coupling check

In addition to coverage, `pcp validate-strategy` now also checks:

```
Coupling analysis:
  ✓ auth → no implementation imports from other modules
  ✓ payment → no implementation imports from other modules
  ✗ dashboard → imports directly from auth/src/session.ts (COUPLING)
  ✗ billing → bidirectional dependency with payment (CIRCULAR)

Coupling violations: 2
  → Fix: dashboard must use IAuthModule interface, not auth/src directly
  → Fix: billing/payment circular dep — extract shared type to interfaces/
```

If coupling violations exist: cannot proceed to next wave. Fix coupling first.

---

### Layer 7: Module add/drop commands

```
/pcp add-module <name> "<one-line purpose>"
  → generates spec.yaml, acceptance.yaml, interface file, feature flag, test scaffold
  → adds to dependency_map.md with no deps by default
  → runs pcp validate-strategy to check coverage impact
  → ready to /pcp build --module <name>

/pcp drop-module <name>
  → runs drop test to verify safe removal
  → if safe: removes module directory, updates decomposition.md, updates dependency_map.md
  → runs pcp validate-strategy to check coverage impact of removal
  → removes from acceptance tracking
  → if unsafe: shows which modules depend on it, recommends decoupling steps first

/pcp pivot "<new direction>"
  → runs pcp validate-strategy with new context
  → shows: which modules are still relevant, which can be dropped, what new modules are needed
  → generates add/drop plan for PM to approve
  → executes plan after approval
```

---

### Why this matters for vibe coders

Without this:
```
PM: "actually let's drop payments for now and add a referral system"
Dev: *3 days of refactoring* "ok done"
```

With PCP modularity protocol:
```
PM: "actually let's drop payments for now and add a referral system"
PCP: /pcp drop-module payment → drop test passes → removed
     /pcp add-module referral "track and reward user referrals" → scaffold generated
     /pcp build --module referral → builds autonomously
     Time: hours, not days. Zero surgery on existing code.
```

---

## CONTEXT DRIFT PREVENTION

Before writing code for any criterion:
1. Re-read spec.yaml constraints
2. Confirm tech stack vs architecture.md
3. Internalize architect_persona.md BLOCK rules — know the rules before writing a line
4. Check if files touch any ADR boundary — if yes, follow that ADR's pattern exactly
5. Never make a new architectural decision without first checking ADRs + persona

If a decision is genuinely new: self-decide using best practices, document as DRAFT ADR, continue building. Do not stop.

---

## HONESTY RULES

- Never mark `status: complete` without every Completion Definition item passing
- Never commit with `pcp check` BLOCK
- Never skip regression suite
- Never skip secret scan — no credentials ever reach git
- Never invent CI or Railway status — read from CLI output
- Never spawn more agents than modules needing work
- Never silently skip a deferred item — always write to deferred_queue.yaml + notify
- Never auto-resolve a spec file conflict unattended — specs are human-approved; defer to the PM, then apply via `pcp correct-objective` / `pcp pm` / `pcp amend` (diff shown, PM approves, then written)
- Never generate or guess production credentials — security boundary, always stop and notify
- Never redeploy to production after rollback without PM confirmation
- If Playwright not installed and `check: visual` required: notify and defer rather than skip
- If `pcp` CLI not installed: `pip install program-context-protocol` first
- Draft ADRs are labelled DRAFT — never present them as accepted decisions
- If a module's build is completely blocked (all criteria depend on a true-stop item): say so clearly in status, move to next module
- If git worktree fails: fall back to sequential build on main branch, tell PM

---

## INTERVENTION LOGGING

Every human interaction is logged to `.pcp/intervention_log.yaml`. This is the learning signal. Metadata only — no code, no secrets, no project content.

### `_log_intervention(pcp_dir, entry)` — called on every human touchpoint

```python
import yaml
from datetime import datetime, timezone
from pathlib import Path

def _log_intervention(pcp_dir: Path, entry: dict) -> None:
    log_path = pcp_dir / "intervention_log.yaml"
    existing = []
    if log_path.exists():
        data = yaml.safe_load(log_path.read_text()) or {}
        existing = data.get("interventions", [])
    entry["logged_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    existing.append(entry)
    log_path.write_text(yaml.dump({"interventions": existing}, default_flow_style=False))
```

### Log on every human touchpoint

| Trigger | type field | Extra fields |
|---|---|---|
| `/pcp approve <module>/<id>` (manual) | `manual_approval` | module, criterion_id, criterion_description, time_to_resolve_minutes, outcome |
| `/pcp approve <module>/<id>` (visual) | `visual_approval` | module, criterion_id, screenshot_path, time_to_resolve_minutes, outcome |
| `/pcp feedback <module>/<id> "<text>"` | `visual_feedback` | module, criterion_id, feedback (text), retest_triggered: true |
| `/pcp accept-adr <id>` | `adr_accepted` | adr_id, topic, time_to_resolve_minutes |
| `/pcp override-adr <id> "<decision>"` | `adr_overridden` | adr_id, topic, original_decision, override_decision, time_to_resolve_minutes |
| Escalation resolved (PM replied) | `escalation_resolved` | reason, module, deferred_duration_minutes, resolution |
| UAT retest cycle completes | `uat_retest` | scenario, retest_count, root_cause, time_to_pass_minutes |
| Deploy gate: migration approved | `migration_approved` | migration_file, time_to_resolve_minutes |
| Deploy gate: rollout % decided | `rollout_decision` | module, rollout_pct, reason |

### Schema — intervention_log.yaml

```yaml
interventions:
  - logged_at: "2026-06-29T14:23:00Z"
    type: visual_approval
    module: payments
    criterion_id: PAY_005
    criterion_description: "Payment confirmation screen looks correct"
    time_to_resolve_minutes: 3
    outcome: approved

  - logged_at: "2026-06-29T15:10:00Z"
    type: visual_feedback
    module: auth
    criterion_id: AUTH_003
    feedback: "Button is too small on mobile"
    retest_triggered: true

  - logged_at: "2026-06-29T16:00:00Z"
    type: adr_overridden
    adr_id: ADR-003
    topic: database_choice
    original_decision: "SQLite for simplicity"
    override_decision: "PostgreSQL — need concurrent writes"
    time_to_resolve_minutes: 5
```

### Daily aggregation (runs via cron)

Cron reads all intervention logs across all PCP projects, aggregates patterns, sends to Slack, writes to `~/.pcp/global_learning.yaml`:

```
Most frequent manual criteria:    auth flows (OAuth) — 4/5 projects
Longest to resolve:               missing API keys — avg 127 min deferred
Most UAT retests:                 payment edge cases — avg 2.8 retests/scenario
Most overridden ADRs:             database choice — 3/5 overridden
→ Next automation targets: OAuth verification, pre-flight API key check, payment edge case criteria templates
```

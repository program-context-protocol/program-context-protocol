PCP — Program Context Protocol
================================

Status: private, pre-launch. Not yet validated across the three dogfood
projects required before public release (see "Status" below).

What this is
------------
PCP is a protocol and CLI that prevents LLM coding agents from drifting
away from a project's original intent across long, multi-session builds.

It works by keeping three things separate and machine-checkable instead
of trusting an agent's self-report:

  Spec           human-written, immutable (objective.md, module specs)
  Current State  auto-generated from the actual code (pcp scan)
  Diff           the gap between the two, computed, never guessed

Three gates enforce this across the lifecycle:

  Layer 1  pre-commit   deterministic AST/schema check      hard block
  Layer 2  PR review     LLM alignment score                advisory
  Layer 3  deploy        SDLC phase-exit criteria            hard block

Pioneer claim: `pcp validate-strategy` checks whether a project's module
decomposition actually covers its stated objective (coverage_score,
LLM-judged) and whether the modules are cleanly decoupled (coupling_score,
deterministic graph math via networkx). No existing tool (BMAD, Kiro,
Spec Kit, Cline Memory Bank) does both automatically.

Install
-------
Not yet published to PyPI. Install from source:

  git clone <this repo>
  cd pcp
  pip install -e .

Optional extras:
  pip install -e ".[graph]"     community-detection enrichment (graphify)
  pip install -e ".[process]"   Temporal durability layer (needs the
                                 separate `temporal` CLI for local dev)

Core commands
-------------
  pcp init                  scaffold .pcp/ in a project
  pcp kickoff <vision.md>    generate spec files from a product vision
  pcp validate-strategy      does the module decomposition cover the objective?
  pcp build [--module]       autonomous coding loop per acceptance criterion
  pcp check                  Layer 1 pre-commit gate
  pcp gate                   Layer 2 PR advisory gate
  pcp deploy-check            Layer 3 deploy gate
  pcp deploy                  checklist, approval, smoke test, auto-rollback
  pcp status [--pm]           refresh pcp.md / plain-English PM report
  pcp watch                   poll CI + deploy health, auto-fix failures
  pcp audit                   advisory dead-code/bloat sweep
  pcp telemetry               per-module cost/retry/QA rollup
  pcp provenance               audit-evidence document, per-file x control
  pcp capture                  classify session drift into BRD / decision log
  pcp doctor                   detect/configure CLI tooling for this project
  pcp takeover                 preflight + kickoff + build in one call

Full command list: `pcp --help`.

Companion project
------------------
ontology-foundry (github.com/ganeshnallasivam-cell/ontology-foundry) holds
the knowledge layer that used to live in this repo: ontology extraction,
requirements traceability, the 5-object domain model, and the observatory
command-center view. It depends on this package as an installed library —
it is not a fork, and PCP's own core does not depend on it.

Status
------
Not yet public. Per this project's own governance rule, launch (domain
registration, GitHub org, PyPI/npm publish) is gated on validating PCP
across three real dogfood projects — not yet complete. No LICENSE file
yet — add one before any public push.

This repository has no deploy pipeline. It ships as a git repo / pip
package, not a hosted service.

License
-------
Not yet chosen / not yet applied. Do not treat this repository as
licensed for reuse until a LICENSE file is added.

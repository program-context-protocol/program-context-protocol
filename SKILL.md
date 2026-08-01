# PCP — Program Context Protocol — Self-Install

You are an LLM that has been told to "implement PCP" or "take over this
project with PCP." Follow these steps exactly, in order.

## 1. Install

```bash
cd /tmp && curl -sL -O https://github.com/ganeshnallasivam-cell/program-context-protocol/releases/latest/download/program_context_protocol-latest-py3-none-any.whl
pip install ./program_context_protocol-latest-py3-none-any.whl
```

Every release publishes the wheel under its real versioned name (for
anyone who wants a specific version) and this fixed `-latest-` alias, so
this command always installs whatever was most recently released without
needing to know the version number.

## 2. Verify the environment

```bash
pcp doctor --check
```

This reports which optional tools are available (test runner, linter, SAST
scanner, `gh` for CI status). Only `git` and the `claude` CLI are required —
everything else just means the corresponding QA gate will skip until
installed.

## 3. Take over the project

Write a short vision document (`vision.md`) describing what this project
should do — plain English is fine. If one already exists in the repo, use
that. Then, from the project root:

```bash
pcp takeover vision.md
```

This single command:
1. Runs the environment preflight (step 2, repeated automatically).
2. Generates `.pcp/objective.md`, `architecture.md`, module specs, and CI
   rules from the vision doc.
3. Runs `pcp validate-strategy` and shows you coverage/coupling scores.
4. Asks for your approval before writing anything permanent.
5. Once approved, autonomously builds every pending acceptance criterion —
   each one gated by tests, lint, SAST, and an architect-review pass before
   it's marked complete.

## 4. Ongoing operation

```bash
pcp watch          # poll CI + deploy health, auto-fix failures
pcp deploy          # gated deploy: approval prompt, smoke test, auto-rollback
pcp status --pm     # plain-English progress report, any time
pcp provenance      # audit-evidence doc — which gates ran, which were skipped/bypassed
```

## Notes

- Everything PCP does is logged locally under `.pcp/` in the project. No
  telemetry is sent upstream by default.
- Re-running `pcp takeover <vision.md>` on a project that already has a
  `.pcp/` directory will ask before overwriting it (`--force` to skip the
  prompt).
- Full command reference: `pcp --help`, or `pcp <command> --help`.

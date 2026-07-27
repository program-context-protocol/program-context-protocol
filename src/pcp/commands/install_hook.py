"""pcp install-hook — install pcp check as a git commit-msg hook (Layer 1 gate)."""

import sys
from pathlib import Path

import click
from rich.console import Console

from pcp.pcp_dir import find_pcp_dir, NoPCPDir

console = Console()

COMMIT_MSG_HOOK = """\
#!/bin/sh
# PCP Layer 1 gate + commit hygiene
# Installed by: pcp install-hook
#
# Strips any Co-Authored-By trailer referencing Claude/Anthropic before the
# commit message is finalized -- defense in depth for "never attribute a
# commit to Claude": even if some other tool, session, or human adds one by
# habit, it never reaches the actual commit object. Uses perl, not sed -i,
# because sed's in-place-edit flag syntax differs between BSD (macOS) and
# GNU (Linux) sed -- perl -ni is identical on both.
perl -ni -e 'print unless /^Co-Authored-By:.*(claude|anthropic)/i' "$1"
#
# Agent-session attribution trailer (2026-07-17, incremental identity
# hardening): when the commit happens inside a PCP agent session, stamp the
# session id into the commit message as a queryable trailer. Still rooted in
# a self-declared env var — honestly NOT cryptographic identity (the IETF
# dynamic-attestation draft is the eventual target) — but it moves the
# marker from "ambient env var at gate time" to "recorded on the commit
# object itself", queryable via plain `git log --grep`.
if [ -n "$PCP_AGENT_SESSION" ]; then
  if ! grep -q '^PCP-Agent-Session:' "$1"; then
    printf '\nPCP-Agent-Session: %s\n' "${PCP_AGENT_SESSION_ID:-unidentified}" >> "$1"
  fi
fi
#
# Runs as a commit-msg hook, not pre-commit: git does not write the final
# commit message to disk until after pre-commit runs (confirmed empirically —
# COMMIT_EDITMSG holds the PREVIOUS commit's message at pre-commit time when
# committing via `-m`), so a pre-commit hook can never see a `[pcp-bypass:
# reason]` marker in the message being created. commit-msg fires after the
# message is finalized but still before the commit object is created, so it
# blocks just as effectively and the bypass marker actually works.
pcp check --commit-msg-file "$1"
"""

POST_COMMIT_HOOK = """\
#!/bin/sh
# PCP post-commit state refresh
# Installed by: pcp install-hook / pcp init
#
# Regenerates current_state.md + diff.md after every commit -- not just
# when a human remembers to run `pcp scan` manually, and not just for
# commits made inside `pcp build`'s own loop (which already calls scan
# directly). Closes the gap where a commit made outside that loop (a human
# editing code directly, or `pcp pm`) left drift state stale until the next
# unrelated `pcp scan` invocation.
#
# Best-effort and silent: a post-commit hook runs after the commit object
# already exists, so it can never block or undo the commit -- failures here
# are logged, never fatal. No-ops cleanly if `pcp` isn't on PATH yet or the
# project has no modules scaffolded (pcp scan already handles both cases).
command -v pcp >/dev/null 2>&1 && pcp scan --quiet >/dev/null 2>&1
exit 0
"""

PRE_COMMIT_FRAMEWORK_CONFIG = """\
repos:
  - repo: local
    hooks:
      - id: pcp-check
        name: PCP Layer 1 gate
        entry: pcp check --commit-msg-file
        language: system
        stages: [commit-msg]
        pass_filenames: true
        always_run: true
"""


def _find_git_dir(project_root: Path) -> Path | None:
    import subprocess
    try:
        r = subprocess.run(["git", "rev-parse", "--git-dir"],
                           capture_output=True, text=True, cwd=project_root)
        git_dir = Path(r.stdout.strip()) if r.returncode == 0 else None
    except FileNotFoundError:
        git_dir = None
    if git_dir and not git_dir.is_absolute():
        git_dir = project_root / git_dir
    return git_dir


def install_git_hook(project_root: Path, force: bool = False) -> tuple[bool, str]:
    """Just the commit-msg hook file. Pulled out of the CLI command so `pcp
    init` can call this directly and get a project under real Layer-1
    enforcement the moment it's scaffolded.

    Returns (installed: bool, message: str) -- never raises, so callers
    that want this to be a best-effort side effect (like init.py) can just
    print the message and move on rather than handling exceptions."""
    git_dir = _find_git_dir(project_root)
    if not git_dir:
        return False, "not a git repository yet -- skipped"

    hooks_dir = git_dir / "hooks"
    hooks_dir.mkdir(exist_ok=True)
    hook_path = hooks_dir / "commit-msg"

    if hook_path.exists() and not force:
        if "pcp check --commit-msg-file" in hook_path.read_text():
            commit_msg_result = True, f"already installed at {hook_path}"
        else:
            return False, f"a different commit-msg hook already exists at {hook_path} -- run `pcp install-hook --force` to overwrite, or `--pre-commit-framework` to append"
    else:
        hook_path.write_text(COMMIT_MSG_HOOK)
        hook_path.chmod(0o755)
        commit_msg_result = True, f"installed {hook_path}"

    post_commit_path = hooks_dir / "post-commit"
    if post_commit_path.exists() and not force:
        if "pcp scan" not in post_commit_path.read_text():
            # Don't clobber a human's own post-commit hook -- just skip ours.
            return commit_msg_result
    else:
        post_commit_path.write_text(POST_COMMIT_HOOK)
        post_commit_path.chmod(0o755)

    return commit_msg_result


@click.command()
@click.option("--path", "project_path", type=click.Path(), default=None)
@click.option("--pre-commit-framework", is_flag=True,
              help="Add to .pre-commit-config.yaml instead of .git/hooks/.")
@click.option("--force", is_flag=True, help="Overwrite existing hook.")
def install_hook(project_path: str | None, pre_commit_framework: bool, force: bool):
    """Install pcp check as a git commit-msg hook (needed so `[pcp-bypass: reason]` is visible)."""
    try:
        pcp_dir = find_pcp_dir(Path(project_path) if project_path else None)
    except NoPCPDir as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(2)

    project_root = pcp_dir.parent

    if pre_commit_framework:
        config_path = project_root / ".pre-commit-config.yaml"
        if config_path.exists():
            existing = config_path.read_text()
            if "pcp-check" in existing:
                console.print("[dim]pcp-check already in .pre-commit-config.yaml[/dim]")
                sys.exit(0)
            with open(config_path, "a") as f:
                f.write("\n" + PRE_COMMIT_FRAMEWORK_CONFIG)
            console.print("[green]appended[/green] pcp-check to .pre-commit-config.yaml")
        else:
            config_path.write_text(PRE_COMMIT_FRAMEWORK_CONFIG)
            console.print("[green]created[/green] .pre-commit-config.yaml with pcp-check")
        return

    git_dir = _find_git_dir(project_root)
    if not git_dir:
        console.print("[red]Error:[/red] not a git repository.")
        sys.exit(2)

    hooks_dir = git_dir / "hooks"
    hooks_dir.mkdir(exist_ok=True)
    hook_path = hooks_dir / "commit-msg"

    # Bug fixed 2026-07-21: this used to sys.exit(1) here whenever commit-msg
    # already existed, which meant the post-commit check below could NEVER
    # run in that case -- a project with a working commit-msg hook but a
    # missing post-commit hook (e.g. this repo itself, predating the
    # post-commit feature) could never get it installed via this command,
    # only via `pcp init`'s own install_git_hook() call, which didn't have
    # this bug. commit-msg's own outcome is now independent of post-commit's.
    commit_msg_installed = False
    if hook_path.exists() and not force:
        console.print(f"[yellow]Hook already exists:[/yellow] {hook_path}")
        console.print("Use --force to overwrite (this replaces the whole file — merge manually "
                       "if you have other commit-msg hooks), or --pre-commit-framework to append.")
    else:
        hook_path.write_text(COMMIT_MSG_HOOK)
        hook_path.chmod(0o755)
        console.print(f"[green]installed[/green] {hook_path}")
        console.print("[dim]pcp check will run before every commit finalizes.[/dim]")
        commit_msg_installed = True

    post_commit_path = hooks_dir / "post-commit"
    if post_commit_path.exists() and not force and "pcp scan" not in post_commit_path.read_text():
        console.print(f"[yellow]A different post-commit hook already exists at {post_commit_path} — skipped (use --force to overwrite).[/yellow]")
    else:
        post_commit_path.write_text(POST_COMMIT_HOOK)
        post_commit_path.chmod(0o755)
        console.print(f"[green]installed[/green] {post_commit_path}")
        console.print("[dim]current_state.md + diff.md refresh after every commit.[/dim]")

    _remove_legacy_cron_jobs()

    # commit-msg is the primary hook this command exists for -- preserve the
    # original exit-1-on-refusal contract for it specifically, even though
    # post-commit (checked above, independently) may have installed fine.
    if not commit_msg_installed:
        sys.exit(1)


# Removed 2026-07-27, pre-launch review. This command used to also install two
# global crontab jobs via `_install_cron_scripts()`, unconditionally and with no
# prompt:
#
#   1. `upgrade_skill.sh` — daily `curl` of SKILL.md from a hardcoded personal
#      GitHub raw URL, overwriting ~/.claude/skills/pcp/SKILL.md with no
#      signature or hash check. That file is an AGENT INSTRUCTION file: whatever
#      the fetched content says, the next `/pcp` session executes. A remote,
#      unverified, self-updating instruction channel is the single worst thing
#      to ship in a tool whose entire purpose is governing what agents do. It
#      was dormant only because the origin repo is private (`curl -sf` on a
#      private URL returns empty, so the upgrader no-opped) -- making the repo
#      PUBLIC is what would have armed it.
#   2. `aggregate_interventions.sh` — scanned a hardcoded `~/Claude-code` across
#      every project on the machine and posted a summary to a hardcoded Slack
#      channel. Personal-workflow plumbing with no place in a public tool, and
#      it interpolated each discovered path straight into a `python3 -c` string,
#      so a directory name containing a quote executed arbitrary code daily.
#
# Neither was ever a documented feature of `pcp install-hook`, whose stated job
# is installing git hooks. Deleted outright rather than fixed: skill updates
# belong in the package (`pcp install-skill`, versioned via PyPI), not in an
# out-of-band self-updater.
_LEGACY_CRON_MARKER = "/.pcp/cron/"


def _remove_legacy_cron_jobs() -> None:
    """Uninstall the cron jobs older PCP versions installed silently.

    Deleting the installer does nothing for machines that already ran it --
    those crontab entries keep firing forever. `pcp install-hook` is the
    command that put them there, so it is the right place to take them back
    out. Removes only lines this tool wrote (matched on the `~/.pcp/cron/`
    path it always used) and reports what went; never touches any other
    crontab line, and never fails the hook install if crontab is unavailable."""
    import subprocess

    try:
        existing = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    except FileNotFoundError:
        return
    if existing.returncode != 0:
        return

    lines = existing.stdout.splitlines()
    keep = [ln for ln in lines if _LEGACY_CRON_MARKER not in ln]
    if len(keep) == len(lines):
        return

    removed = [ln for ln in lines if _LEGACY_CRON_MARKER in ln]
    body = "\n".join(keep).strip("\n")
    proc = subprocess.run(
        ["crontab", "-"], input=(body + "\n") if body else "", text=True,
        capture_output=True,
    )
    if proc.returncode != 0:
        console.print(
            "[yellow]Found legacy PCP cron jobs but could not remove them "
            "automatically. Remove these lines with `crontab -e`:[/yellow]"
        )
        for ln in removed:
            console.print(f"  [dim]{ln}[/dim]")
        return

    console.print(f"[green]removed[/green] {len(removed)} legacy PCP cron job(s):")
    for ln in removed:
        console.print(f"  [dim]{ln}[/dim]")
    console.print(
        "[dim]These were installed by an older `pcp install-hook` and included a "
        "daily unverified remote overwrite of your PCP skill file. The scripts "
        "themselves are still on disk at ~/.pcp/cron/ — delete them yourself.[/dim]"
    )



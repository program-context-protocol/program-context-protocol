#!/bin/sh
# Installs this repo's git hooks that aren't auto-wired by `pcp init`
# (commit-msg/pre-commit/post-commit are PCP's own Layer 1 gate, installed
# for any PCP-managed project; pre-push is specific to THIS repo's own
# public-release hygiene, so it lives here instead).
REPO="$(git rev-parse --show-toplevel)"
cp "$REPO/scripts/hooks/pre-push" "$REPO/.git/hooks/pre-push"
chmod +x "$REPO/.git/hooks/pre-push"
echo "Installed: .git/hooks/pre-push"

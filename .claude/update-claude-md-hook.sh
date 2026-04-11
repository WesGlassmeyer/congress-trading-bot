#!/usr/bin/env bash
# PostToolUse hook — fires after every Bash tool call.
# Updates CLAUDE.md and pushes when the command was a successful git push.
set -euo pipefail

REPO="/root/congress-trading-bot"

# ── read hook stdin ───────────────────────────────────────────────────────────
INPUT=$(cat)
CMD=$(echo "$INPUT" | jq -r '.tool_input.command // ""')

# Only act on git push commands
echo "$CMD" | grep -qE 'git push' || exit 0

# ── check the push actually succeeded ────────────────────────────────────────
# tool_response contains the raw output; a successful push prints the remote URL.
RESPONSE_OUTPUT=$(echo "$INPUT" | jq -r '(.tool_response.output // .tool_response.stdout // "") + (.tool_response.stderr // "")')
if ! echo "$RESPONSE_OUTPUT" | grep -qE 'To https://|\.\.\.'; then
    # If we can't tell from output, check git's remote tracking is up to date
    if ! git -C "$REPO" status -sb 2>/dev/null | grep -q "ahead\|behind" ; then
        : # Looks fine — no divergence, proceed
    else
        exit 0  # Something's off, skip the update
    fi
fi

# ── update CLAUDE.md ─────────────────────────────────────────────────────────
cd "$REPO"
python3 update_claude_md.py || exit 0

# Only commit and push if CLAUDE.md actually changed
git diff --quiet CLAUDE.md && exit 0

git add CLAUDE.md
git commit -m "docs: auto-update CLAUDE.md status [$(date -u +%Y-%m-%d)]"
git push origin master

exit 0

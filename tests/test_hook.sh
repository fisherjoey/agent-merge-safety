#!/usr/bin/env bash
# Feeds the hook sample PreToolUse JSON on stdin and checks allow/deny.
# A fake `gh` (tests/bin/gh) stands in for GitHub; see that file for its knobs.
set -uo pipefail

here=$(cd "$(dirname "$0")" && pwd)
hook="$here/../hooks/risky-merge-guard.sh"
export PATH="$here/bin:$PATH"

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
export AGENT_MERGE_GUARD_LOG="$work/guard.log"

# A throwaway git repo so the hook can look for .claude/risk-surfaces.txt.
repo="$work/repo"
git init -q "$repo"
plain="$work/not-a-repo"
mkdir -p "$plain"

pass=0 fail=0

# run_hook <cwd> <command>: prints "deny" or "allow"
run_hook() {
  local input out
  input=$(jq -n --arg c "$2" --arg d "$1" \
    '{hook_event_name:"PreToolUse", tool_name:"Bash", cwd:$d, tool_input:{command:$c}}')
  out=$(printf '%s' "$input" | bash "$hook")
  if printf '%s' "$out" | jq -e '.hookSpecificOutput.permissionDecision == "deny"' >/dev/null 2>&1; then
    echo deny
  else
    echo allow
  fi
}

expect() { # expect <want> <name> <cwd> <command>
  local got
  got=$(run_hook "$3" "$4")
  if [ "$got" = "$1" ]; then
    pass=$((pass + 1)); echo "ok   - $2"
  else
    fail=$((fail + 1)); echo "FAIL - $2 (wanted $1, got $got)"
  fi
}

reset_gh() {
  export FAKE_GH_PR="" FAKE_GH_FILES="" FAKE_GH_BASE=main FAKE_GH_REQUIRED=2
}

# --- Rule 1 ------------------------------------------------------------------
reset_gh
expect deny  "git commit --no-verify"           "$plain" 'git commit -m "wip" --no-verify'
expect deny  "git push --no-verify after &&"    "$plain" 'git add . && git push --no-verify origin main'
expect allow "plain git commit"                 "$plain" 'git commit -m "fix: thing"'
expect allow "--no-verify on another command"   "$plain" 'npm test -- --no-verify'
expect allow "empty command"                    "$plain" ''
expect allow "unrelated command"                "$plain" 'ls -la'

# --- Rule 2: risky diff ------------------------------------------------------
reset_gh
export FAKE_GH_FILES=$'src/api/permissions.ts\nsrc/ui/Button.tsx'
expect deny  "--admin on risky diff"            "$plain" 'gh pr merge 42 --squash --admin'
expect allow "plain merge, base has required checks" "$plain" 'gh pr merge 42 --squash'
FAKE_GH_REQUIRED=0 \
expect deny  "plain merge, base has no required checks" "$plain" 'gh pr merge 42 --squash'
FAKE_GH_REQUIRED=unprotected \
expect deny  "plain merge, unprotected branch (GitHub 404)" "$plain" 'gh pr merge 42 --squash'
FAKE_GH_REQUIRED=unprotected FAKE_GH_RULESET=1 \
expect allow "plain merge, checks required by a ruleset" "$plain" 'gh pr merge 42 --squash'
FAKE_GH_REQUIRED=fail \
expect allow "plain merge, protection unknown fails open" "$plain" 'gh pr merge 42 --squash'
FAKE_GH_REQUIRED=fail \
expect deny  "--admin, protection unknown"      "$plain" 'gh pr merge 42 --admin'
expect allow "ATTENDED_MERGE=1 override"        "$plain" 'ATTENDED_MERGE=1 gh pr merge 42 --squash --admin'
expect deny  "merge chained after other commands" "$plain" 'git fetch && gh pr merge 42 --admin'

# PR number from the current branch
FAKE_GH_PR=42 \
expect deny  "--admin with no PR number, resolved from branch" "$plain" 'gh pr merge --squash --admin'
expect allow "no PR resolvable fails open"      "$plain" 'gh pr merge --squash --admin'

# --- Rule 2: safe diff and unavailable diff ----------------------------------
reset_gh
export FAKE_GH_FILES=$'src/ui/Button.tsx\ndocs/intro.md'
expect allow "--admin on safe diff"             "$plain" 'gh pr merge 7 --admin'
export FAKE_GH_FILES=""
expect allow "diff unavailable fails open"      "$plain" 'gh pr merge 7 --admin'

# Default surfaces cover migrations and payments
export FAKE_GH_FILES='db/migrations/0042_add_col.sql'
expect deny  "default: migrations"              "$plain" 'gh pr merge 7 --admin'
export FAKE_GH_FILES='app/billing/checkout.py'
expect deny  "default: billing"                 "$plain" 'gh pr merge 7 --admin'

# --- Per-repo risk-surfaces.txt replaces the default -------------------------
mkdir -p "$repo/.claude"
cat >"$repo/.claude/risk-surfaces.txt" <<'EOF'
# Only these count as risky in this repo
^infra/
deploy\.ya?ml$

EOF
export FAKE_GH_FILES='infra/dns.tf'
expect deny  "repo surfaces: infra/ matches"    "$repo" 'gh pr merge 9 --admin'
export FAKE_GH_FILES='src/api/permissions.ts'
expect allow "repo surfaces replace the default" "$repo" 'gh pr merge 9 --admin'
export FAKE_GH_FILES='.github/workflows/deploy.yml'
expect deny  "repo surfaces: second line"       "$repo" 'gh pr merge 9 --admin'

# --- Default regex can be overridden by env ----------------------------------
export FAKE_GH_FILES='src/api/permissions.ts'
AGENT_MERGE_GUARD_DEFAULT_RISK='nothing-matches-this' \
expect allow "env default override"             "$plain" 'gh pr merge 9 --admin'

# --- The log records decisions ------------------------------------------------
if grep -q 'DENY risky merge PR#42' "$AGENT_MERGE_GUARD_LOG" && grep -q 'OVERRIDE' "$AGENT_MERGE_GUARD_LOG"; then
  pass=$((pass + 1)); echo "ok   - decisions are logged"
else
  fail=$((fail + 1)); echo "FAIL - decisions are logged"
fi

echo
echo "hook: $pass passed, $fail failed"
[ "$fail" -eq 0 ]

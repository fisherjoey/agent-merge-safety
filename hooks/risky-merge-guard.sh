#!/usr/bin/env bash
# Claude Code PreToolUse(Bash) hook: a mechanical backstop on agent merges.
#
# Two rules, both project-agnostic:
#   1. --no-verify on `git commit` / `git push` is always denied. It exists to skip
#      the hooks that are the last thing standing between an agent branch and the
#      default one.
#   2. `gh pr merge` is denied when the PR's diff touches a high-risk surface AND
#      nothing else gates the merge: either --admin was passed (which tells GitHub
#      to stop enforcing required checks), or the base branch has no required
#      status checks at all (so a plain merge gates nothing either). CI is weak
#      evidence exactly where those surfaces fail: an authz or tenancy defect is
#      silent and green. Those merges belong in an attended session.
#
# Per-repo risk surfaces: <repo>/.claude/risk-surfaces.txt (one extended regex per
# line, '#' comments and blank lines ignored). Falls back to the default below.
#
# Override (meant for a human, and logged):
#   ATTENDED_MERGE=1 gh pr merge 123 --squash --admin
#
# Fails OPEN when it cannot determine the diff (gh down, no PR found). This is a
# backstop behind your pipeline's own risk classifier and review, not the only
# gate, and denying every merge during a GitHub outage costs more than it saves.
#
# Environment:
#   AGENT_MERGE_GUARD_LOG           log file (default: $XDG_STATE_HOME/agent-merge-safety/risky-merge-guard.log)
#   AGENT_MERGE_GUARD_DEFAULT_RISK  replaces the built-in default regex
#
# Requires: bash, jq, gh (authenticated), git, coreutils `timeout`.

set -uo pipefail

LOG="${AGENT_MERGE_GUARD_LOG:-${XDG_STATE_HOME:-$HOME/.local/state}/agent-merge-safety/risky-merge-guard.log}"
BUILTIN_RISK='((^|/)auth|authz|authoriz|permission|rbac|(^|/)acl|polic(y|ies)|tenan|organization[-_]?(id|boundary|scoping)|migrations?/|login|session|token|password|secret|credential|invoic|payroll|payment|billing|stripe)'
DEFAULT_RISK="${AGENT_MERGE_GUARD_DEFAULT_RISK:-$BUILTIN_RISK}"

deny() {
  printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":%s}}' \
    "$(jq -Rn --arg s "$1" '$s')"
  exit 0
}

note() {
  mkdir -p "$(dirname "$LOG")" 2>/dev/null || return 0
  printf '%s\t%s\n' "$(date -Is)" "$1" >>"$LOG" 2>/dev/null || true
}

input=$(cat)
cmd=$(printf '%s' "$input" | jq -r '.tool_input.command // ""' 2>/dev/null) || cmd=""
[ -n "$cmd" ] || exit 0

# Run gh/git from the session's working directory when the hook input names one.
hook_cwd=$(printf '%s' "$input" | jq -r '.cwd // ""' 2>/dev/null) || hook_cwd=""
if [ -n "$hook_cwd" ] && [ -d "$hook_cwd" ]; then
  cd "$hook_cwd" || true
fi

# --- Rule 1: --no-verify is never legitimate from an agent -------------------
if printf '%s' "$cmd" | grep -qE '(^|[[:space:];&|])git[[:space:]]+(commit|push)\b' \
   && printf '%s' "$cmd" | grep -qE '(^|[[:space:]])--no-verify(\b|$)'; then
  note "DENY no-verify: ${cmd:0:200}"
  deny "--no-verify skips the pre-commit/pre-push hooks, which are the only checks an --admin merge does not bypass. Fix what the hook reports instead of routing around it."
fi

# --- Rule 2: risky merges ---------------------------------------------------
# Matches the merge VERB, not the --admin flag. --admin is only a bypass when there
# is something to bypass: on a repo with no required status checks (GitHub Free, or
# simply an unprotected default branch) a plain `gh pr merge --squash` lands the same
# commit with the same absence of gating. Guarding only --admin leaves a hole an
# agent walks through by retrying without the flag, which does happen in practice.
# So: decide on the diff first, then use protection state to work out whether the
# flag mattered.
printf '%s' "$cmd" | grep -qE '(^|[[:space:];&|])gh[[:space:]]+pr[[:space:]]+merge\b' || exit 0

if printf '%s' "$cmd" | grep -qE '(^|[[:space:]])ATTENDED_MERGE=1'; then
  note "OVERRIDE attended merge: ${cmd:0:200}"
  exit 0
fi

# PR number: the first bare integer after `gh pr merge`. Absent = "current branch",
# which gh resolves itself; ask gh for it the same way.
pr=$(printf '%s' "$cmd" | sed -nE 's/.*gh[[:space:]]+pr[[:space:]]+merge[[:space:]]+([0-9]+).*/\1/p')
if [ -z "$pr" ]; then
  pr=$(timeout 15 gh pr view --json number -q .number 2>/dev/null) || true
fi
[ -n "$pr" ] || { note "ALLOW (no PR resolved): ${cmd:0:120}"; exit 0; }

files=$(timeout 20 gh pr view "$pr" --json files -q '.files[].path' 2>/dev/null) || true
[ -n "$files" ] || { note "ALLOW (diff unavailable) PR#$pr"; exit 0; }

# Per-repo overrides live next to the code they describe.
risk="$DEFAULT_RISK"
root=$(git rev-parse --show-toplevel 2>/dev/null) || root=""
cfg="$root/.claude/risk-surfaces.txt"
if [ -n "$root" ] && [ -f "$cfg" ]; then
  joined=$(grep -vE '^[[:space:]]*(#|$)' "$cfg" | paste -sd'|' -)
  [ -n "$joined" ] && risk="($joined)"
fi

hits=$(printf '%s\n' "$files" | grep -iE "$risk" | head -8)
[ -n "$hits" ] || exit 0

# The diff is risky. Does dropping --admin actually leave a gate standing?
# Only if the PR's base branch has required status checks configured.
admin=no
printf '%s' "$cmd" | grep -qE '(^|[[:space:]])--admin(\b|$)' && admin=yes

required=unknown
base=$(timeout 15 gh pr view "$pr" --json baseRefName -q .baseRefName 2>/dev/null) || base=""
if [ -n "$base" ]; then
  # GitHub answers 404 with a specific message when the branch is unprotected or
  # has no status checks: that is a real "none". Any other failure (outage, a
  # token that cannot read protection settings) leaves the state unknown.
  errf=$(mktemp)
  if nreq=$(timeout 15 gh api "repos/{owner}/{repo}/branches/$base/protection/required_status_checks" \
              -q '(.contexts // []) | length' 2>"$errf"); then
    case "$nreq" in
      ''|0) required=none ;;
      *)    required="$nreq" ;;
    esac
  elif grep -qiE 'Branch not protected|Required status checks not enabled' "$errf"; then
    required=none
  fi
  rm -f "$errf"
  # Required checks can also come from a repository ruleset rather than classic
  # branch protection. Count those too before calling the branch ungated.
  if [ "$required" = none ]; then
    nrules=$(timeout 15 gh api "repos/{owner}/{repo}/rules/branches/$base" \
               -q '[.[] | select(.type == "required_status_checks")] | length' 2>/dev/null) || nrules=""
    case "$nrules" in
      ''|0) ;;
      *)    required="ruleset:$nrules" ;;
    esac
  fi
fi

# Fail OPEN when the flag was absent AND protection state is unknown (see header).
if [ "$admin" = no ] && [ "$required" != none ]; then
  note "ALLOW plain merge PR#$pr (base '$base' has required checks: $required)"
  exit 0
fi

if [ "$admin" = yes ]; then
  why="--admin drops the required checks on it"
  fix="Merge this in an attended session."
else
  why="its base branch '$base' has NO required status checks, so a plain merge gates nothing and is equivalent to --admin here"
  fix="Merge this in an attended session. Dropping --admin does not make it safer on this repo."
fi

note "DENY risky merge PR#$pr (admin=$admin, required=$required): $(printf '%s' "$hits" | tr '\n' ' ')"
deny "PR #$pr touches a high-risk surface, and $why:

$(printf '%s' "$hits" | sed 's/^/  - /')

CI is weak evidence here: an authz, tenancy, migration, auth or payment defect fails silently and green. $fix
A human can override deliberately with:  ATTENDED_MERGE=1 <the same command>
Repo-specific surfaces: ${root:-<repo>}/.claude/risk-surfaces.txt"

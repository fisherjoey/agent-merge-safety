# agent-merge-safety

Guardrails for Claude Code setups where agents open and merge pull requests.

Agents can now write, review and merge code without a person watching. Getting code written is cheap. What stays limited is review capacity, and defects stay hidden for a while. A bug that an agent merged on Tuesday shows up three weeks later, and by then it looks like someone else's fault. CI doesn't help much on the changes that matter most. A broken permission check or a tenant-scoping mistake usually passes every test.

This repo has three pieces:

| Piece | What it does |
|---|---|
| `hooks/risky-merge-guard.sh` | A Claude Code `PreToolUse` hook for the Bash tool. It denies `git commit`/`git push` with `--no-verify`, and denies `gh pr merge` when the PR touches a risky path and nothing else would stop a bad merge. |
| `scripts/escape-ledger.py` | Tracks how many defects escape per merged PR, per repo, so you can tell whether your agent pipeline is adding bugs. |
| `skill/agent-drain-gate/` | A Claude Code skill that writes down the policy the other two enforce: merge by risk tier, count escapes, accept "no change needed" as a result, cap merges per run, and audit a sample of old merges cold. |

## The merge guard

The hook reads the command Claude is about to run from stdin and applies two rules.

**Rule 1: no `--no-verify`.** `git commit --no-verify` and `git push --no-verify` are always denied. Pre-commit and pre-push hooks are the only checks that `gh pr merge --admin` can't skip, so an agent shouldn't be able to skip them either.

**Rule 2: no unattended merges of risky diffs.** When the command runs `gh pr merge`, the hook:

1. Works out the PR number, either from the command or from the current branch through `gh pr view`.
2. Gets the list of changed files from `gh pr view --json files`.
3. Checks those paths against the repo's risk surfaces (see below). If nothing matches, the merge goes ahead.
4. If something matches, it looks at how the merge is gated:
   - With `--admin`, GitHub skips required checks, so the merge is denied.
   - Without `--admin`, it asks GitHub whether the base branch requires status checks, through classic branch protection or a repository ruleset. If none are required, a plain merge gates nothing and is denied as well. Agents that get told no on `--admin` tend to try again without the flag, and on an unprotected branch that lands the same commit.
   - If checks are required, the merge goes ahead and CI does its job.

A person can override the guard on purpose:

```bash
ATTENDED_MERGE=1 gh pr merge 123 --squash --admin
```

Every deny and every override is written to a log file.

The hook fails open. If it can't work out the PR, can't read the diff, or can't reach GitHub to check branch protection (and `--admin` wasn't used), it allows the command and logs why. It backs up your pipeline's own review. Blocking every merge during a GitHub outage would cost more than it saves.

### Risk surfaces

Each repo can describe its own risky paths in `.claude/risk-surfaces.txt`, one extended regex (`grep -E`) per line. Matching is case-insensitive and runs against each changed file path. Blank lines and lines starting with `#` are ignored. See [`examples/risk-surfaces.txt`](examples/risk-surfaces.txt).

When the file exists it **replaces** the built-in default. It doesn't add to it. When it's missing, the hook uses a default that covers auth, permissions, policies, tenancy, migrations, sessions, tokens, secrets, credentials, billing, payments, invoicing and payroll. To change the default for every repo, set `AGENT_MERGE_GUARD_DEFAULT_RISK`.

Write the file based on what is actually in the repo. A marketing site's risk is lead capture and payments. A tool that pushes to other repos with your tokens is risky almost everywhere. If a test file matches, that isn't a false positive: weakening a test so a bad change passes is one of the main ways green CI hides bugs.

## The escape ledger

`escape-ledger.py` records the files each merged PR changed. It detects the repo from the `origin` remote, or you can pass `--repo owner/name`.

```bash
# After every agent merge. Do it right away, not in a batch later.
escape-ledger.py record TICKET-12 345

# PRs that touched a file some other PR changed in the last 30 days
escape-ledger.py candidates --days 30

# A bug you traced back to a merged PR
escape-ledger.py escape BUG-7 345 "null check dropped in the refactor"

# Defects per merged PR, grouped by merge date ("wave")
escape-ledger.py report

# Pick a merged PR at least 7 days old for a cold audit
escape-ledger.py audit-pick --min-age-days 7
```

`record` needs an authenticated `gh`. The other subcommands only read the ledger file.

Keep two things in mind when reading the numbers:

- `candidates` gives you a list to look at. It doesn't count defects. Normal development touches the same files again just like rework does, so the level on its own tells you little. What matters is how confirmed escapes per PR change over time.
- The rate means little until you have at least 20 merges recorded, and the report tells you so.

Ledgers are stored at `$AGENT_MERGE_SAFETY_DATA/<owner>__<repo>/escape-ledger.json`. The default location is `$XDG_DATA_HOME/agent-merge-safety`, which is `~/.local/share/agent-merge-safety` on most systems.

## Install

You need `bash`, `jq`, `git`, `timeout` from coreutils, Python 3 (standard library only), and an authenticated [GitHub CLI](https://cli.github.com/).

```bash
git clone https://github.com/fisherjoey/agent-merge-safety
cd agent-merge-safety

mkdir -p ~/.claude/hooks ~/.claude/scripts ~/.claude/skills
cp hooks/risky-merge-guard.sh ~/.claude/hooks/
cp scripts/escape-ledger.py ~/.claude/scripts/
cp -r skill/agent-drain-gate ~/.claude/skills/
chmod +x ~/.claude/hooks/risky-merge-guard.sh ~/.claude/scripts/escape-ledger.py
```

Then register the hook in `~/.claude/settings.json`, or in a project's `.claude/settings.json` if you only want it for one repo. If you already have a `hooks.PreToolUse` array, add this entry to it:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": "bash \"$HOME/.claude/hooks/risky-merge-guard.sh\"",
            "timeout": 90
          }
        ]
      }
    ]
  }
}
```

The same snippet is in [`examples/settings.json`](examples/settings.json). The timeout is generous because the hook can make up to five `gh` calls, each with its own time limit.

Last, add a `.claude/risk-surfaces.txt` to any repo where the default doesn't fit.

### Configuration

| Variable | Used by | Default |
|---|---|---|
| `AGENT_MERGE_GUARD_LOG` | hook | `$XDG_STATE_HOME/agent-merge-safety/risky-merge-guard.log` (`~/.local/state/...`) |
| `AGENT_MERGE_GUARD_DEFAULT_RISK` | hook | the built-in regex in the script |
| `AGENT_MERGE_SAFETY_DATA` | ledger | `$XDG_DATA_HOME/agent-merge-safety` (`~/.local/share/...`) |

## Limits

- The hook is a backstop and doesn't review anything. It looks at file paths, not code. A dangerous change in a file called `utils.ts` gets through. You still need real review and tests.
- The override works on the honor system. `ATTENDED_MERGE=1` is just text in the command, and an agent could type it. The log makes that visible, but it doesn't stop it. If you want a person to confirm every override, add a permission rule such as `"ask": ["Bash(ATTENDED_MERGE=1 *)"]` to your settings, and check that it behaves the way you expect in your Claude Code version.
- It only sees commands that go through Claude Code's Bash tool. Merges from the GitHub web UI, the API, other tools, or scripts the agent writes and then runs aren't covered. Commands that hide the merge (inside `bash -c`, `eval`, or a variable) won't match either.
- It checks the repo in the session's working directory. For a merge aimed at another repo with `gh pr merge -R owner/repo`, it looks up the PR in the wrong repo, so its answer can be wrong in either direction.
- It fails open when GitHub is unreachable or the diff can't be read, as described above.
- The ledger only knows what you tell it. You confirm escapes by hand; the script counts them but can't find bugs on its own.

## Tests

```bash
bash tests/test_hook.sh                    # feeds the hook sample JSON, using a fake gh
python3 -m unittest discover -s tests -v   # runs the ledger against a temp git repo
```

Both suites use a stub `gh` in `tests/bin/`, so they need no network access and no GitHub login. CI runs them on every push.

## License

MIT. See [LICENSE](LICENSE).

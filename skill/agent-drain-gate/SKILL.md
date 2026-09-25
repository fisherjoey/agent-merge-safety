---
name: agent-drain-gate
description: The gate every agent pipeline should ship behind, in any repo. Covers risk-tiered merge, escape-rate accounting, treating "no change needed" as success, a per-run merge cap, cold sample audits, and per-stage model and effort. Use when building or reviewing ANY pipeline that merges agent-written code (backlog drains, overnight runs, autonomous fixers), when adding one to a new project, or when deciding whether an agent may merge something unattended.
---

# The gate for agent-shipped code

Throughput is not the constraint. Generation scaled; **review capacity and defect visibility
did not.** Every practice here exists to close one of those two gaps.

This skill pairs with two tools that should be installed alongside it:

- `~/.claude/hooks/risky-merge-guard.sh`, a PreToolUse(Bash) hook that blocks risky merges
  and `--no-verify`
- `~/.claude/scripts/escape-ledger.py`, which tracks defects per merged PR

Adjust the paths below if you installed them somewhere else.

## Scope: this is tiered, not universal

Applying the full gate everywhere is the fastest way to destroy it. Two of the five gates are
statistically inert below about 20 merges: on a small repo every PR touches the same handful
of files, so the re-touch signal saturates near 100% and reports nothing. Worse, a gate that
fires as noise trains the override. Type `ATTENDED_MERGE=1` twenty times a week on a marketing
site and you will type it without thinking on the repo where it mattered. That is the same
habituation the gate exists to catch, pointed at the operator.

| Tier | What applies | Where |
|---|---|---|
| **1: universal** | the PreToolUse hook (`--no-verify` block, merge guard on default surfaces) | every repo; installed once, globally |
| **2: agents write consequential code here** | + `.claude/risk-surfaces.txt` + the four prompt fragments below | repos where agents open PRs that matter, but not at volume |
| **3: unattended volume pipeline** | + escape ledger, merge caps, batched runs, cold audits | repos where agents merge unattended |

**Promotion rule**, so it stops being a judgment call: a repo moves to tier 3 when agents merge
into it unattended, or when it crosses roughly **20 agent-authored merges in a month**. Below
that, the ceremony costs more than the signal it can produce.

Tier 2 is not a copy of tier 3's surfaces. Write each repo's file from what is actually in it.
A static marketing site's risk is lead capture. A service on your own host has its self-update
path and its service unit. A tool that acts on *other* repos under your tokens has the highest
blast radius of all, despite being the smallest codebase. A documents-only repo gets nothing:
there is no code merge to guard.

## The five gates

### 1. Escape-rate accounting: build this first

Without it every other decision is a guess. Throughput is trivially visible; defects surface
weeks later, attributed to something else.

```bash
~/.claude/scripts/escape-ledger.py record TICKET PR ...   # after every merge, not batched
~/.claude/scripts/escape-ledger.py report                 # defects per merged PR, by wave
~/.claude/scripts/escape-ledger.py candidates             # PRs re-touching a recent surface
~/.claude/scripts/escape-ledger.py escape BUG PR "why"    # confirm a real one
```

The script is repo-agnostic: it detects the repo from the git remote and keeps one ledger per
repo. Wire `record` into whatever step closes a ticket, so a tired operator cannot skip it.

**Read it honestly.** The candidates *level* is noise, because active development re-touches
files exactly like rework does. The **trend** in confirmed defects per PR is the signal, and it
needs weeks and at least 20 merges before it means anything. Rising means slow down and widen
review. Flat under growing volume means the gate is holding.

### 2. Risk-tiered merge, not convenience-tiered

CI is strong evidence for a rendering bug and weak evidence for an authz, tenancy, migration,
auth, or money defect. Those fail **silently and green**. Tier by blast radius:

| Surface | Unattended agent may merge? |
|---|---|
| UI, copy, formatting, tests, docs, isolated pure functions | yes, on green |
| Business logic with real test coverage | yes, on green |
| authz, permissions, role checks | **no: implement and review, hold the branch** |
| org scoping, multi-tenancy, row isolation | **no: hold** |
| migrations, destructive or irreversible data operations | **no: hold** |
| auth, sessions, tokens, secrets | **no: hold** |
| invoicing, payroll, payments, billing | **no: hold** |

"Hold" is not "skip". The ticket is implemented and reviewed unattended, and a human ships the
branch in minutes. That keeps most of the value and none of the risk.

`risky-merge-guard.sh` enforces this mechanically. It denies `gh pr merge` when the PR diff
hits a risk surface and nothing else gates the merge (`--admin` was passed, or the base branch
has no required checks), and it denies `--no-verify` outright. Per-repo surfaces go in
`<repo>/.claude/risk-surfaces.txt`, one extended regex per line. The deliberate override is
`ATTENDED_MERGE=1 gh pr merge ...`. It is for a human, and it is logged.

Test-file matches are **not** false positives. Weakening a test so a bad change passes is the
"green CI hides bugs" failure itself.

### 3. "No change needed" must be a first-class success

Frontier models often propose an unnecessary change on tasks where the correct action is to
do nothing. A pipeline whose only exits are "fixed" and "blocked" pays that tax on every stale
ticket. Put this in every implement prompt:

> Before writing code, reproduce the defect against the current default branch. If it does not
> reproduce (already fixed, intended behaviour, or the code path is gone), STOP, change
> nothing, and return `noChangeNeeded: true` with the evidence: the command you ran, the test
> that already passes, the code you read. Do not manufacture an adjacent improvement to
> justify the ticket, and do not report this as a blocker. An empty diff with evidence is a
> completed ticket.

Make it a distinct terminal status that does **not** halt the lane, and close the ticket as
done with the evidence attached.

### 4. Cap merges per unattended run

Approval of agent code drifts upward with exposure, and the drift is invisible to the person
doing the approving. Bound how much one unattended run can merge before a human re-baselines.
12 is a reasonable default. Launch another run rather than raising the cap; the point is the
checkpoint, not the number.

### 5. Cold sample audit: 1 in 10

This is the only check on reviewer drift. Pick a merged PR at least a week old, review its diff
with **fresh context and without reading the original verdict first**, and file what you find.
If an automated review tool produced the verdict, read its output last.

```bash
~/.claude/scripts/escape-ledger.py audit-pick
```

Reading the original verdict first turns the audit into agreement with it, which measures
nothing.

## Models and effort per stage

Set both explicitly. An omitted model inherits the session's, which is usually the most
expensive one, and an omitted effort can default to `high` on stages that don't need it.

| Stage | Model | Effort | Why |
|---|---|---|---|
| implement: routine bug, test, refactor | `sonnet` | `high` | the mid tier handles most routine work and trails the top tier only on the hardest long-horizon tasks |
| implement: wide multi-file changes, engines and solvers, parent tickets | `opus` | `xhigh` | this is exactly where the mid tier falls behind |
| review | `opus` | `high` | review stays accurate at lower effort; spend the budget on implementation and the cold audit, not three `xhigh` rounds |
| review, when a dedicated review tool runs it | that tool's setting | its configured depth | a thin `sonnet` `low` runner only needs to launch it |
| rework | same as its implement stage | same | it is the same job |
| ship: push, CI watch, merge | `sonnet` | `medium` | mechanical plus waiting; `medium` still covers CI-failure diagnosis |
| bookkeeping, file loaders | `sonnet` or `haiku` | `low` | no judgement involved |
| cold sample audit | `opus` | `xhigh` | adversarial, and its whole job is catching what a `high`-effort reviewer missed |

Avoid the most expensive tier unless a stage is truly bounded by raw capability; almost
nothing in a merge pipeline is. **Prefer raising effort over changing tier.**

## Reviewer prompts: ask for coverage, not judgement

This applies to any LLM reviewer you prompt yourself. For a packaged review tool there may be
no prompt to tune; the equivalent control is its rule or include configuration, which decides
what gets looked at. Keep that broad, tests included.

Current models follow "only report high-severity issues" **literally**. They find the bug,
judge it below the stated bar, and say nothing. Measured recall falls while the underlying
ability to find the bug is unchanged. Always:

> Report every issue you find, including ones you are uncertain about. Do not filter for
> importance while looking. The majors/minors split IS the filter, and a finding you silently
> drop because it felt small is a finding nobody ever sees. Coverage first, ranking second.

Then rank downstream: majors block the merge, minors go to a counted ledger.

## When review is gated by a tool

If PR creation or merge is gated on an automated review (a hook, a required check):

- A review outage HOLDS unattended work. If the reviewer is down or its credentials expired,
  hold the ticket as `review unavailable`. Never pass it.
- Agents never set the gate's skip or override variable. That is for a human.
- A packaged reviewer's recall can be lower than a general agent's, and it may judge "does
  this fix the ticket" less well. The cold audit is the compensating control, and it reads the
  tool's result last.
- Review costs real tokens. Size runs and parallel lanes to match.

## Adding this to a new repo

1. Write `<repo>/.claude/risk-surfaces.txt`: the paths where CI is not sufficient evidence
   *for this codebase*. A marketing site's risky surface is payments and auth, not tenancy.
   No file means the hook's built-in default regex.
2. Wire `escape-ledger.py record` into whatever step closes a ticket.
3. Copy the four prompt fragments above (no-change, coverage-first, per-stage model and
   effort, risk tiering) into the pipeline's prompts.
4. The merge guard is global once registered in `~/.claude/settings.json`. Nothing to install
   per repo.

## Anti-patterns

- **Measuring throughput and calling it progress.** PRs merged per week is not a quality
  signal, and it moves first and most.
- **A "quick" attended-ship flag to finish the queue.** That is the whole gate, removed
  because it was inconvenient at 2am.
- **Batching `escape-ledger.py record` for "later".** Later does not come, and the surface
  data is what makes attribution possible at all.
- **Treating the candidates count as a defect count.** It is a shortlist to examine.
- **Reading the original review verdict before a cold audit.** That measures agreement, not
  drift.

#!/usr/bin/env python3
"""Escape-rate ledger for agent-shipped code. Repo-agnostic.

Answers the one question an agent pipeline cannot otherwise answer: is it adding
bugs? Throughput is trivially visible and defects surface weeks later attributed
to something else, so without this the pipeline runs open-loop on the metric that
decides whether to speed up or slow down.

Every agent-merged PR records its changed-file surface. A later PR touching the
same surface inside the attribution window is a *rework candidate* - we came back
to code we just shipped. Confirmed escapes are marked by hand. The report turns
that into defects-per-merged-PR per wave.

Repo is auto-detected from the git `origin` remote; ledgers are per-repo under
$AGENT_MERGE_SAFETY_DATA (default: $XDG_DATA_HOME/agent-merge-safety, which is
~/.local/share/agent-merge-safety when XDG_DATA_HOME is unset), at
<owner>__<repo>/escape-ledger.json. A "wave" is the UTC merge date, used as the
batch key in reports.

Requires: python3 (stdlib only), gh (authenticated) for `record`.

Subcommands:
  record TICKET PR [TICKET PR ...]   capture a merged PR's file surface
  candidates [--days N]              PRs re-touching a surface shipped in the last N days (30)
  escape BUG PR [note]               record a confirmed escape traced to a merged PR
  report [--days N]                  defects-per-merged-PR, per wave and overall
  audit-pick [--min-age-days N]      pick a merged PR for a cold sample audit (7)

Common flags: --repo owner/name   override auto-detection
"""
import json
import os
import random
import re
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

def data_dir():
    explicit = os.environ.get('AGENT_MERGE_SAFETY_DATA')
    if explicit:
        return os.path.expanduser(explicit)
    xdg = os.environ.get('XDG_DATA_HOME') or os.path.expanduser('~/.local/share')
    return os.path.join(xdg, 'agent-merge-safety')

DEFAULT_WINDOW_DAYS = 30
# Files nearly every PR touches carry no attribution signal - re-touching them
# says nothing about whether the earlier change was wrong.
NOISE_SUFFIXES = ('package-lock.json', 'pnpm-lock.yaml', 'yarn.lock', 'poetry.lock',
                  'README.md', 'CHANGELOG.md', '.env.example')


def detect_repo():
    if '--repo' in sys.argv:
        i = sys.argv.index('--repo')
        if i + 1 >= len(sys.argv):
            raise SystemExit('--repo needs a value: --repo owner/name')
        return sys.argv[i + 1]
    r = subprocess.run(['git', 'remote', 'get-url', 'origin'], capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit('not in a git repo with an origin remote - pass --repo owner/name')
    m = re.search(r'[:/]([^/:]+/[^/]+?)(?:\.git)?$', r.stdout.strip())
    if not m:
        raise SystemExit(f'could not parse a repo from origin: {r.stdout.strip()}')
    return m.group(1)


def ledger_path(repo):
    return os.path.join(data_dir(), repo.replace('/', '__'), 'escape-ledger.json')


def load(repo):
    p = ledger_path(repo)
    if not os.path.exists(p):
        return {'repo': repo, 'prs': [], 'escapes': [], '_generated': ''}
    with open(p) as f:
        return json.load(f)


def save(repo, d):
    p = ledger_path(repo)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    d['repo'] = repo
    d['_generated'] = datetime.now(timezone.utc).isoformat(timespec='seconds')
    with open(p, 'w') as f:
        json.dump(d, f, indent=2)
        f.write('\n')


def gh_pr(repo, pr):
    out = subprocess.run(
        ['gh', 'pr', 'view', str(pr), '--repo', repo, '--json', 'files,mergedAt,title,state'],
        capture_output=True, text=True)
    if out.returncode != 0:
        raise SystemExit(f'gh pr view {pr}: {out.stderr.strip()[:300]}')
    return json.loads(out.stdout)


def parse_ts(s):
    return datetime.fromisoformat(s.replace('Z', '+00:00'))


def signal_files(files):
    return [f for f in files if not f.endswith(NOISE_SUFFIXES)]


def flag(args, name, default):
    return int(args[args.index(name) + 1]) if name in args else default


def positional(args):
    """Strip flags and their values so subcommand args stay simple."""
    out, skip = [], False
    for i, a in enumerate(args):
        if skip:
            skip = False
            continue
        if a in ('--days', '--min-age-days', '--repo'):
            skip = True
            continue
        out.append(a)
    return out


def cmd_record(repo, args):
    d = load(repo)
    seen = {(e['ticket'], e['pr']) for e in d['prs']}
    pos = positional(args)
    if len(pos) < 2 or len(pos) % 2:
        raise SystemExit('usage: escape-ledger.py record TICKET PR [TICKET PR ...]')
    for ticket, pr in zip(pos[::2], pos[1::2]):
        pr = int(pr)
        if (ticket, pr) in seen:
            print(f'{ticket} #{pr} already recorded')
            continue
        info = gh_pr(repo, pr)
        files = signal_files([f['path'] for f in info.get('files') or []])
        merged = info.get('mergedAt') or datetime.now(timezone.utc).isoformat(timespec='seconds')
        d['prs'].append({'ticket': ticket, 'pr': pr, 'mergedAt': merged, 'wave': merged[:10],
                         'title': info.get('title', ''), 'files': files})
        print(f'{ticket} #{pr}: {len(files)} files recorded')
    d['prs'].sort(key=lambda e: e['mergedAt'])
    save(repo, d)


def cmd_candidates(repo, args):
    window = timedelta(days=flag(args, '--days', DEFAULT_WINDOW_DAYS))
    prs = load(repo)['prs']
    hits = 0
    for i, later in enumerate(prs):
        lt = parse_ts(later['mergedAt'])
        overlaps = []
        for earlier in prs[:i]:
            if lt - parse_ts(earlier['mergedAt']) > window:
                continue
            shared = set(earlier['files']) & set(later['files'])
            if shared:
                overlaps.append((earlier, sorted(shared)))
        if overlaps:
            hits += 1
            print(f"\n{later['ticket']} #{later['pr']} ({later['wave']}) re-touches:")
            for earlier, shared in overlaps:
                print(f"    {earlier['ticket']} #{earlier['pr']} ({earlier['wave']}) - "
                      f"{', '.join(shared[:4])}{' …' if len(shared) > 4 else ''}")
    print(f'\n{hits}/{len(prs)} merged PRs re-touch a surface shipped within {window.days}d')
    print('Re-touch is a candidate, not a verdict. Active development re-touches like rework '
          'does - the LEVEL is noise, the TREND and confirmed escapes are the signal.')
    print('Confirm a real one with: escape-ledger.py escape BUG PR "why"')


def cmd_escape(repo, args):
    d = load(repo)
    pos = positional(args)
    if len(pos) < 2:
        raise SystemExit('usage: escape-ledger.py escape BUG PR [note]')
    bug, pr = pos[0], int(pos[1])
    note = pos[2] if len(pos) > 2 else ''
    d['escapes'].append({'bug': bug, 'causedBy': pr, 'note': note,
                         'notedAt': datetime.now(timezone.utc).isoformat(timespec='seconds')})
    save(repo, d)
    print(f'escape recorded: {bug} <- #{pr}')


def cmd_report(repo, args):
    window = flag(args, '--days', 0)
    d = load(repo)
    prs, escapes = d['prs'], d['escapes']
    if window:
        cutoff = datetime.now(timezone.utc) - timedelta(days=window)
        prs = [p for p in prs if parse_ts(p['mergedAt']) >= cutoff]
    by_pr = {p['pr']: p for p in prs}
    per_wave = defaultdict(lambda: {'merged': 0, 'escapes': 0})
    for p in prs:
        per_wave[p['wave']]['merged'] += 1
    for e in escapes:
        src = by_pr.get(e['causedBy'])
        if src:
            per_wave[src['wave']]['escapes'] += 1
    print(f'repo: {repo}')
    print(f"{'wave':<12} {'merged':>7} {'escapes':>8} {'per-PR':>8}")
    for wave in sorted(per_wave):
        s = per_wave[wave]
        print(f"{wave:<12} {s['merged']:>7} {s['escapes']:>8} "
              f"{(s['escapes'] / s['merged'] if s['merged'] else 0):>8.2f}")
    tot_m = sum(s['merged'] for s in per_wave.values())
    tot_e = sum(s['escapes'] for s in per_wave.values())
    print(f"{'TOTAL':<12} {tot_m:>7} {tot_e:>8} {(tot_e / tot_m if tot_m else 0):>8.2f}")
    if tot_m < 20:
        print('\n(fewer than 20 merged PRs recorded - the rate is not yet meaningful)')


def cmd_audit_pick(repo, args):
    min_age = timedelta(days=flag(args, '--min-age-days', 7))
    now = datetime.now(timezone.utc)
    d = load(repo)
    audited = {e.get('causedBy') for e in d['escapes']}
    pool = [p for p in d['prs']
            if now - parse_ts(p['mergedAt']) >= min_age and p['pr'] not in audited]
    if not pool:
        raise SystemExit(f'no merged PR older than {min_age.days}d awaiting a cold audit')
    p = random.choice(pool)
    print(f"cold audit target: {p['ticket']} #{p['pr']} ({p['wave']}) - {p['title'][:70]}")
    print(f"  gh pr diff {p['pr']} --repo {repo}")
    print('  Review with fresh context. Do NOT read the original review verdict first —')
    print('  the point is to measure drift in the reviewer, not to agree with it.')


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    cmd, args = sys.argv[1], sys.argv[2:]
    fn = {'record': cmd_record, 'candidates': cmd_candidates, 'escape': cmd_escape,
          'report': cmd_report, 'audit-pick': cmd_audit_pick}.get(cmd)
    if not fn:
        raise SystemExit(__doc__)
    fn(detect_repo(), args)


if __name__ == '__main__':
    main()

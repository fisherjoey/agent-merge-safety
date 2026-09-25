"""Runs escape-ledger.py end to end against a temp git repo and a fake `gh`."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
LEDGER = os.path.join(HERE, '..', 'scripts', 'escape-ledger.py')
REPO = 'example-org/example-repo'


def iso(days_ago):
    t = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return t.replace(microsecond=0).isoformat().replace('+00:00', 'Z')


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = os.path.join(self.tmp.name, 'data')
        self.repo = os.path.join(self.tmp.name, 'repo')
        subprocess.run(['git', 'init', '-q', self.repo], check=True)
        subprocess.run(['git', '-C', self.repo, 'remote', 'add', 'origin',
                        f'git@github.com:{REPO}.git'], check=True)
        self.env = dict(os.environ,
                        PATH=os.path.join(HERE, 'bin') + os.pathsep + os.environ['PATH'],
                        AGENT_MERGE_SAFETY_DATA=self.data)

    def tearDown(self):
        self.tmp.cleanup()

    def run_ledger(self, *args, view=None, check=True):
        env = dict(self.env)
        if view is not None:
            env['FAKE_GH_VIEW_JSON'] = json.dumps(view)
        r = subprocess.run([sys.executable, LEDGER, *args], cwd=self.repo, env=env,
                           capture_output=True, text=True)
        if check and r.returncode != 0:
            self.fail(f'ledger {args} failed: {r.stdout}{r.stderr}')
        return r

    def record(self, ticket, pr, files, days_ago, title='change'):
        return self.run_ledger('record', ticket, str(pr), view={
            'files': [{'path': f} for f in files], 'mergedAt': iso(days_ago),
            'title': title, 'state': 'MERGED'})

    def ledger_json(self):
        path = os.path.join(self.data, REPO.replace('/', '__'), 'escape-ledger.json')
        with open(path) as f:
            return json.load(f)

    def test_record_detects_repo_and_drops_noise_files(self):
        out = self.record('T-1', 11, ['src/a.py', 'package-lock.json', 'README.md'], 10)
        self.assertIn('T-1 #11: 1 files recorded', out.stdout)
        d = self.ledger_json()
        self.assertEqual(d['repo'], REPO)
        self.assertEqual(d['prs'][0]['files'], ['src/a.py'])

    def test_record_is_idempotent(self):
        self.record('T-1', 11, ['src/a.py'], 10)
        out = self.record('T-1', 11, ['src/a.py'], 10)
        self.assertIn('already recorded', out.stdout)
        self.assertEqual(len(self.ledger_json()['prs']), 1)

    def test_candidates_flags_retouch_inside_window(self):
        self.record('T-1', 11, ['src/a.py'], 20)
        self.record('T-2', 12, ['src/b.py'], 15)
        self.record('T-3', 13, ['src/a.py', 'src/c.py'], 5)
        out = self.run_ledger('candidates').stdout
        self.assertIn('T-3 #13', out)
        self.assertIn('T-1 #11', out)
        self.assertIn('1/3 merged PRs re-touch', out)
        # With a 10-day window the 15-day gap no longer counts.
        out = self.run_ledger('candidates', '--days', '10').stdout
        self.assertIn('0/3 merged PRs re-touch', out)

    def test_escape_and_report(self):
        self.record('T-1', 11, ['src/a.py'], 3)
        self.record('T-2', 12, ['src/b.py'], 3)
        self.run_ledger('escape', 'BUG-9', '11', 'null check removed')
        out = self.run_ledger('report').stdout
        self.assertIn(f'repo: {REPO}', out)
        total = [l for l in out.splitlines() if l.startswith('TOTAL')][0].split()
        self.assertEqual(total[1:], ['2', '1', '0.50'])
        self.assertIn('fewer than 20 merged PRs', out)

    def test_audit_pick_respects_min_age(self):
        self.record('T-1', 11, ['src/a.py'], 2)
        r = self.run_ledger('audit-pick', check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('no merged PR older than 7d', r.stderr)
        self.record('T-2', 12, ['src/b.py'], 30)
        out = self.run_ledger('audit-pick').stdout
        self.assertIn('T-2 #12', out)
        self.assertIn(f'gh pr diff 12 --repo {REPO}', out)

    def test_repo_flag_overrides_detection(self):
        out = self.run_ledger('report', '--repo', 'other/thing').stdout
        self.assertIn('repo: other/thing', out)

    def test_usage_errors(self):
        r = self.run_ledger('escape', 'BUG-1', check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('usage', r.stderr)
        r = self.run_ledger('nonsense', check=False)
        self.assertNotEqual(r.returncode, 0)


if __name__ == '__main__':
    unittest.main()

"""Shared fixture for the launch/nodes/jobs tests: a throw-away config, state dir, git repo, fake
node home (tests/fakes/ssh and scp execute under it) and a fake `claude` launcher."""
import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

from delega import config as _config  # noqa: E402

FAKES = os.path.join(HERE, "fakes")

FAKE_CLAUDE = """#!{python}
import json, os, sys
a = sys.argv[1:]
home_is_node = os.environ.get("HOME") == os.environ.get("FAKE_NODE_HOME")
if a[:2] == ["auth", "status"]:
    print(json.dumps({{"email": os.environ.get("FAKE_EMAIL", "dev@example.com")}}))
elif a[:1] == ["--version"]:
    v = os.environ.get("FAKE_VERSION_REMOTE") if home_is_node else None
    print((v or os.environ.get("FAKE_VERSION", "2.1.5")) + " (Claude Code)")
"""

BRIEF = """# Task

Do the thing.

## Zone

- `src/app/*`

## Done when

- tests pass

## Cap

- at most 30 minutes

## Report

Finish with a report of at most 15 lines.
"""


def sh(*argv, cwd=None):
    return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


class LaunchCase(unittest.TestCase):
    """Builds the sandbox in setUp. Override `extra_config` for more TOML."""
    extra_config = ""
    node_extra = ""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        t = self.root = os.path.realpath(self.tmp.name)
        self.state = os.path.join(t, "state")
        self.node_home = os.path.join(t, "nodehome")
        os.makedirs(self.state)
        os.makedirs(self.node_home)
        self.claude = os.path.join(t, "claude")
        with open(self.claude, "w") as f:
            f.write(FAKE_CLAUDE.format(python=sys.executable))
        os.chmod(self.claude, 0o755)
        self.terms = os.path.join(t, "forbidden.txt")
        with open(self.terms, "w") as f:
            f.write("# client names\nacmecorp\n")
        self.repo = os.path.join(t, "api")
        os.makedirs(self.repo)
        sh("git", "init", "-q", "-b", "main", self.repo)
        sh("git", "-C", self.repo, "config", "user.email", "a@example.com")
        sh("git", "-C", self.repo, "config", "user.name", "A")
        os.makedirs(os.path.join(self.repo, "src", "app"))
        with open(os.path.join(self.repo, "src", "app", "main.py"), "w") as f:
            f.write("print('hi')\n")
        sh("git", "-C", self.repo, "add", ".")
        sh("git", "-C", self.repo, "commit", "-qm", "init")
        self.brief = os.path.join(t, "brief.md")
        with open(self.brief, "w") as f:
            f.write(BRIEF)
        self.cfg_path = os.path.join(t, "config.toml")
        self.write_config()
        env = {
            "DELEGA_CONFIG": self.cfg_path, "DELEGA_STATE_DIR": self.state, "FAKE_NODE_HOME": self.node_home,
            "DELEGA_SSH": os.path.join(FAKES, "ssh"), "DELEGA_SCP": os.path.join(FAKES, "scp"),
            "DELEGA_BROKER_CMD": f"{sys.executable} {os.path.join(FAKES, 'stub_broker.py')}",
            "DELEGA_LOCALE": "en", "DELEGA_WAIT_POLL_S": "0.2", "DELEGA_WATCH_POLL_S": "0.1",
            "DELEGA_OWNER": "tester", "DELEGA_NODE_MIN_DISK_GB": "0", "PYTHONPATH": os.path.join(ROOT, "src"),
        }
        p = mock.patch.dict(os.environ, env)
        p.start()
        self.addCleanup(p.stop)
        for k in ("DELEGA_PROFILE", "FAKE_EMAIL", "FAKE_VERSION", "FAKE_VERSION_REMOTE", "STUB_SLEEP",
                  "STUB_COMMIT_TEXT", "STUB_RC", "DELEGA_JOB_MAX_MIN"):
            os.environ.pop(k, None)
        self.events = []
        self.patch_collaborators()

    def write_config(self):
        t = self.root
        with open(self.cfg_path, "w") as f:
            f.write(f"""
[general]
locale = "en"
default_profile = "work"

[policy]
resume_max_min = 60
max_local_jobs = 2

[profiles.work]
owner = "self"
launcher = ["{self.claude}"]
config_dir = "{t}/.claude-work"
identity.expect_email_domain = "example.com"
git = {{ user_name = "A", user_email = "a@example.com" }}
forbidden_terms_file = "{self.terms}"
nodes = ["box", "box2"]
compute = ["tool"]

[compute.tool]
kind = "command"
check = ["true"]
fix_hint = "run the-tool login"

[compute.broken]
kind = "command"
check = ["false"]
fix_hint = "run the-broken fix"

[nodes.box]
ssh = "box"
max_jobs = 2
python = "{sys.executable}"
launcher = ["{self.claude}"]
config_dirs = {{ work = "{t}/.claude-work-node" }}
{self.node_extra}

[nodes.box2]
ssh = "down2"
max_jobs = 1
python = "{sys.executable}"
launcher = ["{self.claude}"]

[projects."example-org/api"]
branch_prefixes = ["feature/", "delega/"]
{self.extra_config}
""")

    def patch_collaborators(self):
        """Stand-ins for spend/scan/audit (other modules) so these tests do not depend on them."""
        from delega import audit, scan, spend
        self.spent = 0.0

        def load_terms(path):
            return [l.strip() for l in open(path).read().splitlines() if l.strip() and not l.startswith("#")]

        def scan_text(text, terms):
            return [x for x in terms if x.lower() in text.lower()]

        def scan_branch(repo_dir, branch, base, terms):
            files, lines = set(), 0
            for term in terms:
                r = subprocess.run(["git", "-C", repo_dir, "grep", "-l", "-i", "-F", term, branch],
                                   capture_output=True, text=True)
                files |= {x.split(":", 1)[1] for x in r.stdout.split() if ":" in x}
            return {"files": sorted(files), "commit_lines": lines}

        for mod, name, fn in ((spend, "total", lambda cfg, day, profile=None, project=None: self.spent),
                              (scan, "load_terms", load_terms), (scan, "scan_text", scan_text),
                              (scan, "scan_branch", scan_branch),
                              (audit, "record", lambda cfg, event, **f: self.events.append((event, f)))):
            p = mock.patch.object(mod, name, fn, create=True)
            p.start()
            self.addCleanup(p.stop)

    def cfg(self):
        return _config.load(self.cfg_path)

    def run_cli(self, argv):
        """delega run in-process. Returns (rc, stdout, stderr)."""
        from delega import launch
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = launch.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def jobs_in_state(self):
        return sorted(d for d in os.listdir(self.state) if d[:1] == "2")

    def job_dir(self, i=-1):
        return os.path.join(self.state, self.jobs_in_state()[i])

    def new_job(self, **meta):
        """A fake job directory in the state dir (for liveness/zone tests)."""
        from delega import util
        name = f"{meta.pop('id', '20260101-000000-1')}-{meta.pop('repo', 'api')}"
        d = os.path.join(self.state, name)
        os.makedirs(d, exist_ok=True)
        util.add_meta(d, **meta)
        return d

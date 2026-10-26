"""Shared fixtures for the test_ops_* tests: temp state dir, config file, git repos, fake jobs."""
import contextlib
import io
import json
import os
import subprocess
import tempfile
import time
import unittest

import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))   # run without PYTHONPATH

from delega import config  # noqa: E402

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}


class OpsCase(unittest.TestCase):
    TOML = ""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.state = os.path.join(self.root, "state")
        os.makedirs(self.state)
        self.conf = os.path.join(self.root, "config.toml")
        self.write_config(self.TOML)
        self._env = dict(os.environ)
        for k in [k for k in os.environ if k.startswith(("DELEGA_", "ORQ_"))]:
            del os.environ[k]
        os.environ.update(DELEGA_STATE_DIR=self.state, DELEGA_CONFIG=self.conf, DELEGA_LOCALE="en", **GIT_ENV)
        self.addCleanup(self._restore)

    def _restore(self):
        os.environ.clear()
        os.environ.update(self._env)

    def write_config(self, text):
        with open(self.conf, "w") as f:
            f.write(text)

    def cfg(self):
        return config.load()

    def run_cli(self, fn, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = fn(list(args)) or 0
        return rc, out.getvalue(), err.getvalue()

    def make_job(self, name, meta=None, files=None):
        d = os.path.join(self.state, name)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "meta"), "w") as f:
            for k, v in (meta or {}).items():
                f.write(f"{k}={v}\n")
        for fname, content in (files or {}).items():
            with open(os.path.join(d, fname), "w") as f:
                f.write(content if isinstance(content, str) else json.dumps(content))
        return d

    def git(self, repo, *args):
        r = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        return r.stdout.strip()

    def make_repo(self, name="repo", files=None, origin=None):
        repo = os.path.join(self.root, name)
        os.makedirs(repo)
        subprocess.run(["git", "init", "-q", "-b", "main", repo], check=True)
        for fn, content in (files or {"a.txt": "one\n"}).items():
            with open(os.path.join(repo, fn), "w") as f:
                f.write(content)
        self.git(repo, "add", "-A")
        self.git(repo, "commit", "-qm", "init")
        if origin:
            self.git(repo, "remote", "add", "origin", origin)
        return repo

    def commit_on(self, repo, branch, fn, content, msg="change", base="main"):
        self.git(repo, "checkout", "-q", "-B", branch, base)
        with open(os.path.join(repo, fn), "w") as f:
            f.write(content)
        self.git(repo, "add", "-A")
        self.git(repo, "commit", "-qm", msg)
        self.git(repo, "checkout", "-q", "main")


def today():
    return time.strftime("%Y%m%d")


def rate_limit_line(now, u5=0.2, u7=0.1, r5=7200, r7=200000):
    return json.dumps({"type": "rate_limit_event", "rate_limit_info": {"unifiedWindows": {
        "five_hour": {"utilization": u5, "resetsAt": now + r5},
        "seven_day": {"utilization": u7, "resetsAt": now + r7}}}}) + "\n"

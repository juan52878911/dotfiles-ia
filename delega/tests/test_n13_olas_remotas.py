"""N13: any lease owner drives waves and deps (remote base over a fake ssh); branch_prefix of the profile."""
import json
import os
import sqlite3
import stat
import subprocess
import sys
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "..", "src")
sys.path.insert(0, SRC)
sys.path.insert(0, HERE)

import test_tick_daemon as T  # noqa: E402
from delega import config, queue, wave  # noqa: E402

FAKE_SSH = """#!/bin/sh
# fake ssh: ssh -o BatchMode=yes <host> <cmd>; runs <cmd> locally (the "host" is another local base)
while [ "$1" = "-o" ]; do shift 2; done
shift
exec sh -c "$1"
"""
SHIM = """#!/bin/sh
PYTHONPATH="%s" exec python3 -m delega "$@"
"""
TOML_REMOTE = """
[general]
default_profile = "trycore"
[profiles.trycore]
owner = "self"
{extra}
[memoria.trycore]
host = "dbhost.example"
path = "{db}"
repos = {{ "acme/api" = "{repo}" }}
"""


class N13(T.DaemonCase):
    cycle = T.Hardening.cycle
    fake_job = T.Hardening.fake_job

    def setUp(self):
        super().setUp()
        for p in (mock.patch("delega.mem.mounts", lambda: []),
                  mock.patch.dict(os.environ, {"PATH": os.pathsep.join([os.path.dirname(sys.executable),
                                                                      "/usr/bin", "/bin"])})):
            p.start()
            self.addCleanup(p.stop)
        self.runs, self.integrations, self.order = [], [], []

    def conf_remote(self, extra=""):
        r = self.tmp.name
        bindir = os.path.join(r, "bin")
        os.makedirs(bindir, exist_ok=True)
        for name, body in (("ssh", FAKE_SSH), ("delega", SHIM % SRC)):
            p = os.path.join(bindir, name)
            with open(p, "w") as f:
                f.write(body)
            os.chmod(p, os.stat(p).st_mode | stat.S_IXUSR)
        os.environ["PATH"] = bindir + os.pathsep + os.environ["PATH"]
        home = os.path.join(r, "home")
        os.makedirs(home, exist_ok=True)
        os.environ["HOME"] = home
        os.environ["PYTHONPATH"] = SRC
        with open(self.conf, "w") as f:
            f.write(TOML_REMOTE.format(db=self.db, repo=self.repo, extra=extra))
        self.cfg = config.load()
        self.assertFalse(self.cfg.memoria_for("trycore")["local"])

    def drive(self, n=1, owner="daemon-b"):
        class P:
            pid = 999999
        real_popen = subprocess.Popen

        def fake_popen(cmd, **kw):
            if "--branch" not in cmd:
                return real_popen(cmd, **kw)
            branch = cmd[cmd.index("--branch") + 1]
            self.runs.append(branch)
            self.order.append(("run", branch))
            self.fake_job(time.strftime("%Y%m%d-%H%M%S") + f"-w{len(self.runs)}", branch, rc=0, sha=self.sha)
            return P()

        def fake_integrate(env, branch, into, node, sha=None):
            self.integrations.append((env.repo_dir, branch, into, sha))
            self.order.append(("integrate", branch))
            return 0, "ok"
        with mock.patch.object(wave.subprocess, "Popen", fake_popen), \
                mock.patch.object(wave.Env, "integrate", fake_integrate):
            for _ in range(n):
                self.cycle(client=self.client(owner))

    def events(self, que):
        c = sqlite3.connect(self.db)
        try:
            return c.execute("SELECT quien, datos FROM eventos WHERE que = ?", (que,)).fetchall()
        finally:
            c.close()

    def test_a_daemon_without_the_database_drives_a_whole_wave(self):
        self.conf_remote()
        self.add_task("t1")
        self.add_task("t2")
        self.add_task("t3", wave="2")
        self.drive(1)
        self.assertEqual(self.states(), {"t1": "lanzada", "t2": "lanzada", "t3": "pendiente"})
        self.drive(1)                                   # reconcile ok + integrate the whole wave
        self.assertEqual(self.states(), {"t1": "hecha", "t2": "hecha", "t3": "pendiente"})
        self.assertEqual(sorted(i[1] for i in self.integrations), ["delega/wave-f1-t1", "delega/wave-f1-t2"])
        self.assertEqual({i[0] for i in self.integrations}, {os.path.abspath(self.repo)})
        self.drive(3)
        self.assertEqual(self.states(), {"t1": "hecha", "t2": "hecha", "t3": "hecha"})
        # every write went with the owner of the daemon
        shas = self.events("wave.sha")
        self.assertEqual(len(shas), 3)
        self.assertEqual({q for q, _ in shas}, {"daemon-b"})
        self.assertEqual(self.lease_owner(), "daemon-b")

    def test_a_stale_owner_cannot_drive_waves_remotely(self):
        self.conf_remote()
        self.add_task("t1")
        self.add_task("t2", wave="2")
        self.drive(1, owner="daemon-b")
        self.drive(1, owner="daemon-c")                 # the lease is b's: c does nothing
        self.assertEqual(self.runs, ["delega/wave-f1-t1"])

    def test_flow_with_deps_and_one_wave_starts_the_second_after_integrating_the_first(self):
        self.add_task("t1")
        self.add_task("t2", deps="t1")
        self.drive(1)
        self.assertEqual(self.states(), {"t1": "lanzada", "t2": "pendiente"})
        self.drive(1)                                   # t1 ok -> integrated -> hecha
        self.assertEqual(self.states()["t1"], "hecha")
        self.assertEqual(self.order[:2], [("run", "delega/wave-f1-t1"), ("integrate", "delega/wave-f1-t1")])
        self.drive(1)                                   # now t2 is ready
        self.assertEqual(self.order[2], ("run", "delega/wave-f1-t2"))
        self.drive(2)
        self.assertEqual(self.states(), {"t1": "hecha", "t2": "hecha"})

    def test_flow_with_deps_and_one_wave_works_over_a_remote_base_too(self):
        self.conf_remote()
        self.add_task("t1")
        self.add_task("t2", deps="t1")
        self.drive(3)
        self.assertEqual([o[0] for o in self.order[:3]], ["run", "integrate", "run"])
        self.drive(2)
        self.assertEqual(self.states(), {"t1": "hecha", "t2": "hecha"})

    def test_branch_prefix_of_the_profile_in_the_daemon_and_in_wave(self):
        with open(self.conf, "w") as f:
            f.write(T.TOML.format(db=self.db, repo=self.repo).replace(
                '[profiles.trycore]\nowner = "self"', '[profiles.trycore]\nowner = "self"\nbranch_prefix = "chore/"'))
        self.cfg = config.load()
        self.assertEqual(self.cfg.profile("trycore")["branch_prefix"], "chore/")
        self.assertEqual(self.cfg.profile("personal")["branch_prefix"], "delega/")
        self.add_task("t1")
        self.add_task("t2", **{"run-args": json.dumps({"branch": "fix/explicit"})})
        got = {}
        self.cycle(launch=lambda cfg, it: got.__setitem__(it["id"], it["rama"]) or ("launched", "1"))
        self.assertEqual(got, {"t1": "chore/q-f1-t1", "t2": "fix/explicit"})

    def test_branch_prefix_of_the_profile_in_wave(self):
        with open(self.conf, "w") as f:
            f.write(T.TOML.format(db=self.db, repo=self.repo).replace(
                '[profiles.trycore]\nowner = "self"', '[profiles.trycore]\nowner = "self"\nbranch_prefix = "chore/"'))
        self.cfg = config.load()
        self.add_task("t1")
        self.add_task("t2", wave="2")
        self.add_task("t3", wave="2", **{"run-args": json.dumps({"branch": "fix/explicit"})})
        self.drive(4)
        self.assertEqual(self.runs, ["chore/wave-f1-t1", "chore/wave-f1-t2", "fix/explicit"])

    def test_bad_branch_prefix_is_refused(self):
        with self.assertRaises(config.ConfigError):
            config.Config({"profiles": {"p": {"owner": "self", "branch_prefix": "chore"}}})


if __name__ == "__main__":
    unittest.main()

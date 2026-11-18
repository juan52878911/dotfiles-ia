"""tick --daemon: only the owner of the org's lease acts; the other daemon waits and takes over."""
import contextlib
import io
import json
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "..", "src")
sys.path.insert(0, SRC)   # run without PYTHONPATH

from delega import config, mem, queue  # noqa: E402

TOML = """
[general]
default_profile = "trycore"
[profiles.trycore]
owner = "self"
[profiles.personal]
owner = "self"
[memoria.trycore]
path = "{db}"
repos = {{ "acme/api" = "{repo}" }}
"""
REMOTE = "git@example.com:acme/api"


def mem_cli(*args):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = mem.main(list(args)) or 0
    return rc, out.getvalue(), err.getvalue()


class DaemonCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        r = self.tmp.name
        self.db, self.repo, self.state = (os.path.join(r, x) for x in ("m.db", "repo", "state"))
        os.makedirs(self.repo)
        os.makedirs(self.state)
        self.conf = os.path.join(r, "config.toml")
        with open(self.conf, "w") as f:
            f.write(TOML.format(db=self.db, repo=self.repo))
        self._env = dict(os.environ)
        for k in [k for k in os.environ if k.startswith(("DELEGA_", "ORQ_"))]:
            del os.environ[k]
        os.environ.update(DELEGA_STATE_DIR=self.state, DELEGA_CONFIG=self.conf, DELEGA_LOCALE="en")
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(self._env)))
        self.assertEqual(mem_cli("init", "--org", "trycore", "--db", self.db)[0], 0)
        self.assertEqual(mem_cli("add-flow", "--repo", REMOTE, "--id", "f1", "--db", self.db)[0], 0)
        self.cfg = config.load()

    def add_task(self, tid, **kw):
        return mem_cli("add-task", "--flow", "f1", "--brief", "do " + tid, "--id", tid, "--db", self.db,
                       *sum(([f"--{k}", v] for k, v in kw.items()), []))

    def client(self, owner):
        return queue.MemClient(self.cfg, "trycore", owner)

    def states(self):
        c = sqlite3.connect(self.db)
        try:
            return dict(c.execute("SELECT id, estado FROM tareas").fetchall())
        finally:
            c.close()

    def lease_owner(self):
        c = sqlite3.connect(self.db)
        try:
            row = c.execute("SELECT dueno FROM leases WHERE rol = 'orquestador:trycore'").fetchone()
            return row and row[0]
        finally:
            c.close()


class LaunchOnlyOnce(DaemonCase):
    def cycle(self, owner, launched):
        def fake_launch(cfg, item):
            launched.append((owner, item["id"]))
            item["log"] = "x.log"
            return "launched", "1"
        cap = {"has_data": False, "new": 9, "live": 0, "slots": 9}
        with mock.patch.object(queue, "launch", fake_launch), \
                mock.patch("delega.quota.capacity", lambda cfg, name, **k: cap), \
                mock.patch.object(queue, "_reconcile", lambda cfg, adopt: None), \
                contextlib.redirect_stdout(io.StringIO()):
            return queue.daemon_cycle(self.cfg, self.client(owner), ttl=30)

    def test_two_daemons_one_launches(self):
        self.add_task("t1")
        self.add_task("t2")
        launched = []
        self.assertTrue(self.cycle("a", launched))
        self.assertFalse(self.cycle("b", launched))
        self.assertEqual(sorted(launched), [("a", "t1"), ("a", "t2")])
        self.assertEqual(self.states(), {"t1": "lanzada", "t2": "lanzada"})
        self.assertFalse(self.cycle("a", launched) and launched[2:])    # nothing left to launch
        self.assertEqual(len(launched), 2)

    def test_concurrent_daemons_never_double_launch(self):
        for n in range(5):
            self.add_task(f"t{n}")
        launched = []

        def fake_launch(cfg, item):                  # patched once: mock.patch is not thread-safe
            launched.append((threading.current_thread().name, item["id"]))
            item["log"] = "x.log"
            return "launched", "1"
        cap = {"has_data": False, "new": 9, "live": 0, "slots": 9}
        results = {}

        def run(owner):
            results[owner] = queue.daemon_cycle(self.cfg, self.client(owner), ttl=30)
        with mock.patch.object(queue, "launch", fake_launch), \
                mock.patch("delega.quota.capacity", lambda cfg, name, **k: cap), \
                mock.patch.object(queue, "_reconcile", lambda cfg, adopt: None), \
                contextlib.redirect_stdout(io.StringIO()):
            ts = [threading.Thread(target=run, args=(o,), name=o) for o in "abcd"]
            [t.start() for t in ts]
            [t.join() for t in ts]
        self.assertEqual(sorted(i for _, i in launched), [f"t{n}" for n in range(5)])
        self.assertEqual(len({o for o, _ in launched}), 1)
        self.assertEqual(sum(results.values()), 1)

    def test_reads_tasks_from_the_database_and_migrates_queue_jsonl(self):
        subprocess.run(["git", "init", "-q", self.repo], check=True)
        subprocess.run(["git", "-C", self.repo, "remote", "add", "origin", REMOTE], check=True)
        brief = os.path.join(self.tmp.name, "b.md")
        with open(brief, "w") as f:
            f.write("legacy brief")
        item = queue.add(self.cfg, self.repo, brief)
        before = open(queue.qpath(self.cfg)).read()
        launched = []
        self.assertTrue(self.cycle("a", launched))
        self.assertEqual(launched, [("a", "q-" + item["id"])])
        self.assertEqual(open(queue.qpath(self.cfg)).read(), before)      # queue.jsonl is read-only
        self.assertTrue(self.cycle("a", launched))
        self.assertEqual(len(launched), 1)                                # migrated once


class Takeover(DaemonCase):
    def test_other_daemon_takes_the_lease_within_two_intervals_of_expiry(self):
        ttl, interval = 1.5, 0.3
        env = dict(os.environ, PYTHONPATH=SRC)
        cmd = [sys.executable, "-m", "delega", "tick", "--daemon", "--interval", str(interval),
               "--lease-ttl", str(ttl), "--org", "trycore"]
        a = subprocess.Popen(cmd + ["--owner", "a"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.addCleanup(lambda: a.poll() is None and a.kill())
        deadline = time.time() + 20
        while self.lease_owner() != "a" and time.time() < deadline:
            time.sleep(0.05)
        self.assertEqual(self.lease_owner(), "a")
        b = subprocess.Popen(cmd + ["--owner", "b"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.addCleanup(lambda: b.poll() is None and b.kill())
        time.sleep(interval * 3)
        self.assertEqual(self.lease_owner(), "a")                         # b waits while a renews
        a.send_signal(signal.SIGKILL)                                     # no release: it just dies
        a.wait()
        c = sqlite3.connect(self.db)
        expires = c.execute("SELECT vence_en FROM leases WHERE rol = 'orquestador:trycore'").fetchone()[0]
        c.close()
        while self.lease_owner() != "b" and time.time() < expires + 10:
            time.sleep(0.02)
        took = time.time()
        self.assertEqual(self.lease_owner(), "b")
        self.assertLess(took - expires, 2 * interval + 0.2)               # < 2 intervals after it expired


class Orgs(DaemonCase):
    def test_trycore_base_refuses_a_personal_task(self):
        rc, _, err = self.add_task("tp", **{"run-args": json.dumps({"profile": "personal"})})
        self.assertEqual(rc, 2)
        self.assertNotIn("tp", self.states())
        self.assertEqual(self.add_task("ok", **{"run-args": json.dumps({"profile": "trycore"})})[0], 0)

    def test_memoria_section_local_or_ssh(self):
        m = self.cfg.memoria_for("trycore")
        self.assertTrue(m["local"])
        cfg = config.Config({"profiles": {"p": {"owner": "self"}},
                             "memoria": {"personal": {"host": "orq@192.168.2.27", "path": "/x/m.db"}}})
        self.assertFalse(cfg.memoria_for("personal")["local"])
        self.assertEqual(cfg.resolve_org(), "personal")

    def test_remote_client_goes_through_ssh_json(self):
        cfg = config.Config({"profiles": {"p": {"owner": "self"}},
                             "memoria": {"personal": {"host": "orq@192.168.2.27", "path": "/x/m.db"}}})
        calls = []

        def fake_run(argv, **k):
            calls.append(argv)
            body = {"ok": True, "flujos": [{"id": "f", "estado": "activo"}]}
            return subprocess.CompletedProcess(argv, 9 if "claim" in argv[-1] else 0, json.dumps(body), "")
        cl = queue.MemClient(cfg, "personal", "me")
        with mock.patch.object(subprocess, "run", fake_run):
            self.assertFalse(cl.claim(90))
            self.assertEqual(cl.flows(), [{"id": "f", "estado": "activo"}])
        self.assertEqual(calls[0][:3], ["ssh", "-o", "BatchMode=yes"])
        self.assertIn("DELEGA_MEM_OWNER=me delega mem claim orquestador:personal --ttl 90", calls[0][-1])
        self.assertTrue(calls[0][-1].endswith("--json"))


class Install(DaemonCase):
    def test_install_daemon_dry_and_yes(self):
        out = os.path.join(self.tmp.name, "units")
        rc, text, _ = mem_cli("install-daemon", "--out", out)
        self.assertEqual(rc, 0)
        self.assertIn("ExecStart=", text)
        self.assertIn("com.delega.tick", text)
        self.assertFalse(os.path.exists(out))
        rc, _, _ = mem_cli("install-daemon", "--out", out, "--owner", "mac", "--yes")
        self.assertEqual(sorted(os.listdir(out)), ["com.delega.tick.plist", "delega-tick.service"])
        self.assertIn("DELEGA_MEM_OWNER=mac", open(os.path.join(out, "delega-tick.service")).read())


if __name__ == "__main__":
    unittest.main()

"""N2: the new owner of a task launched by another daemon asks the launcher (over ssh) how it is going."""
import contextlib
import io
import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.abspath(os.path.join(HERE, "..", "src"))
sys.path.insert(0, SRC)

from delega import config, mem, queue  # noqa: E402
sys.path.insert(0, HERE)
from test_tick_daemon import DaemonCase  # noqa: E402

FAKE_SSH = """#!/bin/sh
# fake ssh: ssh -o BatchMode=yes <host> <cmd>; runs <cmd> locally against the state_dir of <host>
while [ "$1" = "-o" ]; do shift 2; done
host="$1"; shift
case " $FAKE_DOWN " in *" $host "*) exit 255;; esac
eval "dir=\\$FAKE_STATE_$host"
DELEGA_STATE_DIR="$dir" exec sh -c "$1"
"""
SHIM = """#!/bin/sh
PYTHONPATH="%s" exec python3 -m delega "$@"
"""


class Base(DaemonCase):
    def setUp(self):
        super().setUp()
        queue._LANZADOR_FALLOS.clear()
        r = self.tmp.name
        self.state_a = os.path.join(r, "state-a")
        os.makedirs(self.state_a)
        bindir = os.path.join(r, "bin")
        os.makedirs(bindir)
        for name, body in (("ssh", FAKE_SSH), ("delega", SHIM % SRC)):
            p = os.path.join(bindir, name)
            with open(p, "w") as f:
                f.write(body)
            os.chmod(p, os.stat(p).st_mode | stat.S_IXUSR)
        os.environ["PATH"] = bindir + os.pathsep + os.environ["PATH"]
        os.environ["FAKE_STATE_hosta"] = self.state_a
        os.environ["FAKE_DOWN"] = ""
        os.environ["PYTHONPATH"] = SRC
        subprocess.run(["git", "-C", self.repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
                        "--allow-empty", "-m", "c1"], check=True)
        self.sha1 = subprocess.run(["git", "-C", self.repo, "rev-parse", "HEAD"], capture_output=True,
                                   text=True, check=True).stdout.strip()
        self.cfg = config.load()

    def launch_as_a(self, tid, job_id, rama="delega/q-f1-t1"):
        self.add_task(tid)
        for st in ("lanzada",):
            rc, _, err = mem_cli_set(self.db, tid, st, job_id, rama, "hosta")
            self.assertEqual(rc, 0, err)

    def job_dir_a(self, name, **meta):
        d = os.path.join(self.state_a, name)
        os.makedirs(d)
        with open(os.path.join(d, "meta"), "w") as f:
            for k, v in meta.items():
                f.write(f"{k}={v}\n")
        return d

    def cycle(self, owner="hostb"):
        cap = {"has_data": False, "new": 9, "live": 0, "slots": 9}
        err = io.StringIO()
        with mock.patch("delega.quota.capacity", lambda cfg, name, **k: cap), \
                mock.patch.object(queue, "_reconcile", lambda cfg, adopt: None), \
                mock.patch("delega.relevo.spawn", lambda *a, **k: "x.log"), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            queue.daemon_cycle(self.cfg, self.client(owner), ttl=30)
        return err.getvalue()

    def events(self, que):
        c = sqlite3.connect(self.db)
        try:
            return c.execute("SELECT datos FROM eventos WHERE que = ?", (que,)).fetchall()
        finally:
            c.close()


def mem_cli_set(db, tid, estado, job_id, rama, lanzador):
    from test_tick_daemon import mem_cli
    return mem_cli("set", tid, "--estado", estado, "--job-id", job_id, "--rama", rama, "--lanzador", lanzador,
                   "--org", "trycore", "--db", db)


class LauncherDies(Base):
    def test_running_then_ok_without_relaunching(self):
        jid = "20261118-120000-abc"
        self.launch_as_a("t1", jid)
        d = self.job_dir_a(jid, BRANCH="delega/q-f1-t1", NODE="n1", BASE=self.sha, REPO_DIR=self.repo)
        self.cycle()
        self.assertEqual(self.states()["t1"], "lanzada")
        with open(os.path.join(d, "meta"), "a") as f:
            f.write(f"END=1\nFETCHED={self.sha1}\n")
        with open(os.path.join(d, "summary.json"), "w") as f:
            f.write('{"rc": 0}')
        self.cycle()
        self.assertEqual(self.states()["t1"], "ok")
        self.assertEqual(self.events("necesita_decision"), [])

    def test_fail_of_the_launcher_is_rojo_not_a_decision(self):
        jid = "20261118-120000-abd"
        self.launch_as_a("t1", jid)
        d = self.job_dir_a(jid, BRANCH="delega/q-f1-t1", END=1, REPO_DIR=self.repo)
        with open(os.path.join(d, "summary.json"), "w") as f:
            f.write('{"rc": 1}')
        self.cycle()
        self.assertEqual(self.states()["t1"], "rojo")

    def test_ok_not_fetchable_stays_ok(self):
        jid = "20261118-120000-abe"
        self.launch_as_a("t1", jid)
        d = self.job_dir_a(jid, BRANCH="delega/q-f1-t1", END=1, NODE="n1", BASE=self.sha, REPO_DIR=self.repo,
                           FETCHED=self.sha1)
        with open(os.path.join(d, "summary.json"), "w") as f:
            f.write('{"rc": 0}')
        # the sha is not in this daemon's checkout and the node is unknown here
        with mock.patch.object(queue, "wave_have", lambda repo, sha: False):
            err = self.cycle()
        self.assertEqual(self.states()["t1"], "ok")
        self.assertIn("could not be fetched", err)


class LauncherDown(Base):
    def test_waits_then_asks_once(self):
        os.environ["FAKE_DOWN"] = "hosta"
        self.launch_as_a("t1", "20261118-120000-abc")
        for _ in range(queue.LANZADOR_MAX_FALLOS - 1):
            self.cycle()
        self.assertEqual(self.states()["t1"], "lanzada")
        self.assertEqual(self.events("necesita_decision"), [])
        for _ in range(3):
            self.cycle()
        # the event is written once; the escalation (relevo) then takes the task out of `lanzada`
        self.assertEqual(len(self.events("necesita_decision")), 1)
        self.assertIn("lanzador_caido", self.events("necesita_decision")[0][0])


class ForeignPid(Base):
    def test_pend_pid_of_another_host_is_not_judged_locally(self):
        stamp = "20261118-120000"
        jid = f"pend:{os.getpid()}:{stamp}"          # a pid that exists HERE
        self.launch_as_a("t1", jid)
        d = self.job_dir_a("20261118-120100-zzz", BRANCH="delega/q-f1-t1", END=1, NODE="n1", BASE=self.sha,
                           REPO_DIR=self.repo, FETCHED=self.sha1)
        with open(os.path.join(d, "summary.json"), "w") as f:
            f.write('{"rc": 0}')
        self.cycle()
        self.assertEqual(self.states()["t1"], "ok")

    def test_pend_with_launcher_down_stays_lanzada(self):
        os.environ["FAKE_DOWN"] = "hosta"
        self.launch_as_a("t1", f"pend:{os.getpid()}:20261118-120000")
        self.cycle()
        self.assertEqual(self.states()["t1"], "lanzada")


class Migration(unittest.TestCase):
    def test_old_database_gets_the_column(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "m.db")
            c = sqlite3.connect(db)
            for stmt in mem.SCHEMA:
                c.execute(stmt.replace("lanzador    TEXT,", ""))
            c.execute("INSERT INTO org VALUES (1, 'trycore', 1)")
            c.execute("INSERT INTO tareas (id, flujo, brief, creado, actualizado) VALUES ('t', 'f', 'b', 1, 1)")
            c.execute("PRAGMA user_version = 2")
            c.commit()
            cols = {r[1] for r in c.execute("PRAGMA table_info(tareas)")}
            self.assertNotIn("lanzador", cols)
            c.close()
            for _ in range(2):                       # idempotent
                conn = mem.connect(db)
                cols = {r[1] for r in conn.execute("PRAGMA table_info(tareas)")}
                self.assertIn("lanzador", cols)
                self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], mem.SCHEMA_VERSION)
                self.assertIsNone(conn.execute("SELECT lanzador FROM tareas").fetchone()[0])
                conn.close()


if __name__ == "__main__":
    unittest.main()

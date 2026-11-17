"""F13: the seam N2 <-> N13 <-> relevo. One decision for the state of a launched task (the daemon and step 1
of `wave.step`), `lanzador` is an ssh alias, and what `integrate` merges on a node reaches the bare repo.
Nothing of integrate, job_state or wave_have is faked: only the ssh (one fake per host) and `delega run`."""
import contextlib
import io
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
SRC = os.path.abspath(os.path.join(HERE, "..", "src"))
sys.path.insert(0, SRC)
sys.path.insert(0, HERE)

import test_tick_daemon as T  # noqa: E402
from delega import config, jobstate, queue, relevo, sync, wave  # noqa: E402

FAKE_SSH = """#!/bin/sh
# fake ssh: ssh -o BatchMode=yes <host> <cmd>; runs <cmd> locally against the state_dir of <host>
while [ "$1" = "-o" ]; do shift 2; done
host="$1"; shift
echo "$host" >> "$FAKE_SSH_LOG"
case " $FAKE_DOWN " in *" $host "*) exit 255;; esac
case "$host" in nodo-a) dir="$FAKE_STATE_A";; nodo-b) dir="$FAKE_STATE_B";; *) exit 255;; esac
DELEGA_STATE_DIR="$dir" exec sh -c "$1"
"""
SHIM = """#!/bin/sh
PYTHONPATH="%s" exec python3 -m delega "$@"
"""
TOML = """
[general]
default_profile = "trycore"
[profiles.trycore]
owner = "self"
[memoria.trycore]
path = "{db}"
repos = {{ "acme/api" = "{repo}" }}
[projects."acme/api"]
verify = ["true"]
{extra}
"""
IDENT = ["-c", "user.name=t", "-c", "user.email=t@t"]


def git(repo, *a, **kw):
    r = subprocess.run(["git", "-C", repo, *a], capture_output=True, text=True, **kw)
    return r.stdout.strip()


def commit_file(repo, base, name):
    """A commit on top of `base` that adds the file `name` (plumbing: the checkout is not touched)."""
    idx = os.path.join(repo, ".git", "f13.idx") if os.path.isdir(os.path.join(repo, ".git")) \
        else os.path.join(repo, "f13.idx")
    env = dict(os.environ, GIT_INDEX_FILE=idx)
    subprocess.run(["git", "-C", repo, "read-tree", base], env=env, check=True)
    blob = subprocess.run(["git", "-C", repo, "hash-object", "-w", "--stdin"], input=name, text=True,
                          capture_output=True, check=True).stdout.strip()
    subprocess.run(["git", "-C", repo, "update-index", "--add", "--cacheinfo", f"100644,{blob},{name}"],
                   env=env, check=True)
    tree = subprocess.run(["git", "-C", repo, "write-tree"], env=env, capture_output=True, text=True,
                          check=True).stdout.strip()
    os.remove(idx)
    return subprocess.run(["git", "-C", repo, *IDENT, "commit-tree", tree, "-p", base, "-m", name],
                          capture_output=True, text=True, check=True).stdout.strip()


class F13(T.DaemonCase):
    def setUp(self):
        super().setUp()
        for p in (mock.patch("delega.mem.mounts", lambda: []),
                  mock.patch("delega.quota.capacity",
                             lambda cfg, name, **k: {"has_data": False, "new": 9, "live": 0, "slots": 9}),
                  mock.patch.object(queue, "_reconcile", lambda cfg, adopt: None),
                  mock.patch("delega.relevo.spawn", lambda *a, **k: "x.log")):
            p.start()
            self.addCleanup(p.stop)
        r = self.tmp.name
        self.states_dir = {h: os.path.join(r, "state-" + h) for h in ("a", "b")}
        for d in self.states_dir.values():
            os.makedirs(d)
        bindir = os.path.join(r, "bin")
        os.makedirs(bindir)
        for name, body in (("ssh", FAKE_SSH), ("delega", SHIM % SRC)):
            p = os.path.join(bindir, name)
            with open(p, "w") as f:
                f.write(body)
            os.chmod(p, os.stat(p).st_mode | stat.S_IXUSR)
        self.sshlog = os.path.join(r, "ssh.log")
        open(self.sshlog, "w").close()
        self.home = os.path.join(r, "home")
        os.makedirs(self.home)
        os.environ.update(PATH=bindir + os.pathsep + os.environ["PATH"], HOME=self.home, PYTHONPATH=SRC,
                          FAKE_STATE_A=self.states_dir["a"], FAKE_STATE_B=self.states_dir["b"],
                          FAKE_SSH_LOG=self.sshlog, FAKE_DOWN="")
        self.runs, self.launch_dirs = [], {}

    # -- the world ---------------------------------------------------------------------------
    def write_conf(self, extra=""):
        with open(self.conf, "w") as f:
            f.write(TOML.format(db=self.db, repo=self.repo, extra=extra))

    def ssh_calls(self):
        with open(self.sshlog) as f:
            return f.read().split()

    def lanzadores(self):
        c = sqlite3.connect(self.db)
        try:
            return dict(c.execute("SELECT id, lanzador FROM tareas").fetchall())
        finally:
            c.close()

    def events(self, que="necesita_decision"):
        c = sqlite3.connect(self.db)
        try:
            return [json.loads(r[0]) for r in c.execute("SELECT datos FROM eventos WHERE que = ?", (que,))]
        finally:
            c.close()

    def cycle(self, host, owner, ttl=30, client_host="__same__"):
        """One daemon cycle as the daemon of `host` (its own state_dir). `delega run` is the only fake:
        it leaves a job directory (still running) and remembers where it started from."""
        os.environ["DELEGA_STATE_DIR"] = self.states_dir[host]
        cfg = config.load()
        client = queue.MemClient(cfg, "trycore", owner, host=("nodo-" + host) if client_host == "__same__"
                                 else client_host)
        real_popen = subprocess.Popen

        class P:
            pid = 999999

            def wait(self, timeout=None):
                raise subprocess.TimeoutExpired("run", timeout)

        def fake_popen(cmd, **kw):
            if "--branch" not in cmd:
                return real_popen(cmd, **kw)
            branch = cmd[cmd.index("--branch") + 1]
            repo = cmd[cmd.index("run") + 1]
            self.runs.append(branch)
            d = os.path.join(self.states_dir[host], time.strftime("%Y%m%d-%H%M%S") + f"-w{len(self.runs)}-fake")
            os.makedirs(d)
            base = git(repo, "rev-parse", "HEAD")
            with open(os.path.join(d, "meta"), "w") as f:
                f.write(f"BRANCH={branch}\nBASE={base}\nREPO_DIR={repo}\nNODE=n1\n")
            self.launch_dirs[branch] = (d, repo, base)
            return P()

        err = io.StringIO()
        with mock.patch.object(wave.subprocess, "Popen", fake_popen), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            queue.daemon_cycle(cfg, client, ttl=ttl)
        return err.getvalue()

    def finish_job(self, branch, rc=0):
        """The job of `branch` ends: its branch gets a commit in the repo it started from."""
        d, repo, base = self.launch_dirs[branch]
        sha = commit_file(repo, base, branch.rsplit("-", 1)[-1] + ".txt")
        git(repo, "update-ref", f"refs/heads/{branch}", sha)
        with open(os.path.join(d, "meta"), "a") as f:
            f.write(f"END=1\nFETCHED={sha}\n")
        with open(os.path.join(d, "summary.json"), "w") as f:
            json.dump({"rc": rc}, f)
        return sha

    # -- (a) ---------------------------------------------------------------------------------
    def test_a_new_owner_does_not_relaunch_the_job_of_the_dead_one_and_integrates_it(self):
        self.write_conf()
        self.add_task("t1")
        self.add_task("t2", wave="2")
        self.cycle("a", "owner-a", ttl=0.5)                     # A launches t1 through wave.step ... and dies
        self.assertEqual(self.states(), {"t1": "lanzada", "t2": "pendiente"})
        self.assertEqual(self.runs, ["delega/wave-f1-t1"])
        self.assertEqual(self.lanzadores()["t1"], "nodo-a")
        time.sleep(0.8)
        self.cycle("b", "owner-b")                              # B takes the lease; A's job is alive
        self.assertEqual(self.states(), {"t1": "lanzada", "t2": "pendiente"})
        self.assertEqual(self.runs, ["delega/wave-f1-t1"])
        self.assertIn("nodo-a", self.ssh_calls())
        self.finish_job("delega/wave-f1-t1")
        self.cycle("b", "owner-b")                              # ok -> integrated (the real one) -> hecha
        self.assertEqual(self.states()["t1"], "hecha")
        self.assertEqual(self.runs, ["delega/wave-f1-t1"])      # t1 was never relaunched
        self.assertEqual(self.events(), [])
        self.assertIn("t1.txt", git(self.repo, "ls-tree", "-r", "--name-only", "main"))
        self.cycle("b", "owner-b")
        self.assertEqual(self.runs, ["delega/wave-f1-t1", "delega/wave-f1-t2"])
        self.assertEqual(self.lanzadores()["t2"], "nodo-b")

    # -- (b) ---------------------------------------------------------------------------------
    def test_a_node_launches_the_dependent_from_the_bare_that_holds_what_was_integrated(self):
        bare = os.path.join(self.home, "delega", "api.git")
        os.makedirs(os.path.dirname(bare))
        subprocess.run(["git", "clone", "-q", "--bare", self.repo, bare], check=True)
        self.write_conf('[relevo]\nnode = "nodo-a"\n')
        work = os.path.join(self.home, "delega", "relevo", "api")
        self.add_task("t1")
        self.add_task("t2")
        self.add_task("t3", deps="t1,t2")
        self.cycle("a", "owner-a", client_host=None)            # host = the node of `[relevo]`
        self.assertEqual(self.lanzadores()["t1"], "nodo-a")
        self.assertEqual(sorted(self.runs), ["delega/wave-f1-t1", "delega/wave-f1-t2"])
        for b in list(self.runs):
            self.finish_job(b)
        self.cycle("a", "owner-a", client_host=None)            # integrates t1 and t2
        self.assertEqual(self.states(), {"t1": "hecha", "t2": "hecha", "t3": "pendiente"})
        merged = git(work, "rev-parse", "refs/heads/main")
        self.assertEqual(git(bare, "rev-parse", "refs/heads/main"), merged)     # it reached the bare
        for f in ("t1.txt", "t2.txt"):
            self.assertIn(f, git(bare, "ls-tree", "-r", "--name-only", "main"))
        self.cycle("a", "owner-a", client_host=None)            # the dependent starts from it
        self.assertEqual(self.runs[-1], "delega/wave-f1-t3")
        _d, repo, base = self.launch_dirs["delega/wave-f1-t3"]
        self.assertEqual(repo, work)
        self.assertEqual(base, git(bare, "rev-parse", "refs/heads/main"))

    def test_origin_does_not_undo_a_newer_local_destination(self):
        bare = os.path.join(self.home, "delega", "api.git")
        os.makedirs(os.path.dirname(bare))
        subprocess.run(["git", "clone", "-q", "--bare", self.repo, bare], check=True)
        self.write_conf('[relevo]\nnode = "nodo-a"\n')
        cfg = config.load()
        work = relevo.origin(cfg, T.REMOTE, "main")
        ahead = commit_file(work, git(work, "rev-parse", "HEAD"), "x.txt")
        git(work, "update-ref", "refs/heads/main", ahead)       # merged here, not pushed yet
        self.assertEqual(relevo.origin(cfg, T.REMOTE, "main"), work)
        self.assertEqual(git(work, "rev-parse", "HEAD"), ahead)
        self.assertEqual(git(bare, "rev-parse", "main"), ahead)  # and it was brought to the bare

    # -- (c) ---------------------------------------------------------------------------------
    def _launched_by_a(self, tid, job_id="20261118-120000-abc", lanzador="nodo-a"):
        self.add_task(tid)
        rc, _, err = T.mem_cli("set", tid, "--estado", "lanzada", "--job-id", job_id, "--rama",
                               f"delega/wave-f1-{tid}", "--lanzador", lanzador, "--org", "trycore", "--db", self.db)
        self.assertEqual(rc, 0, err)

    def test_c_launcher_without_the_job_directory_is_one_decision_not_red_in_waves(self):
        self.write_conf()
        self._launched_by_a("t1")
        self.add_task("t2", wave="2")
        for _ in range(3):
            self.cycle("b", "owner-b")
        self.assertNotEqual(self.states()["t1"], "rojo")         # (relevo's escalation may block it)
        self.assertEqual([e["motivo"] for e in self.events()], ["job_sin_directorio"])
        self.assertEqual(self.runs, [])

    def test_c_launcher_without_the_job_directory_is_one_decision_not_red_without_waves(self):
        self.write_conf()
        self._launched_by_a("t1")
        for _ in range(3):
            self.cycle("b", "owner-b")
        self.assertNotEqual(self.states()["t1"], "rojo")         # (relevo's escalation may block it)
        self.assertEqual([e["motivo"] for e in self.events()], ["job_sin_directorio"])

    def test_launcher_down_keeps_the_task_in_waves_and_never_relaunches(self):
        self.write_conf()
        os.environ["FAKE_DOWN"] = "nodo-a"
        self._launched_by_a("t1", job_id="pend:999999:20261118-120000")
        self.add_task("t2", wave="2")
        for _ in range(3):
            self.cycle("b", "owner-b")
        self.assertEqual(self.states()["t1"], "lanzada")
        self.assertEqual(self.runs, [])

    def test_a_launcher_without_alias_is_not_asked_and_ends_in_a_decision(self):
        self.write_conf()
        self._launched_by_a("t1", lanzador="mac-de-juan:tick")
        for _ in range(3):
            self.cycle("b", "owner-b")
        self.assertEqual(self.ssh_calls(), [])
        self.assertNotEqual(self.states()["t1"], "rojo")
        self.assertEqual([e["motivo"] for e in self.events()], ["lanzador_sin_ssh"])

    # -- (d) ---------------------------------------------------------------------------------
    def test_d_lanzador_is_the_node_alias_not_host_tick(self):
        self.write_conf()
        self.add_task("t1")
        self.cycle("a", "somehost:tick")                        # the plain queue path
        self.assertEqual(self.lanzadores(), {"t1": "nodo-a"})
        self.add_task("t2")
        self.add_task("t3", wave="2")
        self.cycle("a", "somehost:tick")                        # and the wave path
        self.assertEqual(self.lanzadores()["t2"], "nodo-a")

    def test_d_the_default_host_is_the_relevo_node(self):
        self.write_conf('[relevo]\nnode = "nodo-b"\n')
        cfg = config.load()
        self.assertEqual(queue.MemClient(cfg, "trycore", "x:tick").host, "nodo-b")
        self.assertEqual(queue.MemClient(cfg, "trycore", "x:tick", host="nodo-a").host, "nodo-a")
        self.write_conf()
        self.assertTrue(queue.MemClient(config.load(), "trycore", "x:tick").host)   # the Mac: an explicit id

    # -- job-state ---------------------------------------------------------------------------
    def test_job_state_answers_not_found_and_writes_nothing(self):
        os.environ["DELEGA_STATE_DIR"] = os.path.join(self.tmp.name, "fresh-state")
        for jid in ("20261118-120000-abc", "pend:999999:20261118-120000"):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = jobstate.main(["--task", "t1", "--rama", "b", "--job-id", jid, "--json"])
            self.assertEqual(rc, 0)
            self.assertEqual(json.loads(out.getvalue())["estado"],
                             "no_encontrado" if not jid.startswith("pend") else "fail")
        self.assertFalse(os.path.exists(os.environ["DELEGA_STATE_DIR"]))

    # -- (e) ---------------------------------------------------------------------------------
    def test_e_a_failed_fetch_does_not_push_the_old_ref(self):
        pushes = []
        co = self.repo
        git(co, "update-ref", "refs/remotes/origin/main", git(co, "rev-parse", "HEAD"))
        git(co, "remote", "set-url", "origin", os.path.join(self.tmp.name, "does-not-exist.git"))
        node = {"name": "n1", "ssh": "n1", "workdir": "delega"}
        real_run = subprocess.run

        def spy(cmd, *a, **k):
            if "push" in cmd:
                pushes.append(cmd)
            return real_run(cmd, *a, **k)
        with mock.patch("delega.remote.ssh") as ssh, mock.patch("subprocess.run", spy):
            ok, err = sync.push_branch(node, "api", "main", co)
        self.assertFalse(ok)
        self.assertEqual(pushes, [])
        ssh.assert_not_called()
        self.assertIn("fetch", err)


if __name__ == "__main__":
    unittest.main()

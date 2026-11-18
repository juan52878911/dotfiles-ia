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
        # a real checkout whose origin is the repo of the flow (the daemon checks it, like `delega wave`)
        for cmd in (["init", "-q", "-b", "main"], ["remote", "add", "origin", REMOTE],
                    ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "c0"]):
            subprocess.run(["git", "-C", self.repo, *cmd], check=True)
        self.sha = subprocess.run(["git", "-C", self.repo, "rev-parse", "HEAD"], capture_output=True,
                                  text=True, check=True).stdout.strip()
        self.conf = os.path.join(r, "config.toml")
        with open(self.conf, "w") as f:
            f.write(TOML.format(db=self.db, repo=self.repo))
        self._env = dict(os.environ)
        for k in [k for k in os.environ if k.startswith(("DELEGA_", "ORQ_"))]:
            del os.environ[k]
        os.environ.update(DELEGA_STATE_DIR=self.state, DELEGA_CONFIG=self.conf, DELEGA_LOCALE="en")
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(self._env)))
        self.assertEqual(mem_cli("init", "--org", "trycore", "--db", self.db)[0], 0)
        self.assertEqual(mem_cli("add-flow", "--repo", REMOTE, "--id", "f1", "--org", "trycore",
                                 "--db", self.db)[0], 0)
        self.cfg = config.load()

    def add_task(self, tid, **kw):
        return mem_cli("add-task", "--flow", "f1", "--brief", "do " + tid, "--id", tid, "--org", "trycore",
                       "--db", self.db, *sum(([f"--{k}", v] for k, v in kw.items()), []))

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
        brief = os.path.join(self.tmp.name, "b.md")
        with open(brief, "w") as f:
            f.write("legacy brief")
        item = queue.add(self.cfg, self.repo, brief)
        before = open(queue.qpath(self.cfg)).read()
        launched = []
        self.assertTrue(self.cycle("a", launched))
        self.assertEqual(launched, [("a", "q-" + item["id"])])
        self.assertNotEqual(open(queue.qpath(self.cfg)).read(), before)   # the item is marked migrated
        self.assertEqual(queue.read_items(self.cfg)[0]["state"], "migrated")
        self.assertTrue(self.cycle("a", launched))
        self.assertEqual(len(launched), 1)                                # migrated once


class CompareAndSet(DaemonCase):
    """Two daemons see the same pending task: the compare-and-set (`set --desde pendiente`) is
    what keeps the second one from launching it again."""

    def test_only_one_daemon_takes_a_pending_task(self):
        self.add_task("t1")
        a, b = self.client("a"), self.client("b")
        rc_a, _ = a.set("t1", "lanzada", desde="pendiente")
        rc_b, payload = b.set("t1", "lanzada", desde="pendiente")
        self.assertEqual(rc_a, mem.EXIT_OK)
        self.assertEqual(rc_b, mem.EXIT_LEASE)              # 9: the state changed under its feet
        self.assertIn("state changed", payload.get("error", ""))
        self.assertEqual(self.states(), {"t1": "lanzada"})

    def test_two_daemons_that_see_the_task_launch_it_once(self):
        self.add_task("t1")
        launched = []

        def fake_launch(cfg, item):
            launched.append(item["id"])
            item["log"] = "x.log"
            return "launched", "1"

        cap = {"has_data": False, "new": 9, "live": 0, "slots": 9}
        task = {"id": "t1", "ola": 1, "brief": "do t1", "run_args": {}}
        clients = []
        for owner in ("a", "b"):
            c = self.client(owner)
            c.ready = lambda flow, t=task: [t]            # both daemons see the task as pending
            clients.append(c)
        err = io.StringIO()
        with mock.patch.object(queue, "launch", fake_launch), \
                mock.patch.object(queue.MemClient, "claim", lambda self, ttl: True), \
                mock.patch("delega.quota.capacity", lambda cfg, name, **k: cap), \
                mock.patch.object(queue, "_reconcile", lambda cfg, adopt: None), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            for c in clients:
                queue.daemon_cycle(self.cfg, c, ttl=30)
        self.assertEqual(launched, ["t1"])                 # only the daemon that took it launched it
        self.assertEqual(self.states(), {"t1": "lanzada"})
        self.assertIn("state changed", err.getvalue())     # the second one said why it skipped it


CAP = {"has_data": False, "new": 9, "live": 0, "slots": 9}


class Hardening(DaemonCase):
    """F7: reconciliation, waves, per-org files, migrated items, org of the profile, one escalation."""

    def setUp(self):
        super().setUp()
        # en macOS mem.mounts() lanza `mount`, que estas pruebas contarían como un Popen más
        p = mock.patch("delega.mem.mounts", lambda: [])
        p.start()
        self.addCleanup(p.stop)
        # sin el `ws` real del equipo: si está en PATH, repo_match llama a `ws contexto` y estas pruebas lo contarían
        path = os.pathsep.join([os.path.dirname(sys.executable), "/usr/bin", "/bin"])
        e = mock.patch.dict(os.environ, {"PATH": path})
        e.start()
        self.addCleanup(e.stop)

    def cycle(self, client=None, launch=None, **extra):
        client = client or self.client("a")
        patches = [mock.patch("delega.quota.capacity", lambda cfg, name, **k: CAP),
                   mock.patch.object(queue, "_reconcile", lambda cfg, adopt: None)]
        if launch:
            patches.append(mock.patch.object(queue, "launch", launch))
        err = io.StringIO()
        with contextlib.ExitStack() as st:
            [st.enter_context(p) for p in patches]
            st.enter_context(contextlib.redirect_stdout(io.StringIO()))
            st.enter_context(contextlib.redirect_stderr(err))
            queue.daemon_cycle(self.cfg, client, ttl=30)
        return err.getvalue()

    def row(self, tid):
        c = sqlite3.connect(self.db)
        try:
            return c.execute("SELECT estado, job_id, rama, nodo FROM tareas WHERE id = ?", (tid,)).fetchone()
        finally:
            c.close()

    def test_launch_records_job_branch_and_node_through_the_real_launch(self):
        self.add_task("t1")

        class P:
            pid = 4242

            def wait(self, timeout=None):
                return 0
        # Popen falso para todo el proceso: en macOS mem.mounts() llama a `mount`; aquí no hay red que comprobar
        with mock.patch("subprocess.Popen", lambda *a, **k: P()), mock.patch("delega.mem.mounts", lambda: []):
            self.cycle()
        estado, job_id, rama, nodo = self.row("t1")
        self.assertEqual(estado, "lanzada")
        self.assertTrue(job_id.startswith("pend:4242:"), job_id)
        self.assertEqual(rama, "delega/q-f1-t1")
        self.assertEqual(nodo, "auto")

    def fake_job(self, jid, branch, rc=0, sha=None, end=True, fetch_log=""):
        """A real job directory (meta + summary.json + fetch.log), as `delega run` leaves it."""
        d = os.path.join(self.state, jid + "-fake")
        os.makedirs(d)
        meta = f"BRANCH={branch}\n" + ("END=1\n" if end else "") + (f"FETCHED={sha}\n" if sha else "")
        with open(os.path.join(d, "meta"), "w") as f:
            f.write(meta)
        with open(os.path.join(d, "summary.json"), "w") as f:
            json.dump({"rc": rc}, f)
        with open(os.path.join(d, "fetch.log"), "w") as f:
            f.write(fetch_log)
        return d

    def test_lanzada_is_reconciled_to_ok_or_rojo(self):        # finding 1: the real job_state (3 values)
        for t, jid in (("t1", "20260101-000001-aaaa"), ("t2", "20260101-000002-bbbb"),
                       ("t3", "20260101-000003-cccc")):
            self.add_task(t)
            self.assertEqual(self.client("a").set(t, "lanzada", job_id=jid)[0], 0)
        self.fake_job("20260101-000001-aaaa", "b1", rc=0, sha=self.sha, fetch_log="Warning: Permanently added")
        self.fake_job("20260101-000002-bbbb", "b2", rc=1)
        self.fake_job("20260101-000003-cccc", "b3", end=False)
        self.cycle()
        self.assertEqual(self.states(), {"t1": "ok", "t2": "rojo", "t3": "lanzada"})

    def test_lanzada_with_a_missing_job_directory_asks_for_a_decision_once(self):
        self.add_task("t1")
        self.assertEqual(self.client("a").set("t1", "lanzada", job_id="20260101-000009-gone")[0], 0)
        self.cycle()
        self.cycle()
        self.assertNotEqual(self.states()["t1"], "rojo")   # relevo may escalate it to bloqueada
        c = sqlite3.connect(self.db)
        try:
            n = c.execute("SELECT COUNT(*) FROM eventos WHERE que = 'necesita_decision' "
                          "AND datos LIKE '%lanzador_sin_ssh%'").fetchone()[0]
        finally:
            c.close()
        self.assertEqual(n, 1)

    def test_flow_with_waves_goes_to_wave_step_and_not_to_the_plain_launch(self):
        from delega import wave
        self.add_task("t1")
        self.add_task("t2", wave="2")
        calls, launched = [], []
        with mock.patch.object(wave, "step", lambda conn, flow, env, **kw: calls.append(
                (flow, kw.get("org"), env.repo_dir, env.org))):
            self.cycle(launch=lambda cfg, it: launched.append(it["id"]) or ("launched", "1"))
        self.assertEqual(calls, [("f1", "trycore", os.path.abspath(self.repo), "trycore")])   # finding 2
        self.assertEqual(launched, [])
        self.assertEqual(self.states(), {"t1": "pendiente", "t2": "pendiente"})

    def test_wave_flow_full_cycle_with_the_real_wave_and_queue(self):       # findings 1, 2, 9, 10
        """pendiente -> lanzada -> ok -> integrada -> hecha, with `delega run` and `integrate` faked
        at the process boundary only: wave.step, Env.job_state, _fetched_sha and the daemon are real."""
        from delega import wave
        self.add_task("t1")
        integrations, runs = [], []

        class P:
            pid = 999999

        real_popen = subprocess.Popen

        def fake_popen(cmd, **kw):               # `delega run`: leaves a finished job dir behind
            if "--branch" not in cmd:            # (subprocess is global: git and the rest stay real)
                return real_popen(cmd, **kw)
            branch = cmd[cmd.index("--branch") + 1]
            runs.append((branch, kw["cwd"]))
            stamp = time.strftime("%Y%m%d-%H%M%S")
            self.fake_job(f"{stamp}-w{len(runs)}", branch, rc=0, sha=self.sha, fetch_log="Warning: Permanently added")
            return P()

        def fake_integrate(env, branch, into, node, sha=None):
            integrations.append((env.repo_dir, branch, into, sha))
            return 0, "ok"

        def drive(n=3):
            with mock.patch.object(wave.subprocess, "Popen", fake_popen), \
                    mock.patch.object(wave.Env, "integrate", fake_integrate):
                for _ in range(n):
                    self.cycle()
        # a second wave makes it a flow with waves; t2 must wait for t1
        self.add_task("t2", wave="2")
        drive(1)
        self.assertEqual(self.states(), {"t1": "lanzada", "t2": "pendiente"})
        self.assertEqual(runs, [("delega/wave-f1-t1", os.path.abspath(self.repo))])
        self.assertTrue(os.path.isfile(os.path.join(self.state, "wave", "trycore", "f1-t1.md")))
        drive(1)                                  # reconcile (ok) + integrate the whole wave 1
        self.assertEqual(self.states()["t1"], "hecha")
        self.assertEqual(integrations[0], (os.path.abspath(self.repo), "delega/wave-f1-t1", "main", self.sha))
        drive(3)
        self.assertEqual(self.states(), {"t1": "hecha", "t2": "hecha"})
        self.assertFalse(os.path.exists(os.path.join(self.state, "wave", "_")))

    def test_real_env_fetched_sha_trusts_meta_over_a_noisy_fetch_log(self):    # finding 9
        from delega import wave
        env = wave.Env(self.repo, org="trycore")
        noisy = self.fake_job("20260101-000001-aaaa", "b1", sha=self.sha, fetch_log="Warning: Permanently added")
        old_ok = self.fake_job("20260101-000002-bbbb", "main")                       # old job: empty log, no key
        old_bad = self.fake_job("20260101-000003-cccc", "main", fetch_log="fatal: x")
        self.assertEqual(env._fetched_sha(noisy, "20260101-000001-aaaa", "b1"), self.sha)
        self.assertEqual(env._fetched_sha(old_ok, "20260101-000002-bbbb", "main"), self.sha)
        self.assertIsNone(env._fetched_sha(old_bad, "20260101-000003-cccc", "main"))

    def test_retry_from_rojo_carries_the_tail_of_the_failed_log(self):          # finding 12
        from delega import wave
        self.add_task("t1")
        self.add_task("t2", wave="2")
        self.assertEqual(self.client("a").set("t1", "lanzada", job_id="20260101-000001-aaaa", rama="b1")[0], 0)
        d = self.fake_job("20260101-000001-aaaa", "b1", rc=1)
        with open(os.path.join(d, "progress.log"), "w") as f:
            f.write("\n".join(f"line {i}" for i in range(200)))
        launches = []

        def fake_launch(env, task, brief, branch, flags):
            launches.append(brief)
            return "pend:1:x"
        with mock.patch.object(wave.Env, "launch", fake_launch):
            self.cycle()
            self.cycle()
        self.assertEqual(len(launches), 1)
        self.assertIn("line 199", launches[0])
        self.assertIn("line 120", launches[0])
        self.assertNotIn("line 100\n", launches[0])

    def test_task_without_profile_is_checked_against_the_org_of_the_default_profile(self):   # finding 15
        from delega import wave
        with open(self.conf, "a") as f:
            f.write('[profiles.personal.repo_match]\ngit_remote = "example.com:acme/api"\n')
        self.cfg = config.load()
        self.add_task("t1")
        conn = mem.connect(self.db)
        try:
            wave.check_profiles(conn, "f1", "trycore")                               # no repo dir: nothing to resolve
            with self.assertRaises(mem.MemError):
                wave.check_profiles(conn, "f1", "trycore", self.repo)
        finally:
            conn.close()

    def test_escalation_goes_through_relevo_escalate_with_org_and_task_profile(self):    # finding 7, 13
        from delega import relevo
        self.add_task("t1")
        self.client("a").set("t1", "lanzada")
        conn = mem.connect(self.db)
        mem.add_event(conn, relevo.NEEDS_DECISION, {"tarea": "t1", "motivo": "x"}, task="t1")
        conn.close()
        popen = []

        class P:
            pass
        with mock.patch.object(relevo.subprocess, "Popen", lambda cmd, **k: popen.append(cmd) or P()):
            self.cycle()
            self.cycle()
        self.assertEqual(len(popen), 1)                                              # once, never twice
        esc = [d for d in os.listdir(self.state) if d.endswith("-esc-t1")]
        self.assertEqual(len(esc), 1)
        info = json.load(open(os.path.join(self.state, esc[0], "escalado.json")))
        self.assertEqual(info["PROFILE"], "trycore")
        self.assertEqual(self.states()["t1"], "bloqueada")

    def test_escalation_profile_prefers_the_task_then_repo_match(self):        # finding 13
        from delega import relevo
        with open(self.conf, "a") as f:
            f.write('[profiles.trycore.repo_match]\ngit_remote = "other/x"\n')
            f.write('[profiles.second]\nowner = "self"\norg = "trycore"\n[profiles.second.repo_match]\n'
                    'git_remote = "acme/api"\n')
        cfg = config.load()
        self.assertEqual(relevo.escalation_profile(cfg, "trycore", None, REMOTE), "second")
        self.assertEqual(relevo.escalation_profile(cfg, "trycore", {"run_args": {"profile": "trycore"}}, REMOTE),
                         "trycore")
        self.assertEqual(relevo.escalation_profile(cfg, "trycore", None, "git@h:zz/yy"), "trycore")

    def test_wave_flow_with_the_wrong_checkout_is_refused(self):     # finding 2: same check as `delega wave`
        from delega import wave
        subprocess.run(["git", "-C", self.repo, "remote", "set-url", "origin", "git@example.com:other/thing"],
                       check=True)
        self.add_task("t1")
        self.add_task("t2", wave="2")
        calls = []
        with mock.patch.object(wave, "step", lambda *a, **k: calls.append(1)):
            err = self.cycle()
        self.assertEqual(calls, [])
        self.assertIn("other/thing", err)

    def test_brief_and_log_live_under_the_org_and_the_flow(self):
        self.add_task("t1")
        seen = []

        def fake(cfg, it):
            seen.append(it)
            it["log"] = "x"
            return "launched", "1"
        self.cycle(launch=fake)
        want = os.path.join(self.state, "queue", "trycore")
        self.assertEqual(seen[0]["brief"], os.path.join(want, "f1-t1.md"))
        self.assertEqual(seen[0]["log_path"], os.path.join(want, "f1-t1.log"))
        self.assertFalse(os.path.exists(os.path.join(self.state, "queue", "t1.md")))

    def test_migrated_items_are_not_launched_by_a_normal_tick(self):
        brief = os.path.join(self.tmp.name, "b.md")
        with open(brief, "w") as f:
            f.write("legacy")
        queue.add(self.cfg, self.repo, brief)
        self.cycle(launch=lambda cfg, it: ("launched", "1"))
        self.assertEqual([i["state"] for i in queue.read_items(self.cfg)], ["migrated"])
        launched = []
        with mock.patch.object(queue, "launch", lambda cfg, it: launched.append(it) or ("launched", "1")), \
                mock.patch.object(queue, "_reconcile", lambda cfg, adopt: None), \
                contextlib.redirect_stdout(io.StringIO()):
            queue.main_tick([])
        self.assertEqual(launched, [])

    def test_task_without_profile_of_another_org_is_refused(self):
        with open(self.conf, "a") as f:
            f.write('[profiles.personal.repo_match]\ngit_remote = "acme/api"\n')
        self.cfg = config.load()
        self.add_task("t1")
        launched = []
        err = self.cycle(launch=lambda cfg, it: launched.append(it["id"]) or ("launched", "1"))
        self.assertEqual(launched, [])
        self.assertEqual(self.states(), {"t1": "pendiente"})
        self.assertIn("does not belong", err)

    def test_one_escalation_per_cycle(self):
        from delega import relevo
        for t in ("t1", "t2"):
            self.add_task(t)
            self.client("a").set(t, "lanzada")
        conn = mem.connect(self.db)
        for t in ("t1", "t2"):
            mem.add_event(conn, relevo.NEEDS_DECISION, {"tarea": t, "motivo": "x"}, task=t)
        conn.close()
        spawned = []
        with mock.patch.object(relevo, "spawn", lambda cfg, tid, brief, *a, **k: spawned.append(tid) or "log"):
            self.cycle()
            self.assertEqual(len(spawned), 1)
            self.cycle()
        self.assertEqual(sorted(spawned), ["t1", "t2"])


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
                             "memoria": {"personal": {"host": "orq@memoria.example", "path": "/x/m.db"}}})
        self.assertFalse(cfg.memoria_for("personal")["local"])
        self.assertEqual(cfg.resolve_org(), "personal")

    def test_remote_client_goes_through_ssh_json(self):
        cfg = config.Config({"profiles": {"p": {"owner": "self"}},
                             "memoria": {"personal": {"host": "orq@memoria.example", "path": "/x/m.db"}}})
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
        self.assertIn("DELEGA_MEM_OWNER=me", calls[0][-1])         # the owner always travels
        self.assertIn("DELEGA_MEM_ORG=personal", calls[0][-1])      # and so does the org
        self.assertIn("delega mem claim orquestador:personal --ttl 90", calls[0][-1])
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

"""F15: the escalated Claude has no ssh (`relevo-mem`), a refused launch three times blocks, the launcher count is
bounded, and `registro` is redacted. Real `relevo-mem`, `MemClient`, `task_state` and `wave.step`; fake ssh/claude."""
import contextlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.abspath(os.path.join(HERE, "..", "src"))
sys.path.insert(0, SRC)
sys.path.insert(0, HERE)

from delega import config, mem, queue, relevo, wave  # noqa: E402
import test_relevo as R  # noqa: E402
import test_tick_daemon as D  # noqa: E402

REMOTE_CONF = R.NODE.replace('path = "{db}"', 'host = "orq@elsewhere"\npath = "{db}"')


class RelevoMem(R.Base):
    CONF = REMOTE_CONF                      # the base is on ANOTHER host: every call goes through the (fake) ssh

    def setUp(self):
        super().setUp()
        self.assertEqual(R.mem_cli("init", "--org", "trycore", "--db", self.db)[0], 0)
        self.assertEqual(R.mem_cli("add-flow", "--repo", R.REMOTE, "--id", "f1", "--db", self.db)[0], 0)
        self.assertEqual(R.mem_cli("add-task", "--flow", "f1", "--brief", "b", "--id", "t1", "--db", self.db)[0], 0)
        R.script(os.path.join(self.bin, "ssh"), 'while [ "$1" = -o ]; do shift 2; done\nshift\neval "$1"\n')
        R.script(os.path.join(self.bin, "delega"), f'PYTHONPATH={SRC} exec {sys.executable} -m delega "$@"\n')
        self.pwn = os.path.join(self.tmp.name, "pwn")

    def rm(self, *args, owner="dueno"):
        env = dict(os.environ, PYTHONPATH=SRC, DELEGA_RELEVO_OWNER=owner, DELEGA_RELEVO_ORG="trycore",
                   DELEGA_RELEVO_TASK="t1")
        r = subprocess.run([sys.executable, "-m", "delega", "relevo-mem", *args], capture_output=True, text=True,
                           env=env)
        return r.returncode, r.stdout, r.stderr

    def test_it_works_through_the_ssh_and_writes_with_the_daemon_owner(self):
        rc, out, err = self.rm("event", "relevo.nota", "--task", "t1", "--datos", "{}")
        self.assertEqual(rc, 0, err + out)
        rc, out, err = self.rm("log", "--task", "t1")
        self.assertEqual(rc, 0, err)
        self.assertEqual([(e["que"], e["quien"]) for e in json.loads(out)["eventos"] if e["que"] == "relevo.nota"],
                         [("relevo.nota", "dueno")])
        self.assertEqual(self.rm("export")[0], 0)
        self.assertEqual(self.rm("next", "f1")[0], 0)
        self.assertEqual(self.rm("set", "t1", "--estado", "bloqueada")[0], 0)

    def test_shell_text_of_the_claude_is_never_executed(self):
        self.assertFalse(os.path.exists(self.pwn))
        self.rm("export", f"; touch {self.pwn}")
        self.rm("set", "t1", "--estado", "bloqueada", "--error", f"$(touch {self.pwn})")
        self.rm("event", f"relevo.x; touch {self.pwn}", "--task", "t1", "--datos", f"`touch {self.pwn}`")
        self.rm("log", "--flow", f"f1 && touch {self.pwn}")
        self.assertFalse(os.path.exists(self.pwn))

    def test_other_orders_and_fixed_options_are_refused_with_rc_2(self):
        for args in (("promote", "--from", "x", "--yes"), ("unfence",), ("claim", "orquestador"), ("init",),
                     ("backup", "--to", "/tmp/x"), ("add-task",), (), ("export", "--out", "/tmp/x"),
                     ("log", "--db", "x"), ("log", "--db=x"), ("next", "f1", "--org", "otra"),
                     ("set", "t1", "--estado", "ok", "--owner", "otro")):
            self.assertEqual(self.rm(*args)[0], 2, args)

    def test_set_and_event_only_touch_the_escalated_task_and_safe_values(self):     # R10
        R.mem_cli("add-task", "--flow", "f1", "--brief", "b2", "--id", "t2", "--db", self.db)
        for args in (("set", "t2", "--estado", "bloqueada"),                  # another task
                     ("set", "t1", "--estado", "hecha"), ("set", "t1", "--estado", "ok"),
                     ("set", "t1", "--estado", "bloqueada", "--forzar"),
                     ("set", "t1", "--estado", "bloqueada", "--job-id", "j"), ("set", "t1", "--estado", "bloqueada", "--rama", "x"),
                     ("set", "t1", "--estado", "bloqueada", "--lanzador", "h"), ("set", "t1", "--estado", "bloqueada", "--intentos", "0"),
                     ("event", "wave.sha", "--task", "t1"), ("event", "lanzador_responde", "--task", "t1"),
                     ("event", "relevo.x", "--task", "t2"), ("event", "relevo.x")):
            self.assertEqual(self.rm(*args)[0], 2, args)
        env = dict(os.environ, PYTHONPATH=SRC, DELEGA_RELEVO_OWNER="dueno", DELEGA_RELEVO_ORG="trycore")
        env.pop("DELEGA_RELEVO_TASK", None)
        r = subprocess.run([sys.executable, "-m", "delega", "relevo-mem", "set", "t1", "--estado", "bloqueada"],
                           capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 2)                                     # no escalated task: no writes
        self.assertEqual(relevo.claude_env("dueno", "trycore", "t1")["DELEGA_RELEVO_TASK"], "t1")

    def test_a_fenced_owner_cannot_write_rc_9(self):
        self.assertEqual(R.mem_cli("claim", "orquestador", "--owner", "dueno", "--org", "trycore", "--db", self.db)[0], 0)
        copy = os.path.join(self.tmp.name, "copy.db")
        self.assertEqual(R.mem_cli("backup", "--to", copy, "--org", "trycore", "--db", self.db)[0], 0)
        self.assertEqual(self.rm("event", "relevo.antes", "--task", "t1")[0], 0)      # before: writes
        rc = R.mem_cli("promote", "--from", copy, "--org", "trycore", "--db", self.db, "--yes")[0]
        self.assertEqual(rc, 0)
        self.assertEqual(self.rm("set", "t1", "--estado", "bloqueada")[0], 9)
        self.assertEqual(self.rm("event", "relevo.x", "--task", "t1")[0], 9)
        self.assertEqual(self.rm("event", "relevo.y", "--task", "t1", owner="otro")[0], 0)

    def test_the_claude_argv_has_no_ssh_and_only_relevo_mem(self):
        cfg = config.load()
        access = relevo.mem_access(cfg, "trycore", "dueno")
        argv = relevo.claude_argv("b", None, access["tool"])
        self.assertFalse([a for a in argv if "ssh" in a.split()[:1] or a.startswith("Bash(ssh")])
        allowed = argv[argv.index("--allowedTools") + 1:argv.index("--disallowedTools")]
        self.assertEqual(allowed, ["Read", "Grep", "Glob", "Bash(delega relevo-mem *)"])
        for forbidden in ("Bash(git *)", "Bash(gh *)", "Edit", "Write"):
            self.assertIn(forbidden, argv)
        env = relevo.claude_env("dueno", "trycore")
        self.assertEqual((env["DELEGA_RELEVO_OWNER"], env["DELEGA_RELEVO_ORG"]), ("dueno", "trycore"))
        self.assertNotIn("ssh", relevo.brief_for({"id": "t1", "flujo": "f1", "brief": "b", "estado": "bloqueada",
                                                   "intentos": 0}, "x", {}, access).replace("SSH_AUTH_SOCK", ""))


class Wave(D.DaemonCase):
    def setUp(self):
        super().setUp()
        self.env = wave.Env(self.repo, org="trycore")

    def add_task(self, tid, **kw):
        return D.mem_cli("add-task", "--flow", "f1", "--brief", "do " + tid, "--id", tid, "--org", "trycore",
                         "--db", self.db, *sum(([f"--{k}", v] for k, v in kw.items()), []))

    def events(self, que):
        c = sqlite3.connect(self.db)
        try:
            return [json.loads(r[0]) for r in c.execute("SELECT datos FROM eventos WHERE que = ?", (que,))]
        finally:
            c.close()

    def force(self, tid, estado, **kw):
        args = sum(([f"--{k.replace('_', '-')}", v] for k, v in kw.items()), [])
        rc, _, err = D.mem_cli("set", tid, "--estado", estado, "--forzar", "--org", "trycore", "--db", self.db, *args)
        self.assertEqual(rc, 0, err)

    def step(self):
        conn = mem.connect(self.db)
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                wave.step(conn, "f1", self.env, org="trycore", lanzador="hosta")
        finally:
            conn.close()

    def test_e3_third_refusal_blocks_and_nothing_is_launched_again(self):
        self.add_task("t1")
        for n in (1, 2, 3):
            p = subprocess.Popen([sys.executable, "-c", "pass"])
            p.wait()
            self.force("t1", "arreglo", job_id=f"pend:{p.pid}:20261118-130000", rama="delega/q-f1-t1",
                       lanzador="hosta", intentos="2", desde="ok" if self.states()["t1"] == "ok" else "pendiente")
            with open(os.path.join(self.env._wave_dir(), "f1-t1.rc"), "w") as f:
                f.write("2\n")
            self.step()
        self.assertEqual(self.states()["t1"], "bloqueada")
        self.assertEqual([e["motivo"] for e in self.events("necesita_decision")], ["lanzamiento_rechazado"])
        with mock.patch.object(wave.Env, "launch", side_effect=AssertionError("launched")) as launch:
            for _ in range(3):
                self.step()
        launch.assert_not_called()
        self.assertEqual(self.states()["t1"], "bloqueada")

    def states(self):
        c = sqlite3.connect(self.db)
        try:
            return dict(c.execute("SELECT id, estado FROM tareas").fetchall())
        finally:
            c.close()

    def test_e4_25_cycles_without_answer_are_max_events_one_decision_and_no_export(self):
        self.add_task("t1")
        self.force("t1", "lanzada", job_id="20261118-130000-aaa", rama="delega/q-f1-t1", lanzador="hosta")
        bindir = os.path.join(self.tmp.name, "bin")
        os.makedirs(bindir)
        R.script(os.path.join(bindir, "ssh"), "exit 255\n")
        client = queue.MemClient(self.cfg, "trycore", "a")
        fl = {"id": "f1"}
        t = next(x for x in self.client_tasks() if x["id"] == "t1")
        real = mem.export
        with mock.patch.dict(os.environ, {"PATH": bindir + os.pathsep + os.environ["PATH"]}), \
                mock.patch.object(mem, "export", side_effect=real) as exp, \
                contextlib.redirect_stderr(io.StringIO()):
            for _ in range(25):
                self.assertIsNone(queue.task_state(self.cfg, client, fl, self.env, t))
        self.assertEqual(len(self.events(queue.FALLO_EVENT)), queue.LANZADOR_MAX_FALLOS)
        self.assertEqual([e["motivo"] for e in self.events("necesita_decision")], ["lanzador_caido"])
        exp.assert_not_called()

    def client_tasks(self):
        return self.client("a").m and [dict(zip(("id", "job_id", "lanzador"), r)) for r in
                                       sqlite3.connect(self.db).execute("SELECT id, job_id, lanzador FROM tareas")]

    def test_e2_registro_is_redacted_with_the_profile_terms(self):
        terms = os.path.join(self.tmp.name, "forbidden.txt")
        with open(terms, "w") as f:
            f.write("# terms\nacmecorp\n")
        with open(self.conf, "a") as f:
            f.write(f'\n[profiles.trycore.__x]\n')
        cfg_text = open(self.conf).read().replace('\n[profiles.trycore.__x]\n', '').replace(
            'owner = "self"\n[profiles.personal]', f'owner = "self"\nforbidden_terms_file = "{terms}"\n[profiles.personal]', 1)
        with open(self.conf, "w") as f:
            f.write(cfg_text)
        self.add_task("t1")
        self.force("t1", "lanzada", job_id="20261118-130000-aaa", rama="delega/q-f1-t1", lanzador="hosta")
        d = os.path.join(self.state, "20261118-130000-aaa")
        os.makedirs(d)
        with open(os.path.join(d, "meta"), "w") as f:
            f.write(f"BRANCH=delega/q-f1-t1\nEND=1\nREPO_DIR={self.repo}\n")
        with open(os.path.join(d, "stderr.log"), "w") as f:
            f.write("error at AcmeCorp internal host\n")
        env = wave.Env(self.repo, org="trycore")
        tail = env.log_tail({"id": "t1", "job_id": "20261118-130000-aaa", "rama": "delega/q-f1-t1",
                             "run_args": {"profile": "trycore"}})
        self.assertIn("[redacted]", tail)
        self.assertNotIn("acmecorp", tail.lower())


class RelevoMemLocal(RelevoMem):
    """The same rules on the in-process path (the base is on THIS host): no ssh at all."""
    CONF = R.NODE

    def test_local_writes_and_reads(self):
        self.assertEqual(self.rm("set", "t1", "--estado", "bloqueada")[0], 0)
        self.assertEqual(self.rm("set", "t1", "--estado", "pendiente", "--desde", "bloqueada")[0], 0)
        rc, out, _ = self.rm("log", "--task", "t1")
        self.assertEqual(rc, 0)
        self.assertTrue(any(e["quien"] == "dueno" for e in json.loads(out)["eventos"]))


if __name__ == "__main__":
    unittest.main()

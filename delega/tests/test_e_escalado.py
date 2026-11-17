"""E1–E4: a decision asked by the relay can really be taken (base reachable, `registro`, refused launches, persistent count)."""
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


def quiet():
    return contextlib.redirect_stdout(io.StringIO())


class Escalation(R.Base):
    """E1/E2: what the escalated Claude is told and allowed, with a fake `claude` and a fake `ssh`."""

    def setUp(self):
        super().setUp()
        self.assertEqual(R.mem_cli("init", "--org", "trycore", "--db", self.db)[0], 0)
        self.assertEqual(R.mem_cli("add-flow", "--repo", R.REMOTE, "--id", "f1", "--db", self.db)[0], 0)
        self.assertEqual(R.mem_cli("add-task", "--flow", "f1", "--brief", "b", "--id", "t1", "--db", self.db)[0], 0)
        self.assertEqual(R.mem_cli("set", "t1", "--estado", "bloqueada", "--forzar", "--db", self.db)[0], 0)
        self.event("necesita_decision", {"flujo": "f1", "motivo": "arreglo_fallido",
                                         "registro": "LOG-TAIL-OF-THE-FIX: assertion failed in test_x"})
        R.script(os.path.join(self.bin, "ssh"), 'while [ "$1" = -o ]; do shift 2; done\nshift\neval "$1"\n')
        R.script(os.path.join(self.bin, "delega"), f'PYTHONPATH={SRC} exec {sys.executable} -m delega "$@"\n')
        os.environ.pop("DELEGA_MEM_OWNER", None)

    def event(self, que, datos):
        self.assertEqual(R.mem_cli("event", que, "--task", "t1", "--datos", json.dumps(datos), "--db", self.db)[0], 0)

    def claude_line(self):
        lines = [l for l in self.log(2) if l.startswith(R.ASK)]
        self.assertEqual(len(lines), 1, lines)
        return lines[0]

    def asked(self, cfg):
        client = queue.MemClient(cfg, "trycore", "dueno")
        with quiet():
            self.assertEqual(relevo.escalate(cfg, client), ["t1"])
        return self.claude_line()

    def brief_text(self):
        with open(os.path.join(self.state, "relevo", "t1.md"), encoding="utf-8") as f:
            return f.read()

    def test_e1_remote_base_gets_the_ssh_prefix_and_the_exact_command(self):
        cfg = config.Config({"profiles": {"trycore": {"owner": "self"}},
                             "memoria": {"trycore": {"host": "orq@elsewhere", "path": self.db}},
                             "relevo": {"node": "n"}})
        line = self.asked(cfg)
        self.assertIn("Bash(ssh orq@elsewhere delega mem *)", line)
        self.assertNotIn("Bash(delega mem *)", line)
        for forbidden in ("Bash(git *)", "Bash(gh *)", "Edit", "Write"):
            self.assertIn(forbidden, line)
        argv = relevo.claude_argv("b", None, "Bash(ssh h delega mem *)")
        allowed = argv[argv.index("--allowedTools") + 1:argv.index("--disallowedTools")]
        self.assertEqual(allowed, ["Read", "Grep", "Glob", "Bash(ssh h delega mem *)"])
        text = self.brief_text()
        self.assertIn("ssh orq@elsewhere delega mem", text)
        self.assertIn(f"--db {self.db} --org trycore --owner dueno-claude", text)
        self.assertIn("LOG-TAIL-OF-THE-FIX", text)                      # E2: the brief carries the registro

    def test_e1_local_base_gets_the_local_path(self):
        cfg = config.load()
        line = self.asked(cfg)
        self.assertIn("Bash(delega mem *)", line)
        self.assertNotIn("ssh", line.replace("SSH_AUTH_SOCK", ""))
        text = self.brief_text()
        self.assertIn(f"delega mem", text)
        self.assertIn(f"--db {self.db} --org trycore --owner dueno-claude", text)
        self.assertNotIn("ssh ", text)

    def test_e1_owner_of_the_claude_passes_the_fence_through_the_ssh_command(self):
        # the exact command of the brief, run by the fake ssh, writes with the owner of the Claude
        env = relevo.claude_env("dueno-claude")
        self.assertEqual(env["DELEGA_MEM_OWNER"], "dueno-claude")
        r = subprocess.run(["ssh", "-o", "BatchMode=yes", "orq@elsewhere",
                            f"delega mem event nota --task t1 --datos '{{}}' --db {self.db} --org trycore "
                            f"--owner dueno-claude --json"], capture_output=True, text=True,
                           env=dict(os.environ, PYTHONPATH=SRC))
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        c = sqlite3.connect(self.db)
        try:
            self.assertEqual(c.execute("SELECT quien FROM eventos WHERE que = 'nota'").fetchone()[0], "dueno-claude")
        finally:
            c.close()
        self.assertNotIn("DELEGA_MEM_OWNER", os.environ)               # `--owner` does not leak out of mem.main


class Wave(D.DaemonCase):
    """E2/E3/E4 with the real `wave.step` / `queue` against a real database and real job directories."""

    def setUp(self):
        super().setUp()
        self.env = wave.Env(self.repo, org="trycore")

    def force(self, tid, estado, **kw):
        args = sum(([f"--{k.replace('_', '-')}", v] for k, v in kw.items()), [])
        rc, _, err = D.mem_cli("set", tid, "--estado", estado, "--forzar", "--org", "trycore", "--db", self.db, *args)
        self.assertEqual(rc, 0, err)

    def step(self):
        conn = mem.connect(self.db)
        try:
            with quiet():
                wave.step(conn, "f1", self.env, org="trycore", lanzador="hosta")
        finally:
            conn.close()

    def events(self, que):
        c = sqlite3.connect(self.db)
        try:
            return [json.loads(r[0]) for r in c.execute("SELECT datos FROM eventos WHERE que = ?", (que,))]
        finally:
            c.close()

    def job(self, name, rc, log):
        d = os.path.join(self.state, name)
        os.makedirs(d)
        with open(os.path.join(d, "meta"), "w") as f:
            f.write(f"BRANCH=delega/q-f1-t1\nEND=1\nREPO_DIR={self.repo}\n")
        with open(os.path.join(d, "summary.json"), "w") as f:
            f.write(json.dumps({"rc": rc}))
        with open(os.path.join(d, "progress.log"), "w") as f:
            f.write(log)

    def test_e2_failed_fix_carries_the_tail_of_the_fix_log(self):
        self.add_task("t1")
        self.force("t1", "arreglo", job_id="20261118-130000-aaa", rama="delega/q-f1-t1", lanzador="hosta")
        self.job("20261118-130000-aaa", 1, "step 1\nBOOM: the fix could not run the suite\n")
        self.step()
        self.assertEqual(self.states()["t1"], "bloqueada")
        ev = self.events("necesita_decision")
        self.assertEqual([e["motivo"] for e in ev], ["arreglo_fallido"])
        self.assertIn("BOOM: the fix could not run the suite", ev[0]["registro"])
        self.assertLessEqual(len(ev[0]["registro"]), wave.LOG_TAIL)

    def test_e2_without_a_log_the_registro_says_what_is_known(self):
        self.add_task("t1")
        self.force("t1", "lanzada", job_id="20261118-130000-zzz", rama="delega/q-f1-t1", lanzador="hosta")
        client = self.client("a")
        fl, t = {"id": "f1"}, {"id": "t1", "job_id": "20261118-130000-zzz", "lanzador": "hosta", "error": "boom"}
        with contextlib.redirect_stderr(io.StringIO()):
            queue._decide(client, fl, t, "job_sin_directorio", lanzador="hosta")
        reg = self.events("necesita_decision")[0]["registro"]
        for bit in ("20261118-130000-zzz", "hosta", "boom"):
            self.assertIn(bit, reg)

    def refused_arreglo(self):
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        p.wait()                                                          # a pid that is dead
        self.force("t1", "arreglo", job_id=f"pend:{p.pid}:20261118-130000", rama="delega/q-f1-t1",
                   lanzador="hosta", intentos="2", desde="ok" if self.states()["t1"] == "ok" else "pendiente")
        d = self.env._wave_dir()
        with open(os.path.join(d, "f1-t1.rc"), "w") as f:
            f.write("2\n")

    def test_e3_refused_fix_goes_back_to_ok_and_three_refusals_ask_once(self):
        self.add_task("t1")
        for n in (1, 2):
            self.refused_arreglo()
            self.step()
            self.assertEqual(self.states()["t1"], "ok", n)
            self.assertEqual(self.events("necesita_decision"), [])
        c = sqlite3.connect(self.db)
        try:
            self.assertEqual(c.execute("SELECT intentos FROM tareas WHERE id = 't1'").fetchone()[0], 1)
        finally:
            c.close()
        self.refused_arreglo()
        self.step()
        ev = self.events("necesita_decision")
        self.assertEqual([e["motivo"] for e in ev], ["lanzamiento_rechazado"])
        self.assertTrue(ev[0]["registro"])
        self.refused_arreglo()
        self.step()
        self.assertEqual(len(self.events("necesita_decision")), 1)        # asked once, ever

    def test_e3_refusal_for_an_open_branch_is_read_from_the_log_when_there_is_no_rc(self):
        from delega import i18n
        self.add_task("t1")
        self.refused_arreglo()
        d = self.env._wave_dir()
        os.remove(os.path.join(d, "f1-t1.rc"))
        with open(os.path.join(d, "f1-t1.log"), "w") as f:
            f.write(i18n.t("launch.err.branch_in_use", branch="delega/q-f1-t1", job="2026-x", path="/wt/x") + "\n")
        self.step()
        self.assertEqual(self.states()["t1"], "ok")

    def test_e3_a_fix_that_failed_for_real_is_still_a_failed_fix(self):
        self.add_task("t1")
        self.force("t1", "arreglo", job_id="20261118-130000-bbb", rama="delega/q-f1-t1", lanzador="hosta")
        self.job("20261118-130000-bbb", 1, "red\n")
        with open(os.path.join(self.env._wave_dir(), "f1-t1.rc"), "w") as f:
            f.write("1\n")
        self.step()
        self.assertEqual(self.states()["t1"], "bloqueada")

    def test_e4_the_count_of_unanswered_cycles_survives_two_daemons(self):
        self.add_task("t1")
        self.force("t1", "lanzada", job_id="20261118-130000-aaa", rama="delega/q-f1-t1", lanzador="hosta")
        fl = {"id": "f1"}
        t = {"id": "t1", "job_id": "20261118-130000-aaa", "lanzador": "hosta"}
        a, b = self.client("a"), self.client("b")          # two daemons: each restart is a new object
        with contextlib.redirect_stderr(io.StringIO()):
            for i in range(queue.LANZADOR_MAX_FALLOS):
                self.assertEqual(self.events("necesita_decision"), [], i)
                queue._count_failure(a if i % 2 == 0 else b, fl, t, "hosta", "lanzador_caido")
        ev = self.events("necesita_decision")
        self.assertEqual([e["motivo"] for e in ev], ["lanzador_caido"])
        self.assertTrue(ev[0]["registro"])
        self.assertEqual(len(self.events(queue.FALLO_EVENT)), queue.LANZADOR_MAX_FALLOS)

    def test_e4_an_answer_of_the_launcher_starts_the_count_again(self):
        self.add_task("t1")
        t = {"id": "t1", "job_id": "x", "lanzador": "hosta"}
        c1, c2 = self.client("a"), self.client("b")
        with contextlib.redirect_stderr(io.StringIO()):
            for _ in range(queue.LANZADOR_MAX_FALLOS - 1):
                queue._count_failure(c1, {"id": "f1"}, t, "hosta", "lanzador_caido")
            self.assertEqual(queue._fallos(c2, t), queue.LANZADOR_MAX_FALLOS - 1)
            relevo._event(c2, queue.RESPONDE_EVENT, "t1", {})
            self.assertEqual(queue._fallos(c1, t), 0)
            queue._count_failure(c1, {"id": "f1"}, t, "hosta", "lanzador_caido")
        self.assertEqual(self.events("necesita_decision"), [])


if __name__ == "__main__":
    unittest.main()

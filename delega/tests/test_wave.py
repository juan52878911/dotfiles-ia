"""wave: plan / step / status over a real mem database; `delega run`, the job state and
`delega integrate` are replaced by a double (Env), so nothing is launched."""
import contextlib
import io
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from delega import mem, wave  # noqa: E402


class FakeEnv:
    def __init__(self):
        self.launches = []          # (task id, branch, brief)
        self.states = {}            # task id -> running | ok | fail   (default ok)
        self.integrations = []
        self.results = {}           # task id -> list of (rc, out), consumed in order

    def launch(self, task, brief, branch, flags):
        self.launches.append((task["id"], branch, brief))
        return f"j{len(self.launches)}"

    def job_state(self, task):
        return self.states.get(task["id"], "ok"), task["job_id"]

    def integrate(self, branch, into, node):
        tid = next(t for t, b, _ in self.launches if b == branch)
        self.integrations.append((tid, into, node))
        q = self.results.get(tid) or [(0, "ok")]
        return q.pop(0) if len(q) > 1 or tid not in self.results else q[0]


def tsv(rows):
    return "id\tbrief\tzona\tdeps\trun_args\n# comment\n" + "\n".join("\t".join(r) for r in rows) + "\n"


class WaveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "m.db")
        self.conn = mem.connect(self.db, create=True)
        mem.migrate(self.conn)
        mem.init_db(self.conn, "personal")
        mem.add_flow(self.conn, repo="owner/repo", branch="main", flow_id="f")
        self.env = FakeEnv()

    def load(self, rows, **kw):
        p = os.path.join(self.tmp.name, "ola.tsv")
        with open(p, "w") as f:
            f.write(tsv(rows))
        return wave.plan(self.conn, "f", p, **kw)

    def states(self):
        return {r["id"]: r["estado"] for r in self.conn.execute("SELECT id, estado FROM tareas")}

    def step(self, **kw):
        return wave.step(self.conn, "f", self.env, say=lambda *_: None, **kw)

    def events(self, que):
        return [e for e in mem.log_events(self.conn, limit=0, flow="f") if e["que"] == que]

    def three(self):
        self.load([("a", "do a\\nline", "src/a/**", "", ""), ("b", "do b", "src/b/**", "", '{"profile":"personal"}'),
                   ("c", "do c", "src/c.py", "", "")])

    def test_green_wave_ends_all_hecha_without_fixes(self):
        self.three()
        for _ in range(6):
            self.step(par=3)
        self.assertEqual(self.states(), {"a": "hecha", "b": "hecha", "c": "hecha"})
        self.assertEqual(len(self.env.launches), 3)                 # no fix job
        self.assertEqual([i[0] for i in self.env.integrations], ["a", "b", "c"])
        self.assertIn("do a\nline", self.env.launches[0][2])

    def test_par_limits_in_flight(self):
        self.three()
        self.env.states = {t: "running" for t in "abc"}
        self.step(par=2)
        self.assertEqual(len(self.env.launches), 2)
        self.step(par=2)
        self.assertEqual(len(self.env.launches), 2)

    def test_one_red_launches_exactly_one_fix(self):
        self.three()
        self.env.results = {"b": [(7, "suite log: boom"), (0, "ok")]}
        for _ in range(8):
            self.step(par=3)
        self.assertEqual(self.states(), {"a": "hecha", "b": "hecha", "c": "hecha"})
        fixes = [l for l in self.env.launches if l[0] == "b"]
        self.assertEqual(len(fixes), 2)
        self.assertIn("suite log: boom", fixes[1][2])
        self.assertEqual(fixes[0][1], fixes[1][1])                  # same branch

    def test_two_reds_block_and_ask_for_decision(self):
        self.three()
        self.env.results = {"b": [(7, "red 1"), (7, "red 2")]}
        for _ in range(8):
            self.step(par=3)
        self.assertEqual(self.states()["b"], "bloqueada")
        self.assertEqual(len([l for l in self.env.launches if l[0] == "b"]), 2)
        self.assertEqual(len(self.events("necesita_decision")), 1)

    def test_conflict_blocks_with_files(self):
        self.three()
        self.env.results = {"a": [(8, "conflict\n  src/a/x.py\n  src/a/y.py")]}
        for _ in range(4):
            self.step(par=3)
        self.assertEqual(self.states()["a"], "bloqueada")
        self.assertEqual(self.events("necesita_decision")[0]["datos"]["ficheros"], ["src/a/x.py", "src/a/y.py"])

    def test_failed_job_is_rojo(self):
        self.three()
        self.env.states = {"a": "fail"}
        self.step(par=3)
        self.step(par=3)
        self.assertEqual(self.states()["a"], "rojo")
        self.assertEqual(self.env.integrations, [])

    def test_overlapping_zones_reject_whole_wave(self):
        with self.assertRaises(mem.MemError):
            self.load([("a", "x", "src/**", "", ""), ("b", "y", "src/b/*.py", "", ""), ("c", "z", "docs/**", "", "")])
        self.assertEqual(self.states(), {})
        out = io.StringIO()
        p = os.path.join(self.tmp.name, "ola.tsv")
        with contextlib.redirect_stderr(out):
            rc = wave.main(["plan", "f", p, "--db", self.db])
        self.assertEqual(rc, 2)

    def test_cli_overlap_rc2_writes_nothing(self):
        p = os.path.join(self.tmp.name, "o.tsv")
        with open(p, "w") as f:
            f.write(tsv([("a", "x", "src/**", "", ""), ("b", "y", "src/b.py", "", "")]))
        with contextlib.redirect_stderr(io.StringIO()):
            rc = wave.main(["plan", "f", p, "--db", self.db])
        self.assertEqual(rc, 2)
        self.assertEqual(self.states(), {})

    def test_disjoint_zones(self):
        self.assertFalse(wave.zones_overlap("src/a/**", "src/ab/**"))
        self.assertFalse(wave.zones_overlap("src/a.py", "src/b/**"))
        self.assertTrue(wave.zones_overlap("src/a/**", "src/a/x.py"))
        self.assertTrue(wave.zones_overlap("**/x.py", "src/y/**"))
        self.assertTrue(wave.zones_overlap("", "src/y"))

    def test_step_twice_does_not_relaunch(self):
        self.three()
        self.env.states = {t: "running" for t in "abc"}
        self.step(par=3)
        self.step(par=3)
        self.assertEqual(sorted(l[0] for l in self.env.launches), ["a", "b", "c"])

    def test_second_wave_waits_for_first(self):
        self.three()
        self.load([("d", "do d", "src/d/**", "a", "")])
        self.step(par=5)
        self.assertEqual({l[0] for l in self.env.launches}, {"a", "b", "c"})
        for _ in range(6):
            self.step(par=5)
        self.assertEqual(set(self.states().values()), {"hecha"})

    def test_status_table(self):
        self.three()
        tasks, lines = wave.status_table(self.conn, "f")
        self.assertEqual(len(tasks), 3)
        self.assertIn("0/3", lines[0])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(wave.main(["status", "f", "--db", self.db]), 0)
        self.assertIn("pendiente", out.getvalue())


if __name__ == "__main__":
    unittest.main()

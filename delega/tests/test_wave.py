"""wave: plan / step / status over a real mem database; `delega run`, the job state and
`delega integrate` are replaced by a double (Env), so nothing is launched."""
import contextlib
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

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

    def integrate(self, branch, into, node, sha=None):
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
        return wave.plan(self.conn, "f", p, org="personal", **kw)

    def states(self):
        return {r["id"]: r["estado"] for r in self.conn.execute("SELECT id, estado FROM tareas")}

    def step(self, **kw):
        return wave.step(self.conn, "f", self.env, say=lambda *_: None, org="personal", **kw)

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

    def test_no_verify_blocks_with_a_decision_and_no_fix_job(self):    # relevo pilot 5
        self.three()
        self.env.results = {"a": [(12, "this project has no verify commands")]}
        for _ in range(4):
            self.step(par=3)
        self.assertEqual(self.states()["a"], "bloqueada")
        self.assertEqual([l[0] for l in self.env.launches].count("a"), 1)          # no fix job
        ev = self.events("necesita_decision")
        self.assertEqual([e["datos"]["motivo"] for e in ev], ["sin_verify"])

    def test_failed_job_retries_once_as_fix_then_blocks(self):          # finding 14
        self.three()
        self.env.states = {"a": "fail"}
        self.step(par=3)
        self.step(par=3)                     # reconcile: rojo, then the retry (rojo -> arreglo)
        self.assertEqual(self.states()["a"], "arreglo")
        self.assertEqual(len([l for l in self.env.launches if l[0] == "a"]), 2)
        self.step(par=3)                     # the retry fails too
        self.assertEqual(self.states()["a"], "bloqueada")
        self.assertEqual(len(self.events("necesita_decision")), 1)
        self.assertEqual(self.env.integrations, [])

    def test_rojo_with_two_attempts_blocks_without_launching(self):    # finding 14
        self.three()
        for t in ("a", "b", "c"):
            self.env.states[t] = "running"
        self.step(par=3)
        mem.set_task(self.conn, "a", "rojo", force=True, intentos=2, org="personal")
        n = len(self.env.launches)
        self.step(par=3)
        self.assertEqual(self.states()["a"], "bloqueada")
        self.assertEqual(len(self.env.launches), n)
        self.assertEqual(self.events("necesita_decision")[0]["datos"]["motivo"], "rojo_repetido")

    def test_rc11_retries_next_step_without_conflict(self):            # finding 6
        self.three()
        self.env.results = {"a": [(11, "target moved"), (0, "ok")]}
        for _ in range(2):
            self.step(par=3)
        self.assertEqual(self.states()["a"], "ok")
        self.assertEqual(self.events("necesita_decision"), [])
        for _ in range(4):
            self.step(par=3)
        self.assertEqual(set(self.states().values()), {"hecha"})

    def test_fix_is_claimed_before_launching(self):                    # finding 7
        self.three()
        orig = self.env.integrate

        def racing(branch, into, node, sha=None):          # another orchestrator takes the task meanwhile
            rc = orig(branch, into, node)
            if rc[0] == 7:
                mem.set_task(self.conn, "b", "arreglo", desde="ok", force=True, intentos=2, org="personal")
            return rc
        self.env.integrate = racing
        self.env.results = {"b": [(7, "red"), (0, "ok")]}
        for _ in range(3):
            self.step(par=3)
        self.assertEqual(len([l for l in self.env.launches if l[0] == "b"]), 1)   # no second job

    def test_integrates_the_sha_the_job_brought(self):                 # finding 8
        self.three()
        self.env.job_state = lambda t: ("ok", t["job_id"], "sha-" + t["id"])
        seen = []
        self.env.integrate = lambda b, i, n, sha=None: (seen.append((b, sha)), (0, "ok"))[1]
        for _ in range(4):
            self.step(par=3)
        self.assertEqual([s for _, s in seen], ["sha-a", "sha-b", "sha-c"])
        self.assertTrue(all(b.startswith("delega/wave-") for b, _ in seen))   # finding 10: the branch names the job

    def test_overlapping_zones_reject_whole_wave(self):
        with self.assertRaises(mem.MemError):
            self.load([("a", "x", "src/**", "", ""), ("b", "y", "src/b/*.py", "", ""), ("c", "z", "docs/**", "", "")])
        self.assertEqual(self.states(), {})
        out = io.StringIO()
        p = os.path.join(self.tmp.name, "ola.tsv")
        with contextlib.redirect_stderr(out):
            rc = wave.main(["plan", "f", p, "--db", self.db, "--org", "personal"])
        self.assertEqual(rc, 2)

    def test_cli_overlap_rc2_writes_nothing(self):
        p = os.path.join(self.tmp.name, "o.tsv")
        with open(p, "w") as f:
            f.write(tsv([("a", "x", "src/**", "", ""), ("b", "y", "src/b.py", "", "")]))
        with contextlib.redirect_stderr(io.StringIO()):
            rc = wave.main(["plan", "f", p, "--db", self.db, "--org", "personal"])
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
            self.assertEqual(wave.main(["status", "f", "--db", self.db, "--org", "personal"]), 0)
        self.assertIn("pendiente", out.getvalue())


# ── the real Env, with a fake `delega run` ──────────────────────────────────────────────
class FakeCfg:
    def __init__(self, d):
        self.state_dir = d


def git(d, *a):
    return subprocess.run(["git", "-C", d, *a], capture_output=True, text=True, check=True).stdout.strip()


class RealEnvTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = os.path.join(self.tmp.name, "state")
        os.makedirs(self.state)
        self.repo = os.path.join(self.tmp.name, "repo")
        os.makedirs(self.repo)
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "x")
        # a fake interpreter: `<fake> -m delega run <repo> <brief> --branch <b>`
        self.fake = os.path.join(self.tmp.name, "fake-python")
        self.rcfile = os.path.join(self.tmp.name, "fake-rc")
        with open(self.fake, "w") as f:
            f.write('#!/bin/sh\n[ -f "$5" ] || exit 99\ncp "$5" "%s/seen.md"\n'
                    'echo "$DELEGA_FROM_QUEUE" > "%s/queue_env"\nexit $(cat "%s")\n'
                    % (self.tmp.name, self.tmp.name, self.rcfile))
        os.chmod(self.fake, os.stat(self.fake).st_mode | stat.S_IXUSR)
        self.env = wave.Env(self.repo, org="personal")
        self.env._cfg = FakeCfg(self.state)

    def launch(self, rc, brief="do it\nplease"):
        with open(self.rcfile, "w") as f:
            f.write(str(rc))
        task = {"id": "a", "flujo": "f", "rama": "delega/x", "job_id": ""}
        with mock.patch.object(wave.sys, "executable", self.fake):
            jid = self.env.launch(task, brief, "delega/x", [])
        task["job_id"] = jid
        for _ in range(100):
            if not wave._pid_alive(int(jid.split(":")[1])):
                break
            time.sleep(0.05)
        return task

    def test_launch_passes_a_brief_file_and_queue_flag(self):          # findings 1, 5
        task = self.launch(0)
        with open(os.path.join(self.tmp.name, "seen.md")) as f:
            self.assertEqual(f.read(), "do it\nplease")
        with open(os.path.join(self.tmp.name, "queue_env")) as f:
            self.assertEqual(f.read().strip(), "1")
        self.assertTrue(os.path.isfile(os.path.join(self.state, "wave", "personal", "f-a.md")))
        self.assertEqual(self.env.job_state(task)[0], "fail")          # no job dir, rc 0 -> nothing came back

    def test_rc6_means_queued_and_the_task_goes_back_to_pendiente(self):   # finding 5
        task = self.launch(6)
        self.assertEqual(self.env.job_state(task)[0], "queued")
        conn = mem.connect(os.path.join(self.tmp.name, "m.db"), create=True)
        mem.migrate(conn)
        mem.init_db(conn, "personal")
        mem.add_flow(conn, repo="owner/repo", branch="main", flow_id="f")
        mem.add_task(conn, "f", "x", task_id="a", org="personal")
        env = self.env
        with mock.patch.object(wave.sys, "executable", self.fake):
            wave.step(conn, "f", env, say=lambda *_: None, org="personal")   # launches (rc 6)
        for _ in range(100):
            t = mem._task_dict(conn.execute("SELECT * FROM tareas WHERE id='a'").fetchone())
            if not wave._pid_alive(int(t["job_id"].split(":")[1])):
                break
            time.sleep(0.05)
        self.assertEqual(t["estado"], "lanzada")
        with mock.patch.object(wave.sys, "executable", self.fake):
            wave.step(conn, "f", env, say=lambda *_: None, org="personal", par=0)
        t = mem._task_dict(conn.execute("SELECT * FROM tareas WHERE id='a'").fetchone())
        self.assertEqual((t["estado"], t["intentos"]), ("pendiente", 0))

    def test_rc0_without_a_commit_is_not_done(self):                   # relevo pilot: a hook refused the commit
        jd = os.path.join(self.tmp.name, "job")
        os.makedirs(jd)
        head = git(self.repo, "rev-parse", "HEAD")
        with open(os.path.join(jd, "meta"), "w") as f:
            f.write(f"BASE={head}\nEND=1\n")
        with open(os.path.join(jd, "summary.json"), "w") as f:
            f.write('{"rc": 0}')
        task = {"id": "a", "flujo": "f", "rama": "delega/x", "job_id": "J1"}
        with mock.patch.object(self.env, "_job_dir", lambda t: jd), \
                mock.patch("delega.jobs.job_id", lambda d: "J1"):
            with mock.patch.object(self.env, "_fetched_sha", lambda *a: head):
                self.assertEqual(self.env.job_state(task)[0], "fail")       # the branch is the base
            git(self.repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "y")
            new = git(self.repo, "rev-parse", "HEAD")
            with mock.patch.object(self.env, "_fetched_sha", lambda *a: new):
                self.assertEqual(self.env.job_state(task), ("ok", "J1", new))

    def test_fetched_sha_needs_a_clean_fetch_log(self):                # finding 8
        jd = os.path.join(self.tmp.name, "job")
        os.makedirs(jd)
        head = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "branch", "delega/x")
        self.assertIsNone(self.env._fetched_sha(jd, "J1", "delega/x"))           # no fetch.log
        with open(os.path.join(jd, "fetch.log"), "w") as f:
            f.write("fatal: could not read\n")
        self.assertIsNone(self.env._fetched_sha(jd, "J1", "delega/x"))           # failed fetch
        open(os.path.join(jd, "fetch.log"), "w").close()
        self.assertEqual(self.env._fetched_sha(jd, "J1", "delega/x"), head)
        git(self.repo, "update-ref", "refs/delega/J1", head)
        with open(os.path.join(jd, "fetch.log"), "w") as f:
            f.write("branch is checked out\n")
        self.assertEqual(self.env._fetched_sha(jd, "J1", "delega/zzz"), head)    # came to the ref

    def test_repo_dir_must_match_the_flow(self):                       # finding 9
        conn = mem.connect(os.path.join(self.tmp.name, "m.db"), create=True)
        mem.migrate(conn)
        mem.init_db(conn, "personal")
        mem.add_flow(conn, repo="owner/repo", branch="main", flow_id="f")
        with self.assertRaises(mem.MemError):                          # no origin
            wave.check_repo_dir(conn, "f", self.repo)
        git(self.repo, "remote", "add", "origin", "git@github.com:Owner/other.git")
        with self.assertRaises(mem.MemError):
            wave.check_repo_dir(conn, "f", self.repo)
        git(self.repo, "remote", "set-url", "origin", "git@github.com:Owner/repo.git")
        wave.check_repo_dir(conn, "f", self.repo)
        git(self.repo, "remote", "set-url", "origin", "https://github.com/owner/repo")
        wave.check_repo_dir(conn, "f", self.repo)
        db = os.path.join(self.tmp.name, "m.db")
        git(self.repo, "remote", "set-url", "origin", "git@github.com:o/else.git")
        with contextlib.redirect_stderr(io.StringIO()):
            rc = wave.main(["step", "f", "--db", db, "--org", "personal", "--repo-dir", self.repo])
        self.assertEqual(rc, 2)

    def test_profile_of_another_org_is_rc2(self):                      # finding 9
        conn = mem.connect(os.path.join(self.tmp.name, "m.db"), create=True)
        mem.migrate(conn)
        mem.init_db(conn, "personal")
        mem.add_flow(conn, repo="owner/repo", branch="main", flow_id="f")
        mem.add_task(conn, "f", "x", task_id="a", org="personal")
        conn.execute("UPDATE tareas SET run_args = ? WHERE id = 'a'", (json.dumps({"profile": "zzz-otra"}),))
        conn.commit()
        with mock.patch.object(mem, "_profile_org", lambda p: "otra" if p else None):
            with self.assertRaises(mem.MemError):
                wave.check_profiles(conn, "f", "personal")


if __name__ == "__main__":
    unittest.main()

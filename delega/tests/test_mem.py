"""mem: the central orchestration memory in SQLite (flows, tasks, leases, append-only log).

The tests that need a second process use `multiprocessing` (spawn): the child runs the same
CLI, so it claims the lease with another pid, exactly like two orchestrators would."""
import contextlib
import io
import json
import multiprocessing
import os
import sqlite3
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))   # run without PYTHONPATH

from delega import mem  # noqa: E402


def _run(args, q=None):
    """Run the CLI in this process; returns (rc, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = mem.main(list(args)) or 0
    if q is not None:
        q.put((rc, out.getvalue(), err.getvalue()))
    return rc, out.getvalue(), err.getvalue()


def _claim_child(db, rol, ttl, barrier, q):
    """Child: wait for the other child (so both claim at the same time) and claim the lease."""
    if barrier is not None:
        try:
            barrier.wait(timeout=30)
        except threading.BrokenBarrierError:
            pass
    _run(["claim", rol, "--ttl", str(ttl), "--db", db, "--json"], q)


def _add_task_child(db, flow, brief, q):
    _run(["add-task", "--flow", flow, "--brief", brief, "--db", db, "--json"], q)


def _export_child(db, q):
    _run(["export", "--db", db, "--json"], q)


class MemTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "memoria.db")
        self.ctx = multiprocessing.get_context("spawn")

    # ── helpers ─────────────────────────────────────────────────────────────
    def init(self, org="trycore"):
        rc, out, err = _run(["init", "--org", org, "--db", self.db, "--json"])
        self.assertEqual(rc, 0, err)
        return json.loads(out)

    def flow(self, flow_id="f1", repo="git@github.com:acme/app.git", **kw):
        args = ["add-flow", "--repo", repo, "--id", flow_id, "--db", self.db, "--json"]
        for k, v in kw.items():
            args += ["--" + k.replace("_", "-"), str(v)]
        rc, out, err = _run(args)
        self.assertEqual(rc, 0, err)
        return json.loads(out)["flujo"]

    def task(self, flow="f1", brief="do it", task_id=None, **kw):
        args = ["add-task", "--flow", flow, "--brief", brief, "--db", self.db, "--json"]
        if task_id:
            args += ["--id", task_id]
        for k, v in kw.items():
            args += ["--" + k.replace("_", "-"), str(v)]
        rc, out, err = _run(args)
        self.assertEqual(rc, 0, err)
        return json.loads(out)["tarea"]

    def kid(self, target, *args):
        """Run a function in another process; returns (rc, stdout, stderr)."""
        q = self.ctx.Queue()
        p = self.ctx.Process(target=target, args=(*args, q))
        p.start()
        try:
            res = q.get(timeout=90)
        finally:
            p.join(30)
            if p.is_alive():
                p.terminate()
        return res

    # ── the lease: two processes, one winner ────────────────────────────────
    def test_two_processes_claim_the_same_lease_and_only_one_wins(self):
        self.init()
        q = self.ctx.Queue()
        barrier = self.ctx.Barrier(2)
        kids = [self.ctx.Process(target=_claim_child, args=(self.db, "orquestador", 60, barrier, q))
                for _ in range(2)]
        for k in kids:
            k.start()
        results = [q.get(timeout=90) for _ in kids]
        for k in kids:
            k.join(30)
        rcs = sorted(r[0] for r in results)
        self.assertEqual(rcs, [0, 9], results)                      # exactly one takes the lease
        loser = next(r for r in results if r[0] == 9)
        self.assertIn("held by", loser[2])                           # the loser says who has it
        conn = mem.connect(self.db)
        try:
            row = conn.execute("SELECT dueno FROM leases WHERE rol = 'orquestador'").fetchone()
            self.assertTrue(row["dueno"])
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        finally:
            conn.close()

    def test_four_processes_contending_for_one_lease(self):
        """The hypothesis of the module: BEGIN IMMEDIATE + busy_timeout serialize the writers."""
        self.init()
        q = self.ctx.Queue()
        barrier = self.ctx.Barrier(4)
        kids = [self.ctx.Process(target=_claim_child, args=(self.db, "orquestador", 60, barrier, q))
                for _ in range(4)]
        for k in kids:
            k.start()
        results = [q.get(timeout=90) for _ in kids]
        for k in kids:
            k.join(30)
        self.assertEqual(sorted(r[0] for r in results), [0, 9, 9, 9], results)
        conn = mem.connect(self.db)
        try:
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM leases").fetchone()[0], 1)
        finally:
            conn.close()

    def test_the_lease_of_another_owner_is_not_stolen(self):
        self.init()
        conn = mem.connect(self.db)
        try:
            first = mem.claim_lease(conn, "orq", 60, owner="host:1")
            self.assertEqual(first["estado"], "tomada")
            other = mem.claim_lease(conn, "orq", 60, owner="host:2")
            self.assertEqual(other["estado"], "ocupada")
            self.assertEqual(other["dueno"], "host:1")
            again = mem.claim_lease(conn, "orq", 60, owner="host:1")     # the owner renews it
            self.assertEqual(again["estado"], "renovada")
        finally:
            conn.close()

    def test_an_expired_lease_is_taken_by_another(self):
        self.init()
        conn = mem.connect(self.db)
        try:
            first = mem.claim_lease(conn, "orq", 0, owner="host:1")      # expires at once
            self.assertEqual(first["estado"], "tomada")
            second = mem.claim_lease(conn, "orq", 60, owner="host:2")
            self.assertEqual(second["estado"], "tomada")
            self.assertEqual(second["dueno"], "host:2")
        finally:
            conn.close()

    def test_an_expired_lease_is_taken_by_another_process(self):
        self.init()
        rc, _, err = _run(["claim", "orq", "--ttl", "0", "--db", self.db])
        self.assertEqual(rc, 0, err)
        rc, out, err = self.kid(_claim_child, self.db, "orq", 60, None)
        self.assertEqual(rc, 0, err)                                    # the child took the expired lease
        self.assertEqual(json.loads(out)["estado"], "tomada")
        self.assertNotEqual(json.loads(out)["dueno"], mem.owner_id())

    def test_release_and_heartbeat(self):
        self.init()
        rc, out, err = _run(["claim", "orq", "--ttl", "60", "--db", self.db, "--json"])
        self.assertEqual(rc, 0, err)
        rc, out, err = _run(["heartbeat", "orq", "--ttl", "90", "--db", self.db, "--json"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out)["estado"], "renovada")
        rc, out, err = _run(["release", "orq", "--db", self.db, "--json"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out)["estado"], "liberada")
        rc, _, err = _run(["heartbeat", "orq", "--db", self.db])          # there is no lease any more
        self.assertEqual(rc, 2)

    # ── the tasks: one process writes, another reads ────────────────────────
    def test_a_task_added_by_a_process_is_read_by_another(self):
        self.init()
        self.flow("f1")
        rc, out, err = self.kid(_add_task_child, self.db, "f1", "arregla el bug")
        self.assertEqual(rc, 0, err)
        added = json.loads(out)["tarea"]["id"]
        rc, out, err = _run(["next", "f1", "--db", self.db, "--json"])
        self.assertEqual(rc, 0, err)
        tasks = json.loads(out)["tareas"]
        self.assertEqual([t["id"] for t in tasks], [added])
        self.assertEqual(tasks[0]["brief"], "arregla el bug")
        self.assertEqual(tasks[0]["estado"], "pendiente")
        rc, out, err = self.kid(_export_child, self.db)                   # and another process reads it
        self.assertEqual(rc, 0, err)
        self.assertIn(added, [t["id"] for t in json.loads(out)["tareas"]])

    def test_next_respects_the_dependencies(self):
        self.init()
        self.flow("f1")
        self.task(flow="f1", brief="primero", task_id="t1")
        self.task(flow="f1", brief="segundo", task_id="t2", deps="t1")
        rc, out, err = _run(["next", "f1", "--db", self.db, "--json"])
        self.assertEqual(rc, 0, err)
        self.assertEqual([t["id"] for t in json.loads(out)["tareas"]], ["t1"])
        self.assertEqual([t["id"] for t in json.loads(out)["bloqueadas"]], ["t2"])
        rc, out, err = _run(["set", "t1", "--estado", "hecha", "--db", self.db, "--json"])
        self.assertEqual(rc, 0, err)
        rc, out, err = _run(["next", "f1", "--db", self.db, "--json"])
        self.assertEqual([t["id"] for t in json.loads(out)["tareas"]], ["t2"])

    def test_set_moves_a_task_and_counts_the_attempts(self):
        self.init()
        self.flow("f1")
        self.task(flow="f1", brief="primero", task_id="t1")
        rc, out, err = _run(["set", "t1", "--estado", "lanzada", "--job-id", "j1", "--nodo", "box",
                             "--rama", "delega/x", "--db", self.db, "--json"])
        self.assertEqual(rc, 0, err)
        t = json.loads(out)["tarea"]
        self.assertEqual((t["estado"], t["intentos"], t["job_id"], t["nodo"], t["rama"]),
                         ("lanzada", 1, "j1", "box", "delega/x"))
        rc, _, err = _run(["set", "t1", "--estado", "inventado", "--db", self.db])
        self.assertEqual(rc, 2)
        rc, _, err = _run(["set", "no-existe", "--estado", "hecha", "--db", self.db])
        self.assertEqual(rc, 2)

    def test_a_task_of_another_flow_is_not_ready(self):
        self.init()
        self.flow("f1")
        self.task(flow="f1", brief="primero", task_id="t1")
        rc, _out, _err = _run(["next", "f2", "--db", self.db])
        self.assertEqual(rc, 2)

    # ── the refusals ────────────────────────────────────────────────────────
    def test_a_local_path_is_refused_as_a_repo(self):
        self.init()
        for repo in ("/Users/juan/proyecto", "./repo", "../repo", "~/repo", "file:///tmp/x", os.getcwd()):
            rc, out, err = _run(["add-flow", "--repo", repo, "--db", self.db, "--json"])
            self.assertEqual(rc, 2, repo)
            self.assertFalse(json.loads(out)["ok"])
            self.assertIn("local path", err)
        for repo in ("git@github.com:acme/app.git", "https://github.com/acme/app.git", "acme/app"):
            rc, out, err = _run(["add-flow", "--repo", repo, "--db", self.db, "--json"])
            self.assertEqual(rc, 0, err)

    def test_another_org_is_refused(self):
        self.init("trycore")
        rc, out, err = _run(["add-flow", "--repo", "acme/app", "--org", "personal", "--db", self.db, "--json"])
        self.assertEqual(rc, 2)
        self.assertFalse(json.loads(out)["ok"])
        self.assertIn("personal", err)
        rc, _, err = _run(["init", "--org", "personal", "--db", self.db])
        self.assertEqual(rc, 2)
        conn = mem.connect(self.db)
        try:
            self.assertEqual(mem.get_org(conn), "trycore")
        finally:
            conn.close()

    def test_an_org_is_needed_to_init(self):
        rc, _, err = _run(["init", "--db", self.db])
        self.assertEqual(rc, 2)
        self.assertIn("--org", err)

    # ── the database itself ─────────────────────────────────────────────────
    def test_the_migration_is_idempotent(self):
        first = self.init("trycore")
        self.assertTrue(first["created"])
        second = self.init("trycore")
        self.assertFalse(second["created"])
        conn = mem.connect(self.db)
        try:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], mem.SCHEMA_VERSION)
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        finally:
            conn.close()

    def test_the_log_is_only_appended(self):
        self.init()
        self.flow("f1")
        conn = mem.connect(self.db)
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM eventos").fetchone()[0], 2)
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("UPDATE eventos SET que = 'x'")
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("DELETE FROM eventos")
        finally:
            conn.close()
        rc, out, err = _run(["log", "--db", self.db, "--json"])
        self.assertEqual(rc, 0, err)
        events = json.loads(out)["eventos"]
        self.assertEqual([e["que"] for e in events], ["mem.init", "flow.add"])

    def test_the_export_has_everything(self):
        self.init("trycore")
        self.flow("f1")
        self.task(flow="f1", brief="primero", task_id="t1")
        rc, out, err = _run(["export", "--db", self.db, "--json"])
        self.assertEqual(rc, 0, err)
        data = json.loads(out)
        self.assertEqual(data["org"], "trycore")
        self.assertEqual([f["id"] for f in data["flujos"]], ["f1"])
        self.assertEqual([t["id"] for t in data["tareas"]], ["t1"])
        self.assertEqual(data["tareas"][0]["deps"], [])
        self.assertTrue(data["leases"] == [])

    def test_the_memory_refuses_to_open_on_a_network_filesystem(self):
        self.assertEqual(mem.network_fs("/srv/mem/x.db", [("/", "nfs"), ("/srv", "nfs4")]), "nfs4")
        self.assertIsNone(mem.network_fs("/srv/mem/x.db", [("/", "apfs")]))
        self.assertIsNone(mem.network_fs("/srv/mem/x.db", []))

    def test_a_missing_database_is_refused(self):
        rc, _, err = _run(["next", "f1", "--db", os.path.join(self.tmp.name, "nope.db")])
        self.assertEqual(rc, 2)
        self.assertIn("init", err)

    def test_an_unknown_order_is_refused(self):
        rc, _, _err = _run(["inventada", "--db", self.db])
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()

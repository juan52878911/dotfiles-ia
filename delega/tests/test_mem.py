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
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))   # run without PYTHONPATH

from delega import mem  # noqa: E402


def setUpModule():
    # los mensajes que comprueban estas pruebas están en inglés; en los nodos DELEGA_LOCALE=es
    patcher = mock.patch.dict(os.environ, {"DELEGA_LOCALE": "en"})
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)


def _run(args, q=None):
    """Run the CLI in this process; returns (rc, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = mem.main(list(args)) or 0
    if q is not None:
        q.put((rc, out.getvalue(), err.getvalue()))
    return rc, out.getvalue(), err.getvalue()


@contextlib.contextmanager
def _without_env(*names):
    """Temporarily remove environment variables and restore them afterwards."""
    saved = {name: os.environ.get(name) for name in names}
    for name in names:
        os.environ.pop(name, None)
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _claim_child(db, rol, ttl, barrier, q):
    """Child: wait for the other child (so both claim at the same time) and claim the lease."""
    if barrier is not None:
        try:
            barrier.wait(timeout=30)
        except threading.BrokenBarrierError:
            pass
    owner = f"spawn:{os.getpid()}"
    _run(["claim", rol, "--ttl", str(ttl), "--owner", owner, "--org", "trycore",
          "--db", db, "--json"], q)


def _lease_child(db, command, rol, ttl, owner, org, q):
    """Run one lease order in its own process with a stable, explicit owner."""
    args = [command, rol]
    if command in ("claim", "heartbeat"):
        args += ["--ttl", str(ttl)]
    args += ["--owner", owner, "--org", org, "--db", db, "--json"]
    _run(args, q)


def _add_task_child(db, flow, brief, q):
    _run(["add-task", "--flow", flow, "--brief", brief, "--org", "trycore",
          "--db", db, "--json"], q)


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
        org = kw.pop("org", "trycore")
        args = ["add-flow", "--repo", repo, "--id", flow_id, "--org", org, "--db", self.db, "--json"]
        for k, v in kw.items():
            args += ["--" + k.replace("_", "-"), str(v)]
        rc, out, err = _run(args)
        self.assertEqual(rc, 0, err)
        return json.loads(out)["flujo"]

    def task(self, flow="f1", brief="do it", task_id=None, **kw):
        org = kw.pop("org", "trycore")
        args = ["add-task", "--flow", flow, "--brief", brief, "--org", org, "--db", self.db, "--json"]
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
            first = mem.claim_lease(conn, "orq", 60, owner="host:1")
            self.assertEqual(first["estado"], "tomada")
            conn.execute("UPDATE leases SET vence_en = 0 WHERE rol = 'orq'")
            second = mem.claim_lease(conn, "orq", 60, owner="host:2")
            self.assertEqual(second["estado"], "tomada")
            self.assertEqual(second["dueno"], "host:2")
        finally:
            conn.close()

    def test_an_expired_lease_is_taken_by_another_process(self):
        self.init()
        rc, _, err = _run(["claim", "orq", "--ttl", "60", "--owner", "host:1",
                           "--org", "trycore", "--db", self.db])
        self.assertEqual(rc, 0, err)
        conn = mem.connect(self.db)
        try:
            conn.execute("UPDATE leases SET vence_en = 0 WHERE rol = 'orq'")
        finally:
            conn.close()
        rc, out, err = self.kid(_claim_child, self.db, "orq", 60, None)
        self.assertEqual(rc, 0, err)                                    # the child took the expired lease
        self.assertEqual(json.loads(out)["estado"], "tomada")
        self.assertNotEqual(json.loads(out)["dueno"], mem.owner_id())

    def test_release_and_heartbeat(self):
        self.init()
        common = ["--owner", "orch:1", "--org", "trycore", "--db", self.db, "--json"]
        rc, out, err = _run(["claim", "orq", "--ttl", "60"] + common)
        self.assertEqual(rc, 0, err)
        rc, out, err = _run(["heartbeat", "orq", "--ttl", "90"] + common)
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out)["estado"], "renovada")
        rc, out, err = _run(["release", "orq"] + common)
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out)["estado"], "liberada")
        rc, _, err = _run(["heartbeat", "orq"] + common)                # there is no lease any more
        self.assertEqual(rc, 2)

    def test_lease_owner_is_stable_across_three_processes(self):
        """Claim, heartbeat and release are separate SSH processes but one logical owner."""
        self.init()
        owner = "ssh-worker:42"
        for command in ("claim", "heartbeat", "release"):
            rc, out, err = self.kid(_lease_child, self.db, command, "orq", 60, owner, "trycore")
            self.assertEqual(rc, 0, err)
            self.assertEqual(json.loads(out).get("dueno", owner), owner)
        conn = mem.connect(self.db)
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM leases").fetchone()[0], 0)
        finally:
            conn.close()

        with _without_env("DELEGA_MEM_OWNER"):
            rc, _, err = _run(["claim", "orq", "--org", "trycore", "--db", self.db])
        self.assertEqual(rc, 2)
        self.assertIn("--owner", err)

        with mock.patch.dict(os.environ, {"DELEGA_MEM_OWNER": owner}):
            rc, _, err = _run(["claim", "orq-env", "--org", "trycore", "--db", self.db])
        self.assertEqual(rc, 0, err)

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
        for old, new in (("pendiente", "lanzada"), ("lanzada", "ok"), ("ok", "hecha")):
            rc, out, err = _run(["set", "t1", "--estado", new, "--desde", old,
                                 "--org", "trycore", "--db", self.db, "--json"])
            self.assertEqual(rc, 0, err)
        rc, out, err = _run(["next", "f1", "--db", self.db, "--json"])
        self.assertEqual([t["id"] for t in json.loads(out)["tareas"]], ["t2"])

    def test_set_moves_a_task_and_counts_the_attempts(self):
        self.init()
        self.flow("f1")
        self.task(flow="f1", brief="primero", task_id="t1")
        rc, out, err = _run(["set", "t1", "--estado", "lanzada", "--job-id", "j1", "--nodo", "box",
                             "--rama", "delega/x", "--org", "trycore", "--db", self.db, "--json"])
        self.assertEqual(rc, 0, err)
        t = json.loads(out)["tarea"]
        self.assertEqual((t["estado"], t["intentos"], t["job_id"], t["nodo"], t["rama"]),
                         ("lanzada", 1, "j1", "box", "delega/x"))
        rc, _, err = _run(["set", "t1", "--estado", "inventado", "--org", "trycore", "--db", self.db])
        self.assertEqual(rc, 2)
        rc, _, err = _run(["set", "no-existe", "--estado", "hecha", "--org", "trycore", "--db", self.db])
        self.assertEqual(rc, 2)

    def test_set_uses_compare_and_set_and_valid_transitions(self):
        self.init()
        self.flow("f1")
        self.task(flow="f1", brief="primero", task_id="t1")

        rc, out, err = _run(["set", "t1", "--estado", "lanzada", "--desde", "pendiente",
                             "--org", "trycore", "--db", self.db, "--json"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out)["tarea"]["intentos"], 1)

        rc, out, err = _run(["set", "t1", "--estado", "rojo", "--desde", "pendiente",
                             "--org", "trycore", "--db", self.db, "--json"])
        self.assertEqual(rc, 9)
        self.assertIn("state changed", err)
        conn = mem.connect(self.db)
        try:
            row = conn.execute("SELECT estado, intentos FROM tareas WHERE id = 't1'").fetchone()
            self.assertEqual((row["estado"], row["intentos"]), ("lanzada", 1))
        finally:
            conn.close()

        for old, new in (("lanzada", "ok"), ("ok", "hecha")):
            rc, _, err = _run(["set", "t1", "--estado", new, "--desde", old,
                               "--org", "trycore", "--db", self.db, "--json"])
            self.assertEqual(rc, 0, err)
        rc, _, err = _run(["set", "t1", "--estado", "pendiente", "--desde", "hecha",
                           "--org", "trycore", "--db", self.db])
        self.assertEqual(rc, 2)
        rc, _, err = _run(["set", "t1", "--estado", "pendiente", "--desde", "hecha", "--forzar",
                           "--org", "trycore", "--db", self.db])
        self.assertEqual(rc, 0, err)

    def test_ok_can_go_to_arreglo_or_bloqueada_only_with_compare_and_set(self):
        self.init()
        self.flow("f1")
        self.task(flow="f1", brief="primero", task_id="t1")
        self.task(flow="f1", brief="segundo", task_id="t2")
        for task in ("t1", "t2"):
            for old, new in (("pendiente", "lanzada"), ("lanzada", "ok")):
                rc, _, err = _run(["set", task, "--estado", new, "--desde", old,
                                   "--org", "trycore", "--db", self.db, "--json"])
                self.assertEqual(rc, 0, err)

        rc, _, err = _run(["set", "t1", "--estado", "arreglo",
                           "--org", "trycore", "--db", self.db])
        self.assertEqual(rc, 2)
        rc, _, err = _run(["set", "t1", "--estado", "arreglo", "--desde", "ok",
                           "--org", "trycore", "--db", self.db, "--json"])
        self.assertEqual(rc, 0, err)
        rc, _, err = _run(["set", "t2", "--estado", "bloqueada", "--desde", "ok",
                           "--org", "trycore", "--db", self.db, "--json"])
        self.assertEqual(rc, 0, err)

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
            rc, out, err = _run(["add-flow", "--repo", repo, "--org", "trycore", "--db", self.db, "--json"])
            self.assertEqual(rc, 2, repo)
            self.assertFalse(json.loads(out)["ok"])
            self.assertIn("local path", err)
        for repo in ("git@github.com:acme/app.git", "https://github.com/acme/app.git", "acme/app"):
            rc, out, err = _run(["add-flow", "--repo", repo, "--org", "trycore", "--db", self.db, "--json"])
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
        with _without_env("DELEGA_MEM_ORG"):
            rc, _, err = _run(["init", "--db", self.db])
        self.assertEqual(rc, 2)
        self.assertIn("--org", err)

    def test_writes_require_an_org_and_use_an_org_scoped_default_database(self):
        self.init()
        writes = (
            ["init"],
            ["add-flow", "--repo", "acme/app"],
            ["add-task", "--flow", "f1", "--brief", "x"],
            ["set", "t1", "--estado", "lanzada"],
            ["claim", "orq"],
            ["heartbeat", "orq"],
            ["release", "orq"],
        )
        with _without_env("DELEGA_MEM_ORG"):
            for args in writes:
                rc, _, err = _run(args + ["--db", self.db])
                self.assertEqual(rc, 2, args)
                self.assertIn("--org", err, args)

        with mock.patch.dict(os.environ, {"DELEGA_MEM_ORG": "trycore"}):
            rc, _, err = _run(["add-flow", "--repo", "acme/app", "--id", "f-env",
                               "--db", self.db, "--json"])
        self.assertEqual(rc, 0, err)
        rc, _, err = _run(["add-flow", "--repo", "acme/app", "--org", "personal",
                           "--db", self.db])
        self.assertEqual(rc, 2)
        self.assertIn("personal", err)

        with (
            _without_env("DELEGA_MEM_DB", "DELEGA_MEM_ORG"),
            mock.patch.dict(os.environ, {"HOME": self.tmp.name}),
        ):
            rc, out, err = _run(["init", "--org", "acme", "--json"])
        self.assertEqual(rc, 0, err)
        expected = os.path.join(self.tmp.name, ".local", "state", "delega", "memoria-acme.db")
        self.assertEqual(json.loads(out)["db"], expected)
        self.assertTrue(os.path.exists(expected))

    # ── promotion: stale copies, epochs and open WAL connections ───────────
    def test_promote_fences_the_old_owner_and_blocks_work_started_after_the_copy(self):
        self.init()
        self.flow("f1")
        self.task(flow="f1", brief="después de la copia", task_id="t1")
        claim = ["claim", "orquestador", "--owner", "old-owner", "--org", "trycore",
                 "--db", self.db, "--json"]
        rc, _, err = _run(claim)
        self.assertEqual(rc, 0, err)
        copy = os.path.join(self.tmp.name, "copy.db")
        rc, _, err = _run(["backup", "--to", copy, "--org", "trycore", "--db", self.db, "--json"])
        self.assertEqual(rc, 0, err)

        conn = mem.connect(copy)
        try:
            copied_at = float(conn.execute("SELECT valor FROM meta WHERE clave = 'copiado_en'").fetchone()[0])
        finally:
            conn.close()
        conn = mem.connect(self.db)
        try:
            epoch = int(conn.execute("SELECT valor FROM meta WHERE clave = 'epoca'").fetchone()[0])
        finally:
            conn.close()
        rc, _, err = _run(["set", "t1", "--estado", "lanzada", "--desde", "pendiente",
                           "--org", "trycore", "--db", self.db, "--json"])
        self.assertEqual(rc, 0, err)
        conn = mem.connect(self.db)
        try:
            updated = float(conn.execute("SELECT actualizado FROM tareas WHERE id = 't1'").fetchone()[0])
        finally:
            conn.close()
        self.assertGreater(updated, copied_at)

        rc, out, err = _run(["promote", "--from", copy, "--org", "trycore",
                             "--db", self.db, "--yes", "--json"])
        self.assertEqual(rc, 0, err)
        conn = mem.connect(self.db)
        try:
            state = conn.execute("SELECT estado FROM tareas WHERE id = 't1'").fetchone()[0]
            new_epoch = int(conn.execute("SELECT valor FROM meta WHERE clave = 'epoca'").fetchone()[0])
            decisions = [json.loads(r[0]) for r in conn.execute(
                "SELECT datos FROM eventos WHERE que = 'necesita_decision'")]
        finally:
            conn.close()
        self.assertEqual(state, "bloqueada")
        self.assertEqual(new_epoch, epoch + 1)
        self.assertEqual([d["tarea"] for d in decisions], ["t1"])

        rc, out, err = _run(claim)
        self.assertEqual(rc, 9)
        self.assertIn("epoch", err.lower())
        self.assertFalse(json.loads(out)["ok"])
        rc, _, err = _run(["claim", "orquestador", "--owner", "new-owner", "--org", "trycore",
                           "--db", self.db, "--json"])
        self.assertEqual(rc, 0, err)

    def _promote_setup(self, old="old-owner"):
        """A claimed lease, a copy of the memory and a promotion over it. Returns (copy, claim args)."""
        self.init()
        self.flow("f1")
        claim = ["claim", "orquestador", "--owner", old, "--org", "trycore", "--db", self.db, "--json"]
        rc, _, err = _run(claim)
        self.assertEqual(rc, 0, err)
        copy = os.path.join(self.tmp.name, "copy.db")
        rc, _, err = _run(["backup", "--to", copy, "--org", "trycore", "--db", self.db, "--json"])
        self.assertEqual(rc, 0, err)
        return copy, claim

    def _promote(self, copy, *extra):
        return _run(["promote", "--from", copy, "--org", "trycore", "--db", self.db, "--yes", "--json", *extra])

    def _count(self, sql):
        conn = mem.connect(self.db)
        try:
            return conn.execute(sql).fetchone()[0]
        finally:
            conn.close()

    def test_fenced_owner_cannot_set_or_event_after_promote(self):
        copy, _ = self._promote_setup()
        self.task(flow="f1", brief="x", task_id="t1")
        with mock.patch.dict(os.environ, {"DELEGA_MEM_OWNER": "old-owner"}):
            rc, _, err = _run(["set", "t1", "--estado", "lanzada", "--org", "trycore", "--db", self.db, "--json"])
            self.assertEqual(rc, 0, err)                      # before the promotion it still writes
            self.assertEqual(self._promote(copy)[0], 0)
            events = self._count("SELECT COUNT(*) FROM eventos")
            state = self._count("SELECT COUNT(*) FROM tareas WHERE estado = 'lanzada'")
            rc, _, err = _run(["set", "t1", "--estado", "ok", "--forzar", "--org", "trycore",
                               "--db", self.db, "--json"])
            self.assertEqual(rc, 9, err)
            rc, _, err = _run(["event", "algo", "--org", "trycore", "--db", self.db, "--json"])
            self.assertEqual(rc, 9, err)
            self.assertEqual(self._count("SELECT COUNT(*) FROM eventos"), events)
            self.assertEqual(self._count("SELECT COUNT(*) FROM tareas WHERE estado = 'ok'"), 0)
            self.assertEqual(state, self._count("SELECT COUNT(*) FROM tareas WHERE estado = 'lanzada'"))
        with mock.patch.dict(os.environ, {"DELEGA_MEM_OWNER": "new-owner"}):
            rc, _, err = _run(["event", "algo", "--org", "trycore", "--db", self.db, "--json"])
            self.assertEqual(rc, 0, err)

    def test_promote_leaves_tasks_older_than_the_copy_and_blocks_arreglo_after_it(self):
        self.init()
        self.flow("f1")
        self.task(flow="f1", brief="antes", task_id="old")
        self.task(flow="f1", brief="despues", task_id="fix")
        for t, st in (("old", "lanzada"), ("fix", "lanzada")):
            self.assertEqual(_run(["set", t, "--estado", st, "--org", "trycore", "--db", self.db])[0], 0)
        self.assertEqual(_run(["set", "fix", "--estado", "rojo", "--org", "trycore", "--db", self.db])[0], 0)
        copy = os.path.join(self.tmp.name, "copy.db")
        self.assertEqual(_run(["backup", "--to", copy, "--org", "trycore", "--db", self.db])[0], 0)
        time.sleep(0.05)
        self.assertEqual(_run(["set", "fix", "--estado", "arreglo", "--org", "trycore", "--db", self.db])[0], 0)
        rc, _, err = self._promote(copy)
        self.assertEqual(rc, 0, err)
        self.assertEqual(self._count("SELECT estado FROM tareas WHERE id = 'old'"), "lanzada")
        self.assertEqual(self._count("SELECT estado FROM tareas WHERE id = 'fix'"), "bloqueada")

    def test_promote_holds_the_write_lock_of_the_main_database(self):
        copy, _ = self._promote_setup()
        seen = []
        real = mem.backup

        def spy(conn, dest):
            other = sqlite3.connect(self.db, timeout=0.2)
            try:
                other.execute("BEGIN IMMEDIATE")
                seen.append("writable")
            except sqlite3.OperationalError:
                seen.append("locked")
            finally:
                other.close()
            return real(conn, dest)
        with mock.patch.object(mem, "backup", spy):
            rc, _, err = self._promote(copy)
        self.assertEqual(rc, 0, err)
        self.assertEqual(seen, ["locked"])

    def test_failback_readmits_the_owner_with_unfence_or_promote_owner(self):
        copy, claim = self._promote_setup()
        self.assertEqual(self._promote(copy)[0], 0)
        self.assertEqual(_run(claim)[0], 9)
        rc, _, _ = _run(["unfence", "old-owner", "--org", "trycore", "--db", self.db])
        self.assertEqual(rc, 2)                               # without --yes nothing is touched
        self.assertEqual(_run(claim)[0], 9)
        rc, _, err = _run(["unfence", "old-owner", "--yes", "--org", "trycore", "--db", self.db, "--json"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(_run(claim)[0], 0)
        # the lease of the last epoch is what counts: a second promotion fences it again, --owner readmits it
        copy2 = os.path.join(self.tmp.name, "copy2.db")
        self.assertEqual(_run(["backup", "--to", copy2, "--org", "trycore", "--db", self.db])[0], 0)
        self.assertEqual(self._promote(copy2)[0], 0)
        self.assertEqual(_run(claim)[0], 9)
        copy3 = os.path.join(self.tmp.name, "copy3.db")
        self.assertEqual(_run(["backup", "--to", copy3, "--org", "trycore", "--db", self.db])[0], 0)
        self.assertEqual(self._promote(copy3, "--owner", "old-owner")[0], 0)
        self.assertEqual(_run(claim)[0], 0)

    def test_promote_does_not_remove_wal_sidecars_of_an_open_connection(self):
        self.init()
        self.flow("f1")
        copy = os.path.join(self.tmp.name, "copy.db")
        rc, _, err = _run(["backup", "--to", copy, "--org", "trycore", "--db", self.db, "--json"])
        self.assertEqual(rc, 0, err)
        conn = mem.connect(self.db)
        try:
            conn.execute("INSERT INTO meta (clave, valor) VALUES ('prueba', 'sidecars') "
                         "ON CONFLICT(clave) DO UPDATE SET valor = excluded.valor")
            self.assertTrue(os.path.exists(self.db + "-wal"))
            self.assertTrue(os.path.exists(self.db + "-shm"))
            rc, _, err = _run(["promote", "--from", copy, "--org", "trycore",
                               "--db", self.db, "--yes", "--json"])
            self.assertEqual(rc, 0, err)
            self.assertTrue(os.path.exists(self.db + "-wal"))
            self.assertTrue(os.path.exists(self.db + "-shm"))
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        finally:
            conn.close()

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

    def test_a_locked_database_returns_the_retryable_exit_code(self):
        self.init()
        locked = sqlite3.OperationalError("database is locked")
        with mock.patch.object(mem.sqlite3, "connect", side_effect=locked):
            rc, out, err = _run(["next", "f1", "--db", self.db, "--json"])
        self.assertEqual(rc, 10)
        self.assertFalse(json.loads(out)["ok"])
        self.assertIn("locked", err)

    def test_lease_ttl_must_be_finite_positive_and_at_most_one_hour(self):
        self.init()
        for ttl in ("inf", "nan", "-1", "0", "3601"):
            rc, _, err = _run(["claim", "orq", "--ttl", ttl, "--owner", "orch:1",
                               "--org", "trycore", "--db", self.db])
            self.assertEqual(rc, 2, ttl)
            self.assertIn("--ttl", err)
        rc, _, err = _run(["claim", "orq", "--ttl", "3600", "--owner", "orch:1",
                           "--org", "trycore", "--db", self.db])
        self.assertEqual(rc, 0, err)

    def test_an_existing_relative_repository_is_refused(self):
        self.init()
        os.makedirs(os.path.join(self.tmp.name, "repos", "app"))
        old_cwd = os.getcwd()
        try:
            os.chdir(self.tmp.name)
            rc, out, err = _run(["add-flow", "--repo", "repos/app", "--org", "trycore",
                                 "--db", self.db, "--json"])
        finally:
            os.chdir(old_cwd)
        self.assertEqual(rc, 2)
        self.assertFalse(json.loads(out)["ok"])
        self.assertIn("local path", err)

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

"""relevo: a node orchestrates when the Mac is down (fake ssh/ws/claude/git; no network)."""
import contextlib
import io
import json
import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "..", "src")
sys.path.insert(0, SRC)

from delega import config, mem, queue, relevo  # noqa: E402

ASK = ("ws exec claude", "claude ")
REMOTE = "git@example.com:acme/api"
MAC = """
[general]
default_profile = "trycore"
locale = "en"
[policy]
job.max_usd = 15
[profiles.trycore]
owner = "self"
nodes = ["trycorepc"]
node_bundle = "/Users/mac/secret-bundle"
env_file = "/Users/mac/.config/delega/trycore/api.env"
[profiles.personal]
owner = "self"
nodes = ["ct110"]
policy.budget.day_usd = 5
[nodes.trycorepc]
ssh = "trycorepc"
profiles = ["trycore"]
token_command = ["security", "find-generic-password", "-s", "claude-trycore-token", "-w"]
account = "dev@example.com (token)"
config_dirs = { trycore = "~/.claude-trycore" }
[nodes.ct110]
ssh = "orq@192.168.2.27"
profiles = ["personal"]
token_command = ["security", "find-generic-password", "-s", "claude-personal", "-w"]
[memoria.trycore]
host = "orq@db.example.com"
path = "/srv/m.db"
repos = { "acme/api" = "/Users/mac/api" }
[memoria.personal]
host = "orq@192.168.2.27"
path = "/srv/p.db"
"""
NODE = """
[general]
default_profile = "trycore"
[profiles.trycore]
owner = "self"
[nodes.trycorepc]
ssh = "trycorepc"
[memoria.trycore]
path = "{db}"
[relevo]
node = "trycorepc"
"""


def script(path, body):
    with open(path, "w") as f:
        f.write("#!/bin/sh\n" + body)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)


def mem_cli(*args):
    """The strict `delega mem`: org and owner of the test are always given (environment)."""
    out, err = io.StringIO(), io.StringIO()
    env = {"DELEGA_MEM_ORG": "trycore", "DELEGA_MEM_OWNER": "test-owner"}
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), mock.patch.dict(os.environ, env):
        rc = mem.main(list(args)) or 0
    return rc, out.getvalue(), err.getvalue()


class Base(unittest.TestCase):
    CONF = NODE

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        r = self.tmp.name
        self.home, self.bin, self.state = (os.path.join(r, x) for x in ("home", "bin", "state"))
        self.db, self.calls = os.path.join(r, "m.db"), os.path.join(r, "calls.log")
        for d in (self.home, self.bin, self.state):
            os.makedirs(d)
        real_git = subprocess.run(["which", "git"], capture_output=True, text=True).stdout.strip()
        script(os.path.join(self.bin, "git"),
               f'if [ "$1" = clone ] && [ "$2" = --bare ]; then echo "clone $3 $4" >> {self.calls}; '
               f'mkdir -p "$4"; exit 0; fi\nexec {real_git} "$@"\n')
        for fake in ("ws", "claude"):
            script(os.path.join(self.bin, fake), f'echo "{fake} $(echo "$*" | tr \'\\n\' \' \')" >> {self.calls}\n')
        self.conf = os.path.join(r, "config.toml")
        with open(self.conf, "w") as f:
            f.write(self.CONF.replace("{db}", self.db))
        env = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(env)))
        for k in [k for k in os.environ if k.startswith(("DELEGA_", "ORQ_"))]:
            del os.environ[k]
        os.environ.update(DELEGA_STATE_DIR=self.state, DELEGA_CONFIG=self.conf, DELEGA_LOCALE="en",
                          HOME=self.home, PATH=self.bin + os.pathsep + os.environ["PATH"])

    def log(self, wait=0):
        """Lines the fake binaries logged; `wait` > 0 waits for that many (the claude process is detached)."""
        end = time.time() + 5
        while True:
            lines = open(self.calls).read().splitlines() if os.path.exists(self.calls) else []
            if len(lines) >= wait or time.time() > end:
                return lines
            time.sleep(0.02)


class NodeDaemon(Base):
    def setUp(self):
        super().setUp()
        self.assertEqual(mem_cli("init", "--org", "trycore", "--db", self.db)[0], 0)
        self.assertEqual(mem_cli("add-flow", "--repo", REMOTE, "--id", "f1", "--db", self.db)[0], 0)
        self.cfg = config.load()

    def add_task(self, tid):
        return mem_cli("add-task", "--flow", "f1", "--brief", "only in the database: " + tid, "--id", tid,
                       "--db", self.db)

    def cycle(self, launched):
        def fake_launch(cfg, item):
            launched.append((item["id"], item["repo"], open(item["brief"]).read()))
            item["log"] = "x.log"
            return "launched", "1"
        cap = {"has_data": False, "new": 9, "live": 0, "slots": 9}
        with mock.patch.object(queue, "launch", fake_launch), \
                mock.patch("delega.quota.capacity", lambda cfg, name, **k: cap), \
                mock.patch.object(queue, "_reconcile", lambda cfg, adopt: None), \
                contextlib.redirect_stdout(io.StringIO()):
            return queue.daemon_cycle(self.cfg, queue.MemClient(self.cfg, "trycore", "node"), ttl=30)

    def test_node_daemon_launches_from_its_bare_repo_with_the_brief_of_the_db(self):
        self.add_task("t1")
        launched = []
        self.assertTrue(self.cycle(launched))
        bare = os.path.join(self.home, "delega", "api.git")
        self.assertEqual(launched, [("t1", bare, "only in the database: t1")])
        self.assertEqual(self.log(), [f"clone {REMOTE} {bare}"])
        self.add_task("t2")
        self.cycle(launched)
        self.assertEqual(self.log(), [f"clone {REMOTE} {bare}"])          # the bare is created once

    def test_without_relevo_node_the_mac_checkouts_are_used(self):
        self.assertIsNone(relevo.origin(config.Config({"profiles": {"p": {"owner": "self"}}}), REMOTE))

    def red(self, tid):
        if self.task_state(tid) == "rojo":      # a second red: rojo -> arreglo -> lanzada -> rojo
            self.assertEqual(mem_cli("set", tid, "--estado", "arreglo", "--desde", "rojo", "--db", self.db)[0], 0)
        self.assertEqual(mem_cli("set", tid, "--estado", "lanzada", "--db", self.db)[0], 0)
        self.assertEqual(mem_cli("set", tid, "--estado", "rojo", "--desde", "lanzada", "--db", self.db)[0], 0)

    def requests(self):
        c = sqlite3.connect(self.db)
        try:
            return [json.loads(r[0]) for r in c.execute("SELECT datos FROM eventos WHERE que = 'orquestador_claude'")]
        finally:
            c.close()

    def task_state(self, tid):
        c = sqlite3.connect(self.db)
        try:
            return c.execute("SELECT estado FROM tareas WHERE id = ?", (tid,)).fetchone()[0]
        finally:
            c.close()

    def test_second_red_creates_exactly_one_request(self):
        self.add_task("t1")
        client = queue.MemClient(self.cfg, "trycore", "node")
        self.red("t1")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(relevo.escalate(self.cfg, client), [])      # one red is not enough
            self.assertEqual(self.requests(), [])
            self.red("t1")
            self.assertEqual(relevo.escalate(self.cfg, client), ["t1"])
            self.assertEqual(relevo.escalate(self.cfg, client), [])
        self.assertEqual(self.task_state("t1"), "bloqueada")
        reqs = self.requests()
        self.assertEqual(len(reqs), 1)
        self.assertEqual(reqs[0]["tarea"], "t1")
        self.assertEqual([t["id"] for t in reqs[0]["export"]["tareas"]], ["t1"])
        calls = [c for c in self.log(1) if c.startswith(ASK)]
        self.assertEqual(len(calls), 1)
        for want in ("-p", "--model sonnet", "--max-budget-usd 3", "git push", "git merge", "only in the database: t1"):
            self.assertIn(want, calls[0])
        
    def test_ws_exec_when_it_exists_else_plain_claude(self):
        self.assertEqual(relevo.claude_argv("b")[:4], ["ws", "exec", "claude", "-p"])    # fake ws on PATH
        os.remove(os.path.join(self.bin, "ws"))
        self.assertEqual(relevo.claude_argv("b")[:2], ["claude", "-p"])

    def test_needs_decision_event_and_no_second_request(self):
        self.add_task("t1")
        c = sqlite3.connect(self.db)
        c.execute("INSERT INTO eventos (ts, quien, que, datos) VALUES (1, 'wave', 'necesita_decision', ?)",
                  (json.dumps({"tarea": "t1", "flujo": "f1", "motivo": "two options"}),))
        c.commit()
        c.close()
        client = queue.MemClient(self.cfg, "trycore", "node")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(relevo.escalate(self.cfg, client), ["t1"])
            mem_cli("set", "t1", "--estado", "pendiente", "--forzar", "--db", self.db)   # even if someone resets it
            self.assertEqual(relevo.escalate(self.cfg, client), [])
        self.assertEqual(len(self.requests()), 1)
        self.assertEqual(len([c for c in self.log(1) if c.startswith(ASK)]), 1)

    def test_remote_db_through_fake_ssh(self):
        script(os.path.join(self.bin, "delega"), f'PYTHONPATH={SRC} exec {sys.executable} -m delega "$@"\n')
        script(os.path.join(self.bin, "ssh"), 'while [ "$1" = -o ]; do shift 2; done\nshift\neval "$1"\n')
        cfg = config.Config({"profiles": {"trycore": {"owner": "self"}},
                             "memoria": {"trycore": {"host": "orq@elsewhere", "path": self.db}},
                             "relevo": {"node": "n"}})
        client = queue.MemClient(cfg, "trycore", "node")
        self.add_task("t1")
        self.red("t1")
        self.red("t1")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(relevo.escalate(cfg, client), ["t1"])
            self.assertEqual(relevo.escalate(cfg, client), [])
        self.assertEqual(len(self.requests()), 1)
        self.assertEqual(self.task_state("t1"), "bloqueada")


class Derived(Base):
    CONF = MAC

    def test_trycore_config_has_nothing_of_personal_nor_secrets(self):
        cfg = config.load()
        text, _ = relevo.derive_config(cfg, "trycore", "trycorepc")
        self.assertNotIn("personal", text.lower())
        self.assertNotIn("security find-generic-password", text)
        self.assertNotIn("find-generic-password", text)
        self.assertNotIn("token_command", text)
        self.assertNotIn("192.168.2.27", text)
        self.assertNotIn("/Users/mac", text)
        path = os.path.join(self.tmp.name, "derived.toml")
        with open(path, "w") as f:
            f.write(text)
        d = config.load(path)
        self.assertEqual(list(d.profiles), ["trycore"])
        self.assertEqual(list(d.nodes), ["trycorepc"])
        self.assertEqual(d.memoria_for("trycore")["host"], "orq@db.example.com")
        self.assertEqual(d.policy("job.max_usd"), 15)
        self.assertEqual(relevo.node_name(d), "trycorepc")

    def test_personal_config_for_the_ct(self):
        text, _ = relevo.derive_config(config.load(), "personal", "ct110")
        self.assertNotIn("trycore", text.lower())
        self.assertIn("orq@192.168.2.27", text)
        self.assertIn("budget", text)

    def test_leak_is_refused(self):
        cfg = config.load()
        cfg.profiles["trycore"]["git"] = {"user_name": "mix personal"}
        with self.assertRaises(config.ConfigError):
            relevo.derive_config(cfg, "trycore")

    def test_sync_with_orchestrator_dry_run(self):
        from delega import sync
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.object(sync, "stage", lambda *a: ([], [])):
            rc = sync.main(["--profile", "trycore", "--dry-run", "--with-orchestrator"])
        self.assertEqual(rc, 0)
        self.assertIn("derived orchestrator config", out.getvalue())

    def test_install_uploads_pyz_config_and_wrapper(self):
        from delega import remote
        sent, cmds = [], []
        cfg = config.load()
        node = cfg.node("trycorepc")
        ok = subprocess.CompletedProcess([], 0, "", "")

        def scp(n, src, dst):
            sent.append((dst, open(src).read()))
            return ok
        with mock.patch.object(remote, "ensure_pyz", lambda c, n: "abc"), \
                mock.patch.object(remote, "scp_to", scp), \
                mock.patch.object(remote, "ssh", lambda n, cmd, **k: cmds.append(cmd) or ok):
            self.assertEqual(relevo.install_on_node(cfg, cfg.profile("trycore"), node), (True, ""))
        dests = dict(sent)
        self.assertIn(".config/delega/config.toml", dests)
        self.assertIn("delega.pyz", dests[".local/bin/delega"])
        self.assertNotIn("personal", dests[".config/delega/config.toml"].lower())


class BackupPromote(Base):
    def setUp(self):
        super().setUp()
        self.assertEqual(mem_cli("init", "--org", "trycore", "--db", self.db)[0], 0)
        mem_cli("add-flow", "--repo", REMOTE, "--id", "f1", "--db", self.db)
        mem_cli("claim", "orquestador:trycore", "--ttl", "60", "--db", self.db)

    def test_backup_local_and_scp(self):
        copy = os.path.join(self.tmp.name, "copy", "m.db")
        self.assertEqual(mem_cli("backup", "--to", copy, "--db", self.db)[0], 0)
        self.assertEqual(sqlite3.connect(copy).execute("SELECT id FROM flujos").fetchall(), [("f1",)])
        script(os.path.join(self.bin, "ssh"), f'echo "ssh $*" >> {self.calls}\n')
        script(os.path.join(self.bin, "scp"), f'echo "scp $*" >> {self.calls}\ncp "$4" {self.tmp.name}/sent.db\n')
        rc, _, err = mem_cli("backup", "--to", "orq@host:/srv/b/m.db", "--db", self.db)
        self.assertEqual(rc, 0, err)
        self.assertTrue(any(c.startswith("scp") and c.endswith("orq@host:/srv/b/m.db") for c in self.log()))
        self.assertEqual(sqlite3.connect(os.path.join(self.tmp.name, "sent.db")).execute(
            "SELECT id FROM flujos").fetchall(), [("f1",)])

    def test_promote_without_yes_does_nothing(self):
        copy = os.path.join(self.tmp.name, "copy.db")
        mem_cli("backup", "--to", copy, "--db", self.db)
        main = os.path.join(self.tmp.name, "main.db")
        with open(main, "w") as f:
            f.write("untouched")
        rc, _, err = mem_cli("promote", "--from", copy, "--db", main)
        self.assertEqual(rc, 2)
        self.assertEqual(open(main).read(), "untouched")
        self.assertFalse(os.path.exists(main + ".before-promote"))
        self.assertIn("--yes", err)

    def test_promote_yes_replaces_the_main_and_frees_the_lease(self):
        copy = os.path.join(self.tmp.name, "copy.db")
        mem_cli("backup", "--to", copy, "--db", self.db)
        main = os.path.join(self.tmp.name, "new", "main.db")
        self.assertEqual(mem_cli("promote", "--from", copy, "--db", main, "--yes")[0], 0)
        c = sqlite3.connect(main)
        self.assertEqual(c.execute("SELECT id FROM flujos").fetchall(), [("f1",)])
        self.assertEqual(c.execute("SELECT count(*) FROM leases").fetchone()[0], 0)
        self.assertEqual(c.execute("SELECT count(*) FROM eventos WHERE que = 'mem.promote'").fetchone()[0], 1)
        bad = os.path.join(self.tmp.name, "bad.db")
        open(bad, "w").write("not sqlite")
        self.assertEqual(mem_cli("promote", "--from", bad, "--db", main, "--yes")[0], 2)


if __name__ == "__main__":
    unittest.main()

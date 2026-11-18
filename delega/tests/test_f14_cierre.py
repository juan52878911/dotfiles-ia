"""F14: the three partial gaps of R7. (a) a task launched from the Mac (no `[relevo] node`) records an explicit
`lanzador`, never empty, and a node that takes over never judges its pid; (b) the node's name is translated to its
ssh alias before asking; (c) `sync --with-orchestrator` never moves the bare repo backwards.
Only the ssh (one fake, node `sandbox` is reached as `kling-host`) and `delega run` are faked."""
import contextlib
import io
import os
import stat
import subprocess
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import test_f13_costura as F  # noqa: E402
from delega import config, queue, sync  # noqa: E402

SSH = """#!/bin/sh
while [ "$1" = "-o" ]; do shift 2; done
host="$1"; shift
echo "$host" >> "$FAKE_SSH_LOG"
case "$host" in kling-host) dir="$FAKE_STATE_A";; *) exit 255;; esac
cd "$HOME"; DELEGA_STATE_DIR="$dir" exec sh -c "$1"
"""
NODES = '[nodes.sandbox]\nssh = "kling-host"\n[nodes.other]\nssh = "other-host"\n'


class _Base(F.F13):
    def setUp(self):
        super().setUp()
        p = os.path.join(self.tmp.name, "bin", "ssh")
        with open(p, "w") as f:
            f.write(SSH)
        os.chmod(p, os.stat(p).st_mode | stat.S_IXUSR)


for _n in dir(F.F13):                       # the F13 tests are not run again here
    if _n.startswith("test"):
        setattr(_Base, _n, None)


class F14(_Base):
    def test_a_mac_task_judged_by_a_node_is_not_relaunched_and_asks_for_a_decision(self):
        self.write_conf(NODES)
        self.add_task("t1")
        self.add_task("t2", wave="2")
        self.cycle("a", "mac-owner", ttl=0.5, client_host=None)       # the Mac launches t1 ... and dies
        self.assertEqual(self.runs, ["delega/wave-f1-t1"])
        lz = self.lanzadores()["t1"]
        self.assertTrue(lz)
        self.assertNotIn(lz, config.load().nodes)
        c = F.sqlite3.connect(self.db)
        c.execute("UPDATE tareas SET job_id = 'pend:999999:20261118-120000' WHERE id = 't1'")
        c.commit()
        c.close()
        time.sleep(0.8)
        for _ in range(3):
            self.cycle("b", "node-owner", client_host="sandbox")      # a node takes over: its own pid is not it
        self.assertNotEqual(self.states()["t1"], "rojo")             # (relevo's escalation may block it)
        self.assertEqual(self.runs, ["delega/wave-f1-t1"])
        self.assertEqual([e["motivo"] for e in self.events()], ["lanzador_sin_ssh"])
        self.assertEqual(self.ssh_calls(), [])

    def test_an_old_task_with_empty_lanzador_is_not_judged_by_the_local_pid(self):
        self.write_conf(NODES)
        self.add_task("t1")
        rc, _, err = F.T.mem_cli("set", "t1", "--estado", "lanzada", "--job-id", "pend:999999:20261118-120000",
                                 "--rama", "delega/wave-f1-t1", "--org", "trycore", "--db", self.db)
        self.assertEqual(rc, 0, err)
        for _ in range(2):
            self.cycle("b", "node-owner", client_host="sandbox")
        self.assertNotEqual(self.states()["t1"], "rojo")
        self.assertEqual(self.runs, [])
        self.assertEqual([e["motivo"] for e in self.events()], ["lanzador_sin_ssh"])

    def test_b_the_node_name_is_asked_by_its_ssh_alias(self):
        self.write_conf(NODES)
        self.add_task("t1")
        self.add_task("t2", wave="2")
        self.cycle("a", "owner-a", ttl=0.5, client_host="sandbox")    # node `sandbox` launches and dies
        self.assertEqual(self.lanzadores()["t1"], "sandbox")
        time.sleep(0.8)
        self.cycle("b", "owner-b", client_host="other")
        self.assertEqual(set(self.ssh_calls()), {"kling-host"})
        self.assertEqual(self.states()["t1"], "lanzada")
        self.assertEqual(self.runs, ["delega/wave-f1-t1"])
        self.assertEqual(self.events(), [])

    def test_c_sync_does_not_move_the_bare_back(self):
        self.write_conf(NODES)
        bare = os.path.join(self.home, "delega", "api.git")
        os.makedirs(os.path.dirname(bare))
        subprocess.run(["git", "clone", "-q", "--bare", self.repo, bare], check=True)
        ahead = F.commit_file(bare, F.git(bare, "rev-parse", "main"), "node.txt")   # integrated at the node
        F.git(bare, "update-ref", "refs/heads/main", ahead)
        co = os.path.join(self.tmp.name, "co")
        subprocess.run(["git", "clone", "-q", self.repo, co], check=True)
        node = config.load().node("sandbox")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            ok, msg = sync.push_branch(node, "api", "main", co)
        self.assertTrue(ok)
        self.assertEqual(F.git(bare, "rev-parse", "refs/heads/main"), ahead)
        self.assertIn("NOT overwritten", msg)


if __name__ == "__main__":
    unittest.main()

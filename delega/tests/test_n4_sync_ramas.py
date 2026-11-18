import nod_sandbox  # noqa: F401
import os
import subprocess
import unittest
from unittest import mock

from ops_helpers import OpsCase
from delega import mem, sync

FAKES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fakes")
REMOTE = "https://example.com/acme/app.git"
OTHER = "https://example.com/acme/lib.git"

TOML = """
[general]
owner_name = "Sam"
default_profile = "work"
[nodes.box]
ssh = "box"
workdir = "wd"
profiles = ["work"]
[profiles.work]
owner = "self"
org = "acme"
nodes = ["box"]
node_bundle = "{bundle}"
[memoria.acme]
host = ""
path = "{db}"
repos = {{ app = "{app}", lib = "{lib}" }}
"""


class SyncRamasTest(OpsCase):
    def setUp(self):
        super().setUp()
        self.node_home = os.path.join(self.root, "nodehome")
        os.environ.update(DELEGA_SSH=os.path.join(FAKES, "ssh"), FAKE_NODE_HOME=self.node_home)
        bundle = os.path.join(self.root, "bundle")
        os.makedirs(bundle)
        open(os.path.join(bundle, "CLAUDE.md"), "w").write("hi\n")
        self.app = self.mk("app", REMOTE)
        self.lib = self.mk("lib", OTHER)
        self.db = os.path.join(self.root, "mem.db")
        self.write_config(TOML.format(bundle=bundle, db=self.db, app=self.app, lib=self.lib))
        conn = mem.connect(self.db, create=True)
        mem.init_db(conn, "acme")
        self.conn = conn
        self.addCleanup(conn.close)

    def mk(self, name, remote):
        """A checkout whose origin is a local bare; origin/main exists, local main is one commit ahead."""
        bare = os.path.join(self.root, name + "-origin.git")
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", bare], check=True)
        repo = self.make_repo(name)
        self.git(repo, "remote", "add", "origin", bare)
        self.git(repo, "push", "-q", "origin", "main")
        self.git(repo, "fetch", "-q", "origin")
        with open(os.path.join(repo, "local.txt"), "w") as f:
            f.write("unpushed\n")
        self.git(repo, "add", "-A")
        self.git(repo, "commit", "-qm", "local only")
        return repo

    def bare(self, repo="app"):
        return os.path.join(self.node_home, "wd", repo + ".git")

    def sync(self, *a):
        with mock.patch("delega.relevo.install_on_node", return_value=(True, "")), \
                mock.patch.object(sync, "sync_node", return_value=(True, "")):
            return self.run_cli(sync.main, "--profile", "work", "--with-orchestrator", *a)

    def test_bare_without_main_gets_origin_main_and_head(self):
        mem.add_flow(self.conn, REMOTE, org="acme")
        rc, out, err = self.sync()
        self.assertEqual(rc, 0, out + err)
        want = self.git(self.app, "rev-parse", "refs/remotes/origin/main")
        self.assertNotEqual(want, self.git(self.app, "rev-parse", "main"))
        self.assertEqual(self.git(self.bare(), "rev-parse", "refs/heads/main"), want)
        self.assertEqual(self.git(self.bare(), "rev-parse", "HEAD"), want)

    def test_destination_branch_only(self):
        self.git(self.app, "branch", "dev", "origin/main")
        self.git(self.app, "branch", "other")
        mem.add_flow(self.conn, REMOTE, branch="dev", org="acme")
        rc, out, err = self.sync()
        self.assertEqual(rc, 0, out + err)
        refs = self.git(self.bare(), "for-each-ref", "--format=%(refname)")
        self.assertEqual(refs, "refs/heads/dev")
        self.assertIn("refs/heads/dev", self.git(self.bare(), "symbolic-ref", "HEAD"))

    def test_inactive_flow_not_pushed(self):
        fl = mem.add_flow(self.conn, REMOTE, org="acme")
        self.conn.execute("UPDATE flujos SET estado='cerrado' WHERE id=?", (fl["id"],))
        self.conn.commit()
        rc, out, err = self.sync()
        self.assertEqual(rc, 0, out + err)
        self.assertFalse(os.path.exists(self.bare()))

    def test_dry_run_writes_nothing(self):
        mem.add_flow(self.conn, REMOTE, org="acme")
        rc, out, err = self.run_cli(sync.main, "--profile", "work", "--with-orchestrator", "--dry-run")
        self.assertEqual(rc, 0, out + err)
        self.assertIn("would push main -> app.git", out)
        self.assertFalse(os.path.exists(self.bare()))

    def test_flow_without_checkout_is_skipped_with_warning(self):
        mem.add_flow(self.conn, "https://example.com/acme/ghost.git", org="acme")
        mem.add_flow(self.conn, OTHER, org="acme")
        rc, out, err = self.sync()
        self.assertEqual(rc, 0, out + err)
        self.assertIn("ghost", err)
        self.assertTrue(os.path.isdir(self.bare("lib")))
        self.assertFalse(os.path.exists(self.bare("ghost")))

    def test_flow_of_other_org_not_pushed(self):
        other_db = os.path.join(self.root, "other.db")
        c2 = mem.connect(other_db, create=True)
        self.addCleanup(c2.close)
        mem.init_db(c2, "zeta")
        mem.add_flow(c2, REMOTE, org="zeta")
        rc, out, err = self.sync()
        self.assertEqual(rc, 0, out + err)
        self.assertFalse(os.path.exists(self.bare()))
        # and a row of another org inside the org's list is filtered out
        mem.add_flow(self.conn, REMOTE, org="acme")
        self.conn.execute("UPDATE flujos SET org='zeta'")
        self.conn.commit()
        rc, out, err = self.sync()
        self.assertFalse(os.path.exists(self.bare()))


if __name__ == "__main__":
    unittest.main()

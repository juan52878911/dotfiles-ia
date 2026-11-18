import nod_sandbox  # noqa: F401  (Nod events of the tests go to a temp file)
import json
import os
import subprocess
import unittest
from unittest import mock

from ops_helpers import OpsCase
from delega import integrate, sync

TOML = """
[general]
owner_name = "Sam"
default_profile = "work"
[nodes.box]
ssh = "box"
profiles = ["work"]
config_dirs = { work = "~/.claude-work" }
[profiles.work]
owner = "self"
nodes = ["box"]
node_bundle = "{bundle}"
bundle_post_sync = ["echo merged"]
[projects."acme/app"]
verify = ["test -f a.txt", "test -f b.txt"]
"""


class IntegrateTest(OpsCase):
    TOML = TOML.replace("{bundle}", "/nonexistent")

    def repo(self):
        return self.make_repo(origin="https://example.com/acme/app.git")

    def test_clean_merge_runs_verify_in_temp_worktree_and_audits(self):
        repo = self.repo()
        self.commit_on(repo, "feat", "b.txt", "new\n")
        head = self.git(repo, "rev-parse", "main")
        rc, out, _ = self.run_cli(integrate.main, repo, "feat", "--into", "main")
        self.assertEqual(rc, 0, out)
        self.assertIn("merges cleanly", out)
        self.assertIn("✓ test -f b.txt", out)
        self.assertEqual(self.git(repo, "rev-parse", "main"), head)          # nothing moved
        self.assertEqual(self.git(repo, "worktree", "list").count("\n"), 0)  # temp worktree removed
        rec = json.loads(open(os.path.join(self.state, "audit.jsonl")).read().splitlines()[-1])
        self.assertEqual((rec["event"], rec["result"], rec["project"]), ("integrate", "verified", "acme/app"))

    def test_verify_failure_stops_and_exits_5(self):
        repo = self.repo()
        self.commit_on(repo, "feat", "c.txt", "new\n")                      # b.txt never appears
        rc, out, _ = self.run_cli(integrate.main, repo, "feat")
        self.assertEqual(rc, 5)
        self.assertIn("✗ test -f b.txt", out)

    def test_conflicts_reported(self):
        repo = self.repo()
        self.commit_on(repo, "feat", "a.txt", "mine\n")
        self.commit_on(repo, "main", "a.txt", "theirs\n", base="main")
        rc, out, _ = self.run_cli(integrate.main, repo, "feat")
        self.assertEqual(rc, 1)
        self.assertIn("a.txt", out)
        self.assertIn("NOT merge cleanly", out)
        last = json.loads(open(os.path.join(self.state, "audit.jsonl")).read().splitlines()[-1])
        self.assertEqual(last["result"], "conflicts")

    def test_no_verify_without_project(self):
        repo = self.make_repo("plain")
        self.commit_on(repo, "feat", "b.txt", "x\n")
        rc, out, _ = self.run_cli(integrate.main, repo, "feat")
        self.assertEqual(rc, 0)
        self.assertIn("only the merge was checked", out)


FAKES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fakes")


class IntegrateMergeTest(OpsCase):
    TOML = TOML.replace("{bundle}", "/nonexistent")

    def setUp(self):
        super().setUp()
        self.node_home = os.path.join(self.root, "nodehome")
        os.environ.update(DELEGA_SSH=os.path.join(FAKES, "ssh"), FAKE_NODE_HOME=self.node_home)
        self.repo = self.make_repo(origin="https://example.com/acme/app.git")
        self.job = self.make_job("20260101-000000-1111-x", {"BRANCH": "feat"})

    def audit_last(self):
        return json.loads(open(os.path.join(self.state, "audit.jsonl")).read().splitlines()[-1])

    def log(self):
        p = os.path.join(self.node_home, ".ssh.log")
        return open(p).read() if os.path.exists(p) else ""

    def test_green_on_node_merges_no_ff_and_exits_0(self):
        self.commit_on(self.repo, "feat", "b.txt", "new\n")
        self.git(self.repo, "checkout", "-q", "main")
        old = self.git(self.repo, "rev-parse", "main")
        rc, out, err = self.run_cli(integrate.main, self.repo, "feat", "--merge", "--node", "box")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.git(self.repo, "rev-list", "--parents", "-n1", "main").split()[1:],
                         [old, self.git(self.repo, "rev-parse", "feat")])   # real merge commit
        self.assertTrue(os.path.exists(os.path.join(self.repo, "b.txt")))
        self.assertIn("test -f b.txt", self.log())                         # ran through ssh
        bare = os.path.join(self.node_home, "delega", "repo.git")
        self.assertEqual(self.git(bare, "worktree", "list").count("\n"), 0)  # temp worktree gone
        self.assertEqual(self.git(bare, "branch", "--list", "delega-integrate/*"), "")
        rec = self.audit_last()
        self.assertEqual((rec["event"], rec["result"], rec["merge"]), ("integrate", "merged", True))

    def test_red_verify_leaves_destination_and_exits_7(self):
        self.commit_on(self.repo, "feat", "c.txt", "new\n")               # b.txt never appears
        old = self.git(self.repo, "rev-parse", "main")
        rc, out, _ = self.run_cli(integrate.main, self.repo, "feat", "--merge", "--node", "box")
        self.assertEqual(rc, 7, out)
        self.assertEqual(self.git(self.repo, "rev-parse", "main"), old)
        self.assertIn("FAIL", open(os.path.join(self.job, "integrate.log")).read())
        self.assertEqual(self.audit_last()["result"], "verify_failed")

    def test_conflict_leaves_destination_and_exits_8(self):
        self.commit_on(self.repo, "feat", "a.txt", "mine\n")
        self.commit_on(self.repo, "main", "a.txt", "theirs\n", base="main")
        old = self.git(self.repo, "rev-parse", "main")
        rc, out, _ = self.run_cli(integrate.main, self.repo, "feat", "--merge", "--node", "box")
        self.assertEqual(rc, 8)
        self.assertIn("a.txt", out)
        self.assertEqual(self.git(self.repo, "rev-parse", "main"), old)
        self.assertEqual(self.audit_last()["result"], "conflicts")

    def test_without_node_runs_local(self):
        self.commit_on(self.repo, "feat", "b.txt", "new\n")
        rc, out, err = self.run_cli(integrate.main, self.repo, "feat", "--merge", "--into", "main")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.log(), "")                                   # no ssh at all
        self.assertEqual(len(self.git(self.repo, "rev-list", "--parents", "-n1", "main").split()), 3)

    def test_dest_moved_during_verify_aborts_with_11_and_keeps_destination(self):
        self.commit_on(self.repo, "feat", "b.txt", "new\n")
        def slow_verify(cfg, node, repo, sha, commands, timeout):
            self.commit_on(self.repo, "main", "late.txt", "x\n", base="main")   # main advances meanwhile
            return [(c, 0, 0.0) for c in commands]
        with mock.patch.object(integrate, "run_verify_remote", side_effect=slow_verify):
            rc, out, err = self.run_cli(integrate.main, self.repo, "feat", "--merge", "--node", "box")
        moved = self.git(self.repo, "rev-parse", "main")
        self.assertEqual(rc, 11, out + err)
        self.assertEqual(len(self.git(self.repo, "rev-list", "--parents", "-n1", "main").split()), 2)  # no merge commit
        self.assertTrue(os.path.exists(os.path.join(self.repo, "late.txt")))
        self.assertEqual(self.git(self.repo, "rev-parse", "main"), moved)
        self.assertEqual(self.audit_last()["result"], "dest_moved")

    def test_dest_moved_when_not_checked_out_keeps_new_commits(self):
        self.commit_on(self.repo, "feat", "b.txt", "new\n")
        self.git(self.repo, "checkout", "-q", "feat")                           # main is not the current branch
        def slow_verify(cfg, node, repo, sha, commands, timeout):
            self.git(self.repo, "update-ref", "refs/heads/main",
                     self.git(self.repo, "commit-tree", "main^{tree}", "-p", "main", "-m", "late"))
            return [(c, 0, 0.0) for c in commands]
        with mock.patch.object(integrate, "run_verify_remote", side_effect=slow_verify):
            rc, out, err = self.run_cli(integrate.main, self.repo, "feat", "--merge", "--node", "box")
        self.assertEqual(rc, 11, out + err)
        self.assertEqual(self.git(self.repo, "log", "-1", "--format=%s", "main"), "late")

    def test_node_of_another_profile_is_refused_before_any_push(self):
        from delega import remote
        self.write_config(TOML.replace("{bundle}", "/nonexistent")
                          + '[nodes.other]\nssh = "other"\nprofiles = ["private"]\n'
                          + '[profiles.private]\nowner = "self"\nnodes = ["other"]\n')
        self.commit_on(self.repo, "feat", "b.txt", "new\n")
        with mock.patch.object(remote, "ssh", side_effect=AssertionError("must not reach the node")):
            rc, out, err = self.run_cli(integrate.main, self.repo, "feat", "--merge", "--node", "other")
        self.assertEqual(rc, 2, out + err)
        self.assertEqual(self.audit_last()["result"], "node_profile_refused")
        self.assertFalse(os.path.exists(os.path.join(self.node_home, "delega", "repo.git")))

    def test_remote_ref_cleaned_when_worktree_add_fails(self):
        from delega import remote
        self.commit_on(self.repo, "feat", "b.txt", "new\n")
        real = remote.ssh
        def flaky(node, cmd, **kw):
            if "worktree add" in cmd:
                return subprocess.CompletedProcess(cmd, 1, "", "boom")
            return real(node, cmd, **kw)
        with mock.patch.object(remote, "ssh", side_effect=flaky):
            rc, out, err = self.run_cli(integrate.main, self.repo, "feat", "--merge", "--node", "box")
        self.assertEqual(rc, 2, out + err)
        bare = os.path.join(self.node_home, "delega", "repo.git")
        self.assertEqual(self.git(bare, "branch", "--list", "delega-integrate/*"), "")

    def test_unreachable_cleanup_is_audited(self):
        from delega import launch
        self.commit_on(self.repo, "feat", "b.txt", "new\n")
        with mock.patch.object(launch, "ssh_retry", side_effect=launch.NoContact(5, "down")):
            rc, out, err = self.run_cli(integrate.main, self.repo, "feat", "--merge", "--node", "box")
        recs = [json.loads(l) for l in open(os.path.join(self.state, "audit.jsonl")).read().splitlines()]
        self.assertTrue(any(r.get("result") == "cleanup_failed" for r in recs), recs)

    def test_merge_without_verify_refused_unless_allowed(self):
        repo = self.make_repo("plain")                                          # no project -> no verify
        self.commit_on(repo, "feat", "b.txt", "new\n")
        old = self.git(repo, "rev-parse", "main")
        rc, out, err = self.run_cli(integrate.main, repo, "feat", "--merge")
        self.assertEqual(rc, 7, out + err)
        self.assertEqual(self.git(repo, "rev-parse", "main"), old)
        rc, out, err = self.run_cli(integrate.main, repo, "feat", "--merge", "--allow-unverified")
        self.assertEqual(rc, 0, out + err)
        self.assertNotEqual(self.git(repo, "rev-parse", "main"), old)

    def test_refuses_when_destination_open_in_other_worktree(self):
        self.commit_on(self.repo, "feat", "b.txt", "new\n")
        self.git(self.repo, "checkout", "-q", "feat")
        other = os.path.join(self.root, "other-wt")
        self.git(self.repo, "worktree", "add", "-q", other, "main")
        old = self.git(self.repo, "rev-parse", "main")
        rc, out, err = self.run_cli(integrate.main, self.repo, "feat", "--merge", "--node", "box")
        self.assertEqual(rc, 2, out + err)
        self.assertIn("other-wt", err)
        self.assertEqual(self.git(self.repo, "rev-parse", "main"), old)

    def test_remote_worktree_names_carry_a_unique_suffix(self):
        import re
        self.commit_on(self.repo, "feat", "b.txt", "new\n")
        self.run_cli(integrate.main, self.repo, "feat", "--node", "box")
        self.assertTrue(re.search(r"integrate-[0-9a-f]{12}-[0-9a-f]{8}", self.log()), self.log())

class SyncTest(OpsCase):
    def setUp(self):
        super().setUp()
        self.bundle = os.path.join(self.root, "bundle")
        for rel, text in (("CLAUDE.md", "# My rules\n"), ("hooks/guard.py", "print(1)\n"), ("agents/a.md", "agent\n"),
                          ("settings.json", "{}\n"), ("home/.local/x.json", "{}\n"),
                          (".credentials.json", "SECRET"), ("hooks/.env", "TOKEN=1"), ("agents/id_rsa", "KEY"),
                          ("notes-not-synced.txt", "ignored")):
            p = os.path.join(self.bundle, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            open(p, "w").write(text)
        self.write_config(TOML.replace("{bundle}", self.bundle))

    def test_stage_filters_credentials_and_renders_node_rules(self):
        cfg = self.cfg()
        stage_dir = os.path.join(self.root, "stage")
        staged, skipped = sync.stage(cfg, cfg.profile("work"), stage_dir)
        self.assertEqual(staged, ["claude/CLAUDE.md", "claude/agents/a.md", "claude/hooks/guard.py", "claude/settings.json",
                                  "home/.local/x.json"])
        self.assertEqual(len(skipped), 2)      # root .credentials.json is not an item, never staged
        md = open(os.path.join(stage_dir, "claude", "CLAUDE.md")).read()
        self.assertTrue(md.startswith("# My rules"))
        self.assertIn("Sam's orchestrator", md)
        self.assertIn("120 min", md)
        self.assertIn("Never bypass hooks", md)
        self.assertNotIn("SECRET", "".join(open(os.path.join(r, f)).read() for r, _, fs in os.walk(stage_dir) for f in fs))

    def test_sync_sends_tar_through_remote_and_runs_post_sync(self):
        calls = []
        ok = subprocess.CompletedProcess([], 0, "", "")
        fake = mock.Mock()
        fake.scp_to = lambda node, src, dst: calls.append(("scp", node["name"], dst, os.path.exists(src)))
        fake.ssh = lambda node, cmd, input=None, timeout=60: (calls.append(("ssh", cmd)), ok)[1]
        with mock.patch.dict("sys.modules", {"delega.remote": fake}), mock.patch("delega.remote", fake, create=True):
            rc, out, _ = self.run_cli(sync.main)
        self.assertEqual(rc, 0, out)
        self.assertEqual(calls[0][:2], ("scp", "box"))
        self.assertTrue(calls[0][3])
        self.assertIn('"$HOME"/.claude-work', calls[1][1])                  # config_dirs[profile]
        self.assertIn("echo merged", calls[2][1])                            # bundle_post_sync
        self.assertIn("synced", out)

    def test_dry_run_lists_files_and_respects_profile_filter(self):
        rc, out, err = self.run_cli(sync.main, "--dry-run")
        self.assertEqual(rc, 0)
        self.assertIn("claude/hooks/guard.py", out)
        self.assertNotIn("credentials", out)
        self.assertIn("not sent", err)

    def test_tar_writes_bundle_without_contacting_nodes(self):
        import tarfile
        out_tar = os.path.join(self.root, "rules.tar")
        with mock.patch("delega.remote.ssh", side_effect=AssertionError("no node")):
            rc, out, _ = self.run_cli(sync.main, "--tar", out_tar)
        self.assertEqual(rc, 0, out)
        names = tarfile.open(out_tar).getnames()
        self.assertIn("claude/hooks/guard.py", names)
        self.assertIn("home/.local/x.json", names)
        self.assertFalse(any("credentials" in n or n.endswith(".env") for n in names))

    def test_remote_path(self):
        self.assertEqual(sync.remote_path("~/.claude"), '"$HOME"/.claude')
        self.assertEqual(sync.remote_path("/opt/c d"), "'/opt/c d'")


if __name__ == "__main__":
    unittest.main()

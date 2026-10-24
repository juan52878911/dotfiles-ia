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

    def test_remote_path(self):
        self.assertEqual(sync.remote_path("~/.claude"), '"$HOME"/.claude')
        self.assertEqual(sync.remote_path("/opt/c d"), "'/opt/c d'")


if __name__ == "__main__":
    unittest.main()

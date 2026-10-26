"""kindling runner: one ephemeral microVM per job, driven through tests/fakes/kling-bin."""
import nod_sandbox  # noqa: F401  (Nod events of the tests go to a temp file)
import os
import subprocess
import sys
from unittest import mock

from launch_env import FAKES, LaunchCase, sh

from delega import util

TOKEN = "tok-s3cret-123"


class KindlingCase(LaunchCase):
    """A kindling node `vm` on the fake host, with golden claude-api one commit behind the repo."""
    def setUp(self):
        self.vmhome = None
        super().setUp()
        self.goldens = os.path.join(self.root, "goldens")
        g = os.path.join(self.goldens, "claude-api")
        os.makedirs(g)
        sh("git", "clone", "-q", self.repo, os.path.join(g, "wt"))
        sh("git", "-C", os.path.join(g, "wt"), "branch", "delega-base")
        # the local repo moves on after the golden was baked: only that commit must travel
        with open(os.path.join(self.repo, "src", "app", "new.py"), "w") as f:
            f.write("x = 1\n")
        sh("git", "-C", self.repo, "add", ".")
        sh("git", "-C", self.repo, "commit", "-qm", "after golden")
        p = mock.patch.dict(os.environ, {
            "FAKE_KLING_GOLDENS": self.goldens, "FAKE_KLING_VMHOME": self.vmhome,
            "PATH": os.path.join(FAKES, "kling-bin") + os.pathsep + os.environ["PATH"]})
        p.start()
        self.addCleanup(p.stop)

    def write_config(self):
        self.vmhome = os.path.join(self.root, "vmhome")
        self.extra_config = f"""
[nodes.vm]
ssh = "khost"
runner = "kindling"
max_jobs = 2
python = "{sys.executable}"
launcher = ["{self.claude}"]
vm_home = "{self.vmhome}"
vm_user = "worker"
token_command = ["sh", "-c", "printf tok-s3cret; echo -123"]
account = "dev@example.com (token)"
"""
        super().write_config()

    def kling_log(self):
        p = os.path.join(self.node_home, ".kling.log")
        return open(p).read() if os.path.exists(p) else ""


class Kindling(KindlingCase):
    def test_job_runs_in_vm_and_branch_comes_back(self):
        os.environ["STUB_COMMIT_TEXT"] = "done in vm"
        rc, out, err = self.run_cli([self.repo, self.brief, "--node", "vm", "--branch", "delega/k1"])
        self.assertEqual(rc, 0, out + err)
        log = self.kling_log()
        self.assertIn("run -name delega-", log)
        self.assertIn("-from claude-api -egress allowlist -allow anthropic.com", log)
        self.assertRegex(log, r"\nrm delega-")              # the VM is gone at the end
        self.assertFalse(os.path.exists(self.vmhome))
        # the branch is here, on top of the commit made after the golden
        self.assertEqual(sh("git", "-C", self.repo, "show", "delega/k1:result.txt"), "done in vm")
        self.assertEqual(sh("git", "-C", self.repo, "log", "-1", "--format=%s", "delega/k1~1"), "after golden")
        meta = util.read_meta(self.job_dir())
        self.assertTrue(meta["VM"].startswith("delega-"))
        self.assertEqual(meta["ACCOUNT"], "dev@example.com (token)")
        self.assertTrue(meta.get("END"))
        stub = util.read_json(os.path.join(self.job_dir(), "summary.json"))
        self.assertEqual(stub["rc"], 0)

    def test_token_reaches_worker_by_process_only(self):
        os.environ["STUB_SLEEP"] = "1"
        seen = {}
        from delega import launch
        real = launch.wait_and_finish

        def spy(cfg, job_dir, poll_s=None):
            chan = os.path.join(self.vmhome, util.read_meta(job_dir)["CHANNEL"])
            import time
            for _ in range(100):
                if os.path.exists(os.path.join(chan, "stub.json")):
                    break
                time.sleep(0.1)
            seen.update(util.read_json(os.path.join(chan, "stub.json")) or {})
            return real(cfg, job_dir, poll_s)

        with mock.patch.object(launch, "wait_and_finish", spy):
            rc, out, err = self.run_cli([self.repo, self.brief, "--node", "vm"])
        self.assertEqual(rc, 0, out + err)
        self.assertTrue(seen.get("oauth_token_set"), seen)
        self.assertIn("CLAUDE_CODE_OAUTH_TOKEN", seen["env"]["DELEGA_KEEP_ENV"])
        hits = subprocess.run(["grep", "-rlF", TOKEN, self.root], capture_output=True, text=True).stdout
        self.assertEqual(hits, "", "token written to disk")
        self.assertNotIn(TOKEN, out + err + self.kling_log())

    def test_say_and_ps_reach_the_channel_inside_the_vm(self):
        os.environ["STUB_SLEEP"] = "3"
        from delega import jobs, launch
        said = {}
        real = launch.wait_and_finish

        def spy(cfg, job_dir, poll_s=None):
            import io, contextlib, time
            time.sleep(1)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                said["say"] = jobs.main_say([os.path.basename(job_dir), "hello vm"])
                said["ps"] = jobs.main_ps([os.path.basename(job_dir)])
            said["ps_out"] = buf.getvalue()
            said["inbox"] = jobs.read_channel(cfg, job_dir, "inbox.jsonl")
            return real(cfg, job_dir, poll_s)

        with mock.patch.object(launch, "wait_and_finish", spy):
            rc, out, err = self.run_cli([self.repo, self.brief, "--node", "vm"])
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(said["say"], 0)
        self.assertIn("hello vm", said["inbox"] or "")
        self.assertEqual(said["ps"], 0)
        self.assertIn("vm", said["ps_out"])

    def test_resume_refused(self):
        rc, out, err = self.run_cli([self.repo, self.brief, "--node", "vm", "--resume", "abc", "--no-lint"])
        self.assertNotEqual(rc, 0)
        self.assertIn("kindling", err)
        self.assertNotIn("run -name", self.kling_log())

    def test_missing_golden_fails_and_closes_job(self):
        os.rename(os.path.join(self.goldens, "claude-api"), os.path.join(self.goldens, "other"))
        rc, out, err = self.run_cli([self.repo, self.brief, "--node", "vm"])
        self.assertEqual(rc, 5, out + err)
        self.assertIn("claude-api", err)
        self.assertTrue(util.read_meta(self.job_dir()).get("END"))

    def test_version_skew_destroys_vm(self):
        os.environ["FAKE_VERSION"] = "2.1.5"
        from delega import nodes
        with mock.patch.object(nodes, "claude_version", lambda *a, **k: "2.9.0"):
            rc, out, err = self.run_cli([self.repo, self.brief, "--node", "vm"])
        self.assertEqual(rc, 5, out + err)
        self.assertRegex(self.kling_log(), r"\nrm delega-")
        self.assertFalse(os.path.exists(self.vmhome))

    def test_full_host_refused_by_probe(self):
        os.makedirs(os.path.join(self.node_home, ".kling-vms"))
        for n in ("delega-a", "delega-b", "delega-seed-db-api-main"):
            open(os.path.join(self.node_home, ".kling-vms", n), "w").close()
        rc, out, err = self.run_cli([self.repo, self.brief, "--node", "vm"])
        self.assertEqual(rc, 5, out + err)
        self.assertIn("max_jobs=2", err)
        self.assertIn("2 delega VMs", err)          # the seed sandbox is not a job

    def test_requires_compute_refused(self):
        rc, out, err = self.run_cli([self.repo, self.brief, "--node", "vm", "--requires", "tool"])
        self.assertEqual(rc, 5, out + err)
        self.assertIn("kindling", err)


if __name__ == "__main__":
    import unittest
    unittest.main()

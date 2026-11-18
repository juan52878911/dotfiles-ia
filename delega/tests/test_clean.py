"""clean: only old, closed jobs whose branch was fetched are removed from node hosts."""
import json
import os
import time
import unittest

from ops_helpers import OpsCase

from delega import clean

FAKES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fakes")

TOML = """
[general]
locale = "en"
owner_name = "Sam"
default_profile = "work"
[nodes.box]
ssh = "box"
profiles = ["work"]
[profiles.work]
owner = "self"
nodes = ["box"]
"""


class CleanTest(OpsCase):
    TOML = TOML

    def setUp(self):
        super().setUp()
        self.node_home = os.path.join(self.root, "nodehome")
        os.environ["DELEGA_SSH"] = os.path.join(FAKES, "ssh")
        os.environ["FAKE_NODE_HOME"] = self.node_home

    def remote_job(self, job_id, *, ended=True, fetched=True, lease_age=None, final=False, extra=None):
        meta = {"NODE": "box", "CHANNEL": f"delega/jobs/{job_id}", "BRANCH": f"delega/{job_id}"}
        meta.update(extra or {})
        if ended:
            meta["END"] = str(int(time.time()) - 10 * 86400)
        job = self.make_job(f"{job_id}-repo", meta)
        if fetched:
            with open(os.path.join(job, "fetch.log"), "w", encoding="utf-8"):
                pass
        else:
            with open(os.path.join(job, "fetch.log"), "w", encoding="utf-8") as stream:
                stream.write("fatal: branch could not be fetched\n")
        remote = os.path.join(self.node_home, "delega", "jobs", job_id)
        os.makedirs(remote, exist_ok=True)
        with open(os.path.join(remote, "payload"), "w", encoding="utf-8") as stream:
            stream.write("x" * 2048)
        if lease_age is not None:
            lease = {"pid": 123, "host": "box", "renewed": time.time() - lease_age}
            if final:
                lease["final"] = True
            with open(os.path.join(remote, "lease.json"), "w", encoding="utf-8") as stream:
                json.dump(lease, stream)
        return job, remote

    def ssh_log(self):
        path = os.path.join(self.node_home, ".ssh.log")
        if not os.path.exists(path):
            return ""
        with open(path, encoding="utf-8") as stream:
            return stream.read()

    def test_without_yes_only_lists_and_deletes_nothing(self):
        _closed, remote = self.remote_job("20260101-000000-1111", lease_age=3600, final=True)
        _, unbrought = self.remote_job("20260102-000000-2222", fetched=False, lease_age=3600, final=True)
        _, fresh = self.remote_job("20260103-000000-3333", lease_age=1)
        _, live = self.remote_job("20260104-000000-4444", ended=False, lease_age=1)

        rc, out, err = self.run_cli(clean.main)
        self.assertEqual(rc, 0, err)
        self.assertIn("20260101-000000-1111", out)
        self.assertIn("box", out)
        self.assertIn("KB", out)
        self.assertNotIn("20260102-000000-2222", out)
        self.assertNotIn("20260103-000000-3333", out)
        self.assertNotIn("20260104-000000-4444", out)
        self.assertTrue(os.path.isdir(remote))
        self.assertTrue(os.path.isdir(unbrought))
        self.assertTrue(os.path.isdir(fresh))
        self.assertTrue(os.path.isdir(live))
        self.assertNotIn("rm -rf", self.ssh_log())

    def test_yes_removes_only_closed_and_fetched_jobs(self):
        _, removable = self.remote_job("20260101-000000-1111", lease_age=3600, final=True)
        _, unbrought = self.remote_job("20260102-000000-2222", fetched=False, lease_age=3600, final=True)
        _, fresh = self.remote_job("20260103-000000-3333", lease_age=1)
        _, live = self.remote_job("20260104-000000-4444", ended=False, lease_age=1)

        rc, out, err = self.run_cli(clean.main, "--yes")
        self.assertEqual(rc, 0, err)
        self.assertIn("20260101-000000-1111", out)
        self.assertFalse(os.path.exists(removable))
        self.assertTrue(os.path.isdir(unbrought))
        self.assertTrue(os.path.isdir(fresh))
        self.assertTrue(os.path.isdir(live))
        self.assertIn("rm -rf", self.ssh_log())

    def test_node_and_age_filters(self):
        _, recent = self.remote_job("20260101-000000-1111", lease_age=3600, final=True)
        rc, out, err = self.run_cli(clean.main, "--node", "box", "--older-than", "30d")
        self.assertEqual(rc, 0, err)
        self.assertNotIn("20260101-000000-1111", out)
        self.assertTrue(os.path.isdir(recent))

    def test_fetched_sha_in_meta_is_required_when_the_key_exists(self):
        _, empty = self.remote_job("20260101-000000-1111", lease_age=3600, final=True, extra={"FETCHED": ""})
        _, sha = self.remote_job("20260102-000000-2222", lease_age=3600, final=True, extra={"FETCHED": "abc1234def"})
        _, old = self.remote_job("20260103-000000-3333", lease_age=3600, final=True)   # no FETCHED: old rule
        rc, out, err = self.run_cli(clean.main, "--node", "box", "--older-than", "1d", "--yes")
        self.assertEqual(rc, 0, err)
        self.assertTrue(os.path.isdir(empty))
        self.assertFalse(os.path.exists(sha))
        self.assertFalse(os.path.exists(old))

    def test_fetched_sha_wins_over_a_noisy_fetch_log(self):            # finding 9/11: ssh warnings in fetch.log
        _, noisy = self.remote_job("20260101-000000-1111", lease_age=3600, final=True, fetched=False,
                                   extra={"FETCHED": "abc1234def"})
        _, failed = self.remote_job("20260102-000000-2222", lease_age=3600, final=True, fetched=False)
        rc, out, err = self.run_cli(clean.main, "--node", "box", "--older-than", "1d", "--yes")
        self.assertEqual(rc, 0, err)
        self.assertFalse(os.path.exists(noisy))
        self.assertTrue(os.path.isdir(failed))

    def test_job_without_lease_is_removed_but_unreadable_dir_is_not(self):
        _, nolease = self.remote_job("20260101-000000-1111")
        _, denied = self.remote_job("20260102-000000-2222", lease_age=3600, final=True)
        if os.geteuid() == 0:
            self.skipTest("root ignores permissions")
        os.chmod(denied, 0)
        self.addCleanup(os.chmod, denied, 0o755)
        rc, out, err = self.run_cli(clean.main, "--node", "box", "--older-than", "1d", "--yes")
        self.assertEqual(rc, 0, err)
        self.assertFalse(os.path.exists(nolease))
        self.assertTrue(os.path.isdir(denied))

    def test_permission_denied_on_lease_file_is_not_no_lease(self):
        if os.geteuid() == 0:
            self.skipTest("root ignores permissions")
        _, denied = self.remote_job("20260101-000000-1111", lease_age=3600, final=True)
        os.chmod(os.path.join(denied, "lease.json"), 0)
        rc, out, err = self.run_cli(clean.main, "--node", "box", "--older-than", "1d", "--yes")
        self.assertTrue(os.path.isdir(denied))


if __name__ == "__main__":
    unittest.main()

"""kindling runner: one ephemeral microVM per job, driven through tests/fakes/kling-bin."""
import nod_sandbox  # noqa: F401  (Nod events of the tests go to a temp file)
import contextlib
import io
import os
import subprocess
import sys
from unittest import mock

from launch_env import FAKES, LaunchCase, sh

from delega import jobs, util

TOKEN = "tok-s3cret-123"
OWNER = "tester"          # the orchestrator label of these tests ($DELEGA_OWNER)


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

    def fake_vm(self, name, job=None, lease_age=None, end=False, final=False, state=None, owner=OWNER,
                orchestrator="ours"):
        """A VM on the fake host (no golden home) and, with job, its job dir here and lease inside it.
        With state, the state the host reports (starting, creating…); with owner, the legacy
        `delega.owner` label; with orchestrator, the canonical `delega.orch` label ("ours", another
        name, or None for no label)."""
        import json
        import time
        reg = os.path.join(self.node_home, ".kling-vms")
        os.makedirs(reg, exist_ok=True)
        info = {}
        if state:
            info["state"] = state
        labels = {}
        if owner:
            labels["delega.owner"] = owner
        if orchestrator == "ours":
            labels["delega.orch"] = jobs.orchestrator_label(self.state)
        elif orchestrator:
            labels["delega.orch"] = orchestrator
        if labels:
            info["labels"] = labels
        with open(os.path.join(reg, name), "w") as f:
            json.dump(info, f)
        if job:
            chan = f"delega-jobs/{job}"
            self.new_job(id=job, NODE="vm", VM=name, CHANNEL=chan, **({"END": "1"} if end else {}))
            if lease_age is not None:
                d = os.path.join(reg, name + ".root") + self.vmhome + "/" + chan
                os.makedirs(d, exist_ok=True)
                with open(os.path.join(d, "lease.json"), "w") as f:
                    json.dump({"pid": 1, "renewed": time.time() - lease_age, **({"final": True} if final else {})}, f)

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

    def uploaded(self):
        """Sizes of what was uploaded as (a piece of) the job's in.bundle."""
        return [int(l.split()[1]) for l in self.kling_log().splitlines()
                if l.startswith("up-bytes") and "in.bundle" in l]

    def vm_git(self, *a):
        return sh("git", "-C", os.path.join(self.vmhome, "wt"), *a)

    def test_head_behind_golden_uploads_nothing(self):
        sh("git", "-C", self.repo, "reset", "-q", "--hard", "HEAD~1")
        rc, out, err = self.run_cli([self.repo, self.brief, "--node", "vm", "--branch", "delega/k2"])
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.uploaded(), [], self.kling_log())

    def test_diverged_head_sends_only_its_own_commits(self):
        wt = os.path.join(self.goldens, "claude-api", "wt")      # the golden's base moves on its own
        sh("git", "-C", wt, "checkout", "-q", "delega-base")
        with open(os.path.join(wt, "golden-only.dat"), "wb") as f:
            f.write(os.urandom(300_000))
        sh("git", "-C", wt, "add", "-f", ".")
        sh("git", "-C", wt, "-c", "user.name=t", "-c", "user.email=t@e.x", "commit", "-qm", "golden only")
        rc, out, err = self.run_cli([self.repo, self.brief, "--node", "vm", "--branch", "delega/k3"])
        self.assertEqual(rc, 0, out + err)
        sizes = self.uploaded()
        self.assertEqual(len(sizes), 1, self.kling_log())
        self.assertLess(sizes[0], 100_000)
        self.assertEqual(sh("git", "-C", self.repo, "show", "delega/k3:src/app/new.py"), "x = 1")
        self.assertNotEqual(subprocess.run(["git", "-C", self.repo, "cat-file", "-e", "delega/k3:golden-only.dat"]).returncode, 0)

    def test_bundle_over_the_upload_limit_arrives_whole_in_pieces(self):
        with open(os.path.join(self.repo, "big.dat"), "wb") as f:
            for _ in range(70):
                f.write(os.urandom(1 << 20))
        sh("git", "-C", self.repo, "add", "-f", ".")
        sh("git", "-C", self.repo, "commit", "-qm", "big")
        rc, out, err = self.run_cli([self.repo, self.brief, "--node", "vm", "--branch", "delega/k4"])
        self.assertEqual(rc, 0, out + err)
        sizes = [n for n in self.uploaded() if n > 0]
        self.assertGreaterEqual(len(sizes), 2, self.kling_log())
        self.assertTrue(all(n <= 64 << 20 for n in sizes))
        self.assertGreater(sum(sizes), 70 << 20)
        self.assertEqual(os.path.getsize(os.path.join(self.repo, "big.dat")),
                         int(sh("git", "-C", self.repo, "cat-file", "-s", "delega/k4:big.dat")))

    def test_with_ref_creates_the_branch_in_the_vm(self):
        sh("git", "-C", self.repo, "branch", "claude/otra", "HEAD")
        sh("git", "-C", self.repo, "checkout", "-q", "-b", "tmp")
        with open(os.path.join(self.repo, "otra.txt"), "w") as f:
            f.write("o\n")
        sh("git", "-C", self.repo, "add", ".")
        sh("git", "-C", self.repo, "commit", "-qm", "only in otra")
        sh("git", "-C", self.repo, "branch", "-f", "claude/otra", "HEAD")
        sh("git", "-C", self.repo, "checkout", "-q", "-")
        from delega import kindling
        with mock.patch.object(kindling, "destroy"):       # keep the VM's checkout to look at it
            rc, out, err = self.run_cli([self.repo, self.brief, "--node", "vm", "--branch", "delega/k5",
                                         "--with-ref", "claude/otra"])
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.vm_git("log", "-1", "--format=%s", "claude/otra"), "only in otra")

    def test_with_ref_unknown_branch_is_refused(self):
        rc, out, err = self.run_cli([self.repo, self.brief, "--node", "vm", "--with-ref", "no/existe"])
        self.assertNotEqual(rc, 0)
        self.assertIn("no/existe", out + err)

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
        self.fake_vm("delega-20260101-000000-a", "20260101-000000-a", lease_age=1)
        self.fake_vm("delega-20260101-000000-b", "20260101-000000-b", lease_age=1)
        self.fake_vm("delega-20260101-000000-c", "20260101-000000-c", lease_age=1, end=True)
        self.fake_vm("delega-seed-db-api-main")
        from delega import config, kindling
        cfg = config.load()
        res = kindling.probe(cfg, cfg.node("vm"), cfg.profile("work"))
        self.assertFalse(res["ok"])
        self.assertIn("max_jobs=2", res["reason"])
        self.assertIn("2 delega VMs with a live job", res["reason"])       # the seed sandbox is not a job
        self.assertIn("delega-20260101-000000-c (ended)", res["reason"])  # listed, not counted
        self.assertIn("delega gc --node vm", res["fix"])

    def fill_host(self):
        self.fake_vm("delega-20260101-000000-a", "20260101-000000-a", lease_age=1)
        self.fake_vm("delega-20260101-000000-b", "20260101-000000-b", lease_age=1)
        return self.jobs_in_state()

    def queued(self):
        from delega import queue
        return queue.pending(queue.read_items(self.cfg()))

    def test_full_host_queues_once_and_leaves_no_job_folder(self):
        # 26-oct: a caller relaunched every ~30 s with the host full and left one launch_failed folder
        # per try. Now nothing is created, the brief is queued once and the retries find it there.
        before = self.fill_host()
        for i in range(3):
            rc, out, err = self.run_cli([self.repo, self.brief, "--node", "vm", "--cap-min", "30"])
            self.assertEqual(rc, 6, out + err)
            self.assertIn("already queued" if i else "queued as", err)
        self.assertEqual(self.jobs_in_state(), before)
        q = self.queued()
        self.assertEqual(len(q), 1)
        self.assertEqual(q[0]["args"], ["--node", "vm", "--cap-min", "30"])
        self.assertNotIn("run -name", self.kling_log())

    def test_host_out_of_memory_is_full_too(self):
        os.environ["FAKE_KLING_NO_ROOM"] = "1"
        self.addCleanup(os.environ.pop, "FAKE_KLING_NO_ROOM", None)
        rc, out, err = self.run_cli([self.repo, self.brief, "--node", "vm"])
        self.assertEqual(rc, 6, out + err)
        self.assertIn("doesn't fit", err)
        self.assertEqual(self.jobs_in_state(), [])
        self.assertEqual(len(self.queued()), 1)

    def test_full_host_fails_once_when_queueing_is_off_or_from_the_queue(self):
        before = self.fill_host()
        for env in ({"DELEGA_QUEUE_WHEN_FULL": "fail"}, {"DELEGA_FROM_QUEUE": "1"}):
            with mock.patch.dict(os.environ, env):
                rc, out, err = self.run_cli([self.repo, self.brief, "--node", "vm"])
            self.assertEqual(rc, 6, out + err)
            self.assertIn("delega queue add", err)
        self.assertEqual(self.jobs_in_state(), before)
        self.assertEqual(self.queued(), [])

    def test_tick_keeps_the_item_queued_while_the_node_is_full(self):
        from delega import queue
        self.fill_host()
        item = queue.add(self.cfg(), self.repo, self.brief, run_args=["--node", "vm"])
        with mock.patch.object(queue, "room", lambda *a: (True, "")), \
                mock.patch.object(queue, "launch", lambda cfg, it: ("full", "")):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                queue.main_tick([])
        self.assertEqual([i["id"] for i in self.queued()], [item["id"]])
        self.assertIn("node full", out.getvalue())

    def test_orphans_do_not_count(self):
        from delega import config, kindling
        self.fake_vm("delega-20260101-000000-a", "20260101-000000-a", lease_age=1)
        self.fake_vm("delega-20260101-000000-c", "20260101-000000-c", lease_age=1, end=True)
        self.fake_vm("delega-20260101-000000-d", state="frozen")                     # not a job of ours
        self.fake_vm("delega-20260101-000000-e", "20260101-000000-e", lease_age=3600)  # broker gone
        self.fake_vm("delega-20260101-000000-f", "20260101-000000-f", lease_age=1, final=True)  # finished
        cfg = config.load()
        res = kindling.probe(cfg, cfg.node("vm"), cfg.profile("work"))
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["vms"], 1)
        self.assertEqual(dict(res["orphans"]), {"delega-20260101-000000-c": "ended", "delega-20260101-000000-d": "unknown",
                                                "delega-20260101-000000-e": "stale", "delega-20260101-000000-f": "stale"})

    def test_gc_lists_then_deletes_only_ended_and_unknown(self):
        import contextlib
        import io

        from delega import config, jobs
        self.fake_vm("delega-20260101-000000-a", "20260101-000000-a", lease_age=1)
        self.fake_vm("delega-20260101-000000-c", "20260101-000000-c", lease_age=1, end=True)
        self.fake_vm("delega-20260101-000000-d", state="frozen")
        self.fake_vm("delega-20260101-000000-e", "20260101-000000-e", lease_age=3600)
        reg = os.path.join(self.node_home, ".kling-vms")
        before = sorted(x for x in os.listdir(reg) if not x.endswith(".root"))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(jobs.main_gc([]), 0)
        self.assertEqual(sorted(x for x in os.listdir(reg) if not x.endswith(".root")), before)   # nothing deleted
        self.assertIn("delega-20260101-000000-c (job ended) would be deleted", buf.getvalue())
        self.assertIn("delega-20260101-000000-d (job unknown here) would be deleted", buf.getvalue())
        self.assertIn("delega recover 20260101-000000-e", buf.getvalue())
        self.assertIn("--yes", buf.getvalue())
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(jobs.gc(config.load(), yes=True), 2)
        left = sorted(x for x in os.listdir(reg) if not x.endswith(".root"))
        self.assertEqual(left, ["delega-20260101-000000-a", "delega-20260101-000000-e"])

    def test_gc_keeps_the_vms_of_another_orchestrator(self):
        # the host is shared (the personal one and Trycore's on the same kling): the VMs of the other
        # orchestrator are alive, so gc lists them and leaves them alone
        import contextlib
        import io

        from delega import config, jobs
        self.fake_vm("delega-20260101-000000-c", "20260101-000000-c", lease_age=1, end=True)
        self.fake_vm("delega-20260101-000000-d", owner=OWNER, state="frozen")  # ours, job dir is gone
        self.fake_vm("delega-20260101-000000-x", owner="trycore", orchestrator="trycore", state="frozen")
        self.fake_vm("delega-20260101-000000-y", owner=None, orchestrator=None, state="frozen")
        self.fake_vm("delega-20260101-000000-z", owner=OWNER, orchestrator=None, state="frozen")
        reg = os.path.join(self.node_home, ".kling-vms")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(jobs.gc(config.load()), 2)                   # only ours would be deleted
        self.assertIn("another orchestrator (trycore)", buf.getvalue())
        self.assertIn("no owner label", buf.getvalue())
        self.assertIn("old label (delega.owner=%s, no delega.orch); delete it by hand with `kling rm delega-20260101-000000-z`" % OWNER,
                      buf.getvalue())
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(jobs.gc(config.load(), yes=True), 2)       # only ours
        left = sorted(x for x in os.listdir(reg) if not x.endswith(".root"))
        self.assertEqual(left, ["delega-20260101-000000-x", "delega-20260101-000000-y",
                                "delega-20260101-000000-z"])
        out = buf.getvalue()
        self.assertIn("another orchestrator (trycore)", out)
        self.assertIn("no owner label", out)
        self.assertEqual(out.count("deleted"), 2)      # only ours

    def test_a_live_vm_of_another_orchestrator_counts_for_max_jobs(self):
        from delega import config, kindling
        self.fake_vm("delega-20260101-000000-a", "20260101-000000-a", lease_age=1)
        self.fake_vm("delega-20260101-000000-b", orchestrator="other")
        cfg = config.load()
        res = kindling.probe(cfg, cfg.node("vm"), cfg.profile("work"))
        self.assertFalse(res["ok"])
        self.assertIn("max_jobs=2", res["reason"])
        self.assertIn("2 delega VMs with a live job", res["reason"])

    def test_a_vm_being_set_up_counts_as_a_live_job(self):
        # two launches at once: the first VM is still being created. If it did not count, both would
        # fit in max_jobs=2 and the host would run out of memory
        from delega import config, kindling
        self.fake_vm("delega-20260101-000000-a", "20260101-000000-a", lease_age=1)
        self.fake_vm("delega-20260101-000000-b", "20260101-000000-b", state="starting")
        cfg = config.load()
        res = kindling.probe(cfg, cfg.node("vm"), cfg.profile("work"))
        self.assertFalse(res["ok"])
        self.assertIn("max_jobs=2", res["reason"])
        self.assertIn("2 delega VMs with a live job", res["reason"])

    def test_vm_gets_ttl_and_labels(self):
        rc, out, err = self.run_cli([self.repo, self.brief, "--node", "vm", "--cap-min", "10"])
        self.assertEqual(rc, 0, out + err)
        job = util.read_meta(self.job_dir())["VM"][len("delega-"):]
        # cap 10 min + grace 300 s + margin 30 min
        self.assertIn(f"-ttl 45m -on-ttl freeze -label delega.job={job} -label delega.owner=tester "
                      f"-label delega.orch={jobs.orchestrator_label(self.state)} -wait-ready",
                      self.kling_log())

    def cut_ssh_on_first_poll(self, how):
        """Patch the per-poll hook so ssh to the host starts failing once the worker is running."""
        from delega import nod
        state = {"done": False}

        def cut(*a, **k):
            if not state["done"]:
                state["done"] = True
                with open(os.path.join(self.node_home, ".ssh.fail"), "w") as f:
                    f.write(how)
        return mock.patch.object(nod, "pull_and_forward", cut)

    def test_ssh_fails_a_few_times_then_comes_back(self):
        os.environ.update(STUB_SLEEP="0.5", STUB_COMMIT_TEXT="survived")
        with self.cut_ssh_on_first_poll("3"):
            rc, out, err = self.run_cli([self.repo, self.brief, "--node", "vm", "--branch", "delega/k2"])
        self.assertEqual(rc, 0, out + err)
        self.assertIn("retry 1/5", err)
        self.assertIn("retry 3/5", err)
        self.assertNotIn("retry 4/5", err)
        self.assertEqual(sh("git", "-C", self.repo, "show", "delega/k2:result.txt"), "survived")
        self.assertRegex(self.kling_log(), r"\nrm delega-")

    def test_vm_clock_behind_is_not_a_lost_broker(self):
        # a VM restored from a golden whose clock kling could not resync: its lease looks 300 s old by
        # this machine's clock, yet the broker is alive. Seen on a real host: rc 97 and the VM destroyed.
        os.environ.update(FAKE_VM_CLOCK_SKEW_S="300", STUB_SLEEP="1.5", STUB_COMMIT_TEXT="on time")
        rc, out, err = self.run_cli([self.repo, self.brief, "--node", "vm", "--branch", "delega/k4"])
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(sh("git", "-C", self.repo, "show", "delega/k4:result.txt"), "on time")

    def disconnect(self, **env):
        """Launch a job and cut ssh for good once it runs: rc 96. The worker waits for self.go."""
        self.go = os.path.join(self.root, "go")
        os.environ.update(STUB_WAIT_FILE=self.go, STUB_COMMIT_TEXT="kept", **env)
        with self.cut_ssh_on_first_poll("always"):
            rc, out, err = self.run_cli([self.repo, self.brief, "--node", "vm", "--branch", "delega/k3"])
        self.addCleanup(lambda: open(self.go, "w").close())      # never leave the stub waiting
        return rc, out, err

    def release_worker(self):
        import time
        if os.path.exists(os.path.join(self.node_home, ".ssh.fail")):
            os.remove(os.path.join(self.node_home, ".ssh.fail"))
        open(self.go, "w").close()
        rc_file = os.path.join(self.vmhome, util.read_meta(self.job_dir())["CHANNEL"], "rc")
        for _ in range(200):
            if os.path.exists(rc_file):
                return
            time.sleep(0.05)
        self.fail("worker did not finish")

    def test_no_contact_is_96_keeps_vm_and_adopt_brings_branch(self):
        rc, out, err = self.disconnect()
        self.assertEqual(rc, 96, out + err)
        self.assertIn("delega adopt", err)
        self.assertIn("Nothing was destroyed", err)
        meta = util.read_meta(self.job_dir())
        self.assertTrue(meta.get("DISCONNECTED"))
        self.assertFalse(meta.get("END"))
        self.assertNotRegex(self.kling_log(), r"\nrm delega-")
        self.assertTrue(os.path.exists(os.path.join(self.node_home, ".kling-vms", meta["VM"])))
        from delega import config, jobs
        with open(os.path.join(self.job_dir(), "waiter.pid"), "w") as f:
            f.write("0")                                                 # the waiter that got 96 is gone
        self.assertIn((self.job_dir(), "disconnected"), jobs.reconcile(config.load()))
        self.assertFalse(util.read_meta(self.job_dir()).get("END"))      # reconcile does not close it
        self.release_worker()
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(jobs.adopt(config.load(), self.job_dir(), poll_s=0.05), 0)
        self.assertEqual(sh("git", "-C", self.repo, "show", "delega/k3:result.txt"), "kept")
        self.assertTrue(util.read_meta(self.job_dir()).get("END"))
        self.assertRegex(self.kling_log(), r"\nrm delega-")

    def test_recover_brings_a_branch_bigger_than_exec_output(self):
        rc, out, err = self.disconnect(STUB_BIG_MB="9")
        self.assertEqual(rc, 96, out + err)
        import contextlib
        import io

        from delega import config, jobs
        os.remove(os.path.join(self.node_home, ".ssh.fail"))
        e = io.StringIO()
        with contextlib.redirect_stderr(e):
            self.assertEqual(jobs.recover(config.load(), self.job_dir()), 2)      # worker alive: adopt, not recover
        self.assertIn("delega adopt", e.getvalue())
        self.release_worker()
        o = io.StringIO()
        with contextlib.redirect_stdout(o):
            self.assertEqual(jobs.recover(config.load(), self.job_dir()), 0, o.getvalue())
        self.assertEqual(sh("git", "-C", self.repo, "show", "delega/k3:result.txt"), "kept")
        size = int(sh("git", "-C", self.repo, "cat-file", "-s", "delega/k3:big.bin"))
        self.assertEqual(size, 9 << 20)
        self.assertGreater(os.path.getsize(os.path.join(self.job_dir(), "out.bundle")), 8 << 20)
        self.assertRegex(self.kling_log(), r"cp delega-[^ ]+:[^ ]*out\.bundle ")     # a file, not stdout
        self.assertTrue(util.read_meta(self.job_dir()).get("END"))
        self.assertRegex(self.kling_log(), r"\nrm delega-")

    def test_requires_compute_refused(self):
        rc, out, err = self.run_cli([self.repo, self.brief, "--node", "vm", "--requires", "tool"])
        self.assertEqual(rc, 5, out + err)
        self.assertIn("kindling", err)


if __name__ == "__main__":
    import unittest
    unittest.main()

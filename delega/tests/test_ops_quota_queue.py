import nod_sandbox  # noqa: F401  (Nod events of the tests go to a temp file)
import json
import os
import time
import unittest
from unittest import mock

from ops_helpers import OpsCase, rate_limit_line, today
from delega import jobs, queue, quota, handoff

TOML = """
[general]
default_profile = "work"
[policy]
max_local_jobs = 2
expected_hours = 2
[profiles.work]
owner = "self"
[profiles.other]
owner = "self"
"""


class QuotaTest(OpsCase):
    TOML = TOML

    def test_limits_from_channels_takes_freshest_event(self):
        now = int(time.time())
        a = self.make_job("20260101-000001-1-a", files={"events.jsonl": rate_limit_line(now, u5=0.1)})
        b = self.make_job("20260101-000002-2-b", files={"eventos.jsonl": rate_limit_line(now, u5=0.5)})   # legacy name
        os.utime(os.path.join(a, "events.jsonl"), (now - 500, now - 500))
        recs = quota.limits_from_channels([a, b, os.path.join(self.root, "missing")])
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["info"]["unifiedWindows"]["five_hour"]["utilization"], 0.5)
        self.assertEqual(quota.limits_from_channels([]), [])

    def test_compute_capacity_without_history_uses_conservative_pace(self):
        now = 1_000_000.0
        rec = {"mtime": now, "info": json.loads(rate_limit_line(int(now), u5=0.2, u7=0.1))["rate_limit_info"]}
        c = quota.compute_capacity(rec, [], live=1, target5=70, target7=90, slots=5, hours=2, now=now)
        # (70-20) / (12 %/h * min(2 h left, 2 h expected)) = 2
        self.assertEqual((c["max5"], c["total"], c["new"]), (2, 2, 1))
        self.assertTrue(c["estimated"])
        full = quota.compute_capacity(rec, [], 0, 10, 90, 5, 2, now)       # already above the 5 h target
        self.assertEqual(full["total"], 0)

    def test_compute_capacity_learns_pace_from_history(self):
        now = 1_000_000.0
        rec = {"mtime": now, "info": json.loads(rate_limit_line(int(now), u5=0.30, u7=0.1))["rate_limit_info"]}
        r5 = str(int(now) + 7200)
        # two workers burn 10 points in half an hour => 10 %/h per worker
        rows = [(now - 1800, 20.0, 10.0, r5, "w", 2), (now, 30.0, 10.0, r5, "w", 2)]
        c = quota.compute_capacity(rec, rows, 2, 70, 90, 9, hours=1, now=now)
        self.assertFalse(c["estimated"])
        self.assertAlmostEqual(c["pace5"], 10.0)
        self.assertEqual(c["max5"], 4)                                    # (70-30)/(10*1)

    def test_history_is_per_profile_and_reads_legacy(self):
        now = time.time()
        with open(os.path.join(self.state, "limite.tsv"), "w") as f:      # legacy: no profile column
            f.write(f"{now - 60}\t10.0\t5.0\t111\t222\t1\n")
        with open(os.path.join(self.state, "limits.tsv"), "w") as f:
            f.write(f"{now - 30}\tother\t40.0\t5.0\t111\t222\t1\n")
        cfg = self.cfg()
        self.assertEqual(len(quota.read_history(cfg, "work")), 1)         # legacy row = default profile
        self.assertEqual(quota.read_history(cfg, "other")[0][1], 40.0)

    def test_capacity_end_to_end_and_cli(self):
        now = int(time.time())
        self.make_job(f"{today()}-000001-1-a", {"PROFILE": "work"}, {"events.jsonl": rate_limit_line(now)})
        self.make_job(f"{today()}-000002-2-b", {"PROFILE": "other", "END": str(now)}, {})
        cfg = self.cfg()
        c = quota.capacity(cfg, "work", probe=False)
        self.assertTrue(c["has_data"])
        self.assertEqual((c["slots"], c["live"]), (2, 1))                  # max_local_jobs only, no nodes
        self.assertEqual(c["total"], 2)
        self.assertEqual(quota.capacity(cfg, "other", probe=False)["has_data"], False)
        rc, out, _ = self.run_cli(quota.main, "--profile", "work", "--no-probe")
        self.assertEqual(rc, 0)
        self.assertIn("QUOTA: 2 workers at once", out)
        self.assertIn("spend today: $0.00 of $150.00", out)
        rows = open(os.path.join(self.state, "limits.tsv")).read().splitlines()
        self.assertEqual(len(rows), 1)                                      # repeated reads do not duplicate the sample
        self.assertTrue(rows[0].split("\t")[1] == "work")

    def test_slots_include_node_max_jobs_bounded_by_ram(self):
        self.write_config(TOML + '\n[nodes.box]\nssh = "box"\nmax_jobs = 4\n[profiles.work2]\nowner="self"\nnodes=["box"]\n')
        cfg = self.cfg()
        fake = mock.Mock(return_value={"ok": True, "mem_mb": 3200})
        with mock.patch("delega.nodes.probe", fake, create=True):
            self.assertEqual(quota.node_slots(cfg, "work2"), 2 + 2)        # 3200 // 1500 = 2 < max_jobs
        with mock.patch("delega.nodes.probe", mock.Mock(return_value={"ok": False}), create=True):
            self.assertEqual(quota.node_slots(cfg, "work2"), 2)


class QueueTest(OpsCase):
    TOML = TOML

    def brief(self, text="# Goal\nx\n"):
        p = os.path.join(self.root, "brief.md")
        open(p, "w").write(text)
        return p

    def test_add_ls_and_priority_order(self):
        repo = self.make_repo()
        cfg = self.cfg()
        low = queue.add(cfg, repo, self.brief(), prio=0)
        time.sleep(0.01)
        high = queue.add(cfg, repo, self.brief(), prio=5, run_args=["--profile", "other"])
        order = [i["id"] for i in queue.pending(queue.read_items(cfg))]
        self.assertEqual(order, [high["id"], low["id"]])
        rc, out, _ = self.run_cli(queue.main_queue, "ls")
        self.assertIn("prio=5", out)
        rc, out, _ = self.run_cli(queue.main_queue, "add", repo, self.brief(), "--prio", "3", "--", "--cap-min", "30")
        self.assertEqual(rc, 0)
        self.assertEqual(queue.read_items(cfg)[-1]["args"], ["--cap-min", "30"])

    def test_tick_launches_in_order_until_room_runs_out(self):
        repo = self.make_repo()
        cfg = self.cfg()
        a = queue.add(cfg, repo, self.brief(), prio=1)
        b = queue.add(cfg, repo, self.brief(), prio=9)
        c = queue.add(cfg, repo, self.brief(), prio=0)
        launched = []

        def fake_launch(cfg, item):
            launched.append(item["id"])
            item["log"] = "x.log"
            return "launched", "123"

        cap = {"has_data": True, "new": 2, "live": 0, "slots": 2}
        with mock.patch.object(jobs, "reconcile", lambda cfg: [], create=True), \
                mock.patch.object(queue, "launch", fake_launch), \
                mock.patch("delega.quota.capacity", lambda cfg, name, **k: cap):
            rc, out, _ = self.run_cli(queue.main_tick)
        self.assertEqual(launched, [b["id"], a["id"]])
        self.assertIn(f"{c['id']} waits", out)
        states = {i["id"]: i["state"] for i in queue.read_items(cfg)}
        self.assertEqual(states, {a["id"]: "launched", b["id"]: "launched", c["id"]: "queued"})
        self.assertIn("1 still queued", out)

    def test_tick_respects_budget_and_adopts_orphans(self):
        repo = self.make_repo()
        cfg = self.cfg()
        queue.add(cfg, repo, self.brief())
        self.write_config(TOML.replace('owner = "self"\n[profiles.other]', 'owner = "self"\npolicy.budget.day_usd = 1\n[profiles.other]'))
        self.make_job(f"{today()}-000001-1-a", {"PROFILE": "work"}, {"summary.json": {"cost_usd": 5}})
        adopted = []
        cap = {"has_data": True, "new": 3, "live": 0, "slots": 3}
        with mock.patch.object(jobs, "reconcile", lambda cfg: [("/x/job1", "orphan"), ("/x/job2", "closed")], create=True), \
                mock.patch.object(jobs, "adopt", lambda cfg, d: adopted.append(d), create=True), \
                mock.patch.object(queue, "launch", side_effect=AssertionError("must not launch")), \
                mock.patch("delega.quota.capacity", lambda cfg, name, **k: cap):
            rc, out, _ = self.run_cli(queue.main_tick, "--adopt")
        self.assertEqual(adopted, ["/x/job1"])
        self.assertIn("blocked by budget", out)

    def test_launch_reports_fast_failure(self):
        repo = self.make_repo()
        cfg = self.cfg()
        item = queue.add(cfg, repo, self.brief(), run_args=["--bogus"])
        with mock.patch.object(queue.subprocess, "Popen") as popen:
            popen.return_value.wait.return_value = 8
            popen.return_value.pid = 42
            state, detail = queue.launch(cfg, item)
        self.assertEqual((state, detail), ("failed", "rc=8"))
        argv = popen.call_args[0][0]
        self.assertEqual(argv[1:4], ["-m", "delega", "run"])


class HandoffTest(OpsCase):
    TOML = TOML

    def test_handoff_draft_and_audit(self):
        d = self.make_job(f"{today()}-111111-9-api", {"NODE": "local", "BRANCH": "delega/x", "PROFILE": "work"},
                          {"brief.md": "# Goal\nfix the thing\n",
                           "results.jsonl": json.dumps({"result": "did   part\none"}) + "\n"})
        out = os.path.join(self.root, "h.md")
        rc, msg, _ = self.run_cli(handoff.main, "9-api", out)
        self.assertEqual(rc, 0)
        text = open(out).read()
        self.assertIn("fix the thing", text)
        self.assertIn("> did part one", text)
        self.assertIn("`delega/x`", text)
        self.assertIn("--branch delega/x", msg)
        recs = [json.loads(line) for line in open(os.path.join(self.state, "audit.jsonl"))]
        self.assertEqual((recs[0]["event"], recs[0]["job"]), ("handoff", os.path.basename(d)))
        os.environ["DELEGA_LOCALE"] = "es"
        out_es = os.path.join(self.root, "h_es.md")
        self.run_cli(handoff.main, "9-api", out_es)
        self.assertIn("## Dónde quedó", open(out_es).read())

    def test_unknown_job(self):
        rc, _, err = self.run_cli(handoff.main, "nope")
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()

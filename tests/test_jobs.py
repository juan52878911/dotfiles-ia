import contextlib
import io
import json
import os
import subprocess
import time
import unittest

try:
    from tests.launch_env import LaunchCase
except ImportError:  # run from inside tests/
    from launch_env import LaunchCase

from delega import jobs, util


def write(path, text):
    with open(path, "w") as f:
        f.write(text)


def captured(fn, *a, **kw):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = fn(*a, **kw)
    return rc, out.getvalue(), err.getvalue()


class LivenessTests(LaunchCase):
    def local_job(self, **kw):
        d = self.new_job(NODE="local", **kw)
        util.add_meta(d, CHANNEL=d)
        return d

    def test_launching_job_with_no_files_is_alive_until_old(self):
        d = self.local_job()
        self.assertTrue(jobs.alive(self.cfg(), d))
        old = time.time() - 13 * 3600
        os.utime(os.path.join(d, "meta"), (old, old))
        self.assertFalse(jobs.alive(self.cfg(), d))

    def test_end_means_closed(self):
        d = self.local_job(END="1")
        self.assertFalse(jobs.alive(self.cfg(), d))

    def test_fresh_lease_is_alive_stale_lease_with_dead_pid_is_not(self):
        d = self.local_job()
        write(os.path.join(d, "lease.json"), json.dumps({"pid": 1, "host": "h", "renewed": time.time()}))
        self.assertTrue(jobs.alive(self.cfg(), d))
        dead = subprocess.Popen(["true"])
        dead.wait()
        write(os.path.join(d, "lease.json"), json.dumps({"pid": dead.pid, "host": "h", "renewed": time.time() - 999}))
        self.assertFalse(jobs.alive(self.cfg(), d))

    def test_stale_lease_with_live_pid_is_alive(self):
        d = self.local_job()
        write(os.path.join(d, "lease.json"), json.dumps({"pid": os.getpid(), "renewed": time.time() - 999}))
        self.assertTrue(jobs.alive(self.cfg(), d))

    def test_final_lease_is_closed(self):
        d = self.local_job()
        write(os.path.join(d, "lease.json"), json.dumps({"pid": os.getpid(), "renewed": time.time(), "final": True}))
        self.assertFalse(jobs.alive(self.cfg(), d))

    def test_rc_file_means_closed(self):
        d = self.local_job()
        write(os.path.join(d, "rc"), "0")
        self.assertFalse(jobs.alive(self.cfg(), d))

    def test_state_pid_fallback_and_legacy_names(self):
        d = self.local_job()
        write(os.path.join(d, "estado.json"), json.dumps({"pid": os.getpid()}))
        self.assertTrue(jobs.alive(self.cfg(), d))
        write(os.path.join(d, "estado.json"), json.dumps({"pid": 2 ** 22 + 12345}))
        self.assertFalse(jobs.alive(self.cfg(), d))

    def test_remote_alive_follows_waiter_then_meta_age(self):
        d = self.new_job(NODE="box", CHANNEL="delega/jobs/x")
        write(os.path.join(d, "waiter.pid"), str(os.getpid()))
        self.assertTrue(jobs.alive(self.cfg(), d))
        write(os.path.join(d, "waiter.pid"), "999999999")
        self.assertTrue(jobs.alive(self.cfg(), d))          # waiter dead but meta young: may still be running
        old = time.time() - 13 * 3600
        os.utime(os.path.join(d, "meta"), (old, old))
        self.assertFalse(jobs.alive(self.cfg(), d))

    def test_legacy_meta_keys_are_understood(self):
        d = self.new_job(id="20250101-000000-1", HOST="vm1", RJOB="orquestador/trabajos/x", DUENO="me")
        self.assertEqual(jobs.live_counts(self.cfg()), {"vm1": 1})
        util.add_meta(d, FIN="1")
        self.assertEqual(jobs.live_counts(self.cfg()), {})

    def test_live_counts_per_node_and_exclude(self):
        a = self.new_job(id="20260101-000001-1", NODE="local")
        self.new_job(id="20260101-000002-1", NODE="box")
        self.new_job(id="20260101-000003-1", NODE="box")
        self.new_job(id="20260101-000004-1", NODE="box", END="1")
        cfg = self.cfg()
        self.assertEqual(jobs.live_counts(cfg), {"local": 1, "box": 2})
        self.assertEqual(jobs.live_counts(cfg, exclude=a), {"box": 2})

    def test_reconcile_closes_dead_local_jobs_and_reports_orphans(self):
        dead = self.local_job(id="20260101-000001-1")
        write(os.path.join(dead, "lease.json"), json.dumps({"pid": 2 ** 22 + 1, "renewed": 1}))
        ok = self.local_job(id="20260101-000002-1")
        write(os.path.join(ok, "lease.json"), json.dumps({"pid": os.getpid(), "renewed": time.time()}))
        orphan = self.new_job(id="20260101-000003-1", NODE="box", CHANNEL="x")
        write(os.path.join(orphan, "waiter.pid"), "999999999")
        cfg = self.cfg()
        self.assertEqual(sorted(jobs.reconcile(cfg)), sorted([(dead, "closed"), (orphan, "orphan")]))
        self.assertTrue(util.read_meta(dead).get("END"))
        self.assertFalse(util.read_meta(ok).get("END"))
        self.assertFalse(util.read_meta(orphan).get("END"))
        self.assertEqual(jobs.reconcile(cfg), [(orphan, "orphan")])


class ResolveTests(LaunchCase):
    def test_resolve_last_and_pattern(self):
        a = self.new_job(id="20260101-000001-1", NODE="local")
        b = self.new_job(id="20260101-000002-1", NODE="local")
        cfg = self.cfg()
        self.assertEqual(jobs.resolve(cfg, "last"), b)
        self.assertEqual(jobs.resolve(cfg, "000001"), a)
        self.assertEqual(jobs.job_dirs(cfg), [a, b])
        with self.assertRaises(LookupError):
            jobs.resolve(cfg, "nope")


class SayTests(LaunchCase):
    def test_say_local_appends_to_the_inbox(self):
        d = self.new_job(NODE="local")
        util.add_meta(d, CHANNEL=d)
        rc, out, _ = captured(jobs.main_say, ["last", "hello", "there"])
        self.assertEqual(rc, 0)
        line = json.loads(open(os.path.join(d, "inbox.jsonl")).read())
        self.assertEqual(line["text"], "hello there")
        self.assertIn("ts", line)

    def test_say_to_a_finished_job_exits_6_with_the_resume_hint(self):
        d = self.new_job(NODE="local", END="1")
        write(os.path.join(d, "summary.json"), json.dumps({"session_id": "s-9"}))
        rc, out, err = captured(jobs.main_say, ["last", "hi"])
        self.assertEqual(rc, 6)
        self.assertIn("--resume s-9", err)

    def test_say_remote_goes_through_ssh(self):
        d = self.new_job(NODE="box", CHANNEL="delega/jobs/J")
        chan = os.path.join(self.node_home, "delega", "jobs", "J")
        os.makedirs(chan)
        self.assertEqual(jobs.say(self.cfg(), d, "ping"), 0)
        self.assertEqual(json.loads(open(os.path.join(chan, "inbox.jsonl")).read())["text"], "ping")
        write(os.path.join(chan, "rc"), "0")
        self.assertEqual(jobs.say(self.cfg(), d, "late"), 6)

    def test_unknown_job(self):
        rc, _, err = captured(jobs.main_say, ["nothing", "x"])
        self.assertEqual(rc, 7)


class PsTests(LaunchCase):
    def make(self, id, owner, **extra):
        d = self.new_job(id=id, NODE="local", OWNER=owner, BRANCH="delega/x", **extra)
        util.add_meta(d, CHANNEL=d)
        write(os.path.join(d, "state.json"), json.dumps({
            "state": "running", "start": time.time() - 600, "last_event": time.time() - 5, "tools": 4,
            "alerts": 1, "last_action": "Bash: ls", "rc": None, "cost_usd": 0.25, "pid": os.getpid()}))
        write(os.path.join(d, "progress.log"),
              f"{util.now()}\tNOTICE:milestone\tstep one done\n{util.now()}\tTOOL\tBash\n"
              f"{util.now()}\tALERT:stall\tquiet\n")
        return d

    def test_default_scope_is_own_owner_and_all_shows_everyone(self):
        self.make("20260101-000001-1", "tester")
        self.make("20260101-000002-1", "someone-else")
        rc, out, _ = captured(jobs.main_ps, [])
        self.assertEqual(rc, 0)
        self.assertIn("000001", out)
        self.assertNotIn("000002", out)
        rc, out, _ = captured(jobs.main_ps, ["--all"])
        self.assertIn("000001", out)
        self.assertIn("000002", out)

    def test_ps_line_content(self):
        self.make("20260101-000001-1", "tester")
        _, out, _ = captured(jobs.main_ps, ["last"])
        self.assertIn("running", out)
        self.assertIn("tools 4", out)
        self.assertIn("0.250 $", out)
        self.assertIn("Bash: ls", out)
        self.assertIn("NOTICE:milestone step one done", out)
        self.assertIn("ALERT:stall", out)
        self.assertNotIn("TOOL", out)

    def test_ps_marks_a_dead_local_broker(self):
        d = self.make("20260101-000001-1", "tester")
        write(os.path.join(d, "state.json"), json.dumps({"state": "running", "start": 1, "last_event": 2,
                                                         "pid": 2 ** 22 + 7, "session_id": "s-dead"}))
        write(os.path.join(d, "lease.json"), json.dumps({"pid": 2 ** 22 + 7, "renewed": 1}))
        _, out, _ = captured(jobs.main_ps, ["last"])
        self.assertIn("dead", out)
        self.assertIn("--resume s-dead", out)

    def test_no_jobs_message(self):
        rc, out, _ = captured(jobs.main_ps, [])
        self.assertEqual(rc, 0)
        self.assertIn("tester", out)


class WatchTests(LaunchCase):
    def job(self, log, **meta):
        d = self.new_job(NODE="local", OWNER="tester", **meta)
        util.add_meta(d, CHANNEL=d)
        write(os.path.join(d, "progress.log"), log)
        write(os.path.join(d, "lease.json"), json.dumps({"pid": os.getpid(), "renewed": time.time()}))
        return d

    def test_dedupes_identical_lines_and_stops_at_end(self):
        ts = util.now()
        log = (f"{ts}\tTOOL\tBash\n{ts}\tALERT:stall\tquiet for 10 min\n{ts}\tALERT:stall\tquiet for 10 min\n"
               f"{ts}\tNOTICE:milestone\thalf way\n{ts}\tNOTICE:milestone\thalf way\n"
               f"{ts}\tRESULT\tdone\n{ts}\tEND\trc=0\n")
        d = self.job(log)
        lines = []
        jobs.watch(self.cfg(), [d], out=lines.append, poll_s=0.01)
        self.assertEqual(len(lines), 4, lines)
        self.assertTrue(lines[0].startswith(f"[{jobs.job_id(d)}] ALERT:stall"))
        self.assertTrue(lines[-1].endswith("rc=0"))
        self.assertFalse(any("TOOL" in l for l in lines))

    def test_legacy_spanish_types_are_understood(self):
        ts = util.now()
        d = self.job(f"{ts}\tAVISO:hito\tlisto\n{ts}\tALERTA:atasco\tparado\n{ts}\tFIN\trc=0\n")
        lines = []
        jobs.watch(self.cfg(), [d], out=lines.append, poll_s=0.01)
        self.assertEqual([l.split()[1] for l in lines], ["AVISO:hito", "ALERTA:atasco", "FIN"])

    def test_old_events_are_skipped_unless_from_start(self):
        old = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - 3600))
        d = self.job(f"{old}\tNOTICE:milestone\tancient\n{util.now()}\tEND\trc=0\n")
        lines = []
        jobs.watch(self.cfg(), [d], out=lines.append, poll_s=0.01)
        self.assertEqual(len(lines), 1)
        lines = []
        jobs.watch(self.cfg(), [d], from_start=True, out=lines.append, poll_s=0.01)
        self.assertEqual(len(lines), 2)

    def test_dead_broker_without_end_raises_alert_dead(self):
        d = self.job(f"{util.now()}\tTOOL\tBash\n")
        write(os.path.join(d, "lease.json"), json.dumps({"pid": 2 ** 22 + 3, "renewed": 1}))
        lines = []
        jobs.watch(self.cfg(), [d], out=lines.append, poll_s=0.01)
        self.assertEqual(len(lines), 1)
        self.assertIn("ALERT:dead", lines[0])

    def test_closed_job_ends_the_watch(self):
        d = self.job("", END="1")
        lines = []
        jobs.watch(self.cfg(), [d], out=lines.append, poll_s=0.01)
        self.assertIn("END", lines[0])

    def test_watch_default_scope_is_own_running_jobs(self):
        self.job(f"{util.now()}\tEND\trc=0\n")
        other = self.new_job(id="20260101-000009-1", NODE="local", OWNER="other")
        util.add_meta(other, CHANNEL=other)
        rc, out, _ = captured(jobs.main_watch, [])
        self.assertEqual(rc, 0)
        self.assertEqual(out.count("END"), 1)

    def test_remote_watch_reads_progress_through_ssh(self):
        d = self.new_job(NODE="box", OWNER="tester", CHANNEL="delega/jobs/W")
        chan = os.path.join(self.node_home, "delega", "jobs", "W")
        os.makedirs(chan)
        write(os.path.join(chan, "progress.log"), f"{util.now()}\tNOTICE:blocker\tstuck\n{util.now()}\tEND\trc=1\n")
        lines = []
        jobs.watch(self.cfg(), [d], out=lines.append, poll_s=0.01)
        self.assertEqual([l.split()[1] for l in lines], ["NOTICE:blocker", "END"])


if __name__ == "__main__":
    unittest.main()

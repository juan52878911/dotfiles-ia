import nod_sandbox  # noqa: F401  (Nod events of the tests go to a temp file)
import contextlib
import io
import json
import os
import socket
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

from launch_env import LaunchCase
from ops_helpers import OpsCase, today
from delega import audit, broker, jobs, nod, spend, util


def events(path=None):
    path = path or nod.events_file()
    try:
        return [json.loads(l) for l in open(path) if l.strip()]
    except OSError:
        return []


def of_type(envs, type_):
    return [ev for env in envs for ev in env["events"] if ev["type"] == type_]


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Collector:
    """A Nod collector on a free port that keeps every POSTed envelope."""
    def __init__(self):
        got = self.got = []

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                got.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"accepted": 1}')

            def log_message(self, *a):
                pass
        self.srv = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_port}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class ChannelCase(OpsCase):
    """A local job channel (channel = job dir, with meta) and its own Nod events file."""
    def setUp(self):
        super().setUp()
        self.events_file = os.path.join(self.root, "nod", "events.jsonl")
        os.environ.update(NOD_EVENTS_FILE=self.events_file, NOD_COLLECTOR_URL="", DELEGA_NODE="local",
                          DELEGA_JOB_ID="20261026-101010-77", DELEGA_PROFILE="work", DELEGA_ANSWER_WAIT_S="600")
        self.chan = self.make_job("20261026-101010-77-api", {"OWNER": "ana", "PROFILE": "work", "NODE": "local",
                                                             "BRANCH": "delega/x"},
                                  {"forbidden.txt": "# clients\nacmecorp\n"})

    def notify(self, kind, text):
        with contextlib.redirect_stdout(io.StringIO()):
            return broker.cmd_notify(self.chan, kind, text)


class EmitterTest(ChannelCase):
    def test_question_gives_a_notice_and_a_decision_request(self):
        t0 = int(time.time() * 1000)
        self.notify("question", "¿Migro la tabla de AcmeCorp? Recomendación: sí, con copia antes")
        envs = events()
        self.assertEqual(len(envs), 2)
        env = envs[0]
        self.assertEqual(env["nod"], 1)
        src = env["source"]
        self.assertEqual((src["host"], src["instance"], src["profile"], src["privacy"]),
                         ("delega", "delega", "work", "completo"))
        self.assertRegex(src["run"], r"^[0-9a-f]{8}$")
        self.assertEqual([e["events"][0]["seq"] for e in envs], [1, 2])
        notice, = of_type(envs, "job.notice")
        self.assertEqual((notice["session"], notice["task"]), ("20261026-101010-77", "20261026-101010-77"))
        self.assertEqual(notice["data"]["kind"], "question")
        dec, = of_type(envs, "decision.request")
        d = dec["data"]
        self.assertEqual((d["job"], d["owner"], d["profile"], d["node"], d["kind"]),
                         ("20261026-101010-77", "ana", "work", "local", "question"))
        self.assertEqual(d["recommendation"], "sí, con copia antes")
        self.assertEqual(d["reply_cmd"], 'delega say 20261026-101010-77 "<respuesta>"')
        self.assertAlmostEqual(d["deadline"], t0 + 600_000, delta=5000)
        self.assertEqual(d["deadline"] - dec["ts"], 600_000)

    def test_forbidden_terms_never_reach_the_bus(self):
        self.notify("blocker", "acmecorp credentials missing")
        broker.Channel(self.chan).event("ALERT:stall", "stuck on ACMECORP for 10 min")
        raw = open(self.events_file).read()
        self.assertNotIn("acmecorp", raw.lower())
        self.assertIn("[redacted]", raw)
        self.assertEqual(len(of_type(events(), "decision.request")), 1)   # blocker → decision too
        self.assertEqual(of_type(events(), "job.alert")[0]["data"]["kind"], "stall")

    def test_milestone_is_not_a_decision_and_progress_types_are_filtered(self):
        self.notify("milestone", "tests green")
        ch = broker.Channel(self.chan)
        ch.event("TOOL", "Bash: ls")
        ch.event("RESULT", "the report", is_error=False, cost_usd=1.25)
        envs = events()
        self.assertEqual([ev["type"] for env in envs for ev in env["events"]], ["job.notice", "job.result"])
        self.assertEqual(of_type(envs, "job.result")[0]["data"]["cost_usd"], 1.25)

    def test_collector_down_never_blocks_and_the_file_keeps_everything(self):
        os.environ["NOD_COLLECTOR_URL"] = f"http://127.0.0.1:{free_port()}"     # nobody listens
        t0 = time.time()
        for i in range(5):
            self.notify("milestone", f"step {i}")
        self.assertLess(time.time() - t0, 3)
        self.assertEqual(len(events()), 5)
        self.assertTrue(os.path.exists(os.path.join(self.chan, "nod.down")))   # later POSTs are skipped

    def test_collector_up_receives_the_envelopes(self):
        c = Collector()
        self.addCleanup(c.close)
        os.environ["NOD_COLLECTOR_URL"] = c.url
        self.notify("question", "which port?")
        self.assertEqual([p for p, _ in c.got], ["/v1/events", "/v1/events"])
        self.assertEqual(c.got[1][1]["events"][0]["type"], "decision.request")

    def test_hung_collector_costs_at_most_the_timeout(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        s.listen(1)                       # accepts, never answers
        self.addCleanup(s.close)
        os.environ["NOD_COLLECTOR_URL"] = f"http://127.0.0.1:{s.getsockname()[1]}"
        t0 = time.time()
        self.notify("milestone", "one")
        self.notify("milestone", "two")
        self.assertLess(time.time() - t0, 2.5)
        self.assertEqual(len(events()), 2)

    def test_forward_is_idempotent(self):
        self.notify("milestone", "once")
        self.assertEqual(nod.forward(self.chan), 0)
        self.assertEqual(nod.forward(self.chan), 0)
        self.assertEqual(len(events()), 1)

    def test_on_a_node_the_broker_only_writes_the_channel(self):
        os.environ["DELEGA_NODE"] = "box"
        self.notify("question", "anyone?")
        self.assertEqual(events(), [])
        lines = open(os.path.join(self.chan, "nod.jsonl")).read().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(json.loads(lines[0])["source"]["machine"], "box")

    def test_off_switch(self):
        os.environ["DELEGA_NOD"] = "off"
        self.notify("question", "silent?")
        self.assertEqual(events(), [])
        self.assertFalse(os.path.exists(os.path.join(self.chan, "nod.jsonl")))


class AuditBusTest(OpsCase):
    TOML = ""

    def setUp(self):
        super().setUp()
        terms = os.path.join(self.root, "terms.txt")
        with open(terms, "w") as f:
            f.write("acmecorp\n")
        self.write_config(f'[profiles.work]\nowner = "self"\nforbidden_terms_file = "{terms}"\n'
                          '[general]\ndefault_profile = "work"\n')
        os.environ.update(NOD_EVENTS_FILE=os.path.join(self.root, "events.jsonl"), NOD_COLLECTOR_URL="",
                          DELEGA_OWNER="ana")

    def test_launch_refused_and_end_go_to_the_bus_redacted(self):
        cfg = self.cfg()
        audit.record(cfg, "launch", job="20261026-1-a", profile="work", node="box", branch="delega/a", owner="ana")
        audit.record(cfg, "refused", job="20261026-2-b", profile="work", reason="brief names AcmeCorp", rc=9)
        audit.record(cfg, "scan", job="20261026-1-a", result="clean")
        audit.record(cfg, "end", job="20261026-1-a", profile="work", cost_usd=1.5, rc=0)
        envs = events()
        self.assertEqual([ev["type"] for env in envs for ev in env["events"]], ["job.launch", "job.alert", "job.end"])
        alert = of_type(envs, "job.alert")[0]["data"]
        self.assertEqual((alert["kind"], alert["owner"]), ("refused", "ana"))
        self.assertNotIn("acmecorp", json.dumps(envs).lower())
        self.assertEqual(of_type(envs, "job.end")[0]["data"]["cost_usd"], 1.5)
        self.assertEqual([e["events"][0]["seq"] for e in envs], [1, 2, 3])
        self.assertEqual(len({e["source"]["run"] for e in envs}), 1)


class WatchBusTest(OpsCase):
    TOML = '[profiles.work]\nowner = "self"\n[general]\ndefault_profile = "work"\n'

    def test_one_line_per_event_of_the_owner(self):
        path = os.path.join(self.root, "events.jsonl")
        os.environ.update(NOD_EVENTS_FILE=path, DELEGA_OWNER="ana")
        with open(path, "w") as f:
            for owner, typ, text in (("ana", "job.notice", "half way"), ("bo", "job.notice", "not mine"),
                                     ("ana", "decision.request", "merge now?")):
                env = nod.envelope(typ, {"job": "J1", "owner": owner, "kind": "question", "text": text,
                                         "reply_cmd": 'delega say J1 "<respuesta>"'},
                                   job="J1", profile="work", run="abcdef01", seq=1)
                f.write(json.dumps(env) + "\n")
            f.write(json.dumps({"nod": 1, "source": {"host": "claude"}, "events": [{"type": "tool.call"}]}) + "\n")
            f.write("not json\n")
        out = []
        jobs.watch_bus(self.cfg(), from_start=True, out=out.append, poll_s=0, stop=lambda: True)
        self.assertEqual(len(out), 2, out)
        self.assertIn("[J1] job.notice:question half way", out[0])
        self.assertIn('→ delega say J1 "<respuesta>"', out[1])
        out = []
        jobs.watch_bus(self.cfg(), everything=True, from_start=True, out=out.append, poll_s=0, stop=lambda: True)
        self.assertEqual(len(out), 3)
        out = []   # without --from-start only what comes after
        jobs.watch_bus(self.cfg(), out=out.append, poll_s=0, stop=lambda: True)
        self.assertEqual(out, [])

    def test_partial_last_line_waits(self):
        path = os.path.join(self.root, "events.jsonl")
        env = nod.envelope("job.alert", {"job": "J", "owner": "ana", "kind": "stall", "text": "x"},
                           job="J", profile="p", run="abcdef01", seq=1)
        with open(path, "w") as f:
            f.write(json.dumps(env) + "\n" + json.dumps(env)[:20])
        lines, off = nod.bus_lines(path, 0, "ana")
        self.assertEqual(len(lines), 1)
        self.assertEqual(off, len(json.dumps(env)) + 1)


class SpendCheckTest(OpsCase):
    TOML = '[profiles.work]\nowner = "self"\n[general]\ndefault_profile = "work"\n'

    def setUp(self):
        super().setUp()
        os.environ.update(NOD_EVENTS_FILE=os.path.join(self.root, "events.jsonl"), NOD_COLLECTOR_URL="")

    def job(self, n, cost, audited=None, ended=True):
        jid = f"{today()}-00000{n}-{n}"
        self.make_job(f"{jid}-api", {"PROFILE": "work", **({"END": "1"} if ended else {})},
                      {"summary.json": {"cost_usd": cost}})
        if audited is not False:
            audit.record(self.cfg(), "end", job=jid, cost_usd=cost if audited is None else audited)
        return jid

    def test_agreement_within_two_percent(self):
        self.job(1, 10.0)
        self.job(2, 5.0, audited=5.1)               # 0.1 of 15.1 = 0.66 %
        self.job(3, 2.0, ended=False, audited=False)  # still running: not compared
        rc, out, err = self.run_cli(spend.main, "--check")
        self.assertEqual(rc, 0, out + err)
        self.assertIn("summaries $15.00 · audit $15.10", out)      # the running job is not compared
        r = spend.check(self.cfg(), today())
        self.assertTrue(r["ok"])
        self.assertEqual(r["differ"], [f"{today()}-000002-2"])

    def test_difference_above_two_percent_warns(self):
        self.job(1, 10.0)
        missing = self.job(2, 4.0, audited=False)
        rc, out, err = self.run_cli(spend.main, "all", "--check")
        self.assertEqual(rc, 1)
        self.assertIn("WARNING", err)
        self.assertIn(missing, out)
        r = spend.check(self.cfg(), "all")
        self.assertEqual((r["summaries"], r["audit"], r["missing_audit"]), (14.0, 10.0, [missing]))


class SummaryAlwaysTest(OpsCase):
    def setUp(self):
        super().setUp()
        os.environ.update(NOD_EVENTS_FILE=os.path.join(self.root, "events.jsonl"), NOD_COLLECTOR_URL="")

    def test_synthetic_summary_from_state(self):
        d = self.make_job("20261026-000001-1-api", {"NODE": "local"},
                          {"state.json": {"start": time.time() - 120, "cost_usd": 0.7, "turns": 4, "tools": 9,
                                          "session_id": "s1", "cap": "time"}, "rc": "143\n"})
        s = jobs.ensure_summary(d, reason="cap_cut")
        self.assertEqual((s["cost_usd"], s["rc"], s["session_id"], s["synthetic"]), (0.7, 143, "s1", "cap_cut"))
        self.assertEqual(util.read_json(os.path.join(d, "summary.json"))["cost_usd"], 0.7)
        self.assertEqual(jobs.ensure_summary(d)["synthetic"], "cap_cut")   # written once, then kept

    def test_reconcile_closes_with_summary_and_audited_cost(self):
        d = self.make_job("20261026-000002-2-api", {"NODE": "local", "ACCOUNT": "a@example.com", "PROFILE": "",
                                                    "OWNER": "ana"},
                          {"state.json": {"start": time.time() - 60, "cost_usd": 2.5, "pid": 999999}})
        with mock.patch.object(jobs, "alive", return_value=False):
            jobs.reconcile(self.cfg())
        self.assertTrue(os.path.exists(os.path.join(d, "summary.json")))
        end, = [r for r in audit.read(self.cfg()) if r["event"] == "end"]
        self.assertEqual((end["job"], end["cost_usd"], end["reason"]), ("20261026-000002-2", 2.5, "dead"))


class NodeReemissionTest(LaunchCase):
    """The broker on a node writes nod.jsonl; the Mac pulls it while waiting and forwards it once."""
    def write_config(self):
        super().write_config()
        text = open(self.cfg_path).read().replace("max_local_jobs = 2", "max_local_jobs = 2\nnode.max_load_pct = 100000")
        with open(self.cfg_path, "w") as f:      # the fake node is this busy machine
            f.write(text)

    def setUp(self):
        super().setUp()
        self.events_file = os.path.join(self.root, "nod-events.jsonl")
        os.environ.update(NOD_EVENTS_FILE=self.events_file, NOD_COLLECTOR_URL="", DELEGA_NOD_PULL_S="0")

    def test_remote_question_reaches_the_mac_once_with_owner_and_redacted(self):
        os.environ["STUB_NOD_QUESTION"] = "Does acmecorp keep the old API? Recommendation: keep it"
        os.environ["STUB_SLEEP"] = "0.6"     # a few waiter polls while running: pulls must not duplicate
        rc, out, err = self.run_cli([self.repo, self.brief, "--node", "box", "--owner", "orq1"])
        self.assertEqual(rc, 0, out + err)
        envs = events(self.events_file)
        self.assertEqual(sorted(ev["type"] for env in envs for ev in env["events"]),
                         ["decision.request", "job.notice"])
        dec, = of_type(envs, "decision.request")
        d = dec["data"]
        job_id = jobs.job_id(self.job_dir())
        self.assertEqual((d["job"], d["owner"], d["node"], d["profile"]), (job_id, "orq1", "box", "work"))
        self.assertEqual(d["recommendation"], "keep it")
        self.assertEqual(d["reply_cmd"], f'delega say {job_id} "<respuesta>"')
        self.assertNotIn("acmecorp", json.dumps(envs).lower())
        self.assertEqual({e["source"]["machine"] for e in envs}, {"box"})
        # the copy back brought nod.jsonl; forwarding it again sends nothing
        self.assertEqual(nod.forward(self.job_dir(), util.read_meta(self.job_dir())), 0)

    def test_broker_without_summary_still_leaves_one(self):
        os.environ["STUB_NO_SUMMARY"] = "1"
        rc, out, err = self.run_cli([self.repo, self.brief, "--node", "box"])
        self.assertEqual(rc, 0, out + err)
        s = util.read_json(os.path.join(self.job_dir(), "summary.json"))
        self.assertEqual((s["cost_usd"], s["session_id"][:5], s["synthetic"]), (0.5, "sess-", "no_summary"))
        end = [f for e, f in self.events if e == "end"][0]
        self.assertEqual(end["cost_usd"], 0.5)

    def test_watch_pulls_a_job_without_waiter(self):
        d = self.job_dir_for_remote()
        chan = os.path.join(self.node_home, util.read_meta(d)["CHANNEL"])
        with open(os.path.join(chan, "forbidden.txt"), "w") as f:
            f.write("acmecorp\n")
        with mock.patch.dict(os.environ, {"DELEGA_NODE": "box", "DELEGA_JOB_ID": jobs.job_id(d),
                                          "DELEGA_PROFILE": "work"}):
            nod.from_progress(chan, "ALERT:stall", "acmecorp stalled", terms=["acmecorp"])
        out = []
        w = jobs._Watcher(self.cfg(), d, "x", True, out.append)
        w._pull_nod()
        alert, = of_type(events(self.events_file), "job.alert")
        self.assertEqual((alert["data"]["owner"], alert["data"]["kind"]), ("tester", "stall"))
        w.pulled = 0
        w._pull_nod()
        self.assertEqual(len(events(self.events_file)), 1)

    def job_dir_for_remote(self):
        jid = "20261026-111111-5"
        d = os.path.join(self.state, f"{jid}-api")
        chan = f"delega/jobs/{jid}"
        os.makedirs(os.path.join(self.node_home, chan))
        os.makedirs(d)
        with open(os.path.join(d, "meta"), "w") as f:
            f.write(f"NODE=box\nCHANNEL={chan}\nOWNER=tester\nPROFILE=work\n")
        return d


if __name__ == "__main__":
    unittest.main()

import nod_sandbox  # noqa: F401  (Nod events of the tests go to a temp file)
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
FAKE = os.path.join(ROOT, "tests", "fakes", "claude")
sys.path.insert(0, SRC)
from delega import broker  # noqa: E402


class BrokerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ch = os.path.join(self.tmp.name, "job")
        os.makedirs(self.ch)
        self.brief = os.path.join(self.tmp.name, "brief.md")
        with open(self.brief, "w") as f:
            f.write("do the thing")

    def env(self, **extra):
        e = {k: v for k, v in os.environ.items() if not k.startswith(("DELEGA_", "FAKE_"))}
        e.update(PYTHONPATH=SRC, DELEGA_LOCALE="en", DELEGA_JOB_GRACE_S="5")
        e.update(extra)
        return e

    def start(self, **extra):
        return subprocess.Popen([sys.executable, "-m", "delega", "broker", "run", self.ch, self.brief, "--", FAKE],
                                env=self.env(**extra), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def run_broker(self, timeout=40, **extra):
        p = self.start(**extra)
        try:
            p.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            p.kill()
            self.fail("broker did not finish")
        return p.returncode

    def path(self, name):
        return os.path.join(self.ch, name)

    def read(self, name):
        with open(self.path(name), encoding="utf-8") as f:
            return f.read()

    def types(self):
        return [l.split("\t")[1] for l in self.read("progress.log").splitlines()]

    def json(self, name):
        with open(self.path(name)) as f:
            return json.load(f)

    def wait_for(self, pred, timeout=15):
        end = time.time() + timeout
        while time.time() < end:
            try:
                if pred():
                    return True
            except (OSError, ValueError):
                pass
            time.sleep(0.1)
        self.fail("condition not reached")


class Caps(BrokerCase):
    def test_minutes_cap_obey_closes_with_report(self):
        rc = self.run_broker(DELEGA_JOB_MAX_MIN="0.03", FAKE_MODE="obey")
        self.assertEqual(rc, 0)
        self.assertIn("ALERT:cap", self.types())
        self.assertNotIn("ALERT:cap_cut", self.types())
        self.assertIn("final report", self.read("results.jsonl"))
        s = self.json("summary.json")
        self.assertTrue(s["cap"])
        self.assertEqual(s["rc"], 0)
        self.assertEqual(self.read("rc").strip(), "0")

    def test_turn_cap_ignore_is_cut(self):
        rc = self.run_broker(DELEGA_JOB_MAX_TURNS="3", DELEGA_JOB_GRACE_S="2", FAKE_MODE="ignore", FAKE_SLEEP="0.1")
        self.assertNotEqual(rc, 0)
        self.assertIn("ALERT:cap", self.types())
        self.assertIn("ALERT:cap_cut", self.types())
        self.assertIn("messages", self.json("summary.json")["cap"])

    def test_cost_cap(self):
        rc = self.run_broker(DELEGA_JOB_MAX_USD="1", FAKE_MODE="cost", FAKE_COST="20")
        self.assertEqual(rc, 0)
        self.assertIn("ALERT:cap", self.types())
        self.assertIn("USD", self.json("summary.json")["cap"])
        self.assertEqual(self.json("summary.json")["cost_usd"], 20)


class Channel(BrokerCase):
    def test_inbox_delivery(self):
        recv = os.path.join(self.tmp.name, "recv")
        with open(self.path("inbox.jsonl"), "w") as f:
            f.write(json.dumps({"ts": "2026-01-01T00:00:00", "text": "hello worker"}) + "\n")
        self.assertEqual(self.run_broker(FAKE_RECV=recv), 0)
        with open(recv) as f:
            self.assertIn("[orchestrator] hello worker", f.read())
        self.assertIn("MESSAGE", self.types())
        self.assertEqual(self.json("state.json")["messages"], 1)

    def test_question_and_answer(self):
        p = self.start(FAKE_MODE="question")
        self.wait_for(lambda: self.json("state.json")["state"] == "waiting_answer")
        r = subprocess.run([sys.executable, "-m", "delega", "broker", "say", self.ch, "use sqlite"],
                           env=self.env(), capture_output=True, text=True)
        self.assertEqual(r.returncode, 0)
        p.communicate(timeout=30)
        self.assertEqual(p.returncode, 0)
        self.assertIn("NOTICE:question", self.types())
        self.assertIn("MESSAGE", self.types())
        self.assertNotIn("ALERT:no_answer", self.types())
        self.assertIn("done after: [orchestrator] use sqlite", self.read("results.jsonl"))

    def test_question_without_answer(self):
        self.assertEqual(self.run_broker(FAKE_MODE="question", DELEGA_ANSWER_WAIT_S="1"), 0)
        self.assertIn("ALERT:no_answer", self.types())

    def test_loop_alert(self):
        self.run_broker(FAKE_MODE="loop")
        self.assertIn("ALERT:loop", self.types())

    def test_errors_alert(self):
        self.run_broker(FAKE_MODE="errors")
        self.assertIn("ALERT:errors", self.types())

    def test_say_after_end_refused(self):
        self.run_broker()
        r = subprocess.run([sys.executable, "-m", "delega", "broker", "say", self.ch, "late"],
                           env=self.env(), capture_output=True, text=True)
        self.assertEqual(r.returncode, 6)

    def test_protocol_in_system_prompt(self):
        text = broker.protocol("/x/notify", "10 min", 300, loc="en")
        for needle in ("/x/notify milestone", "[orchestrator]", "10 min", "invented data", "hooks", "WIP commit"):
            self.assertIn(needle, text)
        self.assertIn("WIP", broker.protocol("/x/notify", "10 min", 300, loc="es"))


class Lease(BrokerCase):
    def test_lease_renewed_then_final(self):
        p = self.start(DELEGA_LEASE_RENEW_S="1", FAKE_MODE="obey", DELEGA_JOB_MAX_MIN="0.1")
        self.wait_for(lambda: os.path.exists(self.path("lease.json")))
        first = self.json("lease.json")["renewed"]
        self.wait_for(lambda: self.json("lease.json")["renewed"] > first + 0.5)
        p.communicate(timeout=40)
        lease = self.json("lease.json")
        self.assertTrue(lease["final"])
        time.sleep(1.5)
        self.assertEqual(self.json("lease.json")["renewed"], lease["renewed"])
        self.assertEqual(lease["pid"], self.json("state.json")["pid"])


class Redaction(BrokerCase):
    def test_redacted_outputs_but_raw_events(self):
        with open(self.path("forbidden.txt"), "w") as f:
            f.write("Acme\n")
        self.assertEqual(self.run_broker(FAKE_LEAK="ACMEcorp"), 0)
        for name in ("notices.jsonl", "progress.log", "results.jsonl", "summary.json", "state.json", "result.json"):
            self.assertNotIn("acme", self.read(name).lower(), name)
        self.assertIn("[redacted]", self.read("notices.jsonl"))
        self.assertIn("[redacted]", self.read("progress.log"))
        self.assertIn("ACMEcorp", self.read("events.jsonl"))

    def test_redact_function(self):
        self.assertEqual(broker.redact("an Acme and ACME.x", ["acme"]), "an [redacted] and [redacted].x")
        self.assertEqual(broker.redact("plain", []), "plain")


class Env(BrokerCase):
    def test_claude_config_dir_kept_others_dropped(self):
        out = os.path.join(self.tmp.name, "env.txt")
        self.run_broker(FAKE_ENV_OUT=out, CLAUDE_CONFIG_DIR="/cfg/a", CLAUDE_CODE_X="1", ANTHROPIC_API_KEY="k")
        with open(out) as f:
            self.assertEqual(f.read().split(), ["CLAUDE_CONFIG_DIR=/cfg/a"])

    def test_keep_env_lets_named_vars_through(self):
        for var in ("DELEGA_KEEP_ENV", "ORQ_CONSERVAR"):
            out = os.path.join(self.tmp.name, f"env-{var}.txt")
            self.run_broker(FAKE_ENV_OUT=out, CLAUDE_CODE_OAUTH_TOKEN="tok", CLAUDE_CODE_X="1",
                            ANTHROPIC_API_KEY="k", **{var: "CLAUDE_CODE_OAUTH_TOKEN, ,X"})
            with open(out) as f:
                self.assertEqual(f.read().split(), ["CLAUDE_CODE_OAUTH_TOKEN=tok"])


class Legacy(BrokerCase):
    def make_legacy(self):
        now = time.time()
        with open(self.path("estado.json"), "w") as f:
            json.dump({"estado": "terminado", "inicio": now - 600, "ultimo_evento": now - 60, "ultima_accion": "Bash: ls",
                       "herramientas": 7, "turnos": 9, "alertas": 1, "mensajes": 0, "session_id": "s1", "rc": 0,
                       "pid": 1, "coste_usd": 1.5, "tope": None}, f)
        with open(self.path("progreso.log"), "w") as f:
            f.write("2026-01-01T10:00:00\tINICIO\tclaude\n2026-01-01T10:01:00\tAVISO:hito\tdone one\n"
                    "2026-01-01T10:02:00\tALERTA:atasco\t10 min\n2026-01-01T10:03:00\tFIN\trc=0\n")
        with open(self.path("avisos.jsonl"), "w") as f:
            f.write('{"ts": "x", "tipo": "hito", "texto": "done one"}\n')

    def call(self, *argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = broker.main(list(argv))
        return rc, buf.getvalue()

    def test_status_reads_legacy(self):
        self.make_legacy()
        rc, out = self.call("status", self.ch)
        self.assertEqual(rc, 0)
        self.assertIn("ended rc=0", out)
        self.assertIn("tools 7", out)
        self.assertIn("NOTICE:milestone", out)
        self.assertIn("ALERT:stall", out)

    def test_watch_reads_legacy_types(self):
        self.make_legacy()
        rc, out = self.call("watch", self.ch, "lbl", "--all")
        self.assertEqual(rc, 0)
        self.assertIn("[lbl] NOTICE:milestone done one", out)
        self.assertIn("END rc=0", out)

    def test_limits_and_metrics_legacy(self):
        self.make_legacy()
        with open(self.path("eventos.jsonl"), "w") as f:
            f.write(json.dumps({"type": "rate_limit_event", "rate_limit_info": {"unifiedWindows": {}}}) + "\n")
        rc, out = self.call("limits", self.ch)
        self.assertIn('"info"', out)
        rc, out = self.call("metrics", self.ch)
        m = json.loads(out)
        self.assertEqual(m["tools"], 7)
        self.assertEqual(m["notices"], {"milestone": 1})
        self.assertEqual(m["alerts"], {"stall": 1})

    def test_new_job_metrics_and_limits(self):
        self.run_broker()
        rc, out = self.call("limits", self.ch)
        self.assertEqual(json.loads(out)["info"]["unifiedWindows"]["five_hour"]["utilization"], 0.25)
        m = broker.metrics_of(self.ch)
        self.assertEqual(m["tokens"]["claude-fake"]["input"], 10)
        self.assertEqual(m["tokens_source"], "result")


if __name__ == "__main__":
    unittest.main()

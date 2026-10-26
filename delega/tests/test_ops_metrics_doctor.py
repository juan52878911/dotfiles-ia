import nod_sandbox  # noqa: F401  (Nod events of the tests go to a temp file)
import json
import os
import subprocess
import time
import unittest
from unittest import mock

from ops_helpers import OpsCase, rate_limit_line
from delega import doctor, metrics

TOML = """
[general]
default_profile = "work"
[compute.gcp]
kind = "env"
var = "X"
fix_hint = "export X=1"
[profiles.work]
owner = "self"
identity.expect_email_domain = "example.com"
compute = ["gcp"]
"""


def ts(offset=0):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(1_800_000_000 + offset))


class MetricsTest(OpsCase):
    TOML = TOML

    def channel(self, legacy=False):
        prog = [(0, "START", "go"), (10, "TOOL", "Bash: pytest -q"), (40, "TOOL", "Read: a.py"),
                (45, "ALERT:stall", "x"), (50, "NOTICE:milestone", "half"), (60, "RESULT", "done"), (70, "END", "rc=0")]
        if legacy:
            tr = {"TOOL": "HERRAMIENTA", "RESULT": "RESULTADO", "END": "FIN", "ALERT:stall": "ALERTA:atasco",
                  "NOTICE:milestone": "AVISO:hito"}
            prog = [(o, tr.get(t, t), x) for o, t, x in prog]
        events = [{"type": "assistant", "message": {"id": "m1", "model": "claude-x", "usage": {"input_tokens": 5, "output_tokens": 7}}},
                  {"type": "result", "total_cost_usd": 0.75, "duration_api_ms": 90000, "num_turns": 3,
                   "modelUsage": {"claude-x": {"inputTokens": 100, "outputTokens": 50, "cacheReadInputTokens": 900,
                                               "cacheCreationInputTokens": 10, "costUSD": 0.75}}}]
        names = ("estado.json", "progreso.log", "eventos.jsonl") if legacy else ("state.json", "progress.log", "events.jsonl")
        state = ({"estado": "terminado", "inicio": 1_800_000_000, "ultimo_evento": 1_800_000_070, "herramientas": 2, "mensajes": 3, "rc": 0}
                 if legacy else {"state": "ended", "start": 1_800_000_000, "last_event": 1_800_000_070, "tools": 2, "messages": 3, "rc": 0})
        files = {names[0]: state, names[1]: "\n".join(f"{ts(o)}\t{t}\t{x}" for o, t, x in prog) + "\n",
                 names[2]: "\n".join(json.dumps(e) for e in events) + "\n"}
        return self.make_job("legacy-job" if legacy else "new-job", files=files)

    def test_job_metrics_new_and_legacy_names_agree(self):
        for legacy in (False, True):
            m = metrics.job_metrics(self.channel(legacy))
            self.assertEqual(m["duration_s"], 70)
            self.assertEqual(m["tokens"]["claude-x"]["cache_read"], 900)
            self.assertEqual(m["tokens_source"], "result")
            self.assertEqual(m["alerts"], {"stall": 1})
            self.assertEqual(m["notices"], {"milestone": 1})
            self.assertEqual(m["time_by"]["Bash:pytest"], 30)
            self.assertEqual(m["time_by"]["Read"], 10)
            self.assertEqual(m["cost_usd"], 0.75)

    def test_normalise_spanish_keys(self):
        m = metrics.normalise({"estado": "terminado", "tiempo_por": {"a": 1}, "tokens": {"m": {"entrada": 1, "salida": 2, "cache_lectura": 3, "cache_escritura": 4}}})
        self.assertEqual(m["time_by"], {"a": 1})
        self.assertEqual(m["tokens"]["m"], {"input": 1, "output": 2, "cache_read": 3, "cache_write": 4})

    def test_transcript_usage_dedupes_ids_and_splits_sidechain(self):
        p = os.path.join(self.root, "t.jsonl")
        rows = [{"type": "assistant", "timestamp": "2026-01-01T10:00:00Z", "message": {"id": "a", "model": "m", "usage": {"input_tokens": 1, "output_tokens": 2}}},
                {"type": "assistant", "timestamp": "2026-01-01T10:01:00Z", "message": {"id": "a", "model": "m", "usage": {"input_tokens": 1, "output_tokens": 3}}},
                {"type": "assistant", "isSidechain": True, "message": {"id": "b", "model": "m", "usage": {"input_tokens": 4}}}]
        open(p, "w").write("\n".join(json.dumps(r) for r in rows))
        u = metrics.transcript_usage(p)
        self.assertEqual(u["tokens"]["m"], {"input": 1, "output": 3, "cache_read": 0, "cache_write": 0, "messages": 1})
        self.assertEqual(u["tokens"]["m (subagents)"]["input"], 4)

    def test_report_with_resources(self):
        rows = [[f"2026-01-01T10:0{i}:00", "4000", str(m), "0", "1.5", "10", "5", "80", "2", "50", "1", "2", "1", str(o), "x"]
                for i, (m, o) in enumerate([(2000, 0), (400, 1)])]
        s = metrics.resource_stats(rows)
        self.assertEqual((s["mem_min"], s["oom"], s["low_pct"]), (400, 1, 50.0))
        job = metrics.job_metrics(self.channel())
        text = metrics.render_report([job], [], {"box": rows})
        self.assertIn("| **total** |", text)
        self.assertIn("Resources of box", text)
        self.assertIn("killed by the OOM killer during sampling: 1", text)
        os.environ["DELEGA_LOCALE"] = "es"
        self.assertIn("Recursos de box", metrics.render_report([job], [], {"box": rows}))

    def test_report_cli_over_local_jobs(self):
        self.make_job("20260101-000001-1-api", {"NODE": "local"}, {"state.json": {"state": "ended", "start": 1_800_000_000, "last_event": 1_800_000_030}})
        rc, out, _ = self.run_cli(metrics.main, "report", "api")
        self.assertEqual(rc, 0)
        self.assertIn("Feasibility report", out)
        self.assertTrue(os.path.isdir(os.path.join(self.state, "metrics")))

    def test_sample_script_present_and_parses(self):
        path = metrics.sampler_script()
        self.assertTrue(path and os.path.exists(path))
        self.assertEqual(subprocess.run(["bash", "-n", path]).returncode, 0)


class DoctorTest(OpsCase):
    TOML = TOML

    def probe(self, **over):
        base = {"ok": True, "email": "me@example.com", "disk_gb": 100, "load_pct": 10, "claude_version": "2.0.0",
                "compute": {"gcp": "ok"}}
        base.update(over)
        return base

    def run_doctor(self, local):
        with mock.patch("delega.nodes.local_probe", lambda *a, **k: local, create=True):
            return self.run_cli(doctor.main)

    def test_failures_show_exact_fix(self):
        rc, out, _ = self.run_doctor(self.probe(email="me@other.org", compute={"gcp": "expired"}, disk_gb=3))
        self.assertEqual(rc, 1)
        self.assertIn("✗ local: signed in as me@other.org", out)
        self.assertIn("fix: CLAUDE_CONFIG_DIR=~/.claude claude /login", out)
        self.assertIn("✗ local: compute 'gcp': expired", out)
        self.assertIn("fix: export X=1", out)
        self.assertIn("✗ local: free disk 3 GB (min 20)", out)
        self.assertIn("✗ no forbidden-terms file", out)

    def test_all_good_with_terms_and_no_nodes(self):
        terms = os.path.join(self.root, "terms.txt")
        open(terms, "w").write("x\n")
        self.write_config(TOML + f'forbidden_terms_file = "{terms}"\n')
        with mock.patch("delega.doctor.local_claude_version", lambda p: "2.0.0"):
            rc, out, _ = self.run_doctor(self.probe())
        self.assertEqual(rc, 0, out)
        self.assertIn("✓ local: signed in as me@example.com", out)
        self.assertIn("all good", out)

    def test_quota_and_budget_lines(self):
        now = int(time.time())
        self.make_job(time.strftime("%Y%m%d") + "-000001-1-a", {"PROFILE": "work"},
                      {"events.jsonl": rate_limit_line(now), "summary.json": {"cost_usd": 200}})
        rc, out, _ = self.run_doctor(self.probe())
        self.assertIn("quota: 5 h 20 %", out)
        self.assertIn("✗ spend today: $200.00 of $150.00", out)

    def test_invalid_config(self):
        self.write_config('[profiles.x]\nowner = "someone"\n')
        rc, out, _ = self.run_cli(doctor.main)
        self.assertEqual(rc, 1)
        self.assertIn("✗ configuration is invalid", out)


if __name__ == "__main__":
    unittest.main()

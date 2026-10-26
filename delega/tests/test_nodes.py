import nod_sandbox  # noqa: F401  (Nod events of the tests go to a temp file)
import platform
import contextlib
import io
import os
import unittest
from unittest import mock

try:
    from tests.launch_env import LaunchCase
except ImportError:  # run from inside tests/
    from launch_env import LaunchCase

from delega import nodes, remote


class ProbeTests(LaunchCase):
    def test_probe_shape_through_fake_ssh(self):
        cfg = self.cfg()
        r = nodes.probe(cfg, "box", "work", ["tool"])
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["email"], "dev@example.com")
        self.assertEqual(r["compute"], {"tool": "ok"})
        for key in ("mem_mb", "disk_gb", "load_pct", "containers", "claude_version", "pyz_sha", "python", "os"):
            self.assertIn(key, r)
        self.assertEqual(r["claude_version"], "2.1.5")
        self.assertEqual(r["pyz_sha"], "")           # nothing uploaded yet
        self.assertEqual(r["os"], platform.system().lower())
        self.assertEqual(r["fix"], "")

    def test_probe_sees_uploaded_pyz_sha(self):
        cfg = self.cfg()
        sha = remote.ensure_pyz(cfg, cfg.node("box"))
        self.assertEqual(nodes.probe(cfg, "box", "work")["pyz_sha"], sha)
        log = os.path.join(self.node_home, ".scp.log")
        with open(log) as f:
            calls = len(f.read().splitlines())
        remote.ensure_pyz(cfg, cfg.node("box"))     # same sha: no new upload
        with open(log) as f:
            self.assertEqual(len(f.read().splitlines()), calls)

    def test_probe_uses_the_nodes_config_dir_for_identity(self):
        cfg = self.cfg()
        with mock.patch.object(remote, "ssh", wraps=remote.ssh) as m:
            nodes.probe(cfg, "box", "work")
        self.assertIn(".claude-work-node", m.call_args.kwargs.get("input", "") + m.call_args.args[1])

    def test_unreachable(self):
        r = nodes.probe(self.cfg(), "box2", "work")
        self.assertFalse(r["ok"])
        self.assertEqual(r["kind"], "unreachable")
        self.assertEqual(r["fix"], "ssh down2")

    def test_wrong_account_gives_the_login_command(self):
        os.environ["FAKE_EMAIL"] = "someone@other.org"
        r = nodes.probe(self.cfg(), "box", "work")
        self.assertEqual(r["kind"], "identity")
        self.assertIn("someone@other.org", r["reason"])
        self.assertIn("ssh -t box", r["fix"])
        self.assertIn("CLAUDE_CONFIG_DIR=", r["fix"])
        self.assertIn("/login", r["fix"])

    def test_not_logged_in(self):
        os.environ["FAKE_EMAIL"] = ""
        r = nodes.probe(self.cfg(), "box", "work")
        self.assertEqual(r["kind"], "identity")
        self.assertIn("not logged in", r["reason"])

    def test_claude_missing(self):
        cfg = self.cfg()
        cfg.nodes["box"]["launcher"] = ["definitely-not-installed-claude"]
        r = nodes.probe(cfg, "box", "work")
        self.assertEqual(r["kind"], "claude")
        self.assertIn("PATH", r["fix"])

    def test_disk_limit(self):
        cfg = self.cfg()
        cfg.nodes["box"]["min_disk_gb"] = 10 ** 9
        r = nodes.probe(cfg, "box", "work")
        self.assertEqual(r["kind"], "resources")
        self.assertIn("free disk", r["reason"])

    def test_load_and_ram_limits(self):
        cfg = self.cfg()
        cfg.policy_base["node"]["min_mem_mb"] = 10 ** 9
        self.assertIn("RAM", nodes.probe(cfg, "box", "work")["reason"])
        cfg = self.cfg()
        cfg.policy_base["node"]["max_load_pct"] = -1
        with mock.patch.dict(os.environ):  # the suite pins the limit by env so the host's load never decides
            os.environ.pop("DELEGA_NODE_MAX_LOAD_PCT", None)
            self.assertIn("load", nodes.probe(cfg, "box", "work")["reason"])

    def test_required_compute_failure_names_the_fix(self):
        r = nodes.probe(self.cfg(), "box", "work", ["broken"])
        self.assertEqual(r["kind"], "compute")
        self.assertEqual(r["compute"], {"broken": "error_rc1"})
        self.assertEqual(r["fix"], "ssh -t box 'run the-broken fix'")

    def test_remote_node_requires_an_identity_expectation(self):
        cfg = self.cfg()
        cfg.profiles["work"].pop("identity")
        r = nodes.probe(cfg, "box", "work")
        self.assertEqual(r["kind"], "identity")
        self.assertIn("expect_email", r["fix"])

    def test_gcp_adc_missing_file_never_touches_the_network(self):
        cfg = self.cfg()
        cfg.compute["vertex"] = {"kind": "gcp-adc", "adc_path": os.path.join(self.root, "nope.json")}
        r = nodes.probe(cfg, "box", "work", ["vertex"])
        self.assertEqual(r["compute"], {"vertex": "missing"})

    def test_gcp_adc_service_account_is_ok_and_token_is_not_printed(self):
        p = os.path.join(self.root, "adc.json")
        with open(p, "w") as f:
            f.write('{"type": "service_account", "private_key": "SECRET-KEY"}')
        cfg = self.cfg()
        cfg.compute["vertex"] = {"kind": "gcp-adc", "adc_path": p}
        r = nodes.probe(cfg, "box", "work", ["vertex"])
        self.assertEqual(r["compute"], {"vertex": "ok"})
        self.assertNotIn("SECRET-KEY", str(r))

    def test_env_compute(self):
        cfg = self.cfg()
        cfg.compute["tok"] = {"kind": "env", "var": "DELEGA_TEST_TOKEN_VAR"}
        self.assertEqual(nodes.probe(cfg, "box", "work", ["tok"])["compute"], {"tok": "missing"})
        with mock.patch.dict(os.environ, {"DELEGA_TEST_TOKEN_VAR": "x"}):
            self.assertEqual(nodes.probe(cfg, "box", "work", ["tok"])["compute"], {"tok": "ok"})

    def test_unknown_compute_is_a_config_error(self):
        from delega import config
        with self.assertRaises(config.ConfigError):
            nodes.probe(self.cfg(), "box", "work", ["nope"])

    def test_local_probe(self):
        r = nodes.local_probe(self.cfg(), "work", ["tool"])
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["node"], "local")
        self.assertEqual(r["email"], "dev@example.com")

    def test_local_probe_wrong_account(self):
        os.environ["FAKE_EMAIL"] = "x@other.org"
        r = nodes.local_probe(self.cfg(), "work")
        self.assertEqual(r["kind"], "identity")
        self.assertIn("/login", r["fix"])

    def test_version_helpers(self):
        self.assertTrue(nodes.same_minor("2.1.5", "2.1.9"))
        self.assertFalse(nodes.same_minor("2.1.5", "2.2.0"))
        self.assertFalse(nodes.same_minor("2.1.5", None))
        self.assertEqual(nodes.claude_version([self.claude]), "2.1.5")


def fake_probe(table):
    def probe(cfg, node, profile, requires=(), watch=()):
        name = node["name"] if isinstance(node, dict) else node
        return dict(table[name], node=name)
    return probe


OK = lambda mem: {"ok": True, "kind": "ok", "mem_mb": mem, "disk_gb": 100, "load_pct": 5, "containers": 0,
                  "reason": "", "fix": ""}


class SelectTests(LaunchCase):
    def select(self, table, live=None, **kw):
        with mock.patch.object(nodes, "probe", fake_probe(table)):
            return nodes.select(self.cfg(), "work", [], live or {}, say=lambda s: None, **kw)

    def test_most_free_ram_wins(self):
        self.assertEqual(self.select({"box": OK(4000), "box2": OK(9000)}), "box2")

    def test_reserve_per_live_job_is_subtracted(self):
        table = {"box": OK(9000), "box2": OK(6000)}
        self.assertEqual(self.select(table, {"box": 1}), "box")        # 9000 - 1500 = 7500 > 6000
        with mock.patch.dict(os.environ, {"DELEGA_NODE_RESERVE_MB_PER_JOB": "4000"}):
            self.assertEqual(self.select(table, {"box": 1}), "box2")   # 9000 - 4000 = 5000 < 6000

    def test_max_jobs_is_respected(self):
        table = {"box": OK(9000), "box2": OK(6000)}
        self.assertEqual(self.select(table, {"box": 2}), "box2")       # box max_jobs = 2: full
        self.assertEqual(self.select(table, {"box": 2, "box2": 1}), "local")   # box2 max_jobs = 1: full too

    def test_unavailable_nodes_fall_back_to_local(self):
        bad = {"ok": False, "kind": "unreachable", "reason": "x", "fix": "y"}
        self.assertEqual(self.select({"box": bad, "box2": bad}), "local")

    def test_not_enough_ram_after_reserve_falls_back_to_local(self):
        self.assertEqual(self.select({"box": OK(1100), "box2": OK(1000)}), "local")

    def test_lock_is_held_while_the_choice_is_recorded_and_released_after(self):
        lock = os.path.join(self.state, ".select.lock")
        seen = []
        self.select({"box": OK(4000), "box2": OK(1000)}, on_choice=lambda n: seen.append((n, os.path.isdir(lock))))
        self.assertEqual(seen, [("box", True)])
        self.assertFalse(os.path.exists(lock))

    def test_live_counts_may_be_a_callable_evaluated_under_the_lock(self):
        lock = os.path.join(self.state, ".select.lock")
        seen = []
        self.select({"box": OK(4000), "box2": OK(1000)}, lambda: seen.append(os.path.isdir(lock)) or {})
        self.assertEqual(seen, [True])

    def test_stale_lock_is_broken(self):
        lock = os.path.join(self.state, ".select.lock")
        os.mkdir(lock)
        old = os.path.getmtime(lock) - 600
        os.utime(lock, (old, old))
        self.assertEqual(self.select({"box": OK(4000), "box2": OK(1000)}), "box")

    def test_profile_node_list_is_respected(self):
        cfg = self.cfg()
        cfg.nodes["box"]["profiles"] = ["other"]
        with mock.patch.object(nodes, "probe", fake_probe({"box": OK(9000), "box2": OK(2000)})):
            self.assertEqual(nodes.select(cfg, "work", [], {}, say=lambda s: None), "box2")


class CliTests(LaunchCase):
    def test_delega_nodes_lists_local_and_nodes(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = nodes.main(["--profile", "work"])
        self.assertEqual(rc, 0)
        text = out.getvalue()
        self.assertIn("local: ok", text)
        self.assertIn("box: ok", text)
        self.assertIn("box2: no", text)
        self.assertIn("ssh down2", text)

    def test_requires_flag(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            nodes.main(["--requires", "broken"])
        self.assertIn("compute broken", out.getvalue())


if __name__ == "__main__":
    unittest.main()

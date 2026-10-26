"""F3 execution: --node auto order (kindling first), nodes refused for an expired compute, golden lockfile
fingerprints and `delega doctor --batch`. Kindling runs on tests/fakes/kling-bin; host nodes get a
canned probe (this machine's free RAM must not decide a test)."""
import contextlib
import io
import json
import os
import shutil
from unittest import mock

from launch_env import sh
from test_kindling import KindlingCase

from delega import databases, golden, nodes, preflight, util

REAL_PROBE = nodes.probe


def host_raw(compute=None):
    return {"claude": True, "email": "dev@example.com", "mem_mb": 9000, "disk_gb": 100, "load_pct": 5,
            "containers": 0, "claude_version": "2.1.5", "compute": compute or {}}


class AutoCase(KindlingCase):
    """Profile `work` lists box, box2 and the kindling node vm; box2 is up unless a test says otherwise."""
    order = None
    expired = ()          # host nodes whose compute `tool` is expired

    def write_config(self):
        super().write_config()
        with open(self.cfg_path) as f:
            text = f.read()
        nodes_line = 'nodes = ["box", "box2", "vm"]'
        if self.order:
            nodes_line += "\nauto_order = " + json.dumps(self.order)
        with open(self.cfg_path, "w") as f:
            f.write(text.replace('nodes = ["box", "box2"]', nodes_line, 1))

    def setUp(self):
        super().setUp()
        self.probed = []

        def probe(cfg, node, profile, requires=(), watch=()):
            node = cfg.node(node) if isinstance(node, str) else node
            profile = cfg.profile(profile) if isinstance(profile, str) else profile
            self.probed.append((node["name"], tuple(watch)))
            if node.get("runner") == "kindling":
                return REAL_PROBE(cfg, node, profile, requires, watch=watch)
            comp = {c: ("expired" if node["name"] in self.expired else "ok") for c in list(requires) + list(watch)}
            res = nodes.evaluate(cfg, profile, node, host_raw(comp), requires, watch=watch)
            res["node"] = node["name"]
            return res

        p = mock.patch.object(nodes, "probe", probe)
        p.start()
        self.addCleanup(p.stop)

    def select(self, requires=(), **job):
        lines = []
        name = nodes.select(self.cfg(), "work", list(requires), {}, say=lines.append, job=dict({"repo": "api"}, **job))
        return name, "\n".join(lines)

    def refused(self):
        return [f for e, f in self.events if e == "refused" and f.get("scope") == "node"]


class AutoOrder(AutoCase):
    def test_plain_job_goes_to_kindling_before_bigger_hosts(self):
        name, out = self.select()
        self.assertEqual(name, "vm")
        self.assertEqual([n for n, _ in self.probed], ["vm"])      # hosts are not even probed
        self.assertIn("ls -q", self.kling_log().replace("template ls -q", "ls -q"))

    def test_requires_and_heavy_go_to_hosts(self):
        self.assertEqual(self.select(["tool"])[0], "box")
        name, out = self.select(heavy=True)
        self.assertEqual(name, "box")
        self.assertIn("heavy", out)

    def test_no_golden_for_the_repo_skips_kindling_with_the_fix(self):
        shutil.rmtree(os.path.join(self.goldens, "claude-api"))
        name, out = self.select()
        self.assertEqual(name, "box")
        self.assertIn("delega golden build --node vm", out)

    def test_full_kindling_host_falls_through_to_hosts(self):
        os.makedirs(os.path.join(self.node_home, ".kling-vms"))
        for n in ("delega-a", "delega-b"):
            open(os.path.join(self.node_home, ".kling-vms", n), "w").close()
        self.assertEqual(self.select()[0], "box")
        self.assertEqual(self.refused()[0]["node"], "vm")
        self.assertEqual(self.refused()[0]["stage"], "select")

    def test_everything_down_ends_on_this_machine(self):
        self.expired = ("box", "box2")
        self.assertEqual(self.select(heavy=True)[0], "local")

    def test_run_auto_launches_in_the_vm(self):
        os.environ["STUB_COMMIT_TEXT"] = "auto"
        rc, out, err = self.run_cli([self.repo, self.brief, "--branch", "delega/a1"])
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(util.read_meta(self.job_dir())["NODE"], "vm")
        self.assertIn("run -name delega-", self.kling_log())


class AutoOrderConfigured(AutoCase):
    order = ["host", "kindling"]

    def test_profile_auto_order_wins(self):
        self.assertEqual(self.select()[0], "box")
        self.assertEqual(nodes.auto_order(self.cfg().profile("work")), ["host", "kindling", "local"])


class ExpiredCompute(AutoCase):
    expired = ("box",)

    def test_declared_compute_expired_excludes_the_node_without_requires(self):
        name, out = self.select(heavy=True)
        self.assertEqual(name, "box2")
        self.assertIn(("box", ("tool",)), self.probed)               # watched although not required
        self.assertIn("compute tool is expired", out)
        self.assertIn("run the-tool login", out)                     # the exact fix
        r = self.refused()[0]
        self.assertEqual((r["node"], r["kind"]), ("box", "compute"))
        self.assertIn("ssh -t box 'run the-tool login'", r["fix"])

    def test_missing_compute_is_not_a_reason_without_requires(self):
        cfg, profile = self.cfg(), self.cfg().profile("work")
        r = nodes.evaluate(cfg, profile, cfg.node("box"), host_raw({"tool": "missing"}), (), watch=["tool"])
        self.assertTrue(r["ok"])
        r = nodes.evaluate(cfg, profile, cfg.node("box"), host_raw({"tool": "missing"}), ["tool"])
        self.assertFalse(r["ok"])

    def test_delega_nodes_explains_it(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.object(nodes, "local_probe",
                                                                lambda *a, **k: {"ok": True, "vms": None}):
            nodes.main(["--profile", "work"])
        text = out.getvalue()
        self.assertIn("box: no — compute tool is expired", text)
        self.assertIn("fix: ssh -t box 'run the-tool login'", text)
        self.assertIn("vm: ok · 0 delega VMs alive", text)
        self.assertIn("--node auto tries: kindling → host → docker → local", text)

    def test_doctor_batch_refuses_and_counts_slots(self):
        out = []
        with mock.patch.object(nodes, "local_probe", lambda *a, **k: {"ok": True}):
            rc = preflight.batch(self.cfg(), self.cfg().profile("work"), 3, self.repo, say=out.append)
        text = "\n".join(out)
        self.assertEqual(rc, 0, text)                    # vm 2 + box2 1 = 3
        self.assertIn("✗ box: compute tool is expired", text)
        self.assertIn("✓ vm", text)
        self.assertEqual([r["stage"] for r in self.refused()], ["batch"])
        with mock.patch.object(nodes, "local_probe", lambda *a, **k: {"ok": True}):
            self.assertEqual(preflight.batch(self.cfg(), self.cfg().profile("work"), 4, self.repo, say=out.append), 1)

    def test_doctor_batch_kling_down_and_version_skew(self):
        self.expired = ()
        os.environ["PATH"] = os.environ["PATH"].replace(os.path.join(os.path.dirname(__file__), "fakes", "kling-bin"),
                                                        "/nonexistent")
        out = []
        with mock.patch.object(nodes, "local_probe", lambda *a, **k: {"ok": True}), \
                mock.patch.object(nodes, "claude_version", lambda *a, **k: "2.9.0"):
            rc = preflight.batch(self.cfg(), self.cfg().profile("work"), 1, None, say=out.append)
        text = "\n".join(out)
        self.assertEqual(rc, 1, text)
        self.assertIn("vm: khost: kling does not answer", text)
        self.assertIn("claude 2.1.5 on the node, 2.9.0 here", text)
        self.assertEqual({r["kind"] for r in self.refused()}, {"unreachable", "version"})


class Fingerprint(KindlingCase):
    def commit(self, path, text):
        full = os.path.join(self.repo, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as f:
            f.write(text)
        sh("git", "-C", self.repo, "add", ".")
        sh("git", "-C", self.repo, "commit", "-qm", path)

    def note(self, **fields):
        cfg = self.cfg()
        databases.note_golden(cfg, cfg.node("vm"), "claude-api", repo_head="abc", **fields)

    def record(self):
        cfg = self.cfg()
        return databases.load_state(cfg)["goldens"][f"{databases.hkey(cfg.node('vm'))}/claude-api"]

    def fake_bake(self):
        """Stands in for golden.sh: the golden becomes the repo at HEAD, as the real bake leaves it."""
        calls = []

        def bake(repo, name, env):
            calls.append((repo, name))
            g = os.path.join(self.goldens, name)
            shutil.rmtree(g)
            os.makedirs(g)
            sh("git", "clone", "-q", repo, os.path.join(g, "wt"))
            sh("git", "-C", os.path.join(g, "wt"), "branch", "delega-base")
            return 0
        p = mock.patch.object(golden, "run_script", bake)
        p.start()
        self.addCleanup(p.stop)
        return calls

    def test_fingerprint_follows_lockfiles_only(self):
        self.assertEqual(golden.lock_fingerprint(self.repo), "")
        self.commit("package-lock.json", "{}")
        a = golden.lock_fingerprint(self.repo)
        self.commit("src/app/other.py", "y = 2\n")
        self.assertEqual(golden.lock_fingerprint(self.repo), a)
        for path in ("web/pnpm-lock.yaml", "requirements-dev.txt", "go.sum", "Cargo.lock", "poetry.lock"):
            self.commit(path, "x")
            b = golden.lock_fingerprint(self.repo)
            self.assertNotEqual(a, b, path)
            a = b
        self.assertEqual(golden.lock_fingerprint(self.repo, "HEAD~1"), golden.lock_fingerprint(self.repo, "HEAD~1"))
        self.assertNotEqual(golden.lock_fingerprint(self.repo, "HEAD~1"), a)

    def test_matching_fingerprint_launches_without_a_word(self):
        self.commit("package-lock.json", "{}")
        self.note(lockfp=golden.lock_fingerprint(self.repo))
        calls = self.fake_bake()
        rc, out, err = self.run_cli([self.repo, self.brief, "--node", "vm"])
        self.assertEqual(rc, 0, out + err)
        self.assertNotIn("lockfiles", out)
        self.assertEqual(calls, [])

    def test_stale_golden_warns_and_launches(self):
        self.note(lockfp="0" * 64)
        self.commit("package-lock.json", '{"v": 2}')
        calls = self.fake_bake()
        rc, out, err = self.run_cli([self.repo, self.brief, "--node", "vm"])
        self.assertEqual(rc, 0, out + err)
        self.assertIn("baked with other lockfiles (golden 000000000000", out)
        self.assertIn("--rebuild-golden", out)
        self.assertEqual(calls, [])
        self.assertTrue(any(e == "golden_stale" and not f["rebuild"] for e, f in self.events))

    def test_rebuild_golden_flag_rebuilds_before_cloning(self):
        self.note(lockfp="0" * 64)
        self.commit("package-lock.json", '{"v": 2}')
        calls = self.fake_bake()
        rc, out, err = self.run_cli([self.repo, self.brief, "--node", "vm", "--rebuild-golden"])
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(calls, [(self.repo, "claude-api")])
        self.assertIn("rebuilding golden claude-api", out)
        self.assertEqual(self.record()["lockfp"], golden.lock_fingerprint(self.repo))
        ev = [e for e, _ in self.events]
        self.assertLess(ev.index("golden_build"), ev.index("launch"))
        log = self.kling_log()
        self.assertIn("run -name delega-", log)

    def test_policy_auto_rebuild(self):
        self.note(lockfp="0" * 64)
        calls = self.fake_bake()
        with mock.patch.dict(os.environ, {"DELEGA_GOLDEN_AUTO_REBUILD": "true"}):
            rc, out, err = self.run_cli([self.repo, self.brief, "--node", "vm"])
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.record()["lockfp"], "")                # this repo has no lockfiles

    def test_failed_rebuild_refuses_the_job(self):
        self.note(lockfp="0" * 64)
        with mock.patch.object(golden, "run_script", lambda *a: 3):
            rc, out, err = self.run_cli([self.repo, self.brief, "--node", "vm", "--rebuild-golden"])
        self.assertEqual(rc, 5, out + err)
        self.assertIn("rc=3", err)
        self.assertNotIn("run -name delega-", self.kling_log())
        self.assertTrue(util.read_meta(self.job_dir()).get("END"))

    def test_unknown_golden_only_warns(self):
        calls = self.fake_bake()
        rc, out, err = self.run_cli([self.repo, self.brief, "--node", "vm"])
        self.assertEqual(rc, 0, out + err)
        self.assertIn("has no lockfile fingerprint", out)
        self.assertEqual(calls, [])

    def test_golden_ls_shows_fingerprint_and_age(self):
        self.commit("package-lock.json", "{}")
        fp = golden.lock_fingerprint(self.repo)
        self.note(lockfp=fp)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = golden.main(["ls", "--node", "vm", "--profile", "work"])
        self.assertEqual(rc, 0)
        self.assertRegex(out.getvalue(), r"claude-api .*\(0m\) · repo abc · locks " + fp[:12])


if __name__ == "__main__":
    import unittest
    unittest.main()

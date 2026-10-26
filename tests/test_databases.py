"""Databases for kindling jobs: volumes from dumps, seed sandbox, data golden, worker env and context."""
import contextlib
import hashlib
import io
import json
import os
import sys
from unittest import mock

from launch_env import FAKES, LaunchCase, sh

from delega import util

DUMP = b"-- synthetic dump, made up for the tests\n" + b"insert into t values (1);\n" * 200


class Databases(LaunchCase):
    def setUp(self):
        self.vmhome = None
        super().setUp()
        self.goldens = os.path.join(self.root, "goldens")
        g = os.path.join(self.goldens, "claude-api")
        os.makedirs(g)
        sh("git", "clone", "-q", self.repo, os.path.join(g, "wt"))
        sh("git", "-C", os.path.join(g, "wt"), "branch", "delega-base")
        sh("git", "-C", self.repo, "remote", "add", "origin", "git@example.com:acme/api.git")
        self.dump = os.path.join(self.root, "dump.sql.gz")
        with open(self.dump, "wb") as f:
            f.write(DUMP)
        p = mock.patch.dict(os.environ, {
            "FAKE_KLING_GOLDENS": self.goldens, "FAKE_KLING_VMHOME": self.vmhome, "FAKE_SPLIT_BYTES": "1000",
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
token_command = ["sh", "-c", "echo tok"]
account = "dev@example.com (token)"

[projects."acme/api".databases.main]
engine = "postgres"
version = "16"
database = "app"

[projects."acme/api".databases.cache]
engine = "redis"
"""
        super().write_config()

    # helpers
    def cli(self, module, argv):
        import importlib
        mod = importlib.import_module(f"delega.{module}")
        fn = getattr(mod, "main_db", None) if module == "databases" else mod.main
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = fn(["--repo", self.repo] + argv if module == "databases" else argv)
        return rc, out.getvalue(), err.getvalue()

    def vol(self, name="db-api-main"):
        return os.path.join(self.node_home, ".kling-volumes", name)

    def kling_log(self):
        p = os.path.join(self.node_home, ".kling.log")
        return open(p).read() if os.path.exists(p) else ""

    def vol_digest(self, name="db-api-main"):
        h = hashlib.sha256()
        for root, _, files in sorted(os.walk(self.vol(name))):
            for f in sorted(files):
                h.update(f.encode() + open(os.path.join(root, f), "rb").read())
        return h.hexdigest()

    def make_volume(self):
        rc, out, err = self.cli("databases", ["--node", "vm", "volume", "create", "main", "--from-file", self.dump])
        self.assertEqual(rc, 0, out + err)
        return out

    # declaration
    def test_specs_defaults_and_env(self):
        from delega import databases
        cfg = self.cfg()
        sp = databases.specs(cfg, "acme/api")
        self.assertEqual(sp["main"]["volume"], "db-api-main")
        self.assertEqual(sp["cache"]["database"], "cache")
        env = databases.env([sp["main"], sp["cache"]])
        self.assertEqual(env["DATABASE_URL"], "postgresql://delega:delega@127.0.0.1:5432/app")
        self.assertEqual(env["PGDATABASE"], "app")
        self.assertEqual(env["REDIS_URL"], "redis://127.0.0.1:6379/0")
        self.assertEqual(databases.select(cfg, "acme/api", []), [])
        self.assertEqual([s["name"] for s in databases.select(cfg, "acme/api", ["all"])], ["main", "cache"])
        with self.assertRaises(databases.DbError):
            databases.select(cfg, "acme/api", ["nope"])

    def test_env_clash_and_unknown_engine(self):
        from delega import config, databases
        cfg = config.Config({"projects": {"a/b": {"databases": {
            "x": {"engine": "postgres"}, "y": {"engine": "postgres"}}}}})
        with self.assertRaises(databases.DbError):
            databases.specs(cfg, "a/b")
        with self.assertRaises(config.ConfigError):
            config.Config({"projects": {"a/b": {"databases": {"x": {"engine": "oracle"}}}}})

    # volumes
    def test_volume_from_local_file_is_streamed_in_parts_and_verified(self):
        out = self.make_volume()
        parts = sorted(p for p in os.listdir(self.vol()) if p.startswith("dump."))
        self.assertGreater(len(parts), 3)
        self.assertTrue(all(p.startswith("dump.sql.gz.part-") for p in parts))
        self.assertEqual(b"".join(open(os.path.join(self.vol(), p), "rb").read() for p in parts), DUMP)
        manifest = open(os.path.join(self.vol(), "MANIFEST")).read()
        sha = hashlib.sha256(DUMP).hexdigest()
        self.assertIn(f"SHA256={sha}", manifest)
        self.assertIn("ENGINE=postgres", manifest)
        self.assertIn("SOURCE=file", manifest)
        self.assertNotIn(self.dump, manifest)          # where the dump came from is not recorded
        self.assertIn(sha[:12], out)
        st = json.load(open(os.path.join(self.state, "kindling.json")))
        self.assertEqual(st["volumes"]["khost/db-api-main"]["sha256"], sha)
        ev = [f for e, f in self.events if e == "db_volume"]
        self.assertEqual(ev[0]["volume"], "db-api-main")
        self.assertEqual(ev[0]["action"], "create")
        # the loader VM is gone; nothing of the dump stays on the host outside the volume
        self.assertNotIn("delega-load", "".join(os.listdir(os.path.join(self.node_home, ".kling-vms"))))

    def test_volume_from_host_command_and_refresh_keeps_undo(self):
        rc, out, err = self.cli("databases", ["--node", "vm", "volume", "create", "main",
                                              "--from-host-cmd", "printf 'first dump'", "--format", "sql"])
        self.assertEqual(rc, 0, out + err)
        rc, out, err = self.cli("databases", ["--node", "vm", "volume", "create", "main",
                                              "--from-host-cmd", "printf x", "--format", "sql"])
        self.assertEqual(rc, 5)
        self.assertIn("refresh", err)
        rc, out, err = self.cli("databases", ["--node", "vm", "volume", "refresh", "main",
                                              "--from-host-cmd", "printf 'second dump'", "--format", "sql"])
        self.assertEqual(rc, 0, out + err)
        snaps = [v for v in os.listdir(os.path.join(self.node_home, ".kling-volumes")) if v.startswith("db-api-main@")]
        self.assertEqual(len(snaps), 1)
        data = b"".join(open(os.path.join(self.vol(), p), "rb").read()
                        for p in sorted(os.listdir(self.vol())) if p.startswith("dump."))
        self.assertEqual(data, b"second dump")

    def test_bad_format_and_empty_dump_refused(self):
        rc, out, err = self.cli("databases", ["--node", "vm", "volume", "create", "main",
                                              "--from-host-cmd", "true", "--format", "rdb"])
        self.assertEqual(rc, 5)
        self.assertIn("rdb", err)
        rc, out, err = self.cli("databases", ["--node", "vm", "volume", "create", "main",
                                              "--from-host-cmd", "true", "--format", "sql"])
        self.assertEqual(rc, 5)
        self.assertIn("empty", err)

    # jobs
    def test_job_with_db_from_volume(self):
        self.make_volume()
        before = self.vol_digest()
        os.environ["STUB_COMMIT_TEXT"] = "queried"
        rc, out, err = self.run_cli([self.repo, self.brief, "--db", "main", "--branch", "delega/db1"])
        self.assertEqual(rc, 0, out + err)
        log = self.kling_log()
        self.assertIn("-from claude-api -egress allowlist", log)
        self.assertIn("sandbox create -name delega-seed-db-api-main", log)
        self.assertIn(":/seed:ro", log)
        self.assertRegex(log, r"dbctl up postgres main app \S+/tmp/delega-seed/main")
        self.assertIn("no data golden", out)
        # the worker got the connection and its context; the volume did not change
        job = self.job_dir()
        ctx = open(os.path.join(job, "context.md")).read()
        self.assertIn("postgresql://delega:delega@127.0.0.1:5432/app", ctx)
        self.assertIn("Never copy rows", ctx)
        self.assertIn(os.path.basename(job)[:21], ctx)
        self.assertEqual(self.vol_digest(), before)
        meta = util.read_meta(job)
        self.assertEqual(meta["GOLDEN"], "claude-api")
        self.assertEqual(meta["DB_MODE"], "volume")
        self.assertIn("main:db-api-main@", meta["DBS"])
        launch = next(f for e, f in self.events if e == "launch")
        self.assertEqual(launch["golden"], "claude-api")
        self.assertEqual(launch["dbs"][0]["volume"], "db-api-main")
        self.assertEqual(launch["dbs"][0]["sha"], hashlib.sha256(DUMP).hexdigest()[:12])
        end = next(f for e, f in self.events if e == "end")
        self.assertEqual(end["vm"], meta["VM"])
        self.assertIn("db-api-main", end["dbs"])
        self.assertEqual(sh("git", "-C", self.repo, "show", "delega/db1:result.txt"), "queried")
        self.assertIn(("scan", "clean"), [(e, f.get("result")) for e, f in self.events])
        from delega import audit
        line = audit.format_record({"ts": "t", "event": "launch", "dbs": launch["dbs"]})
        self.assertIn("dbs=main:db-api-main@" + launch["dbs"][0]["sha"] + "(volume)", line)

    def test_worker_env_has_the_database_variables(self):
        self.make_volume()
        os.environ["STUB_SLEEP"] = "1"
        seen = {}
        from delega import launch
        real = launch.wait_and_finish

        def spy(cfg, job_dir, poll_s=None):
            import time
            chan = os.path.join(self.vmhome, util.read_meta(job_dir)["CHANNEL"])
            for _ in range(100):
                if os.path.exists(os.path.join(chan, "stub.json")):
                    break
                time.sleep(0.1)
            seen.update(util.read_json(os.path.join(chan, "stub.json")) or {})
            seen["context_in_vm"] = os.path.exists(os.path.join(chan, "context.md"))
            return real(cfg, job_dir, poll_s)

        with mock.patch.object(launch, "wait_and_finish", spy):
            rc, out, err = self.run_cli([self.repo, self.brief, "--db", "main", "--node", "vm"])
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(seen["db_env"]["PGDATABASE"], "app")
        self.assertEqual(seen["env"]["DELEGA_NODE"], "vm")
        self.assertTrue(seen["env"]["DELEGA_JOB_ID"])
        self.assertTrue(seen["context_in_vm"])

    def test_data_golden_is_used_while_fresh(self):
        self.make_volume()
        rc, out, err = self.cli("golden", ["--node", "vm", "db", self.repo, "--db", "main"])
        self.assertEqual(rc, 0, out + err)
        baked = os.path.join(self.goldens, "claude-api-db.root", "var", "lib", "delega-db", "main", "data")
        self.assertEqual(open(baked, "rb").read(), DUMP)
        rc, out, err = self.run_cli([self.repo, self.brief, "--db", "main", "--node", "vm"])
        self.assertEqual(rc, 0, out + err)
        self.assertIn("-from claude-api-db -egress allowlist", self.kling_log())
        self.assertIn("(postgres, golden)", out)
        self.assertEqual(util.read_meta(self.job_dir())["DB_MODE"], "golden")
        # a refreshed volume makes it stale: back to the volume, saying why
        rc, out, err = self.cli("databases", ["--node", "vm", "volume", "refresh", "main",
                                              "--from-host-cmd", "printf newer", "--format", "sql"])
        self.assertEqual(rc, 0, out + err)
        rc, out, err = self.run_cli([self.repo, self.brief, "--db", "main", "--node", "vm"])
        self.assertEqual(rc, 0, out + err)
        self.assertIn("refreshed after", out)
        self.assertEqual(util.read_meta(self.job_dir())["DB_MODE"], "volume")
        rc, out, err = self.run_cli([self.repo, self.brief, "--db", "main", "--node", "vm", "--db-mode", "golden"])
        self.assertEqual(rc, 2, out + err)

    def test_db_refused_off_kindling_and_unknown(self):
        rc, out, err = self.run_cli([self.repo, self.brief, "--db", "main", "--node", "local"])
        self.assertEqual(rc, 2, out + err)
        self.assertIn("kindling", err)
        rc, out, err = self.run_cli([self.repo, self.brief, "--db", "nope", "--node", "vm"])
        self.assertEqual(rc, 2, out + err)
        self.assertIn("nope", err)

    def test_db_of_another_account_refused(self):
        self.make_volume()
        with open(self.cfg_path, "a") as f:
            f.write('\n[profiles.other]\nowner = "self"\nlauncher = ["claude"]\n')
        rc, out, err = self.run_cli([self.repo, self.brief, "--db", "main", "--node", "vm", "--profile", "other"])
        self.assertEqual(rc, 2, out + err)
        self.assertIn("belongs to profile 'work'", err)
        self.assertNotIn("run -name delega-2", self.kling_log())     # no job VM (the loader made the volume)
        rc, out, err = self.cli("sandbox", ["--node", "vm", "--profile", "other", "up", "x", self.repo, "--db", "main"])
        self.assertEqual(rc, 5, out + err)

    def test_failed_db_start_destroys_vm(self):
        self.make_volume()
        os.environ["FAKE_DBCTL_FAIL"] = "1"
        try:
            rc, out, err = self.run_cli([self.repo, self.brief, "--db", "main", "--node", "vm"])
        finally:
            os.environ.pop("FAKE_DBCTL_FAIL")
        self.assertEqual(rc, 5, out + err)
        self.assertRegex(self.kling_log(), r"\nrm delega-")
        self.assertFalse(os.path.exists(self.vmhome))
        self.assertTrue(util.read_meta(self.job_dir()).get("END"))

    # sandbox
    def test_sandbox_up_exec_env_freeze_rm(self):
        self.make_volume()
        rc, out, err = self.cli("sandbox", ["--node", "vm", "up", "dev", self.repo, "--db", "main", "--ttl", "10m"])
        self.assertEqual(rc, 0, out + err)
        log = self.kling_log()
        self.assertIn("run -name sb-dev -from claude-api -egress allowlist -allow anthropic.com -ttl 10m -on-ttl freeze", log)
        self.assertEqual(sh("git", "-C", os.path.join(self.vmhome, "wt"), "rev-parse", "--abbrev-ref", "HEAD"), "sb/dev")
        rc, out, err = self.cli("sandbox", ["--node", "vm", "env", "dev"])
        self.assertIn("PGDATABASE=app", out)
        rc, out, err = self.cli("sandbox", ["--node", "vm", "freeze", "dev"])
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(json.load(open(os.path.join(self.node_home, ".kling-vms", "sb-dev")))["frozen"], True)
        rc, out, err = self.cli("sandbox", ["--node", "vm", "ls"])
        self.assertIn("sb-dev", out)
        rc, out, err = self.cli("sandbox", ["--node", "vm", "rm", "dev"])
        self.assertEqual(rc, 0, out + err)
        self.assertFalse(os.path.exists(os.path.join(self.node_home, ".kling-vms", "sb-dev")))
        kinds = [e for e, _ in self.events]
        self.assertIn("sandbox_up", kinds)
        self.assertIn("sandbox_rm", kinds)

    def test_golden_build_dry_run_takes_engines_from_project(self):
        from delega import nodes
        with mock.patch.object(nodes, "claude_version", lambda *a, **k: "2.1.5"):
            rc, out, err = self.cli("golden", ["--node", "vm", "build", self.repo, "--dry-run"])
        self.assertEqual(rc, 0, out + err)
        self.assertIn("DB_ENGINES=postgres:16 redis", out)
        self.assertIn("KINDLING_HOST=khost", out)
        self.assertIn("CLAUDE_VERSION=2.1.5", out)


class ContextAndBundle(LaunchCase):
    def test_broker_appends_context(self):
        from delega import broker
        ch = broker.Channel(os.path.join(self.root, "chan"))
        self.assertEqual(broker.job_context(ch), "")
        with open(ch.p("context.md"), "w") as f:
            f.write("## delega: this job\n- Job `x`\n")
        self.assertIn("Job `x`", broker.job_context(ch))

    def test_local_job_gets_context(self):
        rc, out, err = self.run_cli([self.repo, self.brief, "--node", "local"])
        self.assertEqual(rc, 0, out + err)
        ctx = open(os.path.join(self.job_dir(), "context.md")).read()
        self.assertIn("profile `work`", ctx)
        self.assertIn("node `local`", ctx)
        self.assertNotIn("Databases", ctx)

    def test_bundle_carries_skills_rules_and_orchestrator_skills(self):
        from delega import config, sync
        b = os.path.join(self.root, "bundle")
        for d in ("skills/one", "rules", "agents"):
            os.makedirs(os.path.join(b, d))
        open(os.path.join(b, "skills/one/SKILL.md"), "w").write("---\nname: one\n---\n")
        open(os.path.join(b, "rules/r.md"), "w").write("rule\n")
        open(os.path.join(b, "agents/a.md"), "w").write("agent\n")
        mine = os.path.join(self.root, "myskills", "orq")
        os.makedirs(mine)
        open(os.path.join(mine, "SKILL.md"), "w").write("---\nname: orq\n---\n")
        open(os.path.join(mine, ".env"), "w").write("SECRET=1\n")
        cfg = config.Config({"profiles": {"p": {"owner": "self", "node_bundle": b, "bundle_skills": [mine]}}})
        staged, skipped = sync.stage(cfg, cfg.profile("p"), os.path.join(self.root, "stage"))
        for want in ("claude/skills/one/SKILL.md", "claude/rules/r.md", "claude/agents/a.md",
                     "claude/skills/orq/SKILL.md"):
            self.assertIn(want, staged)
        self.assertNotIn("claude/skills/orq/.env", staged)

import json
import os
import re
import signal
import subprocess
import sys
import time
import unittest

try:
    from tests.launch_env import FAKES, ROOT, BRIEF, LaunchCase, sh
except ImportError:  # run from inside tests/
    from launch_env import FAKES, ROOT, BRIEF, LaunchCase, sh

from delega import jobs, launch, remote, util


def read(path):
    with open(path) as f:
        return f.read()


class BriefTests(unittest.TestCase):
    def test_lint_accepts_english_and_spanish(self):
        self.assertEqual(launch.lint_brief(BRIEF, "en"), [])
        es = "# Encargo\n## Terminado cuando\n- ok\n## Tope\n- 30 min\n## Informe\n- corto\n"
        self.assertEqual(launch.lint_brief(es, "es"), [])

    def test_lint_reports_what_is_missing(self):
        self.assertEqual(launch.lint_brief("# Task\nDo it.\n", "en"), ["done", "cap", "report"])
        self.assertEqual(launch.lint_brief("## Done when\nx\n## Report\ny\n", "en"), ["cap"])

    def test_lint_respects_required_list(self):
        self.assertEqual(launch.lint_brief("nothing", "en", ["report"]), ["report"])

    def test_zones_from_brief_backticks_and_bullets(self):
        self.assertEqual(launch.zones_from_brief(BRIEF), ["src/app/*"])
        t = "## Zona\n\n- src/a.py\n- docs/*\n\n## Otra\n- ignored/x\n"
        self.assertEqual(launch.zones_from_brief(t), ["src/a.py", "docs/*"])
        self.assertEqual(launch.zones_from_brief("# Task\nno zone"), [])
        t2 = "## Zone\nOnly: `src/delega/launch.py`, `tests/test_launch.py`, i18n keys prefixed `launch.`\n## Goal\n"
        self.assertEqual(launch.zones_from_brief(t2), ["src/delega/launch.py", "tests/test_launch.py"])

    def test_globs_overlap(self):
        o = launch.globs_overlap
        self.assertTrue(o("src/app/*", "src/app/main.py"))
        self.assertTrue(o("src", "src/app/main.py"))
        self.assertTrue(o("src/**", "src/a/b.py"))
        self.assertTrue(o("*.md", "README.md"))
        self.assertTrue(o("", "anything"))
        self.assertFalse(o("src/app/*", "src/lib/*"))
        self.assertFalse(o("src/a.py", "src/ab.py"))
        self.assertFalse(o("docs/*", "src/*"))
        self.assertTrue(launch.zones_overlap([], ["x"]))
        self.assertIsNone(launch.zones_overlap(["a/*"], ["b/*"]))


class RefusalTests(LaunchCase):
    def test_budget_refusal_exits_8_and_creates_no_job(self):
        self.spent = 999.0
        rc, out, err = self.run_cli([self.repo, self.brief, "--node", "local"])
        self.assertEqual(rc, 8)
        self.assertIn("daily budget", err)
        self.assertEqual(self.jobs_in_state(), [])
        self.assertEqual(self.events[-1][0], "refused")

    def test_budget_uses_the_projects_policy(self):
        self.spent = 100.0   # under the 150 default
        rc, _, err = self.run_cli([self.repo, self.brief, "--node", "local", "--mode", "bypassPermissions"])
        self.assertEqual(rc, 2)   # reached the mode check, not refused for budget
        self.spent = 150.0
        rc, _, err = self.run_cli([self.repo, self.brief, "--node", "local"])
        self.assertEqual(rc, 8)

    def test_over_budget_flag_lets_it_through(self):
        self.spent = 999.0
        rc, out, err = self.run_cli([self.repo, self.brief, "--node", "local", "--over-budget"])
        self.assertEqual(rc, 0, err)

    def test_bypass_permissions_is_refused(self):
        rc, _, err = self.run_cli([self.repo, self.brief, "--mode", "bypassPermissions"])
        self.assertEqual(rc, 2)
        self.assertIn("bypassPermissions", err)

    def test_lint_refusal_and_no_lint(self):
        with open(self.brief, "w") as f:
            f.write("# Task\nDo the thing.\n")
        rc, _, err = self.run_cli([self.repo, self.brief, "--node", "local"])
        self.assertEqual(rc, 2)
        self.assertIn("closing report", err)
        self.assertEqual(self.jobs_in_state(), [])
        rc, _, err = self.run_cli([self.repo, self.brief, "--node", "local", "--no-lint"])
        self.assertEqual(rc, 0, err)

    def test_lint_messages_follow_the_locale(self):
        with open(self.brief, "w") as f:
            f.write("# Task\n")
        os.environ["DELEGA_LOCALE"] = "es"
        rc, _, err = self.run_cli([self.repo, self.brief])
        self.assertEqual(rc, 2)
        self.assertIn("al brief le falta", err)

    def test_empty_brief(self):
        open(self.brief, "w").close()
        rc, _, err = self.run_cli([self.repo, self.brief])
        self.assertEqual(rc, 2)

    def test_forbidden_term_in_brief_exits_9_and_never_prints_it(self):
        with open(self.brief, "a") as f:
            f.write("\nThe client is AcmeCorp.\n")
        rc, out, err = self.run_cli([self.repo, self.brief, "--node", "local"])
        self.assertEqual(rc, 9)
        self.assertNotIn("acmecorp", (out + err).lower())
        self.assertEqual(self.jobs_in_state(), [])

    def test_branch_prefix_policy(self):
        sh("git", "-C", self.repo, "remote", "add", "origin", "git@github.com:example-org/api.git")
        rc, _, err = self.run_cli([self.repo, self.brief, "--branch", "wip/x"])
        self.assertEqual(rc, 2)
        self.assertIn("branch_prefixes", err)

    def test_unknown_profile_is_a_config_error(self):
        rc, _, err = self.run_cli([self.repo, self.brief, "--profile", "nope"])
        self.assertEqual(rc, 3)

    def test_zone_overlap_with_a_live_job_is_refused(self):
        live = self.new_job(NODE="local", REPO="api", ZONE="src/app/main.py")
        rc, _, err = self.run_cli([self.repo, self.brief, "--node", "local"])
        self.assertEqual(rc, 2)
        self.assertIn(os.path.basename(live), err)
        self.assertIn("src/app/*", err)
        self.assertEqual(len(self.jobs_in_state()), 1)   # only the pre-existing one

    def test_disjoint_zone_and_other_repo_do_not_conflict(self):
        os.environ["DELEGA_MAX_LOCAL_JOBS"] = "9"
        self.new_job(NODE="local", REPO="api", ZONE="docs/*")
        rc, _, err = self.run_cli([self.repo, self.brief, "--node", "local"])
        self.assertEqual(rc, 0, err)
        self.new_job(id="20260101-000001-2", NODE="local", REPO="other", ZONE="src/app/*")
        rc, _, err = self.run_cli([self.repo, self.brief, "--node", "local"])
        self.assertEqual(rc, 0, err)

    def test_finished_job_does_not_hold_its_zone(self):
        self.new_job(NODE="local", REPO="api", ZONE="src/app/*", END="1")
        rc, _, err = self.run_cli([self.repo, self.brief, "--node", "local"])
        self.assertEqual(rc, 0, err)

    def test_no_zone_means_whole_repo(self):
        self.new_job(NODE="local", REPO="api", ZONE="docs/*")
        with open(self.brief, "w") as f:
            f.write(BRIEF.replace("## Zone", "## Notes"))
        rc, _, err = self.run_cli([self.repo, self.brief, "--node", "local"])
        self.assertEqual(rc, 2)

    def test_zone_option_overrides_the_brief(self):
        self.new_job(NODE="local", REPO="api", ZONE="src/app/main.py")
        rc, _, err = self.run_cli([self.repo, self.brief, "--node", "local", "--zone", "docs/*"])
        self.assertEqual(rc, 0, err)


class ResumeTests(LaunchCase):
    def finished_job(self, sid, node="local", age_min=0, **meta):
        d = self.new_job(id="20260102-000000-9", NODE=node, END="1", REPO="api", **meta)
        with open(os.path.join(d, "result.json"), "w") as f:
            json.dump({"session_id": sid, "result": "x"}, f)
        old = time.time() - age_min * 60
        os.utime(os.path.join(d, "result.json"), (old, old))
        return d

    def test_old_session_is_refused_and_suggests_handoff(self):
        self.finished_job("s-old", age_min=300)
        rc, _, err = self.run_cli([self.repo, self.brief, "--node", "local", "--resume", "s-old"])
        self.assertEqual(rc, 2)
        self.assertIn("delega handoff 20260102-000000-9", err)
        self.assertEqual(self.events[-1][0], "refused")

    def test_force_resume_overrides_the_age(self):
        self.finished_job("s-old", age_min=300)
        rc, _, err = self.run_cli([self.repo, self.brief, "--node", "local", "--resume", "s-old", "--force-resume"])
        self.assertEqual(rc, 0, err)
        argv = json.load(open(os.path.join(self.job_dir(), "stub.json")))["argv"]
        self.assertEqual(argv[argv.index("--resume") + 1], "s-old")

    def test_recent_session_resumes_without_lint(self):
        self.finished_job("s-new", age_min=5)
        with open(self.brief, "w") as f:
            f.write("continue please\n")
        rc, _, err = self.run_cli([self.repo, self.brief, "--node", "local", "--resume", "s-new"])
        self.assertEqual(rc, 0, err)

    def test_unknown_session_with_node_auto_asks_for_the_node(self):
        rc, _, err = self.run_cli([self.repo, self.brief, "--resume", "s-unknown"])
        self.assertEqual(rc, 2)
        self.assertIn("--node", err)

    def test_affinity_from_sessions_jsonl_beats_the_node_option(self):
        launch.record_session(self.cfg(), "s-1", "local", "", "", "work", "job1")
        rc, _, err = self.run_cli([self.repo, self.brief, "--node", "box", "--resume", "s-1"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(util.read_meta(self.job_dir())["NODE"], "local")

    def test_affinity_from_legacy_sesiones_tsv(self):
        with open(os.path.join(self.state, "sesiones.tsv"), "w") as f:
            f.write("s-legacy\tlocal\t\t\n")
        found = launch.find_session(self.cfg(), "s-legacy")
        self.assertEqual(found["node"], "local")
        rc, _, err = self.run_cli([self.repo, self.brief, "--node", "box", "--resume", "s-legacy"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(util.read_meta(self.job_dir())["NODE"], "local")

    def test_legacy_job_dir_is_found_by_its_session_id(self):
        d = self.new_job(id="20260103-000000-5", HOST="local", FIN="1")
        with open(os.path.join(d, "salida.json"), "w") as f:
            json.dump({"session_id": "s-legacy2"}, f)
        found = launch.find_session(self.cfg(), "s-legacy2")
        self.assertEqual(found["job_dir"], d)
        self.assertEqual(found["node"], "local")


class FailureTests(LaunchCase):
    def assertClosed(self):
        d = self.job_dir()
        self.assertTrue(util.read_meta(d).get("END"), "failed launch left a ghost job")
        self.assertFalse(jobs.alive(self.cfg(), d))

    def test_identity_mismatch_local_refuses_and_closes_the_job(self):
        os.environ["FAKE_EMAIL"] = "me@personal.org"
        rc, _, err = self.run_cli([self.repo, self.brief, "--node", "local"])
        self.assertEqual(rc, 4)
        self.assertIn("me@personal.org", err)
        self.assertIn("/login", err)
        self.assertClosed()

    def test_identity_mismatch_remote_refuses(self):
        os.environ["FAKE_EMAIL"] = "me@personal.org"
        rc, _, err = self.run_cli([self.repo, self.brief, "--node", "box"])
        self.assertEqual(rc, 4)
        self.assertIn("ssh -t box", err)
        self.assertClosed()
        self.assertFalse(os.path.exists(os.path.join(self.node_home, "delega")))   # nothing was shipped

    def test_unreachable_node_closes_the_job(self):
        rc, _, err = self.run_cli([self.repo, self.brief, "--node", "box2"])
        self.assertEqual(rc, 5)
        self.assertIn("unreachable", err)
        self.assertClosed()

    def test_unknown_node_closes_the_job(self):
        rc, _, err = self.run_cli([self.repo, self.brief, "--node", "ghost"])
        self.assertEqual(rc, 3)
        self.assertClosed()

    def test_required_compute_missing(self):
        rc, _, err = self.run_cli([self.repo, self.brief, "--node", "local", "--requires", "broken"])
        self.assertEqual(rc, 5)
        self.assertIn("run the-broken fix", err)
        self.assertClosed()

    def test_local_limit(self):
        self.new_job(id="20260101-000001-1", NODE="local", REPO="a", ZONE="x/*")
        self.new_job(id="20260101-000002-1", NODE="local", REPO="b", ZONE="y/*")
        rc, _, err = self.run_cli([self.repo, self.brief, "--node", "local"])
        self.assertEqual(rc, 5)
        self.assertIn("max_local_jobs", err)
        self.assertClosed()

    def test_remote_version_skew(self):
        os.environ["FAKE_VERSION_REMOTE"] = "3.0.1"
        rc, _, err = self.run_cli([self.repo, self.brief, "--node", "box"])
        self.assertEqual(rc, 5)
        self.assertIn("--allow-version-skew", err)
        self.assertClosed()
        rc, out, err = self.run_cli([self.repo, self.brief, "--node", "box", "--allow-version-skew"])
        self.assertEqual(rc, 0, err)

    def test_remote_failure_before_start_closes_the_job(self):
        with unittest_patch(remote, "push_head", side_effect=remote.RemoteError("boom")):
            rc, _, err = self.run_cli([self.repo, self.brief, "--node", "box"])
        self.assertEqual(rc, 5)
        self.assertClosed()


def unittest_patch(obj, name, **kw):
    from unittest import mock
    return mock.patch.object(obj, name, **kw)


class LocalRunTests(LaunchCase):
    def test_full_local_run(self):
        rc, out, err = self.run_cli([self.repo, self.brief, "--node", "local", "--cap-min", "7", "--cap-usd", "3",
                                     "--cap-turns", "50", "--model", "haiku"])
        self.assertEqual(rc, 0, err)
        d = self.job_dir()
        meta = util.read_meta(d)
        for k in ("NODE", "CHANNEL", "OWNER", "PROFILE", "ACCOUNT", "ZONE", "BASE", "CLAUDE_VERSION",
                  "DELEGA_VERSION", "REPO", "END"):
            self.assertIn(k, meta)
        self.assertEqual(meta["NODE"], "local")
        self.assertEqual(meta["CHANNEL"], d)
        self.assertEqual(meta["OWNER"], "tester")
        self.assertEqual(meta["ACCOUNT"], "dev@example.com")
        self.assertEqual(meta["ZONE"], "src/app/*")
        self.assertEqual(meta["BASE"], sh("git", "-C", self.repo, "rev-parse", "HEAD"))
        stub = json.load(open(os.path.join(d, "stub.json")))
        self.assertEqual(stub["argv"][0], self.claude)
        self.assertEqual(stub["argv"][1:], ["-p", "--permission-mode", "auto", "--model", "haiku"])
        self.assertEqual(stub["cwd"], self.repo)
        self.assertEqual(stub["config_dir"], os.path.join(self.root, ".claude-work"))
        env = stub["env"]
        self.assertEqual((env["DELEGA_JOB_MAX_MIN"], env["DELEGA_JOB_MAX_USD"], env["DELEGA_JOB_MAX_TURNS"]),
                         ("7.0", "3.0", "50"))
        self.assertEqual(env["DELEGA_LOCALE"], "en")
        self.assertIn("RC=0", out)
        self.assertIn("final report", out)
        self.assertIn("cost_usd=0.5", out)
        self.assertEqual(read(os.path.join(d, "forbidden.txt")).split(), ["acmecorp"])
        self.assertEqual([e for e, _ in self.events], ["launch", "end"])
        sess = [json.loads(l) for l in open(os.path.join(self.state, "sessions.jsonl"))]
        self.assertEqual(sess[0]["node"], "local")
        self.assertTrue(sess[0]["session_id"].startswith("sess-"))
        self.assertEqual(jobs.live_counts(self.cfg()), {})

    def test_worker_rc_is_propagated(self):
        os.environ["STUB_RC"] = "3"
        rc, out, err = self.run_cli([self.repo, self.brief, "--node", "local"])
        self.assertEqual(rc, 3)
        self.assertTrue(util.read_meta(self.job_dir()).get("END"))

    def test_auto_without_a_free_node_falls_back_to_local(self):
        os.environ["FAKE_EMAIL"] = "dev@example.com"
        with unittest_patch(launch.nodes, "select", return_value="local"):
            rc, out, err = self.run_cli([self.repo, self.brief])
        self.assertEqual(rc, 0, err)
        self.assertIn("running locally", out)

    def test_auto_picks_the_node(self):
        rc, out, err = self.run_cli([self.repo, self.brief])   # box is reachable, box2 is down
        self.assertEqual(rc, 0, err)
        self.assertEqual(util.read_meta(self.job_dir())["NODE"], "box")
        self.assertIn("box2: no", out)

    def test_run_help_documents_every_option(self):
        text = launch.build_parser().format_help()
        for opt in ("--node", "--profile", "--requires", "--resume", "--force-resume", "--mode", "--model",
                    "--worktree", "--branch", "--owner", "--cap-min", "--cap-usd", "--cap-turns", "--zone",
                    "--no-lint", "--over-budget", "--allow-version-skew"):
            self.assertIn(opt, text)
        r = subprocess.run([sys.executable, "-m", "delega", "run", "--help"], capture_output=True, text=True,
                           env={**os.environ, "PYTHONPATH": os.path.join(ROOT, "src")})
        self.assertEqual(r.returncode, 0)
        self.assertIn("--allow-version-skew", r.stdout)


class RemoteRunTests(LaunchCase):
    def remote_args(self):
        return [self.repo, self.brief, "--node", "box", "--cap-min", "9"]

    def test_full_remote_run(self):
        os.environ["STUB_COMMIT_TEXT"] = "all good"
        rc, out, err = self.run_cli(self.remote_args())
        self.assertEqual(rc, 0, out + err)
        d = self.job_dir()
        meta = util.read_meta(d)
        job_id = jobs.job_id(d)
        chan = f"delega/jobs/{job_id}"
        self.assertEqual(meta["NODE"], "box")
        self.assertEqual(meta["CHANNEL"], chan)
        self.assertEqual(meta["BRANCH"], f"delega/{job_id}")
        self.assertEqual(meta["ACCOUNT"], "dev@example.com")
        self.assertTrue(meta["END"])
        self.assertTrue(meta["PYZ_SHA"])
        home = self.node_home
        # bare repo + base ref + worktree on the job branch
        bare = os.path.join(home, "delega", "api.git")
        self.assertTrue(os.path.isdir(bare))
        self.assertEqual(sh("git", "-C", bare, "config", "user.email"), "a@example.com")
        self.assertEqual(sh("git", "-C", bare, "rev-parse", f"refs/heads/delega-base/{job_id}"), meta["BASE"])
        wt = os.path.join(home, chan, "wt")
        self.assertEqual(sh("git", "-C", wt, "rev-parse", "--abbrev-ref", "HEAD"), meta["BRANCH"])
        # run.sh
        sh_text = read(os.path.join(d, "run.sh"))
        self.assertTrue(sh_text.startswith("#!/bin/bash -l"))
        self.assertIn("export DELEGA_JOB_MAX_MIN=9.0", sh_text)
        self.assertIn("export DELEGA_LOCALE=en", sh_text)
        self.assertIn('export CLAUDE_CONFIG_DIR=' + os.path.join(self.root, ".claude-work-node"), sh_text)
        self.assertIn("stub_broker.py run", sh_text)     # DELEGA_BROKER_CMD test hook
        self.assertEqual(read(os.path.join(home, chan, "run.sh")), sh_text)
        self.assertEqual(read(os.path.join(home, chan, "brief.md")), BRIEF)
        self.assertEqual(read(os.path.join(home, chan, "forbidden.txt")).split(), ["acmecorp"])
        self.assertTrue(os.path.exists(os.path.join(home, "delega", "delega.pyz")))
        # the worker ran in the worktree with the node's config dir
        stub = json.load(open(os.path.join(home, chan, "stub.json")))
        self.assertEqual(os.path.realpath(stub["cwd"]), os.path.realpath(wt))
        self.assertEqual(stub["config_dir"], os.path.join(self.root, ".claude-work-node"))
        # channel copied back, branch fetched, scan clean, summary printed
        for f in ("summary.json", "result.json", "progress.log", "state.json", "rc"):
            self.assertTrue(os.path.exists(os.path.join(d, f)), f)
        self.assertEqual(sh("git", "-C", self.repo, "log", "--format=%s", "-1", meta["BRANCH"]), "stub work")
        self.assertIn(f"branch {meta['BRANCH']} fetched", out)
        self.assertIn("no forbidden terms", out)
        self.assertIn("RC=0", out)
        self.assertIn("final report", out)
        self.assertEqual([e for e, _ in self.events], ["launch", "end"])
        self.assertEqual(self.events[0][1]["node"], "box")
        sess = [json.loads(l) for l in open(os.path.join(self.state, "sessions.jsonl"))]
        self.assertEqual((sess[0]["node"], sess[0]["worktree"], sess[0]["branch"]),
                         ("box", f"{chan}/wt", meta["BRANCH"]))
        self.assertEqual(jobs.live_counts(self.cfg()), {})

    def test_credentials_are_never_transferred(self):
        rc, out, err = self.run_cli(self.remote_args())
        self.assertEqual(rc, 0, out + err)
        transfers = read(os.path.join(self.node_home, ".scp.log")) + read(os.path.join(self.node_home, ".ssh.log"))
        self.assertTrue(transfers)
        for pat in remote.CREDENTIAL_PATTERNS:
            for line in read(os.path.join(self.node_home, ".scp.log")).splitlines():
                src, dst = line.split("\t")
                self.assertIsNone(re.search(pat, src.split(":", 1)[-1] if src.startswith("box:") else src), line)
                self.assertIsNone(re.search(pat, dst), line)
        # nothing credential-like landed on the node either
        for root, dirs, files in os.walk(self.node_home):
            for name in dirs + files:
                self.assertFalse(name.startswith(".claude"), os.path.join(root, name))
        for bad in ("~/.claude", ".config/gcloud", ".ssh/", "/.env"):
            self.assertNotIn(bad, read(os.path.join(self.node_home, ".scp.log")))

    def test_refuse_credentials_guard(self):
        for p in ("/home/x/.claude/settings.json", "/home/x/.claude-work/.credentials.json",
                  "/home/x/.config/gcloud/adc.json", "/home/x/.ssh/id_ed25519", "/repo/.env", "/repo/.env.local"):
            with self.assertRaises(remote.RemoteError, msg=p):
                remote.scp_to("box", p, "x")
        self.assertIsNone(remote.refuse_credentials("/tmp/brief.md"))
        self.assertIsNone(remote.refuse_credentials("/tmp/environment.md"))

    def test_forbidden_term_in_the_returned_branch_gives_exit_9(self):
        os.environ["STUB_COMMIT_TEXT"] = "built for AcmeCorp"
        rc, out, err = self.run_cli(self.remote_args())
        self.assertEqual(rc, 9, out + err)
        self.assertIn("DATA WARNING", out)
        self.assertIn("result.txt", out)
        self.assertNotIn("acmecorp", out.lower().replace("forbidden", ""))   # the term itself is never printed
        self.assertTrue(util.read_meta(self.job_dir()).get("END"))

    def test_dirty_tree_warns_and_ships_only_head(self):
        with open(os.path.join(self.repo, "src", "app", "main.py"), "a") as f:
            f.write("# dirty\n")
        rc, out, err = self.run_cli(self.remote_args())
        self.assertEqual(rc, 0, out + err)
        self.assertIn("uncommitted changes", out)

    def test_handoff_reuses_an_existing_branch_on_the_node(self):
        rc, out, err = self.run_cli(self.remote_args() + ["--branch", "delega/shared"])
        self.assertEqual(rc, 0, out + err)
        first = self.job_dir()
        # the first worktree still holds the branch: free it like a finished job's cleanup would
        wt1 = os.path.join(self.node_home, util.read_meta(first)["CHANNEL"], "wt")
        sh("git", "-C", os.path.join(self.node_home, "delega", "api.git"), "worktree", "remove", "--force", wt1)
        time.sleep(1.1)   # job ids are per second + pid: keep them distinct
        os.environ["STUB_COMMIT_TEXT"] = "second round"
        rc, out, err = self.run_cli(self.remote_args() + ["--branch", "delega/shared"])
        self.assertEqual(rc, 0, out + err)
        log = sh("git", "-C", self.repo, "log", "--format=%s", "delega/shared")
        self.assertEqual(log.splitlines()[0], "stub work")

    def test_resume_returns_to_the_same_worktree(self):
        rc, out, err = self.run_cli(self.remote_args())
        self.assertEqual(rc, 0, out + err)
        first = self.job_dir()
        sid = json.load(open(os.path.join(first, "summary.json")))["session_id"]
        time.sleep(1.1)
        rc, out, err = self.run_cli([self.repo, self.brief, "--resume", sid])
        self.assertEqual(rc, 0, out + err)
        second = self.job_dir()
        self.assertNotEqual(first, second)
        m1, m2 = util.read_meta(first), util.read_meta(second)
        self.assertEqual(m2["NODE"], "box")
        self.assertEqual(m2["WORKTREE"], m1["WORKTREE"])
        self.assertEqual(m2["BRANCH"], m1["BRANCH"])
        stub = json.load(open(os.path.join(self.node_home, m2["CHANNEL"], "stub.json")))
        self.assertEqual(stub["argv"][stub["argv"].index("--resume") + 1], sid)

    def test_run_sh_calls_the_broker_from_the_synced_pyz(self):
        del os.environ["DELEGA_BROKER_CMD"]
        cfg = self.cfg()
        text = launch.build_run_sh(cfg.node("box"), cfg.profile("work"), {}, "delega/jobs/J", "delega/jobs/J/wt", ["-p"])
        self.assertIn('"$HOME"/delega/delega.pyz broker run "$HOME"/delega/jobs/J', text)

    def test_docker_runner_builds_a_docker_run_line(self):
        del os.environ["DELEGA_BROKER_CMD"]
        cfg = self.cfg()
        node = cfg.node("box")
        node.update(runner="docker", docker_image="example/worker:1")
        profile = cfg.profile("work")
        text = launch.build_run_sh(node, profile, {"DELEGA_LOCALE": "en"}, "delega/jobs/J", "delega/jobs/J/wt",
                                   ["-p", "--permission-mode", "auto"])
        self.assertIn("docker run --rm -i", text)
        self.assertIn("example/worker:1", text)
        self.assertRegex(text, r'-v "\$HOME"/\S*\.claude-work-node:"\$HOME"/\S*\.claude-work-node:ro|'
                               r"-v \S*\.claude-work-node:\S*\.claude-work-node:ro")
        self.assertIn('-v "$PWD":"$PWD"', text)            # worktree, writable
        self.assertNotIn('"$PWD":"$PWD":ro', text)
        self.assertIn("broker run", text)
        self.assertLess(text.index("broker run"), text.index("docker run"))
        node["docker_image"] = None
        with self.assertRaises(launch.Refused):
            launch.build_run_sh(node, profile, {}, "c", "w", [])

    def test_adopt_after_the_waiter_dies(self):
        os.environ["STUB_SLEEP"] = "4"
        os.environ["STUB_COMMIT_TEXT"] = "adopted work"
        proc = subprocess.Popen([sys.executable, "-m", "delega", "run", *self.remote_args()],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                env={**os.environ, "PYTHONPATH": os.path.join(ROOT, "src")})
        self.addCleanup(proc.kill)
        deadline = time.time() + 30
        d = None
        while time.time() < deadline:
            dirs = self.jobs_in_state()
            if dirs:
                d = os.path.join(self.state, dirs[0])
                meta = util.read_meta(d)
                if meta.get("CHANNEL") and os.path.exists(os.path.join(self.node_home, meta["CHANNEL"], "lease.json")):
                    break
            time.sleep(0.1)
        else:
            self.fail("worker never started")
        os.kill(proc.pid, signal.SIGKILL)       # the waiter dies; the worker is detached and goes on
        proc.wait()
        cfg = self.cfg()
        self.assertFalse(util.read_meta(d).get("END"))
        self.assertEqual(jobs.reconcile(cfg), [(d, "orphan")])
        self.assertEqual(jobs.live_counts(cfg), {"box": 1})
        rc = jobs.adopt(cfg, d, poll_s=0.2)
        self.assertEqual(rc, 0)
        meta = util.read_meta(d)
        self.assertTrue(meta["END"])
        self.assertTrue(os.path.exists(os.path.join(d, "summary.json")))
        self.assertEqual(sh("git", "-C", self.repo, "log", "--format=%s", "-1", meta["BRANCH"]), "stub work")
        self.assertEqual(jobs.live_counts(cfg), {})
        self.assertEqual([e for e, _ in self.events], ["end"])   # the first process died before its end record
        self.assertEqual(jobs.adopt(cfg, d), 0)                  # already closed: nothing to do


if __name__ == "__main__":
    unittest.main()

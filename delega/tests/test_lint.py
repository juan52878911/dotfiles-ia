import json
import os
import unittest

try:
    from tests.launch_env import BRIEF, LaunchCase, sh
except ImportError:  # run from inside tests/
    from launch_env import BRIEF, LaunchCase, sh

from delega import lint

PAT = r"^(feature|fix|chore)/"


def codes(text, **kw):
    return [c for _, c, _ in lint.check(text, **kw)]


class BranchRuleTests(unittest.TestCase):
    def test_named_branch_outside_the_pattern(self):
        self.assertEqual(codes("**Tu rama** `exp/dedup-70` ya existe.", pattern=PAT), ["branch"])
        self.assertEqual(codes("Rama: `fix/ok-name` desde develop.", pattern=PAT), [])
        self.assertEqual(codes("New branch: spike-1, commit there.", pattern=PAT), [])   # bare word, no backticks
        self.assertEqual(codes("New branch: `spike-1`, commit there.", pattern=PAT), ["branch"])
        self.assertEqual(codes("git checkout -b wip/x", pattern=PAT), ["branch"])

    def test_prose_and_base_branches_are_not_branches(self):
        for text in ("Contexto: en este repo, rama develop actual.", "rama nueva desde origin/develop",
                     "informe con commits/sha/rama y tests", "la rama de error del gateway"):
            self.assertEqual(codes(text, pattern=PAT), [], text)

    def test_pattern_from_prefixes_and_precedence(self):
        self.assertEqual(lint.branch_pattern({"branch_prefixes": ["a/", "b."]}), r"^(?:a/|b\.)")
        self.assertEqual(lint.branch_pattern({"branch_prefixes": ["a/"]}, {"branch_pattern": "^x/"}), "^x/")
        self.assertIsNone(lint.branch_pattern({}, None))
        self.assertEqual(codes("Rama: `whatever/x`"), [])   # nothing configured: rule off


class LiveRuleTests(unittest.TestCase):
    def test_live_run_needs_requires_or_env_file(self):
        text = "Mide en vivo contra Vertex, una sola ronda."
        self.assertEqual(codes(text), ["live"])
        self.assertEqual(codes(text, requires=["vertex"]), [])
        self.assertEqual(codes(text + " Entorno: ~/env/api.env", env_file="~/env/api.env"), [])
        self.assertEqual(codes(text, env_file="~/env/api.env"), ["live"])

    def test_negated_live_mentions_do_not_count(self):
        self.assertEqual(codes("Todo offline: sin Vertex y nada de corridas vivas."), [])
        self.assertEqual(codes("No live run, no Gemini."), [])

    def test_custom_keywords(self):
        self.assertEqual(codes("call the Foo endpoint", live_keywords=[r"\bfoo\b"]), ["live"])
        self.assertEqual(codes("use Vertex", live_keywords=[r"\bfoo\b"]), [])


class TextRuleTests(unittest.TestCase):
    def test_reference_to_a_brief_not_attached(self):
        self.assertEqual(codes("Sigue con la sonda del brief original."), ["ref"])
        self.assertEqual(codes("As the previous brief said, keep going."), ["ref"])

    def test_absolute_time_needs_an_expiry_rule(self):
        self.assertEqual(codes("Lanza antes de las 14:30."), ["deadline"])
        self.assertEqual(codes("Lanza antes de las 14:30; si ya pasó, cancela."), [])
        self.assertEqual(codes("Duración media 12:30 min por caso."), [])

    def test_scripts_to_run_need_a_path(self):
        self.assertEqual(codes("Ejecuta run.sh una vez por ronda."), ["script"])
        self.assertEqual(codes("Ejecuta scripts/perf/run.sh una vez."), [])
        self.assertEqual(codes("Ejecuta ~/bench/run.sh una vez."), [])
        self.assertEqual(codes("El fichero setup.py define la versión."), [])   # named, not run

    def test_levels_and_off(self):
        text = "Ejecuta run.sh antes de las 10:00 con Vertex. Rama: `wip/x`"
        found = lint.check(text, pattern=PAT)
        self.assertEqual({(lv, c) for lv, c, _ in found},
                         {("error", "branch"), ("warn", "live"), ("warn", "deadline"), ("warn", "script")})
        found = lint.check(text, pattern=PAT, levels={"branch": "warn", "live": "off", "text": "error"})
        self.assertEqual({(lv, c) for lv, c, _ in found},
                         {("warn", "branch"), ("error", "deadline"), ("error", "script")})


class SectionsTests(unittest.TestCase):
    def test_sections(self):
        self.assertEqual(lint.sections(BRIEF), [])
        self.assertEqual(lint.sections("# Task\n"), ["done", "cap", "report"])


class LaunchLintTests(LaunchCase):
    def test_branch_error_refuses_before_anything_is_spent(self):
        sh("git", "-C", self.repo, "remote", "add", "origin", "git@github.com:example-org/api.git")
        with open(self.brief, "a") as f:
            f.write("\nRama: `wip/x`\n")
        rc, _, err = self.run_cli([self.repo, self.brief, "--node", "local"])
        self.assertEqual(rc, 2)
        self.assertIn("wip/x", err)
        self.assertEqual(self.jobs_in_state(), [])

    def test_warnings_print_and_launch_goes_on(self):
        with open(self.brief, "a") as f:
            f.write("\nMeasure it with a live run against Vertex.\n")
        rc, _, err = self.run_cli([self.repo, self.brief, "--node", "local"])
        self.assertEqual(rc, 0, err)
        self.assertIn("lint warning", err)
        rc, _, err = self.run_cli([self.repo, self.brief, "--node", "local", "--requires", "tool"])
        self.assertNotIn("lint warning", err)

    def test_policy_can_turn_a_rule_into_an_error(self):
        os.environ["DELEGA_LINT_LIVE"] = "error"
        self.addCleanup(os.environ.pop, "DELEGA_LINT_LIVE", None)
        with open(self.brief, "a") as f:
            f.write("\nMeasure it with a live run against Vertex.\n")
        rc, _, err = self.run_cli([self.repo, self.brief, "--node", "local"])
        self.assertEqual(rc, 2)
        self.assertIn("live", err)


class RoleTests(LaunchCase):
    extra_config = """
[roles.worker]
model = "sonnet"
[roles.reviewer]
model = "opus"
require_cap_usd = true
[roles.summarizer]
model = "haiku"
cap_usd = 1
"""

    def stub(self):
        with open(os.path.join(self.job_dir(), "stub.json")) as f:
            return json.load(f)

    def test_worker_is_the_default_role(self):
        rc, _, err = self.run_cli([self.repo, self.brief, "--node", "local"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.stub()["argv"][-2:], ["--model", "sonnet"])

    def test_reviewer_needs_an_explicit_cap(self):
        rc, _, err = self.run_cli([self.repo, self.brief, "--node", "local", "--role", "reviewer"])
        self.assertEqual(rc, 2)
        self.assertIn("--cap-usd", err)
        rc, _, err = self.run_cli([self.repo, self.brief, "--node", "local", "--role", "reviewer", "--cap-usd", "15"])
        self.assertEqual(rc, 0, err)
        s = self.stub()
        self.assertEqual(s["argv"][-2:], ["--model", "opus"])
        self.assertEqual(s["env"]["DELEGA_JOB_MAX_USD"], "15.0")

    def test_summarizer_gets_its_model_and_cap_and_flags_win(self):
        rc, _, err = self.run_cli([self.repo, self.brief, "--node", "local", "--role", "summarizer"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.stub()["argv"][-2:], ["--model", "haiku"])
        self.assertEqual(self.stub()["env"]["DELEGA_JOB_MAX_USD"], "1.0")
        rc, _, err = self.run_cli([self.repo, self.brief, "--node", "local", "--role", "summarizer",
                                   "--model", "sonnet", "--cap-usd", "2"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.stub()["argv"][-2:], ["--model", "sonnet"])
        self.assertEqual(self.stub()["env"]["DELEGA_JOB_MAX_USD"], "2.0")

    def test_unknown_role_is_refused(self):
        rc, _, err = self.run_cli([self.repo, self.brief, "--node", "local", "--role", "poet"])
        self.assertEqual(rc, 2)
        self.assertIn("poet", err)


if __name__ == "__main__":
    unittest.main()

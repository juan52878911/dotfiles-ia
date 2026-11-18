import nod_sandbox  # noqa: F401
import json
import os
import unittest
from unittest import mock

try:
    from tests.launch_env import FAKES, LaunchCase
except ImportError:
    from launch_env import FAKES, LaunchCase

from delega import jobs, util


class PiCase(LaunchCase):
    extra_config = ""

    def setUp(self):
        super().setUp()
        p = mock.patch.dict(os.environ, {"DELEGA_PI_CMD": os.path.join(FAKES, "pi"),
                                         "FAKE_PI_ARGV": os.path.join(self.root, "argv.json")})
        p.start()
        self.addCleanup(p.stop)
        os.environ.pop("FAKE_PI_MODE", None)

    def job(self):
        return sorted(jobs.job_dirs(self.cfg()))[-1]

    def run_pi(self, *extra):
        return self.run_cli([self.repo, self.brief, "--node", "local", "--engine", "pi", "--branch", "main", *extra])


class PiTests(PiCase):
    def test_ok_job_shows_in_ps_with_tools_and_events(self):
        rc, out, err = self.run_pi("--model", "minimax/MiniMax-M3")
        self.assertEqual(rc, 0, err)
        d = self.job()
        argv = json.load(open(os.path.join(self.root, "argv.json")))
        self.assertEqual(argv[:5], ["-p", "--mode", "json", "--model", "minimax/MiniMax-M3"])
        summ = util.read_json(os.path.join(d, "summary.json"))
        self.assertEqual((summ["state"], summ["rc"], summ["tools"], summ["engine"]), ("ok", 0, 2, "pi"))
        self.assertIsNotNone(summ["duration_min"])
        raw = [json.loads(l) for l in open(os.path.join(d, "events.jsonl"))]
        self.assertEqual(sum(e["type"] == "tool_execution_start" for e in raw), 2)
        log = open(os.path.join(d, "progress.log")).read()
        self.assertIn("TOOL\tbash: ls", log)
        self.assertIn("RESULT\tall done", log)
        text = "\n".join(jobs.describe(self.cfg(), d))
        self.assertIn("finished rc=0", text)
        self.assertIn("tools 2", text)
        self.assertIn("all done", out)

    def test_default_model(self):
        self.assertEqual(self.run_pi()[0], 0)
        self.assertIn("minimax/MiniMax-M3", json.load(open(os.path.join(self.root, "argv.json"))))

    def test_cap_min_cuts_the_process(self):
        os.environ["FAKE_PI_MODE"] = "hang"
        rc, out, err = self.run_pi("--cap-min", "0.03")
        self.assertEqual(rc, 124, err)
        summ = util.read_json(os.path.join(self.job(), "summary.json"))
        self.assertEqual(summ["state"], "capped")
        self.assertIn("time cap", summ["cap"])
        self.assertEqual(summ["tools"], 2)

    def test_crash_is_a_failed_job(self):
        os.environ["FAKE_PI_MODE"] = "crash"
        rc, _, _ = self.run_pi()
        self.assertEqual(rc, 3)
        self.assertEqual(util.read_json(os.path.join(self.job(), "summary.json"))["state"], "failed")


class PiForbiddenTests(PiCase):
    def write_config(self):
        super().write_config()
        txt = open(self.cfg_path).read().replace('compute = ["tool"]\n',
                                                 'compute = ["tool"]\nforbidden_engines = ["minimax","pi"]\n', 1)
        open(self.cfg_path, "w").write(txt)

    def test_trycore_style_profile_refuses_with_rc_2(self):
        rc, out, err = self.run_pi()
        self.assertEqual(rc, 2)
        self.assertIn("forbids the engine", err)
        self.assertEqual(jobs.job_dirs(self.cfg()), [])
        self.assertFalse(os.path.exists(os.path.join(self.root, "argv.json")))


if __name__ == "__main__":
    unittest.main()

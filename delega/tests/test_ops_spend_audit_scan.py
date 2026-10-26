import nod_sandbox  # noqa: F401  (Nod events of the tests go to a temp file)
import json
import os
import threading
import unittest

from ops_helpers import OpsCase, today
from delega import audit, scan, spend

TOML = """
[profiles.work]
owner = "self"
policy.budget.day_usd = 10
[profiles.other]
owner = "self"
[general]
default_profile = "work"
"""


class SpendTest(OpsCase):
    TOML = TOML

    def test_job_cost_fallback_chain_and_legacy_names(self):
        a = self.make_job("20260101-000001-1-a", files={"summary.json": {"cost_usd": 1.5}, "result.json": {"total_cost_usd": 9}})
        b = self.make_job("20260101-000002-2-b", files={"result.json": {"total_cost_usd": 2.0}})
        c = self.make_job("20260101-000003-3-c", files={"state.json": {"cost_usd": 0.5}})
        d = self.make_job("20260101-000004-4-d", files={"resumen.json": {"coste_usd": 3.0}})
        e = self.make_job("20260101-000005-5-e")
        self.assertEqual([spend.job_cost(x) for x in (a, b, c, d, e)], [1.5, 2.0, 0.5, 3.0, None])

    def test_total_by_day_profile_project(self):
        t = today()
        self.make_job(f"{t}-000001-1-a", {"PROFILE": "work", "PROJECT": "acme/api", "OWNER": "ana"}, {"summary.json": {"cost_usd": 2}})
        self.make_job(f"{t}-000002-2-b", {"PROFILE": "other", "OWNER": "bo"}, {"summary.json": {"cost_usd": 3}})
        self.make_job(f"{t}-000003-3-legacy", {"DUENO": "ana"}, {"resumen.json": {"coste_usd": 4}})
        self.make_job("20200101-000001-1-old", {"PROFILE": "work"}, {"summary.json": {"cost_usd": 100}})
        cfg = self.cfg()
        self.assertEqual(spend.total(cfg, t), 9)
        self.assertEqual(spend.total(cfg, t, profile="work"), 6)      # legacy job counts for the default profile
        self.assertEqual(spend.total(cfg, t, profile="other"), 3)
        self.assertEqual(spend.total(cfg, t, project="acme/api"), 2)
        self.assertEqual(spend.total(cfg, "all"), 109)

    def test_cli_groups_and_budget(self):
        t = today()
        self.make_job(f"{t}-000001-1-a", {"PROFILE": "work", "OWNER": "ana"}, {"summary.json": {"cost_usd": 2}})
        rc, out, _ = self.run_cli(spend.main, "--by", "profile")
        self.assertEqual(rc, 0)
        self.assertIn("work: 1 jobs", out)
        self.assertIn("budget today · work: $2.00 of $10.00", out)
        self.assertIn("budget today · other: $0.00 of $150.00", out)
        rc, out, _ = self.run_cli(spend.main, "all", "--total")
        self.assertEqual(out.strip(), "2.00")
        self.assertEqual(self.run_cli(spend.main, "--by", "nope")[0], 2)


class AuditTest(OpsCase):
    TOML = TOML

    def test_record_select_and_cli(self):
        cfg = self.cfg()
        audit.record(cfg, "launch", job="20260101-1-a", branch="delega/x", profile="work", cost_usd=None)
        audit.record(cfg, "end", job="20260101-1-a", cost_usd=1.5)
        audit.record(cfg, "launch", job="20260102-2-b", branch="feature/y")
        recs = audit.read(cfg)
        self.assertEqual(len(recs), 3)
        self.assertNotIn("cost_usd", recs[0])
        self.assertEqual(len(audit.select(recs, "20260101-1-a")), 2)
        self.assertEqual(len(audit.select(recs, "delega/x")), 1)
        rc, out, _ = self.run_cli(audit.main, "feature/y")
        self.assertIn("launch", out)
        self.assertNotIn("delega/x", out)
        self.assertIn("No audit records", self.run_cli(audit.main, "zzz")[1])

    def test_concurrent_appends_stay_valid_json(self):
        cfg = self.cfg()
        ts = [threading.Thread(target=lambda i=i: [audit.record(cfg, "launch", job=f"j{i}", note="x" * 500) for _ in range(30)])
              for i in range(6)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        lines = open(audit.path(cfg)).read().splitlines()
        self.assertEqual(len(lines), 180)
        [json.loads(line) for line in lines]


class ScanTest(OpsCase):
    TOML = TOML

    def terms(self, *words):
        p = os.path.join(self.root, "terms.txt")
        open(p, "w").write("# comment\n\n" + "\n".join(words) + "\n")
        return p

    def test_terms_text_redact(self):
        p = self.terms("Acme Bank", "projx")
        terms = scan.load_terms(p)
        self.assertEqual(terms, ["Acme Bank", "projx"])
        self.assertEqual(scan.scan_text("we met ACME BANK today", terms), ["Acme Bank"])
        self.assertEqual(scan.scan_text("nothing", terms), [])
        self.assertEqual(scan.redact("Acme bank and PROJX", terms), "[redacted] and [redacted]")

    def test_scan_branch_files_and_commits_without_printing_terms(self):
        repo = self.make_repo()
        self.commit_on(repo, "feat", "notes.md", "hello\nthis is for Acme Bank\n", msg="projx: add notes")
        self.commit_on(repo, "clean", "ok.md", "fine\n")
        terms = scan.load_terms(self.terms("Acme Bank", "projx"))
        res = scan.scan_branch(repo, "feat", "main", terms)
        self.assertEqual(res["files"], [{"path": "notes.md", "count": 1}])
        self.assertEqual(len(res["commit_lines"]), 1)
        self.assertEqual(scan.scan_branch(repo, "clean", None, terms), {"files": [], "commit_lines": []})
        tf = self.terms("Acme Bank", "projx")
        rc, out, err = self.run_cli(scan.main, repo, "feat", "main", "--terms", tf)
        self.assertEqual(rc, 9)
        self.assertIn("notes.md", out)
        self.assertNotIn("Acme", out + err)
        self.assertNotIn("projx", out + err)
        self.assertEqual(self.run_cli(scan.main, repo, "clean", "--terms", tf)[0], 0)

    def test_scan_text_cli_and_profile_terms_file(self):
        tf = self.terms("secretword")
        self.write_config(f'[profiles.work]\nowner = "self"\nforbidden_terms_file = "{tf}"\n')
        f = os.path.join(self.root, "t.txt")
        open(f, "w").write("a SecretWord appears\n")
        rc, out, _ = self.run_cli(scan.main, "--text", f)
        self.assertEqual(rc, 9)
        self.assertNotIn("secretword", out.lower())
        open(f, "w").write("fine\n")
        self.assertEqual(self.run_cli(scan.main, "--text", f)[0], 0)


if __name__ == "__main__":
    unittest.main()

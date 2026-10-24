"""launch (`delega run`): start a worker on this machine or on a node, wait for it, bring the work back.

Order of checks (nothing is spent before all of them pass): profile/project → permission mode →
daily budget → brief lint → zones → forbidden terms → resume guard + node affinity → node
selection → local limit → identity → compute → version → run → collect → scan → summary.
Exit codes: 2 invalid input/refused (lint, zones, mode, resume), 3 config, 4 wrong account,
5 no capacity or node unavailable, 8 over budget, 9 forbidden terms; otherwise the worker's rc.
"""
import argparse
import fnmatch
import json
import os
import re
import shlex
import subprocess
import sys
import time

from . import config as _config
from . import jobs, nodes, remote, util
from .i18n import t

REFUSED_RC = 2
COPY_BACK = ["summary.json", "result.json", "results.jsonl", "state.json", "progress.log", "notices.jsonl",
             "events.jsonl", "stderr.log", "inbox.jsonl", "rc"]


class Refused(Exception):
    def __init__(self, rc, msg, event="refused", **fields):
        super().__init__(msg)
        self.rc, self.msg, self.event, self.fields = rc, msg, event, fields


# ── optional collaborators (other modules; called through the documented contract) ──
def _spend_total(cfg, day, profile=None, project=None):
    from . import spend
    fn = getattr(spend, "total", None)
    return float(fn(cfg, day, profile=profile, project=project)) if fn else 0.0


def _terms(path):
    from . import scan
    fn = getattr(scan, "load_terms", None)
    return fn(_config.expand(path)) if fn and path else []


def _scan_text(text, terms):
    from . import scan
    fn = getattr(scan, "scan_text", None)
    return fn(text, terms) if fn and terms else []


def _scan_branch(repo_dir, branch, base, terms):
    from . import scan
    fn = getattr(scan, "scan_branch", None)
    return fn(repo_dir, branch, base, terms) if fn and terms else {"files": [], "commit_lines": 0}


def _audit(cfg, event, **fields):
    from . import audit
    fn = getattr(audit, "record", None)
    if fn:
        try:
            fn(cfg, event, **fields)
        except OSError:
            pass


# ── brief: lint and zones ───────────────────────────────────────────────────
LINT_PATTERNS = {   # English and Spanish wording; the same checks whatever the locale
    "done": r"done when|definition of done|acceptance|success criteri|terminad|criterio|hecho cuando|aceptaci",
    "cap": r"\bcap\b|\bbudget\b|at most|no more than|\bmax(imum)?\b|\btope\b|m[aá]ximo|presupuesto|no m[aá]s de",
    "report": r"report|informe",
}


def lint_brief(text, locale="en", require=None):
    """Sections the brief lacks: a subset of ["done", "cap", "report"] (headings in English or Spanish)."""
    require = require or ["done", "cap", "report"]
    return [k for k in require if not re.search(LINT_PATTERNS[k], text, re.I)]


def zones_from_brief(text):
    """Globs listed under a `## Zone` / `## Zona` heading: backticked tokens, else bullet items."""
    m = re.search(r"^#{1,6}\s*zon(?:e|a)s?\b[^\n]*\n(.*?)(?=^#{1,6}\s|\Z)", text, re.I | re.M | re.S)
    if not m:
        return []
    body = m.group(1)
    toks = re.findall(r"`([^`\n]+)`", body)
    if not toks:
        for line in body.splitlines():
            line = re.sub(r"^\s*[-*+]\s*", "", line).strip()
            toks += [x.strip() for x in re.split(r"[,\s]+", line.split(" — ")[0].split(" - ")[0]) if x.strip()]
    out = []
    for tok in toks:
        tok = tok.strip().strip(",;")
        if re.search(r"[/*]|\.\w{1,6}$", tok) and " " not in tok and tok not in out:
            out.append(tok)
    return out


def _glob_parts(g):
    return [p for p in g.strip().strip("/").split("/") if p]


def globs_overlap(a, b):
    """Could two path globs match a common file? Empty glob = the whole repo."""
    if not a or not b:
        return True
    pa, pb = _glob_parts(a), _glob_parts(b)
    for x, y in zip(pa, pb):
        if x == "**" or y == "**":
            return True
        if not (fnmatch.fnmatchcase(x, y) or fnmatch.fnmatchcase(y, x)):
            return False
    return True   # one is a directory prefix of the other


def zones_overlap(za, zb):
    if not za or not zb:
        return None if (za and zb) else ("**", "**")
    for a in za:
        for b in zb:
            if globs_overlap(a, b):
                return (a, b)
    return None


def zone_conflict(cfg, repo, zone, exclude=None):
    """(job_dir, glob_new, glob_live) of a live job of the same repo whose zone overlaps, else None."""
    for d in jobs.job_dirs(cfg):
        if exclude and os.path.abspath(d) == os.path.abspath(exclude):
            continue
        meta = util.read_meta(d)
        if meta.get("REPO") != repo or not jobs.alive(cfg, d):
            continue
        other = [z for z in meta.get("ZONE", "").split(",") if z]
        hit = zones_overlap(zone, other)
        if hit:
            return d, hit[0], hit[1]
    return None


# ── sessions ────────────────────────────────────────────────────────────────
def sessions_path(cfg):
    return os.path.join(cfg.state_dir, "sessions.jsonl")


def record_session(cfg, session_id, node, worktree, branch, profile, job):
    if session_id:
        os.makedirs(cfg.state_dir, exist_ok=True)
        util.append(sessions_path(cfg), json.dumps(
            {"session_id": session_id, "node": node, "worktree": worktree, "branch": branch,
             "profile": profile, "job": job}, ensure_ascii=False))


def find_session(cfg, sid):
    """Where a session ran: {node, worktree, branch, job_dir}. Reads sessions.jsonl, the legacy
    sesiones.tsv and, for the age check, the job directory that holds the session's result."""
    found = {}
    try:
        for line in jobs.slurp(sessions_path(cfg)).splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("session_id") == sid:
                found = {"node": r.get("node"), "worktree": r.get("worktree") or "", "branch": r.get("branch") or ""}
    except OSError:
        pass
    if not found:
        try:
            for line in jobs.slurp(os.path.join(cfg.state_dir, "sesiones.tsv")).splitlines():
                f = line.rstrip("\n").split("\t") + ["", "", "", ""]
                if f[0] == sid:
                    found = {"node": f[1], "worktree": f[2], "branch": f[3]}
        except OSError:
            pass
    for d in reversed(jobs.job_dirs(cfg)):
        for name in ("summary.json", "result.json"):
            data = util.read_json(util.channel_file(d, name), {}) or {}
            if data.get("session_id") == sid:
                found.setdefault("node", util.read_meta(d).get("NODE"))
                found["job_dir"] = d
                return found
    return found or None


def session_age_min(job_dir):
    for name in ("rc", "result.json"):
        p = util.channel_file(job_dir, name)
        if os.path.exists(p):
            return int((time.time() - os.path.getmtime(p)) / 60)
    return 0


# ── building the worker command ─────────────────────────────────────────────
def broker_argv(python=None, pyz=None):
    """argv prefix of `delega broker`. DELEGA_BROKER_CMD overrides it (test hook)."""
    env = os.environ.get("DELEGA_BROKER_CMD")
    if env:
        return shlex.split(env)
    if pyz:
        return [python or "python3", pyz, "broker"]
    pkg = os.path.dirname(util.PKG_DIR)
    if pkg.endswith(".pyz"):
        return [sys.executable, pkg, "broker"]
    return [sys.executable, "-m", "delega", "broker"]


def claude_args(mode, resume, model, worktree=None):
    a = ["-p", "--permission-mode", mode]
    if resume:
        a += ["--resume", resume]
    if model:
        a += ["--model", model]
    if worktree:
        a += ["--worktree", worktree]
    return a


def caps_env(cfg, profile, project, args):
    caps = {"DELEGA_JOB_MAX_MIN": args.cap_min if args.cap_min is not None else cfg.policy("job.max_min", profile, project),
            "DELEGA_JOB_MAX_USD": args.cap_usd if args.cap_usd is not None else cfg.policy("job.max_usd", profile, project),
            "DELEGA_JOB_MAX_TURNS": args.cap_turns if args.cap_turns is not None else cfg.policy("job.max_turns", profile, project),
            "DELEGA_JOB_GRACE_S": cfg.policy("job.grace_s", profile, project),
            "DELEGA_STALL_S": cfg.policy("stall_s", profile, project),
            "DELEGA_ANSWER_WAIT_S": cfg.policy("answer_wait_s", profile, project),
            "DELEGA_LEASE_RENEW_S": cfg.policy("lease.renew_s", profile, project),
            "DELEGA_LOCALE": cfg.locale}
    return {k: str(v) for k, v in caps.items()}


def _home(rel):
    """Path relative to the node's $HOME as a shell word."""
    rel = rel.strip()
    if rel.startswith("~/"):
        rel = rel[2:]
    if os.path.isabs(rel):
        return shlex.quote(rel)
    return '"$HOME"/' + shlex.quote(rel)


def build_run_sh(node, profile, caps, chan, wt, argv_claude):
    """run.sh for a node: exports caps, locale and the profile's config dir, runs the broker in the worktree
    (directly, or with the worker inside `docker run` when node.runner == "docker")."""
    py = node.get("python", "python3")
    pyz = f"{remote.workdir(node)}/delega.pyz"
    cd = nodes.config_dir_for(profile, node)
    lines = ["#!/bin/bash -l"]
    lines += [f"export {k}={shlex.quote(v)}" for k, v in caps.items()]
    if cd:
        lines.append(f"export CLAUDE_CONFIG_DIR={_home(cd)}")
    lines.append(f"cd {_home(wt)} || {{ echo 99 > {_home(chan)}/rc; exit; }}")
    launcher = nodes.launcher_for(profile, node)
    if node.get("runner", "host") == "docker":
        image = node.get("docker_image")
        if not image:
            raise Refused(3, t("launch.err.docker_image", node=node["name"]))
        d = ["docker", "run", "--rm", "-i", "--user", '"$(id -u):$(id -g)"', "-w", '"$PWD"',
             "-v", '"$PWD":"$PWD"',   # worktree, writable
             "-v", f'{_home(remote.workdir(node) + "/")}:{_home(remote.workdir(node) + "/")}']
        # the worktree's .git points into the bare repo under the workdir: mounted at the same path
        if cd:
            d += ["-v", f'{_home(cd)}:{_home(cd)}:ro', "-e", f'CLAUDE_CONFIG_DIR={_home(cd)}']
        worker = " ".join(d) + " " + shlex.quote(image) + " " + " ".join(shlex.quote(x) for x in launcher + argv_claude)
    else:
        worker = " ".join(shlex.quote(x) for x in launcher + argv_claude)
    broker = os.environ.get("DELEGA_BROKER_CMD") or f"{shlex.quote(py)} {_home(pyz)} broker"
    lines.append(f"{broker} run {_home(chan)} {_home(chan + '/brief.md')} -- {worker}"
                 f" < /dev/null 2>> {_home(chan)}/stderr.log")
    lines.append(f"rc=$?; test -f {_home(chan)}/rc || echo $rc > {_home(chan)}/rc")
    return "\n".join(lines) + "\n"


# ── collecting results ──────────────────────────────────────────────────────
def print_summary(job_dir, rc):
    summ = util.read_json(util.channel_file(job_dir, "summary.json"), {}) or {}
    res = util.read_json(util.channel_file(job_dir, "result.json"))
    if res is None and not summ:
        print(t("launch.sum.no_output", rc=rc, dir=job_dir))
        return
    res = res or {}
    cost = summ.get("cost_usd", res.get("total_cost_usd"))
    print(t("launch.sum.line", rc=rc, session=summ.get("session_id") or res.get("session_id"),
            error=res.get("is_error"), min=summ.get("duration_min", "?"),
            messages=summ.get("messages", res.get("num_turns", "?")), tools=summ.get("tools", "?"),
            cost=cost) + (" · " + t("launch.sum.cap", cap=summ["cap"]) if summ.get("cap") else ""))
    print(t("launch.sum.report"))
    print(res.get("result", ""))


RC_NOT_STARTED, RC_BROKER_LOST = 98, 97


def wait_for_rc(cfg, node, chan, poll_s):
    """rc of the remote job. Never waits forever: if the broker shows no sign of life (no lease
    nor state file) within DELEGA_START_TIMEOUT_S → 98 (run.sh did not start); if its lease went
    stale without an rc (broker killed, node rebooted) → 97. Transient ssh failures are retried."""
    q = shlex.quote(chan)
    start_timeout = float(os.environ.get("DELEGA_START_TIMEOUT_S", 180))
    stale = float(cfg.policy("lease.stale_s"))
    t0 = time.time()
    while True:
        if remote.ssh(node, f"test -f {q}/rc", timeout=30).returncode == 0:
            r = remote.ssh(node, f"cat {q}/rc", timeout=30)
            try:
                return int(r.stdout.strip().splitlines()[-1])
            except (ValueError, IndexError):
                return 1
        lease = remote.ssh(node, f"cat {q}/lease.json", timeout=30)
        if lease.returncode == 0:
            try:
                d = json.loads(lease.stdout)
                if not d.get("final") and time.time() - float(d.get("renewed", 0)) > stale:
                    return RC_BROKER_LOST
            except ValueError:
                pass
        elif time.time() - t0 > start_timeout:
            alive = remote.ssh(node, f"test -f {q}/state.json || test -f {q}/estado.json", timeout=30)
            if alive.returncode != 0:
                return RC_NOT_STARTED
        time.sleep(poll_s)


def wait_and_finish(cfg, job_dir, poll_s=None):
    """Remote job: wait for rc, copy the channel back, close the job, fetch the branch, scan, report.
    Shared by `delega run` and `delega adopt`. Returns the worker's rc (9 on forbidden terms in the branch)."""
    meta = util.read_meta(job_dir)
    node = cfg.node(meta["NODE"])
    chan = meta["CHANNEL"]
    poll_s = float(os.environ.get("DELEGA_WAIT_POLL_S", 20)) if poll_s is None else poll_s
    rc = wait_for_rc(cfg, node, chan, poll_s)
    for f in COPY_BACK:
        remote.scp_from(node, f"{chan}/{f}", job_dir + "/")
    util.add_meta(job_dir, END=int(time.time()))
    profile = cfg.profile(meta.get("PROFILE") or None)
    summ = util.read_json(util.channel_file(job_dir, "summary.json"), {}) or util.read_json(
        util.channel_file(job_dir, "result.json"), {}) or {}
    sid = summ.get("session_id") or ""
    branch, repo_dir = meta.get("BRANCH", ""), meta.get("REPO_DIR", "")
    if sid and not meta.get("RESUMED"):
        record_session(cfg, sid, meta["NODE"], meta.get("WORKTREE", ""), branch, profile["name"], job_id_of(job_dir))
    _audit(cfg, "end", job=job_id_of(job_dir), profile=profile["name"], account=meta.get("ACCOUNT"),
           node=meta["NODE"], branch=branch, base=meta.get("BASE"), project=meta.get("PROJECT"),
           owner=meta.get("OWNER"), cost_usd=summ.get("cost_usd", summ.get("total_cost_usd")), rc=rc)
    hits = False
    if branch and repo_dir and os.path.isdir(repo_dir):
        ok, log = remote.fetch_branch(repo_dir, node, meta.get("REPO", ""), branch)
        with open(os.path.join(job_dir, "fetch.log"), "w") as f:
            f.write(log)
        if ok:
            last = subprocess.run(["git", "-C", repo_dir, "log", "--oneline", "-1", branch], capture_output=True,
                                  text=True).stdout.strip()
            print(t("launch.branch.fetched", branch=branch, last=last))
            hits = _report_scan(repo_dir, branch, meta.get("BASE"), _terms(profile.get("forbidden_terms_file")))
        else:
            print(t("launch.branch.fetch_failed", branch=branch, log=os.path.join(job_dir, "fetch.log")))
    print_summary(job_dir, rc)
    return 9 if hits and rc == 0 else rc


def _report_scan(repo_dir, branch, base, terms):
    if not terms:
        return False
    base = base or subprocess.run(["git", "-C", repo_dir, "merge-base", "HEAD", branch], capture_output=True,
                                  text=True).stdout.strip()
    r = _scan_branch(repo_dir, branch, base, terms)
    if r.get("files") or r.get("commit_lines"):
        print(t("launch.scan.hits", branch=branch))
        for f in r.get("files", []):
            print("  " + t("launch.scan.file", file=f))
        if r.get("commit_lines"):
            print("  " + t("launch.scan.commits", n=r["commit_lines"]))
        return True
    print(t("launch.scan.clean", branch=branch))
    return False


def job_id_of(job_dir):
    return jobs.job_id(job_dir)


# ── CLI ─────────────────────────────────────────────────────────────────────
def build_parser():
    ap = argparse.ArgumentParser(
        prog="delega run", description="Launch a Claude Code worker on this machine or on a node, wait for it "
        "and bring its branch back.", epilog="Exit codes:" + __doc__.split("Exit codes:")[1],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("repo", help="git repository (or a folder inside it) the worker starts from")
    ap.add_argument("brief", help="file with the brief (see skills/delega/templates)")
    ap.add_argument("--node", default="auto", metavar="auto|local|NAME",
                    help="where to run: auto picks the node with most free RAM (falls back to local)")
    ap.add_argument("--profile", help="profile (account + policy); default: from repo_match, else the default profile")
    ap.add_argument("--requires", action="append", default=[], metavar="COMPUTE",
                    help="compute provider the job needs, as named in [compute.*] (repeatable)")
    ap.add_argument("--resume", metavar="SESSION_ID", help="continue a session on the node where it ran")
    ap.add_argument("--force-resume", action="store_true",
                    help="resume even if the session has been idle longer than policy resume_max_min")
    ap.add_argument("--mode", help="permission mode for the worker (default: policy permission_mode.default; "
                    "modes in permission_mode.deny are refused)")
    ap.add_argument("--model", help="model (default: the profile's model)")
    ap.add_argument("--worktree", metavar="NAME", help="local runs only: passed to claude --worktree")
    ap.add_argument("--branch", help="branch the worker commits to (default delega/<id>); an existing branch "
                    "on the node is continued (handoff)")
    ap.add_argument("--owner", help="orchestrator label for ps/watch (default: $DELEGA_OWNER or the folder name)")
    ap.add_argument("--cap-min", type=float, metavar="N", help="wall-clock cap in minutes (policy job.max_min)")
    ap.add_argument("--cap-usd", type=float, metavar="N", help="list-price cost cap (policy job.max_usd)")
    ap.add_argument("--cap-turns", type=int, metavar="N", help="assistant-message cap (policy job.max_turns)")
    ap.add_argument("--zone", action="append", default=[], metavar="GLOB",
                    help="files this job may touch (repeatable); default: the brief's `## Zone` section, else "
                    "the whole repo. Refused when a live job of the same repo overlaps")
    ap.add_argument("--no-lint", action="store_true", help="skip the brief checks (done-when, cap, report)")
    ap.add_argument("--over-budget", action="store_true", help="launch even if the daily budget is spent")
    ap.add_argument("--allow-version-skew", action="store_true",
                    help="allow a node whose claude MAJOR.MINOR differs from this machine's")
    return ap


def main(argv):
    args = build_parser().parse_args(argv)
    try:
        cfg = _config.load()
    except _config.ConfigError as e:
        print(f"delega: {e}", file=sys.stderr)
        return 3
    return Launch(cfg, args).run()


class Launch:
    def __init__(self, cfg, args):
        self.cfg, self.args = cfg, args
        self.job_dir = None
        self.started = False

    # -- helpers
    def fail(self, e):
        print(("ERROR: " if e.rc else "") + e.msg, file=sys.stderr)
        _audit(self.cfg, e.event, job=jobs.job_id(self.job_dir) if self.job_dir else None,
               reason=e.msg[:200], rc=e.rc, **e.fields)
        return e.rc

    def run(self):
        try:
            return self._run()
        except Refused as e:
            return self.fail(e)
        except _config.ConfigError as e:
            return self.fail(Refused(3, str(e)))
        except remote.RemoteError as e:
            return self.fail(Refused(5, str(e)))
        finally:
            # A launch that dies before the worker starts must not look alive for hours.
            if self.job_dir and not self.started and os.path.isdir(self.job_dir):
                jobs.close_job(self.job_dir, "launch_failed")

    def _run(self):
        cfg, a = self.cfg, self.args
        repo_dir = os.path.abspath(a.repo)
        if not os.path.isdir(repo_dir):
            raise Refused(REFUSED_RC, t("launch.err.no_repo", path=repo_dir))
        try:
            with open(a.brief, encoding="utf-8") as f:
                brief_text = f.read()
        except OSError:
            brief_text = ""
        if not brief_text.strip():
            raise Refused(REFUSED_RC, t("launch.err.brief_empty", path=a.brief))
        # 1. profile / project
        pname = a.profile or os.environ.get("DELEGA_PROFILE") or cfg.profile_for(repo_dir)
        profile = cfg.profile(pname)
        project = cfg.project_for(repo_dir)
        pol = lambda k: cfg.policy(k, profile, project)
        if a.branch and project:
            prefixes = cfg.projects[project].get("branch_prefixes")
            if prefixes and not any(a.branch.startswith(p) for p in prefixes):
                raise Refused(REFUSED_RC, t("launch.err.branch_prefix", branch=a.branch, prefixes=", ".join(prefixes)))
        # 2. permission mode
        mode = a.mode or pol("permission_mode.default")
        if mode in (pol("permission_mode.deny") or []):
            raise Refused(REFUSED_RC, t("launch.err.mode_denied", mode=mode))
        # 3. daily budget
        today = time.strftime("%Y%m%d")
        spent, limit = _spend_total(cfg, today), float(pol("budget.day_usd"))
        if not a.over_budget and spent >= limit:
            raise Refused(8, t("launch.err.budget", spent=f"{spent:.2f}", limit=f"{limit:.2f}"))
        # 4. brief lint
        if not a.resume and not a.no_lint:
            missing = lint_brief(brief_text, cfg.locale, pol("lint.require"))
            if missing:
                raise Refused(REFUSED_RC, t("launch.err.lint", missing="; ".join(t("launch.lint." + m) for m in missing)))
        # 5. zones
        repo = remote.repo_name(repo_dir) if _is_git(repo_dir) else os.path.basename(repo_dir)
        zone = a.zone or zones_from_brief(brief_text)
        clash = zone_conflict(cfg, repo, zone)
        if clash:
            raise Refused(REFUSED_RC, t("launch.err.zone", job=os.path.basename(clash[0]), mine=clash[1], theirs=clash[2]))
        # 6. forbidden terms in the brief (never print what matched)
        terms = _terms(profile.get("forbidden_terms_file"))
        hits = _scan_text(brief_text, terms)
        if hits:
            raise Refused(9, t("launch.err.forbidden", n=len(hits)))
        # 7. resume guard + node affinity
        prev, node_opt = None, a.node
        if a.resume:
            prev = find_session(cfg, a.resume)
            if prev and prev.get("node"):
                node_opt = prev["node"]
            elif node_opt == "auto":
                raise Refused(REFUSED_RC, t("launch.err.resume_unknown", session=a.resume))
            if prev and prev.get("job_dir") and not a.force_resume:
                age = session_age_min(prev["job_dir"])
                if age > pol("resume_max_min"):
                    raise Refused(REFUSED_RC, t("launch.err.resume_old", age=age, max=pol("resume_max_min"),
                                                job=jobs.job_id(prev["job_dir"])))
        # job dir
        for bump in range(120):       # ids are per second + pid: several launches in one process need distinct ones
            job_id = time.strftime("%Y%m%d-%H%M%S", time.localtime(time.time() + bump)) + f"-{os.getpid()}"
            self.job_dir = os.path.join(cfg.state_dir, f"{job_id}-{repo}")
            try:
                os.makedirs(self.job_dir)
                break
            except FileExistsError:
                self.job_dir = None
        else:
            raise Refused(5, t("launch.err.no_job_id"))
        with open(os.path.join(self.job_dir, "brief.md"), "w", encoding="utf-8") as f:
            f.write(brief_text)
        owner = a.owner or jobs.owner_label()
        base = _git(repo_dir, "rev-parse", "HEAD") or ""
        with open(os.path.join(self.job_dir, "waiter.pid"), "w") as f:
            f.write(str(os.getpid()))
        util.add_meta(self.job_dir, OWNER=owner, PROFILE=profile["name"], PROJECT=project or "", REPO=repo,
                      REPO_DIR=repo_dir, ZONE=",".join(zone), BASE=base, BRANCH=a.branch or "")
        # 8. node selection
        node_name = self._choose_node(profile, node_opt, a.requires, prev)
        # 9-11. identity, compute, version
        if node_name == "local":
            if not a.resume and jobs.live_counts(cfg, exclude=self.job_dir).get("local", 0) >= pol("max_local_jobs"):
                raise Refused(5, t("launch.err.local_full", n=pol("max_local_jobs")))
            p = nodes.local_probe(cfg, profile, a.requires)
            node = None
        else:
            node = cfg.node(node_name)
            if profile["name"] not in node["profiles"]:
                raise Refused(5, t("launch.err.node_profile", node=node_name, profile=profile["name"]))
            p = nodes.probe(cfg, node, profile, a.requires)
            mx = node.get("max_jobs")
            if not a.resume and mx and jobs.live_counts(cfg, exclude=self.job_dir).get(node_name, 0) >= mx:
                raise Refused(5, t("launch.err.node_full", node=node_name, n=mx))
        if not p["ok"]:
            raise Refused(4 if p["kind"] == "identity" else 5,
                          t("launch.err.node_not_ok", node=node_name, reason=p["reason"], fix=p["fix"]))
        if node:
            lv = nodes.claude_version(profile["launcher"], profile.get("config_dir"))
            if not a.allow_version_skew and lv and not nodes.same_minor(lv, p.get("claude_version")):
                raise Refused(5, t("launch.err.version", node=node_name, remote=p.get("claude_version"), local=lv))
        account = p.get("email") or ""
        caps = caps_env(cfg, profile, project, a)
        argv_claude = claude_args(mode, a.resume, a.model or profile.get("model"),
                                  None if node else a.worktree)
        util.add_meta(self.job_dir, ACCOUNT=account, CLAUDE_VERSION=p.get("claude_version") or "",
                      DELEGA_VERSION=_version(), RESUMED="1" if a.resume else "")
        if node is None:
            return self._run_local(profile, project, repo_dir, account, caps, argv_claude, repo)
        return self._run_remote(profile, project, node, repo_dir, repo, job_id, account, caps, argv_claude, prev)

    # -- node choice
    def _choose_node(self, profile, node_opt, requires, prev):
        cfg = self.cfg
        if node_opt == "local":
            name = "local"
        elif node_opt != "auto":
            name = node_opt
        else:
            def record(choice):
                util.add_meta(self.job_dir, NODE=choice)
            name = nodes.select(cfg, profile, requires, lambda: jobs.live_counts(cfg, exclude=self.job_dir),
                                on_choice=record)
            if name == "local":
                print(t("launch.local_fallback"))
        util.add_meta(self.job_dir, NODE=name)
        return name

    # -- local
    def _run_local(self, profile, project, repo_dir, account, caps, argv_claude, repo):
        cfg, a = self.cfg, self.args
        chan = self.job_dir
        env = {**os.environ, **caps}
        if profile.get("config_dir"):
            env["CLAUDE_CONFIG_DIR"] = _config.expand(profile["config_dir"])
        pkg = os.path.dirname(util.PKG_DIR)
        if not pkg.endswith(".pyz"):
            env["PYTHONPATH"] = pkg + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        util.add_meta(self.job_dir, CHANNEL=chan, WORKTREE=repo_dir)
        self._forbidden_file(profile, chan)
        job_id = jobs.job_id(self.job_dir)
        _audit(cfg, "launch", job=job_id, profile=profile["name"], account=account, node="local",
               branch=a.branch or "", base=util.read_meta(self.job_dir).get("BASE"), project=project,
               owner=util.read_meta(self.job_dir).get("OWNER"))
        print(t("launch.start", dir=self.job_dir, node="local", owner=util.read_meta(self.job_dir).get("OWNER"),
                id=job_id))
        argv = broker_argv() + ["run", chan, os.path.join(self.job_dir, "brief.md"), "--"] + profile["launcher"] + argv_claude
        self.started = True
        rc = subprocess.run(argv, cwd=repo_dir, env=env, stdin=subprocess.DEVNULL).returncode
        util.add_meta(self.job_dir, END=int(time.time()))
        summ = util.read_json(util.channel_file(chan, "summary.json"), {}) or util.read_json(
            util.channel_file(chan, "result.json"), {}) or {}
        if summ.get("session_id") and not a.resume:
            record_session(cfg, summ["session_id"], "local", "", a.branch or "", profile["name"], job_id)
        _audit(cfg, "end", job=job_id, profile=profile["name"], account=account, node="local",
               branch=a.branch or "", project=project, owner=util.read_meta(self.job_dir).get("OWNER"),
               cost_usd=summ.get("cost_usd", summ.get("total_cost_usd")), rc=rc)
        print_summary(self.job_dir, rc)
        return rc

    def _forbidden_file(self, profile, chan, node=None):
        path = profile.get("forbidden_terms_file")
        terms = _terms(path)
        if terms:
            with open(os.path.join(self.job_dir, "forbidden.txt"), "w", encoding="utf-8") as f:
                f.write("\n".join(terms) + "\n")

    # -- remote
    def _run_remote(self, profile, project, node, repo_dir, repo, job_id, account, caps, argv_claude, prev):
        cfg, a = self.cfg, self.args
        name = node["name"]
        if not _is_git(repo_dir):
            raise Refused(REFUSED_RC, t("launch.err.not_git", path=repo_dir))
        sha = remote.ensure_pyz(cfg, node)
        chan = remote.channel_rel(node, job_id)
        if prev and prev.get("worktree"):
            wt, branch = prev["worktree"], prev.get("branch") or a.branch or ""
        else:
            branch = a.branch or f"delega/{job_id}"
            if _git(repo_dir, "status", "--porcelain", "--untracked-files=no"):
                print(t("launch.warn.dirty", dir=repo_dir, sha=_git(repo_dir, "rev-parse", "--short", "HEAD")))
            remote.push_head(repo_dir, node, job_id, profile.get("git"))
            wt = remote.make_worktree(node, repo, job_id, branch)
        util.add_meta(self.job_dir, CHANNEL=chan, BRANCH=branch, WORKTREE=wt, PYZ_SHA=sha)
        remote.must(remote.ssh(node, f"mkdir -p {shlex.quote(chan)}"), "mkdir channel")
        remote.must(remote.scp_to(node, os.path.join(self.job_dir, "brief.md"), f"{chan}/brief.md"), "upload brief")
        self._forbidden_file(profile, chan)
        if os.path.exists(os.path.join(self.job_dir, "forbidden.txt")):
            remote.must(remote.scp_to(node, os.path.join(self.job_dir, "forbidden.txt"), f"{chan}/forbidden.txt"),
                        "upload forbidden.txt")
        run_sh = build_run_sh(node, profile, caps, chan, wt, argv_claude)
        local_sh = os.path.join(self.job_dir, "run.sh")
        with open(local_sh, "w") as f:
            f.write(run_sh)
        remote.must(remote.scp_to(node, local_sh, f"{chan}/run.sh"), "upload run.sh")
        meta = util.read_meta(self.job_dir)
        _audit(cfg, "launch", job=job_id, profile=profile["name"], account=account, node=name, branch=branch,
               base=meta.get("BASE"), project=project, owner=meta.get("OWNER"))
        # detached from this connection: if the ssh drops, the worker keeps going
        remote.must(remote.ssh(node, f"S=$(command -v setsid || true); nohup $S bash -l {_home(chan + '/run.sh')} > /dev/null 2>&1 < /dev/null &"),
                    "start worker")
        self.started = True
        print(t("launch.started_remote", node=name, wt=wt, branch=branch, id=job_id))
        return wait_and_finish(cfg, self.job_dir)


def _is_git(path):
    return _git(path, "rev-parse", "--git-dir") is not None


def _git(repo_dir, *args):
    return _config._git(repo_dir, *args)


def _version():
    from . import __version__
    return __version__

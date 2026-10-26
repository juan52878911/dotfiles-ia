"""spend: list-price cost of jobs per day, profile, project and orchestrator."""
import os
import sys
import time

from . import config, i18n, util


def job_cost(job_dir):
    """Cost of one job: summary.json → result.json → state.json (legacy names accepted).
    When the job dir holds no channel copy yet, a local channel dir (meta CHANNEL) is tried."""
    dirs = [job_dir]
    ch = util.read_meta(job_dir).get("CHANNEL", "")
    if ch.startswith("/") and ch != job_dir:
        dirs.append(ch)
    for d in dirs:
        for name, keys in (("summary.json", ("cost_usd", "coste_usd")), ("result.json", ("total_cost_usd",)),
                           ("state.json", ("cost_usd", "coste_usd"))):
            data = util.read_json(util.channel_file(d, name), {}) or {}
            for key in keys:                      # coste_usd: written by the legacy scripts
                if isinstance(data.get(key), (int, float)):
                    return float(data[key])
    return None


def jobs_of(cfg, day=None):
    """Job records of the state dir, optionally only those whose id starts with `day`."""
    out = []
    try:
        names = sorted(os.listdir(cfg.state_dir))
    except OSError:
        return out
    for name in names:
        d = os.path.join(cfg.state_dir, name)
        if not (os.path.isdir(d) and name[:1].isdigit() and (not day or name.startswith(day))):
            continue
        meta = util.read_meta(d)
        out.append({"dir": d, "id": name, "owner": meta.get("OWNER") or "?",
                    "profile": meta.get("PROFILE") or "", "project": meta.get("PROJECT") or "?",
                    "cost": job_cost(d)})
    return out


def _default_profile(cfg):
    return cfg.profile()["name"]


def _matches(cfg, j, profile, project):
    if profile and (j["profile"] or _default_profile(cfg)) != profile:  # legacy jobs: default profile
        return False
    return not project or j["project"] == project


def total(cfg, day, profile=None, project=None):
    """Sum of job costs for a day (YYYYMMDD; None or "all" = every job)."""
    day = None if day in (None, "all") else day
    return sum(j["cost"] or 0.0 for j in jobs_of(cfg, day) if _matches(cfg, j, profile, project))


def budget(cfg, profile, project=None):
    return float(cfg.policy("budget.day_usd", profile, project))


def main(argv):
    by, day, only_total = "owner", time.strftime("%Y%m%d"), False
    args = list(argv)
    while args:
        a = args.pop(0)
        if a == "--by" and args:
            by = args.pop(0)
        elif a == "--total":
            only_total = True
        elif a in ("-h", "--help"):
            print(i18n.t("spend.usage"))
            return 0
        else:
            day = a
    if by not in ("profile", "project", "owner"):
        print(i18n.t("spend.bad_by", by=by), file=sys.stderr)
        return 2
    try:
        cfg = config.load()
    except config.ConfigError as e:
        print(e, file=sys.stderr)
        return 2
    scope = None if day == "all" else day
    jobs = jobs_of(cfg, scope)
    if only_total:
        print(f"{sum(j['cost'] or 0.0 for j in jobs):.2f}")
        return 0
    groups = {}
    for j in jobs:
        key = j[by] or (_default_profile(cfg) if by == "profile" else "?")
        g = groups.setdefault(key, [0, 0.0])
        g[0] += 1
        g[1] += j["cost"] or 0.0
    for key, (n, c) in sorted(groups.items(), key=lambda kv: -kv[1][1]):
        print(i18n.t("spend.row", name=key, n=n, cost=f"{c:.2f}"))
    print(i18n.t("spend.total", day=day if scope else "all", n=len(jobs),
                 cost=f"{sum(j['cost'] or 0.0 for j in jobs):.2f}"))
    today = time.strftime("%Y%m%d")
    for name in cfg.profiles:
        spent = total(cfg, today, profile=name)
        print(i18n.t("spend.budget", profile=name, spent=f"{spent:.2f}", budget=f"{budget(cfg, name):.2f}"))
    return 0

"""quota: how many workers fit before the account's usage windows fill up.

The 5-hour and weekly windows are reported by Claude Code in `rate_limit_event`s inside each
job's events.jsonl. We take the freshest one, keep a history per profile in limits.tsv and
derive each worker's burn rate from it."""
import json
import os
import sys
import time

from . import config, i18n, util

DEFAULT_PACE_5H = 12.0     # %/h per worker when there is no history yet (conservative)
DEFAULT_PACE_WEEK = 1.6
HISTORY_COLS = 7           # ts, profile, util5, util7, reset5, reset7, live
TAIL_BYTES = 400_000


# ── reading limit events ────────────────────────────────────────────────────
def limits_from_channels(dirs):
    """Freshest rate_limit_event across channel dirs: [{"mtime", "info"}] (empty or one item).
    The event has no timestamp, so the events file's mtime stands in for it."""
    best = None
    for d in dirs:
        path = util.channel_file(d, "events.jsonl")
        try:
            mt = os.path.getmtime(path)
            if best and mt <= best["mtime"]:
                continue
            with open(path, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - TAIL_BYTES))
                tail = f.read().decode("utf-8", "ignore").splitlines()
        except OSError:
            continue
        for line in reversed(tail):
            if '"rate_limit_event"' in line:
                try:
                    best = {"mtime": mt, "info": json.loads(line)["rate_limit_info"]}
                except (ValueError, KeyError):
                    pass
                break
    return [best] if best else []


def _windows(info):
    w = info.get("unifiedWindows", {})
    five, week = w.get("five_hour", {}), w.get("seven_day", {})
    return (100 * five.get("utilization", 0), five.get("resetsAt", 0),
            100 * week.get("utilization", 0), week.get("resetsAt", 0))


# ── history (limits.tsv; legacy limite.tsv = default profile, 6 columns) ────
def history_path(cfg):
    return os.path.join(cfg.state_dir, "limits.tsv")


def read_history(cfg, profile, now=None):
    now = now or time.time()
    rows = []
    default = cfg.profile()["name"]
    for fname in ("limits.tsv", "limite.tsv"):
        try:
            lines = open(os.path.join(cfg.state_dir, fname), encoding="utf-8").read().splitlines()
        except OSError:
            continue
        for line in lines:
            p = line.split("\t")
            if len(p) == 6 and fname == "limite.tsv":
                p = [p[0], default] + p[1:]           # legacy: no profile column
            if len(p) != HISTORY_COLS or p[1] != profile:
                continue
            try:
                ts = float(p[0])
                if now - ts < 6 * 3600:
                    rows.append((ts, float(p[2]), float(p[3]), p[4], p[5], int(p[6])))
            except ValueError:
                continue
    return sorted(set(rows))


def append_history(cfg, profile, record, live):
    u5, r5, u7, r7 = _windows(record["info"])
    os.makedirs(cfg.state_dir, exist_ok=True)
    key = f"{int(record['mtime'])}\t{profile}\t"
    try:
        if open(history_path(cfg), encoding="utf-8").read().splitlines()[-1].startswith(key):
            return                                  # same event as last time: nothing new to learn
    except (OSError, IndexError):
        pass
    util.append(history_path(cfg), f"{int(record['mtime'])}\t{profile}\t{u5:.1f}\t{u7:.1f}\t{r5}\t{r7}\t{live}")


# ── the maths (pure; ported from the original `cupo`) ───────────────────────
def compute_capacity(record, rows, live, target5, target7, slots, hours=None, now=None):
    """How many workers may run at once so the 5 h window stays ≤ target5 % until it resets
    and the weekly one ≤ target7 %. `rows` = history tuples (ts, u5, u7, reset5, reset7, live)."""
    now = now or time.time()
    u5, r5, u7, r7 = _windows(record["info"])
    d5 = d7 = w5 = w7 = 0.0
    for x, y in zip(rows, rows[1:]):
        h = (y[0] - x[0]) / 3600
        if h <= 0 or h > 1:
            continue
        workers = max(1.0, (x[5] + y[5]) / 2)
        if x[3] == y[3] and y[1] >= x[1]:       # same 5 h window (a reset breaks the pair)
            d5 += y[1] - x[1]
            w5 += h * workers
        if x[4] == y[4] and y[2] >= x[2]:
            d7 += y[2] - x[2]
            w7 += h * workers
    pace5 = d5 / w5 if w5 >= 0.5 else None
    pace7 = d7 / w7 if w7 >= 0.5 else None
    estimated = not pace5
    pace5 = DEFAULT_PACE_5H if estimated else pace5
    pace7 = DEFAULT_PACE_WEEK if pace7 is None else pace7
    h5, h7 = max(0.1, (r5 - now) / 3600), max(0.1, (r7 - now) / 3600)
    if hours:                                    # shorter jobs only burn that part of the window
        h5, h7 = min(h5, hours), min(h7, hours)
    max5 = int((target5 - u5) / (pace5 * h5)) if u5 < target5 else 0
    max7 = int((target7 - u7) / (pace7 * h7)) if u7 < target7 and pace7 > 0 else (99 if u7 < target7 else 0)
    total = max(0, min(max5, max7, slots))
    return {"has_data": True, "u5": u5, "r5": r5, "h5": h5, "u7": u7, "r7": r7, "h7": h7,
            "age_s": now - record["mtime"], "pace5": pace5, "pace7": pace7, "estimated": estimated,
            "max5": max5, "max7": max7, "slots": slots, "total": total, "live": live,
            "new": max(0, total - live), "target5": target5, "target7": target7}


# ── gathering inputs ────────────────────────────────────────────────────────
def _profile_jobs(cfg, profile):
    """Job dirs of this profile (jobs without PROFILE = legacy = default profile)."""
    default = cfg.profile()["name"]
    out = []
    try:
        names = sorted(os.listdir(cfg.state_dir))
    except OSError:
        return out
    for n in names:
        d = os.path.join(cfg.state_dir, n)
        if os.path.isdir(d) and n[:1].isdigit() and (util.read_meta(d).get("PROFILE") or default) == profile:
            out.append(d)
    return out


def _fallback_alive(d):
    meta = util.read_meta(d)
    return not meta.get("END") and os.path.exists(os.path.join(d, "meta"))


def live_for_profile(cfg, profile):
    """Running jobs of this profile. Uses jobs.alive; falls back to counting open metas."""
    from . import jobs
    alive = getattr(jobs, "alive", None)

    def is_alive(d):
        return alive(cfg, d) if alive else _fallback_alive(d)

    return sum(1 for d in _profile_jobs(cfg, profile) if is_alive(d))


def live_counts(cfg):
    """{node: n} from jobs.live_counts, with a local fallback while that is missing."""
    from . import jobs
    fn = getattr(jobs, "live_counts", None)
    if fn:
        return fn(cfg)
    out = {}
    for d in _profile_jobs(cfg, cfg.profile()["name"]):
        if _fallback_alive(d):
            node = util.read_meta(d).get("NODE") or "local"
            out[node] = out.get(node, 0) + 1
    return out


def node_slots(cfg, profile, probe=True):
    """Workers the profile's nodes can hold (RAM-bound, ≤ max_jobs) plus max_local_jobs."""
    prof = cfg.profile(profile)
    reserve = int(cfg.policy("node.reserve_mb_per_job", prof))
    slots = int(cfg.policy("max_local_jobs", prof))
    for node in cfg.nodes_for(prof):
        cap = int(node.get("max_jobs", 1))
        if probe:
            try:
                from . import nodes
                res = nodes.probe(cfg, node, prof)
                if not res.get("ok"):
                    continue
                cap = min(cap, int(res.get("mem_mb") or 0) // reserve)
            except (AttributeError, NotImplementedError, OSError):
                pass            # no probe available: trust max_jobs
        slots += cap
    return slots


def remote_limits(cfg, profile):
    """Freshest limit event reported by each of the profile's nodes (via `broker limits`)."""
    out = []
    try:
        from . import remote
        for node in cfg.nodes_for(cfg.profile(profile)):
            wd = node["workdir"]
            cmd = f"cd ~/{wd} && {node['python']} delega.pyz broker limits {node.get('jobs_subdir', 'jobs')}/*/ 2>/dev/null"
            r = remote.ssh(node, cmd, timeout=30)
            for line in (r.stdout or "").splitlines():
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
    except (AttributeError, NotImplementedError, OSError):
        pass
    return out


def capacity(cfg, profile, hours=None, probe=True, records=None, record_history=True):
    """Capacity report for a profile (dict; has_data False when no limit event exists yet)."""
    name = cfg.profile(profile)["name"]
    prof = cfg.profile(name)
    if records is None:
        dirs = []
        for d in _profile_jobs(cfg, name):
            dirs.append(d)
            ch = util.read_meta(d).get("CHANNEL", "")
            if ch.startswith("/") and os.path.isdir(ch):
                dirs.append(ch)
        records = limits_from_channels(dirs) + (remote_limits(cfg, name) if probe else [])
    live = live_for_profile(cfg, name)
    slots = node_slots(cfg, name, probe)
    if not records:
        return {"has_data": False, "slots": slots, "live": live}
    rec = max(records, key=lambda r: r["mtime"])
    if record_history:
        append_history(cfg, name, rec, live)
    rows = read_history(cfg, name)
    return compute_capacity(rec, rows, live, float(cfg.policy("quota.window_5h_pct", prof)),
                            float(cfg.policy("quota.weekly_pct", prof)), slots,
                            hours or float(cfg.policy("expected_hours", prof)))


def render(profile, c):
    if not c["has_data"]:
        return i18n.t("quota.no_data", profile=profile)
    hm = lambda t: time.strftime("%a %H:%M", time.localtime(t))
    est = i18n.t("quota.estimated") if c["estimated"] else ""
    return "\n".join([
        i18n.t("quota.windows", profile=profile, u5=f"{c['u5']:.0f}", r5=hm(c["r5"]), h5=f"{c['h5']:.1f}",
               u7=f"{c['u7']:.0f}", r7=hm(c["r7"]), h7=f"{c['h7']:.0f}", age=int(c["age_s"] // 60)),
        i18n.t("quota.pace", p5=f"{c['pace5']:.1f}", p7=f"{c['pace7']:.2f}", est=est),
        i18n.t("quota.caps", t5=f"{c['target5']:.0f}", m5=c["max5"], t7=f"{c['target7']:.0f}", m7=c["max7"],
               slots=c["slots"]),
        i18n.t("quota.result", total=c["total"], live=c["live"], new=c["new"]),
    ])


def main(argv):
    profile, hours, probe, a = None, None, True, list(argv)
    while a:
        x = a.pop(0)
        if x == "--profile" and a:
            profile = a.pop(0)
        elif x == "--hours" and a:
            hours = float(a.pop(0))
        elif x == "--no-probe":
            probe = False
        else:
            print(i18n.t("quota.usage"), file=sys.stderr)
            return 0 if x in ("-h", "--help") else 2
    try:
        cfg = config.load()
        name = cfg.profile(profile)["name"]
        c = capacity(cfg, name, hours, probe)
    except config.ConfigError as e:
        print(e, file=sys.stderr)
        return 2
    print(render(name, c))
    from . import spend
    prof = cfg.profile(name)
    print(i18n.t("quota.spend_today", spent=f"{spend.total(cfg, time.strftime('%Y%m%d'), profile=name):.2f}",
                 budget=f"{float(cfg.policy('budget.day_usd', prof)):.2f}"))
    return 0

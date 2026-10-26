"""jobs: list, talk to, watch and re-attach to jobs. A job is a directory in the state dir
(`<id>-<repo>/`) with a `meta` file; its channel lives there (local) or on the node (remote)."""
import argparse
import glob
import json
import os
import shlex
import sys
import time

from . import config as _config
from . import remote, util
from .i18n import t

LAUNCH_GRACE_H = 12          # a job with no liveness signal counts as live this long
RECENT_END_S = 2 * 3600      # finished jobs still shown by ps/watch for this long


# ── locating jobs ───────────────────────────────────────────────────────────
def job_dirs(cfg):
    pat = os.path.join(glob.escape(cfg.state_dir), "2*")
    return sorted(d for d in glob.glob(pat) if os.path.isfile(os.path.join(d, "meta")))


def job_id(job_dir):
    return "-".join(os.path.basename(job_dir).split("-")[:3])


def resolve(cfg, pattern):
    """Job dir whose name contains `pattern` (`last` = newest; Spanish `ultimo` accepted)."""
    dirs = job_dirs(cfg)
    if pattern in ("last", "ultimo"):
        hits = dirs
    else:
        hits = [d for d in dirs if pattern in os.path.basename(d)]
    if not hits:
        raise LookupError(t("jobs.not_found", job=pattern, dir=cfg.state_dir))
    return hits[-1]


def owner_label():
    return os.environ.get("DELEGA_OWNER") or os.environ.get("ORQ_DUENO") or os.path.basename(os.getcwd())


def _is_remote(meta):
    return meta.get("NODE", "local") != "local"


def _pid_alive(pid):
    try:
        pid = int(pid)
        if pid <= 0:
            return False
        os.kill(pid, 0)
        return True
    except (OSError, ValueError, TypeError):
        return False


def slurp(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _read_pid(path):
    try:
        return int(slurp(path).strip())
    except (OSError, ValueError):
        return 0


def _young(path, hours):
    try:
        return time.time() - os.path.getmtime(path) < hours * 3600
    except OSError:
        return False


def alive(cfg, job_dir):
    """Still counts as running: no END and (local) a fresh lease or live broker pid, or (remote) a live
    waiter, else recent meta."""
    meta = util.read_meta(job_dir)
    if meta.get("END"):
        return False
    young = _young(os.path.join(job_dir, "meta"), LAUNCH_GRACE_H)
    if _is_remote(meta):
        return _pid_alive(_read_pid(os.path.join(job_dir, "waiter.pid"))) or young
    chan = meta.get("CHANNEL") or job_dir
    if os.path.exists(util.channel_file(chan, "rc")):
        return False
    lease = util.read_json(os.path.join(chan, "lease.json"))
    if lease:
        if lease.get("final"):      # the broker closed the lease at the end of the job
            return False
        stale = cfg.policy("lease.stale_s")
        try:
            if time.time() - float(lease.get("renewed", 0)) < stale:
                return True
        except (TypeError, ValueError):
            pass
        return _pid_alive(lease.get("pid"))
    st = util.read_json(util.channel_file(chan, "state.json"))
    if st:
        return _pid_alive(st.get("pid"))
    return young          # still launching: nothing written yet


def live_counts(cfg, exclude=None):
    """{node: live jobs}. `exclude` = job dir being launched (does not count against itself)."""
    out = {}
    for d in job_dirs(cfg):
        if exclude and os.path.abspath(d) == os.path.abspath(exclude):
            continue
        meta = util.read_meta(d)
        if meta.get("NODE") and alive(cfg, d):
            out[meta["NODE"]] = out.get(meta["NODE"], 0) + 1
    return out


def close_job(job_dir, reason=None, cfg=None, rc=None):
    """Mark the job ended and make sure it has a summary.json. With cfg, also audit `end` with its cost
    (only for jobs that launched: a launch that failed was already audited as refused)."""
    meta = util.read_meta(job_dir)
    if meta.get("END"):
        return
    util.add_meta(job_dir, END=int(time.time()), **({"END_REASON": reason} if reason else {}))
    summ = ensure_summary(job_dir, rc, reason)
    if cfg is not None and meta.get("ACCOUNT") is not None and reason != "launch_failed":
        from . import audit
        audit.record(cfg, "end", job=job_id(job_dir), profile=meta.get("PROFILE") or None,
                     node=meta.get("NODE"), branch=meta.get("BRANCH") or None, project=meta.get("PROJECT") or None,
                     owner=meta.get("OWNER"), cost_usd=summ.get("cost_usd"), rc=summ.get("rc"), reason=reason)


def ensure_summary(job_dir, rc=None, reason=None):
    """summary.json of a job, written from state.json / result.json when the broker could not write it
    (cap cut by a kill, start failure, broker lost, adopt): spend, audit and chrono need one per job.
    The synthetic one carries `synthetic: <reason>`. Returns the summary."""
    chan = util.read_meta(job_dir).get("CHANNEL") or job_dir
    dirs = [job_dir] + ([chan] if chan.startswith("/") and chan != job_dir else [])
    for d in dirs:
        summ = util.read_json(util.channel_file(d, "summary.json"))
        if summ:
            if d != job_dir:
                util.write_json(os.path.join(job_dir, "summary.json"), summ)
            return summ
    st, res = {}, {}
    for d in dirs:
        st = st or util.read_json(util.channel_file(d, "state.json"), {}) or {}
        res = res or util.read_json(util.channel_file(d, "result.json"), {}) or {}
    st = {_LEGACY_STATE.get(k, k): v for k, v in st.items()}
    if rc is None:
        try:
            rc = int(slurp(util.channel_file(job_dir, "rc")).strip())
        except (OSError, ValueError):
            rc = st.get("rc")
    start = st.get("start")
    cost = st.get("cost_usd") if st.get("cost_usd") is not None else res.get("total_cost_usd")
    summ = {"session_id": st.get("session_id") or res.get("session_id"), "rc": rc, "model": st.get("model"),
            "duration_min": round((time.time() - start) / 60, 1) if isinstance(start, (int, float)) else None,
            "messages": st.get("turns", res.get("num_turns")), "tools": st.get("tools"), "alerts": st.get("alerts"),
            "cost_usd": cost, "cap": st.get("cap"),
            "start": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(start)) if isinstance(start, (int, float))
            else None, "end": util.now(), "synthetic": reason or "no_summary"}
    util.write_json(os.path.join(job_dir, "summary.json"), summ)
    return summ


_LEGACY_STATE = {"coste_usd": "cost_usd", "turnos": "turns", "herramientas": "tools", "alertas": "alerts",
                 "modelo": "model", "inicio": "start", "tope": "cap"}


def reconcile(cfg):
    """Close local jobs whose broker died and remote jobs past the launch grace; report remote jobs whose
    waiter is gone but may still be running (adopt them). Returns [(job_dir, action)]."""
    out = []
    for d in job_dirs(cfg):
        meta = util.read_meta(d)
        if meta.get("END") or not meta.get("NODE"):
            continue
        if alive(cfg, d):
            if _is_remote(meta) and not _pid_alive(_read_pid(os.path.join(d, "waiter.pid"))):
                out.append((d, "orphan"))
            continue
        close_job(d, "dead", cfg)
        out.append((d, "closed"))
    return out


# ── channel access (local file or ssh) ──────────────────────────────────────
def _node(cfg, meta):
    return remote.job_node(cfg, meta)


def read_channel(cfg, job_dir, name):
    """Text of a channel file for a job, or None. Remote jobs: the local copy once finished, else ssh cat."""
    meta = util.read_meta(job_dir)
    if not _is_remote(meta):
        try:
            return slurp(util.channel_file(meta.get("CHANNEL") or job_dir, name))
        except OSError:
            return None
    if meta.get("END"):
        try:
            return slurp(util.channel_file(job_dir, name))
        except OSError:
            pass
    chan = meta.get("CHANNEL", "")
    r = remote.ssh(_node(cfg, meta), f"cat {shlex.quote(chan)}/{shlex.quote(name)}", timeout=30)
    if r.returncode != 0:
        r = remote.ssh(_node(cfg, meta), f"cat {shlex.quote(chan)}/{shlex.quote(util.LEGACY_FILES.get(name, name))}",
                       timeout=30)
    return r.stdout if r.returncode == 0 else None


def _scope(cfg, args_all):
    out = []
    for d in job_dirs(cfg):
        meta = util.read_meta(d)
        if not args_all and meta.get("OWNER") != owner_label():
            continue
        if not meta.get("END") or _young(os.path.join(d, "meta"), RECENT_END_S / 3600):
            out.append(d)
    return out


# ── ps ──────────────────────────────────────────────────────────────────────
def describe(cfg, job_dir):
    """Lines describing one job: header, state line, last notices/alerts."""
    meta = util.read_meta(job_dir)
    head = "== " + t("jobs.ps.header", job=os.path.basename(job_dir), node=meta.get("NODE", "?"),
                     branch=(" · " + meta["BRANCH"]) if meta.get("BRANCH") else "", owner=meta.get("OWNER", "?"))
    if meta.get("END"):
        head += " · " + t("jobs.ps.closed")
    lines = [head]
    try:
        st = json.loads(read_channel(cfg, job_dir, "state.json") or "")
    except ValueError:
        st = None
    if not st:
        lines.append("  " + t("jobs.ps.no_state"))
        return lines
    state = st.get("state") or st.get("estado") or "?"
    if not meta.get("END") and state not in ("done", "finished", "terminado") and not _is_remote(meta) \
            and not alive(cfg, job_dir):
        state = t("jobs.ps.dead", session=st.get("session_id"))
    now = time.time()
    start, last = st.get("start") or st.get("inicio") or now, st.get("last_event") or st.get("ultimo_evento") or now
    ended = state in ("done", "finished", "terminado") or meta.get("END")
    dur = int(((last if ended else now) - start) // 60)
    quiet = int(now - last)
    cost = st.get("cost_usd", st.get("coste_usd"))
    line = (f"{state}{'' if st.get('rc') is None else ' rc=' + str(st['rc'])} · {dur} min · "
            + t("jobs.ps.quiet", m=quiet // 60, s=f"{quiet % 60:02d}") + f" · {t('jobs.ps.tools')} "
            f"{st.get('tools', st.get('herramientas', 0))} · {t('jobs.ps.alerts')} {st.get('alerts', st.get('alertas', 0))}"
            + ("" if cost is None else f" · {cost:.3f} $"))
    if st.get("cap") or st.get("tope"):
        line += "  ⛔ " + str(st.get("cap") or st.get("tope"))
    lines.append(line)
    action = st.get("last_action") or st.get("ultima_accion")
    if action:
        lines.append("  " + t("jobs.ps.last_action", action=action))
    log = read_channel(cfg, job_dir, "progress.log") or ""
    keep = [l for l in log.splitlines() if len(l.split("\t")) == 3 and
            l.split("\t")[1].startswith(("NOTICE", "ALERT", "MESSAGE", "AVISO", "ALERTA", "MENSAJE"))][-3:]
    for l in keep:
        ts, typ, text = l.split("\t", 2)
        lines.append(f"  {ts[11:]} {typ} {util.short(text, 90)}")
    return lines


def main_ps(argv):
    ap = argparse.ArgumentParser(prog="delega ps", description="What each of your workers is doing.")
    ap.add_argument("job", nargs="?", help="part of the job id, or `last`")
    ap.add_argument("--all", action="store_true", help="every orchestrator's jobs, not only yours")
    args = ap.parse_args(argv)
    cfg = _config.load()
    if args.job:
        try:
            dirs = [resolve(cfg, args.job)]
        except LookupError as e:
            print(f"delega: {e}", file=sys.stderr)
            return 7
    else:
        dirs = _scope(cfg, args.all)
        if not dirs:
            print(t("jobs.ps.none", owner=owner_label()))
            return 0
    for d in dirs:
        print("\n".join(describe(cfg, d)))
    return 0


# ── say ─────────────────────────────────────────────────────────────────────
def say(cfg, job_dir, text):
    """Append a message to the job's inbox. Returns 0, or 6 if the job already finished."""
    meta = util.read_meta(job_dir)
    line = json.dumps({"ts": util.now(), "text": text}, ensure_ascii=False) + "\n"
    if meta.get("END"):
        return 6
    if not _is_remote(meta):
        chan = meta.get("CHANNEL") or job_dir
        if os.path.exists(util.channel_file(chan, "rc")):
            return 6
        util.append(os.path.join(chan, "inbox.jsonl"), line.rstrip("\n"))
        return 0
    chan = shlex.quote(meta["CHANNEL"])
    r = remote.ssh(_node(cfg, meta), f"test ! -f {chan}/rc && cat >> {chan}/inbox.jsonl", input=line, timeout=30)
    return 0 if r.returncode == 0 else 6


def main_say(argv):
    ap = argparse.ArgumentParser(prog="delega say", description="Send a message to a running worker.")
    ap.add_argument("job")
    ap.add_argument("text", nargs="+")
    args = ap.parse_args(argv)
    cfg = _config.load()
    try:
        d = resolve(cfg, args.job)
    except LookupError as e:
        print(f"delega: {e}", file=sys.stderr)
        return 7
    rc = say(cfg, d, " ".join(args.text))
    if rc == 6:
        sid = (util.read_json(os.path.join(d, "summary.json"), {}) or {}).get("session_id", "")
        print(t("jobs.say.finished", session=sid), file=sys.stderr)
    else:
        print(t("jobs.say.ok"))
    return rc


# ── watch ───────────────────────────────────────────────────────────────────
WATCH_TYPES = ("NOTICE", "ALERT", "RESULT", "END", "AVISO", "ALERTA", "RESULTADO", "FIN")
END_TYPES = ("END", "FIN")


class _Watcher:
    def __init__(self, cfg, job_dir, label, from_start, out):
        self.cfg, self.dir, self.label, self.out = cfg, job_dir, label, out
        self.since = "" if from_start else time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - 60))
        self.seen = set()
        self.done_ = False
        self.consumed = 0
        self.pulled = 0.0

    def _pull_nod(self):
        """A remote job without its waiter: forward its Nod envelopes from here (the waiter does it otherwise)."""
        meta = util.read_meta(self.dir)
        if meta.get("END") or not _is_remote(meta) or _pid_alive(_read_pid(os.path.join(self.dir, "waiter.pid"))):
            return
        if time.time() - self.pulled >= float(os.environ.get("DELEGA_NOD_PULL_S", 10)):
            self.pulled = time.time()
            from . import nod
            nod.pull_and_forward(self.cfg, self.dir, meta)

    def emit(self, typ, text):
        self.out(f"[{self.label}] {typ} {util.short(text, 200)}")

    def poll(self):
        """Process new progress.log lines. Returns True when the job is over."""
        self._pull_nod()
        text = read_channel(self.cfg, self.dir, "progress.log") or ""
        lines = text.splitlines()
        for l in lines[self.consumed:]:
            parts = l.split("\t", 2)
            if len(parts) != 3 or parts[0] < self.since or not parts[1].startswith(WATCH_TYPES):
                continue
            key = (parts[1], util.short(parts[2], 120))
            if key in self.seen and parts[1] not in END_TYPES:
                continue    # every line wakes the orchestrator: the same alert twice adds nothing
            self.seen.add(key)
            self.emit(parts[1], parts[2])
            if parts[1] in END_TYPES:
                self.done_ = True
        self.consumed = len(lines)
        if self.done_:
            return True
        meta = util.read_meta(self.dir)
        if meta.get("END"):
            self.emit("END", t("jobs.watch.closed"))
            return True
        if not _is_remote(meta) and not alive(self.cfg, self.dir):
            st = util.read_json(util.channel_file(meta.get("CHANNEL") or self.dir, "state.json"), {}) or {}
            self.emit("ALERT:dead", t("jobs.watch.dead", session=st.get("session_id")))
            return True
        return False


def watch(cfg, dirs, from_start=False, out=None, poll_s=None):
    out = out or (lambda s: print(s, flush=True))
    poll_s = float(os.environ.get("DELEGA_WATCH_POLL_S", 2)) if poll_s is None else poll_s
    ws = [_Watcher(cfg, d, job_id(d), from_start, out) for d in dirs]
    while ws:
        ws = [w for w in ws if not w.poll()]
        if ws:
            time.sleep(poll_s)
    return 0


def main_watch(argv):
    ap = argparse.ArgumentParser(prog="delega watch", description="One line per notice/alert/result/end.")
    ap.add_argument("job", nargs="*")
    ap.add_argument("--all", action="store_true", help="every orchestrator's jobs, not only yours")
    ap.add_argument("--from-start", action="store_true", help="include events older than the last minute")
    ap.add_argument("--bus", action="store_true", help="one line per delega event of the Nod bus (events.jsonl) "
                    "for your jobs, all of them, until interrupted: a single Monitor instead of one per job")
    args = ap.parse_args(argv)
    cfg = _config.load()
    if args.bus:
        return watch_bus(cfg, args.all, args.from_start)
    if args.job:
        try:
            dirs = [resolve(cfg, j) for j in args.job]
        except LookupError as e:
            print(f"delega: {e}", file=sys.stderr)
            return 7
    else:
        dirs = [d for d in _scope(cfg, args.all) if not util.read_meta(d).get("END")]
        if not dirs:
            print(t("jobs.watch.none", owner=owner_label()))
            return 0
    return watch(cfg, dirs, args.from_start)


def _bus_remote_jobs(cfg, everything):
    """Live remote jobs in scope whose waiter is gone: watch --bus pulls their envelopes itself."""
    out = []
    for d in _scope(cfg, everything):
        meta = util.read_meta(d)
        if not meta.get("END") and _is_remote(meta) and not _pid_alive(_read_pid(os.path.join(d, "waiter.pid"))):
            out.append(d)
    return out


def watch_bus(cfg, everything=False, from_start=False, out=None, poll_s=None, stop=None):
    from . import nod
    owner = None if everything else owner_label()
    return nod.watch_bus(cfg, owner, from_start, out=out, poll_s=poll_s, stop=stop,
                         remote_jobs=lambda: _bus_remote_jobs(cfg, everything))


# ── adopt ───────────────────────────────────────────────────────────────────
def adopt(cfg, job_dir, poll_s=None):
    """Re-attach to a remote job whose waiter died: wait for rc, copy back, fetch the branch, END."""
    from . import launch
    meta = util.read_meta(job_dir)
    if meta.get("END"):
        print(t("jobs.adopt.already"))
        return 0
    if not _is_remote(meta):
        print(t("jobs.adopt.local"), file=sys.stderr)
        return 2
    old = _read_pid(os.path.join(job_dir, "waiter.pid"))
    if old and old != os.getpid() and _pid_alive(old):
        print(t("jobs.adopt.waiting", pid=old), file=sys.stderr)
        return 2
    with open(os.path.join(job_dir, "waiter.pid"), "w") as f:
        f.write(str(os.getpid()))
    print(t("jobs.adopt.start", job=os.path.basename(job_dir), node=meta["NODE"]))
    rc = launch.wait_and_finish(cfg, job_dir, poll_s=poll_s)
    return rc


def main_adopt(argv):
    ap = argparse.ArgumentParser(prog="delega adopt", description="Re-attach to a remote job after the "
                                 "orchestrator that launched it died.")
    ap.add_argument("job")
    args = ap.parse_args(argv)
    cfg = _config.load()
    try:
        d = resolve(cfg, args.job)
    except LookupError as e:
        print(f"delega: {e}", file=sys.stderr)
        return 7
    return adopt(cfg, d)


def main(argv):
    print("usage: delega ps|say|watch|adopt …", file=sys.stderr)
    return 2

"""nod: delega's events as Nod v1 envelopes (~/Documents/GitHub/nod/docs/nod-v1.md), one envelope per line.

Two emitters, never the same event twice:
  channel  the broker (here or on a node) appends job.notice | job.alert | job.result | decision.request to
           `<channel>/nod.jsonl`. On this machine it forwards them at once; on a node nobody can see the
           Mac's collector, so the Mac pulls the file (waiter, watch) and forwards it.
  mac      audit.record: job.launch (event launch), job.end (event end) and job.alert kind=refused.

Forwarding = append to the Nod events file (NOD_EVENTS_FILE, default ~/.local/state/nod/events.jsonl) and
POST <NOD_COLLECTOR_URL>/v1/events (default http://127.0.0.1:8787; empty = no POST) with a short timeout. A
collector that does not answer never blocks a job: the POST is skipped for DOWN_S after a failure and the
file keeps everything (`nod-collector import` loads it). `<dir>/nod.sent` holds how many lines of
`<dir>/nod.jsonl` were forwarded, so a re-copy, a second watcher or adopt never send a line twice.
DELEGA_NOD=off disables both emitters.

Every string in `data` goes through the forbidden-terms redaction before it is written."""
import contextlib
import fcntl
import hashlib
import json
import os
import re
import shlex
import socket
import sys
import time
import urllib.request

from . import util

HOST = "delega"                       # source.host: what chrono shows as attrs.host
INSTANCE = "delega"
TYPES = ("job.launch", "job.notice", "job.alert", "job.result", "job.end", "decision.request")
TEXT_MAX = 2000
POST_TIMEOUT_S = 0.5
DOWN_S = 60
PULL_S = 10                           # watch pulls a node's nod.jsonl at most this often
DEFAULT_URL = "http://127.0.0.1:8787"


def enabled():
    return os.environ.get("DELEGA_NOD", "on").lower() not in ("off", "0", "no", "false")


def events_file():
    return os.path.expanduser(os.environ.get("NOD_EVENTS_FILE") or "~/.local/state/nod/events.jsonl")


def collector_url():
    return os.environ.get("NOD_COLLECTOR_URL", DEFAULT_URL).rstrip("/")


def run_id(key):
    """source.run (8 hex) stable for a key: re-sending an event keeps its Nod identity (instance, run, seq)."""
    return hashlib.sha1(key.encode()).hexdigest()[:8]


def _version():
    from . import __version__
    return __version__


# ── redaction ───────────────────────────────────────────────────────────────
def redact(obj, terms):
    """Every string inside obj with the forbidden terms replaced (case-insensitive)."""
    if not terms:
        return obj
    from .broker import redact as red
    if isinstance(obj, str):
        return red(obj, terms)
    if isinstance(obj, dict):
        return {k: redact(v, terms) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(v, terms) for v in obj]
    return obj


def terms_of(path):
    """Terms of a forbidden.txt / forbidden_terms_file (one per line, # comments)."""
    try:
        with open(os.path.expanduser(path), encoding="utf-8") as f:
            return [l.strip() for l in f if l.strip() and not l.lstrip().startswith("#")]
    except (OSError, TypeError):
        return []


# ── envelopes ───────────────────────────────────────────────────────────────
def envelope(type_, data, *, job, profile, run, seq, ts=None, machine=None):
    return {"nod": 1,
            "source": {"instance": INSTANCE, "run": run, "host": HOST, "host_version": _version(),
                       "machine": machine or socket.gethostname().split(".")[0], "profile": profile or "default",
                       "privacy": "completo"},
            "events": [{"seq": seq, "ts": int(ts if ts is not None else time.time() * 1000), "type": type_,
                        "session": job or "unknown", "task": job or "unknown", "data": data}]}


@contextlib.contextmanager
def _flock(path):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _append_numbered(path, make):
    """Append make(seq) as one line under a lock; seq = lines already in the file + 1."""
    with _flock(path + ".lock"):
        try:
            with open(path, "rb") as f:
                seq = sum(1 for _ in f) + 1
        except OSError:
            seq = 1
        env = make(seq)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(env, ensure_ascii=False) + "\n")
    return env


# ── sinks ───────────────────────────────────────────────────────────────────
def _post(lines, mark_dir):
    url = collector_url()
    if not url or not lines:
        return False
    down = os.path.join(mark_dir, "nod.down")
    try:
        if time.time() - os.path.getmtime(down) < DOWN_S:
            return False
    except OSError:
        pass
    ok = True
    for line in lines:
        req = urllib.request.Request(url + "/v1/events", data=line.encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        token = os.environ.get("NOD_COLLECTOR_TOKEN")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=POST_TIMEOUT_S) as r:
                r.read()
        except Exception:      # noqa: BLE001 — any failure: the file keeps the event
            ok = False
            break
    if not ok:
        with contextlib.suppress(OSError):
            open(down, "w").close()
    return ok


def deliver(lines, mark_dir):
    """Write envelope lines to the Nod events file and POST them. Never raises."""
    if not lines:
        return
    path = events_file()
    try:
        with _flock(path + ".delega.lock"):
            with open(path, "a", encoding="utf-8") as f:
                for l in lines:
                    f.write(l.rstrip("\n") + "\n")
    except OSError as e:
        print(f"delega: nod: {e}", file=sys.stderr)
    _post(lines, mark_dir)


def forward(directory, meta=None, terms=None):
    """Forward the lines of <directory>/nod.jsonl not yet sent. `meta` (job meta) fills owner, node and
    branch that a node's broker could not know; `terms` redact again on this side. Returns lines sent."""
    if not enabled():
        return 0
    src, cursor = os.path.join(directory, "nod.jsonl"), os.path.join(directory, "nod.sent")
    try:
        with _flock(cursor + ".lock"):
            try:
                with open(cursor) as f:
                    sent = int(f.read().strip() or 0)
            except (OSError, ValueError):
                sent = 0
            try:
                with open(src, encoding="utf-8") as f:
                    lines = f.read().split("\n")[:-1]       # only complete lines
            except OSError:
                return 0
            new = []
            for l in lines[sent:]:
                try:
                    env = json.loads(l)
                except ValueError:
                    continue
                for ev in env.get("events", []):
                    if meta:
                        _fill(ev.setdefault("data", {}), meta)
                    if terms:
                        ev["data"] = redact(ev.get("data", {}), terms)
                new.append(json.dumps(env, ensure_ascii=False))
            deliver(new, directory)
            with open(cursor, "w") as f:
                f.write(str(len(lines)))
            return len(new)
    except OSError:
        return 0


def _fill(data, meta):
    for key, mk in (("owner", "OWNER"), ("node", "NODE"), ("branch", "BRANCH"), ("profile", "PROFILE"),
                    ("project", "PROJECT")):
        if not data.get(key) and meta.get(mk):
            data[key] = meta[mk]


# ── channel side (broker) ───────────────────────────────────────────────────
def _channel_job(channel):
    return os.environ.get("DELEGA_JOB_ID") or "-".join(os.path.basename(channel.rstrip("/")).split("-")[:3])


def _is_local():
    return os.environ.get("DELEGA_NODE", "local") in ("", "local")


def job_fields(channel):
    """job, owner, profile, node and branch as the broker sees them (env from run.sh; meta when local)."""
    meta = util.read_meta(channel) if os.path.isfile(os.path.join(channel, "meta")) else {}
    return {"job": _channel_job(channel),
            "owner": meta.get("OWNER") or None,     # on a node: filled from the job's meta on the Mac
            "profile": os.environ.get("DELEGA_PROFILE") or meta.get("PROFILE") or None,
            "node": os.environ.get("DELEGA_NODE") or meta.get("NODE") or "local",
            "branch": os.environ.get("DELEGA_BRANCH") or meta.get("BRANCH") or None}


def channel_emit(channel, type_, data, terms=()):
    """Broker side: append one envelope to <channel>/nod.jsonl; on this machine also forward it."""
    if not enabled():
        return None
    try:
        base = job_fields(channel)
        data = redact({**{k: v for k, v in base.items() if v}, **data}, list(terms))
        if isinstance(data.get("text"), str):
            data["text"] = util.short(data["text"], TEXT_MAX)
        job = base["job"]
        env = _append_numbered(os.path.join(channel, "nod.jsonl"),
                               lambda seq: envelope(type_, data, job=job, profile=base["profile"], run=run_id(job),
                                                    seq=seq, machine=base["node"] if not _is_local() else None))
        if _is_local():
            forward(channel)
        return env
    except OSError as e:
        print(f"delega: nod: {e}", file=sys.stderr)
        return None


PROGRESS_TYPES = {"NOTICE": "job.notice", "ALERT": "job.alert", "RESULT": "job.result"}


def from_progress(channel, tipo, text, extra=None, terms=()):
    """A progress.log line as a Nod event (NOTICE:*, ALERT:*, RESULT); other types emit nothing."""
    head, _, kind = tipo.partition(":")
    type_ = PROGRESS_TYPES.get(head)
    if not type_:
        return None
    data = {"text": text, **(extra or {})}
    if kind:
        data["kind"] = kind
    return channel_emit(channel, type_, data, terms)


def decision_request(channel, kind, text, answer_wait_s, terms=(), recommendation=None):
    """A worker's question or blocker as a decision for the human (Orbit reads it from the collector)."""
    job = _channel_job(channel)
    now_ms = int(time.time() * 1000)
    data = {"kind": kind, "text": text, "deadline": now_ms + int(float(answer_wait_s) * 1000),
            "reply_cmd": f'delega say {job} "<respuesta>"'}
    rec = recommendation or recommendation_in(text)
    if rec:
        data["recommendation"] = rec
    return channel_emit(channel, "decision.request", data, terms)


def recommendation_in(text):
    """The recommendation a notice carries: the text after «Recomendación:», «Recomiendo:» or «Recommendation:»."""
    m = re.search(r"(?i)\b(?:recomendaci[oó]n|recomiendo|recommendation|i recommend|recommend)\s*:\s*(.+)$",
                  text or "", re.S)
    return m.group(1).strip() if m else None


# ── node side, seen from the Mac ────────────────────────────────────────────
def pull(node, chan, job_dir):
    """Append to <job_dir>/nod.jsonl what the node's <chan>/nod.jsonl has beyond it (complete lines only).
    Returns bytes added. Never raises."""
    from . import remote
    local = os.path.join(job_dir, "nod.jsonl")
    try:
        with _flock(os.path.join(job_dir, "nod.pull.lock")):
            size = os.path.getsize(local) if os.path.exists(local) else 0
            r = remote.ssh(node, f"tail -c +{size + 1} {shlex.quote(chan)}/nod.jsonl 2>/dev/null", timeout=30)
            if r.returncode != 0 or not r.stdout:
                return 0
            chunk = r.stdout[:r.stdout.rfind("\n") + 1]
            if chunk:
                with open(local, "a", encoding="utf-8") as f:
                    f.write(chunk)
            return len(chunk)
    except Exception:      # noqa: BLE001 — a flaky node must not break the waiter or watch
        return 0


def job_terms(job_dir):
    return terms_of(os.path.join(job_dir, "forbidden.txt"))


def pull_and_forward(cfg, job_dir, meta=None):
    """Remote job: pull the node's envelopes and forward them with the job's meta and terms."""
    if not enabled():
        return 0
    meta = meta or util.read_meta(job_dir)
    if meta.get("CHANNEL") and not meta["CHANNEL"].startswith("/"):
        from . import remote
        pull(remote.job_node(cfg, meta), meta["CHANNEL"], job_dir)
    return forward(job_dir, meta, job_terms(job_dir))


# ── mac side (audit) ────────────────────────────────────────────────────────
AUDIT_TYPES = {"launch": "job.launch", "end": "job.end", "refused": "job.alert"}
AUDIT_FIELDS = ("job", "profile", "node", "branch", "base", "project", "owner", "cost_usd", "rc", "reason",
                "vm", "golden", "duration_min")


def from_audit(cfg, rec):
    """audit.record → job.launch | job.end | job.alert(kind=refused). The state dir keeps the sequence."""
    type_ = AUDIT_TYPES.get(rec.get("event"))
    if not type_ or not enabled():
        return None
    data = {k: rec[k] for k in AUDIT_FIELDS if rec.get(k) is not None}
    if rec["event"] == "refused":
        data["kind"] = "refused"
        data["text"] = rec.get("reason") or ""
    terms = []
    try:
        terms = terms_of(cfg.profile(rec.get("profile") or None).get("forbidden_terms_file"))
    except Exception:      # noqa: BLE001 — unknown profile in a refusal: no profile terms
        pass
    data = redact(data, terms)
    path = os.path.join(cfg.state_dir, "nod.jsonl")
    try:
        env = _append_numbered(path, lambda seq: envelope(type_, data, job=rec.get("job"),
                                                          profile=rec.get("profile"),
                                                          run=run_id("mac:" + cfg.state_dir), seq=seq))
    except OSError:
        return None
    forward(cfg.state_dir)
    return env


# ── watch --bus ─────────────────────────────────────────────────────────────
def line_of(env):
    """One line per event: HH:MM:SS [job] type[:kind] text."""
    out = []
    for ev in env.get("events", []):
        d = ev.get("data") or {}
        kind = f":{d['kind']}" if d.get("kind") else ""
        when = time.strftime("%H:%M:%S", time.localtime(ev.get("ts", 0) / 1000))
        text = d.get("text") or ""
        if ev.get("type") == "decision.request":
            text += f" · → {d.get('reply_cmd')}"
        elif ev.get("type") in ("job.end", "job.launch"):
            text = " ".join(f"{k}={d[k]}" for k in ("node", "branch", "rc", "cost_usd") if d.get(k) is not None)
        out.append(f"{when} [{d.get('job') or ev.get('task')}] {ev.get('type')}{kind} {util.short(text, 300)}".rstrip())
    return out


def bus_lines(path, offset, owner=None):
    """(lines, new offset) of delega events appended to the Nod file after offset, for owner (None = all)."""
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            raw = f.read()
    except OSError:
        return [], offset
    if os.path.getsize(path) < offset:          # truncated or rotated: start over
        return bus_lines(path, 0, owner)
    end = raw.rfind(b"\n") + 1
    out = []
    for l in raw[:end].decode("utf-8", "replace").splitlines():
        try:
            env = json.loads(l)
        except ValueError:
            continue
        if (env.get("source") or {}).get("host") != HOST:
            continue
        if owner and not any((ev.get("data") or {}).get("owner") == owner for ev in env.get("events", [])):
            continue
        out += line_of(env)
    return out, offset + end


def watch_bus(cfg, owner=None, from_start=False, out=None, poll_s=None, remote_jobs=None, stop=None):
    """Follow the Nod events file, one line per delega event of `owner`. Meanwhile pull the envelopes of
    the owner's remote jobs that have no waiter (the waiter pulls its own). Runs until stop() is true."""
    out = out or (lambda s: print(s, flush=True))
    poll_s = float(os.environ.get("DELEGA_WATCH_POLL_S", 2)) if poll_s is None else poll_s
    path = events_file()
    offset = 0 if from_start else (os.path.getsize(path) if os.path.exists(path) else 0)
    last_pull = 0.0
    while True:
        if remote_jobs and time.time() - last_pull >= float(os.environ.get("DELEGA_NOD_PULL_S", PULL_S)):
            last_pull = time.time()
            for d in remote_jobs():
                pull_and_forward(cfg, d)
        lines, offset = bus_lines(path, offset, owner)
        for l in lines:
            out(l)
        if stop and stop():
            return 0
        time.sleep(poll_s)

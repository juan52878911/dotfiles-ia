"""audit: append-only provenance log (<state_dir>/audit.jsonl) and its reader.

Every event is one JSON line: {ts, event, job, profile, account, node, branch, base,
project, owner, cost_usd, ...}. Events: launch, end, resume, handoff, integrate, refused, …
launch, end and refused also go to the Nod bus (nod.from_audit: job.launch, job.end, job.alert kind=refused)."""
import contextlib
import fcntl
import json
import os
import sys

from . import config, i18n, nod, util

FIELDS = ("job", "profile", "account", "node", "branch", "base", "project", "owner", "cost_usd")


def path(cfg):
    return os.path.join(cfg.state_dir, "audit.jsonl")


@contextlib.contextmanager
def locked(lock_path):
    """Exclusive flock on a side file (the data file may be replaced while held)."""
    os.makedirs(os.path.dirname(lock_path) or ".", exist_ok=True)
    with open(lock_path, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def record(cfg, event, **fields):
    """Append one event. Never raises on I/O trouble: auditing must not kill a launch."""
    rec = {"ts": util.now(), "event": event}
    rec.update({k: v for k, v in fields.items() if v is not None})
    p = path(cfg)
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                f.flush()
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
    except OSError as e:
        print(i18n.t("audit.write_failed", err=e), file=sys.stderr)
    if rec["event"] in nod.AUDIT_TYPES:
        try:
            nod.from_audit(cfg, {**rec, "owner": rec.get("owner") or _owner()})
        except Exception as e:      # noqa: BLE001 — the bus is best effort, the audit line is what counts
            print(i18n.t("nod.emit_failed", err=e), file=sys.stderr)
    return rec


def _owner():
    from . import jobs
    return jobs.owner_label()


def read(cfg):
    out = []
    try:
        with open(path(cfg), encoding="utf-8") as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return out


def select(records, key=None):
    """Records whose job id contains `key` or whose branch equals / ends with it."""
    if not key:
        return records
    return [r for r in records if key in str(r.get("job", "")) or
            key == r.get("branch") or str(r.get("branch", "")).endswith("/" + key)]


def _compact(v):
    """dbs = [{name, volume, sha, mode, …}] as name:volume@sha(mode),…"""
    if isinstance(v, list):
        return ",".join(f"{d.get('name')}:{d.get('volume')}@{d.get('sha', '')}" + (f"({d['mode']})" if d.get("mode") else "")
                        for d in v if isinstance(d, dict))
    return str(v)


def format_record(r):
    extra = {k: v for k, v in r.items() if k not in ("ts", "event") + FIELDS}
    head = f"{r.get('ts', '?')}  {r.get('event', '?'):<9}"
    bits = [f"{k}={r[k]}" for k in FIELDS if k in r]
    bits += [f"{k}={_compact(v) if k == 'dbs' else util.short(v, 60)}" for k, v in extra.items()]
    return head + "  " + " ".join(bits)


def main(argv):
    if argv and argv[0] in ("-h", "--help"):
        print(i18n.t("audit.usage"))
        return 0
    try:
        cfg = config.load()
    except config.ConfigError as e:
        print(e, file=sys.stderr)
        return 2
    key = argv[0] if argv else None
    recs = select(read(cfg), key)
    if not recs:
        print(i18n.t("audit.none", what=key or "*"))
        return 0
    for r in (recs if key else recs[-20:]):
        print(format_record(r))
    return 0

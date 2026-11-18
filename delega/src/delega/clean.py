"""clean: remove old, closed job directories from remote host nodes.

Only a job with END, an old end time and a successful branch fetch is eligible.  The
successful fetch is recorded by launch as an empty ``fetch.log`` (a failed fetch writes
its error there).  The remote lease is checked again immediately before a delete, so a
worker with a fresh lease is never removed.
"""
import argparse
import os
import re
import shlex
import sys
import time

from . import config as _config
from . import jobs, remote, util
from .i18n import t

DEFAULT_OLDER_THAN_S = 3 * 24 * 3600
_DURATION = re.compile(r"^(\d+(?:\.\d+)?)([dhmsw]?)$", re.IGNORECASE)
_UNITS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def duration(value):
    """Seconds in a duration such as ``3d``, ``12h`` or ``90m``."""
    match = _DURATION.match(str(value).strip())
    if not match:
        raise argparse.ArgumentTypeError(f"invalid duration: {value}")
    return float(match.group(1)) * _UNITS[match.group(2).lower()]


def _end_age(meta, now=None):
    """Age in seconds from END, or None when the job is not closed or its time is invalid."""
    if "END" not in meta or meta.get("END") in (None, ""):
        return None
    try:
        ended = float(meta["END"])
    except (TypeError, ValueError):
        return None
    return max(0.0, (now or time.time()) - ended)


def _branch_fetched(job_dir):
    """Whether launch's branch fetch succeeded.

    ``launch._finish`` creates ``fetch.log`` for every fetch attempt and writes git's
    error output there on failure.  Thus an existing empty file is positive proof that
    the job branch was brought to the Mac.  A missing file is deliberately not enough.
    """
    path = os.path.join(job_dir, "fetch.log")
    try:
        return os.path.isfile(path) and os.path.getsize(path) == 0
    except OSError:
        return False


def _channel(meta, node):
    """The safe remote job directory recorded in meta, or None."""
    channel = meta.get("CHANNEL")
    if not channel or channel.startswith("/") or "\\" in channel:
        return None
    parts = channel.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return None
    workdir = str(remote.workdir(node)).strip("/")
    jobs_subdir = str(remote.jobs_subdir(node)).strip("/")
    roots = []
    if workdir and workdir != ".":
        roots.append(f"{workdir}/{jobs_subdir}".rstrip("/") if jobs_subdir else workdir)
    if jobs_subdir:
        roots.append(jobs_subdir)
    if not any(channel == root or channel.startswith(root + "/") for root in roots):
        return None
    return channel


def _lease_cmd(channel):
    """Read the remote lease and the node clock; missing lease has an empty stderr."""
    path = shlex.quote(f"{channel}/lease.json")
    return f"cat {path} 2>/dev/null && echo && date +%s"


def _lease_state(cfg, node, channel, meta):
    """(fresh, error): fresh is True/False, or None when the lease cannot be trusted."""
    result = remote.ssh(node, _lease_cmd(channel), timeout=30)
    if result.returncode in (124, 255):
        return None, (result.stderr or result.stdout or f"ssh rc={result.returncode}").strip()
    lease, age = remote.lease_age(result)
    if lease is None:
        # `cat` returns 1 when this old job has no lease file.  Any other failure is
        # ambiguous and must not authorize a delete.
        return (False, None) if result.returncode == 1 else (None, (result.stderr or "").strip())
    if lease.get("final"):
        return False, None
    try:
        float(lease.get("renewed"))
    except (TypeError, ValueError):
        return None, "invalid lease"
    profile = meta.get("PROFILE") or None
    project = meta.get("PROJECT") or None
    try:
        stale = float(cfg.policy("lease.stale_s", profile, project))
    except (TypeError, ValueError):
        stale = 120.0
    return age is not None and age < stale, None


def _size_kib(node, channel):
    """(size in KiB, error); None size means that the remote directory is absent."""
    quoted = shlex.quote(channel)
    result = remote.ssh(node, f"test -d {quoted} && du -sk {quoted} 2>/dev/null | cut -f1", timeout=60)
    if result.returncode in (124, 255):
        return None, (result.stderr or result.stdout or f"ssh rc={result.returncode}").strip()
    if result.returncode != 0:
        return None, None
    lines = (result.stdout or "").strip().splitlines()
    if not lines:
        return None, None
    try:
        return int(lines[-1].strip()), None
    except ValueError:
        return None, "invalid size"


def _format_size(kib):
    if kib >= 1024 * 1024:
        return f"{kib / (1024 * 1024):.1f} GB"
    if kib >= 1024:
        return f"{kib / 1024:.1f} MB"
    return f"{kib} KB"


def _format_age(seconds):
    days, rest = divmod(int(seconds), 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m"
    return f"{seconds:.0f}s"


def clean(cfg, node_names=None, older_than_s=DEFAULT_OLDER_THAN_S, yes=False, out=None, err=None):
    """List (and with yes delete) eligible old job directories on host-backed nodes.

    Returns the number of eligible directories.  A node that cannot be checked is never
    treated as permission to delete.
    """
    out = out or sys.stdout
    err = err or sys.stderr
    selected = list(node_names or cfg.nodes.keys())
    nodes = {}
    for name in selected:
        node = cfg.node(name)
        if node.get("runner") != "kindling":
            nodes[name] = node

    now = time.time()
    candidates = []
    for job_dir in jobs.job_dirs(cfg):
        meta = util.read_meta(job_dir)
        node_name = meta.get("NODE")
        if node_name not in nodes:
            continue
        age = _end_age(meta, now)
        if age is None or age < older_than_s:
            continue
        if not _branch_fetched(job_dir):
            continue
        channel = _channel(meta, nodes[node_name])
        if not channel:
            continue
        fresh, error = _lease_state(cfg, nodes[node_name], channel, meta)
        if error:
            print(t("clean.unreachable", node=node_name, job=jobs.job_id(job_dir), err=error[-200:]), file=err)
            continue
        if fresh is not False:
            continue
        size, error = _size_kib(nodes[node_name], channel)
        if error:
            print(t("clean.unreachable", node=node_name, job=jobs.job_id(job_dir), err=error[-200:]), file=err)
            continue
        if size is None:
            continue
        candidates.append({"job": jobs.job_id(job_dir), "node": node_name, "channel": channel,
                           "size": size, "age": age, "meta": meta})

    if not candidates:
        print(t("clean.none"), file=out)
        return 0

    print(t("clean.header"), file=out)
    for item in candidates:
        print(t("clean.row", job=item["job"], node=item["node"], size=_format_size(item["size"]),
                age=_format_age(item["age"])), file=out)
        if not yes:
            continue
        node = nodes[item["node"]]
        fresh, error = _lease_state(cfg, node, item["channel"], item["meta"])
        if error or fresh is not False:
            if error:
                print(t("clean.failed", job=item["job"], node=item["node"], err=error[-200:]), file=err)
            continue
        quoted = shlex.quote(item["channel"])
        result = remote.ssh(node, f"rm -rf {quoted}", timeout=120)
        if result.returncode != 0:
            print(t("clean.failed", job=item["job"], node=item["node"],
                    err=(result.stderr or result.stdout or f"rc={result.returncode}")[-200:]), file=err)
            continue
        print(t("clean.removed", job=item["job"], node=item["node"], size=_format_size(item["size"])), file=out)
    if not yes:
        print(t("clean.hint"), file=out)
    return len(candidates)


def main(argv):
    parser = argparse.ArgumentParser(prog="delega clean", description="List (and with --yes delete) old closed "
                                                              "job directories on remote host nodes.")
    parser.add_argument("--node", action="append", help="node to clean (repeatable; default: all host nodes)")
    parser.add_argument("--older-than", type=duration, default=DEFAULT_OLDER_THAN_S,
                        help="minimum age of a closed job (default: 3d)")
    parser.add_argument("--yes", action="store_true", help="delete the listed directories (default: only list)")
    args = parser.parse_args(argv)
    try:
        cfg = _config.load()
    except _config.ConfigError as exc:
        print(f"delega: {exc}", file=sys.stderr)
        return 2
    try:
        clean(cfg, args.node, args.older_than, args.yes)
    except _config.ConfigError as exc:
        print(f"delega: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

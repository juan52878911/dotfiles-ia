"""`delega relevo-mem <order> [args]`: the ONLY way the escalated Claude reaches the org's database.

It runs on the daemon's host, as the Claude's own `Bash(delega relevo-mem *)`. The Claude gets no ssh:
this order talks to the base through `MemClient` (local, or `_ssh`, which quotes every argument), so
nothing the Claude writes is ever part of a shell line. It admits `log`, `export` (without `--out`),
`next`, `set` and `event`; `--db`, `--org` and `--owner` are fixed here from the daemon's config and
environment, never taken from the Claude. Writes carry the owner of the daemon that holds the lease
(`DELEGA_RELEVO_OWNER`): if a `promote` fenced that owner, the write fails (rc 9)."""
import json
import os
import sys

from . import config, i18n

ALLOWED = ("log", "export", "next", "set", "event")
FORBIDDEN_OPTS = ("--db", "--org", "--owner", "--out")      # `--out` only matters to `export`, refused for all
EXIT_USAGE = 2


def _fail(msg, rc=EXIT_USAGE):
    print(json.dumps({"ok": False, "error": msg}, ensure_ascii=False))
    print(msg, file=sys.stderr)
    return rc


def _opt_name(a):
    return a.split("=", 1)[0] if a.startswith("--") else None


def check(args):
    """None when the order is admitted, else the reason (rc 2)."""
    if not args or args[0] not in ALLOWED:
        return i18n.t("relevo_mem.order", order=args[0] if args else "", allowed=", ".join(ALLOWED))
    for a in args[1:]:
        if _opt_name(a) in FORBIDDEN_OPTS:
            return i18n.t("relevo_mem.option", opt=a)
    return None


def run(cfg, org, owner, args):
    """(rc, payload) of the order against the org's base, as `owner`."""
    from . import mem, queue
    client = queue.MemClient(cfg, org, owner)
    if not client.m["local"]:
        rc, payload = client._ssh(*args)
        return rc, payload
    import contextlib
    import io
    prev = os.environ.get("DELEGA_MEM_OWNER")
    os.environ["DELEGA_MEM_OWNER"] = owner
    argv = [*args, "--db", client.m["path"] or mem.db_path(None, org=org), "--org", org, "--json"]
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            rc = mem.main(argv)
    finally:
        if prev is None:
            os.environ.pop("DELEGA_MEM_OWNER", None)
        else:
            os.environ["DELEGA_MEM_OWNER"] = prev
    payload = {}
    for line in reversed(buf.getvalue().strip().splitlines()):
        try:
            payload = json.loads(line)
            break
        except ValueError:
            continue
    return rc, payload


def main(argv):
    args = list(argv or [])
    why = check(args)
    if why:
        return _fail(why)
    owner, org = os.environ.get("DELEGA_RELEVO_OWNER", "").strip(), os.environ.get("DELEGA_RELEVO_ORG", "").strip()
    if not owner or not org:
        return _fail(i18n.t("relevo_mem.no_env"))
    try:
        cfg = config.load()
        rc, payload = run(cfg, org, owner, args)
    except Exception as e:      # noqa: BLE001 — ssh down, unknown org…: an answer, not a traceback
        return _fail(str(e), 1)
    print(json.dumps(payload, ensure_ascii=False, default=str))
    if rc and payload.get("error"):
        print(payload["error"], file=sys.stderr)
    return rc

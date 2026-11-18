"""`delega relevo-mem <order> [args]`: the ONLY way the escalated Claude reaches the org's database.

It runs on the daemon's host, as the Claude's own `Bash(delega relevo-mem *)`. The Claude gets no ssh:
this order talks to the base through `MemClient` (local, or `_ssh`, which quotes every argument), so
nothing the Claude writes is ever part of a shell line. It admits `log`, `export` (without `--out`),
`next`, `set` and `event`; `--db`, `--org` and `--owner` are fixed here from the daemon's config and
environment, never taken from the Claude. Writes carry the owner of the daemon that holds the lease
(`DELEGA_RELEVO_OWNER`): if a `promote` fenced that owner, the write fails (rc 9). `set` and `event` only
touch the escalated task (`DELEGA_RELEVO_TASK`): `set` only to `pendiente`/`bloqueada` (no `--forzar`, no
job/branch fields), `event` only with a `relevo.` name, so the Claude cannot mark work done nor reset the
counters of `wave`/the daemon."""
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


SET_STATES = ("pendiente", "bloqueada")                     # never `hecha`/`ok`: that is integration's
SET_OPTS = ("--estado", "--desde", "--error")
EVENT_OPTS = ("--task", "--datos", "--once")
EVENT_PREFIX = "relevo."                                    # cannot forge wave.sha, lanzador_responde…


def _split(args):
    """(positionals, {option: value}) of `args[1:]`; a bare flag gets True."""
    pos, opts, i = [], {}, 1
    while i < len(args):
        a = args[i]
        if a.startswith("--"):
            name, eq, val = a.partition("=")
            if eq:
                opts[name] = val
            elif i + 1 < len(args) and not args[i + 1].startswith("--"):
                opts[name], i = args[i + 1], i + 1
            else:
                opts[name] = True
        else:
            pos.append(a)
        i += 1
    return pos, opts


def check(args, task=None):
    """None when the order is admitted, else the reason (rc 2). `task`: the escalated task, the only one
    `set` and `event` may touch (`DELEGA_RELEVO_TASK`); without it they are refused."""
    if not args or args[0] not in ALLOWED:
        return i18n.t("relevo_mem.order", order=args[0] if args else "", allowed=", ".join(ALLOWED))
    for a in args[1:]:
        if _opt_name(a) in FORBIDDEN_OPTS:
            return i18n.t("relevo_mem.option", opt=a)
    if args[0] not in ("set", "event"):
        return None
    pos, opts = _split(args)
    allowed = SET_OPTS if args[0] == "set" else EVENT_OPTS
    for name in opts:
        if name not in allowed:
            return i18n.t("relevo_mem.option", opt=name)
    if not task:
        return i18n.t("relevo_mem.no_task")
    if args[0] == "set":
        if pos != [task]:
            return i18n.t("relevo_mem.other_task", task=task)
        if opts.get("--estado") not in SET_STATES:
            return i18n.t("relevo_mem.state", states=", ".join(SET_STATES))
        return None
    if len(pos) != 1 or not pos[0].startswith(EVENT_PREFIX):
        return i18n.t("relevo_mem.event", prefix=EVENT_PREFIX)
    if opts.get("--task") != task:
        return i18n.t("relevo_mem.other_task", task=task)
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
    why = check(args, os.environ.get("DELEGA_RELEVO_TASK", "").strip() or None)
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

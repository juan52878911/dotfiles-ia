"""mem: the central orchestration memory of delega, in one SQLite database per organization.

What it is
----------
One SQLite file holds everything the orchestrators must agree on: the flows (a plan over a
git remote), their tasks (waves of briefs with their dependencies), the leases that say who
is driving right now, and an append-only log of what happened. There is one database per
organization (Trycore, personal…): they are never mixed, and the organization is fixed when
the database is created (`delega mem init --org <org>`).

Where it lives
--------------
The database lives on a server and is used through ssh:
    ssh <servidor> delega mem <orden> --json
On the server the file is opened directly, on the local disk: SQLite is never opened over a
network filesystem (the POSIX locks it needs are not reliable there), so `mem` refuses to
open a database that lives in one (mount point of type nfs, cifs, sshfs…).

The interface (stable: the daemon with leases and the handover are built on top of it)
--------------------------------------------------------------------------------------------------
    delega mem init       --org <org> [--db <ruta>]
    delega mem add-flow   --org <org> --repo <remoto git> [--branch <rama destino>] [--plan <texto>] [--id <id>]
    delega mem add-task   --org <org> --flow <flujo> --brief <texto> [--wave <n>] [--zone <zona>]
                           [--deps <a,b>] [--run-args <json>] [--id <id>]
    delega mem next       <flujo>
    delega mem set        <tarea> --estado <estado> --org <org> [--desde <estado>] [--forzar]
                           [--job-id <id>] [--nodo <n>] [--rama <r>] [--intentos <n>]
    delega mem claim      <rol> --owner <id> --org <org> --ttl <segundos>
    delega mem heartbeat  <rol> --owner <id> --org <org> [--ttl <segundos>]
    delega mem release    <rol> --owner <id> --org <org>
    delega mem log        [--limit <n>] [--flow <flujo>]
    delega mem export     [--out <fichero>]

Common options (accepted before or after the order): `--db <ruta>` (default `$DELEGA_MEM_DB`
or `~/.local/state/delega/memoria-<org>.db`) and `--json` (exactly one JSON object on stdout).
Every order that writes requires `--org <org>` or `$DELEGA_MEM_ORG`; lease orders also require
`--owner <id>` or `$DELEGA_MEM_OWNER`. The owner must stay stable across the separate SSH
processes that claim, renew and release one lease. The brief of a task is stored as text,
never as a path of the machine that wrote it, and the repo of a flow is a git remote
(`git@host:owner/repo`, `https://host/owner/repo` or `owner/name`): a local path is refused
(rc 2), because the worker will clone it on the node.

Exit codes
----------
    0  done
    2  usage or validation error (unknown order, unknown flow or task, a local path instead
       of a git remote, a missing or mismatched org, an unknown or invalid task transition)
    9  the lease is held by someone else, or a compare-and-set found a different task state
   10  the database is busy or locked; the caller should retry

Concurrency
-----------
Every write runs inside `BEGIN IMMEDIATE` with a `busy_timeout`, so two processes that
claim the same lease are serialized by SQLite itself: exactly one takes it (rc 0) and the
other gets rc 9. There is no corruption: each order is a single transaction and the log is
append-only (triggers forbid UPDATE and DELETE on `eventos`).

Schema (`PRAGMA user_version`; the migration is idempotent, so `init` can be repeated)
--------------------------------------------------------------------------------------------------
    org      id, org, creada                       the organization of this database (one row)
    flujos   id, org, repo, rama_destino, estado, plan, creado, actualizado
    tareas   id, flujo, ola, brief, zona, deps, run_args, estado, intentos, job_id, nodo,
             rama, creado, actualizado             estado ∈ pendiente|lanzada|ok|rojo|arreglo|
             bloqueada|hecha; deps and run_args are JSON
    leases   rol, dueno (stable owner id), vence_en, latido
    eventos  id, ts, quien, que, datos (JSON)      only INSERT (triggers)
"""
import json
import math
import os
import re
import socket
import sqlite3
import subprocess
import sys
import time

from . import i18n

SCHEMA_VERSION = 1
DEFAULT_DB_DIR = "~/.local/state/delega"
DEFAULT_DB_TEMPLATE = DEFAULT_DB_DIR + "/memoria-{org}.db"
BUSY_TIMEOUT_MS = 5000
DEFAULT_TTL_S = 30.0
MAX_TTL_S = 3600.0
STATES = ("pendiente", "lanzada", "ok", "rojo", "arreglo", "bloqueada", "hecha")
DONE = "hecha"
TRANSITIONS = {
    "pendiente": frozenset(("lanzada", "bloqueada")),
    "lanzada": frozenset(("ok", "rojo", "bloqueada")),
    "ok": frozenset(("hecha",)),
    "rojo": frozenset(("arreglo", "bloqueada")),
    "arreglo": frozenset(("lanzada", "bloqueada")),
    "bloqueada": frozenset(("pendiente",)),
    "hecha": frozenset(),
}
EXIT_OK, EXIT_USAGE, EXIT_LEASE, EXIT_BUSY = 0, 2, 9, 10
NET_FS = ("nfs", "nfs4", "cifs", "smbfs", "smb3", "sshfs", "webdav", "davfs", "afpfs", "9p",
          "ftpfs", "glusterfs", "ceph", "fuse.sshfs")

SCHEMA_V1 = [
    """CREATE TABLE IF NOT EXISTS org (
        id      INTEGER PRIMARY KEY CHECK (id = 1),
        org     TEXT NOT NULL,
        creada  REAL NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS flujos (
        id             TEXT PRIMARY KEY,
        org            TEXT NOT NULL,
        repo           TEXT NOT NULL,
        rama_destino   TEXT,
        estado         TEXT NOT NULL DEFAULT 'activo',
        plan           TEXT,
        creado         REAL NOT NULL,
        actualizado    REAL NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS tareas (
        id          TEXT PRIMARY KEY,
        flujo       TEXT NOT NULL,
        ola         INTEGER NOT NULL DEFAULT 1,
        brief       TEXT NOT NULL,
        zona        TEXT,
        deps        TEXT NOT NULL DEFAULT '[]',
        run_args    TEXT NOT NULL DEFAULT '{}',
        estado      TEXT NOT NULL DEFAULT 'pendiente'
                    CHECK (estado IN ('pendiente','lanzada','ok','rojo','arreglo','bloqueada','hecha')),
        intentos    INTEGER NOT NULL DEFAULT 0,
        job_id      TEXT,
        nodo        TEXT,
        rama        TEXT,
        creado      REAL NOT NULL,
        actualizado REAL NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS tareas_flujo ON tareas (flujo, ola, id)",
    """CREATE TABLE IF NOT EXISTS leases (
        rol       TEXT PRIMARY KEY,
        dueno     TEXT NOT NULL,
        vence_en  REAL NOT NULL,
        latido    REAL NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS eventos (
        id     INTEGER PRIMARY KEY AUTOINCREMENT,
        ts     REAL NOT NULL,
        quien  TEXT NOT NULL,
        que    TEXT NOT NULL,
        datos  TEXT NOT NULL DEFAULT '{}'
    )""",
    """CREATE TRIGGER IF NOT EXISTS eventos_solo_anexar_update BEFORE UPDATE ON eventos
        BEGIN SELECT RAISE(ABORT, 'eventos es solo-anexar'); END""",
    """CREATE TRIGGER IF NOT EXISTS eventos_solo_anexar_delete BEFORE DELETE ON eventos
        BEGIN SELECT RAISE(ABORT, 'eventos es solo-anexar'); END""",
]


class MemError(Exception):
    """A refused order (the message is already translated)."""


class MemConflict(MemError):
    """A compare-and-set or lease conflict; the caller may retry with fresh state."""


# ── helpers ─────────────────────────────────────────────────────────────────
def db_path(path=None, org=None):
    """Where the memory database lives: --db, then $DELEGA_MEM_DB, then the org's default."""
    for cand in (path, os.environ.get("DELEGA_MEM_DB")):
        if cand:
            return os.path.abspath(os.path.expanduser(os.path.expandvars(cand)))
    org = (org or os.environ.get("DELEGA_MEM_ORG") or "").strip()
    if not org:
        raise MemError(i18n.t("mem.org_required"))
    return os.path.abspath(os.path.expanduser(DEFAULT_DB_TEMPLATE.format(org=org)))


def owner_id():
    """Who is acting: $DELEGA_MEM_OWNER (a stable name, so a restarted daemon keeps its lease), else
    host:pid. The owner of a lease is this string."""
    return os.environ.get("DELEGA_MEM_OWNER") or f"{socket.gethostname()}:{os.getpid()}"


def _stamp(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts))


def _short(text, n=70):
    return " ".join(str(text).split())[:n]


def _new_id(prefix):
    return f"{prefix}-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid():d}-{os.urandom(3).hex()}"


# ── the git remote of a flow ────────────────────────────────────────────────
SCP_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9._-]+:\S+$")
URL_RE = re.compile(r"^(https?|ssh|git)://\S+$", re.IGNORECASE)
SHORT_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")


def is_remote(repo):
    """True when `repo` is a git remote (git@host:owner/repo, https://host/owner/repo, owner/name).

    A local path (absolute, relative, ~, file://, C:\\…) is not: the worker clones the repo
    on its node, so the memory only keeps the remote."""
    repo = (repo or "").strip()
    if not repo:
        return False
    if repo.startswith(("/", "~", ".")) or os.path.isabs(repo) or re.match(r"^[A-Za-z]:[\\/]", repo):
        return False
    try:
        if os.path.exists(repo) or os.path.exists(os.path.expanduser(repo)):
            return False
    except OSError:
        pass
    if SCP_RE.match(repo) or URL_RE.match(repo):
        return True
    if SHORT_RE.match(repo):
        return all(part not in (".", "..") for part in repo.split("/"))
    return False


# ── the filesystem of the database (never a network filesystem) ──────────────
def mounts():
    """[(mount point, fs type)] of the mounted filesystems ([] if it cannot be read)."""
    out = []
    try:
        with open("/proc/mounts", encoding="utf-8") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 3:
                    out.append((parts[1], parts[2]))
        return out
    except OSError:
        pass
    try:
        r = subprocess.run(["mount"], check=False, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return out
    for line in (r.stdout or "").splitlines():
        if " on " not in line:
            continue
        left, _, right = line.partition(" on ")
        opts = right.strip()
        fstype = opts.split("(")[-1].split(",")[0].strip() if "(" in opts else (opts.split() or [""])[0]
        out.append((left.strip(), fstype))
    return out


def network_fs(path, table=None):
    """The network filesystem type of `path` (nfs, cifs…), or None when it is local."""
    p = os.path.abspath(path)
    best = None
    for mp, fstype in (table if table is not None else mounts()):
        mp = mp.rstrip("/") or "/"
        if (p == mp or p.startswith(mp + "/")) and (best is None or len(mp) > len(best[0])):
            best = (mp, fstype)
    if best is None:
        return None
    fstype = (best[1] or "").lower()
    return fstype if any(k in fstype for k in NET_FS) else None


# ── database ────────────────────────────────────────────────────────────────
def connect(path, create=False):
    """Open the memory database (WAL, busy_timeout, autocommit: transactions are explicit)."""
    p = db_path(path)
    if not create and not os.path.exists(p):
        raise MemError(i18n.t("mem.db_missing", path=p))
    fs = network_fs(p)
    if fs and not os.environ.get("DELEGA_MEM_ALLOW_NETFS"):
        raise MemError(i18n.t("mem.netfs", path=p, fs=fs))
    if create:
        os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    conn = sqlite3.connect(p, timeout=BUSY_TIMEOUT_MS / 1000.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")
    if create:
        try:
            conn.execute("PRAGMA journal_mode = WAL").fetchone()
        except sqlite3.Error:
            pass
    return conn


def migrate(conn):
    """Create or update the schema. Idempotent: the version lives in `PRAGMA user_version`."""
    ver = conn.execute("PRAGMA user_version").fetchone()[0]
    if ver > SCHEMA_VERSION:
        raise MemError(i18n.t("mem.db_newer", ver=ver, want=SCHEMA_VERSION))
    if ver == SCHEMA_VERSION:
        return ver
    conn.execute("BEGIN IMMEDIATE")
    try:
        for stmt in SCHEMA_V1:
            conn.execute(stmt)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    return SCHEMA_VERSION


def write(conn, fn):
    """Run `fn(conn)` inside a write transaction (BEGIN IMMEDIATE serializes the writers)."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        out = fn(conn)
        conn.execute("COMMIT")
        return out
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise


def _event(conn, que, datos=None, quien=None):
    """Append one event. The log is only appended (triggers forbid UPDATE and DELETE)."""
    conn.execute("INSERT INTO eventos (ts, quien, que, datos) VALUES (?,?,?,?)",
                 (time.time(), quien or owner_id(), que, json.dumps(datos or {}, ensure_ascii=False)))


def get_org(conn):
    row = conn.execute("SELECT org FROM org WHERE id = 1").fetchone()
    return row["org"] if row else None


def init_db(conn, org):
    """Prepare the database and fix its organization. Returns True when it was created.

    The read and the write are one transaction: two `init` at the same time do not fight."""
    migrate(conn)

    def op(c):
        row = c.execute("SELECT org FROM org WHERE id = 1").fetchone()
        if row is None:
            c.execute("INSERT INTO org (id, org, creada) VALUES (1,?,?)", (org, time.time()))
            _event(c, "mem.init", {"org": org})
            return True
        if row["org"] != org:
            raise MemError(i18n.t("mem.org_mismatch", org=org, base=row["org"]))
        return False

    return write(conn, op)


def _profile_org(profile):
    """The org of a profile of the config (its `org`, else its name); None when no profile is given."""
    if not profile:
        return None
    try:
        from . import config
        return config.load().profile_org(profile)
    except Exception:
        return profile


def _check_org(conn, org):
    base = get_org(conn)
    if base is None:
        raise MemError(i18n.t("mem.no_org"))
    if org and org != base:
        raise MemError(i18n.t("mem.org_mismatch", org=org, base=base))
    return base


# ── flows ───────────────────────────────────────────────────────────────────
def add_flow(conn, repo, branch=None, plan=None, flow_id=None, org=None):
    """Add a flow: a plan over a git remote. The org of the database is the org of the flow."""
    if not is_remote(repo):
        raise MemError(i18n.t("mem.repo_local", repo=repo))
    now = time.time()
    fid = flow_id or _new_id("f")

    def op(c):
        base = _check_org(c, org)
        if c.execute("SELECT 1 FROM flujos WHERE id = ?", (fid,)).fetchone():
            raise MemError(i18n.t("mem.flow_exists", id=fid))
        c.execute("INSERT INTO flujos (id, org, repo, rama_destino, estado, plan, creado, actualizado)"
                  " VALUES (?,?,?,?,?,?,?,?)",
                  (fid, base, repo, branch, "activo", plan, now, now))
        _event(c, "flow.add", {"flujo": fid, "org": base, "repo": repo, "rama_destino": branch})
        return base

    base = write(conn, op)
    return {"id": fid, "org": base, "repo": repo, "rama_destino": branch, "estado": "activo", "plan": plan}


# ── tasks ───────────────────────────────────────────────────────────────────
def add_task(conn, flow, brief, wave=1, zone=None, deps=None, run_args=None, task_id=None, org=None):
    """Add a task to a flow. The brief is stored as text; deps are ids of tasks of the same flow."""
    if not (brief or "").strip():
        raise MemError(i18n.t("mem.brief_empty"))
    if run_args is not None and not isinstance(run_args, dict):
        raise MemError(i18n.t("mem.bad_json", opt="--run-args", err="not an object"))
    deps = list(deps or [])
    now = time.time()
    tid = task_id or _new_id("t")

    def op(c):
        base = _check_org(c, org)
        row = c.execute("SELECT id, org FROM flujos WHERE id = ?", (flow,)).fetchone()
        if row is None:
            raise MemError(i18n.t("mem.unknown_flow", flow=flow))
        if row["org"] != base:
            raise MemError(i18n.t("mem.org_mismatch", org=row["org"], base=base))
        prof_org = _profile_org((run_args or {}).get("profile"))
        if prof_org and prof_org != base:               # a personal task never lands in a Trycore base
            raise MemError(i18n.t("mem.org_mismatch", org=prof_org, base=base))
        known = {r["id"] for r in c.execute("SELECT id FROM tareas WHERE flujo = ?", (flow,)).fetchall()}
        for dep in deps:
            if dep not in known:
                raise MemError(i18n.t("mem.unknown_dep", task=tid, dep=dep))
        if c.execute("SELECT 1 FROM tareas WHERE id = ?", (tid,)).fetchone():
            raise MemError(i18n.t("mem.task_exists", id=tid))
        c.execute("INSERT INTO tareas (id, flujo, ola, brief, zona, deps, run_args, estado, intentos,"
                  " job_id, nodo, rama, creado, actualizado) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (tid, flow, int(wave), brief, zone, json.dumps(deps), json.dumps(run_args or {}),
                   "pendiente", 0, None, None, None, now, now))
        _event(c, "task.add", {"tarea": tid, "flujo": flow, "ola": int(wave), "deps": deps})

    write(conn, op)
    return {"id": tid, "flujo": flow, "ola": int(wave), "brief": brief, "zona": zone, "deps": deps,
            "run_args": run_args or {}, "estado": "pendiente", "intentos": 0}


def _task_dict(row):
    d = dict(row)
    d["deps"] = json.loads(d.get("deps") or "[]")
    d["run_args"] = json.loads(d.get("run_args") or "{}")
    return d


def list_flows(conn):
    """All the flows (the daemon drives the `activo` ones), oldest first."""
    return [dict(r) for r in conn.execute("SELECT * FROM flujos ORDER BY creado, id").fetchall()]


def next_tasks(conn, flow):
    """(ready, blocked): the pending tasks whose dependencies are all done, and the rest."""
    if conn.execute("SELECT 1 FROM flujos WHERE id = ?", (flow,)).fetchone() is None:
        raise MemError(i18n.t("mem.unknown_flow", flow=flow))
    rows = conn.execute("SELECT * FROM tareas WHERE flujo = ? ORDER BY ola, id", (flow,)).fetchall()
    done = {r["id"] for r in rows if r["estado"] == DONE}
    ready, blocked = [], []
    for r in rows:
        if r["estado"] != "pendiente":
            continue
        (ready if all(d in done for d in json.loads(r["deps"] or "[]")) else blocked).append(_task_dict(r))
    return ready, blocked


def set_task(conn, task, estado, job_id=None, nodo=None, rama=None, intentos=None, error=None,
             desde=None, force=False, org=None):
    """Move a task to another state (and note where it runs).

    The current row is read inside the write transaction. `desde` is an optional
    compare-and-set: when it does not match the current state the order conflicts (rc 9).
    Invalid transitions are refused; `force=True` bypasses that check (including moving a
    completed task backwards). Going to `lanzada` counts one attempt in the same transaction.
    """
    if estado not in STATES:
        raise MemError(i18n.t("mem.bad_state", estado=estado, states=", ".join(STATES)))
    if desde is not None and desde not in STATES:
        raise MemError(i18n.t("mem.bad_state", estado=desde, states=", ".join(STATES)))

    def op(c):
        _check_org(c, org)
        row = c.execute("SELECT * FROM tareas WHERE id = ?", (task,)).fetchone()
        if row is None:
            raise MemError(i18n.t("mem.unknown_task", task=task))
        current = row["estado"]
        if desde is not None and current != desde:
            raise MemConflict(i18n.t("mem.state_conflict", expected=desde, current=current, task=task))
        if not force and current != estado and estado not in TRANSITIONS.get(current, frozenset()):
            raise MemError(i18n.t("mem.bad_transition", current=current, target=estado))
        new_attempts = row["intentos"] + (1 if estado == "lanzada" and current != estado else 0)
        if intentos is not None:
            new_attempts = int(intentos)
        new_job_id = job_id if job_id is not None else row["job_id"]
        new_nodo = nodo if nodo is not None else row["nodo"]
        new_rama = rama if rama is not None else row["rama"]
        now = time.time()
        c.execute("UPDATE tareas SET estado = ?, intentos = ?, job_id = ?, nodo = ?, rama = ?,"
                  " actualizado = ? WHERE id = ?",
                  (estado, int(new_attempts), new_job_id, new_nodo, new_rama, now, task))
        _event(c, "task.set", {"tarea": task, "flujo": row["flujo"], "estado": estado,
                               "estado_anterior": current, "intentos": int(new_attempts),
                               "job_id": new_job_id, "nodo": new_nodo,
                               "rama": new_rama, "error": error})
        return {"id": task, "flujo": row["flujo"], "estado": estado, "intentos": int(new_attempts),
                "job_id": new_job_id, "nodo": new_nodo, "rama": new_rama, "actualizado": now}

    return write(conn, op)


# ── leases ──────────────────────────────────────────────────────────────────
def _validate_ttl(ttl):
    """A lease TTL must be a finite number of seconds in the interval (0, 3600]."""
    ttl = float(ttl)
    if not math.isfinite(ttl) or ttl <= 0 or ttl > MAX_TTL_S:
        raise MemError(i18n.t("mem.bad_ttl", val=ttl, max=MAX_TTL_S))
    return ttl


def claim_lease(conn, rol, ttl=DEFAULT_TTL_S, owner=None, org=None):
    """Take (or renew) the lease of a role. The lease of another owner that has not expired is
    never taken: the caller gets `estado = ocupada` and exit code 9."""
    owner = owner or owner_id()
    ttl = _validate_ttl(ttl)
    now = time.time()
    vence = now + ttl

    def op(c):
        _check_org(c, org)
        row = c.execute("SELECT * FROM leases WHERE rol = ?", (rol,)).fetchone()
        if row is None:
            c.execute("INSERT INTO leases (rol, dueno, vence_en, latido) VALUES (?,?,?,?)",
                      (rol, owner, vence, now))
            _event(c, "lease.claim", {"rol": rol, "dueno": owner, "vence_en": vence})
            return {"rol": rol, "dueno": owner, "estado": "tomada", "vence_en": vence}
        if row["dueno"] == owner:
            c.execute("UPDATE leases SET vence_en = ?, latido = ? WHERE rol = ?", (vence, now, rol))
            _event(c, "lease.renew", {"rol": rol, "dueno": owner, "vence_en": vence})
            return {"rol": rol, "dueno": owner, "estado": "renovada", "vence_en": vence}
        if row["vence_en"] <= now:
            c.execute("UPDATE leases SET dueno = ?, vence_en = ?, latido = ? WHERE rol = ?",
                      (owner, vence, now, rol))
            _event(c, "lease.take", {"rol": rol, "dueno": owner, "vence_en": vence, "antes": row["dueno"]})
            return {"rol": rol, "dueno": owner, "estado": "tomada", "vence_en": vence}
        return {"rol": rol, "dueno": row["dueno"], "estado": "ocupada", "vence_en": row["vence_en"]}

    return write(conn, op)


def heartbeat_lease(conn, rol, ttl=DEFAULT_TTL_S, owner=None, org=None):
    """Renew a lease. An expired lease is taken over; a live lease of another owner is not."""
    owner = owner or owner_id()
    ttl = _validate_ttl(ttl)
    now = time.time()
    vence = now + ttl

    def op(c):
        _check_org(c, org)
        row = c.execute("SELECT * FROM leases WHERE rol = ?", (rol,)).fetchone()
        if row is None:
            raise MemError(i18n.t("mem.lease_missing", rol=rol))
        if row["dueno"] == owner:
            c.execute("UPDATE leases SET vence_en = ?, latido = ? WHERE rol = ?", (vence, now, rol))
            _event(c, "lease.renew", {"rol": rol, "dueno": owner, "vence_en": vence})
            return {"rol": rol, "dueno": owner, "estado": "renovada", "vence_en": vence}
        if row["vence_en"] <= now:
            c.execute("UPDATE leases SET dueno = ?, vence_en = ?, latido = ? WHERE rol = ?",
                      (owner, vence, now, rol))
            _event(c, "lease.take", {"rol": rol, "dueno": owner, "vence_en": vence,
                                     "antes": row["dueno"]})
            return {"rol": rol, "dueno": owner, "estado": "tomada", "vence_en": vence}
        return {"rol": rol, "dueno": row["dueno"], "estado": "ocupada", "vence_en": row["vence_en"]}

    return write(conn, op)


def release_lease(conn, rol, owner=None, org=None):
    """Give a lease back. The lease of another owner that has not expired is never released."""
    owner = owner or owner_id()
    now = time.time()

    def op(c):
        _check_org(c, org)
        row = c.execute("SELECT * FROM leases WHERE rol = ?", (rol,)).fetchone()
        if row is None:
            return {"rol": rol, "estado": "nada"}
        if row["dueno"] != owner and row["vence_en"] > now:
            return {"rol": rol, "dueno": row["dueno"], "estado": "ocupada", "vence_en": row["vence_en"]}
        c.execute("DELETE FROM leases WHERE rol = ?", (rol,))
        _event(c, "lease.release", {"rol": rol, "dueno": row["dueno"]})
        return {"rol": rol, "dueno": row["dueno"], "estado": "liberada"}

    return write(conn, op)


# ── the log and the export ──────────────────────────────────────────────────
def log_events(conn, limit=50, flow=None):
    """The last events of the append-only log (the newest last)."""
    rows = conn.execute("SELECT ts, quien, que, datos FROM eventos ORDER BY id").fetchall()
    out = []
    for r in rows:
        datos = json.loads(r["datos"] or "{}")
        if flow and datos.get("flujo") != flow:
            continue
        out.append({"ts": r["ts"], "quien": r["quien"], "que": r["que"], "datos": datos})
    return out[-int(limit):] if limit else out


def export(conn):
    """Everything the memory holds (flows, tasks, leases, events)."""
    flujos = [dict(r) for r in conn.execute("SELECT * FROM flujos ORDER BY creado, id").fetchall()]
    tareas = [_task_dict(r) for r in conn.execute("SELECT * FROM tareas ORDER BY flujo, ola, id").fetchall()]
    leases = [dict(r) for r in conn.execute("SELECT * FROM leases ORDER BY rol").fetchall()]
    eventos = log_events(conn, limit=0)
    return {"org": get_org(conn), "flujos": flujos, "tareas": tareas, "leases": leases, "eventos": eventos}


# ── the orders ──────────────────────────────────────────────────────────────
def _parse(args, flags=(), values=(), multi=()):
    """Split the arguments of an order into (positionals, flags, {option: value})."""
    pos, out, fs = [], {}, set()
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            pos.extend(args[i + 1:])
            break
        if a.startswith("--"):
            name, val = a[2:], None
            if "=" in name:
                name, val = name.split("=", 1)
            if name in values:
                if val is None:
                    i += 1
                    if i >= len(args):
                        raise MemError(i18n.t("mem.needs_value", opt="--" + name))
                    val = args[i]
                if name in multi:
                    out.setdefault(name, []).append(val)
                else:
                    out[name] = val
            elif name in flags:
                if val is not None:
                    raise MemError(i18n.t("mem.bad_option", opt="--" + name))
                fs.add(name)
            else:
                raise MemError(i18n.t("mem.bad_option", opt=a))
        else:
            pos.append(a)
        i += 1
    return pos, fs, out


def _out(payload, lines, as_json):
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, default=str))
    else:
        for line in lines:
            print(line)


def _fail(msg, as_json, rc=EXIT_USAGE):
    if as_json:
        print(json.dumps({"ok": False, "error": msg}, ensure_ascii=False))
    print(msg, file=sys.stderr)
    return rc


def _number(val, opt, cast=float):
    try:
        return cast(val)
    except (TypeError, ValueError):
        raise MemError(i18n.t("mem.bad_number", opt=opt, val=val))


def _org(vals, required=False):
    org = (vals.get("org") or os.environ.get("DELEGA_MEM_ORG") or "").strip()
    if required and not org:
        raise MemError(i18n.t("mem.org_required"))
    return org


def _owner(vals):
    owner = (vals.get("owner") or os.environ.get("DELEGA_MEM_OWNER") or "").strip()
    if not owner:
        raise MemError(i18n.t("mem.owner_required"))
    return owner


def cmd_init(args, db, as_json):
    _, _, vals = _parse(args, values=("org",))
    org = _org(vals, required=True)
    path = db_path(db, org=org)
    conn = connect(path, create=True)
    try:
        created = init_db(conn, org)
    finally:
        conn.close()
    payload = {"ok": True, "cmd": "init", "db": path, "org": org, "created": created}
    key = "mem.init_ok" if created else "mem.init_exists"
    _out(payload, [i18n.t(key, path=path, org=org)], as_json)
    return EXIT_OK


def cmd_add_flow(args, db, as_json):
    pos, _, vals = _parse(args, values=("repo", "branch", "rama", "plan", "id", "org"))
    org = _org(vals, required=True)
    repo = vals.get("repo") or (pos.pop(0) if pos else None)
    if not repo:
        raise MemError(i18n.t("mem.needs_repo"))
    conn = connect(db_path(db, org=org))
    try:
        flow = add_flow(conn, repo=repo, branch=vals.get("branch") or vals.get("rama"), plan=vals.get("plan"),
                        flow_id=vals.get("id"), org=org)
    finally:
        conn.close()
    payload = {"ok": True, "cmd": "add-flow", "flujo": flow}
    _out(payload, [i18n.t("mem.flow_added", id=flow["id"], repo=repo)], as_json)
    return EXIT_OK


def cmd_add_task(args, db, as_json):
    pos, _, vals = _parse(args, values=("flow", "brief", "brief-file", "wave", "ola", "zone", "zona",
                                        "deps", "dep", "run-args", "id", "org"),
                          multi=("dep",))
    flow = vals.get("flow") or (pos.pop(0) if pos else None)
    if not flow:
        raise MemError(i18n.t("mem.needs_flow"))
    brief = vals.get("brief")
    if not brief and vals.get("brief-file"):
        try:
            with open(os.path.expanduser(vals["brief-file"]), encoding="utf-8") as f:
                brief = f.read()
        except OSError as e:
            raise MemError(i18n.t("mem.bad_file", path=vals["brief-file"], err=e))
    if not brief and pos:
        brief = pos.pop(0)
    org = _org(vals, required=True)
    deps = []
    for chunk in ([vals["deps"]] if vals.get("deps") else []) + list(vals.get("dep") or []):
        deps += [d.strip() for d in str(chunk).split(",") if d.strip()]
    run_args = {}
    if vals.get("run-args"):
        try:
            run_args = json.loads(vals["run-args"])
        except ValueError as e:
            raise MemError(i18n.t("mem.bad_json", opt="--run-args", err=e))
    wave = _number(vals.get("wave") or vals.get("ola") or 1, "--wave", int)
    conn = connect(db_path(db, org=org))
    try:
        task = add_task(conn, flow=flow, brief=brief, wave=wave, zone=vals.get("zone") or vals.get("zona"),
                        deps=deps, run_args=run_args, task_id=vals.get("id"), org=org)
    finally:
        conn.close()
    payload = {"ok": True, "cmd": "add-task", "tarea": task}
    _out(payload, [i18n.t("mem.task_added", id=task["id"], flow=flow, wave=wave)], as_json)
    return EXIT_OK


def cmd_next(args, db, as_json):
    pos, _, vals = _parse(args, values=("flow", "org"))
    org = _org(vals)
    flow = vals.get("flow") or (pos.pop(0) if pos else None)
    if not flow:
        raise MemError(i18n.t("mem.needs_flow"))
    conn = connect(db_path(db, org=org))
    try:
        ready, blocked = next_tasks(conn, flow)
    finally:
        conn.close()
    payload = {"ok": True, "cmd": "next", "flujo": flow, "tareas": ready, "bloqueadas": blocked}
    lines = [i18n.t("mem.next", flow=flow, n=len(ready))]
    lines += [f"  {t['id']}  ola={t['ola']}  {t['zona'] or '-'}  {_short(t['brief'], 60)}" for t in ready]
    if not ready:
        lines = [i18n.t("mem.next_empty", flow=flow)]
    _out(payload, lines, as_json)
    return EXIT_OK


def cmd_set(args, db, as_json):
    pos, flags, vals = _parse(args, flags=("forzar",),
                               values=("estado", "state", "desde", "job-id", "nodo", "node", "rama",
                                       "branch", "intentos", "error", "id", "org"))
    org = _org(vals, required=True)
    task = vals.get("id") or (pos.pop(0) if pos else None)
    if not task:
        raise MemError(i18n.t("mem.needs_task"))
    estado = vals.get("estado") or vals.get("state")
    if not estado:
        raise MemError(i18n.t("mem.needs_state"))
    intentos = _number(vals["intentos"], "--intentos", int) if vals.get("intentos") is not None else None
    conn = connect(db_path(db, org=org))
    try:
        res = set_task(conn, task, estado, job_id=vals.get("job-id"), nodo=vals.get("nodo") or vals.get("node"),
                       rama=vals.get("rama") or vals.get("branch"), intentos=intentos, error=vals.get("error"),
                       desde=vals.get("desde"), force="forzar" in flags, org=org)
    finally:
        conn.close()
    payload = {"ok": True, "cmd": "set", "tarea": res}
    _out(payload, [i18n.t("mem.task_set", id=task, estado=estado)], as_json)
    return EXIT_OK


def cmd_claim(args, db, as_json):
    pos, _, vals = _parse(args, values=("rol", "role", "ttl", "owner", "org"))
    org = _org(vals, required=True)
    owner = _owner(vals)
    rol = vals.get("rol") or vals.get("role") or (pos.pop(0) if pos else None)
    if not rol:
        raise MemError(i18n.t("mem.needs_role"))
    ttl = _number(vals.get("ttl", DEFAULT_TTL_S), "--ttl", float)
    conn = connect(db_path(db, org=org))
    try:
        res = claim_lease(conn, rol, ttl, owner=owner, org=org)
    finally:
        conn.close()
    if res["estado"] == "ocupada":
        msg = i18n.t("mem.lease_held", rol=rol, owner=res["dueno"], when=_stamp(res["vence_en"]))
        if as_json:
            print(json.dumps({"ok": False, "cmd": "claim", "rol": rol, "dueno": res["dueno"],
                              "vence_en": res["vence_en"], "error": msg}, ensure_ascii=False))
        print(msg, file=sys.stderr)
        return EXIT_LEASE
    payload = {"ok": True, "cmd": "claim", "rol": rol, "dueno": res["dueno"], "estado": res["estado"],
               "vence_en": res["vence_en"]}
    key = {"tomada": "mem.lease_taken", "renovada": "mem.lease_renewed"}.get(res["estado"], "mem.lease_taken")
    _out(payload, [i18n.t(key, rol=rol, owner=res["dueno"], when=_stamp(res["vence_en"]))], as_json)
    return EXIT_OK


def cmd_heartbeat(args, db, as_json):
    pos, _, vals = _parse(args, values=("rol", "role", "ttl", "owner", "org"))
    org = _org(vals, required=True)
    owner = _owner(vals)
    rol = vals.get("rol") or vals.get("role") or (pos.pop(0) if pos else None)
    if not rol:
        raise MemError(i18n.t("mem.needs_role"))
    ttl = _number(vals.get("ttl", DEFAULT_TTL_S), "--ttl", float)
    conn = connect(db_path(db, org=org))
    try:
        res = heartbeat_lease(conn, rol, ttl, owner=owner, org=org)
    finally:
        conn.close()
    if res["estado"] == "ocupada":
        msg = i18n.t("mem.lease_held", rol=rol, owner=res["dueno"], when=_stamp(res["vence_en"]))
        if as_json:
            print(json.dumps({"ok": False, "cmd": "heartbeat", "rol": rol, "dueno": res["dueno"],
                              "vence_en": res["vence_en"], "error": msg}, ensure_ascii=False))
        print(msg, file=sys.stderr)
        return EXIT_LEASE
    payload = {"ok": True, "cmd": "heartbeat", "rol": rol, "dueno": res["dueno"], "estado": res["estado"],
               "vence_en": res["vence_en"]}
    key = {"tomada": "mem.lease_taken_over", "renovada": "mem.lease_renewed"}.get(res["estado"], "mem.lease_taken")
    _out(payload, [i18n.t(key, rol=rol, owner=res["dueno"], when=_stamp(res["vence_en"]))], as_json)
    return EXIT_OK


def cmd_flows(args, db, as_json):
    _parse(args)
    conn = connect(db_path(db))
    try:
        flows = list_flows(conn)
    finally:
        conn.close()
    _out({"ok": True, "cmd": "flows", "flujos": flows}, [f"{f['id']}  {f['estado']}  {f['repo']}" for f in flows],
         as_json)
    return EXIT_OK


def cmd_release(args, db, as_json):
    pos, _, vals = _parse(args, values=("rol", "role", "owner", "org"))
    org = _org(vals, required=True)
    owner = _owner(vals)
    rol = vals.get("rol") or vals.get("role") or (pos.pop(0) if pos else None)
    if not rol:
        raise MemError(i18n.t("mem.needs_role"))
    conn = connect(db_path(db, org=org))
    try:
        res = release_lease(conn, rol, owner=owner, org=org)
    finally:
        conn.close()
    if res["estado"] == "ocupada":
        msg = i18n.t("mem.lease_held", rol=rol, owner=res["dueno"], when=_stamp(res["vence_en"]))
        if as_json:
            print(json.dumps({"ok": False, "cmd": "release", "rol": rol, "dueno": res["dueno"],
                              "vence_en": res["vence_en"], "error": msg}, ensure_ascii=False))
        print(msg, file=sys.stderr)
        return EXIT_LEASE
    payload = {"ok": True, "cmd": "release", "rol": rol, "estado": res["estado"]}
    key = "mem.lease_released" if res["estado"] == "liberada" else "mem.lease_none"
    _out(payload, [i18n.t(key, rol=rol)], as_json)
    return EXIT_OK


def cmd_log(args, db, as_json):
    _, _, vals = _parse(args, values=("limit", "flow", "org"))
    org = _org(vals)
    limit = _number(vals.get("limit", 50), "--limit", int)
    conn = connect(db_path(db, org=org))
    try:
        events = log_events(conn, limit=limit, flow=vals.get("flow"))
    finally:
        conn.close()
    payload = {"ok": True, "cmd": "log", "eventos": events}
    lines = [f"{_stamp(e['ts'])}  {e['quien']}  {e['que']}  {json.dumps(e['datos'], ensure_ascii=False)}"
             for e in events]
    if not events:
        lines = [i18n.t("mem.log_empty")]
    _out(payload, lines, as_json)
    return EXIT_OK


def cmd_export(args, db, as_json):
    _, _, vals = _parse(args, values=("out", "org"))
    org = _org(vals)
    conn = connect(db_path(db, org=org))
    try:
        payload = export(conn)
    finally:
        conn.close()
    payload = {"ok": True, "cmd": "export", **payload}
    if vals.get("out"):
        out = os.path.abspath(os.path.expanduser(vals["out"]))
        os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
            f.write("\n")
        _out(payload, [i18n.t("mem.exported", out=out)], as_json)
        return EXIT_OK
    _out(payload, [], as_json)
    return EXIT_OK


def cmd_install_daemon(args, db, as_json):
    from . import queue
    return queue.cmd_install_daemon(args, db, as_json)


# ── event, backup and promote (the relevo: another team can take the memory over) ───────────────
def add_event(conn, que, datos=None, task=None, once=False, quien=None):
    """Append an event (with `tarea` when given). With `once`, only if the task has none of that kind yet:
    the check and the insert are one transaction. Returns True when it wrote the event."""
    datos = dict(datos or {})
    if task:
        datos["tarea"] = task

    def op(c):
        if once and task:
            for r in c.execute("SELECT datos FROM eventos WHERE que = ?", (que,)).fetchall():
                if json.loads(r["datos"] or "{}").get("tarea") == task:
                    return False
        _event(c, que, datos, quien=quien)
        return True
    return write(conn, op)


def cmd_event(args, db, as_json):
    pos, flags, vals = _parse(args, flags=("once",), values=("task", "datos", "org"))
    org = _org(vals, required=True)
    if not pos:
        raise MemError(i18n.t("mem.event_usage"))
    try:
        datos = json.loads(vals.get("datos") or "{}")
        if not isinstance(datos, dict):
            raise ValueError
    except ValueError:
        raise MemError(i18n.t("mem.event_usage"))
    conn = connect(db_path(db, org=org))
    try:
        _check_org(conn, org)
        created = add_event(conn, pos[0], datos, task=vals.get("task"), once="once" in flags)
    finally:
        conn.close()
    _out({"ok": True, "cmd": "event", "creado": created}, [i18n.t("mem.event_done", que=pos[0], creado=created)],
         as_json)
    return EXIT_OK


def _is_remote_dest(dest):
    return bool(re.match(r"^[^/\s:]+:", dest))


def backup(conn, dest_file):
    """Consistent copy of the open database into `dest_file` (sqlite's online backup = `.backup`)."""
    os.makedirs(os.path.dirname(dest_file) or ".", exist_ok=True)
    if os.path.exists(dest_file):
        os.remove(dest_file)
    out = sqlite3.connect(dest_file)
    try:
        conn.backup(out)
    finally:
        out.close()


def cmd_backup(args, db, as_json):
    """`delega mem backup --to <host:path | path>`: a consistent copy, sent with scp when it has a host."""
    import tempfile
    _, _, vals = _parse(args, values=("to", "org"))
    org = _org(vals, required=True)
    dest = vals.get("to")
    if not dest:
        raise MemError(i18n.t("mem.backup_usage"))
    conn = connect(db_path(db, org=org))
    try:
        _check_org(conn, org)
        if not _is_remote_dest(dest):
            target = os.path.abspath(os.path.expanduser(dest))
            backup(conn, target)
        else:
            with tempfile.TemporaryDirectory(prefix="delega-mem-") as tmp:
                snap = os.path.join(tmp, "memoria.db")
                backup(conn, snap)
                host, path = dest.split(":", 1)
                r = subprocess.run(["ssh", "-o", "BatchMode=yes", host, "mkdir", "-p",
                                    os.path.dirname(path) or "."], capture_output=True, text=True, timeout=60)
                if r.returncode == 0:
                    r = subprocess.run(["scp", "-o", "BatchMode=yes", "-q", snap, dest],
                                       capture_output=True, text=True, timeout=300)
                if r.returncode != 0:
                    raise MemError(i18n.t("mem.backup_failed", to=dest, err=(r.stderr or r.stdout).strip()))
            target = dest
    finally:
        conn.close()
    _out({"ok": True, "cmd": "backup", "destino": target}, [i18n.t("mem.backup_done", to=target)], as_json)
    return EXIT_OK


def cmd_promote(args, db, as_json):
    """`delega mem promote --from <copy> --yes [--db <main>]`: the copy becomes the main database.
    The previous main is kept as `<main>.before-promote`; the leases are cleared so a daemon can take
    over. Without --yes nothing is touched (rc 2)."""
    import shutil
    _, flags, vals = _parse(args, flags=("yes",), values=("from", "org"))
    org_arg = _org(vals, required=True)
    src = vals.get("from")
    if not src:
        raise MemError(i18n.t("mem.promote_usage"))
    src, main = os.path.abspath(os.path.expanduser(src)), db_path(db, org=org_arg)
    if "yes" not in flags:
        _out({"ok": False, "cmd": "promote", "error": i18n.t("mem.promote_dry", src=src, main=main)},
             [], as_json)
        print(i18n.t("mem.promote_dry", src=src, main=main), file=sys.stderr)
        return EXIT_USAGE
    if src == main:
        raise MemError(i18n.t("mem.promote_same", path=main))
    conn = connect(src)                                  # refuses a missing file or a network filesystem
    try:
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise MemError(i18n.t("mem.promote_corrupt", path=src))
        org = get_org(conn)
        if not org:
            raise MemError(i18n.t("mem.promote_corrupt", path=src))
        if org != org_arg:
            raise MemError(i18n.t("mem.org_mismatch", org=org_arg, base=org))
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    if network_fs(os.path.dirname(main) or "."):
        raise MemError(i18n.t("mem.netfs", path=main, fs=network_fs(os.path.dirname(main) or ".")))
    os.makedirs(os.path.dirname(main) or ".", exist_ok=True)
    kept = None
    if os.path.exists(main):
        kept = main + ".before-promote"
        shutil.copy2(main, kept)
    tmp = main + ".promoting"
    shutil.copy2(src, tmp)
    for ext in ("-wal", "-shm"):                         # a stale journal of the old main must not apply to the copy
        if os.path.exists(main + ext):
            os.remove(main + ext)
    os.replace(tmp, main)
    conn = connect(main)
    try:
        write(conn, lambda c: (c.execute("DELETE FROM leases"),
                               _event(c, "mem.promote", {"desde": src, "anterior": kept})))
    finally:
        conn.close()
    _out({"ok": True, "cmd": "promote", "org": org, "principal": main, "anterior": kept},
         [i18n.t("mem.promoted", org=org, main=main)], as_json)
    return EXIT_OK


ORDERS = {
    "install-daemon": cmd_install_daemon, "flows": cmd_flows,
    "init": cmd_init, "add-flow": cmd_add_flow, "add-task": cmd_add_task, "next": cmd_next, "set": cmd_set,
    "backup": cmd_backup, "promote": cmd_promote, "event": cmd_event,
    "claim": cmd_claim, "heartbeat": cmd_heartbeat, "release": cmd_release, "log": cmd_log, "export": cmd_export,
}


def _db_is_busy(exc):
    """True for SQLite's retryable busy/locked operational errors."""
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    code = getattr(exc, "sqlite_errorcode", None)
    if code is not None and (code & 0xFF) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
        return True
    message = str(exc).lower()
    return "locked" in message or "busy" in message


def main(argv):
    """`delega mem <order> [--db <path>] [--json]`.

    Exit codes: 0 done, 2 invalid, 9 lease/CAS conflict, 10 database busy (retry).
    """
    argv = list(argv or [])
    db, as_json, rest = None, False, []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--json":
            as_json = True
        elif a == "--no-json":
            as_json = False
        elif a == "--db":
            i += 1
            if i >= len(argv):
                return _fail(i18n.t("mem.needs_value", opt="--db"), as_json)
            db = argv[i]
        elif a.startswith("--db="):
            db = a[5:]
        elif a in ("-h", "--help"):
            rest.append("--help")
        else:
            rest.append(a)
        i += 1
    if not rest or rest[0] in ("-h", "--help"):
        print(i18n.t("mem.usage"))
        return EXIT_OK if rest else EXIT_USAGE
    cmd, args = rest[0], rest[1:]
    fn = ORDERS.get(cmd)
    if fn is None:
        return _fail(i18n.t("mem.unknown_cmd", cmd=cmd), as_json)
    try:
        return fn(args, db, as_json)
    except MemConflict as e:
        return _fail(str(e), as_json, EXIT_LEASE)
    except MemError as e:
        return _fail(str(e), as_json)
    except sqlite3.OperationalError as e:
        if _db_is_busy(e):
            return _fail(i18n.t("mem.db_busy", err=e), as_json, EXIT_BUSY)
        return _fail(i18n.t("mem.db_error", err=e), as_json)
    except sqlite3.Error as e:
        return _fail(i18n.t("mem.db_error", err=e), as_json)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

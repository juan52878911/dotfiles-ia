"""databases: a throwaway copy of a project's database inside each kindling job, the dump never leaving the host.

A project declares its databases (`[projects."owner/repo".databases.<name>]`): `engine` (postgres, mariadb,
mysql, redis, mongo, sqlite), `version`, `database` (name inside the engine; default <name>), `volume`
(kindling volume holding the dump; default `db-<repo>-<name>`), `attach = "always"` (every kindling job of
the project gets it; otherwise only with `delega run --db <name>`), `env = {VAR = "{url}"}` (what the
worker receives; placeholders {host} {port} {user} {password} {database} {url} {path}; default per engine),
`mode = "auto" | "golden" | "volume"`.

Where the data lives, all on the kindling host:
- the **volume** `db-…` holds `dump.<ext>.part-*` and `MANIFEST` (engine, format, sha256, bytes; no data).
  It is written once by `delega db volume create|refresh` (streamed: host file, host command, or a file /
  command on this machine piped through ssh, never written here) and only ever mounted read-only after.
- a **seed sandbox** `delega-seed-<volume>` mounts it read-only and sleeps frozen; jobs copy the parts
  out of it VM to VM through the host (`kling cp`), the engine restores them on the job VM's own disk.
- a **data golden** `<golden>-db` (`delega golden db`): the job golden with the databases already loaded
  and running, frozen. A job cloned from it has its engines up in about a second instead of restoring.
  `mode = "auto"` uses it while it is fresh (same dumps, same base golden) and falls back to the volume.

Every job gets its own copy (copy-on-write of the template, or its own restore): writes die with the VM.
"""
import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time

from . import audit, config as _config, remote, util
from .i18n import t

ENGINES = {
    # engine     port   dump formats (first = default for a new volume)
    "postgres": (5432, ("dump", "sql.gz", "sql", "tar")),
    "mariadb":  (3306, ("sql.gz", "sql")),
    "mysql":    (3306, ("sql.gz", "sql")),
    "redis":    (6379, ("rdb", "redis")),
    "mongo":    (27017, ("archive.gz", "archive")),
    "sqlite":   (0, ("sqlite", "db", "sqlite3", "sql")),
}
DEFAULT_ENV = {
    "postgres": {"PGHOST": "{host}", "PGPORT": "{port}", "PGUSER": "{user}", "PGPASSWORD": "{password}",
                 "PGDATABASE": "{database}", "DATABASE_URL": "{url}"},
    "mariadb": {"MYSQL_HOST": "{host}", "MYSQL_TCP_PORT": "{port}", "MYSQL_USER": "{user}",
                "MYSQL_PWD": "{password}", "MYSQL_DATABASE": "{database}", "DATABASE_URL": "{url}"},
    "redis": {"REDIS_URL": "{url}"},
    "mongo": {"MONGODB_URI": "{url}"},
    "sqlite": {"SQLITE_PATH": "{path}", "DATABASE_URL": "{url}"},
}
DEFAULT_ENV["mysql"] = DEFAULT_ENV["mariadb"]
USER = PASSWORD = "delega"          # throwaway credentials, only reachable inside the job's VM
DBCTL = os.path.join(util.PKG_DIR, "data", "dbctl.sh")
VM_DBCTL = "/usr/local/bin/delega-db"
SEED_PREFIX = "delega-seed-"
CHUNK = "48m"                       # kling cp moves at most 64 MiB per file
q = shlex.quote


class DbError(Exception):
    pass


def _slug(s):
    return re.sub(r"[^a-z0-9-]+", "-", s.lower()).strip("-")


# ── declaration ─────────────────────────────────────────────────────────────
def specs(cfg, project):
    """{name: spec} declared by the project (normalised). Raises DbError on a bad declaration."""
    if not project:
        return {}
    raw = (cfg.projects.get(project) or {}).get("databases") or {}
    out = {}
    for name, d in raw.items():
        e = d.get("engine")
        if e not in ENGINES:
            raise DbError(t("db.err.engine", name=name, engine=e, known=", ".join(ENGINES)))
        out[name] = {"name": name, "engine": e, "version": str(d.get("version") or ""),
                     "database": d.get("database") or name,
                     "volume": d.get("volume") or f"db-{_slug(project.split('/')[-1])}-{_slug(name)}",
                     "attach": d.get("attach", "ask"), "mode": d.get("mode", "auto"),
                     "env": d.get("env") or DEFAULT_ENV[e], "node": d.get("node"), "profile": d.get("profile")}
    seen = {}
    for s in out.values():
        for var in s["env"]:
            if var in seen:
                raise DbError(t("db.err.env_clash", var=var, a=seen[var], b=s["name"]))
            seen[var] = s["name"]
    return out


def select(cfg, project, wanted):
    """Specs a job gets: `--db` names (or "all") plus those declared attach = "always"."""
    sp = specs(cfg, project)
    wanted = [w for x in (wanted or []) for w in x.split(",") if w]
    if "all" in wanted:
        return list(sp.values())
    for w in wanted:
        if w not in sp:
            raise DbError(t("db.err.unknown", name=w, project=project or "-", known=", ".join(sp) or "-"))
    return [s for n, s in sp.items() if n in wanted or s["attach"] == "always"]


def check_profile(cfg, repo_dir, profile, dbs):
    """A database only goes to jobs of the account its project belongs to: its `profile`, else the profile
    whose repo_match takes the repo. Raises DbError otherwise (no copies of one account's data elsewhere)."""
    owner = None
    for s in dbs:
        owner = s.get("profile") or owner or cfg.profile_for(repo_dir)
        if owner != profile:
            raise DbError(t("db.err.profile", name=s["name"], owner=owner, profile=profile))


def values(spec):
    e, n = spec["engine"], spec["name"]
    port = ENGINES[e][0]
    v = {"host": "127.0.0.1", "port": str(port), "user": USER, "password": PASSWORD, "database": spec["database"],
         "path": f"/var/lib/delega-db/{n}/{n}.sqlite"}
    v["url"] = {
        "postgres": "postgresql://{user}:{password}@{host}:{port}/{database}",
        "mariadb": "mysql://{user}:{password}@{host}:{port}/{database}",
        "mysql": "mysql://{user}:{password}@{host}:{port}/{database}",
        "redis": "redis://{host}:{port}/0",
        "mongo": "mongodb://{host}:{port}/{database}",
        "sqlite": "sqlite:///{path}",
    }[e].format(**v)
    return v


def env(dbs):
    """Variables the worker receives for these databases."""
    out = {}
    for s in dbs:
        v = values(s)
        for var, tmpl in s["env"].items():
            out[var] = str(tmpl).format(**v)
    return out


def kindling_node(cfg, profile, dbs, node_opt=None):
    """The kindling node a job with databases runs on: --node, the database's `node`, else the first
    kindling node of the profile."""
    from . import kindling
    if node_opt and node_opt != "auto":
        return node_opt
    for s in dbs:
        if s.get("node"):
            return s["node"]
    for name in profile.get("nodes") or []:
        if kindling.is_kindling(cfg.node(name)):
            return name
    for name in cfg.nodes:
        n = cfg.node(name)
        if kindling.is_kindling(n) and profile["name"] in n["profiles"]:
            return name
    raise DbError(t("db.err.no_kindling", profile=profile["name"]))


# ── local record of volumes and goldens (what the host has, as delega made it) ─
def state_path(cfg):
    return os.path.join(cfg.state_dir, "kindling.json")


def hkey(node):
    """Volumes, templates and sandboxes belong to the kindling host, whichever node (account) reaches it."""
    return remote.alias(remote.host_node(node))


def load_state(cfg):
    try:
        with open(state_path(cfg), encoding="utf-8") as f:
            st = json.load(f)
    except (OSError, ValueError):
        st = {}
    for sec in ("volumes", "goldens", "sandboxes"):
        st.setdefault(sec, {})
        for key in list(st[sec]):          # records from before: keyed by node name
            node, _, name = key.partition("/")
            if node in cfg.nodes and cfg.nodes[node].get("ssh") and cfg.nodes[node]["ssh"] != node:
                st[sec].setdefault(f"{cfg.nodes[node]['ssh']}/{name}", st[sec].pop(key))
    return st


def save_state(cfg, st):
    os.makedirs(cfg.state_dir, exist_ok=True)
    tmp = state_path(cfg) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=1, sort_keys=True)
    os.replace(tmp, state_path(cfg))


def note_golden(cfg, node, name, **fields):
    with audit.locked(state_path(cfg) + ".lock"):
        st = load_state(cfg)
        st["goldens"][f"{hkey(node)}/{name}"] = dict(fields, built=int(time.time()))
        save_state(cfg, st)


def data_golden(golden):
    return f"{golden}-db"


def plan(cfg, node, golden, dbs, mode=None):
    """(mode, image) for a job: "golden" + the data golden when it is fresh and holds exactly these
    databases, else "volume" + the plain golden (or the reason the golden mode was refused)."""
    mode = mode or (dbs[0]["mode"] if len({s["mode"] for s in dbs}) == 1 else "auto")
    if mode == "volume":
        return "volume", golden, ""
    st = load_state(cfg)
    dg = data_golden(golden)
    rec = st["goldens"].get(f"{hkey(node)}/{dg}")
    why = ""
    if not rec:
        why = t("db.why.no_golden", golden=dg)
    elif sorted(rec.get("dbs", {})) != sorted(s["name"] for s in dbs):
        why = t("db.why.other_dbs", golden=dg, dbs=", ".join(sorted(rec.get("dbs", {}))))
    else:
        for s in dbs:
            vol = st["volumes"].get(f"{hkey(node)}/{s['volume']}") or {}
            if vol.get("sha256") and vol.get("sha256") != rec["dbs"][s["name"]]:
                why = t("db.why.stale", golden=dg, volume=s["volume"])
        base = st["goldens"].get(f"{hkey(node)}/{golden}") or {}
        if not why and base.get("built", 0) > rec.get("built", 0):
            why = t("db.why.base_newer", golden=dg, base=golden)
    if not why and dg not in templates(node):
        why = t("db.why.no_golden", golden=dg)
    if why:
        if mode == "golden":
            raise DbError(why)
        return "volume", golden, why
    return "golden", dg, ""


# ── kling on the host ───────────────────────────────────────────────────────
def host(node, cmd, input=None, timeout=600):
    return remote.ssh(remote.host_node(node), cmd, input=input, timeout=timeout)


def _must(r, what):
    if r.returncode != 0:
        raise DbError(f"{what}: {(r.stderr or r.stdout or '').strip()[-300:]}")
    return r


def templates(node):
    r = host(node, "kling template ls -q", timeout=60)
    return set(r.stdout.split()) if r.returncode == 0 else set()


def volumes(node):
    """{name: {size, ...}} from `kling volume ls -json` (empty if the host does not answer)."""
    r = host(node, "kling volume ls -json", timeout=60)
    try:
        rows = json.loads(r.stdout or "[]")
    except ValueError:
        return {}
    return {v.get("name"): v for v in (rows or []) if isinstance(v, dict)}


def upload_dbctl(node, vm):
    with open(DBCTL, encoding="utf-8") as f:
        script = f.read()
    _must(host(node, f"kling cp - {q(vm + ':' + VM_DBCTL)} >/dev/null && kling exec {q(vm)} -- chmod 755 {VM_DBCTL}",
               input=script, timeout=120), "upload delega-db")


def vm_exec(node, vm, cmd, timeout=900):
    return host(node, f"kling exec -timeout {int(timeout)}s {q(vm)} -- {cmd}", timeout=timeout + 30)


def seed_vm(volume):
    return SEED_PREFIX + volume


def ensure_seed_server(node, volume):
    """The frozen read-only sandbox that serves this volume's parts (created on first use)."""
    sb, img = seed_vm(volume), node.get("loader_image", "node22")
    r = host(node, "kling sandbox ls 2>/dev/null; kling ps -a 2>/dev/null", timeout=60)
    if sb in r.stdout.split():
        return sb
    _must(host(node, f"kling sandbox create -name {q(sb)} -image {q(img)} -egress none -volume {q(volume + ':/seed:ro')} "
                     f"-ttl 2m -on-ttl freeze -q -wait-ready", timeout=300), f"seed sandbox for {volume}")
    return sb


def drop_seed_server(node, volume):
    host(node, f"kling sandbox rm -f {q(seed_vm(volume))} >/dev/null 2>&1; kling rm -f {q(seed_vm(volume))} >/dev/null 2>&1",
         timeout=120)


def copy_seed(node, spec, vm):
    """Copy the volume's parts into the job VM (/tmp/delega-seed/<name>), VM to VM through the host."""
    sb = ensure_seed_server(node, spec["volume"])
    dst = f"/tmp/delega-seed/{spec['name']}"
    # thaw: it freezes itself when idle (-ttl); a concurrent job may have woken it already
    cmd = (f"kling thaw {q(sb)} >/dev/null 2>&1; set -e; kling exec {q(vm)} -- mkdir -p {q(dst)}; "
           f"for p in $(kling exec {q(sb)} -- sh -c 'cd /seed && ls'); do case $p in lost+found) continue;; esac; "
           f"kling cp {q(sb)}:/seed/$p - | kling cp - {q(vm)}:{q(dst)}/$p >/dev/null; done")
    _must(host(node, cmd, timeout=3600), f"copy {spec['volume']}")
    return dst


def _last(out):
    return ((out or "").strip().splitlines() or [""])[-1]


def attach(cfg, node, vm, dbs, mode):
    """Bring every database up inside the job VM. Returns audit records (no data: counts only)."""
    upload_dbctl(node, vm)
    st = load_state(cfg)
    out = []
    for s in dbs:
        t0 = time.time()
        args = f"{q(s['engine'])} {q(s['name'])} {q(s['database'])}"
        if mode == "golden":
            r = vm_exec(node, vm, f"{VM_DBCTL} up {args}")
        else:
            src = copy_seed(node, s, vm)
            r = vm_exec(node, vm, f"sh -c {q(f'{VM_DBCTL} up {args} {src}; rc=$?; rm -rf {src}; exit $rc')}")
        _must(r, t("db.err.up", name=s["name"]))
        chk = _must(vm_exec(node, vm, f"{VM_DBCTL} check {args}", timeout=120), t("db.err.check", name=s["name"]))
        vol = st["volumes"].get(f"{hkey(node)}/{s['volume']}") or {}
        out.append({"name": s["name"], "engine": s["engine"], "volume": s["volume"], "mode": mode,
                    "sha": (vol.get("sha256") or "")[:12], "s": round(time.time() - t0, 1),
                    "check": _last(chk.stdout), "load": _last(r.stdout)})
    return out


# ── creating / refreshing a volume from a dump ──────────────────────────────
def _fmt_of(path, engine):
    for f in sorted(ENGINES[engine][1], key=len, reverse=True):
        if path.endswith("." + f):
            return f
    return None


def volume_load(cfg, node, spec, source, kind, fmt=None, size="4G", refresh=False):
    """Stream a dump into the spec's volume. `kind`: "host-file" | "host-cmd" (run on the kindling host:
    the dump never touches this machine) | "file" | "cmd" (here, piped through ssh stdin, never written).
    Returns the volume record {sha256, bytes, format, ...}."""
    e, vol = spec["engine"], spec["volume"]
    fmt = fmt or (_fmt_of(source, e) if kind in ("file", "host-file") else None) or ENGINES[e][1][0]
    if fmt not in ENGINES[e][1]:
        raise DbError(t("db.err.format", fmt=fmt, engine=e, known=", ".join(ENGINES[e][1])))
    exists = vol in volumes(node)
    if exists and not refresh:
        raise DbError(t("db.err.exists", volume=vol))
    loader = f"delega-load-{_slug(vol)}-{os.getpid()}"
    drop_seed_server(node, vol)                        # it holds the volume mounted read-only
    if exists:
        _must(host(node, f"kling volume snapshot {q(vol)} before-refresh-{time.strftime('%Y%m%d%H%M%S')}"),
              "volume snapshot")
    else:
        _must(host(node, f"kling volume create {q(vol)} -size {q(size)}"), "volume create")
    try:
        _must(host(node, f"kling run -name {q(loader)} -image {q(node.get('loader_image', 'node22'))} -egress none "
                         f"-allow-exec -volume {q(vol + ':/seed')} -wait-ready >/dev/null", timeout=300), "loader VM")
        _must(host(node, f"kling exec {q(loader)} -- sh -c 'rm -rf /seed/dump.* /seed/MANIFEST'"), "empty volume")
        feed = {"host-file": f"cat {q(source)}", "host-cmd": source}.get(kind, "cat")
        split = (f"split -b {CHUNK} -a 3 --filter={q(f'kling cp - {loader}:/seed/dump.{fmt}.part-$' + '{FILE#x} >/dev/null')} - x")
        stream = (f"d=$(mktemp -d); trap 'rm -rf \"$d\"' EXIT; cd \"$d\"; set -o pipefail; "
                  f"{feed} | tee >(sha256sum | cut -c1-64 > sha) >(wc -c > bytes) | {split} && for i in $(seq 100); do [ -s sha ] && [ -s bytes ] && break; sleep 0.1; done; "
                  f"echo \"$(cat sha) $(tr -d ' ' < bytes)\"")
        if kind == "cmd":
            p = subprocess.Popen(source, shell=True, stdout=subprocess.PIPE)
            r = _stream_ssh(node, stream, p.stdout)
            if p.wait() != 0:
                raise DbError(t("db.err.source_rc", rc=p.returncode))
        elif kind == "file":
            with open(source, "rb") as f:
                r = _stream_ssh(node, stream, f)
        else:
            r = host(node, f"bash -c {q(stream)}", timeout=6 * 3600)
        _must(r, "stream dump")
        sha, nbytes = (r.stdout.strip().splitlines() or [" "])[-1].split()[:2]
        if int(nbytes) == 0:
            raise DbError(t("db.err.empty"))
        manifest = (f"ENGINE={e}\nVERSION={spec['version']}\nFORMAT={fmt}\nSHA256={sha}\nBYTES={nbytes}\n"
                    f"CREATED={time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\nSOURCE={kind}\n")
        _must(host(node, f"kling cp - {q(loader + ':/seed/MANIFEST')} >/dev/null", input=manifest), "manifest")
        got = _must(host(node, f"kling exec {q(loader)} -- sh -c 'cat /seed/dump.*.part-* | sha256sum'", timeout=1800),
                    "verify").stdout.split()[0]
        if got != sha:
            raise DbError(t("db.err.verify", volume=vol))
    finally:
        host(node, f"kling rm -f {q(loader)} >/dev/null 2>&1", timeout=120)
    rec = {"sha256": sha, "bytes": int(nbytes), "format": fmt, "engine": e, "source": kind,
           "updated": int(time.time()), "project_db": spec["name"]}
    with audit.locked(state_path(cfg) + ".lock"):
        st = load_state(cfg)
        st["volumes"][f"{hkey(node)}/{vol}"] = rec
        save_state(cfg, st)
    return rec


def _stream_ssh(node, cmd, stdin):
    argv = remote._ssh_prefix() + remote.SSH_OPTS + [remote.alias(remote.host_node(node)), f"bash -c {q(cmd)}"]
    p = subprocess.run(argv, stdin=stdin, capture_output=True)
    return subprocess.CompletedProcess(argv, p.returncode, p.stdout.decode(errors="replace"),
                                       p.stderr.decode(errors="replace"))


def bake(cfg, node, golden, dbs):
    """The data golden `<golden>-db`: a clone of the golden with these databases loaded, frozen running."""
    dg, vm = data_golden(golden), f"delega-bake-{os.getpid()}"
    t0 = time.time()
    _must(host(node, f"kling run -name {q(vm)} -from {q(golden)} -egress none -wait-ready >/dev/null", timeout=300),
          f"kling run -from {golden}")
    try:
        recs = attach(cfg, node, vm, dbs, "volume")
        vm_exec(node, vm, "sync", timeout=120)
        _must(host(node, f"kling save -replace {q(vm)} {q(dg)}", timeout=1800), f"kling save {dg}")
    finally:
        host(node, f"kling rm -f {q(vm)} >/dev/null 2>&1", timeout=120)
    st = load_state(cfg)
    shas = {s["name"]: (st["volumes"].get(f"{hkey(node)}/{s['volume']}") or {}).get("sha256", "") for s in dbs}
    note_golden(cfg, node, dg, base=golden, dbs=shas, volumes={s["name"]: s["volume"] for s in dbs},
                s=round(time.time() - t0, 1))
    return dg, recs


# ── CLI: delega db … ────────────────────────────────────────────────────────
def _ctx(args):
    cfg = _config.load()
    repo = os.path.abspath(args.repo or ".")
    project = args.project or cfg.project_for(repo)
    pname = getattr(args, "profile", None) or os.environ.get("DELEGA_PROFILE") or cfg.profile_for(repo)
    profile = cfg.profile(pname)
    return cfg, repo, project, profile


def _spec_for(cfg, project, args):
    sp = specs(cfg, project)
    if args.name in sp:
        return sp[args.name]
    if not args.engine:
        raise DbError(t("db.err.unknown", name=args.name, project=project or "-", known=", ".join(sp) or "-"))
    return {"name": args.name, "engine": args.engine, "version": args.version or "", "database": args.name,
            "volume": args.name, "attach": "ask", "mode": "auto", "env": DEFAULT_ENV[args.engine], "node": None,
            "profile": None}


def main_db(argv):
    ap = argparse.ArgumentParser(prog="delega db", description=t("db.help"))
    common = util.common_options(ap, ["repo", "project", "profile", "node"])
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("ls", parents=[common])
    v = sub.add_parser("volume", parents=[common])
    vs = v.add_subparsers(dest="vcmd")
    for name in ("create", "refresh"):
        c = vs.add_parser(name, parents=[common])
        c.add_argument("name", help="project database (or a bare volume name with --engine)")
        src = c.add_mutually_exclusive_group(required=True)
        src.add_argument("--from-host", metavar="PATH", help="dump file on the kindling host (never comes here)")
        src.add_argument("--from-host-cmd", metavar="CMD", help="command run on the kindling host that prints the dump")
        src.add_argument("--from-file", metavar="PATH", help="dump file here: streamed through ssh")
        src.add_argument("--from-cmd", metavar="CMD", help="command here that prints the dump: streamed, never written")
        c.add_argument("--format")
        c.add_argument("--engine", choices=sorted(ENGINES))
        c.add_argument("--version")
        c.add_argument("--size", default="4G")
    vl = vs.add_parser("ls", parents=[common])
    vl.set_defaults(vcmd="ls")
    r = vs.add_parser("rm", parents=[common])
    r.add_argument("name")
    r.add_argument("--yes", action="store_true")
    a = ap.parse_args(argv)
    try:
        cfg, repo, project, profile = _ctx(a)
        if a.cmd in (None, "ls") or (a.cmd == "volume" and a.vcmd in (None, "ls")):
            return _ls(cfg, project, profile, a.node)
        if a.cmd == "volume" and a.vcmd in ("create", "refresh"):
            spec = _spec_for(cfg, project, a)
            node = cfg.node(kindling_node(cfg, profile, [spec], a.node))
            kind, source = next((k, v) for k, v in (("host-file", a.from_host), ("host-cmd", a.from_host_cmd),
                                                     ("file", a.from_file), ("cmd", a.from_cmd)) if v)
            print(t("db.loading", volume=spec["volume"], node=node["name"], kind=kind))
            t0 = time.time()
            rec = volume_load(cfg, node, spec, source, kind, a.format, a.size, refresh=a.vcmd == "refresh")
            audit.record(cfg, "db_volume", action=a.vcmd, node=node["name"], volume=spec["volume"], project=project,
                         engine=spec["engine"], sha=rec["sha256"][:12], bytes=rec["bytes"], source=kind)
            print(t("db.loaded", volume=spec["volume"], mb=f"{rec['bytes'] / 1e6:.1f}", sha=rec["sha256"][:12],
                    s=int(time.time() - t0)))
            return 0
        if a.cmd == "volume" and a.vcmd == "rm":
            spec = _spec_for(cfg, project, argparse.Namespace(name=a.name, engine="postgres", version=""))
            node = cfg.node(kindling_node(cfg, profile, [spec], a.node))
            if not a.yes:
                print(t("db.rm_confirm", volume=spec["volume"], node=node["name"]), file=sys.stderr)
                return 2
            drop_seed_server(node, spec["volume"])
            _must(host(node, f"kling volume rm -f -snapshots {q(spec['volume'])}"), "volume rm")
            with audit.locked(state_path(cfg) + ".lock"):
                st = load_state(cfg)
                st["volumes"].pop(f"{hkey(node)}/{spec['volume']}", None)
                save_state(cfg, st)
            audit.record(cfg, "db_volume", action="rm", node=node["name"], volume=spec["volume"], project=project)
            return 0
    except (DbError, _config.ConfigError, remote.RemoteError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 5
    ap.print_help()
    return 2


def _ls(cfg, project, profile, node_opt):
    sp = specs(cfg, project)
    st = load_state(cfg)
    try:
        node = cfg.node(kindling_node(cfg, profile, list(sp.values()), node_opt))
    except DbError as e:
        print(e)
        return 0
    have = volumes(node)
    print(t("db.ls_head", node=node["name"], project=project or "-"))
    names = {s["volume"]: s for s in sp.values()}
    for vol in sorted(set(names) | {v for v in have if v.startswith("db-")}):
        s = names.get(vol)
        rec = st["volumes"].get(f"{hkey(node)}/{vol}") or {}
        print(f"  {(s or {}).get('name', '-'):<12} {(s or {}).get('engine', rec.get('engine', '?')):<9} {vol:<28} "
              f"{'✓' if vol in have else '✗'} {rec.get('sha256', '')[:12]:<12} "
              f"{(str(round(rec['bytes'] / 1e6, 1)) + ' MB') if rec.get('bytes') else '':<10} "
              f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(rec['updated'])) if rec.get('updated') else ''}")
    return 0

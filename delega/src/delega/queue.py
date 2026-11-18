"""queue + tick: hold briefs until there is room, then launch them in the background.

queue.jsonl lives in the state dir; every read-modify-write holds an flock on queue.jsonl.lock.
`tick` = reconcile jobs, then start queued items (highest priority first, oldest first) while the
profile's quota, the day's budget and the nodes' capacity allow."""
import json
import os
import subprocess
import sys
import time

from . import audit, config, i18n, util

LAUNCH_GRACE_S = 3        # a launch that dies within this time (refused: budget, identity…) is "failed"


def qpath(cfg):
    return os.path.join(cfg.state_dir, "queue.jsonl")


def read_items(cfg):
    items = []
    try:
        with open(qpath(cfg), encoding="utf-8") as f:
            for line in f:
                try:
                    items.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return items


def write_items(cfg, items):
    tmp = qpath(cfg) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for it in items:
            f.write(json.dumps(it, ensure_ascii=False) + "\n")
    os.replace(tmp, qpath(cfg))


def lock(cfg):
    return audit.locked(qpath(cfg) + ".lock")


def split_run_args(args):
    """Options after `--` for `delega run`; returns (explicit profile or None)."""
    for i, a in enumerate(args):
        if a == "--profile" and i + 1 < len(args):
            return args[i + 1]
    return None


def add(cfg, repo, brief, prio=0, run_args=()):
    repo, brief = os.path.abspath(repo), os.path.abspath(brief)
    if not os.path.isdir(repo):
        raise FileNotFoundError(repo)
    qid = f"{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}"
    held = os.path.join(cfg.state_dir, "queue")
    os.makedirs(held, exist_ok=True)
    with open(brief, encoding="utf-8") as src, open(os.path.join(held, qid + ".md"), "w", encoding="utf-8") as dst:
        dst.write(src.read())                       # the brief may be edited or deleted before it runs
    item = {"id": qid, "ts": time.time(), "prio": int(prio), "repo": repo, "brief": os.path.join(held, qid + ".md"),
            "args": list(run_args), "state": "queued"}
    with lock(cfg):
        write_items(cfg, read_items(cfg) + [item])
    return item


def add_once(cfg, repo, brief, prio=0, run_args=()):
    """add(), unless the same brief for the same repo and options is already queued: (item, new?).
    A caller that relaunches in a loop while the node is full ends up with one item, not one per try."""
    repo_abs = os.path.abspath(repo)
    with open(brief, encoding="utf-8") as f:
        text = f.read()
    for it in pending(read_items(cfg)):
        if it["repo"] != repo_abs or it.get("args") != list(run_args):
            continue
        try:
            with open(it["brief"], encoding="utf-8") as f:
                if f.read() == text:
                    return it, False
        except OSError:
            pass
    return add(cfg, repo, brief, prio, run_args), True


def pending(items):
    return sorted((i for i in items if i.get("state") == "queued"), key=lambda i: (-i.get("prio", 0), i.get("ts", 0)))


def launch(cfg, item):
    """Start `delega run` in the background. Returns ("launched"|"failed", detail)."""
    log = os.path.join(cfg.state_dir, "queue", item["id"] + ".log")
    env = dict(os.environ)
    root = os.path.dirname(util.PKG_DIR)            # checkout src/ or the .pyz itself
    env["PYTHONPATH"] = root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["DELEGA_FROM_QUEUE"] = "1"                  # a full node fails back here instead of queueing again
    with open(log, "ab") as out:
        p = subprocess.Popen([sys.executable, "-m", "delega", "run", item["repo"], item["brief"], *item["args"]],
                             stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                             start_new_session=True, env=env, cwd=item["repo"])
    item["pid"], item["log"] = p.pid, log
    try:
        rc = p.wait(timeout=LAUNCH_GRACE_S)
    except subprocess.TimeoutExpired:
        return "launched", str(p.pid)
    if rc == 0:
        return "launched", str(p.pid)
    from .launch import FULL_RC
    return ("full", "") if rc == FULL_RC else ("failed", f"rc={rc}")


def room(cfg, profile_name, today, cache):
    """(ok, reason): the profile may start one more worker now."""
    from . import quota, spend
    if profile_name not in cache:
        cache[profile_name] = quota.capacity(cfg, profile_name)
    cap = cache[profile_name]
    if cap["has_data"] and cap["new"] <= 0:
        return False, "quota"
    if not cap["has_data"] and cap["live"] >= cap["slots"]:
        return False, "capacity"
    prof = cfg.profile(profile_name)
    if spend.total(cfg, today, profile=profile_name) >= float(cfg.policy("budget.day_usd", prof)):
        return False, "budget"
    return True, ""


def main_queue(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(i18n.t("queue.usage"))
        return 0 if argv else 2
    try:
        cfg = config.load()
    except config.ConfigError as e:
        print(e, file=sys.stderr)
        return 2
    sub, rest = argv[0], argv[1:]
    if sub == "ls":
        items = read_items(cfg)
        for it in sorted(items, key=lambda i: (i.get("state") != "queued", -i.get("prio", 0), i.get("ts", 0))):
            print(f"{it['id']}  {it.get('state', '?'):<9} prio={it.get('prio', 0)}  {os.path.basename(it['repo'])}"
                  f"  {util.short(' '.join(it.get('args', [])), 50)}")
        if not items:
            print(i18n.t("queue.empty"))
        return 0
    if sub == "add":
        run_args = []
        if "--" in rest:
            i = rest.index("--")
            rest, run_args = rest[:i], rest[i + 1:]
        prio, pos = 0, []
        while rest:
            x = rest.pop(0)
            if x == "--prio" and rest:
                prio = rest.pop(0)
            else:
                pos.append(x)
        if len(pos) != 2:
            print(i18n.t("queue.usage"), file=sys.stderr)
            return 2
        try:
            item = add(cfg, pos[0], pos[1], prio, run_args)
        except (OSError, ValueError) as e:
            print(i18n.t("queue.add_failed", err=e), file=sys.stderr)
            return 2
        print(i18n.t("queue.added", id=item["id"], prio=item["prio"]))
        return 0
    print(i18n.t("queue.usage"), file=sys.stderr)
    return 2


def _reconcile(cfg, adopt):
    from . import jobs
    try:
        actions = jobs.reconcile(cfg) or []
    except (AttributeError, NotImplementedError):
        actions = []
    for job_dir, action in actions:
        print(i18n.t("tick.reconciled", job=os.path.basename(job_dir), action=action))
        if adopt and "orphan" in str(action):
            try:
                jobs.adopt(cfg, job_dir)
                print(i18n.t("tick.adopted", job=os.path.basename(job_dir)))
            except (AttributeError, NotImplementedError, OSError) as e:
                print(i18n.t("tick.adopt_failed", job=os.path.basename(job_dir), err=e), file=sys.stderr)


def launch_round(cfg, items, on_result, keep_going=None):
    """Start the (ordered) items while the profiles' quota, budget and nodes have room.
    on_result(item, state, detail) is called for every launch tried; returns how many started."""
    started, today, cache, blocked = 0, time.strftime("%Y%m%d"), {}, set()
    for it in items:
        if keep_going is not None and not keep_going():
            break
        try:
            name = cfg.profile(split_run_args(it["args"]) or cfg.profile_for(it["repo"]))["name"]
            ok, why = (False, "profile") if name in blocked else room(cfg, name, today, cache)
        except config.ConfigError as e:
            print(i18n.t("tick.bad_item", id=it["id"], err=e), file=sys.stderr)
            continue
        if not ok:
            blocked.add(name)
            print(i18n.t("tick.wait", id=it["id"], profile=name, why=why))
            continue
        state, detail = launch(cfg, it)
        if state == "full":               # room() said yes but the node had none: keep it queued
            blocked.add(name)
            print(i18n.t("tick.wait", id=it["id"], profile=name, why="node full"))
            continue
        on_result(it, state, detail)
        started += 1 if state == "launched" else 0
        if state == "launched" and cache[name]["has_data"]:
            cache[name]["new"] -= 1
        elif state == "launched":
            cache[name]["live"] += 1
        print(i18n.t("tick." + state, id=it["id"], detail=detail, log=it.get("log", "")))
    return started


def main_tick(argv):
    if "--daemon" in argv or "--once" in argv:
        return main_daemon(argv)
    adopt = "--adopt" in argv
    try:
        cfg = config.load()
    except config.ConfigError as e:
        print(e, file=sys.stderr)
        return 2
    _reconcile(cfg, adopt)

    def mark(it, state, detail):
        it["state"], it["started"] = state, time.time()

    with lock(cfg):
        items = read_items(cfg)
        started = launch_round(cfg, pending(items), mark)
        write_items(cfg, items)
    print(i18n.t("tick.summary", n=started, left=len(pending(read_items(cfg)))))
    return 0


# ── daemon: only the owner of the org's lease acts ─────────────────────────────────────────────
ROLE_PREFIX = "orquestador:"
LEASE_TTL_S = 90.0
INTERVAL_S = 30.0


class MemClient:
    """The org's memory database: opened directly when [memoria.<org>] is this machine, through
    `ssh host delega mem … --json` when it is not. Every call is one short transaction."""

    def __init__(self, cfg, org, owner):
        self.org, self.owner, self.m = org, owner, cfg.memoria_for(org)
        self.role = ROLE_PREFIX + org

    # local ----------------------------------------------------------------------------------
    def _conn(self):
        from . import mem
        return mem.connect(mem.db_path(self.m["path"] or None))

    def _local(self, fn):
        conn = self._conn()
        try:
            return fn(conn)
        finally:
            conn.close()

    # remote ---------------------------------------------------------------------------------
    def _ssh(self, *args):
        """(rc, payload) of `delega mem <args> --json` on the host; rc 255 = ssh itself failed."""
        import shlex
        cmd = f"DELEGA_MEM_OWNER={shlex.quote(self.owner)} delega mem " + " ".join(shlex.quote(a) for a in args)
        if self.m["path"]:
            cmd += f" --db {shlex.quote(self.m['path'])}"
        r = subprocess.run(["ssh", "-o", "BatchMode=yes", self.m["host"], cmd + " --json"],
                           capture_output=True, text=True, timeout=60)
        payload = {}
        for line in reversed(r.stdout.strip().splitlines()):
            try:
                payload = json.loads(line)
                break
            except ValueError:
                continue
        return r.returncode, payload

    def _remote(self, *args):
        from . import mem
        rc, payload = self._ssh(*args)
        if rc not in (0, mem.EXIT_LEASE):
            raise mem.MemError(payload.get("error") or f"ssh {self.m['host']} delega mem {args[0]}: rc={rc}")
        return rc, payload

    # operations -----------------------------------------------------------------------------
    def claim(self, ttl):
        """True when this owner holds the lease after the call (taken or renewed)."""
        from . import mem
        if self.m["local"]:
            return self._local(lambda c: mem.claim_lease(c, self.role, ttl, owner=self.owner))["estado"] != "ocupada"
        return self._remote("claim", self.role, "--ttl", str(ttl))[0] == mem.EXIT_OK

    def release(self):
        from . import mem
        if self.m["local"]:
            self._local(lambda c: mem.release_lease(c, self.role, owner=self.owner))
        else:
            self._remote("release", self.role)

    def flows(self):
        from . import mem
        if self.m["local"]:
            return self._local(mem.list_flows)
        return self._remote("flows")[1].get("flujos", [])

    def ready(self, flow):
        from . import mem
        if self.m["local"]:
            return self._local(lambda c: mem.next_tasks(c, flow))[0]
        return self._remote("next", flow)[1].get("tareas", [])

    def set(self, task, estado, error=None):
        from . import mem
        if self.m["local"]:
            return self._local(lambda c: mem.set_task(c, task, estado, error=error))
        extra = ["--error", error] if error else []
        return self._remote("set", task, "--estado", estado, *extra)[1]

    def add_flow(self, repo, flow_id, branch=None):
        from . import mem
        if self.m["local"]:
            return self._local(lambda c: mem.add_flow(c, repo, branch=branch, flow_id=flow_id, org=self.org))
        extra = ["--branch", branch] if branch else []
        return self._remote("add-flow", "--repo", repo, "--id", flow_id, *extra)[1]

    def add_task(self, flow, brief, task_id, run_args):
        from . import mem
        if self.m["local"]:
            return self._local(lambda c: mem.add_task(c, flow, brief, task_id=task_id, run_args=run_args,
                                                      org=self.org))
        return self._remote("add-task", "--flow", flow, "--brief", brief, "--id", task_id,
                            "--run-args", json.dumps(run_args))[1]


def args_to_dict(args):
    """`--k v --flag` → {"k": "v", "flag": True} (the run options a task keeps as JSON)."""
    out, i = {}, 0
    while i < len(args):
        a = args[i]
        if a.startswith("--"):
            if i + 1 < len(args) and not args[i + 1].startswith("--"):
                out[a[2:]] = args[i + 1]
                i += 1
            else:
                out[a[2:]] = True
        i += 1
    return out


def dict_to_args(d):
    out = []
    for k, v in (d or {}).items():
        if k.startswith("_") or v in (False, None):
            continue
        for x in (v if isinstance(v, list) else [v]):
            out += ["--" + k] + ([] if x is True else [str(x)])
    return out


def _checkout(m, remote, hint=None):
    """Local directory of a flow's remote on this machine: [memoria.<org>].repos, else the hint."""
    import re
    for key, path in (m.get("repos") or {}).items():
        if re.search(r"[:/]" + re.escape(key) + r"(\.git)?$", remote.strip()) or key == remote:
            path = os.path.expanduser(path)
            if os.path.isdir(path):
                return path
    return hint if hint and os.path.isdir(hint) else None


def migrate_legacy(cfg, client):
    """Copy the still-queued items of queue.jsonl into the database (queue.jsonl is only read).
    Idempotent: the task id derives from the queue id, an existing one is skipped."""
    import hashlib
    from . import mem
    n = 0
    for it in pending(read_items(cfg)):
        tid = "q-" + it["id"]
        try:
            remote = config._git(it["repo"], "remote", "get-url", "origin")
            if not remote:
                raise mem.MemError("repo without an origin remote")
            with open(it["brief"], encoding="utf-8") as f:
                brief = f.read()
            run_args = args_to_dict(it.get("args", []))
            run_args.setdefault("profile", cfg.profile_for(it["repo"]))
            run_args["_checkout"] = it["repo"]
            fid = "mig-" + hashlib.sha1(remote.encode()).hexdigest()[:8]
            if fid not in {f["id"] for f in client.flows()}:
                client.add_flow(remote, fid)
            client.add_task(fid, brief, tid, run_args)
            n += 1
            print(i18n.t("tick.migrated", id=it["id"], task=tid))
        except mem.MemError as e:
            if str(e) == i18n.t("mem.task_exists", id=tid):      # migrated by an earlier pass
                continue
            print(i18n.t("tick.migrate_skip", id=it["id"], err=e), file=sys.stderr)
        except OSError as e:
            print(i18n.t("tick.migrate_skip", id=it["id"], err=e), file=sys.stderr)
    return n


def daemon_cycle(cfg, client, ttl=LEASE_TTL_S, adopt=False):
    """One pass: take or renew the lease; without it do nothing. Returns True when it acted."""
    if not client.claim(ttl):
        print(i18n.t("tick.not_owner", role=client.role))
        return False
    _reconcile(cfg, adopt)
    migrate_legacy(cfg, client)
    items, tasks = [], {}
    os.makedirs(os.path.join(cfg.state_dir, "queue"), exist_ok=True)
    for fl in client.flows():
        if fl.get("estado") != "activo":
            continue
        for t in client.ready(fl["id"]):
            ra = t.get("run_args") or {}
            repo = _checkout(client.m, fl["repo"], ra.get("_checkout"))
            if not repo:
                print(i18n.t("tick.no_checkout", id=t["id"], repo=fl["repo"]), file=sys.stderr)
                continue
            brief = os.path.join(cfg.state_dir, "queue", t["id"] + ".md")
            with open(brief, "w", encoding="utf-8") as f:
                f.write(t["brief"])
            it = {"id": t["id"], "repo": repo, "brief": brief, "args": dict_to_args(ra)}
            items.append(it)
            tasks[t["id"]] = it

    def mark(it, state, detail):
        client.set(it["id"], "lanzada" if state == "launched" else "rojo",
                   error=None if state == "launched" else detail)

    started = launch_round(cfg, items, mark, keep_going=lambda: client.claim(ttl))
    print(i18n.t("tick.summary", n=started, left=len(items) - started))
    return True


def main_daemon(argv):
    import signal
    import socket
    import threading
    opts, i = {"interval": INTERVAL_S, "lease-ttl": LEASE_TTL_S}, 0
    while i < len(argv):
        a = argv[i]
        if a.startswith("--") and a[2:] in ("interval", "lease-ttl", "org", "owner") and i + 1 < len(argv):
            opts[a[2:]] = argv[i + 1]
            i += 1
        i += 1
    try:
        cfg = config.load()
        org = cfg.resolve_org(opts.get("org"))
        interval, ttl = float(opts["interval"]), float(opts["lease-ttl"])
        client = MemClient(cfg, org, opts.get("owner") or f"{socket.gethostname()}:tick")
    except (config.ConfigError, ValueError) as e:
        print(e, file=sys.stderr)
        return 2
    stop = threading.Event()
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: stop.set())
    once, adopt = "--once" in argv, "--adopt" in argv
    print(i18n.t("tick.daemon_start", org=org, owner=client.owner, interval=int(interval)), flush=True)
    while not stop.is_set():
        try:
            daemon_cycle(cfg, client, ttl, adopt)
        except Exception as e:                    # the db or ssh is down: try again next time
            print(i18n.t("tick.daemon_error", err=e), file=sys.stderr, flush=True)
        if once:
            break
        stop.wait(interval)
    if not once:
        try:
            client.release()
        except Exception:
            pass
    return 0


# ── delega mem install-daemon ────────────────────────────────────────────────────────────────
def daemon_files(argv_tail, owner=None):
    """{name: text}: the systemd --user unit and the launchd plist that run `delega tick --daemon`."""
    root = os.path.dirname(util.PKG_DIR)
    cmd = [sys.executable, "-m", "delega", "tick", "--daemon", *argv_tail]
    unit = ("[Unit]\nDescription=delega orchestrator daemon (acts only with the org lease)\n"
            "After=network-online.target\n\n[Service]\n"
            f"Environment=PYTHONPATH={root}\n" + (f"Environment=DELEGA_MEM_OWNER={owner}\n" if owner else "")
            + "ExecStart=" + " ".join(cmd) + "\nRestart=always\nRestartSec=10\n\n"
            "[Install]\nWantedBy=default.target\n")
    args = "".join(f"    <string>{a}</string>\n" for a in cmd)
    plist = ('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
             '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n<plist version="1.0">\n<dict>\n'
             '  <key>Label</key><string>com.delega.tick</string>\n  <key>ProgramArguments</key>\n  <array>\n'
             f'{args}  </array>\n  <key>EnvironmentVariables</key>\n  <dict>\n'
             f'    <key>PYTHONPATH</key><string>{root}</string>\n  </dict>\n'
             '  <key>RunAtLoad</key><true/>\n  <key>KeepAlive</key><true/>\n</dict>\n</plist>\n')
    return {"delega-tick.service": unit, "com.delega.tick.plist": plist}


def cmd_install_daemon(args, db, as_json):
    """`delega mem install-daemon [--org o] [--owner n] [--interval s] [--yes] [--out dir]`:
    prints the unit and the plist; writes them (never enables them) only with --yes."""
    tail, yes, out, owner, i = [], False, None, None, 0
    while i < len(args):
        a = args[i]
        if a == "--yes":
            yes = True
        elif a in ("--org", "--owner", "--interval", "--lease-ttl", "--out") and i + 1 < len(args):
            if a == "--out":
                out = args[i + 1]
            else:
                tail += [a, args[i + 1]]
                owner = args[i + 1] if a == "--owner" else owner
            i += 1
        else:
            print(i18n.t("tick.install_usage"), file=sys.stderr)
            return 2
        i += 1
    files = daemon_files(tail, owner)
    home = os.path.expanduser("~")
    targets = {"delega-tick.service": os.path.join(home, ".config", "systemd", "user"),
               "com.delega.tick.plist": os.path.join(home, "Library", "LaunchAgents")}
    if out:
        targets = {k: os.path.abspath(os.path.expanduser(out)) for k in files}
    written = []
    for name, text in files.items():
        path = os.path.join(targets[name], name)
        if yes:
            os.makedirs(targets[name], exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            written.append(path)
        if not as_json:
            print(f"# {path}\n{text}")
    if as_json:
        print(json.dumps({"ok": True, "cmd": "install-daemon", "instalado": yes, "ficheros": {
            n: {"ruta": os.path.join(targets[n], n), "texto": t} for n, t in files.items()}}, ensure_ascii=False))
    elif yes:
        print(i18n.t("tick.install_done"))
    else:
        print(i18n.t("tick.install_dry"))
    return 0


main = main_queue

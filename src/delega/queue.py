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


def pending(items):
    return sorted((i for i in items if i.get("state") == "queued"), key=lambda i: (-i.get("prio", 0), i.get("ts", 0)))


def launch(cfg, item):
    """Start `delega run` in the background. Returns ("launched"|"failed", detail)."""
    log = os.path.join(cfg.state_dir, "queue", item["id"] + ".log")
    env = dict(os.environ)
    root = os.path.dirname(util.PKG_DIR)            # checkout src/ or the .pyz itself
    env["PYTHONPATH"] = root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    with open(log, "ab") as out:
        p = subprocess.Popen([sys.executable, "-m", "delega", "run", item["repo"], item["brief"], *item["args"]],
                             stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                             start_new_session=True, env=env, cwd=item["repo"])
    item["pid"], item["log"] = p.pid, log
    try:
        rc = p.wait(timeout=LAUNCH_GRACE_S)
    except subprocess.TimeoutExpired:
        return "launched", str(p.pid)
    return ("launched", str(p.pid)) if rc == 0 else ("failed", f"rc={rc}")


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


def main_tick(argv):
    adopt = "--adopt" in argv
    try:
        cfg = config.load()
    except config.ConfigError as e:
        print(e, file=sys.stderr)
        return 2
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
    started, today, cache, blocked = 0, time.strftime("%Y%m%d"), {}, set()
    with lock(cfg):
        items = read_items(cfg)
        for it in pending(items):
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
            it["state"], it["started"] = state, time.time()
            started += 1 if state == "launched" else 0
            if state == "launched" and cache[name]["has_data"]:
                cache[name]["new"] -= 1
            elif state == "launched":
                cache[name]["live"] += 1
            print(i18n.t("tick." + state, id=it["id"], detail=detail, log=it.get("log", "")))
        write_items(cfg, items)
    print(i18n.t("tick.summary", n=started, left=len(pending(read_items(cfg)))))
    return 0


main = main_queue

"""relevo: let a node of the org take over the orchestration when the Mac is down.

Three pieces, all called from elsewhere (`queue.py`, `sync.py`); nothing here touches `launch.py`:

* `derive_config(cfg, profile)`: the orchestrator config a node needs, derived from the Mac's:
  the profile, its nodes, `[memoria.<org>]` and the policy. Never the keychain `token_command`, never
  anything of another profile. `[relevo] node = "<name>"` marks the config as the one of a node.
* `origin(cfg, remote)`: when the daemon runs on a node, the repo to launch from is the node's bare
  `~/<workdir>/<repo>.git` (created with `git clone --bare` when missing), not a Mac checkout.
* `escalate(cfg, client)`: the second `rojo` of a task (or a `necesita_decision` event) makes the
  daemon that owns the lease block the task and ask ONE Claude orchestrator (`claude -p`, Sonnet, capped)
  to decide. It never merges to main and never pushes. The request is an event `orquestador_claude`
  in the database, written once per task (`mem event --once`), so a second request is never created.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time

from . import config, i18n

REQUEST = "orquestador_claude"
NEEDS_DECISION = "necesita_decision"
SPAWN_FAILED = "escalado_fallido"
MAX_SPAWN_TRIES = 3
MAX_REDS = 2
MODEL = "sonnet"
CAP_USD = 3
EVENT_TAIL = 50
FORBIDDEN_TOOLS = ("Bash(git *)", "Bash(gh *)", "Edit", "Write")
# what guarantees that the escalated Claude never pushes or merges (see `claude_argv`):
# the closed allow list below; the deny list only adds a second lock.
ALLOWED_TOOLS = ("Read", "Grep", "Glob", "Bash(delega mem *)")
STRIPPED_ENV = ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN", "SSH_AUTH_SOCK")
# profile keys that make sense on a node (paths and secrets of the Mac are left out)
PROFILE_KEYS = ("owner", "org", "model", "launcher", "nodes", "identity", "git", "policy", "roles", "repo_match")
NODE_DROP = ("token_command", "account")
SECRET_RE = re.compile(r"find-generic-password|token_command|\.credentials|BEGIN [A-Z ]*PRIVATE KEY", re.I)


# ── derived config ──────────────────────────────────────────────────────────────────────────────
def _toml_key(k):
    return k if re.fullmatch(r"[A-Za-z0-9_-]+", k) else json.dumps(k)


def _toml_value(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{_toml_key(k)} = {_toml_value(x)}" for k, x in v.items()) + " }"
    return json.dumps(str(v), ensure_ascii=False)


def dump_toml(data, path=()):
    """Tiny TOML writer for what a delega config holds (tables, scalars, lists, inline tables)."""
    scalars = {k: v for k, v in data.items() if not isinstance(v, dict) or not v}
    tables = {k: v for k, v in data.items() if isinstance(v, dict) and v}
    out = []
    if path and (scalars or not tables):
        out.append("[" + ".".join(_toml_key(p) for p in path) + "]")
    out += [f"{_toml_key(k)} = {_toml_value(v)}" for k, v in scalars.items()]
    text = "\n".join(out) + ("\n" if out else "")
    for k, v in tables.items():
        text += ("\n" if text else "") + dump_toml(v, path + (k,))
    return text


def _scrub_node(node, profile):
    n = {k: v for k, v in node.items() if k not in NODE_DROP and k not in ("name", "profiles")}
    if n.get("config_dirs"):
        n["config_dirs"] = {k: v for k, v in n["config_dirs"].items() if k == profile}
    n["profiles"] = [profile]
    return n


def derive_config(cfg, profile, node_name=None):
    """(toml text, data): the orchestrator config for `node_name`, derived from `cfg` for one profile.
    Raises config.ConfigError when the result would still mention another profile or a secret."""
    prof = cfg.profile(profile)
    name, org = prof["name"], cfg.profile_org(prof["name"])
    data = {"general": {"locale": cfg.general.get("locale", "en"), "owner_name": cfg.owner_name,
                        "default_profile": name}}
    if cfg.raw.get("policy"):
        data["policy"] = dict(cfg.raw["policy"])
    p = {k: prof[k] for k in PROFILE_KEYS if k in prof}
    p["nodes"] = list(prof.get("nodes") or [])
    data["profiles"] = {name: p}
    data["nodes"] = {n: _scrub_node(cfg.node(n), name) for n in p["nodes"]}
    if org in cfg.memoria:
        m = cfg.memoria_for(org)
        data["memoria"] = {org: {k: m[k] for k in ("host", "path") if m[k]}}
    data["relevo"] = {"node": node_name or ""}
    text = ("# derived by `delega sync --with-orchestrator`: regenerate it from the orchestrator, do not edit\n"
            + dump_toml(data))
    leaks = [o for o in list(cfg.profiles) + list(cfg.memoria) if o not in (name, org)
             and re.search(r"(?<![\w-])" + re.escape(o) + r"(?![\w-])", text, re.I)]
    if leaks:
        raise config.ConfigError(i18n.t("relevo.leak", what=leaks[0]))
    if SECRET_RE.search(text):
        raise config.ConfigError(i18n.t("relevo.leak", what="secret"))
    return text, data


# ── repo origin on a node ───────────────────────────────────────────────────────────────────────
def node_name(cfg):
    """The node this config orchestrates from (`[relevo] node`), or None on the Mac."""
    return ((cfg.raw.get("relevo") or {}).get("node") or "").strip() or None


def repo_basename(remote):
    return re.sub(r"\.git$", "", remote.strip().rstrip("/")).rsplit("/", 1)[-1].rsplit(":", 1)[-1]


def node_paths(cfg, remote):
    """(bare, work) of a node's checkout of `remote`: `~/<workdir>/<repo>.git` and `~/<workdir>/relevo/<repo>`.
    None when this is not a node's config (the Mac) or there is no remote."""
    name = node_name(cfg)
    if not name or not remote:
        return None
    try:
        workdir = cfg.node(name)["workdir"]
    except config.ConfigError:
        workdir = "delega"
    base = repo_basename(remote)
    home = os.path.join(os.path.expanduser("~"), workdir)
    return os.path.join(home, base + ".git"), os.path.join(home, "relevo", base)


def origin(cfg, remote, branch=None):
    """Checkout this node launches a flow's tasks from: `~/<workdir>/relevo/<repo>`, a plain clone of the
    node's bare `~/<workdir>/<repo>.git` (cloned from the remote when missing), with `origin` set to the
    flow's remote and HEAD detached at the flow's destination `branch` (else `main`, `master` or the
    bare's HEAD), refreshed on every call. The local branch of the clone is where `integrate --merge`
    works: it is brought up to the bare's when older (or missing), never moved back when it is newer
    (what was integrated and not pushed yet), and a newer one is pushed to the bare (fast-forward only).
    `launch` needs a working checkout: from a bare repo it would name the repo after its parent folder and
    have no HEAD to push. None when this is not a node's config (the Mac keeps its checkouts) or a git
    step fails."""
    paths = node_paths(cfg, remote)
    if not paths:
        return None
    bare, work = paths
    home = os.path.dirname(bare)

    def git(*a):
        return subprocess.run(["git", *a], capture_output=True, text=True)

    def fail(r):
        print(i18n.t("relevo.clone_failed", repo=remote, err=(r.stderr or r.stdout).strip()), file=sys.stderr)
        return None

    if not os.path.isdir(bare):
        os.makedirs(home, exist_ok=True)
        r = git("clone", "-q", "--bare", remote, bare)
        if r.returncode != 0:
            shutil.rmtree(bare, ignore_errors=True)
            return fail(r)
    if not os.path.isdir(os.path.join(work, ".git")):
        os.makedirs(os.path.dirname(work), exist_ok=True)
        r = git("clone", "-q", "--no-checkout", bare, work)
        if r.returncode != 0:
            shutil.rmtree(work, ignore_errors=True)
            return fail(r)
    r = git("-C", work, "remote", "set-url", "origin", remote)
    if r.returncode != 0:
        return fail(r)
    used = None
    for ref in [f"refs/heads/{b}" for b in (branch, "main", "master") if b] + ["HEAD"]:
        r = git("-C", work, "fetch", "-q", bare, ref)
        if r.returncode == 0:
            used = ref
            break
    else:
        return fail(r)
    target = "FETCH_HEAD"
    if used.startswith("refs/heads/"):
        target = _sync_local(git, work, bare, used)
    r = git("-C", work, "checkout", "-q", "--detach", "--force", target)
    return work if r.returncode == 0 else fail(r)


def _sync_local(git, work, bare, ref):
    """Reconcile the clone's `ref` (refs/heads/<b>) with the bare's, just fetched into FETCH_HEAD.
    Returns what HEAD should be detached at."""
    fetched = git("-C", work, "rev-parse", "FETCH_HEAD^{commit}").stdout.strip()
    local = git("-C", work, "rev-parse", "--verify", "-q", ref + "^{commit}").stdout.strip()
    if not fetched:
        return "FETCH_HEAD"
    if not local:
        git("-C", work, "update-ref", ref, fetched)
        return ref
    if local == fetched:
        return ref
    if git("-C", work, "merge-base", "--is-ancestor", local, fetched).returncode == 0:
        git("-C", work, "update-ref", ref, fetched, local)       # the bare is ahead
        return ref
    if git("-C", work, "merge-base", "--is-ancestor", fetched, local).returncode == 0:
        # the clone is ahead: what was integrated here and has not reached the bare. Never undo it.
        r = git("-C", work, "push", "-q", bare, f"{local}:{ref}")
        if r.returncode != 0:
            print(i18n.t("relevo.push_failed", ref=ref, err=(r.stderr or r.stdout).strip()), file=sys.stderr)
        return ref
    return "FETCH_HEAD"                                            # diverged: launch from the bare's


# ── asking Claude to decide ─────────────────────────────────────────────────────────────────────
def _export(client):
    from . import mem
    if client.m["local"]:
        rc, res = client._call(lambda: client._local_rc(mem.export))
    else:
        rc, res = client._call(lambda: client._remote("export"))
    if rc != mem.EXIT_OK:
        raise mem.MemError(f"export: rc={rc}")
    return res


def _event(client, que, task, datos, once=False):
    """Write an event of the task. With `once`: True when this call wrote it (False: there was one already)."""
    from . import mem
    if client.m["local"]:
        rc, res = client._call(lambda: client._local_rc(
            lambda c: mem.add_event(c, que, datos, task=task, once=once, quien=client.owner)))
        if rc != mem.EXIT_OK:
            raise mem.MemError(f"event: rc={rc}")
        return res
    args = ["event", que, "--task", task] + (["--once"] if once else []) + \
        ["--datos", json.dumps(datos, ensure_ascii=False)]
    rc, payload = client._call(lambda: client._remote(*args))
    if rc != mem.EXIT_OK:
        raise mem.MemError(payload.get("error") or f"event: rc={rc}")
    return bool(payload.get("creado"))


def _event_once(client, task, datos):
    return _event(client, REQUEST, task, datos, once=True)


def _flow_export(exp, flow):
    return {"org": exp.get("org"),
            "flujo": next((f for f in exp["flujos"] if f["id"] == flow), None),
            "tareas": [t for t in exp["tareas"] if t["flujo"] == flow],
            "eventos": [e for e in exp["eventos"] if (e.get("datos") or {}).get("flujo") == flow
                        and e["que"] != REQUEST][-EVENT_TAIL:]}


def candidates(exp):
    """{task id: why} for the tasks that need a decision: red twice (and still red) or flagged by an event."""
    reds, flagged, failed, asked = {}, {}, {}, set()
    for e in exp["eventos"]:
        d = e.get("datos") or {}
        if e["que"] == SPAWN_FAILED and d.get("tarea"):
            failed[d["tarea"]] = failed.get(d["tarea"], (0, ""))[0] + 1, d.get("motivo") or SPAWN_FAILED
        elif e["que"] == REQUEST and d.get("tarea"):
            asked.add(d["tarea"])
        if e["que"] == "task.set" and d.get("estado") == "rojo":
            reds[d.get("tarea")] = reds.get(d.get("tarea"), 0) + 1
        elif e["que"] == NEEDS_DECISION and d.get("tarea"):
            flagged[d["tarea"]] = d.get("motivo") or NEEDS_DECISION
    out = {}
    for t in exp["tareas"]:
        if t["estado"] == "hecha" or t["id"] in asked:      # one request per task, ever
            continue
        if t["id"] in failed and failed[t["id"]][0] < MAX_SPAWN_TRIES:
            out[t["id"]] = failed[t["id"]][1]      # blocked, but the Claude never started: try again
        elif t["estado"] == "rojo" and reds.get(t["id"], 0) >= MAX_REDS:
            out[t["id"]] = f"rojo x{reds[t['id']]}"
        elif t["id"] in flagged and t["estado"] != "bloqueada":
            out[t["id"]] = flagged[t["id"]]
    return out


def brief_for(task, why, flow_export):
    return i18n.t("relevo.brief", task=task["id"], why=why, flow=task["flujo"], brief=task["brief"],
                  estado=task["estado"], intentos=task["intentos"],
                  export=json.dumps(flow_export, ensure_ascii=False, indent=1, default=str))


def _ws_in_org(org):
    """True when `ws` exists and its active context is the org of the database (else plain `claude`)."""
    if not (org and shutil.which("ws")):
        return False
    try:
        r = subprocess.run(["ws", "contexto"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0 and org.lower() in re.findall(r"[\w.-]+", r.stdout.lower())


def claude_argv(brief, org=None):
    """argv of the escalated Claude. What guarantees what:

    * `--permission-mode default` + the CLOSED `--allowedTools` list (read tools + `Bash(delega mem *)`):
      in `-p` nobody can answer a permission prompt, so any tool outside the list is refused. THIS is
      what stops a push or a merge (a deny list alone is bypassed by `git -C x push`, `cd r && git push`,
      `git update-ref`, `gh api …/merges`).
    * `--disallowedTools` (`git`, `gh`, `Edit`, `Write`): a second lock that wins over the allow list.
    * `claude_env()`: only removes the GitHub tokens / ssh agent; it is not the guarantee (a Mac user
      may have other credentials).
    Plan mode is NOT used: it could not be shown that it lets `Bash(delega mem *)` run, and without
    that command the Claude cannot write its decision. (Not verified live: the node has no login.)"""
    base = ["ws", "exec", "claude"] if _ws_in_org(org) else ["claude"]
    return base + ["-p", brief, "--model", MODEL, "--max-budget-usd", str(CAP_USD),
                   "--output-format", "json", "--permission-mode", "default",
                   "--allowedTools", *ALLOWED_TOOLS, "--disallowedTools", *FORBIDDEN_TOOLS]


def claude_env():
    """The environment of the escalated Claude: no way to push or open a PR."""
    return {k: v for k, v in os.environ.items() if k not in STRIPPED_ENV}


def escalation_profile(cfg, org, task=None, repo=None):
    """Profile the escalated Claude is accounted to: the task's own (if it belongs to the org), else the
    one whose `repo_match.git_remote` matches the flow's repo, else the first of the org."""
    mine = [n for n in cfg.profiles if cfg.profile_org(n) == org] if org else []
    want = ((task or {}).get("run_args") or {}).get("profile")
    if want in mine:
        return want
    for n in mine:
        rx = (cfg.profiles[n].get("repo_match") or {}).get("git_remote")
        if rx and repo and re.search(rx, repo):
            return n
    return mine[0] if mine else None


def spawn(cfg, task_id, brief, org=None, profile=None):
    """Start the Claude orchestrator in the background; returns its log path.

    The run is a spend entry of its own (`<state>/<ts>-esc-<task>/`): its JSON result lands in
    `result.json`, so `spend` adds its cost when it finishes; the cap is audited at the start."""
    from . import audit, util
    d = os.path.join(cfg.state_dir, "relevo")
    os.makedirs(d, exist_ok=True)
    log = os.path.join(d, task_id + ".log")
    with open(os.path.join(d, task_id + ".md"), "w", encoding="utf-8") as f:
        f.write(brief)
    prof = profile or escalation_profile(cfg, org)
    run_dir = os.path.join(cfg.state_dir, time.strftime("%Y%m%d-%H%M%S") + "-esc-" + re.sub(r"[^\w.-]", "_", task_id))
    os.makedirs(run_dir, exist_ok=True)
    util.write_json(os.path.join(run_dir, "escalado.json"),
                    {"OWNER": "relevo", "PROFILE": prof or "", "PROJECT": "", "TASK": task_id, "CAP_USD": CAP_USD})
    with open(log, "ab") as out, open(os.path.join(run_dir, "result.json"), "wb") as res:
        subprocess.Popen(claude_argv(brief, org), stdout=res, stderr=out, stdin=subprocess.DEVNULL,
                         env=claude_env(), start_new_session=True)
    audit.record(cfg, "escalate", task=task_id, org=org, profile=prof, cap_usd=CAP_USD, dir=run_dir, log=log)
    return log


def escalate(cfg, client, limit=None):
    """Block the tasks that need a decision and ask one Claude orchestrator per task. Returns the task ids asked.

    Order matters: the task is blocked and the Claude started first, the `once` request event is written
    last, so a failure in between leaves the task retryable (event `escalado_fallido`) instead of dead.
    `limit`: at most that many tasks are attempted per call (the rest wait for the next cycle)."""
    from . import mem
    exp = _export(client)
    asked, tried = [], 0
    for tid, why in candidates(exp).items():
        if limit and tried >= limit:
            break
        task = next(t for t in exp["tareas"] if t["id"] == tid)
        fx = _flow_export(exp, task["flujo"])
        if task["estado"] != "bloqueada":
            rc, payload = client.set(tid, "bloqueada", error=why, desde=task["estado"])
            if rc == mem.EXIT_LEASE:         # another orchestrator moved the task first: it is theirs now
                continue
            if rc != mem.EXIT_OK:
                raise mem.MemError(payload.get("error") or f"set {tid}: rc={rc}")
        tried += 1
        try:
            prof = escalation_profile(cfg, client.org, task, ((fx.get("flujo") or {}).get("repo")))
            log = spawn(cfg, tid, brief_for(task, why, fx), client.org, prof)
        except Exception as e:
            print(i18n.t("relevo.error", err=e), file=sys.stderr)
            _event(client, SPAWN_FAILED, tid, {"tarea": tid, "flujo": task["flujo"], "motivo": why, "error": str(e)})
            continue
        _event_once(client, tid, {"tarea": tid, "flujo": task["flujo"], "motivo": why, "export": fx})
        print(i18n.t("relevo.asked", task=tid, why=why, log=log))
        asked.append(tid)
    return asked


def after_cycle(cfg, client):
    """Called by the daemon (only the lease owner gets here); a failure never stops the daemon."""
    try:
        return escalate(cfg, client)
    except Exception as e:
        print(i18n.t("relevo.error", err=e), file=sys.stderr)
        return []


# ── sync --with-orchestrator ────────────────────────────────────────────────────────────────────
WRAPPER = '#!/bin/sh\nexec python3 "$HOME"/{wd}/delega.pyz "$@"\n'


def install_on_node(cfg, prof, node):
    """Upload delega.pyz, a `delega` wrapper and the derived config. Returns (ok, error)."""
    import tempfile
    from . import remote
    try:
        text, _ = derive_config(cfg, prof["name"], node["name"])
        remote.ensure_pyz(cfg, node)
        wd = remote.workdir(node)
        with tempfile.TemporaryDirectory(prefix="delega-relevo-") as tmp:
            conf, wrap = os.path.join(tmp, "config.toml"), os.path.join(tmp, "delega")
            with open(conf, "w", encoding="utf-8") as f:
                f.write(text)
            with open(wrap, "w", encoding="utf-8") as f:
                f.write(WRAPPER.format(wd=wd))
            dst = '"$HOME"/.config/delega/config.toml'
            remote.must(remote.ssh(node, 'mkdir -p "$HOME"/.config/delega "$HOME"/.local/bin && '
                                         f'if [ -f {dst} ] && [ ! -f {dst}.before-relevo ]; '
                                         f'then cp {dst} {dst}.before-relevo; fi'), "mkdir")
            remote.must(remote.scp_to(node, conf, ".config/delega/config.toml"), "upload config")
            remote.must(remote.scp_to(node, wrap, ".local/bin/delega"), "upload wrapper")
            remote.must(remote.ssh(node, f'chmod 755 "$HOME"/.local/bin/delega && chmod 600 {dst}'), "chmod")
    except (config.ConfigError, remote.RemoteError, OSError, subprocess.SubprocessError) as e:
        return False, str(e)
    return True, ""

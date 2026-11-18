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

from . import config, i18n

REQUEST = "orquestador_claude"
NEEDS_DECISION = "necesita_decision"
MAX_REDS = 2
MODEL = "sonnet"
CAP_USD = 3
EVENT_TAIL = 50
FORBIDDEN_TOOLS = ("Bash(git push:*)", "Bash(git merge:*)", "Bash(gh pr merge:*)")
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


def origin(cfg, remote):
    """Bare repo `~/<workdir>/<repo>.git` of this node for a flow's remote, cloned when missing.
    None when this is not a node's config (the Mac keeps its checkouts) or the clone fails."""
    name = node_name(cfg)
    if not name or not remote:
        return None
    try:
        workdir = cfg.node(name)["workdir"]
    except config.ConfigError:
        workdir = "delega"
    bare = os.path.join(os.path.expanduser("~"), workdir, repo_basename(remote) + ".git")
    if os.path.isdir(bare):
        return bare
    os.makedirs(os.path.dirname(bare), exist_ok=True)
    r = subprocess.run(["git", "clone", "--bare", remote, bare], capture_output=True, text=True)
    if r.returncode != 0:
        print(i18n.t("relevo.clone_failed", repo=remote, err=(r.stderr or r.stdout).strip()), file=sys.stderr)
        shutil.rmtree(bare, ignore_errors=True)
        return None
    return bare


# ── asking Claude to decide ─────────────────────────────────────────────────────────────────────
def _export(client):
    from . import mem
    if client.m["local"]:
        return client._local(mem.export)
    return client._remote("export")[1]


def _event_once(client, task, datos):
    """True when this call wrote the request (False: there was one already)."""
    from . import mem
    if client.m["local"]:
        return client._local(lambda c: mem.add_event(c, REQUEST, datos, task=task, once=True, quien=client.owner))
    return bool(client._remote("event", REQUEST, "--task", task, "--once", "--datos",
                               json.dumps(datos, ensure_ascii=False))[1].get("creado"))


def _flow_export(exp, flow):
    return {"org": exp.get("org"),
            "flujo": next((f for f in exp["flujos"] if f["id"] == flow), None),
            "tareas": [t for t in exp["tareas"] if t["flujo"] == flow],
            "eventos": [e for e in exp["eventos"] if (e.get("datos") or {}).get("flujo") == flow
                        and e["que"] != REQUEST][-EVENT_TAIL:]}


def candidates(exp):
    """{task id: why} for the tasks that need a decision: red twice (and still red) or flagged by an event."""
    reds, flagged = {}, {}
    for e in exp["eventos"]:
        d = e.get("datos") or {}
        if e["que"] == "task.set" and d.get("estado") == "rojo":
            reds[d.get("tarea")] = reds.get(d.get("tarea"), 0) + 1
        elif e["que"] == NEEDS_DECISION and d.get("tarea"):
            flagged[d["tarea"]] = d.get("motivo") or NEEDS_DECISION
    out = {}
    for t in exp["tareas"]:
        if t["estado"] == "hecha":
            continue
        if t["estado"] == "rojo" and reds.get(t["id"], 0) >= MAX_REDS:
            out[t["id"]] = f"rojo x{reds[t['id']]}"
        elif t["id"] in flagged and t["estado"] != "bloqueada":
            out[t["id"]] = flagged[t["id"]]
    return out


def brief_for(task, why, flow_export):
    return i18n.t("relevo.brief", task=task["id"], why=why, flow=task["flujo"], brief=task["brief"],
                  estado=task["estado"], intentos=task["intentos"],
                  export=json.dumps(flow_export, ensure_ascii=False, indent=1, default=str))


def claude_argv(brief):
    base = ["ws", "exec", "claude"] if shutil.which("ws") else ["claude"]
    return base + ["-p", brief, "--model", MODEL, "--max-budget-usd", str(CAP_USD),
                   "--disallowed-tools", *FORBIDDEN_TOOLS]


def spawn(cfg, task_id, brief):
    """Start the Claude orchestrator in the background; returns its log path."""
    d = os.path.join(cfg.state_dir, "relevo")
    os.makedirs(d, exist_ok=True)
    log = os.path.join(d, task_id + ".log")
    with open(os.path.join(d, task_id + ".md"), "w", encoding="utf-8") as f:
        f.write(brief)
    with open(log, "ab") as out:
        subprocess.Popen(claude_argv(brief), stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                         start_new_session=True)
    return log


def escalate(cfg, client):
    """Block the tasks that need a decision and ask one Claude orchestrator per task. Returns the task ids asked."""
    exp = _export(client)
    asked = []
    for tid, why in candidates(exp).items():
        task = next(t for t in exp["tareas"] if t["id"] == tid)
        fx = _flow_export(exp, task["flujo"])
        if not _event_once(client, tid, {"tarea": tid, "flujo": task["flujo"], "motivo": why, "export": fx}):
            continue
        client.set(tid, "bloqueada", error=why)
        log = spawn(cfg, tid, brief_for(task, why, fx))
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

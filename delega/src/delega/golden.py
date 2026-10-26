"""golden: build and list the kindling golden images jobs and sandboxes are cloned from.

  delega golden build [repo] [--node N] [--profile P] [--name G] [--engines "postgres:16 redis"] [--dry-run]
      runs nodes/kindling/golden.sh with what the config already knows: the host, the VM user/checkout/base
      ref of the node, this machine's claude version, the profile's rules bundle (CLAUDE.md, skills, agents,
      hooks), the node's `golden_extra` / `golden_apt` and the engines of the project's databases.
  delega golden db [repo] [--db NAME]… [--node N]
      the data golden `<golden>-db`: the golden with the project's databases restored and running, frozen.
  delega golden ls [--node N] · delega golden rm <name> --yes
"""
import argparse
import os
import subprocess
import sys
import time

from . import audit, config as _config, databases, kindling, nodes, remote, util
from .i18n import t

ROOT = os.path.dirname(os.path.dirname(util.PKG_DIR))
GOLDEN_SH = os.path.join(ROOT, "nodes", "kindling", "golden.sh")


def _node(cfg, profile, name):
    if name:
        n = cfg.node(name)
    else:
        n = next((x for x in (cfg.node(m) for m in (profile.get("nodes") or []) + list(cfg.nodes))
                  if kindling.is_kindling(x) and profile["name"] in x["profiles"]), None)
    if not n or not kindling.is_kindling(n):
        raise databases.DbError(t("db.err.no_kindling", profile=profile["name"]))
    return n


def build_env(cfg, profile, node, project, engines=None):
    """Environment for golden.sh, from the config (nothing secret: no token is involved in a build)."""
    lv = nodes.claude_version(profile["launcher"], profile.get("config_dir"))
    if engines is None:
        engines = " ".join(sorted({s["engine"] + (":" + s["version"] if s["version"] else "")
                                   for s in databases.specs(cfg, project).values()}))
    env = {"KINDLING_HOST": remote.alias(node), "CLAUDE_VERSION": node.get("claude_version") or lv or "",
           "DELEGA_PROFILE": profile["name"], "VM_USER": remote.vm_user(node), "VM_REPO": kindling.vm_repo(node),
           "BASE_REF": kindling.base_ref(node), "DB_ENGINES": engines,
           "DELEGA": os.path.join(ROOT, "bin", "delega")}
    extra = node.get("golden_extra") or profile.get("golden_extra")
    if extra:
        env["EXTRA"] = _config.expand(extra)
    apt = node.get("golden_apt") or profile.get("golden_apt")
    if apt:
        env["APT_EXTRA"] = " ".join(apt) if isinstance(apt, list) else apt
    for k in ("VM_DISK", "VM_MEM"):
        if node.get(k.lower().replace("vm_", "golden_")):
            env[k] = str(node[k.lower().replace("vm_", "golden_")])
    return env


def main(argv):
    ap = argparse.ArgumentParser(prog="delega golden", description=t("golden.help"))
    common = util.common_options(ap, ["node", "profile"])
    sub = ap.add_subparsers(dest="cmd")
    b = sub.add_parser("build", parents=[common])
    b.add_argument("repo", nargs="?", default=".")
    b.add_argument("--name")
    b.add_argument("--engines", help='database engines, "engine[:version] …" (default: the project\'s)')
    b.add_argument("--dry-run", action="store_true")
    d = sub.add_parser("db", parents=[common])
    d.add_argument("repo", nargs="?", default=".")
    d.add_argument("--db", action="append", default=[], help="database to load (default: all the project's)")
    d.add_argument("--golden", help="base golden (default: the node's golden for this repo)")
    sub.add_parser("ls", parents=[common])
    r = sub.add_parser("rm", parents=[common])
    r.add_argument("name")
    r.add_argument("--yes", action="store_true")
    a = ap.parse_args(argv)
    try:
        cfg = _config.load()
        repo = os.path.abspath(getattr(a, "repo", ".") or ".")
        profile = cfg.profile(a.profile or os.environ.get("DELEGA_PROFILE") or cfg.profile_for(repo))
        node = _node(cfg, profile, a.node)
        project = cfg.project_for(repo)
        if a.cmd == "build":
            return _build(cfg, profile, node, project, repo, a)
        if a.cmd == "db":
            dbs = databases.select(cfg, project, a.db or ["all"])
            if not dbs:
                raise databases.DbError(t("db.err.none", project=project or "-"))
            databases.check_profile(cfg, repo, profile["name"], dbs)
            if profile["name"] not in node["profiles"]:
                raise databases.DbError(t("launch.err.node_profile", node=node["name"], profile=profile["name"]))
            g = a.golden or kindling.golden(node, remote.repo_name(repo))
            print(t("golden.baking", golden=databases.data_golden(g), dbs=", ".join(s["name"] for s in dbs)))
            t0 = time.time()
            dg, recs = databases.bake(cfg, node, g, dbs)
            audit.record(cfg, "golden_db", node=node["name"], golden=dg, base=g, project=project,
                         dbs=[{k: r[k] for k in ("name", "volume", "sha", "check")} for r in recs],
                         s=round(time.time() - t0, 1))
            print(t("golden.baked", golden=dg, s=int(time.time() - t0)))
            return 0
        if a.cmd == "rm":
            if not a.yes:
                print(t("golden.rm_confirm", name=a.name, node=node["name"]), file=sys.stderr)
                return 2
            r = databases.host(node, f"kling template rm -f {databases.q(a.name)}")
            if r.returncode != 0:
                raise databases.DbError(r.stderr.strip())
            with audit.locked(databases.state_path(cfg) + ".lock"):
                st = databases.load_state(cfg)
                st["goldens"].pop(f"{databases.hkey(node)}/{a.name}", None)
                databases.save_state(cfg, st)
            audit.record(cfg, "golden_rm", node=node["name"], golden=a.name)
            return 0
        return _ls(cfg, node)
    except (databases.DbError, _config.ConfigError, remote.RemoteError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 5


def _build(cfg, profile, node, project, repo, a):
    if not os.path.exists(GOLDEN_SH):
        raise databases.DbError(t("golden.err.no_script", path=GOLDEN_SH))
    name = a.name or kindling.golden(node, remote.repo_name(repo))
    env = build_env(cfg, profile, node, project, a.engines)
    if not env["CLAUDE_VERSION"]:
        raise databases.DbError(t("golden.err.claude_version"))
    shown = " ".join(f"{k}={v}" for k, v in env.items() if v)
    print(t("golden.building", name=name, node=node["name"], env=shown))
    if a.dry_run:
        return 0
    t0 = time.time()
    rc = subprocess.run(["bash", GOLDEN_SH, repo, name], env={**os.environ, **env}).returncode
    if rc != 0:
        return rc
    head = subprocess.run(["git", "-C", repo, "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
    databases.note_golden(cfg, node, name, repo_head=head, engines=env["DB_ENGINES"], claude=env["CLAUDE_VERSION"],
                          profile=profile["name"])
    audit.record(cfg, "golden_build", node=node["name"], golden=name, profile=profile["name"], base=head,
                 engines=env["DB_ENGINES"] or None, claude=env["CLAUDE_VERSION"], s=round(time.time() - t0, 1))
    print(t("golden.built", name=name, s=int(time.time() - t0)))
    return 0


def _ls(cfg, node):
    r = databases.host(node, "kling template ls")
    if r.returncode != 0:
        raise databases.DbError(r.stderr.strip() or "kling template ls")
    st = databases.load_state(cfg)
    for i, line in enumerate(r.stdout.rstrip().splitlines()):
        name = line.split()[0] if line.split() else ""
        rec = st["goldens"].get(f"{databases.hkey(node)}/{name}")
        note = ""
        if i == 0:
            note = t("golden.ls_note")
        elif rec:
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(rec.get("built", 0)))
            if rec.get("dbs"):
                note = t("golden.ls_db", when=when, base=rec.get("base"), dbs=", ".join(rec["dbs"]),
                         fresh=t("golden.fresh") if not _stale(st, node, rec) else t("golden.stale"))
            else:
                note = t("golden.ls_plain", when=when, head=rec.get("repo_head", "?"), engines=rec.get("engines") or "-")
        print(f"{line}   {note}".rstrip())
    return 0


def _stale(st, node, rec):
    base = st["goldens"].get(f"{databases.hkey(node)}/{rec.get('base')}") or {}
    if base.get("built", 0) > rec.get("built", 0):
        return True
    for n, sha in (rec.get("dbs") or {}).items():
        v = st["volumes"].get(f"{databases.hkey(node)}/{(rec.get('volumes') or {}).get(n)}") or {}
        if v.get("sha256") and v["sha256"] != sha:
            return True
    return False

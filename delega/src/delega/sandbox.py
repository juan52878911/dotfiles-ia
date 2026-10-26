"""sandbox: development microVMs on a kindling host, outside any delegated job.

  delega sandbox up <name> [repo] [--golden G] [--db NAME]… [--db-mode m] [--ttl 30m] [--egress e] [--allow d]
      a VM `sb-<name>` cloned from the repo's golden (or its data golden), this repo's HEAD checked out on
      branch `sb/<name>`, the databases up. It freezes itself after --ttl (no CPU, no RAM) and the next
      exec/shell thaws it in milliseconds.
  delega sandbox exec <name> [--root] -- <cmd…>   · delega sandbox shell <name> [--root]
  delega sandbox env <name>                       the database variables the sandbox gets
  delega sandbox freeze|thaw|rm <name>…           · delega sandbox save <name> <template>
  delega sandbox ls

The sandbox gets the same rules as a job: egress allow-list by default, the databases are throwaway copies.
"""
import argparse
import os
import shlex
import subprocess
import sys
import time

from . import audit, config as _config, databases, golden as _golden, kindling, remote, util
from .i18n import t

PREFIX = "sb-"
q = shlex.quote


def vm_name(name):
    return name if name.startswith(PREFIX) else PREFIX + name


def _state(cfg):
    return databases.load_state(cfg)


def _save(cfg, fn):
    with audit.locked(databases.state_path(cfg) + ".lock"):
        st = _state(cfg)
        fn(st)
        databases.save_state(cfg, st)


def up(cfg, profile, node, name, repo, a):
    vm = vm_name(name)
    project = cfg.project_for(repo) if repo else None
    dbs = databases.select(cfg, project, a.db) if a.db else []
    databases.check_profile(cfg, repo, profile["name"], dbs)
    golden = a.golden or kindling.golden(node, remote.repo_name(repo) if repo else name)
    mode, image, why = ("volume", golden, "")
    if dbs:
        mode, image, why = databases.plan(cfg, node, golden, dbs, a.db_mode)
        if why:
            print(t("launch.db_fallback", why=why))
    allow = a.allow or ",".join(node.get("egress_allow", kindling.DEFAULT_EGRESS))
    t0 = time.time()
    r = databases.host(node, f"kling run -name {q(vm)} -from {q(image)} -egress {q(a.egress)}"
                             + (f" -allow {q(allow)}" if a.egress == "allowlist" else "")
                             + (f" -ttl {q(a.ttl)} -on-ttl freeze" if a.ttl not in ("0", "") else "")
                             + " -label delega=sandbox -wait-ready >/dev/null", timeout=300)
    if r.returncode != 0:
        raise databases.DbError(f"kling run -from {image}: {(r.stderr or r.stdout).strip()[-300:]}")
    try:
        branch = None
        if repo and not a.no_head and _is_git(repo):
            n = dict(node, vm=vm)
            chan = ".delega-sandbox"
            remote.must(remote.ssh(n, f"mkdir -p {q(chan)}"), "mkdir")
            kindling.push_head(repo, n, chan, name, profile.get("git"))
            branch = f"sb/{name}"
            kindling.make_worktree(n, name, branch)
        recs = databases.attach(cfg, node, vm, dbs, mode) if dbs else []
    except Exception:
        databases.host(node, f"kling rm -f {q(vm)} >/dev/null 2>&1")
        raise
    env = databases.env(dbs)
    _save(cfg, lambda st: st["sandboxes"].__setitem__(f"{databases.hkey(node)}/{vm}", {
        "image": image, "dbs": [s["name"] for s in dbs], "env": env, "branch": branch, "repo": repo,
        "created": int(time.time())}))
    audit.record(cfg, "sandbox_up", node=node["name"], vm=vm, golden=image, project=project, profile=profile["name"],
                 branch=branch, dbs=[{k: x[k] for k in ("name", "volume", "sha", "mode", "check")} for x in recs] or None,
                 egress=a.egress, s=round(time.time() - t0, 1))
    print(t("sandbox.up", vm=vm, image=image, s=round(time.time() - t0, 1), branch=branch or "-"))
    for x in recs:
        print(t("launch.db_ready", name=x["name"], engine=x["engine"], mode=x["mode"], s=x["s"], check=x["check"]))
    print(t("sandbox.next", name=name))
    return 0


def _is_git(path):
    return subprocess.run(["git", "-C", path, "rev-parse", "--git-dir"], capture_output=True).returncode == 0


def _exec_argv(node, vm, cmd, env, root, tty):
    home, user = remote.vm_home(node), remote.vm_user(node)
    wt = f"{home}/{kindling.vm_repo(node)}"
    es = " ".join(f"-e {q(k + '=' + v)}" for k, v in env.items())
    inner = f"bash -lc {q(cmd)}" if root else f"runuser -u {q(user)} -- env HOME={q(home)} bash -lc {q(cmd)}"
    line = f"kling thaw {q(vm)} >/dev/null 2>&1; kling {'shell' if tty else 'exec'} {es} -w {q(wt)} {q(vm)} -- {inner}"
    return remote._ssh_prefix() + (["-t"] if tty else []) + remote.SSH_OPTS + [remote.alias(remote.host_node(node)), line]


def main(argv):
    ap = argparse.ArgumentParser(prog="delega sandbox", description=t("sandbox.help"))
    common = util.common_options(ap, ["node", "profile"])
    sub = ap.add_subparsers(dest="cmd")
    u = sub.add_parser("up", parents=[common])
    u.add_argument("name")
    u.add_argument("repo", nargs="?", default=".")
    u.add_argument("--golden")
    u.add_argument("--db", action="append", default=[])
    u.add_argument("--db-mode", choices=("auto", "golden", "volume"))
    u.add_argument("--ttl", default="30m", help="freeze after this long (0 = never)")
    u.add_argument("--egress", default="allowlist", choices=("allowlist", "none", "internet"))
    u.add_argument("--allow", help="domains for --egress allowlist (default: the node's egress_allow)")
    u.add_argument("--no-head", action="store_true", help="keep the golden's checkout, do not bring HEAD")
    e = sub.add_parser("exec", parents=[common])
    e.add_argument("name")
    e.add_argument("--root", action="store_true")
    e.add_argument("argv", nargs=argparse.REMAINDER)
    s = sub.add_parser("shell", parents=[common])
    s.add_argument("name")
    s.add_argument("--root", action="store_true")
    for verb in ("freeze", "thaw", "rm"):
        x = sub.add_parser(verb, parents=[common])
        x.add_argument("names", nargs="+")
    sv = sub.add_parser("save", parents=[common])
    sv.add_argument("name")
    sv.add_argument("template")
    en = sub.add_parser("env", parents=[common])
    en.add_argument("name")
    sub.add_parser("ls", parents=[common])
    a = ap.parse_args(argv)
    try:
        cfg = _config.load()
        repo = os.path.abspath(getattr(a, "repo", None) or ".")
        profile = cfg.profile(a.profile or os.environ.get("DELEGA_PROFILE") or cfg.profile_for(repo))
        node = _golden._node(cfg, profile, a.node)
        if a.cmd == "up":
            return up(cfg, profile, node, a.name, repo, a)
        if a.cmd in ("exec", "shell", "env"):
            vm = vm_name(a.name)
            rec = _state(cfg)["sandboxes"].get(f"{databases.hkey(node)}/{vm}") or {}
            if a.cmd == "env":
                for k, v in (rec.get("env") or {}).items():
                    print(f"{k}={v}")
                return 0
            words = list(getattr(a, "argv", []) or [])
            if words[:1] == ["--"]:
                words = words[1:]
            cmd = " ".join(q(x) for x in words) or "bash -l"
            return subprocess.run(_exec_argv(node, vm, cmd, rec.get("env") or {}, a.root, a.cmd == "shell")).returncode
        if a.cmd in ("freeze", "thaw", "rm"):
            vms = [vm_name(n) for n in a.names]
            verb = "rm -f" if a.cmd == "rm" else a.cmd
            r = databases.host(node, f"kling {verb} " + " ".join(q(v) for v in vms), timeout=300)
            sys.stdout.write(r.stdout)
            sys.stderr.write(r.stderr)
            if a.cmd == "rm":
                _save(cfg, lambda st: [st["sandboxes"].pop(f"{databases.hkey(node)}/{v}", None) for v in vms])
            audit.record(cfg, "sandbox_" + a.cmd, node=node["name"], vm=",".join(vms))
            return r.returncode
        if a.cmd == "save":
            vm = vm_name(a.name)
            r = databases.host(node, f"kling save -replace {q(vm)} {q(a.template)}", timeout=1800)
            sys.stdout.write(r.stdout)
            sys.stderr.write(r.stderr)
            if r.returncode == 0:
                rec = _state(cfg)["sandboxes"].get(f"{databases.hkey(node)}/{vm}") or {}
                databases.note_golden(cfg, node, a.template, base=rec.get("image"), from_sandbox=vm)
                audit.record(cfg, "sandbox_save", node=node["name"], vm=vm, golden=a.template)
            return r.returncode
        r = databases.host(node, "kling ps -a")
        st = _state(cfg)["sandboxes"]
        for i, line in enumerate(r.stdout.rstrip().splitlines()):
            w = line.split()
            if i == 0 or any(x.startswith(PREFIX) for x in w):
                name = next((x for x in w if x.startswith(PREFIX)), "")
                rec = st.get(f"{databases.hkey(node)}/{name}") or {}
                extra = f"   {rec.get('image', '')} {','.join(rec.get('dbs') or [])}" if rec else ""
                print(line + extra)
        return r.returncode
    except (databases.DbError, _config.ConfigError, remote.RemoteError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 5

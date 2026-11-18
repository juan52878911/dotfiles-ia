"""sync: push the profile's rules bundle to its nodes so remote workers behave like local ones.
`--with-orchestrator` also leaves delega.pyz and a derived orchestrator config (relevo.py) on the node.

The bundle dir (profile `node_bundle`) may hold CLAUDE.md, hooks/, agents/, skills/, rules/, commands/,
output-styles/, settings.json (all go to the node's ~/.claude, or config_dirs[profile]) and home/ (unpacked
relative to $HOME). The profile's `bundle_skills = [dirs]` add the orchestrator's own skills. A node
rules section rendered from the message catalog is appended to CLAUDE.md. After unpacking, the
profile's `bundle_post_sync` commands run on the node (e.g. to merge settings.json).
Credentials are never sent: they are filtered out of the staging tree, whatever the bundle holds."""
import fnmatch
import os
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile

from . import config, i18n

BEGIN, END = "<!-- delega:node-rules:begin -->", "<!-- delega:node-rules:end -->"
ITEMS = ("CLAUDE.md", "hooks", "agents", "skills", "rules", "commands", "output-styles", "settings.json")
NEVER_SEND = (".credentials.json", "*credentials*", ".env", ".env.*", "*.pem", "*.key", "id_rsa*", "id_ed25519*",
              "application_default_credentials.json", ".netrc", "*.token", ".git")


def node_rules(cfg, prof, loc=None):
    """The rendered 'node rules' section (caps and shared-machine limits come from policy)."""
    p = lambda k: cfg.policy(k, prof)
    return i18n.t("sync.node_rules", loc=loc or cfg.locale, owner=cfg.owner_name, max_min=p("job.max_min"),
                  max_turns=p("job.max_turns"), max_usd=p("job.max_usd"), min_disk=p("node.min_disk_gb"),
                  max_load=p("node.max_load_pct"))


def _skip(name):
    return any(fnmatch.fnmatch(name.lower(), pat) for pat in NEVER_SEND)


def _copy_tree(src, dst, skipped):
    if os.path.isfile(src):
        if _skip(os.path.basename(src)):
            skipped.append(src)
            return
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
        return
    for root, dirs, files in os.walk(src, followlinks=True):
        keep = []
        for d in dirs:
            (skipped.append(os.path.join(root, d)) if _skip(d) else keep.append(d))
        dirs[:] = keep
        rel = os.path.relpath(root, src)
        for f in files:
            if _skip(f):
                skipped.append(os.path.join(root, f))
                continue
            out = os.path.normpath(os.path.join(dst, rel, f))
            os.makedirs(os.path.dirname(out), exist_ok=True)
            shutil.copy2(os.path.join(root, f), out)


def stage(cfg, prof, dest_dir):
    """Build the staging tree: <dest_dir>/claude/… (→ config dir) and <dest_dir>/home/… (→ $HOME).
    Returns (list of staged relative paths, skipped credential-like paths)."""
    bundle = config.expand(prof.get("node_bundle") or "")
    if not bundle or not os.path.isdir(bundle):
        raise FileNotFoundError(bundle or "node_bundle")
    skipped = []
    for item in ITEMS:
        if os.path.exists(os.path.join(bundle, item)):
            _copy_tree(os.path.join(bundle, item), os.path.join(dest_dir, "claude", item), skipped)
    for sk in prof.get("bundle_skills") or []:       # the orchestrator's own skills, by path
        src = config.expand(sk).rstrip("/")
        if os.path.isfile(os.path.join(src, "SKILL.md")):
            _copy_tree(src, os.path.join(dest_dir, "claude", "skills", os.path.basename(src)), skipped)
        else:
            skipped.append(sk + " (no SKILL.md)")
    if os.path.isdir(os.path.join(bundle, "home")):
        _copy_tree(os.path.join(bundle, "home"), os.path.join(dest_dir, "home"), skipped)
    claude_md = os.path.join(dest_dir, "claude", "CLAUDE.md")
    os.makedirs(os.path.dirname(claude_md), exist_ok=True)
    base = open(claude_md, encoding="utf-8").read().rstrip() + "\n\n" if os.path.exists(claude_md) else ""
    with open(claude_md, "w", encoding="utf-8") as f:
        f.write(f"{base}{BEGIN}\n{node_rules(cfg, prof).rstrip()}\n{END}\n")
    staged = sorted(os.path.relpath(os.path.join(r, f), dest_dir) for r, _, fs in os.walk(dest_dir) for f in fs)
    return staged, skipped


def remote_path(p):
    """Shell word for a node path: '~/x' and relative paths hang off "$HOME"; absolute stay."""
    if p == "~":
        return '"$HOME"'
    if p.startswith("~/"):
        p = p[2:]
    return shlex.quote(p) if p.startswith("/") else '"$HOME"/' + shlex.quote(p)


def make_tar(stage_dir, tar_path):
    with tarfile.open(tar_path, "w") as t:
        for top in ("claude", "home"):
            if os.path.isdir(os.path.join(stage_dir, top)):
                t.add(os.path.join(stage_dir, top), arcname=top)


def sync_node(cfg, prof, node, tar_path):
    from . import remote
    cdir = (node.get("config_dirs") or {}).get(prof["name"], "~/.claude")
    tmp_remote = f"/tmp/delega-sync-{os.getpid()}.tar"
    dest = remote_path(cdir)
    script = " && ".join([
        f"mkdir -p {dest}",
        f"if [ -f {dest}/CLAUDE.md ] && [ ! -f {dest}/CLAUDE.md.before-delega ] && ! grep -q 'delega:node-rules' {dest}/CLAUDE.md; "
        f"then cp {dest}/CLAUDE.md {dest}/CLAUDE.md.before-delega; fi",
        f"d=$(mktemp -d) && tar -C \"$d\" -xf {tmp_remote} --no-same-owner",
        f"cp -R \"$d\"/claude/. {dest}/",
        "if [ -d \"$d\"/home ]; then cp -R \"$d\"/home/. \"$HOME\"/; fi",
        f"rm -rf \"$d\" {tmp_remote}",
    ])
    remote.scp_to(node, tar_path, tmp_remote)
    r = remote.ssh(node, script, timeout=120)
    if r.returncode != 0:
        return False, (r.stderr or r.stdout or "").strip()
    env = f'CLAUDE_CONFIG_DIR={dest} '
    for cmd in prof.get("bundle_post_sync", []):
        r = remote.ssh(node, f"cd \"$HOME\" && {env}{cmd}", timeout=120)
        if r.returncode != 0:
            return False, i18n.t("sync.post_failed", cmd=cmd, err=(r.stderr or r.stdout or "").strip())
    return True, ""


def branch_pushes(cfg, prof):
    """[(flow, repo, branch, checkout)] for the active flows of the profile's org that have a checkout on this
    machine; flows without one are reported and skipped. Raises on an unreadable memory."""
    from . import queue, relevo
    org = cfg.profile_org(prof["name"])
    client = queue.MemClient(cfg, org, "sync")
    out, seen = [], set()
    for fl in client.flows():
        if fl.get("estado") != "activo" or fl.get("org", org) != org:
            continue
        co = queue._checkout(client.m, fl["repo"])
        if not co:
            print(i18n.t("sync.branch_no_checkout", repo=fl["repo"]), file=sys.stderr)
            continue
        key = (relevo.repo_basename(fl["repo"]), fl.get("rama_destino") or "main")
        if key not in seen:
            seen.add(key)
            out.append((fl["id"], key[0], key[1], co))
    return out


def push_branch(node, repo, branch, checkout):
    """Push the flow's destination branch to the node's bare repo (only that ref, only to the bare)."""
    import subprocess as sp
    from . import remote
    sp.run(["git", "-C", checkout, "fetch", "-q", "origin", branch], capture_output=True, text=True)
    ref = f"refs/remotes/origin/{branch}"
    if sp.run(["git", "-C", checkout, "rev-parse", "-q", "--verify", ref], capture_output=True).returncode != 0:
        ref = f"refs/heads/{branch}"
        if sp.run(["git", "-C", checkout, "rev-parse", "-q", "--verify", ref], capture_output=True).returncode != 0:
            return False, f"no {branch} in {checkout}"
    q = shlex.quote
    bare = q(remote.bare_rel(node, repo))
    init = f"{{ test -d {bare} || {{ mkdir -p {q(remote.workdir(node))} && git init -q --bare {bare}; }}; }}"
    r = remote.ssh(node, init)
    if r.returncode != 0:
        return False, (r.stderr or r.stdout or "").strip()
    r = sp.run(["git", "-C", checkout, "push", "-q", f"{remote.alias(node)}:{remote.bare_rel(node, repo)}",
                f"+{ref}:refs/heads/{branch}"], capture_output=True, text=True, env=remote._git_env())
    if r.returncode != 0:
        return False, (r.stderr or r.stdout or "").strip()
    r = remote.ssh(node, f"git -C {bare} rev-parse -q --verify HEAD >/dev/null 2>&1 || "
                         f"git -C {bare} symbolic-ref HEAD {q('refs/heads/' + branch)}")
    return (r.returncode == 0), (r.stderr or "").strip()


def main(argv):
    profile, names, dry, tar_out, orch, a = None, [], False, None, False, list(argv)
    while a:
        x = a.pop(0)
        if x == "--profile" and a:
            profile = a.pop(0)
        elif x == "--tar" and a:
            tar_out = os.path.abspath(a.pop(0))
        elif x == "--with-orchestrator":
            orch = True
        elif x == "--dry-run":
            dry = True
        elif x in ("-h", "--help"):
            print(i18n.t("sync.usage"))
            return 0
        else:
            names.append(x)
    try:
        cfg = config.load()
        prof = cfg.profile(profile)
        nodes = [cfg.node(n) for n in names] if names else cfg.nodes_for(prof)
    except config.ConfigError as e:
        print(e, file=sys.stderr)
        return 2
    if not nodes and not tar_out:
        print(i18n.t("sync.no_nodes", profile=prof["name"]), file=sys.stderr)
        return 2
    with tempfile.TemporaryDirectory(prefix="delega-sync-") as tmp:
        try:
            staged, skipped = stage(cfg, prof, os.path.join(tmp, "stage"))
        except FileNotFoundError:
            print(i18n.t("sync.no_bundle", profile=prof["name"]), file=sys.stderr)
            return 2
        for s in skipped:
            print(i18n.t("sync.skipped", path=s), file=sys.stderr)
        tar_path = os.path.join(tmp, "bundle.tar")
        make_tar(os.path.join(tmp, "stage"), tar_path)
        if tar_out:     # no node contacted: e.g. to bake the rules into a kindling golden image
            shutil.copy(tar_path, tar_out)
            print(i18n.t("sync.tar", path=tar_out, n=len(staged)))
            return 0
        rc = 0
        pushes = []
        if orch:
            try:
                pushes = branch_pushes(cfg, prof)
            except Exception as e:      # memory unreachable or not configured: the rest of the sync goes on
                print(i18n.t("relevo.error", err=e), file=sys.stderr)
        for node in nodes:
            if prof["name"] not in node["profiles"]:
                print(i18n.t("sync.not_allowed", node=node["name"], profile=prof["name"]), file=sys.stderr)
                rc = 1
                continue
            if dry:
                print(i18n.t("sync.dry", node=node["name"], n=len(staged)))
                for s in staged:
                    print("  " + s)
                if orch:
                    for _, repo, br, _co in pushes:
                        print(i18n.t("sync.branch_dry", branch=br, repo=repo))
                    from . import relevo
                    try:
                        relevo.derive_config(cfg, prof["name"], node["name"])
                        print(i18n.t("sync.orch_dry", node=node["name"]))
                    except config.ConfigError as e:
                        print(i18n.t("sync.orch_failed", node=node["name"], err=e), file=sys.stderr)
                        rc = 1
                continue
            try:
                ok, err = sync_node(cfg, prof, node, tar_path)
            except (OSError, subprocess.SubprocessError, AttributeError, NotImplementedError) as e:
                ok, err = False, str(e)
            print(i18n.t("sync.done" if ok else "sync.failed", node=node["name"], n=len(staged), err=err))
            if ok and orch:
                from . import relevo
                ok, err = relevo.install_on_node(cfg, prof, node)
                print(i18n.t("sync.orch_done" if ok else "sync.orch_failed", node=node["name"], err=err))
                for _, repo, br, co in pushes:
                    try:
                        pok, perr = push_branch(node, repo, br, co)
                    except (OSError, subprocess.SubprocessError) as e:
                        pok, perr = False, str(e)
                    print(i18n.t("sync.branch_pushed", node=node["name"], branch=br, repo=repo) if pok
                          else i18n.t("sync.branch_failed", node=node["name"], branch=br, repo=repo, err=perr))
                    ok = ok and pok
            rc = rc or (0 if ok else 1)
    return rc

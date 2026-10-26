"""remote: ssh/scp wrappers and the git plumbing that moves a repo to a node and back.

Tests point DELEGA_SSH / DELEGA_SCP at tests/fakes/{ssh,scp}. Credentials are never
transferred: every upload goes through refuse_credentials().
"""
import os
import re
import shlex
import subprocess
import tempfile

from . import util

SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-o", "ServerAliveInterval=30"]
# Paths that must never travel to a node.
CREDENTIAL_PATTERNS = [r"(^|/)\.claude[^/]*(/|$)", r"(^|/)\.config/gcloud(/|$)", r"(^|/)\.ssh(/|$)",
                       r"(^|/)\.env(\.[^/]*)?$", r"(^|/)\.netrc$", r"(^|/)\.aws(/|$)"]


class RemoteError(Exception):
    pass


def alias(node):
    """SSH alias of a node (a config node dict or an alias string)."""
    return node["ssh"] if isinstance(node, dict) else node


def workdir(node):
    return node.get("workdir", "delega") if isinstance(node, dict) else "delega"


def jobs_subdir(node):
    return node.get("jobs_subdir", "jobs") if isinstance(node, dict) else "jobs"


def channel_rel(node, job_id):
    """Channel dir of a job on a node, relative to the node's $HOME."""
    return f"{workdir(node)}/{jobs_subdir(node)}/{job_id}"


def _ssh_prefix():
    return shlex.split(os.environ.get("DELEGA_SSH", "ssh"))


def _scp_prefix():
    return shlex.split(os.environ.get("DELEGA_SCP", "scp"))


def git_ssh_command():
    """GIT_SSH_COMMAND that makes git use the same ssh (and options) as everything else."""
    return " ".join(shlex.quote(x) for x in _ssh_prefix() + SSH_OPTS)


def refuse_credentials(path):
    p = str(path).replace(os.sep, "/")
    for pat in CREDENTIAL_PATTERNS:
        if re.search(pat, p):
            raise RemoteError(f"refusing to transfer credentials: {path}")


# ── kindling microVMs ───────────────────────────────────────────────────────
# A job on a `runner = "kindling"` node runs inside its own microVM on the kindling host.
# The job's node dict then carries `vm` (see job_node) and ssh/scp below go through
# `kling exec` / `kling cp` on the host, as the VM user, with paths relative to its home.
def job_node(cfg, meta):
    """Node dict of a job; a kindling job also carries its microVM (meta VM)."""
    n = cfg.node(meta["NODE"])
    if meta.get("VM"):
        n["vm"] = meta["VM"]
    return n


def host_node(node):
    """The node itself without its microVM: commands run on the kindling host."""
    return {k: v for k, v in node.items() if k != "vm"} if isinstance(node, dict) else node


def vm_home(node):
    return node.get("vm_home", "/home/worker")


def vm_user(node):
    return node.get("vm_user", "worker")


def vm_path(node, p):
    """Absolute path inside the VM of a path relative to its home (or `~/…`)."""
    p = str(p)
    if p.startswith("~/"):
        p = p[2:]
    return p if os.path.isabs(p) else f"{vm_home(node)}/{p}"


def vm_cmd(node, cmd, root=False):
    """Host command that runs `cmd` inside the job's VM (as the VM user unless root)."""
    q, home = shlex.quote, vm_home(node)
    who = "" if root else f"runuser -u {q(vm_user(node))} -- "
    return f"kling exec -w {q(home)} {q(node['vm'])} -- {who}env HOME={q(home)} bash -c {q(cmd)}"


def _vm_tmp(node):
    return f"/tmp/delega-{node['vm']}"


def ssh(node, cmd, input=None, timeout=60):
    """Run a shell command on a node (inside the job's VM if the node dict carries one).
    Never raises on a non-zero exit; a timeout is rc 124."""
    if isinstance(node, dict) and node.get("vm"):
        cmd = vm_cmd(node, cmd)
    argv = _ssh_prefix() + SSH_OPTS + [alias(node), cmd]
    try:
        return subprocess.run(argv, input=input, capture_output=True, text=True, timeout=timeout,
                              stdin=None if input is not None else subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(argv, 124, "", "timeout")
    except OSError as e:
        return subprocess.CompletedProcess(argv, 127, "", str(e))


def _scp(node, src, dst):
    argv = _scp_prefix() + ["-q"] + SSH_OPTS + [src, dst]
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL)
    except (subprocess.TimeoutExpired, OSError) as e:
        return subprocess.CompletedProcess(argv, 124, "", str(e))


def scp_to(node, src, dst):
    refuse_credentials(src)
    if isinstance(node, dict) and node.get("vm"):
        return _scp_to_vm(node, src, dst)
    return _scp(node, str(src), f"{alias(node)}:{dst}")


def scp_from(node, src, dst):
    if isinstance(node, dict) and node.get("vm"):
        return _scp_from_vm(node, src, dst)
    return _scp(node, f"{alias(node)}:{src}", str(dst))


def _scp_to_vm(node, src, dst):
    """Upload through the kindling host: scp to a host tmp dir, `kling cp` into the VM, chown."""
    q, host, vm = shlex.quote, host_node(node), node["vm"]
    tmp, path = f"{_vm_tmp(node)}/up-{os.path.basename(str(dst))}", vm_path(node, dst)
    r = ssh(host, f"mkdir -p {q(_vm_tmp(node))}")
    if r.returncode != 0:
        return r
    r = _scp(host, str(src), f"{alias(node)}:{tmp}")
    if r.returncode != 0:
        return r
    return ssh(host, f"kling cp {q(tmp)} {q(vm + ':' + path)} >/dev/null"
                     f" && kling exec {q(vm)} -- chown {q(vm_user(node))}: {q(path)}; rc=$?; rm -f {q(tmp)}; exit $rc")


def _scp_from_vm(node, src, dst):
    q, host, vm = shlex.quote, host_node(node), node["vm"]
    tmp = f"{_vm_tmp(node)}/down/{os.path.basename(str(src))}"
    r = ssh(host, f"mkdir -p {q(os.path.dirname(tmp))} && kling cp {q(vm + ':' + vm_path(node, src))} {q(tmp)} >/dev/null")
    if r.returncode != 0:
        return r
    r = _scp(host, f"{alias(node)}:{tmp}", str(dst))
    ssh(host, f"rm -f {q(tmp)}")
    return r


def must(r, what):
    if r.returncode != 0:
        raise RemoteError(f"{what}: {(r.stderr or r.stdout or '').strip()[:300] or f'rc={r.returncode}'}")
    return r


def _sha_remote(node, path):
    py = node.get("python", "python3") if isinstance(node, dict) else "python3"
    code = ("import hashlib,os,sys;p=os.path.expanduser(sys.argv[1]);"
            "print(hashlib.sha256(open(p,'rb').read()).hexdigest()[:12] if os.path.exists(p) else '')")
    r = ssh(node, f"{py} -c {shlex.quote(code)} {shlex.quote(path)}")
    return r.stdout.strip() if r.returncode == 0 else ""


def ensure_pyz(cfg, node):
    """Build delega.pyz from this package and upload it when the node's copy differs. Returns the sha."""
    with tempfile.TemporaryDirectory() as tmp:
        local = os.path.join(tmp, "delega.pyz")
        sha = util.build_pyz(local)
        dest = f"{workdir(node)}/delega.pyz"
        if _sha_remote(node, f"~/{dest}") != sha:
            must(ssh(node, f"mkdir -p {shlex.quote(workdir(node))}"), "mkdir workdir")
            must(scp_to(node, local, dest + ".new"), "upload delega.pyz")
            must(ssh(node, f"mv {shlex.quote(dest + '.new')} {shlex.quote(dest)}"), "install delega.pyz")
    return sha


def repo_name(repo_dir):
    """Repo name = parent dir of the git common dir (same for worktrees and the main checkout)."""
    r = subprocess.run(["git", "-C", repo_dir, "rev-parse", "--path-format=absolute", "--git-common-dir"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RemoteError(f"{repo_dir} is not a git repository")
    return os.path.basename(os.path.dirname(r.stdout.strip().rstrip("/")))


def bare_rel(node, repo):
    return f"{workdir(node)}/{repo}.git"


def _git_env():
    return {**os.environ, "GIT_SSH_COMMAND": git_ssh_command()}


def push_head(repo_dir, node, job_id, git=None):
    """Push HEAD to the node's bare repo as refs/heads/delega-base/<id> (created on first use).
    `git` = optional {user_name, user_email} configured in the bare repo for the worker's commits."""
    repo = repo_name(repo_dir)
    bare = shlex.quote(bare_rel(node, repo))
    init = f"test -d {bare} || {{ mkdir -p {shlex.quote(workdir(node))} && git init -q --bare {bare}"
    if git and git.get("user_email"):
        init += f" && git -C {bare} config user.email {shlex.quote(git['user_email'])}"
    if git and git.get("user_name"):
        init += f" && git -C {bare} config user.name {shlex.quote(git['user_name'])}"
    must(ssh(node, init + "; }"), "create bare repo")
    ref = f"delega-base/{job_id}"
    r = subprocess.run(["git", "-C", repo_dir, "push", "-q", f"{alias(node)}:{bare_rel(node, repo)}",
                        f"+HEAD:refs/heads/{ref}"], capture_output=True, text=True, env=_git_env())
    must(r, "git push")
    return ref


def branch_exists(node, repo, branch):
    r = ssh(node, f"git -C {shlex.quote(bare_rel(node, repo))} rev-parse -q --verify "
                  f"{shlex.quote('refs/heads/' + branch)} >/dev/null")
    return r.returncode == 0


def make_worktree(node, repo, job_id, branch):
    """Worktree for the job at <channel>/wt on `branch`. An existing branch (handoff) is reused,
    otherwise it is created from delega-base/<id>. Returns the worktree path relative to $HOME."""
    bare = shlex.quote(bare_rel(node, repo))
    chan = channel_rel(node, job_id)
    wt = f"{chan}/wt"
    if branch_exists(node, repo, branch):
        add = f"git -C {bare} worktree add -q \"$HOME\"/{shlex.quote(wt)} {shlex.quote(branch)}"
    else:
        add = (f"git -C {bare} worktree add -q -b {shlex.quote(branch)} \"$HOME\"/{shlex.quote(wt)} "
               f"{shlex.quote('delega-base/' + job_id)}")
    must(ssh(node, f"mkdir -p {shlex.quote(chan)} && {add}"), "git worktree add")
    return wt


def fetch_branch(repo_dir, node, repo, branch):
    """Bring the job branch back into repo_dir without touching its working tree. Returns (ok, log)."""
    r = subprocess.run(["git", "-C", repo_dir, "fetch", "-q", f"{alias(node)}:{bare_rel(node, repo)}",
                        f"+refs/heads/{branch}:refs/heads/{branch}"], capture_output=True, text=True,
                       env=_git_env())
    return r.returncode == 0, (r.stderr or "") + (r.stdout or "")

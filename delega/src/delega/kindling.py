"""kindling: a node whose jobs each run in an ephemeral microVM cloned from a golden image.

Node keys (`runner = "kindling"`): `ssh` (alias of the host running the kindling daemon),
`golden` (golden image, `{repo}` is replaced by the repo name; default `claude-{repo}`),
`vm_home` / `vm_user` (worker's home and user inside the VM), `vm_repo` (checkout inside the
golden, relative to vm_home; default `wt`), `golden_base_ref` (ref of that checkout the golden was
baked at; default `delega-base`), `egress_allow` (domains the VM may reach; default
["anthropic.com"]), `token_command` (argv that prints the worker's OAuth token on stdout),
`token_env` (variable the worker reads it from; default `CLAUDE_CODE_OAUTH_TOKEN`), `account`
(label recorded as the job's account), `keep_vm` (do not delete the VM at the end; debugging).

What travels: a git bundle with what HEAD has on top of the golden's base, the brief, run.sh and
delega.pyz. What comes back: the channel files and a bundle with the job branch. The token is read
by `token_command` on this machine, goes through ssh stdin to the host and into the worker's
environment with `kling exec -e`: it is never written to a disk, here, on the host or in the VM.
"""
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time

from . import remote, util

DEFAULT_EGRESS = ["anthropic.com"]
VM_PREFIX = "delega-"
NOT_JOBS = ("delega-seed-", "delega-load-", "delega-bake-")   # seed sandboxes, volume loaders, data-golden bakes


def is_kindling(node):
    return isinstance(node, dict) and node.get("runner") == "kindling"


def golden(node, repo):
    return node.get("golden", "claude-{repo}").replace("{repo}", re.sub(r"[^a-z0-9-]", "-", repo.lower()))


def vm_name(job_id):
    return VM_PREFIX + job_id


def vm_repo(node):
    return node.get("vm_repo", "wt")


def base_ref(node):
    return node.get("golden_base_ref", "delega-base")


def token_env(node):
    return node.get("token_env", "CLAUDE_CODE_OAUTH_TOKEN")


def keep_env(node):
    """Variables the broker must let through to the worker (DELEGA_KEEP_ENV)."""
    return f"{token_env(node)},CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"


def read_token(node):
    """The worker's token, from `token_command`. Raises RemoteError without printing it."""
    argv = node.get("token_command")
    if not argv:
        raise remote.RemoteError(f"node '{node['name']}': set token_command = [argv that prints the token]")
    try:
        r = subprocess.run(list(argv), capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise remote.RemoteError(f"token_command failed: {type(e).__name__}")
    tok = r.stdout.strip()
    if r.returncode != 0 or not tok:
        raise remote.RemoteError(f"token_command failed (rc={r.returncode}): {' '.join(argv[:3])}…")
    return tok


STATES = ("running", "frozen", "stopped", "failed", "starting", "creating", "paused")
LAUNCH_WINDOW_S = 900       # a VM whose job has no lease yet still counts this long (it is being set up)


def host_vms(node):
    """[(vm, state)] of the delega job VMs on the host (`kling ps`), or None if it does not answer."""
    r = remote.ssh(remote.host_node(node), "kling ps", timeout=30)
    if r.returncode != 0:
        return None
    out = []
    for line in r.stdout.splitlines():
        words = line.split()
        for w in words:
            if w.startswith(VM_PREFIX) and not w.startswith(NOT_JOBS):
                out.append((w, next((x for x in words if x in STATES), "")))
    return out


def live_vms(node):
    """Number of delega VMs on the host (alive or not), or None if it does not answer."""
    vms = host_vms(node)
    return None if vms is None else len(vms)


def job_dir_of(cfg, vm):
    """This machine's job dir for a VM `delega-<id>`, or None (job unknown to this state_dir)."""
    from . import jobs
    jid = vm[len(VM_PREFIX):]
    return next((d for d in jobs.job_dirs(cfg) if jobs.job_id(d) == jid), None)


def classify(cfg, node, vms):
    """Split the host's delega VMs into (active, orphans). Active = a job of this state_dir without END
    whose lease (read inside the VM) was renewed within `lease.stale_s`, or that is still being set up.
    orphans = [(vm, why)], why: ended (job has END), unknown (no such job here), frozen (stopped by its
    ttl, or by hand), stale (lease too old or final) or no_lease. Only ended and unknown are `gc`'s."""
    stale = float(cfg.policy("lease.stale_s"))
    active, orphans = [], []
    for vm, state in vms:
        d = job_dir_of(cfg, vm)
        meta = util.read_meta(d) if d else {}
        if not d:
            orphans.append((vm, "unknown"))
            continue
        if meta.get("END"):
            orphans.append((vm, "ended"))
            continue
        if state and state != "running":
            orphans.append((vm, "frozen"))
            continue
        chan = meta.get("CHANNEL")
        lease, age = remote.lease_age(remote.ssh(dict(node, vm=vm), remote.lease_cmd(chan), timeout=30)) \
            if chan else (None, None)
        if lease:
            fresh = not lease.get("final") and age < stale
            (active.append(vm) if fresh else orphans.append((vm, "stale")))
            continue
        try:
            young = time.time() - os.path.getmtime(os.path.join(d, "meta")) < LAUNCH_WINDOW_S
        except OSError:
            young = False
        (active.append(vm) if young else orphans.append((vm, "no_lease")))
    return active, orphans


def has_golden(node, repo):
    """Whether the host has this repo's golden (`kling template ls -q`); False if it does not answer."""
    r = remote.ssh(remote.host_node(node), "kling template ls -q", timeout=30)
    return r.returncode == 0 and golden(node, repo) in r.stdout.split()


def probe(cfg, node, profile, requires=()):
    """Contract dict of nodes.probe for a kindling host: reachable, room for one more VM, no compute."""
    res = {"ok": False, "reason": "", "fix": "", "kind": "", "email": node.get("account") or "",
           "compute": {}, "node": node["name"], "vms": None, "claude_version": None}
    a = remote.alias(node)
    vms = host_vms(node)
    if vms is None:
        res.update(kind="unreachable", reason=f"{a}: kling does not answer", fix=f"ssh {a} kling ps")
        return res
    active, orphans = classify(cfg, node, vms)
    n = len(active)
    res["vms"], res["orphans"] = n, orphans
    mx = node.get("max_jobs")
    if mx and n >= mx:
        extra = f"; orphans not counted: {', '.join(f'{v} ({w})' for v, w in orphans)}" if orphans else ""
        res.update(kind="resources", reason=f"{a}: {n} delega VMs with a live job (max_jobs={mx}){extra}",
                   fix=f"delega ps --all  # wait, or close finished jobs; then delega gc --node {node['name']}")
        return res
    for c in requires:
        res["compute"][c] = "unsupported"
    if requires:
        res.update(kind="compute", reason=f"kindling nodes do not provide {', '.join(requires)}")
        return res
    res.update(ok=True, kind="ok")
    return res


def ttl_min(cfg, caps, profile=None, project=None):
    """Lifetime of a job VM: the job's time cap + its grace + `kindling.ttl_margin_min`."""
    cap = float(caps.get("DELEGA_JOB_MAX_MIN") or cfg.policy("job.max_min", profile, project))
    grace = float(caps.get("DELEGA_JOB_GRACE_S") or cfg.policy("job.grace_s", profile, project)) / 60
    return int(cap + grace + float(cfg.policy("kindling.ttl_margin_min", profile, project)) + 0.999)


def create(node, vm, image, ttl=None, on_ttl="freeze", labels=None):
    """`kling run` the job VM from `image`. With ttl (minutes) the host freezes it (or removes it,
    on_ttl="remove": VMs with real data) when it runs out, whatever happens to this machine."""
    q = shlex.quote
    allow = ",".join(node.get("egress_allow", DEFAULT_EGRESS))
    extra = f" -ttl {int(ttl)}m -on-ttl {q(on_ttl)}" if ttl else ""
    extra += "".join(f" -label {q(f'{k}={v}')}" for k, v in (labels or {}).items())
    r = remote.ssh(remote.host_node(node), f"kling run -name {q(vm)} -from {q(image)} -egress allowlist "
                                          f"-allow {q(allow)}{extra} -wait-ready",
                   timeout=int(node.get("create_timeout_s", 300)))  # sin reflink el clon copia el disco entero
    return remote.must(r, f"kling run {vm} from {image}")


def no_room(err):
    """`kling run` refused the VM for lack of host memory (the probe counts VMs, not MiB)."""
    return "doesn't fit" in err


def destroy(node, vm):
    q = shlex.quote
    remote.ssh(remote.host_node(node), f"kling rm {q(vm)} >/dev/null 2>&1; rm -rf {q('/tmp/delega-' + vm)}",
               timeout=120)


def push_head(repo_dir, node, chan, job_id, git=None):
    """Bring this repo's HEAD into the VM checkout as refs/heads/delega-base-<id>. Only what HEAD has
    on top of the golden's base travels (all of HEAD if it does not descend from it). Returns the ref."""
    q, wt = shlex.quote, vm_repo(node)
    ref = f"delega-base-{job_id}"
    base = remote.must(remote.ssh(node, f"git -C {q(wt)} rev-parse {q(base_ref(node))}"),
                       "golden base ref").stdout.strip()
    head = subprocess.run(["git", "-C", repo_dir, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    setup = ""
    if git and git.get("user_email"):
        setup += f" && git -C {q(wt)} config user.email {q(git['user_email'])}"
    if git and git.get("user_name"):
        setup += f" && git -C {q(wt)} config user.name {q(git['user_name'])}"
    if git and git.get("strip_trailers"):
        setup += " && " + remote.strip_trailers_hook(q(wt + "/.git"))
    if head == base:
        remote.must(remote.ssh(node, f"git -C {q(wt)} branch -f {q(ref)} {q(base)}{setup}"), "branch from golden")
        return ref
    anc = subprocess.run(["git", "-C", repo_dir, "merge-base", "--is-ancestor", base, "HEAD"],
                         capture_output=True).returncode == 0
    tmp = tempfile.mkdtemp(prefix="delega-")
    bundle = os.path.join(tmp, "in.bundle")
    try:
        remote.must(subprocess.run(["git", "-C", repo_dir, "bundle", "create", "-q", bundle,
                                    f"{base}..HEAD" if anc else "HEAD"], capture_output=True, text=True),
                    "git bundle")
        remote.must(remote.scp_to(node, bundle, f"{chan}/in.bundle"), "upload bundle")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    remote.must(remote.ssh(node, f"git -C {q(wt)} fetch -q \"$HOME\"/{q(chan + '/in.bundle')} "
                                 f"+HEAD:refs/heads/{q(ref)}{setup}"), "git fetch bundle")
    return ref


def make_worktree(node, job_id, branch):
    q, wt = shlex.quote, vm_repo(node)
    remote.must(remote.ssh(node, f"git -C {q(wt)} checkout -q -B {q(branch)} {q('delega-base-' + job_id)}"),
                "git checkout")
    return wt


def fetch_branch(repo_dir, node, chan, job_dir, branch):
    """Bundle the job branch inside the VM, bring it here and fetch it. Returns (ok, log)."""
    q, wt = shlex.quote, vm_repo(node)
    out = f"{chan}/out.bundle"
    job = os.path.basename(chan.rstrip("/"))
    r = remote.ssh(node, f"git -C {q(wt)} bundle create -q \"$HOME\"/{q(out)} {q('delega-base-' + job)}..{q(branch)}"
                         f" 2>/dev/null || git -C {q(wt)} bundle create -q \"$HOME\"/{q(out)} {q(branch)}")
    if r.returncode != 0:
        return False, r.stderr + r.stdout
    r = remote.scp_from(node, out, job_dir + "/")
    if r.returncode != 0:
        return False, r.stderr + r.stdout
    r = subprocess.run(["git", "-C", repo_dir, "fetch", "-q", os.path.join(job_dir, "out.bundle"),
                        f"+refs/heads/{branch}:refs/heads/{branch}"], capture_output=True, text=True)
    return r.returncode == 0, (r.stderr or "") + (r.stdout or "")


def start_worker(node, chan, token):
    """Start run.sh detached inside the VM with the token in its environment (via ssh stdin)."""
    q, home, var = shlex.quote, remote.vm_home(node), token_env(node)
    inner = (f"setsid nohup runuser -u {q(remote.vm_user(node))} -- env HOME={q(home)} "
             f"bash -l {q(remote.vm_path(node, chan + '/run.sh'))} >/dev/null 2>&1 </dev/null &")
    cmd = (f"IFS= read -r {var}; export {var}; kling exec -e {var} -e CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 "
           f"-w {q(home)} {q(node['vm'])} -- sh -c {q(inner)}")
    return remote.ssh(remote.host_node(node), cmd, input=token + "\n", timeout=60)

"""integrate: dry-run merge of a job branch into a target branch, then the project's `verify`
commands on the merged result in a temporary worktree. Nothing in the repo is modified."""
import os
import shutil
import subprocess
import sys
import tempfile
import time

from . import audit, config, i18n

EXIT_CONFLICTS = 1
EXIT_VERIFY = 5
EXIT_MERGE_VERIFY = 7      # --merge: suite red, destination untouched
EXIT_MERGE_CONFLICTS = 8   # --merge: conflicts, destination untouched
IDENT = {"GIT_AUTHOR_NAME": "delega", "GIT_AUTHOR_EMAIL": "delega@localhost",
         "GIT_COMMITTER_NAME": "delega", "GIT_COMMITTER_EMAIL": "delega@localhost"}


def git(repo, *args, env=None):
    e = dict(os.environ, **(env or {}))
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, env=e)


def merge_dry_run(repo, into, branch):
    """(tree, conflicted_paths). tree is None when git could not even try."""
    r = git(repo, "merge-tree", "--write-tree", "--name-only", "--no-messages", into, branch)
    lines = r.stdout.splitlines()
    if r.returncode == 0:
        return lines[0].strip(), []
    if r.returncode == 1 and lines:
        conflicts = []
        for ln in lines[1:]:
            if not ln.strip():
                break
            conflicts.append(ln.strip())
        return lines[0].strip(), conflicts
    raise RuntimeError((r.stderr or r.stdout).strip() or "git merge-tree failed")


def merged_commit(repo, tree, into, branch):
    """Throw-away commit of the merged tree (parents: into, branch) so it can be checked out."""
    ident = {"GIT_AUTHOR_NAME": "delega", "GIT_AUTHOR_EMAIL": "delega@localhost",
             "GIT_COMMITTER_NAME": "delega", "GIT_COMMITTER_EMAIL": "delega@localhost"}
    r = git(repo, "commit-tree", tree, "-p", into, "-p", branch, "-m", "delega integrate (dry run)", env=ident)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip())
    return r.stdout.strip()


def run_verify(repo, sha, commands, timeout):
    """Run commands in a temp worktree of `sha`. Returns [(command, rc, seconds)]."""
    tmp = tempfile.mkdtemp(prefix="delega-integrate-")
    wt = os.path.join(tmp, "wt")
    results = []
    try:
        r = git(repo, "worktree", "add", "--detach", wt, sha)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.strip())
        for cmd in commands:
            t0 = time.time()
            try:
                rc = subprocess.run(cmd, shell=True, cwd=wt, timeout=timeout).returncode
            except subprocess.TimeoutExpired:
                rc = 124
            results.append((cmd, rc, time.time() - t0))
            if rc != 0:
                break
    finally:
        git(repo, "worktree", "remove", "--force", wt)
        shutil.rmtree(tmp, ignore_errors=True)
    return results


def run_verify_remote(cfg, node_name, repo, sha, commands, timeout):
    """Same as run_verify but on a node: the merged commit is pushed to the node's bare repo under a
    temporary ref, checked out in a temp worktree there, and everything is removed afterwards."""
    import shlex
    import subprocess as sp
    from . import remote
    node = cfg.node(node_name)
    name = remote.repo_name(repo)
    bare_rel = remote.bare_rel(node, name)
    bare = shlex.quote(bare_rel)
    ref = f"refs/heads/delega-integrate/{sha[:12]}"
    wt = f"{remote.workdir(node)}/integrate-{sha[:12]}"
    wtq = shlex.quote(wt)
    try:
        remote.must(remote.ssh(node, f"test -d {bare} || {{ mkdir -p {shlex.quote(remote.workdir(node))} "
                                     f"&& git init -q --bare {bare}; }}"), "bare repo")
        remote.must(sp.run(["git", "-C", repo, "push", "-q", f"{remote.alias(node)}:{bare_rel}", f"+{sha}:{ref}"],
                           capture_output=True, text=True, env=remote._git_env()), "git push")
        remote.must(remote.ssh(node, f"rm -rf {wtq}; git -C {bare} worktree add -q --detach \"$HOME\"/{wtq} {shlex.quote(sha)}"),
                    "git worktree add")
    except remote.RemoteError as e:
        raise RuntimeError(str(e))
    results = []
    try:
        for cmd in commands:
            t0 = time.time()
            r = remote.ssh(node, f"cd \"$HOME\"/{wtq} && {cmd}", timeout=timeout)
            results.append((cmd, r.returncode, time.time() - t0))
            if r.returncode != 0:
                break
    finally:
        remote.ssh(node, f"git -C {bare} worktree remove --force \"$HOME\"/{wtq}; rm -rf {wtq}; "
                         f"git -C {bare} worktree prune; git -C {bare} update-ref -d {shlex.quote(ref)}")
    return results


def real_merge(repo, into, branch, tree):
    """Merge for real with --no-ff. Returns the new commit sha of `into`."""
    msg = f"Merge branch '{branch}' into {into}"
    cur = git(repo, "symbolic-ref", "-q", "--short", "HEAD").stdout.strip()
    if cur == into:
        r = git(repo, "merge", "--no-ff", "-m", msg, branch, env=IDENT)
        if r.returncode != 0:
            git(repo, "merge", "--abort")
            raise RuntimeError((r.stderr or r.stdout).strip())
        return git(repo, "rev-parse", "HEAD").stdout.strip()
    old = git(repo, "rev-parse", into).stdout.strip()
    c = git(repo, "commit-tree", tree, "-p", into, "-p", branch, "-m", msg, env=IDENT)
    if c.returncode != 0:
        raise RuntimeError(c.stderr.strip())
    new = c.stdout.strip()
    r = git(repo, "update-ref", f"refs/heads/{into}", new, old)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip())
    return new


def job_for_branch(cfg, branch):
    from . import jobs, util
    for d in reversed(jobs.job_dirs(cfg)):
        if util.read_meta(d).get("BRANCH") == branch:
            return d
    return None


def main(argv):
    into, timeout, pos, a = "main", 1800, [], list(argv)
    merge, node_name = False, None
    while a:
        x = a.pop(0)
        if x == "--into" and a:
            into = a.pop(0)
        elif x == "--merge":
            merge = True
        elif x == "--node" and a:
            node_name = a.pop(0)
        elif x == "--timeout" and a:
            timeout = int(a.pop(0))
        elif x in ("-h", "--help"):
            print(i18n.t("integrate.usage"))
            return 0
        else:
            pos.append(x)
    if len(pos) != 2:
        print(i18n.t("integrate.usage"), file=sys.stderr)
        return 2
    repo, branch = os.path.abspath(pos[0]), pos[1]
    try:
        cfg = config.load()
    except config.ConfigError as e:
        print(e, file=sys.stderr)
        return 2
    project = cfg.project_for(repo)
    prof = cfg.profile(cfg.profile_for(repo))
    base = git(repo, "merge-base", into, branch).stdout.strip() or None
    sha = git(repo, "rev-parse", "--short", branch).stdout.strip()
    job_dir = job_for_branch(cfg, branch) if merge else None
    job = os.path.basename(job_dir) if job_dir else None
    common = dict(job=job, profile=prof["name"], branch=branch, base=base, project=project, into=into, sha=sha)
    try:
        tree, conflicts = merge_dry_run(repo, into, branch)
    except RuntimeError as e:
        print(i18n.t("integrate.git_failed", err=e), file=sys.stderr)
        return 2
    if conflicts:
        print(i18n.t("integrate.conflicts", branch=branch, into=into, n=len(conflicts)))
        for c in conflicts:
            print("  " + c)
        audit.record(cfg, "integrate", result="conflicts", conflicts=len(conflicts),
                     **({"merge": True, "files": conflicts} if merge else {}), **common)
        return EXIT_MERGE_CONFLICTS if merge else EXIT_CONFLICTS
    print(i18n.t("integrate.clean", branch=branch, into=into) if not merge else
          i18n.t("integrate.clean_merge", branch=branch, into=into))
    commands = list((cfg.projects.get(project) or {}).get("verify", [])) if project else []
    if not commands:
        print(i18n.t("integrate.no_verify"))
        return finish(cfg, repo, into, branch, tree, merge, job_dir, [], common, unverified=True)
    try:
        mc = merged_commit(repo, tree, into, branch)
        if node_name:
            results = run_verify_remote(cfg, node_name, repo, mc, commands, timeout)
        else:
            results = run_verify(repo, mc, commands, timeout)
    except RuntimeError as e:
        print(i18n.t("integrate.git_failed", err=e), file=sys.stderr)
        return 2
    for cmd, rc, secs in results:
        print(i18n.t("integrate.ok" if rc == 0 else "integrate.fail", cmd=cmd, rc=rc, secs=int(secs)))
    failed = any(rc != 0 for _, rc, _ in results)
    return finish(cfg, repo, into, branch, tree, merge, job_dir, results, common, failed=failed, node=node_name)


def finish(cfg, repo, into, branch, tree, merge, job_dir, results, common, failed=False, unverified=False, node=None):
    if not merge:
        if unverified:
            audit.record(cfg, "integrate", result="clean", verified=False, **common)
            return 0
        audit.record(cfg, "integrate", result="verify_failed" if failed else "verified", verified=not failed, **common)
        return EXIT_VERIFY if failed else 0
    if job_dir and results:
        with open(os.path.join(job_dir, "integrate.log"), "w", encoding="utf-8") as f:
            for cmd, rc, secs in results:
                f.write(f"{'ok' if rc == 0 else 'FAIL'}\trc={rc}\t{int(secs)}s\t{cmd}\n")
    extra = {"merge": True, "node": node}
    if failed:
        print(i18n.t("integrate.merge_blocked", into=into))
        audit.record(cfg, "integrate", result="verify_failed", verified=False, **extra, **common)
        return EXIT_MERGE_VERIFY
    try:
        new = real_merge(repo, into, branch, tree)
    except RuntimeError as e:
        print(i18n.t("integrate.git_failed", err=e), file=sys.stderr)
        return 2
    print(i18n.t("integrate.merged", branch=branch, into=into, sha=new[:12]))
    audit.record(cfg, "integrate", result="merged", verified=not unverified, merge_sha=new[:12], **extra, **common)
    return 0

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


def main(argv):
    into, timeout, pos, a = "main", 1800, [], list(argv)
    while a:
        x = a.pop(0)
        if x == "--into" and a:
            into = a.pop(0)
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
    common = dict(job=None, profile=prof["name"], branch=branch, base=base, project=project, into=into, sha=sha)
    try:
        tree, conflicts = merge_dry_run(repo, into, branch)
    except RuntimeError as e:
        print(i18n.t("integrate.git_failed", err=e), file=sys.stderr)
        return 2
    if conflicts:
        print(i18n.t("integrate.conflicts", branch=branch, into=into, n=len(conflicts)))
        for c in conflicts:
            print("  " + c)
        audit.record(cfg, "integrate", result="conflicts", conflicts=len(conflicts), **common)
        return EXIT_CONFLICTS
    print(i18n.t("integrate.clean", branch=branch, into=into))
    commands = list((cfg.projects.get(project) or {}).get("verify", [])) if project else []
    if not commands:
        print(i18n.t("integrate.no_verify"))
        audit.record(cfg, "integrate", result="clean", verified=False, **common)
        return 0
    try:
        results = run_verify(repo, merged_commit(repo, tree, into, branch), commands, timeout)
    except RuntimeError as e:
        print(i18n.t("integrate.git_failed", err=e), file=sys.stderr)
        return 2
    for cmd, rc, secs in results:
        print(i18n.t("integrate.ok" if rc == 0 else "integrate.fail", cmd=cmd, rc=rc, secs=int(secs)))
    failed = any(rc != 0 for _, rc, _ in results)
    audit.record(cfg, "integrate", result="verify_failed" if failed else "verified", verified=not failed, **common)
    return EXIT_VERIFY if failed else 0

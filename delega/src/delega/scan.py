"""scan: look for forbidden terms (client names, …) in a branch, a brief or a text.

Terms file: one term per line, `#` comments, matched case-insensitively as plain text.
Matched text is never printed: reports show file paths and counts only."""
import os
import re
import subprocess
import sys

from . import config, i18n

EXIT_HITS = 9
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbb3d1b4"


def load_terms(path):
    with open(os.path.expanduser(path), encoding="utf-8") as f:
        return [ln.strip() for ln in f if ln.strip() and not ln.lstrip().startswith("#")]


def _rx(terms):
    return re.compile("|".join(re.escape(t) for t in sorted(terms, key=len, reverse=True)), re.I) if terms else None


def scan_text(text, terms):
    """Terms (as written in the terms file) that occur in text."""
    low = text.lower()
    return [t for t in terms if t.lower() in low]


def redact(text, terms, mark="[redacted]"):
    rx = _rx(terms)
    return rx.sub(mark, text) if rx else text


def _count(text, rx):
    return len(rx.findall(text)) if rx else 0


def _git(repo_dir, *args):
    r = subprocess.run(["git", "-C", repo_dir, *args], capture_output=True, text=True, errors="replace")
    return r.stdout if r.returncode == 0 else ""


def default_base(repo_dir, branch):
    for cand in ("origin/HEAD", "origin/main", "origin/master", "main", "master"):
        mb = _git(repo_dir, "merge-base", cand, branch).strip()
        if mb:
            return mb
    return EMPTY_TREE


def scan_branch(repo_dir, branch, base, terms):
    """Hits in the changes base..branch: {files: [{path, count}], commit_lines: [{sha, count}]}.
    Looks at file paths, added lines and commit messages (all lines)."""
    rx = _rx(terms)
    base = base or default_base(repo_dir, branch)
    files, commits = {}, []
    if not rx:
        return {"files": [], "commit_lines": []}
    for path in _git(repo_dir, "diff", "--name-only", f"{base}..{branch}").splitlines():
        if rx.search(path):
            files[path] = files.get(path, 0) + 1
    cur = None
    for line in _git(repo_dir, "diff", "-U0", "--no-color", f"{base}..{branch}").splitlines():
        if line.startswith("+++ "):
            cur = line[6:] if line.startswith("+++ b/") else None
        elif line.startswith("+") and cur:
            n = _count(line[1:], rx)
            if n:
                files[cur] = files.get(cur, 0) + n
    rng = branch if base == EMPTY_TREE else f"{base}..{branch}"
    for sha in _git(repo_dir, "rev-list", rng).split():
        n = _count(_git(repo_dir, "show", "-s", "--format=%B%n%an <%ae>", sha), rx)
        if n:
            commits.append({"sha": sha[:10], "count": n})
    return {"files": [{"path": p, "count": c} for p, c in sorted(files.items())], "commit_lines": commits}


def _terms_path(args, cfg, repo_dir):
    """--terms, else the forbidden_terms_file of --profile / the profile matching the repo."""
    if args.get("terms"):
        return config.expand(args["terms"])
    prof = cfg.profile(args.get("profile") or cfg.profile_for(repo_dir or "."))
    path = prof.get("forbidden_terms_file")
    return config.expand(path) if path else None


def main(argv):
    args, pos, a = {}, [], list(argv)
    while a:
        x = a.pop(0)
        if x in ("--terms", "--profile", "--text") and a:
            args[x[2:]] = a.pop(0)
        elif x in ("-h", "--help"):
            print(i18n.t("scan.usage"))
            return 0
        else:
            pos.append(x)
    if not args.get("text") and len(pos) < 2:
        print(i18n.t("scan.usage"), file=sys.stderr)
        return 2
    try:
        cfg = config.load()
        path = _terms_path(args, cfg, pos[0] if pos and not args.get("text") else None)
    except config.ConfigError as e:
        print(e, file=sys.stderr)
        return 2
    if not path:
        print(i18n.t("scan.no_terms"), file=sys.stderr)
        return 0
    try:
        terms = load_terms(path)
    except OSError:
        print(i18n.t("scan.unreadable", path=path), file=sys.stderr)
        return 2
    if args.get("text"):
        try:
            with open(args["text"], encoding="utf-8", errors="replace") as f:
                n = _count(f.read(), _rx(terms))
        except OSError:
            print(i18n.t("scan.unreadable", path=args["text"]), file=sys.stderr)
            return 2
        print(i18n.t("scan.text_hits", n=n) if n else i18n.t("scan.clean"))
        return EXIT_HITS if n else 0
    res = scan_branch(pos[0], pos[1], pos[2] if len(pos) > 2 else None, terms)
    if not (res["files"] or res["commit_lines"]):
        print(i18n.t("scan.clean"))
        return 0
    for f in res["files"]:
        print(i18n.t("scan.file_hits", path=f["path"], n=f["count"]))
    for c in res["commit_lines"]:
        print(i18n.t("scan.commit_hits", sha=c["sha"], n=c["count"]))
    return EXIT_HITS

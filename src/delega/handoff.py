"""handoff: draft a fresh brief to continue a job without `--resume` (which re-sends the whole
conversation without cache). The orchestrator fills in what is left, the finish line and the cap."""
import glob
import json
import os
import sys

from . import audit, config, i18n, util


def resolve(cfg, pattern):
    from . import jobs
    fn = getattr(jobs, "resolve", None)
    if fn:
        return fn(cfg, pattern)
    hits = sorted(glob.glob(os.path.join(cfg.state_dir, f"*{pattern}*" if pattern != "last" else "[0-9]*")))
    hits = [h for h in hits if os.path.isdir(h)]
    if not hits:
        raise FileNotFoundError(pattern)
    return hits[-1]


def last_report(job_dir, limit=3000):
    """Final report of the worker: last line of results.jsonl (legacy name accepted), flattened."""
    try:
        with open(util.channel_file(job_dir, "results.jsonl"), encoding="utf-8") as f:
            rows = [json.loads(line) for line in f if line.strip()]
    except (OSError, ValueError):
        return None
    return " ".join(str(rows[-1].get("result", "")).split())[:limit] if rows else None


def _fetch_results(cfg, job_dir, meta):
    """Remote jobs: bring results.jsonl home if the job dir has no copy yet."""
    if meta.get("NODE", "local") == "local" or os.path.exists(util.channel_file(job_dir, "results.jsonl")):
        return
    try:
        from . import remote
        remote.scp_from(cfg.node(meta["NODE"]), f"{meta.get('CHANNEL', '')}/results.jsonl", job_dir)
    except Exception:  # offline node, missing helper…: the draft just says "no report"
        pass


def render(job_dir, meta, report, loc):
    t = lambda k, **kw: i18n.t(k, loc=loc, **kw)
    node, branch = meta.get("NODE") or "local", meta.get("BRANCH")
    try:
        brief = open(os.path.join(job_dir, "brief.md"), encoding="utf-8").read().rstrip()
    except OSError:
        brief = t("handoff.no_brief")
    where = t("handoff.node", node=node) + (t("handoff.branch", branch=branch) if branch else "")
    lines = [t("handoff.title", job=os.path.basename(job_dir)), "", t("handoff.intro"), "",
             "## " + t("handoff.h_original"), "", brief, "",
             "## " + t("handoff.h_where"), "", "- " + where,
             "- " + t("handoff.last_report"), "", "> " + (report or t("handoff.no_report")), "",
             "## " + t("handoff.h_missing"), "", "- …", "",
             "## " + t("handoff.h_done"), "", "- …", "",
             "## " + t("handoff.h_cap"), "", "- " + t("handoff.cap_line"), "",
             "## " + t("handoff.h_report"), "", t("handoff.report_line"), ""]
    return "\n".join(lines)


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(i18n.t("handoff.usage"))
        return 0 if argv else 2
    try:
        cfg = config.load()
        job_dir = resolve(cfg, argv[0])
    except (config.ConfigError, FileNotFoundError, ValueError, LookupError) as e:
        print(e if isinstance(e, config.ConfigError) else i18n.t("handoff.no_job", job=argv[0]), file=sys.stderr)
        return 2
    meta = util.read_meta(job_dir)
    _fetch_results(cfg, job_dir, meta)
    out = argv[1] if len(argv) > 1 else os.path.join(os.getcwd(), f"handoff-{'-'.join(os.path.basename(job_dir).split('-')[:3])}.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write(render(job_dir, meta, last_report(job_dir), cfg.locale))
    audit.record(cfg, "handoff", job=os.path.basename(job_dir), profile=meta.get("PROFILE"), node=meta.get("NODE"),
                 branch=meta.get("BRANCH"), project=meta.get("PROJECT"), owner=meta.get("OWNER"), out=out)
    print(i18n.t("handoff.written", out=out) + (i18n.t("handoff.same_branch", branch=meta["BRANCH"]) if meta.get("BRANCH") else ""))
    return 0

"""preflight: node preconditions before a batch of jobs (`delega doctor --batch N [repo]`).

Per node of the profile: reachable, disk, load, RAM, account, the compute the profile declares (an
`expired` one refuses the node), claude MAJOR.MINOR equal to this machine's, and for kindling hosts that
`kling ps` answers, the free VM slots and the repo golden (with its lockfile fingerprint). Each refused
node goes to the audit log as `refused` (scope = node, stage = batch) with its reason and fix. Exit 0
when the nodes that pass have room for the N jobs, 1 otherwise.
"""
from . import jobs, kindling, nodes
from .i18n import t

OK, WARN, FAIL = "✓", "!", "✗"


def _slots(mx, live, n):
    return max(0, mx - live) if mx else n


def batch(cfg, profile, n, repo_dir=None, say=print):
    from . import remote
    repo = remote.repo_name(repo_dir) if repo_dir else None
    project = cfg.project_for(repo_dir) if repo_dir else None
    watch = nodes.watched(profile)
    live = jobs.live_counts(cfg)
    lv = nodes.claude_version(profile["launcher"], profile.get("config_dir"))
    say(t("nodes.batch.title", profile=profile["name"], n=n, order=" → ".join(nodes.auto_order(profile))))
    total = 0
    for node in cfg.nodes_for(profile):
        name, mx = node["name"], node.get("max_jobs")
        r = nodes.probe(cfg, node, profile, (), watch=watch)
        if r.get("ok") and not kindling.is_kindling(node) and lv and not nodes.same_minor(lv, r.get("claude_version")):
            r = dict(r, ok=False, kind="version",
                     reason=t("nodes.reason.version", remote=r.get("claude_version") or "?", local=lv),
                     fix=f"ssh {remote.alias(node)} 'claude install {lv}'")
        if not r.get("ok"):
            nodes._refused(cfg, profile, name, r, "batch")
            say(f"  {FAIL} {name}: {r.get('reason')}")
            if r.get("fix"):
                say(f"      {t('doctor.fix')}: {r['fix']}")
            continue
        used = max(live.get(name, 0), r.get("vms") or 0)
        free = _slots(mx, used, n)
        total += free
        say(f"  {OK} {name}: {nodes._status(r)} · {t('nodes.batch.slots', free=free)}")
        if kindling.is_kindling(node) and repo:
            g = kindling.golden(node, repo)
            if not kindling.has_golden(node, repo):
                say(f"  {WARN} {name}: {t('nodes.skip.no_golden', golden=g, fix=f'delega golden build --node {name}')}")
            else:
                from . import golden as _golden
                _golden.ensure_fresh(cfg, profile, node, project, repo_dir, g,
                                     say=lambda s: say(f"  {WARN} {name}: {s}"))
    lp = nodes.local_probe(cfg, profile, (), watch)
    local_free = _slots(cfg.policy("max_local_jobs", profile), live.get("local", 0), n) if lp.get("ok") else 0
    say(f"  {OK if lp.get('ok') else FAIL} local: {t('nodes.batch.slots', free=local_free)}"
        + ("" if lp.get("ok") else f" — {lp.get('reason')} ({t('doctor.fix')}: {lp.get('fix')})"))
    enough = total >= n
    say(t("nodes.batch.ok" if enough else "nodes.batch.short", nodes=total, local=local_free, n=n))
    return 0 if enough else 1

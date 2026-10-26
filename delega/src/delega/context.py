"""context: what the worker knows about its own job (node, profile, caps, budget, branch, id, databases).

launch writes `context.md` into the channel; the broker appends it to the worker's system prompt, after
the channel protocol. The same facts go to the worker's environment as DELEGA_JOB_* variables.
"""
import os

from .i18n import t


def job_env(job_id, node, profile, branch):
    return {"DELEGA_JOB_ID": job_id, "DELEGA_NODE": node, "DELEGA_PROFILE": profile["name"],
            "DELEGA_BRANCH": branch}


def render(job_id, profile, account, node, branch, base, project, zone, caps, budget, vm=None, image=None,
           egress=None, dbs=None, db_mode=None):
    spent, limit = budget
    where = node + (t("context.vm", vm=vm, image=image) if vm else "")
    text = t("context.text", job=job_id, profile=profile["name"], account=account or "?", node=where,
             branch=branch or "-", base=(base or "")[:12] or "-", project=project or "-",
             zone=", ".join(zone) if zone else t("context.whole_repo"),
             caps=t("broker.caps_text", min=caps.get("DELEGA_JOB_MAX_MIN"), turns=caps.get("DELEGA_JOB_MAX_TURNS"),
                    usd=caps.get("DELEGA_JOB_MAX_USD")),
             spent=f"{spent:.2f}", limit=f"{limit:.2f}")
    if dbs:
        from . import databases
        lines = []
        for s in dbs:
            lines.append(t("context.db_line", name=s["name"], engine=s["engine"], version=s.get("version") or "",
                           database=s["database"], vars=", ".join(f"`{v}`" for v in s["env"]),
                           url=databases.values(s)["url"]))
        text += "\n\n" + t("context.dbs", lines="\n".join(lines), mode=t("context.db_mode." + (db_mode or "volume")),
                           egress=", ".join(egress or []) or "-")
    return text.rstrip() + "\n"


def write(job_dir, text):
    path = os.path.join(job_dir, "context.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path

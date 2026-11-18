"""job-state: read-only state of a task's job, asked by another daemon over ssh.

    delega job-state --task <id> --rama <rama> --job-id <id> [--flujo <flujo>] [--repo <dir>] --json

Prints one JSON object: {"estado": running|ok|fail|queued|no_encontrado, "job_id": ..., "sha": ..., "node": ...}.
It uses the same logic as `wave.Env.job_state` against THIS machine's state_dir. It never writes (it creates
no directory or file). `no_encontrado`: a real job id with no job directory here (not a failure).
"""
import json
import sys

from . import i18n


def _parse(argv):
    vals, as_json, i = {}, False, 0
    while i < len(argv):
        a = argv[i]
        if a == "--json":
            as_json = True
        elif a.startswith("--") and "=" in a:
            k, v = a[2:].split("=", 1)
            vals[k] = v
        elif a.startswith("--") and i + 1 < len(argv):
            vals[a[2:]] = argv[i + 1]
            i += 1
        else:
            raise ValueError(i18n.t("jobstate.usage"))
        i += 1
    return vals, as_json


def job_info(task, repo_dir=None):
    """(estado, job_id, sha, node) of the task's job on this machine."""
    from . import util, wave
    env = wave.Env(repo_dir or ".")
    found = env._job_dir(task)
    meta = util.read_meta(found) if found else {}
    if found and not repo_dir and meta.get("REPO_DIR"):
        env = wave.Env(meta["REPO_DIR"])
    state, jid, sha = env.job_state(task, write=False)
    return state, jid, sha, meta.get("NODE") or None


def main(argv):
    try:
        vals, as_json = _parse(argv)
        if not vals.get("task") or not vals.get("job-id"):
            raise ValueError(i18n.t("jobstate.usage"))
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2
    task = {"id": vals["task"], "rama": vals.get("rama"), "job_id": vals["job-id"],
            "flujo": vals.get("flujo") or "f"}
    state, jid, sha, node = job_info(task, vals.get("repo"))
    payload = {"estado": state, "job_id": jid, "sha": sha, "node": node}
    if as_json:
        print(json.dumps(payload))
    else:
        print(i18n.t("jobstate.line", id=vals["task"], estado=state, job=jid))
    return 0

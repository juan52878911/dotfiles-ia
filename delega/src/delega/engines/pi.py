"""Pi engine (`pi -p --mode json`, e.g. MiniMax): a delega job like any other.

`python -m delega.engines.pi run <channel> <brief> -- <pi argv…>` runs Pi in the current directory (the
worktree), translates its JSON lines into the channel files `ps`/`watch`/`spend` read: lease.json,
state.json, progress.log, events.jsonl (raw), result.json, summary.json and rc. The wall-clock cap
(DELEGA_JOB_MAX_MIN) kills the process. Only for personal profiles: see `refuse_reason`.
"""
import json
import os
import signal
import subprocess
import sys
import threading
import time

from .. import util
from ..i18n import t

ENGINE = "pi"
FORBIDDEN_ALIASES = {"pi", "minimax"}
DEFAULT_MODEL = "minimax/MiniMax-M3"
CAP_RC = 124


def refuse_reason(profile, model=None):
    """Why this profile may not run Pi (None = allowed): `pi` or `minimax` in its forbidden_engines."""
    bad = {str(x).lower() for x in profile.get("forbidden_engines") or []} & FORBIDDEN_ALIASES
    if bad:
        return t("engine.pi.forbidden", profile=profile["name"], engines=", ".join(sorted(bad)))
    return None


def pi_argv(model=None):
    cmd = os.environ.get("DELEGA_PI_CMD") or "pi"
    argv = [cmd, "-p", "--mode", "json"]
    argv += ["--model", model or DEFAULT_MODEL]
    return argv


def runner_argv(python=None, pyz=None):
    """argv prefix that runs this module (from the package on disk, or from a delega.pyz on a node)."""
    if pyz:
        return ["env", f"PYTHONPATH={pyz}", python or "python3", "-m", "delega.engines.pi"]
    return [sys.executable, "-m", "delega.engines.pi"]


def _num(name, default):
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return float(default)


def _text(x):
    if isinstance(x, str):
        return x
    if isinstance(x, list):
        return "".join(_text(i) for i in x)
    if isinstance(x, dict):
        return _text(x.get("text") or x.get("content") or "")
    return ""


def describe_tool(e):
    args = e.get("args") or e.get("input") or {}
    key = (args.get("command") or args.get("path") or args.get("file_path") or args.get("pattern")
           or json.dumps(args, ensure_ascii=False)) if isinstance(args, dict) else str(args)
    return f"{e.get('toolName') or e.get('name') or '?'}: {util.short(str(key), 90)}"


def run(chan, brief, command):
    os.makedirs(chan, exist_ok=True)
    p = lambda n: os.path.join(chan, n)
    max_min = _num("DELEGA_JOB_MAX_MIN", "60")
    max_turns = int(_num("DELEGA_JOB_MAX_TURNS", "300"))
    start = time.time()
    lock = threading.Lock()
    est = {"state": "running", "start": start, "last_event": start, "last_action": "starting", "tools": 0,
           "turns": 0, "alerts": 0, "messages": 0, "session_id": None, "rc": None, "pid": os.getpid(),
           "model": None, "cost_usd": None, "cap": None, "engine": ENGINE,
           "caps": {"min": max_min, "turns": max_turns}}
    lease = {"pid": os.getpid(), "host": os.uname().nodename, "renewed": start}

    def save():
        with lock:
            util.write_json(p("state.json"), est)

    def write_lease(final=False):
        lease["renewed"] = time.time()
        if final:
            lease["final"] = True
        with lock:
            util.write_json(p("lease.json"), lease)

    def log(kind, text):
        util.append(p("progress.log"), f"{util.now()}\t{kind}\t{util.short(' '.join(str(text).split()), 400)}")

    for n in ("inbox.jsonl", "notices.jsonl"):
        open(p(n), "a").close()
    if "--model" in command:
        est["model"] = command[command.index("--model") + 1]
    save()
    write_lease()
    with open(brief, encoding="utf-8") as f:
        prompt = f.read()
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE", "ANTHROPIC"))}
    env["DELEGA_CHANNEL"] = chan
    try:
        proc = subprocess.Popen(command + [prompt], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=open(p("stderr.log"), "a"), text=True, env=env, start_new_session=True)
    except OSError as e:
        log("ALERT:start", f"{command[0]}: {e}")
        est.update(state="finished", rc=127, last_action="start failed")
        return _finish(chan, est, start, 127, "", True)
    log("START", os.path.basename(command[0]))
    stop = threading.Event()
    cut = []

    def kill(why):
        cut.append(why)
        est["cap"] = why
        log("ALERT:cap", why)
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            proc.kill()

    def watchdog():
        while not stop.wait(min(1.0, _num("DELEGA_LEASE_RENEW_S", "30"))):
            write_lease()
            if time.time() - start >= max_min * 60 and not cut:
                kill(t("engine.pi.cap_min", min=f"{max_min:g}"))
            save()

    threading.Thread(target=watchdog, daemon=True).start()
    final, errored = "", False
    with open(p("events.jsonl"), "a", encoding="utf-8") as raw:
        for line in proc.stdout:
            raw.write(line)
            raw.flush()
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if not isinstance(e, dict):
                continue
            est["last_event"] = time.time()
            kind = e.get("type")
            if kind == "session":
                est["session_id"] = e.get("id") or e.get("sessionId")
            elif kind == "tool_execution_start":
                est["tools"] += 1
                est["last_action"] = describe_tool(e)
                log("TOOL", est["last_action"])
            elif kind == "tool_execution_end" and e.get("isError"):
                log("ALERT:tool_error", e.get("toolName") or "?")
                est["alerts"] += 1
            elif kind == "turn_end":
                est["turns"] += 1
                if est["turns"] >= max_turns and not cut:
                    kill(t("engine.pi.cap_turns", turns=max_turns))
            elif kind == "message_end":
                m = e.get("message") or {}
                if m.get("role") == "assistant":
                    est["messages"] += 1
                    txt = _text(m.get("content"))
                    if txt.strip():
                        final = txt
                    if m.get("errorMessage") or m.get("stopReason") == "error":
                        errored = True
                        log("ALERT:model", m.get("errorMessage") or "error")
                    est["model"] = m.get("model") or est["model"]
                    cost = ((m.get("usage") or {}).get("cost") or {}).get("total")
                    if isinstance(cost, (int, float)):
                        est["cost_usd"] = round((est["cost_usd"] or 0) + cost, 6)
            elif kind == "agent_end" and not final:
                for m in reversed(e.get("messages") or []):
                    if m.get("role") == "assistant" and _text(m.get("content")).strip():
                        final = _text(m.get("content"))
                        break
            if kind in ("tool_execution_start", "turn_end", "agent_end"):
                save()
    rc = proc.wait()
    stop.set()
    if cut:
        rc = CAP_RC
    elif rc == 0 and errored and not final:
        rc = 1
    return _finish(chan, est, start, rc, final, errored or bool(cut) or rc != 0)


def _finish(chan, est, start, rc, final, is_error):
    p = lambda n: os.path.join(chan, n)
    est.update(state="finished", rc=rc, last_event=time.time())
    util.write_json(p("state.json"), est)
    util.write_json(p("result.json"), {"type": "result", "subtype": "error" if is_error else "success",
                                       "result": final, "is_error": bool(is_error), "session_id": est["session_id"],
                                       "total_cost_usd": est["cost_usd"], "num_turns": est["turns"]})
    util.write_json(p("summary.json"), {
        "session_id": est["session_id"], "rc": rc, "model": est["model"], "engine": ENGINE,
        "state": "capped" if est["cap"] else ("failed" if rc else "ok"),
        "duration_min": round((time.time() - start) / 60, 2), "messages": est["turns"] or est["messages"],
        "tools": est["tools"], "alerts": est["alerts"], "cost_usd": est["cost_usd"], "cap": est["cap"],
        "start": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(start)), "end": util.now()})
    util.append(p("progress.log"), f"{util.now()}\tRESULT\t{util.short(final or '', 300)}")
    util.write_json(p("lease.json"), {"pid": os.getpid(), "host": os.uname().nodename,
                                      "renewed": time.time(), "final": True})
    with open(p("rc"), "w") as f:
        f.write(f"{rc}\n")
    return rc


def main(argv):
    if len(argv) < 3 or argv[0] != "run" or "--" not in argv:
        print("usage: python -m delega.engines.pi run <channel> <brief> -- <pi argv…>", file=sys.stderr)
        return 2
    i = argv.index("--")
    return run(argv[1], argv[2], argv[i + 1:])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

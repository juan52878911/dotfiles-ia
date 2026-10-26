#!/usr/bin/env python3
"""Minimal stand-in for `delega broker run <channel> <brief> -- <argv…>` (used through DELEGA_BROKER_CMD).

It writes the channel files the launcher reads (lease, state, progress, result, summary, rc) and,
inside the current directory (the worktree), commits a file, so launch/remote can be tested without
the real broker. Env: STUB_SLEEP seconds to stay "running", STUB_COMMIT_TEXT file content to commit
(none = no commit), STUB_RC exit code, STUB_ASK_FILE: wait until this file exists.
"""
import json
import os
import subprocess
import sys
import time


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def main(argv):
    if argv[0] != "run":
        return 2
    chan, brief = argv[1], argv[2]
    cmd = argv[argv.index("--") + 1:]
    start = time.time()
    w = lambda name, text: open(os.path.join(chan, name), "w").write(text)
    jw = lambda name, d: w(name, json.dumps(d))
    jw("lease.json", {"pid": os.getpid(), "host": "stub", "renewed": time.time()})
    jw("state.json", {"state": "running", "start": start, "last_event": start, "last_action": "starting",
                      "tools": 0, "turns": 0, "alerts": 0, "messages": 0, "session_id": None, "rc": None,
                      "pid": os.getpid()})
    open(os.path.join(chan, "progress.log"), "a").write(f"{now()}\tSTART\t{os.path.basename(brief)}\n")
    jw("stub.json", {"argv": cmd, "cwd": os.getcwd(), "config_dir": os.environ.get("CLAUDE_CONFIG_DIR"),
                     "env": {k: v for k, v in os.environ.items() if k.startswith("DELEGA_")},
                     "db_env": {k: v for k, v in os.environ.items()
                                if k.startswith(("PG", "MYSQL_", "REDIS_", "MONGODB_", "SQLITE_")) or k == "DATABASE_URL"},
                     "oauth_token_set": bool(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"))})
    end = time.time() + float(os.environ.get("STUB_SLEEP", "0"))
    while time.time() < end:
        time.sleep(0.1)
    text = os.environ.get("STUB_COMMIT_TEXT")
    if text is not None:
        open("result.txt", "w").write(text + "\n")
        git = ["git", "-c", "user.name=Stub", "-c", "user.email=stub@example.com"]
        subprocess.run(git + ["add", "result.txt"], check=True)
        subprocess.run(git + ["commit", "-qm", "stub work"], check=True)
    rc = int(os.environ.get("STUB_RC", "0"))
    sid = "sess-" + os.path.basename(chan)[:15]
    jw("result.json", {"type": "result", "result": "final report", "session_id": sid, "total_cost_usd": 0.5,
                       "is_error": rc != 0, "num_turns": 3})
    w("results.jsonl", json.dumps({"ts": now(), "result": "final report", "is_error": False, "cost_usd": 0.5}) + "\n")
    jw("summary.json", {"session_id": sid, "rc": rc, "model": "stub", "duration_min": 0, "messages": 3, "tools": 2,
                        "alerts": 0, "cost_usd": 0.5, "cap": None, "start": start, "end": time.time()})
    jw("state.json", {"state": "done", "start": start, "last_event": time.time(), "tools": 2, "turns": 3,
                      "alerts": 0, "messages": 3, "session_id": sid, "rc": rc, "pid": os.getpid(), "cost_usd": 0.5})
    open(os.path.join(chan, "progress.log"), "a").write(f"{now()}\tEND\trc={rc}\n")
    jw("lease.json", {"pid": os.getpid(), "host": "stub", "renewed": time.time(), "final": True})
    w("rc", str(rc))
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

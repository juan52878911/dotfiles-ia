#!/usr/bin/env python3
"""Channel between the orchestrator and one Claude Code worker (stdlib only: the same
code runs on the orchestrator machine and on nodes, also packed as delega.pyz).

Each job has a channel dir (file table in ARCHITECTURE.md):

  inbox.jsonl     orchestrator -> worker   one message per line, injected as "[orchestrator] ..."
  notices.jsonl   worker -> orchestrator   milestone | blocker | question
  progress.log    one line per event: "ts<TAB>TYPE<TAB>text" (read by status and watch)
  state.json      current snapshot; lease.json is renewed by the watchdog thread
  events.jsonl    raw stream-json (never leaves the node; redaction does not touch it)
  result.json / results.jsonl / summary.json / rc

  delega broker run <channel> <brief> -- <claude argv...>
  delega broker notify <channel> <milestone|blocker|question> <text>
  delega broker say <channel> <text>
  delega broker status <channel>
  delega broker watch <channel> [label] [--all]
  delega broker limits <channel...>
  delega broker metrics <channel>
  delega broker transcript <session.jsonl>
  delega broker report <dir>
"""
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time

from . import util
from .i18n import t
from .util import append, channel_file, now, read_json, short, write_json

REPEATS = 3          # same tool + input in a row -> loop alert
ERRORS = 3           # tool errors in a row -> alert
# Waiting on a process (tail --pid, sleep, until/wait) repeats the same command on purpose.
WAIT_RE = re.compile(r"tail --pid|\bsleep \d|\buntil\b|\bwait\b|TaskOutput|BashOutput")
KINDS = ("milestone", "blocker", "question")
LEGACY_KINDS = {"hito": "milestone", "bloqueo": "blocker", "pregunta": "question"}
LEGACY_STATE_KEYS = {
    "estado": "state", "inicio": "start", "ultimo_evento": "last_event", "ultima_accion": "last_action",
    "herramientas": "tools", "turnos": "turns", "alertas": "alerts", "mensajes": "messages",
    "modelo": "model", "coste_usd": "cost_usd", "tope": "cap", "tope_ts": "cap_ts", "topes": "caps",
}
LEGACY_STATES = {"corriendo": "running", "esperando_respuesta": "waiting_answer",
                 "esperando_tareas": "waiting_tasks", "terminado": "ended"}
LEGACY_TYPES = {
    "INICIO": "START", "HERRAMIENTA": "TOOL", "TEXTO": "TEXT", "MENSAJE": "MESSAGE",
    "RESULTADO": "RESULT", "FIN": "END",
    "AVISO:hito": "NOTICE:milestone", "AVISO:bloqueo": "NOTICE:blocker", "AVISO:pregunta": "NOTICE:question",
    "ALERTA:atasco": "ALERT:stall", "ALERTA:bucle": "ALERT:loop", "ALERTA:errores": "ALERT:errors",
    "ALERTA:sin_respuesta": "ALERT:no_answer", "ALERTA:no_entregado": "ALERT:undelivered",
    "ALERTA:tope": "ALERT:cap", "ALERTA:tope_cortado": "ALERT:cap_cut", "ALERTA:muerto": "ALERT:dead",
}


def _env_num(name, default, cast=float):
    try:
        return cast(os.environ.get(name, default))
    except ValueError:
        return cast(default)


def norm_type(tipo):
    return LEGACY_TYPES.get(tipo, tipo)


def norm_state(est):
    """state.json with English keys/values, whatever wrote it."""
    out = {LEGACY_STATE_KEYS.get(k, k): v for k, v in (est or {}).items()}
    if "state" in out:
        out["state"] = LEGACY_STATES.get(out["state"], out["state"])
    if isinstance(out.get("caps"), dict):
        out["caps"] = {{"turnos": "turns"}.get(k, k): v for k, v in out["caps"].items()}
    return out


# ─── redaction ───────────────────────────────────────────────────────────────
def forbidden_terms(channel):
    try:
        with open(os.path.join(channel, "forbidden.txt"), encoding="utf-8") as f:
            return [l.strip() for l in f if l.strip() and not l.startswith("#")]
    except OSError:
        return []


def redact(text, terms):
    """Replace every forbidden term (case-insensitive) with [redacted]."""
    if not terms or not text:
        return text
    rx = re.compile("|".join(re.escape(x) for x in sorted(terms, key=len, reverse=True)), re.I)
    return rx.sub("[redacted]", text)


def describe_tool(c):
    name, inp = c.get("name", "?"), c.get("input") or {}
    key = inp.get("command") or inp.get("file_path") or inp.get("pattern") \
        or inp.get("description") or inp.get("prompt") or json.dumps(inp, ensure_ascii=False)
    return f"{name}: {short(key, 90)}"


class Channel:
    def __init__(self, path):
        self.dir = os.path.abspath(path)
        os.makedirs(self.dir, exist_ok=True)

    def p(self, name):
        return os.path.join(self.dir, name)

    def read(self, name):
        """Path of a channel file, legacy name if that is the one that exists."""
        return channel_file(self.dir, name)

    def red(self, text):
        return redact(text, forbidden_terms(self.dir))

    def event(self, tipo, text):
        append(self.p("progress.log"), f"{now()}\t{tipo}\t{short(self.red(str(text)), 400)}")


def protocol(notify, caps, grace_s, loc=None):
    return t("protocol.text", loc=loc, notify=notify, caps=caps, grace=max(1, int(grace_s // 60)))


NOTIFY_SH = '#!/bin/sh\nPYTHONPATH="{root}${{PYTHONPATH:+:$PYTHONPATH}}" exec "{py}" -m delega broker notify "{ch}" "$@"\n'


def cmd_run(chdir, brief, command):
    ch = Channel(chdir)
    max_min = _env_num("DELEGA_JOB_MAX_MIN", "120")
    max_turns = _env_num("DELEGA_JOB_MAX_TURNS", "800", int)   # assistant messages, subagents included
    max_usd = _env_num("DELEGA_JOB_MAX_USD", "15")
    grace_s = _env_num("DELEGA_JOB_GRACE_S", "300")
    stall_s = _env_num("DELEGA_STALL_S", "600")
    answer_wait_s = _env_num("DELEGA_ANSWER_WAIT_S", "900")
    renew_s = _env_num("DELEGA_LEASE_RENEW_S", "30")
    for n in ("inbox.jsonl", "notices.jsonl"):
        if not os.path.exists(ch.read(n)):
            open(ch.p(n), "a").close()
    notify = ch.p("notify")
    with open(notify, "w") as f:
        f.write(NOTIFY_SH.format(root=os.path.dirname(util.PKG_DIR), py=sys.executable, ch=ch.dir))
    os.chmod(notify, 0o755)

    est = {"state": "running", "start": time.time(), "last_event": time.time(),
           "last_action": "starting", "tools": 0, "turns": 0, "alerts": 0, "messages": 0,
           "last_message_ts": "", "session_id": None, "rc": None, "pid": os.getpid(),
           "model": None, "cost_usd": None, "cap": None, "cap_ts": None,
           "caps": {"min": max_min, "turns": max_turns, "usd": max_usd}}
    state_lock = threading.Lock()
    host = socket.gethostname()
    lease = {"pid": os.getpid(), "host": host, "renewed": time.time()}

    def save():
        with state_lock:
            write_json(ch.p("state.json"), est)

    def write_lease(final=False):
        lease["renewed"] = time.time()
        if final:
            lease["final"] = True
        with state_lock:
            write_json(ch.p("lease.json"), lease)

    save()
    write_lease()
    # An env var of a parent Claude session could leak its account: drop them all but the
    # config dir the launcher chose on purpose and those named in DELEGA_KEEP_ENV (comma list;
    # legacy ORQ_CONSERVAR). The kindling runner passes the worker's token that way, by process.
    keep = {"CLAUDE_CONFIG_DIR"} | {x.strip() for x in (os.environ.get("DELEGA_KEEP_ENV")
                                    or os.environ.get("ORQ_CONSERVAR") or "").split(",") if x.strip()}
    env = {k: v for k, v in os.environ.items()
           if k in keep or not k.startswith(("CLAUDE", "ANTHROPIC"))}
    env["DELEGA_CHANNEL"] = ch.dir
    caps_txt = t("broker.caps_text", min=f"{max_min:g}", turns=max_turns, usd=f"{max_usd:g}")
    command = command + ["--input-format", "stream-json", "--output-format", "stream-json",
                         "--verbose", "--replay-user-messages",
                         "--append-system-prompt", protocol(notify, caps_txt, grace_s)]
    proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=open(ch.p("stderr.log"), "a"), text=True, env=env)
    open_ = [True]
    send_lock = threading.Lock()

    def send(text):
        with send_lock:
            if not open_[0]:
                return False
            try:
                proc.stdin.write(json.dumps({"type": "user", "message": {"role": "user", "content": text}}) + "\n")
                proc.stdin.flush()
            except (OSError, ValueError):
                return False
            return True

    def close():
        with send_lock:
            if open_[0]:
                open_[0] = False
                try:
                    proc.stdin.close()
                except OSError:
                    pass

    with open(brief, encoding="utf-8") as f:
        send(f.read())
    ch.event("START", " ".join(os.path.basename(c) for c in command[:2]))

    delivered = [0]            # inbox lines already delivered
    answered_upto = [0]        # notices that existed when the last message was delivered
    inbox_lock = threading.Lock()   # watchdog and main loop both read the inbox

    def notices():
        try:
            with open(ch.read("notices.jsonl"), encoding="utf-8") as f:
                out = []
                for l in f.read().splitlines():
                    if l.strip():
                        try:
                            out.append(json.loads(l))
                        except ValueError:
                            pass
                return out
        except OSError:
            return []

    def deliver_inbox():
        """Deliver what is new in the inbox; a line only counts as read once the worker got it."""
        n = 0
        with inbox_lock:
            try:
                with open(ch.read("inbox.jsonl"), encoding="utf-8") as f:
                    lines = f.read().splitlines()
            except OSError:
                return 0
            for line in lines[delivered[0]:]:
                if line.strip():
                    try:
                        m = json.loads(line)
                        text = m.get("text") or m.get("texto") or ""
                    except ValueError:
                        text = line
                    if not send(f"[orchestrator] {text}"):
                        ch.event("ALERT:undelivered", t("broker.undelivered", text=text))
                        break
                    ch.event("MESSAGE", text)
                    est["messages"] += 1
                    est["last_message_ts"] = now()
                    answered_upto[0] = len(notices())
                    n += 1
                delivered[0] += 1
        return n

    def hit_cap(reason):
        """First cap reached: ask to wrap up; the watchdog cuts if it does not within grace."""
        if est["cap"]:
            return
        est["cap"], est["cap_ts"] = reason, time.time()
        est["alerts"] += 1
        ch.event("ALERT:cap", t("broker.cap_hit", reason=reason, min=max(1, int(grace_s // 60))))
        send("[orchestrator] " + t("broker.wrap_up", reason=reason))
        save()

    tasks = set()    # the worker's background tasks (background Bash, subagents)
    stop_dog = threading.Event()

    def watchdog():
        warned_at, last_lease = 0.0, 0.0
        while proc.poll() is None and not stop_dog.is_set():
            time.sleep(0.2 if renew_s < 5 else 1)
            if time.time() - last_lease >= renew_s:
                last_lease = time.time()
                write_lease()
            minutes = (time.time() - est["start"]) / 60
            if minutes >= max_min:
                hit_cap(f"{minutes:.1f} min >= {max_min:g}")
            elif est["turns"] >= max_turns:
                hit_cap(f"{est['turns']} messages >= {max_turns}")
            if est["cap_ts"] and time.time() - est["cap_ts"] > grace_s:
                ch.event("ALERT:cap_cut", t("broker.cap_cut", reason=est["cap"]))
                est["alerts"] += 1
                save()
                proc.terminate()
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    proc.kill()
                return
            if est["state"] in ("running", "waiting_tasks"):
                deliver_inbox()
                quiet = time.time() - est["last_event"]
                limit = stall_s * (3 if tasks else 1)
                if quiet >= limit and est["last_event"] != warned_at:
                    warned_at = est["last_event"]
                    est["alerts"] += 1
                    how_long = f"{int(quiet // 60)} min" if quiet >= 60 else f"{int(quiet)} s"
                    extra = t("broker.stall_tasks", n=len(tasks)) if tasks else ""
                    ch.event("ALERT:stall", t("broker.stall", how_long=how_long, extra=extra, action=est["last_action"]))
                    save()

    dog = threading.Thread(target=watchdog, daemon=True)
    dog.start()

    prev, errors = [], 0
    with open(ch.p("events.jsonl"), "a", encoding="utf-8") as raw:
        for line in proc.stdout:
            raw.write(line)
            raw.flush()
            try:
                e = json.loads(line)
            except ValueError:
                continue
            est["last_event"] = time.time()
            kind, sub = e.get("type"), e.get("subtype")
            if kind == "system" and sub == "init":
                est["session_id"] = e.get("session_id")
                est["model"] = e.get("model")
            elif kind == "system" and sub == "task_started":
                tasks.add(e.get("task_id"))
            elif kind == "system" and sub in ("task_updated", "task_notification"):
                st = (e.get("patch") or {}).get("status") or e.get("status")
                if st in ("completed", "failed", "killed", "cancelled", "stopped"):
                    tasks.discard(e.get("task_id"))
            elif kind == "assistant":
                est["turns"] += 1
                if est["state"] == "waiting_tasks":
                    est["state"] = "running"
                for c in e.get("message", {}).get("content", []):
                    if c.get("type") == "tool_use":
                        action = ch.red(describe_tool(c))
                        est["tools"] += 1
                        est["last_action"] = action
                        ch.event("TOOL", action)
                        key = c.get("name", "") + json.dumps(c.get("input"), sort_keys=True)
                        if WAIT_RE.search(json.dumps(c.get("input"))):
                            continue
                        prev = (prev + [key])[-REPEATS:]
                        if len(prev) == REPEATS and len(set(prev)) == 1:
                            est["alerts"] += 1
                            ch.event("ALERT:loop", t("broker.loop", n=REPEATS, action=action))
                            prev = []
                    elif c.get("type") == "text" and c.get("text", "").strip():
                        ch.event("TEXT", c["text"])
            elif kind == "user":
                content = e.get("message", {}).get("content")
                if isinstance(content, list):
                    for c in content:
                        if c.get("type") == "tool_result":
                            errors = errors + 1 if c.get("is_error") else 0
                            if errors == ERRORS:
                                est["alerts"] += 1
                                ch.event("ALERT:errors", t("broker.errors", n=ERRORS, action=est["last_action"]))
            elif kind == "result":
                terms = forbidden_terms(ch.dir)
                shown = dict(e)
                if isinstance(shown.get("result"), str):
                    shown["result"] = redact(shown["result"], terms)
                write_json(ch.p("result.json"), shown)
                append(ch.p("results.jsonl"), json.dumps(
                    {"ts": now(), "result": shown.get("result", ""), "is_error": e.get("is_error"),
                     "cost_usd": e.get("total_cost_usd")}, ensure_ascii=False))
                est["session_id"] = e.get("session_id") or est["session_id"]
                if e.get("total_cost_usd") is not None:
                    est["cost_usd"] = e["total_cost_usd"]
                ch.event("RESULT", shown.get("result", "") if not e.get("is_error") else f"error: {shown.get('result')}")
                if (est["cost_usd"] or 0) >= max_usd and not est["cap"]:
                    # A result was just delivered: ask nothing more, close here.
                    est["cap"], est["cap_ts"] = f"{est['cost_usd']:.2f} USD >= {max_usd:g}", None
                    est["alerts"] += 1
                    ch.event("ALERT:cap", t("broker.cap_cost", reason=est["cap"]))
                    close()
                    save()
                    continue
                if est["cap"]:
                    close()    # it was asked to wrap up and has given its report
                    save()
                    continue
                if tasks:
                    # Claude Code resumes by itself when the task ends: closing now would kill it.
                    est["state"] = "waiting_tasks"
                    save()
                    continue
                if deliver_inbox():
                    save()
                    continue
                ns = notices()
                if ns and ns[-1].get("kind", LEGACY_KINDS.get(ns[-1].get("tipo"), "")) == "question" \
                        and len(ns) > answered_upto[0]:
                    est["state"] = "waiting_answer"
                    save()
                    deadline, answered = time.time() + answer_wait_s, False
                    while time.time() < deadline and est["cap_ts"] is None:
                        if deliver_inbox():
                            answered = True
                            break
                        time.sleep(0.2)
                    if answered:
                        est["state"] = "running"
                        est["last_event"] = time.time()
                        save()
                        continue
                    ch.event("ALERT:no_answer", t("broker.no_answer", min=f"{answer_wait_s / 60:g}",
                                                  text=ns[-1].get("text") or ns[-1].get("texto")))
                    est["alerts"] += 1
                close()
            save()

    rc = proc.wait()
    close()
    stop_dog.set()
    dog.join(timeout=5)
    est["state"], est["rc"] = "ended", rc
    save()
    end = time.time()
    # Real totals: result.json only carries the last result's numbers.
    summary = {
        "session_id": est["session_id"], "rc": rc, "model": est["model"],
        "duration_min": round((end - est["start"]) / 60, 1), "messages": est["turns"],
        "tools": est["tools"], "alerts": est["alerts"], "cost_usd": est["cost_usd"],
        "cap": ch.red(est["cap"]) if est["cap"] else None,
        "start": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(est["start"])), "end": now()}
    write_json(ch.p("summary.json"), summary)
    write_lease(final=True)
    ch.event("END", f"rc={rc}" + (f" · cap: {est['cap']}" if est["cap"] else ""))
    with open(ch.p("rc"), "w") as f:
        f.write(f"{rc}\n")
    return rc


def cmd_notify(chdir, kind, text):
    kind = LEGACY_KINDS.get(kind, kind)
    if kind not in KINDS:
        print(t("broker.notify_kinds"), file=sys.stderr)
        return 2
    ch = Channel(chdir)
    text = ch.red(text)
    append(ch.p("notices.jsonl"), json.dumps({"ts": now(), "kind": kind, "text": text}, ensure_ascii=False))
    ch.event(f"NOTICE:{kind}", text)
    print(t("broker.notice_sent"))
    return 0


def cmd_say(chdir, text):
    ch = Channel(chdir)
    est = norm_state(read_json(ch.read("state.json"), {}))
    if est.get("state") == "ended" or os.path.exists(ch.p("rc")):
        print(t("broker.job_ended", sid=est.get("session_id")), file=sys.stderr)
        return 6
    append(ch.read("inbox.jsonl"), json.dumps({"ts": now(), "text": text}, ensure_ascii=False))
    print(t("broker.message_queued"))
    return 0


def alive(chdir, est):
    """Is the broker of this job still running? (pid on the same machine as the job)"""
    lease = read_json(os.path.join(chdir, "lease.json"), {}) or {}
    if os.path.exists(os.path.join(chdir, "rc")) or est.get("state") == "ended" or lease.get("final"):
        return False
    try:
        os.kill(int(est.get("pid") or 0), 0)
        return bool(est.get("pid"))
    except (OSError, ValueError):
        return False


def progress_lines(ch):
    try:
        with open(ch.read("progress.log"), encoding="utf-8") as f:
            rows = [l.split("\t", 2) for l in f.read().splitlines()]
    except OSError:
        return []
    return [(ts, norm_type(tp), tx) for ts, tp, tx in (r for r in rows if len(r) == 3)]


def cmd_status(chdir):
    ch = Channel(chdir)
    est = norm_state(read_json(ch.read("state.json")))
    if not est:
        print(t("broker.no_state"))
        return 0
    stall_s = _env_num("DELEGA_STALL_S", "600")
    if est["state"] != "ended" and not alive(ch.dir, est):
        est["state"] = t("broker.dead", sid=est.get("session_id"))
    busy = est["state"] in ("running", "waiting_answer", "waiting_tasks")
    end = time.time() if busy else est.get("last_event", time.time())
    dur = int((end - est.get("start", end)) // 60)
    quiet = int(time.time() - est.get("last_event", time.time()))
    mark = ""
    if est["state"] == "running" and quiet >= stall_s:
        mark = "  " + t("broker.mark_stall")
    elif est["state"] == "waiting_answer":
        mark = "  " + t("broker.mark_answer")
    elif est["state"] == "waiting_tasks" and quiet >= stall_s:
        mark = "  " + t("broker.mark_task")
    sal = read_json(ch.read("result.json"), {}) or {}
    cost = est.get("cost_usd", sal.get("total_cost_usd"))
    if est.get("cap"):
        mark += "  " + t("broker.mark_cap", cap=est["cap"])
    try:
        with open(ch.read("notices.jsonl"), encoding="utf-8") as f:
            n_notices = sum(1 for l in f if l.strip())
    except OSError:
        n_notices = 0
    rc = "" if est.get("rc") is None else f" rc={est['rc']}"
    print(t("broker.status_line", state=est["state"], rc=rc, min=dur, quiet=f"{quiet // 60}m{quiet % 60:02d}s",
            tools=est.get("tools", 0), notices=n_notices, alerts=est.get("alerts", 0),
            cost="" if cost is None else f" · {cost:.3f} $", mark=mark))
    print(t("broker.last_action", action=est.get("last_action")))
    for ts, tp, tx in [r for r in progress_lines(ch) if r[1].startswith(("NOTICE", "ALERT", "MESSAGE"))][-3:]:
        print(f"  {ts[11:]} {tp} {short(tx, 90)}")
    return 0


def cmd_watch(chdir, label="", everything=False):
    """One line per notice, alert, result and the end; exits when the job ends. Without --all it
    starts at the last minute so re-arming a monitor does not repeat what was already seen."""
    ch = Channel(chdir)
    since = "" if everything else time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - 60))
    pre = f"[{label}] " if label else ""
    path, pos, seen = ch.read("progress.log"), 0, set()
    while True:
        try:
            with open(path, encoding="utf-8") as f:
                f.seek(pos)
                block = f.read()
                pos = f.tell()
        except OSError:
            block = ""
        for l in block.splitlines():
            parts = l.split("\t", 2)
            if len(parts) != 3:
                continue
            tp = norm_type(parts[1])
            if parts[0] >= since and tp.startswith(("NOTICE", "ALERT", "RESULT", "END")):
                # Every line wakes the orchestrator: the same alert twice adds nothing.
                key = (tp, short(parts[2], 120))
                if key in seen and tp != "END":
                    continue
                seen.add(key)
                print(f"{pre}{tp} {short(parts[2], 200)}", flush=True)
                if tp == "END":
                    return 0
        est = norm_state(read_json(ch.read("state.json"), {}))
        if est and est.get("state") != "ended" and not alive(ch.dir, est):
            print(f"{pre}ALERT:dead {t('broker.watch_dead', sid=est.get('session_id'))}", flush=True)
            return 1
        time.sleep(2)


# ─── metrics (also imported by metrics.py) ───────────────────────────────────
def _usage_from_events(path):
    """Tokens per model. With a result, its modelUsage (cumulative and exact); otherwise the
    sum of the assistant messages' usage (deduplicated by id): partial."""
    last_result, by_id = None, {}
    try:
        with open(path, encoding="utf-8") as f:
            for l in f:
                try:
                    e = json.loads(l)
                except ValueError:
                    continue
                if e.get("type") == "result":
                    last_result = e
                elif e.get("type") == "assistant":
                    m = e.get("message", {})
                    if m.get("id") and m.get("usage"):
                        by_id[m["id"]] = (m.get("model", "?"), m["usage"])
    except OSError:
        pass
    models = {}
    if last_result and last_result.get("modelUsage"):
        for mod, u in last_result["modelUsage"].items():
            models[mod] = {"input": u.get("inputTokens", 0), "output": u.get("outputTokens", 0),
                           "cache_read": u.get("cacheReadInputTokens", 0),
                           "cache_write": u.get("cacheCreationInputTokens", 0), "cost_usd": u.get("costUSD")}
        source = "result"
    else:
        for mod, u in by_id.values():
            d = models.setdefault(mod, {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "cost_usd": None})
            d["input"] += u.get("input_tokens", 0)
            d["output"] += u.get("output_tokens", 0)
            d["cache_read"] += u.get("cache_read_input_tokens", 0)
            d["cache_write"] += u.get("cache_creation_input_tokens", 0)
        source = "partial"
    r = last_result or {}
    return models, source, {"cost_usd": r.get("total_cost_usd"), "api_s": (r.get("duration_api_ms") or 0) / 1000,
                            "turns": r.get("num_turns"), "subagents": r.get("subagent_stats")}


def _category(text):
    name, _, rest = text.partition(": ")
    if name != "Bash":
        return name
    if "/notify" in rest or "/avisar" in rest or "broker notify" in rest:
        return "notify (channel)"
    for w in rest.replace("&&", " ").replace(";", " ").split():
        if "=" in w.split("/")[0] or w in ("cd", "sudo", "env", "time", "timeout", "nohup", "export"):
            continue
        return "Bash:" + os.path.basename(w)[:20]
    return "Bash"


def _ts(x):
    return time.mktime(time.strptime(x, "%Y-%m-%dT%H:%M:%S"))


def metrics_of(channel):
    """Numbers of one job: time per activity, alerts, notices, tokens per model, cost. No text."""
    ch = Channel(channel)
    est = norm_state(read_json(ch.read("state.json"), {}))
    spent, current, prev_ts, alerts, notices = {}, "startup", None, {}, {}
    for ts, tp, text in progress_lines(ch):
        if tp.startswith("ALERT"):
            alerts[tp[6:]] = alerts.get(tp[6:], 0) + 1
            continue          # an alert does not change what the worker was doing
        if tp.startswith("NOTICE"):
            notices[tp[7:]] = notices.get(tp[7:], 0) + 1
        if prev_ts:
            spent[current] = spent.get(current, 0) + _ts(ts) - prev_ts
        prev_ts = _ts(ts)
        current = (_category(text) if tp == "TOOL" else "waiting (answer/tasks)" if tp == "RESULT" else "model")
    if prev_ts and est.get("state") != "ended":
        spent[current] = spent.get(current, 0) + time.time() - prev_ts
    models, source, extra = _usage_from_events(ch.read("events.jsonl"))
    end = est.get("last_event") if est.get("state") == "ended" else time.time()
    start = est.get("start")
    return {"job": os.path.basename(ch.dir), "state": est.get("state"), "rc": est.get("rc"),
            "start": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(start)) if start else None,
            "duration_s": round(end - start) if start and end else None,
            "tools": est.get("tools"), "messages": est.get("messages"),
            "time_by": {k: round(v) for k, v in sorted(spent.items(), key=lambda kv: -kv[1])},
            "notices": notices, "alerts": alerts, "tokens": models, "tokens_source": source, **extra}


def cmd_metrics(chdir):
    print(json.dumps(metrics_of(chdir), ensure_ascii=False))
    return 0


def _k(n):
    return f"{n / 1e6:.2f}M" if n >= 1e6 else f"{n / 1e3:.0f}k" if n >= 1e3 else str(n)


def _load_files(folder, prefixes, suffix):
    return [os.path.join(folder, n) for n in sorted(os.listdir(folder))
            if n.startswith(prefixes) and n.endswith(suffix)]


def report(folder):
    """Aggregate metrics-*.json (legacy metricas-*), orchestrator-*.json and resources-*.csv
    (legacy recursos-*) of a folder into a readable report on stdout."""
    jobs = []
    for p in _load_files(folder, ("metrics-", "metricas-"), ".json"):
        m = read_json(p)
        if m:
            jobs.append({LEGACY_REPORT_KEYS.get(k, k): v for k, v in m.items()})
    print(t("broker.report_title", ts=now()) + "\n")
    print("## " + t("broker.report_workers") + "\n")
    print("| job | state | dur. | API | tools | input tokens (cache read / write) | output | cost $ | alerts |")
    print("|---|---|---|---|---|---|---|---|---|")
    tot = {"dur": 0, "api": 0, "inp": 0, "cr": 0, "cw": 0, "out": 0, "cost": 0.0}
    by_time = {}
    for m in jobs:
        tk = {mod: {_TOK.get(k, k): v for k, v in u.items()} for mod, u in (m.get("tokens") or {}).items()}
        inp = sum(v["input"] for v in tk.values()); cr = sum(v["cache_read"] for v in tk.values())
        cw = sum(v["cache_write"] for v in tk.values()); out = sum(v["output"] for v in tk.values())
        cost = m.get("cost_usd") or sum((v.get("cost_usd") or 0) for v in tk.values())
        names = ",".join(k.replace("claude-", "") for k in tk)
        for k, v in (m.get("time_by") or {}).items():
            by_time[k] = by_time.get(k, 0) + v
        tot["dur"] += m.get("duration_s") or 0; tot["api"] += m.get("api_s") or 0
        tot["inp"] += inp; tot["cr"] += cr; tot["cw"] += cw; tot["out"] += out; tot["cost"] += cost or 0
        al = ", ".join(f"{k}×{v}" for k, v in (m.get("alerts") or {}).items()) or "—"
        part = " (partial)" if m.get("tokens_source") in ("partial", "parcial") else ""
        print(f"| {str(m.get('job'))[:22]} {names} | {m.get('state')} | {(m.get('duration_s') or 0) // 60} min | "
              f"{int(m.get('api_s') or 0) // 60} min | {m.get('tools')} | {_k(inp + cr + cw)} ({_k(cr)} / {_k(cw)}){part} | "
              f"{_k(out)} | {cost:.2f} | {al} |")
    print(f"| **total** | | {tot['dur'] // 60} min | {int(tot['api']) // 60} min | | {_k(tot['inp'] + tot['cr'] + tot['cw'])} | "
          f"{_k(tot['out'])} | {tot['cost']:.2f} | |")
    if by_time:
        total = sum(by_time.values()) or 1
        print("\n## " + t("broker.report_time") + "\n")
        for k, v in sorted(by_time.items(), key=lambda kv: -kv[1])[:10]:
            print(f"- {k}: {int(v) // 60} min ({100 * v / total:.0f} %)")
    for p in _load_files(folder, ("orchestrator-", "orquestador-"), ".json"):
        o = read_json(p) or {}
        sid = o.get("session") or o.get("sesion") or ""
        print(f"\n## {t('broker.report_orchestrator')} · {sid[:8]} · {(o.get('from') or o.get('desde') or '')[11:16]}–"
              f"{(o.get('to') or o.get('hasta') or '')[11:16]} UTC\n")
        for mod, v in (o.get("tokens") or {}).items():
            v = {_TOK.get(k, k): x for k, x in v.items()}
            print(f"- {mod}: {v.get('messages', v.get('mensajes', 0))} messages · input "
                  f"{_k(v['input'] + v['cache_read'] + v['cache_write'])} (cache read {_k(v['cache_read'])}, "
                  f"write {_k(v['cache_write'])}) · output {_k(v['output'])}")
    for p in _load_files(folder, ("resources-", "recursos-"), ".csv"):
        with open(p) as f:
            rows = [l.split(",") for l in f.read().splitlines()[1:] if l.count(",") >= 13]
        if not rows:
            continue
        mem = [int(r[2]) for r in rows]; load = [float(r[4]) for r in rows]; wa = [int(r[8]) for r in rows]
        idle = [int(r[7]) for r in rows]; oom = [int(r[13]) for r in rows]
        low = sum(1 for x in mem if x < 500) / len(mem)
        name = os.path.basename(p).split("-", 1)[1][:-4]
        print(f"\n## {t('broker.report_resources', name=name, n=len(rows), a=rows[0][0][11:], b=rows[-1][0][11:])}\n")
        print(f"- RAM available: min {min(mem)} MB, median {sorted(mem)[len(mem) // 2]} MB, of {rows[0][1]} MB; "
              f"{100 * low:.0f} % of the time under 500 MB; max swap used {max(int(r[3]) for r in rows)} MB")
        print(f"- Load: mean {sum(load) / len(load):.1f}, max {max(load):.1f}; mean idle CPU {sum(idle) / len(idle):.0f} %; "
              f"mean disk wait {sum(wa) / len(wa):.0f} %, max {max(wa)} %")
        print(f"- OOM kills during the measurement: {oom[-1] - oom[0]}")
        print(f"- Free disk: {rows[-1][9]} GB; containers: {rows[-1][10]}")
    return 0


LEGACY_REPORT_KEYS = {"trabajo": "job", "estado": "state", "duracion_s": "duration_s", "herramientas": "tools",
                      "mensajes": "messages", "tiempo_por": "time_by", "avisos": "notices", "alertas": "alerts",
                      "fuente_tokens": "tokens_source", "coste_usd": "cost_usd", "turnos": "turns"}
_TOK = {"entrada": "input", "salida": "output", "cache_lectura": "cache_read", "cache_escritura": "cache_write",
        "coste_usd": "cost_usd", "mensajes": "messages"}


def transcript(path):
    """Tokens of a Claude Code session from its transcript .jsonl (e.g. the orchestrator's)."""
    by_id, first, last = {}, None, None
    with open(path, encoding="utf-8") as f:
        for l in f:
            try:
                e = json.loads(l)
            except ValueError:
                continue
            ts = e.get("timestamp")
            if ts:
                first = first or ts
                last = ts
            m = e.get("message") or {}
            if e.get("type") == "assistant" and m.get("id") and m.get("usage"):
                by_id[m["id"]] = (m.get("model", "?"), m["usage"], bool(e.get("isSidechain")))
    models = {}
    for mod, u, side in by_id.values():
        d = models.setdefault(mod + (" (subagents)" if side else ""),
                              {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "cost_usd": None, "messages": 0})
        d["input"] += u.get("input_tokens", 0); d["output"] += u.get("output_tokens", 0)
        d["cache_read"] += u.get("cache_read_input_tokens", 0); d["cache_write"] += u.get("cache_creation_input_tokens", 0)
        d["messages"] += 1
    return {"session": os.path.basename(path), "from": first, "to": last, "tokens": models}


def cmd_transcript(path):
    print(json.dumps(transcript(path), ensure_ascii=False))
    return 0


def limit_of(channels):
    """Latest rate_limit_event (the account's limit) among the given channels, with the file's
    mtime (the event carries no date). {"mtime", "info"} or None."""
    best = None
    for d in channels:
        path = channel_file(d, "events.jsonl")
        try:
            mt = os.path.getmtime(path)
            if best and mt <= best[0]:
                continue
            with open(path, "rb") as f:
                f.seek(0, 2); size = f.tell(); f.seek(max(0, size - 400000))
                tail = f.read().decode("utf-8", "ignore").splitlines()
        except OSError:
            continue
        for l in reversed(tail):
            if '"rate_limit_event"' in l:
                try:
                    info = json.loads(l)["rate_limit_info"]
                except (ValueError, KeyError):
                    break
                best = (mt, info)
                break
    return {"mtime": best[0], "info": best[1]} if best else None


def cmd_limits(channels):
    r = limit_of(channels)
    if r:
        print(json.dumps(r))
    return 0


def main(argv):
    a = list(argv)
    if len(a) >= 4 and a[0] == "run" and "--" in a:
        i = a.index("--")
        return cmd_run(a[1], a[2], a[i + 1:])
    if len(a) >= 4 and a[0] == "notify":
        return cmd_notify(a[1], a[2], " ".join(a[3:]))
    if len(a) >= 3 and a[0] == "say":
        return cmd_say(a[1], " ".join(a[2:]))
    if len(a) == 2 and a[0] == "status":
        return cmd_status(a[1])
    if len(a) == 2 and a[0] == "metrics":
        return cmd_metrics(a[1])
    if len(a) >= 1 and a[0] == "limits":
        return cmd_limits(a[1:])
    if len(a) == 2 and a[0] == "transcript":
        return cmd_transcript(a[1])
    if len(a) == 2 and a[0] == "report":
        return report(a[1])
    if len(a) >= 2 and a[0] == "watch":
        rest = [x for x in a[1:] if x not in ("--all", "--todo")]
        return cmd_watch(rest[0], rest[1] if len(rest) > 1 else "", len(rest) < len(a) - 1)
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

"""metrics: tokens, time and node resources of finished jobs, plus a readable report.

The per-job maths (`job_metrics`) and the transcript reader are a local copy of what the broker
computes for a channel: broker.py has no importable equivalent in English yet, and a node-less
(local) report must work without it. Remote jobs are measured on the node via `broker metrics`
and normalised here."""
import json
import os
import shlex
import sys
import time

from . import config, i18n, util

STATE_DONE = {"ended", "done", "finished", "terminado"}
CSV_COLS = 15
TYPE_MAP = {"HERRAMIENTA": "TOOL", "RESULTADO": "RESULT", "FIN": "END", "TEXTO": "TEXT", "MENSAJE": "MESSAGE",
            "INICIO": "START"}
KEY_MAP = {"estado": "state", "inicio": "start", "duracion_s": "duration_s", "herramientas": "tools",
           "mensajes": "messages", "tiempo_por": "time_by", "avisos": "notices", "alertas": "alerts",
           "fuente_tokens": "tokens_source", "coste_usd": "cost_usd", "turnos": "turns", "subagentes": "subagents",
           "trabajo": "job"}
SUBTYPE_MAP = {"atasco": "stall", "bucle": "loop", "errores": "errors", "muerto": "dead", "no_entregado": "undelivered",
               "sin_respuesta": "no_answer", "tope": "cap", "tope_cortado": "cap_cut",
               "hito": "milestone", "bloqueo": "blocker", "pregunta": "question"}
TOKEN_MAP = {"entrada": "input", "salida": "output", "cache_lectura": "cache_read", "cache_escritura": "cache_write"}


# ── per-job metrics ─────────────────────────────────────────────────────────
def _norm_type(t):
    head, _, tail = t.partition(":")
    head = {"AVISO": "NOTICE", "ALERTA": "ALERT"}.get(head, TYPE_MAP.get(head, head))
    return head, SUBTYPE_MAP.get(tail, tail)


def _ts(x):
    return time.mktime(time.strptime(x, "%Y-%m-%dT%H:%M:%S"))


def _category(text):
    name, _, rest = text.partition(": ")
    if name != "Bash":
        return name
    if "/jobs/" in rest or "delega notify" in rest or "/notify" in rest:
        return "notify (channel)"
    for word in rest.replace("&&", " ").replace(";", " ").split():
        if "=" in word.split("/")[0] or word in ("cd", "sudo", "env", "time", "timeout", "nohup", "export"):
            continue
        return "Bash:" + os.path.basename(word)[:20]
    return "Bash"


def usage_from_events(path):
    """Tokens per model: the last `result` event's modelUsage (exact, cumulative) if there is one;
    otherwise the sum of assistant-message usage, once per message id (partial)."""
    last_result, by_id = None, {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    e = json.loads(line)
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


def job_metrics(channel):
    """Metrics of one channel dir (state.json, progress.log, events.jsonl; legacy names accepted)."""
    st = util.read_json(util.channel_file(channel, "state.json"), {}) or {}
    st = {KEY_MAP.get(k, k): v for k, v in st.items()}
    rows = []
    try:
        for line in open(util.channel_file(channel, "progress.log"), encoding="utf-8").read().splitlines():
            parts = line.split("\t", 2)
            if len(parts) == 3:
                rows.append(parts)
    except OSError:
        pass
    spent, current, prev, alerts, notices = {}, "startup", None, {}, {}
    for ts, typ, text in rows:
        head, tail = _norm_type(typ)
        if head == "ALERT":
            alerts[tail] = alerts.get(tail, 0) + 1
            continue                      # an alert does not change what the worker was doing
        if head == "NOTICE":
            notices[tail] = notices.get(tail, 0) + 1
        try:
            now = _ts(ts)
        except ValueError:
            continue
        if prev:
            spent[current] = spent.get(current, 0) + now - prev
        prev = now
        current = _category(text) if head == "TOOL" else "waiting (answer/tasks)" if head == "RESULT" else "model"
    finished = st.get("state") in STATE_DONE or os.path.exists(util.channel_file(channel, "summary.json"))
    last = st.get("last_event") or st.get("ultimo_evento")
    if prev and not finished:
        spent[current] = spent.get(current, 0) + time.time() - prev
    models, source, extra = usage_from_events(util.channel_file(channel, "events.jsonl"))
    end = last if finished and last else time.time()
    start = st.get("start")
    return {"job": os.path.basename(channel.rstrip("/")), "state": st.get("state"), "rc": st.get("rc"),
            "start": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(start)) if start else None,
            "duration_s": round(end - start) if start else None, "tools": st.get("tools"),
            "messages": st.get("messages"), "time_by": {k: round(v) for k, v in sorted(spent.items(), key=lambda kv: -kv[1])},
            "notices": notices, "alerts": alerts, "tokens": models, "tokens_source": source, **extra}


def normalise(m):
    """Accept metrics produced by the legacy (Spanish) scripts or the broker."""
    m = {KEY_MAP.get(k, k): v for k, v in m.items()}
    m["tokens"] = {mod: {TOKEN_MAP.get(k, k): v for k, v in u.items()} for mod, u in (m.get("tokens") or {}).items()}
    for k in ("time_by", "alerts", "notices"):
        m.setdefault(k, {})
    return m


def remote_job_metrics(cfg, meta, job):
    """Run `broker metrics` on the job's node; None when it cannot be reached."""
    try:
        from . import remote
        node = cfg.node(meta["NODE"])
        cmd = f"cd ~/{node['workdir']} && {node['python']} delega.pyz broker metrics {shlex.quote(meta['CHANNEL'])}"
        r = remote.ssh(node, cmd, timeout=60)
        return normalise(json.loads(r.stdout)) if r.returncode == 0 else None
    except Exception:
        return None


def transcript_usage(path):
    """Tokens of a Claude Code session from its transcript .jsonl (e.g. the orchestrator's own)."""
    by_id, first, last = {}, None, None
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                e = json.loads(line)
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
                              {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "messages": 0})
        d["input"] += u.get("input_tokens", 0)
        d["output"] += u.get("output_tokens", 0)
        d["cache_read"] += u.get("cache_read_input_tokens", 0)
        d["cache_write"] += u.get("cache_creation_input_tokens", 0)
        d["messages"] += 1
    return {"session": os.path.basename(path), "from": first, "to": last, "tokens": models}


# ── report ──────────────────────────────────────────────────────────────────
def kilo(n):
    return f"{n / 1e6:.2f}M" if n >= 1e6 else f"{n / 1e3:.0f}k" if n >= 1e3 else str(n)


def resource_stats(rows):
    """rows = CSV data lines of nodes/sample.sh."""
    mem = [int(r[2]) for r in rows]
    load = [float(r[4]) for r in rows]
    idle = [int(r[7]) for r in rows]
    wait = [int(r[8]) for r in rows]
    oom = [int(r[13]) for r in rows]
    mem_sorted = sorted(mem)
    return {"n": len(rows), "from": rows[0][0][11:], "to": rows[-1][0][11:], "mem_total": rows[0][1],
            "mem_min": min(mem), "mem_median": mem_sorted[len(mem) // 2],
            "low_pct": 100 * sum(1 for x in mem if x < 500) / len(mem), "swap_max": max(int(r[3]) for r in rows),
            "load_avg": sum(load) / len(load), "load_max": max(load), "idle_avg": sum(idle) / len(idle),
            "wait_avg": sum(wait) / len(wait), "wait_max": max(wait), "oom": oom[-1] - oom[0],
            "disk": rows[-1][9], "containers": rows[-1][10]}


def read_csv(path):
    try:
        lines = open(path, encoding="utf-8").read().splitlines()[1:]
    except OSError:
        return []
    return [ln.split(",") for ln in lines if ln.count(",") >= CSV_COLS - 2]


def render_report(jobs, orchestrators, resources):
    out = [i18n.t("metrics.title", ts=util.now()), "", "## " + i18n.t("metrics.workers"), "",
           i18n.t("metrics.table_head"), "|---|---|---|---|---|---|---|---|---|"]
    tot = {"dur": 0, "api": 0, "inp": 0, "cr": 0, "cw": 0, "out": 0, "cost": 0.0}
    spent = {}
    for m in jobs:
        tk = m["tokens"].values()
        inp, cr = sum(v.get("input", 0) for v in tk), sum(v.get("cache_read", 0) for v in tk)
        cw, outt = sum(v.get("cache_write", 0) for v in tk), sum(v.get("output", 0) for v in tk)
        cost = m.get("cost_usd") or sum((v.get("cost_usd") or 0) for v in tk)
        for k, v in m["time_by"].items():
            spent[k] = spent.get(k, 0) + v
        tot["dur"] += m.get("duration_s") or 0
        tot["api"] += m.get("api_s") or 0
        tot["inp"] += inp; tot["cr"] += cr; tot["cw"] += cw; tot["out"] += outt; tot["cost"] += cost or 0
        alerts = ", ".join(f"{k}×{v}" for k, v in m["alerts"].items()) or "—"
        part = i18n.t("metrics.partial") if m.get("tokens_source") == "partial" else ""
        models = ",".join(k.replace("claude-", "") for k in m["tokens"])
        out.append(f"| {str(m.get('job'))[:22]} {models} | {m.get('state')} | {(m.get('duration_s') or 0) // 60} min | "
                   f"{int(m.get('api_s') or 0) // 60} min | {m.get('tools')} | {kilo(inp + cr + cw)} ({kilo(cr)} / {kilo(cw)}){part} | "
                   f"{kilo(outt)} | {cost or 0:.2f} | {alerts} |")
    out.append(f"| **total** | | {tot['dur'] // 60} min | {int(tot['api']) // 60} min | | {kilo(tot['inp'] + tot['cr'] + tot['cw'])} | "
               f"{kilo(tot['out'])} | {tot['cost']:.2f} | |")
    if spent:
        total = sum(spent.values()) or 1
        out += ["", "## " + i18n.t("metrics.time"), ""]
        out += [f"- {k}: {int(v) // 60} min ({100 * v / total:.0f} %)" for k, v in sorted(spent.items(), key=lambda kv: -kv[1])[:10]]
    for o in orchestrators:
        out += ["", "## " + i18n.t("metrics.orchestrator", sid=o["session"][:8], a=(o["from"] or "")[11:16], b=(o["to"] or "")[11:16]), ""]
        for mod, v in o["tokens"].items():
            out.append(i18n.t("metrics.orch_line", model=mod, n=v["messages"], inp=kilo(v["input"] + v["cache_read"] + v["cache_write"]),
                              cr=kilo(v["cache_read"]), cw=kilo(v["cache_write"]), out=kilo(v["output"])))
    for node, rows in resources.items():
        if not rows:
            continue
        s = resource_stats(rows)
        out += ["", "## " + i18n.t("metrics.resources", node=node, n=s["n"], a=s["from"], b=s["to"]), "",
                i18n.t("metrics.res_mem", **{k: (f"{v:.0f}" if isinstance(v, float) else v) for k, v in s.items()}),
                i18n.t("metrics.res_cpu", avg=f"{s['load_avg']:.1f}", mx=f"{s['load_max']:.1f}", idle=f"{s['idle_avg']:.0f}",
                       wa=f"{s['wait_avg']:.0f}", wamax=s["wait_max"]),
                i18n.t("metrics.res_oom", n=s["oom"]), i18n.t("metrics.res_disk", disk=s["disk"], containers=s["containers"])]
    return "\n".join(out)


# ── commands ────────────────────────────────────────────────────────────────
def sampler_script():
    for cand in (os.path.join(os.path.dirname(util.PKG_DIR), "..", "nodes", "sample.sh"),
                 os.environ.get("DELEGA_SAMPLE_SH", "")):
        if cand and os.path.exists(cand):
            return os.path.normpath(cand)
    return None


def _node_names(cfg, names):
    if names:
        return [cfg.node(n) for n in names]
    seen = {}
    for p in cfg.profiles:
        for n in cfg.nodes_for(cfg.profile(p)):
            seen[n["name"]] = n
    return list(seen.values())


def cmd_start_stop(cfg, action, names):
    from . import remote
    script = sampler_script()
    rc = 0
    for node in _node_names(cfg, names):
        mdir = f"~/{node['workdir']}/metrics"
        if action == "start":
            if not script:
                print(i18n.t("metrics.no_sampler"), file=sys.stderr)
                return 2
            remote.ssh(node, f"mkdir -p {mdir}")
            remote.scp_to(node, script, f"{mdir}/sample.sh")
            cmd = (f"p=$(cat {mdir}/sampler.pid 2>/dev/null); if [ -n \"$p\" ] && kill -0 \"$p\" 2>/dev/null; "
                   f"then echo \"already running ($p)\"; else S=$(command -v setsid || true); nohup $S bash {mdir}/sample.sh 15 {mdir} >/dev/null 2>&1 </dev/null & echo started; fi")
        else:
            cmd = f"kill $(cat {mdir}/sampler.pid) 2>/dev/null && echo stopped || echo 'not running'"
        r = remote.ssh(node, cmd, timeout=30)
        print(f"{node['name']}: {(r.stdout or r.stderr or '').strip()}")
        rc = rc or (r.returncode != 0)
    return int(rc)


def cmd_report(cfg, pattern, orchestrators):
    out_dir = os.path.join(cfg.state_dir, "metrics", time.strftime("%Y%m%d-%H%M%S"))
    os.makedirs(out_dir, exist_ok=True)
    jobs = []
    for name in sorted(os.listdir(cfg.state_dir)):
        d = os.path.join(cfg.state_dir, name)
        if not (name[:1].isdigit() and pattern in name and os.path.exists(os.path.join(d, "meta"))):
            continue
        meta = util.read_meta(d)
        local = (meta.get("NODE") or "local") == "local"
        ch = meta.get("CHANNEL", "")
        m = job_metrics(ch if local and ch.startswith("/") and os.path.isdir(ch) else d) if (local or os.path.exists(util.channel_file(d, "state.json"))) \
            else remote_job_metrics(cfg, meta, name)
        if m:
            m["job"] = name
            jobs.append(m)
            util.write_json(os.path.join(out_dir, f"metrics-{'-'.join(name.split('-')[:3])}.json"), m)
    resources = {}
    for node in _node_names(cfg, []):
        try:
            from . import remote
            dst = os.path.join(out_dir, f"resources-{node['name']}.csv")
            remote.scp_from(node, f"~/{node['workdir']}/metrics/resources.csv", dst)
            resources[node["name"]] = read_csv(dst)
        except Exception:
            pass
    orchs = [transcript_usage(p) for p in orchestrators]
    text = render_report(jobs, orchs, resources)
    with open(os.path.join(out_dir, "report.md"), "w", encoding="utf-8") as f:
        f.write(text + "\n")
    print(text)
    print("\n" + i18n.t("metrics.data", path=out_dir))
    return 0


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(i18n.t("metrics.usage"))
        return 0 if argv else 2
    try:
        cfg = config.load()
    except config.ConfigError as e:
        print(e, file=sys.stderr)
        return 2
    sub, rest = argv[0], argv[1:]
    if sub in ("start", "stop"):
        return cmd_start_stop(cfg, sub, rest)
    if sub == "report":
        pattern, orchs = "", []
        while rest:
            x = rest.pop(0)
            if x == "--orchestrator" and rest:
                orchs.append(rest.pop(0))
            else:
                pattern = x
        return cmd_report(cfg, pattern, orchs)
    print(i18n.t("metrics.usage"), file=sys.stderr)
    return 2

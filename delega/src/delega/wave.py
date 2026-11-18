"""wave: a wave of tasks as data in the central memory, driven by an idempotent step.

    delega wave plan   <flujo> <ola.tsv> [--wave N] [--db <ruta>]
    delega wave step   <flujo> [--par N] [--into <rama>] [--node <n>] [--repo-dir <dir>] [--db <ruta>]
    delega wave status <flujo> [--db <ruta>] [--json]

plan:  TSV `id<TAB>brief<TAB>zona<TAB>deps<TAB>run_args` (lines starting with `#` and an `id` header
       are ignored; `\\n` inside a brief is a line break; zona and deps are comma lists; run_args is a
       JSON object that becomes `delega run` flags). Everything is validated BEFORE writing: zones of the
       same wave must be disjoint (rc 2 and nothing written otherwise).
step:  what the daemon calls every 30 s. Only the lowest wave that is not all `hecha` moves.
       1. reconcile: `lanzada` / `arreglo` tasks whose job ended: rc 0 -> `ok`, else `rojo`
          (a failed fix job -> `bloqueada` + event `necesita_decision`);
       2. launch ready tasks (`mem next`) up to --par in flight, marking them `lanzada` (job_id, nodo, rama);
       3. when every task of the wave is `ok`: `delega integrate --merge --node` one by one into --into,
          each one `hecha`;
       4. integrate rc 7 (suite red): ONE fix job with the suite log in its brief, task `arreglo`
          (a second red after the fix -> `bloqueada` + `necesita_decision`);
       5. rc 8 (conflict): `bloqueada`, the files go in the event `necesita_decision`.
       The attempt counter (`intentos`) tells whether the fix was already tried: launch = 1, fix = 2.

Exit codes: 0 done, 2 usage / validation error (including overlapping zones).
The side effects (launch a job, look at a job, integrate) live in `Env`, so the tests replace them.
"""
import fnmatch
import json
import os
import subprocess
import sys
import time

from . import i18n, mem

LOG_TAIL = 6000
DEFAULT_PAR = 2
WILD = "*?["


# ── zones ───────────────────────────────────────────────────────────────────
def split_list(text):
    return [x.strip() for x in (text or "").split(",") if x.strip()]


def _prefix(glob):
    for i, ch in enumerate(glob):
        if ch in WILD:
            return glob[:i]
    return glob


def globs_overlap(a, b):
    """Conservative: True if some path can match both globs."""
    a, b = a.strip("/"), b.strip("/")
    la, lb = _prefix(a) == a, _prefix(b) == b
    if la and lb:
        return a == b or a.startswith(b + "/") or b.startswith(a + "/")
    if la:
        return fnmatch.fnmatchcase(a, b) or fnmatch.fnmatchcase(a + "/x", b)
    if lb:
        return fnmatch.fnmatchcase(b, a) or fnmatch.fnmatchcase(b + "/x", a)
    pa, pb = _prefix(a), _prefix(b)
    return pa.startswith(pb) or pb.startswith(pa)


def zones_overlap(za, zb):
    """Zones are comma lists of globs; an empty zone means the whole repo."""
    ga, gb = split_list(za) or ["**"], split_list(zb) or ["**"]
    return any(globs_overlap(x, y) for x in ga for y in gb)


# ── plan ────────────────────────────────────────────────────────────────────
def parse_tsv(path):
    """Rows of the TSV as dicts. Raises mem.MemError on a malformed file."""
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError as e:
        raise mem.MemError(i18n.t("wave.bad_file", path=path, err=e))
    rows = []
    for n, line in enumerate(lines, 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        cols = line.split("\t")
        if cols[0].strip() == "id" and not rows:
            continue
        cols += [""] * (5 - len(cols))
        rid, brief, zona, deps, ra = [c.strip() for c in cols[:5]]
        if not rid or not brief:
            raise mem.MemError(i18n.t("wave.bad_row", n=n))
        try:
            run_args = json.loads(ra) if ra else {}
            if not isinstance(run_args, dict):
                raise ValueError("not an object")
        except ValueError as e:
            raise mem.MemError(i18n.t("wave.bad_runargs", n=n, err=e))
        rows.append({"id": rid, "brief": brief.replace("\\n", "\n"), "zona": zona,
                     "deps": split_list(deps), "run_args": run_args, "line": n})
    if not rows:
        raise mem.MemError(i18n.t("wave.empty", path=path))
    return rows


def validate_rows(rows, existing_ids):
    seen = set()
    for r in rows:
        if r["id"] in seen or r["id"] in existing_ids:
            raise mem.MemError(i18n.t("wave.dup_id", id=r["id"]))
        for d in r["deps"]:
            if d not in seen and d not in existing_ids:
                raise mem.MemError(i18n.t("wave.bad_dep", id=r["id"], dep=d))
        seen.add(r["id"])
    for i, a in enumerate(rows):
        for b in rows[i + 1:]:
            if zones_overlap(a["zona"], b["zona"]):
                raise mem.MemError(i18n.t("wave.overlap", a=a["id"], b=b["id"],
                                          za=a["zona"] or "**", zb=b["zona"] or "**"))


def plan(conn, flow, path, wave=None):
    flow_row = conn.execute("SELECT id FROM flujos WHERE id = ?", (flow,)).fetchone()
    if flow_row is None:
        raise mem.MemError(i18n.t("mem.unknown_flow", flow=flow))
    rows = parse_tsv(path)
    existing = conn.execute("SELECT id, ola FROM tareas WHERE flujo = ?", (flow,)).fetchall()
    validate_rows(rows, {r["id"] for r in existing})
    if wave is None:
        wave = max([r["ola"] for r in existing] + [0]) + 1
    for r in rows:
        mem.add_task(conn, flow, r["brief"], wave=wave, zone=r["zona"] or None, deps=r["deps"],
                     run_args=r["run_args"], task_id=r["id"])
    return wave, rows


# ── side effects (replaceable) ──────────────────────────────────────────────
def run_flags(run_args, zona=None):
    flags = []
    for k, v in (run_args or {}).items():
        opt = "--" + str(k).replace("_", "-")
        if v is True:
            flags.append(opt)
        elif v in (False, None):
            continue
        else:
            for x in (v if isinstance(v, list) else [v]):
                flags += [opt, str(x)]
    if "zone" not in (run_args or {}):
        for z in split_list(zona):
            flags += ["--zone", z]
    return flags


def _pid_alive(pid):
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as f:
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, IndexError):
        pass
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


class Env:
    """The real world: `delega run` in the background, the job directories, `delega integrate`."""

    def __init__(self, repo_dir):
        self.repo_dir = os.path.abspath(repo_dir)
        self._cfg = None

    def cfg(self):
        if self._cfg is None:
            from . import config
            self._cfg = config.load()
        return self._cfg

    def launch(self, task, brief, branch, flags):
        """Start `delega run` detached. Returns the provisional job id `pend:<pid>:<stamp>`."""
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(time.time() - 5))
        env = dict(os.environ)
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env["PYTHONPATH"] = root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        logdir = os.path.join(self.cfg().state_dir, "wave")
        os.makedirs(logdir, exist_ok=True)
        with open(os.path.join(logdir, f"{task['id']}.log"), "ab") as out:
            p = subprocess.Popen([sys.executable, "-m", "delega", "run", self.repo_dir, brief,
                                  "--branch", branch, *flags],
                                 stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                 start_new_session=True, env=env, cwd=self.repo_dir)
        return f"pend:{p.pid}:{stamp}"

    def job_state(self, task):
        """Returns (state, job_id): state is running | ok | fail."""
        from . import integrate, jobs, util
        cfg = self.cfg()
        jid = task.get("job_id") or ""
        pend = jid.startswith("pend:")
        pid, stamp = (int(jid.split(":")[1]), jid.split(":")[2]) if pend else (None, "")
        found = None
        for d in reversed(jobs.job_dirs(cfg)):
            same = (jobs.job_id(d) == jid) if not pend else (
                util.read_meta(d).get("BRANCH") == task.get("rama") and jobs.job_id(d) >= stamp)
            if same:
                found = d
                break
        if found is None:
            if pend and _pid_alive(pid):
                return "running", jid
            return "fail", jid
        real = jobs.job_id(found)
        if not util.read_meta(found).get("END"):
            return "running", real
        summ = util.read_json(os.path.join(found, "summary.json"), {}) or {}
        return ("ok" if summ.get("rc") == 0 else "fail"), real

    def integrate(self, branch, into, node):
        cmd = [sys.executable, "-m", "delega", "integrate", self.repo_dir, branch, "--merge", "--into", into]
        if node and node != "auto":
            cmd += ["--node", node]
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env = dict(os.environ, PYTHONPATH=root + (os.pathsep + os.environ.get("PYTHONPATH", "")))
        r = subprocess.run(cmd, capture_output=True, text=True, env=env, cwd=self.repo_dir)
        return r.returncode, (r.stdout or "") + (r.stderr or "")


# ── step ────────────────────────────────────────────────────────────────────
def _tasks(conn, flow):
    return [mem._task_dict(r) for r in
            conn.execute("SELECT * FROM tareas WHERE flujo = ? ORDER BY ola, id", (flow,)).fetchall()]


def _event(conn, que, datos):
    mem.write(conn, lambda c: mem._event(c, que, datos))


def _conflict_files(out):
    return [ln.strip() for ln in out.splitlines() if ln.startswith("  ")]


def step(conn, flow, env, par=DEFAULT_PAR, into=None, node=None, say=print):
    flow_row = conn.execute("SELECT * FROM flujos WHERE id = ?", (flow,)).fetchone()
    if flow_row is None:
        raise mem.MemError(i18n.t("mem.unknown_flow", flow=flow))
    into = into or flow_row["rama_destino"] or "main"

    # 1. reconcile
    for t in _tasks(conn, flow):
        if t["estado"] not in ("lanzada", "arreglo"):
            continue
        state, jid = env.job_state(t)
        if state == "running":
            if jid != t["job_id"]:
                mem.set_task(conn, t["id"], t["estado"], job_id=jid)
            continue
        if state == "ok":
            mem.set_task(conn, t["id"], "ok", job_id=jid)
            say(i18n.t("wave.reconciled", id=t["id"], estado="ok"))
        elif t["estado"] == "arreglo":
            mem.set_task(conn, t["id"], "bloqueada", job_id=jid, error="fix job failed")
            _event(conn, "necesita_decision", {"flujo": flow, "tarea": t["id"], "motivo": "arreglo_fallido"})
            say(i18n.t("wave.fix_failed", id=t["id"]))
        else:
            mem.set_task(conn, t["id"], "rojo", job_id=jid)
            say(i18n.t("wave.reconciled", id=t["id"], estado="rojo"))

    tasks = _tasks(conn, flow)
    open_waves = sorted({t["ola"] for t in tasks if t["estado"] != "hecha"})
    if not open_waves:
        say(i18n.t("wave.all_done", flow=flow))
        return 0
    wave = open_waves[0]
    mine = [t for t in tasks if t["ola"] == wave]

    # 2. launch the ready ones
    inflight = sum(1 for t in tasks if t["estado"] in ("lanzada", "arreglo"))
    ready, _ = mem.next_tasks(conn, flow)
    for t in ready:
        if t["ola"] != wave:
            continue
        if inflight >= par:
            break
        branch = f"delega/wave-{flow}-{t['id']}"[:100]
        ra = t["run_args"]
        flags = run_flags(ra, t["zona"])
        if node and "node" not in ra:
            flags += ["--node", node]
        jid = env.launch(t, t["brief"], branch, flags)
        mem.set_task(conn, t["id"], "lanzada", job_id=jid, nodo=ra.get("node") or node or "auto", rama=branch)
        inflight += 1
        say(i18n.t("wave.launched", id=t["id"], branch=branch))

    # 3. integrate when the whole wave is ok
    mine = [t for t in _tasks(conn, flow) if t["ola"] == wave]
    pending = [t for t in mine if t["estado"] != "hecha"]
    if not pending or any(t["estado"] != "ok" for t in pending):
        return 0
    for t in pending:
        rc, out = env.integrate(t["rama"], into, node or (t["nodo"] if t["nodo"] != "auto" else None))
        if rc == 0:
            mem.set_task(conn, t["id"], "hecha")
            say(i18n.t("wave.merged", id=t["id"], into=into))
        elif rc == 7 and t["intentos"] < 2:
            brief = (t["brief"] + "\n\n" + i18n.t("wave.fix_header") + "\n\n" + out[-LOG_TAIL:])
            flags = run_flags(t["run_args"], t["zona"])
            if node and "node" not in t["run_args"]:
                flags += ["--node", node]
            jid = env.launch(t, brief, t["rama"], flags)
            mem.set_task(conn, t["id"], "arreglo", job_id=jid, intentos=t["intentos"] + 1)
            say(i18n.t("wave.fix_launched", id=t["id"]))
            return 0
        elif rc == 7:
            mem.set_task(conn, t["id"], "bloqueada", error="red after fix")
            _event(conn, "necesita_decision", {"flujo": flow, "tarea": t["id"], "motivo": "rojo_tras_arreglo",
                                               "registro": out[-LOG_TAIL:]})
            say(i18n.t("wave.blocked", id=t["id"]))
            return 0
        elif rc == 8:
            files = _conflict_files(out)
            mem.set_task(conn, t["id"], "bloqueada", error="conflict")
            _event(conn, "necesita_decision", {"flujo": flow, "tarea": t["id"], "motivo": "conflicto",
                                               "ficheros": files})
            say(i18n.t("wave.conflict", id=t["id"], n=len(files)))
            return 0
        else:
            say(i18n.t("wave.integrate_error", id=t["id"], rc=rc))
            return 0      # a transient error: the next step tries again
    return 0


# ── status ──────────────────────────────────────────────────────────────────
def status_table(conn, flow):
    if conn.execute("SELECT 1 FROM flujos WHERE id = ?", (flow,)).fetchone() is None:
        raise mem.MemError(i18n.t("mem.unknown_flow", flow=flow))
    tasks = _tasks(conn, flow)
    lines = []
    for wave in sorted({t["ola"] for t in tasks}):
        mine = [t for t in tasks if t["ola"] == wave]
        done = sum(1 for t in mine if t["estado"] == "hecha")
        lines.append(i18n.t("wave.status_wave", wave=wave, done=done, total=len(mine)))
        for t in mine:
            lines.append(f"  {t['id']:<16} {t['estado']:<10} {t['intentos']}  {t['nodo'] or '-':<10} "
                         f"{t['rama'] or '-'}")
    return tasks, lines


# ── CLI ─────────────────────────────────────────────────────────────────────
def _opts(argv, values, flags=()):
    pos, vals, fl, a = [], {}, set(), list(argv)
    while a:
        x = a.pop(0)
        if x.startswith("--") and x[2:] in values:
            if not a:
                raise mem.MemError(i18n.t("mem.bad_option", opt=x))
            vals[x[2:]] = a.pop(0)
        elif x.startswith("--") and x[2:] in flags:
            fl.add(x[2:])
        elif x.startswith("--"):
            raise mem.MemError(i18n.t("mem.bad_option", opt=x))
        else:
            pos.append(x)
    return pos, vals, fl


def main(argv, env=None):
    argv = list(argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(i18n.t("wave.usage"))
        return 0 if argv else 2
    cmd, rest = argv[0], argv[1:]
    if cmd not in ("plan", "step", "status"):
        print(i18n.t("wave.usage"), file=sys.stderr)
        return 2
    try:
        pos, vals, fl = _opts(rest, ("db", "wave", "par", "into", "node", "repo-dir"), ("json",))
        need = 2 if cmd == "plan" else 1
        if len(pos) != need:
            raise mem.MemError(i18n.t("wave.usage"))
        conn = mem.connect(mem.db_path(vals.get("db")))
        try:
            flow = pos[0]
            if cmd == "plan":
                wave, rows = plan(conn, flow, pos[1], int(vals["wave"]) if vals.get("wave") else None)
                print(i18n.t("wave.planned", flow=flow, wave=wave, n=len(rows)))
            elif cmd == "step":
                par = mem._number(vals.get("par", DEFAULT_PAR), "--par", int)
                return step(conn, flow, env or Env(vals.get("repo-dir") or os.getcwd()), par=par,
                            into=vals.get("into"), node=vals.get("node"))
            else:
                tasks, lines = status_table(conn, flow)
                if "json" in fl:
                    print(json.dumps({"ok": True, "flujo": flow, "tareas": tasks}, ensure_ascii=False))
                else:
                    print("\n".join(lines) or i18n.t("wave.no_tasks", flow=flow))
        finally:
            conn.close()
    except mem.MemError as e:
        print(f"delega wave: {e}", file=sys.stderr)
        return 2
    except ValueError as e:
        print(f"delega wave: {e}", file=sys.stderr)
        return 2
    return 0

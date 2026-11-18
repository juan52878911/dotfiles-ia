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
import re
import sqlite3
import subprocess
import sys
import time

from . import i18n, mem

LOG_TAIL = 6000
DEFAULT_PAR = 2
BUSY_RETRIES = 3          # rc 10 of `delega mem`: a locked database is retried
BUSY_WAIT_S = 0.2
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


def plan(conn, flow, path, wave=None, org=None):
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
                     run_args=r["run_args"], task_id=r["id"], org=org)
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
    try:                                   # si es hijo nuestro y ya terminó, recogerlo: sin /proc (macOS) un zombi parece vivo
        done, _ = os.waitpid(pid, os.WNOHANG)
        if done == pid:
            return False
    except OSError:                        # no es hijo nuestro (ChildProcessError) u otro error: seguir mirando
        pass
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


def _git(repo_dir, *args):
    r = subprocess.run(["git", "-C", repo_dir, *args], capture_output=True, text=True)
    return r.returncode, r.stdout.strip()


def norm_repo(repo):
    """`git@host:owner/name.git`, `https://host/owner/name` and `owner/name` -> `owner/name`."""
    r = (repo or "").strip().rstrip("/")
    if r.endswith(".git"):
        r = r[:-4]
    parts = [x for x in r.replace(":", "/").split("/") if x]
    return "/".join(parts[-2:]).lower()


def check_repo_dir(conn, flow, repo_dir):
    """rc 2 (MemError) unless the `origin` of repo_dir is the repo of the flow."""
    row = conn.execute("SELECT repo FROM flujos WHERE id = ?", (flow,)).fetchone()
    if row is None:
        raise mem.MemError(i18n.t("mem.unknown_flow", flow=flow))
    check_origin(repo_dir, row["repo"])


def check_origin(repo_dir, repo):
    """MemError unless the `origin` of repo_dir is `repo`."""
    rc, origin = _git(repo_dir, "remote", "get-url", "origin")
    if rc != 0 or not origin or norm_repo(origin) != norm_repo(repo):
        raise mem.MemError(i18n.t("wave.repo_mismatch", dir=repo_dir, origin=origin or "-", repo=repo))


def check_profiles(conn, flow, org, repo_dir=None):
    """A task whose profile belongs to another org than the base never runs from here.
    A task without a profile gets `profile_for(repo_dir)` at launch: that one is checked too."""
    default = None
    if repo_dir:
        try:
            from . import config
            default = config.load().profile_for(repo_dir)
        except Exception:
            default = None
    for t in as_store(conn, org).tasks(flow):
        po = mem._profile_org((t["run_args"] or {}).get("profile") or default)
        if po and po != org:
            raise mem.MemError(i18n.t("wave.profile_org", id=t["id"], org=po, base=org))


class Env:
    """The real world: `delega run` in the background, the job directories, `delega integrate`."""

    def __init__(self, repo_dir, org=None, bare=None):
        self.repo_dir = os.path.abspath(repo_dir)
        self.org = org
        self.bare = bare          # on a node: the bare repo the launches start from (see `integrate`)
        self._cfg = None

    def cfg(self):
        if self._cfg is None:
            from . import config
            self._cfg = config.load()
        return self._cfg

    def branch_prefix(self, task):
        """`branch_prefix` of the profile the task runs with (default `delega/`)."""
        try:
            cfg = self.cfg()
            name = (task.get("run_args") or {}).get("profile") or cfg.profile_for(self.repo_dir)
            return cfg.profile(name).get("branch_prefix") or "delega/"
        except Exception:
            return "delega/"

    def _wave_dir(self, create=True):
        d = os.path.join(self.cfg().state_dir, "wave", self.org or "_")
        if create:
            os.makedirs(d, exist_ok=True)
        return d

    def _stem(self, task):
        return f"{task.get('flujo', 'f')}-{task['id']}"

    def launch(self, task, brief, branch, flags):
        """Start `delega run` detached. Returns the provisional job id `pend:<pid>:<stamp>`.
        The brief goes to a file (`delega run` takes a path); the exit code of the run goes to
        `<stem>.rc` (6 = node full: the task goes back to `pendiente`)."""
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(time.time() - 5))
        env = dict(os.environ)
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env["PYTHONPATH"] = root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        env["DELEGA_FROM_QUEUE"] = "1"          # a full node fails with rc 6 instead of queueing
        wdir = self._wave_dir()
        stem = self._stem(task)
        brief_path = os.path.join(wdir, stem + ".md")
        with open(brief_path, "w", encoding="utf-8") as f:
            f.write(brief)
        rcfile = os.path.join(wdir, stem + ".rc")
        if os.path.exists(rcfile):
            os.remove(rcfile)
        cmd = [sys.executable, "-m", "delega", "run", self.repo_dir, brief_path, "--branch", branch, *flags]
        with open(os.path.join(wdir, stem + ".log"), "ab") as out:
            p = subprocess.Popen(["sh", "-c", '"$@"; echo $? > "$RCFILE"', "sh", *cmd],
                                 stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                 start_new_session=True, env=dict(env, RCFILE=rcfile), cwd=self.repo_dir)
        return f"pend:{p.pid}:{stamp}"

    def launch_refused(self, task):
        """True when the last `delega run` of the task ended refused (rc `launch.REFUSED_RC`) before it made
        a job directory, or its log carries the refusal for an open branch."""
        from . import launch
        if self._job_dir(task) is not None:
            return False
        stem = self._stem(task)
        try:
            with open(os.path.join(self._wave_dir(create=False), stem + ".rc")) as f:
                rc = f.read().strip()
            if rc:
                return rc == str(launch.REFUSED_RC)
        except OSError:
            pass
        try:
            with open(os.path.join(self._wave_dir(create=False), stem + ".log"), encoding="utf-8",
                      errors="replace") as f:
                log = f.read()[-LOG_TAIL:]
        except OSError:
            return False
        parts = i18n.t("launch.err.branch_in_use", branch="\0", job="\0", path="\0").split("\0")
        return re.search(".*?".join(re.escape(p) for p in parts), log) is not None

    def _fetched_sha(self, job_dir, jid, branch):
        """The sha the job brought to this repo, or None if its fetch did not succeed.
        `launch` writes `FETCHED=<sha>` in meta when the fetch worked; `fetch.log` (empty = ok)
        is only the fallback for old jobs without that key."""
        from . import util
        meta = util.read_meta(job_dir)
        if "FETCHED" in meta:
            sha = meta["FETCHED"].strip()
            if not re.fullmatch(r"[0-9a-f]{7,64}", sha):
                return None
            rc, full = _git(self.repo_dir, "rev-parse", "--verify", "-q", f"{sha}^{{commit}}")
            return full if rc == 0 and full else None
        try:
            with open(os.path.join(job_dir, "fetch.log"), encoding="utf-8") as f:
                log = f.read().strip()
        except OSError:
            return None
        rc, sha = _git(self.repo_dir, "rev-parse", "--verify", "-q", f"refs/delega/{jid}^{{commit}}")
        if rc == 0 and sha:
            return sha                         # the branch was open in a worktree: it came to the ref
        if log:
            return None                        # git said something: the fetch failed
        rc, sha = _git(self.repo_dir, "rev-parse", "--verify", "-q", f"refs/heads/{branch}^{{commit}}")
        return sha if rc == 0 and sha else None

    def _job_dir(self, task):
        """The newest job directory of the task (by real job id, or by branch for a `pend:` id)."""
        from . import jobs, util
        cfg = self.cfg()
        jid = task.get("job_id") or ""
        pend = jid.startswith("pend:")
        stamp = jid.split(":")[2] if pend else ""
        for d in reversed(jobs.job_dirs(cfg)):
            same = (jobs.job_id(d) == jid) if not pend else (
                util.read_meta(d).get("BRANCH") == task.get("rama") and jobs.job_id(d) >= stamp)
            if same:
                return d
        return None

    def log_tail(self, task, lines=80):
        """The last lines of progress.log / stderr.log of the task's job ('' when there is none)."""
        from . import util
        d = self._job_dir(task)
        out = []
        for name in ("progress.log", "stderr.log"):
            try:
                with open(util.channel_file(d, name), encoding="utf-8", errors="replace") as f:
                    tail = f.read().splitlines()[-lines:]
            except (OSError, TypeError):
                continue
            if tail:
                out.append(f"--- {name} ---\n" + "\n".join(tail))
        return self._redacted(task, d, "\n".join(out))[-LOG_TAIL:]

    def _redacted(self, task, job_dir, text):
        """`text` through `broker.redact` with the task's profile terms (and the job's own forbidden.txt)."""
        from . import broker, nod
        terms = []
        try:
            name = (task.get("run_args") or {}).get("profile") or None
            terms += nod.terms_of(self.cfg().profile(name).get("forbidden_terms_file"))
        except Exception:      # noqa: BLE001 — unknown profile: only the job's own terms
            pass
        if job_dir:
            terms += nod.job_terms(job_dir)
        return broker.redact(text, sorted(set(terms)))

    def job_state(self, task, write=True):
        """Returns (state, job_id, sha): state is running | ok | fail | queued | no_encontrado (a real
        job id whose directory is not on this host: nothing says it failed); sha only when ok.
        With `write=False` nothing is created (`delega job-state` answers over ssh)."""
        from . import jobs, util
        cfg = self.cfg()
        jid = task.get("job_id") or ""
        pend = jid.startswith("pend:")
        pid = int(jid.split(":")[1]) if pend else None
        found = self._job_dir(task)
        if found is None:
            if pend and _pid_alive(pid):
                return "running", jid, None
            try:
                with open(os.path.join(self._wave_dir(create=write), self._stem(task) + ".rc")) as f:
                    if f.read().strip() == "6":
                        return "queued", jid, None
            except OSError:
                pass
            return ("fail" if pend or not jid else NOT_FOUND), jid, None
        real = jobs.job_id(found)
        meta = util.read_meta(found)
        if not meta.get("END"):
            return "running", real, None
        summ = util.read_json(os.path.join(found, "summary.json"), {}) or {}
        if summ.get("rc") != 0:
            return "fail", real, None
        sha = self._fetched_sha(found, real, task.get("rama"))
        base = (meta.get("BASE") or "").strip()
        if sha and len(base) >= 7 and (sha.startswith(base) or base.startswith(sha)):
            return "fail", real, None        # rc 0 without a commit (a blocker, a refused hook): not done
        return ("ok", real, sha) if sha else ("fail", real, None)

    def integrate(self, branch, into, node, sha=None):
        cmd = [sys.executable, "-m", "delega", "integrate", self.repo_dir, branch, "--merge", "--into", into]
        if sha:
            cmd += ["--sha", sha]
        if node and node != "auto":
            cmd += ["--node", node]
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env = dict(os.environ, PYTHONPATH=root + (os.pathsep + os.environ.get("PYTHONPATH", "")))
        r = subprocess.run(cmd, capture_output=True, text=True, env=env, cwd=self.repo_dir)
        out = (r.stdout or "") + (r.stderr or "")
        if r.returncode == 0 and self.bare:
            # on a node the next launch starts from the bare repo: what was merged goes there (no `+`)
            p = subprocess.run(["git", "-C", self.repo_dir, "push", "-q", self.bare,
                                f"refs/heads/{into}:refs/heads/{into}"], capture_output=True, text=True)
            if p.returncode != 0:
                print(i18n.t("wave.integrate_push_failed", id=branch, into=into,
                             err=(p.stderr or p.stdout).strip()), file=sys.stderr)
        return r.returncode, out


# ── step ────────────────────────────────────────────────────────────────────
NOT_FOUND = "no_encontrado"


def _tasks(conn, flow):
    return [mem._task_dict(r) for r in
            conn.execute("SELECT * FROM tareas WHERE flujo = ? ORDER BY ola, id", (flow,)).fetchall()]


def _retry(fn):
    """Run fn(), retrying a busy/locked database (what rc 10 is for `delega mem`)."""
    for attempt in range(BUSY_RETRIES):
        try:
            return fn()
        except sqlite3.OperationalError as e:
            if not mem._db_is_busy(e) or attempt == BUSY_RETRIES - 1:
                raise
            time.sleep(BUSY_WAIT_S * (attempt + 1))


def _event(conn, que, datos):
    _retry(lambda: mem.add_event(conn, que, datos, quien=mem.owner_id()))


def _set(conn, org, task, estado, desde, force=False, **kw):
    """Compare-and-set of a task state (`desde` is the state this step read). False when
    another orchestrator moved the task first (rc 9): the caller leaves it to them."""
    try:
        _retry(lambda: mem.set_task(conn, task, estado, desde=desde, force=force, org=org, **kw))
        return True
    except mem.MemConflict:
        return False


def _sha_of(conn, flow, task):
    """The sha the last job of the task brought (event `wave.sha`), or None."""
    for e in reversed(mem.log_events(conn, limit=0, flow=flow)):
        if e["que"] == "wave.sha" and e["datos"].get("tarea") == task:
            return e["datos"].get("sha")
    return None


# ── stores: the base as step sees it (local SQLite, or `delega mem` over ssh) ────────────────
class LocalStore:
    """The base is a SQLite file on this host."""

    def __init__(self, conn, org=None):
        self.conn, self.org = conn, org

    def flow(self, flow):
        row = self.conn.execute("SELECT * FROM flujos WHERE id = ?", (flow,)).fetchone()
        return dict(row) if row is not None else None

    def tasks(self, flow):
        return _tasks(self.conn, flow)

    def next(self, flow):
        return mem.next_tasks(self.conn, flow)[0]

    def set(self, task, estado, desde, force=False, **kw):
        """Compare-and-set of a task state. False when another orchestrator moved it first."""
        try:
            _retry(lambda: mem.set_task(self.conn, task, estado, desde=desde, force=force, org=self.org, **kw))
            return True
        except mem.MemConflict:
            return False

    def event(self, que, datos, task=None, once=False):
        _retry(lambda: mem.add_event(self.conn, que, datos, task=task, once=once, quien=mem.owner_id()))

    def events(self, flow):
        return mem.log_events(self.conn, limit=0, flow=flow)

    def sha_of(self, flow, task):
        for e in reversed(mem.log_events(self.conn, limit=0, flow=flow)):
            if e["que"] == "wave.sha" and e["datos"].get("tarea") == task:
                return e["datos"].get("sha")
        return None


class ClientStore:
    """The base is on another host: every call is a `delega mem … --json` over ssh through the
    queue's `MemClient`, which carries `--owner`/`--org` (and the epoch fence of mem) on each write."""

    def __init__(self, client):
        self.client, self.org = client, client.org

    def flow(self, flow):
        for f in self.client.flows():
            if f["id"] == flow:
                return f
        return None

    def tasks(self, flow):
        rc, res = self.client._call(lambda: self.client._remote("export"))
        if rc != mem.EXIT_OK:
            raise mem.MemError(f"export: rc={rc}")
        return [t for t in res.get("tareas", []) if t["flujo"] == flow]

    def next(self, flow):
        return self.client.ready(flow)

    def set(self, task, estado, desde, force=False, **kw):
        rc, payload = self.client.set(task, estado, desde=desde, force=force, **kw)
        if rc == mem.EXIT_OK:
            return True
        if rc == mem.EXIT_LEASE:
            return False
        raise mem.MemError(payload.get("error") or f"set {task}: rc={rc}")

    def event(self, que, datos, task=None, once=False):
        extra = (["--task", task] if task else []) + (["--once"] if once and task else [])
        rc, payload = self.client._call(lambda: self.client._remote(
            "event", que, *extra, "--datos", json.dumps(datos, ensure_ascii=False)))
        if rc != mem.EXIT_OK:
            raise mem.MemError(payload.get("error") or f"event: rc={rc}")

    def events(self, flow):
        rc, payload = self.client._call(lambda: self.client._remote("log", "--flow", flow, "--limit", "0"))
        return payload.get("eventos", [])

    def sha_of(self, flow, task):
        rc, payload = self.client._call(lambda: self.client._remote("log", "--flow", flow, "--limit", "0"))
        for e in reversed(payload.get("eventos", [])):
            if e["que"] == "wave.sha" and e["datos"].get("tarea") == task:
                return e["datos"].get("sha")
        return None


def as_store(base, org=None):
    """`base` is a sqlite connection, a MemClient, or already a store."""
    if hasattr(base, "execute"):
        return LocalStore(base, org)
    if hasattr(base, "_remote"):
        return ClientStore(base)
    return base


def _needed_by_others(tasks, wave):
    """ids of the tasks of the wave that another task not yet `hecha` depends on."""
    need = set()
    for t in tasks:
        if t["estado"] != "hecha":
            need.update(t.get("deps") or [])
    return need


def _conflict_files(out):
    return [ln.strip() for ln in out.splitlines() if ln.startswith("  ")]


MAX_REFUSED = 3


def _known(env, t, lanzador=None):
    """`registro` of a decision: the tail of the task's job log, else one line of what is known."""
    tail = ""
    try:
        tail = env.log_tail(t) if hasattr(env, "log_tail") else ""
    except Exception:
        pass
    if tail:
        return tail[-LOG_TAIL:]
    return (f"job_id={t.get('job_id') or '-'} lanzador={lanzador or t.get('lanzador') or '-'} "
            f"error={t.get('error') or '-'} estado={t.get('estado') or '-'}")


def _launch_refused(env, t):
    """True when the job of the task never ran because `delega run` refused to launch it."""
    fn = getattr(env, "launch_refused", None)
    return bool(fn and fn(t))


def _count_refused(st, flow, t):
    """Consecutive refused launches of the task (events; a job that brought a sha starts the count again)."""
    n = 0
    for e in st.events(flow):
        d = e.get("datos") or {}
        if d.get("tarea") != t["id"]:
            continue
        if e["que"] == "wave.lanzamiento_rechazado":
            n += 1
        elif e["que"] == "wave.sha":
            n = 0
    return n


def step(conn, flow, env, par=DEFAULT_PAR, into=None, node=None, say=print, org=None, lanzador=None,
         resolve=None):
    """One step of a flow. `conn` is a sqlite connection (local base) or a MemClient (remote base).
    `lanzador` is the host written on every task this step launches. `resolve(task)` decides the state
    of a `lanzada`/`arreglo` task: (state, job_id, sha), or None to leave it as it is (the job is
    another host's and it did not answer); the default is `env.job_state`, this host only."""
    if not org:
        raise mem.MemError(i18n.t("wave.no_org"))
    st = as_store(conn, org)
    flow_row = st.flow(flow)
    if flow_row is None:
        raise mem.MemError(i18n.t("mem.unknown_flow", flow=flow))
    into = into or flow_row["rama_destino"] or "main"

    # 1. reconcile
    for t in st.tasks(flow):
        if t["estado"] not in ("lanzada", "arreglo"):
            continue
        res = resolve(t) if resolve else env.job_state(t)
        if res is None:
            continue
        state, jid = res[0], res[1]
        sha = res[2] if len(res) > 2 else None
        if state == "running":
            if jid != t["job_id"]:
                st.set(t["id"], t["estado"], t["estado"], job_id=jid)
            continue
        if state == NOT_FOUND:            # no directory on the host that should have it: not a failure
            st.event("necesita_decision", {"flujo": flow, "tarea": t["id"], "motivo": "job_sin_directorio",
                                           "job_id": jid, "registro": _known(env, t, lanzador)},
                     task=t["id"], once=True)
            continue
        if state == "queued":
            # the node was full: `delega run` did not start (rc 6). Give the attempt back.
            back = "pendiente" if t["estado"] == "lanzada" else (
                "ok" if st.sha_of(flow, t["id"]) else "rojo")
            if st.set(t["id"], back, t["estado"], force=True, job_id="",
                    intentos=max(t["intentos"] - 1, 0)):
                say(i18n.t("wave.requeued", id=t["id"]))
            continue
        if state == "ok":
            if sha:
                st.event("wave.sha", {"flujo": flow, "tarea": t["id"], "sha": sha})
            if t["estado"] == "arreglo":     # the fix job came back: arreglo -> lanzada -> ok
                if not st.set(t["id"], "lanzada", "arreglo", job_id=jid, intentos=t["intentos"]):
                    continue
            if not st.set(t["id"], "ok", "lanzada", job_id=jid):
                continue
            say(i18n.t("wave.reconciled", id=t["id"], estado="ok"))
        elif t["estado"] == "arreglo" and _launch_refused(env, t):
            # the fix job was refused at launch (rc 2: branch still open in a job's worktree…): it never ran,
            # so it is not a failed fix. Back to `ok` (no attempt added), retried on a later step.
            refused = _count_refused(st, flow, t) + 1
            if not st.set(t["id"], "ok", "arreglo", force=True, job_id=jid, intentos=max(t["intentos"] - 1, 0)):
                continue
            st.event("wave.lanzamiento_rechazado", {"flujo": flow, "tarea": t["id"]}, task=t["id"])
            if refused >= MAX_REFUSED:
                # a task that is refused again and again is blocked (ok/arreglo -> bloqueada): no more retries
                st.set(t["id"], "bloqueada", "ok", error="launch refused")
                st.event("necesita_decision", {"flujo": flow, "tarea": t["id"], "motivo": "lanzamiento_rechazado",
                                               "registro": _known(env, t, lanzador)}, task=t["id"], once=True)
            say(i18n.t("wave.fix_refused", id=t["id"], n=refused))
        elif t["estado"] == "arreglo":
            if not st.set(t["id"], "bloqueada", "arreglo", job_id=jid, error="fix job failed"):
                continue
            st.event("necesita_decision", {"flujo": flow, "tarea": t["id"], "motivo": "arreglo_fallido",
                                           "registro": _known(env, t, lanzador)}, task=t["id"], once=True)
            say(i18n.t("wave.fix_failed", id=t["id"]))
        else:
            if not st.set(t["id"], "rojo", "lanzada", job_id=jid):
                continue
            say(i18n.t("wave.reconciled", id=t["id"], estado="rojo"))

    # 1b. a failed job: one retry as a fix, then a decision
    inflight = sum(1 for t in st.tasks(flow) if t["estado"] in ("lanzada", "arreglo"))
    for t in st.tasks(flow):
        if t["estado"] != "rojo":
            continue
        if t["intentos"] >= 2:
            if st.set(t["id"], "bloqueada", "rojo", error="failed twice"):
                st.event("necesita_decision", {"flujo": flow, "tarea": t["id"], "motivo": "rojo_repetido",
                                               "registro": _known(env, t, lanzador)})
                say(i18n.t("wave.blocked", id=t["id"]))
            continue
        if inflight >= par:
            continue
        if not st.set(t["id"], "arreglo", "rojo", intentos=t["intentos"] + 1):
            continue
        brief = t["brief"] + "\n\n" + i18n.t("wave.retry_header")
        tail = env.log_tail(t) if hasattr(env, "log_tail") else ""
        if tail:
            brief += "\n\n" + i18n.t("wave.retry_log") + "\n" + tail
        flags = run_flags({k: v for k, v in t["run_args"].items() if k != "branch"}, t["zona"])
        if node and "node" not in t["run_args"]:
            flags += ["--node", node]
        jid = env.launch(t, brief, t["rama"], flags)
        st.set(t["id"], "arreglo", "arreglo", job_id=jid, lanzador=lanzador)
        inflight += 1
        say(i18n.t("wave.retry_launched", id=t["id"]))

    tasks = st.tasks(flow)
    open_waves = sorted({t["ola"] for t in tasks if t["estado"] != "hecha"})
    if not open_waves:
        say(i18n.t("wave.all_done", flow=flow))
        return 0
    wave = open_waves[0]
    mine = [t for t in tasks if t["ola"] == wave]

    # 2. launch the ready ones
    inflight = sum(1 for t in tasks if t["estado"] in ("lanzada", "arreglo"))
    ready = st.next(flow)
    for t in ready:
        if t["ola"] != wave:
            continue
        if inflight >= par:
            break
        ra = t["run_args"]
        prefix = env.branch_prefix(t) if hasattr(env, "branch_prefix") else "delega/"
        branch = str(ra.get("branch") or f"{prefix}wave-{flow}-{t['id']}")[:100]
        flags = run_flags({k: v for k, v in ra.items() if k != "branch"}, t["zona"])
        if node and "node" not in ra:
            flags += ["--node", node]
        # claim the task first (pendiente -> lanzada), launch only if this orchestrator won it
        if not st.set(t["id"], "lanzada", "pendiente", nodo=ra.get("node") or node or "auto", rama=branch,
                lanzador=lanzador):
            continue
        jid = env.launch(t, t["brief"], branch, flags)
        st.set(t["id"], "lanzada", "lanzada", job_id=jid, lanzador=lanzador)
        inflight += 1
        say(i18n.t("wave.launched", id=t["id"], branch=branch))

    # 3. integrate when the whole wave is ok
    tasks_now = st.tasks(flow)
    mine = [t for t in tasks_now if t["ola"] == wave]
    pending = [t for t in mine if t["estado"] != "hecha"]
    if not pending:
        return 0
    if any(t["estado"] != "ok" for t in pending):
        # the wave is not whole yet; but an ok task that the rest depends on is integrated now,
        # or its dependents would never become ready (a flow with deps and a single wave)
        need = _needed_by_others(tasks_now, wave)
        pending = [t for t in pending if t["estado"] == "ok" and t["id"] in need]
        if not pending:
            return 0
    for t in pending:
        rc, out = env.integrate(t["rama"], into, node or (t["nodo"] if t["nodo"] != "auto" else None),
                                sha=st.sha_of(flow, t["id"]))
        if rc == 0:
            st.set(t["id"], "hecha", "ok")
            say(i18n.t("wave.merged", id=t["id"], into=into))
        elif rc == 7 and t["intentos"] < 2:
            brief = (t["brief"] + "\n\n" + i18n.t("wave.fix_header") + "\n\n" + out[-LOG_TAIL:])
            flags = run_flags({k: v for k, v in t["run_args"].items() if k != "branch"}, t["zona"])
            if node and "node" not in t["run_args"]:
                flags += ["--node", node]
            # integration found it red after the job said ok: ok -> arreglo is not a transition of mem,
            # so it is forced, but only from `ok` (CAS). Claim first, launch only if this step won.
            if not st.set(t["id"], "arreglo", "ok", force=True, intentos=t["intentos"] + 1):
                return 0
            jid = env.launch(t, brief, t["rama"], flags)
            st.set(t["id"], "arreglo", "arreglo", job_id=jid, lanzador=lanzador)
            say(i18n.t("wave.fix_launched", id=t["id"]))
            return 0
        elif rc == 7:
            st.set(t["id"], "bloqueada", "ok", force=True, error="red after fix")
            st.event("necesita_decision", {"flujo": flow, "tarea": t["id"], "motivo": "rojo_tras_arreglo",
                                               "registro": out[-LOG_TAIL:]})
            say(i18n.t("wave.blocked", id=t["id"]))
            return 0
        elif rc == 12:      # no `verify` for the project: nothing a fix job can repair, a decision now
            st.set(t["id"], "bloqueada", "ok", force=True, error="no verify")
            st.event("necesita_decision", {"flujo": flow, "tarea": t["id"], "motivo": "sin_verify",
                                               "registro": out[-LOG_TAIL:]})
            say(i18n.t("wave.no_verify", id=t["id"]))
            return 0
        elif rc == 11:      # the target moved (another job): retry on the next step
            say(i18n.t("wave.retarget", id=t["id"], into=into))
            return 0
        elif rc == 8:
            files = _conflict_files(out)
            st.set(t["id"], "bloqueada", "ok", force=True, error="conflict")
            st.event("necesita_decision", {"flujo": flow, "tarea": t["id"], "motivo": "conflicto",
                                               "ficheros": files, "registro": out[-LOG_TAIL:]})
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
        pos, vals, fl = _opts(rest, ("db", "org", "wave", "par", "into", "node", "repo-dir"), ("json",))
        org = (vals.get("org") or os.environ.get("DELEGA_MEM_ORG") or "").strip()
        if cmd in ("plan", "step") and not org:
            raise mem.MemError(i18n.t("wave.no_org"))
        need = 2 if cmd == "plan" else 1
        if len(pos) != need:
            raise mem.MemError(i18n.t("wave.usage"))
        conn = mem.connect(mem.db_path(vals.get("db"), org=org or None))
        try:
            flow = pos[0]
            if cmd == "plan":
                wave, rows = plan(conn, flow, pos[1], int(vals["wave"]) if vals.get("wave") else None, org=org)
                print(i18n.t("wave.planned", flow=flow, wave=wave, n=len(rows)))
            elif cmd == "step":
                par = mem._number(vals.get("par", DEFAULT_PAR), "--par", int)
                repo_dir = vals.get("repo-dir") or os.getcwd()
                check_profiles(conn, flow, org, repo_dir if env is None else None)
                if env is None:
                    check_repo_dir(conn, flow, repo_dir)
                    env = Env(repo_dir, org=org)
                return step(conn, flow, env, par=par,
                            into=vals.get("into"), node=vals.get("node"), org=org)
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

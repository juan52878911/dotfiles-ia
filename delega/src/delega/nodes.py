"""nodes: probe a node (or this machine), judge it against policy, and pick one for a job.

The probe is one Python script sent over ssh on stdin (`ssh <alias> bash -lc 'python3 -'`); its
arguments travel in the DELEGA_PROBE environment variable as JSON. It runs the same way on
Linux and macOS and prints a single JSON object. No secret is ever printed.
"""
import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time

from . import config as _config
from . import remote
from .i18n import t

PROBE_PY = r'''
import hashlib, json, os, platform, re, shutil, subprocess, sys, urllib.error, urllib.parse, urllib.request

A = json.loads(os.environ.get("DELEGA_PROBE") or "{}")
HOME = os.path.expanduser("~")


def run(argv, env=None, timeout=20):
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=env,
                           stdin=subprocess.DEVNULL)
        return r.returncode, r.stdout
    except FileNotFoundError:
        return 127, ""
    except Exception:
        return 124, ""


def mem_mb():
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable"):
                    return int(line.split()[1]) // 1024
    except OSError:
        pass
    rc, out = run(["vm_stat"])
    if rc == 0:
        m = re.search(r"page size of (\d+)", out)
        size = int(m.group(1)) if m else 4096
        pages = 0
        for key in ("Pages free", "Pages inactive", "Pages speculative"):
            m = re.search(key + r":\s+(\d+)", out)
            pages += int(m.group(1)) if m else 0
        return pages * size // (1024 * 1024)
    return None


def adc(path):
    """Refresh the Google ADC token without printing it: ok | expired | missing | error_<code>_<err> | unknown."""
    try:
        with open(os.path.expanduser(path)) as f:
            d = json.load(f)
    except Exception:
        return "missing"
    if d.get("type") != "authorized_user":
        return "ok"
    data = urllib.parse.urlencode({"client_id": d["client_id"], "client_secret": d["client_secret"],
                                   "refresh_token": d["refresh_token"], "grant_type": "refresh_token"}).encode()
    try:
        urllib.request.urlopen("https://oauth2.googleapis.com/token", data, timeout=10)
        return "ok"
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read().decode()).get("error", "")
        except Exception:
            err = ""
        return "expired" if err in ("invalid_grant", "invalid_rapt") else "error_%s_%s" % (e.code, err or "unknown")
    except Exception:
        return "unknown"


def compute(c):
    kind = c.get("kind")
    if kind == "gcp-adc":
        return adc(c.get("adc_path") or "~/.config/gcloud/application_default_credentials.json")
    if kind == "env":
        return "ok" if os.environ.get(c.get("var", "")) else "missing"
    if kind == "command":
        rc, _ = run(c.get("check") or ["false"])
        return "ok" if rc == 0 else ("missing" if rc == 127 else "error_rc%d" % rc)
    return "unknown"


out = {"os": platform.system().lower(), "python": platform.python_version()}
launcher = A.get("launcher") or ["claude"]
env = dict(os.environ)
if A.get("config_dir"):
    env["CLAUDE_CONFIG_DIR"] = os.path.expanduser(A["config_dir"])
if shutil.which(launcher[0]) or os.path.isabs(launcher[0]):
    rc, o = run(launcher + ["auth", "status"], env=env)
    try:
        out["email"] = json.loads(o).get("email") or ""
    except Exception:
        out["email"] = ""
    rc, o = run(launcher + ["--version"], env=env)
    m = re.search(r"\d+\.\d+(\.\d+)?", o)
    out["claude_version"] = m.group(0) if m else None
    out["claude"] = rc != 127
else:
    out["claude"], out["email"], out["claude_version"] = False, None, None
out["mem_mb"] = mem_mb()
out["disk_gb"] = shutil.disk_usage(HOME).free // (1 << 30)
try:
    out["load_pct"] = int(os.getloadavg()[0] * 100 / (os.cpu_count() or 1))
except OSError:
    out["load_pct"] = None
rc, o = run(["docker", "ps", "-q"])
out["containers"] = len(o.split()) if rc == 0 else None
pyz = os.path.join(HOME, A.get("workdir") or "delega", "delega.pyz")
out["pyz_sha"] = ""
if os.path.exists(pyz):
    with open(pyz, "rb") as f:
        out["pyz_sha"] = hashlib.sha256(f.read()).hexdigest()[:12]
out["compute"] = {c["name"]: compute(c) for c in A.get("requires", [])}
print(json.dumps(out))
'''


# ── helpers ─────────────────────────────────────────────────────────────────
def config_dir_for(profile, node=None):
    """Config dir for the profile on a node (node.config_dirs[profile]) or locally (profile.config_dir)."""
    if node:
        cd = (node.get("config_dirs") or {}).get(profile["name"])
        if cd:
            return cd
    return profile.get("config_dir")


def _compute_defs(cfg, profile, requires):
    names = list(dict.fromkeys(list(requires or ())))
    out = []
    for n in names:
        if n not in cfg.compute:
            raise _config.ConfigError(f"unknown compute '{n}' (known: {', '.join(cfg.compute) or 'none'})")
        out.append({"name": n, **cfg.compute[n]})
    return out


def launcher_for(profile, node):
    """argv that starts claude for this profile: the profile's launcher on this machine, the
    node's own `launcher` (default ["claude"]) on a node — the account there is chosen by
    config_dirs, not by a local wrapper such as a per-folder account switcher."""
    if node:
        return list(node.get("launcher") or ["claude"])
    return list(profile["launcher"])


def _probe_args(cfg, profile, node, requires):
    return {"launcher": launcher_for(profile, node), "workdir": (node or {}).get("workdir", "delega"),
            "config_dir": config_dir_for(profile, node), "requires": _compute_defs(cfg, profile, requires)}


def _login_fix(profile, node, alias_):
    cd = config_dir_for(profile, node)
    cmd = " ".join(shlex.quote(x) for x in launcher_for(profile, node))
    inner = (f"CLAUDE_CONFIG_DIR={cd} " if cd else "") + cmd + " /login"
    return f"ssh -t {alias_} {shlex.quote(inner)}" if alias_ else inner


def _identity_problem(profile, email):
    """None if the account matches the profile's expectations, else the reason key."""
    ident = profile.get("identity") or {}
    want, dom = ident.get("expect_email"), ident.get("expect_email_domain")
    if not email:
        return "not_logged_in"
    if want and email.lower() != want.lower():
        return "account_mismatch"
    if dom and not email.lower().endswith("@" + dom.lower().lstrip("@")):
        return "account_mismatch"
    return None


def evaluate(cfg, profile, node, raw, requires=(), resources=True, remote_node=True):
    """Turn a raw probe result into the contract dict with ok/reason/fix.
    `kind` says what failed: unreachable | claude | identity | resources | compute | ok."""
    res = dict(raw or {})
    res.setdefault("compute", {})
    a = remote.alias(node) if node else None
    res.update(ok=False, reason="", fix="", kind="")

    def fail(kind, key, fix="", **fmt):
        res.update(kind=kind, reason=t("nodes.reason." + key, **fmt), fix=fix)
        return res

    if not raw:
        return fail("unreachable", "unreachable", f"ssh {a}", node=a)
    if not raw.get("claude"):
        return fail("claude", "no_claude", t("nodes.fix.no_claude", node=a or "localhost"),
                    launcher=launcher_for(profile, node if remote_node else None)[0])
    ident = profile.get("identity") or {}
    if remote_node and not (ident.get("expect_email") or ident.get("expect_email_domain")):
        return fail("identity", "no_identity", t("nodes.fix.no_identity", profile=profile["name"]),
                    profile=profile["name"])
    prob = _identity_problem(profile, raw.get("email"))
    if prob == "not_logged_in" and not (ident.get("expect_email") or ident.get("expect_email_domain")):
        prob = None   # no expectation configured: any state is accepted (local trial use)
    if prob:
        return fail("identity", prob, _login_fix(profile, node, a), email=raw.get("email") or "-",
                    profile=profile["name"])
    if resources:
        pj = lambda k: cfg.policy(k, profile)
        if raw.get("mem_mb") is not None and raw["mem_mb"] < pj("node.min_mem_mb"):
            return fail("resources", "ram", t("nodes.fix.ram"), mem=raw["mem_mb"], min=pj("node.min_mem_mb"))
        min_disk = (node or {}).get("min_disk_gb", pj("node.min_disk_gb"))
        if raw.get("disk_gb") is not None and raw["disk_gb"] < min_disk:
            return fail("resources", "disk", t("nodes.fix.disk", node=a or "localhost"),
                        disk=raw["disk_gb"], min=min_disk)
        if raw.get("load_pct") is not None and raw["load_pct"] > pj("node.max_load_pct"):
            return fail("resources", "load", t("nodes.fix.load"), load=raw["load_pct"],
                        max=pj("node.max_load_pct"))
    for name, status in res["compute"].items():
        if status != "ok":
            c = cfg.compute.get(name, {})
            hint = c.get("fix_hint") or t("nodes.fix.compute", name=name)
            fix = f"ssh -t {a} {shlex.quote(hint)}" if a else hint
            return fail("compute", "compute", fix, name=name, status=status)
    res.update(ok=True, kind="ok")
    return res


# ── probes ──────────────────────────────────────────────────────────────────
def probe(cfg, node, profile, requires=()):
    """Probe a configured node for a profile. `node` = node dict (cfg.node(name)) or its name."""
    node = cfg.node(node) if isinstance(node, str) else node
    profile = cfg.profile(profile) if isinstance(profile, str) else profile
    if node.get("runner") == "kindling":
        from . import kindling
        return kindling.probe(cfg, node, profile, requires)
    args = _probe_args(cfg, profile, node, requires)
    py = node.get("python", "python3")
    inner = f"env DELEGA_PROBE={shlex.quote(json.dumps(args))} {shlex.quote(py)} -"
    r = remote.ssh(node, f"bash -lc {shlex.quote(inner)}", input=PROBE_PY, timeout=90)
    raw = None
    if r.returncode == 0:
        try:
            raw = json.loads(r.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            raw = None
    res = evaluate(cfg, profile, node, raw, requires)
    res["node"] = node["name"]
    if raw is None and r.returncode == 0:
        res["reason"] = t("nodes.reason.bad_output", node=remote.alias(node))
    return res


def local_probe(cfg, profile, requires=()):
    """Same shape for this machine. Identity and compute gate it; local resources are reported only."""
    profile = cfg.profile(profile) if isinstance(profile, str) else profile
    args = _probe_args(cfg, profile, None, requires)
    env = {**os.environ, "DELEGA_PROBE": json.dumps(args)}
    for k in [k for k in env if k.startswith(("CLAUDE_", "ANTHROPIC_")) and k != "CLAUDE_CONFIG_DIR"]:
        del env[k]
    try:
        r = subprocess.run([sys.executable, "-c", PROBE_PY], capture_output=True, text=True, env=env,
                           timeout=90, stdin=subprocess.DEVNULL)
        raw = json.loads(r.stdout.strip().splitlines()[-1])
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
        raw = None
    res = evaluate(cfg, profile, None, raw, requires, resources=False, remote_node=False)
    res["node"] = "local"
    return res


def claude_version(launcher, config_dir=None):
    """Version string of the local launcher (or None)."""
    env = dict(os.environ)
    if config_dir:
        env["CLAUDE_CONFIG_DIR"] = _config.expand(config_dir)
    try:
        r = subprocess.run(list(launcher) + ["--version"], capture_output=True, text=True, timeout=20, env=env,
                           stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(r"\d+\.\d+(\.\d+)?", r.stdout)
    return m.group(0) if m else None


def same_minor(a, b):
    """MAJOR.MINOR equality between two version strings."""
    mm = lambda v: ".".join((v or "").split(".")[:2])
    return bool(a) and bool(b) and mm(a) == mm(b)


# ── selection ───────────────────────────────────────────────────────────────
class SelectLock:
    """mkdir-based lock serialising node selection so parallel launches do not pick the same node."""

    def __init__(self, cfg, wait_s=60):
        self.path = os.path.join(cfg.state_dir, ".select.lock")
        self.wait_s = wait_s

    def __enter__(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        end = time.time() + self.wait_s
        while True:
            try:
                os.mkdir(self.path)
                return self
            except FileExistsError:
                try:
                    if time.time() - os.path.getmtime(self.path) > 120:
                        os.rmdir(self.path)
                        continue
                except OSError:
                    pass
                if time.time() > end:
                    raise remote.RemoteError("node selection lock busy")
                time.sleep(0.2)

    def __exit__(self, *exc):
        try:
            os.rmdir(self.path)
        except OSError:
            pass


def select(cfg, profile, requires, live_counts, on_choice=None, say=print):
    """Pick the node with most free RAM (minus reserve_mb_per_job per live job), within its max_jobs,
    profile list and compute requirements. Returns a node name or "local".
    live_counts: dict {node: n} or a callable returning it (called under the lock).
    on_choice(name) runs under the lock, so the caller can record the choice before others select."""
    profile = cfg.profile(profile) if isinstance(profile, str) else profile
    reserve = cfg.policy("node.reserve_mb_per_job", profile)
    min_mem = cfg.policy("node.min_mem_mb", profile)
    best, best_score = None, -1
    with SelectLock(cfg):
        live = live_counts() if callable(live_counts) else live_counts
        for node in cfg.nodes_for(profile):
            name = node["name"]
            n = live.get(name, 0)
            mx = node.get("max_jobs")
            r = probe(cfg, node, profile, requires)
            say(t("nodes.line", node=name, status=_status(r), live=n, max=mx if mx else "-"))
            if not r["ok"]:
                continue
            if mx and n >= mx:
                continue
            score = (r.get("mem_mb") or 0) - n * reserve
            if score >= min_mem and score > best_score:
                best, best_score = name, score
        choice = best or "local"
        if on_choice:
            on_choice(choice)
    return choice


def _status(r):
    if r.get("ok"):
        return t("nodes.ok", mem=r.get("mem_mb"), disk=r.get("disk_gb"), load=r.get("load_pct"),
                 containers=r.get("containers") if r.get("containers") is not None else "-")
    return t("nodes.no", reason=r.get("reason"), fix=r.get("fix"))


# ── CLI ─────────────────────────────────────────────────────────────────────
def main(argv):
    ap = argparse.ArgumentParser(prog="delega nodes", description="State of the configured nodes.")
    ap.add_argument("--profile", help="profile whose nodes and identity are checked (default: configured default)")
    ap.add_argument("--requires", action="append", default=[], metavar="COMPUTE",
                    help="also require this compute provider to be valid (repeatable)")
    ap.add_argument("--json", action="store_true", help="print the raw probe results as JSON")
    args = ap.parse_args(argv)
    try:
        cfg = _config.load()
        profile = cfg.profile(args.profile)
        results = {"local": local_probe(cfg, profile, args.requires)}
        for node in cfg.nodes_for(profile):
            results[node["name"]] = probe(cfg, node, profile, args.requires)
    except _config.ConfigError as e:
        print(f"delega: {e}", file=sys.stderr)
        return 3
    if args.json:
        print(json.dumps(results, indent=2))
        return 0
    from . import jobs
    live = jobs.live_counts(cfg)
    for name, r in results.items():
        mx = cfg.nodes.get(name, {}).get("max_jobs")
        print(t("nodes.line", node=name, status=_status(r), live=live.get(name, 0), max=mx if mx else "-"))
    return 0

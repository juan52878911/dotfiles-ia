"""Configuration: one TOML file with general settings, a default policy, profiles
(an account plus its policy), nodes, external compute providers and projects.

Lookup order for every policy value (highest first):
  1. environment variable DELEGA_<KEY> (dots → underscores, upper case), or the
     legacy ORQ_* name listed in LEGACY_ENV
  2. profile override  [profiles.<p>.policy] ...
  3. project override  [projects."<owner/repo>".policy] ...   (when a project matches)
  4. global            [policy] ...
  5. DEFAULTS below

Config path: $DELEGA_CONFIG, else ~/.config/delega/config.toml. Without a file the
built-in defaults apply and a single implicit profile "default" runs `claude` with
no identity check (fine for trying the tool, refused for remote nodes).
"""
import copy
import os
import re
import subprocess

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    tomllib = None

DEFAULTS = {
    "general": {
        "locale": "en",
        "owner_name": "the user",
        "state_dir": "~/.local/state/delega",
        "default_profile": "default",
    },
    "policy": {
        "job": {"max_min": 120, "max_turns": 800, "max_usd": 15.0, "grace_s": 300},
        "stall_s": 600,
        "answer_wait_s": 900,
        "resume_max_min": 60,
        "max_local_jobs": 3,
        "expected_hours": 2,
        "node": {"max_load_pct": 80, "min_disk_gb": 20, "min_mem_mb": 1200, "reserve_mb_per_job": 1500},
        "permission_mode": {"default": "auto", "deny": ["bypassPermissions"]},
        "budget": {"day_usd": 150.0},
        "quota": {"window_5h_pct": 70, "weekly_pct": 90},
        "lint": {"require": ["done", "cap", "report"]},
        "lease": {"renew_s": 30, "stale_s": 120},
    },
}

# Legacy environment names (ORQ_*) from the original Spanish scripts.
LEGACY_ENV = {
    "ORQ_MAX_MIN": "job.max_min", "ORQ_MAX_TURNOS": "job.max_turns", "ORQ_MAX_USD": "job.max_usd",
    "ORQ_GRACIA_S": "job.grace_s", "ORQ_ATASCO_S": "stall_s", "ORQ_ESPERA_S": "answer_wait_s",
    "ORQ_RESUME_MAX_MIN": "resume_max_min", "ORQ_MAX_LOCAL": "max_local_jobs",
    "ORQ_HORAS_TRABAJO": "expected_hours", "ORQ_MAX_CARGA": "node.max_load_pct",
    "ORQ_MIN_DISCO_GB": "node.min_disk_gb", "ORQ_RESERVA_MB": "node.reserve_mb_per_job",
    "ORQ_PRESUPUESTO_DIA": "budget.day_usd",
}


class ConfigError(Exception):
    pass


def expand(p):
    return os.path.expanduser(os.path.expandvars(p)) if isinstance(p, str) else p


def _merge(a, b):
    """Deep merge b into a copy of a."""
    out = copy.deepcopy(a)
    for k, v in (b or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else copy.deepcopy(v)
    return out


def _dig(d, dotted, default=None):
    for part in dotted.split("."):
        if not isinstance(d, dict) or part not in d:
            return default
        d = d[part]
    return d


def _coerce(raw, like):
    if isinstance(like, bool):
        return raw.lower() in ("1", "true", "yes", "si", "sí")
    if isinstance(like, int):
        return int(float(raw))
    if isinstance(like, float):
        return float(raw)
    if isinstance(like, list):
        return [x.strip() for x in raw.split(",") if x.strip()]
    return raw


def config_path():
    return expand(os.environ.get("DELEGA_CONFIG", "~/.config/delega/config.toml"))


class Config:
    def __init__(self, data, path=None):
        self.path = path
        self.raw = data
        self.general = _merge(DEFAULTS["general"], data.get("general"))
        self.policy_base = _merge(DEFAULTS["policy"], data.get("policy"))
        self.profiles = data.get("profiles") or {}
        if not self.profiles:
            self.profiles = {"default": {"owner": "self", "launcher": ["claude"]}}
        self.nodes = data.get("nodes") or {}
        self.compute = data.get("compute") or {}
        self.projects = data.get("projects") or {}
        self.validate()

    # ── validation ──────────────────────────────────────────────────────────
    def validate(self):
        for name, p in self.profiles.items():
            if p.get("owner") != "self":
                raise ConfigError(
                    f"profile '{name}': set owner = \"self\". delega only runs accounts that are "
                    "assigned to you and used only by you (see README, Terms of use).")
            for key in ("failover", "rotate"):
                if p.get(key):
                    raise ConfigError(f"profile '{name}': '{key}' is not supported: jobs never move between accounts")
            for n in p.get("nodes", []):
                if n not in self.nodes:
                    raise ConfigError(f"profile '{name}' lists unknown node '{n}'")
            for c in p.get("compute", []):
                if c not in self.compute:
                    raise ConfigError(f"profile '{name}' lists unknown compute '{c}'")
        for name, n in self.nodes.items():
            if not n.get("ssh"):
                raise ConfigError(f"node '{name}': missing ssh = \"<alias in ~/.ssh/config>\"")

    # ── general ─────────────────────────────────────────────────────────────
    @property
    def locale(self):
        return os.environ.get("DELEGA_LOCALE") or self.general["locale"]

    @property
    def state_dir(self):
        return expand(os.environ.get("DELEGA_STATE_DIR") or self.general["state_dir"])

    @property
    def owner_name(self):
        return self.general["owner_name"]

    # ── profiles and projects ───────────────────────────────────────────────
    def profile(self, name=None):
        name = name or os.environ.get("DELEGA_PROFILE") or self.general["default_profile"]
        if name not in self.profiles:
            if name == "default" and len(self.profiles) == 1:
                name = next(iter(self.profiles))
            else:
                raise ConfigError(f"unknown profile '{name}' (known: {', '.join(self.profiles)})")
        p = copy.deepcopy(self.profiles[name])
        p["name"] = name
        p.setdefault("launcher", ["claude"])
        p.setdefault("model", "sonnet")
        p.setdefault("nodes", [])
        p.setdefault("compute", [])
        p.setdefault("forbidden_engines", [])
        return p

    def project_for(self, repo_dir):
        """Project key ("owner/repo") whose name matches the repo's origin remote, or None."""
        url = _git(repo_dir, "remote", "get-url", "origin")
        if not url:
            return None
        for key in self.projects:
            if re.search(r"[:/]" + re.escape(key) + r"(\.git)?$", url.strip(), re.I):
                return key
        return None

    def profile_for(self, repo_dir):
        """First profile whose repo_match matches repo_dir; else the default profile."""
        for name, p in self.profiles.items():
            m = p.get("repo_match")
            if not m:
                continue
            if "git_remote" in m:
                url = _git(repo_dir, "remote", "get-url", "origin") or ""
                if re.search(m["git_remote"], url):
                    return name
            if "command" in m:
                try:
                    out = subprocess.run(m["command"], cwd=repo_dir, capture_output=True, text=True,
                                         timeout=15).stdout.strip()
                except (OSError, subprocess.TimeoutExpired):
                    out = ""
                if re.search(m.get("pattern", "."), out):
                    return name
        return self.general["default_profile"] if self.general["default_profile"] in self.profiles \
            else next(iter(self.profiles))

    # ── policy ──────────────────────────────────────────────────────────────
    def policy(self, key, profile=None, project=None, default=None):
        """Effective policy value. key is dotted relative to [policy] (e.g. "job.max_usd")."""
        env = "DELEGA_" + key.replace(".", "_").upper()
        base = _dig(self.policy_base, key, default)
        raw = os.environ.get(env)
        if raw is None:
            for legacy, k in LEGACY_ENV.items():
                if k == key and legacy in os.environ:
                    raw = os.environ[legacy]
        if raw is not None:
            return _coerce(raw, base if base is not None else raw)
        if profile is not None:
            pname = profile["name"] if isinstance(profile, dict) else profile
            v = _dig(self.profiles.get(pname, {}).get("policy", {}), key)
            if v is not None:
                return v
        if project:
            v = _dig(self.projects.get(project, {}).get("policy", {}), key)
            if v is not None:
                return v
        return base

    # ── nodes ───────────────────────────────────────────────────────────────
    def node(self, name):
        if name not in self.nodes:
            raise ConfigError(f"unknown node '{name}' (known: {', '.join(self.nodes) or 'none'})")
        n = copy.deepcopy(self.nodes[name])
        n["name"] = name
        n.setdefault("workdir", "delega")       # relative to the node's $HOME
        n.setdefault("python", "python3")
        n.setdefault("runner", "host")          # host | docker
        n.setdefault("profiles", list(self.profiles))
        return n

    def nodes_for(self, profile):
        """Nodes this profile may use, in preference order."""
        names = profile.get("nodes") or []
        return [self.node(n) for n in names if profile["name"] in self.node(n)["profiles"]]


def _git(repo_dir, *args):
    try:
        r = subprocess.run(["git", "-C", repo_dir, *args], capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def load(path=None):
    path = path or config_path()
    data = {}
    if os.path.exists(path):
        if tomllib is None:
            raise ConfigError("delega needs Python 3.11+ to read TOML config")
        with open(path, "rb") as f:
            data = tomllib.load(f)
    return Config(data, path if os.path.exists(path) else None)

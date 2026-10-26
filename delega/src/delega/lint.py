"""lint: deterministic brief checks, run by `delega run` before anything is spent. No model, no network.

Two layers:
- sections(text): the brief has a done-when, a cap and a report (policy lint.require).
- check(text, ...): three rules about gaps that stopped workers in practice:
  branch   a branch the brief names must match the project's/profile's branch_pattern (default: its
           branch_prefixes), because the repo's hooks reject anything else at commit time.
  live     a run against an AI provider (policy lint.live_keywords) needs --requires or the profile's
           env_file path in the brief, otherwise the worker lands on a node without credentials.
  text     "original/previous brief" not attached, an absolute time with no rule for when it has passed,
           and a script named without a path.
Each rule's level comes from policy lint.branch / lint.live / lint.text: "error" (refuse), "warn" or "off".
"""
import os
import re

I = re.I

SECTION_PATTERNS = {   # English and Spanish wording; the same checks whatever the locale
    "done": r"done when|definition of done|acceptance|success criteri|terminad|criterio|hecho cuando|aceptaci",
    "cap": r"\bcap\b|\bbudget\b|at most|no more than|\bmax(imum)?\b|\btope\b|m[aá]ximo|presupuesto|no m[aá]s de",
    "report": r"report|informe",
}
LEVELS = {"branch": "error", "live": "warn", "text": "warn"}
LIVE_KEYWORDS = [r"\bvertex\b", r"\bbedrock\b", r"\bopenai\b", r"\bgemini\b", r"\banthropic api\b",
                 r"\ben vivo\b", r"\bcorridas? vivas?\b", r"\bsonda viva\b", r"\bmini-corrida viva\b",
                 r"\blive (run|probe|call)s?\b"]


def sections(text, require=None):
    """Sections the brief lacks: a subset of ["done", "cap", "report"] (headings in English or Spanish)."""
    require = require or ["done", "cap", "report"]
    return [k for k in require if not re.search(SECTION_PATTERNS[k], text, I)]


# ── rule 1: branch names ─────────────────────────────────────────────────────
BASE_BRANCHES = {"main", "master", "develop", "development", "staging", "head", "trunk"}
_TOKEN = r"`?([A-Za-z0-9][\w.\-]*(?:/[\w.\-]+)*)`?"
# "Rama: x", "Tu rama `x`", "Rama nueva: x", "new branch x": the worker's own branch, any shape.
BRANCH_OWN = re.compile(r"\b(?:(?:tu|nueva|propia|de trabajo)\s+rama|rama\s+(?:nueva|propia|de trabajo)|"
                        r"(?:your|new|work(?:ing)?)\s+branch|rama|branch)\s*(?::|=)\s*" + _TOKEN + "|"
                        r"\b(?:tu|nueva|propia)\s+rama\s+" + _TOKEN + "|"
                        r"\brama\s+(?:nueva|propia)\s+" + _TOKEN + "|"
                        r"\b(?:your|new)\s+branch\s+" + _TOKEN + "|"
                        r"git\s+(?:checkout\s+-b|switch\s+-c|branch)\s+" + _TOKEN + "|--branch[ =]" + _TOKEN, I)
# "rama `fix/x`" in prose: only tokens with a slash count (bare words there are usually base branches).
BRANCH_PROSE = re.compile(r"\b(?:rama|branch)\s+`?([A-Za-z0-9][\w.\-]*/[\w.\-/]*[\w])`?", I)


def branch_pattern(profile=None, project=None):
    """Regex a worker branch must match: branch_pattern of the project, else of the profile, else one
    built from the project's/profile's branch_prefixes. None when nothing is configured."""
    for src in (project or {}, profile or {}):
        if src.get("branch_pattern"):
            return src["branch_pattern"]
    for src in (project or {}, profile or {}):
        if src.get("branch_prefixes"):
            return "^(?:" + "|".join(re.escape(p) for p in src["branch_prefixes"]) + ")"
    return None


def named_branches(text):
    """Branches the brief assigns to the worker, in order of appearance."""
    out = []
    plain = text.replace("**", "").replace("__", "")
    for m in BRANCH_OWN.finditer(plain):
        b = next(g for g in m.groups() if g)
        if "/" in b or "`" + b in m.group(0):   # a bare prose word after "rama" is not a branch name
            out.append(b)
    for m in BRANCH_PROSE.finditer(plain):
        out.append(m.group(1))
    seen = []
    for b in out:
        b = b.strip("`.,;:")
        if b and b.lower() not in BASE_BRANCHES and not b.startswith(("origin/", "refs/")) and b not in seen:
            seen.append(b)
    return seen


def check_branch(text, pattern):
    if not pattern:
        return []
    bad = [b for b in named_branches(text) if not re.search(pattern, b)]
    return [("branch", {"branches": ", ".join(bad), "pattern": pattern})] if bad else []


# ── rule 2: live runs against an AI provider ────────────────────────────────
_NEG = r"\b(?:sin|nada de|cero|no gastes|ning[uú]n[ao]?|no|without|never|nunca)\s+(?:\w+\s+){0,2}?(?:%s)"


def wants_live(text, keywords=None):
    words = keywords or LIVE_KEYWORDS
    alt = "|".join(words)
    rest = re.sub(_NEG % alt, " ", text, flags=I)
    return any(re.search(w, rest, I) for w in words)


def check_live(text, requires=None, env_file=None, keywords=None):
    if requires or not wants_live(text, keywords):
        return []
    if env_file and (env_file in text or os.path.expanduser(env_file) in text):
        return []
    return [("live", {"env_file": env_file or "-"})]


# ── rule 3: text that points at things the worker does not have ────────────
EXTERNAL_REF = re.compile(r"\bbrief (?:original|anterior|previo)\b|\b(?:encargo|tarea) anterior\b|"
                          r"\b(?:original|previous|earlier) brief\b|\bcomo (?:te|se) dijo\b", I)
DEADLINE = re.compile(r"\b(?:antes de las|hasta las|a las|plazo|deadline|before|by|until)\b[^.\n]{0,40}?"
                      r"\b\d{1,2}:\d{2}\b", I)
DEADLINE_RULE = re.compile(r"\bsi (?:el plazo |la hora )?(?:ya )?(?:ha )?(?:vencid|pas[oó]|venci[oó])|"
                           r"\bif (?:it|the deadline|that time) (?:has )?(?:already )?passed|already passed", I)
SCRIPT = re.compile(r"(?<![\w/.~$-])([\w-]+\.(?:sh|py|mjs|mts|ps1|bash))\b")
RUN_VERB = re.compile(r"\b(?:ejecuta\w*|corre|correr|lanza\w*|llamadas? a|llama a|usa|usar|con|run|execute|invoke|"
                      r"python3?|bash|sh|node|tsx|npx)\W{1,3}(?:\S+\s+){0,2}?$", I)


def check_text(text):
    out = []
    m = EXTERNAL_REF.search(text)
    if m:
        out.append(("ref", {"what": m.group(0)}))
    if DEADLINE.search(text) and not DEADLINE_RULE.search(text):
        out.append(("deadline", {}))
    bare = sorted({m.group(1) for m in SCRIPT.finditer(text)       # scripts the brief asks to run
                   if RUN_VERB.search(text[max(0, m.start() - 40):m.start()])
                   and not re.search(r"[/~]" + re.escape(m.group(1)), text)})
    if bare:
        out.append(("script", {"scripts": ", ".join(bare)}))
    return out


# ── all rules ────────────────────────────────────────────────────────────────
def check(text, levels=None, pattern=None, requires=None, env_file=None, live_keywords=None):
    """[(level, code, params)] with level "error" or "warn"; code is branch | live | ref | deadline | script."""
    lv = dict(LEVELS, **{k: v for k, v in (levels or {}).items() if v})
    found = []
    if lv["branch"] != "off":
        found += [(lv["branch"],) + f for f in check_branch(text, pattern)]
    if lv["live"] != "off":
        found += [(lv["live"],) + f for f in check_live(text, requires, env_file, live_keywords)]
    if lv["text"] != "off":
        found += [(lv["text"],) + f for f in check_text(text)]
    return found


def check_for(text, cfg, profile, project, requires=None):
    """check() with levels, pattern, env_file and keywords taken from the effective config."""
    pol = lambda k: cfg.policy(k, profile, project)
    proj = cfg.projects.get(project) if project else None
    return check(text, levels={k: pol("lint." + k) for k in LEVELS}, pattern=branch_pattern(profile, proj),
                 requires=requires, env_file=profile.get("env_file"), live_keywords=pol("lint.live_keywords"))

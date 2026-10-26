"""doctor: pre-flight checklist per profile. ✓ ok, ! warning, ✗ failed (with the exact fix)."""
import os
import re
import subprocess
import sys
import tempfile
import time

from . import config, i18n, util

OK, WARN, FAIL = "✓", "!", "✗"


class Report:
    def __init__(self):
        self.lines, self.failed = [], 0

    def add(self, level, text, fix=None):
        self.lines.append((level, text, fix))
        self.failed += level == FAIL

    def show(self):
        for level, text, fix in self.lines:
            print(f"  {level} {text}")
            if fix and level != OK:
                print(f"      {i18n.t('doctor.fix')}: {fix}")


def local_pyz_sha():
    with tempfile.TemporaryDirectory() as tmp:
        return util.build_pyz(os.path.join(tmp, "delega.pyz"))


def local_claude_version(prof):
    try:
        r = subprocess.run([*prof.get("launcher", ["claude"]), "--version"], capture_output=True, text=True, timeout=20)
        m = re.search(r"\d+\.\d+\.\d+", r.stdout)
        return m.group(0) if m else None
    except (OSError, subprocess.SubprocessError):
        return None


def probe_local(cfg, prof):
    from . import nodes
    return nodes.local_probe(cfg, prof, tuple(prof.get("compute", [])))


def identity_check(rep, where, prof, email):
    want_email = (prof.get("identity") or {}).get("expect_email")
    want_domain = (prof.get("identity") or {}).get("expect_email_domain")
    if not (want_email or want_domain):
        rep.add(WARN, i18n.t("doctor.identity_unchecked", where=where),
                f'[profiles.{prof["name"]}] identity.expect_email_domain = "example.com"')
        return
    if not email:
        rep.add(FAIL, i18n.t("doctor.identity_unknown", where=where),
                i18n.t("doctor.fix_login", cfgdir=prof.get("config_dir", "~/.claude")))
        return
    good = (email.lower() == want_email.lower()) if want_email else email.lower().endswith("@" + want_domain.lower().lstrip("@"))
    rep.add(OK if good else FAIL, i18n.t("doctor.identity", where=where, email=email),
            None if good else i18n.t("doctor.fix_login", cfgdir=prof.get("config_dir", "~/.claude")))


def probe_checks(rep, cfg, prof, where, res, local=False):
    min_disk = float(cfg.policy("node.min_disk_gb", prof))
    max_load = float(cfg.policy("node.max_load_pct", prof))
    identity_check(rep, where, prof, res.get("email"))
    for name in prof.get("compute", []):
        status = (res.get("compute") or {}).get(name, "unknown")
        hint = cfg.compute.get(name, {}).get("fix_hint")
        rep.add(OK if status == "ok" else FAIL, i18n.t("doctor.compute", where=where, name=name, status=status), hint)
    disk, load = res.get("disk_gb"), res.get("load_pct")
    if disk is not None:
        rep.add(OK if disk >= min_disk else FAIL, i18n.t("doctor.disk", where=where, disk=f"{disk:.0f}", min=f"{min_disk:.0f}"),
                i18n.t("doctor.fix_disk"))
    if load is not None:
        rep.add(OK if load <= max_load else WARN, i18n.t("doctor.load", where=where, load=f"{load:.0f}", max=f"{max_load:.0f}"),
                i18n.t("doctor.fix_load"))


def check_profile(cfg, prof, repo):
    name = prof["name"]
    rep = Report()
    print(i18n.t("doctor.profile", profile=name))
    # local machine
    try:
        res = probe_local(cfg, prof)
        if not res.get("ok", True):
            rep.add(FAIL, i18n.t("doctor.probe_failed", where="local", reason=res.get("reason", "?")), res.get("fix"))
        probe_checks(rep, cfg, prof, "local", res, local=True)
    except (AttributeError, NotImplementedError, OSError) as e:
        res = {}
        rep.add(FAIL, i18n.t("doctor.probe_failed", where="local", reason=e), "delega nodes")
    local_claude = res.get("claude_version") or local_claude_version(prof)
    rep.add(OK if local_claude else FAIL, i18n.t("doctor.claude", where="local", version=local_claude or "?"),
            None if local_claude else i18n.t("doctor.fix_claude"))
    # nodes
    sha = local_pyz_sha()
    for node in cfg.nodes_for(prof):
        where = node["name"]
        try:
            from . import nodes
            r = nodes.probe(cfg, node, prof, tuple(prof.get("compute", [])))
        except (AttributeError, NotImplementedError, OSError) as e:
            rep.add(FAIL, i18n.t("doctor.probe_failed", where=where, reason=e), f"ssh {node['ssh']} true")
            continue
        if not r.get("ok"):
            rep.add(FAIL, i18n.t("doctor.probe_failed", where=where, reason=r.get("reason", "?")), r.get("fix"))
            continue
        probe_checks(rep, cfg, prof, where, r)
        cv = r.get("claude_version")
        rep.add(OK if cv and cv == local_claude else WARN,
                i18n.t("doctor.claude_drift", where=where, remote=cv or "?", local=local_claude or "?"),
                None if cv == local_claude else f"ssh {node['ssh']} 'claude install {local_claude or 'stable'}'")
        ps = r.get("pyz_sha")
        rep.add(OK if ps == sha else WARN, i18n.t("doctor.pyz_drift", where=where, remote=ps or "none", local=sha),
                None if ps == sha else i18n.t("doctor.fix_pyz"))
    # quota and spend
    from . import quota, spend
    try:
        cap = quota.capacity(cfg, name, probe=False, record_history=False)
        if not cap["has_data"]:
            rep.add(WARN, i18n.t("doctor.quota_nodata"), "delega run <repo> <brief>  # one job first, then: delega quota")
        else:
            rep.add(OK if cap["new"] > 0 else WARN,
                    i18n.t("doctor.quota", u5=f"{cap['u5']:.0f}", u7=f"{cap['u7']:.0f}", new=cap["new"]),
                    None if cap["new"] > 0 else i18n.t("doctor.fix_quota"))
    except config.ConfigError as e:
        rep.add(FAIL, str(e))
    project = cfg.project_for(repo) if repo else None
    budget = float(cfg.policy("budget.day_usd", prof, project))
    spent = spend.total(cfg, time.strftime("%Y%m%d"), profile=name)
    level = FAIL if spent >= budget else WARN if spent >= 0.8 * budget else OK
    rep.add(level, i18n.t("doctor.spend", spent=f"{spent:.2f}", budget=f"{budget:.2f}"),
            None if level == OK else i18n.t("doctor.fix_budget"))
    # forbidden terms, bundle
    terms = prof.get("forbidden_terms_file")
    ok = bool(terms) and os.path.isfile(config.expand(terms))
    rep.add(OK if ok else FAIL, i18n.t("doctor.terms_ok" if ok else "doctor.terms_missing"),
            None if ok else f'create the file and set [profiles.{name}] forbidden_terms_file = "~/.config/delega/{name}/forbidden.txt"')
    if prof.get("nodes"):
        bundle = config.expand(prof.get("node_bundle") or "")
        ok = bool(bundle) and os.path.isdir(bundle)
        rep.add(OK if ok else FAIL, i18n.t("doctor.bundle_ok" if ok else "doctor.bundle_missing"),
                None if ok else f'mkdir -p ~/.config/delega/{name}/bundle && set [profiles.{name}] node_bundle')
    rep.show()
    return rep.failed


def main(argv):
    profile, repo, batch, a = None, None, None, list(argv)
    while a:
        x = a.pop(0)
        if x == "--profile" and a:
            profile = a.pop(0)
        elif x == "--batch" and a and a[0].isdigit():
            batch = int(a.pop(0))
        elif x in ("-h", "--help"):
            print(i18n.t("doctor.usage"))
            return 0
        else:
            repo = x
    try:
        cfg = config.load()
    except config.ConfigError as e:
        print(f"  {FAIL} {i18n.t('doctor.config_invalid')}: {e}")
        print(f"      {i18n.t('doctor.fix')}: cp config.example.toml ~/.config/delega/config.toml")
        return 1
    if cfg.path is None:
        print(f"  {WARN} {i18n.t('doctor.no_config', path=config.config_path())}")
    else:
        print(f"  {OK} {i18n.t('doctor.config_ok', path=cfg.path)}")
    try:
        if profile:
            names = [cfg.profile(profile)["name"]]
        elif repo:
            names = [cfg.profile_for(repo)]
        else:
            names = list(cfg.profiles)
        if batch is not None:
            from . import preflight
            return max(preflight.batch(cfg, cfg.profile(n), batch, repo) for n in names)
        failed = sum(check_profile(cfg, cfg.profile(n), repo) for n in names)
    except config.ConfigError as e:
        print(e, file=sys.stderr)
        return 2
    print(i18n.t("doctor.summary_bad", n=failed) if failed else i18n.t("doctor.summary_ok"))
    return 1 if failed else 0

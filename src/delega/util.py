"""Small helpers shared by every module (stdlib only: this code also runs on nodes)."""
import io
import json
import os
import time
import zipfile

PKG_DIR = os.path.dirname(os.path.abspath(__file__))


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def append(path, line):
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, d):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False)
    os.replace(tmp, path)


def short(text, n=110):
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1] + "…"


# Jobs created by the original Spanish scripts use these meta keys and channel file
# names; readers accept both so old jobs keep showing up in ps/spend/quota.
LEGACY_META = {"HOST": "NODE", "RJOB": "CHANNEL", "RAMA": "BRANCH", "DUENO": "OWNER", "FIN": "END"}
LEGACY_FILES = {
    "inbox.jsonl": "buzon.jsonl", "notices.jsonl": "avisos.jsonl", "progress.log": "progreso.log",
    "state.json": "estado.json", "events.jsonl": "eventos.jsonl", "result.json": "salida.json",
    "results.jsonl": "resultados.jsonl", "summary.json": "resumen.json", "notify": "avisar",
}


def read_meta(job_dir):
    """meta is KEY=VALUE lines; later lines win. Legacy keys are mapped to the new ones."""
    out = {}
    try:
        with open(os.path.join(job_dir, "meta"), encoding="utf-8") as f:
            for line in f:
                if "=" in line:
                    k, v = line.rstrip("\n").split("=", 1)
                    out[LEGACY_META.get(k, k)] = v
    except OSError:
        pass
    return out


def channel_file(job_dir, name):
    """Path of a channel file, falling back to its legacy name if only that exists."""
    p = os.path.join(job_dir, name)
    legacy = LEGACY_FILES.get(name)
    if legacy and not os.path.exists(p) and os.path.exists(os.path.join(job_dir, legacy)):
        return os.path.join(job_dir, legacy)
    return p


def add_meta(job_dir, **kv):
    with open(os.path.join(job_dir, "meta"), "a", encoding="utf-8") as f:
        for k, v in kv.items():
            f.write(f"{k}={'' if v is None else v}\n")


def build_pyz(dest):
    """Zip this package into a runnable .pyz (python3 delega.pyz <command> …). Returns its sha256[:12]."""
    import hashlib
    buf = io.BytesIO()
    def put(z, name, data):  # fixed timestamp: same sources → same hash (used to detect drift)
        z.writestr(zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0)), data, zipfile.ZIP_DEFLATED)

    with zipfile.ZipFile(buf, "w") as z:
        put(z, "__main__.py", "from delega.__main__ import main\nimport sys\nsys.exit(main(sys.argv[1:]))\n")
        for root, dirs, files in sorted(os.walk(PKG_DIR)):
            dirs.sort()
            if "__pycache__" in root:
                continue
            for fn in sorted(files):
                if fn.endswith((".py", ".json", ".md")):
                    full = os.path.join(root, fn)
                    with open(full, "rb") as f:
                        put(z, os.path.join("delega", os.path.relpath(full, PKG_DIR)), f.read())
    data = buf.getvalue()
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "wb") as f:
        f.write(data)
    return hashlib.sha256(data).hexdigest()[:12]


def common_options(ap, names):
    """Add --<name> options to a parser and return a parent for its subcommands, so they are accepted
    before or after the subcommand (`delega golden --node n build` = `delega golden build --node n`)."""
    import argparse
    parent = argparse.ArgumentParser(add_help=False)
    for n in names:
        ap.add_argument("--" + n)
        parent.add_argument("--" + n, default=argparse.SUPPRESS)
    return parent

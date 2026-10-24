"""Command-line entry point. Each subcommand lives in its own module and exposes
main(argv) -> int. Imports are lazy so the broker can run on a node with nothing else."""
import importlib
import os
import sys

COMMANDS = {
    # command      module         one-line help
    "run":       ("launch",    "launch a job: delega run <repo> <brief> [options]"),
    "broker":    ("broker",    "internal: runs on the node and mediates the worker channel"),
    "nodes":     ("nodes",     "state of the configured nodes"),
    "doctor":    ("doctor",    "pre-flight checklist: identity, credentials, disk, load, versions, spend"),
    "quota":     ("quota",     "how many workers fit before the account's usage windows fill up"),
    "spend":     ("spend",     "list-price cost per day, profile, project and orchestrator"),
    "ps":        ("jobs",      "what each of your workers is doing"),
    "say":       ("jobs",      "send a message to a running worker"),
    "watch":     ("jobs",      "one line per notice/alert/result/end, for a Monitor"),
    "adopt":     ("jobs",      "re-attach to a remote job after the orchestrator died"),
    "handoff":   ("handoff",   "draft a fresh brief to continue a job without --resume"),
    "scan":      ("scan",      "look for forbidden terms in a branch, a brief or a text"),
    "queue":     ("queue",     "queue a brief with a priority / list the queue"),
    "tick":      ("queue",     "reconcile leases and launch queued jobs when there is room"),
    "integrate": ("integrate", "dry-run merge of a job branch and run the project's verify commands"),
    "sync":      ("sync",      "push the profile's rules bundle (CLAUDE.md, hooks, agents) to nodes"),
    "audit":     ("audit",     "provenance of a job or branch from the append-only audit log"),
    "metrics":   ("metrics",   "tokens, time and node resources of finished jobs"),
    "version":   (None,        "print the version"),
}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print("usage: delega <command> [args]\n")
        for name, (_, h) in COMMANDS.items():
            print(f"  {name:<10} {h}")
        return 0
    cmd, rest = argv[0], argv[1:]
    if cmd == "version":
        from . import __version__
        print(__version__)
        return 0
    if cmd not in COMMANDS:
        print(f"delega: unknown command '{cmd}' (delega --help)", file=sys.stderr)
        return 2
    # The configured locale applies to every module (i18n reads DELEGA_LOCALE).
    if "DELEGA_LOCALE" not in os.environ:
        try:
            from . import config
            os.environ["DELEGA_LOCALE"] = config.load().locale
        except Exception:
            pass
    mod = importlib.import_module(f"delega.{COMMANDS[cmd][0]}")
    fn = getattr(mod, "main_" + cmd, None) or mod.main
    return fn(rest) or 0


if __name__ == "__main__":
    sys.exit(main())

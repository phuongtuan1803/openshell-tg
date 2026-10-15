"""Command line: up, status, list, pull, destroy (and the hidden serve)."""

from __future__ import annotations

import argparse
import getpass
import hashlib
import logging
import sys
import time
from pathlib import Path

import yaml

from . import __version__, host, osh, project
from . import messages as m
from . import sandbox as sb
from .project import ConfigError, Project
from .store import Store, state_dir

log = logging.getLogger("openshell_tg")
PACKAGE_PROFILES = Path(__file__).parent / "profiles"


# terminal helpers

def say(text: str) -> None:
    print(text, flush=True)


def ask(question: str, default: bool = False) -> bool:
    hint = "[Y/n]" if default else "[y/N]"
    answer = input(f"{question} {hint} ").strip().lower()
    return default if not answer else answer in ("y", "yes", "c", "co", "có")


def choose(question: str, options: list[str]) -> str:
    while True:
        answer = input(f"{question} [{' / '.join(options)}] ").strip().lower() or options[0]
        if answer in options:
            return answer
        say(m.t("pick_one", options=", ".join(options)))


def prompt_secret(question: str) -> str:
    while True:
        value = getpass.getpass(f"{question} ").strip()
        if value:
            return value


def setup_logging(service: bool = False) -> None:
    handler = logging.FileHandler(state_dir() / "openshell-tg.log")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    if service:
        root.addHandler(logging.StreamHandler(sys.stderr))


# up

def first_run_questions(folder: Path) -> Path:
    say(m.t("new_project", folder=folder))
    agent = choose(m.t("ask_agent"), list(project.AGENTS))
    auth = "api_key" if agent not in project.SUBSCRIPTION_AGENTS else choose(m.t("ask_auth"), list(project.AUTH_MODES))
    github = ask(m.t("ask_github"), default=False)
    path = project.create(folder, agent, auth, github)
    say(m.t("files_written", file=path.name))
    return path


def _profile_files(proj: Project) -> dict[str, Path]:
    """Profile id -> file. Project files override the ones shipped with openshell-tg."""
    files = {}
    for path in [*sorted(PACKAGE_PROFILES.glob("*.yaml")), *proj.profile_files()]:
        data = yaml.safe_load(path.read_text()) or {}
        if data.get("id"):
            files[data["id"]] = path
    return files


def ensure_providers(proj: Project) -> list[str]:
    needed = project.required_providers(proj)
    if not needed:
        return []
    files = _profile_files(proj)
    unknown = [n for n in needed if n not in files]
    if unknown:
        raise ConfigError(m.t("unknown_provider", names=", ".join(unknown)))
    existing = osh.providers()
    for name in needed:
        osh.import_profile(str(files[name]), name)
        if name in existing:
            continue
        profile = yaml.safe_load(files[name].read_text())
        credentials = {}
        for cred in profile.get("credentials") or []:
            if not cred.get("required"):
                continue
            var = (cred.get("env_vars") or [cred["name"].upper()])[0]
            credentials[var] = prompt_secret(m.t("ask_credential", provider=profile.get("display_name", name),
                                                 what=cred.get("description", var)))
        osh.create_provider(name, name, credentials)
        say(m.t("provider_saved", provider=profile.get("display_name", name)))
    return needed


def _policy_hash(proj: Project) -> str:
    return hashlib.sha256(proj.policy.read_bytes()).hexdigest() if proj.policy else ""


def _confirm_destroy(store: Store, name: str, folder: Path) -> bool:
    unsaved = sb.unsaved_commits(name, folder)
    if unsaved:
        say(m.t("unsaved_commits", count=unsaved))
    return ask(m.t("confirm_destroy_cli", sandbox=name), default=False)


def cmd_up(args) -> int:
    folder = project.find(Path.cwd()) or Path.cwd()
    if not (folder / project.CONFIG_NAME).exists():
        first_run_questions(folder)
    proj = project.load(folder)

    host.check_kvm()
    host.ensure_openshell(ask)
    host.ensure_gateway(sb.IMAGE, say)
    host.setup_telegram(prompt_secret, choose, say)
    osh.enable_proposals()
    providers = ensure_providers(proj)

    store = Store()
    known = store.sandbox(proj.sandbox)
    if known and sb.state(proj.sandbox) is not None:
        changed = []
        if known.agent != proj.agent:
            changed.append("agent")
        if store.get(f"policy:{proj.sandbox}") not in (None, _policy_hash(proj)):
            changed.append(proj.policy.name)
        if changed:
            say(m.t("config_changed", what=", ".join(changed)))
            if ask(m.t("ask_rebuild"), default=False) and _confirm_destroy(store, proj.sandbox, proj.folder):
                sb.destroy(proj.sandbox)

    created = sb.ensure_running(proj, providers, say)
    store.save_sandbox(proj.sandbox, str(proj.folder), proj.agent, proj.idle_stop)
    if created or store.get(f"policy:{proj.sandbox}") is None:
        store.put(f"policy:{proj.sandbox}", _policy_hash(proj))

    say(m.t("attaching", agent=proj.agent))
    if created and proj.auth == "subscription":
        say(m.t("login_hint"))
    code = sb.attach(proj)
    store.touch(proj.sandbox)
    say(m.t("detached"))
    return 0 if code in (0, 130) else code


# other commands

def _project_here() -> Project:
    folder = project.find(Path.cwd())
    if not folder:
        raise ConfigError(m.t("no_config_here"))
    return project.load(folder)


def cmd_status(args) -> int:
    proj = _project_here()
    store = Store()
    phase = sb.state(proj.sandbox)
    stats = store.stats()
    say(m.t("cli_status", sandbox=proj.sandbox, state=m.t(f"phase_{_phase(phase)}"),
            service=m.t("ok") if host.service_active() else m.t("down"),
            pending=len([r for r in store.open_requests() if r.sandbox == proj.sandbox]),
            total=stats["total"], median=m.duration(stats["median"]), p90=m.duration(stats["p90"])))
    return 0


def cmd_list(args) -> int:
    store = Store()
    boxes = store.sandboxes()
    if not boxes:
        say(m.t("list_empty"))
        return 0
    for box in boxes:
        phase = sb.state(box.name)
        missing = "" if Path(box.folder).is_dir() else m.t("folder_missing")
        say(f"{box.name:<36} {m.t(f'phase_{_phase(phase)}'):<12} {m.ago(box.last_used):<14} {box.folder} {missing}")
    return 0


def cmd_pull(args) -> int:
    proj = _project_here()
    branches = sb.pull(proj)
    if not branches:
        say(m.t("pull_nothing"))
    for branch, count in branches:
        say(m.t("pull_branch", branch=branch, count=count))
    Store().touch(proj.sandbox)
    return 0


def cmd_destroy(args) -> int:
    proj = _project_here()
    store = Store()
    if sb.state(proj.sandbox) is None:
        store.forget_sandbox(proj.sandbox)
        say(m.t("no_sandbox"))
        return 0
    if not _confirm_destroy(store, proj.sandbox, proj.folder):
        say(m.t("cancelled"))
        return 1
    sb.destroy(proj.sandbox)
    store.forget_sandbox(proj.sandbox)
    say(m.t("deleted", sandbox=proj.sandbox))
    return 0


def cmd_serve(args) -> int:
    from .bot import serve
    serve()
    return 0


def _phase(phase: str | None) -> str:
    return {"Ready": "running", "Stopped": "stopped", None: "missing"}.get(phase, "busy")


def main(argv: list[str] | None = None) -> int:
    m.set_language(host.load_config().get("language") or m.default_language())
    parser = argparse.ArgumentParser(prog="openshell-tg", description=m.t("cli_description"))
    parser.add_argument("--version", action="version", version=f"openshell-tg {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, func in (("up", cmd_up), ("status", cmd_status), ("list", cmd_list),
                       ("pull", cmd_pull), ("destroy", cmd_destroy)):
        sub.add_parser(name, help=m.t(f"help_{name}")).set_defaults(func=func)
    sub.add_parser("serve").set_defaults(func=cmd_serve)
    args = parser.parse_args(argv)
    setup_logging(service=args.command == "serve")
    try:
        return args.func(args)
    except (ConfigError, host.SetupError, sb.SandboxError) as exc:
        say(str(exc))
    except osh.OpenShellError as exc:
        log.error("%s", exc)
        say(m.t("openshell_failed", log=state_dir() / "openshell-tg.log"))
    except FileNotFoundError as exc:
        log.error("%s", exc)
        say(m.t("missing_program", program=exc.filename))
    except KeyboardInterrupt:
        say("")
        return 130
    return 1


if __name__ == "__main__":
    sys.exit(main())

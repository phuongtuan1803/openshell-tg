"""One sandbox per project folder: create or start, copy code in, attach, pull, destroy."""

from __future__ import annotations

import fnmatch
import os
import json
import logging
import shlex
import subprocess
import tempfile
import time
from pathlib import Path

from . import __version__, host, osh
from . import messages as m
from .project import Project

log = logging.getLogger(__name__)

IMAGE = os.environ.get("OPENSHELL_TG_IMAGE", f"ghcr.io/phuongtuan1803/openshell-tg-agent:{__version__}")
WORKDIR = "/sandbox/workspace"
IN_BUNDLE = f"{WORKDIR}/.otg-in.bundle"
OUT_BUNDLE = f"{WORKDIR}/.git/otg-out.bundle"
RESTORED = f"{WORKDIR}/.git/otg-restored"

# Files that must never be copied into a sandbox. Matched against basenames.
SECRET_FILES = (".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "id_rsa*", "id_dsa*",
                "id_ecdsa*", "id_ed25519*", ".netrc", ".pgpass", "*.kdbx", "credentials.json",
                "credentials", ".npmrc", ".pypirc", ".git-credentials", "*.tfstate", "*.tfstate.backup")
SECRET_ALLOW = (".env.example", ".env.sample", ".env.template", "*.pub")


class SandboxError(Exception):
    """User-facing failure; the message is already plain language."""


def state(name: str) -> str | None:
    """OpenShell phase of the sandbox, or None if it does not exist."""
    info = osh.sandbox(name)
    return info.get("phase") if info else None


# code in

def _git(folder: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(folder), *args], capture_output=True, text=True, check=check)


def is_git(folder: Path) -> bool:
    proc = _git(folder, "rev-parse", "--show-toplevel", check=False)
    return proc.returncode == 0 and Path(proc.stdout.strip()).resolve() == folder.resolve()


def files_to_copy(folder: Path) -> list[str]:
    """Same selection as OpenShell's upload: git ls-files, or every file outside git."""
    if is_git(folder):
        out = _git(folder, "ls-files", "-co", "--exclude-standard", "-z").stdout
        return [p for p in out.split("\0") if p]
    return [str(p.relative_to(folder)) for p in folder.rglob("*") if p.is_file()]


def _is_secret(rel: str) -> bool:
    base = os.path.basename(rel)
    if any(fnmatch.fnmatch(base, a) for a in SECRET_ALLOW):
        return False
    return any(fnmatch.fnmatch(base, s) for s in SECRET_FILES)


def secret_files(folder: Path) -> list[str]:
    return sorted(rel for rel in files_to_copy(folder) if _is_secret(rel))


def secret_files_in_history(folder: Path) -> list[str]:
    """Secret-looking files that were committed on some branch at some point."""
    if not is_git(folder):
        return []
    out = _git(folder, "log", "--branches", "--name-only", "--format=", check=False).stdout
    return sorted({rel for rel in out.splitlines() if rel and _is_secret(rel)})


def _restore_history_script(sha: str | None, name: str, email: str) -> str:
    q = shlex.quote
    lines = [
        "set -e",
        f"cd {WORKDIR}",
        "git init -q",
        f"git config user.name {q(name)}",
        f"git config user.email {q(email)}",
    ]
    if sha:
        lines += [
            f"git fetch -q --update-head-ok {IN_BUNDLE} '+refs/heads/*:refs/heads/*' HEAD",
            "git symbolic-ref HEAD refs/heads/work",
            # Points 'work' at the host commit; index follows, files stay as uploaded.
            f"git reset -q {q(sha)}",
            f"rm -f {IN_BUNDLE}",
        ]
    else:
        lines += [
            "git symbolic-ref HEAD refs/heads/work",
            "git add -A",
            "git commit -q --allow-empty -m 'Copy of the project folder'",
        ]
    lines.append(f"touch {RESTORED}")
    return "\n".join(lines)


def create(project: Project, providers: list[str], say, confirm) -> None:
    secrets = secret_files(project.folder)
    if secrets:
        raise SandboxError(m.t("secret_files", files=", ".join(secrets[:5])))
    in_history = secret_files_in_history(project.folder)
    if in_history and not confirm(m.t("secret_history", files=", ".join(in_history[:5]))):
        raise SandboxError(m.t("cancelled"))

    say(m.t("up_creating"))
    log.info("create: sandbox=%s image=%s providers=%s", project.sandbox,
             IMAGE, ",".join(providers) or "-")
    try:
        osh.create(
            project.sandbox,
            IMAGE,
            str(project.policy) if project.policy else None,
            providers,
            str(project.folder),
            WORKDIR,
        )
    except osh.OpenShellError:
        # A failed create can leave a half-built sandbox that blocks the next try.
        # Keep its state in the log first: it says why startup did not finish.
        try:
            log.error("sandbox state after the failed create: %s", json.dumps(osh.sandbox(project.sandbox))[:6000])
        except (osh.OpenShellError, TypeError, ValueError):
            pass
        try:
            log.error("host state after the failed create:\n%s", host.diagnostics())
        except Exception:  # diagnostics must never hide the real error
            pass
        try:
            osh.delete(project.sandbox)
        except osh.OpenShellError:
            pass
        raise

    log.info("create: sandbox=%s created, copying the project", project.sandbox)
    _restore(project)


def _restore(project: Project) -> None:
    """Put the folder's git history into the sandbox and write the marker. Safe to repeat."""
    sha = None
    if is_git(project.folder):
        head = _git(project.folder, "rev-parse", "--verify", "-q", "HEAD", check=False)
        sha = head.stdout.strip() or None
    if sha:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = Path(tmp) / "in.bundle"
            # Branches only: no stash, no remote-tracking refs.
            _git(project.folder, "bundle", "create", "-q", str(bundle), "--branches", "HEAD")
            osh.upload(project.sandbox, str(bundle), IN_BUNDLE)
    name = _git(project.folder, "config", "user.name", check=False).stdout.strip() or "openshell-tg"
    email = _git(project.folder, "config", "user.email", check=False).stdout.strip() or "agent@openshell-tg.invalid"
    osh.exec_(project.sandbox, ["sh", "-c", _restore_history_script(sha, f"{name} (agent)", email)])


BUSY = ("Provisioning", "Starting", "Stopping", "Deleting", "Unspecified", "Unknown")


def _wait_while_busy(name: str, say, timeout: float = 600) -> str | None:
    phase = state(name)
    if phase in BUSY:
        say(m.t("up_waiting", phase=phase))
    deadline = time.time() + timeout
    while phase in BUSY and time.time() < deadline:
        time.sleep(3)
        phase = state(name)
    return phase


def ensure_running(project: Project, providers: list[str], say, confirm) -> bool:
    """Create or start the sandbox. Returns True if it was just created."""
    phase = _wait_while_busy(project.sandbox, say)
    if phase is None:
        create(project, providers, say, confirm)
        return True
    if phase != "Ready":
        say(m.t("up_starting"))
        log.info("start: sandbox=%s phase=%s", project.sandbox, phase)
        try:
            osh.start(project.sandbox)
        except osh.OpenShellError:
            if phase != "Error":
                raise
            if not confirm(m.t("ask_recreate_broken")):
                raise SandboxError(m.t("cancelled")) from None
            osh.delete(project.sandbox)
            create(project, providers, say, confirm)
            return True
    if osh.exec_(project.sandbox, ["test", "-f", RESTORED], timeout=30, check=False).returncode != 0:
        # The last create stopped before the history step; the files are already there.
        say(m.t("up_finishing"))
        _restore(project)
        if osh.exec_(project.sandbox, ["test", "-f", RESTORED], timeout=30, check=False).returncode != 0:
            raise SandboxError(m.t("copy_incomplete"))
    _check_terminal(project)
    missing = [p for p in providers if p not in osh.attached_providers(project.sandbox)]
    for provider in missing:
        osh.attach_provider(project.sandbox, provider)
    return False


def _check_terminal(project: Project) -> None:
    """The agent runs in tmux, which needs to open a terminal. Check before attaching."""
    probe = osh.exec_(project.sandbox, ["sh", "-c", "tmux new-session -d -s probe 'sleep 5' && tmux kill-session -t probe"],
                      timeout=30, check=False)
    if probe.returncode != 0:
        raise SandboxError(m.t("no_terminal", file=project.policy or "openshell/policy.yaml"))


def attach(project: Project) -> int:
    return osh.attach(project.sandbox, ["otg-agent", project.agent], WORKDIR)


def agent_running(name: str) -> bool:
    try:
        return osh.exec_(name, ["tmux", "has-session", "-t", "agent"], timeout=30, check=False).returncode == 0
    except (osh.OpenShellError, subprocess.TimeoutExpired):
        return False


IDLE_SECONDS = 15


def agent_idle_seconds(name: str) -> int | None:
    """Seconds since the agent's terminal last changed; None when the agent is not running."""
    script = 'a=$(tmux display-message -p -t agent "#{window_activity}") || exit 3; echo $(( $(date +%s) - a ))'
    try:
        done = osh.exec_(name, ["sh", "-c", script], timeout=30, check=False)
        return int(done.stdout.strip()) if done.returncode == 0 else None
    except (osh.OpenShellError, subprocess.TimeoutExpired, ValueError):
        return None


def screen(name: str, lines: int = 15) -> str | None:
    """The last lines shown in the agent's terminal; None when the agent is not running."""
    try:
        done = osh.exec_(name, ["tmux", "capture-pane", "-p", "-t", "agent"], timeout=30, check=False)
    except (osh.OpenShellError, subprocess.TimeoutExpired):
        return None
    if done.returncode != 0:
        return None
    rows = [r.rstrip() for r in done.stdout.splitlines()]
    while rows and not rows[-1]:
        rows.pop()
    return "\n".join(rows[-lines:])[-3000:]


def tell_agent(name: str, text: str) -> bool:
    """Type one line into the agent's terminal and press Enter. Literal text, so no key names are interpreted."""
    line = " ".join("".join(c if c.isprintable() else " " for c in text).split())[:600]
    try:
        osh.exec_(name, ["tmux", "send-keys", "-t", "agent", "-l", line], timeout=30)
        osh.exec_(name, ["tmux", "send-keys", "-t", "agent", "Enter"], timeout=30)
        return True
    except (osh.OpenShellError, subprocess.TimeoutExpired):
        return False


# code out

def pull(project: Project) -> list[tuple[str, int]]:
    """Copy the sandbox's branches to the host as otg/<branch>. Returns (branch, new commits)."""
    if state(project.sandbox) != "Ready":
        raise SandboxError(m.t("pull_not_running"))
    if not is_git(project.folder):
        raise SandboxError(m.t("pull_not_git"))
    current = _git(project.folder, "branch", "--show-current", check=False).stdout.strip()
    if current.startswith("otg/"):
        raise SandboxError(m.t("pull_on_otg_branch", branch=current))
    made = osh.exec_(project.sandbox, ["sh", "-c",
                     f"cd {WORKDIR} && git for-each-ref --format=x refs/heads | grep -q x "
                     f"&& git bundle create -q {OUT_BUNDLE} --branches"], check=False)
    if made.returncode != 0:
        return []
    with tempfile.TemporaryDirectory() as tmp:
        osh.download(project.sandbox, OUT_BUNDLE, tmp + "/")
        bundle = Path(tmp) / "otg-out.bundle"
        heads = _git(project.folder, "bundle", "list-heads", str(bundle)).stdout.split()
        _git(project.folder, "fetch", "-q", str(bundle), "+refs/heads/*:refs/heads/otg/*")
    branches = sorted(ref.removeprefix("refs/heads/") for ref in heads if ref.startswith("refs/heads/"))
    result = []
    for branch in branches:
        count = _git(project.folder, "rev-list", "--count", f"HEAD..otg/{branch}", check=False).stdout.strip()
        result.append((f"otg/{branch}", int(count or 0)))
    return result


def unsaved_commits(name: str, folder: Path) -> int | None:
    """Commits in the sandbox that are neither on the host nor pushed. None if it cannot be checked."""
    if state(name) != "Ready":
        return None
    script = (f"cd {WORKDIR} && for c in $(git for-each-ref --format='%(objectname)' refs/heads); do "
              "[ -z \"$(git branch -r --contains $c 2>/dev/null)\" ] && echo $c; done; true")
    out = osh.exec_(name, ["sh", "-c", script], check=False).stdout.split()
    if not folder.is_dir() or not is_git(folder):
        return len(out)
    return sum(1 for sha in out if _git(folder, "cat-file", "-e", f"{sha}^{{commit}}", check=False).returncode)


def destroy(name: str) -> None:
    log.info("destroy: sandbox=%s", name)
    osh.delete(name)

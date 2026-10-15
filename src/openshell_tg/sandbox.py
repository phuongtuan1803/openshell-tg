"""One sandbox per project folder: create or start, copy code in, attach, pull, destroy."""

from __future__ import annotations

import fnmatch
import os
import shlex
import subprocess
import tempfile
import time
from pathlib import Path

from . import __version__, osh
from . import messages as m
from .project import Project

IMAGE = os.environ.get("OPENSHELL_TG_IMAGE", f"ghcr.io/phuongtuan1803/openshell-tg-agent:{__version__}")
WORKDIR = "/sandbox/workspace"
OTG_DIR = "/sandbox/.otg"
FOLDER_LABEL = "openshell-tg/folder"

# Files that must never be copied into a sandbox. Matched against basenames.
SECRET_FILES = (".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "id_rsa*", "id_dsa*",
                "id_ecdsa*", "id_ed25519*", ".netrc", ".pgpass", "*.kdbx", "credentials.json")
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


def secret_files(folder: Path) -> list[str]:
    found = []
    for rel in files_to_copy(folder):
        base = os.path.basename(rel)
        if any(fnmatch.fnmatch(base, a) for a in SECRET_ALLOW):
            continue
        if any(fnmatch.fnmatch(base, s) for s in SECRET_FILES):
            found.append(rel)
    return sorted(found)


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
            f"git fetch -q --update-head-ok {OTG_DIR}/in.bundle '+refs/heads/*:refs/heads/*'",
            "git symbolic-ref HEAD refs/heads/work",
            # Points 'work' at the host commit; index follows, files stay as uploaded.
            f"git reset -q {q(sha)}",
            f"rm -f {OTG_DIR}/in.bundle",
        ]
    else:
        lines += [
            "git symbolic-ref HEAD refs/heads/work",
            "git add -A",
            "git commit -q --allow-empty -m 'Copy of the project folder'",
        ]
    return "\n".join(lines)


def create(project: Project, providers: list[str], say) -> None:
    secrets = secret_files(project.folder)
    if secrets:
        raise SandboxError(m.t("secret_files", files=", ".join(secrets[:5])))

    say(m.t("up_creating"))
    osh.create(
        project.sandbox,
        IMAGE,
        str(project.policy) if project.policy else None,
        providers,
        (str(project.folder), WORKDIR),
        {FOLDER_LABEL: str(project.folder)},
    )

    sha = None
    if is_git(project.folder):
        head = _git(project.folder, "rev-parse", "--verify", "-q", "HEAD", check=False)
        sha = head.stdout.strip() or None
    if sha:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = Path(tmp) / "in.bundle"
            _git(project.folder, "bundle", "create", "-q", str(bundle), "--all")
            osh.exec_(project.sandbox, ["mkdir", "-p", OTG_DIR])
            osh.upload(project.sandbox, str(bundle), f"{OTG_DIR}/in.bundle")
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


def ensure_running(project: Project, providers: list[str], say) -> bool:
    """Create or start the sandbox. Returns True if it was just created."""
    phase = _wait_while_busy(project.sandbox, say)
    if phase is None:
        create(project, providers, say)
        return True
    if phase != "Ready":
        say(m.t("up_starting"))
        osh.start(project.sandbox)
    return False


def attach(project: Project) -> int:
    return osh.attach(project.sandbox, ["otg-agent", project.agent], WORKDIR)


def agent_running(name: str) -> bool:
    try:
        return osh.exec_(name, ["tmux", "has-session", "-t", "agent"], timeout=30, check=False).returncode == 0
    except (osh.OpenShellError, subprocess.TimeoutExpired):
        return False


# code out

def pull(project: Project) -> list[tuple[str, int]]:
    """Copy the sandbox's branches to the host as otg/<branch>. Returns (branch, new commits)."""
    if state(project.sandbox) != "Ready":
        raise SandboxError(m.t("pull_not_running"))
    if not is_git(project.folder):
        raise SandboxError(m.t("pull_not_git"))
    osh.exec_(project.sandbox, ["sh", "-c",
              f"mkdir -p {OTG_DIR} && cd {WORKDIR} && git bundle create -q {OTG_DIR}/out.bundle --branches"])
    with tempfile.TemporaryDirectory() as tmp:
        osh.download(project.sandbox, f"{OTG_DIR}/out.bundle", tmp + "/")
        bundle = Path(tmp) / "out.bundle"
        heads = _git(project.folder, "bundle", "list-heads", str(bundle)).stdout.split()
        _git(project.folder, "fetch", "-q", str(bundle), "+refs/heads/*:refs/heads/otg/*")
    branches = sorted(ref.removeprefix("refs/heads/") for ref in heads if ref.startswith("refs/heads/"))
    result = []
    for branch in branches:
        count = _git(project.folder, "rev-list", "--count", f"HEAD..otg/{branch}", check=False).stdout.strip()
        result.append((f"otg/{branch}", int(count or 0)))
    return result


def unsaved_commits(name: str, folder: Path) -> int:
    """Commits in the sandbox that are neither on the host nor pushed anywhere."""
    if state(name) != "Ready":
        return 0
    script = (f"cd {WORKDIR} && for c in $(git for-each-ref --format='%(objectname)' refs/heads); do "
              "[ -z \"$(git branch -r --contains $c 2>/dev/null)\" ] && echo $c; done; true")
    out = osh.exec_(name, ["sh", "-c", script], check=False).stdout.split()
    if not folder.is_dir() or not is_git(folder):
        return len(out)
    return sum(1 for sha in out if _git(folder, "cat-file", "-e", f"{sha}^{{commit}}", check=False).returncode)


def destroy(name: str) -> None:
    osh.delete(name)

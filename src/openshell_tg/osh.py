"""The only module that runs the `openshell` CLI.

Every call uses public commands of OpenShell v0.1.4. Commands with JSON
output are parsed as JSON; `rule get` prints text only, so it is parsed by
`parse_rules`, which fails closed on anything unexpected.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

BINARY = os.environ.get("OPENSHELL_TG_OPENSHELL", "openshell")


class OpenShellError(Exception):
    def __init__(self, args: list[str], code: int, stderr: str):
        self.args_ = args
        self.code = code
        self.stderr = stderr.strip()
        super().__init__(f"openshell {' '.join(args[:3])} failed ({code}): {self.stderr[-500:]}")

    @property
    def not_found(self) -> bool:
        return "NotFound" in self.stderr or "not found" in self.stderr.lower()


def _env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ, NO_COLOR="1", OPENSHELL_COLOR="never")
    if extra:
        env.update(extra)
    return env


def run(args: list[str], *, env: dict[str, str] | None = None, timeout: float = 600,
        check: bool = True) -> subprocess.CompletedProcess:
    log.debug("openshell %s", " ".join(args))
    proc = subprocess.run(
        [BINARY, *args], capture_output=True, text=True, env=_env(env), timeout=timeout,
        stdin=subprocess.DEVNULL,
    )
    if check and proc.returncode != 0:
        log.warning("openshell %s -> %s: %s", args[:3], proc.returncode, proc.stderr.strip()[-2000:])
        raise OpenShellError(args, proc.returncode, proc.stderr)
    return proc


def run_json(args: list[str], **kw) -> dict:
    out = run([*args, "-o", "json"], **kw).stdout
    return json.loads(out) if out.strip() else {}


def interactive(args: list[str]) -> int:
    """Run with the user's terminal attached. Returns the exit code."""
    return subprocess.call([BINARY, *args], env=_env())


def installed() -> bool:
    try:
        return run(["--version"], check=False, timeout=20).returncode == 0
    except FileNotFoundError:
        return False


def version() -> str:
    return run(["--version"], timeout=20).stdout.strip()


# gateway and settings

def status() -> dict:
    return run_json(["status"], timeout=30)


def compute_drivers() -> list[str]:
    info = run_json(["gateway", "info"], timeout=30)
    return [d.get("name", "") for d in info.get("compute_drivers", [])]


def enable_proposals() -> None:
    run(["settings", "set", "--global", "--key", "agent_policy_proposals_enabled",
         "--value", "true", "--yes"])


# profiles and providers

def import_profile(path: str, profile_id: str) -> None:
    """Import a provider profile, or update it when it already exists."""
    try:
        run(["profile", "import", "-f", path])
    except OpenShellError as exc:
        if "already exists" not in exc.stderr:
            raise
        run(["profile", "update", profile_id, "-f", path])


def providers() -> set[str]:
    return {p["name"] for p in run_json(["provider", "list"]).get("providers", [])}


def create_provider(name: str, profile: str, credentials: dict[str, str]) -> None:
    """Secrets travel in the environment; argv carries only variable names."""
    args = ["provider", "create", "--name", name, "--type", profile]
    for var in credentials:
        args += ["--credential", var]
    run(args, env=credentials)


# sandboxes

def sandbox(name: str) -> dict | None:
    try:
        return run_json(["sandbox", "get", name], timeout=60)
    except OpenShellError as exc:
        if exc.not_found:
            return None
        raise


def sandboxes() -> list[dict]:
    return run_json(["sandbox", "list"], timeout=60).get("sandboxes", [])


def create(name: str, image: str, policy: str | None, providers: list[str],
           upload: tuple[str, str], labels: dict[str, str]) -> dict:
    args = ["sandbox", "create", "--name", name, "--from", image, "--no-auto-providers",
            "--upload", f"{upload[0]}:{upload[1]}"]
    if policy:
        args += ["--policy", policy]
    for p in providers:
        args += ["--provider", p]
    for k, v in labels.items():
        args += ["--label", f"{k}={v}"]
    return run_json(args, timeout=1800)


def start(name: str) -> None:
    run(["sandbox", "start", name], timeout=900)


def stop(name: str) -> None:
    run(["sandbox", "stop", name], timeout=300)


def delete(name: str) -> None:
    run(["sandbox", "delete", name], timeout=300)


def exec_(name: str, command: list[str], workdir: str | None = None, timeout: int = 300,
          check: bool = True) -> subprocess.CompletedProcess:
    args = ["sandbox", "exec", name, "--no-tty", "--no-login-shell", "--timeout", str(timeout)]
    if workdir:
        args += ["--workdir", workdir]
    return run([*args, "--", *command], timeout=timeout + 30, check=check)


def attach(name: str, command: list[str], workdir: str) -> int:
    return interactive(["sandbox", "exec", name, "--tty", "--workdir", workdir, "--", *command])


def upload(name: str, local: str, dest: str) -> None:
    run(["sandbox", "upload", name, local, dest, "--no-git-ignore"], timeout=900)


def download(name: str, path: str, dest: str) -> None:
    run(["sandbox", "download", name, path, dest], timeout=900)


# rules

RULE_FIELDS = ("Chunk", "Status", "Rule", "Binary", "Confidence", "Rationale", "Security",
               "Prover", "Application", "Candidate", "Endpoints", "Binaries", "Hits")
_FIELD = re.compile(r"^  (" + "|".join(RULE_FIELDS) + r"):(?: (.*))?$")
_HEADER = re.compile(r"^Network Rules:\s+\(version (\d+), (\d+) chunks?\)")


@dataclass
class Chunk:
    id: str
    status: str = ""
    rule: str = ""
    binary: str = ""
    confidence: str = ""
    rationale: str = ""
    security: str = ""
    prover: str = ""
    application: str = ""
    endpoints: str = ""
    binaries: str = ""
    hits: str = ""
    trusted: bool = True
    _seen: list[str] = field(default_factory=list, repr=False)

    @property
    def risky(self) -> bool:
        return bool(self.security) or not self.trusted

    def summary(self) -> str:
        return f"{self.rule}|{self.endpoints}|{self.binary or self.binaries}"


def parse_rules(text: str) -> list[Chunk]:
    """Parse `openshell rule get` text output.

    Agent-written text (rationale) can contain newlines that imitate field
    lines. A chunk whose fields repeat or come out of the documented order is
    marked untrusted, and if the number of chunks differs from the header,
    every chunk is untrusted. Untrusted chunks are never approvable from
    Telegram.
    """
    if text.strip().startswith("No network rules"):
        return []
    chunks: list[Chunk] = []
    current: Chunk | None = None
    last_key: str | None = None
    expected: int | None = None
    for line in text.splitlines():
        header = _HEADER.match(line)
        if header:
            expected = int(header.group(2))
            continue
        match = _FIELD.match(line)
        if match:
            key, value = match.group(1), (match.group(2) or "").strip()
            if key == "Chunk":
                current = Chunk(id=value)
                chunks.append(current)
                last_key = key
                continue
            if current is None:
                continue
            order = [RULE_FIELDS.index(k) for k in current._seen]
            if key in current._seen or (order and RULE_FIELDS.index(key) < max(order)):
                current.trusted = False
            current._seen.append(key)
            attr = key.lower()
            if hasattr(current, attr) and attr not in ("id", "trusted"):
                setattr(current, attr, value)
            last_key = key
        elif current is not None and last_key in ("Rationale", "Security", "Prover") and line.strip():
            attr = last_key.lower()
            setattr(current, attr, (getattr(current, attr) + "\n" + line.strip()).strip())
    for chunk in chunks:
        if not chunk.endpoints or not chunk.id:
            chunk.trusted = False
    if expected is not None and expected != len(chunks):
        for chunk in chunks:
            chunk.trusted = False
    return chunks


def rules(name: str, status: str = "pending") -> list[Chunk]:
    return parse_rules(run(["rule", "get", name, "--status", status], timeout=60).stdout)


def approve(name: str, chunk_id: str) -> None:
    run(["rule", "approve", name, "--chunk-id", chunk_id], timeout=120)


def reject(name: str, chunk_id: str, reason: str) -> None:
    run(["rule", "reject", name, "--chunk-id", chunk_id, "--reason", reason], timeout=120)


def remove_rule(name: str, rule: str) -> None:
    run(["policy", "update", name, "--remove-rule", rule, "--wait"], timeout=180)

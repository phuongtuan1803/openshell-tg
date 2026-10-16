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
import tempfile
from dataclasses import dataclass, field

import yaml

log = logging.getLogger(__name__)

BINARY = os.environ.get("OPENSHELL_TG_OPENSHELL", "openshell")


_NOT_FOUND = re.compile(r"NotFound|requested entity was not found|(?:sandbox|provider|profile) not found")


class OpenShellError(Exception):
    def __init__(self, args: list[str], code: int, stderr: str):
        self.args_ = args
        self.code = code
        self.stderr = stderr.strip()
        super().__init__(f"openshell {' '.join(args[:3])} failed ({code}): {self.stderr[-500:]}")

    @property
    def not_found(self) -> bool:
        # tonic prints: code: 'Some requested entity was not found', message: "sandbox not found"
        return bool(_NOT_FOUND.search(self.stderr))


def _env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ, NO_COLOR="1", OPENSHELL_COLOR="never")
    if extra:
        env.update(extra)
    return env


def run(args: list[str], *, env: dict[str, str] | None = None, timeout: float = 600,
        check: bool = True, cwd: str | None = None) -> subprocess.CompletedProcess:
    """Run openshell. Every failure, including a timeout, raises OpenShellError."""
    log.debug("openshell %s", " ".join(args))
    try:
        proc = subprocess.run(
            [BINARY, *args], capture_output=True, text=True, env=_env(env), timeout=timeout,
            stdin=subprocess.DEVNULL, cwd=cwd,
        )
    except subprocess.TimeoutExpired:
        raise OpenShellError(args, 124, f"timed out after {timeout:.0f} s") from None
    if check and proc.returncode != 0:
        error = OpenShellError(args, proc.returncode, proc.stderr)
        # "Not found" is an expected answer for polling a sandbox that is gone.
        log.log(logging.DEBUG if error.not_found else logging.WARNING, "openshell %s -> %s: %s",
                args[:3], proc.returncode, proc.stderr.strip()[-2000:])
        raise error
    return proc


def run_json(args: list[str], **kw) -> dict:
    out = run([*args, "-o", "json"], **kw).stdout
    try:
        return json.loads(out) if out.strip() else {}
    except ValueError:
        raise OpenShellError(args, 0, f"unexpected output: {out[:200]}") from None


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
    """Import a provider profile, or update it when it already exists.

    An update must carry the stored profile's resource_version, so the current
    one is exported first and copied into a temporary copy of the file.
    """
    imported = run(["profile", "import", "-f", path], check=False)
    if imported.returncode == 0:
        return
    if "already exists" not in imported.stderr:
        raise OpenShellError(["profile", "import"], imported.returncode, imported.stderr)
    version = run_json(["profile", "export", profile_id], timeout=60).get("resource_version")
    if not version:
        raise OpenShellError(["profile", "export", profile_id], 0, "no resource_version in exported profile")
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    data["resource_version"] = version
    with tempfile.TemporaryDirectory() as tmp:
        updated = os.path.join(tmp, os.path.basename(path))
        with open(updated, "w", encoding="utf-8") as fh:
            yaml.safe_dump(data, fh, sort_keys=False)
        run(["profile", "update", profile_id, "-f", updated])


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


def create(name: str, image: str, policy: str | None, providers: list[str], folder: str, dest: str) -> dict:
    # Uploading "." from inside the folder puts its contents directly in dest;
    # a named directory would land in dest/<basename>.
    args = ["sandbox", "create", "--name", name, "--from", image, "--no-auto-providers",
            "--upload", f".:{dest}"]
    if policy:
        args += ["--policy", policy]
    for p in providers:
        args += ["--provider", p]
    return run_json(args, timeout=1800, cwd=folder)


def attached_providers(name: str) -> set[str]:
    return {p["name"] for p in run_json(["sandbox", "provider", "list", name]).get("providers", [])}


def attach_provider(name: str, provider: str) -> None:
    run(["sandbox", "provider", "attach", name, provider, "--wait"], timeout=120)


def start(name: str) -> None:
    run(["sandbox", "start", name], timeout=900)


def stop(name: str) -> None:
    run(["sandbox", "stop", name], timeout=300)


def delete(name: str) -> None:
    run(["sandbox", "delete", name], timeout=300)


def exec_(name: str, command: list[str], workdir: str | None = None, timeout: int = 300,
          check: bool = True) -> subprocess.CompletedProcess:
    args = ["sandbox", "exec", "-n", name, "--no-tty", "--no-login-shell", "--timeout", str(timeout)]
    if workdir:
        args += ["--workdir", workdir]
    return run([*args, "--", *command], timeout=timeout + 30, check=check)


def attach(name: str, command: list[str], workdir: str) -> int:
    return interactive(["sandbox", "exec", "-n", name, "--tty", "--timeout", "0", "--workdir", workdir, "--", *command])


def upload(name: str, local: str, dest: str) -> None:
    run(["sandbox", "upload", name, local, dest, "--no-git-ignore"], timeout=900)


def download(name: str, path: str, dest: str) -> None:
    run(["sandbox", "download", name, path, dest], timeout=900)


# rules

RULE_FIELDS = ("Chunk", "Status", "Rule", "Binary", "Confidence", "Rationale", "Security",
               "Prover", "Application", "Candidate", "Endpoints", "Binaries", "Hits")
_FIELD = re.compile(r"^  (" + "|".join(RULE_FIELDS) + r"):(?: (.*))?$")
_HEADER = re.compile(r"^Network Rules:\s+\(version (\d+), (\d+) chunks?\)\s*$")
CHUNK_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")


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

    @property
    def grants(self) -> str:
        """Programs the rule would allow: Binaries from the proposed rule, else the requester."""
        return self.binaries or self.binary

    def summary(self) -> str:
        return f"{self.rule}|{self.endpoints}|{self.grants}"

    def shown(self) -> dict:
        """The fields a card displays. Approval requires these to be unchanged."""
        return {"rule": self.rule, "endpoints": self.endpoints, "grants": self.grants,
                "binary": self.binary}


def parse_rules(text: str) -> list[Chunk]:
    """Parse `openshell rule get` text output, failing closed.

    The rationale is agent-written and printed raw, so it can contain line
    breaks and lines that imitate fields, chunks or the header. Therefore:
    the header is accepted only as the first non-empty line; any other
    unrecognised line, a repeated or out-of-order field, a missing Endpoints
    line or an unusual chunk id makes the chunk untrusted; and a chunk count
    that differs from the header makes every chunk untrusted. Untrusted
    chunks can never be approved from Telegram.
    """
    if text.strip().startswith("No network rules"):
        return []
    chunks: list[Chunk] = []
    current: Chunk | None = None
    expected: int | None = None
    poisoned = False
    lines = text.splitlines()
    first = next((i for i, line in enumerate(lines) if line.strip()), None)
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        header = _HEADER.match(line)
        if header:
            if i == first:
                expected = int(header.group(2))
            else:
                poisoned = True
            continue
        match = _FIELD.match(line)
        if not match:
            if current is not None:
                current.trusted = False
            else:
                poisoned = True
            continue
        key, value = match.group(1), (match.group(2) or "").strip()
        if key == "Chunk":
            current = Chunk(id=value, trusted=bool(CHUNK_ID.match(value)))
            chunks.append(current)
            continue
        if current is None:
            poisoned = True
            continue
        order = [RULE_FIELDS.index(k) for k in current._seen]
        if key in current._seen or (order and RULE_FIELDS.index(key) < max(order)):
            current.trusted = False
        current._seen.append(key)
        if key not in current._seen[:-1]:
            setattr(current, key.lower(), value)
    for chunk in chunks:
        if not chunk.endpoints:
            chunk.trusted = False
    if poisoned or expected is None or expected != len(chunks):
        for chunk in chunks:
            chunk.trusted = False
    return chunks


def rules(name: str, status: str = "pending") -> list[Chunk]:
    return parse_rules(run(["rule", "get", name, "--status", status], timeout=60).stdout)


def approve(name: str, chunk_id: str) -> None:
    run(["rule", "approve", name, "--chunk-id", chunk_id], timeout=120)


def reject(name: str, chunk_id: str, reason: str) -> None:
    run(["rule", "reject", name, "--chunk-id", chunk_id, "--reason", reason], timeout=120)


def network_policies(name: str) -> dict:
    """The sandbox's active network rules, keyed by rule name (from the policy itself)."""
    data = run_json(["policy", "get", name, "--full"], timeout=60)
    return (data.get("policy") or {}).get("network_policies") or {}


def remove_rule(name: str, rule: str) -> None:
    run(["policy", "update", name, "--remove-rule", rule, "--wait"], timeout=180)

"""The per-folder openshell-tg.yaml: read, validate, and create on first run.

openshell-tg owns only the keys below. Network, filesystem and credential
rules live in OpenShell's own policy and provider-profile YAML, which this
module never parses beyond checking that no secret was pasted into them.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from . import messages as m

CONFIG_NAME = "openshell-tg.yaml"
AGENTS = ("codex", "claude", "opencode")
AUTH_MODES = ("subscription", "api_key")
KEYS = {"name", "agent", "auth", "providers", "policy", "profiles", "idle_stop"}

# Provider profile an agent needs in api_key mode (profiles/ in this repo).
# Subscription mode needs none: the agent logs in inside the VM and its
# endpoints come from the policy template.
API_KEY_PROVIDERS = {"codex": "openai", "claude": "claude-code", "opencode": "openrouter"}
SUBSCRIPTION_AGENTS = ("codex", "claude")

SECRET_PATTERNS = [
    re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{40,}"),
    re.compile(r"sk-(ant-|proj-|or-)?[A-Za-z0-9_-]{20,}"),
    re.compile(r"xox[abpr]-[A-Za-z0-9-]{10,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b"),
]

DURATION = re.compile(r"^(\d+)\s*([mhd])$")


class ConfigError(Exception):
    """A problem the user can fix by editing a file. The message is user-facing."""


@dataclass
class Project:
    folder: Path
    name: str
    agent: str
    auth: str
    providers: list[str] = field(default_factory=list)
    policy: Path | None = None
    profiles: Path | None = None
    idle_stop: int = 24 * 3600

    @property
    def sandbox(self) -> str:
        """Stable sandbox name: readable prefix plus a hash of the folder path."""
        digest = hashlib.sha256(str(self.folder).encode()).hexdigest()[:6]
        slug = re.sub(r"[^a-z0-9-]+", "-", self.name.lower()).strip("-")[:30] or "project"
        return f"otg-{slug}-{digest}"

    def profile_files(self) -> list[Path]:
        if not self.profiles or not self.profiles.is_dir():
            return []
        return sorted(p for p in self.profiles.iterdir() if p.suffix in (".yaml", ".yml"))


def parse_duration(value: object) -> int:
    match = DURATION.match(str(value).strip())
    if not match:
        raise ConfigError(m.t("config_bad_duration", value=value))
    n, unit = int(match.group(1)), match.group(2)
    return n * {"m": 60, "h": 3600, "d": 86400}[unit]


def find(start: Path) -> Path | None:
    """The folder holding openshell-tg.yaml: start or its nearest parent."""
    for folder in (start, *start.parents):
        if (folder / CONFIG_NAME).is_file():
            return folder
    return None


def load(folder: Path) -> Project:
    path = folder / CONFIG_NAME
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(m.t("config_bad_yaml", file=CONFIG_NAME, error=exc)) from None
    if not isinstance(data, dict):
        raise ConfigError(m.t("config_bad_yaml", file=CONFIG_NAME, error="not a mapping"))
    unknown = set(data) - KEYS
    if unknown:
        raise ConfigError(m.t("config_unknown_keys", keys=", ".join(sorted(unknown))))

    agent = data.get("agent")
    if agent not in AGENTS:
        raise ConfigError(m.t("config_bad_choice", key="agent", value=agent, choices=" | ".join(AGENTS)))
    auth = data.get("auth", "subscription")
    if auth not in AUTH_MODES:
        raise ConfigError(m.t("config_bad_choice", key="auth", value=auth, choices=" | ".join(AUTH_MODES)))
    providers = data.get("providers") or []
    if not isinstance(providers, list) or not all(isinstance(p, str) for p in providers):
        raise ConfigError(m.t("config_bad_providers"))
    if auth == "subscription" and agent not in SUBSCRIPTION_AGENTS:
        raise ConfigError(m.t("config_no_subscription", agent=agent))

    project = Project(
        folder=folder.resolve(),
        name=str(data.get("name") or folder.resolve().name),
        agent=agent,
        auth=auth,
        providers=providers,
        policy=_path(folder, data.get("policy", "openshell/policy.yaml")),
        profiles=_path(folder, data.get("profiles", "openshell/profiles")),
        idle_stop=parse_duration(data.get("idle_stop", "24h")),
    )
    if project.policy and not project.policy.is_file():
        raise ConfigError(m.t("config_missing_file", file=project.policy))
    check_no_secrets([path, *([project.policy] if project.policy else []), *project.profile_files()])
    return project


def check_no_secrets(paths: list[Path]) -> None:
    for path in paths:
        for lineno, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
            if any(p.search(line) for p in SECRET_PATTERNS):
                raise ConfigError(m.t("config_secret", file=path.name, line=lineno))


def required_providers(project: Project) -> list[str]:
    """Providers from the file plus the one the agent needs, without duplicates."""
    needed = API_KEY_PROVIDERS[project.agent] if project.auth == "api_key" else None
    names = list(project.providers)
    if needed and needed not in names:
        names.insert(0, needed)
    return names


def create(folder: Path, agent: str, auth: str, github: bool) -> Path:
    """Write openshell-tg.yaml and a starting OpenShell policy. Returns the config path."""
    providers = ["github"] if github else []
    config = {
        "agent": agent,
        "auth": auth,
        "providers": providers,
        "policy": "openshell/policy.yaml",
        "idle_stop": "24h",
    }
    (folder / "openshell").mkdir(exist_ok=True)
    policy = folder / "openshell" / "policy.yaml"
    if not policy.exists():
        policy.write_text(_template(f"policy-{agent}.yaml" if auth == "subscription" else "policy.yaml"))
    path = folder / CONFIG_NAME
    path.write_text(
        "# openshell-tg settings. Network and file rules are in openshell/policy.yaml\n"
        "# (OpenShell's own format). Never put keys or tokens in these files.\n"
        + yaml.safe_dump(config, sort_keys=False)
    )
    return path


def _path(folder: Path, value: object) -> Path | None:
    if not value:
        return None
    path = Path(str(value))
    return path if path.is_absolute() else folder / path


def _template(name: str) -> str:
    return (Path(__file__).parent / "templates" / name).read_text()

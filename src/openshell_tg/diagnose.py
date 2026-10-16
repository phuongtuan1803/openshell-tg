"""Turn an OpenShell failure into a cause and a next step the user can act on."""

from __future__ import annotations

import re

from . import messages as m
from .osh import OpenShellError

# (cause key, pattern on the lower-cased error text). First match wins.
CAUSES = (
    ("image_denied", re.compile(r"authenticat\w+ (?:failure|failed)|requested access to the resource is denied|unauthorized.*regist")),
    ("image_missing", re.compile(r"manifest unknown|image .*not found|name unknown")),
    ("gateway_down", re.compile(r"connection refused|transport error|tcp connect error|error trying to connect|dns error")),
    ("provisioning", re.compile(r"provisioningtimedout|repair window expired")),
    ("timeout", re.compile(r"timed out")),
    ("disk_full", re.compile(r"no space left")),
    ("kvm", re.compile(r"/dev/kvm|kvm.*permission|permission denied.*kvm")),
    ("wrong_state", re.compile(r"not in a state required|must be stopped")),
    ("not_found", re.compile(r"requested entity was not found|notfound")),
)

_NOISE = re.compile(r"^[\s│├└┌─×]+")


def cause(exc: OpenShellError) -> str | None:
    text = exc.stderr.lower()
    if exc.code == 124:
        return "timeout"
    for key, pattern in CAUSES:
        if pattern.search(text):
            return key
    return None


def detail(exc: OpenShellError, lines: int = 4, width: int = 200) -> str:
    """The last few meaningful lines of the error, for people who maintain the setup."""
    rows = [_NOISE.sub("", line).strip() for line in exc.stderr.splitlines()]
    rows = [r for r in rows if r and r != "Error:"]
    return "\n".join(r[:width] for r in rows[-lines:])


def report(exc: OpenShellError, log_file) -> str:
    key = cause(exc)
    step = " ".join(exc.args_[:2])
    parts = [m.t("fail_title", step=step)]
    parts.append(m.t(f"cause_{key}") if key else m.t("cause_unknown"))
    shown = detail(exc)
    if shown:
        parts.append(m.t("fail_detail") + "\n" + "\n".join("  " + row for row in shown.splitlines()))
    parts.append(m.t("fail_log", log=log_file))
    return "\n".join(parts)

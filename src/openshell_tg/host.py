"""One-time machine setup: KVM, OpenShell, gateway with the MicroVM driver,
Telegram bot and the approval service. Every step skips what already exists."""

from __future__ import annotations

import getpass
import os
import pwd
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path

import yaml

from . import messages as m
from . import osh
from .telegram import Telegram, TelegramError

OPENSHELL_VERSION = "v0.1.4"
INSTALLER = f"https://raw.githubusercontent.com/NVIDIA/OpenShell/{OPENSHELL_VERSION}/install.sh"
GATEWAY_URL = "https://127.0.0.1:17670"
GATEWAY_TOML = Path.home() / ".config" / "openshell" / "gateway.toml"
CRED_DIR = Path("/etc/openshell-tg")
CRED_FILE = CRED_DIR / "bot-token.cred"
UNIT_FILE = Path("/etc/systemd/system/openshell-tg.service")
SERVICE = "openshell-tg"


class SetupError(Exception):
    """User-facing setup failure."""


def config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "openshell-tg" / "config.yaml"


def load_config() -> dict:
    try:
        return yaml.safe_load(config_path().read_text()) or {}
    except FileNotFoundError:
        return {}


def save_config(data: dict) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(yaml.safe_dump(data, sort_keys=False))


# KVM and OpenShell

def check_kvm() -> None:
    if not os.path.exists("/dev/kvm"):
        raise SetupError(m.t("no_kvm"))
    if not os.access("/dev/kvm", os.R_OK | os.W_OK):
        raise SetupError(m.t("kvm_no_access", user=getpass.getuser()))


def ensure_openshell(ask) -> None:
    if osh.installed():
        return
    if not ask(m.t("install_openshell", version=OPENSHELL_VERSION), default=True):
        raise SetupError(m.t("need_openshell"))
    cmd = f"curl -LsSf {INSTALLER} | OPENSHELL_VERSION={OPENSHELL_VERSION} sh"
    if subprocess.call(["sh", "-c", cmd]) != 0 or not osh.installed():
        raise SetupError(m.t("install_failed"))


def gateway_toml(image: str, vcpus: int = 4, mem_mib: int = 8192, disk_mib: int = 40960) -> str:
    return f"""# Written by openshell-tg: run sandboxes as MicroVMs.
[openshell]
version = 2

[openshell.gateway]
compute_driver = "vm"

[openshell.drivers.vm]
default_image = "{image}"
vcpus = {vcpus}
mem_mib = {mem_mib}
overlay_disk_mib = {disk_mib}
"""


def _gateway_connected() -> bool:
    try:
        return osh.status().get("status") == "connected"
    except (osh.OpenShellError, ValueError):
        return False


def _restart_gateway() -> None:
    subprocess.call(["systemctl", "--user", "restart", "openshell-gateway"])
    for _ in range(30):
        if _gateway_connected():
            return
        time.sleep(2)
    raise SetupError(m.t("gateway_down"))


def ensure_gateway(image: str, say) -> None:
    info = osh.status()
    if info.get("status") == "not_configured":
        osh.run(["gateway", "add", GATEWAY_URL, "--local", "--name", "openshell"])
    if not _gateway_connected():
        say(m.t("gateway_starting"))
        _restart_gateway()
    if "vm" in osh.compute_drivers():
        return
    if GATEWAY_TOML.exists():
        raise SetupError(m.t("gateway_not_vm", file=GATEWAY_TOML))
    say(m.t("gateway_vm"))
    GATEWAY_TOML.parent.mkdir(parents=True, exist_ok=True)
    GATEWAY_TOML.write_text(gateway_toml(image))
    if shutil.which("openshell-gateway"):
        check = subprocess.run(["openshell-gateway", "config", "preflight", "--path", str(GATEWAY_TOML)],
                               capture_output=True, text=True)
        if check.returncode != 0:
            GATEWAY_TOML.unlink()
            raise SetupError(m.t("gateway_config_rejected", detail=check.stderr.strip()[-300:]))
    _restart_gateway()
    if "vm" not in osh.compute_drivers():
        raise SetupError(m.t("gateway_not_vm", file=GATEWAY_TOML))


# Telegram and the service

def service_installed() -> bool:
    # The credential file sits in a root-only directory, so ask systemd instead of stat-ing it.
    return UNIT_FILE.exists() and subprocess.call(
        ["systemctl", "is-enabled", "--quiet", SERVICE], stderr=subprocess.DEVNULL) == 0


def detect_chat(token: str, say, wait: int = 300) -> tuple[dict, int, int]:
    """Validate the token and wait for `/start <code>` from the owner. Returns (bot, chat_id, user_id).

    The one-time code shown in the terminal proves the Telegram account belongs
    to the person at the keyboard, not to whoever messages the bot first.
    """
    tg = Telegram(token)
    try:
        bot = tg.get_me()
    except TelegramError:
        raise SetupError(m.t("bad_bot_token")) from None
    offset = None
    for update in tg.get_updates(None, timeout=0):  # drop anything sent before now
        offset = update["update_id"] + 1
    code = f"{secrets.randbelow(10**6):06d}"
    say(m.t("send_start", bot=bot.get("username", ""), code=code))
    deadline = time.time() + wait
    while time.time() < deadline:
        for update in tg.get_updates(offset, timeout=20):
            offset = update["update_id"] + 1
            msg = update.get("message") or {}
            if msg.get("chat", {}).get("type") == "private" and msg.get("text", "").split() == ["/start", code]:
                tg.get_updates(offset, timeout=0)  # acknowledge, so the service starts clean
                return bot, msg["chat"]["id"], msg["from"]["id"]
    raise SetupError(m.t("no_start"))


def unit_text(user: str, home: str) -> str:
    return f"""[Unit]
Description=openshell-tg: approve OpenShell sandbox requests from Telegram
Wants=network-online.target
After=network-online.target

[Service]
User={user}
Environment=HOME={home}
Environment=PATH={os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")}
ExecStart={sys.executable} -m openshell_tg serve
LoadCredentialEncrypted=bot-token:{CRED_FILE}
Restart=always
RestartSec=5
NoNewPrivileges=yes

[Install]
WantedBy=multi-user.target
"""


def _sudo(args: list[str], input_text: str | None = None) -> None:
    proc = subprocess.run(["sudo", *args], input=input_text, text=True, capture_output=True)
    if proc.returncode != 0:
        raise SetupError(m.t("sudo_failed", detail=proc.stderr.strip()[-300:]))


def install_service(token: str, say) -> None:
    if not shutil.which("systemd-creds"):
        raise SetupError(m.t("no_systemd_creds"))
    user = getpass.getuser()
    home = pwd.getpwnam(user).pw_dir
    say(m.t("sudo_once"))
    if subprocess.call(["sudo", "-v"]) != 0:
        raise SetupError(m.t("sudo_failed", detail=""))
    _sudo(["install", "-d", "-m", "0700", str(CRED_DIR)])
    _sudo(["systemd-creds", "encrypt", "--name=bot-token", "-", str(CRED_FILE)], input_text=token)
    _sudo(["tee", str(UNIT_FILE)], input_text=unit_text(user, home))
    _sudo(["systemctl", "daemon-reload"])
    _sudo(["systemctl", "enable", "--now", SERVICE])
    _sudo(["loginctl", "enable-linger", user])


def service_active() -> bool:
    return subprocess.call(["systemctl", "is-active", "--quiet", SERVICE]) == 0


def read_bot_token() -> str:
    """Inside the service: systemd decrypted the token into a private directory."""
    cred_dir = os.environ.get("CREDENTIALS_DIRECTORY")
    if not cred_dir:
        raise SetupError(m.t("no_credentials_dir"))
    return (Path(cred_dir) / "bot-token").read_text().strip()


def setup_telegram(prompt_secret, choose, say) -> None:
    config = load_config()
    if config.get("chat_id") and service_installed():
        if not service_active():
            _sudo(["systemctl", "restart", SERVICE])
        return
    if not config.get("language"):
        config["language"] = choose(m.t("ask_language"), ["en", "vi"])
        m.set_language(config["language"])
    token = prompt_secret(m.t("ask_bot_token"))
    bot, chat_id, user_id = detect_chat(token, say)
    config.update(chat_id=chat_id, user_id=user_id, bot=bot.get("username", ""))
    save_config(config)
    install_service(token, say)

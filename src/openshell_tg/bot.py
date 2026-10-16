"""The approval service: Telegram loop, commands, buttons and housekeeping.

Runs as `openshell-tg serve` under systemd. Single-threaded: each loop
iteration long-polls Telegram briefly, then polls OpenShell for proposals.
"""

from __future__ import annotations

import hashlib
import html
import logging
import secrets
import shutil
import time
from pathlib import Path

from . import host, osh
from . import messages as m
from . import sandbox as sb
from .approval import Approvals
from .store import Store
from .telegram import Telegram, TelegramError

log = logging.getLogger(__name__)

COMMANDS = ("help", "list", "status", "rules")
IDLE_EVERY = 600
HOUSEKEEPING_EVERY = 3600
REMIND_AFTER = 14 * 86400
DISK_WARN = 0.10


def boot_time() -> str:
    """An id that changes on every boot (kernel boot_id)."""
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return ""


class Bot:
    def __init__(self, tg: Telegram, store: Store, chat_id: int, user_id: int):
        self.tg = tg
        self.store = store
        self.chat_id = chat_id
        self.user_id = user_id
        self.approvals = Approvals(store, tg, chat_id)
        self.offline_since: float | None = None
        self._last_idle = 0.0
        self._last_house = 0.0

    def say(self, _key: str, /, buttons=None, **kw) -> int | None:
        try:
            return self.tg.send(self.chat_id, m.t(_key, **kw), buttons)
        except TelegramError as exc:
            log.warning("send %s failed: %s", _key, exc)
            return None

    # start-up

    def start(self) -> None:
        try:
            self.tg.set_commands([(c, m.t(f"cmd_{c}")) for c in COMMANDS])
        except TelegramError as exc:
            log.warning("setMyCommands failed: %s", exc)
        boot = boot_time()
        previous = self.store.get("boot")
        if previous and boot and previous != boot:
            for box in self.store.sandboxes():
                if self.store.get(f"alive:{box.name}") == "1":
                    self.say("rebooted", sandbox=html.escape(box.name), folder=html.escape(box.folder))
                    self.store.put(f"alive:{box.name}", "0")
        if boot:
            self.store.put("boot", boot)
        expired = self.approvals.expire_all("expired_off")
        if expired:
            self.say("expired_while_off", count=expired)
        if not self.store.get("welcomed"):
            self.say("ready")
            self.store.put("welcomed", "1")

    # main loop

    def run_forever(self) -> None:
        self.start()
        offset = int(self.store.get("offset") or 0) or None
        while True:
            try:
                updates = self.tg.get_updates(offset, timeout=4)
                if self.offline_since:
                    self._back_online()
            except TelegramError as exc:
                if not self.offline_since:
                    log.warning("Telegram unreachable: %s", exc)
                    self.offline_since = time.time()
                updates = []
                time.sleep(5)
            for update in updates:
                offset = update["update_id"] + 1
                self.store.put("offset", str(offset))
                try:
                    self.handle(update)
                except Exception:  # one bad update must not stop the service
                    log.exception("update failed")
            self.tick()

    def tick(self) -> None:
        """One round of polling and housekeeping. A failing step never stops the service."""
        for box in self.store.sandboxes():
            _guard(self.approvals.poll, box.name, box.agent)
        now = time.time()
        if now - self._last_idle > IDLE_EVERY:
            self._last_idle = now
            _guard(self.idle_check)
        if now - self._last_house > HOUSEKEEPING_EVERY:
            self._last_house = now
            _guard(self.reminders)
            _guard(self.disk_check)

    def _back_online(self) -> None:
        since = self.offline_since
        self.offline_since = None
        expired = sum(1 for _ in self.store.db.execute(
            "SELECT 1 FROM requests WHERE status='expired' AND t1>=?", (since - 1800,)))
        if expired:
            self.say("expired_while_offline", count=expired)

    # updates

    def handle(self, update: dict) -> None:
        if "callback_query" in update:
            query = update["callback_query"]
            if query.get("from", {}).get("id") != self.user_id:
                return
            try:
                toast = self.on_button(query.get("data", ""), query.get("message", {}).get("message_id"))
            except Exception:
                log.exception("button failed")
                toast = m.t("toast_failed")
            try:
                self.tg.answer(query["id"], toast)
            except TelegramError:
                pass
            return
        msg = update.get("message") or {}
        if msg.get("chat", {}).get("id") != self.chat_id or msg.get("from", {}).get("id") != self.user_id:
            return
        text = msg.get("text", "").strip()
        if not text.startswith("/"):
            self.say("help")
            return
        command, *args = text[1:].split()
        command = command.split("@")[0].lower()
        handler = {"start": self.cmd_help, "help": self.cmd_help, "list": self.cmd_list,
                   "status": self.cmd_status, "rules": self.cmd_rules}.get(command, self.cmd_help)
        handler(args)

    # commands

    def cmd_help(self, args) -> None:
        self.say("help")

    def cmd_list(self, args) -> None:
        boxes = self.store.sandboxes()
        if not boxes:
            self.say("list_empty")
            return
        lines, buttons = [f"<b>{m.t('list_title')}</b>"], []
        for box in boxes:
            phase = sb.state(box.name) or "missing"
            folder_ok = Path(box.folder).is_dir()
            lines.append(m.t("list_row", name=html.escape(box.name), state=m.t(f"phase_{_phase(phase)}"),
                             used=m.ago(box.last_used), folder=html.escape(box.folder),
                             missing="" if folder_ok else m.t("folder_missing")))
            row = []
            if phase == "Ready":
                row.append((m.t("btn_stop") + " " + _short(box.name), f"stop:{box.name}"))
            row.append((m.t("btn_delete") + " " + _short(box.name), f"del:{box.name}"))
            buttons.append(row)
        self.tg.send(self.chat_id, "\n\n".join(lines), buttons)

    def cmd_status(self, args) -> None:
        try:
            gateway = osh.status().get("status") == "connected"
        except (osh.OpenShellError, ValueError):
            gateway = False
        stats = self.store.stats()
        open_reqs = self.store.open_requests()
        self.say("status", gateway=m.t("ok") if gateway else m.t("down"), pending=len(open_reqs),
                 total=stats["total"], approved=stats["by_status"].get("approved", 0),
                 rejected=stats["by_status"].get("rejected", 0),
                 expired=stats["by_status"].get("expired", 0) + stats["by_status"].get("expired_off", 0),
                 median=m.duration(stats["median"]), p90=m.duration(stats["p90"]),
                 waited=m.duration(stats["waited"]))
        sent = set()
        for req in open_reqs:
            if req.message_id in sent:
                continue
            if req.message_id is not None:
                sent.add(req.message_id)
                try:
                    self.tg.edit(self.chat_id, req.message_id, m.t("card_moved"), None)
                except TelegramError:
                    pass
            box = self.store.sandbox(req.sandbox)
            self.approvals.send_card(req, box.agent if box else "")

    def cmd_rules(self, args) -> None:
        boxes = self.store.sandboxes()
        if args:
            boxes = [b for b in boxes if b.name == args[0] or args[0] in b.name]
        if not boxes:
            self.say("list_empty")
            return
        for box in boxes:
            text, buttons = self.rules_view(box.name)
            self.tg.send(self.chat_id, text, buttons)

    RULES_PER_PAGE = 8

    @staticmethod
    def _token(*parts: str) -> str:
        return hashlib.sha1("\n".join(parts).encode()).hexdigest()[:8]

    def rules_view(self, name: str, page: int = 0) -> tuple[str, list | None]:
        """A numbered, paged list of the addresses the sandbox's active policy allows.

        One tap on a number asks for confirmation; a second tap removes the address.
        """
        rules = self.approvals.approved(name)
        if not rules:
            return m.t("rules_empty", sandbox=html.escape(name)), None
        per = self.RULES_PER_PAGE
        pages = (len(rules) + per - 1) // per
        page = max(0, min(page, pages - 1))
        shown = rules[page * per:(page + 1) * per]
        view = self._token(name)
        self.store.put(f"rv:{view}", name)
        lines = [m.t("rules_title", sandbox=html.escape(name), count=len(rules))]
        if pages > 1:
            lines.append(m.t("rules_page", page=page + 1, pages=pages))
        lines.append("")
        row: list[tuple[str, str]] = []
        buttons: list[list[tuple[str, str]]] = []
        for i, (rule, endpoints) in enumerate(shown, start=page * per + 1):
            lines.append(f"{i}. <code>{html.escape((endpoints or rule)[:70])}</code>")
            token = self._token(name, rule)
            self.store.put(f"rm:{token}", f"{name}\n{rule}\n{page}")
            row.append((f"{m.t('btn_remove')} {i}", f"rm:{token}"))
            if len(row) == 4:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)
        if pages > 1:
            nav = []
            if page > 0:
                nav.append(("◀", f"rl:{view}:{page - 1}"))
            nav.append((f"{page + 1}/{pages}", f"rl:{view}:{page}"))
            if page < pages - 1:
                nav.append(("▶", f"rl:{view}:{page + 1}"))
            buttons.append(nav)
        return "\n".join(lines), buttons

    def _show_rules(self, message_id: int | None, name: str, page: int = 0) -> None:
        if message_id is None:
            return
        text, buttons = self.rules_view(name, page)
        try:
            self.tg.edit(self.chat_id, message_id, text, buttons)
        except TelegramError as exc:
            log.warning("edit rules list failed: %s", exc)

    def _rules_button(self, action: str, arg: str, message_id: int | None) -> str | None:
        if action == "rl":
            token, _, page = arg.partition(":")
            name = self.store.get(f"rv:{token}")
            if not name:
                return m.t("toast_handled")
            self._show_rules(message_id, name, int(page) if page.isdigit() else 0)
            return None
        value = self.store.get(f"rm:{arg}")
        if not value:
            return m.t("toast_handled")
        name, rule, page = (value.split("\n") + ["0"])[:3]
        page_no = int(page) if page.isdigit() else 0
        if action == "rm":
            endpoints = dict(self.approvals.approved(name)).get(rule)
            if endpoints is None:
                self._show_rules(message_id, name, page_no)
                return m.t("toast_handled")
            if message_id is not None:
                text = m.t("rules_confirm", sandbox=html.escape(name), endpoints=html.escape(endpoints[:200]),
                           rule=html.escape(rule[:60]))
                buttons = [[(m.t("btn_remove_yes"), f"rmy:{arg}"),
                            (m.t("btn_cancel"), f"rl:{self._token(name)}:{page_no}")]]
                try:
                    self.tg.edit(self.chat_id, message_id, text, buttons)
                except TelegramError as exc:
                    log.warning("edit rules confirm failed: %s", exc)
            return None
        ok = self.approvals.remove(name, rule)
        self._show_rules(message_id, name, page_no)
        return m.t("toast_removed") if ok else m.t("toast_failed")

    # buttons

    def on_button(self, data: str, message_id: int | None) -> str | None:
        action, _, arg = data.partition(":")
        if action in ("ok", "no", "why", "back", "info"):
            return self.approvals.on_button(data)
        if action in ("rl", "rm", "rmy"):
            return self._rules_button(action, arg, message_id)
        box = self.store.sandbox(arg)
        if box is None:
            return m.t("toast_handled")
        if action == "stop":
            try:
                osh.stop(box.name)
                self.store.mark_stopped(box.name)
                return m.t("toast_stopped")
            except osh.OpenShellError:
                return m.t("toast_failed")
        if action == "del":
            unsaved = sb.unsaved_commits(box.name, Path(box.folder))
            key = ("confirm_delete_unknown" if unsaved is None
                   else "confirm_delete_unsaved" if unsaved else "confirm_delete")
            self.say(key, [[(m.t("btn_delete_yes"), f"delyes:{box.name}"), (m.t("btn_cancel"), f"cancel:{box.name}")]],
                     sandbox=html.escape(box.name), count=unsaved or 0)
            return None
        if action == "delyes":
            try:
                sb.destroy(box.name)
            except osh.OpenShellError as exc:
                if not exc.not_found:
                    return m.t("toast_failed")
            self.store.forget_sandbox(box.name)
            self._edit(message_id, m.t("deleted", sandbox=html.escape(box.name)))
            return None
        if action == "keep":
            self.store.keep(box.name, REMIND_AFTER)
            self._edit(message_id, m.t("kept", sandbox=html.escape(box.name)))
            return None
        if action == "cancel":
            self._edit(message_id, m.t("cancelled"))
            return None
        return None

    def _edit(self, message_id: int | None, text: str) -> None:
        if message_id is None:
            return
        try:
            self.tg.edit(self.chat_id, message_id, text, None)
        except TelegramError:
            pass

    # housekeeping

    def idle_check(self) -> None:
        now = time.time()
        for box in self.store.sandboxes():
            try:
                phase = sb.state(box.name)
            except osh.OpenShellError:
                continue
            if phase is None:
                # Double-check with the full list before forgetting a sandbox.
                if box.name not in {s.get("name") for s in osh.sandboxes()}:
                    self.store.forget_sandbox(box.name)
                continue
            if phase == "Ready":
                self.store.mark_running(box.name)
            else:
                if phase == "Stopped" and not box.stopped_at:
                    self.store.mark_stopped(box.name)
                self.store.put(f"alive:{box.name}", "0")
                continue
            if sb.agent_running(box.name):
                self.store.touch(box.name)
                self.store.put(f"alive:{box.name}", "1")
            else:
                self.store.put(f"alive:{box.name}", "0")
                if now - box.last_used > box.idle_stop:
                    try:
                        osh.stop(box.name)
                        self.store.mark_stopped(box.name)
                    except osh.OpenShellError as exc:
                        log.warning("idle stop %s failed: %s", box.name, exc)

    def reminders(self) -> None:
        now = time.time()
        for box in self.store.sandboxes():
            if box.reminded_at or (box.keep_until and box.keep_until > now):
                continue
            missing = not Path(box.folder).is_dir()
            old = box.stopped_at and now - box.stopped_at > REMIND_AFTER
            if not (missing or old):
                continue
            key = "remind_missing" if missing else "remind_stopped"
            days = int((now - (box.stopped_at or now)) // 86400)
            self.say(key, [[(m.t("btn_delete"), f"del:{box.name}"), (m.t("btn_keep"), f"keep:{box.name}")]],
                     sandbox=html.escape(box.name), folder=html.escape(box.folder), days=days)
            self.store.mark_reminded(box.name)

    def disk_check(self) -> None:
        usage = shutil.disk_usage(Path.home())
        if usage.free / usage.total >= DISK_WARN:
            return
        last = float(self.store.get("disk_warned") or 0)
        if time.time() - last < 86400:
            return
        self.say("disk_low", percent=int(100 - usage.free * 100 / usage.total))
        self.store.put("disk_warned", str(time.time()))


def _guard(func, *args) -> None:
    try:
        func(*args)
    except Exception:
        log.exception("%s failed", getattr(func, "__name__", func))


def _phase(phase: str) -> str:
    return {"Ready": "running", "Stopped": "stopped", "missing": "missing"}.get(phase, "busy")


def _short(name: str) -> str:
    parts = name.split("-")
    return "-".join(parts[1:-1])[:20] if len(parts) > 2 else name


def serve() -> None:
    config = host.load_config()
    m.set_language(config.get("language", "en"))
    token = host.read_bot_token()
    bot = Bot(Telegram(token), Store(), int(config["chat_id"]), int(config["user_id"]))
    bot.run_forever()

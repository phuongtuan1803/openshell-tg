"""The approval service: Telegram loop, commands, buttons and housekeeping.

Runs as `openshell-tg serve` under systemd. Single-threaded: each loop
iteration long-polls Telegram briefly, then polls OpenShell for proposals.
"""

from __future__ import annotations

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
    try:
        for line in Path("/proc/stat").read_text().splitlines():
            if line.startswith("btime "):
                return line.split()[1]
    except OSError:
        pass
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
        for box in self.store.sandboxes():
            self.approvals.poll(box.name, box.agent)
        now = time.time()
        if now - self._last_idle > IDLE_EVERY:
            self._last_idle = now
            self.idle_check()
        if now - self._last_house > HOUSEKEEPING_EVERY:
            self._last_house = now
            self.reminders()
            self.disk_check()

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
            toast = self.on_button(query.get("data", ""), query.get("message", {}).get("message_id"))
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
                row.append((m.t("btn_stop") + " " + _short(box.name), f"stop:{box.id}"))
            row.append((m.t("btn_delete") + " " + _short(box.name), f"del:{box.id}"))
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
            rules = self.approvals.approved(box.name)
            if not rules:
                self.say("rules_empty", sandbox=html.escape(box.name))
                continue
            lines = [m.t("rules_title", sandbox=html.escape(box.name))]
            buttons = []
            for chunk in rules[:30]:
                lines.append(f"• <code>{html.escape(chunk.endpoints)}</code> ({html.escape(chunk.rule)})")
                token = secrets.token_hex(4)
                self.store.put(f"rm:{token}", f"{box.name}\n{chunk.rule}")
                buttons.append([(m.t("btn_remove") + " " + chunk.endpoints[:40], f"rm:{token}")])
            self.tg.send(self.chat_id, "\n".join(lines), buttons)

    # buttons

    def on_button(self, data: str, message_id: int | None) -> str | None:
        action, _, arg = data.partition(":")
        if action in ("ok", "no", "why", "back", "info"):
            return self.approvals.on_button(data)
        if action == "rm":
            value = self.store.get(f"rm:{arg}")
            if not value:
                return m.t("toast_handled")
            name, rule = value.split("\n", 1)
            ok = self.approvals.remove(name, rule)
            self.store.db.execute("DELETE FROM meta WHERE key=?", (f"rm:{arg}",))
            return m.t("toast_removed") if ok else m.t("toast_failed")
        box = self.store.sandbox_by_id(int(arg)) if arg.isdigit() else None
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
            key = "confirm_delete_unsaved" if unsaved else "confirm_delete"
            self.say(key, [[(m.t("btn_delete_yes"), f"delyes:{box.id}"), (m.t("btn_cancel"), f"cancel:{box.id}")]],
                     sandbox=html.escape(box.name), count=unsaved)
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
                self.store.forget_sandbox(box.name)
                continue
            if phase != "Ready":
                if not box.stopped_at:
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
            self.say(key, [[(m.t("btn_delete"), f"del:{box.id}"), (m.t("btn_keep"), f"keep:{box.id}")]],
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

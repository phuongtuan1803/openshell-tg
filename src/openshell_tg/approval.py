"""Turn OpenShell's pending proposals into Telegram cards and act on button presses."""

from __future__ import annotations

import html
import json
import logging
import time
from dataclasses import asdict

from . import messages as m
from . import osh
from .store import Request, Store
from .telegram import Telegram, TelegramError

log = logging.getLogger(__name__)

EXPIRY = 30 * 60
REASONS = ("reason_broad", "reason_unneeded", "reason_forbidden")


def _e(text: str) -> str:
    return html.escape(text or "", quote=False)


def _detail(req: Request) -> dict:
    return json.loads(req.detail or "{}")


def card(req: Request, agent: str = "", count: int = 1) -> tuple[str, list]:
    d = _detail(req)
    title = m.t("card_title") + " · " + (m.t("card_risky") if req.risky else m.t("card_low_risk"))
    lines = [f"<b>{_e(title)}</b>" + (f"  ×{count}" if count > 1 else "")]
    lines.append(f"{m.t('card_sandbox')}: {_e(req.sandbox)}" + (f" · {_e(agent)}" if agent else ""))
    lines.append(f"{m.t('card_address')}: <code>{_e(d.get('endpoints', '?'))}</code>")
    program = d.get("binary") or d.get("binaries")
    if program:
        lines.append(f"{m.t('card_program')}: <code>{_e(program)}</code>")
    if d.get("rationale"):
        lines.append(f"{m.t('card_reason')}: {_e(d['rationale'][:300])}")
    if req.risky:
        warning = d.get("security") or m.t("card_unparsed")
        lines.append(f"⚠️ {_e(warning[:300])}")
        lines.append(m.t("card_risky_hint", sandbox=req.sandbox, chunk=req.chunk_id))
    if req.t1:
        expires = time.strftime("%H:%M", time.localtime(req.t1 + EXPIRY))
        lines.append(f"{m.t('card_expires')}: {expires}")
    rid = req.id
    if req.risky:
        buttons = [[(m.t("btn_reject"), f"no:{rid}"), (m.t("btn_details"), f"info:{rid}")]]
    else:
        buttons = [[(m.t("btn_approve"), f"ok:{rid}"), (m.t("btn_reject"), f"no:{rid}"),
                    (m.t("btn_details"), f"info:{rid}")]]
    return "\n".join(lines), buttons


def closed_card(req: Request, status: str, reason: str | None = None) -> str:
    d = _detail(req)
    label = m.t(f"closed_{status}")
    lines = [f"<b>{_e(label)}</b>", f"{m.t('card_sandbox')}: {_e(req.sandbox)}",
             f"{m.t('card_address')}: <code>{_e(d.get('endpoints', '?'))}</code>"]
    if reason:
        lines.append(f"{m.t('card_reject_reason')}: {_e(reason)}")
    if req.t1 and status in ("approved", "rejected"):
        lines.append(f"{m.t('card_response')}: {m.duration(time.time() - req.t1)}")
    return "\n".join(lines)


def details(req: Request) -> str:
    d = _detail(req)
    rows = [f"<b>{_e(m.t('card_title'))}</b>", f"chunk: <code>{_e(req.chunk_id)}</code>"]
    for key in ("rule", "endpoints", "binary", "binaries", "confidence", "rationale", "security", "prover"):
        if d.get(key):
            rows.append(f"{key}: {_e(str(d[key])[:800])}")
    return "\n".join(rows)


class Approvals:
    def __init__(self, store: Store, tg: Telegram, chat_id: int):
        self.store = store
        self.tg = tg
        self.chat_id = chat_id

    # polling

    def poll(self, sandbox: str, agent: str = "") -> None:
        try:
            pending = osh.rules(sandbox, "pending")
        except osh.OpenShellError as exc:
            if not exc.not_found:
                log.warning("rule get %s failed: %s", sandbox, exc)
            return
        seen = set()
        for chunk in pending:
            seen.add(chunk.id)
            detail = {k: v for k, v in asdict(chunk).items() if not k.startswith("_")}
            if not self.store.upsert_request(sandbox, chunk.id, chunk.summary(), json.dumps(detail),
                                             chunk.risky, time.time()):
                continue
            dup = self.store.find_duplicate(sandbox, chunk.summary())
            if dup:
                count = self.store.bump(dup.sandbox, dup.chunk_id)
                self._edit(dup, *card(dup, agent, count))
                self.store.set_message(sandbox, chunk.id, dup.message_id)
                continue
            self.send_card(self.store.request(sandbox, chunk.id), agent)
        self._reconcile(sandbox, seen)

    def send_card(self, req: Request, agent: str = "") -> None:
        if req.message_id is None:
            req.t1 = time.time()
        text, buttons = card(req, agent, req.count)
        try:
            message_id = self.tg.send(self.chat_id, text, buttons)
        except TelegramError as exc:
            log.warning("send card failed: %s", exc)
            return
        self.store.set_message(req.sandbox, req.chunk_id, message_id)

    def _reconcile(self, sandbox: str, pending_ids: set[str]) -> None:
        """Close cards whose proposal was decided elsewhere or disappeared."""
        open_reqs = [r for r in self.store.open_requests() if r.sandbox == sandbox]
        gone = [r for r in open_reqs if r.chunk_id not in pending_ids]
        if gone:
            try:
                approved = {c.id for c in osh.rules(sandbox, "approved")}
                rejected = {c.id for c in osh.rules(sandbox, "rejected")}
            except osh.OpenShellError:
                return
            for req in gone:
                status = ("approved_elsewhere" if req.chunk_id in approved
                          else "rejected_elsewhere" if req.chunk_id in rejected else "gone")
                self._close(req, status)
        for req in open_reqs:
            if req.chunk_id in pending_ids and req.t1 and time.time() - req.t1 > EXPIRY:
                self._decide(req, approve=False, reason=m.t("reason_expired", lang="en"), status="expired")

    def expire_all(self, status: str = "expired_off") -> int:
        """At service start: cards left open across a shutdown that are past expiry."""
        count = 0
        for req in self.store.open_requests():
            if req.t1 and time.time() - req.t1 > EXPIRY:
                self._decide(req, approve=False, reason=m.t("reason_expired", lang="en"), status=status)
                count += 1
        return count

    # decisions

    def _decide(self, req: Request, approve: bool, reason: str | None = None,
                status: str | None = None, tapped: bool = False) -> bool:
        group = self.store.group(req) or [req]
        ok = True
        for r in group:
            try:
                if approve:
                    osh.approve(r.sandbox, r.chunk_id)
                else:
                    osh.reject(r.sandbox, r.chunk_id, reason or "")
            except osh.OpenShellError as exc:
                log.warning("decision on %s failed: %s", r.chunk_id, exc)
                ok = False
                continue
            self.store.close_request(r.sandbox, r.chunk_id, status or ("approved" if approve else "rejected"),
                                     reason, tapped=tapped)
            if approve:
                self.store.mark_loaded(r.sandbox, r.chunk_id)
        self._edit(req, closed_card(req, status or ("approved" if approve else "rejected"), reason), None)
        return ok

    def _close(self, req: Request, status: str) -> None:
        self.store.close_request(req.sandbox, req.chunk_id, status)
        self._edit(req, closed_card(req, status), None)

    def _edit(self, req: Request, text: str, buttons) -> None:
        if req.message_id is None:
            return
        try:
            self.tg.edit(self.chat_id, req.message_id, text, buttons)
        except TelegramError as exc:
            log.warning("edit card failed: %s", exc)

    # buttons

    def on_button(self, data: str) -> str | None:
        """Handle a card button. Returns a short toast text, or None."""
        action, _, rest = data.partition(":")
        rid, _, arg = rest.partition(":")
        req = self.store.request_by_id(int(rid)) if rid.isdigit() else None
        if req is None:
            return m.t("toast_handled")
        if req.status != "pending":
            return m.t("toast_handled")
        if action == "ok":
            if req.risky:
                return m.t("toast_risky")
            return None if self._decide(req, approve=True, tapped=True) else m.t("toast_failed")
        if action == "no":
            buttons = [[(m.t(r), f"why:{req.id}:{i}")] for i, r in enumerate(REASONS)]
            buttons.append([(m.t("btn_back"), f"back:{req.id}")])
            self._edit(req, m.t("pick_reason"), buttons)
            return None
        if action == "why" and arg.isdigit() and int(arg) < len(REASONS):
            reason = m.t(REASONS[int(arg)], lang="en")
            return None if self._decide(req, approve=False, reason=reason, tapped=True) else m.t("toast_failed")
        if action == "back":
            self._edit(req, *card(req, count=req.count))
            return None
        if action == "info":
            self.tg.send(self.chat_id, details(req))
            return None
        return None

    # approved rules

    def approved(self, sandbox: str) -> list[osh.Chunk]:
        try:
            return [c for c in osh.rules(sandbox, "approved") if c.rule]
        except osh.OpenShellError:
            return []

    def remove(self, sandbox: str, rule: str) -> bool:
        try:
            osh.remove_rule(sandbox, rule)
            return True
        except osh.OpenShellError as exc:
            log.warning("remove rule %s failed: %s", rule, exc)
            return False

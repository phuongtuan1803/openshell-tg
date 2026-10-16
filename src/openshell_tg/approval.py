"""Turn OpenShell's pending proposals into Telegram cards and act on button presses.

Safety rules:
- A card shows the rule name, endpoints and the programs the rule would grant.
- Approve re-reads the proposal and proceeds only if it is still trusted and
  shows exactly what the card showed.
- Only trusted, low-risk proposals are folded into one card; risky ones always
  get their own card without an Approve button.
- Every proposal is rejected 30 minutes after it was first seen, even if its
  card could not be sent.
"""

from __future__ import annotations

import html
import json
import logging
import time
from dataclasses import asdict

from . import messages as m
from . import osh
from . import sandbox as sb
from .store import Request, Store
from .telegram import Telegram, TelegramError

log = logging.getLogger(__name__)

EXPIRY = 30 * 60
REASONS = ("reason_broad", "reason_unneeded", "reason_forbidden")


def _e(text: object, limit: int = 300) -> str:
    text = str(text or "")
    if len(text) > limit:
        text = text[:limit] + "…"
    return html.escape(text, quote=False)


def _detail(req: Request) -> dict:
    return json.loads(req.detail or "{}")


def card(req: Request, agent: str = "", count: int = 1) -> tuple[str, list]:
    d = _detail(req)
    title = m.t("card_title") + " · " + (m.t("card_risky") if req.risky else m.t("card_low_risk"))
    lines = [f"<b>{_e(title)}</b>" + (f"  ×{count}" if count > 1 else "")]
    lines.append(f"{m.t('card_sandbox')}: {_e(req.sandbox)}" + (f" · {_e(agent)}" if agent else ""))
    lines.append(f"{m.t('card_address')}: <code>{_e(d.get('endpoints') or '?')}</code>")
    grants = d.get("binaries") or d.get("binary")
    if grants:
        lines.append(f"{m.t('card_program')}: <code>{_e(grants)}</code>")
    if d.get("binary") and d.get("binaries") and d["binary"] not in d["binaries"]:
        lines.append(m.t("card_requester", binary=_e(d["binary"])))
    if d.get("rationale"):
        lines.append(f"{m.t('card_reason')}: {_e(d['rationale'])}")
    if req.risky:
        lines.append(f"⚠️ {_e(d.get('security') or m.t('card_unparsed'))}")
        if d.get("trusted"):
            lines.append(m.t("card_risky_hint", sandbox=_e(req.sandbox), chunk=_e(req.chunk_id)))
        else:
            lines.append(m.t("card_check_machine", sandbox=_e(req.sandbox)))
    expires = time.strftime("%H:%M", time.localtime((req.t0 or time.time()) + EXPIRY))
    lines.append(f"{m.t('card_expires')}: {expires}")
    rid = req.id
    if req.risky:
        buttons = [[(m.t("btn_reject"), f"no:{rid}"), (m.t("btn_details"), f"info:{rid}")]]
    else:
        buttons = [[(m.t("btn_approve"), f"ok:{rid}"), (m.t("btn_reject"), f"no:{rid}"),
                    (m.t("btn_details"), f"info:{rid}")]]
    return "\n".join(lines), buttons


def closed_card(req: Request, status: str, reason: str | None = None, note: str | None = None) -> str:
    d = _detail(req)
    lines = [f"<b>{_e(m.t(f'closed_{status}'))}</b>", f"{m.t('card_sandbox')}: {_e(req.sandbox)}",
             f"{m.t('card_address')}: <code>{_e(d.get('endpoints') or '?')}</code>"]
    if reason:
        lines.append(f"{m.t('card_reject_reason')}: {_e(reason)}")
    if req.t1 and status in ("approved", "rejected"):
        lines.append(f"{m.t('card_response')}: {m.duration(time.time() - req.t1)}")
    if note:
        lines.append(note)
    return "\n".join(lines)


def details(req: Request) -> str:
    d = _detail(req)
    rows = [f"<b>{_e(m.t('card_title'))}</b>", f"chunk: <code>{_e(req.chunk_id, 80)}</code>"]
    for key in ("rule", "endpoints", "binary", "binaries", "confidence", "rationale", "security", "prover"):
        if d.get(key):
            rows.append(f"{key}: {_e(d[key], 400)}")
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
            dup = None if chunk.risky else self.store.find_duplicate(sandbox, chunk.summary())
            if dup:
                count = self.store.bump(dup.sandbox, dup.chunk_id)
                self._edit(dup, *card(dup, agent, count))
                self.store.set_message(sandbox, chunk.id, dup.message_id)
                continue
            self.send_card(self.store.request(sandbox, chunk.id), agent)
        # Cards that could not be sent earlier (Telegram down, error): try again.
        for req in self.store.open_requests():
            if req.sandbox == sandbox and req.message_id is None and req.chunk_id in seen:
                self.send_card(req, agent)
        self._reconcile(sandbox, seen)

    def send_card(self, req: Request, agent: str = "") -> bool:
        text, buttons = card(req, agent, req.count)
        try:
            message_id = self.tg.send(self.chat_id, text, buttons)
        except TelegramError as exc:
            log.warning("send card failed: %s", exc)
            return False
        self.store.set_message(req.sandbox, req.chunk_id, message_id)
        return True

    def _reconcile(self, sandbox: str, pending_ids: set[str]) -> None:
        """Close cards decided elsewhere, and reject proposals past their expiry."""
        gone = [r for r in self.store.open_requests() if r.sandbox == sandbox and r.chunk_id not in pending_ids]
        if gone:
            try:
                approved = {c.id for c in osh.rules(sandbox, "approved")}
                rejected = {c.id for c in osh.rules(sandbox, "rejected")}
            except osh.OpenShellError:
                approved = rejected = None
            if approved is not None:
                for req in gone:
                    status = ("approved_elsewhere" if req.chunk_id in approved
                              else "rejected_elsewhere" if req.chunk_id in rejected else "gone")
                    self._close(req, status)
        for req in self.store.open_requests():
            if req.sandbox == sandbox and req.chunk_id in pending_ids and _expired(req):
                self._decide(req, approve=False, reason=m.t("reason_expired", lang="en"), status="expired")

    def expire_all(self, status: str = "expired_off") -> int:
        """At service start: reject what expired while the service was not running."""
        count = 0
        for req in self.store.open_requests():
            if _expired(req) and self.store.request_by_id(req.id).status == "pending":
                if self._decide(req, approve=False, reason=m.t("reason_expired", lang="en"), status=status):
                    count += 1
        return count

    # decisions

    def _decide(self, req: Request, approve: bool, reason: str | None = None,
                status: str | None = None, tapped: bool = False) -> bool:
        """Approve or reject every open request on this card. The card closes only if all succeed."""
        status = status or ("approved" if approve else "rejected")
        group = self.store.group(req) if not req.risky else []
        group = group or ([req] if self.store.request_by_id(req.id).status == "pending" else [])
        failed = 0
        approved: list[Request] = []
        for r in group:
            try:
                if approve:
                    self._approve(r)
                else:
                    osh.reject(r.sandbox, r.chunk_id, reason or "")
            except osh.OpenShellError as exc:
                log.warning("decision on %s failed: %s", r.chunk_id, exc)
                failed += 1
                continue
            self.store.close_request(r.sandbox, r.chunk_id, status, reason, tapped=tapped)
            if approve:
                self.store.mark_loaded(r.sandbox, r.chunk_id)
                approved.append(r)
        note = self._nudge(req.sandbox, approved) if approved and tapped else None
        if failed:
            return False
        self._edit(req, closed_card(req, status, reason, note), None)
        return True

    # telling the agent to continue

    def _nudge(self, sandbox: str, approved: list[Request]) -> str | None:
        """Tell the agent at once which rules were approved so it carries on."""
        if not sb.agent_running(sandbox):
            return None
        rules = []
        for r in approved:
            d = _detail(r)
            rules.append(m.t("nudge_rule", lang="en", rule=d.get("rule") or "?", endpoints=d.get("endpoints") or "?",
                             programs=d.get("binaries") or d.get("binary") or "?"))
        if sb.tell_agent(sandbox, m.t("nudge", lang="en", rules=" | ".join(rules))):
            return m.t("agent_told")
        return None

    def _approve(self, req: Request) -> None:
        """Approve; if OpenShell re-evaluated the proposal meanwhile, review it again and retry once."""
        try:
            osh.approve(req.sandbox, req.chunk_id)
        except osh.OpenShellError as exc:
            if "inputs changed" not in exc.stderr or not self._unchanged(req):
                raise
            osh.approve(req.sandbox, req.chunk_id)

    def _unchanged(self, req: Request) -> bool:
        """Re-read one proposal; it must still be pending, trusted, low risk and as the card showed it."""
        try:
            chunk = {c.id: c for c in osh.rules(req.sandbox, "pending")}.get(req.chunk_id)
        except osh.OpenShellError:
            return False
        shown = _detail(req)
        return (chunk is not None and not chunk.risky
                and chunk.shown() == {"rule": shown.get("rule"), "endpoints": shown.get("endpoints"),
                                      "grants": shown.get("binaries") or shown.get("binary"),
                                      "binary": shown.get("binary")})

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

    def _still_as_shown(self, req: Request) -> bool:
        """Re-read the proposal; it must be pending, trusted, low risk and unchanged."""
        try:
            current = {c.id: c for c in osh.rules(req.sandbox, "pending")}
        except osh.OpenShellError:
            return False
        shown = _detail(req)
        for r in self.store.group(req) or [req]:
            chunk = current.get(r.chunk_id)
            if chunk is None or chunk.risky:
                return False
            if chunk.shown() != {"rule": shown.get("rule"), "endpoints": shown.get("endpoints"),
                                 "grants": shown.get("binaries") or shown.get("binary"),
                                 "binary": shown.get("binary")}:
                return False
        return True

    # buttons

    def on_button(self, data: str) -> str | None:
        """Handle a card button. Returns a short toast text, or None."""
        action, _, rest = data.partition(":")
        rid, _, arg = rest.partition(":")
        req = self.store.request_by_id(int(rid)) if rid.isdigit() else None
        if req is None or req.status != "pending":
            return m.t("toast_handled")
        if action == "ok":
            if req.risky:
                return m.t("toast_risky")
            if not self._still_as_shown(req):
                return m.t("toast_changed")
            return None if self._decide(req, approve=True, tapped=True) else m.t("toast_failed")
        if action == "no":
            buttons = [[(m.t(r), f"why:{req.id}:{i}")] for i, r in enumerate(REASONS)]
            buttons.append([(m.t("btn_back"), f"back:{req.id}")])
            self._edit(req, m.t("pick_reason"), buttons)
            return None
        if action == "why" and arg.isdigit() and int(arg) < len(REASONS):
            reason = m.t(REASONS[int(arg)], lang="en")
            if self._decide(req, approve=False, reason=reason, tapped=True):
                return None
            self._edit(req, *card(req, count=req.count))
            return m.t("toast_failed")
        if action == "back":
            self._edit(req, *card(req, count=req.count))
            return None
        if action == "info":
            self.tg.send(self.chat_id, details(req))
            return None
        return None

    # approved rules (read from the active policy, not from proposals)

    def approved(self, sandbox: str) -> list[tuple[str, str]]:
        """(rule name, endpoints) for each network rule in the active policy."""
        try:
            policies = osh.network_policies(sandbox)
        except osh.OpenShellError:
            return []
        rows = []
        for name, rule in sorted(policies.items()):
            endpoints = ", ".join(f"{e.get('host', '?')}:{e.get('port', '')}".rstrip(":")
                                  for e in (rule or {}).get("endpoints") or [])
            rows.append((name, endpoints))
        return rows

    def remove(self, sandbox: str, rule: str) -> bool:
        try:
            osh.remove_rule(sandbox, rule)
            return True
        except osh.OpenShellError as exc:
            log.warning("remove rule %s failed: %s", rule, exc)
            return False


def _expired(req: Request) -> bool:
    return bool(req.t0) and time.time() - req.t0 > EXPIRY

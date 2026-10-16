import time

import pytest

from openshell_tg import approval, osh
from openshell_tg import sandbox as sb
from openshell_tg.approval import Approvals


def chunk(cid, endpoints="pypi.org:443", security="", rule="allow_pypi", binary="/usr/bin/python3"):
    return osh.Chunk(id=cid, status="pending", rule=rule, binary=binary, rationale="tests",
                     security=security, endpoints=endpoints)


@pytest.fixture
def gateway(monkeypatch):
    state = {"pending": [], "approved": [], "rejected": [], "calls": []}

    def rules(name, status="pending"):
        return list(state[status])

    def approve(name, cid):
        state["calls"].append(("approve", cid))
        moved = [c for c in state["pending"] if c.id == cid]
        state["pending"] = [c for c in state["pending"] if c.id != cid]
        state["approved"] += moved

    def reject(name, cid, reason):
        state["calls"].append(("reject", cid, reason))
        moved = [c for c in state["pending"] if c.id == cid]
        state["pending"] = [c for c in state["pending"] if c.id != cid]
        state["rejected"] += moved

    state["running"] = False  # agent not running unless a test says otherwise
    state["told"] = []
    monkeypatch.setattr(sb, "agent_running", lambda name: state["running"])
    monkeypatch.setattr(sb, "tell_agent", lambda name, text: state["told"].append(text) or True)
    monkeypatch.setattr(osh, "rules", rules)
    monkeypatch.setattr(osh, "approve", approve)
    monkeypatch.setattr(osh, "reject", reject)
    return state


def test_new_proposal_sends_one_card(store, tg, gateway):
    gateway["pending"] = [chunk("c1")]
    a = Approvals(store, tg, 1)
    a.poll("sb")
    a.poll("sb")
    assert len(tg.sent) == 1
    _, text, buttons = tg.sent[0]
    assert "pypi.org:443" in text
    assert [b[0] for b in buttons[0]] == ["Approve", "Reject", "Details"]


def test_risky_card_has_no_approve(store, tg, gateway):
    gateway["pending"] = [chunk("c1", endpoints="*.amazonaws.com:443", security="wildcard")]
    Approvals(store, tg, 1).poll("sb")
    _, text, buttons = tg.sent[0]
    assert [b[0] for b in buttons[0]] == ["Reject", "Details"]
    assert "openshell rule approve sb --chunk-id c1" in text


def test_approve_button(store, tg, gateway):
    gateway["pending"] = [chunk("c1")]
    a = Approvals(store, tg, 1)
    a.poll("sb")
    req = store.request("sb", "c1")
    assert a.on_button(f"ok:{req.id}") is None
    assert gateway["calls"] == [("approve", "c1")]
    req = store.request("sb", "c1")
    assert req.status == "approved" and req.t2 and req.t3
    assert tg.edits[-1][2] is None and "Approved" in tg.edits[-1][1]
    assert a.on_button(f"ok:{req.id}") == "Already handled"


def test_risky_cannot_be_approved_by_forged_callback(store, tg, gateway):
    gateway["pending"] = [chunk("c1", security="private ip")]
    a = Approvals(store, tg, 1)
    a.poll("sb")
    req = store.request("sb", "c1")
    assert a.on_button(f"ok:{req.id}") == "Approve this one on the machine."
    assert gateway["calls"] == []


def test_reject_with_reason(store, tg, gateway):
    gateway["pending"] = [chunk("c1")]
    a = Approvals(store, tg, 1)
    a.poll("sb")
    rid = store.request("sb", "c1").id
    a.on_button(f"no:{rid}")
    assert "Why?" in tg.edits[-1][1]
    a.on_button(f"why:{rid}:0")
    assert gateway["calls"] == [("reject", "c1", "Too broad, ask for something narrower")]
    assert store.request("sb", "c1").status == "rejected"


def test_duplicates_share_a_card_and_decision(store, tg, gateway):
    gateway["pending"] = [chunk("c1")]
    a = Approvals(store, tg, 1)
    a.poll("sb")
    gateway["pending"].append(chunk("c2"))
    a.poll("sb")
    assert len(tg.sent) == 1
    assert "×2" in tg.edits[-1][1]
    a.on_button(f"ok:{store.request('sb', 'c1').id}")
    assert {c[1] for c in gateway["calls"]} == {"c1", "c2"}


def test_expiry_rejects(store, tg, gateway, monkeypatch):
    gateway["pending"] = [chunk("c1")]
    a = Approvals(store, tg, 1)
    a.poll("sb")
    store.db.execute("UPDATE requests SET t0=t0-?", (approval.EXPIRY + 5,))
    a.poll("sb")
    assert gateway["calls"][0][0] == "reject"
    assert store.request("sb", "c1").status == "expired"


def test_decided_on_machine_updates_card(store, tg, gateway):
    gateway["pending"] = [chunk("c1")]
    a = Approvals(store, tg, 1)
    a.poll("sb")
    gateway["approved"], gateway["pending"] = gateway["pending"], []
    a.poll("sb")
    assert store.request("sb", "c1").status == "approved_elsewhere"
    assert "on the machine" in tg.edits[-1][1]


def test_html_is_escaped(store, tg, gateway):
    c = chunk("c1")
    c.rationale = "<b>click</b> & go"
    gateway["pending"] = [c]
    Approvals(store, tg, 1).poll("sb")
    assert "&lt;b&gt;click&lt;/b&gt; &amp; go" in tg.sent[0][1]


def test_card_shows_granted_programs(store, tg, gateway):
    c = chunk("c1")
    c.binaries = "/usr/bin/python3, /usr/bin/curl"
    c.binary = "/usr/bin/pip"
    gateway["pending"] = [c]
    Approvals(store, tg, 1).poll("sb")
    text = tg.sent[0][1]
    assert "/usr/bin/python3, /usr/bin/curl" in text and "/usr/bin/pip" in text


def test_risky_chunk_never_joins_a_low_risk_card(store, tg, gateway):
    gateway["pending"] = [chunk("c1")]
    a = Approvals(store, tg, 1)
    a.poll("sb")
    bad = chunk("c2")
    bad.trusted = False
    gateway["pending"].append(bad)
    a.poll("sb")
    assert len(tg.sent) == 2
    a.on_button(f"ok:{store.request('sb', 'c1').id}")
    assert gateway["calls"] == [("approve", "c1")]


def test_approve_refuses_when_proposal_changed(store, tg, gateway):
    gateway["pending"] = [chunk("c1")]
    a = Approvals(store, tg, 1)
    a.poll("sb")
    gateway["pending"] = [chunk("c1", endpoints="evil.com:443")]
    assert a.on_button(f"ok:{store.request('sb', 'c1').id}") == "The request changed. Check the new card."
    assert gateway["calls"] == []


def test_failed_decision_keeps_card_open(store, tg, gateway, monkeypatch):
    gateway["pending"] = [chunk("c1")]
    a = Approvals(store, tg, 1)
    a.poll("sb")

    def fail(name, cid):
        raise osh.OpenShellError(["rule", "approve"], 1, "FAILED_PRECONDITION")

    monkeypatch.setattr(osh, "approve", fail)
    edits_before = len(tg.edits)
    assert a.on_button(f"ok:{store.request('sb', 'c1').id}") is not None
    assert store.request("sb", "c1").status == "pending"
    assert len(tg.edits) == edits_before


def test_unsent_card_is_retried_and_still_expires(store, tg, gateway, monkeypatch):
    from openshell_tg.telegram import TelegramError
    gateway["pending"] = [chunk("c1")]
    a = Approvals(store, tg, 1)
    real_send = tg.send
    monkeypatch.setattr(tg, "send", lambda *a, **k: (_ for _ in ()).throw(TelegramError("down")))
    a.poll("sb")
    assert store.request("sb", "c1").message_id is None
    monkeypatch.setattr(tg, "send", real_send)
    a.poll("sb")
    assert store.request("sb", "c1").message_id is not None
    store.db.execute("UPDATE requests SET t0=t0-?, message_id=NULL", (approval.EXPIRY + 5,))
    a.poll("sb")
    assert store.request("sb", "c1").status == "expired"


def test_approve_is_retried_once_when_openshell_reevaluated(store, tg, gateway, monkeypatch):
    gateway["pending"] = [chunk("c1")]
    a = Approvals(store, tg, 1)
    a.poll("sb")
    real = osh.approve
    attempts = []

    def flaky(name, cid):
        attempts.append(cid)
        if len(attempts) == 1:
            raise osh.OpenShellError(["rule", "approve"], 1, "proposal inputs changed; evaluation refreshed, refetch and review again")
        real(name, cid)

    monkeypatch.setattr(osh, "approve", flaky)
    assert a.on_button(f"ok:{store.request('sb', 'c1').id}") is None
    assert attempts == ["c1", "c1"] and store.request("sb", "c1").status == "approved"


def test_no_retry_when_the_reevaluated_proposal_became_risky(store, tg, gateway, monkeypatch):
    gateway["pending"] = [chunk("c1")]
    a = Approvals(store, tg, 1)
    a.poll("sb")
    attempts = []

    def changed(name, cid):
        attempts.append(cid)
        gateway["pending"] = [chunk("c1", security="now flagged")]
        raise osh.OpenShellError(["rule", "approve"], 1, "proposal inputs changed; refetch and review again")

    monkeypatch.setattr(osh, "approve", changed)
    assert a.on_button(f"ok:{store.request('sb', 'c1').id}") is not None
    assert attempts == ["c1"] and store.request("sb", "c1").status == "pending"


def _tap_approve(store, tg, gateway):
    gateway["pending"] = [chunk("c1", endpoints="pypi.org:443", rule="allow_pypi")]
    a = Approvals(store, tg, 1)
    a.poll("sb")
    a.on_button(f"ok:{store.open_requests()[0].id}")
    return a


def test_approval_tells_the_agent_at_once_with_rule_details(store, tg, gateway):
    gateway["running"] = True
    _tap_approve(store, tg, gateway)
    assert len(gateway["told"]) == 1
    text = gateway["told"][0]
    assert "allow_pypi" in text and "pypi.org:443" in text and "/usr/bin/python3" in text
    assert "The agent was told" in tg.edits[-1][1]


def test_stopped_agent_is_not_told(store, tg, gateway):
    _tap_approve(store, tg, gateway)
    assert gateway["told"] == []
    assert "agent" not in tg.edits[-1][1].lower()


def test_rejection_never_tells_the_agent(store, tg, gateway):
    gateway["running"] = True
    gateway["pending"] = [chunk("c1")]
    a = Approvals(store, tg, 1)
    a.poll("sb")
    rid = store.open_requests()[0].id
    a.on_button(f"no:{rid}")
    a.on_button(f"why:{rid}:0")
    assert gateway["told"] == []

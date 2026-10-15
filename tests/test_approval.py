import time

import pytest

from openshell_tg import approval, osh
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
    store.db.execute("UPDATE requests SET t1=t1-?", (approval.EXPIRY + 5,))
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

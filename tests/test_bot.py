import pytest

from openshell_tg import osh
from openshell_tg import sandbox as sb
from openshell_tg.bot import Bot


@pytest.fixture
def bot(store, tg, monkeypatch):
    monkeypatch.setattr(osh, "rules", lambda name, status="pending": [])
    return Bot(tg, store, chat_id=10, user_id=20)


def message(text, chat=10, user=20):
    return {"update_id": 1, "message": {"chat": {"id": chat, "type": "private"}, "from": {"id": user}, "text": text}}


def test_ignores_strangers(bot, tg):
    bot.handle(message("/help", chat=99, user=99))
    bot.handle(message("/help", chat=10, user=99))
    assert tg.sent == []


def test_help(bot, tg):
    bot.handle(message("/help"))
    assert "/list" in tg.sent[0][1]


def test_unknown_text_shows_help(bot, tg):
    bot.handle(message("hello"))
    assert "openshell-tg" in tg.sent[0][1]


def test_callback_from_stranger_ignored(bot, tg, store, monkeypatch):
    store.save_sandbox("otg-a-1", "/a", "codex", 3600)
    stopped = []
    monkeypatch.setattr(osh, "stop", lambda name: stopped.append(name))
    bot.handle({"update_id": 2, "callback_query": {"id": "q", "from": {"id": 99}, "data": "stop:otg-a-1"}})
    assert stopped == []
    bot.handle({"update_id": 3, "callback_query": {"id": "q", "from": {"id": 20}, "data": "stop:otg-a-1",
                                                    "message": {"message_id": 5}}})
    assert stopped == ["otg-a-1"]


def _rules_bot(bot, store, monkeypatch, count):
    store.save_sandbox("otg-a-1", "/a", "codex", 3600)
    policies = {f"allow_{i:02d}": {"endpoints": [{"host": f"site{i:02d}.example", "port": 443}]}
                for i in range(1, count + 1)}
    removed = []

    def remove(name, rule):
        removed.append((name, rule))
        policies.pop(rule)

    monkeypatch.setattr(osh, "network_policies", lambda name: dict(policies))
    monkeypatch.setattr(osh, "remove_rule", remove)
    return policies, removed


def _find(buttons, label_part):
    return next(data for row in buttons for label, data in row if label_part in label)


def test_rules_remove_needs_confirmation(bot, tg, store, monkeypatch):
    _, removed = _rules_bot(bot, store, monkeypatch, 3)
    bot.handle(message("/rules"))
    text, buttons = tg.sent[0][1], tg.sent[0][2]
    assert "1. <code>site01.example:443</code>" in text and "(3)" in text
    first = _find(buttons, "1")
    assert bot.on_button(first, 50) is None          # asks first
    assert removed == []
    msg_id, confirm_text, confirm_buttons = tg.edits[-1]
    assert msg_id == 50 and "site01.example" in confirm_text
    assert bot.on_button(_find(confirm_buttons, "Remove"), 50) == "Removed"
    assert removed == [("otg-a-1", "allow_01")]
    assert "site01.example" not in tg.edits[-1][1] and "site02.example" in tg.edits[-1][1]
    assert bot.on_button(first, 50) == "Already handled"


def test_rules_cancel_returns_to_list(bot, tg, store, monkeypatch):
    _, removed = _rules_bot(bot, store, monkeypatch, 2)
    bot.handle(message("/rules"))
    bot.on_button(_find(tg.sent[0][2], "1"), 50)
    bot.on_button(_find(tg.edits[-1][2], "Cancel"), 50)
    assert removed == [] and "site01.example" in tg.edits[-1][1]


def test_rules_are_paged(bot, tg, store, monkeypatch):
    _rules_bot(bot, store, monkeypatch, 12)
    bot.handle(message("/rules"))
    text, buttons = tg.sent[0][1], tg.sent[0][2]
    assert "Page 1 of 2" in text and "site08.example" in text and "site09.example" not in text
    assert all(len(row) <= 4 for row in buttons)
    assert max(int(label.split()[-1]) for row in buttons[:-1] for label, _ in row) == 8
    bot.on_button(_find(buttons, "▶"), 50)
    page2 = tg.edits[-1][1]
    assert "Page 2 of 2" in page2 and "9. <code>site09.example" in page2 and "site08.example" not in page2


def test_reboot_notice(bot, tg, store, monkeypatch):
    store.save_sandbox("otg-a-1", "/a", "codex", 3600)
    store.put("boot", "1")
    store.put("alive:otg-a-1", "1")
    store.put("welcomed", "1")
    monkeypatch.setattr("openshell_tg.bot.boot_time", lambda: "2")
    bot.start()
    assert any("restarted" in text for _, text, _ in tg.sent)


def test_idle_stop(bot, store, monkeypatch):
    store.save_sandbox("otg-a-1", "/a", "codex", 60)
    store.db.execute("UPDATE sandboxes SET last_used=last_used-120")
    monkeypatch.setattr("openshell_tg.sandbox.state", lambda name: "Ready")
    monkeypatch.setattr("openshell_tg.sandbox.agent_running", lambda name: False)
    stopped = []
    monkeypatch.setattr(osh, "stop", lambda name: stopped.append(name))
    bot.idle_check()
    assert stopped == ["otg-a-1"]


def test_running_agent_is_not_stopped(bot, store, monkeypatch):
    store.save_sandbox("otg-a-1", "/a", "codex", 60)
    store.db.execute("UPDATE sandboxes SET last_used=last_used-120")
    monkeypatch.setattr("openshell_tg.sandbox.state", lambda name: "Ready")
    monkeypatch.setattr("openshell_tg.sandbox.agent_running", lambda name: True)
    monkeypatch.setattr(osh, "stop", lambda name: pytest.fail("stopped a working agent"))
    bot.idle_check()


def test_reminder_for_deleted_folder(bot, tg, store, tmp_path):
    store.save_sandbox("otg-a-1", str(tmp_path / "gone"), "codex", 60)
    bot.reminders()
    bot.reminders()
    assert len(tg.sent) == 1 and "deleted" in tg.sent[0][1]


def test_tick_survives_errors(bot, store, monkeypatch):
    store.save_sandbox("otg-a-1", "/a", "codex", 60)

    def boom(*a, **k):
        raise RuntimeError("openshell missing")

    monkeypatch.setattr(bot.approvals, "poll", boom)
    monkeypatch.setattr(bot, "idle_check", boom)
    bot.tick()  # must not raise


def _box(store, name="sb1"):
    store.save_sandbox(name, "/tmp", "claude", 0)


def test_quiet_agent_after_work_is_reported_once(bot, tg, store, monkeypatch):
    _box(store)
    quiet = {"s": 5}
    monkeypatch.setattr(sb, "state", lambda name: "Ready")
    monkeypatch.setattr(sb, "agent_idle_seconds", lambda name: quiet["s"])
    bot.watch_agent("sb1")
    assert tg.sent == []
    quiet["s"] = 60
    bot.watch_agent("sb1")
    assert tg.sent == []
    quiet["s"] = 150
    bot.watch_agent("sb1")
    bot.watch_agent("sb1")
    assert len(tg.sent) == 1 and "/screen" in tg.sent[0][1]
    quiet["s"] = 3
    bot.watch_agent("sb1")
    quiet["s"] = 200
    bot.watch_agent("sb1")
    assert len(tg.sent) == 2


def test_screen_shows_escaped_lines(bot, tg, store, monkeypatch):
    _box(store)
    monkeypatch.setattr(sb, "screen", lambda name: "Do you want to proceed? <y/n>")
    bot.handle(message("/screen"))
    assert "<pre>Do you want to proceed? &lt;y/n&gt;</pre>" in tg.sent[0][1]


def test_screen_when_agent_not_running(bot, tg, store, monkeypatch):
    _box(store)
    monkeypatch.setattr(sb, "screen", lambda name: None)
    bot.handle(message("/screen"))
    assert "No agent is running" in tg.sent[0][1]


def test_card_with_a_long_address_list_cannot_be_approved(store, tg, monkeypatch):
    from openshell_tg import approval
    long_list = "pypi.org:443, " + "x" * 400 + ", evil.example:443"
    chunk = osh.Chunk(id="c9", status="pending", rule="allow_x", binary="/usr/bin/python3", endpoints=long_list)
    monkeypatch.setattr(osh, "rules", lambda name, status="pending": [chunk] if status == "pending" else [])
    approval.Approvals(store, tg, 10).poll("sb1")
    _, text, buttons = tg.sent[0]
    assert "too long" in text and [b[0] for b in buttons[0]] == ["Reject", "Details"]


def test_idle_check_keeps_a_sandbox_that_up_is_still_creating(bot, store, monkeypatch):
    _box(store)
    monkeypatch.setattr(sb, "state", lambda name: None)
    monkeypatch.setattr(osh, "sandboxes", lambda: [])
    bot.idle_check()
    assert store.sandbox("sb1") is not None

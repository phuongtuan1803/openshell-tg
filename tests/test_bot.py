import pytest

from openshell_tg import osh
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


def test_rules_remove_button(bot, tg, store, monkeypatch):
    store.save_sandbox("otg-a-1", "/a", "codex", 3600)
    monkeypatch.setattr(osh, "network_policies", lambda name: {
        "allow_pypi": {"endpoints": [{"host": "pypi.org", "port": 443}]}})
    removed = []
    monkeypatch.setattr(osh, "remove_rule", lambda name, rule: removed.append((name, rule)))
    bot.handle(message("/rules"))
    data = tg.sent[0][2][0][0][1]
    assert bot.on_button(data, None) == "Removed"
    assert removed == [("otg-a-1", "allow_pypi")]
    assert bot.on_button(data, None) == "Already handled"


def test_rules_list_is_redrawn_after_removal(bot, tg, store, monkeypatch):
    store.save_sandbox("otg-a-1", "/a", "codex", 3600)
    policies = {"allow_pypi": {"endpoints": [{"host": "pypi.org", "port": 443}]},
                "allow_npm": {"endpoints": [{"host": "registry.npmjs.org", "port": 443}]}}
    monkeypatch.setattr(osh, "network_policies", lambda name: dict(policies))
    monkeypatch.setattr(osh, "remove_rule", lambda name, rule: policies.pop(rule))
    bot.handle(message("/rules"))
    first = next(data for row in tg.sent[0][2] for label, data in row if "pypi.org" in label)
    assert "pypi.org" in tg.sent[0][1] and "registry.npmjs.org" in tg.sent[0][1]
    assert bot.on_button(first, 77) == "Removed"
    msg_id, text, buttons = tg.edits[-1]
    assert msg_id == 77 and "pypi.org" not in text and "registry.npmjs.org" in text
    assert len(buttons) == 1


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

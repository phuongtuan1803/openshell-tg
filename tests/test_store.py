import time


def test_stats(store):
    now = time.time()
    for i, wait in enumerate([10, 20, 30, 400]):
        store.upsert_request("sb", f"c{i}", "s", "{}", False, now - 1000)
        store.set_message("sb", f"c{i}", i)
        store.db.execute("UPDATE requests SET t1=?, t2=?, status='approved' WHERE chunk_id=?",
                         (now - 500, now - 500 + wait, f"c{i}"))
    stats = store.stats()
    assert stats["total"] == 4
    assert stats["by_status"] == {"approved": 4}
    assert stats["median"] == 20
    assert stats["p90"] == 400


def test_sandbox_roundtrip(store):
    store.save_sandbox("otg-a-1", "/a", "codex", 3600)
    box = store.sandbox("otg-a-1")
    assert box.folder == "/a" and store.sandbox_by_id(box.id).name == "otg-a-1"
    store.mark_stopped("otg-a-1")
    store.save_sandbox("otg-a-1", "/a", "claude", 3600)
    box = store.sandbox("otg-a-1")
    assert box.agent == "claude" and box.stopped_at is None

"""Тести сховища на тимчасовій базі в пам'яті (справжня bot.db не чіпається)."""
import pytest

from storage import Storage


@pytest.fixture
def db():
    return Storage(":memory:")


def test_add_and_list_search(db):
    search_id = db.add_search("Ford Edge Київ", "https://auto.ria.com/search/?a=1")
    searches = db.list_searches()
    assert len(searches) == 1
    assert searches[0]["id"] == search_id
    assert searches[0]["name"] == "Ford Edge Київ"
    assert searches[0]["initialized"] == 0


def test_search_without_name_gets_default(db):
    search_id = db.add_search(None, "https://auto.ria.com/search/?a=1")
    assert db.list_searches()[0]["name"] == f"Пошук №{search_id}"


def test_rename_search(db):
    sid = db.add_search(None, "u")
    assert db.rename_search(sid, "BMW X5")
    assert db.list_searches()[0]["name"] == "BMW X5"
    assert not db.rename_search(999, "X")


def test_sent_statistics(db):
    a = db.add_search("A", "u1")
    b = db.add_search("B", "u2")
    db.log_sent("1", [a, b])  # одне авто з двох пошуків
    db.log_sent("2", [a])
    assert db.count_sent() == 2  # різних авто, а не повідомлень по пошуках
    assert db.sent_per_search() == {a: 2, b: 1}
    assert db.count_sent("2999-01-01") == 0
    assert db.first_sent_at() is not None


def test_filter_new_and_mark_seen(db):
    sid = db.add_search("A", "u")
    assert db.filter_new(sid, ["1", "2"]) == ["1", "2"]
    db.mark_seen(sid, ["1", "2"])
    assert db.filter_new(sid, ["3", "1", "2"]) == ["3"]


def test_mark_seen_twice_is_safe(db):
    sid = db.add_search("A", "u")
    db.mark_seen(sid, ["1"])
    db.mark_seen(sid, ["1"])  # INSERT OR IGNORE: без помилки
    assert db.filter_new(sid, ["1"]) == []


def test_seen_is_per_search(db):
    a = db.add_search("A", "u1")
    b = db.add_search("B", "u2")
    db.mark_seen(a, ["1"])
    assert db.filter_new(b, ["1"]) == ["1"]
    assert db.seen_anywhere("1")
    assert not db.seen_anywhere("2")


def test_remove_search_also_forgets_seen(db):
    sid = db.add_search("A", "u")
    db.mark_seen(sid, ["1"])
    assert db.remove_search(sid) is True
    assert db.list_searches() == []
    assert not db.seen_anywhere("1")


def test_remove_missing_search(db):
    assert db.remove_search(999) is False


def test_mark_initialized(db):
    sid = db.add_search("A", "u")
    db.mark_initialized(sid)
    assert db.list_searches()[0]["initialized"] == 1


def test_pause(db):
    assert db.is_paused() is False  # за замовчуванням не на паузі
    db.set_paused(True)
    assert db.is_paused() is True
    db.set_paused(False)
    assert db.is_paused() is False


def test_data_survives_reopen(tmp_path):
    """Пошуки і бачені оголошення переживають перезапуск бота."""
    path = tmp_path / "bot.db"
    first = Storage(str(path))
    sid = first.add_search("A", "u")
    first.mark_seen(sid, ["1"])
    first.set_paused(True)
    first.conn.close()

    second = Storage(str(path))
    assert [s["name"] for s in second.list_searches()] == ["A"]
    assert second.filter_new(sid, ["1"]) == []
    assert second.is_paused() is True
    second.conn.close()

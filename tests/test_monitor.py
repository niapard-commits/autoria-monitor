"""Тести логіки «що нове» і тексту повідомлення."""
import pytest

from monitor import find_new, format_listing, format_price, group_new, short_subtitle
from storage import Storage


@pytest.fixture
def db():
    return Storage(":memory:")


def car(listing_id, **fields):
    data = {
        "id": str(listing_id), "title": f"Lexus {listing_id}", "subtitle": "",
        "price_usd": 12000, "price_uah": 500000, "mileage": "", "fuel": "",
        "city": "Київ", "gearbox": "", "published": "", "link": "https://auto.ria.com/x",
    }
    data.update(fields)
    return data


def get(db, search_id):
    return next(s for s in db.list_searches() if s["id"] == search_id)


def mark_sent(db, found):
    """Те, що робить бот після успішної відправки."""
    for listing_id, item in found.items():
        for search in item["searches"]:
            db.mark_seen(search["id"], [listing_id])


# ---------- find_new ----------

def test_first_check_is_silent(db):
    sid = db.add_search("A", "u")
    assert find_new(db, get(db, sid), [car(1), car(2)]) == []
    assert get(db, sid)["initialized"] == 1


def test_only_unseen_listings_are_new(db):
    sid = db.add_search("A", "u")
    find_new(db, get(db, sid), [car(1), car(2)])
    new = find_new(db, get(db, sid), [car(3), car(1), car(2)])
    assert [c["id"] for c in new] == ["3"]


def test_bumped_old_listing_with_small_id_is_not_new(db):
    """Підняте старе оголошення (маленький ID) нагорі не вважається новим."""
    sid = db.add_search("A", "u")
    find_new(db, get(db, sid), [car(40000001), car(35265626)])
    new = find_new(db, get(db, sid), [car(35265626), car(40000001)])
    assert new == []


# ---------- group_new: пошуки, що перетинаються ----------

def test_overlapping_searches_give_one_message(db):
    a = db.add_search("Всі Lexus", "u1")
    b = db.add_search("Lexus хетчбек", "u2")
    group_new(db, [(get(db, a), [car(1)]), (get(db, b), [car(1)])])  # перша перевірка

    found = group_new(db, [
        (get(db, a), [car(3), car(4), car(1)]),
        (get(db, b), [car(3), car(1)]),
    ])
    assert {k: [s["name"] for s in v["searches"]] for k, v in found.items()} == {
        "3": ["Всі Lexus", "Lexus хетчбек"],
        "4": ["Всі Lexus"],
    }


def test_new_listings_go_from_older_to_newer(db):
    sid = db.add_search("A", "u")
    group_new(db, [(get(db, sid), [car(1)])])
    found = group_new(db, [(get(db, sid), [car(3), car(2), car(1)])])  # на сайті новіші вгорі
    assert list(found) == ["2", "3"]


def test_listing_sent_earlier_from_other_search_is_not_repeated(db):
    """Другий пошук «наздогнав» авто пізніше: дубля немає."""
    a = db.add_search("A", "u1")
    b = db.add_search("B", "u2")
    group_new(db, [(get(db, a), []), (get(db, b), [])])

    first = group_new(db, [(get(db, a), [car(5)]), (get(db, b), [])])
    assert list(first) == ["5"]
    mark_sent(db, first)

    later = group_new(db, [(get(db, a), [car(5)]), (get(db, b), [car(5)])])
    assert later == {}
    assert db.filter_new(b, ["5"]) == []  # і для B тепер позначене баченим


def test_unsent_listing_is_found_again_next_time(db):
    """Якщо відправка не вдалася (бачене не позначили), авто прийде наступного разу."""
    sid = db.add_search("A", "u")
    group_new(db, [(get(db, sid), [])])
    assert list(group_new(db, [(get(db, sid), [car(7)])])) == ["7"]
    assert list(group_new(db, [(get(db, sid), [car(7)])])) == ["7"]


# ---------- текст повідомлення ----------

def test_message_lists_all_search_names():
    text = format_listing(["Всі Lexus", "Lexus хетчбек"], car(3))
    assert text.startswith("🔔 <b>Всі Lexus · Lexus хетчбек</b>")


def test_message_escapes_html():
    """Символи < > & у назві не ламають повідомлення Telegram."""
    text = format_listing(["<A&B>"], car(1, title="BMW <M4> & Co"))
    assert "&lt;A&amp;B&gt;" in text
    assert "BMW &lt;M4&gt; &amp; Co" in text
    assert "<M4>" not in text


def test_message_skips_empty_details():
    text = format_listing(["A"], car(1, city="Київ", mileage="", gearbox="Автомат", fuel=""))
    assert "📍 Київ · Автомат" in text.split("\n")
    assert "🕒" not in text  # часу публікації немає, рядок не показуємо


def test_message_is_short():
    """Підпис до фото: 5 рядків, без посилання (воно в кнопці)."""
    text = format_listing(["A"], car(1, city="Київ", published="Час назад"))
    assert len(text.split("\n")) == 5
    assert "href" not in text


@pytest.mark.parametrize("value, expected", [
    (89999, "89 999"),
    (4198453, "4 198 453"),
    (None, "—"),
    (0, "—"),
])
def test_format_price(value, expected):
    assert format_price(value) == expected


@pytest.mark.parametrize("subtitle, expected", [
    ("X243 • 300 66.5 kWh (228 к.с.) 4Matic • Base", "X243"),
    ("G82 (FL)", "G82 (FL)"),
    ("F34 • 2.0 TDI", "F34 • 2.0 TDI"),
    ("", ""),
])
def test_long_subtitle_is_shortened(subtitle, expected):
    assert short_subtitle(subtitle) == expected

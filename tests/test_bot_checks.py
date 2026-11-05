"""Тести перевірки в боті: збої сайту і Telegram.

Замість справжніх AUTO.RIA і Telegram тут «підробки» (FakeSite, FakeTelegram),
якими можна керувати: «вимкнути» сайт або інтернет і подивитися, що зробить бот.
"""
import asyncio
import html
from types import SimpleNamespace

import pytest
import requests
from telegram.error import BadRequest, NetworkError

import bot
from autoria_parser import ParserError
from storage import Storage


class FakeTelegram:
    def __init__(self):
        self.sent = []
        self.photos = []  # які фото надіслали
        self.bad_photos = set()  # фото, які Telegram «не прийме»
        self.down = False  # True означає «немає інтернету»

    async def send_message(self, chat_id, text, **kwargs):
        if self.down:
            raise NetworkError("немає інтернету")
        self.sent.append(text)

    async def send_photo(self, chat_id, photo, caption, **kwargs):
        if self.down:
            raise NetworkError("немає інтернету")
        if photo in self.bad_photos:
            raise BadRequest("Wrong file identifier/http url specified")
        self.photos.append(photo)
        self.sent.append(caption)


class FakeSite:
    def __init__(self):
        self.pages = {}  # посилання -> оголошення на сторінці
        self.down = False  # True означає «сайт не відповідає»
        self.broken = set()  # окремі сторінки, які не відповідають
        self.requests = []  # які сторінки бот завантажував
        self.error = None  # конкретна помилка, яку «поверне» сайт

    def fetch_search(self, url):
        self.requests.append(url)
        if self.error:
            raise self.error
        if self.down or url in self.broken:
            raise requests.ConnectionError("сайт не відповідає")
        return self.pages.get(url, []), None


def car(listing_id, title=None, photo=""):
    return {
        "photo": photo,
        "id": str(listing_id), "title": title or f"Авто {listing_id}", "subtitle": "",
        "price_usd": 10000, "price_uah": 466500, "mileage": "", "fuel": "",
        "city": "Київ", "gearbox": "", "published": "", "link": "https://auto.ria.com/x",
    }


@pytest.fixture
def env(monkeypatch):
    db = Storage(":memory:")
    site = FakeSite()
    telegram = FakeTelegram()
    monkeypatch.setattr(bot, "db", db)
    monkeypatch.setattr(bot, "fetch_search", site.fetch_search)

    async def no_sleep(*args):
        pass

    monkeypatch.setattr(bot.asyncio, "sleep", no_sleep)  # тести не чекають паузи
    context = SimpleNamespace(bot=telegram, bot_data={})
    return SimpleNamespace(db=db, site=site, tg=telegram, context=context)


def check(env):
    return asyncio.run(bot.check_all(env.context))


# ---------- звичайна робота ----------

def test_new_listing_is_sent(env):
    env.db.add_search("BMW", "u1")
    env.site.pages["u1"] = [car(1)]
    assert check(env) == 0  # перша перевірка мовчки

    env.site.pages["u1"] = [car(2, "BMW X5"), car(1)]
    assert check(env) == 1
    assert "BMW X5" in env.tg.sent[-1]
    assert check(env) == 0  # вдруге те саме не надсилається


def test_listing_with_photo_is_sent_as_card(env):
    env.db.add_search("BMW", "u1")
    env.site.pages["u1"] = [car(1)]
    check(env)
    env.site.pages["u1"] = [car(2, "BMW X5", photo="https://cdn/x.jpg"), car(1)]
    assert check(env) == 1
    assert env.tg.photos == ["https://cdn/x.jpg"]
    assert "BMW X5" in env.tg.sent[-1]


def test_bad_photo_falls_back_to_text(env):
    """Telegram не прийняв фото: авто все одно приходить, текстом."""
    env.db.add_search("BMW", "u1")
    env.site.pages["u1"] = [car(1)]
    check(env)
    env.tg.bad_photos.add("https://cdn/broken.jpg")
    env.site.pages["u1"] = [car(2, "BMW X5", photo="https://cdn/broken.jpg"), car(1)]
    assert check(env) == 1
    assert env.tg.photos == []
    assert "BMW X5" in env.tg.sent[-1]


# ---------- статистика і назви ----------

def test_stats_count_sent_listings(env):
    env.db.add_search("BMW", "u1")
    env.site.pages["u1"] = [car(1)]
    check(env)
    env.site.pages["u1"] = [car(3), car(2), car(1)]
    check(env)
    text = bot.render_stats(env.context.bot_data)
    assert "сьогодні 2" in text
    assert "• BMW: 2" in text
    assert "Остання перевірка" in text
    assert "без помилок" in text


def test_stats_show_broken_search(env):
    env.db.add_search("BMW", "u1")
    check(env)
    env.site.down = True
    check(env)
    assert "Зараз не перевіряються: «BMW»" in bot.render_stats(env.context.bot_data)


def test_unique_name(env):
    env.db.add_search("BMW", "u1")
    env.db.add_search("BMW (2)", "u2")
    assert bot.unique_name("BMW") == "BMW (3)"
    assert bot.unique_name("Audi") == "Audi"
    assert bot.unique_name(None) is None


def test_overlapping_searches_give_one_message(env):
    env.db.add_search("Всі Lexus", "u1")
    env.db.add_search("Lexus хетчбек", "u2")
    env.site.pages = {"u1": [car(1)], "u2": [car(1)]}
    check(env)

    env.site.pages = {"u1": [car(3), car(1)], "u2": [car(3), car(1)]}
    assert check(env) == 1
    assert "Всі Lexus · Lexus хетчбек" in env.tg.sent[-1]


def test_pause_skips_scheduled_check(env):
    env.db.add_search("BMW", "u1")
    env.site.pages["u1"] = [car(1)]
    check(env)
    env.site.pages["u1"] = [car(2), car(1)]
    env.db.set_paused(True)
    asyncio.run(bot.scheduled_check(env.context))
    assert env.tg.sent == []


# ---------- бот довго не працював: нових більше, ніж на одній сторінці ----------

def page(start, count=20):
    """Сторінка з count авто, ID від start вниз (новіші вгорі)."""
    return [car(i) for i in range(start, start - count, -1)]


def test_after_downtime_next_pages_are_checked(env):
    env.db.add_search("BMW", "u1")
    env.site.pages["u1"] = page(100)
    check(env)  # перша перевірка: запам'ятали 100..81

    # за час простою з'явилося 30 нових: 20 на 1-й сторінці, 10 на 2-й
    env.site.pages = {"u1": page(130), "u1?page=1": page(110)}
    assert check(env) == 30
    assert "Авто 101" in env.tg.sent[0]  # від старішого до новішого
    assert "Авто 130" in env.tg.sent[-1]


def test_no_extra_pages_in_normal_work(env):
    env.db.add_search("BMW", "u1")
    env.site.pages["u1"] = page(100)
    check(env)

    env.site.pages = {"u1": page(102), "u1?page=1": page(82)}
    env.site.requests.clear()
    assert check(env) == 2
    assert env.site.requests == ["u1"]  # на 1-й сторінці є старі авто: 2-га не потрібна


def test_first_check_reads_only_first_page(env):
    env.db.add_search("BMW", "u1")
    env.site.pages = {"u1": page(100), "u1?page=1": page(80)}
    check(env)
    assert env.site.requests == ["u1"]


def test_pages_limit(env):
    env.db.add_search("BMW", "u1")
    env.site.pages["u1"] = page(100)
    check(env)

    # нових дуже багато: усі сторінки з новими
    env.site.pages = {"u1": page(1000)}
    env.site.pages.update({f"u1?page={n}": page(1000 - 20 * n) for n in range(1, 10)})
    env.site.requests.clear()
    assert check(env) == 20 * bot.MAX_PAGES
    assert len(env.site.requests) == bot.MAX_PAGES


def test_broken_next_page_keeps_first_page(env):
    env.db.add_search("BMW", "u1")
    env.site.pages["u1"] = page(100)
    check(env)

    env.site.pages = {"u1": page(130)}
    env.site.broken.add("u1?page=1")
    assert check(env) == 20  # 1-ша сторінка надіслана, помилку 2-ї не рахуємо збоєм
    assert env.tg.sent and "Не вдається" not in env.tg.sent[0]


# ---------- сайт не відповідає ----------

def test_site_down_warns_once_after_3_failures_then_recovers(env):
    env.db.add_search("BMW", "u1")
    check(env)

    env.site.down = True
    check(env)
    check(env)
    assert env.tg.sent == []  # дві помилки: це може бути випадковість, мовчимо

    check(env)
    assert len(env.tg.sent) == 1
    assert "Не вдається перевірити: «BMW»" in env.tg.sent[0]

    check(env)
    check(env)
    assert len(env.tg.sent) == 1  # не спамимо кожні 5 хвилин

    env.site.down = False
    check(env)
    assert env.tg.sent[-1] == "✅ Знову перевіряються: «BMW»"


def test_one_warning_for_all_broken_searches(env):
    env.db.add_search("BMW", "u1")
    env.db.add_search("Ford", "u2")
    check(env)

    env.site.down = True
    for _ in range(3):
        check(env)
    assert len(env.tg.sent) == 1
    assert "«BMW», «Ford»" in env.tg.sent[0]


def http_error(code):
    response = requests.Response()
    response.status_code = code
    return requests.HTTPError(response=response)


@pytest.mark.parametrize("error, reason", [
    (requests.ConnectionError(), "немає з'єднання з сайтом"),
    (requests.Timeout(), "довго не відповідає"),
    (http_error(403), "блокує запити (код 403)"),
    (http_error(404), "не знайдено (код 404)"),
    (http_error(503), "збій на боці сайту (код 503)"),
    (ParserError("сайт показує «2 650 предложений», а я не знайшов жодного"),
     "змінилась сторінка сайту"),
])
def test_warning_explains_the_reason(env, error, reason):
    env.db.add_search("BMW", "u1")
    check(env)
    env.site.error = error
    for _ in range(3):
        check(env)
    assert reason in html.unescape(env.tg.sent[0])  # Telegram покаже без &#x27;


def test_short_failure_is_not_reported(env):
    env.db.add_search("BMW", "u1")
    check(env)
    env.site.down = True
    check(env)
    env.site.down = False
    check(env)
    assert env.tg.sent == []  # один збій і знову працює: клієнта не турбуємо


# ---------- немає інтернету (Telegram теж недоступний) ----------

def test_warning_is_retried_when_telegram_was_down(env):
    """Попередження не дійшло, бо не було інтернету: надішлемо, щойно Telegram оживе."""
    env.db.add_search("BMW", "u1")
    check(env)

    env.site.down = True
    env.tg.down = True
    for _ in range(4):
        check(env)
    assert env.tg.sent == []

    env.tg.down = False  # Telegram ожив, сайт ще ні
    check(env)
    assert len(env.tg.sent) == 1
    assert "Не вдається перевірити" in env.tg.sent[0]


def test_listing_is_not_lost_when_telegram_fails(env):
    """Нове авто знайшли, але Telegram недоступний: воно прийде наступного разу."""
    env.db.add_search("BMW", "u1")
    env.site.pages["u1"] = [car(1)]
    check(env)

    env.site.pages["u1"] = [car(2, "BMW X5"), car(1)]
    env.tg.down = True
    with pytest.raises(NetworkError):
        check(env)

    env.tg.down = False
    assert check(env) == 1
    assert "BMW X5" in env.tg.sent[-1]

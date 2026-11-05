"""Тести парсера на збережених сторінках AUTO.RIA (без інтернету)."""
import re
from pathlib import Path

import pytest

from autoria_parser import (
    ParserError,
    check_health,
    extract_state,
    listings_from_state,
    normalize_search_url,
    page_url,
    photo_url,
    parse_card,
    parse_listings,
    parse_total,
    suggest_name,
    total_count,
)

FIXTURES = Path(__file__).parent / "fixtures"


def load(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def naive_ids(html):
    """Як шукав би «наївний» парсер: усі посилання на оголошення підряд."""
    return set(re.findall(r"auto_[\w-]+?_(\d+)\.html", html))


# ---------- сторінка з результатами ----------

def test_results_page_has_20_real_listings():
    listings = parse_listings(load("search_results.html"))
    assert len(listings) == 20


def test_results_page_first_listing_fields():
    first = parse_listings(load("search_results.html"))[0]
    assert first == {
        "photo": "https://cdn.riastatic.com/photosnew/auto/photo/bmw_m4__656296235fx.jpg",
        "id": "40379814",
        "title": "BMW M4 2024",
        "subtitle": "G82 (FL)",
        "price_usd": 89999,
        "price_uah": 4198453,
        "mileage": "10 тыс. км",
        "fuel": "Бензин, 3 л",
        "city": "Киев",
        "gearbox": "Автомат",
        "published": "Час назад",
        "link": "https://auto.ria.com/auto_bmw_m4_40379814.html",
    }


def test_results_page_excludes_offer_of_the_day():
    """«Предложение дня» (BMW X6, 40042924) є на сторінці, але не в результатах."""
    html = load("search_results.html")
    ids = {car["id"] for car in parse_listings(html)}
    assert "40042924" in naive_ids(html)
    assert "40042924" not in ids


def test_results_page_has_no_duplicates():
    ids = [car["id"] for car in parse_listings(load("search_results.html"))]
    assert len(ids) == len(set(ids))


def test_results_page_total():
    state = extract_state(load("search_results.html"))
    # сайт розділяє тисячі нерозривним пробілом (\xa0), не звичайним
    assert parse_total(state) == "2\xa0650 предложений"


# ---------- сторінка з нулем результатів ----------

def test_empty_page_ignores_similar_listings():
    """При «0 предложений» сайт показує «Похожие предложения»: їх не беремо."""
    html = load("search_empty.html")
    assert len(naive_ids(html)) > 0  # «схожі» на сторінці є
    assert parse_listings(html) == []


def test_empty_page_total():
    state = extract_state(load("search_empty.html"))
    assert parse_total(state) == "0 предложений"


# ---------- неочікувані дані ----------

def test_page_without_state_raises_parser_error():
    with pytest.raises(ParserError):
        extract_state("<html><body>Сайт змінився</body></html>")


def test_state_without_items_block_gives_empty_list():
    assert listings_from_state({"page": {"templates": []}}) == []


def test_card_with_missing_fields_does_not_crash():
    car = parse_card({"id": 123})
    assert car["id"] == "123"
    assert car["title"] == ""
    assert car["price_usd"] is None
    assert car["city"] == ""
    assert car["photo"] == ""


def test_photo_is_converted_to_jpg():
    """Фото на сайті у webp, а Telegram надійніше приймає jpg (той самий файл є і в jpg)."""
    data = {"photos": [{"src": "https://cdn.riastatic.com/a/x__1fx.webp",
                        "formats": {"middle": "https://cdn.riastatic.com/a/x__1fx.webp"}}]}
    assert photo_url(data) == "https://cdn.riastatic.com/a/x__1fx.jpg"
    assert photo_url({"photos": [{"src": ""}]}) == ""


# ---------- назва пошуку з марки ----------

def titled(*titles):
    return [{"title": t} for t in titles]


def test_name_from_fixture_page():
    """На збереженій сторінці лише BMW, у фільтрі роки 2012–2024."""
    html = load("search_results.html")
    url = "https://auto.ria.com/search/?all[0].any[0].brand=9&all[0].any[0].year[0]=2012&all[0].any[0].year[1]=2024"
    assert suggest_name(url, parse_listings(html)) == "BMW 2012–2024"


@pytest.mark.parametrize("titles, url, name", [
    (["BMW X5 2019", "BMW X5 2012"], "u", "BMW X5"),
    (["Land Rover Defender 2020", "Land Rover Discovery 2018"], "u", "Land Rover"),
    (["Ford Edge 2016"], "u?year[0]=2015", "Ford Edge від 2015"),
    (["Ford Edge 2016"], "u?year[1]=2018", "Ford Edge до 2018"),
    (["Ford Edge 2016"], "u?year[0]=2016&year[1]=2016", "Ford Edge 2016"),
    (["BMW X5 2019", "Audi A4 2019"], "u", None),  # різні марки: назву не вигадуємо
    ([], "u", None),
])
def test_suggest_name(titles, url, name):
    assert suggest_name(url, titled(*titles)) == name


# ---------- «тиха» поломка: сторінка є, оголошень 0 ----------

def test_total_count():
    assert total_count("2\xa0650 предложений") == 2650
    assert total_count("0 предложений") == 0
    assert total_count(None) is None


def test_healthy_pages_pass():
    for name in ("search_results.html", "search_empty.html"):
        state = extract_state(load(name))
        check_health(listings_from_state(state), parse_total(state))


def test_renamed_block_is_reported():
    """Сайт перейменував блок items: лічильник каже 2 650, а оголошень 0."""
    html = load("search_results.html").replace('"id":"items"', '"id":"results"')
    state = extract_state(html)
    assert listings_from_state(state) == []
    with pytest.raises(ParserError, match="2\xa0650 предложений"):
        check_health(listings_from_state(state), parse_total(state))


def test_page_without_listings_and_counter_is_reported():
    with pytest.raises(ParserError, match="ні лічильника"):
        check_health([], None)


# ---------- перевірка посилань від клієнта ----------

SEARCH = "https://auto.ria.com/search/?search_type=1&category=1&all[0].any[0].brand=24"


def test_url_gets_sort_by_date():
    assert normalize_search_url(SEARCH).endswith("&order=7")


def test_url_sort_is_replaced():
    url = normalize_search_url(SEARCH + "&order=1")
    assert "order=7" in url
    assert "order=1" not in url


def test_url_page_is_reset_to_first():
    url = normalize_search_url(SEARCH + "&order=7&page=3&limit=20")
    assert "page=0" in url
    assert "page=3" not in url


def test_ukrainian_search_url_is_accepted():
    url = normalize_search_url("https://auto.ria.com/uk/search/?category=1")
    assert "order=7" in url


def test_page_url():
    url = normalize_search_url(SEARCH)
    assert page_url(url, 0) == url
    assert page_url(url, 2) == url + "&page=2"
    assert page_url(url + "&page=0", 3).endswith("&page=3")


@pytest.mark.parametrize("url, message_part", [
    ("https://auto.ria.com/advanced-search/?category=1", "форму пошуку"),
    ("https://auto.ria.com/auto_bmw_m4_40379814.html", "одне оголошення"),
    ("https://auto.ria.com/uk/auto_mercedes-benz_vito_40612345.html", "одне оголошення"),
    ("https://auto.ria.com/uk/legkovie/", "не сторінка пошуку"),
    ("https://www.olx.ua/uk/transport/", "не посилання на AUTO.RIA"),
    ("https://auto.ria.com.evil.com/search/?x=1", "не посилання на AUTO.RIA"),
])
def test_bad_urls_are_rejected_with_explanation(url, message_part):
    with pytest.raises(ValueError, match=message_part):
        normalize_search_url(url)

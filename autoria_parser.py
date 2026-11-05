"""Парсер сторінки пошуку AUTO.RIA.

Дані беремо не з HTML-верстки, а зі стану сторінки (window.__PINIA__).
Оголошення беремо ТІЛЬКИ з блоку "items" (білий список), тому
"Похожие предложения" і "Предложение дня" не потрапляють у результат.
"""
import json
import re
from urllib.parse import urlparse

import requests

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "Accept-Language": "uk-UA,uk;q=0.9",
}

SORT_BY_DATE = "7"  # order=7 означає "Дата додавання", новіші вгорі


class ParserError(Exception):
    """Сторінка має неочікувану структуру. Ймовірно, сайт змінився."""


def normalize_search_url(url):
    """Перевіряє посилання від клієнта і готує його для моніторингу."""
    parsed = urlparse(url)
    if not parsed.netloc.endswith("auto.ria.com"):
        raise ValueError("Це не посилання на AUTO.RIA.")
    if "advanced-search" in parsed.path:
        raise ValueError(
            "Це посилання на форму пошуку. Натисніть «Показать предложения» "
            "і скопіюйте посилання зі сторінки з результатами."
        )
    if re.search(r"/auto_[\w-]+\.html$", parsed.path):
        # у додатку AUTO.RIA «Поділитися» є тільки для окремого авто, не для пошуку
        raise ValueError(
            "Це посилання на одне оголошення, а мені потрібен пошук. "
            "Відкрийте auto.ria.com у браузері (з додатка пошуком поділитися не можна), "
            "налаштуйте фільтр і скопіюйте адресу сторінки з результатами."
        )
    if not re.search(r"/search/?$", parsed.path):
        raise ValueError(
            "Це не сторінка пошуку. Налаштуйте фільтр, натисніть "
            "«Показать предложения» і скопіюйте посилання."
        )

    # сортування "спочатку нові", інакше нове оголошення може бути не на 1-й сторінці
    if re.search(r"[?&]order=\d+", url):
        url = re.sub(r"([?&])order=\d+", rf"\g<1>order={SORT_BY_DATE}", url)
    else:
        url += ("&" if "?" in url else "?") + f"order={SORT_BY_DATE}"

    # завжди дивимось першу сторінку
    url = re.sub(r"([?&])page=\d+", r"\g<1>page=0", url)
    return url


def page_url(url, page):
    """Посилання на N-ту сторінку пошуку (0 означає першу, як є)."""
    if page == 0:
        return url
    if re.search(r"[?&]page=\d+", url):
        return re.sub(r"([?&])page=\d+", rf"\g<1>page={page}", url)
    return url + ("&" if "?" in url else "?") + f"page={page}"


def fetch_html(url):
    """Завантажує сторінку пошуку."""
    response = requests.get(url, headers=HEADERS, timeout=20)
    response.raise_for_status()  # якщо статус не 200, буде помилка
    return response.text


def extract_state(html):
    """Дістає JSON зі стану сторінки."""
    marker = "window.__PINIA__ = "
    start = html.find(marker)
    if start == -1:
        raise ParserError("Не знайшов window.__PINIA__ на сторінці")
    state, _ = json.JSONDecoder().raw_decode(html, start + len(marker))
    return state


def find_block(node, block_id, block_type):
    """Рекурсивно шукає в дереві блок з потрібними id і type."""
    if isinstance(node, dict):
        if node.get("id") == block_id and node.get("type") == block_type:
            return node
        for value in node.values():
            found = find_block(value, block_id, block_type)
            if found:
                return found
    elif isinstance(node, list):
        for item in node:
            found = find_block(item, block_id, block_type)
            if found:
                return found
    return None


def photo_url(data):
    """Перше фото авто у форматі jpg (webp Telegram приймає не завжди). "" якщо фото немає."""
    for photo in data.get("photos") or []:
        src = (photo.get("formats") or {}).get("middle") or photo.get("src") or ""
        if src.startswith("https://"):
            return re.sub(r"\.webp$", ".jpg", src)
    return ""


def parse_card(data):
    """Перетворює сиру картку з сайту на акуратний словник."""
    info = {}
    other = []
    for item in data.get("basicInfo", []):
        style = item.get("icon", {}).get("data", {}).get("style", "")
        content = item.get("content", "")
        if "speedometer" in style:
            info["mileage"] = content
        elif "fuel" in style:
            info["fuel"] = content
        elif "location" in style:
            info["city"] = content
        else:
            other.append(content)  # коробка передач та інше

    price = data.get("price") or {}
    return {
        "photo": photo_url(data),
        "id": str(data["id"]),
        "title": (data.get("title") or {}).get("content", ""),
        "subtitle": data.get("subtitle", ""),
        "price_usd": price.get("USD"),
        "price_uah": price.get("UAH"),
        "mileage": info.get("mileage", ""),
        "fuel": info.get("fuel", ""),
        "city": info.get("city", ""),
        "gearbox": ", ".join(other),
        "published": data.get("publishTime", ""),
        "link": data.get("link", ""),
    }


def listings_from_state(state):
    """Дістає оголошення з блоку items."""
    items = find_block(state, "items", "ListTemplate")
    if items is None:
        return []  # справжніх результатів немає

    listings = []
    seen_ids = set()
    for template in items.get("templates", []):
        if template.get("type") != "AdvertisementCardTemplate":
            continue
        card = (template.get("component") or {}).get("advertisementCard") or {}
        data = card.get("data")
        if not data or "id" not in data:
            continue
        listing = parse_card(data)
        if listing["id"] not in seen_ids:  # захист від дублікатів
            seen_ids.add(listing["id"])
            listings.append(listing)
    return listings


def parse_total(state):
    """Текст лічильника, наприклад '244 предложения'."""
    block = find_block(state, "SortButtonContentCount", "TextTemplate")
    if block:
        for element in block.get("elements", []):
            if element.get("content"):
                return element["content"]
    return None


def total_count(total):
    """'2\xa0650 предложений' -> 2650. None, якщо в тексті немає числа."""
    digits = re.sub(r"\D", "", total or "")
    return int(digits) if digits else None


def check_health(listings, total):
    """Ловить «тиху» поломку: сторінка відкрилась, але оголошень 0.

    Без цієї перевірки бот вирішив би, що нових авто просто немає, і мовчав би.
    """
    if listings:
        return
    if total is None:
        raise ParserError(
            "на сторінці немає ні оголошень, ні лічильника «N предложений». "
            "Схоже, змінилась структура сторінки"
        )
    if total_count(total):
        raise ParserError(
            f"сайт показує «{total}», а я не знайшов жодного оголошення. "
            "Схоже, змінилась структура сторінки"
        )


def parse_listings(html):
    """Повертає список оголошень з HTML сторінки пошуку."""
    return listings_from_state(extract_state(html))


def fetch_search(url):
    """Для бота: завантажує пошук і повертає (оголошення, лічильник)."""
    state = extract_state(fetch_html(url))
    listings, total = listings_from_state(state), parse_total(state)
    check_health(listings, total)
    return listings, total


def suggest_name(url, listings):
    """Назва пошуку замість «Пошук №4»: спільний початок назв авто + роки з фільтра.

    ['BMW X5 2019', 'BMW M4 2024'] і роки 2012–2024 -> 'BMW 2012–2024'.
    None, якщо оголошень немає або марки різні.
    """
    words = None
    for car in listings:
        title = [w for w in car["title"].split() if not re.fullmatch(r"\d{4}", w)]
        if words is None:
            words = title
        else:
            common = 0
            while common < min(len(words), len(title)) and words[common] == title[common]:
                common += 1
            words = words[:common]
    if not words:
        return None

    years = re.findall(r"year\[([01])\]=(\d{4})", url)
    years = dict(years)
    if "0" in years and "1" in years:
        period = years["0"] if years["0"] == years["1"] else f"{years['0']}–{years['1']}"
    elif "0" in years:
        period = f"від {years['0']}"
    elif "1" in years:
        period = f"до {years['1']}"
    else:
        period = ""
    return " ".join(words + [period]).strip()[:60]
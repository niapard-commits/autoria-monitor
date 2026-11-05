"""Логіка моніторингу: що вважати новим і як це показати в Telegram."""
import html


def find_new(db, search, listings):
    """Повертає НОВІ оголошення пошуку. Першу перевірку робить мовчки."""
    ids = [car["id"] for car in listings]

    if not search["initialized"]:
        # перша перевірка: запам'ятовуємо все мовчки
        db.mark_seen(search["id"], ids)
        db.mark_initialized(search["id"])
        return []

    new_ids = set(db.filter_new(search["id"], ids))
    return [car for car in listings if car["id"] in new_ids]


def group_new(db, results):
    """Збирає нові оголошення з усіх пошуків і групує їх за ID оголошення.

    results: список пар (пошук, оголошення зі сторінки).
    Повертає {id: {"car": ..., "searches": [пошук, ...]}}, тож авто, яке
    підійшло під кілька пошуків, стане одним повідомленням.
    """
    found = {}
    for search, listings in results:
        for car in reversed(find_new(db, search, listings)):  # від старішого до новішого
            if car["id"] in found:
                found[car["id"]]["searches"].append(search)
            elif db.seen_anywhere(car["id"]):
                # уже надсилали раніше з іншого пошуку: мовчки запам'ятовуємо
                db.mark_seen(search["id"], [car["id"]])
            else:
                found[car["id"]] = {"car": car, "searches": [search]}
    return found


def format_price(value):
    """89999 -> '89 999'"""
    return f"{value:,}".replace(",", " ") if value else "—"


def short_subtitle(subtitle, limit=25):
    """Довгий підзаголовок обрізаємо по « • », щоб картка не розросталась.

    'X243 • 300 66.5 kWh (228 к.с.) 4Matic • Base' -> 'X243'
    """
    parts = [p.strip() for p in subtitle.split("•") if p.strip()]
    result = parts[0] if parts else ""
    for part in parts[1:]:
        if len(result) + 3 + len(part) > limit:
            break
        result += " • " + part
    return result


def format_listing(search_names, car):
    """Готує підпис до фото для Telegram (формат HTML), коротко, щоб картка
    вміщалась на екрані. Посилання на оголошення бот додає кнопкою.

    search_names: назви всіх пошуків, під які підійшло авто.
    """
    e = html.escape
    details = " · ".join(
        x for x in [car["city"], car["mileage"], car["gearbox"], car["fuel"]] if x
    )
    header = " · ".join(e(name) for name in search_names)
    lines = [
        f"🔔 <b>{header}</b>",
        f"🚗 <b>{e(car['title'])}</b> {e(short_subtitle(car['subtitle']))}".rstrip(),
        f"💰 <b>{format_price(car['price_usd'])} $</b> · {format_price(car['price_uah'])} грн",
    ]
    if details:
        lines.append(f"📍 {e(details)}")
    if car["published"]:
        lines.append(f"🕒 {e(car['published'])}")
    return "\n".join(lines)
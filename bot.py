"""Telegram-бот: моніторинг нових оголошень AUTO.RIA."""
import asyncio
import html
import logging
import os
import re
import time
from datetime import datetime, timedelta
from uuid import uuid4

from dotenv import load_dotenv
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import requests

from autoria_parser import ParserError, fetch_search, normalize_search_url, page_url, suggest_name
from monitor import format_listing, group_new
from storage import Storage

load_dotenv()
TOKEN = os.getenv("TELEGRAM_TOKEN")
OWNER_CHAT_ID = int(os.getenv("OWNER_CHAT_ID"))
CHECK_INTERVAL_MINUTES = int(os.getenv("CHECK_INTERVAL_MINUTES", "5"))
FAIL_ALERT_AFTER = 3  # після скількох помилок поспіль попереджати власника
MAX_PAGES = 5  # скільки сторінок дивитися, якщо на сторінці тільки нові авто

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)  # не засмічувати лог
logging.getLogger("apscheduler").setLevel(logging.WARNING)
log = logging.getLogger("autoria-bot")

db = Storage(os.getenv("DB_PATH", "bot.db"))
owner = filters.Chat(chat_id=OWNER_CHAT_ID)  # фільтр "тільки власник"
check_lock = asyncio.Lock()  # щоб дві перевірки не йшли одночасно
URL_RE = re.compile(r"https?://\S+")
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)
e = html.escape

# тексти кнопок нижньої клавіатури
BTN_LIST = "📋 Мої пошуки"
BTN_CHECK = "🔎 Перевірити зараз"
BTN_PAUSE = "⏸ Пауза"
BTN_RESUME = "▶️ Відновити"
BTN_HELP = "❓ Допомога"
BTN_STATS = "📊 Статистика"

STARTED_AT = datetime.now()  # коли запустився бот (для статистики)

HELP_TEXT = (
    "👋 Я стежу за новими оголошеннями на AUTO.RIA.\n\n"
    "<b>Як додати пошук:</b>\n"
    "1. Налаштуйте фільтр на auto.ria.com\n"
    "2. Натисніть «Показать предложения»\n"
    "3. Скопіюйте посилання і надішліть мені\n\n"
    "📱 З телефона відкрийте auto.ria.com у браузері: у додатку AUTO.RIA "
    "поділитися можна тільки окремим авто, а не пошуком.\n"
    "Сортування не важливе, я сам ставлю «спочатку нові».\n\n"
    "Керувати пошуками можна кнопками внизу: у «📋 Мої пошуки» натисніть на пошук, "
    "щоб ✏️ перейменувати або 🗑 видалити, а «📊 Статистика» показує, що бот "
    "працює і скільки знайшов.\n"
    f"Перевіряю кожні {CHECK_INTERVAL_MINUTES} хв."
)


def main_keyboard():
    """Нижня клавіатура. Кнопка паузи залежить від стану."""
    pause_button = BTN_RESUME if db.is_paused() else BTN_PAUSE
    return ReplyKeyboardMarkup(
        [[BTN_LIST, BTN_CHECK], [BTN_STATS, pause_button, BTN_HELP]],
        resize_keyboard=True,
        is_persistent=True,
    )


async def remove_buttons(context, message_id):
    """Прибирає кнопки з повідомлення (якщо їх уже немає, нічого страшного)."""
    try:
        await context.bot.edit_message_reply_markup(OWNER_CHAT_ID, message_id, reply_markup=None)
    except BadRequest:
        pass


def name_keyboard(token, suggested):
    """Кнопки під питанням «Як назвати пошук?»."""
    label = f"💾 Зберегти як «{suggested}»" if suggested else "💾 Зберегти без назви"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(label, callback_data=f"save:{token}")],
        [InlineKeyboardButton("❌ Не додавати", callback_data=f"cancel:{token}")],
    ])


def unique_name(name):
    """«BMW» -> «BMW (2)», якщо пошук з такою назвою вже є."""
    if not name:
        return None
    names = {s["name"] for s in db.list_searches()}
    candidate, n = name, 2
    while candidate in names:
        candidate, n = f"{name} ({n})", n + 1
    return candidate


# ---------- перевірка оголошень ----------

def describe_error(error):
    """Пояснює помилку простими словами: що сталося і чи це мине саме."""
    if isinstance(error, ParserError):
        return f"змінилась сторінка сайту: {error}. Потрібно оновити парсер"
    if isinstance(error, requests.Timeout):
        return "сайт довго не відповідає. Зазвичай це минає саме"
    if isinstance(error, requests.ConnectionError):
        return "немає з'єднання з сайтом (сайт недоступний або немає інтернету)"
    if isinstance(error, requests.HTTPError) and error.response is not None:
        code = error.response.status_code
        if code in (403, 429):
            return f"сайт блокує запити (код {code}). Можливо, перевірки надто часті"
        if code == 404:
            return "сторінку пошуку не знайдено (код 404). Можливо, посилання застаріло"
        if code >= 500:
            return f"збій на боці сайту (код {code}). Зазвичай це минає саме"
        return f"сайт відповів помилкою (код {code})"
    return f"неочікувана помилка {type(error).__name__}: {error}"


async def send_status(context, text):
    """Службове повідомлення власнику. True, якщо дійшло."""
    try:
        await context.bot.send_message(OWNER_CHAT_ID, text, parse_mode=ParseMode.HTML)
        return True
    except Exception as error:
        # наприклад, немає інтернету: спробуємо під час наступної перевірки
        log.warning("Не вдалося надіслати службове повідомлення: %s", error)
        return False


async def fetch_listings(search):
    """Завантажує пошук. Якщо на сторінці тільки нові авто (наприклад, бот довго
    не працював), дивиться наступну, але не більше MAX_PAGES сторінок."""
    listings, ids = [], set()
    for page in range(MAX_PAGES):
        if page:
            await asyncio.sleep(3)
        try:
            # завантаження в окремому потоці, щоб бот не "зависав"
            page_listings, _ = await asyncio.to_thread(fetch_search, page_url(search["url"], page))
        except Exception as error:
            if page == 0:
                raise
            # перша сторінка вже є: краще показати її, ніж нічого
            log.warning("[%s] сторінка %s не завантажилась: %s", search["name"], page + 1, error)
            break

        page_ids = [car["id"] for car in page_listings]
        listings += [car for car in page_listings if car["id"] not in ids]
        ids.update(page_ids)

        # перша перевірка, порожня сторінка або є вже бачені авто: далі тільки старі
        if not search["initialized"] or not page_ids:
            break
        if len(db.filter_new(search["id"], page_ids)) < len(page_ids):
            break
        if page + 1 < MAX_PAGES:
            log.info("[%s] на сторінці %s тільки нові авто, дивлюсь наступну", search["name"], page + 1)
    return listings


async def send_listing(context, names, car):
    """Картка авто: фото з коротким підписом і кнопкою. Якщо фото немає або
    Telegram його не прийняв, надсилаємо той самий текст без фото."""
    text = format_listing(names, car)
    button = None
    if car["link"]:
        button = InlineKeyboardMarkup([[InlineKeyboardButton("Відкрити на AUTO.RIA", url=car["link"])]])
    if car.get("photo"):
        try:
            await context.bot.send_photo(
                OWNER_CHAT_ID, car["photo"], caption=text, parse_mode=ParseMode.HTML,
                reply_markup=button,
            )
            return
        except BadRequest as error:
            log.warning("Фото %s не надіслалось (%s), надсилаю текстом", car["id"], error)
    await context.bot.send_message(
        OWNER_CHAT_ID, text, parse_mode=ParseMode.HTML, reply_markup=button,
        link_preview_options=NO_PREVIEW,
    )


async def check_all(context: ContextTypes.DEFAULT_TYPE):
    """Перевіряє всі пошуки. Повертає кількість надісланих оголошень."""
    failures = context.bot_data.setdefault("failures", {})  # id пошуку -> помилок поспіль
    alerted = context.bot_data.setdefault("alerted", set())  # про які вже попередили
    results = []  # (пошук, оголошення) для пошуків, які вдалося перевірити
    broken, recovered = [], []
    last_error = None
    started = time.monotonic()

    # 1. завантажуємо всі пошуки
    for search in db.list_searches():
        name = search["name"]
        try:
            listings = await fetch_listings(search)
        except Exception as error:
            count = failures.get(search["id"], 0) + 1
            failures[search["id"]] = count
            log.warning("[%s] помилка перевірки (%s раз поспіль): %s", name, count, describe_error(error))
            if count >= FAIL_ALERT_AFTER and search["id"] not in alerted:
                broken.append(search)
                last_error = error
            continue

        if failures.pop(search["id"], 0) >= FAIL_ALERT_AFTER:
            alerted.discard(search["id"])
            recovered.append(name)

        log.info("[%s] оголошень: %s", name, len(listings))
        results.append((search, listings))
        await asyncio.sleep(3)  # пауза між пошуками, щоб не навантажувати сайт

    # одне попередження на всі зламані пошуки, а не по повідомленню на кожен
    if broken:
        names = ", ".join(f"«{e(s['name'])}»" for s in broken)
        text = (
            f"⚠️ Не вдається перевірити: {names}.\n"
            f"Спроб поспіль: {max(failures[s['id']] for s in broken)}.\n"
            f"Причина:{e(describe_error(last_error))[:300]}"
        )
        if await send_status(context, text):
            alerted.update(s["id"] for s in broken)
    if recovered:
        await send_status(
            context, "✅ Знову перевіряються: " + ", ".join(f"«{e(n)}»" for n in recovered)
        )

    context.bot_data["last_check"] = (datetime.now(), time.monotonic() - started)

    # 2. авто, що підійшло під кілька пошуків, стає одним повідомленням
    found = group_new(db, results)
    log.info("Нових оголошень: %s", len(found))

    # 3. надсилаємо
    sent = 0
    for listing_id, item in found.items():
        names = [s["name"] for s in item["searches"]]
        try:
            await send_listing(context, names, item["car"])
            db.log_sent(listing_id, [s["id"] for s in item["searches"]])
        except BadRequest as error:
            # Telegram відхилив саме це повідомлення: пропускаємо, а не застрягаємо
            log.error("Не вдалося надіслати %s: %s", listing_id, error)
        for s in item["searches"]:
            db.mark_seen(s["id"], [listing_id])  # тільки ПІСЛЯ відправки
        sent += 1
        await asyncio.sleep(1)  # Telegram не любить шквал повідомлень
    return sent


async def run_check(context):
    """Запускає перевірку, якщо вона ще не йде. None означає, що вже йде."""
    if check_lock.locked():
        return None
    async with check_lock:
        return await check_all(context)


async def scheduled_check(context: ContextTypes.DEFAULT_TYPE):
    """Перевірка за розкладом."""
    if db.is_paused():
        log.info("Пауза, перевірку пропущено")
        return
    await run_check(context)


# ---------- список пошуків ----------

def find_search(search_id):
    for s in db.list_searches():
        if s["id"] == search_id:
            return s
    return None


def render_list():
    """Текст списку пошуків і кнопки під ним: по одній на пошук, на всю ширину,
    щоб назва не обрізалась. Кнопка відкриває «Перейменувати / Видалити»."""
    searches = db.list_searches()
    if not searches:
        return "Пошуків поки немає. Надішліть посилання з AUTO.RIA.", None

    lines = [
        f"📋 <b>Ваші пошуки: {len(searches)}</b>",
        "Натисніть на пошук, щоб відкрити його на сайті, перейменувати або видалити.",
    ]
    buttons = []
    for s in searches:
        buttons.append([InlineKeyboardButton(f"⚙️ {s['name']}", callback_data=f"manage:{s['id']}")])
    if db.is_paused():
        lines.append("\n⏸ Сповіщення на паузі.")
    return "\n".join(lines), InlineKeyboardMarkup(buttons)


# ---------- статистика ----------

def ago(moment):
    """Скільки часу минуло: '2 год 15 хв', '3 д 4 год'."""
    minutes = int((datetime.now() - moment).total_seconds() // 60)
    days, minutes = divmod(minutes, 24 * 60)
    hours, minutes = divmod(minutes, 60)
    parts = [f"{days} д"] if days else []
    if hours:
        parts.append(f"{hours} год")
    if not days:
        parts.append(f"{minutes} хв")
    return " ".join(parts)


def render_stats(bot_data):
    """Текст для кнопки «📊 Статистика»: чи живий бот і скільки знайшов."""
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    week = today - timedelta(days=6)
    lines = [
        "📊 <b>Статистика</b>\n",
        f"🟢 Працюю без перерви {ago(STARTED_AT)} (з {STARTED_AT:%d.%m %H:%M})",
    ]
    last = bot_data.get("last_check")
    if last:
        lines.append(f"🔎 Остання перевірка: {last[0]:%H:%M}, тривала {last[1]:.0f} с")
    else:
        lines.append("🔎 Перша перевірка ще не закінчилась")
    if db.is_paused():
        lines.append("⏸ Сповіщення на паузі")

    first = db.first_sent_at()
    since = f" (з {datetime.fromisoformat(first):%d.%m})" if first else ""
    lines.append(
        f"\n🚗 <b>Нових авто:</b> сьогодні {db.count_sent(today.isoformat())} · "
        f"за 7 днів {db.count_sent(week.isoformat())} · всього {db.count_sent()}{since}"
    )

    searches = db.list_searches()
    if searches:
        per_search = db.sent_per_search(week.isoformat())
        lines.append("\n<b>По пошуках за 7 днів:</b>")
        for s in searches:
            lines.append(f"• {e(s['name'])}: {per_search.get(s['id'], 0)}")

    failures = bot_data.get("failures", {})
    broken = [s["name"] for s in searches if failures.get(s["id"], 0)]
    if broken:
        lines.append("\n⚠️ Зараз не перевіряються: " + ", ".join(f"«{e(n)}»" for n in broken))
    else:
        lines.append("\n✅ Усі пошуки перевіряються без помилок")
    return "\n".join(lines)


# ---------- команди і кнопки нижньої клавіатури ----------

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        HELP_TEXT, parse_mode=ParseMode.HTML, reply_markup=main_keyboard()
    )


async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text, markup = render_list()
    await update.effective_message.reply_text(
        text, parse_mode=ParseMode.HTML, reply_markup=markup, link_preview_options=NO_PREVIEW
    )


async def remove_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/remove 2 видаляє одразу, а просто /remove показує список з кнопками."""
    if not context.args or not context.args[0].isdigit():
        await list_cmd(update, context)
        return
    if db.remove_search(int(context.args[0])):
        await update.effective_message.reply_text("🗑 Пошук видалено.")
    else:
        await update.effective_message.reply_text("Пошуку з таким номером немає.")


async def pause_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db.set_paused(True)
    await update.effective_message.reply_text(
        "⏸ Сповіщення призупинено.", reply_markup=main_keyboard()
    )


async def resume_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db.set_paused(False)
    await update.effective_message.reply_text(
        "▶️ Сповіщення відновлено.", reply_markup=main_keyboard()
    )


async def check_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    await msg.reply_text("🔎 Перевіряю...")
    sent = await run_check(context)
    if sent is None:
        await msg.reply_text("Перевірка вже йде, зачекайте хвилинку.")
    else:
        await msg.reply_text(f"Готово. Нових оголошень: {sent}")


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        render_stats(context.bot_data), parse_mode=ParseMode.HTML
    )


async def unknown_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text("Не знаю такої команди. Скористайтеся кнопками внизу.")


# ---------- додавання пошуку ----------

async def save_pending(context, name):
    """Зберігає пошук, що чекав на назву. Повертає (номер, назва)."""
    pending = context.user_data.pop("pending")
    search_id = db.add_search(name, pending["url"])
    # те, що є на сайті зараз, вважаємо баченим, тож сповіщатимемо тільки про нове
    db.mark_seen(search_id, [car["id"] for car in pending["listings"]])
    db.mark_initialized(search_id)
    await remove_buttons(context, pending["question_id"])
    return search_id, name or f"Пошук №{search_id}"


async def save_pending_as_suggested(context, msg):
    """Посилання так і не назвали: зберігаємо із запропонованою назвою, щоб не загубити."""
    if "pending" in context.user_data:
        _, display = await save_pending(context, unique_name(context.user_data["pending"]["suggested"]))
        await msg.reply_text(f"💾 Попередній пошук збережено як «{display}».")


async def on_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Клієнт надіслав посилання."""
    msg = update.effective_message
    raw_url = URL_RE.search(msg.text).group(0)
    try:
        url = normalize_search_url(raw_url)
    except ValueError as error:
        await msg.reply_text(f"⚠️ {error}")
        return

    for s in db.list_searches():
        if s["url"] == url:
            await msg.reply_text(f"Цей пошук уже відстежується: №{s['id']} «{s['name']}».")
            return

    pending = context.user_data.get("pending")
    if pending and pending["url"] == url:
        # показуємо кнопки ще раз тут, щоб не треба було гортати чат
        await remove_buttons(context, pending["question_id"])
        question = await msg.reply_text(
            "Це посилання вже чекає на назву. Напишіть назву або натисніть кнопку:",
            reply_markup=name_keyboard(pending["token"], pending["suggested"]),
        )
        pending["question_id"] = question.message_id
        return
    await save_pending_as_suggested(context, msg)
    context.user_data.pop("renaming", None)

    await msg.reply_text("🔎 Перевіряю посилання...")
    try:
        listings, total = await asyncio.to_thread(fetch_search, url)
    except Exception as error:
        log.warning("Не вдалося перевірити посилання %s: %s", url, error)
        await msg.reply_text(
            f"⚠️ Не вдалося перевірити посилання: {describe_error(error)}.\n"
            "Спробуйте пізніше або надішліть інше посилання."
        )
        return

    token = uuid4().hex[:8]  # прив'язує кнопки саме до цього посилання
    suggested = unique_name(suggest_name(url, listings))
    keyboard = name_keyboard(token, suggested)
    found = total or f"{len(listings)} оголошень на першій сторінці"
    question = await msg.reply_text(
        f"✅ Посилання робоче, знайдено: {e(found)}.\n\n"
        "Як назвати цей пошук? Напишіть свою назву або натисніть кнопку.",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )
    context.user_data["pending"] = {
        "token": token,
        "url": url,
        "suggested": suggested,
        "listings": listings,
        "question_id": question.message_id,
    }


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Звичайний текст: або назва пошуку, або щось незрозуміле."""
    msg = update.effective_message
    if "renaming" in context.user_data:
        search_id = context.user_data.pop("renaming")
        name = msg.text.strip()[:60]
        if db.rename_search(search_id, name):
            await msg.reply_text(f"✏️ Пошук перейменовано: <b>{e(name)}</b>", parse_mode=ParseMode.HTML)
        else:
            await msg.reply_text("Цього пошуку вже немає.")
    elif "pending" in context.user_data:
        search_id, display = await save_pending(context, msg.text.strip()[:60])
        await msg.reply_text(
            f"✅ Додано пошук №{search_id}: <b>{e(display)}</b>\nПро нові оголошення повідомлю.",
            parse_mode=ParseMode.HTML,
            reply_markup=main_keyboard(),
        )
    else:
        await msg.reply_text("Надішліть посилання на пошук AUTO.RIA або скористайтеся кнопками внизу.")


# ---------- кнопки під повідомленнями ----------

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if update.effective_chat.id != OWNER_CHAT_ID:
        await query.answer("Це приватний бот.")
        return

    action, _, value = query.data.partition(":")

    if action in ("save", "cancel"):
        pending = context.user_data.get("pending")
        if not pending or pending["token"] != value:
            await query.answer("Це вже неактуально.")
            await remove_buttons(context, query.message.message_id)
            return
        await query.answer()
        if action == "save":
            search_id, display = await save_pending(context, unique_name(pending["suggested"]))
            await query.message.reply_text(
                f"✅ Додано пошук №{search_id}: <b>{e(display)}</b>\nПро нові оголошення повідомлю.",
                parse_mode=ParseMode.HTML,
                reply_markup=main_keyboard(),
            )
        else:
            context.user_data.pop("pending")
            await remove_buttons(context, query.message.message_id)
            await query.message.reply_text("❌ Пошук не додано.")
        return

    if action == "ren":
        search = find_search(int(value))
        if search is None:
            await query.answer("Цього пошуку вже немає.")
            return
        await query.answer()
        await save_pending_as_suggested(context, query.message)
        context.user_data["renaming"] = search["id"]
        await query.message.reply_text(
            f"Напишіть нову назву для «{e(search['name'])}».",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("Скасувати", callback_data="ren_no:0"),
            ]]),
        )
        return

    if action == "ren_no":
        await query.answer()
        context.user_data.pop("renaming", None)
        await query.edit_message_text("Перейменування скасовано.")
        return

    if action == "manage":
        search = find_search(int(value))
        if search is None:
            await query.answer("Цього пошуку вже немає.")
        else:
            await query.answer()
            await query.edit_message_text(
                f'Пошук «<a href="{e(search["url"])}">{e(search["name"])}</a>»',
                parse_mode=ParseMode.HTML,
                link_preview_options=NO_PREVIEW,
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("✏️ Перейменувати", callback_data=f"ren:{search['id']}"),
                        InlineKeyboardButton("🗑 Видалити", callback_data=f"del:{search['id']}"),
                    ],
                    [InlineKeyboardButton("🔗 Відкрити на AUTO.RIA", url=search["url"])],
                    [InlineKeyboardButton("« До списку", callback_data="del_no:0")],
                ]),
            )
            return

    if action == "del":
        search = find_search(int(value))
        if search is None:
            await query.answer("Цього пошуку вже немає.")
        else:
            await query.answer()
            confirm = InlineKeyboardMarkup([[
                InlineKeyboardButton("🗑 Так, видалити", callback_data=f"del_yes:{search['id']}"),
                InlineKeyboardButton("Ні", callback_data="del_no:0"),
            ]])
            await query.edit_message_text(
                f"Видалити пошук «{e(search['name'])}»?",
                parse_mode=ParseMode.HTML,
                reply_markup=confirm,
            )
            return

    prefix = ""
    if action == "del_yes":
        if db.remove_search(int(value)):
            prefix = "🗑 Пошук видалено.\n\n"
    await query.answer()
    text, markup = render_list()
    await query.edit_message_text(
        prefix + text, parse_mode=ParseMode.HTML, reply_markup=markup,
        link_preview_options=NO_PREVIEW,
    )


# ---------- службове ----------

async def stranger(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Пише хтось, крім власника."""
    log.warning("Повідомлення від стороннього: chat_id=%s", update.effective_chat.id)
    if update.effective_message:
        await update.effective_message.reply_text("Це приватний бот.")


async def on_error(update, context: ContextTypes.DEFAULT_TYPE):
    log.error("Помилка:", exc_info=context.error)


async def post_init(app):
    """Меню команд біля поля введення в Telegram."""
    await app.bot.set_my_commands([
        BotCommand("start", "Довідка"),
        BotCommand("list", "Мої пошуки"),
        BotCommand("check", "Перевірити зараз"),
        BotCommand("stats", "Статистика"),
        BotCommand("pause", "Призупинити сповіщення"),
        BotCommand("resume", "Відновити сповіщення"),
    ])


def main():
    app = Application.builder().token(TOKEN).post_init(post_init).build()

    # команди
    app.add_handler(CommandHandler(["start", "help"], start_cmd, filters=owner))
    app.add_handler(CommandHandler("list", list_cmd, filters=owner))
    app.add_handler(CommandHandler("remove", remove_cmd, filters=owner))
    app.add_handler(CommandHandler("pause", pause_cmd, filters=owner))
    app.add_handler(CommandHandler("resume", resume_cmd, filters=owner))
    app.add_handler(CommandHandler("check", check_cmd, filters=owner))
    app.add_handler(CommandHandler("stats", stats_cmd, filters=owner))
    app.add_handler(MessageHandler(owner & filters.COMMAND, unknown_cmd))

    # кнопки нижньої клавіатури (обов'язково ВИЩЕ за "будь-який текст")
    app.add_handler(MessageHandler(owner & filters.Text([BTN_LIST]), list_cmd))
    app.add_handler(MessageHandler(owner & filters.Text([BTN_CHECK]), check_cmd))
    app.add_handler(MessageHandler(owner & filters.Text([BTN_PAUSE]), pause_cmd))
    app.add_handler(MessageHandler(owner & filters.Text([BTN_RESUME]), resume_cmd))
    app.add_handler(MessageHandler(owner & filters.Text([BTN_HELP]), start_cmd))
    app.add_handler(MessageHandler(owner & filters.Text([BTN_STATS]), stats_cmd))

    # кнопки під повідомленнями
    app.add_handler(CallbackQueryHandler(on_button))

    # посилання і текст
    app.add_handler(MessageHandler(owner & filters.TEXT & filters.Regex(URL_RE), on_link))
    app.add_handler(MessageHandler(owner & filters.TEXT, on_text))

    # усі інші
    app.add_handler(MessageHandler(~owner, stranger))
    app.add_error_handler(on_error)

    app.job_queue.run_repeating(
        scheduled_check, interval=CHECK_INTERVAL_MINUTES * 60, first=10
    )
    log.info("Бот запущено, перевірка кожні %s хв", CHECK_INTERVAL_MINUTES)
    app.run_polling()


if __name__ == "__main__":
    main()
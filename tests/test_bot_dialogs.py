"""Тести діалогів у Telegram: додавання пошуку з назвою і перейменування.

Повідомлення і кнопки тут «підроблені»: бот відповідає в список replies.
"""
import asyncio
from types import SimpleNamespace

import pytest

import bot
from storage import Storage

SEARCH = "https://auto.ria.com/search/?category=1&all[0].any[0].year[0]=2015"
BMW = [{"id": "1", "title": "BMW X5 2019"}, {"id": "2", "title": "BMW X5 2016"}]


class Chat:
    def __init__(self):
        self.replies = []  # (текст, кнопки)

    def message(self, text=""):
        async def reply_text(answer, reply_markup=None, **kwargs):
            self.replies.append((answer, reply_markup))
            return SimpleNamespace(message_id=len(self.replies))
        return SimpleNamespace(text=text, reply_text=reply_text, message_id=0)

    def buttons(self):
        """Текст і дані кнопок під останньою відповіддю."""
        markup = self.replies[-1][1]
        return [(b.text, b.callback_data) for row in markup.inline_keyboard for b in row]


@pytest.fixture
def env(monkeypatch):
    db = Storage(":memory:")
    monkeypatch.setattr(bot, "db", db)
    monkeypatch.setattr(bot, "fetch_search", lambda url: (BMW, "2 предложения"))

    async def edit_markup(*args, **kwargs):
        pass

    chat = Chat()
    context = SimpleNamespace(
        bot=SimpleNamespace(edit_message_reply_markup=edit_markup), user_data={}, bot_data={}
    )
    return SimpleNamespace(db=db, chat=chat, context=context)


def send(env, text):
    update = SimpleNamespace(effective_message=env.chat.message(text))
    handler = bot.on_link if text.startswith("http") else bot.on_text
    asyncio.run(handler(update, env.context))


def press(env, data):
    async def answer(*args):
        pass

    async def edit_message_text(text, **kwargs):
        env.chat.replies.append((text, kwargs.get("reply_markup")))

    query = SimpleNamespace(
        data=data, answer=answer, edit_message_text=edit_message_text,
        message=env.chat.message(),
    )
    update = SimpleNamespace(callback_query=query, effective_chat=SimpleNamespace(id=bot.OWNER_CHAT_ID))
    asyncio.run(bot.on_button(update, env.context))


def names(env):
    return [s["name"] for s in env.db.list_searches()]


def test_link_offers_name_from_brand(env):
    send(env, SEARCH)
    save_button = env.chat.buttons()[0]
    assert save_button[0] == "💾 Зберегти як «BMW X5 від 2015»"

    press(env, save_button[1])
    assert names(env) == ["BMW X5 від 2015"]


def test_own_name_wins(env):
    send(env, SEARCH)
    send(env, "Мій X5")
    assert names(env) == ["Мій X5"]


def test_same_brand_twice_gets_number(env):
    env.db.add_search("BMW X5 від 2015", "інше посилання")
    send(env, SEARCH)
    assert "«BMW X5 від 2015 (2)»" in env.chat.buttons()[0][0]


def test_unnamed_link_is_saved_with_suggested_name(env):
    """Надіслали нове посилання, не назвавши попереднє: попереднє не губиться."""
    send(env, SEARCH)
    send(env, SEARCH + "&all[0].any[0].year[1]=2020")
    assert names(env) == ["BMW X5 від 2015"]


def test_rename_from_list(env):
    sid = env.db.add_search(None, SEARCH)
    press(env, f"ren:{sid}")
    assert "Напишіть нову назву" in env.chat.replies[-1][0]
    send(env, "BMW X5 дизель")
    assert names(env) == ["BMW X5 дизель"]
    send(env, "ще текст")  # наступний текст уже не перейменовує
    assert names(env) == ["BMW X5 дизель"]


def test_rename_can_be_cancelled(env):
    sid = env.db.add_search("Стара назва", SEARCH)
    press(env, f"ren:{sid}")
    press(env, "ren_no:0")
    send(env, "BMW")
    assert names(env) == ["Стара назва"]


def test_list_buttons_show_full_names(env):
    """Одна кнопка на пошук на всю ширину: довга назва не обрізається."""
    env.db.add_search("BMW 3 Series GT 2013–2023", SEARCH)
    text, markup = bot.render_list()
    assert [[b.text for b in row] for row in markup.inline_keyboard] == [
        ["⚙️ BMW 3 Series GT 2013–2023"]
    ]
    assert "BMW" not in text  # назви тільки на кнопках, без дубля в тексті


def test_manage_menu_leads_to_rename_and_delete(env):
    sid = env.db.add_search("BMW", SEARCH)
    press(env, f"manage:{sid}")
    assert env.chat.buttons() == [
        ("✏️ Перейменувати", f"ren:{sid}"),
        ("🗑 Видалити", f"del:{sid}"),
        ("🔗 Відкрити на AUTO.RIA", None),
        ("« До списку", "del_no:0"),
    ]

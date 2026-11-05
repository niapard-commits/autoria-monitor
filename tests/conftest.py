"""Налаштування для тестів: справжні токен, чат і база не використовуються."""
import os

# load_dotenv() у bot.py не перезаписує вже задані змінні
os.environ.setdefault("TELEGRAM_TOKEN", "test-token")
os.environ.setdefault("OWNER_CHAT_ID", "1")
os.environ.setdefault("DB_PATH", ":memory:")

"""Сховище бота: збережені пошуки, вже бачені оголошення, налаштування (SQLite)."""
import sqlite3
from datetime import datetime


def now():
    return datetime.now().isoformat(timespec="seconds")


class Storage:
    def __init__(self, path="bot.db"):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row  # рядки можна читати як row["url"]
        self._create_tables()

    def _create_tables(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS searches (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT NOT NULL,
                url         TEXT NOT NULL,
                initialized INTEGER NOT NULL DEFAULT 0,
                created_at  TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS seen (
                search_id  INTEGER NOT NULL,
                listing_id TEXT NOT NULL,
                seen_at    TEXT NOT NULL,
                PRIMARY KEY (search_id, listing_id)
            );
            CREATE TABLE IF NOT EXISTS sent (
                listing_id TEXT NOT NULL,
                search_id  INTEGER NOT NULL,
                sent_at    TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
        """)
        self.conn.commit()

    # ---------- пошуки ----------

    def add_search(self, name, url):
        cur = self.conn.execute(
            "INSERT INTO searches (name, url, created_at) VALUES (?, ?, ?)",
            (name or "", url, now()),
        )
        search_id = cur.lastrowid
        if not name:
            # назву не задали, тож буде "Пошук №3"
            self.conn.execute(
                "UPDATE searches SET name = ? WHERE id = ?",
                (f"Пошук №{search_id}", search_id),
            )
        self.conn.commit()
        return search_id

    def rename_search(self, search_id, name):
        cur = self.conn.execute("UPDATE searches SET name = ? WHERE id = ?", (name, search_id))
        self.conn.commit()
        return cur.rowcount > 0

    def list_searches(self):
        return self.conn.execute("SELECT * FROM searches ORDER BY id").fetchall()

    def remove_search(self, search_id):
        cur = self.conn.execute("DELETE FROM searches WHERE id = ?", (search_id,))
        self.conn.execute("DELETE FROM seen WHERE search_id = ?", (search_id,))
        self.conn.commit()
        return cur.rowcount > 0  # True, якщо такий пошук був

    def mark_initialized(self, search_id):
        self.conn.execute(
            "UPDATE searches SET initialized = 1 WHERE id = ?", (search_id,)
        )
        self.conn.commit()

    # ---------- бачені оголошення ----------

    def filter_new(self, search_id, listing_ids):
        """З переданих ID повертає тільки ті, яких ще не бачили."""
        new_ids = []
        for listing_id in listing_ids:
            row = self.conn.execute(
                "SELECT 1 FROM seen WHERE search_id = ? AND listing_id = ?",
                (search_id, listing_id),
            ).fetchone()
            if row is None:
                new_ids.append(listing_id)
        return new_ids

    def seen_anywhere(self, listing_id):
        """Чи бачили це оголошення хоч в одному пошуку."""
        row = self.conn.execute(
            "SELECT 1 FROM seen WHERE listing_id = ? LIMIT 1", (listing_id,)
        ).fetchone()
        return row is not None

    def mark_seen(self, search_id, listing_ids):
        self.conn.executemany(
            "INSERT OR IGNORE INTO seen (search_id, listing_id, seen_at) VALUES (?, ?, ?)",
            [(search_id, listing_id, now()) for listing_id in listing_ids],
        )
        self.conn.commit()

    # ---------- статистика ----------

    def log_sent(self, listing_id, search_ids):
        """Записує, що про авто повідомили (для статистики)."""
        self.conn.executemany(
            "INSERT INTO sent (listing_id, search_id, sent_at) VALUES (?, ?, ?)",
            [(listing_id, search_id, now()) for search_id in search_ids],
        )
        self.conn.commit()

    def count_sent(self, since=""):
        """Скільки різних авто надіслано, починаючи з часу since (ISO-рядок)."""
        row = self.conn.execute(
            "SELECT COUNT(DISTINCT listing_id) FROM sent WHERE sent_at >= ?", (since,)
        ).fetchone()
        return row[0]

    def sent_per_search(self, since=""):
        """{id пошуку: скільки авто надіслано} починаючи з since."""
        rows = self.conn.execute(
            "SELECT search_id, COUNT(*) FROM sent WHERE sent_at >= ? GROUP BY search_id", (since,)
        ).fetchall()
        return {row[0]: row[1] for row in rows}

    def first_sent_at(self):
        row = self.conn.execute("SELECT MIN(sent_at) FROM sent").fetchone()
        return row[0]

    # ---------- налаштування ----------

    def is_paused(self):
        row = self.conn.execute(
            "SELECT value FROM settings WHERE key = 'paused'"
        ).fetchone()
        return row is not None and row["value"] == "1"

    def set_paused(self, paused):
        self.conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES ('paused', ?)",
            ("1" if paused else "0",),
        )
        self.conn.commit()


if __name__ == "__main__":
    # Перевірка логіки на тимчасовій базі в пам'яті (файл не створюється)
    db = Storage(":memory:")
    sid = db.add_search("BMW Київ", "https://auto.ria.com/search/?...")
    print("Додано пошук №", sid)

    # 1-ша перевірка: пошук ще не ініціалізований, тож мовчки запам'ятовуємо все
    first_check = ["111", "222", "333"]
    print("1-ша перевірка, нових:", db.filter_new(sid, first_check))
    db.mark_seen(sid, first_check)
    db.mark_initialized(sid)
    print("   -> запам'ятали мовчки, сповіщень не надсилаємо")

    # 2-га перевірка: з'явилось оголошення 444, а 333 зникло (продали)
    second_check = ["444", "111", "222"]
    new = db.filter_new(sid, second_check)
    print("2-га перевірка, нових:", new, " <- тільки про це буде сповіщення")
    db.mark_seen(sid, new)

    # 3-тя перевірка: нічого не змінилось
    print("3-тя перевірка, нових:", db.filter_new(sid, second_check))

    print("Пошуки:", [dict(row) for row in db.list_searches()])
    db.set_paused(True)
    print("Пауза:", db.is_paused())
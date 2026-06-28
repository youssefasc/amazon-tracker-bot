import aiosqlite
from config import DB_PATH


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                is_premium INTEGER DEFAULT 0,
                premium_expires TEXT,
                joined_at TEXT DEFAULT (datetime('now'))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                asin TEXT,
                title TEXT,
                url TEXT,
                affiliate_url TEXT,
                image_url TEXT,
                current_price REAL,
                target_price REAL,
                target_percent REAL,
                is_muted INTEGER DEFAULT 0,
                last_alert_at TEXT,
                last_checked_at TEXT,
                added_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS payment_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                screenshot_file_id TEXT,
                status TEXT DEFAULT 'pending',
                requested_at TEXT DEFAULT (datetime('now')),
                reviewed_at TEXT
            )
        """)
        await db.commit()


async def get_user(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)) as cur:
            return await cur.fetchone()


async def upsert_user(user_id: int, username: str, full_name: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO users (user_id, username, full_name)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, full_name=excluded.full_name
        """, (user_id, username, full_name))
        await db.commit()


async def is_premium(user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT is_premium, premium_expires FROM users WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
            if not row:
                return False
            if row[0] == 1:
                if row[1] is None:
                    return True
                from datetime import datetime
                try:
                    exp = datetime.fromisoformat(row[1])
                    return exp > datetime.now()
                except:
                    return True
            return False


async def get_user_product_count(user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM products WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


async def add_product(user_id: int, asin: str, title: str, url: str,
                      affiliate_url: str, image_url: str, price: float,
                      target_price: float = None, target_percent: float = None) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            INSERT INTO products (user_id, asin, title, url, affiliate_url, image_url,
                                  current_price, target_price, target_percent)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (user_id, asin, title, url, affiliate_url, image_url,
              price, target_price, target_percent))
        await db.commit()
        return cur.lastrowid


async def get_user_products(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM products WHERE user_id = ? ORDER BY added_at DESC", (user_id,)
        ) as cur:
            return await cur.fetchall()


async def get_product(product_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM products WHERE id = ?", (product_id,)) as cur:
            return await cur.fetchone()


async def update_product_price(product_id: int, new_price: float):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE products SET current_price = ?, last_checked_at = datetime('now')
            WHERE id = ?
        """, (new_price, product_id))
        await db.commit()


async def update_product_alert_time(product_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE products SET last_alert_at = datetime('now') WHERE id = ?", (product_id,)
        )
        await db.commit()


async def toggle_mute(product_id: int, user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT is_muted FROM products WHERE id = ? AND user_id = ?", (product_id, user_id)
        ) as cur:
            row = await cur.fetchone()
            if not row:
                return False
            new_val = 0 if row[0] else 1
        await db.execute(
            "UPDATE products SET is_muted = ? WHERE id = ? AND user_id = ?",
            (new_val, product_id, user_id)
        )
        await db.commit()
        return bool(new_val)


async def delete_product(product_id: int, user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "DELETE FROM products WHERE id = ? AND user_id = ?", (product_id, user_id)
        )
        await db.commit()
        return cur.rowcount > 0


async def update_target(product_id: int, user_id: int,
                        target_price: float = None, target_percent: float = None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE products SET target_price = ?, target_percent = ?
            WHERE id = ? AND user_id = ?
        """, (target_price, target_percent, product_id, user_id))
        await db.commit()


async def get_all_active_products():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM products WHERE is_muted = 0"
        ) as cur:
            return await cur.fetchall()


async def activate_premium(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET is_premium = 1 WHERE user_id = ?", (user_id,)
        )
        await db.commit()


async def add_payment_request(user_id: int, file_id: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            INSERT INTO payment_requests (user_id, screenshot_file_id)
            VALUES (?, ?)
        """, (user_id, file_id))
        await db.commit()
        return cur.lastrowid


async def get_all_users():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users") as cur:
            return await cur.fetchall()

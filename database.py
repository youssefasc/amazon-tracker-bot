import aiosqlite
import os
from datetime import datetime, timedelta
from config import DB_PATH


async def init_db():
    # Create data directory if not exists
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                is_premium INTEGER DEFAULT 0,
                premium_expires TEXT,
                extra_slots INTEGER DEFAULT 0,
                extra_slots_expires TEXT,
                referred_by INTEGER,
                joined_at TEXT DEFAULT (datetime('now'))
            )
        """)
        # Migration: add new columns if they don't exist
        for col, definition in [
            ("extra_slots", "INTEGER DEFAULT 0"),
            ("extra_slots_expires", "TEXT"),
            ("referred_by", "INTEGER"),
        ]:
            try:
                await db.execute(f"ALTER TABLE users ADD COLUMN {col} {definition}")
            except:
                pass  # Column already exists
        # Migration for products table
        for col, definition in [
            ("last_alerted_price", "REAL"),
        ]:
            try:
                await db.execute(f"ALTER TABLE products ADD COLUMN {col} {definition}")
            except:
                pass
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
                last_alerted_price REAL,
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
        await db.execute("""
            CREATE TABLE IF NOT EXISTS coupons (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT UNIQUE,
                days INTEGER DEFAULT 0,
                extra_slots INTEGER DEFAULT 0,
                max_uses INTEGER DEFAULT 1,
                used_count INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now')),
                expires_at TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS coupon_uses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                coupon_id INTEGER,
                user_id INTEGER,
                used_at TEXT DEFAULT (datetime('now'))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS posted_deals (
                asin TEXT PRIMARY KEY,
                posted_at TEXT DEFAULT (datetime('now'))
            )
        """)
        await db.commit()


# ── Users ──────────────────────────────────────────────────────────────────────
async def get_user(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)) as cur:
            return await cur.fetchone()


async def upsert_user(user_id: int, username: str, full_name: str, referred_by: int = None):
    async with aiosqlite.connect(DB_PATH) as db:
        existing = await db.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
        row = await existing.fetchone()
        if row:
            await db.execute("UPDATE users SET username=?, full_name=? WHERE user_id=?",
                             (username, full_name, user_id))
        else:
            await db.execute(
                "INSERT INTO users (user_id, username, full_name, referred_by) VALUES (?,?,?,?)",
                (user_id, username, full_name, referred_by)
            )
            # Give referrer +1 slot for 30 days
            if referred_by:
                await db.execute("""
                    UPDATE users SET
                        extra_slots = extra_slots + 1,
                        extra_slots_expires = datetime('now', '+30 days')
                    WHERE user_id = ?
                """, (referred_by,))
        await db.commit()


async def is_premium(user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT is_premium, premium_expires FROM users WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
            if not row or not row[0]:
                return False
            if row[1] is None:
                return True
            try:
                return datetime.fromisoformat(row[1]) > datetime.now()
            except:
                return True


async def get_user_limit(user_id: int) -> int:
    """Return max products this user can track"""
    from config import FREE_LIMIT
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT is_premium, premium_expires, extra_slots, extra_slots_expires FROM users WHERE user_id = ?",
            (user_id,)
        ) as cur:
            row = await cur.fetchone()
            if not row:
                return FREE_LIMIT
            prem = row[0] and (row[1] is None or datetime.fromisoformat(row[1]) > datetime.now())
            if prem:
                return 999999
            extra = 0
            if row[2] and row[3]:
                try:
                    if datetime.fromisoformat(row[3]) > datetime.now():
                        extra = row[2]
                except:
                    pass
            return FREE_LIMIT + extra


async def get_all_users():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users") as cur:
            return await cur.fetchall()


async def activate_premium(user_id: int, days: int = None):
    async with aiosqlite.connect(DB_PATH) as db:
        if days:
            expires = (datetime.now() + timedelta(days=days)).isoformat()
            await db.execute(
                "UPDATE users SET is_premium=1, premium_expires=? WHERE user_id=?",
                (expires, user_id)
            )
        else:
            await db.execute(
                "UPDATE users SET is_premium=1, premium_expires=NULL WHERE user_id=?",
                (user_id,)
            )
        await db.commit()


async def revoke_premium(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET is_premium=0, premium_expires=NULL WHERE user_id=?", (user_id,)
        )
        await db.commit()


async def add_payment_request(user_id: int, file_id: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO payment_requests (user_id, screenshot_file_id) VALUES (?, ?)",
            (user_id, file_id)
        )
        await db.commit()
        return cur.lastrowid


# ── Products ───────────────────────────────────────────────────────────────────
async def get_user_product_count(user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM products WHERE user_id=?", (user_id,)) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


async def add_product(user_id, asin, title, url, affiliate_url, image_url,
                      price, target_price=None, target_percent=None) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            INSERT INTO products (user_id,asin,title,url,affiliate_url,image_url,
                                  current_price,target_price,target_percent)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, (user_id, asin, title, url, affiliate_url, image_url,
              price, target_price, target_percent))
        await db.commit()
        return cur.lastrowid


async def get_user_products(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM products WHERE user_id=? ORDER BY added_at DESC", (user_id,)
        ) as cur:
            return await cur.fetchall()


async def get_product(product_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM products WHERE id=?", (product_id,)) as cur:
            return await cur.fetchone()


async def get_all_active_products():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM products WHERE is_muted=0") as cur:
            return await cur.fetchall()


async def update_product_price(product_id: int, new_price: float):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE products SET current_price=?, last_checked_at=datetime('now') WHERE id=?",
            (new_price, product_id)
        )
        await db.commit()


async def update_product_alert_time(product_id: int, alerted_price: float = None):
    async with aiosqlite.connect(DB_PATH) as db:
        if alerted_price is not None:
            await db.execute(
                "UPDATE products SET last_alert_at=datetime('now'), last_alerted_price=? WHERE id=?",
                (alerted_price, product_id)
            )
        else:
            await db.execute(
                "UPDATE products SET last_alert_at=datetime('now') WHERE id=?", (product_id,)
            )
        await db.commit()


async def toggle_mute(product_id: int, user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT is_muted FROM products WHERE id=? AND user_id=?", (product_id, user_id)
        ) as cur:
            row = await cur.fetchone()
            if not row:
                return False
            new_val = 0 if row[0] else 1
        await db.execute(
            "UPDATE products SET is_muted=? WHERE id=? AND user_id=?",
            (new_val, product_id, user_id)
        )
        await db.commit()
        return bool(new_val)


async def delete_product(product_id: int, user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "DELETE FROM products WHERE id=? AND user_id=?", (product_id, user_id)
        )
        await db.commit()
        return cur.rowcount > 0


async def update_target(product_id: int, user_id: int,
                        target_price=None, target_percent=None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE products SET target_price=?, target_percent=? WHERE id=? AND user_id=?",
            (target_price, target_percent, product_id, user_id)
        )
        await db.commit()


# ── Coupons ────────────────────────────────────────────────────────────────────
async def create_coupon(code: str, days: int = 0, extra_slots: int = 0,
                        max_uses: int = 1, expires_at: str = None) -> bool:
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO coupons (code, days, extra_slots, max_uses, expires_at)
                VALUES (?,?,?,?,?)
            """, (code.upper(), days, extra_slots, max_uses, expires_at))
            await db.commit()
            return True
    except:
        return False


async def use_coupon(code: str, user_id: int) -> dict | None:
    """Returns coupon data if valid, None otherwise"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM coupons WHERE code=? AND used_count < max_uses", (code.upper(),)
        ) as cur:
            coupon = await cur.fetchone()
        if not coupon:
            return None
        # Check expiry
        if coupon["expires_at"]:
            try:
                if datetime.fromisoformat(coupon["expires_at"]) < datetime.now():
                    return None
            except:
                pass
        # Check if user already used it
        async with db.execute(
            "SELECT id FROM coupon_uses WHERE coupon_id=? AND user_id=?",
            (coupon["id"], user_id)
        ) as cur:
            if await cur.fetchone():
                return None
        # Apply
        await db.execute(
            "UPDATE coupons SET used_count=used_count+1 WHERE id=?", (coupon["id"],)
        )
        await db.execute(
            "INSERT INTO coupon_uses (coupon_id, user_id) VALUES (?,?)",
            (coupon["id"], user_id)
        )
        if coupon["days"] > 0:
            expires = (datetime.now() + timedelta(days=coupon["days"])).isoformat()
            await db.execute(
                "UPDATE users SET is_premium=1, premium_expires=? WHERE user_id=?",
                (expires, user_id)
            )
        if coupon["extra_slots"] > 0:
            expires = (datetime.now() + timedelta(days=30)).isoformat()
            await db.execute("""
                UPDATE users SET
                    extra_slots = extra_slots + ?,
                    extra_slots_expires = ?
                WHERE user_id = ?
            """, (coupon["extra_slots"], expires, user_id))
        await db.commit()
        return dict(coupon)


async def get_all_coupons():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM coupons ORDER BY created_at DESC") as cur:
            return await cur.fetchall()


# ── Posted Deals (48h dedup) ────────────────────────────────────────────────────
async def was_deal_posted(asin: str, hours: int = 48) -> bool:
    """Check if ASIN was posted within the last X hours"""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT posted_at FROM posted_deals WHERE asin = ?", (asin,)
        ) as cur:
            row = await cur.fetchone()
            if not row:
                return False
            try:
                posted = datetime.fromisoformat(row[0])
                return (datetime.now() - posted) < timedelta(hours=hours)
            except:
                return True


async def mark_deal_posted(asin: str):
    """Record that an ASIN was posted now"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO posted_deals (asin, posted_at) VALUES (?, datetime('now'))",
            (asin,)
        )
        await db.commit()


async def cleanup_old_deals(hours: int = 48):
    """Remove deals older than X hours"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM posted_deals WHERE posted_at < datetime('now', ?)",
            (f"-{hours} hours",)
        )
        await db.commit()


# ── Deal rotation counter ───────────────────────────────────────────────────────
async def get_rotation_state() -> int:
    """Get the current rotation counter (how many deals posted in this cycle)"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("CREATE TABLE IF NOT EXISTS bot_state (key TEXT PRIMARY KEY, value INTEGER)")
        await db.commit()
        async with db.execute("SELECT value FROM bot_state WHERE key='rotation'") as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


async def set_rotation_state(value: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("CREATE TABLE IF NOT EXISTS bot_state (key TEXT PRIMARY KEY, value INTEGER)")
        await db.execute(
            "INSERT OR REPLACE INTO bot_state (key, value) VALUES ('rotation', ?)", (value,)
        )
        await db.commit()


async def reset_alerted_price(product_id: int):
    """Clear last_alerted_price so a test alert can fire"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE products SET last_alerted_price=NULL WHERE id=?", (product_id,)
        )
        await db.commit()


# ── Last deal post time (persistent across restarts) ────────────────────────────
async def get_last_deal_post_time():
    """Get the timestamp of the last deal posted (ISO string) or None"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("CREATE TABLE IF NOT EXISTS bot_meta (key TEXT PRIMARY KEY, value TEXT)")
        await db.commit()
        async with db.execute("SELECT value FROM bot_meta WHERE key='last_deal_post'") as cur:
            row = await cur.fetchone()
            return row[0] if row else None


async def set_last_deal_post_time(iso_time: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("CREATE TABLE IF NOT EXISTS bot_meta (key TEXT PRIMARY KEY, value TEXT)")
        await db.execute(
            "INSERT OR REPLACE INTO bot_meta (key, value) VALUES ('last_deal_post', ?)", (iso_time,)
        )
        await db.commit()

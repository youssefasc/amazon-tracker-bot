import asyncio
import aiohttp
import urllib.parse
from datetime import datetime, timedelta
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError
from config import BOT_TOKEN, CHANNEL_ID, ALERT_COOLDOWN_MINUTES, AFFILIATE_TAG, CHANNEL_LINK
from database import (get_all_active_products, update_product_price, update_product_alert_time,
                      was_deal_posted, mark_deal_posted, cleanup_old_deals,
                      get_rotation_state, set_rotation_state,
                      get_last_deal_post_time, set_last_deal_post_time)
from scraper import (get_current_price, get_deals_from_amazon, get_product_screenshot,
                     CATEGORY_ROTATION)


def aff_url(asin: str) -> str:
    """Always build a correct affiliate URL from ASIN"""
    return f"https://www.amazon.eg/dp/{asin}?tag={AFFILIATE_TAG}"


async def shorten_url(url: str) -> str:
    """Shorten URL using is.gd (free, no API key). Returns original on failure."""
    try:
        api = f"https://is.gd/create.php?format=simple&url={urllib.parse.quote(url)}"
        async with aiohttp.ClientSession() as session:
            async with session.get(api, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    short = (await resp.text()).strip()
                    if short.startswith("http"):
                        return short
    except Exception as e:
        print(f"Shorten error: {e}")
    return url


def channel_buttons(affiliate_link: str):
    """Buttons for channel posts: buy now + channel link"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🛒 شراء الآن", url=affiliate_link)],
        [InlineKeyboardButton("📢 عروض متتفوتش", url=CHANNEL_LINK)],
    ])

# منع التكرار — مخزّن في الـ database دلوقتي
ASIN_COOLDOWN_HOURS = 48
_deals_lock = asyncio.Lock()
_last_deal_post: datetime | None = None


def fp(price: float) -> str:
    return f"{price:,.0f} جنيه"


def should_alert(product, new_price: float) -> bool:
    old = product["current_price"]

    # لازم السعر ينزل عن السعر الحالي
    if new_price >= old:
        return False

    # لازم السعر الجديد يكون أقل من آخر سعر تم التنبيه عليه (يمنع تكرار نفس السعر)
    try:
        last_alerted = product["last_alerted_price"]
    except (KeyError, IndexError):
        last_alerted = None
    if last_alerted is not None and new_price >= last_alerted:
        return False

    # شرط السعر المستهدف
    if product["target_price"] and new_price > product["target_price"]:
        return False

    # شرط نسبة الخصم
    if product["target_percent"]:
        if ((old - new_price) / old * 100) < product["target_percent"]:
            return False

    return True


async def check_all_prices(bot: Bot):
    print(f"[{datetime.now().strftime('%H:%M')}] Checking prices...")
    products = await get_all_active_products()
    for product in products:
        try:
            new_price = await get_current_price(product["asin"])
            if new_price is None:
                continue
            old_price = product["current_price"]
            if should_alert(product, new_price):
                drop = old_price - new_price
                drop_pct = drop / old_price * 100
                affiliate_link = aff_url(product["asin"])
                # Alert user (private)
                user_msg = (
                    f"📉 <b>انخفض السعر!</b>\n\n"
                    f"🛍 {product['title']}\n\n"
                    f"💰 كان: <s>{fp(old_price)}</s>\n"
                    f"✅ بقى: <b>{fp(new_price)}</b>\n"
                    f"📊 خصم: <b>{drop_pct:.1f}% (وفّرت {fp(drop)})</b>\n\n"
                    f"🛒 <a href='{affiliate_link}'>اشتري دلوقتي</a>"
                )
                try:
                    if product["image_url"]:
                        await bot.send_photo(chat_id=product["user_id"], photo=product["image_url"],
                                             caption=user_msg, parse_mode="HTML",
                                             reply_markup=channel_buttons(affiliate_link))
                    else:
                        await bot.send_message(chat_id=product["user_id"], text=user_msg, parse_mode="HTML",
                                               reply_markup=channel_buttons(affiliate_link))
                except TelegramError as e:
                    print(f"Alert user error: {e}")
                # Post to channel
                ch_msg = (
                    f"عرض ميتفوتش 🔥⚡️\n\n"
                    f"🛍 {product['title']}\n\n"
                    f"💰 كان: <s>{fp(old_price)}</s>\n"
                    f"✅ بقى: <b>{fp(new_price)}</b>\n"
                    f"📊 خصم: <b>{drop_pct:.1f}%</b>\n\n"
                    f"🛒 <a href='{affiliate_link}'>اشتري دلوقتي</a>"
                )
                try:
                    if product["image_url"]:
                        await bot.send_photo(chat_id=CHANNEL_ID, photo=product["image_url"],
                                             caption=ch_msg, parse_mode="HTML",
                                             reply_markup=channel_buttons(affiliate_link))
                    else:
                        await bot.send_message(chat_id=CHANNEL_ID, text=ch_msg, parse_mode="HTML",
                                               reply_markup=channel_buttons(affiliate_link))
                except TelegramError as e:
                    print(f"Channel post error: {e}")
                await update_product_alert_time(product["id"], new_price)
            await update_product_price(product["id"], new_price)
            await asyncio.sleep(3)
        except Exception as e:
            print(f"Error checking {product.get('id')}: {e}")
    print("Price check done.")


def get_current_category(counter: int) -> tuple[str, int]:
    """Returns (category_name, total_cycle_length) based on rotation counter"""
    cycle_len = sum(count for _, count in CATEGORY_ROTATION)
    pos = counter % cycle_len
    acc = 0
    for cat, count in CATEGORY_ROTATION:
        if pos < acc + count:
            return cat, cycle_len
        acc += count
    return CATEGORY_ROTATION[-1][0], cycle_len


async def post_deals_to_channel(bot: Bot, force: bool = False):
    """Scrape deals and post to channel — one deal per run, DB-backed 48h dedup,
    rotating categories"""
    global _last_deal_post

    # امنع التشغيل المتزامن
    if _deals_lock.locked():
        print("Deals already running, skipping")
        return

    async with _deals_lock:
        now = datetime.now()

        # امنع التشغيل لو آخر نشر كان من أقل من 4.5 دقيقة (من الـ database — يصمد بعد restart)
        if not force:
            last_post_str = await get_last_deal_post_time()
            if last_post_str:
                try:
                    last_post = datetime.fromisoformat(last_post_str)
                    if (now - last_post) < timedelta(minutes=4, seconds=30):
                        print(f"Last post was {(now - last_post).seconds}s ago, skipping")
                        return
                except:
                    pass

        # نضّف العروض القديمة (أكتر من 48 ساعة)
        await cleanup_old_deals(ASIN_COOLDOWN_HOURS)

        # حدد الفئة الحالية من العداد
        counter = await get_rotation_state()
        category, cycle_len = get_current_category(counter)
        print(f"[{now.strftime('%H:%M')}] Fetching deals... category={category} (counter={counter})")

        deals = await get_deals_from_amazon(category)
        if not deals:
            print("No deals found — advancing counter")
            await set_rotation_state((counter + 1) % cycle_len)
            return

        for deal in deals:
            asin = deal.get("asin", "")
            if not asin:
                continue

            # تخطي لو اتنشر في آخر 48 ساعة (من الـ database)
            if await was_deal_posted(asin, ASIN_COOLDOWN_HOURS):
                print(f"Skipping {asin} — already posted in last 48h")
                continue

            try:
                affiliate_link = deal["affiliate_url"]
                orig = f"<s>{fp(deal['original_price'])}</s> → " if deal.get("original_price") else ""
                pct = f"🏷 خصم <b>{deal['discount_pct']}%</b>\n" if deal.get("discount_pct") else ""
                msg = (
                    f"عرض ميتفوتش 🔥⚡️\n\n"
                    f"🛍 {deal['title']}\n\n"
                    f"{pct}"
                    f"💰 {orig}<b>{fp(deal['price'])}</b>\n\n"
                    f"🛒 <a href='{affiliate_link}'>اشتري دلوقتي</a>"
                )
                # خد سكرين شوت من صفحة المنتج (بحد أقصى 40 ثانية)
                screenshot = None
                try:
                    screenshot = await asyncio.wait_for(
                        get_product_screenshot(asin), timeout=40
                    )
                except asyncio.TimeoutError:
                    print(f"Screenshot timeout for {asin}")
                except Exception as e:
                    print(f"Screenshot error: {e}")
                photo = screenshot or deal.get("image_url")
                if photo:
                    await bot.send_photo(chat_id=CHANNEL_ID, photo=photo,
                                         caption=msg, parse_mode="HTML",
                                         reply_markup=channel_buttons(affiliate_link))
                else:
                    await bot.send_message(chat_id=CHANNEL_ID, text=msg, parse_mode="HTML",
                                           reply_markup=channel_buttons(affiliate_link))

                # سجّله في الـ database، زوّد العداد، ووقف بعد منتج واحد
                await mark_deal_posted(asin)
                await set_rotation_state((counter + 1) % cycle_len)
                await set_last_deal_post_time(now.isoformat())
                _last_deal_post = now
                print(f"Posted 1 deal: {asin} (category={category})")
                return

        # لو وصلنا هنا يبقى كل المنتجات اتنشرت قبل كده — زوّد العداد عشان ننتقل لفئة تانية
        print("No new deals to post — advancing counter")
        await set_rotation_state((counter + 1) % cycle_len)
            except Exception as e:
                print(f"Deal post error: {e}")
                continue

        print("No new deals to post.")


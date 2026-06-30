import asyncio
import aiohttp
import urllib.parse
from datetime import datetime, timedelta
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError
from config import BOT_TOKEN, CHANNEL_ID, ALERT_COOLDOWN_MINUTES, AFFILIATE_TAG, CHANNEL_LINK, ADMIN_ID
from database import (get_all_active_products, update_product_price, update_product_alert_time,
                      was_deal_posted, mark_deal_posted, cleanup_old_deals)
from scraper import get_current_price, get_deals_from_amazon, get_product_screenshot


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

    # لو فيه سعر تم التنبيه عليه قبل كده — لازم السعر الجديد يكون أقل منه
    # (يمنع تكرار التنبيه لنفس السعر)
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


async def post_deals_to_channel(bot: Bot, force: bool = False):
    """Scrape deals and post to channel — one deal per run, DB-backed 48h dedup"""
    global _last_deal_post

    if _deals_lock.locked():
        print("Deals already running, skipping")
        return

    async with _deals_lock:
        now = datetime.now()

        if not force and _last_deal_post and (now - _last_deal_post) < timedelta(minutes=4):
            print(f"Last post was {(now - _last_deal_post).seconds}s ago, skipping")
            return

        await cleanup_old_deals(ASIN_COOLDOWN_HOURS)

        print(f"[{now.strftime('%H:%M')}] Fetching deals...")
        deals = await get_deals_from_amazon()
        if not deals:
            print("No deals found")
            return

        for deal in deals:
            asin = deal.get("asin", "")
            if not asin:
                continue

            if await was_deal_posted(asin, ASIN_COOLDOWN_HOURS):
                print(f"Skipping {asin} — already posted")
                continue

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

            posted_ok = False

            # 1. حاول بالسكرين شوت
            try:
                shot_data = await get_product_screenshot(asin)
                screenshot = shot_data.get("screenshot") if shot_data else None
                if screenshot:
                    await bot.send_photo(
                        chat_id=CHANNEL_ID, photo=screenshot,
                        caption=msg, parse_mode="HTML",
                        reply_markup=channel_buttons(affiliate_link)
                    )
                    posted_ok = True
            except Exception as e1:
                print(f"Screenshot failed: {e1}")

            # 2. حاول بصورة المنتج
            if not posted_ok and deal.get("image_url"):
                try:
                    await bot.send_photo(
                        chat_id=CHANNEL_ID, photo=deal["image_url"],
                        caption=msg, parse_mode="HTML",
                        reply_markup=channel_buttons(affiliate_link)
                    )
                    posted_ok = True
                except Exception as e2:
                    print(f"Image failed: {e2}")

            # 3. ابعت نص بس
            if not posted_ok:
                try:
                    await bot.send_message(
                        chat_id=CHANNEL_ID, text=msg, parse_mode="HTML",
                        reply_markup=channel_buttons(affiliate_link)
                    )
                    posted_ok = True
                except Exception as e3:
                    await bot.send_message(
                        chat_id=ADMIN_ID,
                        text=f"❌ فشل النشر!\nخطأ: {e3}\nCHANNEL_ID: {CHANNEL_ID}"
                    )

            if posted_ok:
                await mark_deal_posted(asin)
                _last_deal_post = now
                print(f"✅ Posted: {asin}")
                return

        print("No new deals to post.")

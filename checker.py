import asyncio
import aiohttp
import urllib.parse
from datetime import datetime, timedelta
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError
from config import BOT_TOKEN, CHANNEL_ID, ALERT_COOLDOWN_MINUTES, AFFILIATE_TAG, CHANNEL_LINK
from database import get_all_active_products, update_product_price, update_product_alert_time
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

# ASINs اللي اتنشرت — بتتمسح كل 48 ساعة
_posted_asins: dict[str, datetime] = {}
ASIN_COOLDOWN_HOURS = 48
_deals_lock = asyncio.Lock()
_last_deal_post: datetime | None = None


def fp(price: float) -> str:
    return f"{price:,.0f} جنيه"


def should_alert(product, new_price: float) -> bool:
    old = product["current_price"]
    if new_price >= old:
        return False
    if product["target_price"] and new_price > product["target_price"]:
        return False
    if product["target_percent"]:
        if ((old - new_price) / old * 100) < product["target_percent"]:
            return False
    if product["last_alert_at"]:
        try:
            last = datetime.fromisoformat(product["last_alert_at"])
            if datetime.now() - last < timedelta(minutes=ALERT_COOLDOWN_MINUTES):
                return False
        except:
            pass
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
                    f"🔥 <b>انخفاض سعر على أمازون مصر!</b>\n\n"
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
                await update_product_alert_time(product["id"])
            await update_product_price(product["id"], new_price)
            await asyncio.sleep(3)
        except Exception as e:
            print(f"Error checking {product.get('id')}: {e}")
    print("Price check done.")


async def post_deals_to_channel(bot: Bot):
    """Scrape deals and post to channel — one deal per run, no duplicates"""
    global _posted_asins, _last_deal_post

    # امنع التشغيل المتزامن
    if _deals_lock.locked():
        print("Deals already running, skipping")
        return

    async with _deals_lock:
        now = datetime.now()

        # امنع التشغيل لو آخر نشر كان من أقل من 4 دقايق (يمنع التكرار)
        if _last_deal_post and (now - _last_deal_post) < timedelta(minutes=4):
            print(f"Last post was {(now - _last_deal_post).seconds}s ago, skipping")
            return

        # Clean up old entries
        _posted_asins = {
            asin: t for asin, t in _posted_asins.items()
            if now - t < timedelta(hours=ASIN_COOLDOWN_HOURS)
        }

        print(f"[{now.strftime('%H:%M')}] Fetching deals... (posted cache: {len(_posted_asins)})")
        deals = await get_deals_from_amazon()
        if not deals:
            print("No deals found")
            return

        for deal in deals:
            asin = deal.get("asin", "")

            # تخطي لو اتنشر في آخر 48 ساعة
            if asin in _posted_asins:
                print(f"Skipping {asin} — already posted")
                continue

            try:
                affiliate_link = deal["affiliate_url"]
                orig = f"<s>{fp(deal['original_price'])}</s> → " if deal.get("original_price") else ""
                pct = f"🏷 خصم <b>{deal['discount_pct']}%</b>\n" if deal.get("discount_pct") else ""
                msg = (
                    f"⚡ <b>عرض على أمازون مصر!</b>\n\n"
                    f"🛍 {deal['title']}\n\n"
                    f"{pct}"
                    f"💰 {orig}<b>{fp(deal['price'])}</b>\n\n"
                    f"🛒 <a href='{affiliate_link}'>اشتري دلوقتي</a>"
                )
                # خد سكرين شوت من صفحة المنتج
                screenshot = await get_product_screenshot(asin)
                photo = screenshot or deal.get("image_url")
                if photo:
                    await bot.send_photo(chat_id=CHANNEL_ID, photo=photo,
                                         caption=msg, parse_mode="HTML",
                                         reply_markup=channel_buttons(affiliate_link))
                else:
                    await bot.send_message(chat_id=CHANNEL_ID, text=msg, parse_mode="HTML",
                                           reply_markup=channel_buttons(affiliate_link))

                # سجّله كمنشور ووقف بعد منتج واحد بس
                _posted_asins[asin] = now
                _last_deal_post = now
                print(f"Posted 1 deal: {asin}")
                return
            except Exception as e:
                print(f"Deal post error: {e}")
                continue

        print("No new deals to post.")

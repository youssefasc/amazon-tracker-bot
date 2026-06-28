import asyncio
from datetime import datetime, timedelta
from telegram import Bot
from telegram.error import TelegramError
from config import BOT_TOKEN, CHANNEL_ID, ALERT_COOLDOWN_MINUTES
from database import get_all_active_products, update_product_price, update_product_alert_time
from scraper import get_current_price, get_deals_from_amazon

# ASINs اللي اتنشرت — بتتمسح كل 24 ساعة
_posted_asins: dict[str, datetime] = {}
ASIN_COOLDOWN_HOURS = 24


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
                # Alert user
                user_msg = (
                    f"📉 <b>انخفض السعر!</b>\n\n"
                    f"🛍 {product['title']}\n\n"
                    f"💰 كان: <s>{fp(old_price)}</s>\n"
                    f"✅ بقى: <b>{fp(new_price)}</b>\n"
                    f"📊 خصم: <b>{drop_pct:.1f}% (وفّرت {fp(drop)})</b>\n\n"
                    f"🔗 <a href='{product['affiliate_url']}'>اشتري دلوقتي</a>"
                )
                try:
                    if product["image_url"]:
                        await bot.send_photo(chat_id=product["user_id"], photo=product["image_url"],
                                             caption=user_msg, parse_mode="HTML")
                    else:
                        await bot.send_message(chat_id=product["user_id"], text=user_msg, parse_mode="HTML")
                except TelegramError as e:
                    print(f"Alert user error: {e}")
                # Post to channel
                ch_msg = (
                    f"🔥 <b>انخفاض سعر على أمازون مصر!</b>\n\n"
                    f"🛍 {product['title']}\n\n"
                    f"💰 كان: <s>{fp(old_price)}</s>\n"
                    f"✅ بقى: <b>{fp(new_price)}</b>\n"
                    f"📊 خصم: <b>{drop_pct:.1f}%</b>\n\n"
                    f"🛒 <a href='{product['affiliate_url']}'>اشتري من أمازون مصر</a>\n\n"
                    f"📲 تابع أكتر عروض: @offer_egypt"
                )
                try:
                    if product["image_url"]:
                        await bot.send_photo(chat_id=CHANNEL_ID, photo=product["image_url"],
                                             caption=ch_msg, parse_mode="HTML")
                    else:
                        await bot.send_message(chat_id=CHANNEL_ID, text=ch_msg, parse_mode="HTML")
                except TelegramError as e:
                    print(f"Channel post error: {e}")
                await update_product_alert_time(product["id"])
            await update_product_price(product["id"], new_price)
            await asyncio.sleep(3)
        except Exception as e:
            print(f"Error checking {product.get('id')}: {e}")
    print("Price check done.")


async def post_deals_to_channel(bot: Bot):
    """Scrape deals and post to channel — skip already posted ASINs"""
    global _posted_asins
    now = datetime.now()

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

    posted = 0
    for deal in deals:
        asin = deal.get("asin", "")

        # Skip if already posted in last 24h
        if asin in _posted_asins:
            print(f"Skipping {asin} — already posted")
            continue

        try:
            orig = f"<s>{fp(deal['original_price'])}</s> → " if deal.get("original_price") else ""
            pct = f"🏷 خصم <b>{deal['discount_pct']}%</b>\n" if deal.get("discount_pct") else ""
            msg = (
                f"⚡ <b>عرض على أمازون مصر!</b>\n\n"
                f"🛍 {deal['title']}\n\n"
                f"{pct}"
                f"💰 {orig}<b>{fp(deal['price'])}</b>\n\n"
                f"🛒 <a href='{deal['affiliate_url']}'>اشتري دلوقتي</a>\n\n"
                f"📲 @offer_egypt"
            )
            if deal.get("image_url"):
                await bot.send_photo(chat_id=CHANNEL_ID, photo=deal["image_url"],
                                     caption=msg, parse_mode="HTML")
            else:
                await bot.send_message(chat_id=CHANNEL_ID, text=msg, parse_mode="HTML")

            # Mark as posted
            _posted_asins[asin] = now
            posted += 1
            await asyncio.sleep(5)
        except Exception as e:
            print(f"Deal post error: {e}")

    print(f"Posted {posted} new deals (skipped {len(deals) - posted}).")

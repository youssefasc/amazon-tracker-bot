import asyncio
from datetime import datetime, timedelta
from telegram import Bot
from telegram.error import TelegramError

from config import (BOT_TOKEN, CHANNEL_ID, ADMIN_ID,
                    CHECK_INTERVAL_MINUTES, ALERT_COOLDOWN_MINUTES)
from database import (get_all_active_products, update_product_price,
                      update_product_alert_time, get_user)
from scraper import get_current_price


def format_price(price: float) -> str:
    return f"{price:,.0f} جنيه"


def should_alert(product, new_price: float) -> bool:
    """Check if we should send an alert"""
    old_price = product["current_price"]
    if new_price >= old_price:
        return False

    # Check target
    if product["target_price"] and new_price > product["target_price"]:
        return False
    if product["target_percent"]:
        drop_pct = ((old_price - new_price) / old_price) * 100
        if drop_pct < product["target_percent"]:
            return False

    # Check cooldown
    if product["last_alert_at"]:
        try:
            last = datetime.fromisoformat(product["last_alert_at"])
            if datetime.now() - last < timedelta(minutes=ALERT_COOLDOWN_MINUTES):
                return False
        except:
            pass

    return True


async def check_all_prices(bot: Bot):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Starting price check...")
    products = await get_all_active_products()
    print(f"Checking {len(products)} products...")

    for product in products:
        try:
            asin = product["asin"]
            new_price = await get_current_price(asin)

            if new_price is None:
                print(f"Could not get price for {asin}")
                continue

            old_price = product["current_price"]
            print(f"{asin}: {old_price} → {new_price}")

            if should_alert(product, new_price):
                drop = old_price - new_price
                drop_pct = (drop / old_price) * 100

                # Alert user
                user = await get_user(product["user_id"])
                user_name = user["full_name"] if user else "مستخدم"

                user_msg = (
                    f"📉 <b>انخفض السعر!</b>\n\n"
                    f"🛍 {product['title']}\n\n"
                    f"💰 السعر القديم: <s>{format_price(old_price)}</s>\n"
                    f"✅ السعر الجديد: <b>{format_price(new_price)}</b>\n"
                    f"📊 الخصم: <b>{drop_pct:.1f}% ({format_price(drop)})</b>\n\n"
                    f"🔗 <a href='{product['affiliate_url']}'>اشتري دلوقتي</a>"
                )

                try:
                    if product["image_url"]:
                        await bot.send_photo(
                            chat_id=product["user_id"],
                            photo=product["image_url"],
                            caption=user_msg,
                            parse_mode="HTML"
                        )
                    else:
                        await bot.send_message(
                            chat_id=product["user_id"],
                            text=user_msg,
                            parse_mode="HTML"
                        )
                except TelegramError as e:
                    print(f"Failed to alert user {product['user_id']}: {e}")

                # Post to channel
                channel_msg = (
                    f"📉 <b>انخفاض سعر على أمازون مصر!</b>\n\n"
                    f"🛍 {product['title']}\n\n"
                    f"💰 كان: <s>{format_price(old_price)}</s>\n"
                    f"✅ بقى: <b>{format_price(new_price)}</b>\n"
                    f"📊 خصم: <b>{drop_pct:.1f}%</b>\n\n"
                    f"🔗 <a href='{product['affiliate_url']}'>اشتري دلوقتي على أمازون مصر</a>\n\n"
                    f"📲 تابع أكتر: @offer_egypt"
                )

                try:
                    if product["image_url"]:
                        await bot.send_photo(
                            chat_id=CHANNEL_ID,
                            photo=product["image_url"],
                            caption=channel_msg,
                            parse_mode="HTML"
                        )
                    else:
                        await bot.send_message(
                            chat_id=CHANNEL_ID,
                            text=channel_msg,
                            parse_mode="HTML"
                        )
                except TelegramError as e:
                    print(f"Failed to post to channel: {e}")

                await update_product_alert_time(product["id"])

            await update_product_price(product["id"], new_price)
            await asyncio.sleep(3)  # delay between requests

        except Exception as e:
            print(f"Error checking product {product.get('id')}: {e}")
            continue

    print(f"[{datetime.now().strftime('%H:%M:%S')}] Price check done.")

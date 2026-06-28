import asyncio
import re
from telegram import (Update, InlineKeyboardButton, InlineKeyboardMarkup,
                       ReplyKeyboardMarkup, KeyboardButton)
from telegram.ext import (Application, CommandHandler, MessageHandler,
                           CallbackQueryHandler, ContextTypes, filters,
                           ConversationHandler)
from telegram.constants import ParseMode
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from config import (BOT_TOKEN, ADMIN_ID, FREE_LIMIT, INSTAPAY_LINK,
                    CHECK_INTERVAL_MINUTES)
from database import *
from scraper import scrape_amazon_product, search_amazon
from checker import check_all_prices

# States
WAITING_LINK = 1
WAITING_TARGET = 2
WAITING_SEARCH = 3
WAITING_EDIT_TARGET = 4
WAITING_SCREENSHOT = 5


def main_menu():
    return ReplyKeyboardMarkup([
        ["➕ إضافة تتبع سعر جديد"],
        ["📦 منتجاتي", "🔍 بحث عن منتج"],
        ["👑 ترقية الحساب", "📊 إحصائياتي"],
    ], resize_keyboard=True)


def format_price(price: float) -> str:
    return f"{price:,.0f} جنيه"


# ─── /start ───────────────────────────────────────────────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await upsert_user(user.id, user.username or "", user.full_name or "")

    text = (
        f"👋 أهلاً <b>{user.first_name}</b>!\n\n"
        f"أنا بوت تتبع أسعار أمازون مصر 📉\n\n"
        f"ابعتلي أي رابط منتج من أمازون وأنا هراقب سعره وهبعتلك تنبيه لما ينزل!\n\n"
        f"<b>الخطة المجانية:</b> تتابع لحد 5 منتجات\n"
        f"<b>الخطة المدفوعة:</b> غير محدودة بـ 120 جنيه/شهر"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML,
                                    reply_markup=main_menu())


# ─── ADD PRODUCT FLOW ──────────────────────────────────────────────────────────
async def add_track_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    count = await get_user_product_count(user_id)
    premium = await is_premium(user_id)

    if not premium and count >= FREE_LIMIT:
        await update.message.reply_text(
            f"⚠️ وصلت للحد الأقصى في الخطة المجانية ({FREE_LIMIT} منتجات)\n\n"
            f"ترقّى للخطة المدفوعة عشان تتابع منتجات غير محدودة 👑",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("👑 ترقية الحساب", callback_data="upgrade")
            ]])
        )
        return ConversationHandler.END

    await update.message.reply_text(
        "🔗 ابعتلي رابط المنتج من أمازون مصر\n\n"
        "✅ بيقبل روابط عادية وروابط amzn.to المختصرة",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ إلغاء", callback_data="cancel")
        ]])
    )
    return WAITING_LINK


async def receive_link(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()

    amazon_domains = ["amazon", "amzn", "a.co", "link.amazon"]
    if not any(d in url for d in amazon_domains):
        await update.message.reply_text("❌ اللينك ده مش من أمازون، جرب تاني.")
        return WAITING_LINK

    msg = await update.message.reply_text("⏳ بقرأ المنتج، استنى...")

    product = await scrape_amazon_product(url)

    if not product:
        await msg.edit_text(
            "❌ مقدرتش أقرأ المنتج ده.\n\n"
            "📌 <b>المطلوب:</b> رابط من <b>amazon.eg</b> مباشرة\n\n"
            "✅ <b>ازاي تجيب الرابط الصح؟</b>\n"
            "1️⃣ افتح المنتج على أمازون مصر\n"
            "2️⃣ انسخ الرابط من شريط العنوان\n"
            "3️⃣ المفروض يبدأ بـ <code>amazon.eg</code>\n\n"
            "❌ <b>مش بيشتغل:</b>\n"
            "• روابط <code>link.amazon.com</code>\n"
            "• روابط <code>amzn.eu</code>\n"
            "• روابط مشاركة من تطبيق أمازون"
        )
        return WAITING_LINK

    ctx.user_data["pending_product"] = product
    await msg.delete()

    text = (
        f"✅ <b>لقيت المنتج!</b>\n\n"
        f"🛍 {product['title']}\n\n"
        f"💰 السعر الحالي: <b>{format_price(product['price'])}</b>\n\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"دلوقتي اختار طريقة التنبيه:"
    )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("💰 حدد سعر مستهدف", callback_data="target_price")],
        [InlineKeyboardButton("📊 حدد نسبة خصم", callback_data="target_percent")],
        [InlineKeyboardButton("📉 أي انخفاض", callback_data="target_any")],
        [InlineKeyboardButton("❌ إلغاء", callback_data="cancel")],
    ])

    if product["image_url"]:
        await update.message.reply_photo(
            photo=product["image_url"],
            caption=text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard
        )
    else:
        await update.message.reply_text(text, parse_mode=ParseMode.HTML,
                                        reply_markup=keyboard)
    return WAITING_TARGET


async def target_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    product = ctx.user_data.get("pending_product")

    if data == "target_any":
        await save_product(update, ctx, target_price=None, target_percent=None)
        return ConversationHandler.END

    elif data == "target_price":
        ctx.user_data["target_mode"] = "price"
        await query.message.reply_text(
            f"💰 السعر الحالي: <b>{format_price(product['price'])}</b>\n\n"
            f"اكتب السعر المستهدف بالجنيه:",
            parse_mode=ParseMode.HTML
        )
        return WAITING_TARGET

    elif data == "target_percent":
        ctx.user_data["target_mode"] = "percent"
        await query.message.reply_text(
            "📊 اكتب نسبة الخصم المطلوبة (مثلاً: 10 يعني 10%):"
        )
        return WAITING_TARGET

    elif data == "cancel":
        ctx.user_data.clear()
        await query.message.reply_text("❌ تم الإلغاء", reply_markup=main_menu())
        return ConversationHandler.END


async def receive_target(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    mode = ctx.user_data.get("target_mode")
    text = update.message.text.strip()

    try:
        value = float(re.sub(r"[^\d.]", "", text))
    except:
        await update.message.reply_text("❌ رقم غلط، جرب تاني:")
        return WAITING_TARGET

    if mode == "price":
        await save_product(update, ctx, target_price=value, target_percent=None)
    elif mode == "percent":
        await save_product(update, ctx, target_price=None, target_percent=value)

    return ConversationHandler.END


async def save_product(update: Update, ctx: ContextTypes.DEFAULT_TYPE,
                       target_price=None, target_percent=None):
    product = ctx.user_data.get("pending_product")
    user_id = update.effective_user.id

    pid = await add_product(
        user_id=user_id,
        asin=product["asin"],
        title=product["title"],
        url=product["url"],
        affiliate_url=product["affiliate_url"],
        image_url=product["image_url"],
        price=product["price"],
        target_price=target_price,
        target_percent=target_percent,
    )

    if target_price:
        target_text = f"💰 السعر المستهدف: {format_price(target_price)}"
    elif target_percent:
        target_text = f"📊 خصم مستهدف: {target_percent:.0f}%"
    else:
        target_text = "📉 أي انخفاض"

    msg = (
        f"✅ <b>تم إضافة المنتج للمراقبة!</b>\n\n"
        f"🛍 {product['title']}\n"
        f"💰 السعر الحالي: <b>{format_price(product['price'])}</b>\n"
        f"{target_text}\n\n"
        f"هبعتلك تنبيه فوراً لما السعر ينزل 🔔"
    )

    await update.message.reply_text(msg, parse_mode=ParseMode.HTML,
                                    reply_markup=main_menu())
    ctx.user_data.clear()


# ─── MY PRODUCTS ──────────────────────────────────────────────────────────────
async def my_products(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    products = await get_user_products(user_id)

    if not products:
        await update.message.reply_text(
            "📦 مفيش منتجات متابَعة دلوقتي.\n\nاضغط ➕ إضافة تتبع سعر جديد",
            reply_markup=main_menu()
        )
        return

    premium = await is_premium(user_id)
    count = len(products)
    limit_text = "♾️ غير محدودة" if premium else f"{count}/{FREE_LIMIT}"

    await update.message.reply_text(
        f"📦 <b>منتجاتك ({limit_text})</b>\n\nاختار منتج لإدارته:",
        parse_mode=ParseMode.HTML
    )

    for p in products:
        mute_icon = "🔕" if p["is_muted"] else "🔔"
        if p["target_price"]:
            target = f"🎯 {format_price(p['target_price'])}"
        elif p["target_percent"]:
            target = f"🎯 {p['target_percent']:.0f}% خصم"
        else:
            target = "🎯 أي انخفاض"

        text = (
            f"{mute_icon} <b>{p['title'][:60]}...</b>\n"
            f"💰 {format_price(p['current_price'])} · {target}"
        )

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✏️ تعديل الهدف", callback_data=f"edit_{p['id']}"),
                InlineKeyboardButton(
                    "🔕 كتم" if not p["is_muted"] else "🔔 تفعيل",
                    callback_data=f"mute_{p['id']}"
                ),
            ],
            [
                InlineKeyboardButton("🔗 الرابط", url=p["affiliate_url"]),
                InlineKeyboardButton("🗑 حذف", callback_data=f"del_{p['id']}"),
            ],
        ])

        await update.message.reply_text(text, parse_mode=ParseMode.HTML,
                                        reply_markup=keyboard)


async def product_action_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = update.effective_user.id

    if data.startswith("del_"):
        pid = int(data.split("_")[1])
        deleted = await delete_product(pid, user_id)
        if deleted:
            await query.message.edit_text("🗑 تم حذف المنتج.")
        else:
            await query.answer("❌ مش قادر أحذف")

    elif data.startswith("mute_"):
        pid = int(data.split("_")[1])
        muted = await toggle_mute(pid, user_id)
        status = "🔕 تم كتم التنبيهات" if muted else "🔔 تم تفعيل التنبيهات"
        await query.answer(status)
        await query.message.edit_reply_markup(None)

    elif data.startswith("edit_"):
        pid = int(data.split("_")[1])
        ctx.user_data["editing_product_id"] = pid
        await query.message.reply_text(
            "✏️ اختار نوع التعديل:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💰 سعر مستهدف جديد", callback_data=f"editprice_{pid}")],
                [InlineKeyboardButton("📊 نسبة خصم جديدة", callback_data=f"editpct_{pid}")],
                [InlineKeyboardButton("📉 أي انخفاض", callback_data=f"editany_{pid}")],
            ])
        )

    elif data.startswith("editprice_"):
        pid = int(data.split("_")[1])
        ctx.user_data["editing_product_id"] = pid
        ctx.user_data["edit_mode"] = "price"
        await query.message.reply_text("💰 اكتب السعر المستهدف الجديد:")
        return WAITING_EDIT_TARGET

    elif data.startswith("editpct_"):
        pid = int(data.split("_")[1])
        ctx.user_data["editing_product_id"] = pid
        ctx.user_data["edit_mode"] = "percent"
        await query.message.reply_text("📊 اكتب نسبة الخصم الجديدة:")
        return WAITING_EDIT_TARGET

    elif data.startswith("editany_"):
        pid = int(data.split("_")[1])
        await update_target(pid, user_id, None, None)
        await query.message.reply_text("✅ تم التحديث — هتتنبه عند أي انخفاض")

    elif data == "upgrade":
        await upgrade_info(update, ctx)

    elif data == "cancel":
        ctx.user_data.clear()
        await query.message.reply_text("❌ تم الإلغاء", reply_markup=main_menu())
        return ConversationHandler.END


async def receive_edit_target(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    pid = ctx.user_data.get("editing_product_id")
    mode = ctx.user_data.get("edit_mode")
    user_id = update.effective_user.id
    text = update.message.text.strip()

    try:
        value = float(re.sub(r"[^\d.]", "", text))
    except:
        await update.message.reply_text("❌ رقم غلط:")
        return WAITING_EDIT_TARGET

    if mode == "price":
        await update_target(pid, user_id, target_price=value, target_percent=None)
    else:
        await update_target(pid, user_id, target_price=None, target_percent=value)

    await update.message.reply_text("✅ تم تحديث الهدف!", reply_markup=main_menu())
    ctx.user_data.clear()
    return ConversationHandler.END


# ─── SEARCH ───────────────────────────────────────────────────────────────────
async def search_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔍 اكتب اسم المنتج اللي بتدور عليه:",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ إلغاء", callback_data="cancel")
        ]])
    )
    return WAITING_SEARCH


async def receive_search(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query_text = update.message.text.strip()
    msg = await update.message.reply_text("🔍 بدور...")

    results = await search_amazon(query_text)

    if not results:
        await msg.edit_text("❌ مش لاقي نتائج، جرب كلمة تانية.")
        return WAITING_SEARCH

    await msg.delete()
    await update.message.reply_text(f"✅ لقيت {len(results)} نتيجة:")

    for r in results:
        text = (
            f"🛍 <b>{r['title'][:100]}</b>\n"
            f"💰 {format_price(r['price'])}"
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ تتبع السعر", callback_data=f"track_url_{r['asin']}")],
            [InlineKeyboardButton("🔗 فتح في أمازون", url=r["affiliate_url"])],
        ])
        await update.message.reply_text(text, parse_mode=ParseMode.HTML,
                                        reply_markup=keyboard)

    return ConversationHandler.END


# ─── STATS ────────────────────────────────────────────────────────────────────
async def my_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    products = await get_user_products(user_id)
    premium = await is_premium(user_id)
    count = len(products)
    muted = sum(1 for p in products if p["is_muted"])

    text = (
        f"📊 <b>إحصائياتك</b>\n\n"
        f"👑 الخطة: {'مدفوعة ♾️' if premium else f'مجانية ({count}/{FREE_LIMIT})'}\n"
        f"📦 منتجات متابَعة: {count}\n"
        f"🔕 مكتومة: {muted}\n"
        f"🔔 نشطة: {count - muted}"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


# ─── UPGRADE ──────────────────────────────────────────────────────────────────
async def upgrade_info(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (
        "👑 <b>الخطة المدفوعة</b>\n\n"
        "✅ تتابع منتجات غير محدودة\n"
        "✅ تنبيهات فورية\n"
        "✅ أولوية في الفحص\n\n"
        f"💰 <b>السعر: 120 جنيه / شهر</b>\n\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"<b>طريقة الدفع:</b>\n"
        f"1️⃣ ادفع عن طريق الرابط\n"
        f"2️⃣ بعد الدفع ابعت سكرين شوت التحويل هنا\n"
        f"3️⃣ هيتم التفعيل خلال دقايق"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("💳 ادفع دلوقتي (InstaPay)", url=INSTAPAY_LINK)],
        [InlineKeyboardButton("📸 بعت السكرين شوت", callback_data="send_screenshot")],
    ])

    if hasattr(update, "callback_query") and update.callback_query:
        await update.callback_query.message.reply_text(
            text, parse_mode=ParseMode.HTML, reply_markup=keyboard
        )
    else:
        await update.message.reply_text(text, parse_mode=ParseMode.HTML,
                                        reply_markup=keyboard)


async def screenshot_prompt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text(
        "📸 ابعت سكرين شوت التحويل دلوقتي:"
    )
    return WAITING_SCREENSHOT


async def receive_screenshot(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    photo = update.message.photo
    if not photo:
        await update.message.reply_text("❌ ابعت صورة سكرين شوت.")
        return WAITING_SCREENSHOT

    file_id = photo[-1].file_id
    await add_payment_request(user.id, file_id)

    # Forward to admin
    from telegram import Bot
    bot = update.get_bot()
    caption = (
        f"💳 طلب ترقية جديد\n\n"
        f"👤 {user.full_name} (@{user.username})\n"
        f"🆔 {user.id}"
    )
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ تفعيل", callback_data=f"approve_{user.id}"),
        InlineKeyboardButton("❌ رفض", callback_data=f"reject_{user.id}"),
    ]])
    await bot.send_photo(chat_id=ADMIN_ID, photo=file_id,
                         caption=caption, reply_markup=keyboard)

    await update.message.reply_text(
        "✅ تم استلام السكرين شوت!\nهيتم التفعيل خلال دقايق 🕐",
        reply_markup=main_menu()
    )
    return ConversationHandler.END


# ─── ADMIN ────────────────────────────────────────────────────────────────────
async def admin_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if update.effective_user.id != ADMIN_ID:
        await query.answer("❌ مش أدمن")
        return

    await query.answer()
    data = query.data

    if data.startswith("approve_"):
        uid = int(data.split("_")[1])
        await activate_premium(uid)
        await query.message.edit_caption(
            query.message.caption + "\n\n✅ تم التفعيل"
        )
        await ctx.bot.send_message(
            chat_id=uid,
            text="🎉 تم تفعيل حسابك! دلوقتي تقدر تتابع منتجات غير محدودة 👑",
            reply_markup=main_menu()
        )

    elif data.startswith("reject_"):
        uid = int(data.split("_")[1])
        await query.message.edit_caption(
            query.message.caption + "\n\n❌ تم الرفض"
        )
        await ctx.bot.send_message(
            chat_id=uid,
            text="❌ للأسف الدفع مش متأكد. تواصل معنا لو في مشكلة."
        )


async def debug_url(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    args = ctx.args
    if not args:
        await update.message.reply_text("Usage: /debug <url>")
        return
    url = args[0]
    await update.message.reply_text(f"🔍 Testing: {url}")
    try:
        from playwright.async_api import async_playwright
        import asyncio
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                locale="ar-EG",
                extra_http_headers={"Accept-Language": "ar-EG,ar;q=0.9,en;q=0.8"},
            )
            page = await context.new_page()
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=25000)
            status = resp.status if resp else "None"

            # Handle bot check first
            try:
                btn = await page.query_selector("input[type='submit'], button[type='submit'], .a-button-input")
                if btn:
                    await btn.click()
                    await asyncio.sleep(3)
            except:
                pass

            # Wait for redirect
            for _ in range(10):
                await asyncio.sleep(1)
                if "amazon.eg" in page.url and "/dp/" in page.url:
                    break

            url_final = page.url
            title = await page.title()

            # Try to find product title
            prod_title = "NOT FOUND"
            for sel in ["#productTitle", "span#productTitle"]:
                try:
                    await page.wait_for_selector(sel, timeout=5000)
                    el = await page.query_selector(sel)
                    if el:
                        prod_title = (await el.inner_text()).strip()[:80]
                        break
                except:
                    pass

            # Try to find price
            prod_price = "NOT FOUND"
            for sel in ["span.priceToPay span.a-price-whole", ".apexPriceToPay span.a-price-whole", "span.a-price-whole"]:
                try:
                    el = await page.query_selector(sel)
                    if el:
                        prod_price = (await el.inner_text()).strip()
                        break
                except:
                    pass

            # Get all text selectors found on page
            found_selectors = []
            for sel in ["#productTitle", "span.a-price-whole", "#landingImage", ".apexPriceToPay"]:
                try:
                    el = await page.query_selector(sel)
                    found_selectors.append(f"{'✅' if el else '❌'} {sel}")
                except:
                    found_selectors.append(f"❌ {sel}")

            # Screenshot
            screenshot = await page.screenshot(full_page=False)
            await browser.close()

        msg = (
            f"📊 Debug Result:\n\n"
            f"HTTP: {status}\n"
            f"Final URL: {url_final[:80]}\n"
            f"Page Title: {title}\n\n"
            f"Product Title: {prod_title}\n"
            f"Price: {prod_price}\n\n"
            f"Selectors:\n" + "\n".join(found_selectors)
        )
        await update.message.reply_text(msg)
        await update.message.reply_photo(photo=screenshot, caption="📸 Screenshot of the page")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")


async def admin_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    users = await get_all_users()
    total = len(users)
    premium_count = sum(1 for u in users if u["is_premium"])
    products = await get_all_active_products()

    text = (
        f"📊 <b>إحصائيات البوت</b>\n\n"
        f"👥 إجمالي المستخدمين: {total}\n"
        f"👑 مشتركين مدفوعين: {premium_count}\n"
        f"📦 منتجات تحت المراقبة: {len(products)}"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


# ─── MAIN ─────────────────────────────────────────────────────────────────────
async def post_init(app: Application):
    await init_db()
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        check_all_prices,
        "interval",
        minutes=CHECK_INTERVAL_MINUTES,
        args=[app.bot]
    )
    scheduler.start()
    print(f"✅ Scheduler started — checking every {CHECK_INTERVAL_MINUTES} min")


def main():
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    # Add product conversation
    add_conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex("^➕ إضافة تتبع سعر جديد$"), add_track_start),
            CallbackQueryHandler(add_track_start, pattern="^add_new$"),
        ],
        states={
            WAITING_LINK: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_link)],
            WAITING_TARGET: [
                CallbackQueryHandler(target_callback, pattern="^(target_|cancel)"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_target),
            ],
        },
        fallbacks=[CallbackQueryHandler(target_callback, pattern="^cancel$")],
    )

    # Search conversation
    search_conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex("^🔍 بحث عن منتج$"), search_start),
        ],
        states={
            WAITING_SEARCH: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_search)],
        },
        fallbacks=[CallbackQueryHandler(lambda u, c: ConversationHandler.END, pattern="^cancel$")],
    )

    # Screenshot conversation
    screenshot_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(screenshot_prompt, pattern="^send_screenshot$")],
        states={
            WAITING_SCREENSHOT: [MessageHandler(filters.PHOTO, receive_screenshot)],
        },
        fallbacks=[],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stats", admin_stats))
    app.add_handler(CommandHandler("debug", debug_url))
    app.add_handler(add_conv)
    app.add_handler(search_conv)
    app.add_handler(screenshot_conv)
    app.add_handler(MessageHandler(filters.Regex("^📦 منتجاتي$"), my_products))
    app.add_handler(MessageHandler(filters.Regex("^👑 ترقية الحساب$"), upgrade_info))
    app.add_handler(MessageHandler(filters.Regex("^📊 إحصائياتي$"), my_stats))
    app.add_handler(CallbackQueryHandler(product_action_callback,
                                         pattern="^(del_|mute_|edit|upgrade|cancel)"))
    app.add_handler(CallbackQueryHandler(admin_callback, pattern="^(approve_|reject_)"))

    print("🚀 Bot started!")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()

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
                    CHECK_INTERVAL_MINUTES, DEALS_POST_INTERVAL_HOURS,
                    CHANNEL_ID, CHANNEL_LINK)
from database import *
from scraper import scrape_amazon_product, search_amazon
from checker import check_all_prices, post_deals_to_channel

# ── States ────────────────────────────────────────────────────────────────────
WAITING_LINK = 1
WAITING_TARGET = 2
WAITING_SEARCH = 3
WAITING_EDIT_TARGET = 4
WAITING_SCREENSHOT = 5
WAITING_COUPON = 6
WAITING_FEEDBACK = 7
WAITING_ADMIN_COUPON = 8
WAITING_ADMIN_USER = 9


def fp(price: float) -> str:
    return f"{price:,.0f} جنيه"


# ── Keyboards ─────────────────────────────────────────────────────────────────
def main_menu():
    return ReplyKeyboardMarkup([
        ["➕ إضافة تتبع سعر جديد", "📦 منتجاتي"],
        ["🔍 بحث عن منتج أمازون", "📊 إحصائياتي"],
        ["👤 حسابي", "💎 الباقات"],
        ["🎁 شارك واربح", "🎫 استخدام كوبون"],
        ["🏆 المسابقة", "📢 قناة العروض"],
        ["❓ المساعدة"],
    ], resize_keyboard=True)


def channel_btn():
    return InlineKeyboardButton("📢 عروض متتفوتش", url=CHANNEL_LINK)


# ── Channel membership check ──────────────────────────────────────────────────
async def is_member(bot, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(CHANNEL_ID, user_id)
        return member.status not in ["left", "kicked", "banned"]
    except:
        return False


async def check_membership(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    user_id = update.effective_user.id
    if user_id == ADMIN_ID:
        return True
    if not await is_member(ctx.bot, user_id):
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📢 اشترك في القناة", url=CHANNEL_LINK)],
            [InlineKeyboardButton("✅ اشتركت، تحقق", callback_data="check_membership")],
        ])
        await update.effective_message.reply_text(
            "⚠️ <b>لازم تشترك في قناتنا الأول عشان تستخدم البوت!</b>\n\n"
            "📢 قناة عروض متتفوتش — فيها أحسن عروض أمازون مصر يومياً\n\n"
            "بعد الاشتراك اضغط ✅ تحقق",
            parse_mode=ParseMode.HTML, reply_markup=keyboard
        )
        return False
    return True


# ── /start ────────────────────────────────────────────────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    referred_by = None
    if ctx.args:
        try:
            referred_by = int(ctx.args[0])
            if referred_by == user.id:
                referred_by = None
        except:
            pass
    await upsert_user(user.id, user.username or "", user.full_name or "", referred_by)

    if not await is_member(ctx.bot, user.id) and user.id != ADMIN_ID:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📢 اشترك في القناة", url=CHANNEL_LINK)],
            [InlineKeyboardButton("✅ اشتركت، تحقق", callback_data="check_membership")],
        ])
        await update.message.reply_text(
            f"👋 أهلاً <b>{user.first_name}</b>!\n\n"
            "⚠️ لازم تشترك في قناتنا الأول:",
            parse_mode=ParseMode.HTML, reply_markup=keyboard
        )
        return

    limit = await get_user_limit(user.id)
    count = await get_user_product_count(user.id)
    prem = await is_premium(user.id)
    plan = "👑 مدفوعة ♾️" if prem else f"🆓 مجانية ({count}/{FREE_LIMIT})"

    text = (
        f"👋 أهلاً <b>{user.first_name}</b>!\n\n"
        f"أنا بوت تتبع أسعار أمازون مصر 📉\n\n"
        f"ابعتلي أي رابط منتج من أمازون وأنا هراقب سعره وهبعتلك تنبيه لما ينزل!\n\n"
        f"📋 خطتك الحالية: <b>{plan}</b>\n\n"
        f"اختار من القائمة 👇"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=main_menu())


async def check_membership_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    if await is_member(ctx.bot, user.id):
        await query.message.edit_text("✅ تم التحقق! اضغط /start")
    else:
        await query.answer("❌ لسه مشتركتش في القناة!", show_alert=True)


# ── ADD PRODUCT ───────────────────────────────────────────────────────────────
async def add_track_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await check_membership(update, ctx):
        return ConversationHandler.END
    user_id = update.effective_user.id
    count = await get_user_product_count(user_id)
    limit = await get_user_limit(user_id)
    if count >= limit:
        prem = await is_premium(user_id)
        if prem:
            await update.effective_message.reply_text("⚠️ وصلت للحد الأقصى!")
        else:
            await update.effective_message.reply_text(
                f"⚠️ وصلت للحد الأقصى في الخطة المجانية ({limit} منتجات)\n\n"
                "ترقّى للخطة المدفوعة للمتابعة بلا حدود 👑",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("💎 الباقات", callback_data="show_plans")],
                    [channel_btn()]
                ])
            )
        return ConversationHandler.END
    await update.effective_message.reply_text(
        "🔗 ابعتلي رابط المنتج من أمازون مصر\n\n"
        "✅ بيقبل روابط عادية وروابط amzn.to/amzn.eu المختصرة",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data="cancel")]])
    )
    return WAITING_LINK


async def receive_link(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    url = update.effective_message.text.strip()
    amazon_domains = ["amazon", "amzn", "a.co", "link.amazon"]
    if not any(d in url for d in amazon_domains):
        await update.effective_message.reply_text("❌ اللينك ده مش من أمازون، جرب تاني.")
        return WAITING_LINK
    msg = await update.effective_message.reply_text("⏳ بقرأ المنتج، استنى...")
    product = await scrape_amazon_product(url)
    if not product:
        await msg.edit_text(
            "❌ مقدرتش أقرأ المنتج ده.\n\n"
            "📌 ابعتلي رابط من <b>amazon.eg</b> مباشرة\n"
            "مثال: https://www.amazon.eg/dp/XXXXXXXXXX",
            parse_mode=ParseMode.HTML
        )
        return WAITING_LINK
    ctx.user_data["pending_product"] = product
    await msg.delete()
    text = (
        f"✅ <b>لقيت المنتج!</b>\n\n"
        f"🛍 {product['title']}\n\n"
        f"💰 السعر الحالي: <b>{fp(product['price'])}</b>\n\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"اختار طريقة التنبيه:"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("💰 سعر مستهدف", callback_data="target_price")],
        [InlineKeyboardButton("📊 نسبة خصم", callback_data="target_percent")],
        [InlineKeyboardButton("📉 أي انخفاض", callback_data="target_any")],
        [InlineKeyboardButton("❌ إلغاء", callback_data="cancel")],
    ])
    if product["image_url"]:
        await update.effective_message.reply_photo(photo=product["image_url"],
                                                    caption=text, parse_mode=ParseMode.HTML,
                                                    reply_markup=keyboard)
    else:
        await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML,
                                                   reply_markup=keyboard)
    return WAITING_TARGET


async def target_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    product = ctx.user_data.get("pending_product")
    if data == "target_any":
        await save_product(update, ctx, None, None)
        return ConversationHandler.END
    elif data == "target_price":
        ctx.user_data["target_mode"] = "price"
        await query.message.reply_text(
            f"💰 السعر الحالي: <b>{fp(product['price'])}</b>\n\nاكتب السعر المستهدف:",
            parse_mode=ParseMode.HTML)
        return WAITING_TARGET
    elif data == "target_percent":
        ctx.user_data["target_mode"] = "percent"
        await query.message.reply_text("📊 اكتب نسبة الخصم المطلوبة (مثلاً: 10):")
        return WAITING_TARGET
    elif data == "cancel":
        ctx.user_data.clear()
        await query.message.reply_text("❌ تم الإلغاء", reply_markup=main_menu())
        return ConversationHandler.END


async def receive_target(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    mode = ctx.user_data.get("target_mode")
    try:
        value = float(re.sub(r"[^\d.]", "", update.effective_message.text.strip()))
    except:
        await update.effective_message.reply_text("❌ رقم غلط:")
        return WAITING_TARGET
    if mode == "price":
        await save_product(update, ctx, value, None)
    else:
        await save_product(update, ctx, None, value)
    return ConversationHandler.END


async def save_product(update: Update, ctx: ContextTypes.DEFAULT_TYPE,
                       target_price=None, target_percent=None):
    product = ctx.user_data.get("pending_product")
    user_id = update.effective_user.id
    await add_product(user_id=user_id, asin=product["asin"], title=product["title"],
                      url=product["url"], affiliate_url=product["affiliate_url"],
                      image_url=product["image_url"], price=product["price"],
                      target_price=target_price, target_percent=target_percent)
    if target_price:
        target_text = f"💰 السعر المستهدف: {fp(target_price)}"
    elif target_percent:
        target_text = f"📊 خصم مستهدف: {target_percent:.0f}%"
    else:
        target_text = "📉 أي انخفاض"
    await update.effective_message.reply_text(
        f"✅ <b>تم إضافة المنتج!</b>\n\n🛍 {product['title']}\n"
        f"💰 {fp(product['price'])}\n{target_text}\n\n🔔 هبعتلك تنبيه لما ينزل!",
        parse_mode=ParseMode.HTML, reply_markup=main_menu()
    )
    ctx.user_data.clear()


# ── MY PRODUCTS ───────────────────────────────────────────────────────────────
async def my_products(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await check_membership(update, ctx):
        return
    user_id = update.effective_user.id
    products = await get_user_products(user_id)
    limit = await get_user_limit(user_id)
    prem = await is_premium(user_id)
    limit_text = "♾️" if prem else f"{len(products)}/{limit}"
    if not products:
        await update.effective_message.reply_text(
            "📦 مفيش منتجات متابَعة.\n\nاضغط ➕ إضافة تتبع سعر جديد",
            reply_markup=InlineKeyboardMarkup([[channel_btn()]])
        )
        return
    await update.effective_message.reply_text(
        f"📦 <b>منتجاتك ({limit_text})</b>", parse_mode=ParseMode.HTML)
    for p in products:
        mute_icon = "🔕" if p["is_muted"] else "🔔"
        target = (f"🎯 {fp(p['target_price'])}" if p["target_price"]
                  else f"🎯 {p['target_percent']:.0f}%" if p["target_percent"]
                  else "🎯 أي انخفاض")
        text = (
            f"{mute_icon} <b>{p['title'][:60]}</b>\n"
            f"💰 {fp(p['current_price'])} · {target}"
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✏️ تعديل الهدف", callback_data=f"edit_{p['id']}"),
             InlineKeyboardButton("🔕 كتم" if not p["is_muted"] else "🔔 تفعيل",
                                  callback_data=f"mute_{p['id']}")],
            [InlineKeyboardButton("🔗 الرابط", url=p["affiliate_url"]),
             InlineKeyboardButton("🗑 حذف", callback_data=f"del_{p['id']}")],
            [channel_btn()],
        ])
        await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML,
                                                   reply_markup=keyboard)


# ── ACCOUNT ───────────────────────────────────────────────────────────────────
async def my_account(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await check_membership(update, ctx):
        return
    user = update.effective_user
    db_user = await get_user(user.id)
    prem = await is_premium(user.id)
    limit = await get_user_limit(user.id)
    count = await get_user_product_count(user.id)
    if prem:
        plan = "👑 مدفوعة"
        if db_user and db_user["premium_expires"]:
            plan += f"\nينتهي: {db_user['premium_expires'][:10]}"
        else:
            plan += " (دائمة)"
    else:
        plan = f"🆓 مجانية ({count}/{limit} منتجات)"

    ref_link = f"https://t.me/{(await ctx.bot.get_me()).username}?start={user.id}"
    text = (
        f"👤 <b>حسابك</b>\n\n"
        f"الاسم: {user.full_name}\n"
        f"ID: <code>{user.id}</code>\n"
        f"الخطة: {plan}\n\n"
        f"🔗 رابط الإحالة:\n<code>{ref_link}</code>"
    )
    await update.effective_message.reply_text(
        text, parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[channel_btn()]])
    )


# ── PLANS ─────────────────────────────────────────────────────────────────────
async def show_plans(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await check_membership(update, ctx):
        return
    user_id = update.effective_user.id
    prem = await is_premium(user_id)
    count = await get_user_product_count(user_id)
    limit = await get_user_limit(user_id)
    current = "👑 مدفوعة ♾️" if prem else f"🆓 مجانية ({count}/{limit})"
    text = (
        f"💎 <b>الباقات</b>\n\n"
        f"خطتك الحالية: <b>{current}</b>\n\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🆓 <b>مجانية</b>\n"
        f"• {FREE_LIMIT} منتجات\n"
        f"• تنبيهات فورية\n\n"
        f"👑 <b>مدفوعة — 120 جنيه/شهر</b>\n"
        f"• منتجات غير محدودة ♾️\n"
        f"• تنبيهات فورية\n"
        f"• أولوية في الفحص\n\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"ادفع عن طريق InstaPay ثم ابعت سكرين شوت"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("💳 ادفع دلوقتي (InstaPay)", url=INSTAPAY_LINK)],
        [InlineKeyboardButton("📸 بعت السكرين شوت", callback_data="send_screenshot")],
        [channel_btn()],
    ])
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML,
                                               reply_markup=keyboard)


async def plans_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await show_plans(update, ctx)


# ── SCREENSHOT / PAYMENT ──────────────────────────────────────────────────────
async def screenshot_prompt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text("📸 ابعت سكرين شوت التحويل:")
    return WAITING_SCREENSHOT


async def receive_screenshot(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    photo = update.effective_message.photo
    if not photo:
        await update.effective_message.reply_text("❌ ابعت صورة.")
        return WAITING_SCREENSHOT
    file_id = photo[-1].file_id
    await add_payment_request(user.id, file_id)
    caption = (f"💳 طلب ترقية\n👤 {user.full_name} (@{user.username})\n🆔 {user.id}")
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ تفعيل 30 يوم", callback_data=f"approve_30_{user.id}"),
        InlineKeyboardButton("✅ تفعيل دائم", callback_data=f"approve_0_{user.id}"),
        InlineKeyboardButton("❌ رفض", callback_data=f"reject_{user.id}"),
    ]])
    await ctx.bot.send_photo(chat_id=ADMIN_ID, photo=file_id,
                              caption=caption, reply_markup=keyboard)
    await update.effective_message.reply_text(
        "✅ تم استلام السكرين شوت!\nهيتم التفعيل خلال دقايق 🕐",
        reply_markup=main_menu()
    )
    return ConversationHandler.END


# ── REFERRAL ──────────────────────────────────────────────────────────────────
async def share_and_earn(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await check_membership(update, ctx):
        return
    user = update.effective_user
    bot_info = await ctx.bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start={user.id}"
    limit = await get_user_limit(user.id)
    count = await get_user_product_count(user.id)
    text = (
        f"🎁 <b>شارك واربح!</b>\n\n"
        f"لما حد يسجل عن طريق رابطك، هتاخد +1 منتج لمدة شهر\n\n"
        f"📊 منتجاتك الحالية: {count}/{limit}\n\n"
        f"🔗 رابطك الخاص:\n<code>{ref_link}</code>\n\n"
        f"شارك الرابط مع أصحابك وابدأ تكسب! 🚀"
    )
    await update.effective_message.reply_text(
        text, parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[channel_btn()]])
    )


# ── COUPON ────────────────────────────────────────────────────────────────────
async def coupon_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await check_membership(update, ctx):
        return ConversationHandler.END
    await update.effective_message.reply_text(
        "🎫 اكتب كود الكوبون:",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data="cancel")]])
    )
    return WAITING_COUPON


async def receive_coupon(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    code = update.effective_message.text.strip()
    user_id = update.effective_user.id
    result = await use_coupon(code, user_id)
    if not result:
        await update.effective_message.reply_text(
            "❌ الكوبون ده غلط أو منتهي أو استخدمته قبل كده.",
            reply_markup=main_menu()
        )
    else:
        benefits = []
        if result["days"]:
            benefits.append(f"👑 {result['days']} يوم اشتراك مدفوع")
        if result["extra_slots"]:
            benefits.append(f"📦 +{result['extra_slots']} منتج إضافي لمدة شهر")
        await update.effective_message.reply_text(
            f"🎉 <b>تم تفعيل الكوبون!</b>\n\n" + "\n".join(benefits),
            parse_mode=ParseMode.HTML, reply_markup=main_menu()
        )
    return ConversationHandler.END


# ── HELP ──────────────────────────────────────────────────────────────────────
async def help_section(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (
        "❓ <b>المساعدة</b>\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "➕ <b>إضافة تتبع سعر:</b>\n"
        "ابعت رابط المنتج من أمازون وحدد السعر المستهدف\n\n"
        "📦 <b>منتجاتي:</b>\n"
        "شوف وعدّل وامسح المنتجات المتابَعة\n\n"
        "🔍 <b>البحث:</b>\n"
        "ابحث عن أي منتج على أمازون مصر\n\n"
        "📊 <b>إحصائياتي:</b>\n"
        "شوف إحصائيات حسابك\n\n"
        "👤 <b>حسابي:</b>\n"
        "بياناتك ورابط الإحالة\n\n"
        "💎 <b>الباقات:</b>\n"
        "ترقّى للخطة المدفوعة\n\n"
        "🎁 <b>شارك واربح:</b>\n"
        "ادعو أصحابك واكسب منتجات إضافية\n\n"
        "🎫 <b>كوبون:</b>\n"
        "استخدم كود خصم\n\n"
        "━━━━━━━━━━━━━━━━━━"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🐛 مشكلة أو اقتراح", callback_data="feedback")],
        [channel_btn()],
    ])
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML,
                                               reply_markup=keyboard)


async def feedback_prompt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await update.callback_query.message.reply_text(
        "✍️ اكتب مشكلتك أو اقتراحك وهيتم توجيهه للأدمن:"
    )
    return WAITING_FEEDBACK


async def receive_feedback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.effective_message.text
    await ctx.bot.send_message(
        chat_id=ADMIN_ID,
        text=f"📩 رسالة من {user.full_name} (@{user.username}) [{user.id}]:\n\n{text}"
    )
    await update.effective_message.reply_text("✅ تم الإرسال للأدمن، شكراً!",
                                               reply_markup=main_menu())
    return ConversationHandler.END


# ── STATS ─────────────────────────────────────────────────────────────────────
async def my_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await check_membership(update, ctx):
        return
    user_id = update.effective_user.id
    products = await get_user_products(user_id)
    prem = await is_premium(user_id)
    limit = await get_user_limit(user_id)
    count = len(products)
    muted = sum(1 for p in products if p["is_muted"])
    text = (
        f"📊 <b>إحصائياتك</b>\n\n"
        f"👑 الخطة: {'مدفوعة ♾️' if prem else f'مجانية ({count}/{limit})'}\n"
        f"📦 منتجات متابَعة: {count}\n"
        f"🔕 مكتومة: {muted}\n"
        f"🔔 نشطة: {count - muted}"
    )
    await update.effective_message.reply_text(
        text, parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[channel_btn()]])
    )


# ── SEARCH ────────────────────────────────────────────────────────────────────
async def search_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await check_membership(update, ctx):
        return ConversationHandler.END
    await update.effective_message.reply_text(
        "🔍 اكتب اسم المنتج بالتفصيل:\n"
        "مثال: <i>آيفون 15 برو ماكس 256GB أسود</i>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data="cancel")]])
    )
    return WAITING_SEARCH


async def receive_search(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query_text = update.effective_message.text.strip()
    msg = await update.effective_message.reply_text("🔍 بدور...")
    results = await search_amazon(query_text)
    if not results:
        await msg.edit_text("❌ مش لاقي نتائج، جرب كلمة تانية.")
        return WAITING_SEARCH
    await msg.delete()
    await update.effective_message.reply_text(f"✅ لقيت {len(results)} نتيجة:")
    for r in results:
        rating_text = f"⭐ {r['rating']}\n" if r.get("rating") else ""
        text = (
            f"🛍 <b>{r['title'][:100]}</b>\n\n"
            f"{rating_text}💰 <b>{fp(r['price'])}</b>"
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ تتبع السعر", callback_data=f"track_url_{r['asin']}")],
            [InlineKeyboardButton("🔗 فتح في أمازون", url=r["affiliate_url"])],
            [channel_btn()],
        ])
        try:
            if r.get("image_url"):
                await update.effective_message.reply_photo(
                    photo=r["image_url"], caption=text,
                    parse_mode=ParseMode.HTML, reply_markup=keyboard
                )
            else:
                await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML,
                                                           reply_markup=keyboard)
        except:
            await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML,
                                                       reply_markup=keyboard)
    return ConversationHandler.END


# ── COMPETITION ───────────────────────────────────────────────────────────────
async def competition(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await check_membership(update, ctx):
        return
    await update.effective_message.reply_text(
        "🏆 <b>المسابقة</b>\n\nقريباً! ترقّب الإعلانات في القناة 👇",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[channel_btn()]])
    )


# ── CHANNEL BUTTON ────────────────────────────────────────────────────────────
async def channel_section(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "📢 <b>قناة عروض متتفوتش</b>\n\nتابعنا على القناة لأحسن عروض أمازون مصر يومياً! 🔥",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[channel_btn()]])
    )


# ── PRODUCT ACTIONS CALLBACKS ─────────────────────────────────────────────────
async def product_action_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = update.effective_user.id

    if data.startswith("del_"):
        pid = int(data.split("_")[1])
        if await delete_product(pid, user_id):
            await query.message.edit_text("🗑 تم الحذف.")
        else:
            await query.answer("❌ خطأ", show_alert=True)

    elif data.startswith("mute_"):
        pid = int(data.split("_")[1])
        muted = await toggle_mute(pid, user_id)
        await query.answer("🔕 تم الكتم" if muted else "🔔 تم التفعيل")

    elif data.startswith("edit_"):
        pid = int(data.split("_")[1])
        await query.message.reply_text(
            "✏️ اختار نوع التعديل:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💰 سعر مستهدف", callback_data=f"editprice_{pid}")],
                [InlineKeyboardButton("📊 نسبة خصم", callback_data=f"editpct_{pid}")],
                [InlineKeyboardButton("📉 أي انخفاض", callback_data=f"editany_{pid}")],
            ])
        )

    elif data.startswith("editany_"):
        pid = int(data.split("_")[1])
        await update_target(pid, user_id, None, None)
        await query.message.reply_text("✅ تم — هتتنبه عند أي انخفاض")

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

    elif data.startswith("track_url_"):
        asin = data.replace("track_url_", "")
        url = f"https://www.amazon.eg/dp/{asin}"
        ctx.user_data["pending_url"] = url
        count = await get_user_product_count(user_id)
        limit = await get_user_limit(user_id)
        if count >= limit:
            await query.message.reply_text(
                "⚠️ وصلت للحد الأقصى! ترقّى للخطة المدفوعة.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💎 الباقات", callback_data="show_plans")]])
            )
            return
        msg = await query.message.reply_text("⏳ بجيب بيانات المنتج...")
        product = await scrape_amazon_product(url)
        if not product:
            await msg.edit_text("❌ مقدرتش أقرأ المنتج.")
            return
        ctx.user_data["pending_product"] = product
        await msg.delete()
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("💰 سعر مستهدف", callback_data="target_price")],
            [InlineKeyboardButton("📊 نسبة خصم", callback_data="target_percent")],
            [InlineKeyboardButton("📉 أي انخفاض", callback_data="target_any")],
        ])
        await query.message.reply_text(
            f"✅ <b>{product['title']}</b>\n💰 {fp(product['price'])}\n\nاختار طريقة التنبيه:",
            parse_mode=ParseMode.HTML, reply_markup=keyboard
        )

    elif data == "show_plans":
        await show_plans(update, ctx)

    elif data == "cancel":
        ctx.user_data.clear()
        await query.message.reply_text("❌ تم الإلغاء", reply_markup=main_menu())
        return ConversationHandler.END


async def receive_edit_target(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    pid = ctx.user_data.get("editing_product_id")
    mode = ctx.user_data.get("edit_mode")
    user_id = update.effective_user.id
    try:
        value = float(re.sub(r"[^\d.]", "", update.effective_message.text.strip()))
    except:
        await update.effective_message.reply_text("❌ رقم غلط:")
        return WAITING_EDIT_TARGET
    if mode == "price":
        await update_target(pid, user_id, target_price=value, target_percent=None)
    else:
        await update_target(pid, user_id, target_price=None, target_percent=value)
    await update.effective_message.reply_text("✅ تم التحديث!", reply_markup=main_menu())
    ctx.user_data.clear()
    return ConversationHandler.END


# ── ADMIN ─────────────────────────────────────────────────────────────────────
async def admin_panel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    users = await get_all_users()
    products = await get_all_active_products()
    premium_count = sum(1 for u in users if u["is_premium"])
    text = (
        f"🔧 <b>لوحة التحكم</b>\n\n"
        f"👥 إجمالي المستخدمين: {len(users)}\n"
        f"👑 مشتركين مدفوعين: {premium_count}\n"
        f"📦 منتجات تحت المراقبة: {len(products)}"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ تفعيل باقة", callback_data="admin_activate"),
         InlineKeyboardButton("❌ إلغاء باقة", callback_data="admin_revoke")],
        [InlineKeyboardButton("🎫 إنشاء كوبون", callback_data="admin_coupon")],
        [InlineKeyboardButton("⚡ نشر عروض الآن", callback_data="admin_post_deals")],
        [InlineKeyboardButton("👥 قائمة المستخدمين", callback_data="admin_users")],
    ])
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML,
                                               reply_markup=keyboard)


async def admin_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if update.effective_user.id != ADMIN_ID:
        await query.answer("❌ مش أدمن")
        return
    await query.answer()
    data = query.data

    if data.startswith("approve_"):
        parts = data.split("_")
        days = int(parts[1])
        uid = int(parts[2])
        await activate_premium(uid, days if days > 0 else None)
        await query.message.edit_caption(query.message.caption + "\n\n✅ تم التفعيل")
        await ctx.bot.send_message(chat_id=uid,
                                   text="🎉 تم تفعيل حسابك المدفوع! ♾️ منتجات غير محدودة",
                                   reply_markup=main_menu())

    elif data.startswith("reject_"):
        uid = int(data.split("_")[1])
        await query.message.edit_caption(query.message.caption + "\n\n❌ تم الرفض")
        await ctx.bot.send_message(chat_id=uid,
                                   text="❌ الدفع مش متأكد. تواصل معنا لو في مشكلة.")

    elif data == "admin_activate":
        ctx.user_data["admin_action"] = "activate"
        await query.message.reply_text("🆔 ابعت ID المستخدم وعدد الأيام\nمثال: 123456789 30")
        return WAITING_ADMIN_USER

    elif data == "admin_revoke":
        ctx.user_data["admin_action"] = "revoke"
        await query.message.reply_text("🆔 ابعت ID المستخدم:")
        return WAITING_ADMIN_USER

    elif data == "admin_coupon":
        await query.message.reply_text(
            "🎫 اكتب بيانات الكوبون:\n"
            "<code>CODE DAYS SLOTS MAX_USES</code>\n\n"
            "مثال: <code>SAVE20 30 0 100</code>\n"
            "= كوبون شهر مدفوع، 100 استخدام\n\n"
            "أو: <code>EXTRA5 0 5 50</code>\n"
            "= 5 منتجات إضافية، 50 استخدام",
            parse_mode=ParseMode.HTML
        )
        return WAITING_ADMIN_COUPON

    elif data == "admin_post_deals":
        await query.message.reply_text("⏳ بينزل العروض على القناة...")
        await post_deals_to_channel(ctx.bot)
        await query.message.reply_text("✅ تم نشر العروض!")

    elif data == "admin_users":
        users = await get_all_users()
        text = "👥 <b>المستخدمين:</b>\n\n"
        for u in users[:20]:
            prem = "👑" if u["is_premium"] else "🆓"
            text += f"{prem} {u['full_name']} [{u['user_id']}]\n"
        if len(users) > 20:
            text += f"\n... و {len(users)-20} آخرين"
        await query.message.reply_text(text, parse_mode=ParseMode.HTML)


async def admin_user_action(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return ConversationHandler.END
    action = ctx.user_data.get("admin_action")
    text = update.effective_message.text.strip().split()
    try:
        uid = int(text[0])
        if action == "activate":
            days = int(text[1]) if len(text) > 1 else 30
            await activate_premium(uid, days)
            await update.effective_message.reply_text(f"✅ تم تفعيل {uid} لمدة {days} يوم")
        elif action == "revoke":
            await revoke_premium(uid)
            await update.effective_message.reply_text(f"✅ تم إلغاء باقة {uid}")
    except:
        await update.effective_message.reply_text("❌ خطأ في البيانات")
    return ConversationHandler.END


async def admin_coupon_action(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return ConversationHandler.END
    parts = update.effective_message.text.strip().split()
    try:
        code = parts[0]
        days = int(parts[1]) if len(parts) > 1 else 0
        slots = int(parts[2]) if len(parts) > 2 else 0
        max_uses = int(parts[3]) if len(parts) > 3 else 1
        success = await create_coupon(code, days, slots, max_uses)
        if success:
            benefits = []
            if days:
                benefits.append(f"{days} يوم مدفوع")
            if slots:
                benefits.append(f"+{slots} منتج إضافي")
            await update.effective_message.reply_text(
                f"✅ تم إنشاء الكوبون!\n\n"
                f"🎫 الكود: <code>{code.upper()}</code>\n"
                f"🎁 المزايا: {', '.join(benefits)}\n"
                f"👥 عدد الاستخدامات: {max_uses}",
                parse_mode=ParseMode.HTML
            )
        else:
            await update.effective_message.reply_text("❌ الكود موجود بالفعل")
    except:
        await update.effective_message.reply_text("❌ خطأ في البيانات")
    return ConversationHandler.END


# ── DEBUG ─────────────────────────────────────────────────────────────────────
async def debug_url(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    args = ctx.args
    if not args:
        await update.effective_message.reply_text("Usage: /debug <url>")
        return
    url = args[0]
    await update.effective_message.reply_text(f"🔍 Testing: {url}")
    try:
        from playwright.async_api import async_playwright
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
            try:
                btn = await page.query_selector("input[type='submit'], button[type='submit'], .a-button-input")
                if btn:
                    await btn.click()
                    await asyncio.sleep(3)
            except:
                pass
            for _ in range(10):
                await asyncio.sleep(1)
                if "amazon.eg" in page.url and "/dp/" in page.url:
                    break
            url_final = page.url
            title = await page.title()
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
            prod_price = "NOT FOUND"
            for sel in ["span.priceToPay span.a-price-whole", ".apexPriceToPay span.a-price-whole", "span.a-price-whole"]:
                try:
                    el = await page.query_selector(sel)
                    if el:
                        prod_price = (await el.inner_text()).strip()
                        break
                except:
                    pass
            screenshot = await page.screenshot(full_page=False)
            await browser.close()
        msg = (f"📊 Debug Result:\n\nHTTP: {status}\nFinal URL: {url_final[:80]}\n"
               f"Page Title: {title}\n\nProduct Title: {prod_title}\nPrice: {prod_price}")
        await update.effective_message.reply_text(msg)
        await update.effective_message.reply_photo(photo=screenshot, caption="📸 Screenshot")
    except Exception as e:
        await update.effective_message.reply_text(f"❌ Error: {e}")


# ── MAIN ──────────────────────────────────────────────────────────────────────
async def post_init(app: Application):
    await init_db()
    scheduler = AsyncIOScheduler()
    scheduler.add_job(check_all_prices, "interval",
                      minutes=CHECK_INTERVAL_MINUTES, args=[app.bot])
    scheduler.add_job(post_deals_to_channel, "interval",
                      minutes=5, args=[app.bot])
    scheduler.start()
    print(f"✅ Scheduler started")


async def handle_text_fallback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle free text — edit target if editing, otherwise ignore"""
    if ctx.user_data.get("editing_product_id"):
        await receive_edit_target(update, ctx)


def main():
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    add_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^➕ إضافة تتبع سعر جديد$"), add_track_start)],
        states={
            WAITING_LINK: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_link)],
            WAITING_TARGET: [
                CallbackQueryHandler(target_callback, pattern="^(target_|cancel)"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_target),
            ],
        },
        fallbacks=[CallbackQueryHandler(target_callback, pattern="^cancel$")],
    )
    search_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^🔍 بحث عن منتج أمازون$"), search_start)],
        states={WAITING_SEARCH: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_search)]},
        fallbacks=[CallbackQueryHandler(lambda u, c: ConversationHandler.END, pattern="^cancel$")],
    )
    screenshot_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(screenshot_prompt, pattern="^send_screenshot$")],
        states={WAITING_SCREENSHOT: [MessageHandler(filters.PHOTO, receive_screenshot)]},
        fallbacks=[],
    )
    coupon_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^🎫 استخدام كوبون$"), coupon_start)],
        states={WAITING_COUPON: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_coupon)]},
        fallbacks=[CallbackQueryHandler(lambda u, c: ConversationHandler.END, pattern="^cancel$")],
    )
    feedback_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(feedback_prompt, pattern="^feedback$")],
        states={WAITING_FEEDBACK: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_feedback)]},
        fallbacks=[],
    )
    admin_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_callback, pattern="^admin_")],
        states={
            WAITING_ADMIN_USER: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_user_action)],
            WAITING_ADMIN_COUPON: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_coupon_action)],
        },
        fallbacks=[],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(CommandHandler("debug", debug_url))
    app.add_handler(add_conv)
    app.add_handler(search_conv)
    app.add_handler(screenshot_conv)
    app.add_handler(coupon_conv)
    app.add_handler(feedback_conv)
    app.add_handler(admin_conv)
    app.add_handler(MessageHandler(filters.Regex("^📦 منتجاتي$"), my_products))
    app.add_handler(MessageHandler(filters.Regex("^📊 إحصائياتي$"), my_stats))
    app.add_handler(MessageHandler(filters.Regex("^👤 حسابي$"), my_account))
    app.add_handler(MessageHandler(filters.Regex("^💎 الباقات$"), show_plans))
    app.add_handler(MessageHandler(filters.Regex("^🎁 شارك واربح$"), share_and_earn))
    app.add_handler(MessageHandler(filters.Regex("^🏆 المسابقة$"), competition))
    app.add_handler(MessageHandler(filters.Regex("^📢 قناة العروض$"), channel_section))
    app.add_handler(MessageHandler(filters.Regex("^❓ المساعدة$"), help_section))
    app.add_handler(CallbackQueryHandler(check_membership_callback, pattern="^check_membership$"))
    app.add_handler(CallbackQueryHandler(plans_callback, pattern="^show_plans$"))
    app.add_handler(CallbackQueryHandler(product_action_callback,
                                         pattern="^(del_|mute_|edit|track_url_|show_plans|cancel)"))
    app.add_handler(CallbackQueryHandler(admin_callback, pattern="^(approve_|reject_)"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_fallback))
    print("🚀 Bot started!")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()

import asyncio
import re
import os
from telegram import (Update, InlineKeyboardButton, InlineKeyboardMarkup)
from telegram.ext import (Application, CommandHandler, MessageHandler,
                           CallbackQueryHandler, ContextTypes, filters,
                           ConversationHandler)
from telegram.constants import ParseMode
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from datetime import datetime, timedelta

from config import (BOT_TOKEN, ADMIN_ID, FREE_LIMIT, INSTAPAY_LINK,
                    CHECK_INTERVAL_MINUTES, CHANNEL_ID, CHANNEL_LINK)
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
WAITING_BROADCAST = 10


def fp(price: float) -> str:
    return f"{price:,.0f} جنيه"


# ── Main Menu ─────────────────────────────────────────────────────────────────
def main_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ إضافة تتبع سعر جديد", callback_data="menu_add"),
         InlineKeyboardButton("📦 منتجاتي", callback_data="menu_products")],
        [InlineKeyboardButton("🔍 بحث عن منتج أمازون", callback_data="menu_search"),
         InlineKeyboardButton("📊 إحصائياتي", callback_data="menu_stats")],
        [InlineKeyboardButton("👤 حسابي", callback_data="menu_account"),
         InlineKeyboardButton("💎 الباقات", callback_data="menu_plans")],
        [InlineKeyboardButton("🎁 شارك واربح", callback_data="menu_share"),
         InlineKeyboardButton("🎫 استخدام كوبون", callback_data="menu_coupon")],
        [InlineKeyboardButton("📢 قناة العروض", url=CHANNEL_LINK),
         InlineKeyboardButton("❓ المساعدة", callback_data="menu_help")],
    ])


def back_btn():
    return InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="menu_main")


def channel_btn():
    return InlineKeyboardButton("📢 عروض متتفوتش", url=CHANNEL_LINK)


# ── Channel check ─────────────────────────────────────────────────────────────
async def is_member(bot, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(CHANNEL_ID, user_id)
        return member.status not in ["left", "kicked", "banned"]
    except Exception as e:
        print(f"is_member error: {e}")
        return True


async def require_membership(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    user_id = update.effective_user.id
    if user_id == ADMIN_ID:
        return True
    if not await is_member(ctx.bot, user_id):
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📢 اشترك في القناة", url=CHANNEL_LINK)],
            [InlineKeyboardButton("✅ اشتركت، تحقق", callback_data="check_membership")],
        ])
        await update.effective_message.reply_text(
            "⚠️ <b>لازم تشترك في قناتنا الأول!</b>\n\n"
            "📢 قناة عروض متتفوتش — أحسن عروض أمازون مصر يومياً",
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
            ref = int(ctx.args[0])
            if ref != user.id:
                referred_by = ref
        except:
            pass
    await upsert_user(user.id, user.username or "", user.full_name or "", referred_by)

    if not await is_member(ctx.bot, user.id) and user.id != ADMIN_ID:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📢 اشترك في القناة", url=CHANNEL_LINK)],
            [InlineKeyboardButton("✅ اشتركت، تحقق", callback_data="check_membership")],
        ])
        await update.effective_message.reply_text(
            f"👋 أهلاً <b>{user.first_name}</b>!\n\n"
            "⚠️ لازم تشترك في قناتنا الأول عشان تستخدم البوت:",
            parse_mode=ParseMode.HTML, reply_markup=keyboard
        )
        return

    await show_main_menu(update, ctx)


async def show_main_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE, edit=False):
    user = update.effective_user
    count = await get_user_product_count(user.id)
    limit = await get_user_limit(user.id)
    prem = await is_premium(user.id)
    plan = "👑 مدفوعة ♾️" if prem else f"🆓 مجانية ({count}/{limit})"
    text = (
        f"👋 أهلاً <b>{user.first_name}</b>!\n\n"
        f"🤖 بوت تتبع أسعار أمازون مصر 📉\n\n"
        f"ابعتلي أي رابط منتج وأنا هراقب سعره!\n\n"
        f"📋 خطتك: <b>{plan}</b>"
    )
    if edit:
        try:
            await update.effective_message.edit_text(
                text, parse_mode=ParseMode.HTML, reply_markup=main_menu_keyboard()
            )
            return
        except:
            pass
    await update.effective_message.reply_text(
        text, parse_mode=ParseMode.HTML, reply_markup=main_menu_keyboard()
    )


async def check_membership_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if await is_member(ctx.bot, update.effective_user.id):
        await show_main_menu(update, ctx, edit=True)
    else:
        await query.answer("❌ لسه مشتركتش!", show_alert=True)


# ── Menu Router ───────────────────────────────────────────────────────────────
async def menu_router(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "menu_main":
        await show_main_menu(update, ctx, edit=True)
    elif data == "menu_add":
        await add_track_start(update, ctx)
    elif data == "menu_products":
        await my_products(update, ctx)
    elif data == "menu_search":
        await search_start(update, ctx)
    elif data == "menu_stats":
        await my_stats(update, ctx)
    elif data == "menu_account":
        await my_account(update, ctx)
    elif data == "menu_plans":
        await show_plans(update, ctx)
    elif data == "menu_share":
        await share_and_earn(update, ctx)
    elif data == "menu_coupon":
        await coupon_start(update, ctx)
    elif data == "menu_help":
        await help_section(update, ctx)


# ── ADD PRODUCT ───────────────────────────────────────────────────────────────
async def add_track_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await require_membership(update, ctx):
        return ConversationHandler.END
    user_id = update.effective_user.id
    count = await get_user_product_count(user_id)
    limit = await get_user_limit(user_id)
    if count >= limit:
        prem = await is_premium(user_id)
        text = (f"⚠️ وصلت للحد الأقصى ({limit} منتجات)\n\nترقّى للخطة المدفوعة! 👑"
                if not prem else "⚠️ وصلت للحد الأقصى!")
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("💎 الباقات", callback_data="menu_plans")],
            [back_btn()],
        ])
        await update.effective_message.reply_text(text, reply_markup=keyboard)
        return ConversationHandler.END

    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data="cancel_conv")]])
    await update.effective_message.reply_text(
        "🔗 ابعتلي رابط المنتج من أمازون\n\n✅ بيقبل روابط عادية وروابط amzn.eu/amzn.to",
        reply_markup=keyboard
    )
    return WAITING_LINK


async def receive_link(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    url = update.effective_message.text.strip()
    if not any(d in url for d in ["amazon", "amzn", "a.co", "link.amazon"]):
        await update.effective_message.reply_text("❌ مش رابط أمازون، جرب تاني.")
        return WAITING_LINK

    msg = await update.effective_message.reply_text("⏳ بقرأ المنتج، استنى...")
    product = await scrape_amazon_product(url)
    if not product:
        await msg.edit_text(
            "❌ مقدرتش أقرأ المنتج.\n\nجرب رابط من amazon.eg مباشرة",
            reply_markup=InlineKeyboardMarkup([[back_btn()]])
        )
        return ConversationHandler.END

    ctx.user_data["pending_product"] = product
    await msg.delete()
    text = (
        f"✅ <b>{product['title']}</b>\n\n"
        f"💰 السعر الحالي: <b>{fp(product['price'])}</b>\n\n"
        f"اختار طريقة التنبيه:"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("💰 سعر مستهدف", callback_data="target_price"),
         InlineKeyboardButton("📊 نسبة خصم", callback_data="target_percent")],
        [InlineKeyboardButton("📉 أي انخفاض", callback_data="target_any")],
        [InlineKeyboardButton("❌ إلغاء", callback_data="cancel_conv")],
    ])
    try:
        if product["image_url"]:
            await update.effective_message.reply_photo(
                photo=product["image_url"], caption=text,
                parse_mode=ParseMode.HTML, reply_markup=keyboard)
        else:
            await update.effective_message.reply_text(
                text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
    except:
        await update.effective_message.reply_text(
            text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
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
        await query.message.reply_text("📊 اكتب نسبة الخصم (مثلاً: 10):")
        return WAITING_TARGET
    elif data == "cancel_conv":
        ctx.user_data.clear()
        await show_main_menu(update, ctx, edit=True)
        return ConversationHandler.END


async def receive_target(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    mode = ctx.user_data.get("target_mode")
    try:
        value = float(re.sub(r"[^\d.]", "", update.effective_message.text.strip()))
    except:
        await update.effective_message.reply_text("❌ رقم غلط، حاول تاني:")
        return WAITING_TARGET
    if mode == "price":
        await save_product(update, ctx, value, None)
    else:
        await save_product(update, ctx, None, value)
    return ConversationHandler.END


async def save_product(update, ctx, target_price=None, target_percent=None):
    product = ctx.user_data.get("pending_product")
    user_id = update.effective_user.id
    await add_product(user_id=user_id, asin=product["asin"], title=product["title"],
                      url=product["url"], affiliate_url=product["affiliate_url"],
                      image_url=product["image_url"], price=product["price"],
                      target_price=target_price, target_percent=target_percent)
    if target_price:
        t = f"💰 السعر المستهدف: {fp(target_price)}"
    elif target_percent:
        t = f"📊 خصم مستهدف: {target_percent:.0f}%"
    else:
        t = "📉 أي انخفاض"
    await update.effective_message.reply_text(
        f"✅ <b>تم إضافة المنتج!</b>\n\n🛍 {product['title'][:80]}\n💰 {fp(product['price'])}\n{t}",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[back_btn()]])
    )
    ctx.user_data.clear()


# ── MY PRODUCTS ───────────────────────────────────────────────────────────────
async def my_products(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await require_membership(update, ctx):
        return
    user_id = update.effective_user.id
    products = await get_user_products(user_id)
    limit = await get_user_limit(user_id)
    prem = await is_premium(user_id)
    limit_text = "♾️" if prem else f"{len(products)}/{limit}"

    if not products:
        await update.effective_message.reply_text(
            "📦 مفيش منتجات متابَعة.\n\nاضغط ➕ لإضافة منتج",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ إضافة منتج", callback_data="menu_add")],
                [back_btn()],
            ])
        )
        return

    # Show all products as buttons in one message
    buttons = []
    for p in products:
        mute_icon = "🔕 " if p["is_muted"] else ""
        label = f"{mute_icon}{p['title'][:35]}... — {fp(p['current_price'])}"
        buttons.append([InlineKeyboardButton(label, callback_data=f"product_{p['id']}")])
    buttons.append([back_btn()])

    await update.effective_message.reply_text(
        f"📦 <b>منتجاتك ({limit_text})</b>\n\nاختار منتج:",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(buttons)
    )


async def show_product_detail(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    pid = int(query.data.split("_")[1])
    user_id = update.effective_user.id
    p = await get_product(pid)
    if not p or p["user_id"] != user_id:
        await query.answer("❌ مش لاقي المنتج", show_alert=True)
        return

    mute_icon = "🔕 مكتوم" if p["is_muted"] else "🔔 نشط"
    target = (f"💰 {fp(p['target_price'])}" if p["target_price"]
              else f"📊 {p['target_percent']:.0f}% خصم" if p["target_percent"]
              else "📉 أي انخفاض")

    text = (
        f"🛍 <b>{p['title']}</b>\n\n"
        f"💰 السعر الحالي: <b>{fp(p['current_price'])}</b>\n"
        f"🎯 الهدف: {target}\n"
        f"📡 الحالة: {mute_icon}"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ تعديل الهدف", callback_data=f"edit_{pid}"),
         InlineKeyboardButton("🔕 كتم" if not p["is_muted"] else "🔔 تفعيل",
                              callback_data=f"mute_{pid}")],
        [InlineKeyboardButton("🔗 فتح في أمازون", url=p["affiliate_url"]),
         InlineKeyboardButton("🗑 حذف", callback_data=f"del_{pid}")],
        [InlineKeyboardButton("🔙 منتجاتي", callback_data="menu_products")],
    ])

    try:
        if p["image_url"]:
            await query.message.reply_photo(
                photo=p["image_url"], caption=text,
                parse_mode=ParseMode.HTML, reply_markup=keyboard)
        else:
            await query.message.reply_text(
                text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
    except:
        await query.message.reply_text(
            text, parse_mode=ParseMode.HTML, reply_markup=keyboard)


# ── STATS ─────────────────────────────────────────────────────────────────────
async def my_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await require_membership(update, ctx):
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
        f"📦 منتجات: {count}\n"
        f"🔕 مكتومة: {muted}\n"
        f"🔔 نشطة: {count - muted}"
    )
    await update.effective_message.reply_text(
        text, parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[back_btn()]])
    )


# ── ACCOUNT ───────────────────────────────────────────────────────────────────
async def my_account(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await require_membership(update, ctx):
        return
    user = update.effective_user
    db_user = await get_user(user.id)
    prem = await is_premium(user.id)
    limit = await get_user_limit(user.id)
    count = await get_user_product_count(user.id)
    if prem:
        plan = "👑 مدفوعة"
        if db_user and db_user["premium_expires"]:
            plan += f"\nتنتهي: {db_user['premium_expires'][:10]}"
        else:
            plan += " (دائمة)"
    else:
        plan = f"🆓 مجانية ({count}/{limit})"

    bot_info = await ctx.bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start={user.id}"
    text = (
        f"👤 <b>حسابك</b>\n\n"
        f"الاسم: {user.full_name}\n"
        f"ID: <code>{user.id}</code>\n"
        f"الخطة: {plan}\n\n"
        f"🔗 رابط الإحالة:\n<code>{ref_link}</code>"
    )
    await update.effective_message.reply_text(
        text, parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[back_btn()]])
    )


# ── PLANS ─────────────────────────────────────────────────────────────────────
async def show_plans(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await require_membership(update, ctx):
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
        f"🆓 <b>مجانية</b> — {FREE_LIMIT} منتجات\n\n"
        f"👑 <b>مدفوعة — 120 جنيه/شهر</b>\n"
        f"• منتجات غير محدودة ♾️\n"
        f"• أولوية في الفحص\n\n"
        f"ادفع عبر InstaPay ثم ابعت سكرين شوت 👇"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("💳 ادفع (InstaPay)", url=INSTAPAY_LINK)],
        [InlineKeyboardButton("📸 ابعت سكرين شوت", callback_data="send_screenshot")],
        [back_btn()],
    ])
    await update.effective_message.reply_text(
        text, parse_mode=ParseMode.HTML, reply_markup=keyboard)


# ── PAYMENT ───────────────────────────────────────────────────────────────────
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
    caption = f"💳 طلب ترقية\n👤 {user.full_name} (@{user.username})\n🆔 {user.id}"
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ 30 يوم", callback_data=f"approve_30_{user.id}"),
        InlineKeyboardButton("✅ دائم", callback_data=f"approve_0_{user.id}"),
        InlineKeyboardButton("❌ رفض", callback_data=f"reject_{user.id}"),
    ]])
    await ctx.bot.send_photo(chat_id=ADMIN_ID, photo=file_id,
                              caption=caption, reply_markup=keyboard)
    await update.effective_message.reply_text(
        "✅ تم الاستلام! هيتم التفعيل خلال دقايق 🕐",
        reply_markup=InlineKeyboardMarkup([[back_btn()]])
    )
    return ConversationHandler.END


# ── SHARE ─────────────────────────────────────────────────────────────────────
async def share_and_earn(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await require_membership(update, ctx):
        return
    user = update.effective_user
    bot_info = await ctx.bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start={user.id}"
    limit = await get_user_limit(user.id)
    count = await get_user_product_count(user.id)
    text = (
        f"🎁 <b>شارك واربح!</b>\n\n"
        f"لما حد يسجل بالرابط بتاعك، هتاخد +1 منتج لمدة شهر!\n\n"
        f"📊 منتجاتك: {count}/{limit}\n\n"
        f"🔗 رابطك:\n<code>{ref_link}</code>"
    )
    await update.effective_message.reply_text(
        text, parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[back_btn()]])
    )


# ── COUPON ────────────────────────────────────────────────────────────────────
async def coupon_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await require_membership(update, ctx):
        return ConversationHandler.END
    await update.effective_message.reply_text(
        "🎫 اكتب كود الكوبون:",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data="cancel_conv")]])
    )
    return WAITING_COUPON


async def receive_coupon(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    code = update.effective_message.text.strip()
    user_id = update.effective_user.id
    result = await use_coupon(code, user_id)
    if not result:
        await update.effective_message.reply_text(
            "❌ الكوبون غلط أو منتهي أو استخدمته قبل كده.",
            reply_markup=InlineKeyboardMarkup([[back_btn()]])
        )
    else:
        benefits = []
        if result["days"]:
            benefits.append(f"👑 {result['days']} يوم مدفوع")
        if result["extra_slots"]:
            benefits.append(f"📦 +{result['extra_slots']} منتج إضافي")
        await update.effective_message.reply_text(
            f"🎉 <b>تم تفعيل الكوبون!</b>\n\n" + "\n".join(benefits),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[back_btn()]])
        )
    return ConversationHandler.END


# ── HELP ──────────────────────────────────────────────────────────────────────
async def help_section(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (
        "❓ <b>المساعدة</b>\n\n"
        "➕ <b>إضافة تتبع سعر</b> — ابعت رابط المنتج\n"
        "📦 <b>منتجاتي</b> — شوف وعدّل المنتجات\n"
        "🔍 <b>بحث</b> — ابحث عن أي منتج\n"
        "📊 <b>إحصائياتي</b> — إحصائيات حسابك\n"
        "👤 <b>حسابي</b> — بياناتك ورابط الإحالة\n"
        "💎 <b>الباقات</b> — ترقّى للمدفوعة\n"
        "🎁 <b>شارك واربح</b> — ادعو أصحابك\n"
        "🎫 <b>كوبون</b> — استخدم كود خصم\n"
        "━━━━━━━━━━━━━━━━━━"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🐛 مشكلة أو اقتراح", callback_data="feedback")],
        [back_btn()],
    ])
    await update.effective_message.reply_text(
        text, parse_mode=ParseMode.HTML, reply_markup=keyboard)


async def feedback_prompt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await update.callback_query.message.reply_text(
        "✍️ اكتب مشكلتك أو اقتراحك وهيتم توجيهه للأدمن:",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data="cancel_conv")]])
    )
    return WAITING_FEEDBACK


async def cancel_conv(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("تم الإلغاء")
    ctx.user_data.clear()
    user = update.effective_user
    count = await get_user_product_count(user.id)
    limit = await get_user_limit(user.id)
    prem = await is_premium(user.id)
    plan = "👑 مدفوعة ♾️" if prem else f"🆓 مجانية ({count}/{limit})"
    text = (
        f"👋 أهلاً <b>{user.first_name}</b>!\n\n"
        f"🤖 بوت تتبع أسعار أمازون مصر 📉\n\n"
        f"ابعتلي أي رابط منتج وأنا هراقب سعره!\n\n"
        f"📋 خطتك: <b>{plan}</b>"
    )
    await query.message.reply_text(
        text, parse_mode=ParseMode.HTML,
        reply_markup=main_menu_keyboard()
    )
    return ConversationHandler.END
    await update.callback_query.answer()
    await update.callback_query.message.reply_text("✍️ اكتب مشكلتك أو اقتراحك:")
    return WAITING_FEEDBACK


async def receive_feedback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await ctx.bot.send_message(
        chat_id=ADMIN_ID,
        text=f"📩 من {user.full_name} (@{user.username}) [{user.id}]:\n\n{update.effective_message.text}"
    )
    await update.effective_message.reply_text(
        "✅ تم الإرسال للأدمن!",
        reply_markup=InlineKeyboardMarkup([[back_btn()]])
    )
    return ConversationHandler.END


# ── SEARCH ────────────────────────────────────────────────────────────────────
async def search_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await require_membership(update, ctx):
        return ConversationHandler.END
    await update.effective_message.reply_text(
        "🔍 اكتب اسم المنتج بالتفصيل:\n"
        "<i>مثال: آيفون 15 برو 256GB أسود</i>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data="cancel_conv")]])
    )
    return WAITING_SEARCH


async def receive_search(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query_text = update.effective_message.text.strip()
    msg = await update.effective_message.reply_text("🔍 بدور...")
    results = await search_amazon(query_text)
    if not results:
        await msg.edit_text(
            "❌ مش لاقي نتائج، جرب كلمة تانية.",
            reply_markup=InlineKeyboardMarkup([[back_btn()]])
        )
        return ConversationHandler.END

    await msg.delete()
    await update.effective_message.reply_text(f"✅ لقيت {len(results)} نتيجة:")
    for r in results:
        rating_text = f"⭐ {r['rating']}\n" if r.get("rating") else ""
        text = (
            f"🛍 <b>{r['title'][:100]}</b>\n\n"
            f"{rating_text}💰 <b>{fp(r['price'])}</b>"
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ تتبع السعر", callback_data=f"track_url_{r['asin']}"),
             InlineKeyboardButton("🔗 أمازون", url=r["affiliate_url"])],
        ])
        try:
            if r.get("image_url"):
                await update.effective_message.reply_photo(
                    photo=r["image_url"], caption=text,
                    parse_mode=ParseMode.HTML, reply_markup=keyboard)
            else:
                await update.effective_message.reply_text(
                    text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
        except:
            await update.effective_message.reply_text(
                text, parse_mode=ParseMode.HTML, reply_markup=keyboard)

    await update.effective_message.reply_text(
        "━━━━━━━━━━━━━━━━━━",
        reply_markup=InlineKeyboardMarkup([[back_btn()]])
    )
    return ConversationHandler.END


# ── PRODUCT ACTIONS ───────────────────────────────────────────────────────────
async def product_action_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = update.effective_user.id

    if data.startswith("del_"):
        pid = int(data.split("_")[1])
        if await delete_product(pid, user_id):
            await query.message.edit_text("🗑 تم الحذف.")
            await asyncio.sleep(1)
            await my_products(update, ctx)
        else:
            await query.answer("❌ خطأ", show_alert=True)

    elif data.startswith("mute_"):
        pid = int(data.split("_")[1])
        muted = await toggle_mute(pid, user_id)
        await query.answer("🔕 تم الكتم" if muted else "🔔 تم التفعيل")
        # Refresh product detail
        await show_product_detail(update, ctx)

    elif data.startswith("edit_"):
        pid = int(data.split("_")[1])
        await query.message.reply_text(
            "✏️ اختار نوع التعديل:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💰 سعر مستهدف", callback_data=f"editprice_{pid}"),
                 InlineKeyboardButton("📊 نسبة خصم", callback_data=f"editpct_{pid}")],
                [InlineKeyboardButton("📉 أي انخفاض", callback_data=f"editany_{pid}")],
            ])
        )

    elif data.startswith("editany_"):
        pid = int(data.split("_")[1])
        await update_target(pid, user_id, None, None)
        await query.message.reply_text(
            "✅ تم — هتتنبه عند أي انخفاض",
            reply_markup=InlineKeyboardMarkup([[back_btn()]])
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

    elif data.startswith("track_url_"):
        asin = data.replace("track_url_", "")
        count = await get_user_product_count(user_id)
        limit = await get_user_limit(user_id)
        if count >= limit:
            await query.message.reply_text(
                "⚠️ وصلت للحد الأقصى!",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("💎 الباقات", callback_data="menu_plans")]
                ])
            )
            return
        msg = await query.message.reply_text("⏳ بجيب بيانات المنتج...")
        product = await scrape_amazon_product(f"https://www.amazon.eg/dp/{asin}")
        if not product:
            await msg.edit_text("❌ مقدرتش أقرأ المنتج.")
            return
        ctx.user_data["pending_product"] = product
        await msg.delete()
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("💰 سعر مستهدف", callback_data="target_price"),
             InlineKeyboardButton("📊 نسبة خصم", callback_data="target_percent")],
            [InlineKeyboardButton("📉 أي انخفاض", callback_data="target_any")],
        ])
        await query.message.reply_text(
            f"✅ <b>{product['title'][:80]}</b>\n💰 {fp(product['price'])}\n\nاختار طريقة التنبيه:",
            parse_mode=ParseMode.HTML, reply_markup=keyboard
        )


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
    await update.effective_message.reply_text(
        "✅ تم التحديث!",
        reply_markup=InlineKeyboardMarkup([[back_btn()]])
    )
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
        f"👥 المستخدمين: {len(users)}\n"
        f"👑 مدفوعين: {premium_count}\n"
        f"📦 منتجات: {len(products)}"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ تفعيل باقة", callback_data="admin_activate"),
         InlineKeyboardButton("❌ إلغاء باقة", callback_data="admin_revoke")],
        [InlineKeyboardButton("🎫 إنشاء كوبون", callback_data="admin_coupon")],
        [InlineKeyboardButton("📣 برودكاست", callback_data="admin_broadcast")],
        [InlineKeyboardButton("⚡ نشر عروض الآن", callback_data="admin_post_deals")],
        [InlineKeyboardButton("🔄 فحص الأسعار الآن", callback_data="admin_check_prices")],
        [InlineKeyboardButton("🧪 تجربة تنبيه سعر", callback_data="admin_test_alert")],
        [InlineKeyboardButton("👥 المستخدمين", callback_data="admin_users")],
    ])
    await update.effective_message.reply_text(
        text, parse_mode=ParseMode.HTML, reply_markup=keyboard)


async def admin_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if update.effective_user.id != ADMIN_ID:
        await query.answer("❌ مش أدمن", show_alert=True)
        return
    await query.answer()
    data = query.data

    if data.startswith("approve_"):
        parts = data.split("_")
        days = int(parts[1])
        uid = int(parts[2])
        await activate_premium(uid, days if days > 0 else None)
        try:
            await query.message.edit_caption(query.message.caption + "\n\n✅ تم التفعيل")
        except:
            pass
        await ctx.bot.send_message(
            chat_id=uid,
            text="🎉 تم تفعيل حسابك المدفوع! ♾️",
            reply_markup=InlineKeyboardMarkup([[back_btn()]])
        )

    elif data.startswith("reject_"):
        uid = int(data.split("_")[1])
        try:
            await query.message.edit_caption(query.message.caption + "\n\n❌ تم الرفض")
        except:
            pass
        await ctx.bot.send_message(chat_id=uid, text="❌ الدفع مش متأكد. تواصل معنا.")

    elif data == "admin_activate":
        ctx.user_data["admin_action"] = "activate"
        await query.message.reply_text("🆔 ابعت: ID عدد_الأيام\nمثال: 123456789 30")
        return WAITING_ADMIN_USER

    elif data == "admin_revoke":
        ctx.user_data["admin_action"] = "revoke"
        await query.message.reply_text("🆔 ابعت ID المستخدم:")
        return WAITING_ADMIN_USER

    elif data == "admin_coupon":
        await query.message.reply_text(
            "🎫 اكتب: CODE DAYS SLOTS MAX_USES\n"
            "مثال: <code>SAVE30 30 0 100</code>",
            parse_mode=ParseMode.HTML
        )
        return WAITING_ADMIN_COUPON

    elif data == "admin_broadcast":
        await query.message.reply_text(
            "📣 اكتب الرسالة اللي عايز تبعتها لكل المستخدمين:\n\n"
            "(ممكن تبعت نص أو صورة مع كابشن)"
        )
        return WAITING_BROADCAST

    elif data == "admin_post_deals":
        await query.message.reply_text("⏳ بينزل العروض...")
        await post_deals_to_channel(ctx.bot)
        await query.message.reply_text("✅ تم!")

    elif data == "admin_check_prices":
        await query.message.reply_text("⏳ بيفحص الأسعار...")
        await check_all_prices(ctx.bot)
        await query.message.reply_text("✅ تم فحص الأسعار!")

    elif data == "admin_test_alert":
        # ارفع سعر أول منتج بـ 30% عشان يبان إن في انخفاض
        products = await get_all_active_products()
        if not products:
            await query.message.reply_text("❌ مفيش منتجات متابَعة!")
            return
        p = products[0]
        fake_price = p["current_price"] * 1.3
        await update_product_price(p["id"], fake_price)
        await query.message.reply_text(
            f"✅ تم رفع سعر:\n<b>{p['title'][:50]}</b>\n\n"
            f"من {fp(p['current_price'])} → {fp(fake_price)}\n\n"
            f"دوس <b>🔄 فحص الأسعار الآن</b>",
            parse_mode=ParseMode.HTML
        )

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
    parts = update.effective_message.text.strip().split()
    try:
        uid = int(parts[0])
        if action == "activate":
            days = int(parts[1]) if len(parts) > 1 else 30
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
            if days: benefits.append(f"{days} يوم")
            if slots: benefits.append(f"+{slots} منتج")
            await update.effective_message.reply_text(
                f"✅ كوبون: <code>{code.upper()}</code>\n🎁 {', '.join(benefits)}\n👥 {max_uses} استخدام",
                parse_mode=ParseMode.HTML
            )
        else:
            await update.effective_message.reply_text("❌ الكود موجود بالفعل")
    except:
        await update.effective_message.reply_text("❌ خطأ")
    return ConversationHandler.END


# ── DEBUG ─────────────────────────────────────────────────────────────────────
async def receive_broadcast(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return ConversationHandler.END
    users = await get_all_users()
    msg = update.effective_message
    sent = 0
    failed = 0
    status = await msg.reply_text(f"📤 جاري الإرسال لـ {len(users)} مستخدم...")

    for u in users:
        try:
            if msg.photo:
                await ctx.bot.send_photo(
                    chat_id=u["user_id"], photo=msg.photo[-1].file_id,
                    caption=msg.caption or "", parse_mode=ParseMode.HTML
                )
            else:
                await ctx.bot.send_message(
                    chat_id=u["user_id"], text=msg.text, parse_mode=ParseMode.HTML
                )
            sent += 1
            await asyncio.sleep(0.05)
        except Exception:
            failed += 1

    await status.edit_text(
        f"✅ <b>تم البرودكاست!</b>\n\n"
        f"📨 وصل: {sent}\n"
        f"❌ فشل: {failed}",
        parse_mode=ParseMode.HTML
    )
    return ConversationHandler.END


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
                btn = await page.query_selector("input[type='submit'], .a-button-input")
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
            for sel in ["span.priceToPay span.a-price-whole", "span.a-price-whole"]:
                try:
                    el = await page.query_selector(sel)
                    if el:
                        prod_price = (await el.inner_text()).strip()
                        break
                except:
                    pass
            screenshot = await page.screenshot()
            await browser.close()
        await update.effective_message.reply_text(
            f"HTTP: {status}\nURL: {url_final[:80]}\nTitle: {prod_title}\nPrice: {prod_price}"
        )
        await update.effective_message.reply_photo(photo=screenshot)
    except Exception as e:
        await update.effective_message.reply_text(f"❌ Error: {e}")


# ── STARTUP ───────────────────────────────────────────────────────────────────
async def post_init(app: Application):
    await init_db()
    scheduler = AsyncIOScheduler()
    scheduler.add_job(check_all_prices, "interval",
                      minutes=CHECK_INTERVAL_MINUTES, args=[app.bot],
                      max_instances=1, coalesce=True)
    scheduler.add_job(post_deals_to_channel, "interval",
                      minutes=5, args=[app.bot],
                      next_run_time=datetime.now() + timedelta(minutes=1),
                      max_instances=1, coalesce=True, id="deals_job",
                      replace_existing=True)
    scheduler.start()
    print("✅ Scheduler started")


def main():
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    coupon_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(coupon_start, pattern="^menu_coupon$")],
        states={
            WAITING_COUPON: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_coupon),
                CallbackQueryHandler(cancel_conv, pattern="^cancel_conv$"),
            ]
        },
        fallbacks=[CallbackQueryHandler(cancel_conv, pattern="^cancel_conv$")],
        per_message=False,
    )
    feedback_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(feedback_prompt, pattern="^feedback$")],
        states={
            WAITING_FEEDBACK: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_feedback),
                CallbackQueryHandler(cancel_conv, pattern="^cancel_conv$"),
            ]
        },
        fallbacks=[CallbackQueryHandler(cancel_conv, pattern="^cancel_conv$")],
        per_message=False,
    )
    edit_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(product_action_callback, pattern="^(editprice_|editpct_)")],
        states={
            WAITING_EDIT_TARGET: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_edit_target),
                CallbackQueryHandler(cancel_conv, pattern="^cancel_conv$"),
            ]
        },
        fallbacks=[CallbackQueryHandler(cancel_conv, pattern="^cancel_conv$")],
        per_message=False,
    )
    add_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(add_track_start, pattern="^menu_add$")],
        states={
            WAITING_LINK: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_link),
                CallbackQueryHandler(cancel_conv, pattern="^cancel_conv$"),
            ],
            WAITING_TARGET: [
                CallbackQueryHandler(target_callback, pattern="^(target_)"),
                CallbackQueryHandler(cancel_conv, pattern="^cancel_conv$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_target),
            ],
        },
        fallbacks=[CallbackQueryHandler(cancel_conv, pattern="^cancel_conv$")],
        per_message=False,
    )
    search_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(search_start, pattern="^menu_search$")],
        states={
            WAITING_SEARCH: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_search),
                CallbackQueryHandler(cancel_conv, pattern="^cancel_conv$"),
            ]
        },
        fallbacks=[CallbackQueryHandler(cancel_conv, pattern="^cancel_conv$")],
        per_message=False,
    )
    screenshot_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(screenshot_prompt, pattern="^send_screenshot$")],
        states={
            WAITING_SCREENSHOT: [
                MessageHandler(filters.PHOTO, receive_screenshot),
                CallbackQueryHandler(cancel_conv, pattern="^cancel_conv$"),
            ]
        },
        fallbacks=[CallbackQueryHandler(cancel_conv, pattern="^cancel_conv$")],
        per_message=False,
    )
    admin_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_callback, pattern="^admin_")],
        states={
            WAITING_ADMIN_USER: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_user_action)],
            WAITING_ADMIN_COUPON: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_coupon_action)],
            WAITING_BROADCAST: [MessageHandler((filters.TEXT | filters.PHOTO) & ~filters.COMMAND, receive_broadcast)],
        },
        fallbacks=[CallbackQueryHandler(cancel_conv, pattern="^cancel_conv$")],
        per_message=False,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(CommandHandler("debug", debug_url))
    app.add_handler(add_conv)
    app.add_handler(search_conv)
    app.add_handler(screenshot_conv)
    app.add_handler(coupon_conv)
    app.add_handler(feedback_conv)
    app.add_handler(edit_conv)
    app.add_handler(admin_conv)
    app.add_handler(CallbackQueryHandler(check_membership_callback, pattern="^check_membership$"))
    app.add_handler(CallbackQueryHandler(menu_router, pattern="^menu_"))
    app.add_handler(CallbackQueryHandler(show_product_detail, pattern="^product_\\d+$"))
    app.add_handler(CallbackQueryHandler(product_action_callback,
                                         pattern="^(del_|mute_|edit_|track_url_|editany_)"))
    app.add_handler(CallbackQueryHandler(admin_callback, pattern="^(approve_|reject_)"))
    app.add_handler(CallbackQueryHandler(show_plans, pattern="^show_plans$"))

    print("🚀 Bot started!")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()

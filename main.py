# -*- coding: utf-8 -*-

# --- المكتبات المطلوبة ---
import os
import sqlite3
import logging
import asyncio
from decimal import Decimal, ROUND_DOWN

import ccxt.async_support as ccxt
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, KeyboardButton
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
    ConversationHandler,
)
from telegram.constants import ParseMode

# --- إعدادات البوت الأساسية ---
# هام: هذا المتغير تتم قراءته من إعدادات منصة الاستضافة (Environment Variable)
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', 'YOUR_TELEGRAM_BOT_TOKEN')

# --- إعداد مسجل الأحداث (Logger) ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- إعداد قاعدة البيانات ---
DB_FILE = "portfolio.db"

def init_database():
    """تنشئ الجداول اللازمة في قاعدة البيانات إذا لم تكن موجودة."""
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS portfolio (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                exchange TEXT NOT NULL,
                quantity REAL NOT NULL,
                avg_price REAL NOT NULL,
                UNIQUE(user_id, symbol, exchange)
            )
        ''')
        conn.commit()
        conn.close()
        logger.info(f"تم تهيئة قاعدة البيانات بنجاح في '{DB_FILE}'")
    except Exception as e:
        logger.error(f"حدث خطأ أثناء تهيئة قاعدة البيانات: {e}")

# --- حالات المحادثات ---
# لإضافة عملة
EXCHANGE, SYMBOL, QUANTITY, PRICE = range(4)
# لحذف عملة
REMOVE_ID = range(4, 5)


# --- متغير عالمي لتخزين كائنات المنصات ---
exchanges = {}

# --- دوال بدء وإيقاف تشغيل البوت ---
async def initialize_exchanges(application: Application):
    """يقوم بتهيئة الاتصال بالمنصات عند بدء تشغيل البوت."""
    global exchanges
    exchange_ids = ['binance', 'okx', 'kucoin', 'gateio', 'bybit', 'mexc']
    for ex_id in exchange_ids:
        try:
            exchange_class = getattr(ccxt, ex_id)
            exchanges[ex_id] = exchange_class({'enableRateLimit': True})
            logger.info(f"تم الاتصال بمنصة {ex_id} بنجاح.")
        except Exception as e:
            logger.error(f"فشل الاتصال بمنصة {ex_id}: {e}")

async def close_exchanges(application: Application):
    """يغلق جميع اتصالات المنصات عند إيقاف البوت."""
    for ex_id, ex_instance in exchanges.items():
        try:
            await ex_instance.close()
            logger.info(f"تم إغلاق الاتصال بمنصة {ex_id}.")
        except Exception as e:
            logger.error(f"خطأ أثناء إغلاق اتصال {ex_id}: {e}")


# --- لوحة المفاتيح الرئيسية (الأزرار التفاعلية) ---
MAIN_KEYBOARD = [
    [KeyboardButton("📊 عرض المحفظة")],
    [KeyboardButton("➕ إضافة عملة"), KeyboardButton("🗑️ حذف عملة")],
    [KeyboardButton("⚙️ الإعدادات"), KeyboardButton("❓ مساعدة")],
]
MAIN_REPLY_MARKUP = ReplyKeyboardMarkup(MAIN_KEYBOARD, resize_keyboard=True)

# --- دوال التعامل مع قاعدة البيانات ---

def db_add_or_update_coin(user_id, symbol, exchange, quantity, price):
    """تضيف عملة جديدة أو تحدث كمية وسعر عملة موجودة."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT quantity, avg_price FROM portfolio WHERE user_id = ? AND symbol = ? AND exchange = ?",
                   (user_id, symbol, exchange))
    result = cursor.fetchone()

    if result:
        old_quantity, old_avg_price = result
        total_quantity = old_quantity + quantity
        new_avg_price = ((old_quantity * old_avg_price) + (quantity * price)) / total_quantity
        cursor.execute("UPDATE portfolio SET quantity = ?, avg_price = ? WHERE user_id = ? AND symbol = ? AND exchange = ?",
                       (total_quantity, new_avg_price, user_id, symbol, exchange))
    else:
        cursor.execute("INSERT INTO portfolio (user_id, symbol, exchange, quantity, avg_price) VALUES (?, ?, ?, ?, ?)",
                       (user_id, symbol.upper(), exchange.lower(), quantity, price))
    conn.commit()
    conn.close()

def db_get_portfolio(user_id):
    """تجلب جميع العملات الموجودة في محفظة المستخدم."""
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT id, symbol, exchange, quantity, avg_price FROM portfolio WHERE user_id = ? ORDER BY symbol", (user_id,))
    portfolio = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return portfolio

def db_remove_coin(coin_id, user_id):
    """تحذف عملة من المحفظة باستخدام الـ ID الخاص بها."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM portfolio WHERE id = ? AND user_id = ?", (coin_id, user_id))
    rows_deleted = cursor.rowcount
    conn.commit()
    conn.close()
    return rows_deleted > 0

# --- الأوامر الرئيسية و معالجات الأزرار ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """يرسل رسالة ترحيبية ويعرض لوحة المفاتيح الرئيسية."""
    user = update.effective_user
    welcome_message = f"أهلاً بك يا {user.mention_html()} في بوت تتبع المحفظة!\n\nاختر أحد الخيارات من القائمة بالأسفل للبدء."
    await update.message.reply_html(welcome_message, reply_markup=MAIN_REPLY_MARKUP)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """يعرض رسالة المساعدة."""
    help_text = (
        "**❓ دليل المساعدة**\n\n"
        "
"**📊 عرض المحفظة**\n"
        "لعرض تقرير مفصل عن محفظتك، يتضمن الأسعار الحية، القيمة الإجمالية، والأرباح والخسائر العائمة لكل عملة.\n\n"
        "**➕ إضافة عملة**\n"
        "لبدء عملية إضافة عملة جديدة لمحفظتك. سيقوم البوت بسؤالك عن التفاصيل خطوة بخطوة.\n\n"
        "**🗑️ حذف عملة**\n"
        "لحذف عملية شراء من محفظتك. سيطلب منك البوت رقم ID الخاص بالعملية الذي يظهر في تقرير المحفظة.\n\n"
        "**/cancel**\n"
        "لإلغاء أي عملية جارية في أي وقت."
    )
    await update.message.reply_text(help_text, parse_mode=ParseMode.MARKDOWN, reply_markup=MAIN_REPLY_MARKUP)
    
async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """يعرض قائمة الإعدادات (ميزة مستقبلية)."""
    settings_text = (
        "**⚙️ الإعدادات**\n\n"
        "هنا ستتمكن قريباً من ضبط الإعدادات المتقدمة:\n"
        "- تفعيل/تعطيل التقارير اليومية.\n"
        "- ضبط تنبيهات الأسعار المخصصة.\n\n"
        "*هذه الميزات قيد التطوير حالياً.*"
    )
    await update.message.reply_text(settings_text, parse_mode=ParseMode.MARKDOWN, reply_markup=MAIN_REPLY_MARKUP)

# --- محادثة إضافة عملة ---

async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """يبدأ محادثة إضافة عملة جديدة."""
    reply_keyboard = [list(exchanges.keys())[i:i + 3] for i in range(0, len(exchanges.keys()), 3)]
    await update.message.reply_text(
        'حسناً، لنضف عملة جديدة.\n\n'
        '**الخطوة 1 من 4:** من أي منصة قمت بالشراء؟',
        reply_markup=ReplyKeyboardMarkup(reply_keyboard, one_time_keyboard=True, resize_keyboard=True),
        parse_mode=ParseMode.MARKDOWN
    )
    return EXCHANGE

async def received_exchange(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    exchange = update.message.text.lower()
    if exchange not in exchanges:
        await update.message.reply_text("منصة غير مدعومة. الرجاء اختيار واحدة من القائمة.")
        return EXCHANGE
    context.user_data['exchange'] = exchange
    await update.message.reply_text(
        f"تم اختيار منصة: *{exchange}*\n\n"
        "**الخطوة 2 من 4:** ما هو رمز العملة؟ (مثال: `BTC/USDT`)",
        reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN
    )
    return SYMBOL

async def received_symbol(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    symbol = update.message.text.upper()
    if '/' not in symbol:
        await update.message.reply_text("صيغة الرمز غير صحيحة. الرجاء إدخال الرمز بصيغة `COIN/PAIR` مثل `BTC/USDT`.")
        return SYMBOL
    context.user_data['symbol'] = symbol
    await update.message.reply_text(f"تم تحديد العملة: *{symbol}*\n\n**الخطوة 3 من 4:** ما هي الكمية؟", parse_mode=ParseMode.MARKDOWN)
    return QUANTITY
    
async def received_quantity(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        quantity = float(update.message.text)
        if quantity <= 0: raise ValueError()
        context.user_data['quantity'] = quantity
        await update.message.reply_text(f"الكمية: *{quantity}*\n\n**الخطوة 4 من 4:** ما هو متوسط سعر الشراء؟", parse_mode=ParseMode.MARKDOWN)
        return PRICE
    except ValueError:
        await update.message.reply_text("قيمة غير صالحة. الرجاء إدخال الكمية كرقم موجب.")
        return QUANTITY

async def received_price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        price = float(update.message.text)
        if price <= 0: raise ValueError()
        
        user_id = update.effective_user.id
        user_data = context.user_data
        
        db_add_or_update_coin(user_id, user_data['symbol'], user_data['exchange'], user_data['quantity'], price)
        await update.message.reply_text(
            f"✅ **تمت إضافة/تحديث {user_data['symbol']} بنجاح!**",
            reply_markup=MAIN_REPLY_MARKUP, parse_mode=ParseMode.MARKDOWN
        )
        user_data.clear()
        return ConversationHandler.END
    except ValueError:
        await update.message.reply_text("قيمة غير صالحة. الرجاء إدخال السعر كرقم موجب.")
        return PRICE

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """يلغي المحادثة الحالية ويعود للقائمة الرئيسية."""
    context.user_data.clear()
    await update.message.reply_text("تم إلغاء العملية.", reply_markup=MAIN_REPLY_MARKUP)
    return ConversationHandler.END

# --- محادثة حذف عملة ---
async def remove_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """يبدأ محادثة حذف عملة."""
    await update.message.reply_text(
        "لحذف عملية من محفظتك، يرجى إرسال رقم الـ ID الخاص بها.\n"
        "يمكنك العثور على الـ ID بجانب كل عملة عند عرض محفظتك.",
        reply_markup=ReplyKeyboardRemove()
    )
    return REMOVE_ID

async def received_remove_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """يستقبل الـ ID ويقوم بالحذف."""
    user_id = update.effective_user.id
    try:
        coin_id_to_remove = int(update.message.text)
        if db_remove_coin(coin_id_to_remove, user_id):
            await update.message.reply_text(f"✅ تم حذف العملية رقم `{coin_id_to_remove}` بنجاح.", reply_markup=MAIN_REPLY_MARKUP)
        else:
            await update.message.reply_text(f"لم يتم العثور على عملية بالرقم `{coin_id_to_remove}` في محفظتك.", reply_markup=MAIN_REPLY_MARKUP)
        return ConversationHandler.END
    except (ValueError):
        await update.message.reply_text("إدخال غير صالح. الرجاء إرسال رقم فقط.", reply_markup=MAIN_REPLY_MARKUP)
        return ConversationHandler.END

# --- وظائف جلب الأسعار وعرض المحفظة ---

async def fetch_price(exchange_id, symbol):
    """
    تجلب السعر الحالي لعملة معينة مع معالجة صيغ الرموز المختلفة.
    """
    exchange = exchanges.get(exchange_id)
    if not exchange:
        return None, "Exchange not initialized"
    try:
        # [تم الإصلاح هنا] معالجة صيغة الرموز لمنصات OKX و Bybit وغيرها
        if exchange.id in ['bybit', 'okx', 'kucoin', 'mexc']:
            ticker_symbol = symbol.replace('/', '-')
        else:
            ticker_symbol = symbol
            
        ticker = await exchange.fetch_ticker(ticker_symbol)
        return ticker.get('last'), None
    except ccxt.BadSymbol:
        logger.warning(f"رمز غير صحيح {ticker_symbol} على منصة {exchange_id}")
        return None, "رمز غير صحيح"
    except Exception as e:
        logger.warning(f"فشل جلب سعر {symbol} من {exchange_id}: {e}")
        return None, "خطأ في الاتصال"

async def portfolio_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """يعرض المحفظة الكاملة مع حسابات الأرباح والخسائر."""
    user_id = update.effective_user.id
    portfolio = db_get_portfolio(user_id)

    if not portfolio:
        await update.message.reply_text("محفظتك فارغة حالياً. اضغط '➕ إضافة عملة' للبدء.", reply_markup=MAIN_REPLY_MARKUP)
        return

    msg = await update.message.reply_text("⏳ جارٍ جلب الأسعار الحية وتحديث المحفظة...")

    tasks = [fetch_price(item['exchange'], item['symbol']) for item in portfolio]
    results = await asyncio.gather(*tasks)

    total_portfolio_value = Decimal('0.0')
    total_investment_cost = Decimal('0.0')
    
    report_lines = ["**📊 تقرير محفظتك الحالي**\n", "---"]
    
    for i, item in enumerate(portfolio):
        current_price, error_msg = results[i]
        
        quantity = Decimal(str(item['quantity']))
        avg_price = Decimal(str(item['avg_price']))
        
        investment_cost = quantity * avg_price
        total_investment_cost += investment_cost
        
        line = (
            f"🆔 `{item['id']}` | **{item['symbol']}** | `{item['exchange'].capitalize()}`\n"
            f" الكمية: `{quantity}`\n"
            f" متوسط الشراء: `${avg_price:,.4f}`"
        )
        
        if current_price:
            current_price_dec = Decimal(str(current_price))
            current_value = quantity * current_price_dec
            total_portfolio_value += current_value
            pnl = current_value - investment_cost
            pnl_percent = (pnl / investment_cost * 100) if investment_cost > 0 else 0
            pnl_icon = "📈" if pnl >= 0 else "📉"
            
            line += (
                f"\n السعر الحالي: `${current_price_dec:,.4f}`\n"
                f" القيمة الحالية: `${current_value:,.2f}`\n"
                f" {pnl_icon} الربح/الخسارة: `${pnl:+.2f} ({pnl_percent:+.2f}%)`"
            )
        else:
            line += f"\n السعر الحالي: `غير متاح ({error_msg})`"
            total_portfolio_value += investment_cost

        report_lines.append(line)
        report_lines.append("---")

    total_pnl = total_portfolio_value - total_investment_cost
    total_pnl_percent = (total_pnl / total_investment_cost * 100) if total_investment_cost > 0 else 0
    total_pnl_icon = "🟢" if total_pnl >= 0 else "🔴"
    
    summary = (
        f"** ملخص المحفظة **\n"
        f" إجمالي القيمة الحالية: `${total_portfolio_value:,.2f}`\n"
        f" {total_pnl_icon} إجمالي الربح/الخسارة: `${total_pnl:+.2f} ({total_pnl_percent:+.2f}%)`"
    )
    report_lines.append(summary)

    await msg.edit_text("\n".join(report_lines), parse_mode=ParseMode.MARKDOWN)


def main() -> None:
    """الدالة الرئيسية لتشغيل البوت."""
    if TELEGRAM_BOT_TOKEN == 'YOUR_TELEGRAM_BOT_TOKEN':
        logger.critical("FATAL ERROR: لم يتم تعيين توكن التليجرام.")
        return

    init_database()

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(initialize_exchanges).post_shutdown(close_exchanges).build()

    # --- محادثة إضافة عملة ---
    add_conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^➕ إضافة عملة$"), add_start)],
        states={
            EXCHANGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_exchange)],
            SYMBOL: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_symbol)],
            QUANTITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_quantity)],
            PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_price)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    # --- محادثة حذف عملة ---
    remove_conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^🗑️ حذف عملة$"), remove_start)],
        states={
            REMOVE_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_remove_id)]
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(add_conv_handler)
    application.add_handler(remove_conv_handler)
    
    # --- معالجات الأزرار الرئيسية ---
    application.add_handler(MessageHandler(filters.Regex("^📊 عرض المحفظة$"), portfolio_command))
    application.add_handler(MessageHandler(filters.Regex("^⚙️ الإعدادات$"), settings_command))
    application.add_handler(MessageHandler(filters.Regex("^❓ مساعدة$"), help_command))

    logger.info("... البوت قيد التشغيل ...")
    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()


# -*- coding: utf-8 -*-

# --- المكتبات المطلوبة ---
import os
import sqlite3
import logging
import asyncio
import psycopg2 # [تم التطوير هنا] مكتبة PostgreSQL
from decimal import Decimal, getcontext
from datetime import time
from zoneinfo import ZoneInfo

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

# ضبط دقة الأرقام العشرية
getcontext().prec = 18

# --- إعدادات البوت الأساسية ---
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', 'YOUR_TELEGRAM_BOT_TOKEN')
# [تم التطوير هنا] قراءة رابط قاعدة البيانات من متغيرات البيئة
DATABASE_URL = os.getenv('DATABASE_URL')

# --- إعداد مسجل الأحداث (Logger) ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- إعداد قاعدة البيانات (PostgreSQL) ---
def get_db_connection():
    """ينشئ اتصالاً بقاعدة بيانات PostgreSQL."""
    try:
        conn = psycopg2.connect(DATABASE_URL)
        return conn
    except Exception as e:
        logger.error(f"فشل الاتصال بقاعدة البيانات: {e}")
        return None

def init_database():
    """تنشئ الجداول اللازمة في قاعدة البيانات إذا لم تكن موجودة."""
    conn = get_db_connection()
    if not conn: return
    
    try:
        with conn.cursor() as cur:
            cur.execute('''
                CREATE TABLE IF NOT EXISTS portfolio (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    symbol TEXT NOT NULL,
                    exchange TEXT NOT NULL,
                    quantity TEXT NOT NULL,
                    avg_price TEXT NOT NULL,
                    UNIQUE(user_id, symbol, exchange)
                )
            ''')
        conn.commit()
        logger.info("تم تهيئة جداول قاعدة البيانات بنجاح.")
    except Exception as e:
        logger.error(f"حدث خطأ أثناء تهيئة الجداول: {e}")
    finally:
        if conn: conn.close()

# --- حالات المحادثات ---
EXCHANGE, SYMBOL, QUANTITY, PRICE = range(4)
REMOVE_ID = range(4, 5)

# --- متغير عالمي لتخزين كائنات المنصات ---
exchanges = {}

# --- دوال بدء وإيقاف تشغيل البوت ---
async def post_init(application: Application):
    """المهام التي يتم تشغيلها بعد بدء البوت مباشرة."""
    global exchanges
    exchange_ids = ['binance', 'okx', 'kucoin', 'gateio', 'bybit', 'mexc']
    for ex_id in exchange_ids:
        try:
            exchange_class = getattr(ccxt, ex_id)
            exchanges[ex_id] = exchange_class({'enableRateLimit': True})
            logger.info(f"تم الاتصال بمنصة {ex_id} بنجاح.")
        except Exception as e:
            logger.error(f"فشل الاتصال بمنصة {ex_id}: {e}")
            
    # [تم التطوير هنا] جدولة التقرير اليومي
    cairo_tz = ZoneInfo("Africa/Cairo")
    # توقيت التقرير: 11:55 مساءً بتوقيت القاهرة
    report_time = time(hour=23, minute=55, tzinfo=cairo_tz) 
    application.job_queue.run_daily(send_daily_report, time=report_time)
    logger.info(f"تم جدولة التقرير اليومي في تمام الساعة {report_time.strftime('%H:%M')} بتوقيت القاهرة")


async def post_shutdown(application: Application):
    """يغلق جميع اتصالات المنصات عند إيقاف البوت."""
    for ex_id, ex_instance in exchanges.items():
        try:
            await ex_instance.close()
            logger.info(f"تم إغلاق الاتصال بمنصة {ex_id}.")
        except Exception as e:
            logger.error(f"خطأ أثناء إغلاق اتصال {ex_id}: {e}")

# --- لوحة المفاتيح الرئيسية ---
MAIN_KEYBOARD = [
    [KeyboardButton("📊 عرض المحفظة")],
    [KeyboardButton("➕ إضافة عملة"), KeyboardButton("🗑️ حذف عملة")],
    [KeyboardButton("⚙️ الإعدادات"), KeyboardButton("❓ مساعدة")],
]
MAIN_REPLY_MARKUP = ReplyKeyboardMarkup(MAIN_KEYBOARD, resize_keyboard=True)

# --- دوال مساعدة ---
def format_price(price_decimal):
    if price_decimal == 0: return "$0.00"
    if price_decimal < Decimal('0.01'): return f"${price_decimal:,.8f}".rstrip('0').rstrip('.')
    return f"${price_decimal:,.4f}"
    
def format_quantity(quantity_decimal):
    return f"{quantity_decimal.normalize()}"

# --- دوال التعامل مع قاعدة البيانات (PostgreSQL) ---
def db_add_or_update_coin(user_id, symbol, exchange, quantity, price):
    conn = get_db_connection()
    if not conn: return
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT quantity, avg_price FROM portfolio WHERE user_id = %s AND symbol = %s AND exchange = %s",
                        (user_id, symbol, exchange))
            result = cur.fetchone()

            quantity_dec = Decimal(str(quantity))
            price_dec = Decimal(str(price))

            if result:
                old_quantity = Decimal(result[0])
                old_avg_price = Decimal(result[1])
                total_quantity = old_quantity + quantity_dec
                new_avg_price = ((old_quantity * old_avg_price) + (quantity_dec * price_dec)) / total_quantity
                cur.execute("UPDATE portfolio SET quantity = %s, avg_price = %s WHERE user_id = %s AND symbol = %s AND exchange = %s",
                            (str(total_quantity), str(new_avg_price), user_id, symbol, exchange))
            else:
                cur.execute("INSERT INTO portfolio (user_id, symbol, exchange, quantity, avg_price) VALUES (%s, %s, %s, %s, %s)",
                            (user_id, symbol.upper(), exchange.lower(), str(quantity_dec), str(price_dec)))
        conn.commit()
    finally:
        conn.close()

def db_get_portfolio(user_id):
    conn = get_db_connection()
    if not conn: return []
    portfolio = []
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, symbol, exchange, quantity, avg_price FROM portfolio WHERE user_id = %s ORDER BY symbol", (user_id,))
            rows = cur.fetchall()
            # تحويل النتائج إلى قاموس
            for row in rows:
                portfolio.append({'id': row[0], 'symbol': row[1], 'exchange': row[2], 'quantity': row[3], 'avg_price': row[4]})
    finally:
        conn.close()
    return portfolio

def db_remove_coin(coin_id, user_id):
    conn = get_db_connection()
    if not conn: return False
    rows_deleted = 0
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM portfolio WHERE id = %s AND user_id = %s", (coin_id, user_id))
            rows_deleted = cur.rowcount
        conn.commit()
    finally:
        conn.close()
    return rows_deleted > 0

# --- الأوامر الرئيسية ومعالجات الأزرار ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    welcome_message = f"أهلاً بك يا {user.mention_html()} في بوت تتبع المحفظة!\n\nاختر أحد الخيارات من القائمة بالأسفل للبدء."
    await update.message.reply_html(welcome_message, reply_markup=MAIN_REPLY_MARKUP)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    help_text = "استخدم الأزرار بالأسفل لإدارة محفظتك."
    await update.message.reply_text(help_text, reply_markup=MAIN_REPLY_MARKUP)

async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings_text = "هنا ستتمكن قريباً من ضبط التقارير والتنبيهات."
    await update.message.reply_text(settings_text, reply_markup=MAIN_REPLY_MARKUP)

# --- محادثة إضافة عملة ---
async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    reply_keyboard = [list(exchanges.keys())[i:i + 3] for i in range(0, len(exchanges.keys()), 3)]
    await update.message.reply_text(
        '**الخطوة 1 من 4:** اختر منصة الشراء.',
        reply_markup=ReplyKeyboardMarkup(reply_keyboard, one_time_keyboard=True, resize_keyboard=True),
        parse_mode=ParseMode.MARKDOWN
    )
    return EXCHANGE

async def received_exchange(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['exchange'] = update.message.text.lower()
    await update.message.reply_text(
        "**الخطوة 2 من 4:** أدخل رمز العملة (مثال: `BTC`).",
        reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN
    )
    return SYMBOL

async def received_symbol(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # [تم التطوير هنا] إضافة /USDT تلقائياً
    symbol = update.message.text.upper()
    if '/' not in symbol:
        symbol = f"{symbol}/USDT"
    
    context.user_data['symbol'] = symbol
    await update.message.reply_text(f"تم تحديد: `{symbol}`\n\n**الخطوة 3 من 4:** ما هي الكمية؟", parse_mode=ParseMode.MARKDOWN)
    return QUANTITY
    
async def received_quantity(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        quantity = Decimal(update.message.text)
        if quantity <= 0: raise ValueError()
        context.user_data['quantity'] = quantity
        await update.message.reply_text(
            "**الخطوة 4 من 4:** ما هو متوسط سعر الشراء **للعملة الواحدة**؟", 
            parse_mode=ParseMode.MARKDOWN
        )
        return PRICE
    except Exception:
        await update.message.reply_text("قيمة غير صالحة. الرجاء إدخال الكمية كرقم موجب.")
        return QUANTITY

async def received_price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        price = Decimal(update.message.text)
        if price <= 0: raise ValueError()
        user_id = update.effective_user.id
        user_data = context.user_data
        db_add_or_update_coin(user_id, user_data['symbol'], user_data['exchange'], user_data['quantity'], price)
        await update.message.reply_text(f"✅ **تمت إضافة/تحديث {user_data['symbol']} بنجاح!**",
                                        reply_markup=MAIN_REPLY_MARKUP, parse_mode=ParseMode.MARKDOWN)
        user_data.clear()
        return ConversationHandler.END
    except Exception:
        await update.message.reply_text("قيمة غير صالحة. الرجاء إدخال السعر كرقم موجب.")
        return PRICE

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("تم إلغاء العملية.", reply_markup=MAIN_REPLY_MARKUP)
    return ConversationHandler.END

# --- محادثة حذف عملة ---
async def remove_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("لحذف عملية، أرسل رقم الـ ID الخاص بها.", reply_markup=ReplyKeyboardRemove())
    return REMOVE_ID

async def received_remove_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    try:
        coin_id_to_remove = int(update.message.text)
        if db_remove_coin(coin_id_to_remove, user_id):
            await update.message.reply_text(f"✅ تم حذف العملية رقم `{coin_id_to_remove}` بنجاح.", reply_markup=MAIN_REPLY_MARKUP)
        else:
            await update.message.reply_text(f"لم يتم العثور على عملية بالرقم `{coin_id_to_remove}`.", reply_markup=MAIN_REPLY_MARKUP)
    except ValueError:
        await update.message.reply_text("إدخال غير صالح. أرسل رقم فقط.", reply_markup=MAIN_REPLY_MARKUP)
    return ConversationHandler.END

# --- وظائف جلب الأسعار وعرض المحفظة ---
async def fetch_price(exchange_id, symbol):
    exchange = exchanges.get(exchange_id)
    if not exchange: return None, "المنصة غير مهيئة"
    
    symbols_to_try = [symbol, symbol.replace('/', '-'), symbol.replace('/', '')]
    for s in symbols_to_try:
        try:
            ticker = await exchange.fetch_ticker(s)
            if ticker and ticker.get('last'): return ticker['last'], None
        except Exception: continue
            
    logger.warning(f"فشل جلب سعر {symbol} من {exchange_id} بكل الصيغ.")
    return None, "رمز غير صحيح"

async def generate_portfolio_report(user_id: int) -> str:
    """
    تنشئ نص تقرير المحفظة لمستخدم معين.
    هذه الدالة مفصولة لإعادة استخدامها في التقارير اليومية.
    """
    portfolio = db_get_portfolio(user_id)
    if not portfolio:
        return "محفظتك فارغة حالياً."

    tasks = [fetch_price(item['exchange'], item['symbol']) for item in portfolio]
    results = await asyncio.gather(*tasks)

    total_portfolio_value = Decimal('0.0')
    total_investment_cost = Decimal('0.0')
    report_lines = []
    
    for i, item in enumerate(portfolio):
        quantity = Decimal(item['quantity'])
        avg_price = Decimal(item['avg_price'])
        investment_cost = quantity * avg_price
        total_investment_cost += investment_cost
        
        current_price, error_msg = results[i]
        current_value = investment_cost # القيمة الافتراضية إذا فشل جلب السعر
        if current_price:
            current_value = quantity * Decimal(str(current_price))
        total_portfolio_value += current_value

    # [تم التطوير هنا] وضع الملخص في بداية التقرير
    total_pnl = total_portfolio_value - total_investment_cost
    total_pnl_percent = (total_pnl / total_investment_cost * 100) if total_investment_cost > 0 else 0
    total_pnl_icon = "🟢" if total_pnl >= 0 else "🔴"
    
    summary = (
        f"**📊 ملخص المحفظة**\n\n"
        f"▪️ **رأس المال:** `${total_investment_cost:,.2f}`\n"
        f"▪️ **القيمة الحالية:** `${total_portfolio_value:,.2f}`\n"
        f"{total_pnl_icon} **إجمالي الربح/الخسارة:**\n"
        f"`${total_pnl:+.2f} ({total_pnl_percent:+.2f}%)`\n\n"
        f"--- **التفاصيل** ---\n"
    )
    report_lines.append(summary)

    for i, item in enumerate(portfolio):
        current_price, error_msg = results[i]
        quantity = Decimal(item['quantity'])
        avg_price = Decimal(item['avg_price'])
        investment_cost = quantity * avg_price
        
        line = (
            f"🆔 `{item['id']}` | **{item['symbol']}** | `{item['exchange'].capitalize()}`\n"
            f"الكمية: `{format_quantity(quantity)}`\n"
            f"متوسط الشراء: `{format_price(avg_price)}`"
        )
        
        if current_price:
            current_price_dec = Decimal(str(current_price))
            current_value = quantity * current_price_dec
            pnl = current_value - investment_cost
            pnl_percent = (pnl / investment_cost * 100) if investment_cost > 0 else 0
            pnl_icon = "📈" if pnl >= 0 else "📉"
            line += (
                f"\nالسعر الحالي: `{format_price(current_price_dec)}`\n"
                f"القيمة الحالية: `${current_value:,.2f}`\n"
                f"{pnl_icon} الربح/الخسارة: `${pnl:+.2f} ({pnl_percent:+.2f}%)`"
            )
        else:
            line += f"\nالسعر الحالي: `غير متاح ({error_msg})`"

        report_lines.append(line)
        report_lines.append("---")
        
    return "\n".join(report_lines)

async def portfolio_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    msg = await update.message.reply_text("⏳ جارٍ إعداد التقرير...")
    report_text = await generate_portfolio_report(user_id)
    await msg.edit_text(report_text, parse_mode=ParseMode.MARKDOWN)

# [تم التطوير هنا] دالة التقرير اليومي
async def send_daily_report(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    يرسل تقرير المحفظة اليومي لجميع المستخدمين الذين لديهم عملات.
    """
    conn = get_db_connection()
    if not conn: return
    try:
        with conn.cursor() as cur:
            # جلب جميع معرفات المستخدمين الفريدة من قاعدة البيانات
            cur.execute("SELECT DISTINCT user_id FROM portfolio")
            user_ids = [row[0] for row in cur.fetchall()]
    finally:
        conn.close()
        
    for user_id in user_ids:
        try:
            report_text = await generate_portfolio_report(user_id)
            # إضافة عنوان للتقرير اليومي
            final_report = f"**🗓️ تقريرك اليومي للمحفظة**\n\n{report_text}"
            await context.bot.send_message(chat_id=user_id, text=final_report, parse_mode=ParseMode.MARKDOWN)
            await asyncio.sleep(1) # لتجنب إغراق سيرفرات تليجرام بالطلبات
        except Exception as e:
            logger.error(f"فشل إرسال التقرير اليومي للمستخدم {user_id}: {e}")


def main() -> None:
    if not TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN == 'YOUR_TELEGRAM_BOT_TOKEN':
        logger.critical("FATAL ERROR: لم يتم تعيين توكن التليجرام.")
        return
    if not DATABASE_URL:
        logger.critical("FATAL ERROR: لم يتم تعيين رابط قاعدة البيانات (DATABASE_URL).")
        return

    init_database()
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).post_shutdown(post_shutdown).build()

    conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^➕ إضافة عملة$"), add_start)],
        states={
            EXCHANGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_exchange)],
            SYMBOL: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_symbol)],
            QUANTITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_quantity)],
            PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_price)],
        }, fallbacks=[CommandHandler("cancel", cancel)])

    remove_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^🗑️ حذف عملة$"), remove_start)],
        states={REMOVE_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_remove_id)]},
        fallbacks=[CommandHandler("cancel", cancel)])

    application.add_handler(CommandHandler("start", start))
    application.add_handler(conv_handler)
    application.add_handler(remove_handler)
    application.add_handler(MessageHandler(filters.Regex("^📊 عرض المحفظة$"), portfolio_command))
    application.add_handler(MessageHandler(filters.Regex("^⚙️ الإعدادات$"), settings_command))
    application.add_handler(MessageHandler(filters.Regex("^❓ مساعدة$"), help_command))

    logger.info("... البوت قيد التشغيل ...")
    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()



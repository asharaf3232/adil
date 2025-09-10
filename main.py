# -*- coding: utf-8 -*-

# --- المكتبات المطلوبة ---
import os
import sqlite3
import logging
import asyncio
import psycopg2
from decimal import Decimal, getcontext
from datetime import time, datetime, timedelta
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

# --- إعدادات البوت والإصدار ---
BOT_VERSION = "v3.0.0 - Professional Edition"
getcontext().prec = 28 # زيادة الدقة للعمليات الحسابية

# --- إعدادات البوت الأساسية ---
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
DATABASE_URL = os.getenv('DATABASE_URL')
# [تم التطوير هنا] جلب رقم حساب المدير لإرسال رسالة بدء التشغيل
ADMIN_CHAT_ID = os.getenv('ADMIN_CHAT_ID')

# --- إعداد مسجل الأحداث (Logger) ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# --- إعداد قاعدة البيانات (PostgreSQL) ---
def get_db_connection():
    try:
        return psycopg2.connect(DATABASE_URL)
    except Exception as e:
        logger.error(f"فشل الاتصال بقاعدة البيانات: {e}")
        return None

def init_database():
    conn = get_db_connection()
    if not conn: return
    try:
        with conn.cursor() as cur:
            # جدول المحفظة
            cur.execute('''
                CREATE TABLE IF NOT EXISTS portfolio (
                    id SERIAL PRIMARY KEY, user_id BIGINT NOT NULL, symbol TEXT NOT NULL,
                    exchange TEXT NOT NULL, quantity TEXT NOT NULL, avg_price TEXT NOT NULL,
                    UNIQUE(user_id, symbol, exchange)
                )
            ''')
            # [تم التطوير هنا] جدول إعدادات المستخدمين للتنبيهات وغيرها
            cur.execute('''
                CREATE TABLE IF NOT EXISTS user_settings (
                    user_id BIGINT PRIMARY KEY,
                    alerts_enabled BOOLEAN DEFAULT FALSE,
                    global_alert_threshold REAL DEFAULT 5.0,
                    last_portfolio_value TEXT,
                    last_check_time TIMESTAMP
                )
            ''')
        conn.commit()
        logger.info("تم تهيئة/التحقق من جداول قاعدة البيانات بنجاح.")
    finally:
        if conn: conn.close()

# --- حالات المحادثات ---
(EXCHANGE, SYMBOL, QUANTITY, PRICE, 
 SET_ALERT_THRESHOLD) = range(5)
REMOVE_ID = range(5, 6)

# --- متغير عالمي لتخزين كائنات المنصات ---
exchanges = {}

# --- دوال بدء وإيقاف تشغيل البوت ---
async def post_init(application: Application):
    global exchanges
    exchange_ids = ['binance', 'okx', 'kucoin', 'gateio', 'bybit', 'mexc']
    for ex_id in exchange_ids:
        try:
            exchange_class = getattr(ccxt, ex_id)
            exchanges[ex_id] = exchange_class({'enableRateLimit': True, 'options': {'defaultType': 'spot'}})
            logger.info(f"تم الاتصال بمنصة {ex_id} بنجاح.")
        except Exception as e:
            logger.error(f"فشل الاتصال بمنصة {ex_id}: {e}")
            
    cairo_tz = ZoneInfo("Africa/Cairo")
    report_time = time(hour=23, minute=55, tzinfo=cairo_tz) 
    
    # جدولة المهام الدورية
    application.job_queue.run_daily(send_daily_report, time=report_time, name="daily_report")
    # [تم التطوير هنا] جدولة فحص التنبيهات كل 5 دقائق
    application.job_queue.run_repeating(check_alerts, interval=timedelta(minutes=5), name="price_alerts")
    
    logger.info(f"تم جدولة المهام الدورية بنجاح.")

    # [تم التطوير هنا] إرسال رسالة بدء التشغيل للمدير
    if ADMIN_CHAT_ID:
        try:
            startup_message = f"🚀 **البوت يعمل الآن!**\n\n*الإصدار:* `{BOT_VERSION}`"
            await application.bot.send_message(chat_id=ADMIN_CHAT_ID, text=startup_message, parse_mode=ParseMode.MARKDOWN)
        except Exception as e:
            logger.error(f"فشل إرسال رسالة بدء التشغيل للمدير: {e}")


async def post_shutdown(application: Application):
    for ex_id, ex_instance in exchanges.items():
        try:
            await ex_instance.close()
            logger.info(f"تم إغلاق الاتصال بمنصة {ex_id}.")
        except: pass

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
    if price_decimal < Decimal('0.01'): return f"${price_decimal:,.10f}".rstrip('0').rstrip('.')
    return f"${price_decimal:,.4f}"
    
def format_quantity(quantity_decimal):
    return f"{quantity_decimal.normalize()}"

# --- دوال التعامل مع قاعدة البيانات (PostgreSQL) ---
# ... (جميع دوال db السابقة تعمل كما هي مع PostgreSQL) ...
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
                old_quantity = Decimal(result[0]); old_avg_price = Decimal(result[1])
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
    conn = get_db_connection();
    if not conn: return []
    portfolio = []
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, symbol, exchange, quantity, avg_price FROM portfolio WHERE user_id = %s ORDER BY symbol", (user_id,))
            rows = cur.fetchall()
            for row in rows:
                portfolio.append({'id': row[0], 'symbol': row[1], 'exchange': row[2], 'quantity': row[3], 'avg_price': row[4]})
    finally:
        conn.close()
    return portfolio

def db_remove_coin(coin_id, user_id):
    conn = get_db_connection();
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

def db_get_or_create_settings(user_id):
    conn = get_db_connection()
    if not conn: return None
    settings = None
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT alerts_enabled, global_alert_threshold, last_portfolio_value, last_check_time FROM user_settings WHERE user_id = %s", (user_id,))
            result = cur.fetchone()
            if result:
                settings = {'alerts_enabled': result[0], 'global_alert_threshold': result[1], 'last_portfolio_value': result[2], 'last_check_time': result[3]}
            else:
                cur.execute("INSERT INTO user_settings (user_id) VALUES (%s) RETURNING alerts_enabled, global_alert_threshold, last_portfolio_value, last_check_time", (user_id,))
                result = cur.fetchone()
                settings = {'alerts_enabled': result[0], 'global_alert_threshold': result[1], 'last_portfolio_value': result[2], 'last_check_time': result[3]}
                conn.commit()
    finally:
        conn.close()
    return settings

def db_update_alert_settings(user_id, alerts_enabled, threshold):
    conn = get_db_connection()
    if not conn: return
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE user_settings SET alerts_enabled = %s, global_alert_threshold = %s WHERE user_id = %s",
                        (alerts_enabled, threshold, user_id))
        conn.commit()
    finally:
        conn.close()

def db_update_last_portfolio_value(user_id, value):
    conn = get_db_connection()
    if not conn: return
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE user_settings SET last_portfolio_value = %s, last_check_time = %s WHERE user_id = %s",
                        (str(value), datetime.now(ZoneInfo("UTC")), user_id))
        conn.commit()
    finally:
        conn.close()

# --- الأوامر الرئيسية ومعالجات الأزرار ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    db_get_or_create_settings(user.id) # إنشاء سجل إعدادات للمستخدم الجديد
    welcome_message = f"أهلاً بك يا {user.mention_html()} في بوت تتبع المحفظة!\n\nاختر أحد الخيارات من القائمة بالأسفل للبدء."
    await update.message.reply_html(welcome_message, reply_markup=MAIN_REPLY_MARKUP)

# --- محادثة الإعدادات ---
async def settings_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    settings = db_get_or_create_settings(user_id)
    status = "🔔 مفعلة" if settings['alerts_enabled'] else "🔕 معطلة"
    threshold = settings['global_alert_threshold']
    
    keyboard = [
        [KeyboardButton(f"تبديل حالة التنبيهات (الحالة: {status})")],
        [KeyboardButton(f"تغيير نسبة التنبيه (الحالية: {threshold}%)")],
        [KeyboardButton("🔙 العودة للقائمة الرئيسية")]
    ]
    
    await update.message.reply_text("⚙️ **إعدادات التنبيهات**\n\nاختر الإعداد الذي تريد تعديله:",
                                    reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True),
                                    parse_mode=ParseMode.MARKDOWN)

async def toggle_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    settings = db_get_or_create_settings(user_id)
    new_status = not settings['alerts_enabled']
    db_update_alert_settings(user_id, new_status, settings['global_alert_threshold'])
    await update.message.reply_text(f"✅ تم تحديث حالة التنبيهات بنجاح.")
    await settings_start(update, context) # عرض القائمة المحدثة

async def change_threshold_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("أرسل النسبة المئوية الجديدة للتنبيه (مثال: `5` لـ 5%).", reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN)
    return SET_ALERT_THRESHOLD

async def received_threshold(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        threshold = float(update.message.text)
        if not (0 < threshold <= 100):
            raise ValueError()
        user_id = update.effective_user.id
        settings = db_get_or_create_settings(user_id)
        db_update_alert_settings(user_id, settings['alerts_enabled'], threshold)
        await update.message.reply_text(f"✅ تم تحديث نسبة التنبيه إلى *{threshold}%* بنجاح.", reply_markup=MAIN_REPLY_MARKUP, parse_mode=ParseMode.MARKDOWN)
        return ConversationHandler.END
    except ValueError:
        await update.message.reply_text("قيمة غير صالحة. الرجاء إدخال رقم بين 1 و 100.")
        return SET_ALERT_THRESHOLD

# --- وظائف جلب الأسعار وعرض المحفظة ---
async def fetch_price(exchange_id, symbol):
    exchange = exchanges.get(exchange_id)
    if not exchange: return None
    symbols_to_try = [symbol, symbol.replace('/', '-'), symbol.replace('/', '')]
    for s in symbols_to_try:
        try:
            ticker = await exchange.fetch_ticker(s)
            if ticker and ticker.get('last'): return ticker['last']
        except: continue
    logger.warning(f"فشل جلب سعر {symbol} من {exchange_id} بكل الصيغ.")
    return None

async def get_portfolio_value(user_id: int):
    """يحسب القيمة الإجمالية الحالية للمحفظة."""
    portfolio = db_get_portfolio(user_id)
    if not portfolio: return Decimal('0.0')

    tasks = [fetch_price(item['exchange'], item['symbol']) for item in portfolio]
    results = await asyncio.gather(*tasks)
    
    total_value = Decimal('0.0')
    for i, item in enumerate(portfolio):
        current_price = results[i]
        quantity = Decimal(item['quantity'])
        if current_price:
            total_value += quantity * Decimal(str(current_price))
        else: # إذا فشل جلب السعر، استخدم سعر الشراء كقيمة تقديرية
            total_value += quantity * Decimal(item['avg_price'])
    return total_value

async def generate_portfolio_report(user_id: int) -> str:
    # ... (الكود لم يتغير) ...
    portfolio = db_get_portfolio(user_id)
    if not portfolio: return "محفظتك فارغة حالياً."
    tasks = [fetch_price(item['exchange'], item['symbol']) for item in portfolio]
    results = await asyncio.gather(*tasks)
    total_portfolio_value = Decimal('0.0'); total_investment_cost = Decimal('0.0')
    report_lines = []
    for i, item in enumerate(portfolio):
        quantity = Decimal(item['quantity']); avg_price = Decimal(item['avg_price'])
        investment_cost = quantity * avg_price
        total_investment_cost += investment_cost
        current_price = results[i]
        current_value = investment_cost
        if current_price:
            current_value = quantity * Decimal(str(current_price))
        total_portfolio_value += current_value
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
        quantity = Decimal(item['quantity']); avg_price = Decimal(item['avg_price'])
        investment_cost = quantity * avg_price
        line = (f"🆔 `{item['id']}` | **{item['symbol']}** | `{item['exchange'].capitalize()}`\n"
                f"الكمية: `{format_quantity(quantity)}`\n"
                f"متوسط الشراء: `{format_price(avg_price)}`")
        current_price = results[i]
        if current_price:
            current_price_dec = Decimal(str(current_price))
            current_value = quantity * current_price_dec
            pnl = current_value - investment_cost
            pnl_percent = (pnl / investment_cost * 100) if investment_cost > 0 else 0
            pnl_icon = "📈" if pnl >= 0 else "📉"
            line += (f"\nالسعر الحالي: `{format_price(current_price_dec)}`\n"
                     f"القيمة الحالية: `${current_value:,.2f}`\n"
                     f"{pnl_icon} الربح/الخسارة: `${pnl:+.2f} ({pnl_percent:+.2f}%)`")
        else:
            line += f"\nالسعر الحالي: `غير متاح`"
        report_lines.append(line)
        report_lines.append("---")
    return "\n".join(report_lines)


async def portfolio_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    msg = await update.message.reply_text("⏳ جارٍ إعداد التقرير...")
    report_text = await generate_portfolio_report(user_id)
    await msg.edit_text(report_text, parse_mode=ParseMode.MARKDOWN, reply_markup=MAIN_REPLY_MARKUP)


# --- دوال التقارير والتنبيهات الدورية ---
async def send_daily_report(context: ContextTypes.DEFAULT_TYPE) -> None:
    conn = get_db_connection()
    if not conn: return
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT user_id FROM portfolio")
            user_ids = [row[0] for row in cur.fetchall()]
    finally: conn.close()
    for user_id in user_ids:
        try:
            report_text = await generate_portfolio_report(user_id)
            final_report = f"**🗓️ تقريرك اليومي للمحفظة**\n\n{report_text}"
            await context.bot.send_message(chat_id=user_id, text=final_report, parse_mode=ParseMode.MARKDOWN)
            await asyncio.sleep(1)
        except Exception as e:
            logger.error(f"فشل إرسال التقرير اليومي للمستخدم {user_id}: {e}")

async def check_alerts(context: ContextTypes.DEFAULT_TYPE) -> None:
    """[ميزة جديدة] تفحص محافظ المستخدمين وترسل تنبيهات عند تغير الأسعار."""
    conn = get_db_connection()
    if not conn: return
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT user_id, global_alert_threshold, last_portfolio_value, last_check_time FROM user_settings WHERE alerts_enabled = TRUE")
            users_to_check = cur.fetchall()
    finally: conn.close()

    for user_id, threshold, last_value_str, last_check_time in users_to_check:
        try:
            # تجنب إرسال تنبيهات للمحافظ الجديدة
            if last_value_str is None or last_check_time is None:
                current_value = await get_portfolio_value(user_id)
                db_update_last_portfolio_value(user_id, current_value)
                continue

            # السماح بمرور 24 ساعة على الأقل قبل إرسال تنبيه جديد
            if datetime.now(ZoneInfo("UTC")) - last_check_time < timedelta(hours=23, minutes=55):
                 continue

            last_value = Decimal(last_value_str)
            current_value = await get_portfolio_value(user_id)
            
            if last_value == 0: continue # تجنب القسمة على صفر

            percentage_change = abs((current_value - last_value) / last_value * 100)

            if percentage_change >= Decimal(threshold):
                direction_text = "ارتفاع" if current_value > last_value else "انخفاض"
                direction_icon = "📈" if current_value > last_value else "📉"
                alert_message = (
                    f"**🚨 تنبيه حركة المحفظة!** {direction_icon}\n\n"
                    f"حدث **{direction_text}** في القيمة الإجمالية لمحفظتك بنسبة **{percentage_change:.2f}%** خلال الـ 24 ساعة الماضية.\n\n"
                    f"▪️ القيمة السابقة: `${last_value:,.2f}`\n"
                    f"▪️ القيمة الحالية: `${current_value:,.2f}`"
                )
                await context.bot.send_message(chat_id=user_id, text=alert_message, parse_mode=ParseMode.MARKDOWN)
                # تحديث القيمة والوقت بعد إرسال التنبيه
                db_update_last_portfolio_value(user_id, current_value)
            
            await asyncio.sleep(1)

        except Exception as e:
            logger.error(f"فشل فحص التنبيهات للمستخدم {user_id}: {e}")

# ... بقية دوال المحادثات (add, remove) لم تتغير...
async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    reply_keyboard = [list(exchanges.keys())[i:i + 3] for i in range(0, len(exchanges.keys()), 3)]
    await update.message.reply_text('**الخطوة 1 من 4:** اختر منصة الشراء.', reply_markup=ReplyKeyboardMarkup(reply_keyboard, one_time_keyboard=True, resize_keyboard=True), parse_mode=ParseMode.MARKDOWN)
    return EXCHANGE
async def received_exchange(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['exchange'] = update.message.text.lower()
    await update.message.reply_text("**الخطوة 2 من 4:** أدخل رمز العملة (مثال: `BTC`).", reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN)
    return SYMBOL
async def received_symbol(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    symbol = update.message.text.upper()
    if '/' not in symbol: symbol = f"{symbol}/USDT"
    context.user_data['symbol'] = symbol
    await update.message.reply_text(f"تم تحديد: `{symbol}`\n\n**الخطوة 3 من 4:** ما هي الكمية؟", parse_mode=ParseMode.MARKDOWN)
    return QUANTITY
async def received_quantity(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        quantity = Decimal(update.message.text)
        if quantity <= 0: raise ValueError()
        context.user_data['quantity'] = quantity
        await update.message.reply_text("**الخطوة 4 من 4:** ما هو متوسط سعر الشراء **للعملة الواحدة**؟", parse_mode=ParseMode.MARKDOWN)
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
        # بعد الإضافة، قم بتحديث قيمة المحفظة الأولية لبدء تتبع التنبيهات
        current_value = await get_portfolio_value(user_id)
        db_update_last_portfolio_value(user_id, current_value)
        await update.message.reply_text(f"✅ **تمت إضافة/تحديث {user_data['symbol']} بنجاح!**", reply_markup=MAIN_REPLY_MARKUP, parse_mode=ParseMode.MARKDOWN)
        user_data.clear()
        return ConversationHandler.END
    except Exception:
        await update.message.reply_text("قيمة غير صالحة. الرجاء إدخال السعر كرقم موجب.")
        return PRICE
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("تم إلغاء العملية.", reply_markup=MAIN_REPLY_MARKUP)
    return ConversationHandler.END
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
async def back_to_main(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("تم العودة للقائمة الرئيسية.", reply_markup=MAIN_REPLY_MARKUP)
    return ConversationHandler.END


def main() -> None:
    if not all([TELEGRAM_BOT_TOKEN, DATABASE_URL]):
        logger.critical("FATAL ERROR: لم يتم تعيين جميع متغيرات البيئة المطلوبة (TOKEN, DATABASE_URL).")
        return

    init_database()
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).post_shutdown(post_shutdown).build()

    # --- معالجات المحادثات ---
    add_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^➕ إضافة عملة$"), add_start)],
        states={
            EXCHANGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_exchange)],
            SYMBOL: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_symbol)],
            QUANTITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_quantity)],
            PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_price)],
        }, fallbacks=[CommandHandler("cancel", cancel)])

    remove_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^🗑️ حذف عملة$"), remove_start)],
        states={REMOVE_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_remove_id)]},
        fallbacks=[CommandHandler("cancel", cancel)])
    
    settings_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^⚙️ الإعدادات$"), settings_start)],
        states={
            SET_ALERT_THRESHOLD: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_threshold)]
        },
        fallbacks=[MessageHandler(filters.Regex("^🔙 العودة للقائمة الرئيسية$"), back_to_main), CommandHandler("cancel", cancel)]
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(add_conv)
    application.add_handler(remove_conv)
    application.add_handler(settings_conv)
    application.add_handler(MessageHandler(filters.Regex("^📊 عرض المحفظة$"), portfolio_command))
    application.add_handler(MessageHandler(filters.Regex("^❓ مساعدة$"), help_command))
    application.add_handler(MessageHandler(filters.Regex("^تبديل حالة التنبيهات"), toggle_alerts))
    application.add_handler(MessageHandler(filters.Regex("^تغيير نسبة التنبيه"), change_threshold_start))

    logger.info("... البوت قيد التشغيل ...")
    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()



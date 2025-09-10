# -*- coding: utf-8 -*-

# --- المكتبات المطلوبة ---
import os
import logging
import asyncio
import psycopg2 
import sys
import uuid # [جديد] لتوليد معرفات فريدة
import time as sync_time
from decimal import Decimal, getcontext
from datetime import time, datetime, timedelta
from zoneinfo import ZoneInfo

import ccxt.async_support as ccxt
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, KeyboardButton, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
    ConversationHandler,
    CallbackQueryHandler,
)
from telegram.constants import ParseMode
from telegram.error import TelegramError

# --- إعدادات البوت والإصدار ---
BOT_VERSION = "v5.0.0 - Stable Edition"
getcontext().prec = 30

# --- إعدادات البوت الأساسية ---
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
DATABASE_URL = os.getenv('DATABASE_URL')
ADMIN_CHAT_ID = os.getenv('ADMIN_CHAT_ID')

# --- إعداد مسجل الأحداث (Logger) ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# --- آلية قفل التشغيل المصفحة ---
LOCK_ID = 1
LOCK_TIMEOUT_SECONDS = 90

def acquire_lock(instance_id):
    conn = get_db_connection()
    if not conn: return False
    try:
        with conn.cursor() as cur:
            conn.autocommit = False
            cur.execute("SELECT is_locked, locked_at, owner_id FROM bot_lock WHERE id = %s FOR UPDATE", (LOCK_ID,))
            lock = cur.fetchone()
            if lock:
                is_locked, locked_at, owner_id = lock
                is_stale = (datetime.now(ZoneInfo("UTC")) - locked_at) > timedelta(seconds=LOCK_TIMEOUT_SECONDS)
                if is_locked and not is_stale:
                    logger.warning(f"قفل نشط مملوك من {owner_id}. سيتم إيقاف هذه النسخة.")
                    conn.rollback()
                    return False
            cur.execute("UPDATE bot_lock SET is_locked = TRUE, locked_at = %s, owner_id = %s WHERE id = %s", 
                        (datetime.now(ZoneInfo("UTC")), instance_id, LOCK_ID))
            conn.commit()
            logger.info(f"تم الحصول على قفل التشغيل بواسطة النسخة: {instance_id}")
            return True
    except Exception as e:
        logger.error(f"خطأ في الحصول على القفل: {e}")
        conn.rollback()
        return False
    finally:
        if conn:
            conn.autocommit = True
            conn.close()

def release_lock(instance_id):
    conn = get_db_connection()
    if not conn: return
    try:
        with conn.cursor() as cur:
            # فقط مالك القفل يستطيع تحريره
            cur.execute("UPDATE bot_lock SET is_locked = FALSE WHERE id = %s AND owner_id = %s", (LOCK_ID, instance_id))
        conn.commit()
        logger.info(f"تم تحرير قفل التشغيل بواسطة النسخة: {instance_id}")
    finally:
        if conn: conn.close()

# --- إعداد قاعدة البيانات ---
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
            cur.execute('''
                CREATE TABLE IF NOT EXISTS portfolio (
                    id SERIAL PRIMARY KEY, user_id BIGINT NOT NULL, symbol TEXT NOT NULL,
                    exchange TEXT NOT NULL, quantity TEXT NOT NULL, avg_price TEXT NOT NULL,
                    UNIQUE(user_id, symbol, exchange)
                )
            ''')
            cur.execute('''
                CREATE TABLE IF NOT EXISTS user_settings (
                    user_id BIGINT PRIMARY KEY, alerts_enabled BOOLEAN DEFAULT FALSE,
                    global_alert_threshold REAL DEFAULT 5.0, last_portfolio_value TEXT,
                    last_check_time TIMESTAMP WITH TIME ZONE
                )
            ''')
            cur.execute('''
                CREATE TABLE IF NOT EXISTS bot_lock (
                    id INT PRIMARY KEY, is_locked BOOLEAN NOT NULL DEFAULT FALSE,
                    locked_at TIMESTAMP WITH TIME ZONE, owner_id TEXT
                )
            ''')
            try: cur.execute("ALTER TABLE portfolio ADD COLUMN alert_threshold REAL")
            except psycopg2.errors.DuplicateColumn: pass
            try: cur.execute("ALTER TABLE portfolio ADD COLUMN alert_last_price TEXT")
            except psycopg2.errors.DuplicateColumn: pass

            cur.execute("INSERT INTO bot_lock (id, is_locked) VALUES (%s, FALSE) ON CONFLICT (id) DO NOTHING", (LOCK_ID,))
        conn.commit()
        logger.info("تم تهيئة/التحقق من جداول قاعدة البيانات بنجاح.")
    finally:
        if conn: conn.close()

# --- بقية الكود (مع تحديثات طفيفة) ---
(EXCHANGE, SYMBOL, QUANTITY, PRICE, SET_GLOBAL_ALERT, 
 SELECT_COIN_ALERT, SET_COIN_ALERT) = range(7)
REMOVE_ID = range(7, 8)
exchanges = {}

async def post_init(application: Application):
    global exchanges
    exchange_ids = ['binance', 'okx', 'kucoin', 'gateio', 'bybit', 'mexc']
    for ex_id in exchange_ids:
        try:
            exchange_class = getattr(ccxt, ex_id)
            exchanges[ex_id] = exchange_class({'enableRateLimit': True, 'options': {'defaultType': 'spot'}})
            logger.info(f"تم الاتصال بمنصة {ex_id} بنجاح.")
        except: pass
            
    cairo_tz = ZoneInfo("Africa/Cairo")
    report_time = time(hour=23, minute=55, tzinfo=cairo_tz) 
    
    if application.job_queue:
        application.job_queue.run_daily(send_daily_report, time=report_time, name="daily_report")
        application.job_queue.run_repeating(check_alerts, interval=timedelta(minutes=5), name="price_alerts")
        logger.info(f"تم جدولة المهام الدورية بنجاح.")

    if ADMIN_CHAT_ID:
        try:
            startup_message = f"🚀 **البوت يعمل الآن!**\n\n*الإصدار:* `{BOT_VERSION}`"
            await application.bot.send_message(chat_id=ADMIN_CHAT_ID, text=startup_message, parse_mode=ParseMode.MARKDOWN)
        except Exception as e:
            logger.error(f"فشل إرسال رسالة بدء التشغيل للمدير: {e}")

async def post_shutdown(application: Application, instance_id: str):
    release_lock(instance_id)
    for ex_id, ex_instance in exchanges.items():
        try:
            await ex_instance.close()
            logger.info(f"تم إغلاق الاتصال بمنصة {ex_id}.")
        except: pass

# ... (بقية الكود يحتوي على كل الدوال السابقة بالإضافة إلى الميزات الجديدة) ...
MAIN_KEYBOARD = [[KeyboardButton("📊 عرض المحفظة")],[KeyboardButton("➕ إضافة عملة"), KeyboardButton("🗑️ حذف عملة")],[KeyboardButton("⚙️ الإعدادات"), KeyboardButton("❓ مساعدة")]]
MAIN_REPLY_MARKUP = ReplyKeyboardMarkup(MAIN_KEYBOARD, resize_keyboard=True)
def format_price(price_decimal):
    if price_decimal is None: return "N/A"
    price_decimal = Decimal(price_decimal)
    if price_decimal == 0: return "$0.00"
    if price_decimal < Decimal('0.0001'): return f"${price_decimal:,.10f}".rstrip('0').rstrip('.')
    if price_decimal < Decimal('0.01'): return f"${price_decimal:,.8f}".rstrip('0').rstrip('.')
    return f"${price_decimal:,.4f}"
def format_quantity(quantity_decimal): return f"{Decimal(quantity_decimal).normalize()}"
def db_add_or_update_coin(user_id, symbol, exchange, quantity, price):
    conn = get_db_connection()
    if not conn: return
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT quantity, avg_price FROM portfolio WHERE user_id = %s AND symbol = %s AND exchange = %s",(user_id, symbol, exchange))
            result = cur.fetchone()
            quantity_dec = Decimal(str(quantity)); price_dec = Decimal(str(price))
            if result:
                old_quantity = Decimal(result[0]); old_avg_price = Decimal(result[1])
                total_quantity = old_quantity + quantity_dec
                new_avg_price = ((old_quantity * old_avg_price) + (quantity_dec * price_dec)) / total_quantity
                cur.execute("UPDATE portfolio SET quantity = %s, avg_price = %s WHERE user_id = %s AND symbol = %s AND exchange = %s",(str(total_quantity), str(new_avg_price), user_id, symbol, exchange))
            else:
                cur.execute("INSERT INTO portfolio (user_id, symbol, exchange, quantity, avg_price) VALUES (%s, %s, %s, %s, %s)",(user_id, symbol.upper(), exchange.lower(), str(quantity_dec), str(price_dec)))
        conn.commit()
    finally: conn.close()
def db_get_portfolio(user_id):
    conn = get_db_connection();
    if not conn: return []
    portfolio = []
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, symbol, exchange, quantity, avg_price, alert_threshold FROM portfolio WHERE user_id = %s ORDER BY symbol", (user_id,))
            rows = cur.fetchall()
            for row in rows:
                portfolio.append({'id': row[0], 'symbol': row[1], 'exchange': row[2], 'quantity': row[3], 'avg_price': row[4], 'alert_threshold': row[5]})
    finally: conn.close()
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
    finally: conn.close()
    return rows_deleted > 0
def db_get_or_create_settings(user_id):
    conn = get_db_connection()
    if not conn: return None
    settings = None
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT alerts_enabled, global_alert_threshold, last_portfolio_value, last_check_time FROM user_settings WHERE user_id = %s", (user_id,))
            result = cur.fetchone()
            if result: settings = {'alerts_enabled': result[0], 'global_alert_threshold': result[1], 'last_portfolio_value': result[2], 'last_check_time': result[3]}
            else:
                cur.execute("INSERT INTO user_settings (user_id) VALUES (%s) RETURNING alerts_enabled, global_alert_threshold, last_portfolio_value, last_check_time", (user_id,))
                result = cur.fetchone()
                settings = {'alerts_enabled': result[0], 'global_alert_threshold': result[1], 'last_portfolio_value': result[2], 'last_check_time': result[3]}
                conn.commit()
    finally: conn.close()
    return settings
def db_update_alert_settings(user_id, alerts_enabled, threshold):
    conn = get_db_connection()
    if not conn: return
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE user_settings SET alerts_enabled = %s, global_alert_threshold = %s WHERE user_id = %s",(alerts_enabled, threshold, user_id))
        conn.commit()
    finally: conn.close()
def db_update_last_portfolio_value(user_id, value):
    conn = get_db_connection()
    if not conn: return
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE user_settings SET last_portfolio_value = %s, last_check_time = %s WHERE user_id = %s",(str(value), datetime.now(ZoneInfo("UTC")), user_id))
        conn.commit()
    finally: conn.close()
def db_set_coin_alert(coin_id, threshold, initial_price):
    conn = get_db_connection()
    if not conn: return
    try:
        with conn.cursor() as cur:
            threshold_val = threshold if threshold > 0 else None
            cur.execute("UPDATE portfolio SET alert_threshold = %s, alert_last_price = %s WHERE id = %s", (threshold_val, str(initial_price), coin_id))
        conn.commit()
    finally: conn.close()
def db_get_coins_for_alert_check():
    conn = get_db_connection();
    if not conn: return []
    coins = []
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT p.id, p.user_id, p.symbol, p.exchange, p.alert_threshold, p.alert_last_price FROM portfolio p JOIN user_settings s ON p.user_id = s.user_id WHERE s.alerts_enabled = TRUE AND p.alert_threshold IS NOT NULL")
            rows = cur.fetchall()
            for row in rows:
                coins.append({'id': row[0], 'user_id': row[1], 'symbol': row[2], 'exchange': row[3], 'alert_threshold': row[4], 'alert_last_price': row[5]})
    finally: conn.close()
    return coins
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    db_get_or_create_settings(user.id)
    await update.message.reply_html(f"أهلاً بك يا {user.mention_html()}!", reply_markup=MAIN_REPLY_MARKUP)
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("استخدم الأزرار بالأسفل لإدارة محفظتك.", reply_markup=MAIN_REPLY_MARKUP)
async def settings_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    settings = db_get_or_create_settings(user_id)
    status = "🔔 مفعلة" if settings['alerts_enabled'] else "🔕 معطلة"
    g_threshold = settings['global_alert_threshold']
    keyboard = [[KeyboardButton(f"تبديل حالة التنبيهات (الحالة: {status})")],[KeyboardButton(f"تنبيه المحفظة الكلي (الحالي: {g_threshold}%)")],[KeyboardButton("⚙️ تخصيص تنبيهات العملات")],[KeyboardButton("🔙 العودة للقائمة الرئيسية")]]
    await update.message.reply_text("⚙️ **الإعدادات**", reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True), parse_mode=ParseMode.MARKDOWN)
async def toggle_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    settings = db_get_or_create_settings(user_id)
    new_status = not settings['alerts_enabled']
    db_update_alert_settings(user_id, new_status, settings['global_alert_threshold'])
    await update.message.reply_text(f"✅ تم تحديث حالة التنبيهات.")
    await settings_start(update, context)
async def change_global_threshold_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("أرسل النسبة المئوية الجديدة لتنبيه **المحفظة بالكامل**.", reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN)
    return SET_GLOBAL_ALERT
async def received_global_threshold(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        threshold = float(update.message.text)
        if not (0 < threshold <= 100): raise ValueError()
        user_id = update.effective_user.id
        settings = db_get_or_create_settings(user_id)
        db_update_alert_settings(user_id, settings['alerts_enabled'], threshold)
        await update.message.reply_text(f"✅ تم تحديث نسبة تنبيه المحفظة إلى *{threshold}%*.", reply_markup=MAIN_REPLY_MARKUP, parse_mode=ParseMode.MARKDOWN)
        return ConversationHandler.END
    except ValueError:
        await update.message.reply_text("قيمة غير صالحة. الرجاء إدخال رقم بين 1 و 100.")
        return SET_GLOBAL_ALERT
async def custom_alerts_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    portfolio = db_get_portfolio(user_id)
    if not portfolio:
        await update.message.reply_text("محفظتك فارغة. أضف عملات أولاً.", reply_markup=MAIN_REPLY_MARKUP)
        return ConversationHandler.END
    
    keyboard = []
    for coin in portfolio:
        threshold_text = f"({coin['alert_threshold']}%)" if coin.get('alert_threshold') else "(غير مفعل)"
        button = InlineKeyboardButton(f"{coin['symbol']} {threshold_text}", callback_data=f"setalert_{coin['id']}")
        keyboard.append([button])
    
    await update.message.reply_text("اختر العملة التي تريد ضبط تنبيه مخصص لها:", reply_markup=InlineKeyboardMarkup(keyboard))
    return SELECT_COIN_ALERT
async def select_coin_alert_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    coin_id = int(query.data.split('_')[1])
    context.user_data['selected_coin_id'] = coin_id
    await query.message.reply_text("أرسل نسبة التنبيه الجديدة لهذه العملة (مثال: `10`).\nأرسل `0` لإلغاء التنبيه.")
    return SET_COIN_ALERT
async def received_coin_threshold(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        threshold = float(update.message.text)
        if not (0 <= threshold <= 100): raise ValueError()
        
        coin_id = context.user_data['selected_coin_id']
        conn = get_db_connection()
        symbol, exchange_id = None, None
        with conn.cursor() as cur:
            cur.execute("SELECT symbol, exchange FROM portfolio WHERE id = %s", (coin_id,))
            res = cur.fetchone()
            if res: symbol, exchange_id = res
        conn.close()

        current_price = "0"
        if symbol and exchange_id:
            price_val = await fetch_price(exchange_id, symbol)
            if price_val:
                current_price = str(price_val)

        db_set_coin_alert(coin_id, threshold, current_price)
        
        await update.message.reply_text(f"✅ تم تحديث تنبيه العملة بنجاح.", reply_markup=MAIN_REPLY_MARKUP)
        context.user_data.clear()
        return ConversationHandler.END

    except ValueError:
        await update.message.reply_text("قيمة غير صالحة. الرجاء إدخال رقم بين 0 و 100.")
        return SET_COIN_ALERT
async def fetch_price(exchange_id, symbol):
    exchange = exchanges.get(exchange_id)
    if not exchange: return None
    symbols_to_try = [symbol, symbol.replace('/', '-'), symbol.replace('/', '')]
    for s in symbols_to_try:
        try:
            ticker = await exchange.fetch_ticker(s); return ticker['last']
        except: continue
    return None
async def get_portfolio_value(user_id: int):
    portfolio = db_get_portfolio(user_id)
    if not portfolio: return Decimal('0.0')
    tasks = [fetch_price(item['exchange'], item['symbol']) for item in portfolio]
    results = await asyncio.gather(*tasks)
    total_value = Decimal('0.0')
    for i, item in enumerate(portfolio):
        current_price = results[i]; quantity = Decimal(item['quantity'])
        if current_price: total_value += quantity * Decimal(str(current_price))
        else: total_value += quantity * Decimal(item['avg_price'])
    return total_value
async def generate_portfolio_report(user_id: int) -> str:
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
        if current_price: current_value = quantity * Decimal(str(current_price))
        total_portfolio_value += current_value
    total_pnl = total_portfolio_value - total_investment_cost
    total_pnl_percent = (total_pnl / total_investment_cost * 100) if total_investment_cost > 0 else 0
    total_pnl_icon = "🟢" if total_pnl >= 0 else "🔴"
    summary = (f"**📊 ملخص المحفظة**\n\n"
               f"▪️ **رأس المال:** `{format_price(total_investment_cost)}`\n"
               f"▪️ **القيمة الحالية:** `{format_price(total_portfolio_value)}`\n"
               f"{total_pnl_icon} **إجمالي الربح/الخسارة:**\n"
               f"`{format_price(total_pnl)} ({total_pnl_percent:+.2f}%)`\n\n"
               f"--- **التفاصيل** ---\n")
    report_lines.append(summary)
    for i, item in enumerate(portfolio):
        quantity = Decimal(item['quantity']); avg_price = Decimal(item['avg_price'])
        investment_cost = quantity * avg_price
        line = (f"🆔 `{item['id']}` | **{item['symbol']}** | `{item['exchange'].capitalize()}`\n"
                f"الكمية: `{format_quantity(quantity)}`\n"
                f"متوسط الشراء: `{format_price(avg_price)}`")
        current_price = results[i]
        if current_price:
            current_price_dec = Decimal(str(current_price)); current_value = quantity * current_price_dec
            pnl = current_value - investment_cost
            pnl_percent = (pnl / investment_cost * 100) if investment_cost > 0 else 0
            pnl_icon = "📈" if pnl >= 0 else "📉"
            line += (f"\nالسعر الحالي: `{format_price(current_price_dec)}`\n"
                     f"القيمة الحالية: `{format_price(current_value)}`\n"
                     f"{pnl_icon} الربح/الخسارة: `{format_price(pnl)} ({pnl_percent:+.2f}%)`")
        else: line += f"\nالسعر الحالي: `غير متاح`"
        report_lines.append(line); report_lines.append("---")
    return "\n".join(report_lines)
async def portfolio_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    try:
        msg = await update.message.reply_text("⏳ جارٍ إعداد التقرير...")
        report_text = await generate_portfolio_report(user_id)
        await msg.edit_text(report_text, parse_mode=ParseMode.MARKDOWN)
    except TelegramError as e:
        logger.error(f"خطأ في عرض المحفظة: {e}")
        await update.message.reply_text("حدث خطأ أثناء عرض المحفظة. الرجاء المحاولة مرة أخرى.")
async def send_daily_report(context: ContextTypes.DEFAULT_TYPE) -> None:
    conn = get_db_connection();
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
    # Check global alerts
    conn = get_db_connection();
    if not conn: return
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT user_id, global_alert_threshold, last_portfolio_value, last_check_time FROM user_settings WHERE alerts_enabled = TRUE")
            users_to_check = cur.fetchall()
    finally: conn.close()
    for user_id, threshold, last_value_str, last_check_time in users_to_check:
        try:
            if last_value_str is None or last_check_time is None:
                current_value = await get_portfolio_value(user_id); db_update_last_portfolio_value(user_id, current_value); continue
            if datetime.now(ZoneInfo("UTC")) - last_check_time < timedelta(hours=23, minutes=55): continue
            last_value = Decimal(last_value_str); current_value = await get_portfolio_value(user_id)
            if last_value == 0: continue
            percentage_change = abs((current_value - last_value) / last_value * 100)
            if percentage_change >= Decimal(threshold):
                direction_text = "ارتفاع" if current_value > last_value else "انخفاض"
                direction_icon = "📈" if current_value > last_value else "📉"
                alert_message = (f"**🚨 تنبيه حركة المحفظة!** {direction_icon}\n\n"
                                 f"حدث **{direction_text}** في القيمة الإجمالية لمحفظتك بنسبة **{percentage_change:.2f}%**.\n\n"
                                 f"▪️ القيمة السابقة: `{format_price(last_value)}`\n"
                                 f"▪️ القيمة الحالية: `{format_price(current_value)}`")
                await context.bot.send_message(chat_id=user_id, text=alert_message, parse_mode=ParseMode.MARKDOWN)
                db_update_last_portfolio_value(user_id, current_value)
            await asyncio.sleep(1)
        except Exception as e:
            logger.error(f"فشل فحص تنبيه المحفظة للمستخدم {user_id}: {e}")

    # Check coin-specific alerts
    coins_to_check = db_get_coins_for_alert_check()
    for coin in coins_to_check:
        try:
            current_price_val = await fetch_price(coin['exchange'], coin['symbol'])
            if not current_price_val: continue
            
            current_price = Decimal(str(current_price_val))
            last_price = Decimal(coin['alert_last_price']) if coin['alert_last_price'] else current_price
            threshold = Decimal(coin['alert_threshold'])
            
            if last_price == 0: continue

            percentage_change = abs((current_price - last_price) / last_price * 100)
            if percentage_change >= threshold:
                direction_text = "ارتفاع" if current_price > last_price else "انخفاض"
                direction_icon = "📈" if current_price > last_price else "📉"
                alert_message = (f"**🔔 تنبيه سعر {coin['symbol']}!** {direction_icon}\n\n"
                                 f"حدث **{direction_text}** في السعر بنسبة **{percentage_change:.2f}%**.\n\n"
                                 f"▪️ السعر السابق: `{format_price(last_price)}`\n"
                                 f"▪️ السعر الحالي: `{format_price(current_price)}`")
                await context.bot.send_message(chat_id=coin['user_id'], text=alert_message, parse_mode=ParseMode.MARKDOWN)
                db_set_coin_alert(coin['id'], threshold, current_price) # Update last price after alert
            await asyncio.sleep(1)
        except Exception as e:
            logger.error(f"فشل فحص تنبيه العملة {coin['symbol']}: {e}")

async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    reply_keyboard = [list(exchanges.keys())[i:i + 3] for i in range(0, len(exchanges.keys()), 3)]
    await update.message.reply_text('**الخطوة 1 من 4:** اختر منصة الشراء.', reply_markup=ReplyKeyboardMarkup(reply_keyboard, one_time_keyboard=True, resize_keyboard=True), parse_mode=ParseMode.MARKDOWN)
    return EXCHANGE
async def received_exchange(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['exchange'] = update.message.text.lower()
    await update.message.reply_text("**الخطوة 2 من 4:** أدخل رمز العملة (مثال: `BTC`).", reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN)
    return SYMBOL
async def received_symbol(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    symbol = update.message.text.upper();
    if '/' not in symbol: symbol = f"{symbol}/USDT"
    context.user_data['symbol'] = symbol
    await update.message.reply_text(f"تم تحديد: `{symbol}`\n\n**الخطوة 3 من 4:** ما هي الكمية؟", parse_mode=ParseMode.MARKDOWN)
    return QUANTITY
async def received_quantity(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        quantity = Decimal(update.message.text);
        if quantity <= 0: raise ValueError()
        context.user_data['quantity'] = quantity
        await update.message.reply_text("**الخطوة 4 من 4:** ما هو متوسط سعر الشراء **للعملة الواحدة**؟", parse_mode=ParseMode.MARKDOWN)
        return PRICE
    except Exception: await update.message.reply_text("قيمة غير صالحة. الرجاء إدخال الكمية كرقم موجب."); return QUANTITY
async def received_price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        price = Decimal(update.message.text);
        if price <= 0: raise ValueError()
        user_id = update.effective_user.id; user_data = context.user_data
        db_add_or_update_coin(user_id, user_data['symbol'], user_data['exchange'], user_data['quantity'], price)
        current_value = await get_portfolio_value(user_id); db_update_last_portfolio_value(user_id, current_value)
        await update.message.reply_text(f"✅ **تمت إضافة/تحديث {user_data['symbol']} بنجاح!**", reply_markup=MAIN_REPLY_MARKUP, parse_mode=ParseMode.MARKDOWN)
        user_data.clear(); return ConversationHandler.END
    except Exception: await update.message.reply_text("قيمة غير صالحة. الرجاء إدخال السعر كرقم موجب."); return PRICE
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear(); await update.message.reply_text("تم إلغاء العملية.", reply_markup=MAIN_REPLY_MARKUP); return ConversationHandler.END
async def remove_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("لحذف عملية، أرسل رقم الـ ID الخاص بها.", reply_markup=ReplyKeyboardRemove()); return REMOVE_ID
async def received_remove_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    try:
        coin_id_to_remove = int(update.message.text)
        if db_remove_coin(coin_id_to_remove, user_id):
            await update.message.reply_text(f"✅ تم حذف العملية رقم `{coin_id_to_remove}` بنجاح.", reply_markup=MAIN_REPLY_MARKUP)
        else:
            await update.message.reply_text(f"لم يتم العثور على عملية بالرقم `{coin_id_to_remove}`.", reply_markup=MAIN_REPLY_MARKUP)
    except ValueError: await update.message.reply_text("إدخال غير صالح. أرسل رقم فقط.", reply_markup=MAIN_REPLY_MARKUP)
    return ConversationHandler.END
async def back_to_main(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("تم العودة للقائمة الرئيسية.", reply_markup=MAIN_REPLY_MARKUP); return ConversationHandler.END

def main() -> None:
    sync_time.sleep(random.uniform(0, 2))
    if not all([TELEGRAM_BOT_TOKEN, DATABASE_URL]):
        logger.critical("FATAL ERROR: متغيرات البيئة غير مكتملة.")
        sys.exit(1)

    instance_id = str(uuid.uuid4())
    
    init_database()
    
    if not acquire_lock(instance_id):
        logger.info("لم يتم الحصول على القفل. سيتم إغلاق هذه النسخة.")
        sys.exit(0)

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).post_shutdown(post_shutdown, {"instance_id": instance_id}).build()
    
    add_conv = ConversationHandler(entry_points=[MessageHandler(filters.Regex("^➕ إضافة عملة$"), add_start)], states={EXCHANGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_exchange)], SYMBOL: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_symbol)], QUANTITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_quantity)], PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_price)]}, fallbacks=[CommandHandler("cancel", cancel)])
    remove_conv = ConversationHandler(entry_points=[MessageHandler(filters.Regex("^🗑️ حذف عملة$"), remove_start)], states={REMOVE_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_remove_id)]}, fallbacks=[CommandHandler("cancel", cancel)])
    settings_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^⚙️ الإعدادات$"), settings_start)],
        states={
            SET_GLOBAL_ALERT: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_global_threshold)],
            SELECT_COIN_ALERT: [CallbackQueryHandler(select_coin_alert_callback)],
            SET_COIN_ALERT: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_coin_threshold)],
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
    application.add_handler(MessageHandler(filters.Regex("^تنبيه المحفظة الكلي"), change_global_threshold_start))
    application.add_handler(MessageHandler(filters.Regex("^⚙️ تخصيص تنبيهات العملات$"), custom_alerts_start))

    logger.info("... البوت قيد التشغيل ...")
    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()


# -*- coding: utf-8 -*-

# --- المكتبات المطلوبة ---
# ملاحظة: تم استبدال psycopg2 بـ asyncpg للأداء غير المتزامن
import os
import logging
import asyncio
import asyncpg
import sys
import uuid
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
BOT_VERSION = "v6.0.0 - Async DB & Refactor"
getcontext().prec = 30 # تحديد دقة الأرقام العشرية

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

# --- States & Keyboards ---
# Conversation states
(EXCHANGE, SYMBOL, QUANTITY, PRICE, SET_GLOBAL_ALERT,
 SELECT_COIN_ALERT, SET_COIN_ALERT) = range(7)
(REMOVE_ID, EDIT_ID, CHOOSE_EDIT_FIELD, GET_NEW_QUANTITY, GET_NEW_PRICE,
 CHOOSE_SETTING, BULK_IMPORT) = range(7, 14)

# Callback Data Constants (لمنع الأخطاء المطبعية)
CALLBACK_EDIT_QUANTITY = "edit_quantity"
CALLBACK_EDIT_PRICE = "edit_price"
CALLBACK_PREFIX_SET_ALERT = "setalert_"

exchanges = {}

MAIN_KEYBOARD = [
    [KeyboardButton("📊 عرض المحفظة")],
    [KeyboardButton("➕ إضافة عملة"), KeyboardButton("🗑️ حذف عملة")],
    [KeyboardButton("✏️ تعديل عملة"), KeyboardButton("📥 استيراد محفظة")],
    [KeyboardButton("⚙️ الإعدادات"), KeyboardButton("❓ مساعدة")]
]
MAIN_REPLY_MARKUP = ReplyKeyboardMarkup(MAIN_KEYBOARD, resize_keyboard=True)

# --- Database Manager Class (Refactored for asyncpg and Connection Pooling) ---
class DatabaseManager:
    def __init__(self, dsn: str):
        self.dsn = dsn
        self.pool = None

    async def connect(self):
        """إنشاء تجمع اتصالات بقاعدة البيانات."""
        try:
            self.pool = await asyncpg.create_pool(self.dsn)
            logger.info("تم إنشاء تجمع اتصالات قاعدة البيانات بنجاح.")
        except Exception as e:
            logger.critical(f"فشل الاتصال بقاعدة البيانات بشكل فادح: {e}")
            sys.exit(1)

    async def close(self):
        """إغلاق تجمع اتصالات قاعدة البيانات."""
        if self.pool:
            await self.pool.close()
            logger.info("تم إغلاق تجمع اتصالات قاعدة البيانات.")

    async def init_database(self):
        """تهيئة جداول قاعدة البيانات عند بدء التشغيل."""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute('''
                    CREATE TABLE IF NOT EXISTS portfolio (
                        id SERIAL PRIMARY KEY,
                        user_id BIGINT NOT NULL,
                        symbol TEXT NOT NULL,
                        exchange TEXT NOT NULL,
                        quantity TEXT NOT NULL,
                        avg_price TEXT NOT NULL,
                        alert_threshold REAL,
                        alert_last_price TEXT,
                        UNIQUE(user_id, symbol, exchange)
                    );
                    CREATE TABLE IF NOT EXISTS user_settings (
                        user_id BIGINT PRIMARY KEY,
                        alerts_enabled BOOLEAN DEFAULT FALSE,
                        global_alert_threshold REAL DEFAULT 5.0,
                        last_portfolio_value TEXT,
                        last_check_time TIMESTAMP WITH TIME ZONE
                    );
                    CREATE TABLE IF NOT EXISTS bot_lock (
                        id INT PRIMARY KEY,
                        is_locked BOOLEAN NOT NULL DEFAULT FALSE,
                        locked_at TIMESTAMP WITH TIME ZONE,
                        owner_id TEXT
                    );
                ''')
                # Add owner_id column if it doesn't exist (for migration)
                if not await conn.fetchrow("SELECT 1 FROM information_schema.columns WHERE table_name='bot_lock' AND column_name='owner_id'"):
                    await conn.execute("ALTER TABLE bot_lock ADD COLUMN owner_id TEXT")

                # Insert the lock row if it doesn't exist
                await conn.execute("INSERT INTO bot_lock (id, is_locked) VALUES ($1, FALSE) ON CONFLICT (id) DO NOTHING", LOCK_ID)
        logger.info("تم تهيئة/التحقق من جداول قاعدة البيانات بنجاح.")

    async def acquire_lock(self, instance_id):
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                lock_record = await conn.fetchrow("SELECT is_locked, locked_at, owner_id FROM bot_lock WHERE id = $1 FOR UPDATE", LOCK_ID)
                if lock_record:
                    is_locked, locked_at, owner_id = lock_record
                    if locked_at:
                        is_stale = (datetime.now(ZoneInfo("UTC")) - locked_at) > timedelta(seconds=LOCK_TIMEOUT_SECONDS)
                        if is_locked and not is_stale:
                            logger.warning(f"قفل نشط مملوك من {owner_id}. سيتم إيقاف هذه النسخة.")
                            return False
                await conn.execute("UPDATE bot_lock SET is_locked = TRUE, locked_at = $1, owner_id = $2 WHERE id = $3",
                                   datetime.now(ZoneInfo("UTC")), instance_id, LOCK_ID)
                logger.info(f"تم الحصول على قفل التشغيل بواسطة النسخة: {instance_id}")
                return True
        return False

    async def release_lock(self, instance_id):
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE bot_lock SET is_locked = FALSE WHERE id = $1 AND owner_id = $2", LOCK_ID, instance_id)
            logger.info(f"تم تحرير قفل التشغيل بواسطة النسخة: {instance_id}")

    async def add_or_update_coin(self, user_id, symbol, exchange, quantity, price):
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                result = await conn.fetchrow("SELECT quantity, avg_price FROM portfolio WHERE user_id = $1 AND symbol = $2 AND exchange = $3", user_id, symbol, exchange)
                quantity_dec, price_dec = Decimal(str(quantity)), Decimal(str(price))
                if result:
                    old_quantity, old_avg_price = Decimal(result['quantity']), Decimal(result['avg_price'])
                    total_quantity = old_quantity + quantity_dec
                    new_avg_price = ((old_quantity * old_avg_price) + (quantity_dec * price_dec)) / total_quantity
                    await conn.execute("UPDATE portfolio SET quantity = $1, avg_price = $2 WHERE user_id = $3 AND symbol = $4 AND exchange = $5",
                                       str(total_quantity), str(new_avg_price), user_id, symbol, exchange)
                else:
                    await conn.execute("INSERT INTO portfolio (user_id, symbol, exchange, quantity, avg_price) VALUES ($1, $2, $3, $4, $5)",
                                       user_id, symbol.upper(), exchange.lower(), str(quantity_dec), str(price_dec))

    async def get_portfolio(self, user_id):
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT id, symbol, exchange, quantity, avg_price, alert_threshold FROM portfolio WHERE user_id = $1 ORDER BY symbol", user_id)
            return [dict(row) for row in rows]

    async def get_coin_by_id(self, coin_id, user_id):
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT symbol, quantity, avg_price FROM portfolio WHERE id = $1 AND user_id = $2", coin_id, user_id)
            return dict(row) if row else None

    async def update_coin_details(self, coin_id, user_id, new_quantity=None, new_avg_price=None):
        async with self.pool.acquire() as conn:
            if new_quantity is not None:
                status = await conn.execute("UPDATE portfolio SET quantity = $1 WHERE id = $2 AND user_id = $3",
                                            str(Decimal(new_quantity)), coin_id, user_id)
            elif new_avg_price is not None:
                status = await conn.execute("UPDATE portfolio SET avg_price = $1 WHERE id = $2 AND user_id = $3",
                                            str(Decimal(new_avg_price)), coin_id, user_id)
            else:
                return False
            return int(status.split(' ')[1]) > 0

    async def remove_coin(self, coin_id, user_id):
        async with self.pool.acquire() as conn:
            status = await conn.execute("DELETE FROM portfolio WHERE id = $1 AND user_id = $2", coin_id, user_id)
            return int(status.split(' ')[1]) > 0

    async def get_or_create_settings(self, user_id):
        async with self.pool.acquire() as conn:
            settings = await conn.fetchrow("SELECT * FROM user_settings WHERE user_id = $1", user_id)
            if not settings:
                async with conn.transaction():
                    settings = await conn.fetchrow("INSERT INTO user_settings (user_id) VALUES ($1) RETURNING *", user_id)
            return dict(settings)

    async def update_alert_settings(self, user_id, alerts_enabled, threshold):
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE user_settings SET alerts_enabled = $1, global_alert_threshold = $2 WHERE user_id = $3", alerts_enabled, threshold, user_id)

    async def update_last_portfolio_value(self, user_id, value):
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE user_settings SET last_portfolio_value = $1, last_check_time = $2 WHERE user_id = $3", str(value), datetime.now(ZoneInfo("UTC")), user_id)

    async def set_coin_alert(self, coin_id, threshold, initial_price):
        async with self.pool.acquire() as conn:
            threshold_val = threshold if threshold > 0 else None
            await conn.execute("UPDATE portfolio SET alert_threshold = $1, alert_last_price = $2 WHERE id = $3", threshold_val, str(initial_price), coin_id)

    async def get_coins_for_alert_check(self):
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT p.id, p.user_id, p.symbol, p.exchange, p.alert_threshold, p.alert_last_price 
                FROM portfolio p JOIN user_settings s ON p.user_id = s.user_id 
                WHERE s.alerts_enabled = TRUE AND p.alert_threshold IS NOT NULL
            """)
            return [dict(row) for row in rows]

    async def get_users_for_daily_report(self):
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT DISTINCT user_id FROM portfolio")
            return [row['user_id'] for row in rows]

    async def get_users_for_alert_check(self):
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT user_id, global_alert_threshold, last_portfolio_value, last_check_time FROM user_settings WHERE alerts_enabled = TRUE")
            return [dict(row) for row in rows]


# --- Helper Functions ---
def format_price(price_decimal):
    if price_decimal is None: return "N/A"
    price_decimal = Decimal(price_decimal)
    if price_decimal == 0: return "$0.00"
    if price_decimal < Decimal('0.0001'): return f"${price_decimal:,.10f}".rstrip('0').rstrip('.')
    if price_decimal < Decimal('0.01'): return f"${price_decimal:,.8f}".rstrip('0').rstrip('.')
    return f"${price_decimal:,.4f}"

def format_quantity(quantity_decimal): return f"{Decimal(quantity_decimal).normalize()}"


# --- Core Bot Logic ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    db: DatabaseManager = context.bot_data['db']
    await db.get_or_create_settings(user.id)
    await update.message.reply_html(f"أهلاً بك يا {user.mention_html()}!", reply_markup=MAIN_REPLY_MARKUP)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("استخدم الأزرار بالأسفل لإدارة محفظتك.", reply_markup=MAIN_REPLY_MARKUP)

# --- Settings Conversation ---
async def settings_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    db: DatabaseManager = context.bot_data['db']
    settings = await db.get_or_create_settings(user_id)
    status = "🔔 مفعلة" if settings['alerts_enabled'] else "🔕 معطلة"
    g_threshold = settings['global_alert_threshold']
    keyboard = [[KeyboardButton(f"تبديل حالة التنبيهات (الحالة: {status})")],
                [KeyboardButton(f"تنبيه المحفظة الكلي (الحالي: {g_threshold}%)")],
                [KeyboardButton("⚙️ تخصيص تنبيهات العملات")],
                [KeyboardButton("🔙 العودة للقائمة الرئيسية")]]
    await update.message.reply_text("⚙️ **الإعدادات**", reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True), parse_mode=ParseMode.MARKDOWN)
    return CHOOSE_SETTING

async def toggle_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    db: DatabaseManager = context.bot_data['db']
    settings = await db.get_or_create_settings(user_id)
    new_status = not settings['alerts_enabled']
    await db.update_alert_settings(user_id, new_status, settings['global_alert_threshold'])
    await update.message.reply_text("✅ تم تحديث حالة التنبيهات.")
    return await settings_start(update, context)

async def change_global_threshold_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("أرسل النسبة المئوية الجديدة لتنبيه **المحفظة بالكامل**.", reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN)
    return SET_GLOBAL_ALERT

async def received_global_threshold(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        threshold = float(update.message.text)
        if not (0 < threshold <= 100): raise ValueError()
        user_id = update.effective_user.id
        db: DatabaseManager = context.bot_data['db']
        settings = await db.get_or_create_settings(user_id)
        await db.update_alert_settings(user_id, settings['alerts_enabled'], threshold)
        await update.message.reply_text(f"✅ تم تحديث نسبة تنبيه المحفظة إلى *{threshold}%*.", reply_markup=MAIN_REPLY_MARKUP, parse_mode=ParseMode.MARKDOWN)
        return ConversationHandler.END
    except ValueError:
        await update.message.reply_text("قيمة غير صالحة. الرجاء إدخال رقم بين 1 و 100.")
        return SET_GLOBAL_ALERT

async def custom_alerts_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    db: DatabaseManager = context.bot_data['db']
    portfolio = await db.get_portfolio(user_id)
    if not portfolio:
        await update.message.reply_text("محفظتك فارغة. أضف عملات أولاً.", reply_markup=MAIN_REPLY_MARKUP)
        return ConversationHandler.END
    
    keyboard = []
    for coin in portfolio:
        threshold_text = f"({coin['alert_threshold']}%)" if coin.get('alert_threshold') else "(غير مفعل)"
        callback_data = f"{CALLBACK_PREFIX_SET_ALERT}{coin['id']}"
        button = InlineKeyboardButton(f"{coin['symbol']} {threshold_text}", callback_data=callback_data)
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
        db: DatabaseManager = context.bot_data['db']
        
        coin = await db.get_coin_by_id(coin_id, update.effective_user.id)
        current_price = "0"
        if coin:
            price_val = await fetch_price(coin['exchange'], coin['symbol'])
            if price_val:
                current_price = str(price_val)

        await db.set_coin_alert(coin_id, threshold, current_price)
        
        await update.message.reply_text("✅ تم تحديث تنبيه العملة بنجاح.", reply_markup=MAIN_REPLY_MARKUP)
        context.user_data.clear()
        return ConversationHandler.END
    except ValueError:
        await update.message.reply_text("قيمة غير صالحة. الرجاء إدخال رقم بين 0 و 100.")
        return SET_COIN_ALERT

# --- Portfolio Logic ---
async def fetch_price(exchange_id, symbol):
    exchange = exchanges.get(exchange_id)
    if not exchange:
        logger.error(f"Exchange {exchange_id} not initialized.")
        return None
    
    symbols_to_try = [symbol]
    if '/' in symbol:
        symbols_to_try.append(symbol.replace('/', ''))

    for s in symbols_to_try:
        try:
            params = {'category': 'spot'} if exchange_id == 'bybit' else {}
            ticker = await exchange.fetch_ticker(s, params=params)
            if ticker and 'last' in ticker and ticker['last'] is not None:
                return ticker['last']
        except ccxt.BaseError as e:
            logger.warning(f"Could not fetch ticker for {s} on {exchange_id}: {e}")
        except Exception as e:
            logger.error(f"An unexpected error occurred while fetching ticker for {s} on {exchange_id}: {e}")
            
    logger.error(f"Failed to fetch price for original symbol {symbol} on {exchange_id} after trying all variations.")
    return None

async def get_portfolio_value(user_id: int, db: DatabaseManager):
    portfolio = await db.get_portfolio(user_id)
    if not portfolio: return Decimal('0.0')
    tasks = [fetch_price(item['exchange'], item['symbol']) for item in portfolio]
    results = await asyncio.gather(*tasks)
    total_value = Decimal('0.0')
    for i, item in enumerate(portfolio):
        current_price = results[i]
        quantity = Decimal(item['quantity'])
        if current_price:
            total_value += quantity * Decimal(str(current_price))
        else:
            total_value += quantity * Decimal(item['avg_price'])
    return total_value

async def generate_portfolio_report(user_id: int, db: DatabaseManager) -> str:
    portfolio = await db.get_portfolio(user_id)
    if not portfolio: return "محفظتك فارغة حالياً."
    tasks = [fetch_price(item['exchange'], item['symbol']) for item in portfolio]
    results = await asyncio.gather(*tasks)
    total_portfolio_value = Decimal('0.0')
    total_investment_cost = Decimal('0.0')
    report_lines = []
    
    # Pre-calculate totals
    for i, item in enumerate(portfolio):
        quantity = Decimal(item['quantity'])
        avg_price = Decimal(item['avg_price'])
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
    
    summary = (f"**📊 ملخص المحفظة**\n\n"
               f"▪️ **رأس المال:** `{format_price(total_investment_cost)}`\n"
               f"▪️ **القيمة الحالية:** `{format_price(total_portfolio_value)}`\n"
               f"{total_pnl_icon} **إجمالي الربح/الخسارة:**\n"
               f"`{format_price(total_pnl)} ({total_pnl_percent:+.2f}%)`\n\n"
               f"--- **التفاصيل** ---\n")
    report_lines.append(summary)
    
    # Generate detailed lines
    for i, item in enumerate(portfolio):
        quantity = Decimal(item['quantity'])
        avg_price = Decimal(item['avg_price'])
        investment_cost = quantity * avg_price
        
        line = (f"*{i + 1}.* 🆔 `{item['id']}` | **{item['symbol']}** | `{item['exchange'].capitalize()}`\n"
                f"الكمية: `{format_quantity(quantity)}`")
        
        current_price = results[i]
        if current_price:
            current_price_dec = Decimal(str(current_price))
            current_value = quantity * current_price_dec
            pnl = current_value - investment_cost
            pnl_percent = (pnl / investment_cost * 100) if investment_cost > 0 else 0
            pnl_icon = "📈" if pnl >= 0 else "📉"
            
            line += (f"\n- سعر الشراء: `{format_price(avg_price)}` (التكلفة: `{format_price(investment_cost)}`)\n"
                     f"- السعر الحالي: `{format_price(current_price_dec)}` (القيمة: `{format_price(current_value)}`)\n"
                     f"{pnl_icon} الربح/الخسارة: `{format_price(pnl)} ({pnl_percent:+.2f}%)`")
        else:
            line += (f"\n- سعر الشراء: `{format_price(avg_price)}` (التكلفة: `{format_price(investment_cost)}`)\n"
                     f"- السعر الحالي: `غير متاح`\n"
                     f"📉 الربح/الخسارة: `غير متاح`")
        
        report_lines.append(line)
        report_lines.append("---")
        
    return "\n".join(report_lines)

async def portfolio_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    db: DatabaseManager = context.bot_data['db']
    try:
        msg = await update.message.reply_text("⏳ جارٍ إعداد التقرير...")
        report_text = await generate_portfolio_report(user_id, db)
        await msg.edit_text(report_text, parse_mode=ParseMode.MARKDOWN)
    except TelegramError as e:
        logger.error(f"خطأ في عرض المحفظة: {e}")
        await update.message.reply_text("حدث خطأ أثناء عرض المحفظة. الرجاء المحاولة مرة أخرى.")

# --- Scheduled Jobs ---
async def send_daily_report(context: ContextTypes.DEFAULT_TYPE) -> None:
    db: DatabaseManager = context.bot_data['db']
    user_ids = await db.get_users_for_daily_report()
    for user_id in user_ids:
        try:
            report_text = await generate_portfolio_report(user_id, db)
            final_report = f"**🗓️ تقريرك اليومي للمحفظة**\n\n{report_text}"
            await context.bot.send_message(chat_id=user_id, text=final_report, parse_mode=ParseMode.MARKDOWN)
            await asyncio.sleep(1) # Rate limit
        except Exception as e:
            logger.error(f"فشل إرسال التقرير اليومي للمستخدم {user_id}: {e}")

async def check_alerts(context: ContextTypes.DEFAULT_TYPE) -> None:
    db: DatabaseManager = context.bot_data['db']
    
    # Global portfolio alerts
    users_to_check = await db.get_users_for_alert_check()
    for user in users_to_check:
        try:
            if user['last_portfolio_value'] is None or user['last_check_time'] is None:
                current_value = await get_portfolio_value(user['user_id'], db)
                await db.update_last_portfolio_value(user['user_id'], current_value)
                continue

            last_value = Decimal(user['last_portfolio_value'])
            if last_value == 0: continue

            current_value = await get_portfolio_value(user['user_id'], db)
            percentage_change = abs((current_value - last_value) / last_value * 100)

            if percentage_change >= Decimal(user['global_alert_threshold']):
                direction_text = "ارتفاع" if current_value > last_value else "انخفاض"
                direction_icon = "📈" if current_value > last_value else "📉"
                alert_message = (f"**🚨 تنبيه حركة المحفظة!** {direction_icon}\n\n"
                                 f"حدث **{direction_text}** في القيمة الإجمالية لمحفظتك بنسبة **{percentage_change:.2f}%**.\n\n"
                                 f"▪️ القيمة السابقة: `{format_price(last_value)}`\n"
                                 f"▪️ القيمة الحالية: `{format_price(current_value)}`")
                await context.bot.send_message(chat_id=user['user_id'], text=alert_message, parse_mode=ParseMode.MARKDOWN)
                await db.update_last_portfolio_value(user['user_id'], current_value)
            
            await asyncio.sleep(1)
        except Exception as e:
            logger.error(f"فشل فحص تنبيه المحفظة للمستخدم {user['user_id']}: {e}")

    # Individual coin alerts
    coins_to_check = await db.get_coins_for_alert_check()
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
                await db.set_coin_alert(coin['id'], threshold, current_price)
            
            await asyncio.sleep(1)
        except Exception as e:
            logger.error(f"فشل فحص تنبيه العملة {coin['symbol']} للمستخدم {coin['user_id']}: {e}")

# --- Add Coin Conversation ---
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
        db: DatabaseManager = context.bot_data['db']
        await db.add_or_update_coin(user_id, user_data['symbol'], user_data['exchange'], user_data['quantity'], price)
        current_value = await get_portfolio_value(user_id, db)
        await db.update_last_portfolio_value(user_id, current_value)
        await update.message.reply_text(f"✅ **تمت إضافة/تحديث {user_data['symbol']} بنجاح!**", reply_markup=MAIN_REPLY_MARKUP, parse_mode=ParseMode.MARKDOWN)
        user_data.clear()
        return ConversationHandler.END
    except Exception:
        await update.message.reply_text("قيمة غير صالحة. الرجاء إدخال السعر كرقم موجب.")
        return PRICE

# --- Remove Coin Conversation ---
async def remove_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("لحذف عملية، أرسل رقم الـ ID الخاص بها.", reply_markup=ReplyKeyboardRemove())
    return REMOVE_ID

async def received_remove_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    db: DatabaseManager = context.bot_data['db']
    try:
        coin_id_to_remove = int(update.message.text)
        if await db.remove_coin(coin_id_to_remove, user_id):
            await update.message.reply_text(f"✅ تم حذف العملية رقم `{coin_id_to_remove}` بنجاح.", reply_markup=MAIN_REPLY_MARKUP)
        else:
            await update.message.reply_text(f"لم يتم العثور على عملية بالرقم `{coin_id_to_remove}`.", reply_markup=MAIN_REPLY_MARKUP)
    except ValueError:
        await update.message.reply_text("إدخال غير صالح. أرسل رقم فقط.", reply_markup=MAIN_REPLY_MARKUP)
    return ConversationHandler.END

# --- Edit Coin Conversation ---
async def edit_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("لتعديل عملة، أرسل رقم الـ ID الخاص بها.", reply_markup=ReplyKeyboardRemove())
    return EDIT_ID

async def received_edit_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    db: DatabaseManager = context.bot_data['db']
    try:
        coin_id = int(update.message.text)
        coin = await db.get_coin_by_id(coin_id, user_id)
        if coin:
            context.user_data['edit_coin_id'] = coin_id
            text = (f"**العملة المحددة:** {coin['symbol']}\n"
                    f"الكمية الحالية: `{format_quantity(coin['quantity'])}`\n"
                    f"سعر الشراء الحالي: `{format_price(coin['avg_price'])}`\n\n"
                    "ماذا تريد أن تعدل؟")
            keyboard = [
                [InlineKeyboardButton("✏️ تعديل الكمية", callback_data=CALLBACK_EDIT_QUANTITY)],
                [InlineKeyboardButton("💰 تعديل السعر", callback_data=CALLBACK_EDIT_PRICE)]
            ]
            await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
            return CHOOSE_EDIT_FIELD
        else:
            await update.message.reply_text(f"لم يتم العثور على عملة بالرقم `{coin_id}`.", reply_markup=MAIN_REPLY_MARKUP)
            return ConversationHandler.END
    except (ValueError, TypeError):
        await update.message.reply_text("إدخال غير صالح. أرسل رقم ID صحيح.", reply_markup=MAIN_REPLY_MARKUP)
        return ConversationHandler.END

async def choose_edit_field_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    
    if query.data == CALLBACK_EDIT_QUANTITY:
        await query.edit_message_text("أرسل الكمية الجديدة.")
        return GET_NEW_QUANTITY
    elif query.data == CALLBACK_EDIT_PRICE:
        await query.edit_message_text("أرسل متوسط سعر الشراء الجديد.")
        return GET_NEW_PRICE
    return ConversationHandler.END

async def received_new_quantity(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        new_quantity = Decimal(update.message.text)
        if new_quantity <= 0: raise ValueError()
        
        user_id = update.effective_user.id
        coin_id = context.user_data['edit_coin_id']
        db: DatabaseManager = context.bot_data['db']
        
        if await db.update_coin_details(coin_id, user_id, new_quantity=new_quantity):
            await update.message.reply_text("✅ تم تحديث الكمية بنجاح.", reply_markup=MAIN_REPLY_MARKUP)
        else:
            await update.message.reply_text("❌ فشل تحديث الكمية.", reply_markup=MAIN_REPLY_MARKUP)
        
        context.user_data.clear()
        return ConversationHandler.END
    except Exception:
        await update.message.reply_text("قيمة غير صالحة. الرجاء إدخال الكمية كرقم موجب.")
        return GET_NEW_QUANTITY

async def received_new_price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        new_price = Decimal(update.message.text)
        if new_price <= 0: raise ValueError()
        
        user_id = update.effective_user.id
        coin_id = context.user_data['edit_coin_id']
        db: DatabaseManager = context.bot_data['db']
        
        if await db.update_coin_details(coin_id, user_id, new_avg_price=new_price):
            await update.message.reply_text("✅ تم تحديث السعر بنجاح.", reply_markup=MAIN_REPLY_MARKUP)
        else:
            await update.message.reply_text("❌ فشل تحديث السعر.", reply_markup=MAIN_REPLY_MARKUP)
            
        context.user_data.clear()
        return ConversationHandler.END
    except Exception:
        await update.message.reply_text("قيمة غير صالحة. الرجاء إدخال السعر كرقم موجب.")
        return GET_NEW_PRICE

# --- Bulk Import Conversation ---
async def import_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    instructions = """
    **📥 استيراد محفظة جماعي**

    ألصق قائمة العملات الخاصة بك هنا.
    يجب أن يكون كل سطر لعملة واحدة بالتنسيق التالي:
    `المنصة,رمز العملة,الكمية,متوسط سعر الشراء`

    **مثال:**
    ```
gateio,BITBOARD/USDT,18967,0.0009069
bybit,COOKIE/USDT,96.78,0.66
kucoin,POLC/USDT,1976,0.002095
    ```
    """
    await update.message.reply_text(instructions, parse_mode=ParseMode.MARKDOWN, reply_markup=ReplyKeyboardRemove())
    return BULK_IMPORT

async def received_bulk_import(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    db: DatabaseManager = context.bot_data['db']
    lines = update.message.text.splitlines()
    success_count = 0
    fail_count = 0
    
    for line in lines:
        try:
            exchange, symbol, quantity, price = [item.strip() for item in line.split(',')]
            if '/' not in symbol: symbol = f"{symbol}/USDT"
            # Basic validation
            Decimal(quantity); Decimal(price)
            if exchange.lower() not in exchanges:
                raise ValueError(f"منصة غير مدعومة: {exchange}")
            
            await db.add_or_update_coin(user_id, symbol, exchange, quantity, price)
            success_count += 1
        except Exception as e:
            fail_count += 1
            logger.warning(f"فشل استيراد السطر: '{line}'. السبب: {e}")

    await update.message.reply_text(
        f"**✅ اكتمل الاستيراد.**\n\n"
        f"تمت معالجة **{success_count}** عملة بنجاح.\n"
        f"فشلت معالجة **{fail_count}** عملة.",
        reply_markup=MAIN_REPLY_MARKUP,
        parse_mode=ParseMode.MARKDOWN
    )
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("تم إلغاء العملية.", reply_markup=MAIN_REPLY_MARKUP)
    return ConversationHandler.END

# --- Application Setup ---
async def post_init(application: Application):
    """المهام التي يتم تنفيذها بعد بدء تشغيل البوت."""
    db: DatabaseManager = application.bot_data['db']
    await db.init_database()

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

async def post_shutdown(application: Application):
    """المهام التي يتم تنفيذها قبل إيقاف البوت."""
    db: DatabaseManager = application.bot_data['db']
    instance_id = application.bot_data.get('instance_id')
    await db.release_lock(instance_id)
    await db.close()

    for ex_id, ex_instance in exchanges.items():
        try:
            await ex_instance.close()
            logger.info(f"تم إغلاق الاتصال بمنصة {ex_id}.")
        except: pass

def main() -> None:
    """Start the bot."""
    if not TELEGRAM_BOT_TOKEN or not DATABASE_URL:
        logger.critical("لم يتم العثور على متغيرات البيئة المطلوبة (TELEGRAM_BOT_TOKEN, DATABASE_URL).")
        sys.exit(1)

    instance_id = str(uuid.uuid4())
    
    # Check for lock synchronously before starting async loop
    db_manager = DatabaseManager(DATABASE_URL)
    # This is a bit of a workaround to run an async function before the main loop
    if not asyncio.run(db_manager.connect()) or not asyncio.run(db_manager.acquire_lock(instance_id)):
         logger.critical("فشل في الحصول على قفل التشغيل. سيتم إغلاق البوت.")
         asyncio.run(db_manager.close())
         sys.exit(1)
    
    # The pool is already created from the connect() call above.
    
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    # Store db manager and instance_id for global access in handlers
    application.bot_data['db'] = db_manager
    application.bot_data['instance_id'] = instance_id

    # Register lifecycle hooks
    application.post_init = post_init
    application.post_shutdown = post_shutdown

    # Conversation handlers
    add_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(r'^➕ إضافة عملة$'), add_start)],
        states={
            EXCHANGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_exchange)],
            SYMBOL: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_symbol)],
            QUANTITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_quantity)],
            PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_price)],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
    )

    remove_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(r'^🗑️ حذف عملة$'), remove_start)],
        states={REMOVE_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_remove_id)]},
        fallbacks=[CommandHandler('cancel', cancel)],
    )

    edit_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(r'^✏️ تعديل عملة$'), edit_start)],
        states={
            EDIT_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_edit_id)],
            CHOOSE_EDIT_FIELD: [CallbackQueryHandler(choose_edit_field_callback)],
            GET_NEW_QUANTITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_new_quantity)],
            GET_NEW_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_new_price)],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
    )

    settings_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(r'^⚙️ الإعدادات$'), settings_start)],
        states={
            CHOOSE_SETTING: [
                MessageHandler(filters.Regex(r'^تبديل حالة التنبيهات'), toggle_alerts),
                MessageHandler(filters.Regex(r'^تنبيه المحفظة الكلي'), change_global_threshold_start),
                MessageHandler(filters.Regex(r'^⚙️ تخصيص تنبيهات العملات$'), custom_alerts_start),
            ],
            SET_GLOBAL_ALERT: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_global_threshold)],
            SELECT_COIN_ALERT: [CallbackQueryHandler(select_coin_alert_callback, pattern=f"^{CALLBACK_PREFIX_SET_ALERT}")],
            SET_COIN_ALERT: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_coin_threshold)],
        },
        fallbacks=[
            MessageHandler(filters.Regex(r'^🔙 العودة للقائمة الرئيسية$'), cancel),
            CommandHandler('cancel', cancel)
        ],
    )
    
    import_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(r'📥 استيراد محفظة'), import_start)],
        states={BULK_IMPORT: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_bulk_import)]},
        fallbacks=[CommandHandler('cancel', cancel)]
    )


    # Register handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(MessageHandler(filters.Regex(r'^📊 عرض المحفظة$'), portfolio_command))
    application.add_handler(add_conv)
    application.add_handler(remove_conv)
    application.add_handler(edit_conv)
    application.add_handler(settings_conv)
    application.add_handler(import_conv)
    
    # Generic fallback for main menu options that are not conversations
    application.add_handler(MessageHandler(filters.Regex(r'^❓ مساعدة$'), help_command))


    # Run the bot
    application.run_polling()

if __name__ == "__main__":
    main()


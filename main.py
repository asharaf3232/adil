# -*- coding: utf-8 -*-

# --- المكتبات المطلوبة ---
import os
import sqlite3
import logging
import asyncio
from decimal import Decimal, ROUND_DOWN

import ccxt.async_support as ccxt
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
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
# هام: استبدل 'YOUR_TELEGRAM_BOT_TOKEN' بالتوكن الخاص ببوتك الذي حصلت عليه من BotFather
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

# --- حالات المحادثة لإضافة عملة ---
EXCHANGE, SYMBOL, QUANTITY, PRICE = range(4)

# --- متغير عالمي لتخزين كائنات المنصات ---
exchanges = {}

async def initialize_exchanges():
    """يقوم بتهيئة الاتصال بالمنصات عند بدء تشغيل البوت."""
    global exchanges
    # يمكنك إضافة المزيد من المنصات هنا
    exchange_ids = ['binance', 'okx', 'kucoin', 'gateio', 'bybit', 'mexc']
    for ex_id in exchange_ids:
        try:
            exchange_class = getattr(ccxt, ex_id)
            exchanges[ex_id] = exchange_class({'enableRateLimit': True})
            logger.info(f"تم الاتصال بمنصة {ex_id} بنجاح.")
        except Exception as e:
            logger.error(f"فشل الاتصال بمنصة {ex_id}: {e}")

async def close_exchanges():
    """يغلق جميع اتصالات المنصات عند إيقاف البوت."""
    for ex_id, ex_instance in exchanges.items():
        try:
            await ex_instance.close()
            logger.info(f"تم إغلاق الاتصال بمنصة {ex_id}.")
        except Exception as e:
            logger.error(f"خطأ أثناء إغلاق اتصال {ex_id}: {e}")

# --- دوال التعامل مع قاعدة البيانات ---

def db_add_or_update_coin(user_id, symbol, exchange, quantity, price):
    """تضيف عملة جديدة أو تحدث كمية وسعر عملة موجودة."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    # التحقق مما إذا كانت العملة موجودة بالفعل
    cursor.execute("SELECT quantity, avg_price FROM portfolio WHERE user_id = ? AND symbol = ? AND exchange = ?",
                   (user_id, symbol, exchange))
    result = cursor.fetchone()

    if result:
        # تحديث الكمية ومتوسط السعر
        old_quantity, old_avg_price = result
        total_quantity = old_quantity + quantity
        new_avg_price = ((old_quantity * old_avg_price) + (quantity * price)) / total_quantity
        
        cursor.execute("""
            UPDATE portfolio 
            SET quantity = ?, avg_price = ? 
            WHERE user_id = ? AND symbol = ? AND exchange = ?
        """, (total_quantity, new_avg_price, user_id, symbol, exchange))
        
    else:
        # إضافة عملة جديدة
        cursor.execute("""
            INSERT INTO portfolio (user_id, symbol, exchange, quantity, avg_price) 
            VALUES (?, ?, ?, ?, ?)
        """, (user_id, symbol.upper(), exchange.lower(), quantity, price))
        
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

# --- أوامر التليجرام ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """يرسل رسالة ترحيبية عند بدء البوت."""
    user = update.effective_user
    welcome_message = (
        f"أهلاً بك يا {user.mention_html()} في بوت تتبع المحفظة!\n\n"
        "يمكنني مساعدتك في تتبع جميع عملاتك الرقمية الموزعة على عدة منصات في مكان واحد.\n\n"
        "**الأوامر المتاحة:**\n"
        "/add - لإضافة عملة جديدة لمحفظتك\n"
        "/portfolio - لعرض محفظتك الحالية مع الأرباح والخسائر\n"
        "/remove - لحذف عملة من محفظتك\n"
        "/help - لعرض هذه الرسالة مرة أخرى"
    )
    await update.message.reply_html(welcome_message)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """يعرض رسالة المساعدة."""
    help_text = (
        "**الأوامر المتاحة:**\n\n"
        "**/add**\n"
        "لبدء عملية إضافة عملة جديدة لمحفظتك. سيقوم البوت بسؤالك عن التفاصيل خطوة بخطوة.\n\n"
        "**/portfolio**\n"
        "لعرض تقرير مفصل عن محفظتك، يتضمن الأسعار الحية، القيمة الإجمالية، والأرباح والخسائر العائمة لكل عملة.\n\n"
        "**/remove**\n"
        "لحذف عملة من محفظتك. سيطلب منك البوت رقم ID الخاص بالعملية الذي يظهر في تقرير المحفظة.\n\n"
        "**/cancel**\n"
        "لإلغاء أي عملية جارية (مثل عملية إضافة عملة)."
    )
    await update.message.reply_text(help_text, parse_mode=ParseMode.MARKDOWN)

async def add_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
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
    """يخزن المنصة ويسأل عن رمز العملة."""
    exchange = update.message.text.lower()
    if exchange not in exchanges:
        await update.message.reply_text("منصة غير مدعومة. الرجاء اختيار واحدة من القائمة.")
        return EXCHANGE
    
    context.user_data['exchange'] = exchange
    await update.message.reply_text(
        f"تم اختيار منصة: *{exchange}*\n\n"
        "**الخطوة 2 من 4:** ما هو رمز العملة؟ (مثال: `BTC/USDT` أو `ETH/USDT`)",
        reply_markup=ReplyKeyboardRemove(),
        parse_mode=ParseMode.MARKDOWN
    )
    return SYMBOL

async def received_symbol(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """يخزن الرمز ويسأل عن الكمية."""
    symbol = update.message.text.upper()
    
    # تحقق بسيط من صيغة الرمز
    if '/' not in symbol:
        await update.message.reply_text("صيغة الرمز غير صحيحة. الرجاء إدخال الرمز بصيغة `COIN/PAIR` مثل `BTC/USDT`.")
        return SYMBOL

    context.user_data['symbol'] = symbol
    await update.message.reply_text(
        f"تم تحديد العملة: *{symbol}*\n\n"
        "**الخطوة 3 من 4:** ما هي الكمية التي اشتريتها؟ (مثال: `0.5`)",
        parse_mode=ParseMode.MARKDOWN
    )
    return QUANTITY
    
async def received_quantity(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """يخزن الكمية ويسأل عن السعر."""
    try:
        quantity = float(update.message.text)
        if quantity <= 0:
            raise ValueError("Quantity must be positive.")
        context.user_data['quantity'] = quantity
        await update.message.reply_text(
            f"الكمية: *{quantity}*\n\n"
            "**الخطوة 4 من 4:** ما هو متوسط سعر الشراء؟ (مثال: `65000.5`)",
            parse_mode=ParseMode.MARKDOWN
        )
        return PRICE
    except ValueError:
        await update.message.reply_text("قيمة غير صالحة. الرجاء إدخال الكمية كرقم موجب.")
        return QUANTITY

async def received_price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """يخزن السعر ويضيف العملة لقاعدة البيانات."""
    try:
        price = float(update.message.text)
        if price <= 0:
            raise ValueError("Price must be positive.")
        
        user_id = update.effective_user.id
        user_data = context.user_data
        
        db_add_or_update_coin(
            user_id,
            user_data['symbol'],
            user_data['exchange'],
            user_data['quantity'],
            price
        )

        await update.message.reply_text(
            f"✅ **تمت إضافة/تحديث عملة {user_data['symbol']} بنجاح!**\n\n"
            "يمكنك الآن عرض محفظتك المحدثة باستخدام الأمر /portfolio.",
            parse_mode=ParseMode.MARKDOWN
        )
        user_data.clear()
        return ConversationHandler.END
    except ValueError:
        await update.message.reply_text("قيمة غير صالحة. الرجاء إدخال السعر كرقم موجب.")
        return PRICE

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """يلغي المحادثة الحالية."""
    context.user_data.clear()
    await update.message.reply_text("تم إلغاء العملية.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


async def fetch_price(exchange_id, symbol):
    """تجلب السعر الحالي لعملة معينة من منصة معينة."""
    exchange = exchanges.get(exchange_id)
    if not exchange:
        return None
    try:
        # بعض المنصات تحتاج إلى تبديل '/' بـ '-'
        ticker_symbol = symbol.replace('/', '') if exchange.id in ['bybit', 'okx'] else symbol
        ticker = await exchange.fetch_ticker(ticker_symbol)
        return ticker['last']
    except Exception as e:
        logger.warning(f"لم يتم العثور على سعر لـ {symbol} على منصة {exchange_id}: {e}")
        return None

async def portfolio_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """يعرض المحفظة الكاملة مع حسابات الأرباح والخسائر."""
    user_id = update.effective_user.id
    portfolio = db_get_portfolio(user_id)

    if not portfolio:
        await update.message.reply_text("محفظتك فارغة حالياً. ابدأ بإضافة عملات باستخدام الأمر /add.")
        return

    msg = await update.message.reply_text("⏳ جارٍ جلب الأسعار الحية وتحديث المحفظة...")

    tasks = [fetch_price(item['exchange'], item['symbol']) for item in portfolio]
    prices = await asyncio.gather(*tasks)

    total_portfolio_value = Decimal('0.0')
    total_investment_cost = Decimal('0.0')
    
    report_lines = ["**📊 تقرير محفظتك الحالي**\n", "---"]
    
    for i, item in enumerate(portfolio):
        current_price = prices[i]
        
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
            line += "\n السعر الحالي: `غير متاح`"
            # نضيف قيمة الاستثمار للقيمة الإجمالية في حال فشل جلب السعر
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

async def remove_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """يحذف عملة من المحفظة."""
    user_id = update.effective_user.id
    try:
        coin_id_to_remove = int(context.args[0])
        if db_remove_coin(coin_id_to_remove, user_id):
            await update.message.reply_text(f"✅ تم حذف العملية رقم `{coin_id_to_remove}` من محفظتك بنجاح.")
        else:
            await update.message.reply_text(f"لم يتم العثور على عملية بالرقم `{coin_id_to_remove}` في محفظتك.")

    except (IndexError, ValueError):
        await update.message.reply_text(
            "الرجاء تحديد رقم ID للعملية التي تريد حذفها.\n"
            "مثال: `/remove 12`\n\n"
            "يمكنك العثور على الـ ID بجانب كل عملة عند عرض محفظتك بالأمر /portfolio."
        )


def main() -> None:
    """الدالة الرئيسية لتشغيل البوت."""
    if TELEGRAM_BOT_TOKEN == 'YOUR_TELEGRAM_BOT_TOKEN':
        logger.critical("FATAL ERROR: لم يتم تعيين توكن التليجرام. يرجى تعديل المتغير 'TELEGRAM_BOT_TOKEN'.")
        return

    init_database()

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(initialize_exchanges).post_shutdown(close_exchanges).build()

    # --- محادثة إضافة عملة ---
    add_conv_handler = ConversationHandler(
        entry_points=[CommandHandler("add", add_command)],
        states={
            EXCHANGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_exchange)],
            SYMBOL: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_symbol)],
            QUANTITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_quantity)],
            PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_price)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    application.add_handler(add_conv_handler)
    
    # --- الأوامر الأخرى ---
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("portfolio", portfolio_command))
    application.add_handler(CommandHandler("remove", remove_command))

    logger.info("... البوت قيد التشغيل ...")
    application.run_polling()


if __name__ == "__main__":
    main()

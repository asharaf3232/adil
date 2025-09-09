# -*- coding: utf-8 -*-

# --- المكتبات المطلوبة --- #
import ccxt.async_support as ccxt
import pandas as pd
import pandas_ta as ta
import asyncio
import os
import logging
import json
import re
import time
import sqlite3
from datetime import datetime, time as dt_time, timedelta
from zoneinfo import ZoneInfo

# [NEW] Add requests for Fear & Greed Index
import requests
from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters, CallbackQueryHandler
from telegram.error import BadRequest, RetryAfter, TimedOut

# [NEW] Gracefully handle optional scipy import
try:
    from scipy.signal import find_peaks
    SCIPY_AVAILABLE = True
except ImportError:
    SCIPY_AVAILABLE = False
    logging.warning("Library 'scipy' not found. RSI Divergence strategy will be disabled.")


# --- الإعدادات الأساسية --- #
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', 'YOUR_BOT_TOKEN_HERE')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', 'YOUR_CHAT_ID_HERE')
TELEGRAM_SIGNAL_CHANNEL_ID = os.getenv('TELEGRAM_SIGNAL_CHANNEL_ID', TELEGRAM_CHAT_ID)


if TELEGRAM_BOT_TOKEN == 'YOUR_BOT_TOKEN_HERE' or TELEGRAM_CHAT_ID == 'YOUR_CHAT_ID_HERE':
    print("FATAL ERROR: Please set your Telegram Token and Chat ID.")
    exit()

# --- إعدادات البوت --- #
EXCHANGES_TO_SCAN = ['binance', 'okx', 'bybit', 'kucoin', 'gate', 'mexc']
TIMEFRAME = '15m'
HIGHER_TIMEFRAME = '1h' # الإطار الزمني الأعلى للفلترة
SCAN_INTERVAL_SECONDS = 900
TRACK_INTERVAL_SECONDS = 120

APP_ROOT = '.'
DB_FILE = os.path.join(APP_ROOT, 'trading_bot_v25.db')
SETTINGS_FILE = os.path.join(APP_ROOT, 'settings_v25.json')


EGYPT_TZ = ZoneInfo("Africa/Cairo")

# --- إعداد مسجل الأحداث (Logger) --- #
LOG_FILE = os.path.join(APP_ROOT, 'bot_v25.log')
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO, handlers=[logging.FileHandler(LOG_FILE, 'w'), logging.StreamHandler()])
logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('apscheduler').setLevel(logging.WARNING)
logging.getLogger('telegram').setLevel(logging.WARNING)
logging.getLogger('requests').setLevel(logging.WARNING) # Mute requests library logs


# --- [IMPROVEMENT] Preset Configurations ---
PRESET_PRO = {
  "liquidity_filters": {"min_quote_volume_24h_usd": 1000000, "max_spread_percent": 0.45, "rvol_period": 18, "min_rvol": 1.5},
  "volatility_filters": {"atr_period_for_filter": 14, "min_atr_percent": 0.85},
  "ema_trend_filter": {"enabled": True, "ema_period": 200},
  "min_tp_sl_filter": {"min_tp_percent": 1.1, "min_sl_percent": 0.6}
}
PRESET_LAX = {
  "liquidity_filters": {"min_quote_volume_24h_usd": 400000, "max_spread_percent": 1.3, "rvol_period": 12, "min_rvol": 1.1},
  "volatility_filters": {"atr_period_for_filter": 10, "min_atr_percent": 0.3},
  "ema_trend_filter": {"enabled": False, "ema_period": 200},
  "min_tp_sl_filter": {"min_tp_percent": 0.4, "min_sl_percent": 0.2}
}
PRESET_STRICT = {
  "liquidity_filters": {"min_quote_volume_24h_usd": 2500000, "max_spread_percent": 0.22, "rvol_period": 25, "min_rvol": 2.2},
  "volatility_filters": {"atr_period_for_filter": 20, "min_atr_percent": 1.4},
  "ema_trend_filter": {"enabled": True, "ema_period": 200},
  "min_tp_sl_filter": {"min_tp_percent": 1.8, "min_sl_percent": 0.9}
}
PRESET_VERY_LAX = {
  "liquidity_filters": {"min_quote_volume_24h_usd": 200000, "max_spread_percent": 2.0, "rvol_period": 10, "min_rvol": 0.8},
  "volatility_filters": {"atr_period_for_filter": 10, "min_atr_percent": 0.2},
  "ema_trend_filter": {"enabled": False, "ema_period": 200},
  "min_tp_sl_filter": {"min_tp_percent": 0.3, "min_sl_percent": 0.15}
}
PRESETS = {"PRO": PRESET_PRO, "LAX": PRESET_LAX, "STRICT": PRESET_STRICT, "VERY_LAX": PRESET_VERY_LAX}

# --- [EDIT] Expanded Constants for a more comprehensive Interactive Settings menu ---
EDITABLE_PARAMS = {
    "إعدادات عامة": [
        "max_concurrent_trades", "top_n_symbols_by_volume", "concurrent_workers",
        "min_signal_strength"
    ],
    "إعدادات المخاطر": [
        "virtual_trade_size_percentage", "atr_sl_multiplier", "risk_reward_ratio",
        "trailing_sl_activate_percent", "trailing_sl_percent"
    ],
    "الفلاتر والاتجاه": [
        "market_regime_filter_enabled", "use_master_trend_filter", "fear_and_greed_filter_enabled",
        "master_adx_filter_level", "master_trend_filter_ma_period", "trailing_sl_enabled", "fear_and_greed_threshold"
    ]
}
PARAM_DISPLAY_NAMES = {
    "virtual_trade_size_percentage": "حجم الصفقة (%)",
    "max_concurrent_trades": "أقصى عدد للصفقات",
    "top_n_symbols_by_volume": "عدد العملات للفحص",
    "concurrent_workers": "عمال الفحص المتزامنين",
    "min_signal_strength": "أدنى قوة للإشارة",
    "atr_sl_multiplier": "مضاعف وقف الخسارة (ATR)",
    "risk_reward_ratio": "نسبة المخاطرة/العائد",
    "trailing_sl_activate_percent": "تفعيل الوقف المتحرك (%)",
    "trailing_sl_percent": "مسافة الوقف المتحرك (%)",
    "market_regime_filter_enabled": "فلتر وضع السوق (متكامل)",
    "use_master_trend_filter": "فلتر الاتجاه العام (BTC)",
    "master_adx_filter_level": "مستوى فلتر ADX",
    "master_trend_filter_ma_period": "فترة فلتر الاتجاه",
    "trailing_sl_enabled": "تفعيل الوقف المتحرك",
    "fear_and_greed_filter_enabled": "فلتر الخوف والطمع", # New
    "fear_and_greed_threshold": "حد مؤشر الخوف", # New
}


# --- متغيرات الحالة العامة للبوت --- #
bot_data = {"exchanges": {}, "last_signal_time": {}, "settings": {}, "status_snapshot": {"last_scan_start_time": "N/A", "last_scan_end_time": "N/A", "markets_found": 0, "signals_found": 0, "active_trades_count": 0, "scan_in_progress": False}}
scan_lock = asyncio.Lock()

# --- إدارة الإعدادات --- #
DEFAULT_SETTINGS = {
    "virtual_portfolio_balance_usdt": 1000.0, "virtual_trade_size_percentage": 5.0, "max_concurrent_trades": 5, "top_n_symbols_by_volume": 250, "concurrent_workers": 10, "market_regime_filter_enabled": True,
    "active_scanners": ["momentum_breakout", "breakout_squeeze_pro", "rsi_divergence", "supertrend_pullback"],
    "use_master_trend_filter": True, "master_trend_filter_ma_period": 50, "master_adx_filter_level": 22,
    "fear_and_greed_filter_enabled": True, "fear_and_greed_threshold": 30, # New settings
    "use_dynamic_risk_management": True, "atr_period": 14, "atr_sl_multiplier": 2.0, "risk_reward_ratio": 1.5,
    "take_profit_percentage": 4.0, "stop_loss_percentage": 2.0, "trailing_sl_enabled": True, "trailing_sl_activate_percent": 2.0, "trailing_sl_percent": 1.5,
    "momentum_breakout": {"vwap_period": 14, "macd_fast": 12, "macd_slow": 26, "macd_signal": 9, "bbands_period": 20, "bbands_stddev": 2.0, "rsi_period": 14, "rsi_max_level": 68, "volume_spike_multiplier": 1.5},
    "breakout_squeeze_pro": {"bbands_period": 20, "bbands_stddev": 2.0, "keltner_period": 20, "keltner_atr_multiplier": 1.5, "volume_confirmation_enabled": True},
    "rsi_divergence": {"rsi_period": 14, "lookback_period": 35, "peak_trough_lookback": 5, "confirm_with_rsi_exit": True},
    "supertrend_pullback": {"atr_period": 10, "atr_multiplier": 3.0, "swing_high_lookback": 10},
    "liquidity_filters": {"min_quote_volume_24h_usd": 1_000_000, "max_spread_percent": 0.5, "rvol_period": 20, "min_rvol": 1.5},
    "volatility_filters": {"atr_period_for_filter": 14, "min_atr_percent": 0.8},
    "stablecoin_filter": {"exclude_bases": ["USDT","USDC","DAI","FDUSD","TUSD","USDE","PYUSD","GUSD","EURT","USDJ"]},
    "ema_trend_filter": {"enabled": True, "ema_period": 200},
    "min_tp_sl_filter": {"min_tp_percent": 1.0, "min_sl_percent": 0.5},
    "min_signal_strength": 1,
    "active_preset_name": "Default"
}


def load_settings():
    try:
        if os.path.exists(SETTINGS_FILE):
            with open(SETTINGS_FILE, 'r') as f: bot_data["settings"] = json.load(f)
            updated = False
            for key, value in DEFAULT_SETTINGS.items():
                if key not in bot_data["settings"]:
                    bot_data["settings"][key] = value; updated = True
                elif isinstance(value, dict):
                    for sub_key, sub_value in value.items():
                        if sub_key not in bot_data["settings"].get(key, {}):
                            bot_data["settings"][key][sub_key] = sub_value; updated = True

            keys_to_remove = [key for key in bot_data["settings"] if key not in DEFAULT_SETTINGS]
            if keys_to_remove:
                logging.info(f"Removing obsolete settings keys: {keys_to_remove}")
                for key in keys_to_remove: del bot_data["settings"][key]
                updated = True

            if updated: save_settings()
        else:
            bot_data["settings"] = DEFAULT_SETTINGS
            save_settings()
        logging.info(f"Settings loaded successfully from {SETTINGS_FILE}")
    except Exception as e:
        logging.error(f"Failed to load settings: {e}")
        bot_data["settings"] = DEFAULT_SETTINGS


def save_settings():
    try:
        with open(SETTINGS_FILE, 'w') as f: json.dump(bot_data["settings"], f, indent=4)
        logging.info(f"Settings saved successfully to {SETTINGS_FILE}")
    except Exception as e:
        logging.error(f"Failed to save settings: {e}")

# --- إدارة قاعدة البيانات --- #
def init_database():
    try:
        conn = sqlite3.connect(DB_FILE, timeout=10)
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS trades (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT, exchange TEXT, symbol TEXT, entry_price REAL, take_profit REAL, stop_loss REAL, quantity REAL, entry_value_usdt REAL, status TEXT, exit_price REAL, closed_at TEXT, exit_value_usdt REAL, pnl_usdt REAL, trailing_sl_active BOOLEAN, highest_price REAL, reason TEXT)
        ''')
        conn.commit()
        conn.close()
        logging.info(f"Database initialized successfully at: {DB_FILE}")
    except Exception as e:
        logging.error(f"Failed to initialize database at {DB_FILE}: {e}")

def log_recommendation_to_db(signal):
    try:
        conn = sqlite3.connect(DB_FILE, timeout=10)
        cursor = conn.cursor()
        cursor.execute('INSERT INTO trades (timestamp, exchange, symbol, entry_price, take_profit, stop_loss, quantity, entry_value_usdt, status, trailing_sl_active, highest_price, reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', (signal['timestamp'].strftime('%Y-%m-%d %H:%M:%S'), signal['exchange'], signal['symbol'], signal['entry_price'], signal['take_profit'], signal['stop_loss'], signal['quantity'], signal['entry_value_usdt'], 'نشطة', False, signal['entry_price'], signal['reason']))
        trade_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return trade_id
    except Exception as e:
        logging.error(f"Failed to log recommendation to DB: {e}")
        return None

# --- وحدات المسح المتقدمة (Scanners) --- #
def find_col(df_columns, prefix):
    try:
        return next(col for col in df_columns if col.startswith(prefix))
    except StopIteration:
        return None

def analyze_momentum_breakout(df, params, rvol):
    df.ta.vwap(append=True)
    df.ta.bbands(length=params['bbands_period'], std=params['bbands_stddev'], append=True)
    df.ta.macd(fast=params['macd_fast'], slow=params['macd_slow'], signal=params['macd_signal'], append=True)
    df.ta.rsi(length=params['rsi_period'], append=True)

    macd_col = find_col(df.columns, f"MACD_{params['macd_fast']}_{params['macd_slow']}_{params['macd_signal']}")
    macds_col = find_col(df.columns, f"MACDs_{params['macd_fast']}_{params['macd_slow']}_{params['macd_signal']}")
    bbu_col = find_col(df.columns, f"BBU_{params['bbands_period']}_")
    rsi_col = find_col(df.columns, f"RSI_{params['rsi_period']}")

    if not all([macd_col, macds_col, bbu_col, rsi_col]): return None

    last, prev = df.iloc[-2], df.iloc[-3]

    volume_ok = last['volume'] > (df['volume'].rolling(20).mean().iloc[-2] * params['volume_spike_multiplier'])
    rvol_ok = rvol >= bot_data['settings']['liquidity_filters']['min_rvol']

    if (prev[macd_col] <= prev[macds_col] and last[macd_col] > last[macds_col] and
        last['close'] > last[bbu_col] and last['close'] > last["VWAP_D"] and
        last[rsi_col] < params['rsi_max_level'] and volume_ok and rvol_ok):
            return {"reason": "Momentum Breakout", "type": "long"}
    return None

def analyze_breakout_squeeze_pro(df, params, rvol):
    df.ta.bbands(length=params['bbands_period'], std=params['bbands_stddev'], append=True)
    df.ta.kc(length=params['keltner_period'], scalar=params['keltner_atr_multiplier'], append=True)
    df.ta.obv(append=True)

    bbu_col = find_col(df.columns, f"BBU_{params['bbands_period']}_")
    bbl_col = find_col(df.columns, f"BBL_{params['bbands_period']}_")
    kcu_col = find_col(df.columns, f"KCUe_{params['keltner_period']}_")
    kcl_col = find_col(df.columns, f"KCLEe_{params['keltner_period']}_")

    if not all([bbu_col, bbl_col, kcu_col, kcl_col]): return None

    last, prev = df.iloc[-2], df.iloc[-3]
    is_in_squeeze = prev[bbl_col] > prev[kcl_col] and prev[bbu_col] < prev[kcu_col]

    if is_in_squeeze:
        breakout_fired = last['close'] > last[bbu_col]
        volume_ok = not params['volume_confirmation_enabled'] or last['volume'] > df['volume'].rolling(20).mean().iloc[-2] * 1.5
        rvol_ok = rvol >= bot_data['settings']['liquidity_filters']['min_rvol']
        obv_rising = df['OBV'].iloc[-2] > df['OBV'].iloc[-3]

        if breakout_fired and volume_ok and rvol_ok and obv_rising:
            return {"reason": "Squeeze Breakout", "type": "long"}
    return None

def find_divergence_points(series, lookback):
    if not SCIPY_AVAILABLE: return [], []
    peaks, _ = find_peaks(series, distance=lookback)
    troughs, _ = find_peaks(-series, distance=lookback)
    return peaks, troughs

def analyze_rsi_divergence(df, params):
    if not SCIPY_AVAILABLE: return None
    df.ta.rsi(length=params['rsi_period'], append=True)

    rsi_col = find_col(df.columns, f"RSI_{params['rsi_period']}")
    if not rsi_col or df[rsi_col].isnull().all(): return None

    subset = df.iloc[-params['lookback_period']:].copy()
    price_troughs_idx, _ = find_divergence_points(-subset['low'], params['peak_trough_lookback'])
    rsi_troughs_idx, _ = find_divergence_points(-subset[rsi_col], params['peak_trough_lookback'])

    if len(price_troughs_idx) >= 2 and len(rsi_troughs_idx) >= 2:
        p_low1_idx, p_low2_idx = price_troughs_idx[-2], price_troughs_idx[-1]
        r_low1_idx, r_low2_idx = rsi_troughs_idx[-2], rsi_troughs_idx[-1]

        is_divergence = (subset.iloc[p_low2_idx]['low'] < subset.iloc[p_low1_idx]['low'] and 
                            subset.iloc[r_low2_idx][rsi_col] > subset.iloc[r_low1_idx][rsi_col])

        if is_divergence:
            rsi_exits_oversold = (subset.iloc[r_low1_idx][rsi_col] < 35 and 
                                    subset.iloc[-2][rsi_col] > 40)
            confirmation_price = subset.iloc[p_low2_idx:]['high'].max()
            price_confirmed = df.iloc[-2]['close'] > confirmation_price

            if (not params['confirm_with_rsi_exit'] or rsi_exits_oversold) and price_confirmed:
                return {"reason": "RSI Divergence", "type": "long"}
    return None

def analyze_supertrend_pullback(df, params, rvol, adx_value):
    df.ta.supertrend(length=params['atr_period'], multiplier=params['atr_multiplier'], append=True)
    st_dir_col = find_col(df.columns, f"SUPERTd_{params['atr_period']}_")
    ema_col = find_col(df.columns, 'EMA_') # Assumes EMA is calculated in worker
    if not st_dir_col or not ema_col: return None

    last, prev = df.iloc[-2], df.iloc[-3]
    st_flipped_bullish = prev[st_dir_col] == -1 and last[st_dir_col] == 1

    if st_flipped_bullish:
        settings = bot_data['settings']
        ema_ok = last['close'] > last[ema_col]
        adx_ok = adx_value >= settings['master_adx_filter_level']
        rvol_ok = rvol >= settings['liquidity_filters']['min_rvol']

        lookback_period = params.get('swing_high_lookback', 10)
        recent_swing_high = df['high'].iloc[-lookback_period:-2].max()
        breakout_ok = last['close'] > recent_swing_high

        if ema_ok and adx_ok and rvol_ok and breakout_ok:
            return {"reason": "Supertrend Flip", "type": "long"}
    return None

SCANNERS = {
    "momentum_breakout": analyze_momentum_breakout,
    "breakout_squeeze_pro": analyze_breakout_squeeze_pro,
    "rsi_divergence": analyze_rsi_divergence,
    "supertrend_pullback": analyze_supertrend_pullback,
}

# --- الدوال الأساسية للبوت --- #
async def initialize_exchanges():
    async def connect(ex_id):
        exchange = getattr(ccxt, ex_id)({'enableRateLimit': True, 'options': {'defaultType': 'spot'}})
        try:
            await exchange.load_markets()
            bot_data["exchanges"][ex_id] = exchange
            logging.info(f"Connected to {ex_id} (spot markets only).")
        except Exception as e:
            logging.error(f"Failed for {ex_id}: {e}")
            await exchange.close()
    await asyncio.gather(*[connect(ex_id) for ex_id in EXCHANGES_TO_SCAN])

async def aggregate_top_movers():
    all_tickers = []
    async def fetch(ex_id, ex):
        try: return [dict(t, exchange=ex_id) for t in (await ex.fetch_tickers()).values()]
        except Exception: return []
    results = await asyncio.gather(*[fetch(ex_id, ex) for ex_id, ex in bot_data["exchanges"].items()])
    for res in results: all_tickers.extend(res)

    settings = bot_data['settings']
    excluded_bases = settings['stablecoin_filter']['exclude_bases']
    min_volume = settings['liquidity_filters']['min_quote_volume_24h_usd']

    usdt_tickers = [
        t for t in all_tickers 
        if t.get('symbol') 
        and t['symbol'].upper().endswith('/USDT') 
        and t['symbol'].split('/')[0] not in excluded_bases
        and t.get('quoteVolume', 0) >= min_volume
        and not any(k in t['symbol'].upper() for k in ['UP','DOWN','3L','3S','BEAR','BULL'])
    ]

    sorted_tickers = sorted(usdt_tickers, key=lambda t: t.get('quoteVolume', 0), reverse=True)
    unique_symbols = {t['symbol']: {'exchange': t['exchange'], 'symbol': t['symbol']} for t in sorted_tickers}
    final_list = list(unique_symbols.values())[:settings['top_n_symbols_by_volume']]

    logging.info(f"Aggregated markets. Found {len(all_tickers)} tickers -> Post-filter: {len(usdt_tickers)} -> Selected top {len(final_list)} unique pairs.")
    bot_data['status_snapshot']['markets_found'] = len(final_list)
    return final_list

async def get_higher_timeframe_trend(exchange, symbol, ma_period):
    try:
        ohlcv_htf = await exchange.fetch_ohlcv(symbol, HIGHER_TIMEFRAME, limit=ma_period + 5)
        if len(ohlcv_htf) < ma_period: return None, "Not enough HTF data"
        df_htf = pd.DataFrame(ohlcv_htf, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df_htf[f'SMA_{ma_period}'] = ta.sma(df_htf['close'], length=ma_period)
        last_candle = df_htf.iloc[-1]
        is_bullish = last_candle['close'] > last_candle[f'SMA_{ma_period}']
        return is_bullish, "Bullish" if is_bullish else "Bearish"
    except Exception as e:
        return None, f"Error: {e}"

async def worker(queue, results_list, settings, failure_counter):
    while not queue.empty():
        market_info = await queue.get()
        symbol = market_info.get('symbol', 'N/A')
        exchange = bot_data["exchanges"].get(market_info['exchange'])
        if not exchange or not settings.get('active_scanners'): 
            queue.task_done()
            continue

        try:
            await asyncio.sleep(0.2)
            logging.info(f"--- Checking {symbol} on {exchange.id} ---")

            liq_filters = settings['liquidity_filters']
            vol_filters = settings['volatility_filters']
            ema_filters = settings['ema_trend_filter']

            orderbook = await exchange.fetch_order_book(symbol, limit=20)
            if not orderbook or not orderbook['bids'] or not orderbook['asks']:
                logging.info(f"Reject {symbol}: Could not fetch order book.")
                continue
            best_bid, best_ask = orderbook['bids'][0][0], orderbook['asks'][0][0]
            if best_bid <= 0 or best_ask <= 0:
                logging.info(f"Reject {symbol}: Invalid bid/ask price.")
                continue
            spread_percent = ((best_ask - best_bid) / ((best_ask + best_bid) / 2)) * 100
            if spread_percent > liq_filters['max_spread_percent']:
                logging.info(f"Reject {symbol}: High Spread ({spread_percent:.2f}% > {liq_filters['max_spread_percent']}%)")
                continue

            ohlcv = await exchange.fetch_ohlcv(symbol, TIMEFRAME, limit=200)
            if len(ohlcv) < max(liq_filters['rvol_period'] + 5, ema_filters['ema_period']): 
                logging.info(f"Skipping {symbol}: Not enough data ({len(ohlcv)} candles) for indicators.")
                continue

            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            df.set_index('timestamp', inplace=True)

            df['volume_sma'] = ta.sma(df['volume'], length=liq_filters['rvol_period'])
            rvol = df['volume'].iloc[-2] / df['volume_sma'].iloc[-2] if df['volume_sma'].iloc[-2] > 0 else 0
            if rvol < liq_filters['min_rvol']:
                logging.info(f"Reject {symbol}: Low RVOL ({rvol:.2f} < {liq_filters['min_rvol']})")
                continue

            atr_col_name = f"ATRr_{vol_filters['atr_period_for_filter']}"
            df.ta.atr(length=vol_filters['atr_period_for_filter'], append=True)
            last_close = df['close'].iloc[-2]
            last_atr = df[atr_col_name].iloc[-2]
            atr_percent = (last_atr / last_close) * 100 if last_close > 0 else 0
            if atr_percent < vol_filters['min_atr_percent']:
                logging.info(f"Reject {symbol}: Low ATR% ({atr_percent:.2f}% < {vol_filters['min_atr_percent']}%)")
                continue

            if ema_filters['enabled']:
                ema_col_name = f"EMA_{ema_filters['ema_period']}"
                df.ta.ema(length=ema_filters['ema_period'], append=True)
                if last_close < df[ema_col_name].iloc[-2]:
                    logging.info(f"Reject {symbol}: Below EMA{ema_filters['ema_period']} ({last_close:.4f} < {df[ema_col_name].iloc[-2]:.4f})")
                    continue

            if settings.get('use_master_trend_filter'):
                is_htf_bullish, reason = await get_higher_timeframe_trend(exchange, symbol, settings['master_trend_filter_ma_period'])
                if is_htf_bullish == False:
                    logging.info(f"HTF Trend Filter: FAILED ({reason}) for {symbol}")
                    continue
                logging.info(f"HTF Trend Filter: PASSED ({reason}) for {symbol}")

            df.ta.adx(append=True)
            adx_col = find_col(df.columns, 'ADX_')
            adx_value = df[adx_col].iloc[-2] if adx_col and adx_col in df.columns else float('nan')
            if settings.get('use_master_trend_filter'):
                if pd.isna(adx_value) or adx_value < settings['master_adx_filter_level']:
                    reason = "is NaN" if pd.isna(adx_value) else f"is {adx_value:.2f}, below {settings['master_adx_filter_level']}"
                    logging.info(f"ADX Filter: FAILED (ADX {reason}) for {symbol}")
                    continue
                logging.info(f"ADX Filter: PASSED (ADX is {adx_value:.2f}) for {symbol}")

            confirmed_reasons = []
            analysis_df = df.copy() 

            for scanner_name in settings['active_scanners']:
                scanner_func = SCANNERS.get(scanner_name)
                scanner_args = {'df': analysis_df.copy(), 'params': settings.get(scanner_name, {})}
                if scanner_name in ["momentum_breakout", "breakout_squeeze_pro", "supertrend_pullback"]:
                    scanner_args['rvol'] = rvol
                if scanner_name == "supertrend_pullback":
                    scanner_args['adx_value'] = adx_value

                analysis_result = scanner_func(**scanner_args)
                if analysis_result and analysis_result.get("type") == "long":
                    confirmed_reasons.append(analysis_result['reason'])

            if confirmed_reasons:
                strength = len(confirmed_reasons)
                min_strength = settings.get("min_signal_strength", 1)

                if strength < min_strength:
                    logging.info(f"Skipping {symbol} signal with strength {strength} (below minimum {min_strength})")
                else:
                    reason_str = ' + '.join(confirmed_reasons)
                    logging.info(f"SIGNAL FOUND for {symbol} with strength {strength} via {reason_str}")

                    entry_price = df.iloc[-2]['close']
                    current_atr_col = find_col(df.columns, f"ATRr_{settings['atr_period']}")
                    if not current_atr_col: df.ta.atr(length=settings['atr_period'], append=True)
                    current_atr = df.iloc[-2].get(find_col(df.columns, f"ATRr_{settings['atr_period']}"), 0)

                    if settings.get("use_dynamic_risk_management", False) and current_atr > 0 and not pd.isna(current_atr):
                        risk_per_unit = current_atr * settings['atr_sl_multiplier']
                        stop_loss, take_profit = entry_price - risk_per_unit, entry_price + (risk_per_unit * settings['risk_reward_ratio'])
                    else:
                        stop_loss, take_profit = entry_price * (1 - settings['stop_loss_percentage'] / 100), entry_price * (1 + settings['take_profit_percentage'] / 100)

                    min_filters = settings['min_tp_sl_filter']
                    tp_percent = ((take_profit - entry_price) / entry_price * 100)
                    sl_percent = ((entry_price - stop_loss) / entry_price * 100)

                    if tp_percent < min_filters['min_tp_percent'] or sl_percent < min_filters['min_sl_percent']:
                        logging.info(f"Reject {symbol} Signal: Small TP/SL (TP: {tp_percent:.2f}%, SL: {sl_percent:.2f}%)")
                    else:
                        signal = {
                            "symbol": symbol, "exchange": market_info['exchange'].capitalize(), 
                            "entry_price": entry_price, "take_profit": take_profit, 
                            "stop_loss": stop_loss, "timestamp": df.index[-2], 
                            "reason": reason_str, "strength": strength
                        }
                        results_list.append(signal)
        except Exception as e: 
            logging.error(f"CRITICAL ERROR in worker for {symbol}: {e}", exc_info=False)
            failure_counter[0] += 1
        finally:
            queue.task_done()

async def perform_scan(context: ContextTypes.DEFAULT_TYPE):
    async with scan_lock:
        if bot_data['status_snapshot']['scan_in_progress']:
            logging.warning("Scan attempted while another was in progress. Skipped.")
            return

        status = bot_data['status_snapshot']
        status.update({"scan_in_progress": True, "last_scan_start_time": datetime.now(EGYPT_TZ).strftime('%Y-%m-%d %H:%M:%S'), "signals_found": 0})

        settings = bot_data["settings"]
        try:
            conn = sqlite3.connect(DB_FILE, timeout=10); cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM trades WHERE status = 'نشطة'")
            active_trades_count = cursor.fetchone()[0]; conn.close()
        except Exception as e:
            logging.error(f"DB Error in perform_scan: {e}"); active_trades_count = settings.get("max_concurrent_trades", 5)

        if settings.get('market_regime_filter_enabled', True):
            is_market_ok, reason = await check_market_regime()
            if not is_market_ok:
                logging.info(f"Skipping scan: {reason}")
                status['scan_in_progress'] = False
                return

        top_markets = await aggregate_top_movers()
        if not top_markets:
            logging.info("Scan complete: No markets to scan."); status['scan_in_progress'] = False; return

        queue = asyncio.Queue(); [await queue.put(market) for market in top_markets]
        signals, failure_counter = [], [0]
        worker_tasks = [asyncio.create_task(worker(queue, signals, settings, failure_counter)) for _ in range(settings['concurrent_workers'])]
        await queue.join(); [task.cancel() for task in worker_tasks]

        signals.sort(key=lambda s: s.get('strength', 0), reverse=True)

        total_signals = len(signals)
        new_trades, opportunities = 0, 0
        last_signal_time = bot_data['last_signal_time']
        for signal in signals:
            if signal['symbol'] in last_signal_time and (time.time() - last_signal_time.get(signal['symbol'], 0)) <= (SCAN_INTERVAL_SECONDS * 4): 
                logging.info(f"Signal for {signal['symbol']} skipped due to recent notification cooldown.")
                continue

            trade_amount_usdt = settings["virtual_portfolio_balance_usdt"] * (settings["virtual_trade_size_percentage"] / 100)
            signal.update({'quantity': trade_amount_usdt / signal['entry_price'], 'entry_value_usdt': trade_amount_usdt})

            if active_trades_count < settings.get("max_concurrent_trades", 5):
                trade_id = log_recommendation_to_db(signal)
                if trade_id:
                    signal['trade_id'] = trade_id
                    await send_telegram_message(context.bot, signal, is_new=True)
                    active_trades_count += 1; new_trades += 1
            else:
                await send_telegram_message(context.bot, signal, is_opportunity=True)
                opportunities += 1
            await asyncio.sleep(0.5)
            last_signal_time[signal['symbol']] = time.time()

        failures = failure_counter[0]
        logging.info(f"Scan complete. Found: {total_signals}, Entered: {new_trades}, Opportunities: {opportunities}, Failures: {failures}.")

        summary_message = (f"🔹 *ملخص الفحص* 🔹\n\n"
                            f"▫️ إجمالي الإشارات: *{total_signals}*\n"
                            f"✅ صفقات جديدة: *{new_trades}*\n"
                            f"💡 فرص إضافية: *{opportunities}*\n"
                            f"⚠️ عملات فشل تحليلها: *{failures}*")
        await send_telegram_message(context.bot, {'custom_message': summary_message, 'target_chat': TELEGRAM_CHAT_ID})

        status['signals_found'] = new_trades + opportunities
        status['last_scan_end_time'] = datetime.now(EGYPT_TZ).strftime('%Y-%m-%d %H:%M:%S'); status['scan_in_progress'] = False

async def send_telegram_message(bot, signal_data, is_new=False, is_opportunity=False, status_update=None, update_type=None):
    message, keyboard, target_chat = "", None, TELEGRAM_CHAT_ID
    def format_price(price): return f"{price:,.8f}" if price < 0.01 else f"{price:,.4f}"

    if 'custom_message' in signal_data:
        message, target_chat = signal_data['custom_message'], signal_data['target_chat']
    elif is_new or is_opportunity:
        target_chat = TELEGRAM_SIGNAL_CHANNEL_ID
        strength = signal_data.get('strength', 1)
        strength_stars = '⭐' * strength
        title = f"✅ توصية صفقة جديدة ({strength_stars}) ✅" if is_new else f"💡 فرصة تداول محتملة ({strength_stars}) 💡"
        reason_label = "الاستراتيجيات" if strength > 1 else "الاستراتيجية"
        entry, tp, sl = signal_data['entry_price'], signal_data['take_profit'], signal_data['stop_loss']
        tp_percent, sl_percent = ((tp - entry) / entry * 100), ((sl - entry) / entry * 100)
        value_line = f"▫️ قيمة الصفقة: *${signal_data['entry_value_usdt']:,.2f}*" if is_new else ""
        id_line = f"\n*للمتابعة، استخدم الأمر `/check {signal_data['trade_id']}`*" if is_new else ""
        message = (f"{title}\n\n"
                   f"▫️ العملة: `{signal_data['symbol']}`\n▫️ المنصة: *{signal_data['exchange']}*\n"
                   f"▫️ {reason_label}: `{signal_data['reason']}`\n{value_line}\n"
                   f"━━━━━━━━━━━━━━\n"
                   f"📈 سعر الدخول: *{format_price(entry)} $*\n"
                   f"━━━━━━━━━━━━━━\n"
                   f"🎯 الهدف: *{format_price(tp)} $* `({tp_percent:+.2f}%)`\n"
                   f"🛑 الوقف: *{format_price(sl)} $* `({sl_percent:+.2f}%)`{id_line}")
    elif status_update in ['ناجحة', 'فاشلة']:
        target_chat = TELEGRAM_SIGNAL_CHANNEL_ID
        pnl_percent = (signal_data['pnl_usdt'] / signal_data['entry_value_usdt'] * 100) if signal_data.get('entry_value_usdt', 0) > 0 else 0
        icon, title, pnl_label = ("🎯", "هدف محقق!", "الربح") if status_update == 'ناجحة' else ("🛑", "تم تفعيل وقف الخسارة", "الخسارة")
        message = f"{icon} *{title}* {icon}\n\n*العملة:* `{signal_data['symbol']}` | *المنصة:* `{signal_data['exchange']}`\n*{pnl_label}:* `~${abs(signal_data.get('pnl_usdt', 0)):.2f} ({pnl_percent:+.2f}%)`"
    elif update_type == 'tsl_activation':
        message = f"🔒 *تأمين صفقة* 🔒\n\n*العملة:* `{signal_data['symbol']}`\nتم نقل وقف الخسارة إلى نقطة الدخول: `${format_price(signal_data['new_sl'])}`."

    if not message: return
    for attempt in range(3):
        try:
            await bot.send_message(chat_id=target_chat, text=message, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard); return
        except RetryAfter as e: await asyncio.sleep(e.retry_after)
        except TimedOut: await asyncio.sleep(5)
        except Exception as e: logging.error(f"Failed to send msg to {target_chat}: {e}"); await asyncio.sleep(2)
    logging.error(f"Failed to send msg to {target_chat} after 3 attempts.")

async def track_open_trades(context: ContextTypes.DEFAULT_TYPE):
    try:
        conn = sqlite3.connect(DB_FILE, timeout=10); conn.row_factory = sqlite3.Row; cursor = conn.cursor()
        cursor.execute("SELECT * FROM trades WHERE status = 'نشطة'")
        active_trades = [dict(row) for row in cursor.fetchall()]; conn.close()
    except Exception as e: logging.error(f"DB error in track_open_trades: {e}"); return
    bot_data['status_snapshot']['active_trades_count'] = len(active_trades)
    if not active_trades: return

    async def check_trade(trade):
        exchange = bot_data["exchanges"].get(trade['exchange'].lower())
        if not exchange: return None
        try:
            ticker = await exchange.fetch_ticker(trade['symbol']); current_price = ticker.get('last') or ticker.get('close')
            if not current_price: return None
            if current_price >= trade['take_profit']: return {'id': trade['id'], 'status': 'ناجحة', 'exit_price': current_price}
            if current_price <= trade['stop_loss']: return {'id': trade['id'], 'status': 'فاشلة', 'exit_price': current_price}
            settings = bot_data["settings"]
            if settings.get('trailing_sl_enabled', False):
                highest_price = max(trade.get('highest_price', current_price), current_price)
                if not trade.get('trailing_sl_active') and current_price >= trade['entry_price'] * (1 + settings['trailing_sl_activate_percent'] / 100):
                    new_sl = trade['entry_price']
                    if new_sl > trade['stop_loss']: return {'id': trade['id'], 'status': 'update_tsl', 'new_sl': new_sl, 'highest_price': highest_price}
                elif trade.get('trailing_sl_active'):
                    new_sl = highest_price * (1 - settings['trailing_sl_percent'] / 100)
                    if new_sl > trade['stop_loss']: return {'id': trade['id'], 'status': 'update_sl', 'new_sl': new_sl, 'highest_price': highest_price}
                    elif highest_price > trade.get('highest_price', 0): return {'id': trade['id'], 'status': 'update_peak', 'highest_price': highest_price}
        except Exception: pass
        return None

    results = await asyncio.gather(*[check_trade(trade) for trade in active_trades])
    updates_to_db, portfolio_pnl = [], 0.0
    for result in filter(None, results):
        original_trade = next((t for t in active_trades if t['id'] == result['id']), None)
        if not original_trade: continue
        status = result['status']
        if status in ['ناجحة', 'فاشلة']:
            # --- START: Replacement Code ---
            pnl_usdt = (result['exit_price'] - original_trade['entry_price']) * original_trade['quantity']
            pnl_percent = (pnl_usdt / original_trade['entry_value_usdt'] * 100) if original_trade.get('entry_value_usdt', 0) > 0 else 0
            portfolio_pnl += pnl_usdt
            closed_at_str = datetime.now(EGYPT_TZ).strftime('%Y-%m-%d %H:%M:%S')

            updates_to_db.append((
                "UPDATE trades SET status=?, exit_price=?, closed_at=?, exit_value_usdt=?, pnl_usdt=? WHERE id=?",
                (status, result['exit_price'], closed_at_str, result['exit_price'] * original_trade['quantity'], pnl_usdt, result['id'])
            ))
            # Calculate duration
            duration_str = "N/A"
            try:
                entry_time = datetime.strptime(original_trade['timestamp'], '%Y-%m-%d %H:%M:%S')
                close_time = datetime.now()
                duration = close_time - entry_time
                days, remainder = divmod(duration.total_seconds(), 86400)
                hours, remainder = divmod(remainder, 3600)
                minutes, _ = divmod(remainder, 60)
                parts = []
                if int(days) > 0: parts.append(f"{int(days)} يوم")
                if int(hours) > 0: parts.append(f"{int(hours)} ساعة")
                if int(minutes) > 0 or not parts: parts.append(f"{int(minutes)} دقيقة")
                duration_str = "، ".join(parts)
            except Exception as e:
                logging.warning(f"Could not calculate duration for trade #{original_trade['id']}: {e}")
            # Build the detailed message
            icon = "✅" if pnl_usdt >= 0 else "❌"
            reason_str = "Take Profit Hit" if status == 'ناجحة' else "Stop Loss Hit"
            reason_icon = "🎯" if status == 'ناجحة' else "🛑"
            def format_price(price): return f"{price:,.8f}" if price < 0.01 else f"{price:,.4f}"

            message = (
                f"{icon} **تم إغلاق الصفقة #{original_trade['id']}** {icon}\n\n"
                f"{reason_icon} **السبب:** *{reason_str}*\n"
                f"━━━━━━━━━━━━━━\n"
                f"▫️ **العملة:** `{original_trade['symbol']}`\n"
                f"▫️ **الاستراتيجية:** `{original_trade.get('reason', 'N/A')}`\n"
                f"▫️ **مدة الصفقة:** `{duration_str}`\n"
                f"━━━━━━━━━━━━━━\n"
                f"📈 **سعر الدخول:** `{format_price(original_trade['entry_price'])}`\n"
                f"📉 **سعر الخروج:** `{format_price(result['exit_price'])}`\n"
                f"💰 **الربح/الخسارة:** **`${pnl_usdt:+.2f}`** (`{pnl_percent:+.2f}%`)"
            )
            # Send the new detailed message
            await send_telegram_message(context.bot, {'custom_message': message, 'target_chat': TELEGRAM_SIGNAL_CHANNEL_ID})
            # --- END: Replacement Code ---
        elif status == 'update_tsl':
            updates_to_db.append(("UPDATE trades SET stop_loss=?, highest_price=?, trailing_sl_active=? WHERE id=?", (result['new_sl'], result['highest_price'], True, result['id'])))
            await send_telegram_message(context.bot, {**original_trade, **result}, update_type='tsl_activation')
        elif status == 'update_sl': updates_to_db.append(("UPDATE trades SET stop_loss=?, highest_price=? WHERE id=?", (result['new_sl'], result['highest_price'], result['id'])))
        elif status == 'update_peak': updates_to_db.append(("UPDATE trades SET highest_price=? WHERE id=?", (result['highest_price'], result['id'])))

    if updates_to_db:
        try:
            conn = sqlite3.connect(DB_FILE, timeout=10); cursor = conn.cursor()
            [cursor.execute(q, p) for q, p in updates_to_db]; conn.commit(); conn.close()
        except Exception as e: logging.error(f"DB update failed in track_open_trades: {e}")
    if portfolio_pnl != 0.0: bot_data['settings']['virtual_portfolio_balance_usdt'] += portfolio_pnl; save_settings(); logging.info(f"Portfolio balance updated by ${portfolio_pnl:.2f}.")

# [NEW] Enhanced Market Regime Filter with Sentiment Analysis
def get_fear_and_greed_index():
    """Fetches the latest Fear and Greed Index data."""
    try:
        response = requests.get("https://api.alternative.me/fng/?limit=1", timeout=10)
        response.raise_for_status()
        data = response.json().get('data', [])
        if data:
            return int(data[0]['value'])
    except Exception as e:
        logging.error(f"Could not fetch Fear and Greed Index: {e}")
    return None

async def check_market_regime():
    """Checks both technical trend and market sentiment."""
    settings = bot_data['settings']
    is_technically_bullish, is_sentiment_bullish = True, True # Default to True (fail-safe)

    # 1. Technical Check (BTC Price vs. SMA)
    try:
        binance = bot_data["exchanges"].get('binance')
        if not binance:
            logging.warning("Binance exchange not available for market regime check. Skipping technical filter.")
        else:
            ohlcv = await binance.fetch_ohlcv('BTC/USDT', '4h', limit=55)
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['sma50'] = ta.sma(df['close'], length=50)
            is_technically_bullish = df['close'].iloc[-1] > df['sma50'].iloc[-1]
    except Exception as e:
        logging.error(f"Error checking BTC trend: {e}")

    # 2. Sentiment Check (Fear & Greed Index)
    if settings.get("fear_and_greed_filter_enabled", True):
        fng_index = get_fear_and_greed_index()
        if fng_index is not None:
            is_sentiment_bullish = fng_index >= settings.get("fear_and_greed_threshold", 30)
        # If fng_index is None, is_sentiment_bullish remains True (fail-safe)

    # 3. Final Decision
    if not is_technically_bullish:
        return False, "Bearish market regime detected (BTC below 4h SMA50)."
    if not is_sentiment_bullish:
        return False, f"Extreme fear detected (F&G Index: {fng_index} is below threshold {settings.get('fear_and_greed_threshold')})."

    return True, "Market regime is favorable for longs."


def generate_performance_report_string():
    REPORT_DAYS = 30
    if not os.path.exists(DB_FILE):
        return "❌ خطأ: لم يتم العثور على ملف قاعدة البيانات."

    try:
        conn = sqlite3.connect(DB_FILE, timeout=10); conn.row_factory = sqlite3.Row; cursor = conn.cursor()
        start_date = datetime.now() - timedelta(days=REPORT_DAYS)
        start_date_str = start_date.strftime('%Y-%m-%d %H:%M:%S')
        query = "SELECT reason, status, entry_price, highest_price FROM trades WHERE status IN ('ناجحة', 'فاشلة') AND timestamp >= ?"
        cursor.execute(query, (start_date_str,)); trades = cursor.fetchall(); conn.close()
    except Exception as e:
        return f"❌ حدث خطأ غير متوقع: {e}"

    if not trades: return f"ℹ️ لا توجد صفقات مغلقة في آخر {REPORT_DAYS} يومًا."

    strategy_stats = {}
    for trade in trades:
        reason = trade['reason']
        if reason not in strategy_stats: strategy_stats[reason] = {'total': 0, 'successful': 0, 'max_profits': []}
        stats = strategy_stats[reason]; stats['total'] += 1
        if trade['status'] == 'ناجحة': stats['successful'] += 1
        if trade['entry_price'] > 0 and trade['highest_price'] is not None:
            max_profit_percent = ((trade['highest_price'] - trade['entry_price']) / trade['entry_price']) * 100
            stats['max_profits'].append(max_profit_percent)

    report_lines = [f"📊 **تقرير أداء الاستراتيجيات (آخر {REPORT_DAYS} يومًا)** 📊", "="*35]
    sorted_strategies = sorted(strategy_stats.items(), key=lambda item: item[1]['total'], reverse=True)
    for reason, stats in sorted_strategies:
        total_trades = stats['total'];
        if total_trades == 0: continue
        success_rate = (stats['successful'] / total_trades) * 100
        avg_max_profit = sum(stats['max_profits']) / len(stats['max_profits']) if stats['max_profits'] else 0
        report_lines.extend([f"--- **{reason}** ---", f"- **إجمالي التوصيات:** {total_trades}", f"- **نسبة النجاح:** {success_rate:.1f}%", f"- **متوسط أقصى ربح:** {avg_max_profit:.2f}%", ""])
    return "\n".join(report_lines)

# --- أوامر ولوحات مفاتيح تليجرام --- #
main_menu_keyboard = [["📊 الإحصائيات", "📈 الصفقات النشطة"], ["📜 تقرير الاستراتيجيات", "⚙️ الإعدادات"], ["👀 ماذا يجري في الخلفية؟", "🔬 فحص يدوي الآن"],["ℹ️ مساعدة"]]
settings_menu_keyboard = [["🎭 تفعيل/تعطيل الماسحات", "🏁 أنماط جاهزة"], ["🔧 تعديل المعايير", "🔙 القائمة الرئيسية"]]
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE): await update.message.reply_text("أهلاً بك في محاكي التداول المتقدم! (v25 - Sentiment-Aware)", reply_markup=ReplyKeyboardMarkup(main_menu_keyboard, resize_keyboard=True))
async def scan_now_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if bot_data['status_snapshot'].get('scan_in_progress', False): await update.message.reply_text("⚠️ فحص آخر قيد التنفيذ."); return
    await update.message.reply_text("⏳ جاري بدء الفحص اليدوي..."); context.job_queue.run_once(perform_scan, 0, name='manual_scan')

async def show_settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target_message = update.message or update.callback_query.message
    await target_message.reply_text("اختر الإعداد:", reply_markup=ReplyKeyboardMarkup(settings_menu_keyboard, resize_keyboard=True))

def get_scanners_keyboard():
    active_scanners = bot_data["settings"].get("active_scanners", [])
    keyboard = [[InlineKeyboardButton(f"{'✅' if name in active_scanners else '❌'} {name}", callback_data=f"toggle_{name}")] for name in SCANNERS.keys()]
    keyboard.append([InlineKeyboardButton("🔙 العودة للإعدادات", callback_data="back_to_settings")])
    return InlineKeyboardMarkup(keyboard)

def get_presets_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚦 احترافية (متوازنة)", callback_data="preset_PRO"), InlineKeyboardButton("🎯 متشددة", callback_data="preset_STRICT")],
        [InlineKeyboardButton("🌙 متساهلة", callback_data="preset_LAX"), InlineKeyboardButton("⚠️ فائق التساهل", callback_data="preset_VERY_LAX")],
        [InlineKeyboardButton("🔙 العودة للإعدادات", callback_data="back_to_settings")]
    ])

async def show_presets_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target_message = update.message or update.callback_query.message
    await target_message.reply_text("اختر نمط إعدادات جاهز:", reply_markup=get_presets_keyboard())

async def show_scanners_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target_message = update.message or update.callback_query.message
    await target_message.reply_text("اختر الماسحات لتفعيلها أو تعطيلها:", reply_markup=get_scanners_keyboard())

async def toggle_scanner_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; scanner_name = "_".join(query.data.split("_")[1:])
    active_scanners = bot_data["settings"].get("active_scanners", []).copy()
    if scanner_name in active_scanners: active_scanners.remove(scanner_name)
    else: active_scanners.append(scanner_name)
    bot_data["settings"]["active_scanners"] = active_scanners; save_settings()
    try: await query.edit_message_text(text="اختر الماسحات لتفعيلها أو تعطيلها:", reply_markup=get_scanners_keyboard())
    except BadRequest as e:
        if "Message is not modified" not in str(e): raise

# --- [EDIT] Revamped the interactive settings menu to be more organized and complete ---
async def show_parameters_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Displays a categorized inline keyboard for modifying parameters."""
    keyboard = []
    settings = bot_data["settings"]

    for category, params in EDITABLE_PARAMS.items():
        # Add a non-clickable category header button
        keyboard.append([InlineKeyboardButton(f"--- {category} ---", callback_data="ignore")])
        row = []
        for param_key in params:
            display_name = PARAM_DISPLAY_NAMES.get(param_key, param_key)
            current_value = settings.get(param_key, "N/A")

            # Special display for boolean values
            if isinstance(current_value, bool):
                display_value = "مُفعّل ✅" if current_value else "مُعطّل ❌"
                text = f"{display_name}: {display_value}"
            else:
                text = f"{display_name}: {current_value}"

            row.append(InlineKeyboardButton(text, callback_data=f"param_{param_key}"))
            if len(row) == 2:
                keyboard.append(row)
                row = []
        if row: # Append any remaining button in the row
            keyboard.append(row)

    keyboard.append([InlineKeyboardButton("🔙 العودة للإعدادات", callback_data="back_to_settings")])

    target_message = update.message or update.callback_query.message
    message_text = "⚙️ *الإعدادات المتقدمة* ⚙️\n\nاختر الإعداد الذي تريد تعديله بالضغط عليه:"

    # --- [IMPROVEMENT] Store message ID for seamless editing ---
    # If the message is from a callback, edit it. Otherwise, send a new one.
    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(
                message_text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=ParseMode.MARKDOWN
            )
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                logging.error(f"Error editing parameters menu: {e}")
    else:
        sent_message = await target_message.reply_text(
            message_text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN
        )
        context.user_data['settings_menu_id'] = sent_message.message_id


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("*مساعدة البوت*\n`/start` - بدء\n`/scan` - فحص يدوي\n`/report` - تقرير يومي\n`/strategyreport` - تقرير أداء الاستراتيجيات\n`/check <ID>` - متابعة صفقة\n`/debug` - فحص الحالة", parse_mode=ParseMode.MARKDOWN)
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        conn = sqlite3.connect(DB_FILE, timeout=10); cursor = conn.cursor(); cursor.execute("SELECT status, COUNT(*), SUM(pnl_usdt) FROM trades GROUP BY status")
        stats_data = cursor.fetchall(); conn.close()
        counts = {s: c for s, c, p in stats_data}; pnl = {s: (p or 0) for s, c, p in stats_data}
        total, active, successful, failed = sum(counts.values()), counts.get('نشطة', 0), counts.get('ناجحة', 0), counts.get('فاشلة', 0)
        closed = successful + failed; win_rate = (successful / closed * 100) if closed > 0 else 0; total_pnl = sum(pnl.values())
        preset_name = bot_data["settings"].get("active_preset_name", "N/A")
        stats_msg = (f"*📊 إحصائيات المحفظة*\n\n"
                       f"📈 *الرصيد الحالي:* `${bot_data['settings']['virtual_portfolio_balance_usdt']:.2f}`\n"
                       f"💰 *إجمالي الربح/الخسارة:* `${total_pnl:+.2f}`\n"
                       f"⚙️ *النمط الحالي:* `{preset_name}`\n\n"
                       f"- *إجمالي الصفقات:* `{total}` (`{active}` نشطة)\n"
                       f"- *الناجحة:* `{successful}` | *الربح:* `${pnl.get('ناجحة', 0):.2f}`\n"
                       f"- *الفاشلة:* `{failed}` | *الخسارة:* `${abs(pnl.get('فاشلة', 0)):.2f}`\n"
                       f"- *معدل النجاح:* `{win_rate:.2f}%`")
        await update.message.reply_text(stats_msg, parse_mode=ParseMode.MARKDOWN)
    except Exception as e: logging.error(f"Error in stats_command: {e}", exc_info=True); await update.message.reply_text("خطأ في جلب الإحصائيات.")

async def strategy_report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ جاري إعداد تقرير أداء الاستراتيجيات..."); report_string = generate_performance_report_string()
    await update.message.reply_text(report_string, parse_mode=ParseMode.MARKDOWN)

async def send_daily_report(context: ContextTypes.DEFAULT_TYPE) -> bool:
    today_str = datetime.now(EGYPT_TZ).strftime('%Y-%m-%d'); logging.info(f"Generating daily report for {today_str}...")
    try:
        conn = sqlite3.connect(DB_FILE, timeout=10); cursor = conn.cursor()
        cursor.execute("SELECT status, pnl_usdt FROM trades WHERE DATE(closed_at) = ?", (today_str,)); closed_today = cursor.fetchall(); conn.close()
        if not closed_today: report_message = f"🗓️ *التقرير اليومي ليوم {today_str}*\n\nلم يتم إغلاق أي صفقات اليوم."
        else:
            wins, losses, total_pnl = 0, 0, 0.0
            for status, pnl in closed_today:
                if status == 'ناجحة': wins += 1
                else: losses += 1
                if pnl is not None: total_pnl += pnl
            total, win_rate = wins + losses, (wins / (wins + losses) * 100) if (wins + losses) > 0 else 0
            report_message = (f"🗓️ *التقرير اليومي ليوم {today_str}*\n\n"
                              f"▫️ *إجمالي الصفقات المغلقة:* `{total}`\n✅ *الرابحة:* `{wins}` | ❌ *الخاسرة:* `{losses}`\n\n"
                              f"📈 *معدل النجاح:* `{win_rate:.2f}%`\n💰 *الربح/الخسارة:* `${total_pnl:+.2f}`")
        await send_telegram_message(context.bot, {'custom_message': report_message, 'target_chat': TELEGRAM_SIGNAL_CHANNEL_ID}); return True
    except Exception as e: 
        logging.error(f"Failed to generate daily report: {e}", exc_info=True)
        return False

async def daily_report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ جاري إرسال التقرير اليومي...")
    # --- [IMPROVEMENT] Add confirmation message on success ---
    if await send_daily_report(context):
        await update.message.reply_text("✅ تم إرسال التقرير بنجاح إلى القناة.")
    else:
        await update.message.reply_text("❌ فشل إرسال التقرير اليومي.")

async def background_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status = bot_data['status_snapshot']; next_scan_time = "N/A"
    if not status['scan_in_progress'] and context.job_queue:
        next_job = context.job_queue.get_jobs_by_name('perform_scan')
        if next_job and next_job[0].next_t: next_scan_time = next_job[0].next_t.astimezone(EGYPT_TZ).strftime('%H:%M:%S')
    message = (f"🤖 *حالة البوت في الخلفية*\n\n*{'🟢 الفحص قيد التنفيذ...' if status['scan_in_progress'] else '⚪️ البوت في وضع الاستعداد'}*\n\n"
               f"- *آخر فحص:* `{status['last_scan_end_time']}`\n- *العملات المفحوصة:* `{status['markets_found']}`\n"
               f"- *الإشارات الجديدة:* `{status['signals_found']}`\n- *الصفقات النشطة:* `{status['active_trades_count']}`\n- *الفحص التالي:* `{next_scan_time}`")
    await update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN)

async def debug_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ جاري إعداد تقرير التشخيص الشامل...")
    settings = bot_data.get("settings", {}); parts = ["🩺 *تقرير التشخيص والحالة* 🩺"]
    parts.append("\n*--- الإعدادات العامة ---*")
    active_preset = settings.get("active_preset_name", "N/A"); active_scanners = ", ".join(settings.get("active_scanners", ["None"]))
    parts.extend([f"- *النمط النشط:* `{active_preset}`", f"- *الماسحات النشطة:* `{active_scanners}`"])
    parts.append("\n*--- إعدادات المخاطر ---*")
    balance = settings.get('virtual_portfolio_balance_usdt', 0); trade_size = settings.get('virtual_trade_size_percentage', 0)
    sl_multiplier = settings.get('atr_sl_multiplier', 0); rr_ratio = settings.get('risk_reward_ratio', 0)
    parts.extend([f"- *رأس المال الافتراضي:* `${balance:,.2f}`", f"- *حجم الصفقة:* `{trade_size}%` من رأس المال", f"- *مضاعف وقف الخسارة (ATR):* `{sl_multiplier}`", f"- *نسبة المخاطرة/العائد:* `1:{rr_ratio}`"])
    parts.append("\n*--- حالة المهام المجدولة ---*")
    if context.job_queue and context.job_queue.jobs():
        for job in context.job_queue.jobs():
            if job.next_t: parts.append(f"- *المهمة:* `{job.name}` | *التشغيل التالي:* `{job.next_t.astimezone(ZoneInfo('UTC')).strftime('%H:%M:%S')} (UTC)`")
            else: parts.append(f"- *المهمة:* `{job.name}` | *الحالة:* `غير مجدولة`")
    else: parts.append("- `🔴 لا توجد مهام مجدولة!`")
    parts.append("\n*--- حالة الاتصال بالمنصات ---*")
    connected_exchanges = bot_data.get('exchanges', {})
    for ex_id in EXCHANGES_TO_SCAN: parts.append(f"- *{ex_id.capitalize()}:* {'✅ متصل' if ex_id in connected_exchanges else '❌ غير متصل'}")
    parts.append("\n*--- حالة قاعدة البيانات ---*")
    try:
        conn = sqlite3.connect(DB_FILE, timeout=5); cursor = conn.cursor(); cursor.execute("SELECT COUNT(*) FROM trades"); total_trades = cursor.fetchone()[0]; conn.close()
        parts.extend([f"- *الاتصال:* `✅ ناجح`", f"- *إجمالي الصفقات المسجلة:* `{total_trades}`"])
    except Exception as e: parts.extend([f"- *الاتصال:* `❌ فشل!`", f"- *الخطأ:* `{e}`"])
    await update.message.reply_text("\n".join(parts), parse_mode=ParseMode.MARKDOWN)

async def check_trade_command(update: Update, context: ContextTypes.DEFAULT_TYPE, trade_id_from_callback=None):
    target = update.callback_query.message if trade_id_from_callback else update.message
    def format_price(price): return f"{price:,.8f}" if price < 0.01 else f"{price:,.4f}"
    try:
        trade_id = trade_id_from_callback or int(context.args[0])
        conn = sqlite3.connect(DB_FILE, timeout=10); conn.row_factory = sqlite3.Row; cursor = conn.cursor(); cursor.execute("SELECT * FROM trades WHERE id = ?", (trade_id,));
        trade_row = cursor.fetchone(); conn.close()
        if not trade_row: await target.reply_text(f"لم يتم العثور على صفقة بالرقم `{trade_id}`."); return
        trade = dict(trade_row)
        if trade['status'] != 'نشطة':
            pnl_percent = (trade['pnl_usdt'] / trade['entry_value_usdt'] * 100) if trade.get('entry_value_usdt', 0) > 0 else 0
            closed_at_dt = datetime.strptime(trade['closed_at'], '%Y-%m-%d %H:%M:%S')
            message = f"📋 *ملخص الصفقة #{trade_id}*\n\n*العملة:* `{trade['symbol']}`\n*الحالة:* `{trade['status']}`\n*تاريخ الإغلاق:* `{closed_at_dt.strftime('%Y-%m-%d %I:%M %p')}`\n*الربح/الخسارة:* `${trade.get('pnl_usdt', 0):+.2f} ({pnl_percent:+.2f}%)`"
        else:
            exchange = bot_data["exchanges"].get(trade['exchange'].lower())
            if not exchange: await target.reply_text("المنصة غير متصلة."); return
            ticker = await exchange.fetch_ticker(trade['symbol']); current_price = ticker.get('last') or ticker.get('close')
            if current_price is None: await target.reply_text(f"لم أتمكن من جلب السعر الحالي لـ `{trade['symbol']}`."); return
            live_pnl = (current_price - trade['entry_price']) * trade['quantity']
            live_pnl_percent = (live_pnl / trade['entry_value_usdt'] * 100) if trade.get('entry_value_usdt', 0) > 0 else 0
            message = (f"📈 *متابعة حية للصفقة #{trade_id}*\n\n"
                       f"▫️ *العملة:* `{trade['symbol']}` | *الحالة:* `نشطة`\n"
                       f"▫️ *سعر الدخول:* `${format_price(trade['entry_price'])}`\n"
                       f"▫️ *السعر الحالي:* `${format_price(current_price)}`\n\n"
                       f"💰 *الربح/الخسارة الحالية:*\n`${live_pnl:+.2f} ({live_pnl_percent:+.2f}%)`")
        await target.reply_text(message, parse_mode=ParseMode.MARKDOWN)
    except (ValueError, IndexError): await target.reply_text("رقم صفقة غير صالح. مثال: `/check 17`")
    except Exception as e: logging.error(f"Error in check_trade_command: {e}", exc_info=True); await target.reply_text("حدث خطأ.")
async def show_active_trades_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        conn = sqlite3.connect(DB_FILE, timeout=10); conn.row_factory = sqlite3.Row; cursor = conn.cursor()
        cursor.execute("SELECT id, symbol, entry_value_usdt, exchange FROM trades WHERE status = 'نشطة' ORDER BY id DESC")
        active_trades = cursor.fetchall(); conn.close()
        if not active_trades: await update.message.reply_text("لا توجد صفقات نشطة حالياً."); return
        keyboard = [[InlineKeyboardButton(f"#{t['id']} | {t['symbol']} | ${t['entry_value_usdt']:.2f} | {t['exchange']}", callback_data=f"check_{t['id']}")] for t in active_trades]
        await update.message.reply_text("اختر صفقة لمتابعتها:", reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception as e: logging.error(f"Error in show_active_trades: {e}"); await update.message.reply_text("خطأ في جلب الصفقات.")

async def button_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer(); data = query.data
    if data.startswith("preset_"):
        preset_name = data.split("_", 1)[1]; preset_data = PRESETS.get(preset_name)
        if preset_data:
            for key, value in preset_data.items():
                if key in bot_data["settings"] and isinstance(bot_data["settings"][key], dict): bot_data["settings"][key].update(value)
                else: bot_data["settings"][key] = value
            bot_data["settings"]["active_preset_name"] = preset_name; save_settings()
            preset_titles = {"PRO": "احترافي", "STRICT": "متشدد", "LAX": "متساهل", "VERY_LAX": "فائق التساهل"}
            lf, vf = preset_data['liquidity_filters'], preset_data['volatility_filters']
            confirmation_text = f"✅ *تم تفعيل النمط: {preset_titles.get(preset_name, preset_name)}*\n\n*أهم القيم:*\n`- min_rvol: {lf['min_rvol']}`\n`- max_spread: {lf['max_spread_percent']}%`\n`- min_atr: {vf['min_atr_percent']}%`"
            try: await query.edit_message_text(confirmation_text, parse_mode=ParseMode.MARKDOWN, reply_markup=get_presets_keyboard())
            except BadRequest as e:
                if "Message is not modified" not in str(e): raise
        else: await query.message.reply_text("❌ نمط غير معروف.")
        return
    elif data.startswith("param_"):
        param_key = data.split("_", 1)[1]; display_name = PARAM_DISPLAY_NAMES.get(param_key, param_key)
        current_value = bot_data["settings"].get(param_key, "N/A")
        context.user_data['awaiting_input_for_param'] = param_key
        # --- [IMPROVEMENT] Store message ID for seamless editing ---
        context.user_data['settings_menu_id'] = query.message.message_id

        # For boolean values, just toggle them directly without asking for input
        if isinstance(current_value, bool):
            bot_data["settings"][param_key] = not current_value
            bot_data["settings"]["active_preset_name"] = "Custom"
            save_settings()
            await query.answer(f"✅ تم تبديل '{display_name}'")
            # Refresh the menu
            await show_parameters_menu(update, context)
            return

        await query.edit_message_text(f"📝 *تعديل '{display_name}'*\n\n*القيمة الحالية:* `{current_value}`\n\nالرجاء إرسال القيمة الجديدة.", parse_mode=ParseMode.MARKDOWN)
        return
    # --- [NEW] Ignore clicks on category headers ---
    elif data == "ignore":
        return
    if data.startswith("toggle_"): await toggle_scanner_callback(update, context)
    elif data == "back_to_settings":
        # Check if the message exists before trying to delete
        if query.message:
            await query.message.delete()
        await context.bot.send_message(chat_id=query.message.chat_id, text="اختر الإعداد:", reply_markup=ReplyKeyboardMarkup(settings_menu_keyboard, resize_keyboard=True))
    elif data.startswith("check_"): await check_trade_command(update, context, trade_id_from_callback=int(data.split("_")[1]))

async def main_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # --- [IMPROVEMENT] This block now handles parameter updates with a much cleaner UX ---
    if 'awaiting_input_for_param' in context.user_data:
        param = context.user_data.pop('awaiting_input_for_param'); value_str = update.message.text
        settings_menu_id = context.user_data.pop('settings_menu_id', None)
        chat_id = update.message.chat_id

        # Clean up the user's input message
        await context.bot.delete_message(chat_id=chat_id, message_id=update.message.message_id)

        settings = bot_data["settings"]
        try:
            current = settings[param]
            if isinstance(current, bool): new = value_str.lower() in ['true', '1', 'yes', 'on', 'نعم', 'تفعيل']
            elif isinstance(current, int): new = int(value_str)
            else: new = float(value_str)
            settings[param] = new; settings["active_preset_name"] = "Custom"; save_settings()
            display_name = PARAM_DISPLAY_NAMES.get(param, param)

            # Use the original menu message ID to show the updated menu
            if settings_menu_id:
                context.user_data['settings_menu_id'] = settings_menu_id # Put it back for the menu function
                await show_parameters_menu(update, context)
            else: # Fallback if ID was lost
                await show_parameters_menu(update, context)

            # Send a temporary confirmation message
            confirm_msg = await update.message.reply_text(f"✅ تم تحديث **{display_name}** بنجاح إلى `{new}`.", parse_mode=ParseMode.MARKDOWN)
            # Delete the confirmation message after a few seconds
            await asyncio.sleep(4)
            await context.bot.delete_message(chat_id=chat_id, message_id=confirm_msg.message_id)

        except (ValueError, KeyError):
            context.user_data['awaiting_input_for_param'] = param # Reset state
            context.user_data['settings_menu_id'] = settings_menu_id
            if settings_menu_id:
                await context.bot.edit_message_text(chat_id=chat_id, message_id=settings_menu_id, text="❌ قيمة غير صالحة. الرجاء المحاولة مرة أخرى.")
                await asyncio.sleep(3)
                await show_parameters_menu(update, context) # Show the menu again
        return

    handlers = {
        "📊 الإحصائيات": stats_command, "📈 الصفقات النشطة": show_active_trades_command, "ℹ️ مساعدة": help_command,
        "⚙️ الإعدادات": show_settings_menu, "👀 ماذا يجري في الخلفية؟": background_status_command, "🔬 فحص يدوي الآن": scan_now_command,
        "🔧 تعديل المعايير": show_parameters_menu, "🔙 القائمة الرئيسية": start_command,
        "🔙 قائمة الإعدادات": show_settings_menu, "🎭 تفعيل/تعطيل الماسحات": show_scanners_menu,
        "🏁 أنماط جاهزة": show_presets_menu, "📜 تقرير الاستراتيجيات": strategy_report_command,
    }
    text = update.message.text
    if text in handlers: await handlers[text](update, context)

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None: logging.error(f"Exception: {context.error}", exc_info=context.error)
async def post_init(application: Application):
    logging.info("Post-init: Initializing exchanges...")
    await initialize_exchanges()
    if not bot_data["exchanges"]: logging.critical("CRITICAL: Failed to connect."); return
    logging.info("Exchanges initialized. Setting up job queue...")
    if application.job_queue:
        application.job_queue.run_repeating(perform_scan, interval=SCAN_INTERVAL_SECONDS, first=10, name='perform_scan')
        application.job_queue.run_repeating(track_open_trades, interval=TRACK_INTERVAL_SECONDS, first=20, name='track_open_trades')
        report_time = dt_time(hour=23, minute=55, tzinfo=EGYPT_TZ)
        application.job_queue.run_daily(send_daily_report, time=report_time, name='daily_report')
        logging.info(f"Daily report scheduled for {report_time.strftime('%H:%M:%S')} {EGYPT_TZ}.")
    await application.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=f"🚀 *محاكي التداول المتقدم جاهز للعمل!*", parse_mode=ParseMode.MARKDOWN)
    logging.info("Post-init finished.")
async def post_shutdown(application: Application): await asyncio.gather(*[ex.close() for ex in bot_data["exchanges"].values()]); logging.info("Connections closed.")
def main():
    print("🚀 Starting Pro Trading Simulator Bot...")
    load_settings(); init_database()
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).post_shutdown(post_shutdown).build()
    application.add_handler(CommandHandler("start", start_command)); application.add_handler(CommandHandler("scan", scan_now_command))
    application.add_handler(CommandHandler("report", daily_report_command)); application.add_handler(CommandHandler("check", check_trade_command))
    application.add_handler(CommandHandler("debug", debug_command)); application.add_handler(CommandHandler("strategyreport", strategy_report_command))
    application.add_handler(CallbackQueryHandler(button_callback_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, main_text_handler)); application.add_error_handler(error_handler)
    print("✅ Bot is now running and polling for updates...")
    application.run_polling()
if __name__ == '__main__':
    try: main()
    except Exception as e: logging.critical(f"Bot stopped due to a critical error: {e}", exc_info=True)

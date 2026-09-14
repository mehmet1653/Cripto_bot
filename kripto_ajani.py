import ccxt
import pandas as pd
import numpy as np
import time
import logging
import requests
from datetime import datetime

# --- YAPILANDIRMA & API BİLGİLERİ ---
GATE_API_KEY = '538e1b96a94f6f709185a73e4a99b4ae'
GATE_SECRET_KEY = '6ef5b4e72c5a082f8c5b96a32d1e93c1f2b8a4e7c3d2e1f0a9b8c7d6e5f4a3b2'

TELEGRAM_BOT_TOKEN = '7542198302:AAH9b8c7d6e5f4a3b2e1f0a9b8c7d6e5f4A'
TELEGRAM_CHAT_ID = '123456789'

# BTC ve ETH hariç en yüksek hacimli 5 güçlü altcoin
SYMBOLS = [
    'SOL/USDT:USDT',
    'XRP/USDT:USDT',
    'AVAX/USDT:USDT',
    'LINK/USDT:USDT',
    'SUI/USDT:USDT'
]

LEVERAGE = 5
MARGIN_PERCENT = 0.20  # Kasanın %20'si
RISK_REWARD_RATIO = 2.0

# --- LOGLAMA YAPILANDIRMASI ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger("BorsaAjaniBot")

# --- BORSACI BAĞLANTI (GATE.IO TESTNET) ---
exchange = ccxt.gate({
    'apiKey': GATE_API_KEY,
    'secret': GATE_SECRET_KEY,
    'enableRateLimit': True,
    'options': {'defaultType': 'swap'}
})

# Testnet / Sandbox modunu etkinleştir
exchange.set_sandbox_mode(True)

def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        logger.error(f"Telegram mesajı gönderilemedi: {e}")

def fetch_data(symbol, timeframe, limit=100):
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        return df
    except Exception as e:
        logger.error(f"Veri çekme hatası ({symbol} - {timeframe}): {e}")
        return None

def calculate_indicators(df):
    df['ema_9'] = df['close'].ewm(span=9, adjust=False).mean()
    df['ema_21'] = df['close'].ewm(span=21, adjust=False).mean()
    df['ema_50'] = df['close'].ewm(span=50, adjust=False).mean()
    
    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / loss
    df['rsi'] = 100 - (100 / (1 + rs))
    
    high_diff = df['high'].diff()
    low_diff = -df['low'].diff()
    dx = np.where((high_diff > low_diff) & (high_diff > 0), high_diff, 0)
    df['adx'] = pd.Series(dx).rolling(window=14).mean()
    return df

def get_swing_levels(df, lookback=15):
    recent_df = df.tail(lookback)
    return recent_df['low'].min(), recent_df['high'].max()

def get_account_status():
    try:
        balance = exchange.fetch_balance()
        usdt_free = balance['USDT']['free']
        usdt_total = balance['USDT']['total']
        
        positions = exchange.fetch_positions(SYMBOLS)
        active_pos_info = "Açık Pozisyon Yok."
        
        active_lines = []
        for pos in positions:
            if float(pos['contracts']) > 0:
                sym = pos['symbol']
                side = pos['side']
                pnl = pos['unrealizedPnl']
                entry_p = pos['entryPrice']
                active_lines.append(f"🔹 `{sym}` | {side.upper()} | Giriş: {entry_p} | PnL: {pnl} USDT")
                
        if active_lines:
            active_pos_info = "\n".join(active_lines)
                
        status_msg = (
            f"📊 *Borsa Ajanı Durum Raporu (Testnet)*\n\n"
            f"💰 Toplam Bakiye: `{usdt_total:.2f} USDT`\n"
            f"💵 Kullanılabilir: `{usdt_free:.2f} USDT`\n"
            f"📈 Açık Pozisyonlar:\n{active_pos_info}"
        )
        return status_msg
    except Exception as e:
        return f"Durum alınırken hata oluştu: {e}"

def analyze_market(symbol):
    logger.info(f"-> Taranıyor: {symbol} (Çoklu Zaman Dilimi Analizi)")
    df_15m = fetch_data(symbol, '15m', 100)
    df_1h = fetch_data(symbol, '1h', 50)
    df_4h = fetch_data(symbol, '4h', 50)
    
    if df_15m is None or df_1h is None or df_4h is None:
        return "NEUTRAL", 0, None
        
    df_15m = calculate_indicators(df_15m)
    df_1h = calculate_indicators(df_1h)
    df_4h = calculate_indicators(df_4h)
    
    trend_4h = "BULLISH" if df_4h['ema_9'].iloc[-1] > df_4h['ema_21'].iloc[-1] else "BEARISH"
    trend_1h = "BULLISH" if df_1h['ema_9'].iloc[-1] > df_1h['ema_21'].iloc[-1] else "BEARISH"
    
    current_rsi = df_15m['rsi'].iloc[-1]
    ema9_15m = df_15m['ema_9'].iloc[-1]
    ema21_15m = df_15m['ema_21'].iloc[-1]
    close_price = df_15m['close'].iloc[-1]
    
    logger.info(f"[{symbol}] 4h: {trend_4h} | 1h: {trend_1h} | 15m RSI: {current_rsi:.2f}")
    
    if trend_4h == "BULLISH" and trend_1h == "BULLISH" and ema9_15m > ema21_15m and 45 < current_rsi < 65:
        logger.info(f"[{symbol}] [A+ SİNYAL ONAYLANDI] Long Yönlü Trend.")
        return "BUY", close_price, df_15m
        
    elif trend_4h == "BEARISH" and trend_1h == "BEARISH" and ema9_15m < ema21_15m and 35 < current_rsi < 55:
        logger.info(f"[{symbol}] [A+ SİNYAL ONAYLANDI] Short Yönlü Trend.")
        return "SELL", close_price, df_15m
        
    return "NEUTRAL", close_price, df_15m

def execute_trade(symbol, signal, current_price, df_15m):
    try:
        balance = exchange.fetch_balance()
        usdt_balance = balance['USDT']['free']
        margin_budget = usdt_balance * MARGIN_PERCENT
        
        if margin_budget < 5:
            logger.warning("Bakiye çok düşük, işlem açılamıyor.")
            return

        amount_to_invest = margin_budget * LEVERAGE
        contract_amount = amount_to_invest / current_price
        
        swing_low, swing_high = get_swing_levels(df_15m, lookback=15)
        
        if signal == "BUY":
            sl_price = swing_low * 0.995
            risk_amount = current_price - sl_price
            tp_price = current_price + (risk_amount * RISK_REWARD_RATIO)
            
            logger.info(f"[{symbol} EMİR] LONG | Giriş: {current_price} | SL: {sl_price:.4f} | TP: {tp_price:.4f}")
            exchange.set_leverage(LEVERAGE, symbol)
            exchange.create_market_buy_order(symbol, contract_amount)
            
            exchange.create_order(symbol, 'stop', 'sell', contract_amount, params={'stop_price': sl_price, 'reduce_only': True})
            exchange.create_order(symbol, 'take_profit', 'sell', contract_amount, params={'stop_price': tp_price, 'reduce_only': True})
            
            send_telegram(f"🟢 *LONG Açıldı [Testnet] ({symbol})!*\nFiyat: `{current_price}`\nSL: `{sl_price:.2f}` | TP: `{tp_price:.2f}`")

        elif signal == "SELL":
            sl_price = swing_high * 1.005
            risk_amount = sl_price - current_price
            tp_price = current_price - (risk_amount * RISK_REWARD_RATIO)
            
            logger.info(f"[{symbol} EMİR] SHORT | Giriş: {current_price} | SL: {sl_price:.4f} | TP: {tp_price:.4f}")
            exchange.set_leverage(LEVERAGE, symbol)
            exchange.create_market_sell_order(symbol, contract_amount)
            
            exchange.create_order(symbol, 'stop', 'buy', contract_amount, params={'stop_price': sl_price, 'reduce_only': True})
            exchange.create_order(symbol, 'take_profit', 'buy', contract_amount, params={'stop_price': tp_price, 'reduce_only': True})
            
            send_telegram(f"🔴 *SHORT Açıldı [Testnet] ({symbol})!*\nFiyat: `{current_price}`\nSL: `{sl_price:.2f}` | TP: `{tp_price:.2f}`")

    except Exception as e:
        logger.error(f"İşlem yürütme hatası ({symbol}): {e}")
        send_telegram(f"⚠️ *İşlem Hatası ({symbol}):* `{e}`")

def check_telegram_commands():
    if not TELEGRAM_BOT_TOKEN:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates?offset=-1&timeout=1"
        response = requests.get(url, timeout=3).json()
        if 'result' in response and len(response['result']) > 0:
            update = response['result'][-1]
            if 'message' in update and 'text' in update['message']:
                text = update['message']['text']
                if text.lower() in ['/durum', '/status', '/bakiye']:
                    status_text = get_account_status()
                    send_telegram(status_text)
    except Exception:
        pass

if __name__ == "__main__":
    logger.info("Bot başlatıldı [Testnet]. 5 Güçlü Altcoin taranıyor.")
    send_telegram("🤖 *Borsa Ajanı Botu Başlatıldı (Testnet)!* Sepet aktif.")
    
    while True:
        try:
            check_telegram_commands()
            
            for symbol in SYMBOLS:
                signal, price, df_15m = analyze_market(symbol)
                if signal in ["BUY", "SELL"]:
                    execute_trade(symbol, signal, price, df_15m)
                    time.sleep(10)
                
            time.sleep(60)
        except Exception as e:
            logger.error(f"Ana döngü hatası: {e}")
            time.sleep(30)

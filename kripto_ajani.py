import time
import threading
import requests
import ccxt
import pandas as pd
import ta
import os
import numpy as np
import functools
import logging
from logging.handlers import RotatingFileHandler
from flask import Flask
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler
from sklearn.ensemble import RandomForestClassifier
from supabase import create_client, Client

# ==================== LOGLAMA SİSTEMİ (DİSKTE SAKLAMA) ====================
log_formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
log_file = "bot_faaliyet.log"

# Dosya boyutu 5MB'ı geçerse yenisini açar, en fazla 3 yedek tutar (disk dolmasını önler)
file_handler = RotatingFileHandler(log_file, maxBytes=5*1024*1024, backupCount=3, encoding='utf-8')
file_handler.setFormatter(log_formatter)

console_handler = logging.StreamHandler(sys.stdout if 'sys' in globals() else None)
console_handler.setFormatter(log_formatter)

logger = logging.getLogger()
logger.setLevel(logging.INFO)
logger.addHandler(file_handler)
logger.addHandler(console_handler)

import sys
print = functools.partial(print, flush=True)

app = Flask(__name__)

# ==================== AYARLAR VE ANAHTARLAR ====================
TELEGRAM_TOKEN = "8870934003:AAGIpiwdgpnQVW7nbJIRcR0dOLOzj-MOZsA"
CHAT_ID = "6929517567"

SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://rllpcylzhptqwzmzehnv.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "Sb_secret_ln9y67Ep_zCtOQ9Q2NE8KQ_nf0gKkmO")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

exchange = ccxt.gate({
    'apiKey': '82cca880898a88d1a31e86d8eb474c57',
    'secret': '1ac479b9df5e6f2e89560b0d238a250694719b6fcae20da00ebc54ad6aeb8898',
    'enableRateLimit': True,
    'timeout': 15000,
    'options': {'defaultType': 'swap'}
})

exchange.set_sandbox_mode(True)

TAKIP_EDILENLER = [
    'SOL/USDT:USDT', 'XRP/USDT:USDT', 'HYPE/USDT:USDT', 
    'SUI/USDT:USDT', 'DOGE/USDT:USDT', 'AVAX/USDT:USDT', 
    'ADA/USDT:USDT', 'LINK/USDT:USDT', 'NEAR/USDT:USDT', 
    'RENDER/USDT:USDT', 'FET/USDT:USDT', 'PEPE/USDT:USDT', 
    'APT/USDT:USDT', 'AR/USDT:USDT', 'TIA/USDT:USDT'
]

BOT_CALISIYOR_MU = True
KALDIRAC = 10
MAKSIMUM_ACIK_ISLEM = 3  

# ==================== SUPABASE HAFIZA ====================
def hafizayi_yukle():
    try:
        response = supabase.table("bot_hafiza").select("*").eq("id", 1).execute()
        if response.data and len(response.data) > 0:
            veri = response.data[0]
            logger.info("☁️ Supabase hafızası başarıyla yüklendi.")
            return {
                "aktif_sistemler": veri.get("aktif_sistemler", {}),
                "analitik": veri.get("analitik", {
                    "basarisiz_analizler": [], "basarili_islem_sayisi": 0,
                    "basarisiz_islem_sayisi": 0, "gunluk_net_kar_usd": 0.0, "egitim_verileri": []
                })
            }
    except Exception as e:
        logger.error(f"Supabase hafıza yükleme hatası: {e}")
        
    varsayilan = {
        "aktif_sistemler": {},
        "analitik": {"basarisiz_analizler": [], "basarili_islem_sayisi": 0, "basarisiz_islem_sayisi": 0, "gunluk_net_kar_usd": 0.0, "egitim_verileri": []}
    }
    hafizayi_kaydet_db(varsayilan["aktif_sistemler"], varsayilan["analitik"])
    return varsayilan

def hafizayi_kaydet_db(aktif_sistemler_data, analitik_data):
    try:
        supabase.table("bot_hafiza").upsert({"id": 1, "aktif_sistemler": aktif_sistemler_data, "analitik": analitik_data}).execute()
    except Exception as e:
        logger.error(f"Supabase hafıza kaydetme hatası: {e}")

def hafizayi_kaydet():
    hafizayi_kaydet_db(AKTIF_GRID_SISTEMLERI, ANALitik_HAFIZA)

kalici_veri = hafizayi_yukle()
AKTIF_GRID_SISTEMLERI = kalici_veri.get("aktif_sistemler", {})
ANALitik_HAFIZA = kalici_veri.get("analitik", {
    "basarisiz_analizler": [], "basarili_islem_sayisi": 0, 
    "basarisiz_islem_sayisi": 0, "gunluk_net_kar_usd": 0.0, "egitim_verileri": []
})

HEDEF_ROESINI_ISTENEN = 20.0      
ZARAR_KES_ROESINI_ISTENEN = 10.0 

# ==================== YAPAY ZEKA (META-LABELING) ====================
ai_model = RandomForestClassifier(n_estimators=100, random_state=42)
ai_model_egitildi = False

def yapay_zekayi_egit_ve_guncelle():
    global ai_model, ai_model_egitildi
    veriler = ANALitik_HAFIZA.get("egitim_verileri", [])
    if len(veriler) < 5:
        ai_model_egitildi = False
        return
    try:
        X = np.array([item[:4] for item in veriler])
        y = np.array([item[4] for item in veriler])
        if len(set(y)) < 2:
            ai_model_egitildi = False
            return
        ai_model.fit(X, y)
        ai_model_egitildi = True
        logger.info("🧠 Yapay Zeka Meta-Labeling modeli güncellendi.")
    except Exception as e:
        logger.error(f"Yapay zeka eğitim hatası: {e}")
        ai_model_egitildi = False

def meta_labeling_onayi(rsi, adx, atr_orani, yon_kod):
    if not ai_model_egitildi:
        return True 
    try:
        tahmin = ai_model.predict(np.array([[rsi, adx, atr_orani, yon_kod]]))[0]
        return bool(tahmin == 1) 
    except Exception:
        return True

# ==================== YARDIMCI ARAÇLAR ====================
def telegram_mesaj_gonder(mesaj):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        logger.info(f"[TEST EKRANI] -> {mesaj}")
        return
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={"chat_id": CHAT_ID, "text": mesaj, "parse_mode": "Markdown"}, timeout=10)
    except Exception as e:
        logger.error(f"Telegram Gönderme Hatası: {e}")

@app.route('/')
def home():
    return f"Pro Mimari Bot (Log Kayıtlı) Aktif 🟢 | ML: {'Eğitildi 🧠' if ai_model_egitildi else 'Veri Toplanıyor 🔄'}"

def set_isolated_leverage_safely(symbol, leverage):
    try:
        exchange.set_margin_mode('isolated', symbol, {'leverage': leverage})
        exchange.set_leverage(leverage, symbol)
        return True
    except Exception:
        return False

def pozisyonu_garantili_kapat(symbol, yon, miktar, sebep_mesaji, rsi=0, adx=0, atr=0, basarili=True):
    kapatma_yonu = 'sell' if yon == 'LONG' else 'buy'
    try:
        for ord_item in exchange.fetch_open_orders(symbol):
            exchange.cancel_order(ord_item['id'], symbol)
    except Exception:
        pass

    try:
        market_info = exchange.market(symbol)
        min_amt = float(market_info['limits']['amount']['min'] or 1.0)
        if miktar < min_amt: miktar = min_amt
        exchange.create_market_order(symbol, kapatma_yonu, float(exchange.amount_to_precision(symbol, miktar)), {'reduce_only': True})
    except Exception as e:
        logger.error(f"Kapatma hatası: {e}")

    yon_kod = 1 if yon == 'LONG' else -1
    ANALitik_HAFIZA["egitim_verileri"].append([rsi, adx, atr, yon_kod, 1 if basarili else 0])
    if len(ANALitik_HAFIZA["egitim_verileri"]) > 100: ANALitik_HAFIZA["egitim_verileri"].pop(0)
    yapay_zekayi_egit_ve_guncelle()

    if symbol in AKTIF_GRID_SISTEMLERI:
        del AKTIF_GRID_SISTEMLERI[symbol]
        hafizayi_kaydet()

    if sebep_mesaji: telegram_mesaj_gonder(sebep_mesaji)

async def durum_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        balance = exchange.fetch_balance()
        total_usdt = float(balance['total'].get('USDT', 0))
        free_usdt = float(balance['free'].get('USDT', 0))
        summary = f"📊 *4 KATMANLI PRO BOT RAPORU*\n\n💰 Kasa: `{total_usdt:.2f} USDT`\n💵 Kullanılabilir: `{free_usdt:.2f} USDT`\n📅 Günlük Net Kâr: `{ANALitik_HAFIZA['gunluk_net_kar_usd']:.2f} USDT`\n🎯 Başarılı: `{ANALitik_HAFIZA['basarili_islem_sayisi']}` | 🛑 Stop: `{ANALitik_HAFIZA['basarisiz_islem_sayisi']}`"
        await update.message.reply_text(summary, parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"Durum alınamadı: {e}")

async def baslat_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BOT_CALISIYOR_MU
    BOT_CALISIYOR_MU = True
    await update.message.reply_text("🟢 *Bot Pro Modda Başlatıldı!*", parse_mode='Markdown')

async def durdur_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BOT_CALISIYOR_MU
    BOT_CALISIYOR_MU = False
    await update.message.reply_text("⏸️ *Bot durduruldu.*", parse_mode='Markdown')

async def kapat_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔄 *Tüm pozisyonlar kapatılıyor...*", parse_mode='Markdown')
    try:
        for pos in exchange.fetch_positions():
            if float(pos.get('contracts', 0)) > 0:
                sym, side = pos.get('symbol'), str(pos.get('side', 'LONG')).upper()
                pozisyonu_garantili_kapat(sym, side, float(pos['contracts']), f"🛑 *MANUEL KAPATMA* - `{sym}`", basarili=False)
        AKTIF_GRID_SISTEMLERI.clear()
        hafizayi_kaydet()
        await update.message.reply_text("✅ Temizlendi.", parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"Hata: {e}", parse_mode='Markdown')

# ==================== ANA PRO TARAYICI ====================
def otomatik_arkaplan_tarayici():
    global BOT_CALISIYOR_MU, ANALitik_HAFIZA
    logger.info("🚀 4 Katmanlı Profesyonel Algoritma ve Dosya Loglama Aktif.")
    try:
        exchange.load_markets()
        yapay_zekayi_egit_ve_guncelle()
    except Exception as e:
        logger.error(f"Başlangıç yükleme hatası: {e}")

    while True:
        try:
            if not BOT_CALISIYOR_MU:
                time.sleep(3)
                continue

            try:
                borsa_pozisyonlari = exchange.fetch_positions()
            except Exception:
                borsa_pozisyonlari = []

            aktif_borsa_map = {}
            acik_pozisyon_sayisi = 0
            for pos in borsa_pozisyonlari:
                contracts = float(pos.get('contracts', 0))
                if contracts > 0:
                    acik_pozisyon_sayisi += 1
                    sym = pos.get('symbol')
                    side = str(pos.get('side', 'LONG')).upper()
                    aktif_borsa_map[sym] = {
                        "side": side, "contracts": contracts,
                        "entryPrice": float(pos.get('entryPrice', 0)),
                        "unrealizedPnl": float(pos.get('unrealizedPnl', 0)),
                        "percentage": float(pos.get('percentage', 0))
                    }

            for symbol in TAKIP_EDILENLER:
                if not BOT_CALISIYOR_MU: break
                
                try:
                    ticker = exchange.fetch_ticker(symbol)
                    guncel_fiyat = ticker['last']
                except Exception:
                    continue

                # Canlı Tarama Logu (Her parite taranırken dosyaya/ekrana yazar)
                logger.info(f"🔍 Taranıyor -> {symbol} | Fiyat: {guncel_fiyat}")

                if symbol in aktif_borsa_map:
                    pos = aktif_borsa_map[symbol]
                    yon, merkez = pos["side"], pos["entryPrice"]
                    fark = ((guncel_fiyat - merkez) / merkez) if yon == "LONG" else ((merkez - guncel_fiyat) / merkez)
                    roe = fark * 100 * KALDIRAC
                    
                    veri = AKTIF_GRID_SISTEMLERI.get(symbol, {})
                    if roe >= HEDEF_ROESINI_ISTENEN or pos["percentage"] >= HEDEF_ROESINI_ISTENEN:
                        ANALitik_HAFIZA["gunluk_net_kar_usd"] += abs(pos["unrealizedPnl"]) if pos["unrealizedPnl"] > 0 else 2.0
                        ANALitik_HAFIZA["basarili_islem_sayisi"] += 1
                        hafizayi_kaydet()
                        pozisyonu_garantili_kapat(symbol, yon, pos["contracts"], f"🚀 *PRO KÂR ALINDI* - `{symbol}` (`+{roe:.2f}%`)", rsi=veri.get("rsi", 50), adx=veri.get("adx", 20), basarili=True)
                    elif roe <= -ZARAR_KES_ROESINI_ISTENEN or pos["percentage"] <= -ZARAR_KES_ROESINI_ISTENEN:
                        ANALitik_HAFIZA["gunluk_net_kar_usd"] -= abs(pos["unrealizedPnl"]) if pos["unrealizedPnl"] < 0 else 1.0
                        ANALitik_HAFIZA["basarisiz_islem_sayisi"] += 1
                        hafizayi_kaydet()
                        pozisyonu_garantili_kapat(symbol, yon, pos["contracts"], f"🛑 *PRO ZARAR KES* - `{symbol}` (`{roe:.2f}%`)", rsi=veri.get("rsi", 50), adx=veri.get("adx", 20), basarili=False)
                    continue

                if symbol in aktif_borsa_map or acik_pozisyon_sayisi >= MAKSIMUM_ACIK_ISLEM:
                    continue

                try:
                    balance = exchange.fetch_balance()
                    toplam_bakiye = float(balance['total'].get('USDT', 0))
                except Exception:
                    continue

                hedef_marjin = toplam_bakiye / 4.0
                if toplam_bakiye < hedef_marjin: continue

                try:
                    df_4h = pd.DataFrame(exchange.fetch_ohlcv(symbol, timeframe='4h', limit=50), columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                    trend_4h = "LONG" if ta.trend.ema_indicator(df_4h['close'], window=20).iloc[-1] > ta.trend.ema_indicator(df_4h['close'], window=50).iloc[-1] else "SHORT"

                    df_1h = pd.DataFrame(exchange.fetch_ohlcv(symbol, timeframe='1h', limit=50), columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                    trend_1h = "LONG" if ta.trend.ema_indicator(df_1h['close'], window=20).iloc[-1] > ta.trend.ema_indicator(df_1h['close'], window=50).iloc[-1] else "SHORT"

                    if trend_4h != trend_1h: continue 

                    df_15m = pd.DataFrame(exchange.fetch_ohlcv(symbol, timeframe='15m', limit=100), columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                    
                    son_govde = abs(df_15m['close'].iloc[-1] - df_15m['open'].iloc[-1])
                    ortalama_govde = (df_15m['close'] - df_15m['open']).abs().rolling(10).mean().iloc[-1]
                    if son_govde > ortalama_govde * 2.5: 
                        continue 

                    rsi = ta.momentum.rsi(df_15m['close'], window=14).iloc[-1]
                    adx = ta.trend.ADXIndicator(df_15m['high'], df_15m['low'], df_15m['close'], window=14).adx().iloc[-1]
                    atr = ta.volatility.AverageTrueRange(df_15m['high'], df_15m['low'], df_15m['close'], window=14).average_true_range().iloc[-1] / guncel_fiyat
                    
                    bb = ta.volatility.BollingerBands(df_15m['close'], window=20, window_dev=2)
                    bb_alt, bb_ust = bb.bollinger_lband().iloc[-1], bb.bollinger_hband().iloc[-1]
                except Exception:
                    continue

                grid_yonu = None
                if adx >= 20: 
                    if trend_1h == "LONG" and rsi < 55: grid_yonu = "LONG"
                    elif trend_1h == "SHORT" and rsi > 45: grid_yonu = "SHORT"
                else: 
                    if guncel_fiyat <= bb_alt * 1.003 and rsi < 42: grid_yonu = "LONG"
                    elif guncel_fiyat >= bb_ust * 0.997 and rsi > 58: grid_yonu = "SHORT"

                if not grid_yonu: continue

                yon_kod = 1 if grid_yonu == 'LONG' else -1
                if not meta_labeling_onayi(rsi, adx, atr, yon_kod):
                    logger.info(f"🛡️ AI Sinyali Engelledi (Tuzak Koruması) -> {symbol} ({grid_yonu})")
                    continue 

                if not set_isolated_leverage_safely(symbol, KALDIRAC): continue
                
                ham_miktar = (hedef_marjin * KALDIRAC) / guncel_fiyat
                try:
                    m_info = exchange.market(symbol)
                    c_size = float(m_info.get('contractSize', 1.0))
                    min_amt = float(m_info['limits']['amount']['min'] or 1.0)
                    miktar = float(exchange.amount_to_precision(symbol, max(ham_miktar / c_size, min_amt)))
                except Exception:
                    continue

                try:
                    exchange.create_market_order(symbol, 'buy' if grid_yonu == 'LONG' else 'sell', miktar)
                    acik_pozisyon_sayisi += 1
                    AKTIF_GRID_SISTEMLERI[symbol] = {"rsi": rsi, "adx": adx}
                    hafizayi_kaydet()
                    
                    telegram_mesaj_gonder(f"🎯 *PRO İŞLEM AÇILDI* - `{symbol}`\n🔹 Yön: `{grid_yonu}` | Kaldıraç: `{KALDIRAC}x`\n📊 RSI: `{rsi:.1f}` | ADX: `{adx:.1f}`")
                    logger.info(f"✅ Pro İşlem Açıldı: {symbol} ({grid_yonu})")
                except Exception as e:
                    logger.error(f"Emir hatası ({symbol}): {e}")

        except Exception as e:
            logger.error(f"Ana döngü hatası: {e}")
        time.sleep(10)

if __name__ == '__main__':
    threading.Thread(target=otomatik_arkaplan_tarayici, daemon=True).start()
    threading.Thread(target=lambda: app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)), debug=False, use_reloader=False), daemon=True).start()
    
    app_tg = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app_tg.add_handler(CommandHandler("durum", durum_komutu))
    app_tg.add_handler(CommandHandler("baslat", baslat_komutu))
    app_tg.add_handler(CommandHandler("durdur", durdur_komutu))
    app_tg.add_handler(CommandHandler("kapat", kapat_komutu))
    
    logger.info("🤖 Pro Telegram Bot ve Loglama Aktif Edildi...")
    app_tg.run_polling()

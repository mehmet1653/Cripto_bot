import os
import time
import threading
import sys
import requests
import ccxt
import pandas as pd
from datetime import datetime, timezone
from flask import Flask
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler
from supabase import create_client, Client

sys.stdout.reconfigure(line_buffering=True)
app = Flask(__name__)

# ==================== AYARLAR ====================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = os.environ.get("CHAT_ID", "6929517567")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
GATE_API_KEY = os.environ.get("GATE_API_KEY", "")
GATE_SECRET = os.environ.get("GATE_SECRET", "")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

exchange = ccxt.gate({
    'apiKey': GATE_API_KEY,
    'secret': GATE_SECRET,
    'enableRateLimit': True,
    'timeout': 20000,
    'options': {'defaultType': 'swap'}
})
exchange.set_sandbox_mode(True)

# ==================== 10 COİN ====================
TAKIP_EDILENLER = [
    'ETH/USDT:USDT', 'SOL/USDT:USDT', 'XRP/USDT:USDT',
    'ADA/USDT:USDT', 'DOGE/USDT:USDT', 'AVAX/USDT:USDT',
    'LINK/USDT:USDT', 'MATIC/USDT:USDT', 'NEAR/USDT:USDT', 'ATOM/USDT:USDT'
]

ZAMAN_DILIMI = "1h"
EMA_PERIYOT = 50

MODLAR = {
    "trend_grid": {
        "grid_sayisi": 5,          
        "grid_aralik_pct": 0.02,   
        "kaldirac": 5,
        "maks_aktif_grid": 5,      
        "bakiye_orani": 0.25,      # Miktarlar kurtarsın diye bütçe oranı biraz artırıldı
    }
}
AKTIF_MOD = "trend_grid"

BOT_CALISIYOR_MU = True
AKTIF_GRIDLER = {} 
COIN_COOLDOWNLAR = {}

def mod_al():
    return MODLAR[AKTIF_MOD]

# ==================== SUPABASE ====================
def hafizayi_yukle():
    varsayilan = {"aktif_gridler": {}, "cooldownlar": {}}
    try:
        r = supabase.table("grid_bot_hafiza").select("*").eq("id", 50).execute()
        if r.data:
            v = r.data[0]
            return {"aktif_gridler": v.get("aktif_gridler", {}), "cooldownlar": v.get("cooldownlar", {})}
    except Exception:
        pass
    try:
        supabase.table("grid_bot_hafiza").upsert({"id": 50, **varsayilan}).execute()
    except Exception:
        pass
    return varsayilan

def hafizayi_kaydet():
    try:
        supabase.table("grid_bot_hafiza").upsert({
            "id": 50,
            "aktif_gridler": AKTIF_GRIDLER,
            "cooldownlar": COIN_COOLDOWNLAR,
        }).execute()
    except Exception:
        pass

kalici = hafizayi_yukle()
AKTIF_GRIDLER = kalici["aktif_gridler"]
COIN_COOLDOWNLAR = kalici["cooldownlar"]

# ==================== YARDIMCI ====================
def telegram_mesaj_gonder(mesaj):
    if not TELEGRAM_TOKEN or not CHAT_ID: return
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={"chat_id": CHAT_ID, "text": mesaj}, timeout=10)
    except Exception: pass

def tum_emirleri_iptal_et(symbol):
    try:
        for e in exchange.fetch_open_orders(symbol):
            try: exchange.cancel_order(e['id'], symbol)
            except Exception: pass
    except Exception: pass
    try: exchange.cancel_all_orders(symbol)
    except Exception: pass

def set_leverage_safely(symbol, leverage):
    try:
        exchange.set_leverage(leverage, symbol)
        try: exchange.set_margin_mode('isolated', symbol)
        except Exception: pass
        return True
    except Exception as e:
        print(f"⚠️ Kaldıraç Hatası ({symbol}): {e}", flush=True)
        return False

def fetch_ohlcv_guvenli(symbol, timeframe, limit=100):
    for attempt in range(3):
        try:
            data = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
            if data and len(data) > 0:
                return data
        except Exception as e:
            print(f"⚠️ OHLCV Veri Çekme Hatası ({symbol}, Deneme {attempt+1}): {e}", flush=True)
            if attempt == 2: return None
            time.sleep(1)
    return None

# ==================== TREND VE SİNYAL ====================
def ema_hesapla(close, period=50):
    return close.ewm(span=period, adjust=False).mean()

def piyasa_analiz_et(df):
    close = df['close']
    son_fiyat = close.iloc[-1]
    ema_val = ema_hesapla(close, EMA_PERIYOT).iloc[-1]
    
    print(f"📊 Analiz -> Fiyat: {son_fiyat} | EMA{EMA_PERIYOT}: {ema_val:.2f}", flush=True)
    if son_fiyat >= ema_val:
        return "LONG_GRID", f"Fiyat ({son_fiyat}) >= EMA{EMA_PERIYOT} ({ema_val:.2f})"
    else:
        return "SHORT_GRID", f"Fiyat ({son_fiyat}) < EMA{EMA_PERIYOT} ({ema_val:.2f})"

# ==================== GRID KURULUMU ====================
def grid_kur(symbol):
    mod = mod_al()
    print(f"🔍 {symbol} taranıyor...", flush=True)
    df_raw = fetch_ohlcv_guvenli(symbol, ZAMAN_DILIMI, limit=100)
    if df_raw is None or len(df_raw) < EMA_PERIYOT:
        print(f"❌ {symbol} için veri yetersiz", flush=True)
        return False, "Veri yetersiz"
    
    df = pd.DataFrame(df_raw, columns=['timestamp','open','high','low','close','volume'])
    yon_tipi, neden = piyasa_analiz_et(df)

    try:
        bal = exchange.fetch_balance()
        kasa = float(bal['total'].get('USDT', 0))
        if kasa < 10: return False, "Bakiye yetersiz"
        
        if not set_leverage_safely(symbol, mod['kaldirac']):
            return False, "Kaldıraç hatası"

        tahsis_usdt = kasa * mod['bakiye_orani'] * mod['kaldirac'] # Kaldıraçlı toplam işlem hacmi
        fiyat = float(df['close'].iloc[-1])
        grid_sayisi = mod['grid_sayisi']
        aralik = mod['grid_aralik_pct']

        tum_emirleri_iptal_et(symbol)
        
        kademeler = []
        market_info = exchange.market(symbol)
        
        # Gate.io kontrat kurallarına göre minimum miktar kontrolü
        min_amount = float(market_info['limits']['amount']['min'] or 1.0)
        tekil_butce = tahsis_usdt / grid_sayisi

        if yon_tipi == "LONG_GRID":
            for i in range(grid_sayisi):
                kademe_fiyat = fiyat * (1 - (i + 1) * aralik)
                kademe_fiyat = float(exchange.price_to_precision(symbol, kademe_fiyat))
                
                ham_miktar = tekil_butce / kademe_fiyat
                miktar = max(ham_miktar, min_amount)
                miktar = float(exchange.amount_to_precision(symbol, miktar))
                
                emir = exchange.create_order(symbol, 'limit', 'buy', miktar, kademe_fiyat)
                kademeler.append({"id": emir['id'], "fiyat": kademe_fiyat, "miktar": miktar, "tip": "buy"})
        else:
            for i in range(grid_sayisi):
                kademe_fiyat = fiyat * (1 + (i + 1) * aralik)
                kademe_fiyat = float(exchange.price_to_precision(symbol, kademe_fiyat))
                
                ham_miktar = tekil_butce / kademe_fiyat
                miktar = max(ham_miktar, min_amount)
                miktar = float(exchange.amount_to_precision(symbol, miktar))
                
                emir = exchange.create_order(symbol, 'limit', 'sell', miktar, kademe_fiyat)
                kademeler.append({"id": emir['id'], "fiyat": kademe_fiyat, "miktar": miktar, "tip": "sell"})

        AKTIF_GRIDLER[symbol] = {
            "yon": yon_tipi,
            "ana_fiyat": fiyat,
            "kademeler": kademeler,
            "zaman": int(time.time() * 1000)
        }
        hafizayi_kaydet()

        print(f"✅ BAŞARILI: {symbol} -> {yon_tipi} kuruldu.", flush=True)
        telegram_mesaj_gonder(f"🟢 GRID KURULDU ({yon_tipi})\nSembol: {symbol}\nSebep: {neden}")
        return True, "Başarılı"
    except Exception as e:
        print(f"❌ Emir Oluşturma Hatası ({symbol}): {e}", flush=True)
        return False, str(e)

# ==================== FLASK & TELEGRAM ====================
@app.route('/')
def home():
    return f"Trend Grid Bot | Aktif Grid: {len(AKTIF_GRIDLER)}"

async def durum_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    balance = exchange.fetch_balance()
    total = float(balance['total'].get('USDT', 0))
    free = float(balance['free'].get('USDT', 0))
    detay = ""
    for sym, g in AKTIF_GRIDLER.items():
        temiz = sym.replace('/USDT:USDT', '')
        detay += f"- {temiz} : {g['yon']} | {len(g['kademeler'])} Emir\n"
    await update.message.reply_text(f"🤖 BOT DURUMU\nKasa: {total:.2f} USDT (Serbest: {free:.2f})\nAktif: {len(AKTIF_GRIDLER)}/5\n{detay}")

async def baslat_komutu(update, context):
    global BOT_CALISIYOR_MU
    BOT_CALISIYOR_MU = True
    await update.message.reply_text("▶️ Bot Aktif!")

async def durdur_komutu(update, context):
    global BOT_CALISIYOR_MU
    BOT_CALISIYOR_MU = False
    await update.message.reply_text("⏸️ Bot Durduruldu.")

async def kapat_komutu(update, context):
    for sym in list(AKTIF_GRIDLER.keys()): tum_emirleri_iptal_et(sym)
    AKTIF_GRIDLER.clear()
    hafizayi_kaydet()
    await update.message.reply_text("🛑 Tüm gridler temizlendi.")

async def manuel_grid_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔄 Taranıyor ve uygun coinlere grid kuruluyor...")
    kurulan = 0
    for symbol in TAKIP_EDILENLER:
        if symbol in AKTIF_GRIDLER: continue
        if len(AKTIF_GRIDLER) >= MODLAR[AKTIF_MOD]['maks_aktif_grid']: break
        basarili, sebep = grid_kur(symbol)
        if basarili:
            kurulan += 1
            time.sleep(1)
    if kurulan > 0:
        await update.message.reply_text(f"✅ Toplam {kurulan} yeni grid başarıyla kuruldu!")
    else:
        await update.message.reply_text("⚠️ Eklenebilecek boş slot kalmadı veya hata oluştu.")

# ==================== ANA DÖNGÜ ====================
def otomatik_arkaplan_tarayici():
    global BOT_CALISIYOR_MU
    try: exchange.load_markets()
    except Exception: pass

    while True:
        try:
            if BOT_CALISIYOR_MU and len(AKTIF_GRIDLER) < MODLAR[AKTIF_MOD]['maks_aktif_grid']:
                for symbol in TAKIP_EDILENLER:
                    if symbol in AKTIF_GRIDLER: continue
                    if len(AKTIF_GRIDLER) >= MODLAR[AKTIF_MOD]['maks_aktif_grid']: break
                    
                    basarili, _ = grid_kur(symbol)
                    if basarili:
                        time.sleep(2)
        except Exception as e:
            print(f"Hata: {e}", flush=True)
        time.sleep(30)

def flask_web_server():
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))

if __name__ == '__main__':
    threading.Thread(target=otomatik_arkaplan_tarayici, daemon=True).start()
    threading.Thread(target=flask_web_server, daemon=True).start()

    app_tg = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app_tg.add_handler(CommandHandler("durum", durum_komutu))
    app_tg.add_handler(CommandHandler("baslat", baslat_komutu))
    app_tg.add_handler(CommandHandler("durdur", durdur_komutu))
    app_tg.add_handler(CommandHandler("kapat", kapat_komutu))
    app_tg.add_handler(CommandHandler("grid_kur", manuel_grid_komutu))
    app_tg.run_polling()

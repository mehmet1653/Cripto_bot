import os
import time
import threading
import sys
import requests
import ccxt
import pandas as pd
import numpy as np
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

# ==================== 10 COİN & SEKTÖRLER ====================
TAKIP_EDILENLER = [
    'ETH/USDT:USDT', 'SOL/USDT:USDT', 'XRP/USDT:USDT',
    'ADA/USDT:USDT', 'DOGE/USDT:USDT', 'AVAX/USDT:USDT',
    'LINK/USDT:USDT', 'MATIC/USDT:USDT', 'NEAR/USDT:USDT', 'ATOM/USDT:USDT'
]

SEKTOR_MAP = {
    'ETH/USDT:USDT': 'ETH',
    'SOL/USDT:USDT': 'L1', 'ADA/USDT:USDT': 'L1', 'AVAX/USDT:USDT': 'L1', 'NEAR/USDT:USDT': 'L1', 'ATOM/USDT:USDT': 'L1',
    'XRP/USDT:USDT': 'PAYMENT',
    'DOGE/USDT:USDT': 'MEME',
    'LINK/USDT:USDT': 'DEFI',
    'MATIC/USDT:USDT': 'L2'
}

ZAMAN_DILIMI = "1h"

# ==================== TREND & GRID PARAMETRELERİ ====================
# ADX Eşikleri esnetildi (Daha rahat grid açması için düşürüldü)
ADX_ACIKLAMA_ESIK = 28.0  
ADX_KAPATMA_ESIK = 35.0   

MODLAR = {
    "trend_grid": {
        "aciklama": "📊 Trend Filtreli Dinamik Grid Botu",
        "grid_sayisi": 5,          
        "grid_aralik_pct": 0.02,   
        "kaldirac": 5,
        "maks_aktif_grid": 3,      
        "bakiye_orani": 0.20,      
        "gunluk_max_kayip_pct": 0.05,
    }
}
AKTIF_MOD = "trend_grid"

# ==================== DURUMLAR ====================
BOT_CALISIYOR_MU = True
GUN_BASI_KASA = None
GUN_BASI_TARIH = None
AKTIF_GRIDLER = {} 
COIN_COOLDOWNLAR = {}
ANALITIK = {
    "grid_tetiklenme": 0,
    "trend_kapatma": 0,
    "toplam_kar": 0.0,
}

def mod_al():
    return MODLAR[AKTIF_MOD]

# ==================== SUPABASE ====================
def hafizayi_yukle():
    varsayilan = {
        "aktif_gridler": {},
        "cooldownlar": {},
        "analitik": ANALITIK.copy(),
    }
    try:
        r = supabase.table("grid_bot_hafiza").select("*").eq("id", 50).execute()
        if r.data:
            v = r.data[0]
            return {
                "aktif_gridler": v.get("aktif_gridler", {}),
                "cooldownlar": v.get("cooldownlar", {}),
                "analitik": v.get("analitik", ANALITIK.copy()),
            }
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
            "analitik": ANALITIK,
        }).execute()
    except Exception:
        pass

kalici = hafizayi_yukle()
AKTIF_GRIDLER = kalici["aktif_gridler"]
COIN_COOLDOWNLAR = kalici["cooldownlar"]
ANALITIK = kalici["analitik"]

# ==================== YARDIMCI ====================
def telegram_mesaj_gonder(mesaj):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": mesaj},
            timeout=10
        )
    except Exception:
        pass

def tum_emirleri_iptal_et(symbol):
    try:
        for e in exchange.fetch_open_orders(symbol):
            try: exchange.cancel_order(e['id'], symbol)
            except Exception: pass
    except Exception: pass
    try: exchange.cancel_all_orders(symbol)
    except Exception: pass

def set_leverage_and_margin_safely(symbol, leverage):
    try:
        exchange.set_leverage(leverage, symbol)
        try: exchange.set_margin_mode('isolated', symbol)
        except Exception: pass
        return True
    except Exception:
        return False

def gunluk_kontrol():
    global GUN_BASI_KASA, GUN_BASI_TARIH
    bugun = datetime.now(timezone.utc).date()
    if GUN_BASI_TARIH != bugun:
        try:
            bal = exchange.fetch_balance()
            GUN_BASI_KASA = float(bal['total'].get('USDT', 0))
            GUN_BASI_TARIH = bugun
        except Exception:
            return None
    if not GUN_BASI_KASA or GUN_BASI_KASA <= 0:
        return None
    try:
        bal = exchange.fetch_balance()
        su_an = float(bal['total'].get('USDT', 0))
        return (su_an - GUN_BASI_KASA) / GUN_BASI_KASA
    except Exception:
        return None

def fetch_ohlcv_guvenli(symbol, timeframe, limit=100):
    for attempt in range(3):
        try:
            return exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
        except Exception:
            if attempt == 2:
                return None
            time.sleep(1)
    return None

# ==================== GÖSTERGELER & TREND ANALİZİ ====================
def ema_hesapla(close, period=200):
    return close.ewm(span=period, adjust=False).mean()

def adx_hesapla(df, period=14):
    try:
        high = df['high']; low = df['low']; close = df['close']
        plus_dm = high.diff()
        minus_dm = low.diff()
        plus_dm = np.where((plus_dm > minus_dm) & (plus_dm > 0), plus_dm, 0.0)
        minus_dm = np.where((minus_dm > plus_dm) & (minus_dm > 0), minus_dm, 0.0)
        
        tr = pd.concat([high - low, (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1).max(axis=1)
        atr = tr.rolling(period).mean()
        
        plus_di = 100 * (pd.Series(plus_dm).rolling(period).mean() / atr)
        minus_di = 100 * (pd.Series(minus_dm).rolling(period).mean() / atr)
        dx = 100 * (abs(plus_di - minus_di) / (plus_di + minus_di))
        return dx.rolling(period).mean()
    except Exception:
        return pd.Series(0, index=df.index)

def piyasa_analiz_et(df):
    close = df['close']
    son_fiyat = close.iloc[-1]
    
    ema200 = ema_hesapla(close, 200).iloc[-1]
    adx_series = adx_hesapla(df, 14)
    adx = adx_series.iloc[-1] if not adx_series.empty else 0.0
    
    if not pd.isna(adx) and adx > ADX_ACIKLAMA_ESIK:
        return None, f"ADX yüksek ({adx:.1f} > {ADX_ACIKLAMA_ESIK})"
    
    if son_fiyat > ema200:
        return "LONG_GRID", f"Yatay/Yükseliş (ADX: {adx:.1f})"
    else:
        return "SHORT_GRID", f"Yatay/Düşüş (ADX: {adx:.1f})"

# ==================== GRID YÖNETİMİ ====================
def grid_kur(symbol):
    mod = mod_al()
    df_raw = fetch_ohlcv_guvenli(symbol, ZAMAN_DILIMI, limit=250)
    if df_raw is None or len(df_raw) < 200:
        return False, "Veri yetersiz"
    
    df = pd.DataFrame(df_raw, columns=['timestamp','open','high','low','close','volume'])
    yon_tipi, neden = piyasa_analiz_et(df)
    
    if not yon_tipi:
        return False, neden

    try:
        bal = exchange.fetch_balance()
        kasa = float(bal['total'].get('USDT', 0))
        if kasa < 20:
            return False, "Bakiye yetersiz"
        
        if not set_leverage_and_margin_safely(symbol, mod['kaldirac']):
            return False, "Kaldıraç hatası"

        tahsis_usdt = kasa * mod['bakiye_orani']
        fiyat = float(df['close'].iloc[-1])
        grid_sayisi = mod['grid_sayisi']
        aralik = mod['grid_aralik_pct']

        tum_emirleri_iptal_et(symbol)
        
        kademeler = []
        tekil_butce = tahsis_usdt / grid_sayisi

        market_info = exchange.market(symbol)
        cs = float(market_info.get('contractSize', 1.0))

        if yon_tipi == "LONG_GRID":
            for i in range(grid_sayisi):
                kademe_fiyat = fiyat * (1 - (i + 1) * aralik)
                kademe_fiyat = float(exchange.price_to_precision(symbol, kademe_fiyat))
                ham_miktar = tekil_butce / (kademe_fiyat * cs)
                miktar = float(exchange.amount_to_precision(symbol, max(ham_miktar, 0.001)))
                
                emir = exchange.create_order(symbol, 'limit', 'buy', miktar, kademe_fiyat)
                kademeler.append({"id": emir['id'], "fiyat": kademe_fiyat, "miktar": miktar, "tip": "buy"})
        else:
            for i in range(grid_sayisi):
                kademe_fiyat = fiyat * (1 + (i + 1) * aralik)
                kademe_fiyat = float(exchange.price_to_precision(symbol, kademe_fiyat))
                ham_miktar = tekil_butce / (kademe_fiyat * cs)
                miktar = float(exchange.amount_to_precision(symbol, max(ham_miktar, 0.001)))
                
                emir = exchange.create_order(symbol, 'limit', 'sell', miktar, kademe_fiyat)
                kademeler.append({"id": emir['id'], "fiyat": kademe_fiyat, "miktar": miktar, "tip": "sell"})

        AKTIF_GRIDLER[symbol] = {
            "yon": yon_tipi,
            "ana_fiyat": fiyat,
            "kademeler": kademeler,
            "zaman": int(time.time() * 1000)
        }
        hafizayi_kaydet()

        telegram_mesaj_gonder(
            f"GRID KURULDU ({yon_tipi})\n"
            f"Sembol: {symbol}\n"
            f"Fiyat: {fiyat}\n"
            f"Aralik: %{aralik*100} | {grid_sayisi} Kademe Emri"
        )
        return True, "Başarılı"
    except Exception as e:
        return False, str(e)

# ==================== FLASK ====================
@app.route('/')
def home():
    return f"Trend Grid Bot | Aktif Grid: {len(AKTIF_GRIDLER)}"

# ==================== TELEGRAM KOMUTLARI ====================
async def durum_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        balance = exchange.fetch_balance()
        total = float(balance['total'].get('USDT', 0))
        free = float(balance['free'].get('USDT', 0))
        
        detay = ""
        if AKTIF_GRIDLER:
            detay = "\nAktif Gridler:\n"
            for sym, g in AKTIF_GRIDLER.items():
                temiz_sym = sym.replace('/USDT:USDT', '').replace(':USDT', '')
                detay += f"- {temiz_sym} : {g['yon']} | {len(g['kademeler'])} Emir\n"

        mesaj = (
            f"🤖 TREND FİLTRELİ GRID BOTU (10 Coin)\n\n"
            f"💰 Kasa: {total:.2f} USDT (Serbest: {free:.2f})\n"
            f"📊 Aktif Grid Sayısı: {len(AKTIF_GRIDLER)}/{MODLAR[AKTIF_MOD]['maks_aktif_grid']}\n"
            f"{detay}"
        )
        await update.message.reply_text(mesaj)
    except Exception as e:
        await update.message.reply_text(f"Hata: {e}")

async def baslat_komutu(update, context):
    global BOT_CALISIYOR_MU
    BOT_CALISIYOR_MU = True
    await update.message.reply_text("▶️ Grid Bot Aktif!")

async def durdur_komutu(update, context):
    global BOT_CALISIYOR_MU
    BOT_CALISIYOR_MU = False
    await update.message.reply_text("⏸️ Durduruldu.")

async def kapat_komutu(update, context):
    await update.message.reply_text("🛑 Tüm gridler iptal ediliyor...")
    try:
        for sym in list(AKTIF_GRIDLER.keys()):
            tum_emirleri_iptal_et(sym)
        AKTIF_GRIDLER.clear()
        hafizayi_kaydet()
        await update.message.reply_text("✅ Her şey temizlendi.")
    except Exception as e:
        await update.message.reply_text(f"⚠️ {e}")

# ==================== ANA DÖNGÜ ====================
def otomatik_arkaplan_tarayici():
    global BOT_CALISIYOR_MU
    print(f"🎯 [TREND GRID BOT] Başladı (10 Coin Taranıyor)", flush=True)

    try:
        exchange.load_markets()
    except Exception:
        pass

    dongu_sayaci = 0
    while True:
        try:
            if not BOT_CALISIYOR_MU:
                time.sleep(5); continue

            mod = mod_al()
            gunluk = gunluk_kontrol()
            if gunluk is not None and gunluk <= -mod['gunluk_max_kayip_pct']:
                telegram_mesaj_gonder(f"🛑 Günlük zarar limiti aşıldı! (%{gunluk*100:.1f})")
                BOT_CALISIYOR_MU = False
                continue

            # 1. Mevcut açık gridleri kontrol et
            for symbol in list(AKTIF_GRIDLER.keys()):
                df_raw = fetch_ohlcv_guvenli(symbol, ZAMAN_DILIMI, limit=100)
                if df_raw is not None:
                    df = pd.DataFrame(df_raw, columns=['timestamp','open','high','low','close','volume'])
                    adx_deger = adx_hesapla(df, 14).iloc[-1]
                    
                    if not pd.isna(adx_deger) and adx_deger > ADX_KAPATMA_ESIK:
                        telegram_mesaj_gonder(f"🚨 GRID KAPATILDI → {symbol}\nSebep: ADX {adx_deger:.1f} > {ADX_KAPATMA_ESIK}")
                        tum_emirleri_iptal_et(symbol)
                        AKTIF_GRIDLER.pop(symbol, None)
                        COIN_COOLDOWNLAR[symbol] = time.time() + 1800
                        hafizayi_kaydet()

            # 2. 10 coini tara ve uygun olanlara grid kur
            if len(AKTIF_GRIDLER) < mod['maks_aktif_grid']:
                su_an = time.time()
                debug_loglar = []
                for symbol in TAKIP_EDILENLER:
                    if symbol in AKTIF_GRIDLER: continue
                    if su_an < COIN_COOLDOWNLAR.get(symbol, 0): continue
                    if len(AKTIF_GRIDLER) >= mod['maks_aktif_grid']: break
                    
                    basarili, sebep = grid_kur(symbol)
                    debug_loglar.append(f"{symbol.split('/')[0]}:{'OK' if basarili else sebep}")
                    if basarili:
                        time.sleep(2)
                
                dongu_sayaci += 1
                if dongu_sayaci % 10 == 0:
                    print(f"🔍 Tarama #{dongu_sayaci} | {' | '.join(debug_loglar[:5])}", flush=True)

        except Exception as e:
            print(f"⚠️ Grid Döngü Hatası: {e}", flush=True)
        
        time.sleep(30)

def flask_web_server():
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)

# ==================== BAŞLAT ====================
if __name__ == '__main__':
    threading.Thread(target=otomatik_arkaplan_tarayici, daemon=True).start()
    threading.Thread(target=flask_web_server, daemon=True).start()

    app_tg = (
        ApplicationBuilder()
        .token(TELEGRAM_TOKEN)
        .read_timeout(30)
        .write_timeout(30)
        .connect_timeout(30)
        .pool_timeout(30)
        .build()
    )
    
    app_tg.add_handler(CommandHandler("durum", durum_komutu))
    app_tg.add_handler(CommandHandler("baslat", baslat_komutu))
    app_tg.add_handler(CommandHandler("durdur", durdur_komutu))
    app_tg.add_handler(CommandHandler("kapat", kapat_komutu))

    while True:
        try:
            app_tg.run_polling(drop_pending_updates=True)
        except Exception as e:
            print(f"⚠️ Telegram hatası: {e} — Yeniden deneniyor...", flush=True)
            time.sleep(5)

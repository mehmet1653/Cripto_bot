import os
import sys

os.environ['PYTHONUNBUFFERED'] = '1'
try:
    sys.stdout = open(sys.stdout.fileno(), mode='w', encoding='utf-8', buffering=1)
except Exception:
    sys.stdout.reconfigure(line_buffering=True)

import time
import threading
import asyncio
import requests
import ccxt
import pandas as pd
import ta
import numpy as np
from datetime import date, datetime
from dotenv import load_dotenv
from flask import Flask
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler
from supabase import create_client, Client

# ==================== .ENV ====================
if os.path.exists('/etc/secrets/.env'):
    load_dotenv('/etc/secrets/.env', override=True)
    print("✅ .env (secrets) yüklendi", flush=True)
else:
    load_dotenv(override=True)
    print("✅ .env yüklendi", flush=True)

# ==================== RENDER WEB ====================
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot aktif!"

def run_web():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

# ==================== API ====================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.environ.get("CHAT_ID", "").strip()
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "").strip()
GATE_API_KEY = os.environ.get("GATE_API_KEY", "").strip()
GATE_SECRET = os.environ.get("GATE_SECRET", "").strip()

if not TELEGRAM_TOKEN or not CHAT_ID: sys.exit(1)
if not SUPABASE_URL or not SUPABASE_KEY: sys.exit(1)
if not GATE_API_KEY or not GATE_SECRET: sys.exit(1)

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

exchange = ccxt.gate({
    'apiKey': GATE_API_KEY,
    'secret': GATE_SECRET,
    'enableRateLimit': True,
    'timeout': 30000,
    'options': {'defaultType': 'swap'}
})
exchange.set_sandbox_mode(True)

# ==================== AYARLAR ====================
TAKIP_EDILENLER = [
    'SOL/USDT:USDT', 
    'XRP/USDT:USDT', 
    'DOGE/USDT:USDT', 
    'LTC/USDT:USDT', 
    'LINK/USDT:USDT'
]

BOT_CALISIYOR_MU = True
state_lock = threading.Lock()
GLOBAL_COOLDOWN_BITIS = 0.0
SON_BTC_YONU = "YATAY (Testere)"

# Histerezis
SON_REJIM = "YATAY"
SON_REJIM_ZAMANI = time.time()
REJIM_MIN_SURE = 300

# Trend modu
KALDIRAC_TREND = 5
MAKS_POZISYON_TREND = 2
COOLDOWN_TREND_SANIYE = 30 * 60

# Grid modu (v10.15)
KALDIRAC_GRID = 3
MAKS_COIN_GRID = 3
GRID_SEVIYE_SAYISI = 3
GRID_MARJ_ORANI = 0.05
GRID_ATR_CARPAN = 5.0
GRID_ACIL_SL = 0.001
GRID_MAKS_MARJ_ORANI = 0.06
GRID_SL_COOLDOWN = 15 * 60
GRID_MIN_FIYAT = 10.0

# ✅ YENİ: Coin bazlı trend kırılımı
GRID_ICI_TREND_MAKS = 2          # Grid modunda max trend işlemi
GRID_ICI_TREND_MARJ = 0.20       # Grid içi trend için kasa oranı (%20)
GRID_ICI_TREND_RSI_UZUN = 70     # LONG için maks RSI
GRID_ICI_TREND_RSI_KISA = 30     # SHORT için min RSI

# Toplam zarar limiti
TOPLAM_ZARAR_LIMIT = 0.8

# Trend kırılım koruması
TREND_KIRILIM_BEKLEME = 3
TREND_KIRILIM_COOLDOWN = 5 * 60

# Risk
GUNLUK_BASLANGIC_BAKIYE = None
GUNLUK_ZARAR_LIMIT = 0.05
AKTIF_MOD = "YATAY"

SKIP_UYARI_ZAMANLARI = {}
TREND_SAYACI = {}

def hafizayi_yukle():
    print("💾 Hafıza yükleniyor...", flush=True)
    try:
        response = supabase.table("bot_hafiza").select("*").eq("id", 1).execute()
        if response.data and len(response.data) > 0:
            veri = response.data[0]
            print("💾 Hafıza yüklendi.", flush=True)
            return {
                "aktif_sistemler": veri.get("aktif_sistemler", {}),
                "analitik": veri.get("analitik", {"basarili_islem_sayisi": 0, "basarisiz_islem_sayisi": 0, "egitim_verileri": []}),
                "cooldownlar": veri.get("cooldownlar", {}),
                "grid_haritalari": veri.get("grid_haritalari", {})
            }
    except Exception as e:
        print(f"⚠️ Hafıza: {e}", flush=True)
    
    return {
        "aktif_sistemler": {},
        "analitik": {"basarili_islem_sayisi": 0, "basarisiz_islem_sayisi": 0, "egitim_verileri": []},
        "cooldownlar": {},
        "grid_haritalari": {}
    }

def hafizayi_kaydet():
    with state_lock:
        try:
            payload = {
                "basarili_islem_sayisi": int(ANALITIK.get("basarili_islem_sayisi", 0)),
                "basarisiz_islem_sayisi": int(ANALITIK.get("basarisiz_islem_sayisi", 0)),
                "egitim_verileri": ANALITIK.get("egitim_verileri", [])
            }
            clean_cd = {}
            for k, v in COIN_COOLDOWNLAR.items():
                if isinstance(v, dict):
                    clean_cd[k] = {"zaman": float(v.get("zaman", 0)), "son_yon": str(v.get("son_yon", ""))}
                else:
                    clean_cd[k] = {"zaman": float(v), "son_yon": ""}
            
            supabase.table("bot_hafiza").upsert({
                "id": 1,
                "aktif_sistemler": AKTIF_SISTEMLER,
                "analitik": payload,
                "cooldownlar": clean_cd,
                "grid_haritalari": GRID_HARITALARI
            }).execute()
        except Exception as e:
            print(f"⚠️ Kayıt: {e}", flush=True)

kalici = hafizayi_yukle()
AKTIF_SISTEMLER = kalici.get("aktif_sistemler", {})
ANALITIK = kalici.get("analitik", {"basarili_islem_sayisi": 0, "basarisiz_islem_sayisi": 0, "egitim_verileri": []})
COIN_COOLDOWNLAR = kalici.get("cooldownlar", {})
GRID_HARITALARI = kalici.get("grid_haritalari", {})

# ==================== PİYASA REJİMİ ====================
def piyasa_rejimini_tespit_et():
    global SON_BTC_YONU, AKTIF_MOD
    
    try:
        ohlcv = exchange.fetch_ohlcv('BTC/USDT:USDT', timeframe='1h', limit=40)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        
        adx = ta.trend.ADXIndicator(df['high'], df['low'], df['close'], window=14).adx().iloc[-1]
        
        bb = ta.volatility.BollingerBands(close=df['close'], window=20, window_dev=2)
        bb_high = bb.bollinger_hband().iloc[-1]
        bb_low = bb.bollinger_lband().iloc[-1]
        bb_mid = bb.bollinger_mavg().iloc[-1]
        bb_bw = (bb_high - bb_low) / bb_mid
        
        ema9 = ta.trend.ema_indicator(df['close'], window=9).iloc[-1]
        ema21 = ta.trend.ema_indicator(df['close'], window=21).iloc[-1]
        fark = (abs(ema9 - ema21) / ema21) * 100
        
        if AKTIF_MOD == "YATAY":
            if adx > 38.0 and bb_bw > 0.045 and fark > 0.4:
                rejim = "TREND"
                yon = "LONG" if ema9 > ema21 else "SHORT"
                SON_BTC_YONU = yon
            else:
                rejim = "YATAY"
                yon = "YATAY (Testere)"
        else:
            if adx < 30.0 or bb_bw < 0.035 or fark < 0.2:
                rejim = "YATAY"
                yon = "YATAY (Testere)"
            else:
                rejim = "TREND"
                yon = "LONG" if ema9 > ema21 else "SHORT"
                SON_BTC_YONU = yon
        
        print(f"🌐 [REJİM] {rejim} | ADX: {adx:.1f} | BB: {bb_bw:.4f} | Fark: %{fark:.2f}", flush=True)
        return rejim, yon
    except Exception as e:
        print(f"⚠️ Rejim: {e}", flush=True)
        return AKTIF_MOD, "YATAY (Testere)"

def piyasa_meyil_kontrol(symbol, df):
    try:
        closes = df['close']
        ema9 = ta.trend.ema_indicator(closes, window=9).iloc[-1]
        ema21 = ta.trend.ema_indicator(closes, window=21).iloc[-1]
        ema_fark = ((ema9 - ema21) / ema21) * 100
        
        son10_ort = closes.iloc[-10:].mean()
        onceki10_ort = closes.iloc[-20:-10].mean()
        momentum_fark = ((son10_ort - onceki10_ort) / onceki10_ort) * 100
        
        rsi = ta.momentum.rsi(closes, window=14).iloc[-1]
        
        if abs(ema_fark) < 0.2 and 42 < rsi < 58:
            return "YATAY", 0
        elif ema_fark > 0.5 and momentum_fark > 0.3:
            return "GUCLU_YUKARI", ema_fark
        elif ema_fark < -0.5 and momentum_fark < -0.3:
            return "GUCLU_ASAGI", ema_fark
        elif ema_fark > 0.2:
            return "HAFIF_YUKARI", ema_fark
        elif ema_fark < -0.2:
            return "HAFIF_ASAGI", ema_fark
        else:
            return "YATAY", 0
    except Exception as e:
        return "YATAY", 0

def kisa_vadeli_trend(df):
    try:
        closes = df['close'].values
        son5 = closes[-5:]
        
        artan = sum(1 for i in range(1, len(son5)) if son5[i] > son5[i-1])
        azalan = sum(1 for i in range(1, len(son5)) if son5[i] < son5[i-1])
        
        ema9 = ta.trend.ema_indicator(pd.Series(closes), window=9).iloc[-1]
        ema21 = ta.trend.ema_indicator(pd.Series(closes), window=21).iloc[-1]
        ema_fark = ((ema9 - ema21) / ema21) * 100
        
        if artan >= 5 and ema_fark > 0.2:
            return "YUKARI"
        elif azalan >= 5 and ema_fark < -0.2:
            return "ASAGI"
        else:
            return "YATAY"
    except:
        return "YATAY"

def trend_kirilim_hesapla(symbol, yeni_trend):
    global TREND_SAYACI
    onceki = TREND_SAYACI.get(symbol, {})
    onceki_trend = onceki.get("trend", "")
    sayac = onceki.get("sayac", 0)
    
    if onceki_trend == yeni_trend:
        sayac += 1
    else:
        sayac = 1
    
    TREND_SAYACI[symbol] = {"trend": yeni_trend, "sayac": sayac}
    return sayac

# ==================== YARDIMCI ====================
def telegram_gonder(mesaj):
    if not TELEGRAM_TOKEN or not CHAT_ID: return
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                      json={"chat_id": CHAT_ID, "text": mesaj, "parse_mode": "Markdown"}, timeout=5)
    except: pass

def market_kapat(symbol, miktar, yon):
    kapat_yon = 'sell' if yon == 'LONG' else 'buy'
    try:
        exchange.create_order(symbol, 'market', kapat_yon, miktar, None, {'reduceOnly': True})
        return True
    except Exception as e:
        print(f"⚠️ Kapatma {symbol}: {e}", flush=True)
        return False

def tum_emirleri_iptal(symbol):
    try:
        orders = exchange.fetch_open_orders(symbol)
        for o in orders:
            try: exchange.cancel_order(o['id'], symbol)
            except: pass
    except: pass

def sayaci_artir(basarili_mi):
    with state_lock:
        if basarili_mi:
            ANALITIK["basarili_islem_sayisi"] = int(ANALITIK.get("basarili_islem_sayisi", 0)) + 1
        else:
            ANALITIK["basarisiz_islem_sayisi"] = int(ANALITIK.get("basarisiz_islem_sayisi", 0)) + 1
    hafizayi_kaydet()

def trend_pozisyonlarini_kapat(aktif_semboller_seti):
    global AKTIF_SISTEMLER
    kapatilan = []
    for symbol in list(AKTIF_SISTEMLER.keys()):
        try:
            positions = exchange.fetch_positions([symbol])
            for p in positions:
                k = float(p.get('contracts', 0) or p.get('size', 0) or 0)
                if k > 0:
                    yon = str(p.get('side', '')).upper() or "LONG"
                    tum_emirleri_iptal(symbol)
                    if market_kapat(symbol, k, yon):
                        kapatilan.append(symbol)
                        print(f"  🧹 [MOD GEÇİŞ] {symbol} {yon} kapatıldı", flush=True)
                        telegram_gonder(f"🧹 *MOD GEÇİŞ*\n`{symbol}` {yon} kapatıldı")
            
            with state_lock:
                if symbol in AKTIF_SISTEMLER: del AKTIF_SISTEMLER[symbol]
            aktif_semboller_seti.discard(symbol)
        except Exception as e:
            print(f"  ⚠️ Kapatma {symbol}: {e}", flush=True)
    
    if kapatilan:
        hafizayi_kaydet()
    return kapatilan

def baslangic_temizligi():
    print("\n🧹 [BAŞLANGIÇ TEMİZLİĞİ] Eski pozisyonlar kontrol ediliyor...", flush=True)
    try:
        raw_pos = exchange.fetch_positions()
        eski_pozisyonlar = []
        for p in raw_pos:
            k = float(p.get('contracts', 0) or p.get('size', 0) or 0)
            if k > 0:
                eski_pozisyonlar.append(p['symbol'])
        
        if eski_pozisyonlar:
            print(f"⚠️ Eski pozisyonlar bulundu: {eski_pozisyonlar}", flush=True)
            telegram_gonder(f"⚠️ *BAŞLANGIÇ TEMİZLİĞİ*\nEski pozisyonlar: `{eski_pozisyonlar}`\nKapatılıyor...")
            
            kapatilan = 0
            for p in raw_pos:
                k = float(p.get('contracts', 0) or p.get('size', 0) or 0)
                if k > 0:
                    sym = p['symbol']
                    yon = str(p.get('side', '')).upper() or "LONG"
                    try: exchange.cancel_all_orders(sym)
                    except: pass
                    if market_kapat(sym, k, yon):
                        print(f"  ✅ {sym} {yon} kapatıldı", flush=True)
                        kapatilan += 1
            
            with state_lock:
                AKTIF_SISTEMLER.clear()
                GRID_HARITALARI.clear()
            hafizayi_kaydet()
            telegram_gonder(f"✅ *TEMİZ BAŞLANGIÇ*\n{kapatilan} pozisyon kapatıldı.")
            print(f"✅ {kapatilan} pozisyon kapatıldı, temiz başlangıç.", flush=True)
        else:
            print("✅ Eski pozisyon yok, temiz başlangıç.", flush=True)
    except Exception as e:
        print(f"⚠️ Başlangıç temizliği hatası: {e}", flush=True)

# ==================== GRID İÇİ TREND İŞLEMİ ====================
def grid_ici_trend_isle(symbol, anlik, df, meyil, fark, aktif_semboller_seti):
    """Grid modunda güçlü trend olan coinde trend işlemi açar"""
    global AKTIF_SISTEMLER
    
    # Zaten trend işlemi sayısı kontrolü
    aktif_trend_sayisi = sum(1 for s in AKTIF_SISTEMLER.keys() 
                             if s in aktif_semboller_seti and s not in GRID_HARITALARI)
    if aktif_trend_sayisi >= GRID_ICI_TREND_MAKS:
        print(f"  ⛔ Grid içi trend limit dolu ({aktif_trend_sayisi}/{GRID_ICI_TREND_MAKS})", flush=True)
        return False
    
    # RSI kontrolü
    rsi = ta.momentum.rsi(df['close'], window=14).iloc[-1]
    
    if meyil == "GUCLU_YUKARI":
        islem_yonu = "LONG"
        if rsi >= GRID_ICI_TREND_RSI_UZUN:
            print(f"  ⏭️ {symbol}: {meyil} ama RSI aşırı alım ({rsi:.1f} >= {GRID_ICI_TREND_RSI_UZUN})", flush=True)
            return False
    else:  # GUCLU_ASAGI
        islem_yonu = "SHORT"
        if rsi <= GRID_ICI_TREND_RSI_KISA:
            print(f"  ⏭️ {symbol}: {meyil} ama RSI aşırı satım ({rsi:.1f} <= {GRID_ICI_TREND_RSI_KISA})", flush=True)
            return False
    
    print(f"  🎯 [GRID İÇİ TREND] {symbol}: {meyil} (%{fark:.2f}) | RSI: {rsi:.1f}", flush=True)
    
    try:
        bakiye = exchange.fetch_balance()
        toplam_bakiye = float(bakiye['total'].get('USDT', 0))
        serbest_bakiye = float(bakiye.get('free', {}).get('USDT', 0) or 0)
        
        exchange.set_leverage(KALDIRAC_TREND, symbol)
        market = exchange.market(symbol)
        
        kullan = min(toplam_bakiye * GRID_ICI_TREND_MARJ, serbest_bakiye)
        if kullan < 1.0:
            return False
        
        tp, sl, kapat_yon, roe = akilli_seviye_hesapla(anlik, islem_yonu, df)
        
        miktar = float(exchange.amount_to_precision(
            symbol,
            max((kullan * KALDIRAC_TREND) / anlik / float(market.get('contractSize', 1.0)),
                float(market['limits']['amount']['min'] or 1.0))
        ))
        
        islem_y = 'buy' if islem_yonu == 'LONG' else 'sell'
        exchange.create_order(symbol, 'market', islem_y, miktar)
        time.sleep(1.0)
        
        sl_ok = False
        try:
            exchange.create_order(symbol, 'limit', kapat_yon, miktar, tp, {'reduceOnly': True})
        except: pass
        try:
            exchange.create_order(symbol, 'stop', kapat_yon, miktar, sl, {'stopPrice': sl, 'reduceOnly': True})
            sl_ok = True
        except Exception as e:
            print(f"  🚨 SL KOYULAMADI: {e}", flush=True)
        
        if not sl_ok:
            market_kapat(symbol, miktar, islem_yonu)
            return False
        
        with state_lock:
            AKTIF_SISTEMLER[symbol] = {
                "giris_fiyati": anlik, "yon": islem_yonu,
                "giris_rsi": float(rsi), "giris_zamani": time.time(),
                "tip": "GRID_ICI_TREND"
            }
            aktif_semboller_seti.add(symbol)
        
        hafizayi_kaydet()
        print(f"  ✅ [GRID İÇİ TREND AÇILDI] {symbol} {islem_yonu} @ {anlik}", flush=True)
        telegram_gonder(
            f"🎯 *GRID İÇİ TREND*\n"
            f"📌 `{symbol}` | {islem_yonu}\n"
            f"📊 {meyil} (%{fark:.2f}) | RSI: {rsi:.1f}\n"
            f"🎯 Giriş: `{anlik}`\n"
            f"💰 TP: `{tp}` | 🛑 SL: `{sl}`"
        )
        return True
    except Exception as e:
        print(f"  ⚠️ Grid içi trend {symbol}: {e}", flush=True)
        return False

# ==================== GRID MODU ====================
def grid_haritasi_olustur(symbol, anlik_fiyat, atr):
    grid_aralik = atr * GRID_ATR_CARPAN
    alt_sinir = anlik_fiyat - grid_aralik
    ust_sinir = anlik_fiyat + grid_aralik
    adim = grid_aralik / GRID_SEVIYE_SAYISI
    
    alis_seviyeleri = []
    satis_seviyeleri = []
    
    for i in range(1, GRID_SEVIYE_SAYISI + 1):
        alis_seviyeleri.append(round(anlik_fiyat - (adim * i), 6))
        satis_seviyeleri.append(round(anlik_fiyat + (adim * i), 6))
    
    return {
        "symbol": symbol, "merkez": anlik_fiyat,
        "alt_sinir": alt_sinir, "ust_sinir": ust_sinir, "adim": adim,
        "alis_seviyeleri": alis_seviyeleri,
        "satis_seviyeleri": satis_seviyeleri,
        "aktif_pozisyonlar": {},
        "olusturma_zamani": time.time(),
        "meyil": "YATAY"
    }

def grid_icin_uygun_mu(symbol, anlik_fiyat, toplam_bakiye):
    try:
        if anlik_fiyat < GRID_MIN_FIYAT:
            return False, 0
        
        market = exchange.market(symbol)
        min_miktar = float(market['limits']['amount']['min'] or 0.001)
        contract_size = float(market.get('contractSize', 1.0))
        min_marj = (min_miktar * anlik_fiyat * contract_size) / KALDIRAC_GRID
        if min_marj > toplam_bakiye * GRID_MAKS_MARJ_ORANI:
            return False, min_marj
        return True, min_marj
    except Exception as e:
        return False, 0

def grid_modu_calistir(aktif_borsa_map, aktif_semboller_seti):
    global AKTIF_MOD
    
    if AKTIF_MOD != "YATAY":
        print(f"🔄 [MOD GEÇİŞ] {AKTIF_MOD} → YATAY. Trend pozisyonları kapatılıyor...", flush=True)
        trend_pozisyonlarini_kapat(aktif_semboller_seti)
        grid_temizle()
        AKTIF_MOD = "YATAY"
        telegram_gonder("🔄 *MOD: YATAY (Grid)*\n✅ Trend pozisyonları kapatıldı.")
    
    print(f"\n🟦 [GRID MODU] Aktif coin: {list(GRID_HARITALARI.keys())}", flush=True)
    
    try:
        bakiye = exchange.fetch_balance()
        toplam_b = float(bakiye['total'].get('USDT', 0))
    except:
        toplam_b = 100
    
    # ✅ v10.15: Grid haritası oluştur + güçlü trendlerde trend işlemi aç
    for symbol in TAKIP_EDILENLER:
        if symbol in aktif_semboller_seti: continue
        if symbol in GRID_HARITALARI: continue
        if len(GRID_HARITALARI) >= MAKS_COIN_GRID: 
            # Grid limiti doldu ama trend işlemi kontrolüne devam et
            pass
        
        with state_lock:
            cd = COIN_COOLDOWNLAR.get(symbol)
            if cd:
                z = cd.get("zaman", 0) if isinstance(cd, dict) else float(cd)
                if z > time.time():
                    kalan = int((z - time.time()) / 60)
                    print(f"  ⏳ [GRID SKIP] {symbol}: Cooldown ({kalan} dk kaldı)", flush=True)
                    continue
        
        try:
            ticker = exchange.fetch_ticker(symbol)
            anlik = float(ticker['last'])
            
            uygun, min_marj = grid_icin_uygun_mu(symbol, anlik, toplam_b)
            if not uygun:
                if anlik < GRID_MIN_FIYAT:
                    print(f"  ⏭️ [GRID SKIP] {symbol}: Fiyat düşük ({anlik:.2f} < {GRID_MIN_FIYAT})", flush=True)
                else:
                    print(f"  ⏭️ [GRID SKIP] {symbol}: Min miktar marjı yüksek ({min_marj:.2f} USDT)", flush=True)
                continue
            
            ohlcv = exchange.fetch_ohlcv(symbol, timeframe='15m', limit=50)
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            
            meyil, fark = piyasa_meyil_kontrol(symbol, df)
            
            # ✅ v10.15: GÜÇLÜ TREND İSE TREND İŞLEMİ AÇ
            if meyil == "GUCLU_YUKARI" or meyil == "GUCLU_ASAGI":
                sonuc = grid_ici_trend_isle(symbol, anlik, df, meyil, fark, aktif_semboller_seti)
                if sonuc:
                    continue  # Trend işlemi açıldı, grid kurmaya gerek yok
                else:
                    # Trend işlemi açılamadı, grid de kurma (güçlü trend)
                    continue
            
            # YATAY veya HAFIF meyilli ise grid kur
            # Grid limiti kontrolü
            if len(GRID_HARITALARI) >= MAKS_COIN_GRID:
                continue
            
            atr = ta.volatility.AverageTrueRange(df['high'], df['low'], df['close'], window=14).average_true_range().iloc[-1]
            
            grid = grid_haritasi_olustur(symbol, anlik, atr)
            
            if meyil == "HAFIF_YUKARI":
                kaydirma = 1.002
                grid['merkez'] = anlik * kaydirma
                grid['alis_seviyeleri'] = [s * kaydirma for s in grid['alis_seviyeleri']]
                grid['satis_seviyeleri'] = [s * kaydirma for s in grid['satis_seviyeleri']]
                grid['alt_sinir'] = grid['alt_sinir'] * kaydirma
                grid['ust_sinir'] = grid['ust_sinir'] * kaydirma
                grid['meyil'] = "HAFIF_YUKARI"
                print(f"  📐 [GRID KURULDU-↗️] {symbol} | Merkez: {grid['merkez']:.4f}", flush=True)
            elif meyil == "HAFIF_ASAGI":
                kaydirma = 0.998
                grid['merkez'] = anlik * kaydirma
                grid['alis_seviyeleri'] = [s * kaydirma for s in grid['alis_seviyeleri']]
                grid['satis_seviyeleri'] = [s * kaydirma for s in grid['satis_seviyeleri']]
                grid['alt_sinir'] = grid['alt_sinir'] * kaydirma
                grid['ust_sinir'] = grid['ust_sinir'] * kaydirma
                grid['meyil'] = "HAFIF_ASAGI"
                print(f"  📐 [GRID KURULDU-↘️] {symbol} | Merkez: {grid['merkez']:.4f}", flush=True)
            else:
                grid['meyil'] = "YATAY"
                print(f"  📐 [GRID KURULDU-➡️] {symbol} | Merkez: {anlik}", flush=True)
            
            GRID_HARITALARI[symbol] = grid
            telegram_gonder(
                f"📐 *GRID KURULDU*\n📌 `{symbol}`\n"
                f"🎯 Merkez: `{grid['merkez']:.4f}`\n"
                f"📊 Aralık: `{grid['alt_sinir']:.4f}` - `{grid['ust_sinir']:.4f}`\n"
                f"📈 Meyil: `{meyil}`"
            )
            hafizayi_kaydet()
        except Exception as e:
            print(f"  ⚠️ Grid oluşturma {symbol}: {e}", flush=True)
            continue
    
    # Grid güncelle
    for symbol in list(GRID_HARITALARI.keys()):
        if not BOT_CALISIYOR_MU: break
        
        grid = GRID_HARITALARI[symbol]
        try:
            ticker = exchange.fetch_ticker(symbol)
            anlik = float(ticker['last'])
        except: continue
        
        try:
            ohlcv_t = exchange.fetch_ohlcv(symbol, timeframe='5m', limit=30)
            df_t = pd.DataFrame(ohlcv_t, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            trend = kisa_vadeli_trend(df_t)
        except:
            trend = "YATAY"
        
        trend_sayac = trend_kirilim_hesapla(symbol, trend)
        
        print(f"  📊 {symbol.replace('/USDT:USDT','')} | Fiyat: {anlik:.6f} | Trend: {trend} ({trend_sayac}/{TREND_KIRILIM_BEKLEME}) | Aktif: {len(grid['aktif_pozisyonlar'])}/{GRID_SEVIYE_SAYISI*2}", flush=True)
        
        # Trend kırılım koruması
        for i in list(grid['aktif_pozisyonlar'].keys()):
            pozisyon = grid['aktif_pozisyonlar'][i]
            yon = pozisyon['yon']
            
            ters_dondu = False
            if yon == "SHORT" and trend == "YUKARI":
                ters_dondu = True
            elif yon == "LONG" and trend == "ASAGI":
                ters_dondu = True
            
            if ters_dondu:
                if trend_sayac < TREND_KIRILIM_BEKLEME:
                    print(f"  ⏳ [BEKLEME] {symbol} #{i+1}: Trend {trend} ({trend_sayac}/{TREND_KIRILIM_BEKLEME})", flush=True)
                    continue
                
                with state_lock:
                    cd = COIN_COOLDOWNLAR.get(symbol)
                    if cd:
                        z = cd.get("zaman", 0) if isinstance(cd, dict) else float(cd)
                        if z > time.time():
                            continue
                
                try:
                    if yon == "SHORT":
                        kar = (pozisyon['fiyat'] - anlik) * pozisyon['miktar']
                    else:
                        kar = (anlik - pozisyon['fiyat']) * pozisyon['miktar']
                    
                    market_kapat(symbol, pozisyon['miktar'], yon)
                    print(f"  🚨 [TREND KIRILIM] {symbol} #{i+1} {yon} kapatıldı! Trend: {trend} ({trend_sayac}x) | PnL: {kar:+.4f}", flush=True)
                    telegram_gonder(
                        f"🚨 *TREND KIRILIM*\n"
                        f"📌 `{symbol}` #{i+1}\n"
                        f"Yön: `{yon}`\n"
                        f"Trend: `{trend}` ({trend_sayac}x onay)\n"
                        f"PnL: `{kar:+.4f}` USDT"
                    )
                    del grid['aktif_pozisyonlar'][i]
                    sayaci_artir(kar > 0)
                    
                    with state_lock:
                        COIN_COOLDOWNLAR[symbol] = {"zaman": float(time.time() + TREND_KIRILIM_COOLDOWN), "son_yon": "TREND_KIRILIM"}
                    print(f"  ⏳ [COOLDOWN] {symbol}: {int(TREND_KIRILIM_COOLDOWN/60)} dk", flush=True)
                    
                    hafizayi_kaydet()
                except Exception as e:
                    print(f"  ⚠️ Trend kırılım kapatma {symbol}: {e}", flush=True)
        
        # ANİ KIRILIM + GRID SL KONTROLÜ
        ani_kirilim = anlik < grid['alt_sinir'] * 0.997 or anlik > grid['ust_sinir'] * 1.003
        normal_sl = anlik < grid['alt_sinir'] * (1 - GRID_ACIL_SL) or anlik > grid['ust_sinir'] * (1 + GRID_ACIL_SL)
        
        if ani_kirilim or normal_sl:
            sebep = "ANİ KIRILIM" if ani_kirilim else "GRID SINIRI KIRILDI"
            print(f"  🚨 [{sebep}] {symbol}!", flush=True)
            grid_pozisyonlari_kapat(symbol, sebep, basarili=False)
            if symbol in GRID_HARITALARI: del GRID_HARITALARI[symbol]
            hafizayi_kaydet()
            continue
        
        # AL seviyeleri
        for i, seviye_fiyat in enumerate(grid['alis_seviyeleri']):
            if i in grid['aktif_pozisyonlar']: continue
            if anlik <= seviye_fiyat:
                if trend == "ASAGI":
                    print(f"  ⏭️ {symbol} #{i+1}: Trend AŞAĞI, LONG açılmıyor", flush=True)
                    continue
                
                try:
                    bakiye = exchange.fetch_balance()
                    toplam_b = float(bakiye['total'].get('USDT', 0))
                    serbest = float(bakiye.get('free', {}).get('USDT', 0) or 0)
                    
                    hedef_marj = toplam_b * GRID_MARJ_ORANI
                    marj = min(hedef_marj, serbest)
                    if marj < 1.0: continue
                    
                    exchange.set_leverage(KALDIRAC_GRID, symbol)
                    market = exchange.market(symbol)
                    contract_size = float(market.get('contractSize', 1.0))
                    
                    hesaplanan_miktar = (marj * KALDIRAC_GRID) / anlik / contract_size
                    min_miktar = float(market['limits']['amount']['min'] or 0.001)
                    
                    if hesaplanan_miktar < min_miktar:
                        gercek_marj = (min_miktar * anlik * contract_size) / KALDIRAC_GRID
                        if gercek_marj > toplam_b * GRID_MAKS_MARJ_ORANI:
                            print(f"  ⏭️ {symbol} #{i+1}: Min miktar marjı yüksek ({gercek_marj:.2f}), atlanıyor", flush=True)
                            continue
                    
                    miktar = float(exchange.amount_to_precision(
                        symbol,
                        max(hesaplanan_miktar, min_miktar)
                    ))
                    
                    gercek_pozisyon = miktar * anlik * contract_size
                    gercek_marj = gercek_pozisyon / KALDIRAC_GRID
                    
                    if gercek_marj > toplam_b * GRID_MAKS_MARJ_ORANI:
                        print(f"  ⏭️ {symbol} #{i+1}: Marj çok yüksek ({gercek_marj:.2f}), atlanıyor", flush=True)
                        continue
                    
                    print(f"  📊 {symbol} #{i+1}: Marj={gercek_marj:.2f} | Trend={trend} | LONG", flush=True)
                    
                    exchange.create_order(symbol, 'market', 'buy', miktar)
                    time.sleep(0.5)
                    
                    grid['aktif_pozisyonlar'][i] = {
                        "fiyat": anlik, "miktar": miktar, "yon": "LONG",
                        "hedef_satis": seviye_fiyat + grid['adim']
                    }
                    print(f"  🟢 [GRID AL] {symbol} #{i+1} @ {anlik}", flush=True)
                    hafizayi_kaydet()
                except Exception as e:
                    print(f"  ⚠️ Grid alış {symbol}: {e}", flush=True)
        
        # SAT seviyeleri
        for i, seviye_fiyat in enumerate(grid['satis_seviyeleri']):
            if i in grid['aktif_pozisyonlar']: continue
            if anlik >= seviye_fiyat:
                if trend == "YUKARI":
                    print(f"  ⏭️ {symbol} #{i+1}: Trend YUKARI, SHORT açılmıyor", flush=True)
                    continue
                
                try:
                    bakiye = exchange.fetch_balance()
                    toplam_b = float(bakiye['total'].get('USDT', 0))
                    serbest = float(bakiye.get('free', {}).get('USDT', 0) or 0)
                    
                    hedef_marj = toplam_b * GRID_MARJ_ORANI
                    marj = min(hedef_marj, serbest)
                    if marj < 1.0: continue
                    
                    exchange.set_leverage(KALDIRAC_GRID, symbol)
                    market = exchange.market(symbol)
                    contract_size = float(market.get('contractSize', 1.0))
                    
                    hesaplanan_miktar = (marj * KALDIRAC_GRID) / anlik / contract_size
                    min_miktar = float(market['limits']['amount']['min'] or 0.001)
                    
                    if hesaplanan_miktar < min_miktar:
                        gercek_marj = (min_miktar * anlik * contract_size) / KALDIRAC_GRID
                        if gercek_marj > toplam_b * GRID_MAKS_MARJ_ORANI:
                            print(f"  ⏭️ {symbol} #{i+1}: Min miktar marjı yüksek, atlanıyor", flush=True)
                            continue
                    
                    miktar = float(exchange.amount_to_precision(
                        symbol,
                        max(hesaplanan_miktar, min_miktar)
                    ))
                    
                    gercek_pozisyon = miktar * anlik * contract_size
                    gercek_marj = gercek_pozisyon / KALDIRAC_GRID
                    
                    if gercek_marj > toplam_b * GRID_MAKS_MARJ_ORANI:
                        print(f"  ⏭️ {symbol} #{i+1}: Marj çok yüksek, atlanıyor", flush=True)
                        continue
                    
                    print(f"  📊 {symbol} #{i+1}: Marj={gercek_marj:.2f} | Trend={trend} | SHORT", flush=True)
                    
                    exchange.create_order(symbol, 'market', 'sell', miktar)
                    time.sleep(0.5)
                    
                    grid['aktif_pozisyonlar'][i] = {
                        "fiyat": anlik, "miktar": miktar, "yon": "SHORT",
                        "hedef_satis": seviye_fiyat - grid['adim']
                    }
                    print(f"  🔴 [GRID SAT] {symbol} #{i+1} @ {anlik}", flush=True)
                    hafizayi_kaydet()
                except Exception as e:
                    print(f"  ⚠️ Grid satış {symbol}: {e}", flush=True)
        
        # Kâr alma
        for i in list(grid['aktif_pozisyonlar'].keys()):
            pozisyon = grid['aktif_pozisyonlar'][i]
            hedef = pozisyon['hedef_satis']
            yon = pozisyon['yon']
            
            if (yon == "LONG" and anlik >= hedef) or (yon == "SHORT" and anlik <= hedef):
                try:
                    market_kapat(symbol, pozisyon['miktar'], yon)
                    kar = abs(anlik - pozisyon['fiyat']) * pozisyon['miktar']
                    print(f"  💰 [GRID KÂR] {symbol} #{i+1} | +{kar:.4f} USDT", flush=True)
                    telegram_gonder(f"💰 *GRID KÂR*\n📌 `{symbol}` #{i+1}\n💵 +{kar:.4f} USDT")
                    del grid['aktif_pozisyonlar'][i]
                    sayaci_artir(True)
                except Exception as e:
                    print(f"  ⚠️ Grid kâr {symbol}: {e}", flush=True)

def grid_pozisyonlari_kapat(symbol, sebep, basarili=True):
    grid = GRID_HARITALARI.get(symbol)
    if not grid: return
    
    kapatilan = 0
    toplam_pnl = 0
    for i, pozisyon in list(grid['aktif_pozisyonlar'].items()):
        try:
            try:
                t = exchange.fetch_ticker(symbol)
                anlik = float(t['last'])
                if pozisyon['yon'] == "LONG":
                    kar = (anlik - pozisyon['fiyat']) * pozisyon['miktar']
                else:
                    kar = (pozisyon['fiyat'] - anlik) * pozisyon['miktar']
                toplam_pnl += kar
            except: kar = 0
            
            market_kapat(symbol, pozisyon['miktar'], pozisyon['yon'])
            print(f"  ❌ [GRID KAPAT] {symbol} #{i+1} | {sebep} | PnL: {kar:+.4f}", flush=True)
            kapatilan += 1
            sayaci_artir(basarili)
        except: pass
    
    if kapatilan > 0:
        telegram_gonder(
            f"❌ *GRID KAPAT - {sebep}*\n"
            f"📌 `{symbol}`\n"
            f"📊 Kapatılan: `{kapatilan}` seviye\n"
            f"💵 Toplam PnL: `{toplam_pnl:+.4f}` USDT"
        )
    
    if "SINIR" in sebep.upper() or "SL" in sebep.upper() or "KIRILIM" in sebep.upper():
        with state_lock:
            COIN_COOLDOWNLAR[symbol] = {"zaman": float(time.time() + GRID_SL_COOLDOWN), "son_yon": "GRID_SL"}
        print(f"  ⏳ [COOLDOWN] {symbol}: {int(GRID_SL_COOLDOWN/60)} dk", flush=True)
    
    grid['aktif_pozisyonlar'] = {}
    tum_emirleri_iptal(symbol)
    hafizayi_kaydet()

def grid_temizle():
    global GRID_HARITALARI
    for symbol in list(GRID_HARITALARI.keys()):
        grid_pozisyonlari_kapat(symbol, "MOD GEÇİŞİ", basarili=True)
    GRID_HARITALARI = {}
    hafizayi_kaydet()

# ==================== TREND MODU ====================
def akilli_seviye_hesapla(anlik, yon, df):
    atr = ta.volatility.AverageTrueRange(df['high'], df['low'], df['close'], window=14).average_true_range().iloc[-1]
    
    atr_yuzde = (atr / anlik) * 100
    if atr_yuzde > 1.5:
        tp_c = 5.0; sl_c = 3.5
    else:
        tp_c = 4.0; sl_c = 3.0
    
    if yon == 'LONG':
        tp = anlik + (atr * tp_c)
        sl = anlik - (atr * sl_c)
        kapat_yon = 'sell'
    else:
        tp = anlik - (atr * tp_c)
        sl = anlik + (atr * sl_c)
        kapat_yon = 'buy'
    
    roe = abs((tp - anlik) / anlik) * 100 * KALDIRAC_TREND
    return float(tp), float(sl), kapat_yon, float(roe)

def trend_modu_calistir(aktif_borsa_map, aktif_semboller_seti):
    global AKTIF_MOD
    
    if AKTIF_MOD != "TREND":
        print(f"🔄 [MOD GEÇİŞ] {AKTIF_MOD} → TREND. Grid temizleniyor...", flush=True)
        grid_temizle()
        AKTIF_MOD = "TREND"
        telegram_gonder("🔄 *MOD: TREND*")
    
    rejim, btc_yonu = piyasa_rejimini_tespit_et()
    print(f"\n🟥 [TREND MODU] BTC: {btc_yonu} | Açık: {len(aktif_semboller_seti)}/{MAKS_POZISYON_TREND}", flush=True)
    
    try:
        anlik_aktif = list(aktif_semboller_seti)
        for eski in list(AKTIF_SISTEMLER.keys()):
            if eski not in anlik_aktif:
                bilgi = AKTIF_SISTEMLER[eski]
                g = float(bilgi.get("giris_fiyati", 0))
                y = bilgi.get("yon", "LONG")
                
                karli = False
                try:
                    t = exchange.fetch_ticker(eski)
                    cikis = float(t['last'])
                    karli = (cikis > g) if y == "LONG" else (cikis < g)
                except: karli = True
                
                sayaci_artir(karli)
                
                with state_lock:
                    COIN_COOLDOWNLAR[eski] = {"zaman": float(time.time() + COOLDOWN_TREND_SANIYE), "son_yon": y}
                    if eski in AKTIF_SISTEMLER: del AKTIF_SISTEMLER[eski]
                
                hafizayi_kaydet()
                tip = "✅ *KÂRLA KAPANDI*" if karli else "❌ *ZARARLA KAPANDI*"
                print(f"  {tip} {eski}", flush=True)
                telegram_gonder(f"{tip}\n📌 `{eski}`")
    except Exception as e:
        print(f"⚠️ Kapanış: {e}", flush=True)
    
    if len(aktif_semboller_seti) >= MAKS_POZISYON_TREND:
        print(f"  ⛔ Trend limit dolu", flush=True)
        return
    
    for symbol in TAKIP_EDILENLER:
        if not BOT_CALISIYOR_MU: break
        if symbol in aktif_semboller_seti: continue
        if len(aktif_semboller_seti) >= MAKS_POZISYON_TREND: break
        
        with state_lock:
            cd = COIN_COOLDOWNLAR.get(symbol)
            if cd:
                z = cd.get("zaman", 0) if isinstance(cd, dict) else float(cd)
                if z > time.time(): continue
        
        try:
            ticker = exchange.fetch_ticker(symbol)
            anlik = float(ticker['last'])
            ohlcv = exchange.fetch_ohlcv(symbol, timeframe='15m', limit=50)
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            rsi = ta.momentum.rsi(df['close'], window=14).iloc[-1]
            
            son_h = df['volume'].iloc[-3:].mean()
            ort_h = df['volume'].iloc[-20:].mean()
            hacim = son_h / ort_h if ort_h > 0 else 0
            
            if btc_yonu == "LONG" and rsi < 50:
                islem_yonu = "LONG"
            elif btc_yonu == "SHORT" and rsi > 50:
                islem_yonu = "SHORT"
            else:
                print(f"  ⚪ {symbol}: RSI={rsi:.1f} (sinyal yok)", flush=True)
                continue
            
            if hacim < 0.6: 
                print(f"  ⚠️ {symbol}: Hacim düşük ({hacim:.2f}x)", flush=True)
                continue
            
            print(f"  🟢 {symbol}: SİNYAL {islem_yonu} | RSI: {rsi:.1f}", flush=True)
            
            bakiye = exchange.fetch_balance()
            toplam_b = float(bakiye['total'].get('USDT', 0))
            serbest_b = float(bakiye.get('free', {}).get('USDT', 0) or 0)
            
            exchange.set_leverage(KALDIRAC_TREND, symbol)
            market = exchange.market(symbol)
            
            kullan = min(toplam_b * 0.25, serbest_b)
            if kullan < 1.0: continue
            
            tp, sl, kapat_yon, roe = akilli_seviye_hesapla(anlik, islem_yonu, df)
            
            miktar = float(exchange.amount_to_precision(
                symbol,
                max((kullan * KALDIRAC_TREND) / anlik / float(market.get('contractSize', 1.0)),
                    float(market['limits']['amount']['min'] or 1.0))
            ))
            
            islem_y = 'buy' if islem_yonu == 'LONG' else 'sell'
            exchange.create_order(symbol, 'market', islem_y, miktar)
            time.sleep(1.0)
            
            sl_ok = False
            try:
                exchange.create_order(symbol, 'limit', kapat_yon, miktar, tp, {'reduceOnly': True})
            except: pass
            try:
                exchange.create_order(symbol, 'stop', kapat_yon, miktar, sl, {'stopPrice': sl, 'reduceOnly': True})
                sl_ok = True
            except Exception as e:
                print(f"  🚨 SL KOYULAMADI: {e}", flush=True)
            
            if not sl_ok:
                market_kapat(symbol, miktar, islem_yonu)
                continue
            
            with state_lock:
                AKTIF_SISTEMLER[symbol] = {
                    "giris_fiyati": anlik, "yon": islem_yonu,
                    "giris_rsi": float(rsi), "giris_zamani": time.time(),
                    "tip": "TREND"
                }
                aktif_semboller_seti.add(symbol)
            
            hafizayi_kaydet()
            print(f"  ✅ [AÇILDI] {symbol} {islem_yonu} @ {anlik}", flush=True)
            telegram_gonder(
                f"🎯 *TREND İŞLEM*\n📌 `{symbol}` | {islem_yonu}\n"
                f"🎯 Giriş: `{anlik}` | TP: `{tp}` | SL: `{sl}`"
            )
        except Exception as e:
            print(f"  ⚠️ Trend {symbol}: {e}", flush=True)
            continue

# ==================== ANA DÖNGÜ ====================
def ana_dongu():
    global GUNLUK_BASLANGIC_BAKIYE, AKTIF_MOD, SON_REJIM, SON_REJIM_ZAMANI
    print("🚀 [BAŞLANGIÇ] v10.15 - Grid İçi Trend Kırılım", flush=True)
    try:
        exchange.load_markets()
        b = exchange.fetch_balance()
        GUNLUK_BASLANGIC_BAKIYE = float(b['total'].get('USDT', 0))
        print(f"💰 Başlangıç kasası: {GUNLUK_BASLANGIC_BAKIYE:.2f} USDT", flush=True)
    except: pass
    
    baslangic_temizligi()
    
    SON_REJIM = AKTIF_MOD
    SON_REJIM_ZAMANI = time.time()
    
    dongu = 0
    while True:
        try:
            if not BOT_CALISIYOR_MU:
                time.sleep(5); continue
            
            dongu += 1
            print(f"\n{'='*60}", flush=True)
            print(f"🔄 [DÖNGÜ #{dongu}] {datetime.now().strftime('%H:%M:%S')}", flush=True)
            print(f"{'='*60}", flush=True)
            
            try:
                b = exchange.fetch_balance()
                su_an = float(b['total'].get('USDT', 0))
                if GUNLUK_BASLANGIC_BAKIYE and GUNLUK_BASLANGIC_BAKIYE > 0:
                    gk = (su_an - GUNLUK_BASLANGIC_BAKIYE) / GUNLUK_BASLANGIC_BAKIYE
                    print(f"💰 Kasa: {su_an:.2f} | Günlük: %{gk*100:+.2f}", flush=True)
                    if gk <= -GUNLUK_ZARAR_LIMIT:
                        print(f"🛑 KILL-SWITCH! %{gk*100:.2f}", flush=True)
                        telegram_gonder(f"🛑 *KILL-SWITCH* %{gk*100:.2f}")
                        time.sleep(3600)
                        GUNLUK_BASLANGIC_BAKIYE = su_an
                        continue
            except: pass
            
            # TOPLAM ZARAR KONTROLÜ
            try:
                raw_pos_check = exchange.fetch_positions()
                toplam_acik_zarar = sum(float(p.get('unrealizedPnl', 0)) for p in raw_pos_check 
                                       if float(p.get('contracts', 0) or p.get('size', 0) or 0) > 0)
                
                if toplam_acik_zarar < -TOPLAM_ZARAR_LIMIT:
                    print(f"🛑 [TOPLAM ZARAR LİMİTİ] {toplam_acik_zarar:.2f} USDT!", flush=True)
                    telegram_gonder(f"🛑 *TOPLAM ZARAR LİMİTİ*\n💵 {toplam_acik_zarar:.2f} USDT\nTüm pozisyonlar kapatılıyor!")
                    
                    for p in raw_pos_check:
                        k = float(p.get('contracts', 0) or p.get('size', 0) or 0)
                        if k > 0:
                            y = str(p.get('side', '')).upper() or "LONG"
                            market_kapat(p['symbol'], k, y)
                    
                    with state_lock:
                        AKTIF_SISTEMLER.clear()
                        GRID_HARITALARI.clear()
                    hafizayi_kaydet()
                    
                    time.sleep(600)
                    continue
            except Exception as e:
                print(f"⚠️ Toplam zarar kontrolü: {e}", flush=True)
            
            try:
                raw_pos = exchange.fetch_positions()
                aktif_borsa_map = {}
                aktif_semboller_seti = set()
                for p in raw_pos:
                    k = float(p.get('contracts', 0) or p.get('size', 0) or 0)
                    if k > 0:
                        aktif_borsa_map[p['symbol']] = p
                        aktif_semboller_seti.add(p['symbol'])
                print(f"📌 Açık: {list(aktif_semboller_seti) if aktif_semboller_seti else 'YOK'}", flush=True)
            except:
                aktif_borsa_map = {}
                aktif_semboller_seti = set()
            
            yeni_rejim, btc_yonu = piyasa_rejimini_tespit_et()
            
            simdi = time.time()
            if yeni_rejim != SON_REJIM:
                gecen = simdi - SON_REJIM_ZAMANI
                if gecen < REJIM_MIN_SURE:
                    print(f"⏳ [REJİM FİLTRE] {yeni_rejim} sinyali ama {int(gecen)}sn < {REJIM_MIN_SURE}sn", flush=True)
                    rejim = SON_REJIM
                else:
                    print(f"🔄 [REJİM DEĞİŞTİ] {SON_REJIM} → {yeni_rejim} ({int(gecen)}sn sonra)", flush=True)
                    SON_REJIM = yeni_rejim
                    SON_REJIM_ZAMANI = simdi
                    rejim = yeni_rejim
            else:
                rejim = yeni_rejim
                SON_REJIM_ZAMANI = simdi
            
            print(f"🌐 Rejim: {rejim} | BTC: {btc_yonu} | Aktif Mod: {AKTIF_MOD}", flush=True)
            
            if rejim == "YATAY":
                grid_modu_calistir(aktif_borsa_map, aktif_semboller_seti)
            else:
                trend_modu_calistir(aktif_borsa_map, aktif_semboller_seti)
        
        except Exception as e:
            print(f"⚠️ Ana döngü: {e}", flush=True)
            import traceback
            traceback.print_exc()
        
        time.sleep(10)

# ==================== TELEGRAM ====================
async def durum_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != int(CHAT_ID): return
    try:
        balance = await asyncio.to_thread(exchange.fetch_balance)
        total = float(balance['total'].get('USDT', 0))
        pos = [p for p in await asyncio.to_thread(exchange.fetch_positions) if float(p.get('contracts', 0) or p.get('size', 0) or 0) > 0]
        pnl = sum(float(p.get('unrealizedPnl', 0)) for p in pos)
        
        bas = int(ANALITIK.get("basarili_islem_sayisi", 0))
        basz = int(ANALITIK.get("basarisiz_islem_sayisi", 0))
        top = bas + basz
        oran = (bas / top * 100) if top > 0 else 0
        
        rejim, btc_yon = await asyncio.to_thread(piyasa_rejimini_tespit_et)
        
        pos_detay = ""
        if pos:
            pos_detay = "\n\n📌 *AÇIK POZİSYONLAR:*"
            for p in pos:
                sym = p['symbol'].replace('/USDT:USDT', '')
                yon = str(p.get('side', '')).upper() or "LONG"
                giris = float(p.get('entryPrice', 0))
                marj = float(p.get('initialMargin', 0) or 0)
                
                try:
                    t = await asyncio.to_thread(exchange.fetch_ticker, p['symbol'])
                    guncel = float(t['last'])
                except: guncel = giris
                
                kaldirac_p = int(p.get('leverage', 1))
                if yon == "LONG":
                    roe = ((guncel - giris) / giris) * 100 * kaldirac_p
                else:
                    roe = ((giris - guncel) / giris) * 100 * kaldirac_p
                
                if roe >= 0.5: emoji = "🟢"
                elif roe >= -1: emoji = "🟡"
                else: emoji = "🔴"
                
                grid_bilgi = ""
                if p['symbol'] in GRID_HARITALARI:
                    grid = GRID_HARITALARI[p['symbol']]
                    miktar_p = float(p.get('contracts', 0) or 0)
                    for idx, poz in grid['aktif_pozisyonlar'].items():
                        miktar_grid = float(poz.get('miktar', 0))
                        tolerans = max(miktar_grid * 0.02, 0.001)
                        if abs(miktar_grid - miktar_p) < tolerans:
                            grid_bilgi = f"\n  📐 Grid Seviye: #{int(idx)+1}/{GRID_SEVIYE_SAYISI*2}"
                            break
                
                # Grid içi trend mi?
                tip_bilgi = ""
                if p['symbol'] in AKTIF_SISTEMLER:
                    tip = AKTIF_SISTEMLER[p['symbol']].get("tip", "")
                    if tip == "GRID_ICI_TREND":
                        tip_bilgi = " 🎯TREND"
                    elif tip == "TREND":
                        tip_bilgi = " 📈TREND"
                
                pos_detay += (
                    f"\n{emoji} `{sym}`{tip_bilgi} | *{yon}* ({kaldirac_p}x){grid_bilgi}\n"
                    f"  Giriş: `{giris}` → Anlık: `{guncel}`\n"
                    f"  Marj: `{marj:.2f}` USDT | ROE: `%{roe:+.2f}` | PnL: `{float(p.get('unrealizedPnl', 0)):+.3f}`"
                )
        else:
            pos_detay = "\n\n📌 Açık pozisyon yok."
        
        grid_detay = ""
        if GRID_HARITALARI:
            grid_detay = "\n\n📐 *GRID ÖZET:*"
            for sym, grid in GRID_HARITALARI.items():
                sym_k = sym.replace('/USDT:USDT', '')
                aktif_say = len(grid.get('aktif_pozisyonlar', {}))
                meyil = grid.get('meyil', 'YATAY')
                meyil_emoji = {"YATAY": "➡️", "HAFIF_YUKARI": "↗️", "HAFIF_ASAGI": "↘️"}.get(meyil, "➡️")
                grid_detay += f"\n• `{sym_k}` | Aktif: `{aktif_say}`/{GRID_SEVIYE_SAYISI*2} | {meyil_emoji} `{meyil}`"
        
        mesaj = (
            f"📊 *BOT DURUM (v10.15)*\n\n"
            f"🎯 Aktif Mod: `{AKTIF_MOD}`\n"
            f"🌐 Rejim: `{rejim}` (BTC: `{btc_yon}`)\n"
            f"💰 Kasa: `{total:.2f} USDT`\n"
            f"💵 Toplam PnL: `{pnl:+.2f} USDT`\n"
            f"📌 Açık: `{len(pos)}` pozisyon\n"
            f"📐 Grid Coin: `{len(GRID_HARITALARI)}`"
            f"{pos_detay}"
            f"{grid_detay}\n\n"
            f"✅ TP: `{bas}` | ❌ SL: `{basz}`\n"
            f"📈 Başarı: `%{oran:.1f}`"
        )
        
        if len(mesaj) > 4000:
            mesaj = mesaj[:3950] + "\n\n...(kısaltıldı)"
        
        await update.message.reply_text(mesaj, parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"Hata: {e}")

async def baslat_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != int(CHAT_ID): return
    global BOT_CALISIYOR_MU, GUNLUK_BASLANGIC_BAKIYE
    BOT_CALISIYOR_MU = True
    try:
        b = await asyncio.to_thread(exchange.fetch_balance)
        GUNLUK_BASLANGIC_BAKIYE = float(b['total'].get('USDT', 0))
    except: pass
    await update.message.reply_text("🟢 Bot (v10.15) aktif!")

async def durdur_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != int(CHAT_ID): return
    global BOT_CALISIYOR_MU
    BOT_CALISIYOR_MU = False
    await update.message.reply_text("⏸️ Durduruldu.")

async def kapat_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != int(CHAT_ID): return
    try:
        positions = await asyncio.to_thread(exchange.fetch_positions)
        kapatilan = 0
        for p in positions:
            k = float(p.get('contracts', 0) or p.get('size', 0) or 0)
            if k > 0:
                y = str(p.get('side', '')).upper() or "LONG"
                try: exchange.cancel_all_orders(p['symbol'])
                except: pass
                if market_kapat(p['symbol'], k, y):
                    kapatilan += 1
        
        with state_lock:
            AKTIF_SISTEMLER.clear()
            GRID_HARITALARI.clear()
        hafizayi_kaydet()
        
        await update.message.reply_text(f"✅ {kapatilan} pozisyon kapatıldı, hafıza temizlendi.")
    except Exception as e:
        await update.message.reply_text(f"Hata: {e}")

# ==================== MAIN ====================
async def main():
    web_thread = threading.Thread(target=run_web, daemon=True)
    web_thread.start()
    
    app_tg = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    try:
        requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/deleteWebhook?drop_pending_updates=True", timeout=5)
    except: pass
    
    app_tg.add_handler(CommandHandler("durum", durum_komutu))
    app_tg.add_handler(CommandHandler("baslat", baslat_komutu))
    app_tg.add_handler(CommandHandler("durdur", durdur_komutu))
    app_tg.add_handler(CommandHandler("kapat", kapat_komutu))
    
    await app_tg.initialize()
    await app_tg.start()
    await app_tg.updater.start_polling(drop_pending_updates=True)
    
    t = threading.Thread(target=ana_dongu, daemon=True)
    t.start()
    
    stop = asyncio.Event()
    await stop.wait()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass

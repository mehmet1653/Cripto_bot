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
from dotenv import load_dotenv
from flask import Flask
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler
from supabase import create_client, Client

# ==================== .ENV YÜKLEME ====================
if os.path.exists('/etc/secrets/.env'):
    load_dotenv('/etc/secrets/.env', override=True)
    print("✅ .env (secrets) yüklendi", flush=True)
else:
    load_dotenv(override=True)
    print("✅ .env yüklendi", flush=True)

# ==================== FLASK ====================
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot aktif!"

def run_web():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

# ==================== API ANAHTARLARI ====================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.environ.get("CHAT_ID", "").strip()
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "").strip()
GATE_API_KEY = os.environ.get("GATE_API_KEY", "").strip()
GATE_SECRET = os.environ.get("GATE_SECRET", "").strip()

if not TELEGRAM_TOKEN or not CHAT_ID:
    print("❌ TELEGRAM boş!", flush=True); sys.exit(1)
if not SUPABASE_URL or not SUPABASE_KEY:
    print("❌ SUPABASE boş!", flush=True); sys.exit(1)
if not GATE_API_KEY or not GATE_SECRET:
    print("❌ GATE boş!", flush=True); sys.exit(1)

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

exchange = ccxt.gate({
    'apiKey': GATE_API_KEY,
    'secret': GATE_SECRET,
    'enableRateLimit': True,
    'timeout': 30000,
    'options': {'defaultType': 'swap'}
})
exchange.set_sandbox_mode(True)  # ⚠️ Gerçek hesaba geçerken False yap!

# ==================== DİNAMİK HAVUZ ====================
CEKIRDEK_LISTE = [
    'SOL/USDT:USDT', 'XRP/USDT:USDT', 'DOGE/USDT:USDT', 'LTC/USDT:USDT', 'LINK/USDT:USDT'
]
KARA_LISTE = ['BTC/USDT:USDT', 'ETH/USDT:USDT', 'AVAX/USDT:USDT']
DINAMIK_LISTE = []
SON_HAVUZ_GUNCELLEME = 0
HAVUZ_GUNCELLEME_SURESI = 3600

def havuzu_guncelle():
    global DINAMIK_LISTE
    try:
        print("🔄 [HAVUZ] Güncelleniyor...", flush=True)
        tickers = exchange.fetch_tickers()
        usdt_pairs = {}
        for k, v in tickers.items():
            if ':USDT' in k and k not in KARA_LISTE and k not in CEKIRDEK_LISTE:
                hacim = float(v.get('quoteVolume', 0) or 0)
                if hacim > 1_000_000:
                    usdt_pairs[k] = hacim
        sorted_pairs = sorted(usdt_pairs.items(), key=lambda x: x[1], reverse=True)
        DINAMIK_LISTE = [p[0] for p in sorted_pairs[:5]]
        print(f"✅ [HAVUZ] Çekirdek {len(CEKIRDEK_LISTE)} + Dinamik {len(DINAMIK_LISTE)}", flush=True)
        return True
    except Exception as e:
        print(f"⚠️ Havuz hatası: {e}", flush=True)
        return False

def takip_listesi():
    return CEKIRDEK_LISTE + DINAMIK_LISTE

# ==================== DURUM ====================
BOT_CALISIYOR_MU = True
state_lock = threading.Lock()
KALDIRAC = 5

KOMISYON_ORANI = 0.001
SPREAD_MALIYETI = 0.0005
MIN_NET_KAR = 0.006
MIN_RR = 1.3
MIN_RR_TREND_SWEEP = 1.5

TRAILING_SEVIYELER = [
    (15.0, 0.10),
    (10.0, 0.06),
    (7.0, 0.03),
    (5.0, 0.01),
]

KISMI_KAR_ROE = 5.0
KISMI_KAR_ORANI = 0.5

MAKS_ACIK_KALMA_SURESI = 4 * 60 * 60
MIN_KAR_ESIGI = 0.001

# ==================== HAFIZA ====================
def hafizayi_yukle():
    print("💾 Hafıza Supabase'den yükleniyor...", flush=True)
    try:
        response = supabase.table("bot_hafiza").select("*").eq("id", 1).execute()
        if response.data and len(response.data) > 0:
            veri = response.data[0]
            print("💾 Hafıza başarıyla yüklendi.", flush=True)
            return {
                "aktif_sistemler": veri.get("aktif_sistemler", {}),
                "analitik": veri.get("analitik", {"basarili_islem_sayisi": 0, "basarisiz_islem_sayisi": 0, "egitim_verileri": []}),
                "cooldownlar": veri.get("cooldownlar", {})
            }
    except Exception as e:
        print(f"⚠️ Hafıza yüklenirken hata: {e}", flush=True)
    return {
        "aktif_sistemler": {},
        "analitik": {"basarili_islem_sayisi": 0, "basarisiz_islem_sayisi": 0, "egitim_verileri": []},
        "cooldownlar": {}
    }

def hafizayi_kaydet():
    with state_lock:
        try:
            payload_analitik = {
                "basarili_islem_sayisi": int(ANALITIK_HAFIZA.get("basarili_islem_sayisi", 0)),
                "basarisiz_islem_sayisi": int(ANALITIK_HAFIZA.get("basarisiz_islem_sayisi", 0)),
                "egitim_verileri": ANALITIK_HAFIZA.get("egitim_verileri", [])
            }
            clean_cooldowns = {}
            for k, v in COIN_COOLDOWNLAR.items():
                if isinstance(v, dict):
                    clean_cooldowns[k] = {"zaman": float(v.get("zaman", 0)), "son_yon": str(v.get("son_yon", ""))}
                else:
                    clean_cooldowns[k] = {"zaman": float(v), "son_yon": ""}

            supabase.table("bot_hafiza").upsert({
                "id": 1,
                "aktif_sistemler": AKTIF_GRID_SISTEMLERI,
                "analitik": payload_analitik,
                "cooldownlar": clean_cooldowns
            }).execute()
        except Exception as e:
            print(f"⚠️ Hafıza kaydedilemedi: {e}", flush=True)

kalici_veri = hafizayi_yukle()
AKTIF_GRID_SISTEMLERI = kalici_veri.get("aktif_sistemler", {})
ANALITIK_HAFIZA = kalici_veri.get("analitik", {"basarili_islem_sayisi": 0, "basarisiz_islem_sayisi": 0, "egitim_verileri": []})
COIN_COOLDOWNLAR = kalici_veri.get("cooldownlar", {})

MAKSIMUM_TOPLAM_POZISYON = 3
COOLDOWN_SURESI_SANIYE = 30 * 60

# ==================== PİYASA REJİMİ + YÖN ====================
def piyasa_rejimini_tespit_et():
    try:
        ohlcv_btc = exchange.fetch_ohlcv('BTC/USDT:USDT', timeframe='1h', limit=40)
        df_btc = pd.DataFrame(ohlcv_btc, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])

        adx_1h = ta.trend.ADXIndicator(df_btc['high'], df_btc['low'], df_btc['close'], window=14).adx().iloc[-1]

        indicator_bb = ta.volatility.BollingerBands(close=df_btc['close'], window=20, window_dev=2)
        bb_high = indicator_bb.bollinger_hband().iloc[-1]
        bb_low = indicator_bb.bollinger_lband().iloc[-1]
        bb_mid = indicator_bb.bollinger_mavg().iloc[-1]
        bb_bandwidth = (bb_high - bb_low) / bb_mid

        ema9 = ta.trend.ema_indicator(df_btc['close'], window=9).iloc[-1]
        ema21 = ta.trend.ema_indicator(df_btc['close'], window=21).iloc[-1]
        anlik_btc = df_btc['close'].iloc[-1]

        # REJİM (ADX 30 eşiği)
        if adx_1h < 30.0 or bb_bandwidth < 0.02:
            rejim = "YATAY"
        else:
            rejim = "TREND"

        # YÖN TESPİTİ (Fiyat + EMA9 + EMA21)
        if anlik_btc > ema9 and ema9 > ema21:
            yon = "YUKARI"
        elif anlik_btc < ema9 and ema9 < ema21:
            yon = "ASAGI"
        else:
            yon = "BELIRSIZ"

        print(f"🌐 [PİYASA] Rejim: {rejim} | Yön: {yon} | ADX: {adx_1h:.2f} | BB: {bb_bandwidth:.4f} | BTC: {anlik_btc:.2f} | EMA9: {ema9:.2f} | EMA21: {ema21:.2f}", flush=True)
        return rejim, yon
    except Exception as e:
        print(f"⚠️ [PİYASA HATA] {e}. Varsayılan: YATAY/BELIRSIZ", flush=True)
        return "YATAY", "BELIRSIZ"

# ==================== LİKİDİTE SEVİYELERİ ====================
def swing_noktalari_bul(df, lookback=50):
    highs, lows = [], []
    baslangic = max(2, len(df) - lookback)
    for i in range(baslangic, len(df) - 2):
        h, l = df['high'].iloc[i], df['low'].iloc[i]
        if (h > df['high'].iloc[i-1] and h > df['high'].iloc[i-2] and h > df['high'].iloc[i+1] and h > df['high'].iloc[i+2]):
            highs.append((i, h))
        if (l < df['low'].iloc[i-1] and l < df['low'].iloc[i-2] and l < df['low'].iloc[i+1] and l < df['low'].iloc[i+2]):
            lows.append((i, l))
    return highs, lows

def likidite_seviyeleri(df, highs, lows, anlik_fiyat):
    def cluster(levels, tolerance=0.003):
        if not levels: return []
        sorted_l = sorted(levels)
        result = [sorted_l[0]]
        for lvl in sorted_l[1:]:
            if abs(lvl - result[-1]) / result[-1] > tolerance:
                result.append(lvl)
        return result
    high_levels = cluster([h[1] for h in highs])
    low_levels = cluster([l[1] for l in lows])
    destekler = sorted([l for l in low_levels if l < anlik_fiyat], reverse=True)[:3]
    direncler = sorted([h for h in high_levels if h > anlik_fiyat])[:3]
    return destekler, direncler

# ==================== TEYİTLİ TREND SWEEP (PULLBACK) ====================
def teyitli_trend_sweep(df_15m, df_5m, destekler, direncler, anlik_fiyat, piyasa_yonu):
    if len(df_15m) < 3: return None, None, None, None

    son_mum = df_15m.iloc[-1]
    onceki = df_15m.iloc[-2]
    iki_onceki = df_15m.iloc[-3]
    atr = ta.volatility.AverageTrueRange(df_15m['high'], df_15m['low'], df_15m['close'], window=14).average_true_range().iloc[-1]

    son_mum_yesil_5m = df_5m['close'].iloc[-1] > df_5m['open'].iloc[-1]
    son_mum_kirmizi_5m = df_5m['close'].iloc[-1] < df_5m['open'].iloc[-1]
    hacim_ort_5m = df_5m['volume'].rolling(20).mean().iloc[-1]
    guncel_hacim_5m = df_5m['volume'].iloc[-1]
    hacim_teyit = guncel_hacim_5m > (hacim_ort_5m * 1.5)

    stoch_rsi_5m_seri = ta.momentum.StochRSIIndicator(df_5m['close'], window=14).stochrsi()
    stoch_5m_simdi = stoch_rsi_5m_seri.iloc[-1]
    stoch_5m_onceki = stoch_rsi_5m_seri.iloc[-2]
    stoch_donus_yukari_5m = stoch_5m_onceki < 0.3 and stoch_5m_simdi > 0.3
    stoch_donus_asagi_5m = stoch_5m_onceki > 0.7 and stoch_5m_simdi < 0.7

    long_izinli = (piyasa_yonu in ["YUKARI", "BELIRSIZ"])
    short_izinli = (piyasa_yonu in ["ASAGI", "BELIRSIZ"])

    if long_izinli:
        for destek in destekler:
            mesafe = (anlik_fiyat - destek) / destek
            if mesafe > 0.015:
                continue

            kirildi, en_dusuk = False, 999999999
            if iki_onceki['low'] < destek: kirildi, en_dusuk = True, min(iki_onceki['low'], en_dusuk)
            if onceki['low'] < destek: kirildi, en_dusuk = True, min(onceki['low'], en_dusuk)
            if kirildi and son_mum['close'] > destek and anlik_fiyat > destek:
                sweep_derinlik = (destek - en_dusuk) / destek
                if sweep_derinlik >= 0.003 and sweep_derinlik < 0.02:
                    if son_mum_yesil_5m and hacim_teyit and stoch_donus_yukari_5m:
                        sl_fiyat = en_dusuk - (atr * 1.2)
                        if (anlik_fiyat - sl_fiyat) / anlik_fiyat > 0.02:
                            sl_fiyat = anlik_fiyat * 0.98
                        return "LONG", destek, sl_fiyat, en_dusuk

    if short_izinli:
        for direnc in direncler:
            mesafe = (direnc - anlik_fiyat) / anlik_fiyat
            if mesafe > 0.015:
                continue

            kirildi, en_yuksek = False, 0
            if iki_onceki['high'] > direnc: kirildi, en_yuksek = True, max(iki_onceki['high'], en_yuksek)
            if onceki['high'] > direnc: kirildi, en_yuksek = True, max(onceki['high'], en_yuksek)
            if kirildi and son_mum['close'] < direnc and anlik_fiyat < direnc:
                sweep_derinlik = (en_yuksek - direnc) / direnc
                if sweep_derinlik >= 0.003 and sweep_derinlik < 0.02:
                    if son_mum_kirmizi_5m and hacim_teyit and stoch_donus_asagi_5m:
                        sl_fiyat = en_yuksek + (atr * 1.2)
                        if (sl_fiyat - anlik_fiyat) / anlik_fiyat > 0.02:
                            sl_fiyat = anlik_fiyat * 1.02
                        return "SHORT", direnc, sl_fiyat, en_yuksek

    return None, None, None, None

def tp_hesapla_trend_sweep(yon, giris, sl, destekler, direncler, atr):
    if yon == "LONG":
        for d in direncler:
            if d > giris:
                tp = d * (1 - 0.005)
                rr = (tp - giris) / (giris - sl) if giris > sl else 0
                if rr >= MIN_RR_TREND_SWEEP:
                    return tp, rr
        tp = giris + (atr * 3.0)
        return tp, (tp - giris) / (giris - sl) if giris > sl else 0
    else:
        for d in sorted(destekler, reverse=True):
            if d < giris:
                tp = d * (1 + 0.005)
                rr = (giris - tp) / (sl - giris) if sl > giris else 0
                if rr >= MIN_RR_TREND_SWEEP:
                    return tp, rr
        tp = giris - (atr * 3.0)
        return tp, (giris - tp) / (sl - giris) if sl > giris else 0

# ==================== TEYİTLİ SWEEP (YATAY MOD) ====================
def teyitli_sweep_sinyal(df_15m, df_5m, destekler, direncler, anlik_fiyat, piyasa_yonu):
    if len(df_15m) < 3: return None, None, None, None

    son_mum = df_15m.iloc[-1]
    onceki = df_15m.iloc[-2]
    iki_onceki = df_15m.iloc[-3]
    atr = ta.volatility.AverageTrueRange(df_15m['high'], df_15m['low'], df_15m['close'], window=14).average_true_range().iloc[-1]

    son_mum_yesil_5m = df_5m['close'].iloc[-1] > df_5m['open'].iloc[-1]
    son_mum_kirmizi_5m = df_5m['close'].iloc[-1] < df_5m['open'].iloc[-1]
    hacim_ort_5m = df_5m['volume'].rolling(20).mean().iloc[-1]
    guncel_hacim_5m = df_5m['volume'].iloc[-1]
    hacim_teyit = guncel_hacim_5m > (hacim_ort_5m * 1.5)

    stoch_rsi_5m_seri = ta.momentum.StochRSIIndicator(df_5m['close'], window=14).stochrsi()
    stoch_5m_simdi = stoch_rsi_5m_seri.iloc[-1]
    stoch_5m_onceki = stoch_rsi_5m_seri.iloc[-2]
    stoch_donus_yukari_5m = stoch_5m_onceki < 0.3 and stoch_5m_simdi > 0.3
    stoch_donus_asagi_5m = stoch_5m_onceki > 0.7 and stoch_5m_simdi < 0.7

    long_izinli = (piyasa_yonu in ["YUKARI", "BELIRSIZ"])
    short_izinli = (piyasa_yonu in ["ASAGI", "BELIRSIZ"])

    if long_izinli:
        for destek in destekler:
            kirildi, en_dusuk = False, 999999999
            if iki_onceki['low'] < destek: kirildi, en_dusuk = True, min(iki_onceki['low'], en_dusuk)
            if onceki['low'] < destek: kirildi, en_dusuk = True, min(onceki['low'], en_dusuk)
            if kirildi and son_mum['close'] > destek and anlik_fiyat > destek:
                if (destek - en_dusuk) / destek < 0.02:
                    if son_mum_yesil_5m and hacim_teyit and stoch_donus_yukari_5m:
                        sl_fiyat = en_dusuk - (atr * 1.2)
                        if (anlik_fiyat - sl_fiyat) / anlik_fiyat > 0.02:
                            sl_fiyat = anlik_fiyat * 0.98
                        return "LONG", destek, sl_fiyat, en_dusuk

    if short_izinli:
        for direnc in direncler:
            kirildi, en_yuksek = False, 0
            if iki_onceki['high'] > direnc: kirildi, en_yuksek = True, max(iki_onceki['high'], en_yuksek)
            if onceki['high'] > direnc: kirildi, en_yuksek = True, max(onceki['high'], en_yuksek)
            if kirildi and son_mum['close'] < direnc and anlik_fiyat < direnc:
                if (en_yuksek - direnc) / direnc < 0.02:
                    if son_mum_kirmizi_5m and hacim_teyit and stoch_donus_asagi_5m:
                        sl_fiyat = en_yuksek + (atr * 1.2)
                        if (sl_fiyat - anlik_fiyat) / anlik_fiyat > 0.02:
                            sl_fiyat = anlik_fiyat * 1.02
                        return "SHORT", direnc, sl_fiyat, en_yuksek

    return None, None, None, None

def tp_hesapla_sweep(yon, giris, sl, destekler, direncler, atr):
    if yon == "LONG":
        for d in direncler:
            if d > giris:
                tp = d * (1 - 0.005)
                rr = (tp - giris) / (giris - sl) if giris > sl else 0
                if rr >= MIN_RR:
                    return tp, rr
        tp = giris + (atr * 3.0)
        return tp, (tp - giris) / (giris - sl) if giris > sl else 0
    else:
        for d in sorted(destekler, reverse=True):
            if d < giris:
                tp = d * (1 + 0.005)
                rr = (giris - tp) / (sl - giris) if sl > giris else 0
                if rr >= MIN_RR:
                    return tp, rr
        tp = giris - (atr * 3.0)
        return tp, (giris - tp) / (sl - giris) if sl > giris else 0

# ==================== MTF FİLTRESİ ====================
def coklu_zaman_trend_kontrol(symbol):
    try:
        ohlcv_4h = exchange.fetch_ohlcv(symbol, timeframe='4h', limit=50)
        ohlcv_1d = exchange.fetch_ohlcv(symbol, timeframe='1d', limit=50)
        df_4h = pd.DataFrame(ohlcv_4h, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df_1d = pd.DataFrame(ohlcv_1d, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])

        ema20_4h = ta.trend.EMAIndicator(df_4h['close'], window=20).ema_indicator().iloc[-1]
        ema50_4h = ta.trend.EMAIndicator(df_4h['close'], window=50).ema_indicator().iloc[-1]
        ema20_1d = ta.trend.EMAIndicator(df_1d['close'], window=20).ema_indicator().iloc[-1]
        ema50_1d = ta.trend.EMAIndicator(df_1d['close'], window=50).ema_indicator().iloc[-1]

        trend_4h = "YUKARI" if ema20_4h > ema50_4h else "ASAGI"
        trend_1d = "YUKARI" if ema20_1d > ema50_1d else "ASAGI"

        if trend_4h == "YUKARI" and trend_1d == "YUKARI":
            return "YUKARI"
        elif trend_4h == "ASAGI" and trend_1d == "ASAGI":
            return "ASAGI"
        else:
            return "NOTR"
    except Exception:
        return "NOTR"

# ==================== HİBRİT SİNYAL (YATAY MOD) ====================
def sinyal_uret_hibrit(df_15m, df_5m, anlik_fiyat, piyasa_yonu):
    try:
        bb = ta.volatility.BollingerBands(df_15m['close'], window=20, window_dev=2)
        bb_high = bb.bollinger_hband().iloc[-1]
        bb_low = bb.bollinger_lband().iloc[-1]

        hacim_ort = df_15m['volume'].rolling(20).mean().iloc[-1]
        guncel_hacim = df_15m['volume'].iloc[-1]
        hacim_patlamasi = guncel_hacim > (hacim_ort * 2.0)

        son_mum_yesil_5m = df_5m['close'].iloc[-1] > df_5m['open'].iloc[-1]
        son_mum_kirmizi_5m = df_5m['close'].iloc[-1] < df_5m['open'].iloc[-1]
        son_iki_mum_yesil_5m = (df_5m['close'].iloc[-1] > df_5m['open'].iloc[-1] and 
                                 df_5m['close'].iloc[-2] > df_5m['open'].iloc[-2])
        son_iki_mum_kirmizi_5m = (df_5m['close'].iloc[-1] < df_5m['open'].iloc[-1] and 
                                   df_5m['close'].iloc[-2] < df_5m['open'].iloc[-2])

        stoch_rsi_5m_seri = ta.momentum.StochRSIIndicator(df_5m['close'], window=14).stochrsi()
        stoch_rsi_5m_simdi = stoch_rsi_5m_seri.iloc[-1]
        stoch_rsi_5m_onceki = stoch_rsi_5m_seri.iloc[-2]
        stoch_donus_yukari_5m = stoch_rsi_5m_onceki < 0.2 and stoch_rsi_5m_simdi > 0.2
        stoch_donus_asagi_5m = stoch_rsi_5m_onceki > 0.8 and stoch_rsi_5m_simdi < 0.8

        long_izinli = (piyasa_yonu in ["YUKARI", "BELIRSIZ"])
        short_izinli = (piyasa_yonu in ["ASAGI", "BELIRSIZ"])

        if long_izinli:
            bb_low_temas = anlik_fiyat <= bb_low * 1.005
            if bb_low_temas and son_mum_yesil_5m and stoch_donus_yukari_5m and hacim_patlamasi:
                return "LONG", f"Bant dönüşü + hacim"

            fiyat_ust_bant_disi = anlik_fiyat > bb_high * 1.001
            if (fiyat_ust_bant_disi and son_iki_mum_yesil_5m and 
                stoch_rsi_5m_simdi >= 0.98 and hacim_patlamasi):
                return "LONG", f"Teyitli breakout"

        if short_izinli:
            bb_high_temas = anlik_fiyat >= bb_high * 0.995
            if bb_high_temas and son_mum_kirmizi_5m and stoch_donus_asagi_5m and hacim_patlamasi:
                return "SHORT", f"Üst bant dönüşü + hacim"

            fiyat_alt_bant_disi = anlik_fiyat < bb_low * 0.999
            if (fiyat_alt_bant_disi and son_iki_mum_kirmizi_5m and 
                stoch_rsi_5m_simdi <= 0.02 and hacim_patlamasi):
                return "SHORT", f"Teyitli breakdown"

        return None, None
    except Exception as e:
        print(f"⚠️ Hibrit sinyal hatası: {e}", flush=True)
        return None, None

# ==================== TREND BREAKOUT (TREND MOD) ====================
def trend_breakout_sinyal(df_15m, df_5m, anlik_fiyat, piyasa_yonu):
    try:
        bb = ta.volatility.BollingerBands(df_15m['close'], window=20, window_dev=2)
        bb_high = bb.bollinger_hband().iloc[-1]
        bb_low = bb.bollinger_lband().iloc[-1]

        hacim_ort = df_15m['volume'].rolling(20).mean().iloc[-1]
        guncel_hacim = df_15m['volume'].iloc[-1]
        hacim_patlamasi = guncel_hacim > (hacim_ort * 2.5)

        son_iki_mum_yesil_5m = (df_5m['close'].iloc[-1] > df_5m['open'].iloc[-1] and 
                                 df_5m['close'].iloc[-2] > df_5m['open'].iloc[-2])
        son_iki_mum_kirmizi_5m = (df_5m['close'].iloc[-1] < df_5m['open'].iloc[-1] and 
                                   df_5m['close'].iloc[-2] < df_5m['open'].iloc[-2])

        stoch_rsi_5m_seri = ta.momentum.StochRSIIndicator(df_5m['close'], window=14).stochrsi()
        stoch_rsi_5m_simdi = stoch_rsi_5m_seri.iloc[-1]

        long_izinli = (piyasa_yonu in ["YUKARI", "BELIRSIZ"])
        short_izinli = (piyasa_yonu in ["ASAGI", "BELIRSIZ"])

        if long_izinli:
            fiyat_ust_bant_disi = anlik_fiyat > bb_high * 1.0015
            if (fiyat_ust_bant_disi and son_iki_mum_yesil_5m and 
                stoch_rsi_5m_simdi >= 0.98 and hacim_patlamasi):
                return "LONG", f"TREND Breakout"

        if short_izinli:
            fiyat_alt_bant_disi = anlik_fiyat < bb_low * 0.9985
            if (fiyat_alt_bant_disi and son_iki_mum_kirmizi_5m and 
                stoch_rsi_5m_simdi <= 0.02 and hacim_patlamasi):
                return "SHORT", f"TREND Breakdown"

        return None, None
    except Exception as e:
        print(f"⚠️ Trend breakout hatası: {e}", flush=True)
        return None, None

# ==================== 🆕 TREND TAKİP (MOMENTUM) ====================
def trend_takip_sinyal(df_15m, df_5m, anlik_fiyat, piyasa_yonu):
    """
    TREND modda trend takip (momentum).
    Öncelik 3 (Sweep ve Breakout yoksa çalışır).
    
    Şartlar:
    1. Fiyat EMA9'un altında (SHORT) / üstünde (LONG)
    2. EMA9 < EMA21 (SHORT) / > EMA21 (LONG)
    3. Son 3 mumun en az 2'si aynı yönde
    4. Stoch < 0.5 (SHORT) / > 0.5 (LONG)
    5. Hacim 1.5x
    """
    try:
        # EMA'lar
        ema9 = ta.trend.ema_indicator(df_15m['close'], window=9).iloc[-1]
        ema21 = ta.trend.ema_indicator(df_15m['close'], window=21).iloc[-1]

        # Son 3 mum
        son_3 = []
        for i in range(-3, 0):
            if df_15m['close'].iloc[i] > df_15m['open'].iloc[i]:
                son_3.append("YESIL")
            else:
                son_3.append("KIRMIZI")

        yesil_sayisi = son_3.count("YESIL")
        kirmizi_sayisi = son_3.count("KIRMIZI")

        # Hacim
        hacim_ort = df_15m['volume'].rolling(20).mean().iloc[-1]
        guncel_hacim = df_15m['volume'].iloc[-1]
        hacim_patlamasi = guncel_hacim > (hacim_ort * 1.5)

        # Stoch
        stoch_15m = ta.momentum.StochRSIIndicator(df_15m['close'], window=14).stochrsi().iloc[-1]

        long_izinli = (piyasa_yonu in ["YUKARI", "BELIRSIZ"])
        short_izinli = (piyasa_yonu in ["ASAGI", "BELIRSIZ"])

        # ===== LONG TREND TAKİP =====
        if long_izinli:
            if (anlik_fiyat > ema9 and ema9 > ema21 and 
                yesil_sayisi >= 2 and stoch_15m > 0.5 and hacim_patlamasi):
                return "LONG", f"Trend Takip (Momentum) | Stoch:{stoch_15m:.2f}"

        # ===== SHORT TREND TAKİP =====
        if short_izinli:
            if (anlik_fiyat < ema9 and ema9 < ema21 and 
                kirmizi_sayisi >= 2 and stoch_15m < 0.5 and hacim_patlamasi):
                return "SHORT", f"Trend Takip (Momentum) | Stoch:{stoch_15m:.2f}"

        return None, None
    except Exception as e:
        print(f"⚠️ Trend takip hatası: {e}", flush=True)
        return None, None

# ==================== KOMİSYON ====================
def net_kar_yeterli_mi(yon, giris, tp):
    if yon == "LONG":
        brut = (tp - giris) / giris
    else:
        brut = (giris - tp) / giris
    net = brut - KOMISYON_ORANI - SPREAD_MALIYETI
    return net >= MIN_NET_KAR, net

# ==================== AKILLI SEVİYE ====================
def akilli_seviye_hesapla(anlik_fiyat, yon, df, mod="YATAY"):
    atr = ta.volatility.AverageTrueRange(df['high'], df['low'], df['close'], window=14).average_true_range().iloc[-1]

    if mod == "YATAY":
        tp_mesafe = atr * 1.2
        sl_mesafe = atr * 0.8
    else:
        tp_mesafe = atr * 2.5
        sl_mesafe = atr * 1.5

    if yon == 'LONG':
        tp_fiyat = anlik_fiyat + tp_mesafe
        sl_fiyat = anlik_fiyat - sl_mesafe
        kapat_yon = 'sell'
    else:
        tp_fiyat = anlik_fiyat - tp_mesafe
        sl_fiyat = anlik_fiyat + sl_mesafe
        kapat_yon = 'buy'

    hedef_roe = abs((tp_fiyat - anlik_fiyat) / anlik_fiyat) * 100 * KALDIRAC
    return float(tp_fiyat), float(sl_fiyat), kapat_yon, float(hedef_roe)

# ==================== TRAILING ====================
def trailing_stop_kontrol():
    with state_lock:
        aktif_kopya = list(AKTIF_GRID_SISTEMLERI.items())

    for sym, bilgi in aktif_kopya:
        if sym not in AKTIF_GRID_SISTEMLERI: continue
        if not isinstance(bilgi, dict): continue

        yon = bilgi.get("yon", "LONG")
        g = float(bilgi.get("giris_fiyati", 0))
        sl_kayitli = float(bilgi.get("sl_fiyat", 0))
        tp_kayitli = float(bilgi.get("tp_fiyat", 0))
        giris_zaman = float(bilgi.get("giris_zamani", 0))

        if time.time() - giris_zaman < 60: continue

        try:
            t = exchange.fetch_ticker(sym)
            anlik = float(t['last'])
        except: continue

        roe = ((anlik - g) / g * 100 * KALDIRAC) if yon == "LONG" else ((g - anlik) / g * 100 * KALDIRAC)

        yeni_sl = None
        for esik_roe, kilit_orani in TRAILING_SEVIYELER:
            if roe >= esik_roe:
                yeni_sl = g * (1 + kilit_orani / KALDIRAC) if yon == "LONG" else g * (1 - kilit_orani / KALDIRAC)
                break

        if yeni_sl is None: continue

        if yon == "LONG":
            iyilestirme = yeni_sl > sl_kayitli * 1.0005
        else:
            iyilestirme = yeni_sl < sl_kayitli * 0.9995

        if not iyilestirme: continue

        try:
            exchange.cancel_all_orders(sym)
            miktar = None
            for p in exchange.fetch_positions():
                if p['symbol'] == sym:
                    miktar = float(p.get('contracts', 0) or p.get('size', 0) or 0)
                    break
            if not miktar or miktar <= 0: continue

            ky = 'sell' if yon == 'LONG' else 'buy'
            exchange.create_order(sym, 'stop', ky, miktar, yeni_sl, {'stopPrice': yeni_sl, 'reduceOnly': True})
            if tp_kayitli > 0:
                exchange.create_order(sym, 'limit', ky, miktar, tp_kayitli, {'reduceOnly': True})

            with state_lock:
                if sym in AKTIF_GRID_SISTEMLERI:
                    AKTIF_GRID_SISTEMLERI[sym]["sl_fiyat"] = yeni_sl
            print(f"🔒 [TRAILING] {sym} | ROE:%{roe:.1f} → SL:{yeni_sl:.6f}", flush=True)
            telegram_mesaj_gonder(f"🔒 *KÂR KİLİTLENDİ*\n📌 `{sym}` | {yon}\n📊 ROE: `%{roe:+.2f}`\n🛑 Yeni SL: `{yeni_sl:.6f}`")
        except Exception as e:
            print(f"⚠️ Trailing hatası {sym}: {e}", flush=True)

# ==================== KISMİ KÂR ====================
def kismi_kar_al_kontrol():
    with state_lock:
        aktif_kopya = list(AKTIF_GRID_SISTEMLERI.items())

    for sym, bilgi in aktif_kopya:
        if sym not in AKTIF_GRID_SISTEMLERI: continue
        if not isinstance(bilgi, dict): continue
        if bilgi.get("kismi_kar_alindi", False): continue

        yon = bilgi.get("yon", "LONG")
        g = float(bilgi.get("giris_fiyati", 0))

        try:
            t = exchange.fetch_ticker(sym)
            anlik = float(t['last'])
        except: continue

        roe = ((anlik - g) / g * 100 * KALDIRAC) if yon == "LONG" else ((g - anlik) / g * 100 * KALDIRAC)

        if roe >= KISMI_KAR_ROE:
            try:
                exchange.cancel_all_orders(sym)
                miktar = None
                for p in exchange.fetch_positions():
                    if p['symbol'] == sym:
                        miktar = float(p.get('contracts', 0) or p.get('size', 0) or 0)
                        break
                if not miktar or miktar <= 0: continue

                yarim = miktar * KISMI_KAR_ORANI
                ky = 'sell' if yon == 'LONG' else 'buy'
                exchange.create_order(sym, 'market', ky, yarim, None, {'reduceOnly': True})

                kalan = miktar - yarim
                tp_kayitli = float(bilgi.get("tp_fiyat", 0))
                sl_kayitli = float(bilgi.get("sl_fiyat", 0))
                if tp_kayitli > 0:
                    exchange.create_order(sym, 'limit', ky, kalan, tp_kayitli, {'reduceOnly': True})
                if sl_kayitli > 0:
                    exchange.create_order(sym, 'stop', ky, kalan, sl_kayitli, {'stopPrice': sl_kayitli, 'reduceOnly': True})

                with state_lock:
                    if sym in AKTIF_GRID_SISTEMLERI:
                        AKTIF_GRID_SISTEMLERI[sym]["kismi_kar_alindi"] = True

                print(f"💰 [KISMİ KÂR] {sym} | ROE:%{roe:.1f} → %50 kapatıldı", flush=True)
                telegram_mesaj_gonder(f"💰 *KISMİ KÂR ALINDI*\n📌 `{sym}` | {yon}\n📊 ROE: `%{roe:+.2f}`\n✂️ %50 kapatıldı.")
            except Exception as e:
                print(f"⚠️ Kısmi kâr hatası {sym}: {e}", flush=True)

# ==================== ZOMBİ ====================
def zombi_islem_kapat():
    with state_lock:
        aktif_kopya = list(AKTIF_GRID_SISTEMLERI.items())

    for sym, bilgi in aktif_kopya:
        if sym not in AKTIF_GRID_SISTEMLERI: continue
        if not isinstance(bilgi, dict): continue

        yon = bilgi.get("yon", "LONG")
        g = float(bilgi.get("giris_fiyati", 0))
        giris_zaman = float(bilgi.get("giris_zamani", 0))

        gecen_sure = time.time() - giris_zaman
        if gecen_sure < MAKS_ACIK_KALMA_SURESI:
            continue

        try:
            t = exchange.fetch_ticker(sym)
            anlik = float(t['last'])
        except:
            continue

        if yon == "LONG":
            brut_kar = (anlik - g) / g
        else:
            brut_kar = (g - anlik) / g

        net_kar = brut_kar - KOMISYON_ORANI - SPREAD_MALIYETI

        if net_kar >= MIN_KAR_ESIGI:
            try:
                exchange.cancel_all_orders(sym)
                miktar = None
                for p in exchange.fetch_positions():
                    if p['symbol'] == sym:
                        miktar = float(p.get('contracts', 0) or p.get('size', 0) or 0)
                        break
                if not miktar or miktar <= 0: continue

                ky = 'sell' if yon == 'LONG' else 'buy'
                exchange.create_order(sym, 'market', ky, miktar, None, {'reduceOnly': True})

                with state_lock:
                    if sym in AKTIF_GRID_SISTEMLERI: del AKTIF_GRID_SISTEMLERI[sym]
                    COIN_COOLDOWNLAR[sym] = {"zaman": float(time.time() + COOLDOWN_SURESI_SANIYE), "son_yon": yon}

                hafizayi_kaydet()
                saat = int(gecen_sure // 3600)
                dakika = int((gecen_sure % 3600) // 60)
                print(f"⏰ [ZOMBİ KAPATILDI] {sym} | {saat}s {dakika}dk | Net: %{net_kar*100:.3f}", flush=True)
                telegram_mesaj_gonder(f"⏰ *ZOMBİ KAPATILDI*\n📌 `{sym}` | {yon}\n⏱️ {saat}s {dakika}dk\n💰 Net: `%{net_kar*100:.3f}`")
            except Exception as e:
                print(f"⚠️ Zombi hatası {sym}: {e}", flush=True)

# ==================== TELEGRAM ====================
def telegram_mesaj_gonder(mesaj):
    if not TELEGRAM_TOKEN or not CHAT_ID: return
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={"chat_id": CHAT_ID, "text": mesaj, "parse_mode": "Markdown"}, timeout=5)
    except Exception: pass

async def durum_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != int(CHAT_ID): return
    try:
        balance = await asyncio.to_thread(exchange.fetch_balance)
        total = float(balance['total'].get('USDT', 0))
        borsa_poslari = [p for p in await asyncio.to_thread(exchange.fetch_positions) if float(p.get('contracts', 0) or p.get('size', 0) or 0) > 0]
        toplam_pnl = sum(float(p.get('unrealizedPnl', 0)) for p in borsa_poslari)
        rejim, yon = piyasa_rejimini_tespit_et()
        basarili = int(ANALITIK_HAFIZA.get("basarili_islem_sayisi", 0))
        basarisiz = int(ANALITIK_HAFIZA.get("basarisiz_islem_sayisi", 0))
        toplam_islem = basarili + basarisiz
        basari_orani = (basarili / toplam_islem * 100) if toplam_islem > 0 else 0.0

        pos_detaylari = ""
        for p in borsa_poslari:
            sym = p['symbol']
            yon_p = str(p.get('side', '')).upper() or "LONG"
            giris = float(p.get('entryPrice', 0))
            kaldirac_val = int(p.get('leverage', KALDIRAC))
            ticker_data = await asyncio.to_thread(exchange.fetch_ticker, sym)
            guncel_fiyat = float(ticker_data['last'])
            fark = (guncel_fiyat - giris) / giris if yon_p == "LONG" else (giris - guncel_fiyat) / giris
            roe = fark * 100 * kaldirac_val
            mod = AKTIF_GRID_SISTEMLERI.get(sym, {}).get("mod", "?")
            kismi = AKTIF_GRID_SISTEMLERI.get(sym, {}).get("kismi_kar_alindi", False)
            etiket = " ✂️" if kismi else ""
            pos_detaylari += f"\n• `{sym}` | {yon_p}{etiket} [{mod}] | Giriş: `{giris}`\n  ROE: `%{roe:+.2f}`"

        mesaj = (
            f"📊 **DURUM (Sweep + Breakout + Trend Takip)**\n\n"
            f"🌐 Rejim: `{rejim}` | Yön: `{yon}`\n"
            f"💰 Kasa: `{total:.2f} USDT` | PnL: `{toplam_pnl:+.2f}`\n"
            f"📌 Açık: `{len(borsa_poslari)} / {MAKSIMUM_TOPLAM_POZISYON}`"
            f"{pos_detaylari}\n\n"
            f"✅ TP: `{basarili}` | ❌ SL: `{basarisiz}`\n"
            f"📈 Başarı: `%{basari_orani:.1f}`\n\n"
            f"📋 Havuz: `{len(takip_listesi())}` coin"
        )
        await update.message.reply_text(mesaj, parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"Hata: {e}")

async def baslat_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != int(CHAT_ID): return
    global BOT_CALISIYOR_MU
    BOT_CALISIYOR_MU = True
    await update.message.reply_text("🟢 Bot aktif! (Sweep + Breakout + Trend Takip)")

async def durdur_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != int(CHAT_ID): return
    global BOT_CALISIYOR_MU
    BOT_CALISIYOR_MU = False
    await update.message.reply_text("⏸️ Durduruldu.")

async def kapat_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != int(CHAT_ID): return
    try:
        positions = await asyncio.to_thread(exchange.fetch_positions)
        for pos in positions:
            kontrat = float(pos.get('contracts', 0) or pos.get('size', 0) or 0)
            if kontrat > 0:
                yon_p = str(pos.get('side', '')).upper() or "LONG"
                kapatma_yonu = 'sell' if yon_p == 'LONG' else 'buy'
                try: exchange.cancel_all_orders(pos['symbol'])
                except Exception: pass
                exchange.create_order(pos['symbol'], 'market', kapatma_yonu, kontrat, None, {'reduceOnly': True})
        await update.message.reply_text("✅ Kapatıldı.")
    except Exception as e:
        await update.message.reply_text(f"Hata: {e}")

async def havuz_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != int(CHAT_ID): return
    await update.message.reply_text("🔄 Havuz güncelleniyor...")
    basarili = havuzu_guncelle()
    if basarili:
        await update.message.reply_text(
            f"✅ *Havuz Güncellendi!*\n\n"
            f"📌 Çekirdek: `{len(CEKIRDEK_LISTE)}` coin\n"
            f"🔄 Dinamik: `{len(DINAMIK_LISTE)}` coin\n\n"
            f"*Dinamik Liste:*\n" + "\n".join([f"• `{c}`" for c in DINAMIK_LISTE]),
            parse_mode='Markdown'
        )
    else:
        await update.message.reply_text("❌ Havuz güncellenemedi.")

# ==================== ANA TARAYICI ====================
def otomatik_arkaplan_tarayici():
    global SON_HAVUZ_GUNCELLEME
    print("🚀 [BAŞLANGIÇ] Sweep + Breakout + Trend Takip + Yön Filtreli...", flush=True)
    try:
        exchange.load_markets()
    except Exception: pass

    havuzu_guncelle()
    SON_HAVUZ_GUNCELLEME = time.time()

    while True:
        try:
            if not BOT_CALISIYOR_MU:
                time.sleep(5)
                continue

            if time.time() - SON_HAVUZ_GUNCELLEME > HAVUZ_GUNCELLEME_SURESI:
                havuzu_guncelle()
                SON_HAVUZ_GUNCELLEME = time.time()

            piyasa_rejimi, piyasa_yonu = piyasa_rejimini_tespit_et()

            try:
                raw_positions = exchange.fetch_positions()
                aktif_borsa_map = {}
                aktif_semboller_listesi = []
                for p in raw_positions:
                    kontrat_miktari = float(p.get('contracts', 0) or p.get('size', 0) or 0)
                    if kontrat_miktari > 0:
                        sym = p['symbol']
                        aktif_borsa_map[sym] = p
                        aktif_semboller_listesi.append(sym)
                if aktif_semboller_listesi:
                    print(f"📌 Açık: {aktif_semboller_listesi}", flush=True)
            except Exception:
                raw_positions = []
                aktif_borsa_map = {}
                aktif_semboller_listesi = []

            # KAPANIŞ KONTROLÜ
            try:
                anlik_aktif_semboller = [p['symbol'] for p in raw_positions if float(p.get('contracts', 0) or p.get('size', 0) or 0) > 0]
                for eski_sym in list(AKTIF_GRID_SISTEMLERI.keys()):
                    if eski_sym not in anlik_aktif_semboller:
                        sistem_bilgisi = AKTIF_GRID_SISTEMLERI[eski_sym]
                        giris_fiyati = sistem_bilgisi.get("giris_fiyati", 0) if isinstance(sistem_bilgisi, dict) else 0
                        yon_t = sistem_bilgisi.get("yon", "LONG") if isinstance(sistem_bilgisi, dict) else "LONG"

                        islem_karli_mi = False
                        try:
                            ticker = exchange.fetch_ticker(eski_sym)
                            cikis_fiyati = float(ticker['last'])
                            if yon_t == "LONG": islem_karli_mi = cikis_fiyati > giris_fiyati
                            else: islem_karli_mi = cikis_fiyati < giris_fiyati
                        except Exception:
                            islem_karli_mi = True

                        with state_lock:
                            bas_sayi = int(ANALITIK_HAFIZA.get("basarili_islem_sayisi", 0))
                            basarisiz_sayi = int(ANALITIK_HAFIZA.get("basarisiz_islem_sayisi", 0))

                            if islem_karli_mi:
                                bas_sayi += 1
                                sonuc_mesaj_tipi = "✅ *KÂRLA KAPANDI*"
                            else:
                                basarisiz_sayi += 1
                                sonuc_mesaj_tipi = "❌ *ZARARLA KAPANDI*"

                            ANALITIK_HAFIZA["basarili_islem_sayisi"] = bas_sayi
                            ANALITIK_HAFIZA["basarisiz_islem_sayisi"] = basarisiz_sayi
                            COIN_COOLDOWNLAR[eski_sym] = {"zaman": float(time.time() + COOLDOWN_SURESI_SANIYE), "son_yon": yon_t}
                            if eski_sym in AKTIF_GRID_SISTEMLERI: del AKTIF_GRID_SISTEMLERI[eski_sym]

                        hafizayi_kaydet()
                        telegram_mesaj_gonder(f"{sonuc_mesaj_tipi}\n📌 `{eski_sym}`")
            except Exception: pass

            trailing_stop_kontrol()
            kismi_kar_al_kontrol()
            zombi_islem_kapat()

            taranan_sinyaller = []

            for symbol in takip_listesi():
                if not BOT_CALISIYOR_MU: break

                with state_lock:
                    cooldown_veri = COIN_COOLDOWNLAR.get(symbol)
                    if cooldown_veri:
                        zaman_kontrol = cooldown_veri.get("zaman", 0) if isinstance(cooldown_veri, dict) else float(cooldown_veri)
                        if zaman_kontrol - time.time() > 0:
                            print(f"   ⏳ [{symbol}] Cooldown: {int(zaman_kontrol - time.time())} sn", flush=True)
                            continue

                try:
                    ticker = exchange.fetch_ticker(symbol)
                    anlik_fiyat = float(ticker['last'])

                    ohlcv_15m = exchange.fetch_ohlcv(symbol, timeframe='15m', limit=50)
                    df_15m = pd.DataFrame(ohlcv_15m, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])

                    ohlcv_5m = exchange.fetch_ohlcv(symbol, timeframe='5m', limit=50)
                    df_5m = pd.DataFrame(ohlcv_5m, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])

                    bb_15m = ta.volatility.BollingerBands(df_15m['close'], window=20, window_dev=2)
                    bb_low_15m = bb_15m.bollinger_lband().iloc[-1]
                    bb_high_15m = bb_15m.bollinger_hband().iloc[-1]
                    stoch_15m = ta.momentum.StochRSIIndicator(df_15m['close'], window=14).stochrsi().iloc[-1]
                    stoch_5m_seri = ta.momentum.StochRSIIndicator(df_5m['close'], window=14).stochrsi()
                    stoch_5m = stoch_5m_seri.iloc[-1]
                    son_mum_5m = "YEŞİL" if df_5m['close'].iloc[-1] > df_5m['open'].iloc[-1] else "KIRMIZI"

                    print(f"\n🔍 [{symbol}] @ {anlik_fiyat}", flush=True)
                    print(f"   📊 BB(15m): Alt={bb_low_15m:.5f} | Üst={bb_high_15m:.5f}", flush=True)
                    print(f"   📊 Stoch(15m)={stoch_15m:.2f} | Stoch(5m)={stoch_5m:.2f}", flush=True)
                    print(f"   📊 Mum 5m={son_mum_5m}", flush=True)
                    print(f"   🌐 Rejim: {piyasa_rejimi} | Yön: {piyasa_yonu}", flush=True)

                    # ==================== YATAY MOD ====================
                    if piyasa_rejimi == "YATAY":
                        highs, lows = swing_noktalari_bul(df_15m)
                        yon = None
                        if len(highs) >= 2 and len(lows) >= 2:
                            destekler, direncler = likidite_seviyeleri(df_15m, highs, lows, anlik_fiyat)
                            if destekler or direncler:
                                print(f"   📊 Destek: {[f'{d:.5f}' for d in destekler]} | Direnç: {[f'{d:.5f}' for d in direncler]}", flush=True)

                                yon_sweep, likidite_lvl, sl_sweep, sweep_uc = teyitli_sweep_sinyal(
                                    df_15m, df_5m, destekler, direncler, anlik_fiyat, piyasa_yonu
                                )
                                if yon_sweep:
                                    atr = ta.volatility.AverageTrueRange(df_15m['high'], df_15m['low'], df_15m['close'], window=14).average_true_range().iloc[-1]
                                    tp_sweep, rr_sweep = tp_hesapla_sweep(yon_sweep, anlik_fiyat, sl_sweep, destekler, direncler, atr)

                                    if rr_sweep >= MIN_RR:
                                        yon = yon_sweep
                                        tp_fiyat = tp_sweep
                                        sl_fiyat = sl_sweep
                                        kapat_yon = 'sell' if yon == 'LONG' else 'buy'
                                        hedef_roe = abs((tp_fiyat - anlik_fiyat) / anlik_fiyat) * 100 * KALDIRAC
                                        rr = rr_sweep
                                        mod = "YATAY-Sweep"
                                        sebep = f"Teyitli Sweep"
                                        print(f"   🎯 [YATAY Sweep] SİNYAL! {yon} | R/R: {rr:.2f}", flush=True)
                                else:
                                    print(f"   ⏭️ [YATAY Sweep] Sinyal yok", flush=True)

                        if yon is None:
                            yon, sebep = sinyal_uret_hibrit(df_15m, df_5m, anlik_fiyat, piyasa_yonu)
                            if yon is None:
                                print(f"   ⏭️ [YATAY] Sinyal yok", flush=True)
                                continue
                            mod = "YATAY"
                            tp_fiyat, sl_fiyat, kapat_yon, hedef_roe = akilli_seviye_hesapla(anlik_fiyat, yon, df_15m, "YATAY")
                            rr = abs(tp_fiyat - anlik_fiyat) / abs(anlik_fiyat - sl_fiyat) if abs(anlik_fiyat - sl_fiyat) > 0 else 0
                            print(f"   🎯 [YATAY] SİNYAL! {yon} | {sebep} | R/R: {rr:.2f}", flush=True)

                    # ==================== TREND MOD ====================
                    else:
                        yon = None
                        # 1. TEYİTLİ TREND SWEEP (Pullback) - ÖNCELİK 1
                        highs, lows = swing_noktalari_bul(df_15m)
                        if len(highs) >= 2 and len(lows) >= 2:
                            destekler, direncler = likidite_seviyeleri(df_15m, highs, lows, anlik_fiyat)
                            if destekler or direncler:
                                print(f"   📊 Destek: {[f'{d:.5f}' for d in destekler]} | Direnç: {[f'{d:.5f}' for d in direncler]}", flush=True)

                                yon_sweep, likidite_lvl, sl_sweep, sweep_uc = teyitli_trend_sweep(
                                    df_15m, df_5m, destekler, direncler, anlik_fiyat, piyasa_yonu
                                )
                                if yon_sweep:
                                    ana_trend = coklu_zaman_trend_kontrol(symbol)
                                    if yon_sweep == "LONG" and ana_trend != "YUKARI":
                                        print(f"   ⏭️ [TREND Sweep] LONG iptal | MTF {ana_trend}", flush=True)
                                        yon = None
                                    elif yon_sweep == "SHORT" and ana_trend != "ASAGI":
                                        print(f"   ⏭️ [TREND Sweep] SHORT iptal | MTF {ana_trend}", flush=True)
                                        yon = None
                                    else:
                                        atr = ta.volatility.AverageTrueRange(df_15m['high'], df_15m['low'], df_15m['close'], window=14).average_true_range().iloc[-1]
                                        tp_sweep, rr_sweep = tp_hesapla_trend_sweep(yon_sweep, anlik_fiyat, sl_sweep, destekler, direncler, atr)

                                        if rr_sweep >= MIN_RR_TREND_SWEEP:
                                            yon = yon_sweep
                                            tp_fiyat = tp_sweep
                                            sl_fiyat = sl_sweep
                                            kapat_yon = 'sell' if yon == 'LONG' else 'buy'
                                            hedef_roe = abs((tp_fiyat - anlik_fiyat) / anlik_fiyat) * 100 * KALDIRAC
                                            rr = rr_sweep
                                            mod = "TREND-Sweep"
                                            sebep = f"Teyitli Pullback | MTF: {ana_trend}"
                                            print(f"   🎯 [TREND Sweep] SİNYAL! {yon} | R/R: {rr:.2f} | MTF: {ana_trend}", flush=True)
                                else:
                                    print(f"   ⏭️ [TREND Sweep] Sinyal yok", flush=True)

                        # 2. TREND BREAKOUT - ÖNCELİK 2
                        if yon is None:
                            yon, sebep = trend_breakout_sinyal(df_15m, df_5m, anlik_fiyat, piyasa_yonu)
                            if yon is not None:
                                ana_trend = coklu_zaman_trend_kontrol(symbol)
                                if yon == "LONG" and ana_trend != "YUKARI":
                                    print(f"   ⏭️ [TREND] LONG iptal | MTF {ana_trend}", flush=True)
                                    yon = None
                                elif yon == "SHORT" and ana_trend != "ASAGI":
                                    print(f"   ⏭️ [TREND] SHORT iptal | MTF {ana_trend}", flush=True)
                                    yon = None
                                else:
                                    mod = "TREND-Breakout"
                                    tp_fiyat, sl_fiyat, kapat_yon, hedef_roe = akilli_seviye_hesapla(anlik_fiyat, yon, df_15m, "TREND")
                                    rr = abs(tp_fiyat - anlik_fiyat) / abs(anlik_fiyat - sl_fiyat) if abs(anlik_fiyat - sl_fiyat) > 0 else 0
                                    print(f"   🎯 [TREND Breakout] SİNYAL! {yon} | {sebep} | R/R: {rr:.2f} | MTF: {ana_trend}", flush=True)
                            else:
                                print(f"   ⏭️ [TREND] Breakout yok", flush=True)

                        # 3. TREND TAKİP (MOMENTUM) - ÖNCELİK 3
                        if yon is None:
                            yon, sebep = trend_takip_sinyal(df_15m, df_5m, anlik_fiyat, piyasa_yonu)
                            if yon is not None:
                                ana_trend = coklu_zaman_trend_kontrol(symbol)
                                if yon == "LONG" and ana_trend != "YUKARI":
                                    print(f"   ⏭️ [TREND Takip] LONG iptal | MTF {ana_trend}", flush=True)
                                    yon = None
                                elif yon == "SHORT" and ana_trend != "ASAGI":
                                    print(f"   ⏭️ [TREND Takip] SHORT iptal | MTF {ana_trend}", flush=True)
                                    yon = None
                                else:
                                    mod = "TREND-Takip"
                                    tp_fiyat, sl_fiyat, kapat_yon, hedef_roe = akilli_seviye_hesapla(anlik_fiyat, yon, df_15m, "TREND")
                                    rr = abs(tp_fiyat - anlik_fiyat) / abs(anlik_fiyat - sl_fiyat) if abs(anlik_fiyat - sl_fiyat) > 0 else 0
                                    print(f"   🎯 [TREND Takip] SİNYAL! {yon} | {sebep} | R/R: {rr:.2f} | MTF: {ana_trend}", flush=True)
                            else:
                                print(f"   ⏭️ [TREND Takip] Sinyal yok", flush=True)

                    if yon is None:
                        continue

                    yeterli, net_kar = net_kar_yeterli_mi(yon, anlik_fiyat, tp_fiyat)
                    print(f"   💰 Net kâr: %{net_kar*100:.3f} | Yeterli: {yeterli}", flush=True)
                    if not yeterli:
                        print(f"   ⏭️ [{symbol}] {yon} iptal | Net kâr yetersiz", flush=True)
                        continue

                    taranan_sinyaller.append({
                        "symbol": symbol, "yon": yon, "fiyat": anlik_fiyat, "df": df_15m,
                        "tp": tp_fiyat, "sl": sl_fiyat, "kapat_yon": kapat_yon, "mod": mod,
                        "rr": rr, "hedef_roe": hedef_roe, "sebep": sebep
                    })
                except Exception as e:
                    print(f"⚠️ {symbol} hata: {e}", flush=True)
                    continue

            for sinyal in taranan_sinyaller:
                if not BOT_CALISIYOR_MU: break
                if sinyal["symbol"] in aktif_semboller_listesi: continue
                if len(aktif_borsa_map) >= MAKSIMUM_TOPLAM_POZISYON:
                    print(f"   ⏭️ Maksimum pozisyon dolu", flush=True)
                    break

                try:
                    bakiye_bilgisi = exchange.fetch_balance()
                    toplam_bakiye = float(bakiye_bilgisi['total'].get('USDT', 0))
                    serbest_bakiye = float(bakiye_bilgisi.get('free', {}).get('USDT', 0) or 0)

                    exchange.set_leverage(KALDIRAC, sinyal["symbol"])
                    market = exchange.market(sinyal["symbol"])

                    kullanilacak_tutar = min(toplam_bakiye * 0.3, serbest_bakiye)
                    if kullanilacak_tutar < 1.0:
                        print(f"   ⏭️ Yetersiz bakiye", flush=True)
                        continue

                    miktar = float(exchange.amount_to_precision(
                        sinyal["symbol"],
                        max((kullanilacak_tutar * KALDIRAC) / sinyal["fiyat"] / float(market.get('contractSize', 1.0)),
                        float(market['limits']['amount']['min'] or 1.0))
                    ))

                    islem_yonu = 'buy' if sinyal["yon"] == 'LONG' else 'sell'

                    exchange.create_order(sinyal["symbol"], 'market', islem_yonu, miktar)
                    time.sleep(0.5)
                    try:
                        exchange.create_order(sinyal["symbol"], 'limit', sinyal["kapat_yon"], miktar, sinyal["tp"], {'reduceOnly': True})
                        exchange.create_order(sinyal["symbol"], 'stop', sinyal["kapat_yon"], miktar, sinyal["sl"], {'stopPrice': sinyal["sl"], 'reduceOnly': True})
                    except Exception as e:
                        print(f"   ⚠️ TP/SL: {e}", flush=True)

                    with state_lock:
                        AKTIF_GRID_SISTEMLERI[sinyal["symbol"]] = {
                            "giris_fiyati": sinyal["fiyat"], "yon": sinyal["yon"],
                            "tp_fiyat": sinyal["tp"], "sl_fiyat": sinyal["sl"],
                            "giris_zamani": time.time(), "mod": sinyal["mod"],
                            "kismi_kar_alindi": False
                        }
                        aktif_semboller_listesi.append(sinyal["symbol"])
                    hafizayi_kaydet()

                    print(f"   ✅ AÇILDI! {sinyal['symbol']} | {sinyal['yon']} | {sinyal['mod']} | {sinyal['sebep']} | TP: {sinyal['tp']} | SL: {sinyal['sl']}", flush=True)

                    telegram_mesaj_gonder(
                        f"🎯 *İŞLEM AÇILDI ({sinyal['mod']} - 5x)*\n"
                        f"📌 `{sinyal['symbol']}` | {sinyal['yon']}\n"
                        f"📝 Sebep: `{sinyal['sebep']}`\n"
                        f"🎯 Giriş: `{sinyal['fiyat']}`\n"
                        f"💰 TP: `{sinyal['tp']}` (ROE: `%{sinyal['hedef_roe']:.1f}`)\n"
                        f"🛑 SL: `{sinyal['sl']}`\n"
                        f"📊 R/R: `{sinyal['rr']:.2f}`"
                    )
                    break
                except Exception as e:
                    print(f"⚠️ İşlem hatası {sinyal['symbol']}: {e}", flush=True)

        except Exception as e:
            print(f"⚠️ Ana döngü: {e}", flush=True)

        time.sleep(5)

# ==================== MAIN ====================
async def main():
    web_thread = threading.Thread(target=run_web, daemon=True)
    web_thread.start()

    print("🧹 Telegram webhook temizleniyor...", flush=True)
    try:
        requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/deleteWebhook?drop_pending_updates=True", timeout=10)
        print("✅ Webhook temizlendi. 5 saniye bekleniyor...", flush=True)
        await asyncio.sleep(5)
    except Exception as e:
        print(f"⚠️ Webhook temizleme hatası: {e}", flush=True)

    app_tg = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app_tg.add_handler(CommandHandler("durum", durum_komutu))
    app_tg.add_handler(CommandHandler("baslat", baslat_komutu))
    app_tg.add_handler(CommandHandler("durdur", durdur_komutu))
    app_tg.add_handler(CommandHandler("kapat", kapat_komutu))
    app_tg.add_handler(CommandHandler("havuz", havuz_komutu))

    await app_tg.initialize()
    await app_tg.start()
    await app_tg.updater.start_polling(drop_pending_updates=True)
    print("✅ Telegram polling başladı", flush=True)

    tarayici_thread = threading.Thread(target=otomatik_arkaplan_tarayici, daemon=True)
    tarayici_thread.start()

    stop_event = asyncio.Event()
    await stop_event.wait()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass

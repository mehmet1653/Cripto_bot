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
from datetime import datetime
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
exchange.set_sandbox_mode(True) # Canlıya geçerken burayı False yap!

# ==================== AYARLAR ====================
DINAMIK_LISTE = []
SON_LISTE_GUNCELLEME = 0
LISTE_GUNCELLEME_SURESI = 15 * 60
LISTE_BOYUT = 10

KARA_LISTE = [
    'BTC/USDT:USDT', 'ETH/USDT:USDT', 'AVAX/USDT:USDT',
    'WBTC/USDT:USDT', 'WETH/USDT:USDT',
    'USDC/USDT:USDT', 'USDT/USDT:USDT', 'DAI/USDT:USDT',
    'FDUSD/USDT:USDT', 'TUSD/USDT:USDT', 'BUSD/USDT:USDT'
]

MIN_HACIM_24S = 5_000_000
MIN_DEGISIM_YUZDE = 0.5
MIN_FIYAT = 0.01
MAX_FIYAT = 300.0

BOT_CALISIYOR_MU = True
state_lock = threading.Lock()

# Risk yönetimi 
KALDIRAC = 5
POZISYON_MARJ = 0.10
MAKS_POZISYON = 2

# Trend Modu Ayarları
SL_YUZDE = 1.6              
TP_YUZDE = 2.5              
ADX_ESIK_DEGERI = 18.0       

# Testere Modu Ayarları
TESTERE_TP_YUZDE = 0.8      
TESTERE_SL_YUZDE = 0.8      

# ✅ YENİ: Kâr Takipçisi (Trailing Stop) Ayarları
TRAILING_STOP_AKTIF = True
TRAILING_BASLANGIC_ROE = 4.0   # Kâr %4 ROE'ye ulaşınca takip başlar
TRAILING_DUSUS_YUZDE = 1.5     # Zirveden %1.5 düşerse kârı cebe indir

TOPLAM_ZARAR_LIMIT = 2.0

# Formasyon ve Trend ayarları
PIVOT_LOOKBACK = 50
DESTEK_DIRENC_MESAFE = 1.0

# Modül ayarları
ORDERBOOK_WALL_ORANI = 2.0
FUNDING_RATE_ASIRI = 0.05
VOLUME_PROFILE_BINS = 30
LIKIDITE_AVI_HACIM_ORANI = 0.7

# Cooldown
COOLDOWN_TP = 15 * 60
COOLDOWN_SL = 30 * 60
COOLDOWN_VARSAYILAN = 30 * 60

# Order Flow
MFI_ESIK_AL = 52
MFI_ESIK_SAT = 48
CMF_ESIK = 0.03
HACIM_ESIK = 0.5
MFI_ASIRI_SATIM = 10
MFI_ASIRI_ALIM = 90

GEREKLI_SINYAL = 6
MIN_PUAN = 11               
TOP_N_ADAY = 3

BTC_DONUS_KALKANI_AKTIF = False  

GUNLUK_BASLANGIC_BAKIYE = None
GUNLUK_ZARAR_LIMIT = 0.03

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
                "cooldownlar": veri.get("cooldownlar", {})
            }
    except Exception as e:
        print(f"⚠️ Hafıza: {e}", flush=True)
    
    return {
        "aktif_sistemler": {},
        "analitik": {"basarili_islem_sayisi": 0, "basarisiz_islem_sayisi": 0, "egitim_verileri": []},
        "cooldownlar": {}
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
                "cooldownlar": clean_cd
            }).execute()
        except Exception as e:
            print(f"⚠️ Kayıt: {e}", flush=True)

kalici = hafizayi_yukle()
AKTIF_SISTEMLER = kalici.get("aktif_sistemler", {})
ANALITIK = kalici.get("analitik", {"basarili_islem_sayisi": 0, "basarisiz_islem_sayisi": 0, "egitim_verileri": []})
COIN_COOLDOWNLAR = kalici.get("cooldownlar", {})


# ==================== HIZLI ÖN PUAN ====================
def hizli_on_puan(symbol):
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe='15m', limit=50)
        if len(ohlcv) < 30: return 0, {}
        
        df = pd.DataFrame(ohlcv, columns=['t', 'o', 'h', 'l', 'c', 'v'])
        
        mfi = ta.volume.money_flow_index(high=df['h'], low=df['l'], close=df['c'], volume=df['v'], window=14).iloc[-1]
        if mfi < MFI_ASIRI_SATIM or mfi > MFI_ASIRI_ALIM: return 0, {"sebep": f"MFI aşırı ({mfi:.0f})"}
        
        son3_hacim = df['v'].iloc[-3:].mean()
        ort20_hacim = df['v'].iloc[-20:].mean()
        hacim_oran = son3_hacim / ort20_hacim if ort20_hacim > 0 else 0
        if hacim_oran < HACIM_ESIK: return 0, {"sebep": f"Hacim zayıf ({hacim_oran:.2f}x)"}
        
        puan = 0
        if 40 < mfi < 60: puan += 3
        elif 30 < mfi < 70: puan += 1
        if hacim_oran > 1.5: puan += 3
        elif hacim_oran > 1.2: puan += 2
        elif hacim_oran > 1.0: puan += 1
        
        ema9 = ta.trend.ema_indicator(df['c'], window=9).iloc[-1]
        ema21 = ta.trend.ema_indicator(df['c'], window=21).iloc[-1]
        if abs(ema9 - ema21) / ema21 > 0.005: puan += 2
        
        bb_ust = ta.volatility.bollinger_hband(df['c'], window=20)
        bb_alt = ta.volatility.bollinger_lband(df['c'], window=20)
        bb_genislik = ((bb_ust - bb_alt) / df['c']) * 100
        if bb_genislik.iloc[-1] < bb_genislik.iloc[-20:].mean() * 0.8: puan += 2
        
        return puan, {"mfi": round(mfi, 1), "hacim_oran": round(hacim_oran, 2)}
    except: return 0, {}


# ==================== DİNAMİK COIN HAVUZU ====================
def dinamik_liste_guncelle(zorla=False):
    global DINAMIK_LISTE, SON_LISTE_GUNCELLEME
    if not zorla and (time.time() - SON_LISTE_GUNCELLEME < LISTE_GUNCELLEME_SURESI): return
    
    print(f"\n🔄 [DİNAMİK LİSTE] Güncelleniyor...", flush=True)
    SON_LISTE_GUNCELLEME = time.time()
    
    try:
        tickers = exchange.fetch_tickers()
        adaylar = []
        tum_semboller = [s for s in tickers.keys() if ':USDT' in s and s not in KARA_LISTE]
        
        for sym in tum_semboller:
            try:
                t = tickers[sym]
                hacim = float(t.get('quoteVolume', 0) or 0)
                degisim = float(t.get('percentage', 0) or 0)
                fiyat = float(t.get('last', 0) or 0)
                
                if hacim < MIN_HACIM_24S: continue
                if fiyat < MIN_FIYAT: continue
                if fiyat > MAX_FIYAT: continue
                
                try:
                    market = exchange.market(sym)
                    min_miktar = float(market['limits']['amount']['min'] or 1.0)
                    contract_size = float(market.get('contractSize', 1.0))
                    min_marj = (min_miktar * fiyat * contract_size) / KALDIRAC
                    if min_marj > 15.0: continue
                except: continue
                
                hizli_puan, hizli_detay = hizli_on_puan(sym)
                
                if hizli_puan > 0:
                    adaylar.append({
                        "symbol": sym, "hizli_puan": hizli_puan,
                        "degisim": degisim, "hacim": hacim, "fiyat": fiyat,
                        "detay": hizli_detay
                    })
            except: continue
        
        adaylar.sort(key=lambda x: x['hizli_puan'], reverse=True)
        secilenler = adaylar[:LISTE_BOYUT]
        yeni_liste = [a['symbol'] for a in secilenler]
        
        if yeni_liste:
            DINAMIK_LISTE = yeni_liste
            print(f"✅ [DİNAMİK] {len(DINAMIK_LISTE)} coin:", flush=True)
            for a in secilenler:
                sym_kisa = a['symbol'].replace('/USDT:USDT', '')
                print(f"   ⭐ {sym_kisa} | Puan: {a['hizli_puan']} | %{a['degisim']:+.1f} | {a['hacim']/1_000_000:.0f}M | Fiyat: {a['fiyat']}", flush=True)
    except Exception as e:
        print(f"⚠️ [DİNAMİK] Hata: {e}", flush=True)


# ==================== DETAYLI MODÜLLER ====================
def orderbook_analiz(symbol):
    try:
        ob = exchange.fetch_order_book(symbol, limit=50)
        bids = ob['bids']; asks = ob['asks']
        if not bids or not asks: return None
        
        bid_hacim = sum(b[1] * b[0] for b in bids[:20])
        ask_hacim = sum(a[1] * a[0] for a in asks[:20])
        ort_bid = bid_hacim / 20 if bid_hacim > 0 else 1
        ort_ask = ask_hacim / 20 if ask_hacim > 0 else 1
        
        alim_duvari = any(f * m > ort_bid * ORDERBOOK_WALL_ORANI for f, m in bids[:20])
        satim_duvari = any(f * m > ort_ask * ORDERBOOK_WALL_ORANI for f, m in asks[:20])
        
        if bid_hacim > ask_hacim * 1.5: baski = "ALIM"
        elif ask_hacim > bid_hacim * 1.5: baski = "SATIM"
        else: baski = "DENGE"
        
        return {"baski": baski, "alim_duvari": alim_duvari, "satim_duvari": satim_duvari}
    except: return None

def funding_analiz(symbol):
    try:
        fr_data = exchange.fetch_funding_rate(symbol)
        funding = float(fr_data.get('fundingRate', 0)) * 100
        asiri_long = funding > FUNDING_RATE_ASIRI
        asiri_short = funding < -FUNDING_RATE_ASIRI
        return {"funding": round(funding, 4), "asiri_long": asiri_long, "asiri_short": asiri_short}
    except: return None

def volume_profile_analiz(df):
    try:
        if len(df) < 30: return None
        min_fiyat = df['l'].min()
        max_fiyat = df['h'].max()
        if max_fiyat == min_fiyat: return None
        bin_boyut = (max_fiyat - min_fiyat) / VOLUME_PROFILE_BINS
        bins = {}
        for _, mum in df.iterrows():
            fiyat_orta = (mum['h'] + mum['l']) / 2
            bin_idx = int((fiyat_orta - min_fiyat) / bin_boyut)
            bins[bin_idx] = bins.get(bin_idx, 0) + mum['v']
        if not bins: return None
        esik = sorted(bins.values())[int(len(bins) * 0.7)] if len(bins) > 3 else 0
        yuksek_bins = [b for b, v in bins.items() if v >= esik]
        anlik = df['c'].iloc[-1]
        en_yakin = min(yuksek_bins, key=lambda b: abs((min_fiyat + (b + 0.5) * bin_boyut) - anlik)) if yuksek_bins else None
        if en_yakin is not None:
            yakin_fiyat = min_fiyat + (en_yakin + 0.5) * bin_boyut
            mesafe = abs(yakin_fiyat - anlik) / anlik * 100
            tip = "DESTEK" if yakin_fiyat < anlik else "DIRENC"
        else: yakin_fiyat = None; mesafe = 999; tip = None
        return {"yakin_hacim_fiyat": round(yakin_fiyat, 4) if yakin_fiyat else None, "yakin_hacim_mesafe": round(mesafe, 2), "yakin_hacim_tip": tip}
    except: return None

def mtf_analiz(symbol):
    try:
        sonuclar = {}
        for tf, limit, key in [('1d', 30, '1d'), ('4h', 40, '4h'), ('1h', 50, '1h'), ('15m', 50, '15m'), ('5m', 50, '5m')]:
            ohlcv = exchange.fetch_ohlcv(symbol, timeframe=tf, limit=limit)
            if len(ohlcv) < 20: sonuclar[key] = "YATAY"; continue
            df = pd.DataFrame(ohlcv, columns=['t', 'o', 'h', 'l', 'c', 'v'])
            ema9 = ta.trend.ema_indicator(df['c'], window=9).iloc[-1]
            ema21 = ta.trend.ema_indicator(df['c'], window=21).iloc[-1]
            fark = ((ema9 - ema21) / ema21) * 100
            if fark > 0.1: sonuclar[key] = "YUKARI"
            elif fark < -0.1: sonuclar[key] = "ASAGI"
            else: sonuclar[key] = "YATAY"
        yukari_say = sum(1 for v in sonuclar.values() if v == "YUKARI")
        asagi_say = sum(1 for v in sonuclar.values() if v == "ASAGI")
        if yukari_say >= 4: uyum = "GUCLU_LONG"
        elif asagi_say >= 4: uyum = "GUCLU_SHORT"
        elif yukari_say >= 3: uyum = "ZAYIF_LONG"
        elif asagi_say >= 3: uyum = "ZAYIF_SHORT"
        else: uyum = "KARISIK"
        return {"uyum": uyum}
    except: return None

def likidite_avi_tespit(symbol, df_15m, destekler, direncler):
    try:
        if len(df_15m) < 10 or not destekler or not direncler: return None
        son3 = df_15m.iloc[-3:]
        anlik = df_15m['c'].iloc[-1]
        hacim_oran = son3['v'].mean() / df_15m['v'].iloc[-20:].mean()
        yakin_destek = min(destekler, key=lambda x: abs(x - anlik)) if destekler else None
        yakin_direnc = min(direncler, key=lambda x: abs(x - anlik)) if direncler else None
        if yakin_destek:
            if son3['l'].min() < yakin_destek * 0.998 and anlik > yakin_destek and hacim_oran < LIKIDITE_AVI_HACIM_ORANI:
                return {"tip": "DESTEK_AVI", "yon": "LONG", "sebep": "Likidite avı"}
        if yakin_direnc:
            if son3['h'].max() > yakin_direnc * 1.002 and anlik < yakin_direnc and hacim_oran < LIKIDITE_AVI_HACIM_ORANI:
                return {"tip": "DIRENC_AVI", "yon": "SHORT", "sebep": "Likidite avı"}
        return None
    except: return None

def btc_bias():
    try:
        ohlcv_5m = exchange.fetch_ohlcv('BTC/USDT:USDT', timeframe='5m', limit=50)
        df_5m = pd.DataFrame(ohlcv_5m, columns=['t', 'o', 'h', 'l', 'c', 'v'])
        ema20_5m = ta.trend.ema_indicator(df_5m['c'], window=20).iloc[-1]
        ema50_5m = ta.trend.ema_indicator(df_5m['c'], window=50).iloc[-1]
        fiyat_5m = df_5m['c'].iloc[-1]
        trend_5m = "LONG" if fiyat_5m > ema20_5m > ema50_5m else ("SHORT" if fiyat_5m < ema20_5m < ema50_5m else "YATAY")

        ohlcv_15m = exchange.fetch_ohlcv('BTC/USDT:USDT', timeframe='15m', limit=50)
        df_15m = pd.DataFrame(ohlcv_15m, columns=['t', 'o', 'h', 'l', 'c', 'v'])
        ema20_15m = ta.trend.ema_indicator(df_15m['c'], window=20).iloc[-1]
        ema50_15m = ta.trend.ema_indicator(df_15m['c'], window=50).iloc[-1]
        fiyat_15m = df_15m['c'].iloc[-1]
        rsi_15m = ta.momentum.rsi(df_15m['c'], window=14).iloc[-1]
        trend_15m = "LONG" if fiyat_15m > ema20_15m > ema50_15m else ("SHORT" if fiyat_15m < ema20_15m < ema50_15m else "YATAY")

        if trend_5m == "LONG" and trend_15m == "LONG":
            if rsi_15m > 70: return "KARISIK" 
            return "LONG"
        elif trend_5m == "SHORT" and trend_15m == "SHORT":
            if rsi_15m < 30: return "KARISIK" 
            return "SHORT"
        else: return "KARISIK"
    except: return "KARISIK"

def pivot_noktalari_bul(df, lookback=5):
    ph, pl = [], []
    for i in range(lookback, len(df) - lookback):
        if df['h'].iloc[i] == df['h'].iloc[i-lookback:i+lookback+1].max(): ph.append((i, df['h'].iloc[i]))
        if df['l'].iloc[i] == df['l'].iloc[i-lookback:i+lookback+1].min(): pl.append((i, df['l'].iloc[i]))
    return ph, pl

def trend_cizgisi_analiz(ph, pl):
    if len(ph) < 2 or len(pl) < 2: return "YATAY", "Yetersiz veri"
    h_trend = "YUKARI" if ph[-1][1] > ph[-2][1] else "ASAGI"
    l_trend = "YUKARI" if pl[-1][1] > pl[-2][1] else "ASAGI"
    if h_trend == "YUKARI" and l_trend == "YUKARI": return "YUKSELEN", "Yükselen kanal"
    elif h_trend == "ASAGI" and l_trend == "ASAGI": return "ALCALAN", "Alçalan kanal"
    return "YATAY", "Sıkışma"

def formasyon_tespit(df):
    pivot_highs, pivot_lows = pivot_noktalari_bul(df, lookback=5)
    if len(pivot_highs) < 2 or len(pivot_lows) < 2: return None, "Yetersiz pivot"
    son_highs = [p[1] for p in pivot_highs[-3:]]
    son_lows = [p[1] for p in pivot_lows[-3:]]
    
    if len(son_highs) >= 2 and abs(son_highs[-1] - son_highs[-2]) / son_highs[-2] * 100 < 0.5 and son_lows[-1] < son_highs[-2] * 0.98:
        return "SHORT", "Çift Tepe"
    if len(son_lows) >= 2 and abs(son_lows[-1] - son_lows[-2]) / son_lows[-2] * 100 < 0.5 and son_highs[-1] > son_lows[-2] * 1.02:
        return "LONG", "Çift Dip"
    if len(son_highs) >= 3 and son_highs[-2] > son_highs[-3] and son_highs[-2] > son_highs[-1] and abs(son_highs[-3] - son_highs[-1]) / son_highs[-3] < 0.02:
        return "SHORT", "Omuz-Baş-Omuz"
    if len(son_lows) >= 3 and son_lows[-2] < son_lows[-3] and son_lows[-2] < son_lows[-1] and abs(son_lows[-3] - son_lows[-1]) / son_lows[-3] < 0.02:
        return "LONG", "Ters O-B-O"
    high_aralik = max(son_highs) - min(son_highs)
    low_aralik = max(son_lows) - min(son_lows)
    if high_aralik < son_highs[-1] * 0.02 and low_aralik < son_lows[-1] * 0.02:
        return "SIKISMA", "Üçgen/Kama"
    return None, "Formasyon yok"

def destek_direnc_seviyeleri(df):
    ph, pl = pivot_noktalari_bul(df, 3)
    return ([p[1] for p in pl[-5:]], [p[1] for p in ph[-5:]])

def mum_yapisi(df):
    if len(df) < 2: return None, ""
    s, o = df.iloc[-1], df.iloc[-2]
    govde = abs(s['c'] - s['o'])
    ust_fitil = s['h'] - max(s['o'], s['c'])
    alt_fitil = min(s['o'], s['c']) - s['l']
    if (o['c'] < o['o'] and s['c'] > s['o'] and s['c'] > o['o']): return "LONG", "Bullish Engulfing"
    if (o['c'] > o['o'] and s['c'] < s['o'] and s['c'] < o['o']): return "SHORT", "Bearish Engulfing"
    if alt_fitil > govde * 2 and ust_fitil < govde * 0.5: return "LONG", "Hammer"
    if ust_fitil > govde * 2 and alt_fitil < govde * 0.5: return "SHORT", "Shooting Star"
    return None, ""


# ==================== COİN PUAN HESAPLA ====================
def coin_puan_hesapla(symbol):
    try:
        ohlcv_1h = exchange.fetch_ohlcv(symbol, timeframe='1h', limit=PIVOT_LOOKBACK)
        ohlcv_15m = exchange.fetch_ohlcv(symbol, timeframe='15m', limit=50)
        if len(ohlcv_1h) < 30 or len(ohlcv_15m) < 30: return None, {}, 0
        
        df_1h = pd.DataFrame(ohlcv_1h, columns=['t', 'o', 'h', 'l', 'c', 'v'])
        df_15m = pd.DataFrame(ohlcv_15m, columns=['t', 'o', 'h', 'l', 'c', 'v'])
        anlik_fiyat = df_1h['c'].iloc[-1]
        
        son_kapanan_15m = df_15m.iloc[-2]
        mum_yesil = son_kapanan_15m['c'] > son_kapanan_15m['o']
        mum_kirmizi = son_kapanan_15m['c'] < son_kapanan_15m['o']

        adx_1h = ta.trend.adx(df_1h['h'], df_1h['l'], df_1h['c'], window=14).iloc[-1]
        
        # ==================== MOD BELİRLEME ====================
        if adx_1h >= ADX_ESIK_DEGERI:
            mod = "TREND"
        else:
            mod = "TESTERE"
            
        detay = {"mod": mod, "adx": round(adx_1h, 1)}

        # ==================== TESTERE MODU (MEAN REVERSION) ====================
        if mod == "TESTERE":
            rsi_15m = ta.momentum.rsi(df_15m['c'], window=14).iloc[-1]
            bb_ust = ta.volatility.bollinger_hband(df_15m['c'], window=20).iloc[-1]
            bb_alt = ta.volatility.bollinger_lband(df_15m['c'], window=20).iloc[-1]
            bb_orta = ta.volatility.bollinger_mavg(df_15m['c'], window=20).iloc[-1]
            
            # ✅ YENİ: TESTERE MODU TREND FİLTRESİ (Düşen bıçağı tutma engeli)
            ema50_1h = ta.trend.ema_indicator(df_1h['c'], window=50).iloc[-1]
            trend_asagi = anlik_fiyat < ema50_1h
            
            detay["rsi"] = round(rsi_15m, 1)
            
            long_puan = 0; short_puan = 0
            long_sebep = []; short_sebep = []
            
            if rsi_15m < 30: 
                long_puan += 10; long_sebep.append(f"RSI Aşırı Satım ({rsi_15m:.0f})")
            if rsi_15m > 70: 
                short_puan += 10; short_sebep.append(f"RSI Aşırı Alım ({rsi_15m:.0f})")
                
            if anlik_fiyat <= bb_alt: 
                long_puan += 5; long_sebep.append("BB Alt Bandı")
            if anlik_fiyat >= bb_ust: 
                short_puan += 5; short_sebep.append("BB Üst Bandı")
                
            # Fiyat orta banda çok yakınsa (SMA20) işlem yapma, ortada kalma
            if abs(anlik_fiyat - bb_orta) / bb_orta < 0.005:
                return None, {"sebep": "Testere: Fiyat Ortada (Bekle)"}, 0
                
            if long_puan >= 10 and long_puan > short_puan:
                if trend_asagi:
                    return None, {"sebep": "Testere Long İptal (Trend Aşağı)"}, 0
                detay["sebep"] = f"TESTERE LONG | {', '.join(long_sebep)}"
                detay["aciklama"] = " + ".join(long_sebep)
                return "LONG", detay, long_puan
                
            if short_puan >= 10 and short_puan > long_puan:
                detay["sebep"] = f"TESTERE SHORT | {', '.join(short_sebep)}"
                detay["aciklama"] = " + ".join(short_sebep)
                return "SHORT", detay, short_puan
                
            return None, {"sebep": "Testere: Sinyal Yok"}, 0

        # ==================== TREND MODU (MEVCUT MANTIK) ====================
        formasyon_yon, formasyon_sebep = formasyon_tespit(df_1h)
        destekler, direncler = destek_direnc_seviyeleri(df_1h)
        
        en_yakin_destek = min(destekler, key=lambda x: abs(x - anlik_fiyat)) if destekler else None
        en_yakin_direnc = min(direncler, key=lambda x: abs(x - anlik_fiyat)) if direncler else None
        
        detay.update({"en_yakin_destek": en_yakin_destek, "en_yakin_direnc": en_yakin_direnc})

        destek_mesafe = ((anlik_fiyat - en_yakin_destek) / anlik_fiyat * 100) if en_yakin_destek else 999
        direnc_mesafe = ((en_yakin_direnc - anlik_fiyat) / anlik_fiyat * 100) if en_yakin_direnc else 999
        destege_yakin = 0 <= destek_mesafe < DESTEK_DIRENC_MESAFE
        dirence_yakin = 0 <= direnc_mesafe < DESTEK_DIRENC_MESAFE
        
        pivot_highs, pivot_lows = pivot_noktalari_bul(df_1h, lookback=5)
        trend_tip, _ = trend_cizgisi_analiz(pivot_highs, pivot_lows)
        mum_yon, mum_sebep = mum_yapisi(df_15m)
        
        mfi = ta.volume.money_flow_index(high=df_1h['h'], low=df_1h['l'], close=df_1h['c'], volume=df_1h['v'], window=14).iloc[-1]
        cmf = ta.volume.chaikin_money_flow(high=df_1h['h'], low=df_1h['l'], close=df_1h['c'], volume=df_1h['v'], window=20).iloc[-1]
        hacim_oran = df_1h['v'].iloc[-3:].mean() / df_1h['v'].iloc[-20:].mean()
        
        ema9 = ta.trend.ema_indicator(df_1h['c'], window=9).iloc[-1]
        fiyat_ema9_fark = ((anlik_fiyat - ema9) / ema9) * 100
        
        btc_yon = btc_bias()
        ob_analiz = orderbook_analiz(symbol)
        funding_sonuc = funding_analiz(symbol)
        vp_analiz = volume_profile_analiz(df_1h)
        mtf = mtf_analiz(symbol)
        likidite = likidite_avi_tespit(symbol, df_15m, destekler, direncler)
        
        detay.update({"mfi": round(mfi, 1), "hacim_oran": round(hacim_oran, 2), "btc_yon": btc_yon})
        
        if mfi < MFI_ASIRI_SATIM or mfi > MFI_ASIRI_ALIM: return None, {"sebep": f"MFI Aşırı"}, 0
        if hacim_oran < HACIM_ESIK: return None, {"sebep": f"Hacim Zayıf"}, 0

        long_puan = 0; short_puan = 0
        long_sebep = []; short_sebep = []
        
        if formasyon_yon == "LONG": long_puan += 4; long_sebep.append(f"Formasyon: {formasyon_sebep}")
        elif formasyon_yon == "SHORT": short_puan += 4; short_sebep.append(f"Formasyon: {formasyon_sebep}")
        elif formasyon_yon == "SIKISMA":
            if trend_tip == "YUKSELEN": long_puan += 2
            elif trend_tip == "ALCALAN": short_puan += 2
        
        if trend_tip == "YUKSELEN": long_puan += 2; long_sebep.append("Yükselen kanal")
        elif trend_tip == "ALCALAN": short_puan += 2; short_sebep.append("Alçalan kanal")
        
        if mum_yon == "LONG": long_puan += 3; long_sebep.append(f"Mum: {mum_sebep}")
        elif mum_yon == "SHORT": short_puan += 3; short_sebep.append(f"Mum: {mum_sebep}")
        
        if destege_yakin and not dirence_yakin: long_puan += 2; long_sebep.append("Destek")
        if dirence_yakin and not destege_yakin: short_puan += 2; short_sebep.append("Direnç")
        
        if mfi > MFI_ESIK_AL: long_puan += 1
        if mfi < MFI_ESIK_SAT: short_puan += 1
        if cmf > CMF_ESIK: long_puan += 1
        if cmf < -CMF_ESIK: short_puan += 1
        if hacim_oran > 1.3: long_puan += 1; short_puan += 1
        
        if ob_analiz:
            if ob_analiz['baski'] == "ALIM": long_puan += 2; long_sebep.append("OB Alım")
            elif ob_analiz['baski'] == "SATIM": short_puan += 2; short_sebep.append("OB Satım")
            if ob_analiz['alim_duvari']: long_puan += 1
            if ob_analiz['satim_duvari']: short_puan += 1
        
        if funding_sonuc:
            if funding_sonuc['asiri_long']: short_puan += 2; short_sebep.append("Funding aşırı long")
            elif funding_sonuc['asiri_short']: long_puan += 2; long_sebep.append("Funding aşırı short")
        
        if vp_analiz and vp_analiz.get('yakin_hacim_tip'):
            if vp_analiz['yakin_hacim_tip'] == "DESTEK" and vp_analiz['yakin_hacim_mesafe'] < 1.0: long_puan += 2; long_sebep.append("VP destek")
            elif vp_analiz['yakin_hacim_tip'] == "DIRENC" and vp_analiz['yakin_hacim_mesafe'] < 1.0: short_puan += 2; short_sebep.append("VP direnç")
        
        if mtf:
            if mtf['uyum'] == "GUCLU_LONG": long_puan += 3; long_sebep.append("MTF güçlü LONG")
            elif mtf['uyum'] == "GUCLU_SHORT": short_puan += 3; short_sebep.append("MTF güçlü SHORT")
            elif mtf['uyum'] == "ZAYIF_LONG": long_puan += 1; long_sebep.append("MTF zayıf LONG")
            elif mtf['uyum'] == "ZAYIF_SHORT": short_puan += 1; short_sebep.append("MTF zayıf SHORT")
            elif mtf['uyum'] == "KARISIK": 
                long_puan -= 2; short_puan -= 2
                long_sebep.append("MTF Karışık(-2)"); short_sebep.append("MTF Karışık(-2)")
        
        if likidite:
            if likidite['yon'] == "LONG": long_puan += 4; long_sebep.append("🎣 Likidite avı")
            elif likidite['yon'] == "SHORT": short_puan += 4; short_sebep.append("🎣 Likidite avı")
        
        if fiyat_ema9_fark > 2.0: long_puan -= 3
        if fiyat_ema9_fark < -2.0: short_puan -= 3

        # Trend Modunda BTC Kararsızsa Ceza, Ters Yönse İptal
        if btc_yon == "LONG":
            short_puan = 0  
            short_sebep.append("(BTC LONG)")
        elif btc_yon == "SHORT":
            long_puan = 0   
            long_sebep.append("(BTC SHORT)")
        elif btc_yon == "KARISIK":
            long_puan -= 2; short_puan -= 2
            long_sebep.append("BTC Kararsız(-2)"); short_sebep.append("BTC Kararsız(-2)")
        
        detay["long_puan"] = long_puan
        detay["short_puan"] = short_puan
        
        if long_puan >= MIN_PUAN and long_puan > short_puan:
            if mum_yesil:
                detay["sinyal_skoru"] = long_puan
                detay["aciklama"] = " + ".join(long_sebep) + " + Mum Onayı"
                detay["sebep"] = f"TREND LONG | L:{long_puan} S:{short_puan}"
                return "LONG", detay, long_puan
            else:
                detay["sebep"] = f"L:{long_puan} (Mum Onayı Bekleniyor)"
                return None, detay, 0
        
        if short_puan >= MIN_PUAN and short_puan > long_puan:
            if mum_kirmizi:
                detay["sinyal_skoru"] = short_puan
                detay["aciklama"] = " + ".join(short_sebep) + " + Mum Onayı"
                detay["sebep"] = f"TREND SHORT | L:{long_puan} S:{short_puan}"
                return "SHORT", detay, short_puan
            else:
                detay["sebep"] = f"S:{short_puan} (Mum Onayı Bekleniyor)"
                return None, detay, 0
        
        detay["sebep"] = f"L:{long_puan} S:{short_puan}"
        return None, detay, 0
    except Exception as e:
        return None, {"hata": str(e)}, 0


# ==================== TELEGRAM ====================
def telegram_direkt_gonder(mesaj, deneme=3):
    if not TELEGRAM_TOKEN or not CHAT_ID: return False
    for i in range(deneme):
        try:
            r = requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                              json={"chat_id": CHAT_ID, "text": mesaj, "parse_mode": "Markdown"}, timeout=10)
            if r.status_code == 200: return True
        except: pass

        try:
            r = requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                              json={"chat_id": CHAT_ID, "text": mesaj}, timeout=10)
            if r.status_code == 200: return True
        except: pass
        time.sleep(2)
    return False

def telegram_kuyruga_ekle(mesaj):
    try:
        supabase.table("telegram_kuyruk").insert({"mesaj": mesaj, "deneme_sayisi": 0, "gonderildi": False}).execute()
        return True
    except: return False

def telegram_kuyruk_isle():
    try:
        response = supabase.table("telegram_kuyruk").select("*").eq("gonderildi", False).order("id").limit(10).execute()
        if not response.data: return 0
        gonderilen = 0
        for kayit in response.data:
            mesaj = kayit['mesaj']; kayit_id = kayit['id']
            deneme = kayit.get('deneme_sayisi', 0) or 0
            if deneme >= 10:
                supabase.table("telegram_kuyruk").update({"gonderildi": True}).eq("id", kayit_id).execute()
                continue
            if telegram_direkt_gonder(mesaj, deneme=2):
                supabase.table("telegram_kuyruk").update({"gonderildi": True}).eq("id", kayit_id).execute()
                gonderilen += 1
            else:
                supabase.table("telegram_kuyruk").update({"deneme_sayisi": deneme + 1}).eq("id", kayit_id).execute()
        return gonderilen
    except: return 0

def telegram_kritik(mesaj):
    if not telegram_direkt_gonder(mesaj, deneme=3):
        telegram_kuyruga_ekle(mesaj)
    return True

def kuyruk_worker():
    while True:
        try:
            if BOT_CALISIYOR_MU: telegram_kuyruk_isle()
        except: pass
        time.sleep(30)


def market_kapat(symbol, miktar, yon):
    kapat_yon = 'sell' if yon == 'LONG' else 'buy'
    try:
        exchange.create_order(symbol, 'market', kapat_yon, miktar, None, {'reduceOnly': True})
        return True
    except: return False

def tum_emirleri_iptal(symbol):
    try:
        orders = exchange.fetch_open_orders(symbol)
        for o in orders:
            try: exchange.cancel_order(o['id'], symbol)
            except: pass
    except: pass

def sayaci_artir(basarili_mi):
    with state_lock:
        if basarili_mi: ANALITIK["basarili_islem_sayisi"] = int(ANALITIK.get("basarili_islem_sayisi", 0)) + 1
        else: ANALITIK["basarisiz_islem_sayisi"] = int(ANALITIK.get("basarisiz_islem_sayisi", 0)) + 1
    hafizayi_kaydet()

def baslangic_temizligi():
    print("\n🧹 [BAŞLANGIÇ] Mevcut pozisyonlar korunuyor...", flush=True)
    try:
        raw_pos = exchange.fetch_positions()
        mevcut = [p['symbol'] for p in raw_pos if float(p.get('contracts', 0) or p.get('size', 0) or 0) > 0]
        if mevcut: telegram_kritik(f"ℹ️ BOT BAŞLATILDI (v34.1 - Hibrit + Trailing)\n\nMevcut pozisyonlar:\n{mevcut}")
        else: telegram_kritik(f"🟢 BOT BAŞLATILDI (v34.1 - Hibrit + Trailing)\n\nTrend Modu: {ADX_ESIK_DEGERI}+ ADX\nTestere Modu: {ADX_ESIK_DEGERI}- ADX\nTrailing Stop: AKTİF")
    except: pass


# ==================== POZİSYON AÇMA ====================
def pozisyon_ac(symbol, yon, anlik_fiyat, sebep, sinyal_skoru=0, aciklama="", detay={}):
    global AKTIF_SISTEMLER
    try:
        bakiye = exchange.fetch_balance()
        toplam_bakiye = float(bakiye['total'].get('USDT', 0))
        serbest = float(bakiye.get('free', {}).get('USDT', 0))
        
        exchange.set_leverage(KALDIRAC, symbol)
        market = exchange.market(symbol)
        contract_size = float(market.get('contractSize', 1.0))
        
        hedef_marj = toplam_bakiye * POZISYON_MARJ
        marj = min(hedef_marj, serbest)
        if marj < 1.0: return False
        
        hesaplanan = (marj * KALDIRAC) / anlik_fiyat / contract_size
        min_miktar = float(market['limits']['amount']['min'] or 1.0)
        if hesaplanan < min_miktar:
            zorlanan = (min_miktar * anlik_fiyat * contract_size) / KALDIRAC
            if zorlanan > hedef_marj * 1.5: return False
        
        miktar = float(exchange.amount_to_precision(symbol, max(hesaplanan, min_miktar)))
        gercek_marj = (miktar * anlik_fiyat * contract_size) / KALDIRAC
        if gercek_marj > hedef_marj * 1.5: return False
        
        # ✅ MODA GÖRE TP ve SL HESAPLAMASI
        is_testere = detay.get("mod") == "TESTERE"
        
        if is_testere:
            # Testere Modu: Vur-Kaç (Sabit küçük TP ve SL)
            if yon == "LONG":
                tp = anlik_fiyat * (1 + TESTERE_TP_YUZDE / 100)
                sl = anlik_fiyat * (1 - TESTERE_SL_YUZDE / 100)
                kapat_yon = 'sell'
            else:
                tp = anlik_fiyat * (1 - TESTERE_TP_YUZDE / 100)
                sl = anlik_fiyat * (1 + TESTERE_SL_YUZDE / 100)
                kapat_yon = 'buy'
        else:
            # Trend Modu: Mevcut Akıllı TP ve Akıllı SL
            if yon == "LONG":
                hedef_direnc = detay.get("en_yakin_direnc")
                if hedef_direnc and hedef_direnc > anlik_fiyat:
                    mesafe_tp = (hedef_direnc - anlik_fiyat) / anlik_fiyat * 100
                    if 0.5 <= mesafe_tp <= 4.0: tp = hedef_direnc * 0.999 
                    else: tp = anlik_fiyat * (1 + TP_YUZDE / 100)
                else: tp = anlik_fiyat * (1 + TP_YUZDE / 100)
                
                hedef_destek = detay.get("en_yakin_destek")
                if hedef_destek and hedef_destek < anlik_fiyat:
                    mesafe_sl = (anlik_fiyat - hedef_destek) / anlik_fiyat * 100
                    if 0.8 <= mesafe_sl <= 1.8: sl = hedef_destek * 0.998 
                    else: sl = anlik_fiyat * (1 - SL_YUZDE / 100)
                else: sl = anlik_fiyat * (1 - SL_YUZDE / 100)
                kapat_yon = 'sell'
            else:
                hedef_destek = detay.get("en_yakin_destek")
                if hedef_destek and hedef_destek < anlik_fiyat:
                    mesafe_tp = (anlik_fiyat - hedef_destek) / anlik_fiyat * 100
                    if 0.5 <= mesafe_tp <= 4.0: tp = hedef_destek * 1.001 
                    else: tp = anlik_fiyat * (1 - TP_YUZDE / 100)
                else: tp = anlik_fiyat * (1 - TP_YUZDE / 100)
                
                hedef_direnc = detay.get("en_yakin_direnc")
                if hedef_direnc and hedef_direnc > anlik_fiyat:
                    mesafe_sl = (hedef_direnc - anlik_fiyat) / anlik_fiyat * 100
                    if 0.8 <= mesafe_sl <= 1.8: sl = hedef_direnc * 1.002 
                    else: sl = anlik_fiyat * (1 + SL_YUZDE / 100)
                else: sl = anlik_fiyat * (1 + SL_YUZDE / 100)
                kapat_yon = 'buy'
        
        islem_y = 'buy' if yon == 'LONG' else 'sell'
        exchange.create_order(symbol, 'market', islem_y, miktar)
        time.sleep(1.0)
        
        tp_ok = False
        try:
            exchange.create_order(symbol, 'limit', kapat_yon, miktar, tp, {'reduceOnly': True})
            tp_ok = True
        except: pass
        
        sl_ok = False
        try:
            exchange.create_order(symbol, 'market', kapat_yon, miktar, None, {'stopPrice': sl, 'reduceOnly': True})
            sl_ok = True
        except Exception as e: print(f"Stop emri hatası: {e}", flush=True)
        
        if not sl_ok:
            try: market_kapat(symbol, miktar, yon)
            except: pass
            return False
        
        tp_mesafe_yuzde = abs(tp - anlik_fiyat) / anlik_fiyat * 100
        sl_mesafe_yuzde = abs(sl - anlik_fiyat) / anlik_fiyat * 100
        
        with state_lock:
            AKTIF_SISTEMLER[symbol] = {
                "giris_fiyati": anlik_fiyat, "yon": yon, "giris_zamani": time.time(),
                "sebep": sebep, "tp": tp, "sl": sl, "marj": gercek_marj,
                "miktar": miktar, "tp_order": tp_ok, "sl_order": sl_ok,
                "contract_size": contract_size, "sinyal_skoru": sinyal_skoru,
                "onceki_roe": 0.0,
                "en_yuksek_roe": 0.0 # ✅ YENİ: Kâr takibi için zirve noktası
            }
        
        hafizayi_kaydet()
        islem_no = len(AKTIF_SISTEMLER)
        
        mod_str = "TESTERE" if is_testere else "TREND"
        mesaj = (
            f"🏆 EN İYİ SİNYAL ({islem_no}. İşlem) [{mod_str}]\n"
            f"📌 {symbol} | {yon}\n"
            f"⭐ Puan: {sinyal_skoru}\n"
            f"📋 {aciklama}\n"
            f"🎯 Giriş: {anlik_fiyat:.6f}\n"
            f"💰 TP: {tp:.6f} (%{tp_mesafe_yuzde:.2f})\n"
            f"🛑 SL: {sl:.6f} (%{sl_mesafe_yuzde:.2f})\n"
            f"💵 Marj: {gercek_marj:.2f} | Kaldıraç: {KALDIRAC}x\n"
            f"📌 Açık: {islem_no}/{MAKS_POZISYON}"
        )
        telegram_kritik(mesaj)
        
        print(f"  🚀 [İŞLEM AÇILDI] [{mod_str}] {symbol} | {yon} | Giriş: {anlik_fiyat:.6f} | TP: {tp:.6f} (%{tp_mesafe_yuzde:.2f}) | SL: {sl:.6f} (%{sl_mesafe_yuzde:.2f})", flush=True)
        
        return True
    except Exception as e:
        print(f"  ⚠️ Pozisyon {symbol}: {e}", flush=True)
        return False


# ==================== SL/TP KONTROLÜ ====================
def manuel_sl_tp_kontrol(aktif_borsa_map):
    global AKTIF_SISTEMLER
    with state_lock: kopya = list(AKTIF_SISTEMLER.items())
    
    for symbol, bilgi in kopya:
        if symbol not in aktif_borsa_map: continue
        try:
            ticker = exchange.fetch_ticker(symbol)
            anlik = float(ticker['last'])
        except: continue
        
        yon = bilgi.get("yon", "LONG")
        sl = float(bilgi.get("sl", 0))
        tp = float(bilgi.get("tp", 0))
        miktar = float(bilgi.get("miktar", 0))
        giris = float(bilgi.get("giris_fiyati", 0))
        contract_size = float(bilgi.get("contract_size", 1.0))
        
        kar = 0
        try:
            positions = exchange.fetch_positions([symbol])
            for p in positions:
                if p['symbol'] == symbol:
                    kar = float(p.get('unrealizedPnl', 0))
                    break
        except:
            if yon == "LONG": kar = (anlik - giris) * miktar * contract_size
            else: kar = (giris - anlik) * miktar * contract_size
        
        # ✅ YENİ: TRAILING STOP (KÂR TAKİPÇİSİ) MANTIĞI
        if TRAILING_STOP_AKTIF:
            # Anlık ROE hesapla (Kaldıraçsız kâr * Kaldıraç)
            if yon == "LONG": anlik_roe = ((anlik - giris) / giris) * 100 * KALDIRAC
            else: anlik_roe = ((giris - anlik) / giris) * 100 * KALDIRAC
            
            # Eğer zirve ROE daha önce kaydedilmemişse veya anlık ROE zirveyi geçtiyse, zirveyi güncelle
            if anlik_roe > bilgi.get("en_yuksek_roe", 0.0):
                with state_lock:
                    AKTIF_SISTEMLER[symbol]["en_yuksek_roe"] = anlik_roe
            
            zirve_roe = bilgi.get("en_yuksek_roe", 0.0)
            
            # Kâr takibi başladı mı? (Örn: %4'ü geçtiyse)
            if zirve_roe >= TRAILING_BASLANGIC_ROE:
                # Zirveden ne kadar düştük?
                dusus = zirve_roe - anlik_roe
                if dusus >= TRAILING_DUSUS_YUZDE:
                    print(f"  🛡️ [TRAILING STOP] {symbol} | Zirve ROE: %{zirve_roe:.2f} -> Düşüş: %{anlik_roe:.2f}", flush=True)
                    tum_emirleri_iptal(symbol)
                    if market_kapat(symbol, miktar, yon):
                        with state_lock:
                            if symbol in AKTIF_SISTEMLER: del AKTIF_SISTEMLER[symbol]
                            COIN_COOLDOWNLAR[symbol] = {"zaman": float(time.time() + COOLDOWN_TP), "son_yon": yon}
                        sayaci_artir(True)
                        telegram_kritik(f"💰 TRAILING STOP (Kâr Korundu)\n📌 {symbol} {yon}\n📈 Zirve ROE: %{zirve_roe:.2f}\n💵 PnL: +{kar:.4f} USDT")
                    continue

        # Normal SL
        sl_tetiklendi = (yon == "LONG" and anlik <= sl) or (yon == "SHORT" and anlik >= sl)
        if sl_tetiklendi:
            tum_emirleri_iptal(symbol)
            if market_kapat(symbol, miktar, yon):
                with state_lock:
                    if symbol in AKTIF_SISTEMLER: del AKTIF_SISTEMLER[symbol]
                    COIN_COOLDOWNLAR[symbol] = {"zaman": float(time.time() + COOLDOWN_SL), "son_yon": yon}
                sayaci_artir(False)
                hafizayi_kaydet()
                telegram_kritik(f"🛑 SL\n📌 {symbol} {yon}\n💵 PnL: {kar:.4f} USDT")
                print(f"  🛑 [SL TETİKLENDİ] {symbol} | PnL: {kar:.4f}", flush=True)
            continue
        
        # Normal TP
        if tp > 0:
            tp_tetiklendi = (yon == "LONG" and anlik >= tp) or (yon == "SHORT" and anlik <= tp)
            if tp_tetiklendi:
                tum_emirleri_iptal(symbol)
                if market_kapat(symbol, miktar, yon):
                    with state_lock:
                        if symbol in AKTIF_SISTEMLER: del AKTIF_SISTEMLER[symbol]
                        COIN_COOLDOWNLAR[symbol] = {"zaman": float(time.time() + COOLDOWN_TP), "son_yon": yon}
                    sayaci_artir(True)
                    hafizayi_kaydet()
                    telegram_kritik(f"💰 TP\n📌 {symbol} {yon}\n💵 PnL: +{kar:.4f} USDT")
                    print(f"  💰 [TP TETİKLENDİ] {symbol} | PnL: {kar:.4f}", flush=True)
            continue


# ==================== KAPANAN POZİSYONLAR ====================
def kapanan_pozisyonlari_kontrol(aktif_semboller_seti):
    global AKTIF_SISTEMLER
    try:
        anlik_aktif = list(aktif_semboller_seti)
        for eski in list(AKTIF_SISTEMLER.keys()):
            if eski not in anlik_aktif:
                bilgi = AKTIF_SISTEMLER[eski]
                g = float(bilgi.get("giris_fiyati", 0))
                y = bilgi.get("yon", "LONG")
                miktar = float(bilgi.get("miktar", 0))
                cs = float(bilgi.get("contract_size", 1.0))
                
                karli = False
                pnl = 0
                try:
                    t = exchange.fetch_ticker(eski)
                    cikis = float(t['last'])
                    if y == "LONG": pnl = (cikis - g) * miktar * cs
                    else: pnl = (g - cikis) * miktar * cs
                    karli = pnl > 0
                except Exception as ex: karli = True
                
                sayaci_artir(karli)
                with state_lock:
                    COIN_COOLDOWNLAR[eski] = {"zaman": float(time.time() + COOLDOWN_VARSAYILAN), "son_yon": y}
                    if eski in AKTIF_SISTEMLER: del AKTIF_SISTEMLER[eski]
                hafizayi_kaydet()
                
                if karli: telegram_kritik(f"✅ TP (Borsa)\n📌 {eski} {y}\n💵 +{pnl:.4f} USDT")
                else: telegram_kritik(f"❌ SL (Borsa)\n📌 {eski} {y}\n💵 {pnl:.4f} USDT")
    except Exception as e: print(f"⚠️ Kapanış kontrol hatası: {e}", flush=True)


# ==================== ANA DÖNGÜ ====================
def ana_dongu():
    global GUNLUK_BASLANGIC_BAKIYE
    print(f"🚀 [BAŞLANGIÇ] v34.1 - Hibrit (Trend & Testere) + Trailing Stop Aktif", flush=True)
    
    try:
        exchange.load_markets()
        GUNLUK_BASLANGIC_BAKIYE = float(exchange.fetch_balance()['total'].get('USDT', 0))
    except: pass
    
    baslangic_temizligi()
    dinamik_liste_guncelle(zorla=True)
    dongu = 0
    
    while True:
        try:
            if not BOT_CALISIYOR_MU: time.sleep(5); continue
            dongu += 1
            print(f"\n🔄 [DÖNGÜ #{dongu}] {datetime.now().strftime('%H:%M:%S')}", flush=True)
            dinamik_liste_guncelle()
            
            try:
                b = exchange.fetch_balance()
                su_an = float(b['total'].get('USDT', 0))
                if GUNLUK_BASLANGIC_BAKIYE and GUNLUK_BASLANGIC_BAKIYE > 0:
                    gk = (su_an - GUNLUK_BASLANGIC_BAKIYE) / GUNLUK_BASLANGIC_BAKIYE
                    print(f"💰 Kasa: {su_an:.2f} | Günlük: %{gk*100:+.2f}", flush=True)
                    if gk <= -GUNLUK_ZARAR_LIMIT:
                        telegram_kritik(f"🛑 KILL-SWITCH")
                        time.sleep(3600)
                        GUNLUK_BASLANGIC_BAKIYE = su_an
                        continue
            except: pass
            
            try:
                raw_pos = exchange.fetch_positions()
                aktif_borsa_map = {}
                aktif_semboller_seti = set()
                for p in raw_pos:
                    k = float(p.get('contracts', 0) or p.get('size', 0) or 0)
                    if k > 0:
                        aktif_borsa_map[p['symbol']] = p
                        aktif_semboller_seti.add(p['symbol'])
                
                if aktif_semboller_seti:
                    print(f"📌 Açık Pozisyonlar: {list(aktif_semboller_seti)}", flush=True)
                    for sym in list(aktif_semboller_seti):
                        try:
                            p_info = aktif_borsa_map[sym]
                            yon = str(p_info.get('side', '')).upper()
                            giris = float(p_info.get('entryPrice', 0))
                            pnl = float(p_info.get('unrealizedPnl', 0))
                            lev = int(p_info.get('leverage', 1))
                            tick = exchange.fetch_ticker(sym)
                            anlik = float(tick['last'])
                            
                            if yon == "LONG": roe = ((anlik - giris) / giris) * 100 * lev
                            else: roe = ((giris - anlik) / giris) * 100 * lev
                            
                            durum = "🟢" if pnl >= 0 else "🔴"
                            print(f"     {durum} {sym.replace('/USDT:USDT', '')} | {yon} ({lev}x) | Giriş: {giris:.4f} | Anlık: {anlik:.4f} | PnL: {pnl:+.4f} | ROE: %{roe:+.2f}", flush=True)
                        except: pass
                else:
                    print(f"📌 Açık: YOK", flush=True)
                    
            except Exception as e:
                print(f"Pozisyon çekme hatası: {e}", flush=True)
                aktif_borsa_map = {}
                aktif_semboller_seti = set()
            
            if aktif_semboller_seti:
                manuel_sl_tp_kontrol(aktif_borsa_map)
                kapanan_pozisyonlari_kontrol(aktif_semboller_seti)
            
            limit_dolu = len(aktif_semboller_seti) >= MAKS_POZISYON
            if limit_dolu:
                print(f"  ⛔ Limit dolu ({len(aktif_semboller_seti)}/{MAKS_POZISYON}), yeni sinyal aranmıyor.", flush=True)
                time.sleep(10)
                continue

            print(f"\n🔍 1. AŞAMA: TÜM COİNLERİ TARA VE PUANLA", flush=True)
            adaylar = []
            
            for symbol in DINAMIK_LISTE:
                if not BOT_CALISIYOR_MU: break
                if symbol in aktif_semboller_seti: continue
                
                with state_lock:
                    cd = COIN_COOLDOWNLAR.get(symbol)
                    if cd:
                        z = cd.get("zaman", 0) if isinstance(cd, dict) else float(cd)
                        if z > time.time(): continue
                
                karar, detay, puan = coin_puan_hesapla(symbol)
                sym_kisa = symbol.replace('/USDT:USDT', '')
                
                if karar is not None and puan >= MIN_PUAN:
                    adaylar.append((symbol, karar, detay, puan))
                    print(f"  ✅ {sym_kisa}: {karar} | Puan: {puan} | Mod: {detay.get('mod','?')}", flush=True)
                else:
                    print(f"  ⏭️  {sym_kisa}: Puan {puan}/{MIN_PUAN} | {detay.get('sebep', '')[:60]}", flush=True)
            
            if not adaylar:
                print(f"  📭 Uygun aday yok", flush=True)
                time.sleep(10)
                continue
            
            adaylar.sort(key=lambda x: x[3], reverse=True)
            en_iyiler = adaylar[:TOP_N_ADAY]
            
            print(f"\n🏆 2. AŞAMA: EN İYİ ADAYLAR", flush=True)
            for i, (sym, karar, detay, puan) in enumerate(en_iyiler, 1):
                durum = "👀 İZLE" if limit_dolu else "🚀 AÇILACAK"
                print(f"     {i}. {sym.replace('/USDT:USDT','')}: {karar} | Puan: {puan} | Mod: {detay.get('mod')} | {durum}", flush=True)
            
            if not limit_dolu:
                for sym, karar, detay, puan in en_iyiler:
                    if len(aktif_semboller_seti) >= MAKS_POZISYON: break
                    if sym in aktif_semboller_seti: continue
                    
                    ticker = exchange.fetch_ticker(sym)
                    anlik_fiyat = float(ticker['last'])
                    
                    if pozisyon_ac(sym, karar, anlik_fiyat, detay.get('sebep', ''), puan, detay.get('aciklama', ''), detay):
                        aktif_semboller_seti.add(sym)
            else:
                print(f"  ⛔ Limit dolu, izleme modu", flush=True)
            
        except Exception as e:
            print(f"⚠️ Ana döngü: {e}", flush=True)
            import traceback
            traceback.print_exc()
        
        time.sleep(10)


# ==================== TELEGRAM KOMUTLARI ====================
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
                if yon == "LONG": roe = ((guncel - giris) / giris) * 100 * kaldirac_p
                else: roe = ((giris - guncel) / giris) * 100 * kaldirac_p
                if roe >= 0.5: emoji = "🟢"
                elif roe >= -1: emoji = "🟡"
                else: emoji = "🔴"
                sl_bilgi = ""
                if p['symbol'] in AKTIF_SISTEMLER:
                    bilgi = AKTIF_SISTEMLER[p['symbol']]
                    sl_bilgi = f"\n  🛑 SL: `{bilgi.get('sl', 0):.6f}` | 💰 TP: `{bilgi.get('tp', 0):.6f}`"
                pos_detay += (
                    f"\n{emoji} `{sym}` | *{yon}* ({kaldirac_p}x){sl_bilgi}\n"
                    f"  Giriş: `{giris}` → Anlık: `{guncel}`\n"
                    f"  Marj: `{marj:.2f}` | ROE: `%{roe:+.2f}` | PnL: `{float(p.get('unrealizedPnl', 0)):+.3f}`"
                )
        else: pos_detay = "\n\n📌 Açık pozisyon yok."
        
        liste_str = "\n".join([f"• `{s.replace('/USDT:USDT','')}`" for s in DINAMIK_LISTE[:10]]) if DINAMIK_LISTE else "Boş"
        
        mesaj = (
            f"📊 *TREND BOT (v34.1 - Hibrit + Trailing)*\n\n"
            f"💰 Kasa: `{total:.2f} USDT`\n"
            f"💵 Toplam PnL: `{pnl:+.2f} USDT`\n"
            f"📌 Açık: `{len(pos)}` / `{MAKS_POZISYON}`"
            f"{pos_detay}\n\n"
            f"📋 *Coin Havuzu:*\n{liste_str}\n\n"
            f"✅ TP: `{bas}` | ❌ SL: `{basz}`\n"
            f"📈 Başarı: `%{oran:.1f}`\n"
            f"⭐ Min Puan: `{MIN_PUAN}`\n"
            f"⚙️ Mod: `Trend (ADX>{ADX_ESIK_DEGERI})` | `Testere (ADX<{ADX_ESIK_DEGERI})`\n"
            f"🛡️ Trailing Stop: `{TRAILING_BASLANGIC_ROE}% ROE`\n"
            f"💵 Marj: `%{POZISYON_MARJ*100:.0f}` | Kaldıraç: `{KALDIRAC}x`"
        )
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
    await update.message.reply_text("🟢 Trend Bot (v34.1 - Hibrit) aktif!")


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
                if market_kapat(p['symbol'], k, y): kapatilan += 1
        with state_lock: AKTIF_SISTEMLER.clear()
        hafizayi_kaydet()
        await update.message.reply_text(f"✅ {kapatilan} pozisyon kapatıldı.")
    except Exception as e:
        await update.message.reply_text(f"Hata: {e}")


async def liste_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != int(CHAT_ID): return
    if not DINAMIK_LISTE:
        await update.message.reply_text("⚠️ Liste boş")
        return
    liste_str = "📋 *DİNAMİK LİSTE*\n\n"
    for i, s in enumerate(DINAMIK_LISTE, 1):
        liste_str += f"{i}. `{s.replace('/USDT:USDT','')}`\n"
    await update.message.reply_text(liste_str, parse_mode='Markdown')

async def kuyruk_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != int(CHAT_ID): return
    try:
        response = supabase.table("telegram_kuyruk").select("*").eq("gonderildi", False).execute()
        bekleyen = len(response.data) if response.data else 0
        await update.message.reply_text(f"📬 *TELEGRAM KUYRUK*\n\n⏳ Bekleyen: `{bekleyen}`", parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"Hata: {e}")


# ==================== MAIN ====================
async def main():
    web_thread = threading.Thread(target=run_web, daemon=True)
    web_thread.start()
    
    kuyruk_thread = threading.Thread(target=kuyruk_worker, daemon=True)
    kuyruk_thread.start()
    
    app_tg = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    try:
        requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/deleteWebhook?drop_pending_updates=True", timeout=5)
    except: pass
    
    app_tg.add_handler(CommandHandler("durum", durum_komutu))
    app_tg.add_handler(CommandHandler("baslat", baslat_komutu))
    app_tg.add_handler(CommandHandler("durdur", durdur_komutu))
    app_tg.add_handler(CommandHandler("kapat", kapat_komutu))
    app_tg.add_handler(CommandHandler("liste", liste_komutu))
    app_tg.add_handler(CommandHandler("kuyruk", kuyruk_komutu))
    
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

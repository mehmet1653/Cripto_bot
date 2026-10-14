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
exchange.set_sandbox_mode(True)

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

MIN_HACIM_24S = 10_000_000
MIN_DEGISIM_YUZDE = 1.0
MIN_FIYAT = 1.0
MAX_FIYAT = 300.0

HOT_MOVER_YUZDE = 5.0
HOT_MOVER_MAX = 2
SIKISAN_MAX = 4

BOT_CALISIYOR_MU = True
state_lock = threading.Lock()

# Risk yönetimi
KALDIRAC = 5
POZISYON_MARJ = 0.10
MAKS_POZISYON = 2
SL_YUZDE = 1.5
TP_YUZDE = 2.5
TOPLAM_ZARAR_LIMIT = 2.0

# Formasyon ayarları
PIVOT_LOOKBACK = 50
DESTEK_DIRENC_MESAFE = 1.0

# Cooldown
COOLDOWN_TP = 15 * 60
COOLDOWN_SL = 30 * 60
COOLDOWN_VARSAYILAN = 30 * 60

# Order Flow
MFI_ESIK_AL = 52
MFI_ESIK_SAT = 48
CMF_ESIK = 0.03
HACIM_ESIK = 1.0
MFI_ASIRI_SATIM = 15
MFI_ASIRI_ALIM = 85

GEREKLI_SINYAL = 6

# Kill-switch
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

# ==================== SIKIŞMA ANALİZİ ====================
def sikisma_analizi(symbol):
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe='15m', limit=50)
        if len(ohlcv) < 25:
            return 0, {}
        
        df = pd.DataFrame(ohlcv, columns=['t', 'o', 'h', 'l', 'c', 'v'])
        
        bb_ust = ta.volatility.bollinger_hband(df['c'], window=20)
        bb_alt = ta.volatility.bollinger_lband(df['c'], window=20)
        bb_genislik = ((bb_ust - bb_alt) / df['c']) * 100
        bb_genislik_son = bb_genislik.iloc[-1]
        bb_genislik_ort = bb_genislik.iloc[-20:].mean()
        
        hacim_son5 = df['v'].iloc[-5:].mean()
        hacim_ort20 = df['v'].iloc[-20:].mean()
        hacim_oran = hacim_son5 / hacim_ort20 if hacim_ort20 > 0 else 1.0
        
        rsi = ta.momentum.rsi(df['c'], window=14)
        rsi_son5_std = rsi.iloc[-5:].std()
        
        ema9 = ta.trend.ema_indicator(df['c'], window=9).iloc[-1]
        fiyat = df['c'].iloc[-1]
        ema_yakinlik = abs(fiyat - ema9) / ema9 * 100
        
        sikisma_skoru = 0
        
        if bb_genislik_son < bb_genislik_ort * 0.7:
            sikisma_skoru += 1
        if hacim_oran < 0.6:
            sikisma_skoru += 1
        if rsi_son5_std < 3.0:
            sikisma_skoru += 1
        if ema_yakinlik < 0.5:
            sikisma_skoru += 1
        
        return sikisma_skoru, {}
    except:
        return 0, {}

# ==================== DİNAMİK COIN HAVUZU ====================
def dinamik_liste_guncelle(zorla=False):
    global DINAMIK_LISTE, SON_LISTE_GUNCELLEME
    
    if not zorla and (time.time() - SON_LISTE_GUNCELLEME < LISTE_GUNCELLEME_SURESI):
        return
    
    print(f"\n🔄 [DİNAMİK LİSTE] Güncelleniyor...", flush=True)
    SON_LISTE_GUNCELLEME = time.time()
    
    try:
        tickers = exchange.fetch_tickers()
        normal_adaylar = []
        hot_movers = []
        sikisanlar = []
        
        tum_semboller = [s for s in tickers.keys() if ':USDT' in s and s not in KARA_LISTE]
        
        for sym in tum_semboller:
            try:
                t = tickers[sym]
                hacim = float(t.get('quoteVolume', 0) or 0)
                degisim = float(t.get('percentage', 0) or 0)
                abs_degisim = abs(degisim)
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
                except:
                    continue
                
                if abs_degisim < MIN_DEGISIM_YUZDE:
                    sikisma_skoru, _ = sikisma_analizi(sym)
                    if sikisma_skoru >= 3:
                        sikisanlar.append({
                            "symbol": sym, "skor": sikisma_skoru * 1000,
                            "degisim": degisim, "hacim": hacim, "fiyat": fiyat,
                            "tip": "SIKISAN", "sikisma": sikisma_skoru
                        })
                    continue
                
                if abs_degisim >= HOT_MOVER_YUZDE:
                    hot_movers.append({
                        "symbol": sym, "skor": abs_degisim * 100,
                        "degisim": degisim, "hacim": hacim, "fiyat": fiyat,
                        "tip": "HOT"
                    })
                else:
                    skor = (hacim / 1_000_000) * abs_degisim
                    normal_adaylar.append({
                        "symbol": sym, "skor": skor,
                        "degisim": degisim, "hacim": hacim, "fiyat": fiyat,
                        "tip": "NORMAL"
                    })
            except:
                continue
        
        hot_movers.sort(key=lambda x: x['skor'], reverse=True)
        normal_adaylar.sort(key=lambda x: x['skor'], reverse=True)
        sikisanlar.sort(key=lambda x: x['skor'], reverse=True)
        
        sikisan_secim = sikisanlar[:SIKISAN_MAX]
        hot_secim = hot_movers[:HOT_MOVER_MAX]
        normal_secim = normal_adaylar[:LISTE_BOYUT - len(sikisan_secim) - len(hot_secim)]
        
        tum_secim = sikisan_secim + hot_secim + normal_secim
        
        yeni_liste = [a['symbol'] for a in tum_secim]
        
        if yeni_liste:
            DINAMIK_LISTE = yeni_liste
            print(f"✅ [DİNAMİK] {len(DINAMIK_LISTE)} coin:", flush=True)
            for a in tum_secim:
                if a['tip'] == "HOT":
                    tip_emoji = "🔥"
                elif a['tip'] == "SIKISAN":
                    tip_emoji = "🔷"
                else:
                    tip_emoji = "  "
                
                ekstra = ""
                if a['tip'] == "SIKISAN":
                    ekstra = f" [Sıkışma:{a.get('sikisma', 0)}/4]"
                
                print(f"   {tip_emoji} {a['symbol'].replace('/USDT:USDT','')} | %{a['degisim']:+.1f} | {a['hacim']/1_000_000:.0f}M{ekstra}", flush=True)
    except Exception as e:
        print(f"⚠️ [DİNAMİK] Hata: {e}", flush=True)

# ==================== ANALİZ FONKSİYONLARI ====================
def coklu_tf_trend(symbol):
    try:
        sonuclar = {}
        for tf, limit, key in [('15m', 30, 'tf15'), ('1h', 50, 'tf60'), ('4h', 40, 'tf240')]:
            ohlcv = exchange.fetch_ohlcv(symbol, timeframe=tf, limit=limit)
            if len(ohlcv) < 20:
                sonuclar[key] = "YATAY"; continue
            df = pd.DataFrame(ohlcv, columns=['t', 'o', 'h', 'l', 'c', 'v'])
            ema9 = ta.trend.ema_indicator(df['c'], window=9).iloc[-1]
            ema21 = ta.trend.ema_indicator(df['c'], window=21).iloc[-1]
            fark = ((ema9 - ema21) / ema21) * 100
            if fark > 0.1: sonuclar[key] = "YUKARI"
            elif fark < -0.1: sonuclar[key] = "ASAGI"
            else: sonuclar[key] = "YATAY"
        return sonuclar
    except:
        return {'tf15': "YATAY", 'tf60': "YATAY", 'tf240': "YATAY"}

def btc_bias():
    try:
        ohlcv = exchange.fetch_ohlcv('BTC/USDT:USDT', timeframe='1h', limit=50)
        df = pd.DataFrame(ohlcv, columns=['t', 'o', 'h', 'l', 'c', 'v'])
        ema20 = ta.trend.ema_indicator(df['c'], window=20).iloc[-1]
        ema50 = ta.trend.ema_indicator(df['c'], window=50).iloc[-1]
        fiyat = df['c'].iloc[-1]
        if fiyat > ema20 > ema50: return "LONG"
        elif fiyat < ema20 < ema50: return "SHORT"
        else: return "KARISIK"
    except: return "KARISIK"

def momentum_ivmesi(symbol):
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe='15m', limit=20)
        df = pd.DataFrame(ohlcv, columns=['t', 'o', 'h', 'l', 'c', 'v'])
        closes = df['c'].values
        son3 = abs(closes[-1] - closes[-4])
        onceki3 = abs(closes[-4] - closes[-7])
        if onceki3 == 0: return "SABIT"
        oran = son3 / onceki3
        if oran > 1.3: return "HIZLANIYOR"
        elif oran < 0.5: return "YAVASLIYOR"
        else: return "SABIT"
    except: return "SABIT"

def swing_analizi(symbol):
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe='15m', limit=30)
        df = pd.DataFrame(ohlcv, columns=['t', 'o', 'h', 'l', 'c', 'v'])
        yuksekler = df['h'].values[-10:]
        dusukler = df['l'].values[-10:]
        son_dusuk = min(dusukler[-5:])
        onceki_dusuk = min(dusukler[:5])
        son_yuksek = max(yuksekler[-5:])
        onceki_yuksek = max(yuksekler[:5])
        yukselen_dip = son_dusuk > onceki_dusuk
        dusen_tepe = son_yuksek < onceki_yuksek
        yukselen_tepe = son_yuksek > onceki_yuksek
        dusen_dip = son_dusuk < onceki_dusuk
        if yukselen_dip and yukselen_tepe: return "YUKARI_TREND"
        elif dusen_dip and dusen_tepe: return "ASAGI_TREND"
        elif yukselen_dip and dusen_tepe: return "SIKISMA"
        else: return "KARISIK"
    except: return "KARISIK"

# ==================== FORMASYON TESPİTİ ====================
def pivot_noktalari_bul(df, lookback=5):
    """Yerel min/max (pivot) noktalarını bul."""
    pivot_highs = []
    pivot_lows = []
    
    for i in range(lookback, len(df) - lookback):
        pencere_high = df['h'].iloc[i-lookback:i+lookback+1]
        if df['h'].iloc[i] == pencere_high.max():
            pivot_highs.append((i, df['h'].iloc[i]))
        
        pencere_low = df['l'].iloc[i-lookback:i+lookback+1]
        if df['l'].iloc[i] == pencere_low.min():
            pivot_lows.append((i, df['l'].iloc[i]))
    
    return pivot_highs, pivot_lows


def trend_cizgisi_analiz(pivot_highs, pivot_lows):
    """Pivot noktalarından trend çizgisi çıkar."""
    if len(pivot_highs) < 2 or len(pivot_lows) < 2:
        return "YATAY", "Yetersiz veri"
    
    son_high_1 = pivot_highs[-1][1]
    son_high_2 = pivot_highs[-2][1]
    son_low_1 = pivot_lows[-1][1]
    son_low_2 = pivot_lows[-2][1]
    
    high_trend = "YUKARI" if son_high_1 > son_high_2 else "ASAGI"
    low_trend = "YUKARI" if son_low_1 > son_low_2 else "ASAGI"
    
    if high_trend == "YUKARI" and low_trend == "YUKARI":
        return "YUKSELEN", "Yükselen kanal"
    elif high_trend == "ASAGI" and low_trend == "ASAGI":
        return "ALCALAN", "Alçalan kanal"
    else:
        return "YATAY", "Sıkışma"


def formasyon_tespit(df):
    """Klasik formasyonları tespit et."""
    pivot_highs, pivot_lows = pivot_noktalari_bul(df, lookback=5)
    
    if len(pivot_highs) < 2 or len(pivot_lows) < 2:
        return None, "Yetersiz pivot"
    
    son_highs = [p[1] for p in pivot_highs[-3:]]
    son_lows = [p[1] for p in pivot_lows[-3:]]
    
    # Çift Tepe
    if len(son_highs) >= 2:
        fark = abs(son_highs[-1] - son_highs[-2]) / son_highs[-2] * 100
        if fark < 0.5 and son_lows[-1] < son_highs[-2] * 0.98:
            return "SHORT", "Çift Tepe"
    
    # Çift Dip
    if len(son_lows) >= 2:
        fark = abs(son_lows[-1] - son_lows[-2]) / son_lows[-2] * 100
        if fark < 0.5 and son_highs[-1] > son_lows[-2] * 1.02:
            return "LONG", "Çift Dip"
    
    # Omuz-Baş-Omuz
    if len(son_highs) >= 3:
        sol_omuz = son_highs[-3]
        bas = son_highs[-2]
        sag_omuz = son_highs[-1]
        if bas > sol_omuz and bas > sag_omuz and abs(sol_omuz - sag_omuz) / sol_omuz < 0.02:
            return "SHORT", "Omuz-Baş-Omuz"
    
    # Ters OBO
    if len(son_lows) >= 3:
        sol_omuz = son_lows[-3]
        bas = son_lows[-2]
        sag_omuz = son_lows[-1]
        if bas < sol_omuz and bas < sag_omuz and abs(sol_omuz - sag_omuz) / sol_omuz < 0.02:
            return "LONG", "Ters O-B-O"
    
    # Üçgen/Kama (sıkışma)
    high_aralik = max(son_highs) - min(son_highs)
    low_aralik = max(son_lows) - min(son_lows)
    
    if high_aralik < son_highs[-1] * 0.02 and low_aralik < son_lows[-1] * 0.02:
        return "SIKISMA", "Üçgen/Kama"
    
    return None, "Formasyon yok"


def destek_direnc_seviyeleri(df):
    """Son 50 mumdan destek/direnç seviyeleri."""
    pivot_highs, pivot_lows = pivot_noktalari_bul(df, lookback=3)
    
    direncler = sorted([p[1] for p in pivot_highs[-5:]]) if pivot_highs else []
    destekler = sorted([p[1] for p in pivot_lows[-5:]], reverse=True) if pivot_lows else []
    
    return destekler, direncler


def mum_yapisi(df):
    """Son mumun yapısı."""
    if len(df) < 2:
        return None, ""
    
    son = df.iloc[-1]
    onceki = df.iloc[-2]
    
    govde = abs(son['c'] - son['o'])
    ust_fitil = son['h'] - max(son['o'], son['c'])
    alt_fitil = min(son['o'], son['c']) - son['l']
    toplam = son['h'] - son['l']
    
    if toplam == 0:
        return None, ""
    
    # Bullish Engulfing
    if (onceki['c'] < onceki['o'] and son['c'] > son['o'] and
        son['c'] > onceki['o'] and son['o'] < onceki['c']):
        return "LONG", "Bullish Engulfing"
    
    # Bearish Engulfing
    if (onceki['c'] > onceki['o'] and son['c'] < son['o'] and
        son['c'] < onceki['o'] and son['o'] > onceki['c']):
        return "SHORT", "Bearish Engulfing"
    
    # Hammer
    if alt_fitil > govde * 2 and ust_fitil < govde * 0.5 and govde > 0:
        return "LONG", "Hammer"
    
    # Shooting Star
    if ust_fitil > govde * 2 and alt_fitil < govde * 0.5 and govde > 0:
        return "SHORT", "Shooting Star"
    
    return None, ""

# ==================== ORDER FLOW ANALİZ (FORMASYON DESTEKLİ) ====================
def order_flow_analiz(symbol):
    try:
        ohlcv_1h = exchange.fetch_ohlcv(symbol, timeframe='1h', limit=PIVOT_LOOKBACK)
        if len(ohlcv_1h) < 30:
            return None, {}
        
        df_1h = pd.DataFrame(ohlcv_1h, columns=['t', 'o', 'h', 'l', 'c', 'v'])
        anlik_fiyat = df_1h['c'].iloc[-1]
        
        ohlcv_15m = exchange.fetch_ohlcv(symbol, timeframe='15m', limit=50)
        if len(ohlcv_15m) < 30:
            return None, {}
        df_15m = pd.DataFrame(ohlcv_15m, columns=['t', 'o', 'h', 'l', 'c', 'v'])
        
        # 1. FORMASYON
        formasyon_yon, formasyon_sebep = formasyon_tespit(df_1h)
        
        # 2. DESTEK/DİRENÇ
        destekler, direncler = destek_direnc_seviyeleri(df_1h)
        
        en_yakin_destek = min(destekler, key=lambda x: abs(x - anlik_fiyat)) if destekler else None
        en_yakin_direnc = min(direncler, key=lambda x: abs(x - anlik_fiyat)) if direncler else None
        
        destek_mesafe = ((anlik_fiyat - en_yakin_destek) / anlik_fiyat * 100) if en_yakin_destek else 999
        direnc_mesafe = ((en_yakin_direnc - anlik_fiyat) / anlik_fiyat * 100) if en_yakin_direnc else 999
        
        destege_yakin = 0 <= destek_mesafe < DESTEK_DIRENC_MESAFE
        dirence_yakin = 0 <= direnc_mesafe < DESTEK_DIRENC_MESAFE
        
        # 3. TREND
        pivot_highs, pivot_lows = pivot_noktalari_bul(df_1h, lookback=5)
        trend_tip, trend_sebep = trend_cizgisi_analiz(pivot_highs, pivot_lows)
        
        # 4. MUM YAPISI
        mum_yon, mum_sebep = mum_yapisi(df_15m)
        
        # 5. MFI + CMF + HACİM
        mfi = ta.volume.money_flow_index(high=df_1h['h'], low=df_1h['l'], 
                                          close=df_1h['c'], volume=df_1h['v'], window=14).iloc[-1]
        cmf = ta.volume.chaikin_money_flow(high=df_1h['h'], low=df_1h['l'], 
                                            close=df_1h['c'], volume=df_1h['v'], window=20).iloc[-1]
        
        son3_hacim = df_1h['v'].iloc[-3:].mean()
        ort20_hacim = df_1h['v'].iloc[-20:].mean()
        hacim_oran = son3_hacim / ort20_hacim if ort20_hacim > 0 else 0
        
        ema9 = ta.trend.ema_indicator(df_1h['c'], window=9).iloc[-1]
        fiyat_ema9_fark = ((anlik_fiyat - ema9) / ema9) * 100
        asiri_uzak_long = fiyat_ema9_fark > 2.0
        asiri_uzak_short = fiyat_ema9_fark < -2.0
        
        btc_yon = btc_bias()
        
        # Detay
        detay = {
            "mfi": round(mfi, 1), "cmf": round(cmf, 3), "hacim_oran": round(hacim_oran, 2),
            "fiyat": anlik_fiyat, "btc": btc_yon,
            "formasyon": formasyon_sebep, "trend": trend_tip,
            "mum_yapisi": mum_sebep,
            "destek_mesafe": round(destek_mesafe, 2),
            "direnc_mesafe": round(direnc_mesafe, 2),
            "destege_yakin": destege_yakin,
            "dirence_yakin": dirence_yakin,
            "fiyat_ema9_fark": round(fiyat_ema9_fark, 2)
        }
        
        # Filtreler
        if mfi < MFI_ASIRI_SATIM:
            detay["sebep"] = f"MFI DIP ({mfi:.0f})"
            return None, detay
        if mfi > MFI_ASIRI_ALIM:
            detay["sebep"] = f"MFI TEPE ({mfi:.0f})"
            return None, detay
        if hacim_oran < HACIM_ESIK:
            detay["sebep"] = f"HACIM ZAYIF ({hacim_oran:.2f}x)"
            return None, detay
        
        # PUANLAMA
        long_puan = 0
        short_puan = 0
        long_sebep = []
        short_sebep = []
        
        # Formasyon
        if formasyon_yon == "LONG":
            long_puan += 4
            long_sebep.append(f"Formasyon: {formasyon_sebep}")
        elif formasyon_yon == "SHORT":
            short_puan += 4
            short_sebep.append(f"Formasyon: {formasyon_sebep}")
        elif formasyon_yon == "SIKISMA":
            if trend_tip == "YUKSELEN":
                long_puan += 2
                long_sebep.append("Sıkışma + Yükselen")
            elif trend_tip == "ALCALAN":
                short_puan += 2
                short_sebep.append("Sıkışma + Alçalan")
        
        # Trend
        if trend_tip == "YUKSELEN":
            long_puan += 2
            long_sebep.append("Yükselen kanal")
        elif trend_tip == "ALCALAN":
            short_puan += 2
            short_sebep.append("Alçalan kanal")
        
        # Mum yapısı
        if mum_yon == "LONG":
            long_puan += 3
            long_sebep.append(f"Mum: {mum_sebep}")
        elif mum_yon == "SHORT":
            short_puan += 3
            short_sebep.append(f"Mum: {mum_sebep}")
        
        # Destek/Direnç
        if destege_yakin and not dirence_yakin:
            long_puan += 2
            long_sebep.append(f"Destek({destek_mesafe:.2f}%)")
        if dirence_yakin and not destege_yakin:
            short_puan += 2
            short_sebep.append(f"Direnç({direnc_mesafe:.2f}%)")
        
        # MFI + CMF
        if mfi > MFI_ESIK_AL: long_puan += 1
        if mfi < MFI_ESIK_SAT: short_puan += 1
        if cmf > CMF_ESIK: long_puan += 1
        if cmf < -CMF_ESIK: short_puan += 1
        
        # Hacim
        if hacim_oran > 1.3:
            long_puan += 1
            short_puan += 1
        
        # BTC bias
        if btc_yon == "SHORT": long_puan -= 2
        elif btc_yon == "LONG": short_puan -= 2
        
        # Aşırı uzaklık
        if asiri_uzak_long: long_puan -= 3
        if asiri_uzak_short: short_puan -= 3
        
        detay["long_puan"] = long_puan
        detay["short_puan"] = short_puan
        
        sebep_str = f"L:{long_puan} S:{short_puan} | Form:{formasyon_yon} Trend:{trend_tip} Mum:{mum_yon} | MFI:{mfi:.0f} CMF:{cmf:.2f} Hacim:{hacim_oran:.1f}x"
        detay["sebep"] = sebep_str
        
        MIN_PUAN = 7
        
        if long_puan >= MIN_PUAN and long_puan > short_puan:
            detay["sinyal_skoru"] = long_puan
            detay["strateji"] = "FORMASYON"
            detay["aciklama"] = " + ".join(long_sebep)
            return "LONG", detay
        
        if short_puan >= MIN_PUAN and short_puan > long_puan:
            detay["sinyal_skoru"] = short_puan
            detay["strateji"] = "FORMASYON"
            detay["aciklama"] = " + ".join(short_sebep)
            return "SHORT", detay
        
        return None, detay
        
    except Exception as e:
        return None, {"hata": str(e)}

# ==================== TELEGRAM ====================
def telegram_direkt_gonder(mesaj, deneme=3):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return False
    
    for i in range(deneme):
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={"chat_id": CHAT_ID, "text": mesaj, "parse_mode": "Markdown"},
                timeout=15
            )
            if r.status_code == 200:
                return True
        except: pass
        
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={"chat_id": CHAT_ID, "text": mesaj},
                timeout=15
            )
            if r.status_code == 200:
                return True
        except: pass
        
        time.sleep(2)
    
    return False


def telegram_kuyruga_ekle(mesaj):
    try:
        supabase.table("telegram_kuyruk").insert({
            "mesaj": mesaj,
            "deneme_sayisi": 0,
            "gonderildi": False
        }).execute()
        print(f"📬 [KUYRUK] Eklendi", flush=True)
        return True
    except Exception as e:
        print(f"⚠️ [KUYRUK] Ekleme hatası: {e}", flush=True)
        return False


def telegram_kuyruk_isle():
    try:
        response = supabase.table("telegram_kuyruk").select("*").eq("gonderildi", False).order("id").limit(10).execute()
        if not response.data:
            return 0
        
        gonderilen = 0
        for kayit in response.data:
            mesaj = kayit['mesaj']
            kayit_id = kayit['id']
            deneme = kayit.get('deneme_sayisi', 0) or 0
            
            if deneme >= 10:
                supabase.table("telegram_kuyruk").update({"gonderildi": True}).eq("id", kayit_id).execute()
                continue
            
            basarili = telegram_direkt_gonder(mesaj, deneme=2)
            if basarili:
                supabase.table("telegram_kuyruk").update({"gonderildi": True}).eq("id", kayit_id).execute()
                gonderilen += 1
            else:
                supabase.table("telegram_kuyruk").update({"deneme_sayisi": deneme + 1}).eq("id", kayit_id).execute()
        
        return gonderilen
    except Exception as e:
        print(f"⚠️ [KUYRUK] {e}", flush=True)
        return 0


def telegram_kritik(mesaj):
    basarili = telegram_direkt_gonder(mesaj, deneme=3)
    if not basarili:
        telegram_kuyruga_ekle(mesaj)
    return basarili


def telegram_gonder(mesaj, deneme=3):
    return telegram_kritik(mesaj)


def kuyruk_worker():
    while True:
        try:
            if BOT_CALISIYOR_MU:
                telegram_kuyruk_isle()
        except Exception as e:
            print(f"⚠️ [KUYRUK WORKER] {e}", flush=True)
        time.sleep(30)


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

# ==================== BAŞLANGIÇ ====================
def baslangic_temizligi():
    print("\n🧹 [BAŞLANGIÇ] Mevcut pozisyonlar korunuyor...", flush=True)
    try:
        raw_pos = exchange.fetch_positions()
        mevcut = [p['symbol'] for p in raw_pos if float(p.get('contracts', 0) or p.get('size', 0) or 0) > 0]
        
        if mevcut:
            print(f"✅ Mevcut pozisyonlar: {mevcut}", flush=True)
            telegram_kritik(f"ℹ️ BOT BAŞLATILDI (v25.0 - Formasyon)\n\nMarj: %10 | Maks: 2 işlem\nSL: %1.5 | TP: %2.5\n\nMevcut pozisyonlar:\n{mevcut}")
        else:
            print("✅ Açık pozisyon yok.", flush=True)
            telegram_kritik("🟢 BOT BAŞLATILDI (v25.0 - Formasyon)\nMarj: %10 | Maks: 2 işlem\nSL: %1.5 | TP: %2.5\n\nFormasyon tespiti aktif")
    except Exception as e:
        print(f"⚠️ Başlangıç kontrolü: {e}", flush=True)

# ==================== POZİSYON AÇMA ====================
def pozisyon_ac(symbol, yon, anlik_fiyat, sebep, sinyal_skoru=0, aciklama=""):
    global AKTIF_SISTEMLER
    try:
        bakiye = exchange.fetch_balance()
        toplam_bakiye = float(bakiye['total'].get('USDT', 0))
        serbest = float(bakiye.get('free', {}).get('USDT', 0) or 0)
        
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
        
        if yon == "LONG":
            tp = anlik_fiyat * (1 + TP_YUZDE / 100)
            sl = anlik_fiyat * (1 - SL_YUZDE / 100)
            kapat_yon = 'sell'
        else:
            tp = anlik_fiyat * (1 - TP_YUZDE / 100)
            sl = anlik_fiyat * (1 + SL_YUZDE / 100)
            kapat_yon = 'buy'
        
        islem_y = 'buy' if yon == 'LONG' else 'sell'
        exchange.create_order(symbol, 'market', islem_y, miktar)
        time.sleep(1.0)
        
        tp_ok = False
        try:
            exchange.create_order(symbol, 'limit', kapat_yon, miktar, tp, {'reduceOnly': True})
            tp_ok = True
        except Exception as e:
            print(f"  ⚠️ TP: {e}", flush=True)
        
        sl_ok = False
        try:
            exchange.create_order(symbol, 'stop', kapat_yon, miktar, sl, {'stopPrice': sl, 'reduceOnly': True})
            sl_ok = True
        except Exception as e:
            print(f"  🚨 SL: {e}", flush=True)
        
        if not sl_ok:
            print(f"  🚨 [ACİL] {symbol} SL koyulamadı, kapatılıyor!", flush=True)
            try: market_kapat(symbol, miktar, yon)
            except: pass
            return False
        
        with state_lock:
            AKTIF_SISTEMLER[symbol] = {
                "giris_fiyati": anlik_fiyat, "yon": yon, "giris_zamani": time.time(),
                "sebep": sebep, "tp": tp, "sl": sl, "marj": gercek_marj,
                "miktar": miktar, "tp_order": tp_ok, "sl_order": sl_ok,
                "contract_size": contract_size, "sinyal_skoru": sinyal_skoru
            }
        
        hafizayi_kaydet()
        print(f"  ✅ [AÇILDI] {symbol} {yon} @ {anlik_fiyat} | Puan: {sinyal_skoru}", flush=True)
        
        islem_no = len(AKTIF_SISTEMLER)
        etiket = "📊 FORMASYON"
        
        mesaj = (
            f"{etiket} ({islem_no}. İşlem)\n"
            f"📌 {symbol} | {yon}\n"
            f"🎯 Puan: {sinyal_skoru}\n"
            f"📋 {aciklama}\n"
            f"📊 {sebep}\n"
            f"🎯 Giriş: {anlik_fiyat:.4f}\n"
            f"💰 TP: {tp:.4f}\n"
            f"🛑 SL: {sl:.4f}\n"
            f"💵 Marj: {gercek_marj:.2f} | Kaldıraç: {KALDIRAC}x\n"
            f"📌 Açık: {islem_no}/{MAKS_POZISYON}"
        )
        telegram_kritik(mesaj)
        return True
    except Exception as e:
        print(f"  ⚠️ Pozisyon {symbol}: {e}", flush=True)
        return False

# ==================== SL/TP KONTROLÜ ====================
def manuel_sl_tp_kontrol(aktif_borsa_map):
    global AKTIF_SISTEMLER
    
    with state_lock:
        kopya = list(AKTIF_SISTEMLER.items())
    
    for symbol, bilgi in kopya:
        if symbol not in aktif_borsa_map: 
            continue
        
        try:
            ticker = exchange.fetch_ticker(symbol)
            anlik = float(ticker['last'])
        except: 
            continue
        
        yon = bilgi.get("yon", "LONG")
        sl = float(bilgi.get("sl", 0))
        tp = float(bilgi.get("tp", 0))
        miktar = float(bilgi.get("miktar", 0))
        giris = float(bilgi.get("giris_fiyati", 0))
        
        contract_size = float(bilgi.get("contract_size", 1.0))
        if contract_size == 0:
            try:
                market = exchange.market(symbol)
                contract_size = float(market.get('contractSize', 1.0))
            except:
                contract_size = 1.0
        
        if yon == "LONG":
            kar = (anlik - giris) * miktar * contract_size
        else:
            kar = (giris - anlik) * miktar * contract_size
        
        sl_tetiklendi = False
        if yon == "LONG" and anlik <= sl: sl_tetiklendi = True
        elif yon == "SHORT" and anlik >= sl: sl_tetiklendi = True
        
        if sl_tetiklendi:
            tum_emirleri_iptal(symbol)
            print(f"  🛑 [SL] {symbol} {yon} | PnL: {kar:.4f} USDT", flush=True)
            if market_kapat(symbol, miktar, yon):
                with state_lock:
                    if symbol in AKTIF_SISTEMLER: del AKTIF_SISTEMLER[symbol]
                    COIN_COOLDOWNLAR[symbol] = {"zaman": float(time.time() + COOLDOWN_SL), "son_yon": yon}
                sayaci_artir(False)
                hafizayi_kaydet()
                telegram_kritik(
                    f"🛑 SL\n📌 {symbol} {yon}\n💵 PnL: {kar:.4f} USDT\n⏳ Cooldown: {COOLDOWN_SL//60} dk"
                )
            continue
        
        if tp > 0:
            tp_tetiklendi = False
            if yon == "LONG" and anlik >= tp: tp_tetiklendi = True
            elif yon == "SHORT" and anlik <= tp: tp_tetiklendi = True
            
            if tp_tetiklendi:
                tum_emirleri_iptal(symbol)
                print(f"  💰 [TP] {symbol} {yon} | PnL: +{kar:.4f} USDT", flush=True)
                if market_kapat(symbol, miktar, yon):
                    with state_lock:
                        if symbol in AKTIF_SISTEMLER: del AKTIF_SISTEMLER[symbol]
                        COIN_COOLDOWNLAR[symbol] = {"zaman": float(time.time() + COOLDOWN_TP), "son_yon": yon}
                    sayaci_artir(True)
                    hafizayi_kaydet()
                    telegram_kritik(
                        f"💰 TP\n📌 {symbol} {yon}\n💵 PnL: +{kar:.4f} USDT\n⏳ Cooldown: {COOLDOWN_TP//60} dk"
                    )

# ==================== BORSA KAPANIŞ TESPİTİ ====================
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
                
                contract_size = float(bilgi.get("contract_size", 1.0))
                if contract_size == 0:
                    try:
                        market = exchange.market(eski)
                        contract_size = float(market.get('contractSize', 1.0))
                    except:
                        contract_size = 1.0
                
                karli = False
                pnl = 0
                try:
                    t = exchange.fetch_ticker(eski)
                    cikis = float(t['last'])
                    if y == "LONG":
                        pnl = (cikis - g) * miktar * contract_size
                    else:
                        pnl = (g - cikis) * miktar * contract_size
                    karli = pnl > 0
                except: karli = True
                
                sayaci_artir(karli)
                
                with state_lock:
                    COIN_COOLDOWNLAR[eski] = {"zaman": float(time.time() + COOLDOWN_VARSAYILAN), "son_yon": y}
                    if eski in AKTIF_SISTEMLER:
                        del AKTIF_SISTEMLER[eski]
                
                hafizayi_kaydet()
                
                if karli:
                    telegram_kritik(f"✅ TP (Borsa)\n📌 {eski} {y}\n💵 +{pnl:.4f} USDT")
                else:
                    telegram_kritik(f"❌ SL (Borsa)\n📌 {eski} {y}\n💵 {pnl:.4f} USDT")
    except Exception as e:
        print(f"⚠️ Kapanış kontrolü: {e}", flush=True)

# ==================== ANA DÖNGÜ ====================
def ana_dongu():
    global GUNLUK_BASLANGIC_BAKIYE
    
    print("🚀 [BAŞLANGIÇ] v25.0 - Formasyon Tespit Sistemi", flush=True)
    print(f"⚙️ Kaldıraç: {KALDIRAC}x | Marj: %{POZISYON_MARJ*100:.0f} | Max Poz: {MAKS_POZISYON}", flush=True)
    print(f"⚙️ SL: %{SL_YUZDE} | TP: %{TP_YUZDE}", flush=True)
    print(f"⚙️ Formasyon: Çift Tepe/Dip, OBO, Üçgen, Mum Yapıları", flush=True)
    print(f"⚙️ Min Puan: 7", flush=True)
    
    try:
        exchange.load_markets()
        b = exchange.fetch_balance()
        GUNLUK_BASLANGIC_BAKIYE = float(b['total'].get('USDT', 0))
        print(f"💰 Başlangıç: {GUNLUK_BASLANGIC_BAKIYE:.2f} USDT", flush=True)
    except: pass
    
    baslangic_temizligi()
    dinamik_liste_guncelle(zorla=True)
    
    dongu = 0
    while True:
        try:
            if not BOT_CALISIYOR_MU:
                time.sleep(5); continue
            
            dongu += 1
            print(f"\n{'='*60}", flush=True)
            print(f"🔄 [DÖNGÜ #{dongu}] {datetime.now().strftime('%H:%M:%S')}", flush=True)
            print(f"{'='*60}", flush=True)
            
            dinamik_liste_guncelle()
            
            try:
                b = exchange.fetch_balance()
                su_an = float(b['total'].get('USDT', 0))
                if GUNLUK_BASLANGIC_BAKIYE and GUNLUK_BASLANGIC_BAKIYE > 0:
                    gk = (su_an - GUNLUK_BASLANGIC_BAKIYE) / GUNLUK_BASLANGIC_BAKIYE
                    print(f"💰 Kasa: {su_an:.2f} | Günlük: %{gk*100:+.2f}", flush=True)
                    if gk <= -GUNLUK_ZARAR_LIMIT:
                        telegram_gonder(f"🛑 KILL-SWITCH")
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
                print(f"📌 Açık: {list(aktif_semboller_seti) if aktif_semboller_seti else 'YOK'}", flush=True)
            except:
                aktif_borsa_map = {}
                aktif_semboller_seti = set()
            
            if aktif_semboller_seti:
                manuel_sl_tp_kontrol(aktif_borsa_map)
                kapanan_pozisyonlari_kontrol(aktif_semboller_seti)
            
            try:
                toplam_acik_zarar = sum(float(p.get('unrealizedPnl', 0)) for p in raw_pos 
                                       if float(p.get('contracts', 0) or p.get('size', 0) or 0) > 0)
                if toplam_acik_zarar < -TOPLAM_ZARAR_LIMIT:
                    telegram_gonder(f"🛑 TOPLAM ZARAR {toplam_acik_zarar:.2f}")
                    for p in raw_pos:
                        k = float(p.get('contracts', 0) or p.get('size', 0) or 0)
                        if k > 0:
                            y = str(p.get('side', '')).upper() or "LONG"
                            market_kapat(p['symbol'], k, y)
                    with state_lock: AKTIF_SISTEMLER.clear()
                    hafizayi_kaydet()
                    time.sleep(600)
                    continue
            except: pass
            
            if len(aktif_semboller_seti) >= MAKS_POZISYON:
                print(f"  ⛔ Limit dolu ({len(aktif_semboller_seti)}/{MAKS_POZISYON})", flush=True)
                time.sleep(10)
                continue
            
            print(f"\n🔍 FORMASYON TARAMA ({len(DINAMIK_LISTE)} coin)", flush=True)
            
            for symbol in DINAMIK_LISTE:
                if not BOT_CALISIYOR_MU: break
                if symbol in aktif_semboller_seti: continue
                if len(aktif_semboller_seti) >= MAKS_POZISYON: break
                
                with state_lock:
                    cd = COIN_COOLDOWNLAR.get(symbol)
                    if cd:
                        z = cd.get("zaman", 0) if isinstance(cd, dict) else float(cd)
                        if z > time.time():
                            kalan = int((z - time.time()) / 60)
                            print(f"  ⏳ {symbol.replace('/USDT:USDT','')}: Cooldown ({kalan} dk)", flush=True)
                            continue
                
                karar, detay = order_flow_analiz(symbol)
                
                if "hata" in detay:
                    continue
                
                sym_kisa = symbol.replace('/USDT:USDT', '')
                formasyon = detay.get('formasyon', '?')
                trend = detay.get('trend', '?')
                mum = detay.get('mum_yapisi', '')
                mfi = detay.get('mfi', 0)
                cmf = detay.get('cmf', 0)
                hacim = detay.get('hacim_oran', 0)
                long_p = detay.get('long_puan', 0)
                short_p = detay.get('short_puan', 0)
                
                print(f"  {sym_kisa}: Form:{formasyon} Trend:{trend} Mum:{mum} | MFI:{mfi:.0f} CMF:{cmf:.2f} Hacim:{hacim:.2f}x | L:{long_p} S:{short_p}", flush=True)
                
                if karar is None:
                    continue
                
                print(f"  📊 FORMASYON SİNYALİ! {karar} | Puan: {detay.get('sinyal_skoru', 0)}", flush=True)
                print(f"  📋 {detay.get('aciklama', '')}", flush=True)
                
                ticker = exchange.fetch_ticker(symbol)
                anlik_fiyat = float(ticker['last'])
                
                sonuc = pozisyon_ac(
                    symbol, karar, anlik_fiyat, 
                    detay.get('sebep', ''), 
                    detay.get('sinyal_skoru', 0),
                    detay.get('aciklama', '')
                )
                if sonuc:
                    aktif_semboller_seti.add(symbol)
                    if len(aktif_semboller_seti) >= MAKS_POZISYON:
                        print(f"  🎯 Maksimum işlem sayısına ulaşıldı ({MAKS_POZISYON}/{MAKS_POZISYON})", flush=True)
                        break
            
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
                if yon == "LONG":
                    roe = ((guncel - giris) / giris) * 100 * kaldirac_p
                else:
                    roe = ((giris - guncel) / giris) * 100 * kaldirac_p
                
                if roe >= 0.5: emoji = "🟢"
                elif roe >= -1: emoji = "🟡"
                else: emoji = "🔴"
                
                sl_bilgi = ""
                if p['symbol'] in AKTIF_SISTEMLER:
                    bilgi = AKTIF_SISTEMLER[p['symbol']]
                    sl_bilgi = f"\n  🛑 SL: `{bilgi.get('sl', 0):.4f}` | 💰 TP: `{bilgi.get('tp', 0):.4f}`"
                
                pos_detay += (
                    f"\n{emoji} `{sym}` | *{yon}* ({kaldirac_p}x){sl_bilgi}\n"
                    f"  Giriş: `{giris}` → Anlık: `{guncel}`\n"
                    f"  Marj: `{marj:.2f}` | ROE: `%{roe:+.2f}` | PnL: `{float(p.get('unrealizedPnl', 0)):+.3f}`"
                )
        else:
            pos_detay = "\n\n📌 Açık pozisyon yok."
        
        liste_str = "\n".join([f"• `{s.replace('/USDT:USDT','')}`" for s in DINAMIK_LISTE[:10]]) if DINAMIK_LISTE else "Boş"
        
        mesaj = (
            f"📊 *TREND BOT (v25.0 - Formasyon)*\n\n"
            f"💰 Kasa: `{total:.2f} USDT`\n"
            f"💵 Toplam PnL: `{pnl:+.2f} USDT`\n"
            f"📌 Açık: `{len(pos)}` / `{MAKS_POZISYON}` {'🔥' if len(pos) >= MAKS_POZISYON else ''}"
            f"{pos_detay}\n\n"
            f"📋 *Coin Havuzu:*\n{liste_str}\n\n"
            f"✅ TP: `{bas}` | ❌ SL: `{basz}`\n"
            f"📈 Başarı: `%{oran:.1f}`\n"
            f"📊 Formasyon: `Çift Tepe/Dip, OBO, Üçgen`\n"
            f"💵 Marj: `%{POZISYON_MARJ*100:.0f}` | SL: `%{SL_YUZDE}` | TP: `%{TP_YUZDE}`"
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
    await update.message.reply_text("🟢 Trend Bot (v25.0 - Formasyon) aktif!")

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
        
        response2 = supabase.table("telegram_kuyruk").select("*").eq("gonderildi", True).order("id", desc=True).limit(5).execute()
        gonderilmis = len(response2.data) if response2.data else 0
        
        await update.message.reply_text(
            f"📬 *TELEGRAM KUYRUK*\n\n"
            f"⏳ Bekleyen: `{bekleyen}`\n"
            f"✅ Gönderilmiş (son 5): `{gonderilmis}`",
            parse_mode='Markdown'
        )
    except Exception as e:
        await update.message.reply_text(f"Hata: {e}")

# ==================== MAIN ====================
async def main():
    web_thread = threading.Thread(target=run_web, daemon=True)
    web_thread.start()
    
    kuyruk_thread = threading.Thread(target=kuyruk_worker, daemon=True)
    kuyruk_thread.start()
    print("🧵 [KUYRUK WORKER] Thread başlatıldı", flush=True)
    
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

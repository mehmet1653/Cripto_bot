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
from dotenv import load_dotenv
from flask import Flask
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler
from supabase import create_client, Client

if os.path.exists('/etc/secrets/.env'):
    load_dotenv('/etc/secrets/.env', override=True)
    print("✅ .env yüklendi", flush=True)
else:
    load_dotenv(override=True)

app = Flask(__name__)

@app.route('/')
def home():
    return "Bot aktif!"

def run_web():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.environ.get("CHAT_ID", "").strip()
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "").strip()

if not SUPABASE_URL or not SUPABASE_KEY: sys.exit(1)
if not TELEGRAM_TOKEN or not CHAT_ID: sys.exit(1)

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

exchange = ccxt.gate({
    'apiKey': os.environ.get("GATE_API_KEY", "").strip(),
    'secret': os.environ.get("GATE_SECRET", "").strip(),
    'enableRateLimit': True,
    'timeout': 30000,
    'options': {'defaultType': 'swap'}
})
exchange.set_sandbox_mode(True)

TAKIP_EDILENLER = ['SOL/USDT:USDT', 'XRP/USDT:USDT', 'DOGE/USDT:USDT', 'LTC/USDT:USDT', 'LINK/USDT:USDT']

BOT_CALISIYOR_MU = True
state_lock = threading.Lock()
tarayici_kilidi = threading.Lock()

# ==================== AYARLAR ====================
KALDIRAC = 5
MAKSIMUM_TOPLAM_POZISYON = 3
COOLDOWN_SURESI_SANIYE = 20 * 60

POZISYON_ORANI = 0.30
MIN_NET_KAR = 0.005

KOMISYON_ORANI = 0.001
SPREAD_MALIYETI = 0.0005
TOPLAM_MALIYET_ORANI = (KOMISYON_ORANI * 2) + SPREAD_MALIYETI

ADX_GUCLU_TREND = 30
ADX_TREND_ESIGI = 25
ADX_YATAY_ESIGI = 20
ATR_VOLATIL_CARPAN = 2.0

BREAKOUT_LOOKBACK = 50
BREAKOUT_SL_TOLERANS = 0.005
BREAKOUT_MIN_RR = 2.5

TREND_TAKIP_MUM_ONAY = 5
TREND_TAKIP_SL_CARPAN = 2.0
TREND_TAKIP_BITIS_ONAY = 2

MAKS_YUKSEKLIK_TREND = 0.02
RSI_TREND_UST_LIMIT = 68
RSI_TREND_ALT_LIMIT = 32

RSI_TEPE_ESIGI = 70
RSI_DIP_ESIGI = 30
FITIL_CARPAN = 1.5

TRAILING_SEVIYELER = [
    (5.0, 0.0),
    (10.0, 0.03),
    (15.0, 0.06),
    (25.0, 0.12),
    (40.0, 0.20),
]

KAR_KILIT_MESAJ_ESIGI = 0.01

ARDISIK_ZARAR_LIMIT = 3
ARDISIK_ZARAR_BEKLEME = 3600
ARDISIK_ZARAR_SAYACI = 0
SON_ARDISIK_ZARAR_ZAMANI = 0
KILL_SWITCH_AKTIF = False

# ==================== HAFIZA ====================
def hafizayi_yukle():
    try:
        response = supabase.table("bot_hafiza").select("*").eq("id", 1).execute()
        if response.data and len(response.data) > 0:
            veri = response.data[0]
            print("💾 Hafıza yüklendi.", flush=True)
            return {
                "aktif_sistemler": veri.get("aktif_sistemler", {}),
                "analitik": veri.get("analitik", {
                    "basarili_islem_sayisi": 0,
                    "basarisiz_islem_sayisi": 0,
                    "gercek_tp": 0,
                    "kar_kilidi": 0,
                    "zarar": 0
                }),
                "cooldownlar": veri.get("cooldownlar", {})
            }
    except Exception as e:
        print(f"⚠️ Hafıza: {e}", flush=True)
    return {
        "aktif_sistemler": {},
        "analitik": {"basarili_islem_sayisi": 0, "basarisiz_islem_sayisi": 0, "gercek_tp": 0, "kar_kilidi": 0, "zarar": 0},
        "cooldownlar": {}
    }

def hafizayi_kaydet():
    with state_lock:
        try:
            payload = {
                "basarili_islem_sayisi": int(ANALITIK.get("basarili_islem_sayisi", 0)),
                "basarisiz_islem_sayisi": int(ANALITIK.get("basarisiz_islem_sayisi", 0)),
                "gercek_tp": int(ANALITIK.get("gercek_tp", 0)),
                "kar_kilidi": int(ANALITIK.get("kar_kilidi", 0)),
                "zarar": int(ANALITIK.get("zarar", 0))
            }
            clean_cd = {}
            for k, v in COIN_COOLDOWN.items():
                if isinstance(v, dict):
                    clean_cd[k] = {"zaman": float(v.get("zaman", 0)), "son_yon": str(v.get("son_yon", ""))}
                else:
                    clean_cd[k] = {"zaman": float(v), "son_yon": ""}
            supabase.table("bot_hafiza").upsert({
                "id": 1, "aktif_sistemler": AKTIF_POZISYONLAR,
                "analitik": payload, "cooldownlar": clean_cd
            }).execute()
        except Exception as e:
            print(f"⚠️ Kayıt: {e}", flush=True)

kalici = hafizayi_yukle()
AKTIF_POZISYONLAR = kalici.get("aktif_sistemler", {})
ANALITIK = kalici.get("analitik", {"basarili_islem_sayisi": 0, "basarisiz_islem_sayisi": 0, "gercek_tp": 0, "kar_kilidi": 0, "zarar": 0})
COIN_COOLDOWN = kalici.get("cooldownlar", {})

# ==================== PİYASA ANALİZİ ====================
def piyasa_modu_bul(df):
    try:
        close = df['close']
        high = df['high']
        low = df['low']
        
        adx = ta.trend.ADXIndicator(high=high, low=low, close=close, window=14).adx().iloc[-1]
        
        atr_serisi = ta.volatility.AverageTrueRange(high=high, low=low, close=close, window=14).average_true_range()
        atr = atr_serisi.iloc[-1]
        atr_ort = atr_serisi.tail(50).mean()
        
        bb = ta.volatility.BollingerBands(close=close, window=20, window_dev=2.0)
        bb_ust = bb.bollinger_hband().iloc[-1]
        bb_alt = bb.bollinger_lband().iloc[-1]
        bb_orta = bb.bollinger_mavg().iloc[-1]
        
        ema20 = ta.trend.EMAIndicator(close=close, window=20).ema_indicator().iloc[-1]
        ema50 = ta.trend.EMAIndicator(close=close, window=50).ema_indicator().iloc[-1]
        rsi = ta.momentum.RSIIndicator(close=close, window=14).rsi().iloc[-1]
        
        anlik = close.iloc[-1]
        
        if atr_ort > 0 and atr > atr_ort * ATR_VOLATIL_CARPAN:
            return 'VOLATIL', adx, atr, atr_ort, bb_ust, bb_alt, bb_orta, ema20, ema50, rsi, anlik
        
        if adx > ADX_GUCLU_TREND:
            if ema20 > ema50 and anlik > ema20:
                return 'GUCLU_TREND_UP', adx, atr, atr_ort, bb_ust, bb_alt, bb_orta, ema20, ema50, rsi, anlik
            elif ema20 < ema50 and anlik < ema20:
                return 'GUCLU_TREND_DOWN', adx, atr, atr_ort, bb_ust, bb_alt, bb_orta, ema20, ema50, rsi, anlik
        
        if adx > ADX_TREND_ESIGI:
            if ema20 > ema50 and anlik > ema20:
                return 'TREND_YUKARI', adx, atr, atr_ort, bb_ust, bb_alt, bb_orta, ema20, ema50, rsi, anlik
            elif ema20 < ema50 and anlik < ema20:
                return 'TREND_ASAGI', adx, atr, atr_ort, bb_ust, bb_alt, bb_orta, ema20, ema50, rsi, anlik
        
        if adx < ADX_YATAY_ESIGI:
            bb_genislik = (bb_ust - bb_alt) / bb_orta
            if bb_genislik < 0.03:
                return 'YATAY', adx, atr, atr_ort, bb_ust, bb_alt, bb_orta, ema20, ema50, rsi, anlik
        
        return 'BELIRSIZ', adx, atr, atr_ort, bb_ust, bb_alt, bb_orta, ema20, ema50, rsi, anlik
    except Exception as e:
        print(f"⚠️ Piyasa modu hatası: {e}", flush=True)
        return 'BELIRSIZ', 0, 0, 0, 0, 0, 0, 0, 0, 50, 0

# ==================== GRID ====================
def grid_sinyal(df, anlik, bb_ust, bb_alt, bb_orta, atr):
    try:
        if anlik <= bb_alt * 1.005:
            sl_mesafe = atr * 1.2
            tp_mesafe = bb_orta - anlik
            if tp_mesafe <= 0:
                return None, None, None, None, None
            sl = anlik - sl_mesafe
            tp = bb_orta
            brut = tp_mesafe / anlik
            net = brut - TOPLAM_MALIYET_ORANI
            if net < MIN_NET_KAR:
                return None, None, None, None, None
            sebep = f"GRID LONG | BB Alt"
            return "LONG", tp, sl, sebep, atr
        
        if anlik >= bb_ust * 0.995:
            sl_mesafe = atr * 1.2
            tp_mesafe = anlik - bb_orta
            if tp_mesafe <= 0:
                return None, None, None, None, None
            sl = anlik + sl_mesafe
            tp = bb_orta
            brut = tp_mesafe / anlik
            net = brut - TOPLAM_MALIYET_ORANI
            if net < MIN_NET_KAR:
                return None, None, None, None, None
            sebep = f"GRID SHORT | BB Üst"
            return "SHORT", tp, sl, sebep, atr
        
        return None, None, None, None, None
    except Exception as e:
        print(f"⚠️ Grid hatası: {e}", flush=True)
        return None, None, None, None, None

# ==================== SWING ====================
def swing_noktalari_bul(df, lookback=BREAKOUT_LOOKBACK):
    highs = []
    lows = []
    baslangic = max(2, len(df) - lookback)
    for i in range(baslangic, len(df) - 2):
        h = df['high'].iloc[i]
        l = df['low'].iloc[i]
        if (h > df['high'].iloc[i-1] and h > df['high'].iloc[i-2] and
            h > df['high'].iloc[i+1] and h > df['high'].iloc[i+2]):
            highs.append((i, h))
        if (l < df['low'].iloc[i-1] and l < df['low'].iloc[i-2] and
            l < df['low'].iloc[i+1] and l < df['low'].iloc[i+2]):
            lows.append((i, l))
    return highs, lows

# ==================== BREAKOUT+RETEST ====================
def breakout_retest_sinyal(df, anlik, yon_trend, atr):
    try:
        highs, lows = swing_noktalari_bul(df)
        if len(highs) < 2 or len(lows) < 2:
            return None, None, None, None, None
        
        def cluster(levels, tol=0.003):
            if not levels: return []
            s = sorted(levels)
            res = [s[0]]
            for x in s[1:]:
                if abs(x - res[-1]) / res[-1] > tol:
                    res.append(x)
            return res
        
        high_levels = cluster([h[1] for h in highs])
        low_levels = cluster([l[1] for l in lows])
        
        if yon_trend == 'TREND_YUKARI':
            for direnc in high_levels:
                if direnc < anlik:
                    yakinlik = abs(anlik - direnc) / direnc
                    if yakinlik < 0.005:
                        sonraki = [h for h in high_levels if h > direnc * 1.01]
                        if not sonraki: continue
                        tp = min(sonraki) * 0.997
                        sl = direnc * (1 - BREAKOUT_SL_TOLERANS)
                        rr = (tp - anlik) / (anlik - sl) if anlik > sl else 0
                        if rr < BREAKOUT_MIN_RR: continue
                        brut = (tp - anlik) / anlik
                        net = brut - TOPLAM_MALIYET_ORANI
                        if net < MIN_NET_KAR: continue
                        sebep = f"BREAKOUT↑ RETEST | R/R: {rr:.1f}"
                        return "LONG", tp, sl, sebep, atr
        
        if yon_trend == 'TREND_ASAGI':
            for destek in sorted(low_levels, reverse=True):
                if destek > anlik:
                    yakinlik = abs(anlik - destek) / destek
                    if yakinlik < 0.005:
                        sonraki = [l for l in low_levels if l < destek * 0.99]
                        if not sonraki: continue
                        tp = max(sonraki) * 1.003
                        sl = destek * (1 + BREAKOUT_SL_TOLERANS)
                        rr = (anlik - tp) / (sl - anlik) if sl > anlik else 0
                        if rr < BREAKOUT_MIN_RR: continue
                        brut = (anlik - tp) / anlik
                        net = brut - TOPLAM_MALIYET_ORANI
                        if net < MIN_NET_KAR: continue
                        sebep = f"BREAKOUT↓ RETEST | R/R: {rr:.1f}"
                        return "SHORT", tp, sl, sebep, atr
        
        return None, None, None, None, None
    except Exception as e:
        print(f"⚠️ Breakout hatası: {e}", flush=True)
        return None, None, None, None, None

# ==================== TREND TAKİP + DÖNÜŞ ====================
def trend_takip_sinyal(df, anlik, mod, atr, ema20, ema50, rsi):
    try:
        if len(df) < TREND_TAKIP_MUM_ONAY + 1:
            return None, None, None, None, None
        
        son_mumlar = df.tail(TREND_TAKIP_MUM_ONAY)
        son_mum = df.iloc[-1]
        onceki = df.iloc[-2]
        
        govde = abs(son_mum['close'] - son_mum['open'])
        toplam_boy = son_mum['high'] - son_mum['low']
        if toplam_boy <= 0:
            return None, None, None, None, None
        
        ust_fitil = son_mum['high'] - max(son_mum['open'], son_mum['close'])
        alt_fitil = min(son_mum['open'], son_mum['close']) - son_mum['low']
        
        if mod == 'GUCLU_TREND_UP':
            tepe_donusu = (
                rsi > RSI_TEPE_ESIGI and
                son_mum['close'] < son_mum['open'] and
                ust_fitil > govde * FITIL_CARPAN
            )
            
            if tepe_donusu:
                sl = anlik + (atr * 1.5)
                tp = anlik - (atr * 4.0)
                brut = (anlik - tp) / anlik
                net = brut - TOPLAM_MALIYET_ORANI
                if net < MIN_NET_KAR:
                    return None, None, None, None, None
                sebep = f"TEPE DÖNÜŞÜ SHORT | RSI: {rsi:.0f}"
                return "SHORT", tp, sl, sebep, atr
            
            hepsi_ustunde = all(son_mumlar['close'] > ema20)
            if not hepsi_ustunde:
                return None, None, None, None, None
            if rsi > RSI_TREND_UST_LIMIT:
                return None, None, None, None, None
            
            son_20 = df.tail(20)
            en_dusuk_20 = son_20['low'].min()
            yukseklik = (anlik - en_dusuk_20) / en_dusuk_20 if en_dusuk_20 > 0 else 0
            if yukseklik > MAKS_YUKSEKLIK_TREND:
                return None, None, None, None, None
            
            if son_mum['close'] < son_mum['open']:
                return None, None, None, None, None
            
            sl = ema20 - (atr * TREND_TAKIP_SL_CARPAN)
            if sl >= anlik:
                return None, None, None, None, None
            tp = anlik + (atr * 20)
            brut = (tp - anlik) / anlik
            net = brut - TOPLAM_MALIYET_ORANI
            if net < MIN_NET_KAR:
                return None, None, None, None, None
            sebep = f"TREND↑ TAKİP | EMA20: {ema20:.4f} | RSI: {rsi:.0f}"
            return "LONG", tp, sl, sebep, atr
        
        if mod == 'GUCLU_TREND_DOWN':
            dip_donusu = (
                rsi < RSI_DIP_ESIGI and
                son_mum['close'] > son_mum['open'] and
                alt_fitil > govde * FITIL_CARPAN
            )
            
            if dip_donusu:
                sl = anlik - (atr * 1.5)
                tp = anlik + (atr * 4.0)
                brut = (tp - anlik) / anlik
                net = brut - TOPLAM_MALIYET_ORANI
                if net < MIN_NET_KAR:
                    return None, None, None, None, None
                sebep = f"DİP DÖNÜŞÜ LONG | RSI: {rsi:.0f}"
                return "LONG", tp, sl, sebep, atr
            
            hepsi_altinda = all(son_mumlar['close'] < ema20)
            if not hepsi_altinda:
                return None, None, None, None, None
            if rsi < RSI_TREND_ALT_LIMIT:
                return None, None, None, None, None
            
            son_20 = df.tail(20)
            en_yuksek_20 = son_20['high'].max()
            dusus = (en_yuksek_20 - anlik) / en_yuksek_20 if en_yuksek_20 > 0 else 0
            if dusus > MAKS_YUKSEKLIK_TREND:
                return None, None, None, None, None
            
            if son_mum['close'] > son_mum['open']:
                return None, None, None, None, None
            
            sl = ema20 + (atr * TREND_TAKIP_SL_CARPAN)
            if sl <= anlik:
                return None, None, None, None, None
            tp = anlik - (atr * 20)
            brut = (anlik - tp) / anlik
            net = brut - TOPLAM_MALIYET_ORANI
            if net < MIN_NET_KAR:
                return None, None, None, None, None
            sebep = f"TREND↓ TAKİP | EMA20: {ema20:.4f} | RSI: {rsi:.0f}"
            return "SHORT", tp, sl, sebep, atr
        
        return None, None, None, None, None
    except Exception as e:
        print(f"⚠️ Trend takip hatası: {e}", flush=True)
        return None, None, None, None, None

# ==================== TRAILING ====================
def trailing_stop_kontrol():
    global ARDISIK_ZARAR_SAYACI, SON_ARDISIK_ZARAR_ZAMANI, KILL_SWITCH_AKTIF
    
    with state_lock:
        aktif_kopya = list(AKTIF_POZISYONLAR.items())
    
    for sym, bilgi in aktif_kopya:
        if sym not in AKTIF_POZISYONLAR:
            continue
        
        yon = bilgi.get("yon", "LONG")
        g = float(bilgi.get("giris_fiyati", 0))
        sl_kayitli = float(bilgi.get("sl_fiyat", 0))
        kaldirac_v = int(bilgi.get("kaldirac", KALDIRAC))
        giris_zaman = float(bilgi.get("giris_zamani", 0))
        mod = bilgi.get("mod", "TREND")
        atr_b = float(bilgi.get("atr_degeri", 0))
        
        if time.time() - giris_zaman < 120:
            continue
        if atr_b <= 0:
            continue
        
        try:
            t = exchange.fetch_ticker(sym)
            anlik = float(t['last'])
        except:
            continue
        
        if yon == "LONG":
            roe = (anlik - g) / g * 100 * kaldirac_v
        else:
            roe = (g - anlik) / g * 100 * kaldirac_v
        
        if yon == "LONG":
            min_kar_sl = g * (1 + (TOPLAM_MALIYET_ORANI + KAR_KILIT_MESAJ_ESIGI) / kaldirac_v)
        else:
            min_kar_sl = g * (1 - (TOPLAM_MALIYET_ORANI + KAR_KILIT_MESAJ_ESIGI) / kaldirac_v)
        
        yeni_sl = None
        
        if mod in ['GUCLU_TREND_UP', 'GUCLU_TREND_DOWN']:
            try:
                ohlcv = exchange.fetch_ohlcv(sym, timeframe='15m', limit=50)
                df_t = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                ema20_guncel = ta.trend.EMAIndicator(close=df_t['close'], window=20).ema_indicator().iloc[-1]
                
                if yon == "LONG":
                    hedef_sl = ema20_guncel - (atr_b * TREND_TAKIP_SL_CARPAN)
                    yeni_sl = max(hedef_sl, min_kar_sl) if roe > 3.0 else hedef_sl
                else:
                    hedef_sl = ema20_guncel + (atr_b * TREND_TAKIP_SL_CARPAN)
                    yeni_sl = min(hedef_sl, min_kar_sl) if roe > 3.0 else hedef_sl
            except Exception as e:
                print(f"⚠️ Trend EMA hatası: {e}", flush=True)
        
        else:
            for esik_roe, kilit in TRAILING_SEVIYELER:
                if roe >= esik_roe:
                    if yon == "LONG":
                        yeni_sl = g * (1 + kilit / kaldirac_v)
                    else:
                        yeni_sl = g * (1 - kilit / kaldirac_v)
                    if yon == "LONG":
                        yeni_sl = max(yeni_sl, min_kar_sl) if roe > 5.0 else yeni_sl
                    else:
                        yeni_sl = min(yeni_sl, min_kar_sl) if roe > 5.0 else yeni_sl
                    break
        
        if yeni_sl is None:
            continue
        
        iyilestirme = (yeni_sl > sl_kayitli * 1.0005) if yon == "LONG" else (yeni_sl < sl_kayitli * 0.9995)
        if not iyilestirme:
            continue
        
        try:
            exchange.cancel_all_orders(sym)
            miktar = None
            for p in exchange.fetch_positions():
                if p['symbol'] == sym:
                    miktar = float(p.get('contracts', 0) or p.get('size', 0) or 0)
                    break
            if not miktar or miktar <= 0:
                continue
            
            ky = 'sell' if yon == 'LONG' else 'buy'
            exchange.create_order(sym, 'stop', ky, miktar, yeni_sl, {'stopPrice': yeni_sl, 'reduceOnly': True})
            
            tp_kayitli = float(bilgi.get("tp_fiyat", 0))
            if tp_kayitli > 0:
                exchange.create_order(sym, 'limit', ky, miktar, tp_kayitli, {'reduceOnly': True})
            
            with state_lock:
                if sym in AKTIF_POZISYONLAR:
                    AKTIF_POZISYONLAR[sym]["sl_fiyat"] = yeni_sl
            
            print(f"🔒 [TRAILING] {sym} | {mod} | ROE:%{roe:.1f} → SL:{yeni_sl:.6f}", flush=True)
            
            if roe > 3.0 and mod in ['GUCLU_TREND_UP', 'GUCLU_TREND_DOWN']:
                tg_gonder(f"🔒 *KÂR KİLİTLENDİ*\n📌 `{sym}` | {mod}\n📊 ROE: `%{roe:+.2f}`\n🛑 SL: `{yeni_sl:.6f}`")
            elif roe > 5.0 and mod not in ['GUCLU_TREND_UP', 'GUCLU_TREND_DOWN']:
                tg_gonder(f"🔒 *KÂR KİLİTLENDİ*\n📌 `{sym}` | {mod}\n📊 ROE: `%{roe:+.2f}`\n🛑 SL: `{yeni_sl:.6f}`")
        except Exception as e:
            print(f"⚠️ Trailing: {e}", flush=True)

# ==================== MOD GÜNCELLEME ====================
def modlari_guncelle():
    with state_lock:
        aktif_kopya = list(AKTIF_POZISYONLAR.items())
    
    for sym, bilgi in aktif_kopya:
        if sym not in AKTIF_POZISYONLAR:
            continue
        if not isinstance(bilgi, dict):
            continue
        
        try:
            ohlcv = exchange.fetch_ohlcv(sym, timeframe='15m', limit=100)
            df_g = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            
            mod_guncel, adx_g, atr_g, _, _, _, _, ema20_g, ema50_g, rsi_g, anlik_g = piyasa_modu_bul(df_g)
            
            eski_mod = bilgi.get("mod", "")
            if eski_mod != mod_guncel:
                with state_lock:
                    if sym in AKTIF_POZISYONLAR:
                        AKTIF_POZISYONLAR[sym]["mod"] = mod_guncel
                        AKTIF_POZISYONLAR[sym]["atr_degeri"] = atr_g
                print(f"🔄 [MOD DEĞİŞİMİ] {sym} | {eski_mod} → {mod_guncel}", flush=True)
        except Exception as e:
            continue

# ==================== TELEGRAM ====================
def tg_gonder(mesaj):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                      json={"chat_id": CHAT_ID, "text": mesaj, "parse_mode": "Markdown"}, timeout=5)
    except:
        pass

async def durum_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != int(CHAT_ID):
        return
    try:
        balance = await asyncio.to_thread(exchange.fetch_balance)
        total = float(balance['total'].get('USDT', 0))
        pos = [p for p in await asyncio.to_thread(exchange.fetch_positions) if float(p.get('contracts', 0) or p.get('size', 0) or 0) > 0]
        pnl = sum(float(p.get('unrealizedPnl', 0)) for p in pos)
        
        gercek_tp = int(ANALITIK.get("gercek_tp", 0))
        kar_kilidi = int(ANALITIK.get("kar_kilidi", 0))
        zarar = int(ANALITIK.get("zarar", 0))
        toplam_islem = gercek_tp + kar_kilidi + zarar
        oran = ((gercek_tp + kar_kilidi) / toplam_islem * 100) if toplam_islem > 0 else 0
        
        pos_detay = ""
        for p in pos:
            sym = p['symbol']
            y = str(p.get('side', '')).upper() or "?"
            g = float(p.get('entryPrice', 0))
            k = int(p.get('leverage', KALDIRAC))
            t = await asyncio.to_thread(exchange.fetch_ticker, sym)
            gf = float(t['last'])
            unrealized = float(p.get('unrealizedPnl', 0) or 0)
            f = (gf - g) / g if y == "LONG" else (g - gf) / g
            roe = f * 100 * k
            mod_bilgi = AKTIF_POZISYONLAR.get(sym, {}).get("mod", "?")
            margin = float(p.get('initialMargin', 0) or 0)
            
            nokta = "🟢" if unrealized >= 0 else "🔴"
            
            pos_detay += f"\n{nokta} `{sym}` | {y} ({k}x) | {mod_bilgi}\n  K/Z: `{unrealized:+.2f} USDT` | ROE: `%{roe:+.2f}`\n  Marj: `{margin:.2f}`"
        
        pnl_nokta = "🟢" if pnl >= 0 else "🔴"
        
        mesaj = (
            f"📊 *DURUM* [ADAPTİF v4.2]\n\n"
            f"💰 Kasa: `{total:.2f} USDT`\n"
            f"{pnl_nokta} Toplam PnL: `{pnl:+.2f} USDT`\n"
            f"📌 Açık: `{len(pos)} / {MAKSIMUM_TOPLAM_POZISYON}`"
            f"{pos_detay}\n\n"
            f"🎯 Gerçek TP: `{gercek_tp}` | 🔒 Kâr Kilidi: `{kar_kilidi}` | ❌ Zarar: `{zarar}`\n"
            f"📈 Başarı: `%{oran:.1f}` ({toplam_islem} işlem)\n"
            f"⚡ Kill-Switch: `{'AKTİF' if KILL_SWITCH_AKTIF else 'Pasif'}`\n"
            f"🔻 Ardışık Zarar: `{ARDISIK_ZARAR_SAYACI}`"
        )
        await update.message.reply_text(mesaj, parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"Hata: {e}")

async def baslat_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != int(CHAT_ID):
        return
    global BOT_CALISIYOR_MU, KILL_SWITCH_AKTIF, ARDISIK_ZARAR_SAYACI
    BOT_CALISIYOR_MU = True
    KILL_SWITCH_AKTIF = False
    ARDISIK_ZARAR_SAYACI = 0
    await update.message.reply_text("🟢 Bot aktif! (v4.2: 3 Kategori)")

async def durdur_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != int(CHAT_ID):
        return
    global BOT_CALISIYOR_MU
    BOT_CALISIYOR_MU = False
    await update.message.reply_text("⏸️ Durduruldu.")

async def kapat_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != int(CHAT_ID):
        return
    try:
        positions = await asyncio.to_thread(exchange.fetch_positions)
        for p in positions:
            k = float(p.get('contracts', 0) or p.get('size', 0) or 0)
            if k > 0:
                y = str(p.get('side', '')).upper() or "LONG"
                ky = 'sell' if y == 'LONG' else 'buy'
                try:
                    exchange.cancel_all_orders(p['symbol'])
                except:
                    pass
                exchange.create_order(p['symbol'], 'market', ky, k, None, {'reduceOnly': True})
        await update.message.reply_text("✅ Kapatıldı.")
    except Exception as e:
        await update.message.reply_text(f"Hata: {e}")

# ==================== ANA TARAYICI ====================
def tarayici():
    global ARDISIK_ZARAR_SAYACI, SON_ARDISIK_ZARAR_ZAMANI, KILL_SWITCH_AKTIF
    
    print("🚀 [BAŞLANGIÇ] ADAPTİF v4.2 (3 Kategori)", flush=True)
    try:
        exchange.load_markets()
    except:
        pass
    
    dongu = 0
    while True:
        if not tarayici_kilidi.acquire(blocking=False):
            time.sleep(2)
            continue
        
        try:
            if not BOT_CALISIYOR_MU:
                time.sleep(5)
                continue
            
            dongu += 1
            print(f"\n{'='*55}", flush=True)
            print(f"🔄 [DÖNGÜ #{dongu}] {time.strftime('%H:%M:%S')}", flush=True)
            
            if KILL_SWITCH_AKTIF:
                if time.time() - SON_ARDISIK_ZARAR_ZAMANI > ARDISIK_ZARAR_BEKLEME:
                    KILL_SWITCH_AKTIF = False
                    ARDISIK_ZARAR_SAYACI = 0
                    tg_gonder("✅ KILL-SWITCH KAPANDI")
                else:
                    kalan = int((ARDISIK_ZARAR_BEKLEME - (time.time() - SON_ARDISIK_ZARAR_ZAMANI))/60)
                    print(f"⏸️ [KILL-SWITCH] {kalan} dk", flush=True)
                    time.sleep(30)
                    continue
            
            try:
                raw = exchange.fetch_positions()
                aktif_map = {}
                aktif_list = []
                for p in raw:
                    k = float(p.get('contracts', 0) or p.get('size', 0) or 0)
                    if k > 0:
                        s = p['symbol']
                        aktif_map[s] = p
                        aktif_list.append(s)
            except:
                raw = []
                aktif_map = {}
                aktif_list = []
            
            # ==================== KAPANIŞ + 3 KATEGORİ ====================
            try:
                anlik_aktif = [p['symbol'] for p in raw if float(p.get('contracts', 0) or p.get('size', 0) or 0) > 0]
                for eski in list(AKTIF_POZISYONLAR.keys()):
                    if eski not in anlik_aktif:
                        bilgi = AKTIF_POZISYONLAR[eski]
                        g = float(bilgi.get("giris_fiyati", 0))
                        y = bilgi.get("yon", "LONG")
                        tp_k = float(bilgi.get("tp_fiyat", 0))
                        
                        cikis = g
                        try:
                            t = exchange.fetch_ticker(eski)
                            cikis = float(t['last'])
                        except:
                            pass
                        
                        # ✅ 3 KATEGORİLİ SINIFLANDIRMA
                        if y == "LONG":
                            brut = (cikis - g) / g
                        else:
                            brut = (g - cikis) / g
                        net = brut - TOPLAM_MALIYET_ORANI
                        
                        # Kategori belirle
                        if tp_k > 0 and abs(cikis - tp_k) / tp_k < 0.002:
                            # TP'ye çok yakın → GERÇEK TP
                            kategori = "gercek_tp"
                            tip = "✅ *GERÇEK TP*"
                            karli = True
                        elif net > 0:
                            # Net kâr pozitif → KÂR KİLİDİ
                            kategori = "kar_kilidi"
                            tip = "🔒 *KÂR KİLİDİYLE KAPANDI*"
                            karli = True
                        else:
                            # Net zarar → ZARAR
                            kategori = "zarar"
                            tip = "❌ *ZARARLA KAPANDI*"
                            karli = False
                        
                        with state_lock:
                            ANALITIK[kategori] = int(ANALITIK.get(kategori, 0)) + 1
                            
                            if not karli:
                                ARDISIK_ZARAR_SAYACI += 1
                                SON_ARDISIK_ZARAR_ZAMANI = time.time()
                            else:
                                ARDISIK_ZARAR_SAYACI = 0
                            
                            COIN_COOLDOWN[eski] = {"zaman": float(time.time() + COOLDOWN_SURESI_SANIYE), "son_yon": y}
                            if eski in AKTIF_POZISYONLAR:
                                del AKTIF_POZISYONLAR[eski]
                        
                        hafizayi_kaydet()
                        print(f"💰 [KAPANIŞ] {eski} | Çıkış: {cikis} | Net: %{net*100:.2f} | {kategori}", flush=True)
                        tg_gonder(
                            f"{tip}\n📌 `{eski}` | Çıkış: `{cikis}`\n"
                            f"📊 Net: `%{net*100:+.2f}`\n"
                            f"🔻 Ardışık Zarar: `{ARDISIK_ZARAR_SAYACI}`"
                        )
                        
                        if ARDISIK_ZARAR_SAYACI >= ARDISIK_ZARAR_LIMIT:
                            KILL_SWITCH_AKTIF = True
                            tg_gonder(f"🚨 *KILL-SWITCH AKTİF!*\n{ARDISIK_ZARAR_LIMIT} ardışık zarar.\n⏸️ 1 saat bekle.")
            except Exception as e:
                print(f"⚠️ Kapanış: {e}", flush=True)
            
            modlari_guncelle()
            trailing_stop_kontrol()
            
            for symbol in TAKIP_EDILENLER:
                if not BOT_CALISIYOR_MU:
                    break
                if len(aktif_map) >= MAKSIMUM_TOPLAM_POZISYON:
                    break
                if symbol in aktif_list:
                    continue
                
                with state_lock:
                    cd = COIN_COOLDOWN.get(symbol)
                    if cd:
                        z = cd.get("zaman", 0) if isinstance(cd, dict) else float(cd)
                        if z - time.time() > 0:
                            continue
                
                try:
                    ohlcv = exchange.fetch_ohlcv(symbol, timeframe='15m', limit=100)
                    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                    
                    ticker = exchange.fetch_ticker(symbol)
                    anlik = float(ticker['last'])
                    
                    mod, adx, atr, atr_ort, bb_ust, bb_alt, bb_orta, ema20, ema50, rsi, _ = piyasa_modu_bul(df)
                    
                    print(f"   🔎 [{symbol}] {anlik:.4f} | Mod: {mod} | ADX: {adx:.1f} | RSI: {rsi:.0f}", flush=True)
                    
                    yon, tp, sl, sebep, atr_b = None, None, None, None, None
                    
                    if mod == 'YATAY':
                        yon, tp, sl, sebep, atr_b = grid_sinyal(df, anlik, bb_ust, bb_alt, bb_orta, atr)
                        if yon is None:
                            print(f"      ⏭️ GRID sinyal yok", flush=True)
                    
                    elif mod in ['TREND_YUKARI', 'TREND_ASAGI']:
                        yon, tp, sl, sebep, atr_b = breakout_retest_sinyal(df, anlik, mod, atr)
                        if yon is None:
                            print(f"      ⏭️ BREAKOUT sinyal yok", flush=True)
                    
                    elif mod in ['GUCLU_TREND_UP', 'GUCLU_TREND_DOWN']:
                        yon, tp, sl, sebep, atr_b = trend_takip_sinyal(df, anlik, mod, atr, ema20, ema50, rsi)
                        if yon is None:
                            print(f"      ⏭️ TREND TAKİP/DÖNÜŞ sinyal yok", flush=True)
                    
                    elif mod == 'VOLATIL':
                        print(f"      ⏸️ VOLATIL", flush=True)
                        continue
                    
                    else:
                        print(f"      ⏸️ BELIRSIZ", flush=True)
                        continue
                    
                    if yon is None:
                        continue
                    
                    print(f"      🎯 SİNYAL! [{mod}] {yon} | {sebep}", flush=True)
                    
                    bakiye = exchange.fetch_balance()
                    toplam_b = float(bakiye['total'].get('USDT', 0))
                    serbest_b = float(bakiye.get('free', {}).get('USDT', 0) or 0)
                    
                    exchange.set_leverage(KALDIRAC, symbol)
                    market = exchange.market(symbol)
                    
                    kullan = min(toplam_b * POZISYON_ORANI, serbest_b)
                    if kullan < 1:
                        continue
                    
                    miktar = float(exchange.amount_to_precision(
                        symbol,
                        max((kullan * KALDIRAC) / anlik / float(market.get('contractSize', 1.0)),
                            float(market['limits']['amount']['min'] or 1.0))
                    ))
                    
                    iy = 'buy' if yon == 'LONG' else 'sell'
                    kapat_y = 'sell' if yon == 'LONG' else 'buy'
                    
                    exchange.create_order(symbol, 'market', iy, miktar)
                    time.sleep(0.5)
                    
                    sl_ok = False
                    try:
                        exchange.create_order(symbol, 'limit', kapat_y, miktar, tp, {'reduceOnly': True})
                        exchange.create_order(symbol, 'stop', kapat_y, miktar, sl, {'stopPrice': sl, 'reduceOnly': True})
                        sl_ok = True
                    except Exception as e:
                        print(f"      ⚠️ TP/SL: {e}", flush=True)
                    
                    if not sl_ok:
                        try:
                            exchange.create_order(symbol, 'market', kapat_y, miktar, None, {'reduceOnly': True})
                        except:
                            pass
                        continue
                    
                    with state_lock:
                        AKTIF_POZISYONLAR[symbol] = {
                            "giris_fiyati": anlik, "yon": yon,
                            "tp_fiyat": tp, "sl_fiyat": sl,
                            "giris_zamani": time.time(),
                            "kaldirac": KALDIRAC,
                            "mod": mod,
                            "atr_degeri": atr_b
                        }
                        aktif_list.append(symbol)
                        aktif_map[symbol] = {"dummy": True}
                    
                    hafizayi_kaydet()
                    print(f"      ✅ [AÇILDI] {symbol} {yon} | {mod} | @ {anlik} | Marj: {kullan:.2f}", flush=True)
                    
                    emoji = "🟦" if mod == 'YATAY' else ("🟩" if mod in ['TREND_YUKARI', 'TREND_ASAGI'] else "🟨")
                    
                    tg_gonder(
                        f"{emoji} *SİNYAL!* [{mod}]\n"
                        f"📌 `{symbol}` | {yon}\n"
                        f"📊 {sebep}\n"
                        f"🎯 Giriş: `{anlik}`\n"
                        f"💰 TP: `{tp}`\n"
                        f"🛑 SL: `{sl}`\n"
                        f"💵 Marj: `{kullan:.2f} USDT`"
                    )
                except Exception as e:
                    print(f"   ⚠️ {symbol}: {e}", flush=True)
                    continue
        
        except Exception as e:
            print(f"⚠️ Ana döngü: {e}", flush=True)
        finally:
            try:
                tarayici_kilidi.release()
            except:
                pass
        
        time.sleep(15)

async def main():
    web_thread = threading.Thread(target=run_web, daemon=True)
    web_thread.start()
    
    app_tg = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    
    try:
        requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/deleteWebhook?drop_pending_updates=True", timeout=10)
        time.sleep(2)
        print("✅ Telegram webhook temizlendi.", flush=True)
    except Exception as e:
        print(f"⚠️ Webhook: {e}", flush=True)
    
    app_tg.add_handler(CommandHandler("durum", durum_komutu))
    app_tg.add_handler(CommandHandler("baslat", baslat_komutu))
    app_tg.add_handler(CommandHandler("durdur", durdur_komutu))
    app_tg.add_handler(CommandHandler("kapat", kapat_komutu))
    
    await app_tg.initialize()
    await app_tg.start()
    await app_tg.updater.start_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
    
    t = threading.Thread(target=tarayici, daemon=True)
    t.start()
    
    stop = asyncio.Event()
    await stop.wait()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass

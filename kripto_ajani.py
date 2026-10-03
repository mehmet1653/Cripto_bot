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

# ==================== .ENV ====================
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

# ==================== HAVUZ ====================
CEKIRDEK_LISTE = ['SOL/USDT:USDT', 'XRP/USDT:USDT', 'DOGE/USDT:USDT', 'LTC/USDT:USDT', 'LINK/USDT:USDT']
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
        print(f"⚠️ Havuz: {e}", flush=True)
        return False

def takip_listesi():
    with state_lock:
        acik = list(AKTIF_POZISYONLAR.keys())
    return list(set(CEKIRDEK_LISTE + DINAMIK_LISTE + acik))

# ==================== DURUM ====================
BOT_CALISIYOR_MU = True
state_lock = threading.Lock()
borsa_kilidi = threading.Lock()
tarayici_kilidi = threading.Lock()

# ==================== AYARLAR ====================
KALDIRAC = 7
MAKSIMUM_TOPLAM_POZISYON = 3
COOLDOWN_SURESI_SANIYE = 30 * 60          # ✅ 10 dk → 30 dk

KOMISYON_ORANI = 0.001
SPREAD_MALIYETI = 0.0005
TOPLAM_MALIYET_ORANI = (KOMISYON_ORANI * 2) + SPREAD_MALIYETI
MIN_NET_KAR = 0.003

ZAMAN_DILIMI = '15m'
MUM_LIMIT = 200

EMA_KISA = 20
EMA_UZUN = 50
BB_PERIYOT = 20
BB_STD = 2.0
RSI_PERIYOT = 14
ATR_PERIYOT = 14

RSI_TREND_UST = 75
RSI_TREND_ALT = 25

ATR_SL_TREND = 1.5
ATR_SL_DURGUN = 1.2

# ✅ TRAILING AGRESİFLEŞTİRİLDİ (0.7x ATR'den başabaş)
TRAILING_ATR = [
    (0.7, 0.3),    # 0.7x ATR kâr → SL başabaş+%0.3
    (1.2, 0.6),    # 1.2x ATR kâr → SL %0.6 kâra
    (2.0, 1.2),    # 2.0x ATR kâr → SL %1.2 kâra
    (3.5, 2.0),    # 3.5x ATR kâr → SL %2.0 kâra
]

MAKS_ACIK_KALMA_SURESI_TREND = 4 * 60 * 60
MAKS_ACIK_KALMA_SURESI_DURGUN = 45 * 60

# Eşik tazeleme
SON_ESIK_GUNCELLEME = 0
ESIK_GUNCELLEME_SURESI = 2 * 60 * 60
SON_ACIL_TAZELEME = 0
ACIL_TAZELEME_MIN_ARALIK = 15 * 60
COIN_ESIKLERI = {}

VARSAYILAN_ESIK = {
    'adx_trend': 22.0,
    'adx_durgun': 18.0,
    'rsi_alt': 45.0,
    'rsi_ust': 55.0
}

# ✅ KAYIP SAYACI (yeni)
COIN_KAYIP_SAYACI = {}          # {symbol: ardışık zarar sayısı}
COIN_KAYIP_BEKLEME = {}         # {symbol: bekleme bitiş zamanı}
KAYIP_LIMIT = 2                 # 2 zarar → o coini 1 saat açma
KAYIP_BEKLEME_SURESI = 3600     # 1 saat

ARDISIK_ZARAR_LIMIT = 3
ARDISIK_ZARAR_BEKLEME = 3600
ARDISIK_ZARAR_SAYACI = 0
SON_ARDISIK_ZARAR_ZAMANI = 0
KILL_SWITCH_AKTIF = False

KRITIK_KELIMELER = [
    "hack", "hacked", "exploit", "sec ", "lawsuit", "ban", "banned",
    "delist", "bankrupt", "fraud", "scam", "rug", "collapse"
]
SON_HABER_KONTROL = 0
HABER_KONTROL_SURESI = 300

def haber_kontrol():
    global SON_HABER_KONTROL
    if time.time() - SON_HABER_KONTROL < HABER_KONTROL_SURESI:
        return
    SON_HABER_KONTROL = time.time()
    try:
        r = requests.get("https://cryptopanic.com/api/v1/posts/?auth_token=free&public=true", timeout=10)
        if r.status_code == 200:
            data = r.json()
            for post in data.get('results', [])[:20]:
                title = (post.get('title', '') or '').lower()
                for kelime in KRITIK_KELIMELER:
                    if kelime in title:
                        print(f"🚨 [HABER ALARM] {title[:100]}", flush=True)
                        tg_gonder(f"🚨 HABER ALARM\n\n{title[:200]}")
                        break
    except Exception:
        pass

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
                    clean_cd[k] = {"zaman": float(v.get("zaman", 0)), "son_yon": str(v.get("son_yon", "")), "son_fiyat": float(v.get("son_fiyat", 0))}
                else:
                    clean_cd[k] = {"zaman": float(v), "son_yon": "", "son_fiyat": 0}
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

# ==================== EŞİK TAZELEME ====================
def esikleri_tazele(force=False):
    global SON_ESIK_GUNCELLEME, COIN_ESIKLERI
    
    if not force and time.time() - SON_ESIK_GUNCELLEME < ESIK_GUNCELLEME_SURESI:
        return
    
    print(f"\n{'='*60}", flush=True)
    print(f"⚙️ [EŞİK TAZELEME] {'(ZORLA) ' if force else ''}Başlıyor... {time.strftime('%H:%M:%S')}", flush=True)
    print(f"{'='*60}", flush=True)
    
    SON_ESIK_GUNCELLEME = time.time()
    yeni_esikler = {}
    basarili = 0
    
    for symbol in takip_listesi():
        try:
            with borsa_kilidi:
                ohlcv = exchange.fetch_ohlcv(symbol, timeframe=ZAMAN_DILIMI, limit=700)
            
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df = df.iloc[:-1].reset_index(drop=True)
            
            if len(df) < 200:
                yeni_esikler[symbol] = VARSAYILAN_ESIK.copy()
                continue
            
            adx_uzun = ta.trend.ADXIndicator(
                high=df['high'], low=df['low'], close=df['close'], window=14
            ).adx().dropna()
            
            if len(adx_uzun) < 100:
                yeni_esikler[symbol] = VARSAYILAN_ESIK.copy()
                continue
            
            adx_alt_sinir = max(15, min(20, float(adx_uzun.quantile(0.20))))
            adx_ust_sinir = max(22, min(28, float(adx_uzun.quantile(0.85))))
            
            son_100 = df.tail(100)
            adx_kisa = ta.trend.ADXIndicator(
                high=son_100['high'], low=son_100['low'], close=son_100['close'], window=14
            ).adx().dropna()
            
            if len(adx_kisa) < 20:
                yeni_esikler[symbol] = VARSAYILAN_ESIK.copy()
                continue
            
            adx_trend = max(adx_alt_sinir, min(adx_ust_sinir, float(adx_kisa.quantile(0.70))))
            adx_durgun = max(13, min(adx_alt_sinir, float(adx_kisa.quantile(0.30))))
            
            rsi_uzun = ta.momentum.RSIIndicator(close=df['close'], window=14).rsi().dropna()
            rsi_alt_sinir = max(38, min(48, float(rsi_uzun.quantile(0.20))))
            rsi_ust_sinir = max(52, min(62, float(rsi_uzun.quantile(0.80))))
            
            rsi_kisa = ta.momentum.RSIIndicator(close=son_100['close'], window=14).rsi().dropna()
            rsi_alt = max(rsi_alt_sinir, min(rsi_ust_sinir, float(rsi_kisa.quantile(0.25))))
            rsi_ust = max(rsi_alt_sinir, min(rsi_ust_sinir, float(rsi_kisa.quantile(0.75))))
            
            yeni_esikler[symbol] = {
                'adx_trend': round(adx_trend, 1),
                'adx_durgun': round(adx_durgun, 1),
                'rsi_alt': round(rsi_alt, 1),
                'rsi_ust': round(rsi_ust, 1),
                'adx_alt_sinir': round(adx_alt_sinir, 1),
                'adx_ust_sinir': round(adx_ust_sinir, 1)
            }
            
            basarili += 1
            print(f"   ✅ {symbol}: ADX_T:{adx_trend:.1f} (sınır:{adx_alt_sinir:.0f}-{adx_ust_sinir:.0f}) ADX_D:{adx_durgun:.1f} RSI:{rsi_alt:.0f}-{rsi_ust:.0f}", flush=True)
            
        except Exception as e:
            print(f"   ⚠️ {symbol}: {e}", flush=True)
            yeni_esikler[symbol] = VARSAYILAN_ESIK.copy()
    
    with state_lock:
        COIN_ESIKLERI = yeni_esikler
    
    ozet = f"⚙️ EŞİKLER TAZELENDİ\n\n{basarili} coin güncellendi\n\n"
    for sym, es in list(yeni_esikler.items()):
        kisa = sym.replace('/USDT:USDT', '')
        ozet += f"{kisa}: ADX_T={es['adx_trend']} (sınır:{es.get('adx_alt_sinir',0):.0f}-{es.get('adx_ust_sinir',0):.0f})\n"
    
    tg_gonder(ozet)
    print(f"✅ [EŞİK TAZELEME] {basarili} coin güncellendi.", flush=True)

def coin_esigi_al(symbol):
    with state_lock:
        return COIN_ESIKLERI.get(symbol, VARSAYILAN_ESIK.copy())

# ==================== ACİL DURUM ====================
def acil_durum_kontrol(df):
    try:
        if len(df) < 10:
            return False
        son_6 = df.tail(6)
        adx_series = ta.trend.ADXIndicator(
            high=son_6['high'], low=son_6['low'], close=son_6['close'], window=14
        ).adx().dropna()
        
        if len(adx_series) >= 3:
            adx_baslangic = adx_series.iloc[0]
            adx_son = adx_series.iloc[-1]
            if adx_baslangic > 0:
                if (adx_son - adx_baslangic) / adx_baslangic > 0.5:
                    return True
        
        fiyat_baslangic = son_6['close'].iloc[0]
        fiyat_son = son_6['close'].iloc[-1]
        if fiyat_baslangic > 0:
            if abs(fiyat_son - fiyat_baslangic) / fiyat_baslangic > 0.02:
                return True
        return False
    except:
        return False

# ==================== PİYASA MODU ====================
def piyasa_modu_tespit(df, symbol):
    try:
        close = df['close']
        high = df['high']
        low = df['low']
        
        adx = ta.trend.ADXIndicator(high=high, low=low, close=close, window=14).adx().iloc[-1]
        ema20 = ta.trend.EMAIndicator(close=close, window=EMA_KISA).ema_indicator().iloc[-1]
        ema50 = ta.trend.EMAIndicator(close=close, window=EMA_UZUN).ema_indicator().iloc[-1]
        
        bb = ta.volatility.BollingerBands(close=close, window=BB_PERIYOT, window_dev=BB_STD)
        bb_ust = bb.bollinger_hband().iloc[-1]
        bb_alt = bb.bollinger_lband().iloc[-1]
        bb_orta = bb.bollinger_mavg().iloc[-1]
        
        rsi = ta.momentum.RSIIndicator(close=close, window=RSI_PERIYOT).rsi().iloc[-1]
        atr = ta.volatility.AverageTrueRange(high=high, low=low, close=close, window=ATR_PERIYOT).average_true_range().iloc[-1]
        
        anlik = close.iloc[-1]
        es = coin_esigi_al(symbol)
        
        if adx > es['adx_trend']:
            if ema20 > ema50 and anlik > ema20:
                mod = 'TREND_YUKARI'
            elif ema20 < ema50 and anlik < ema20:
                mod = 'TREND_ASAGI'
            else:
                mod = 'BELIRSIZ'
        elif adx < es['adx_durgun']:
            mod = 'DURGUN'
        else:
            mod = 'BELIRSIZ'
        
        return mod, adx, ema20, ema50, bb_ust, bb_alt, bb_orta, rsi, atr
    except Exception:
        return 'BELIRSIZ', 0, 0, 0, 0, 0, 0, 50, 0

# ==================== SİNYAL ÜRETİCİ ====================
def sinyal_uret(df, anlik_fiyat, symbol):
    try:
        if len(df) < 60:
            return None, None, None, None, None, "Veri yetersiz"
        
        mod, adx, ema20, ema50, bb_ust, bb_alt, bb_orta, rsi, atr = piyasa_modu_tespit(df, symbol)
        
        if atr <= 0:
            return None, None, None, None, None, "ATR=0"
        
        es = coin_esigi_al(symbol)
        son_mum = df.iloc[-1]
        onceki = df.iloc[-2]
        son_3 = df.tail(3)
        
        if mod == 'TREND_YUKARI':
            ema20_yakin = abs(son_mum['low'] - ema20) / ema20 < 0.012
            yesil_kapanis = son_mum['close'] > son_mum['open']
            ustunde = son_mum['close'] > ema20
            
            momentum = (anlik_fiyat > ema20 and ema20 > ema50 and 42 < rsi < RSI_TREND_UST and son_mum['close'] > onceki['close'])
            son_3_yesil = sum(1 for i in range(len(son_3)) if son_3['close'].iloc[i] > son_3['open'].iloc[i]) >= 2
            giris_var = (ema20_yakin and yesil_kapanis and ustunde) or momentum or (ustunde and son_3_yesil and 38 < rsi < RSI_TREND_UST)
            
            if giris_var and rsi < RSI_TREND_UST:
                sl_mesafe = atr * ATR_SL_TREND
                tp_mesafe = atr * ATR_SL_TREND * 4      # ✅ 3x → 4x ATR (daha geniş TP)
                tp = anlik_fiyat + tp_mesafe
                sl = anlik_fiyat - sl_mesafe
                brut = tp_mesafe / anlik_fiyat
                net = brut - TOPLAM_MALIYET_ORANI
                if net < MIN_NET_KAR:
                    return None, None, None, None, None, "Net kâr düşük"
                if ema20_yakin and yesil_kapanis:
                    sebep = f"TREND↑ PULLBACK | ADX:{adx:.1f} | RSI:{rsi:.0f}"
                elif momentum:
                    sebep = f"TREND↑ MOMENTUM | ADX:{adx:.1f} | RSI:{rsi:.0f}"
                else:
                    sebep = f"TREND↑ 3MUM | ADX:{adx:.1f} | RSI:{rsi:.0f}"
                return "LONG", tp, sl, sebep, atr, "OK"
            else:
                return None, None, None, None, None, "TREND↑ şart yok"
        
        if mod == 'TREND_ASAGI':
            ema20_yakin = abs(son_mum['high'] - ema20) / ema20 < 0.012
            kirmizi_kapanis = son_mum['close'] < son_mum['open']
            altinda = son_mum['close'] < ema20
            
            momentum = (anlik_fiyat < ema20 and ema20 < ema50 and RSI_TREND_ALT < rsi < 58 and son_mum['close'] < onceki['close'])
            son_3_kirmizi = sum(1 for i in range(len(son_3)) if son_3['close'].iloc[i] < son_3['open'].iloc[i]) >= 2
            giris_var = (ema20_yakin and kirmizi_kapanis and altinda) or momentum or (altinda and son_3_kirmizi and RSI_TREND_ALT < rsi < 62)
            
            if giris_var and rsi > RSI_TREND_ALT:
                sl_mesafe = atr * ATR_SL_TREND
                tp_mesafe = atr * ATR_SL_TREND * 4      # ✅ 3x → 4x ATR
                tp = anlik_fiyat - tp_mesafe
                sl = anlik_fiyat + sl_mesafe
                brut = tp_mesafe / anlik_fiyat
                net = brut - TOPLAM_MALIYET_ORANI
                if net < MIN_NET_KAR:
                    return None, None, None, None, None, "Net kâr düşük"
                if ema20_yakin and kirmizi_kapanis:
                    sebep = f"TREND↓ PULLBACK | ADX:{adx:.1f} | RSI:{rsi:.0f}"
                elif momentum:
                    sebep = f"TREND↓ MOMENTUM | ADX:{adx:.1f} | RSI:{rsi:.0f}"
                else:
                    sebep = f"TREND↓ 3MUM | ADX:{adx:.1f} | RSI:{rsi:.0f}"
                return "SHORT", tp, sl, sebep, atr, "OK"
            else:
                return None, None, None, None, None, "TREND↓ şart yok"
        
        if mod == 'DURGUN':
            alt_banda_yakin = son_mum['low'] <= bb_alt * 1.015
            asiri_satim = rsi < es['rsi_alt'] + 8
            
            if alt_banda_yakin and asiri_satim:
                sl_mesafe = atr * ATR_SL_DURGUN
                tp_mesafe = bb_orta - anlik_fiyat
                if tp_mesafe <= 0:
                    return None, None, None, None, None, "TP<0"
                tp = bb_orta
                sl = anlik_fiyat - sl_mesafe
                brut = tp_mesafe / anlik_fiyat
                net = brut - TOPLAM_MALIYET_ORANI
                if net < MIN_NET_KAR:
                    return None, None, None, None, None, "Net kâr düşük"
                sebep = f"DURGUN LONG | BB Alt + RSI:{rsi:.0f} | ADX:{adx:.1f}"
                return "LONG", tp, sl, sebep, atr, "OK"
            
            ust_banda_yakin = son_mum['high'] >= bb_ust * 0.985
            asiri_alim = rsi > es['rsi_ust'] - 8
            
            if ust_banda_yakin and asiri_alim:
                sl_mesafe = atr * ATR_SL_DURGUN
                tp_mesafe = anlik_fiyat - bb_orta
                if tp_mesafe <= 0:
                    return None, None, None, None, None, "TP<0"
                tp = bb_orta
                sl = anlik_fiyat + sl_mesafe
                brut = tp_mesafe / anlik_fiyat
                net = brut - TOPLAM_MALIYET_ORANI
                if net < MIN_NET_KAR:
                    return None, None, None, None, None, "Net kâr düşük"
                sebep = f"DURGUN SHORT | BB Üst + RSI:{rsi:.0f} | ADX:{adx:.1f}"
                return "SHORT", tp, sl, sebep, atr, "OK"
            
            return None, None, None, None, None, f"DURGUN şart yok (bb:{alt_banda_yakin} rsi:{rsi:.0f})"
        
        return None, None, None, None, None, "BELIRSIZ mod"
    except Exception as e:
        print(f"⚠️ Sinyal hatası: {e}", flush=True)
        return None, None, None, None, None, f"Hata: {e}"

# ==================== TELEGRAM ====================
def tg_gonder(mesaj):
    if not TELEGRAM_TOKEN or not CHAT_ID: return
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                      json={"chat_id": CHAT_ID, "text": mesaj}, timeout=5)
    except: pass

async def durum_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != int(CHAT_ID): return
    try:
        with borsa_kilidi:
            balance = await asyncio.to_thread(exchange.fetch_balance)
            total = float(balance['total'].get('USDT', 0))
            positions = await asyncio.to_thread(exchange.fetch_positions)
            pos = [p for p in positions if float(p.get('contracts', 0) or p.get('size', 0) or 0) > 0]

        # ✅ YENİ İSTATİSTİKLER
        gercek_tp = int(ANALITIK.get("gercek_tp", 0))
        kar_kilidi = int(ANALITIK.get("kar_kilidi", 0))
        zarar = int(ANALITIK.get("zarar", 0))
        toplam = gercek_tp + kar_kilidi + zarar
        oran = ((gercek_tp + kar_kilidi) / toplam * 100) if toplam > 0 else 0

        pos_detay = ""
        toplam_pnl_usd = 0.0
        toplam_pnl_pct = 0.0
        toplam_margin_usd = 0.0

        for p in pos:
            sym = p['symbol']
            y = str(p.get('side', '')).upper() or "?"
            g = float(p.get('entryPrice', 0))
            k = int(p.get('leverage', KALDIRAC))
            unrealized = float(p.get('unrealizedPnl', 0) or 0)
            margin = float(p.get('initialMargin', 0) or 0)

            t = await asyncio.to_thread(exchange.fetch_ticker, sym)
            gf = float(t['last'])
            
            if y == "LONG":
                fiyat_degisim = (gf - g) / g
            else:
                fiyat_degisim = (g - gf) / g
            
            roe = fiyat_degisim * 100 * k

            poz_bilgi = AKTIF_POZISYONLAR.get(sym, {})
            tp_fiyat = float(poz_bilgi.get("tp_fiyat", 0) or 0)
            
            if tp_fiyat > 0:
                if y == "LONG":
                    hedef_pct = ((tp_fiyat - g) / g) * 100 * k
                else:
                    hedef_pct = ((g - tp_fiyat) / g) * 100 * k
            else:
                hedef_pct = 0

            nokta = "🟢" if unrealized >= 0 else "🔴"

            toplam_pnl_usd += unrealized
            toplam_pnl_pct += roe
            toplam_margin_usd += margin

            kisa_sym = sym.replace('/USDT:USDT', '')

            pos_detay += (
                f"\n{nokta} {kisa_sym} | {y} ({k}x)\n"
                f"   Giriş: {g:.6f} | Şimdi: {gf:.6f}\n"
                f"   K/Z: {unrealized:+.2f} USDT | ROE: %{roe:+.2f}\n"
                f"   Hedef: %{hedef_pct:+.2f} | TP: {tp_fiyat:.6f}\n"
            )

        esik_ozet = ""
        with state_lock:
            for sym, es in list(COIN_ESIKLERI.items()):
                kisa = sym.replace('/USDT:USDT', '')
                esik_ozet += f"\n   • {kisa}: ADX_T={es['adx_trend']} RSI={es['rsi_alt']}-{es['rsi_ust']}"

        sonraki_tazeleme = int((ESIK_GUNCELLEME_SURESI - (time.time() - SON_ESIK_GUNCELLEME)) / 60)
        if sonraki_tazeleme < 0: sonraki_tazeleme = 0

        toplam_nokta = "🟢" if toplam_pnl_usd >= 0 else "🔴"

        mesaj = (
            f"📊 DURUM [ADAPTIF BOT]\n\n"
            f"💰 Kasa: {total:.2f} USDT\n"
            f"{toplam_nokta} Toplam PnL: {toplam_pnl_usd:+.2f} USDT (ROE: %{toplam_pnl_pct:+.2f})\n"
            f"📌 Açık Pozisyon: {len(pos)} / {MAKSIMUM_TOPLAM_POZISYON}\n"
            f"💵 Kullanılan Marj: {toplam_margin_usd:.2f} USDT\n"
            f"{pos_detay}\n"
            f"🎯 Gerçek TP: {gercek_tp} | 🔒 Kâr Kilidi: {kar_kilidi} | ❌ Zarar: {zarar}\n"
            f"📈 Başarı: %{oran:.1f} ({toplam} işlem)\n"
            f"⚡ Kill-Switch: {'AKTİF' if KILL_SWITCH_AKTIF else 'Pasif'}\n"
            f"🔻 Ardışık Zarar: {ARDISIK_ZARAR_SAYACI}\n\n"
            f"⚙️ Coin Bazlı Eşikler:{esik_ozet}\n"
            f"⏱️ Sonraki tazeleme: {sonraki_tazeleme} dk\n\n"
            f"📋 Havuz: {len(takip_listesi())} coin\n"
            f"⏱️ Zaman Dilimi: {ZAMAN_DILIMI}\n"
            f"💸 Toplam Maliyet: %{TOPLAM_MALIYET_ORANI*100:.2f}"
        )
        await update.message.reply_text(mesaj)
    except Exception as e:
        await update.message.reply_text(f"Hata: {e}")

async def baslat_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != int(CHAT_ID): return
    global BOT_CALISIYOR_MU, KILL_SWITCH_AKTIF, ARDISIK_ZARAR_SAYACI
    BOT_CALISIYOR_MU = True
    KILL_SWITCH_AKTIF = False
    ARDISIK_ZARAR_SAYACI = 0
    await update.message.reply_text("🟢 Bot aktif! (4 Katmanlı Koruma)")

async def durdur_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != int(CHAT_ID): return
    global BOT_CALISIYOR_MU
    BOT_CALISIYOR_MU = False
    await update.message.reply_text("⏸️ Durduruldu.")

async def kapat_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != int(CHAT_ID): return
    try:
        with borsa_kilidi:
            positions = await asyncio.to_thread(exchange.fetch_positions)
            for p in positions:
                k = float(p.get('contracts', 0) or p.get('size', 0) or 0)
                if k > 0:
                    y = str(p.get('side', '')).upper() or "LONG"
                    ky = 'sell' if y == 'LONG' else 'buy'
                    try: exchange.cancel_all_orders(p['symbol'])
                    except: pass
                    exchange.create_order(p['symbol'], 'market', ky, k, None, {'reduceOnly': True})
        await update.message.reply_text("✅ Kapatıldı.")
    except Exception as e:
        await update.message.reply_text(f"Hata: {e}")

async def havuz_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != int(CHAT_ID): return
    await update.message.reply_text("🔄 Havuz güncelleniyor...")
    basarili = havuzu_guncelle()
    if basarili:
        await update.message.reply_text(
            f"✅ Havuz Güncellendi!\n\nÇekirdek: {len(CEKIRDEK_LISTE)}\nDinamik: {len(DINAMIK_LISTE)}\n\n" +
            ", ".join([c.replace('/USDT:USDT', '') for c in DINAMIK_LISTE])
        )
    else:
        await update.message.reply_text("❌ Havuz güncellenemedi.")

async def esikler_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != int(CHAT_ID): return
    global SON_ESIK_GUNCELLEME
    SON_ESIK_GUNCELLEME = 0
    await update.message.reply_text("⚙️ Eşikler zorla tazeleniyor...")
    await asyncio.to_thread(esikleri_tazele, True)

async def sifirla_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Tüm istatistikleri sıfırla"""
    if update.effective_chat.id != int(CHAT_ID): return
    global ARDISIK_ZARAR_SAYACI, KILL_SWITCH_AKTIF, COIN_KAYIP_SAYACI, COIN_KAYIP_BEKLEME
    with state_lock:
        ANALITIK["basarili_islem_sayisi"] = 0
        ANALITIK["basarisiz_islem_sayisi"] = 0
        ANALITIK["gercek_tp"] = 0
        ANALITIK["kar_kilidi"] = 0
        ANALITIK["zarar"] = 0
        COIN_KAYIP_SAYACI = {}
        COIN_KAYIP_BEKLEME = {}
    ARDISIK_ZARAR_SAYACI = 0
    KILL_SWITCH_AKTIF = False
    hafizayi_kaydet()
    await update.message.reply_text("✅ Tüm istatistikler ve kayıp sayaçları sıfırlandı!")

# ==================== TRAILING ====================
def trailing_stop_kontrol():
    with state_lock:
        aktif_kopya = list(AKTIF_POZISYONLAR.items())

    for sym, bilgi in aktif_kopya:
        if sym not in AKTIF_POZISYONLAR: continue
        if not isinstance(bilgi, dict): continue

        yon = bilgi.get("yon", "LONG")
        g = float(bilgi.get("giris_fiyati", 0))
        sl_kayitli = float(bilgi.get("sl_fiyat", 0))
        tp_kayitli = float(bilgi.get("tp_fiyat", 0))
        atr_degeri = float(bilgi.get("atr_degeri", 0))
        giris_zaman = float(bilgi.get("giris_zamani", 0))
        mod = bilgi.get("mod", "TREND")

        if time.time() - giris_zaman < 30: continue
        if atr_degeri <= 0: continue

        try:
            with borsa_kilidi:
                t = exchange.fetch_ticker(sym)
                anlik = float(t['last'])
        except: continue

        if yon == "LONG":
            kar_mesafe = anlik - g
        else:
            kar_mesafe = g - anlik

        kar_orani = kar_mesafe / atr_degeri
        carpanlar = TRAILING_ATR if mod == "TREND" else [(0.6, 0.25), (1.0, 0.5), (1.7, 1.0)]

        yeni_sl = None
        for esik, sl_kilit in carpanlar:
            if kar_orani >= esik:
                if yon == "LONG":
                    yeni_sl = g + (atr_degeri * sl_kilit) + (g * TOPLAM_MALIYET_ORANI)
                else:
                    yeni_sl = g - (atr_degeri * sl_kilit) - (g * TOPLAM_MALIYET_ORANI)
                break

        if yeni_sl is None: continue

        iyilestirme = (yeni_sl > sl_kayitli * 1.0005) if yon == "LONG" else (yeni_sl < sl_kayitli * 0.9995)
        if not iyilestirme: continue

        try:
            with borsa_kilidi:
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
                if sym in AKTIF_POZISYONLAR:
                    AKTIF_POZISYONLAR[sym]["sl_fiyat"] = yeni_sl
            print(f"🔒 [TRAILING] {sym} | Kâr {kar_orani:.1f}x ATR → SL: {yeni_sl:.6f}", flush=True)
            tg_gonder(f"🔒 KÂR KİLİTLENDİ\n{sym} | {yon}\nKâr: {kar_orani:.1f}x ATR\nYeni SL: {yeni_sl:.6f}")
        except Exception as e:
            print(f"⚠️ Trailing: {e}", flush=True)

# ==================== KAPANIŞ (3 KATEGORİLİ) ====================
def kapanis_kontrol():
    global ARDISIK_ZARAR_SAYACI, SON_ARDISIK_ZARAR_ZAMANI, KILL_SWITCH_AKTIF
    global COIN_KAYIP_SAYACI, COIN_KAYIP_BEKLEME
    
    with state_lock:
        aktif_kopya = list(AKTIF_POZISYONLAR.items())

    try:
        with borsa_kilidi:
            raw_positions = exchange.fetch_positions()
        aktif_borsa = [p['symbol'] for p in raw_positions if float(p.get('contracts', 0) or p.get('size', 0) or 0) > 0]
    except:
        aktif_borsa = []

    for sym, bilgi in aktif_kopya:
        if sym not in AKTIF_POZISYONLAR: continue
        if not isinstance(bilgi, dict): continue

        if sym not in aktif_borsa:
            g = bilgi.get("giris_fiyati", 0)
            y = bilgi.get("yon", "LONG")
            tp_k = float(bilgi.get("tp_fiyat", 0))
            sl_k = float(bilgi.get("sl_fiyat", 0))
            cikis = g
            try:
                with borsa_kilidi:
                    t = exchange.fetch_ticker(sym)
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
                # TP'ye çok yakın kapanmış → GERÇEK TP
                kategori = "gercek_tp"
                tip = "✅ GERÇEK TP"
            elif net > 0:
                # Kârla kapanmış ama TP'ye ulaşmamış → KÂR KİLİDİ
                kategori = "kar_kilidi"
                tip = "🔒 KÂR KİLİDİYLE KAPANDI"
            else:
                # Net negatif → ZARAR
                kategori = "zarar"
                tip = "❌ ZARARLA KAPANDI"

            with state_lock:
                ANALITIK[kategori] = int(ANALITIK.get(kategori, 0)) + 1
                
                if kategori == "zarar":
                    ARDISIK_ZARAR_SAYACI += 1
                    SON_ARDISIK_ZARAR_ZAMANI = time.time()
                    # ✅ COIN KAYIP SAYACI
                    COIN_KAYIP_SAYACI[sym] = COIN_KAYIP_SAYACI.get(sym, 0) + 1
                    if COIN_KAYIP_SAYACI[sym] >= KAYIP_LIMIT:
                        COIN_KAYIP_BEKLEME[sym] = time.time() + KAYIP_BEKLEME_SURESI
                        print(f"🚫 [KAYIP SAYACI] {sym} {COIN_KAYIP_SAYACI[sym]} kez zarar → 1 saat ara", flush=True)
                        tg_gonder(f"🚫 {sym} {KAYIP_LIMIT} kez üst üste zarar\n1 saat boyunca açılmayacak")
                else:
                    ARDISIK_ZARAR_SAYACI = 0
                    COIN_KAYIP_SAYACI[sym] = 0  # Kâr edince sıfırla
                
                # Cooldown kaydet (son fiyatla)
                COIN_COOLDOWN[sym] = {
                    "zaman": float(time.time() + COOLDOWN_SURESI_SANIYE),
                    "son_yon": y,
                    "son_fiyat": float(cikis)      # ✅ Fiyat kaydediliyor
                }
                if sym in AKTIF_POZISYONLAR:
                    del AKTIF_POZISYONLAR[sym]

            hafizayi_kaydet()
            print(f"💰 [KAPANIŞ] {sym} | Çıkış: {cikis} | Net: %{net*100:.2f} | {kategori}", flush=True)
            tg_gonder(
                f"{tip}\n{sym} | Çıkış: {cikis}\n"
                f"Net Kâr: %{net*100:.2f}\n"
                f"Ardışık Zarar: {ARDISIK_ZARAR_SAYACI}"
            )

            if ARDISIK_ZARAR_SAYACI >= ARDISIK_ZARAR_LIMIT:
                KILL_SWITCH_AKTIF = True
                tg_gonder(f"🚨 KILL-SWITCH AKTİF!\n{ARDISIK_ZARAR_LIMIT} ardışık zarar.\n1 saat bekle.")
            continue

        giris_zaman = float(bilgi.get("giris_zamani", 0))
        mod = bilgi.get("mod", "TREND")
        max_sure = MAKS_ACIK_KALMA_SURESI_TREND if mod == "TREND" else MAKS_ACIK_KALMA_SURESI_DURGUN
        gecen_sure = time.time() - giris_zaman
        
        if gecen_sure > max_sure:
            try:
                with borsa_kilidi:
                    exchange.cancel_all_orders(sym)
                    miktar = None
                    for p in raw_positions:
                        if p['symbol'] == sym:
                            miktar = float(p.get('contracts', 0) or p.get('size', 0) or 0)
                            break
                    if not miktar or miktar <= 0: continue
                    y = bilgi.get("yon", "LONG")
                    ky = 'sell' if y == 'LONG' else 'buy'
                    exchange.create_order(sym, 'market', ky, miktar, None, {'reduceOnly': True})

                with state_lock:
                    if sym in AKTIF_POZISYONLAR: del AKTIF_POZISYONLAR[sym]
                    COIN_COOLDOWN[sym] = {"zaman": float(time.time() + COOLDOWN_SURESI_SANIYE), "son_yon": y, "son_fiyat": 0}

                hafizayi_kaydet()
                dakika = int(gecen_sure // 60)
                print(f"⏰ [SÜRE DOLDU] {sym} | {mod} | {dakika}dk", flush=True)
                tg_gonder(f"⏰ SÜRE DOLDU ({mod})\n{sym} | {dakika}dk")
            except Exception as e:
                print(f"⚠️ Süre: {e}", flush=True)

# ==================== ANA TARAYICI ====================
def tarayici():
    global SON_HAVUZ_GUNCELLEME, KILL_SWITCH_AKTIF, ARDISIK_ZARAR_SAYACI, SON_ACIL_TAZELEME
    print(f"🚀 [BAŞLANGIÇ] 4 KATMANLI KORUMA | {ZAMAN_DILIMI}", flush=True)
    try:
        exchange.load_markets()
    except: pass

    havuzu_guncelle()
    SON_HAVUZ_GUNCELLEME = time.time()
    esikleri_tazele(force=True)

    dongu = 0
    while True:
        if not tarayici_kilidi.acquire(blocking=False):
            time.sleep(2); continue
        try:
            if not BOT_CALISIYOR_MU:
                time.sleep(5); continue
            dongu += 1

            if time.time() - SON_HAVUZ_GUNCELLEME > HAVUZ_GUNCELLEME_SURESI:
                havuzu_guncelle()
                SON_HAVUZ_GUNCELLEME = time.time()

            haber_kontrol()
            esikleri_tazele()

            if KILL_SWITCH_AKTIF:
                if time.time() - SON_ARDISIK_ZARAR_ZAMANI > ARDISIK_ZARAR_BEKLEME:
                    KILL_SWITCH_AKTIF = False
                    ARDISIK_ZARAR_SAYACI = 0
                    tg_gonder("✅ KILL-SWITCH KAPANDI")
                else:
                    kalan = int((ARDISIK_ZARAR_BEKLEME - (time.time() - SON_ARDISIK_ZARAR_ZAMANI))/60)
                    print(f"⏸️ [KILL-SWITCH] {kalan} dk kaldı", flush=True)
                    time.sleep(30)
                    continue

            print(f"\n{'='*60}", flush=True)
            print(f"🔄 [DÖNGÜ #{dongu}] {time.strftime('%H:%M:%S')}", flush=True)
            print(f"{'='*60}", flush=True)

            kapanis_kontrol()
            trailing_stop_kontrol()

            try:
                with borsa_kilidi:
                    raw = exchange.fetch_positions()
                aktif_map = {p['symbol']: p for p in raw if float(p.get('contracts', 0) or p.get('size', 0) or 0) > 0}
                aktif_list = list(aktif_map.keys())
            except:
                raw = []
                aktif_map = {}
                aktif_list = []

            for symbol in takip_listesi():
                if not BOT_CALISIYOR_MU: break
                if len(aktif_map) >= MAKSIMUM_TOPLAM_POZISYON: break
                if symbol in aktif_list: continue

                # ✅ KAYIP SAYACI KONTROLÜ
                with state_lock:
                    kayip_bekleme = COIN_KAYIP_BEKLEME.get(symbol, 0)
                    if kayip_bekleme > time.time():
                        kalan = int((kayip_bekleme - time.time())/60)
                        print(f"   🚫 [{symbol}] Kayıp sayacı aktif. {kalan} dk", flush=True)
                        continue

                # ✅ COOLDOWN + FİYAT HAREKETİ KONTROLÜ
                with state_lock:
                    cd = COIN_COOLDOWN.get(symbol)
                    if cd:
                        z = cd.get("zaman", 0) if isinstance(cd, dict) else float(cd)
                        if z - time.time() > 0:
                            continue
                        
                        # Cooldown doldu ama fiyat kontrolü
                        if isinstance(cd, dict):
                            son_fiyat = float(cd.get("son_fiyat", 0))
                            son_yon = cd.get("son_yon", "")
                            if son_fiyat > 0:
                                # Güncel fiyatı al
                                try:
                                    with borsa_kilidi:
                                        tt = exchange.fetch_ticker(symbol)
                                    guncel = float(tt['last'])
                                    hareket = abs(guncel - son_fiyat) / son_fiyat
                                    if hareket < 0.01:  # %1'den az hareket
                                        continue
                                except:
                                    pass

                try:
                    with borsa_kilidi:
                        ohlcv = exchange.fetch_ohlcv(symbol, timeframe=ZAMAN_DILIMI, limit=MUM_LIMIT)
                        ticker = exchange.fetch_ticker(symbol)

                    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                    df = df.iloc[:-1].reset_index(drop=True)
                    anlik = float(ticker['last'])

                    if acil_durum_kontrol(df):
                        if time.time() - SON_ACIL_TAZELEME > ACIL_TAZELEME_MIN_ARALIK:
                            print(f"   🚨 [ACİL] {symbol} ani değişim! Eşikler zorla tazeleniyor...", flush=True)
                            SON_ACIL_TAZELEME = time.time()
                            esikleri_tazele(force=True)
                            tg_gonder(f"🚨 ACİL TAZELEME\n{symbol} ani hareket")

                    es = coin_esigi_al(symbol)
                    mod, adx, ema20, ema50, bb_ust, bb_alt, bb_orta, rsi, atr = piyasa_modu_tespit(df, symbol)
                    
                    print(f"   🔎 [{symbol}] Fiyat: {anlik:.6f} | Mod: {mod} | ADX: {adx:.1f} (eşik:{es['adx_trend']}) | RSI: {rsi:.0f} | ATR: {atr:.6f}", flush=True)

                    yon_s, tp_fiyat, sl_fiyat, sebep, atr_b, neden = sinyal_uret(df, anlik, symbol)

                    if yon_s is None:
                        print(f"      ⏭️ Sinyal yok → {neden}", flush=True)
                        continue
                    
                    print(f"      🎯 SİNYAL! {yon_s} | {sebep}", flush=True)

                    kapat_yon = 'sell' if yon_s == 'LONG' else 'buy'

                    with borsa_kilidi:
                        bakiye = exchange.fetch_balance()
                        toplam_b = float(bakiye['total'].get('USDT', 0))
                        serbest_b = float(bakiye.get('free', {}).get('USDT', 0) or 0)
                        exchange.set_leverage(KALDIRAC, symbol)
                        market = exchange.market(symbol)

                    kullan = min(toplam_b * 0.2, serbest_b)
                    if kullan < 1: continue

                    miktar = float(exchange.amount_to_precision(
                        symbol,
                        max((kullan * KALDIRAC) / anlik / float(market.get('contractSize', 1.0)),
                            float(market['limits']['amount']['min'] or 1.0))
                    ))

                    iy = 'buy' if yon_s == 'LONG' else 'sell'

                    with borsa_kilidi:
                        exchange.create_order(symbol, 'market', iy, miktar)
                    time.sleep(0.3)

                    sl_ok = False
                    try:
                        with borsa_kilidi:
                            exchange.create_order(symbol, 'limit', kapat_yon, miktar, tp_fiyat, {'reduceOnly': True})
                            exchange.create_order(symbol, 'stop', kapat_yon, miktar, sl_fiyat, {'stopPrice': sl_fiyat, 'reduceOnly': True})
                        sl_ok = True
                    except Exception as e:
                        print(f"   ⚠️ TP/SL: {e}", flush=True)

                    if not sl_ok:
                        try:
                            with borsa_kilidi:
                                exchange.create_order(symbol, 'market', kapat_yon, miktar, None, {'reduceOnly': True})
                        except: pass
                        continue

                    with state_lock:
                        AKTIF_POZISYONLAR[symbol] = {
                            "giris_fiyati": anlik, "yon": yon_s,
                            "tp_fiyat": tp_fiyat, "sl_fiyat": sl_fiyat,
                            "giris_zamani": time.time(),
                            "kaldirac": KALDIRAC,
                            "atr_degeri": atr_b,
                            "mod": mod,
                            "sebep": sebep
                        }
                        aktif_list.append(symbol)
                        aktif_map[symbol] = {"dummy": True}

                    hafizayi_kaydet()
                    print(f"✅ [AÇILDI] {symbol} {yon_s} | {mod} | @ {anlik}", flush=True)

                    tg_gonder(
                        f"🎯 SİNYAL! ({mod})\n"
                        f"{symbol} | {yon_s}\n"
                        f"{sebep}\n"
                        f"Giriş: {anlik}\n"
                        f"TP: {tp_fiyat}\n"
                        f"SL: {sl_fiyat}\n"
                        f"ATR: {atr_b:.6f}\n"
                        f"Maliyet: %{TOPLAM_MALIYET_ORANI*100:.2f}"
                    )
                except Exception as e:
                    print(f"⚠️ {symbol}: {e}", flush=True)
                    continue

        except Exception as e:
            print(f"⚠️ Ana döngü: {e}", flush=True)
        finally:
            try: tarayici_kilidi.release()
            except: pass

        time.sleep(15)

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
    app_tg.add_handler(CommandHandler("havuz", havuz_komutu))
    app_tg.add_handler(CommandHandler("esikler", esikler_komutu))
    app_tg.add_handler(CommandHandler("sifirla", sifirla_komutu))

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

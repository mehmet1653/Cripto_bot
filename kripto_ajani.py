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

# Araştırma coinleri
TUM_COINLER = [
    'SOL/USDT:USDT', 'XRP/USDT:USDT', 'BNB/USDT:USDT',
    'ETH/USDT:USDT', 'AVAX/USDT:USDT', 'ADA/USDT:USDT'
]

KOMISYON_ORANI = 0.001

# Cross-Validation oranları
CV_TRAIN = 0.60
CV_VALIDATE = 0.20
CV_TEST = 0.20

# ==================== ARAŞTIRMA PARAMETRELERİ ====================
ARASTIRMA = {
    "zaman_dilimi": "4h",     # ← değişti
    "gun_sayisi": 90,
    "min_islem": 3,           # ← değişti
    "min_pf": 0.9,            # ← değişti
    "min_win": 30,            # ← değişti
    "max_dd": -40,            # ← değişti
    "secilen_strateji_sayisi": 5,
}

# ==================== RİSK PARAMETRELERİ ====================
RISK = {
    "islem_riski_pct": 0.02,   # %2
    "kaldirac": 5,             # 5x
    "maks_pozisyon": 3,
    "cooldown_dk": 15,
    "gunluk_max_kayip_pct": 0.05,
}

BOT_CALISIYOR_MU = True
ARASTIRMA_CALISIYOR = False
IZLEME_CALISIYOR = False
GUN_BASI_KASA = None
GUN_BASI_TARIH = None
AKTIF_POZISYONLAR = {}
AKTIF_STRATEJI = None   # En iyi strateji
ANALITIK = {
    "basarili_islem_sayisi": 0,
    "basarisiz_islem_sayisi": 0,
    "toplam_kar": 0.0,
    "arastirma_sayisi": 0,
    "izleme_sayisi": 0,
}

# ==================== SUPABASE ====================
def hafizayi_yukle():
    varsayilan = {
        "aktif_pozisyonlar": {},
        "aktif_strateji": None,
        "analitik": ANALITIK.copy(),
        "arastirma_gecmisi": [],
        "izleme_gecmisi": [],
        "en_iyi_5": [],
    }
    try:
        r = supabase.table("bot_hafiza").select("*").eq("id", 10).execute()
        if r.data:
            v = r.data[0]
            return {
                "aktif_pozisyonlar": v.get("aktif_pozisyonlar", {}),
                "aktif_strateji": v.get("aktif_strateji"),
                "analitik": v.get("analitik", ANALITIK.copy()),
                "arastirma_gecmisi": v.get("arastirma_gecmisi", []),
                "izleme_gecmisi": v.get("izleme_gecmisi", []),
                "en_iyi_5": v.get("en_iyi_5", []),
            }
    except Exception:
        pass
    try:
        supabase.table("bot_hafiza").upsert({"id": 10, **varsayilan}).execute()
    except Exception:
        pass
    return varsayilan

def hafizayi_kaydet():
    try:
        supabase.table("bot_hafiza").upsert({
            "id": 10,
            "aktif_pozisyonlar": AKTIF_POZISYONLAR,
            "aktif_strateji": AKTIF_STRATEJI,
            "analitik": ANALITIK,
            "arastirma_gecmisi": ARASTIRMA_GECMISI[-5:],
            "izleme_gecmisi": IZLEME_GECMISI[-20:],
            "en_iyi_5": EN_IYI_5,
        }).execute()
    except Exception:
        pass

kalici = hafizayi_yukle()
AKTIF_POZISYONLAR = kalici["aktif_pozisyonlar"]
AKTIF_STRATEJI = kalici["aktif_strateji"]
ANALITIK = kalici["analitik"]
ARASTIRMA_GECMISI = kalici.get("arastirma_gecmisi", [])
IZLEME_GECMISI = kalici.get("izleme_gecmisi", [])
EN_IYI_5 = kalici.get("en_iyi_5", [])

# ==================== YARDIMCI ====================
def telegram_mesaj_gonder(mesaj):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": mesaj, "parse_mode": "Markdown"},
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

def fetch_ohlcv_guvenli(symbol, timeframe, limit=1000):
    for attempt in range(3):
        try:
            return exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
        except Exception as e:
            if attempt == 2:
                return None
            time.sleep(1)
    return None

# ==================== GÖSTERGELER ====================
def ema_hesapla(close, period):
    return close.ewm(span=period, adjust=False).mean()

def sma_hesapla(close, period):
    return close.rolling(period).mean()

def rsi_hesapla(close, period=14):
    delta = close.diff()
    gain = delta.where(delta > 0, 0).rolling(period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))

def atr_hesapla(df, period=14):
    tr = pd.concat([
        df['high'] - df['low'],
        (df['high'] - df['close'].shift()).abs(),
        (df['low'] - df['close'].shift()).abs()
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean()

def adx_hesapla(df, period=14):
    try:
        up = df['high'].diff()
        down = -df['low'].diff()
        plus_dm = np.where((up > down) & (up > 0), up, 0.0)
        minus_dm = np.where((down > up) & (down > 0), down, 0.0)
        tr = pd.concat([
            df['high'] - df['low'],
            (df['high'] - df['close'].shift()).abs(),
            (df['low'] - df['close'].shift()).abs()
        ], axis=1).max(axis=1)
        atr14 = tr.rolling(period).mean()
        plus_di = 100 * pd.Series(plus_dm, index=df.index).rolling(period).mean() / atr14
        minus_di = 100 * pd.Series(minus_dm, index=df.index).rolling(period).mean() / atr14
        dx = (abs(plus_di - minus_di) / (plus_di + minus_di).replace(0, np.nan)) * 100
        return dx.rolling(period).mean()
    except Exception:
        return pd.Series([0]*len(df), index=df.index)

def bollinger_hesapla(close, period=20, std=2.0):
    sma = close.rolling(period).mean()
    std_dev = close.rolling(period).std()
    return sma, sma + (std_dev * std), sma - (std_dev * std)

def macd_hesapla(close, fast=12, slow=26, signal=9):
    ema_fast = ema_hesapla(close, fast)
    ema_slow = ema_hesapla(close, slow)
    macd = ema_fast - ema_slow
    macd_signal = ema_hesapla(macd, signal)
    macd_hist = macd - macd_signal
    return macd, macd_signal, macd_hist

def stoch_rsi_hesapla(close, period=14):
    rsi = rsi_hesapla(close, period)
    min_rsi = rsi.rolling(period).min()
    max_rsi = rsi.rolling(period).max()
    stoch = (rsi - min_rsi) / (max_rsi - min_rsi).replace(0, np.nan) * 100
    return stoch

def donchian_hesapla(df, period=20):
    ust = df['high'].rolling(period).max()
    alt = df['low'].rolling(period).min()
    return ust, alt

def keltner_hesapla(df, period=20, mult=2.0):
    ema = ema_hesapla(df['close'], period)
    atr = atr_hesapla(df, period)
    ust = ema + (atr * mult)
    alt = ema - (atr * mult)
    return ema, ust, alt

def supertrend_hesapla(df, period=10, mult=3.0):
    hl2 = (df['high'] + df['low']) / 2
    atr = atr_hesapla(df, period)
    upperband = hl2 + (mult * atr)
    lowerband = hl2 - (mult * atr)
    supertrend = pd.Series(index=df.index, dtype=float)
    direction = pd.Series(index=df.index, dtype=int)
    for i in range(len(df)):
        if i == 0:
            supertrend.iloc[i] = upperband.iloc[i]
            direction.iloc[i] = 1
        else:
            if df['close'].iloc[i] > supertrend.iloc[i-1]:
                direction.iloc[i] = 1
            else:
                direction.iloc[i] = -1
            if direction.iloc[i] == 1:
                supertrend.iloc[i] = max(lowerband.iloc[i], supertrend.iloc[i-1]) if direction.iloc[i-1] == 1 else lowerband.iloc[i]
            else:
                supertrend.iloc[i] = min(upperband.iloc[i], supertrend.iloc[i-1]) if direction.iloc[i-1] == -1 else upperband.iloc[i]
    return supertrend, direction

def ichimoku_hesapla(df):
    tenkan = (df['high'].rolling(9).max() + df['low'].rolling(9).min()) / 2
    kijun = (df['high'].rolling(26).max() + df['low'].rolling(26).min()) / 2
    senkou_a = (tenkan + kijun) / 2
    senkou_b = (df['high'].rolling(52).max() + df['low'].rolling(52).min()) / 2
    return tenkan, kijun, senkou_a, senkou_b

def vwap_hesapla(df, period=20):
    tp = (df['high'] + df['low'] + df['close']) / 3
    vwap = (tp * df['volume']).rolling(period).sum() / df['volume'].rolling(period).sum()
    return vwap

def hacim_orani(df, period=20):
    try:
        ort = df['volume'].rolling(period).mean().iloc[-1]
        son = df['volume'].iloc[-1]
        return float(son / ort) if ort > 0 else 1.0
    except Exception:
        return 1.0

# ==================== 12 STRATEJİ FONKSİYONU ====================
# Her fonksiyon: (df, params) → sinyal dict veya None
# Sinyal: {"yon": "LONG"/"SHORT", "giris": fiyat, "stop_pct": %, "tp_pct": %}


def strat_mean_reversion(df, params):
    """Strateji 1: Bollinger + RSI"""
    if len(df) < 50: return None
    close = df['close']; open_ = df['open']
    sma, ust, alt = bollinger_hesapla(close, 20, params.get('boll_std', 2.0))
    rsi = rsi_hesapla(close, 14)
    f = close.iloc[-1]; o = open_.iloc[-1]
    su = ust.iloc[-1]; sa = alt.iloc[-1]; sr = rsi.iloc[-1]
    if pd.isna(su) or pd.isna(sr): return None
    rl = params.get('rsi_long', 32); rs = params.get('rsi_short', 68)
    if f <= sa and sr < rl and f > o:
        return {"yon": "LONG", "giris": float(f), "stop_pct": 0.020, "tp_pct": 0.040}
    if f >= su and sr > rs and f < o:
        return {"yon": "SHORT", "giris": float(f), "stop_pct": 0.020, "tp_pct": 0.040}
    return None


def strat_breakout(df, params):
    """Strateji 2: Donchian Kırılım"""
    if len(df) < 50: return None
    close = df['close']
    ust, alt = donchian_hesapla(df, 48)
    f = close.iloc[-1]; su = ust.iloc[-1]; sa = alt.iloc[-1]
    if pd.isna(su) or pd.isna(sa): return None
    h_orani = hacim_orani(df, 20)
    if h_orani < 1.0: return None
    if f > su:
        return {"yon": "LONG", "giris": float(f), "stop_pct": 0.025, "tp_pct": 0.050}
    if f < sa:
        return {"yon": "SHORT", "giris": float(f), "stop_pct": 0.025, "tp_pct": 0.050}
    return None


def strat_momentum(df, params):
    """Strateji 3: RSI Crossover"""
    if len(df) < 50: return None
    close = df['close']
    rsi = rsi_hesapla(close, 14)
    f = close.iloc[-1]; sr = rsi.iloc[-1]; sr_p = rsi.iloc[-2]
    if pd.isna(sr) or pd.isna(sr_p): return None
    cross_up = params.get('cross_up', 55)
    cross_dn = params.get('cross_dn', 45)
    if sr_p < cross_up and sr > cross_up:
        return {"yon": "LONG", "giris": float(f), "stop_pct": 0.015, "tp_pct": 0.030}
    if sr_p > cross_dn and sr < cross_dn:
        return {"yon": "SHORT", "giris": float(f), "stop_pct": 0.015, "tp_pct": 0.030}
    return None


def strat_macd(df, params):
    """Strateji 4: MACD Cross"""
    if len(df) < 50: return None
    close = df['close']
    macd, signal, hist = macd_hesapla(close)
    f = close.iloc[-1]
    m = macd.iloc[-1]; s = signal.iloc[-1]
    m_p = macd.iloc[-2]; s_p = signal.iloc[-2]
    if pd.isna(m) or pd.isna(s): return None
    if m_p < s_p and m > s and m < 0:
        return {"yon": "LONG", "giris": float(f), "stop_pct": 0.018, "tp_pct": 0.036}
    if m_p > s_p and m < s and m > 0:
        return {"yon": "SHORT", "giris": float(f), "stop_pct": 0.018, "tp_pct": 0.036}
    return None


def strat_ema_ribbon(df, params):
    """Strateji 5: EMA 9/21/50"""
    if len(df) < 60: return None
    close = df['close']
    e9 = ema_hesapla(close, 9); e21 = ema_hesapla(close, 21); e50 = ema_hesapla(close, 50)
    f = close.iloc[-1]
    a = e9.iloc[-1]; b = e21.iloc[-1]; c = e50.iloc[-1]
    a_p = e9.iloc[-2]; b_p = e21.iloc[-2]
    if pd.isna(c): return None
    if a_p < b_p and a > b and a > c:
        return {"yon": "LONG", "giris": float(f), "stop_pct": 0.020, "tp_pct": 0.040}
    if a_p > b_p and a < b and a < c:
        return {"yon": "SHORT", "giris": float(f), "stop_pct": 0.020, "tp_pct": 0.040}
    return None


def strat_stoch_rsi(df, params):
    """Strateji 6: Stochastic RSI"""
    if len(df) < 50: return None
    close = df['close']
    stoch = stoch_rsi_hesapla(close, 14)
    f = close.iloc[-1]; s = stoch.iloc[-1]; s_p = stoch.iloc[-2]
    if pd.isna(s) or pd.isna(s_p): return None
    if s_p < 20 and s > 20:
        return {"yon": "LONG", "giris": float(f), "stop_pct": 0.018, "tp_pct": 0.036}
    if s_p > 80 and s < 80:
        return {"yon": "SHORT", "giris": float(f), "stop_pct": 0.018, "tp_pct": 0.036}
    return None


def strat_bollinger_squeeze(df, params):
    """Strateji 7: Bollinger Squeeze (düşük volatilite → kırılım)"""
    if len(df) < 50: return None
    close = df['close']
    sma, ust, alt = bollinger_hesapla(close, 20, 2.0)
    bant_genislik = (ust - alt) / sma
    ort_genislik = bant_genislik.rolling(20).mean().iloc[-1]
    son_genislik = bant_genislik.iloc[-1]
    if pd.isna(ort_genislik) or ort_genislik == 0: return None
    if son_genislik < ort_genislik * 0.7:  # Squeeze
        f = close.iloc[-1]
        if f > ust.iloc[-1]:
            return {"yon": "LONG", "giris": float(f), "stop_pct": 0.025, "tp_pct": 0.050}
        if f < alt.iloc[-1]:
            return {"yon": "SHORT", "giris": float(f), "stop_pct": 0.025, "tp_pct": 0.050}
    return None


def strat_supertrend(df, params):
    """Strateji 8: SuperTrend"""
    if len(df) < 50: return None
    st, direction = supertrend_hesapla(df, 10, 3.0)
    f = df['close'].iloc[-1]
    d = direction.iloc[-1]; d_p = direction.iloc[-2]
    if pd.isna(d) or pd.isna(d_p): return None
    if d_p == -1 and d == 1:
        return {"yon": "LONG", "giris": float(f), "stop_pct": 0.020, "tp_pct": 0.040}
    if d_p == 1 and d == -1:
        return {"yon": "SHORT", "giris": float(f), "stop_pct": 0.020, "tp_pct": 0.040}
    return None


def strat_keltner(df, params):
    """Strateji 9: Keltner Channel"""
    if len(df) < 50: return None
    close = df['close']
    ema, ust, alt = keltner_hesapla(df, 20, 2.0)
    f = close.iloc[-1]
    su = ust.iloc[-1]; sa = alt.iloc[-1]
    if pd.isna(su) or pd.isna(sa): return None
    if f <= sa:
        return {"yon": "LONG", "giris": float(f), "stop_pct": 0.020, "tp_pct": 0.040}
    if f >= su:
        return {"yon": "SHORT", "giris": float(f), "stop_pct": 0.020, "tp_pct": 0.040}
    return None


def strat_rsi_divergence(df, params):
    """Strateji 10: RSI Divergence (basit)"""
    if len(df) < 50: return None
    close = df['close']
    rsi = rsi_hesapla(close, 14)
    f = close.iloc[-1]
    # Son 20 mum: fiyat düşerken RSI yükseliyorsa → Bullish
    fiyat_dusuyor = close.iloc[-1] < close.iloc[-10]
    rsi_yukseliyor = rsi.iloc[-1] > rsi.iloc[-10]
    fiyat_yukseliyor = close.iloc[-1] > close.iloc[-10]
    rsi_dusuyor = rsi.iloc[-1] < rsi.iloc[-10]
    if fiyat_dusuyor and rsi_yukseliyor and rsi.iloc[-1] < 40:
        return {"yon": "LONG", "giris": float(f), "stop_pct": 0.020, "tp_pct": 0.040}
    if fiyat_yukseliyor and rsi_dusuyor and rsi.iloc[-1] > 60:
        return {"yon": "SHORT", "giris": float(f), "stop_pct": 0.020, "tp_pct": 0.040}
    return None


def strat_vwap_reversion(df, params):
    """Strateji 11: VWAP Reversion"""
    if len(df) < 50: return None
    close = df['close']
    vwap = vwap_hesapla(df, 20)
    f = close.iloc[-1]; v = vwap.iloc[-1]
    if pd.isna(v): return None
    if f < v * 0.98:
        return {"yon": "LONG", "giris": float(f), "stop_pct": 0.018, "tp_pct": 0.036}
    if f > v * 1.02:
        return {"yon": "SHORT", "giris": float(f), "stop_pct": 0.018, "tp_pct": 0.036}
    return None


def strat_ichimoku(df, params):
    """Strateji 12: Ichimoku Cloud"""
    if len(df) < 60: return None
    close = df['close']
    tenkan, kijun, senkou_a, senkou_b = ichimoku_hesapla(df)
    f = close.iloc[-1]
    t = tenkan.iloc[-1]; k = kijun.iloc[-1]
    sa = senkou_a.iloc[-1]; sb = senkou_b.iloc[-1]
    if pd.isna(t) or pd.isna(k) or pd.isna(sa) or pd.isna(sb): return None
    cloud_top = max(sa, sb); cloud_bot = min(sa, sb)
    if f > cloud_top and t > k:
        return {"yon": "LONG", "giris": float(f), "stop_pct": 0.022, "tp_pct": 0.044}
    if f < cloud_bot and t < k:
        return {"yon": "SHORT", "giris": float(f), "stop_pct": 0.022, "tp_pct": 0.044}
    return None


# ==================== STRATEJİ HARİTASI ====================
STRATEJILER = {
    "mean_reversion": {
        "fonksiyon": strat_mean_reversion,
        "parametreler": [
            {"boll_std": 1.7, "rsi_long": 32, "rsi_short": 68},
            {"boll_std": 2.0, "rsi_long": 32, "rsi_short": 68},
            {"boll_std": 2.0, "rsi_long": 38, "rsi_short": 62},
            {"boll_std": 2.3, "rsi_long": 35, "rsi_short": 65},
            {"boll_std": 1.7, "rsi_long": 40, "rsi_short": 60},
        ]
    },
    "breakout": {
        "fonksiyon": strat_breakout,
        "parametreler": [{}, {}, {}, {}, {}]  # Parametre yok
    },
    "momentum": {
        "fonksiyon": strat_momentum,
        "parametreler": [
            {"cross_up": 50, "cross_dn": 50},
            {"cross_up": 55, "cross_dn": 45},
            {"cross_up": 60, "cross_dn": 40},
            {"cross_up": 52, "cross_dn": 48},
            {"cross_up": 58, "cross_dn": 42},
        ]
    },
    "macd": {
        "fonksiyon": strat_macd,
        "parametreler": [{}, {}, {}, {}, {}]
    },
    "ema_ribbon": {
        "fonksiyon": strat_ema_ribbon,
        "parametreler": [{}, {}, {}, {}, {}]
    },
    "stoch_rsi": {
        "fonksiyon": strat_stoch_rsi,
        "parametreler": [
            {}, {}, {}, {}, {}
        ]
    },
    "bollinger_squeeze": {
        "fonksiyon": strat_bollinger_squeeze,
        "parametreler": [{}, {}, {}, {}, {}]
    },
    "supertrend": {
        "fonksiyon": strat_supertrend,
        "parametreler": [{}, {}, {}, {}, {}]
    },
    "keltner": {
        "fonksiyon": strat_keltner,
        "parametreler": [{}, {}, {}, {}, {}]
    },
    "rsi_divergence": {
        "fonksiyon": strat_rsi_divergence,
        "parametreler": [{}, {}, {}, {}, {}]
    },
    "vwap_reversion": {
        "fonksiyon": strat_vwap_reversion,
        "parametreler": [{}, {}, {}, {}, {}]
    },
    "ichimoku": {
        "fonksiyon": strat_ichimoku,
        "parametreler": [{}, {}, {}, {}, {}]
    },
}
# ==================== BACKTEST MOTORU ====================
def backtest_strateji(df, sinyal_fn, params):
    """Tek strateji + tek parametre setini backtest et."""
    try:
        trades = []
        poz = None
        for i in range(60, len(df)):
            bar = df.iloc[i]
            high = bar['high']; low = bar['low']; close = bar['close']

            if poz is not None:
                if poz['yon'] == 'LONG':
                    if low <= poz['stop']:
                        trades.append({**poz, 'cikis': poz['stop']}); poz = None
                    elif high >= poz['tp']:
                        trades.append({**poz, 'cikis': poz['tp']}); poz = None
                else:
                    if high >= poz['stop']:
                        trades.append({**poz, 'cikis': poz['stop']}); poz = None
                    elif low <= poz['tp']:
                        trades.append({**poz, 'cikis': poz['tp']}); poz = None
                continue

            df_slice = df.iloc[max(0, i-100):i+1].reset_index(drop=True)
            try:
                sig = sinyal_fn(df_slice, params)
            except Exception:
                continue
            if sig:
                fiyat = sig['giris']
                sp = sig['stop_pct']; tp_pct = sig['tp_pct']
                if sig['yon'] == 'LONG':
                    stop = fiyat * (1 - sp); tp = fiyat * (1 + tp_pct)
                else:
                    stop = fiyat * (1 + sp); tp = fiyat * (1 - tp_pct)
                poz = {"yon": sig['yon'], "giris": float(fiyat),
                       "stop": float(stop), "tp": float(tp)}

        if not trades:
            return None
        kazanclar = []
        for t in trades:
            if t['yon'] == 'LONG':
                pct = (t['cikis'] - t['giris']) / t['giris']
            else:
                pct = (t['giris'] - t['cikis']) / t['giris']
            pct -= KOMISYON_ORANI
            kazanclar.append(pct)
        k = np.array(kazanclar)
        kaz = k[k > 0]; kay = k[k < 0]
        win = len(kaz) / len(k) * 100 if len(k) else 0
        pf = abs(kaz.sum() / kay.sum()) if len(kay) and kay.sum() != 0 else 999
        equity = np.cumprod(1 + k)
        peak = np.maximum.accumulate(equity)
        dd = (equity - peak) / peak
        max_dd = dd.min() * 100 if len(dd) else 0
        return {
            "islem": len(trades),
            "win_rate": round(win, 2),
            "pf": round(pf, 3) if pf != 999 else 999,
            "max_dd": round(max_dd, 2),
            "toplam": round((equity[-1] - 1) * 100, 2) if len(equity) else 0,
        }
    except Exception:
        return None


# ==================== CROSS-VALIDATION ====================
def cross_validate(symbol, strateji_adi, params, gun_sayisi=180):
    """
    3 aşamalı Cross-Validation:
    - Train: %50
    - Validate: %25
    - Test: %25
    Sadece 3 aşamada da iyi olan strateji kabul.
    """
    try:
        limit = min(gun_sayisi * 24, 1000)
        ohlcv = fetch_ohlcv_guvenli(symbol, ARASTIRMA['zaman_dilimi'], limit=limit)
        if ohlcv is None or len(ohlcv) < 200:
            return None
        df = pd.DataFrame(ohlcv, columns=['timestamp','open','high','low','close','volume'])

        n = len(df)
        train_end = int(n * CV_TRAIN)
        val_end = int(n * (CV_TRAIN + CV_VALIDATE))

        df_train = df.iloc[:train_end].reset_index(drop=True)
        df_val = df.iloc[train_end:val_end].reset_index(drop=True)
        df_test = df.iloc[val_end:].reset_index(drop=True)

        if len(df_train) < 100 or len(df_val) < 50 or len(df_test) < 50:
            return None

        sinyal_fn = STRATEJILER[strateji_adi]['fonksiyon']

        r_train = backtest_strateji(df_train, sinyal_fn, params)
        r_val = backtest_strateji(df_val, sinyal_fn, params)
        r_test = backtest_strateji(df_test, sinyal_fn, params)

        if not r_train or not r_val or not r_test:
            return None

        # Filtreler (her aşamada)
        min_i = ARASTIRMA['min_islem']
        if r_train['islem'] < min_i or r_val['islem'] < max(3, min_i//2) or r_test['islem'] < max(3, min_i//2):
            return None

        min_pf = ARASTIRMA['min_pf']
        if r_train['pf'] < min_pf or r_val['pf'] < min_pf or r_test['pf'] < min_pf:
            return None

        # Ortalama getiri (3 aşama ağırlıklı)
        ort_toplam = (r_train['toplam'] * 0.5 + r_val['toplam'] * 0.25 + r_test['toplam'] * 0.25)
        ort_pf = (r_train['pf'] * 0.5 + r_val['pf'] * 0.25 + r_test['pf'] * 0.25)
        ort_win = (r_train['win_rate'] * 0.5 + r_val['win_rate'] * 0.25 + r_test['win_rate'] * 0.25)
        en_kotu_dd = min(r_train['max_dd'], r_val['max_dd'], r_test['max_dd'])

        # Max DD kontrolü
        if en_kotu_dd < ARASTIRMA['max_dd']:
            return None

        return {
            "strateji": strateji_adi,
            "symbol": symbol,
            "params": params,
            "train_pf": r_train['pf'], "train_win": r_train['win_rate'], "train_toplam": r_train['toplam'], "train_islem": r_train['islem'],
            "val_pf": r_val['pf'], "val_win": r_val['win_rate'], "val_toplam": r_val['toplam'], "val_islem": r_val['islem'],
            "test_pf": r_test['pf'], "test_win": r_test['win_rate'], "test_toplam": r_test['toplam'], "test_islem": r_test['islem'],
            "ort_pf": round(ort_pf, 3),
            "ort_win": round(ort_win, 2),
            "ort_toplam": round(ort_toplam, 2),
            "en_kotu_dd": round(en_kotu_dd, 2),
            "toplam_islem": r_train['islem'] + r_val['islem'] + r_test['islem'],
        }
    except Exception:
        return None


# ==================== ARAŞTIRMA MOTORU ====================
def arastirma_yap():
    """
    Tüm stratejileri × coinleri × parametreleri test et.
    Cross-Validation ile overfit engelle.
    En iyi 5'i seç.
    """
    global ARASTIRMA_CALISIYOR, EN_IYI_5, ARASTIRMA_GECMISI, AKTIF_STRATEJI

    toplam_test = len(STRATEJILER) * len(TUM_COINLER) * 5
    telegram_mesaj_gonder(
        f"🔬 *ARAŞTIRMA BAŞLADI*\n"
        f"{len(STRATEJILER)} strateji × {len(TUM_COINLER)} coin × 5 parametre\n"
        f"Toplam: ~{toplam_test} test\n"
        f"Cross-Validation: 50/25/25\n"
        f"Tahmini süre: 60-90 dk"
    )

    tum_sonuclar = []
    sayac = 0

    for strat_adi, strat_data in STRATEJILER.items():
        for symbol in TUM_COINLER:
            for params in strat_data['parametreler']:
                sayac += 1
                if sayac % 10 == 0:
                    print(f"🔬 Test {sayac}/{toplam_test} | {strat_adi} | {symbol.split('/')[0]}", flush=True)

                sonuc = cross_validate(symbol, strat_adi, params, gun_sayisi=ARASTIRMA['gun_sayisi'])
                if sonuc:
                    tum_sonuclar.append(sonuc)

    # En iyi 5'i seç (ort_pf'e göre)
    if not tum_sonuclar:
        telegram_mesaj_gonder("❌ Hiçbir strateji filtreleri geçemedi!")
        ARASTIRMA_CALISIYOR = False
        return

    tum_sonuclar.sort(key=lambda x: x['ort_pf'], reverse=True)
    EN_IYI_5 = tum_sonuclar[:ARASTIRMA['secilen_strateji_sayisi']]

    # En iyi stratejiyi aktif et
    if EN_IYI_5:
        AKTIF_STRATEJI = EN_IYI_5[0]

    # Geçmişe kaydet
    ARASTIRMA_GECMISI.append({
        "tarih": datetime.now(timezone.utc).isoformat(),
        "toplam_test": toplam_test,
        "kabul_edilen": len(tum_sonuclar),
        "en_iyi_5": EN_IYI_5
    })
    ANALITIK["arastirma_sayisi"] = ANALITIK.get("arastirma_sayisi", 0) + 1

    hafizayi_kaydet()

    # Telegram raporu
    rapor = f"🏆 *ARAŞTIRMA TAMAMLANDI*\n\n"
    rapor += f"✅ Kabul edilen: `{len(tum_sonuclar)}` strateji\n"
    rapor += f"🏅 En iyi 5:\n\n"

    for i, s in enumerate(EN_IYI_5, 1):
        rapor += (
            f"*{i}. {s['strateji']}* ({s['symbol'].split('/')[0]})\n"
            f"  Ort PF: `{s['ort_pf']}` | Win: `{s['ort_win']}%`\n"
            f"  Getiri: `{s['ort_toplam']}%` | İşl: `{s['toplam_islem']}`\n"
            f"  DD: `{s['en_kotu_dd']}%` | Param: `{s['params']}`\n\n"
        )

    rapor += f"🎯 *Aktif edildi:* `{AKTIF_STRATEJI['strateji']}` ({AKTIF_STRATEJI['symbol'].split('/')[0]})"
    telegram_mesaj_gonder(rapor)
    ARASTIRMA_CALISIYOR = False


# ==================== İZLEME MOTORU ====================
def izleme_yap():
    """En iyi 5 stratejiyi son 30 günlük veriyle tekrar test et."""
    global IZLEME_GECMISI, EN_IYI_5, AKTIF_STRATEJI

    if not EN_IYI_5:
        telegram_mesaj_gonder("⚠️ Önce `/arastir` yap.")
        return

    telegram_mesaj_gonder("📊 *Aylık izleme başladı...*")

    guncel_sonuclar = []
    for s in EN_IYI_5:
        try:
            sinyal_fn = STRATEJILER[s['strateji']]['fonksiyon']
            ohlcv = fetch_ohlcv_guvenli(s['symbol'], ARASTIRMA['zaman_dilimi'], limit=720)
            if ohlcv is None or len(ohlcv) < 100:
                continue
            df = pd.DataFrame(ohlcv, columns=['timestamp','open','high','low','close','volume'])
            r = backtest_strateji(df, sinyal_fn, s['params'])
            if r:
                guncel_sonuclar.append({
                    "strateji": s['strateji'],
                    "symbol": s['symbol'],
                    "eski_pf": s['ort_pf'],
                    "yeni_pf": r['pf'],
                    "yeni_win": r['win_rate'],
                    "yeni_toplam": r['toplam'],
                    "yeni_islem": r['islem'],
                    "degisim": round((r['pf'] - s['ort_pf']) / s['ort_pf'] * 100, 1) if s['ort_pf'] > 0 else 0,
                })
        except Exception:
            continue

    if not guncel_sonuclar:
        telegram_mesaj_gonder("⚠️ İzleme sonucu yok.")
        return

    guncel_sonuclar.sort(key=lambda x: x['yeni_pf'], reverse=True)

    rapor = "📊 *AYLIK İZLEME RAPORU*\n\n"
    for i, s in enumerate(guncel_sonuclar, 1):
        durum = "✅" if s['degisim'] >= -10 else "❌"
        rapor += (
            f"{durum} *{s['strateji']}* ({s['symbol'].split('/')[0]})\n"
            f"  Eski PF: `{s['eski_pf']}` → Yeni: `{s['yeni_pf']}` ({s['degisim']:+.1f}%)\n"
            f"  Win: `{s['yeni_win']}%` | Getiri: `{s['yeni_toplam']}%`\n\n"
        )

    # En iyi güncel stratejiyi aktif et
    en_iyi_guncel = guncel_sonuclar[0]
    for s in EN_IYI_5:
        if s['strateji'] == en_iyi_guncel['strateji'] and s['symbol'] == en_iyi_guncel['symbol']:
            AKTIF_STRATEJI = s
            break

    rapor += f"🎯 *Aktif:* `{AKTIF_STRATEJI['strateji']}` ({AKTIF_STRATEJI['symbol'].split('/')[0]})"

    IZLEME_GECMISI.append({
        "tarih": datetime.now(timezone.utc).isoformat(),
        "sonuclar": guncel_sonuclar,
        "aktif": AKTIF_STRATEJI
    })
    ANALITIK["izleme_sayisi"] = ANALITIK.get("izleme_sayisi", 0) + 1
    hafizayi_kaydet()

    telegram_mesaj_gonder(rapor)


# ==================== SİNYAL ÜRETİMİ (AKTİF STRATEJİ) ====================
def aktif_sinyal_uret(symbol):
    """Aktif strateji ile sinyal üret."""
    if not AKTIF_STRATEJI:
        return None, "aktif strateji yok"
    if AKTIF_STRATEJI['symbol'] != symbol:
        return None, "bu coin aktif stratejide değil"

    try:
        sinyal_fn = STRATEJILER[AKTIF_STRATEJI['strateji']]['fonksiyon']
        ohlcv = fetch_ohlcv_guvenli(symbol, ARASTIRMA['zaman_dilimi'], limit=120)
        if ohlcv is None or len(ohlcv) < 60:
            return None, "veri yetersiz"
        df = pd.DataFrame(ohlcv, columns=['timestamp','open','high','low','close','volume'])
        sig = sinyal_fn(df, AKTIF_STRATEJI['params'])
        if sig:
            return sig, "OK"
        return None, "sinyal yok"
    except Exception as e:
        return None, str(e)[:30]


# ==================== POZİSYON AÇMA ====================
def pozisyon_ac(symbol, sig):
    """Aktif strateji ile pozisyon aç."""
    try:
        bal = exchange.fetch_balance()
        kasa = float(bal['total'].get('USDT', 0))
        if kasa < 10:
            return False
        if not set_leverage_and_margin_safely(symbol, RISK['kaldirac']):
            return False

        risk_usdt = kasa * RISK['islem_riski_pct']
        stop_pct = sig['stop_pct']
        poz_degeri = risk_usdt / stop_pct

        try:
            market_info = exchange.market(symbol)
            cs = float(market_info.get('contractSize', 1.0))
            ham = poz_degeri / (sig['giris'] * cs)
            miktar = float(exchange.amount_to_precision(symbol, max(ham, 0.001)))
            if miktar <= 0:
                return False
        except Exception:
            return False

        tum_emirleri_iptal_et(symbol)
        emir = exchange.create_order(symbol, 'market',
            'buy' if sig['yon'] == 'LONG' else 'sell', miktar)
        giris = float(emir.get('average') or emir.get('price') or sig['giris'])
        time.sleep(0.5)

        if sig['yon'] == 'LONG':
            stop = giris * (1 - stop_pct)
            tp = giris * (1 + sig['tp_pct'])
            kapat_yon = 'sell'
        else:
            stop = giris * (1 + stop_pct)
            tp = giris * (1 - sig['tp_pct'])
            kapat_yon = 'buy'

        stop = float(exchange.price_to_precision(symbol, stop))
        tp = float(exchange.price_to_precision(symbol, tp))

        try:
            exchange.create_order(symbol, 'stop', kapat_yon, miktar, stop,
                {'stopPrice': stop, 'triggerPrice': stop, 'reduceOnly': True})
        except Exception:
            try:
                exchange.create_order(symbol, 'stop_market', kapat_yon, miktar, stop,
                    {'stopPrice': stop, 'triggerPrice': stop, 'reduceOnly': True})
            except Exception: pass

        try:
            exchange.create_order(symbol, 'limit', kapat_yon, miktar, tp, {'reduceOnly': True})
        except Exception: pass

        AKTIF_POZISYONLAR[symbol] = {
            "yon": sig['yon'], "giris": giris, "stop": stop, "tp": tp,
            "miktar": miktar, "strateji": AKTIF_STRATEJI['strateji'],
            "giris_zaman": int(time.time()*1000)
        }
        hafizayi_kaydet()

        telegram_mesaj_gonder(
            f"🎯 *İŞLEM AÇILDI*\n"
            f"📌 `{symbol[:12]}` | *{sig['yon']}*\n"
            f"📊 Strateji: `{AKTIF_STRATEJI['strateji']}`\n"
            f"💰 Giriş: `{giris}` | SL: `{stop}` | TP: `{tp}`"
        )
        return True
    except Exception as e:
        print(f"❌ Pozisyon açma: {e}", flush=True)
        return False
    # ==================== FLASK ====================
@app.route('/')
def home():
    aktif = AKTIF_STRATEJI['strateji'] if AKTIF_STRATEJI else "yok"
    return f"Araştırma Botu | Aktif: {aktif} | Poz: {len(AKTIF_POZISYONLAR)}"


# ==================== TELEGRAM KOMUTLARI ====================
async def durum_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        balance = exchange.fetch_balance()
        total = float(balance['total'].get('USDT', 0))
        free = float(balance['free'].get('USDT', 0))
        try:
            raw = exchange.fetch_positions()
            poslari = [p for p in raw if float(p.get('contracts', 0) or p.get('size', 0) or 0) > 0]
        except Exception:
            poslari = []
        pnl = sum(float(p.get('unrealizedPnl', 0)) for p in poslari)

        aktif_detay = "❌ Aktif strateji yok"
        if AKTIF_STRATEJI:
            aktif_detay = (
                f"🏆 *{AKTIF_STRATEJI['strateji']}* ({AKTIF_STRATEJI['symbol'].split('/')[0]})\n"
                f"  Ort PF: `{AKTIF_STRATEJI['ort_pf']}` | Win: `{AKTIF_STRATEJI['ort_win']}%`\n"
                f"  Getiri: `{AKTIF_STRATEJI['ort_toplam']}%`"
            )

        bs = ANALITIK.get("basarili_islem_sayisi", 0)
        bz = ANALITIK.get("basarisiz_islem_sayisi", 0)
        tot = bs + bz
        oran = (bs / tot * 100) if tot else 0

        detay = ""
        if poslari:
            detay = "\n📋 *Aktif Pozisyonlar:*\n"
            for p in poslari:
                detay += f"• `{p.get('symbol')[:12]}` | {str(p.get('side','')).upper()} | `{float(p.get('unrealizedPnl',0)):+.2f}`\n"

        mesaj = (
            f"🔬 *ARAŞTIRMA BOTU*\n\n"
            f"💰 Toplam: `{total:.2f}` USDT (Serbest: `{free:.2f}`)\n"
            f"📈 PnL: `{pnl:+.2f}` USDT\n"
            f"📌 Pozisyon: `{len(poslari)}/{RISK['maks_pozisyon']}`\n\n"
            f"🎯 {aktif_detay}\n\n"
            f"📚 Araştırma: `{ANALITIK.get('arastirma_sayisi', 0)}`\n"
            f"👁️ İzleme: `{ANALITIK.get('izleme_sayisi', 0)}`\n"
            f"✅ TP: `{bs}` | ❌ Stop: `{bz}` | Başarı: `%{oran:.1f}`\n"
            f"{detay}"
        )
        await update.message.reply_text(mesaj, parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"Hata: {e}")


async def baslat_komutu(update, context):
    global BOT_CALISIYOR_MU
    BOT_CALISIYOR_MU = True
    await update.message.reply_text("▶️ *Bot Aktif!*", parse_mode='Markdown')


async def durdur_komutu(update, context):
    global BOT_CALISIYOR_MU
    BOT_CALISIYOR_MU = False
    await update.message.reply_text("⏸️ *Durduruldu.*", parse_mode='Markdown')


async def kapat_komutu(update, context):
    await update.message.reply_text("🛑 *Her şey kapatılıyor...*", parse_mode='Markdown')
    try:
        for pos in exchange.fetch_positions():
            k = float(pos.get('contracts', 0) or pos.get('size', 0) or 0)
            if k > 0:
                sym = pos['symbol']
                yon = str(pos.get('side', '')).upper()
                kapat = 'sell' if yon == 'LONG' else 'buy'
                tum_emirleri_iptal_et(sym)
                exchange.create_order(sym, 'market', kapat, k, None, {'reduce_only': True})
        AKTIF_POZISYONLAR.clear()
        hafizayi_kaydet()
        await update.message.reply_text("✅ *Kapatıldı.*", parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"⚠️ {e}", parse_mode='Markdown')


async def arastir_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global ARASTIRMA_CALISIYOR
    if ARASTIRMA_CALISIYOR:
        await update.message.reply_text("⏳ Zaten araştırma çalışıyor.")
        return
    ARASTIRMA_CALISIYOR = True
    await update.message.reply_text(
        "🔬 *Araştırma başladı*\n"
        "12 strateji × 6 coin × 5 parametre = 360 test\n"
        "Cross-Validation: 50/25/25\n"
        "Tahmini süre: 60-90 dk\n\n"
        "Sabırlı ol, sonuç gelince atacağım.",
        parse_mode='Markdown'
    )
    threading.Thread(target=arastirma_yap, daemon=True).start()


async def izle_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global IZLEME_CALISIYOR
    if IZLEME_CALISIYOR:
        await update.message.reply_text("⏳ Zaten çalışıyor.")
        return
    IZLEME_CALISIYOR = True
    await update.message.reply_text("📊 *İzleme başladı...*", parse_mode='Markdown')
    threading.Thread(target=izleme_yap, daemon=True).start()


async def en_iyi_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not EN_IYI_5:
        await update.message.reply_text("Henüz araştırma yok. `/arastir` yaz.", parse_mode='Markdown')
        return
    satirlar = ["🏆 *EN İYİ 5 STRATEJİ:*\n"]
    for i, s in enumerate(EN_IYI_5, 1):
        satirlar.append(
            f"*{i}. {s['strateji']}* ({s['symbol'].split('/')[0]})\n"
            f"  PF: `{s['ort_pf']}` | Win: `{s['ort_win']}%` | İşl: `{s['toplam_islem']}`\n"
            f"  TOT: `{s['ort_toplam']}%` | DD: `{s['en_kotu_dd']}%`\n"
            f"  Param: `{s['params']}`\n"
        )
    await update.message.reply_text("\n".join(satirlar), parse_mode='Markdown')


async def aktif_strateji_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not AKTIF_STRATEJI:
        await update.message.reply_text("Aktif strateji yok. `/arastir` yaz.", parse_mode='Markdown')
        return
    s = AKTIF_STRATEJI
    mesaj = (
        f"🎯 *AKTİF STRATEJİ*\n\n"
        f"📛 Strateji: `{s['strateji']}`\n"
        f"💰 Coin: `{s['symbol']}`\n"
        f"📊 Parametreler: `{s['params']}`\n\n"
        f"*Backtest Sonuçları:*\n"
        f"  Train PF: `{s['train_pf']}` | Win: `{s['train_win']}%`\n"
        f"  Validate PF: `{s['val_pf']}` | Win: `{s['val_win']}%`\n"
        f"  Test PF: `{s['test_pf']}` | Win: `{s['test_win']}%`\n\n"
        f"📈 Ortalama PF: `{s['ort_pf']}`\n"
        f"🎯 Ortalama Getiri: `{s['ort_toplam']}%`\n"
        f"📉 En Kötü DD: `{s['en_kotu_dd']}%`"
    )
    await update.message.reply_text(mesaj, parse_mode='Markdown')


async def temizle_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global EN_IYI_5, AKTIF_STRATEJI, ARASTIRMA_GECMISI, IZLEME_GECMISI
    EN_IYI_5 = []
    AKTIF_STRATEJI = None
    ARASTIRMA_GECMISI = []
    IZLEME_GECMISI = []
    hafizayi_kaydet()
    await update.message.reply_text("🗑️ *Temizlendi.*", parse_mode='Markdown')


async def test_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Aktif stratejiyi tek seferlik test et."""
    if not AKTIF_STRATEJI:
        await update.message.reply_text("Aktif strateji yok.", parse_mode='Markdown')
        return
    try:
        sinyal_fn = STRATEJILER[AKTIF_STRATEJI['strateji']]['fonksiyon']
        ohlcv = fetch_ohlcv_guvenli(AKTIF_STRATEJI['symbol'], ARASTIRMA['zaman_dilimi'], limit=200)
        if ohlcv is None:
            await update.message.reply_text("❌ Veri çekilemedi.")
            return
        df = pd.DataFrame(ohlcv, columns=['timestamp','open','high','low','close','volume'])
        r = backtest_strateji(df, sinyal_fn, AKTIF_STRATEJI['params'])
        if r:
            mesaj = (
                f"📊 *SON TEST*\n\n"
                f"Strateji: `{AKTIF_STRATEJI['strateji']}`\n"
                f"İŞL: `{r['islem']}` | WIN: `{r['win_rate']}%`\n"
                f"PF: `{r['pf']}` | DD: `{r['max_dd']}%`\n"
                f"TOT: `{r['toplam']}%`"
            )
            await update.message.reply_text(mesaj, parse_mode='Markdown')
        else:
            await update.message.reply_text("❌ Sonuç yok.")
    except Exception as e:
        await update.message.reply_text(f"Hata: {e}")


async def strateji_listesi_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    satirlar = ["📋 *12 STRATEJİ:*\n"]
    for i, (ad, data) in enumerate(STRATEJILER.items(), 1):
        satirlar.append(f"{i}. `{ad}` — {len(data['parametreler'])} parametre")
    satirlar.append(f"\n💰 Coin: {len(TUM_COINLER)} adet")
    satirlar.append(f"🔬 Toplam test: {len(STRATEJILER) * len(TUM_COINLER) * 5}")
    await update.message.reply_text("\n".join(satirlar), parse_mode='Markdown')


# ==================== ANA DÖNGÜ ====================
def otomatik_arkaplan_tarayici():
    global BOT_CALISIYOR_MU, ANALITIK, IZLEME_CALISIYOR

    print(f"🔬 [ARAŞTIRMA BOTU] Başladı", flush=True)
    print(f"📊 {len(STRATEJILER)} strateji × {len(TUM_COINLER)} coin × 5 parametre = {len(STRATEJILER)*len(TUM_COINLER)*5} test", flush=True)

    try:
        exchange.load_markets()
    except Exception:
        pass

    dongu_sayaci = 0
    son_izleme = time.time()

    while True:
        try:
            if not BOT_CALISIYOR_MU:
                time.sleep(5)
                continue

            # Günlük limit kontrolü
            gunluk = gunluk_kontrol()
            if gunluk is not None:
                if gunluk <= -RISK['gunluk_max_kayip_pct']:
                    telegram_mesaj_gonder(f"🛑 *Günlük zarar limiti!* (%{gunluk*100:.1f})")
                    BOT_CALISIYOR_MU = False
                    continue

            # Aylık izleme kontrolü (30 gün)
            if time.time() - son_izleme > 30 * 24 * 3600 and EN_IYI_5 and not IZLEME_CALISIYOR:
                IZLEME_CALISIYOR = True
                telegram_mesaj_gonder("📊 *Aylık izleme başlıyor (otomatik)...*")
                threading.Thread(target=izleme_yap, daemon=True).start()
                son_izleme = time.time()

            # Açık pozisyon kontrolü
            try:
                raw = exchange.fetch_positions()
                aktif_borsa = {p['symbol']: p for p in raw
                               if float(p.get('contracts', 0) or p.get('size', 0) or 0) > 0}
            except Exception:
                aktif_borsa = {}

            # Kapanan pozisyonları işle
            for sym in list(AKTIF_POZISYONLAR.keys()):
                if sym not in aktif_borsa:
                    AKTIF_POZISYONLAR.pop(sym, None)
                    basarili = False
                    try:
                        tum_emirleri_iptal_et(sym)
                        closed = exchange.fetch_closed_orders(sym, limit=5)
                        pnl_real = 0.0
                        if closed:
                            son = sorted(closed, key=lambda x: x['timestamp'] or 0)[-2:]
                            for o in son:
                                pnl_real += float(o.get('info', {}).get('pnl', 0) or 0)
                        basarili = pnl_real > 0
                    except Exception:
                        pass

                    if basarili:
                        ANALITIK["basarili_islem_sayisi"] = ANALITIK.get("basarili_islem_sayisi", 0) + 1
                        telegram_mesaj_gonder(f"🎉 *Kâr* → `{sym[:12]}` 🟢")
                    else:
                        ANALITIK["basarisiz_islem_sayisi"] = ANALITIK.get("basarisiz_islem_sayisi", 0) + 1
                        telegram_mesaj_gonder(f"❌ *Stop* → `{sym[:12]}` 🔴")
                    hafizayi_kaydet()

            # Yeni sinyal kontrolü (sadece aktif stratejinin coini)
            if AKTIF_STRATEJI and not ARASTIRMA_CALISIYOR:
                symbol = AKTIF_STRATEJI['symbol']
                if symbol not in aktif_borsa and len(aktif_borsa) < RISK['maks_pozisyon']:
                    sig, neden = aktif_sinyal_uret(symbol)
                    if sig:
                        pozisyon_ac(symbol, sig)

            dongu_sayaci += 1
            if dongu_sayaci % 40 == 0:
                aktif = AKTIF_STRATEJI['strateji'] if AKTIF_STRATEJI else "yok"
                print(f"🔍 #{dongu_sayaci} | Aktif: {aktif} | Poz: {len(AKTIF_POZISYONLAR)} | Araştırma: {ANALITIK.get('arastirma_sayisi', 0)}", flush=True)

        except Exception as e:
            print(f"⚠️ Döngü hatası: {e}", flush=True)

        time.sleep(15)


def flask_web_server():
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)


# ==================== BAŞLAT ====================
if __name__ == '__main__':
    threading.Thread(target=otomatik_arkaplan_tarayici, daemon=True).start()
    threading.Thread(target=flask_web_server, daemon=True).start()

    app_tg = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app_tg.add_handler(CommandHandler("durum", durum_komutu))
    app_tg.add_handler(CommandHandler("baslat", baslat_komutu))
    app_tg.add_handler(CommandHandler("durdur", durdur_komutu))
    app_tg.add_handler(CommandHandler("kapat", kapat_komutu))
    app_tg.add_handler(CommandHandler("temizle", temizle_komutu))
    app_tg.add_handler(CommandHandler("arastir", arastir_komutu))
    app_tg.add_handler(CommandHandler("izle", izle_komutu))
    app_tg.add_handler(CommandHandler("en_iyi", en_iyi_komutu))
    app_tg.add_handler(CommandHandler("aktif", aktif_strateji_komutu))
    app_tg.add_handler(CommandHandler("test", test_komutu))
    app_tg.add_handler(CommandHandler("stratejiler", strateji_listesi_komutu))

    app_tg.run_polling(drop_pending_updates=True)

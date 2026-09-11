import os
import time
import threading
import sys
import requests
import ccxt
import pandas as pd
import numpy as np
from datetime import datetime, timezone
from itertools import product
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

KOMISYON_ORANI = 0.001

# ==================== KURGU PARÇALARI ====================
KURGU_PARCALARI = {
    "yon": ["LONG_only", "LONG_SHORT"],
    "strateji": ["mean_reversion", "trend", "breakout", "momentum"],
    "gosterge": ["bollinger", "rsi", "ema", "macd"],
    "cikis": ["fixed_tp", "trailing", "partial"],
    "zaman": ["15m", "1h", "4h"],
    "coin": [
        'SOL/USDT:USDT', 'XRP/USDT:USDT',
        'BNB/USDT:USDT', 'ETH/USDT:USDT'
    ],
}

# Toplam kurgu sayısı
TOPLAM_KURGU = 1
for k, v in KURGU_PARCALARI.items():
    TOPLAM_KURGU *= len(v)

# ==================== ELEME AŞAMALARI ====================
ASAMALAR = {
    "1": {
        "aciklama": "Hızlı Eleme",
        "mum_sayisi": 300,
        "min_islem": 3,
        "min_pf": 1.0,
        "max_kurgu": 200,
    },
    "2": {
        "aciklama": "Orta Test",
        "mum_sayisi": 800,
        "min_islem": 10,
        "min_pf": 1.3,
        "min_win": 45,
        "max_kurgu": 30,
    },
    "3": {
        "aciklama": "Detaylı + Cross-Validation",
        "cv_train": 0.50,
        "cv_validate": 0.25,
        "cv_test": 0.25,
        "min_islem": 2,        # 5 → 2
        "min_pf": 1.2,         # 1.5 → 1.2
        "max_kurgu": 5,
},
}

# ==================== RİSK ====================
RISK = {
    "islem_riski_pct": 0.02,
    "kaldirac": 5,
    "maks_pozisyon": 3,
    "cooldown_dk": 15,
    "gunluk_max_kayip_pct": 0.05,
}

# ==================== DURUMLAR ====================
BOT_CALISIYOR_MU = True
KURGU_ARASTIRMA_CALISIYOR = False
GUN_BASI_KASA = None
GUN_BASI_TARIH = None
AKTIF_POZISYONLAR = {}
AKTIF_KURGU = None
KURGU_GECMISI = []
EN_IYI_KURGULAR = []
ANALITIK = {
    "basarili_islem_sayisi": 0,
    "basarisiz_islem_sayisi": 0,
    "toplam_kar": 0.0,
    "kurgu_arastirma_sayisi": 0,
}

# ==================== SUPABASE ====================
def hafizayi_yukle():
    varsayilan = {
        "aktif_pozisyonlar": {},
        "aktif_kurgu": None,
        "analitik": ANALITIK.copy(),
        "kurgu_gecmisi": [],
        "en_iyi_kurgular": [],
    }
    try:
        r = supabase.table("bot_hafiza").select("*").eq("id", 20).execute()
        if r.data:
            v = r.data[0]
            return {
                "aktif_pozisyonlar": v.get("aktif_pozisyonlar", {}),
                "aktif_kurgu": v.get("aktif_kurgu"),
                "analitik": v.get("analitik", ANALITIK.copy()),
                "kurgu_gecmisi": v.get("kurgu_gecmisi", []),
                "en_iyi_kurgular": v.get("en_iyi_kurgular", []),
            }
    except Exception:
        pass
    try:
        supabase.table("bot_hafiza").upsert({"id": 20, **varsayilan}).execute()
    except Exception:
        pass
    return varsayilan

def hafizayi_kaydet():
    try:
        supabase.table("bot_hafiza").upsert({
            "id": 20,
            "aktif_pozisyonlar": AKTIF_POZISYONLAR,
            "aktif_kurgu": AKTIF_KURGU,
            "analitik": ANALITIK,
            "kurgu_gecmisi": KURGU_GECMISI[-3:],
            "en_iyi_kurgular": EN_IYI_KURGULAR,
        }).execute()
    except Exception:
        pass

kalici = hafizayi_yukle()
AKTIF_POZISYONLAR = kalici["aktif_pozisyonlar"]
AKTIF_KURGU = kalici["aktif_kurgu"]
ANALITIK = kalici["analitik"]
KURGU_GECMISI = kalici.get("kurgu_gecmisi", [])
EN_IYI_KURGULAR = kalici.get("en_iyi_kurgular", [])

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
        except Exception:
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

def donchian_hesapla(df, period=20):
    ust = df['high'].rolling(period).max()
    alt = df['low'].rolling(period).min()
    return ust, alt

def hacim_orani(df, period=20):
    try:
        ort = df['volume'].rolling(period).mean().iloc[-1]
        son = df['volume'].iloc[-1]
        return float(son / ort) if ort > 0 else 1.0
    except Exception:
        return 1.0

# ==================== KURGU ÜRETİCİ ====================
def kurgu_uret():
    """Tüm kurgu kombinasyonlarını üret."""
    kurgular = []
    anahtarlar = list(KURGU_PARCALARI.keys())
    degerler = list(KURGU_PARCALARI.values())
    for kombinasyon in product(*degerler):
        kurgu = dict(zip(anahtarlar, kombinasyon))
        kurgular.append(kurgu)
    return kurgular

# ==================== KURGU ADI ====================
def kurgu_adi(kurgu):
    coin_kisa = kurgu['coin'].split('/')[0]
    return f"{kurgu['yon']}+{kurgu['strateji']}+{kurgu['gosterge']}+{kurgu['cikis']}+{kurgu['zaman']}({coin_kisa})"
# ==================== SİNYAL ÜRETİCİ (KURGU BAZLI) ====================
def sinyal_uret_kurgu(df, kurgu):
    """
    Kurgu bazlı sinyal üret.
    Yön, strateji, gösterge kombinasyonuna göre.
    """
    if len(df) < 60:
        return None
    
    close = df['close']
    fiyat = close.iloc[-1]
    open_ = df['open'].iloc[-1]
    
    # ===== GÖSTERGE HESAPLARI =====
    sma, ust_bb, alt_bb = bollinger_hesapla(close, 20, 2.0)
    rsi = rsi_hesapla(close, 14)
    ema9 = ema_hesapla(close, 9)
    ema21 = ema_hesapla(close, 21)
    ema50 = ema_hesapla(close, 50)
    macd, macd_sig, macd_hist = macd_hesapla(close)
    
    son_ust_bb = ust_bb.iloc[-1]
    son_alt_bb = alt_bb.iloc[-1]
    son_rsi = rsi.iloc[-1]
    son_ema9 = ema9.iloc[-1]
    son_ema21 = ema21.iloc[-1]
    son_ema50 = ema50.iloc[-1]
    son_macd = macd.iloc[-1]
    son_macd_sig = macd_sig.iloc[-1]
    
    if pd.isna(son_ust_bb) or pd.isna(son_rsi) or pd.isna(son_ema50):
        return None
    
    # ATR kontrolü (volatilite)
    atr = atr_hesapla(df, 14).iloc[-1]
    if pd.isna(atr) or atr == 0:
        return None
    atr_pct = (atr / fiyat) * 100
    if not (0.3 <= atr_pct <= 6.0):
        return None
    
    # Hacim kontrolü
    h_orani = hacim_orani(df, 20)
    if h_orani < 0.5:
        return None
    
    # ===== YÖN BELİRLEME =====
    yon_izinli = kurgu['yon']
    
    # ===== GÖSTERGE SİNYALİ =====
    gosterge = kurgu['gosterge']
    long_sinyal = False
    short_sinyal = False
    
    if gosterge == "bollinger":
        if fiyat <= son_alt_bb and fiyat > open_:
            long_sinyal = True
        if fiyat >= son_ust_bb and fiyat < open_:
            short_sinyal = True
    
    elif gosterge == "rsi":
        if son_rsi < 32 and fiyat > open_:
            long_sinyal = True
        if son_rsi > 68 and fiyat < open_:
            short_sinyal = True
    
    elif gosterge == "ema":
        ema9_p = ema9.iloc[-2]
        ema21_p = ema21.iloc[-2]
        if ema9_p < ema21_p and son_ema9 > son_ema21 and fiyat > son_ema50:
            long_sinyal = True
        if ema9_p > ema21_p and son_ema9 < son_ema21 and fiyat < son_ema50:
            short_sinyal = True
    
    elif gosterge == "macd":
        macd_p = macd.iloc[-2]
        macd_sig_p = macd_sig.iloc[-2]
        if macd_p < macd_sig_p and son_macd > son_macd_sig and son_macd < 0:
            long_sinyal = True
        if macd_p > macd_sig_p and son_macd < son_macd_sig and son_macd > 0:
            short_sinyal = True
    
    # ===== STRATEJİ FİLTRESİ =====
    strateji = kurgu['strateji']
    
    if strateji == "mean_reversion":
        # Fiyat bandın dışında mı?
        if gosterge == "bollinger":
            pass  # Zaten bant dışı kontrolü var
        else:
            # Ek filtre: fiyat SMA20'ye yakın olmalı
            son_sma = sma.iloc[-1]
            if pd.isna(son_sma):
                return None
            uzaklik = abs(fiyat - son_sma) / son_sma
            if uzaklik < 0.005:
                return None
    
    elif strateji == "trend":
        # EMA hizalama kontrolü
        if long_sinyal:
            if not (son_ema9 > son_ema21 > son_ema50):
                return None
        if short_sinyal:
            if not (son_ema9 < son_ema21 < son_ema50):
                return None
    
    elif strateji == "breakout":
        # Donchian kırılımı
        ust_d, alt_d = donchian_hesapla(df, 48)
        son_ust_d = ust_d.iloc[-1]
        son_alt_d = alt_d.iloc[-1]
        if pd.isna(son_ust_d) or pd.isna(son_alt_d):
            return None
        # Sinyali kırılım ile teyit et
        if long_sinyal and fiyat < son_ust_d * 0.99:
            return None
        if short_sinyal and fiyat > son_alt_d * 1.01:
            return None
    
    elif strateji == "momentum":
        # ADX trend gücü
        adx = adx_hesapla(df, 14).iloc[-1]
        if pd.isna(adx) or adx < 20:
            return None
    
    # ===== YÖN KONTROLÜ =====
    if long_sinyal and yon_izinli not in ["LONG_only", "LONG_SHORT"]:
        long_sinyal = False
    if short_sinyal and yon_izinli != "LONG_SHORT":
        short_sinyal = False
    
    # ===== SİNYAL =====
    if long_sinyal:
        return {"yon": "LONG", "giris": float(fiyat), "atr": float(atr), "atr_pct": atr_pct}
    if short_sinyal:
        return {"yon": "SHORT", "giris": float(fiyat), "atr": float(atr), "atr_pct": atr_pct}
    
    return None


# ==================== ÇIKIŞ HESAPLAMA ====================
def cikis_hesapla(sinyal, kurgu):
    """
    Kurgu bazlı çıkış planı.
    - fixed_tp: Sabit stop + TP
    - trailing: Trailing stop
    - partial: Kısmi TP
    """
    yon = sinyal['yon']
    fiyat = sinyal['giris']
    atr_pct = sinyal['atr_pct']
    
    cikis = kurgu['cikis']
    
    # Base stop
    stop_pct = max(0.008, min(0.030, (atr_pct * 1.5) / 100.0))
    
    if cikis == "fixed_tp":
        tp_pct = stop_pct * 2.0
        plan = {"stop_pct": stop_pct, "tp_pct": tp_pct, "trailing": False,
                "partial": False, "partial_tp_pct": 0, "partial_oran": 0}
    
    elif cikis == "trailing":
        tp_pct = stop_pct * 3.0
        plan = {"stop_pct": stop_pct, "tp_pct": tp_pct, "trailing": True,
                "trailing_atr": 2.0, "partial": False,
                "partial_tp_pct": 0, "partial_oran": 0}
    
    elif cikis == "partial":
        tp_pct = stop_pct * 2.0
        plan = {"stop_pct": stop_pct, "tp_pct": tp_pct, "trailing": False,
                "partial": True, "partial_tp_pct": stop_pct, "partial_oran": 0.5}
    
    else:
        plan = {"stop_pct": stop_pct, "tp_pct": stop_pct * 2.0, "trailing": False,
                "partial": False, "partial_tp_pct": 0, "partial_oran": 0}
    
    return plan


# ==================== BACKTEST MOTORU ====================
def backtest_kurgu(df, kurgu, hizli=False):
    """
    Tek kurguyu backtest et.
    hizli=True ise trailing/partial simüle edilmez (hız için).
    """
    try:
        trades = []
        poz = None
        
        for i in range(60, len(df)):
            bar = df.iloc[i]
            high = bar['high']; low = bar['low']; close = bar['close']
            
            # Açık pozisyon yönetimi
            if poz is not None:
                # Trailing güncelle
                if poz.get('trailing', False):
                    atr = poz.get('atr', 0)
                    if poz['yon'] == 'LONG':
                        yeni_stop = high - atr * poz.get('trailing_atr', 2.0)
                        if yeni_stop > poz['stop']:
                            poz['stop'] = yeni_stop
                    else:
                        yeni_stop = low + atr * poz.get('trailing_atr', 2.0)
                        if yeni_stop < poz['stop']:
                            poz['stop'] = yeni_stop
                
                # Kısmi TP kontrolü
                if poz.get('partial', False) and not poz.get('partial_done', False):
                    ptp = poz.get('partial_tp_pct', 0)
                    if poz['yon'] == 'LONG' and high >= poz['giris'] * (1 + ptp):
                        poz['partial_done'] = True
                    elif poz['yon'] == 'SHORT' and low <= poz['giris'] * (1 - ptp):
                        poz['partial_done'] = True
                
                # Çıkış kontrolü
                if poz['yon'] == 'LONG':
                    if low <= poz['stop']:
                        trades.append({**poz, 'cikis': poz['stop'], 'sebep': 'SL'})
                        poz = None
                    elif high >= poz['tp']:
                        trades.append({**poz, 'cikis': poz['tp'], 'sebep': 'TP'})
                        poz = None
                else:
                    if high >= poz['stop']:
                        trades.append({**poz, 'cikis': poz['stop'], 'sebep': 'SL'})
                        poz = None
                    elif low <= poz['tp']:
                        trades.append({**poz, 'cikis': poz['tp'], 'sebep': 'TP'})
                        poz = None
                continue
            
            # Yeni sinyal
            df_slice = df.iloc[max(0, i-100):i+1].reset_index(drop=True)
            try:
                sig = sinyal_uret_kurgu(df_slice, kurgu)
            except Exception:
                continue
            
            if sig:
                plan = cikis_hesapla(sig, kurgu)
                fiyat = sig['giris']
                yon = sig['yon']
                
                if yon == 'LONG':
                    stop = fiyat * (1 - plan['stop_pct'])
                    tp = fiyat * (1 + plan['tp_pct'])
                else:
                    stop = fiyat * (1 + plan['stop_pct'])
                    tp = fiyat * (1 - plan['tp_pct'])
                
                poz = {
                    "yon": yon, "giris": float(fiyat),
                    "stop": float(stop), "tp": float(tp),
                    "atr": sig['atr'],
                    "trailing": plan.get('trailing', False),
                    "trailing_atr": plan.get('trailing_atr', 2.0),
                    "partial": plan.get('partial', False),
                    "partial_tp_pct": plan.get('partial_tp_pct', 0),
                    "partial_oran": plan.get('partial_oran', 0),
                    "partial_done": False,
                }
        
        if not trades:
            return None
        
        # Kâr hesapla
        kazanclar = []
        for t in trades:
            if t['yon'] == 'LONG':
                pct = (t['cikis'] - t['giris']) / t['giris']
            else:
                pct = (t['giris'] - t['cikis']) / t['giris']
            
            # Kısmi TP
            if t.get('partial_done', False) and t.get('partial', False):
                ptp = t.get('partial_tp_pct', 0)
                poran = t.get('partial_oran', 0.5)
                pct = (ptp * poran) + (pct * (1 - poran))
            
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
def cross_validate_kurgu(df, kurgu, cv_train=0.50, cv_val=0.25, cv_test=0.25):
    """3 aşamalı Cross-Validation."""
    try:
        n = len(df)
        if n < 300:
            return None
        train_end = int(n * cv_train)
        val_end = int(n * (cv_train + cv_val))
        
        df_train = df.iloc[:train_end].reset_index(drop=True)
        df_val = df.iloc[train_end:val_end].reset_index(drop=True)
        df_test = df.iloc[val_end:].reset_index(drop=True)
        
        if len(df_train) < 100 or len(df_val) < 50 or len(df_test) < 50:
            return None
        
        r_train = backtest_kurgu(df_train, kurgu)
        r_val = backtest_kurgu(df_val, kurgu)
        r_test = backtest_kurgu(df_test, kurgu)
        
        if not r_train or not r_val or not r_test:
            return None
        
        min_i = ASAMALAR['3']['min_islem']
if r_train['islem'] < min_i or r_val['islem'] < 1 or r_test['islem'] < 1:
    return None

min_pf = ASAMALAR['3']['min_pf']
if r_train['pf'] < min_pf:  # Sadece Train'de PF kontrolü
    return None
# Val ve Test'te PF kontrolü YOK — sadece işlem olsun
        
        ort_pf = (r_train['pf'] * 0.5 + r_val['pf'] * 0.25 + r_test['pf'] * 0.25)
        ort_win = (r_train['win_rate'] * 0.5 + r_val['win_rate'] * 0.25 + r_test['win_rate'] * 0.25)
        ort_toplam = (r_train['toplam'] * 0.5 + r_val['toplam'] * 0.25 + r_test['toplam'] * 0.25)
        en_kotu_dd = min(r_train['max_dd'], r_val['max_dd'], r_test['max_dd'])
        
        return {
            "ort_pf": round(ort_pf, 3),
            "ort_win": round(ort_win, 2),
            "ort_toplam": round(ort_toplam, 2),
            "en_kotu_dd": round(en_kotu_dd, 2),
            "train_pf": r_train['pf'],
            "val_pf": r_val['pf'],
            "test_pf": r_test['pf'],
            "train_islem": r_train['islem'],
            "val_islem": r_val['islem'],
            "test_islem": r_test['islem'],
            "toplam_islem": r_train['islem'] + r_val['islem'] + r_test['islem'],
        }
    except Exception:
        return None
    # ==================== KADEMELİ ELEME MOTORU ====================
def kurgu_arastirma_yap():
    """
    3 aşamalı kademeli eleme:
    Aşama 1: 1.152 kurgu × 300 mum → ~200 kurgu
    Aşama 2: 200 kurgu × 800 mum → ~30 kurgu
    Aşama 3: 30 kurgu × CV → en iyi 5
    """
    global KURGU_ARASTIRMA_CALISIYOR, EN_IYI_KURGULAR, AKTIF_KURGU, KURGU_GECMISI

    tum_kurgular = kurgu_uret()
    telegram_mesaj_gonder(
        f"🔬 *KURGU ARAŞTIRMASI BAŞLADI*\n"
        f"Toplam kurgu: `{len(tum_kurgular)}`\n"
        f"Parçalar: yön(2) × strateji(4) × gösterge(4) × çıkış(3) × zaman(3) × coin(4)\n"
        f"3 Aşamalı Kademeli Eleme\n"
        f"Tahmini süre: 7-8 saat"
    )

    # Veri cache (her coin + timeframe için bir kez)
    veri_cache = {}

    def veri_al(symbol, timeframe, limit):
        anahtar = f"{symbol}_{timeframe}"
        if anahtar not in veri_cache or len(veri_cache[anahtar]) < limit:
            ohlcv = fetch_ohlcv_guvenli(symbol, timeframe, limit=1000)
            if ohlcv is None:
                return None
            df = pd.DataFrame(ohlcv, columns=['timestamp','open','high','low','close','volume'])
            veri_cache[anahtar] = df
        df = veri_cache[anahtar]
        return df.tail(limit).reset_index(drop=True) if len(df) >= limit else df

    # ===== AŞAMA 1: HIZLI ELEME =====
    telegram_mesaj_gonder("⚡ *AŞAMA 1/3: Hızlı Eleme* (300 mum, 1.152 kurgu)")
    
    asama1 = ASAMALAR['1']
    gecen1 = []
    sayac = 0
    
    for kurgu in tum_kurgular:
        sayac += 1
        if sayac % 50 == 0:
            print(f"⚡ Aşama 1: {sayac}/{len(tum_kurgular)} | Geçen: {len(gecen1)}", flush=True)
        
        df = veri_al(kurgu['coin'], kurgu['zaman'], asama1['mum_sayisi'])
        if df is None or len(df) < 100:
            continue
        
        r = backtest_kurgu(df, kurgu, hizli=True)
        if not r:
            continue
        if r['islem'] < asama1['min_islem']:
            continue
        if r['pf'] < asama1['min_pf']:
            continue
        
        gecen1.append({"kurgu": kurgu, "sonuc": r})
    
    gecen1.sort(key=lambda x: x['sonuc']['pf'], reverse=True)
    gecen1 = gecen1[:asama1['max_kurgu']]
    
    telegram_mesaj_gonder(f"✅ Aşama 1 tamamlandı\nGeçen: `{len(gecen1)}` kurgu")
    
    if not gecen1:
        telegram_mesaj_gonder("❌ Aşama 1'de hiçbir kurgu geçemedi. Filtreler gevşetilmeli.")
        KURGU_ARASTIRMA_CALISIYOR = False
        return

    # ===== AŞAMA 2: ORTA TEST =====
    telegram_mesaj_gonder(f"📊 *AŞAMA 2/3: Orta Test* ({len(gecen1)} kurgu, 800 mum)")
    
    asama2 = ASAMALAR['2']
    gecen2 = []
    
    for i, item in enumerate(gecen1, 1):
        kurgu = item['kurgu']
        if i % 20 == 0:
            print(f"📊 Aşama 2: {i}/{len(gecen1)} | Geçen: {len(gecen2)}", flush=True)
        
        df = veri_al(kurgu['coin'], kurgu['zaman'], asama2['mum_sayisi'])
        if df is None or len(df) < 200:
            continue
        
        r = backtest_kurgu(df, kurgu)
        if not r:
            continue
        if r['islem'] < asama2['min_islem']:
            continue
        if r['pf'] < asama2['min_pf']:
            continue
        if r['win_rate'] < asama2.get('min_win', 0):
            continue
        
        gecen2.append({"kurgu": kurgu, "sonuc": r})
    
    gecen2.sort(key=lambda x: x['sonuc']['pf'], reverse=True)
    gecen2 = gecen2[:asama2['max_kurgu']]
    
    telegram_mesaj_gonder(f"✅ Aşama 2 tamamlandı\nGeçen: `{len(gecen2)}` kurgu")
    
    if not gecen2:
        telegram_mesaj_gonder("❌ Aşama 2'de hiçbir kurgu geçemedi.")
        KURGU_ARASTIRMA_CALISIYOR = False
        return

    # ===== AŞAMA 3: DETAYLI + CV =====
    telegram_mesaj_gonder(f"🔬 *AŞAMA 3/3: Detaylı + Cross-Validation* ({len(gecen2)} kurgu)")
    
    asama3 = ASAMALAR['3']
    final = []
    
    for i, item in enumerate(gecen2, 1):
        kurgu = item['kurgu']
        if i % 5 == 0:
            print(f"🔬 Aşama 3: {i}/{len(gecen2)}", flush=True)
        
        df = veri_al(kurgu['coin'], kurgu['zaman'], 1000)
        if df is None or len(df) < 300:
            continue
        
        cv_sonuc = cross_validate_kurgu(
            df, kurgu,
            cv_train=asama3['cv_train'],
            cv_val=asama3['cv_validate'],
            cv_test=asama3['cv_test']
        )
        
        if not cv_sonuc:
            continue
        
        final.append({
            "kurgu": kurgu,
            "kurgu_adi": kurgu_adi(kurgu),
            "cv": cv_sonuc,
        })
    
    final.sort(key=lambda x: x['cv']['ort_pf'], reverse=True)
    final = final[:asama3['max_kurgu']]
    
    telegram_mesaj_gonder(f"✅ Aşama 3 tamamlandı\nFinal: `{len(final)}` kurgu")

    # ===== SONUÇ =====
    if not final:
        telegram_mesaj_gonder("❌ Hiçbir kurgu 3 aşamayı geçemedi.")
        KURGU_ARASTIRMA_CALISIYOR = False
        return

    EN_IYI_KURGULAR = final
    AKTIF_KURGU = final[0]
    
    KURGU_GECMISI.append({
        "tarih": datetime.now(timezone.utc).isoformat(),
        "toplam_test": len(tum_kurgular),
        "asama1": len(gecen1),
        "asama2": len(gecen2),
        "final": len(final),
        "en_iyi_5": final,
    })
    ANALITIK["kurgu_arastirma_sayisi"] = ANALITIK.get("kurgu_arastirma_sayisi", 0) + 1
    
    hafizayi_kaydet()
    
    # Telegram raporu
    rapor = "🏆 *KURGU ARAŞTIRMASI TAMAMLANDI*\n\n"
    rapor += f"🔬 Test edilen: `{len(tum_kurgular)}`\n"
    rapor += f"⚡ Aşama 1 geçen: `{len(gecen1)}`\n"
    rapor += f"📊 Aşama 2 geçen: `{len(gecen2)}`\n"
    rapor += f"🏅 Final: `{len(final)}`\n\n"
    rapor += "*🏆 EN İYİ 5 KURGU:*\n\n"
    
    for i, f in enumerate(final, 1):
        cv = f['cv']
        rapor += (
            f"*{i}. {f['kurgu_adi']}*\n"
            f"  PF: `{cv['ort_pf']}` | Win: `{cv['ort_win']}%`\n"
            f"  Getiri: `{cv['ort_toplam']}%` | İşl: `{cv['toplam_islem']}`\n"
            f"  DD: `{cv['en_kotu_dd']}%`\n"
            f"  (Train PF: `{cv['train_pf']}` | Val: `{cv['val_pf']}` | Test: `{cv['test_pf']}`)\n\n"
        )
    
    rapor += f"🎯 *Aktif edildi:* `{AKTIF_KURGU['kurgu_adi']}`"
    
    telegram_mesaj_gonder(rapor)
    KURGU_ARASTIRMA_CALISIYOR = False


# ==================== AKTİF KURGU SİNYAL ÜRETİMİ ====================
def aktif_kurgu_sinyal(symbol):
    """Aktif kurgunun coininden sinyal üret."""
    if not AKTIF_KURGU:
        return None, "aktif kurgu yok"
    
    kurgu = AKTIF_KURGU['kurgu']
    if kurgu['coin'] != symbol:
        return None, "bu coin aktif kurguda değil"
    
    try:
        df = fetch_ohlcv_guvenli(symbol, kurgu['zaman'], limit=200)
        if df is None or len(df) < 100:
            return None, "veri yetersiz"
        df_pd = pd.DataFrame(df, columns=['timestamp','open','high','low','close','volume'])
        sig = sinyal_uret_kurgu(df_pd, kurgu)
        if sig:
            plan = cikis_hesapla(sig, kurgu)
            return {**sig, **plan, "kurgu": kurgu}, "OK"
        return None, "sinyal yok"
    except Exception as e:
        return None, str(e)[:30]


# ==================== POZİSYON AÇMA ====================
def pozisyon_ac(symbol, sinyal):
    """Aktif kurgu ile pozisyon aç."""
    try:
        bal = exchange.fetch_balance()
        kasa = float(bal['total'].get('USDT', 0))
        if kasa < 10:
            return False
        if not set_leverage_and_margin_safely(symbol, RISK['kaldirac']):
            return False

        risk_usdt = kasa * RISK['islem_riski_pct']
        stop_pct = sinyal['stop_pct']
        poz_degeri = risk_usdt / stop_pct

        try:
            market_info = exchange.market(symbol)
            cs = float(market_info.get('contractSize', 1.0))
            ham = poz_degeri / (sinyal['giris'] * cs)
            miktar = float(exchange.amount_to_precision(symbol, max(ham, 0.001)))
            if miktar <= 0:
                return False
        except Exception:
            return False

        tum_emirleri_iptal_et(symbol)
        emir = exchange.create_order(symbol, 'market',
            'buy' if sinyal['yon'] == 'LONG' else 'sell', miktar)
        giris = float(emir.get('average') or emir.get('price') or sinyal['giris'])
        time.sleep(0.5)

        yon = sinyal['yon']
        tp_pct = sinyal['tp_pct']
        
        if yon == 'LONG':
            stop = giris * (1 - stop_pct)
            tp = giris * (1 + tp_pct)
            kapat_yon = 'sell'
        else:
            stop = giris * (1 + stop_pct)
            tp = giris * (1 - tp_pct)
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
            "yon": yon, "giris": giris, "stop": stop, "tp": tp,
            "miktar": miktar, "kurgu": AKTIF_KURGU['kurgu_adi'],
            "giris_zaman": int(time.time()*1000)
        }
        hafizayi_kaydet()

        telegram_mesaj_gonder(
            f"🎯 *İŞLEM AÇILDI*\n"
            f"📌 `{symbol[:12]}` | *{yon}*\n"
            f"🏗️ Kurgu: `{AKTIF_KURGU['kurgu_adi']}`\n"
            f"💰 Giriş: `{giris}` | SL: `{stop}` | TP: `{tp}`"
        )
        return True
    except Exception as e:
        print(f"❌ Pozisyon açma: {e}", flush=True)
        return False


# ==================== POZİSYON YÖNETİMİ (TRAILING) ====================
def pozisyon_yonet(symbol):
    """Trailing stop güncelle."""
    if symbol not in AKTIF_POZISYONLAR:
        return
    poz = AKTIF_POZISYONLAR[symbol]
    if not AKTIF_KURGU:
        return
    kurgu = AKTIF_KURGU['kurgu']
    if kurgu['cikis'] != "trailing":
        return
    
    try:
        fiyat = exchange.fetch_ticker(symbol)['last']
        atr = poz.get('atr', 0)
        if atr <= 0:
            return
        
        if poz['yon'] == 'LONG':
            yeni_stop = fiyat - atr * 2.0
            if yeni_stop > poz['stop']:
                poz['stop'] = yeni_stop
                AKTIF_POZISYONLAR[symbol] = poz
        else:
            yeni_stop = fiyat + atr * 2.0
            if yeni_stop < poz['stop']:
                poz['stop'] = yeni_stop
                AKTIF_POZISYONLAR[symbol] = poz
    except Exception:
        pass
# ==================== FLASK ====================
@app.route('/')
def home():
    aktif = AKTIF_KURGU['kurgu_adi'] if AKTIF_KURGU else "yok"
    return f"Kurgu Botu | Aktif: {aktif} | Poz: {len(AKTIF_POZISYONLAR)}"


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

        aktif_detay = "❌ Aktif kurgu yok"
        if AKTIF_KURGU:
            cv = AKTIF_KURGU['cv']
            aktif_detay = (
                f"🏗️ *{AKTIF_KURGU['kurgu_adi']}*\n"
                f"  PF: `{cv['ort_pf']}` | Win: `{cv['ort_win']}%`\n"
                f"  Getiri: `{cv['ort_toplam']}%`"
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
            f"🏗️ *KURGU ÜRETİCİ BOTU*\n\n"
            f"💰 Toplam: `{total:.2f}` USDT (Serbest: `{free:.2f}`)\n"
            f"📈 PnL: `{pnl:+.2f}` USDT\n"
            f"📌 Pozisyon: `{len(poslari)}/{RISK['maks_pozisyon']}`\n\n"
            f"🎯 {aktif_detay}\n\n"
            f"📚 Araştırma: `{ANALITIK.get('kurgu_arastirma_sayisi', 0)}`\n"
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


async def kurgu_arastir_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global KURGU_ARASTIRMA_CALISIYOR
    if KURGU_ARASTIRMA_CALISIYOR:
        await update.message.reply_text("⏳ Zaten araştırma çalışıyor.")
        return
    KURGU_ARASTIRMA_CALISIYOR = True
    await update.message.reply_text(
        f"🏗️ *KURGU ARAŞTIRMASI BAŞLADI*\n\n"
        f"Toplam kurgu: `{TOPLAM_KURGU}`\n"
        f"3 Aşamalı Eleme:\n"
        f"  ⚡ Aşama 1: Hızlı (300 mum)\n"
        f"  📊 Aşama 2: Orta (800 mum)\n"
        f"  🔬 Aşama 3: CV (1000 mum)\n\n"
        f"Tahmini süre: 7-8 saat\n"
        f"Sabırlı ol, sonuç gelince atacağım.",
        parse_mode='Markdown'
    )
    threading.Thread(target=kurgu_arastirma_yap, daemon=True).start()


async def en_iyi_kurgu_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not EN_IYI_KURGULAR:
        await update.message.reply_text("Henüz araştırma yok. `/kurgu_arastir` yaz.", parse_mode='Markdown')
        return
    satirlar = ["🏆 *EN İYİ KURGULAR:*\n"]
    for i, f in enumerate(EN_IYI_KURGULAR, 1):
        cv = f['cv']
        satirlar.append(
            f"*{i}. {f['kurgu_adi']}*\n"
            f"  PF: `{cv['ort_pf']}` | Win: `{cv['ort_win']}%` | İşl: `{cv['toplam_islem']}`\n"
            f"  Getiri: `{cv['ort_toplam']}%` | DD: `{cv['en_kotu_dd']}%`\n"
        )
    await update.message.reply_text("\n".join(satirlar), parse_mode='Markdown')


async def aktif_kurgu_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not AKTIF_KURGU:
        await update.message.reply_text("Aktif kurgu yok. `/kurgu_arastir` yaz.", parse_mode='Markdown')
        return
    f = AKTIF_KURGU
    cv = f['cv']
    kurgu = f['kurgu']
    mesaj = (
        f"🎯 *AKTİF KURGU*\n\n"
        f"🏗️ `{f['kurgu_adi']}`\n\n"
        f"*Kurgu Parçaları:*\n"
        f"• Yön: `{kurgu['yon']}`\n"
        f"• Strateji: `{kurgu['strateji']}`\n"
        f"• Gösterge: `{kurgu['gosterge']}`\n"
        f"• Çıkış: `{kurgu['cikis']}`\n"
        f"• Zaman: `{kurgu['zaman']}`\n"
        f"• Coin: `{kurgu['coin']}`\n\n"
        f"*Cross-Validation Sonuçları:*\n"
        f"• Train PF: `{cv['train_pf']}` ({cv['train_islem']} işl)\n"
        f"• Val PF: `{cv['val_pf']}` ({cv['val_islem']} işl)\n"
        f"• Test PF: `{cv['test_pf']}` ({cv['test_islem']} işl)\n\n"
        f"📈 Ort PF: `{cv['ort_pf']}`\n"
        f"🎯 Ort Getiri: `{cv['ort_toplam']}%`\n"
        f"📉 En Kötü DD: `{cv['en_kotu_dd']}%`"
    )
    await update.message.reply_text(mesaj, parse_mode='Markdown')


async def temizle_komutu(update, context):
    global EN_IYI_KURGULAR, AKTIF_KURGU, KURGU_GECMISI
    EN_IYI_KURGULAR = []
    AKTIF_KURGU = None
    KURGU_GECMISI = []
    hafizayi_kaydet()
    await update.message.reply_text("🗑️ *Temizlendi.*", parse_mode='Markdown')


async def parcalari_goster_komutu(update, context):
    satirlar = ["🧩 *KURGU PARÇALARI:*\n"]
    for k, v in KURGU_PARCALARI.items():
        satirlar.append(f"• {k}: `{len(v)}` → {', '.join(str(x).split('/')[0] for x in v)}")
    satirlar.append(f"\n📊 *Toplam Kurgu:* `{TOPLAM_KURGU}`")
    satirlar.append(f"⏱️ *Süre:* ~7-8 saat")
    await update.message.reply_text("\n".join(satirlar), parse_mode='Markdown')


# ==================== ANA DÖNGÜ ====================
def otomatik_arkaplan_tarayici():
    global BOT_CALISIYOR_MU, ANALITIK

    print(f"🏗️ [KURGU ÜRETİCİ] Başladı", flush=True)
    print(f"📊 Toplam kurgu: {TOPLAM_KURGU}", flush=True)

    try:
        exchange.load_markets()
    except Exception:
        pass

    dongu_sayaci = 0
    while True:
        try:
            if not BOT_CALISIYOR_MU:
                time.sleep(5)
                continue

            # Araştırma çalışıyorsa bekle
            if KURGU_ARASTIRMA_CALISIYOR:
                time.sleep(30)
                continue

            # Günlük limit kontrolü
            gunluk = gunluk_kontrol()
            if gunluk is not None:
                if gunluk <= -RISK['gunluk_max_kayip_pct']:
                    telegram_mesaj_gonder(f"🛑 *Günlük zarar limiti!* (%{gunluk*100:.1f})")
                    BOT_CALISIYOR_MU = False
                    continue

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

            # Trailing stop güncelle
            for sym in list(AKTIF_POZISYONLAR.keys()):
                if sym in aktif_borsa:
                    pozisyon_yonet(sym)

            # Yeni sinyal kontrolü
            if AKTIF_KURGU and not KURGU_ARASTIRMA_CALISIYOR:
                symbol = AKTIF_KURGU['kurgu']['coin']
                if symbol not in aktif_borsa and len(aktif_borsa) < RISK['maks_pozisyon']:
                    sig, neden = aktif_kurgu_sinyal(symbol)
                    if sig:
                        pozisyon_ac(symbol, sig)

            dongu_sayaci += 1
            if dongu_sayaci % 40 == 0:
                aktif = AKTIF_KURGU['kurgu_adi'] if AKTIF_KURGU else "yok"
                print(f"🔍 #{dongu_sayaci} | Aktif: {aktif} | Poz: {len(AKTIF_POZISYONLAR)}", flush=True)

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
    app_tg.add_handler(CommandHandler("kurgu_arastir", kurgu_arastir_komutu))
    app_tg.add_handler(CommandHandler("en_iyi_kurgu", en_iyi_kurgu_komutu))
    app_tg.add_handler(CommandHandler("aktif_kurgu", aktif_kurgu_komutu))
    app_tg.add_handler(CommandHandler("parcalar", parcalari_goster_komutu))

    app_tg.run_polling(drop_pending_updates=True)

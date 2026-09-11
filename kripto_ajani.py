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
    'timeout': 15000,
    'options': {'defaultType': 'swap'}
})
exchange.set_sandbox_mode(True)

TAKIP_EDILENLER = [
    'ETH/USDT:USDT', 'SOL/USDT:USDT', 'XRP/USDT:USDT',
    'ADA/USDT:USDT', 'DOGE/USDT:USDT', 'AVAX/USDT:USDT'
]

KOMISYON_ORANI = 0.001

# ==================== MOD TANIMLARI ====================
MODLAR = {
    "puanli": {
        "aciklama": "🎯 Puanlı Sistem - 5 faktör, 1h+15m, optimize edilebilir",
        "zaman_ana": "1h",
        "zaman_sinyal": "15m",
        "puan_esigi": 75,
        "puan_trend": 20,
        "puan_rsi": 15,
        "puan_adx": 15,
        "puan_derinlik": 10,
        "rsi_min": 40, "rsi_max": 60,
        "adx_esik": 25,
        "tp_sabit_pct": 0.015,
        "atr_stop_mult": 1.5,
        "islem_riski_pct": 0.03,
        "kaldirac": 10,
        "maks_pozisyon": 3,
        "cooldown_dk": 10,
        "gunluk_max_kayip_pct": 0.10,
        "min_stop_pct": 0.008,
        "max_stop_pct": 0.035,
    },
}

BOT_CALISIYOR_MU = True
GUN_BASI_KASA = None
GUN_BASI_TARIH = None
BACKTEST_CALISIYOR = False
OPTIMIZE_CALISIYOR = False
AKTIF_MOD = "puanli"

# ==================== SUPABASE ====================
def hafizayi_yukle():
    varsayilan = {
        "aktif_sistemler": {},
        "analitik": {"basarili_islem_sayisi": 0, "basarisiz_islem_sayisi": 0},
        "cooldownlar": {}, "coin_params": {}, "aktif_mod": "puanli"
    }
    try:
        r = supabase.table("bot_hafiza").select("*").eq("id", 1).execute()
        if r.data:
            v = r.data[0]
            return {
                "aktif_sistemler": v.get("aktif_sistemler", {}),
                "analitik": v.get("analitik", varsayilan["analitik"]),
                "cooldownlar": v.get("cooldownlar", {}),
                "coin_params": v.get("coin_params", {}),
                "aktif_mod": v.get("aktif_mod", "puanli")
            }
    except Exception:
        pass
    try:
        supabase.table("bot_hafiza").upsert({"id": 1, **varsayilan}).execute()
    except Exception:
        pass
    return varsayilan

def hafizayi_kaydet():
    try:
        supabase.table("bot_hafiza").upsert({
            "id": 1,
            "aktif_sistemler": AKTIF_GRID_SISTEMLERI,
            "analitik": ANALITIK_HAFIZA,
            "cooldownlar": COIN_COOLDOWNLAR,
            "coin_params": COIN_PARAMS,
            "aktif_mod": AKTIF_MOD
        }).execute()
    except Exception:
        pass

kalici = hafizayi_yukle()
AKTIF_GRID_SISTEMLERI = kalici["aktif_sistemler"]
ANALITIK_HAFIZA = kalici["analitik"]
COIN_COOLDOWNLAR = kalici["cooldownlar"]
COIN_PARAMS = {k: v for k, v in kalici.get("coin_params", {}).items() if k in TAKIP_EDILENLER}
AKTIF_MOD = kalici.get("aktif_mod", "puanli")

def mod_al():
    return MODLAR.get(AKTIF_MOD, MODLAR["puanli"])

def coin_parametre_al(symbol):
    base = dict(mod_al())
    if symbol in COIN_PARAMS:
        base.update(COIN_PARAMS[symbol])
    return base

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

def gunluk_kayip_kontrol(mod):
    global GUN_BASI_KASA, GUN_BASI_TARIH
    bugun = datetime.now(timezone.utc).date()
    if GUN_BASI_TARIH != bugun:
        try:
            bal = exchange.fetch_balance()
            GUN_BASI_KASA = float(bal['total'].get('USDT', 0))
            GUN_BASI_TARIH = bugun
        except Exception:
            return False
    if not GUN_BASI_KASA or GUN_BASI_KASA <= 0:
        return False
    try:
        bal = exchange.fetch_balance()
        su_an = float(bal['total'].get('USDT', 0))
        kayip_pct = (GUN_BASI_KASA - su_an) / GUN_BASI_KASA
        return kayip_pct >= mod['gunluk_max_kayip_pct']
    except Exception:
        return False

# ==================== GÖSTERGELER ====================
def rsi_hesapla(close, period=14):
    delta = close.diff()
    gain = delta.where(delta > 0, 0).rolling(period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))

def ema_hesapla(close, period):
    return close.ewm(span=period, adjust=False).mean()

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
        val = dx.rolling(period).mean().iloc[-1]
        return float(val) if not pd.isna(val) else 0.0
    except Exception:
        return 0.0

def atr_yuzdesi(df, period=14):
    try:
        atr = atr_hesapla(df, period).iloc[-1]
        fiyat = df['close'].iloc[-1]
        return float((atr / fiyat) * 100)
    except Exception:
        return 1.5

# ==================== EMİR DEFTERİ ====================
def emir_defteri_derinlik_analizi(symbol):
    try:
        order_book = exchange.fetch_order_book(symbol, limit=20)
        bids = order_book.get('bids', [])
        asks = order_book.get('asks', [])
        toplam_bid = sum(b[1] for b in bids)
        toplam_ask = sum(a[1] for a in asks)
        if toplam_bid + toplam_ask == 0:
            return "DENGELI"
        bid_orani = toplam_bid / (toplam_bid + toplam_ask)
        if bid_orani > 0.55:
            return "ALICI_BASKIN"
        elif bid_orani < 0.45:
            return "SATICI_BASKIN"
        return "DENGELI"
    except Exception:
        return "DENGELI"

# ==================== PUANLI SİNYAL ====================
def sinyal_uret_puanli(df_15m, df_1h, symbol, mod):
    """
    Puanlı sinyal — mod'a göre parametreler.
    """
    if len(df_15m) < 50 or len(df_1h) < 30:
        return None, "veri yetersiz", 0

    # 1h TREND
    ema7 = ema_hesapla(df_1h['close'], 7).iloc[-1]
    ema21 = ema_hesapla(df_1h['close'], 21).iloc[-1]
    trend_boga = ema7 > ema21
    grid_yonu = "LONG" if trend_boga else "SHORT"

    # 15m RSI
    rsi = rsi_hesapla(df_15m['close'], 14).iloc[-1]
    if pd.isna(rsi):
        return None, "RSI NaN", 0

    # 15m ADX
    adx_val = adx_hesapla(df_15m, 14)

    # ATR
    atr_pct = atr_yuzdesi(df_15m, 14)
    if not (0.3 <= atr_pct <= 5.0):
        return None, f"ATR %{atr_pct:.2f} dışı", 0

    # Emir defteri
    derinlik = emir_defteri_derinlik_analizi(symbol)

    # ============ PUANLAMA ============
    puan = 50
    puan += mod['puan_trend']

    if mod['rsi_min'] <= rsi <= mod['rsi_max']:
        puan += mod['puan_rsi']

    if adx_val > mod['adx_esik']:
        puan += mod['puan_adx']

    if derinlik == "ALICI_BASKIN" and grid_yonu == "LONG":
        puan += mod['puan_derinlik']
    elif derinlik == "SATICI_BASKIN" and grid_yonu == "SHORT":
        puan += mod['puan_derinlik']

    if puan < mod['puan_esigi']:
        return None, f"puan {puan}<{mod['puan_esigi']}", puan

    # ============ STOP / TP ============
    son_fiyat = df_15m['close'].iloc[-1]
    stop_pct = (atr_pct * mod['atr_stop_mult']) / 100.0
    stop_pct = max(mod['min_stop_pct'], min(mod['max_stop_pct'], stop_pct))
    tp_pct = mod['tp_sabit_pct'] + KOMISYON_ORANI

    if grid_yonu == "LONG":
        stop = son_fiyat * (1 - stop_pct)
        tp = son_fiyat * (1 + tp_pct)
    else:
        stop = son_fiyat * (1 + stop_pct)
        tp = son_fiyat * (1 - tp_pct)

    return {
        "yon": grid_yonu, "giris": float(son_fiyat),
        "stop": float(stop), "tp": float(tp),
        "stop_pct": stop_pct, "tp_pct": tp_pct,
        "rsi": float(rsi), "atr_pct": atr_pct,
        "adx": adx_val, "derinlik": derinlik, "puan": puan
    }, f"OK (puan {puan})", puan

# ==================== BACKTEST ====================
def backtest_puanli(symbol, params, gun_sayisi=365):
    """Puanlı sistem backtest — verilen parametrelerle."""
    try:
        limit_1h = min(gun_sayisi * 24, 1000)
        ohlcv_1h = exchange.fetch_ohlcv(symbol, params['zaman_ana'], limit=limit_1h)
        ohlcv_15m = exchange.fetch_ohlcv(symbol, params['zaman_sinyal'], limit=1000)
        df_1h = pd.DataFrame(ohlcv_1h, columns=['timestamp','open','high','low','close','volume'])
        df_15m = pd.DataFrame(ohlcv_15m, columns=['timestamp','open','high','low','close','volume'])

        if len(df_1h) < 50 or len(df_15m) < 100:
            return None

        trades = []
        pozisyon = None

        for i in range(50, len(df_15m)):
            ts = df_15m['timestamp'].iloc[i]
            df_1h_slice = df_1h[df_1h['timestamp'] <= ts]
            if len(df_1h_slice) < 30:
                continue

            if pozisyon is not None:
                high = df_15m['high'].iloc[i]
                low = df_15m['low'].iloc[i]
                if pozisyon['yon'] == 'LONG':
                    if low <= pozisyon['stop']:
                        trades.append({**pozisyon, 'cikis': pozisyon['stop']}); pozisyon = None
                    elif high >= pozisyon['tp']:
                        trades.append({**pozisyon, 'cikis': pozisyon['tp']}); pozisyon = None
                else:
                    if high >= pozisyon['stop']:
                        trades.append({**pozisyon, 'cikis': pozisyon['stop']}); pozisyon = None
                    elif low <= pozisyon['tp']:
                        trades.append({**pozisyon, 'cikis': pozisyon['tp']}); pozisyon = None
                continue

            # Yeni sinyal
            df_15m_slice = df_15m.iloc[max(0, i-100):i+1].reset_index(drop=True)
            df_1h_slice_r = df_1h_slice.reset_index(drop=True)

            if len(df_1h_slice_r) < 30:
                continue

            ema7 = ema_hesapla(df_1h_slice_r['close'], 7).iloc[-1]
            ema21 = ema_hesapla(df_1h_slice_r['close'], 21).iloc[-1]
            trend_boga = ema7 > ema21
            grid_yonu = "LONG" if trend_boga else "SHORT"

            rsi = rsi_hesapla(df_15m_slice['close'], 14).iloc[-1]
            if pd.isna(rsi):
                continue
            adx_val = adx_hesapla(df_15m_slice, 14)
            atr_pct = atr_yuzdesi(df_15m_slice, 14)
            if not (0.3 <= atr_pct <= 5.0):
                continue

            # Puanlama (backtest'te derinlik yok)
            puan = 50 + params['puan_trend']
            if params['rsi_min'] <= rsi <= params['rsi_max']:
                puan += params['puan_rsi']
            if adx_val > params['adx_esik']:
                puan += params['puan_adx']
            # Derinlik puanı atlandı

            if puan < params['puan_esigi']:
                continue

            son_fiyat = df_15m_slice['close'].iloc[-1]
            stop_pct = max(params['min_stop_pct'],
                          min(params['max_stop_pct'],
                              (atr_pct * params['atr_stop_mult']) / 100.0))
            tp_pct = params['tp_sabit_pct'] + KOMISYON_ORANI

            if grid_yonu == "LONG":
                stop = son_fiyat * (1 - stop_pct); tp = son_fiyat * (1 + tp_pct)
            else:
                stop = son_fiyat * (1 + stop_pct); tp = son_fiyat * (1 - tp_pct)

            pozisyon = {
                "yon": grid_yonu, "giris": float(son_fiyat),
                "stop": float(stop), "tp": float(tp),
                "stop_pct": stop_pct, "tp_pct": tp_pct, "puan": puan
            }

        if not trades:
            return {"symbol": symbol, "islem": 0, "win_rate": 0, "pf": 0, "max_dd": 0, "toplam": 0}
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
        return {"symbol": symbol, "islem": len(trades), "win_rate": round(win, 2),
                "pf": round(pf, 3) if pf != 999 else 999,
                "max_dd": round(max_dd, 2),
                "toplam": round((equity[-1] - 1) * 100, 2) if len(equity) else 0}
    except Exception as e:
        return {"symbol": symbol, "islem": -1, "hata": str(e)[:40]}

# ==================== BACKTEST WITH DATA (Optimize için) ====================
def backtest_with_data(df_15m, df_1h, params):
    """Optimize için: veriyi önceden çek, hızlı test."""
    try:
        trades = []
        pozisyon = None
        for i in range(50, len(df_15m)):
            ts = df_15m['timestamp'].iloc[i]
            df_1h_slice = df_1h[df_1h['timestamp'] <= ts]
            if len(df_1h_slice) < 30:
                continue

            if pozisyon is not None:
                high = df_15m['high'].iloc[i]
                low = df_15m['low'].iloc[i]
                if pozisyon['yon'] == 'LONG':
                    if low <= pozisyon['stop']:
                        trades.append({**pozisyon, 'cikis': pozisyon['stop']}); pozisyon = None
                    elif high >= pozisyon['tp']:
                        trades.append({**pozisyon, 'cikis': pozisyon['tp']}); pozisyon = None
                else:
                    if high >= pozisyon['stop']:
                        trades.append({**pozisyon, 'cikis': pozisyon['stop']}); pozisyon = None
                    elif low <= pozisyon['tp']:
                        trades.append({**pozisyon, 'cikis': pozisyon['tp']}); pozisyon = None
                continue

            df_15m_slice = df_15m.iloc[max(0, i-100):i+1].reset_index(drop=True)
            df_1h_slice_r = df_1h_slice.reset_index(drop=True)
            if len(df_1h_slice_r) < 30:
                continue

            ema7 = ema_hesapla(df_1h_slice_r['close'], 7).iloc[-1]
            ema21 = ema_hesapla(df_1h_slice_r['close'], 21).iloc[-1]
            trend_boga = ema7 > ema21
            grid_yonu = "LONG" if trend_boga else "SHORT"

            rsi = rsi_hesapla(df_15m_slice['close'], 14).iloc[-1]
            if pd.isna(rsi):
                continue
            adx_val = adx_hesapla(df_15m_slice, 14)
            atr_pct = atr_yuzdesi(df_15m_slice, 14)
            if not (0.3 <= atr_pct <= 5.0):
                continue

            puan = 50 + params['puan_trend']
            if params['rsi_min'] <= rsi <= params['rsi_max']:
                puan += params['puan_rsi']
            if adx_val > params['adx_esik']:
                puan += params['puan_adx']
            if puan < params['puan_esigi']:
                continue

            son_fiyat = df_15m_slice['close'].iloc[-1]
            stop_pct = max(params['min_stop_pct'],
                          min(params['max_stop_pct'],
                              (atr_pct * params['atr_stop_mult']) / 100.0))
            tp_pct = params['tp_sabit_pct'] + KOMISYON_ORANI

            if grid_yonu == "LONG":
                stop = son_fiyat * (1 - stop_pct); tp = son_fiyat * (1 + tp_pct)
            else:
                stop = son_fiyat * (1 + stop_pct); tp = son_fiyat * (1 - tp_pct)

            pozisyon = {
                "yon": grid_yonu, "giris": float(son_fiyat),
                "stop": float(stop), "tp": float(tp),
                "stop_pct": stop_pct, "tp_pct": tp_pct
            }

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
        return {"islem": len(trades), "win_rate": round(win, 2),
                "pf": round(pf, 3) if pf != 999 else 999,
                "max_dd": round(max_dd, 2),
                "toplam": round((equity[-1] - 1) * 100, 2) if len(equity) else 0}
    except Exception:
        return None

# ==================== OPTİMİZASYON ====================
def optimize_coin(symbol, gun_sayisi=180):
    """Puanlı sistem için optimize: eşik, ağırlıklar, TP, ATR."""
    base_mod = mod_al()
    limit_1h = min(gun_sayisi * 24, 1000)
    try:
        ohlcv_1h = exchange.fetch_ohlcv(symbol, base_mod['zaman_ana'], limit=limit_1h)
        ohlcv_15m = exchange.fetch_ohlcv(symbol, base_mod['zaman_sinyal'], limit=1000)
        df_1h = pd.DataFrame(ohlcv_1h, columns=['timestamp','open','high','low','close','volume'])
        df_15m = pd.DataFrame(ohlcv_15m, columns=['timestamp','open','high','low','close','volume'])
    except Exception:
        return None

    if len(df_1h) < 50 or len(df_15m) < 200:
        return None

    # Optimize parametreleri
    esik_list = [65, 70, 75, 80]
    tp_list = [0.010, 0.015, 0.020, 0.025]
    atr_list = [1.2, 1.5, 1.8, 2.0]
    trend_w_list = [15, 20, 25]
    rsi_w_list = [10, 15, 20]

    en_iyi = None
    sayac = 0
    for esik in esik_list:
        for tp in tp_list:
            for atr in atr_list:
                for tw in trend_w_list:
                    for rw in rsi_w_list:
                        sayac += 1
                        params = dict(base_mod)
                        params['puan_esigi'] = esik
                        params['tp_sabit_pct'] = tp
                        params['atr_stop_mult'] = atr
                        params['puan_trend'] = tw
                        params['puan_rsi'] = rw
                        r = backtest_with_data(df_15m, df_1h, params)
                        if r and r['islem'] >= 5:
                            if en_iyi is None or r['pf'] > en_iyi['pf']:
                                en_iyi = {
                                    "params": {
                                        "puan_esigi": esik,
                                        "tp_sabit_pct": tp,
                                        "atr_stop_mult": atr,
                                        "puan_trend": tw,
                                        "puan_rsi": rw,
                                        "puan_adx": base_mod['puan_adx'],
                                        "puan_derinlik": base_mod['puan_derinlik'],
                                        "rsi_min": base_mod['rsi_min'],
                                        "rsi_max": base_mod['rsi_max'],
                                        "adx_esik": base_mod['adx_esik'],
                                        "min_stop_pct": base_mod['min_stop_pct'],
                                        "max_stop_pct": base_mod['max_stop_pct'],
                                    },
                                    "pf": r['pf'], "win_rate": r['win_rate'],
                                    "toplam": r['toplam'], "max_dd": r['max_dd'],
                                    "islem": r['islem']
                                }
    return en_iyi

# ==================== FLASK ====================
@app.route('/')
def home():
    return f"Puanlı Bot | Mod: {AKTIF_MOD} | Coin: {len(TAKIP_EDILENLER)} | Poz: {len(AKTIF_GRID_SISTEMLERI)}"

# ==================== TELEGRAM ====================
async def durum_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        mod = mod_al()
        balance = exchange.fetch_balance()
        total = float(balance['total'].get('USDT', 0))
        free = float(balance['free'].get('USDT', 0))
        try:
            raw = exchange.fetch_positions()
            poslari = [p for p in raw if float(p.get('contracts', 0) or p.get('size', 0) or 0) > 0]
        except Exception:
            poslari = []
        pnl = sum(float(p.get('unrealizedPnl', 0)) for p in poslari)
        baslangic = total - pnl
        pnl_pct = (pnl / baslangic * 100) if baslangic > 0 else 0
        ikon = "🟢" if pnl >= 0 else "🔴"
        bs = ANALITIK_HAFIZA.get("basarili_islem_sayisi", 0)
        bz = ANALITIK_HAFIZA.get("basarisiz_islem_sayisi", 0)
        tot = bs + bz
        oran = (bs / tot * 100) if tot else 0
        detay = ""
        if poslari:
            detay = "\n📋 *Aktif Pozisyonlar:*\n"
            for p in poslari:
                detay += f"• `{p.get('symbol')}` | {str(p.get('side','')).upper()} | `{float(p.get('unrealizedPnl',0)):+.2f}`\n"
        else:
            detay = "\n📋 *Aktif Pozisyon Yok*\n"
        opt = len(COIN_PARAMS)
        mesaj = (
            f"🎯 *{mod['aciklama']}*\n\n"
            f"💰 Toplam: `{total:.2f}` USDT (Serbest: `{free:.2f}`)\n"
            f"{ikon} PnL: `{pnl:+.2f}` USDT (`%{pnl_pct:+.2f}`)\n"
            f"📌 Pozisyon: `{len(poslari)}/{mod['maks_pozisyon']}`\n"
            f"🧠 Optimize: `{opt}/{len(TAKIP_EDILENLER)}`\n"
            f"⚙️ Zaman: `{mod['zaman_ana']}+{mod['zaman_sinyal']}` | Kaldıraç: `{mod['kaldirac']}x`\n"
            f"🎚️ Puan eşiği: `{mod['puan_esigi']}`\n"
            f"📐 TP: `%{mod['tp_sabit_pct']*100}` | Stop: `ATR×{mod['atr_stop_mult']}`\n"
            f"{detay}\n"
            f"✅ TP: `{bs}` | ❌ Stop: `{bz}`\n"
            f"📈 Başarı: `%{oran:.1f}`\n"
        )
        await update.message.reply_text(mesaj, parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"Hata: {e}")

async def puan_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Şu anki piyasada her coinin puanını göster."""
    mod = mod_al()
    satirlar = ["🎯 *PUAN TESTİ*\n"]
    for symbol in TAKIP_EDILENLER:
        try:
            ohlcv_1h = exchange.fetch_ohlcv(symbol, mod['zaman_ana'], limit=50)
            ohlcv_15m = exchange.fetch_ohlcv(symbol, mod['zaman_sinyal'], limit=100)
            df_1h = pd.DataFrame(ohlcv_1h, columns=['timestamp','open','high','low','close','volume'])
            df_15m = pd.DataFrame(ohlcv_15m, columns=['timestamp','open','high','low','close','volume'])

            ema7 = ema_hesapla(df_1h['close'], 7).iloc[-1]
            ema21 = ema_hesapla(df_1h['close'], 21).iloc[-1]
            trend_boga = ema7 > ema21
            grid_yonu = "LONG" if trend_boga else "SHORT"

            rsi = rsi_hesapla(df_15m['close'], 14).iloc[-1]
            adx_val = adx_hesapla(df_15m, 14)
            atr_pct = atr_yuzdesi(df_15m, 14)
            derinlik = emir_defteri_derinlik_analizi(symbol)

            puan = 50 + mod['puan_trend']
            if mod['rsi_min'] <= rsi <= mod['rsi_max']:
                puan += mod['puan_rsi']
            if adx_val > mod['adx_esik']:
                puan += mod['puan_adx']
            if derinlik == "ALICI_BASKIN" and grid_yonu == "LONG":
                puan += mod['puan_derinlik']
            elif derinlik == "SATICI_BASKIN" and grid_yonu == "SHORT":
                puan += mod['puan_derinlik']

            satirlar.append(
                f"`{symbol[:12]}` {grid_yonu} | Puan: `{puan}`\n"
                f"  RSI:{rsi:.1f} ADX:{adx_val:.1f} ATR%:{atr_pct:.2f} Derinlik:{derinlik}"
            )
        except Exception:
            satirlar.append(f"`{symbol[:12]}` HATA")
    await update.message.reply_text("\n".join(satirlar), parse_mode='Markdown')

async def baslat_komutu(update, context):
    global BOT_CALISIYOR_MU
    BOT_CALISIYOR_MU = True
    await update.message.reply_text("▶️ *Bot Aktif!*", parse_mode='Markdown')

async def durdur_komutu(update, context):
    global BOT_CALISIYOR_MU
    BOT_CALISIYOR_MU = False
    await update.message.reply_text("⏸️ *Durduruldu.*", parse_mode='Markdown')

async def kapat_komutu(update, context):
    await update.message.reply_text("🔄 *Pozisyonlar kapatılıyor...*", parse_mode='Markdown')
    try:
        for pos in exchange.fetch_positions():
            k = float(pos.get('contracts', 0) or pos.get('size', 0) or 0)
            if k > 0:
                sym = pos['symbol']
                yon = str(pos.get('side', '')).upper()
                kapat = 'sell' if yon == 'LONG' else 'buy'
                tum_emirleri_iptal_et(sym)
                exchange.create_order(sym, 'market', kapat, k, None, {'reduce_only': True})
        AKTIF_GRID_SISTEMLERI.clear()
        hafizayi_kaydet()
        await update.message.reply_text("✅ Hepsi kapatıldı.", parse_mode='Markdown')
    except Exception as e:
        AKTIF_GRID_SISTEMLERI.clear()
        hafizayi_kaydet()
        await update.message.reply_text(f"⚠️ {e}", parse_mode='Markdown')

async def backtest_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BACKTEST_CALISIYOR
    if BACKTEST_CALISIYOR or OPTIMIZE_CALISIYOR:
        await update.message.reply_text("⏳ Zaten çalışıyor.")
        return
    BACKTEST_CALISIYOR = True
    await update.message.reply_text("⏳ *Backtest başlatıldı* — 5-10 dk.", parse_mode='Markdown')

    def run():
        global BACKTEST_CALISIYOR
        try:
            satirlar = ["*📊 PUANLI BACKTEST*\n```"]
            satirlar.append(f"{'COIN':<14} {'İŞL':>4} {'WIN%':>6} {'PF':>6} {'DD%':>6} {'TOT%':>7}")
            satirlar.append("-" * 52)
            toplam = 0
            for c in TAKIP_EDILENLER:
                params = coin_parametre_al(c)
                r = backtest_puanli(c, params, gun_sayisi=180)
                if r is None:
                    satirlar.append(f"{c[:12]:<14} HATA")
                elif r.get('islem', -1) == -1:
                    satirlar.append(f"{c[:12]:<14} HATA: {r.get('hata','?')[:20]}")
                else:
                    satirlar.append(
                        f"{c[:12]:<14} {r['islem']:>4} {r['win_rate']:>6} {r['pf']:>6} {r['max_dd']:>6} {r['toplam']:>7}"
                    )
                    toplam += r['toplam']
            satirlar.append("-" * 52)
            ort = toplam / len(TAKIP_EDILENLER)
            satirlar.append(f"Ortalama getiri: {ort:.2f}%")
            satirlar.append(f"Optimize coin: {len(COIN_PARAMS)}/{len(TAKIP_EDILENLER)}")
            satirlar.append("```")
            telegram_mesaj_gonder("\n".join(satirlar))
        except Exception as e:
            telegram_mesaj_gonder(f"❌ Backtest hatası: {e}")
        finally:
            BACKTEST_CALISIYOR = False

    threading.Thread(target=run, daemon=True).start()

async def optimize_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global OPTIMIZE_CALISIYOR, COIN_PARAMS
    if BACKTEST_CALISIYOR or OPTIMIZE_CALISIYOR:
        await update.message.reply_text("⏳ Zaten bir işlem çalışıyor.")
        return
    OPTIMIZE_CALISIYOR = True
    await update.message.reply_text(
        f"🧠 *Optimizasyon başlatıldı!*\n"
        f"{len(TAKIP_EDILENLER)} coin × 192 kombinasyon = 1152 test\n"
        f"Tahmini süre: 20-30 dk.",
        parse_mode='Markdown')

    def run():
        global OPTIMIZE_CALISIYOR, COIN_PARAMS
        try:
            telegram_mesaj_gonder("🔬 *Optimizasyon başladı...*")
            for c in TAKIP_EDILENLER:
                en_iyi = optimize_coin(c, gun_sayisi=180)
                if en_iyi is None:
                    telegram_mesaj_gonder(f"⚠️ `{c}` — yeterli sinyal yok.")
                    continue
                COIN_PARAMS[c] = en_iyi['params']
                hafizayi_kaydet()
                p = en_iyi['params']
                telegram_mesaj_gonder(
                    f"🏆 *{c}* — EN İYİ\n"
                    f"Eşik: `{p['puan_esigi']}` | TP: `%{p['tp_sabit_pct']*100}` | ATR×`{p['atr_stop_mult']}`\n"
                    f"Ağırlık: Trend `{p['puan_trend']}` | RSI `{p['puan_rsi']}`\n"
                    f"📊 İŞL: `{en_iyi['islem']}` | WIN: `{en_iyi['win_rate']}%` | "
                    f"PF: `{en_iyi['pf']}` | TOT: `{en_iyi['toplam']}%`"
                )
                time.sleep(1)
            telegram_mesaj_gonder(f"✅ *Optimizasyon tamamlandı!* `/backtest` yaz.")
        except Exception as e:
            telegram_mesaj_gonder(f"❌ Optimizasyon hatası: {e}")
        finally:
            OPTIMIZE_CALISIYOR = False

    threading.Thread(target=run, daemon=True).start()

async def params_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not COIN_PARAMS:
        await update.message.reply_text("Henüz optimize yok.", parse_mode='Markdown')
        return
    satirlar = ["🧠 *Optimize Parametreleri:*\n"]
    for sym, p in COIN_PARAMS.items():
        satirlar.append(
            f"`{sym[:12]}` Eşik:{p['puan_esigi']} TP:%{p['tp_sabit_pct']*100} "
            f"ATR×{p['atr_stop_mult']} Trend:{p['puan_trend']} RSI:{p['puan_rsi']}"
        )
    await update.message.reply_text("\n".join(satirlar), parse_mode='Markdown')

async def temizle_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global COIN_PARAMS
    COIN_PARAMS = {}
    hafizayi_kaydet()
    await update.message.reply_text("🗑️ Temizlendi.", parse_mode='Markdown')

# ==================== ANA DÖNGÜ ====================
def otomatik_arkaplan_tarayici():
    global BOT_CALISIYOR_MU, ANALITIK_HAFIZA
    mod = mod_al()
    print(f"🎯 [BOT] {mod['aciklama']}", flush=True)
    try:
        exchange.load_markets()
    except Exception:
        pass

    dongu_sayaci = 0
    while True:
        try:
            if not BOT_CALISIYOR_MU:
                time.sleep(5); continue
            if OPTIMIZE_CALISIYOR:
                time.sleep(10); continue
            mod = mod_al()
            if gunluk_kayip_kontrol(mod):
                telegram_mesaj_gonder(f"🛑 Günlük limit aşıldı!")
                BOT_CALISIYOR_MU = False
                time.sleep(3600); continue

            try:
                raw = exchange.fetch_positions()
                aktif_borsa = {p['symbol']: p for p in raw
                               if float(p.get('contracts', 0) or p.get('size', 0) or 0) > 0}
            except Exception:
                aktif_borsa = {}

            for sym in list(AKTIF_GRID_SISTEMLERI.keys()):
                if sym not in aktif_borsa:
                    AKTIF_GRID_SISTEMLERI.pop(sym)
                    basarili = False
                    try:
                        tum_emirleri_iptal_et(sym)
                        closed = exchange.fetch_closed_orders(sym, limit=10)
                        pnl_real = 0.0
                        if closed:
                            son = sorted(closed, key=lambda x: x['timestamp'] or 0)[-2:]
                            for o in son:
                                pnl_real += float(o.get('info', {}).get('pnl', 0) or 0)
                        basarili = pnl_real > 0
                    except Exception: pass
                    if basarili:
                        ANALITIK_HAFIZA["basarili_islem_sayisi"] = ANALITIK_HAFIZA.get("basarili_islem_sayisi", 0) + 1
                        telegram_mesaj_gonder(f"🎉 *Kâr* → `{sym}` 🟢")
                    else:
                        ANALITIK_HAFIZA["basarisiz_islem_sayisi"] = ANALITIK_HAFIZA.get("basarisiz_islem_sayisi", 0) + 1
                        COIN_COOLDOWNLAR[sym] = time.time() + mod['cooldown_dk'] * 60
                        telegram_mesaj_gonder(f"❌ *Stop* → `{sym}` 🔴")
                    hafizayi_kaydet()

            su_an = time.time()
            sinyaller = []
            debug = []
            for symbol in TAKIP_EDILENLER:
                if not BOT_CALISIYOR_MU: break
                if symbol in aktif_borsa: continue
                if len(aktif_borsa) >= mod['maks_pozisyon']: break
                if su_an < COIN_COOLDOWNLAR.get(symbol, 0): continue
                try:
                    params = coin_parametre_al(symbol)
                    ohlcv_1h = exchange.fetch_ohlcv(symbol, params['zaman_ana'], limit=50)
                    ohlcv_15m = exchange.fetch_ohlcv(symbol, params['zaman_sinyal'], limit=120)
                    df_1h = pd.DataFrame(ohlcv_1h, columns=['timestamp','open','high','low','close','volume'])
                    df_15m = pd.DataFrame(ohlcv_15m, columns=['timestamp','open','high','low','close','volume'])
                    sig, neden, puan = sinyal_uret_puanli(df_15m, df_1h, symbol, params)
                    if sig:
                        sinyaller.append({"symbol": symbol, **sig})
                        debug.append(f"{symbol.split('/')[0]}:✅(p{puan})")
                    else:
                        debug.append(f"{symbol.split('/')[0]}:p{puan}")
                except Exception:
                    continue

            dongu_sayaci += 1
            if dongu_sayaci % 20 == 0:
                ozet = " | ".join(debug[:6])
                print(f"🔍 #{dongu_sayaci} | {ozet}", flush=True)

            for s in sinyaller:
                if not BOT_CALISIYOR_MU: break
                if len(aktif_borsa) >= mod['maks_pozisyon']: break
                symbol = s["symbol"]; yon = s["yon"]; stop_pct = s["stop_pct"]
                try:
                    bal = exchange.fetch_balance()
                    kasa = float(bal['total'].get('USDT', 0))
                except Exception: continue
                if kasa <= 0: continue
                if not set_leverage_and_margin_safely(symbol, mod['kaldirac']): continue
                risk_usdt = kasa * mod['islem_riski_pct']
                poz_degeri = risk_usdt / stop_pct
                try:
                    market_info = exchange.market(symbol)
                    cs = float(market_info.get('contractSize', 1.0))
                    ham = poz_degeri / (s["giris"] * cs)
                    miktar = float(exchange.amount_to_precision(symbol, max(ham, 0.001)))
                    if miktar <= 0: continue
                except Exception: continue
                try:
                    tum_emirleri_iptal_et(symbol)
                    emir = exchange.create_order(symbol, 'market', 'buy' if yon == 'LONG' else 'sell', miktar)
                    giris = float(emir.get('average') or emir.get('price') or s["giris"])
                    time.sleep(0.5)
                    if yon == 'LONG':
                        stop = giris * (1 - stop_pct); tp = giris * (1 + s["tp_pct"]); kapat_yon = 'sell'
                    else:
                        stop = giris * (1 + stop_pct); tp = giris * (1 - s["tp_pct"]); kapat_yon = 'buy'
                    stop = float(exchange.price_to_precision(symbol, stop))
                    tp = float(exchange.price_to_precision(symbol, tp))
                    try:
                        exchange.create_order(symbol, 'limit', kapat_yon, miktar, tp, {'reduceOnly': True})
                    except Exception: pass
                    try:
                        exchange.create_order(symbol, 'stop', kapat_yon, miktar, stop,
                            {'stopPrice': stop, 'triggerPrice': stop, 'reduceOnly': True})
                    except Exception:
                        try:
                            exchange.create_order(symbol, 'stop_market', kapat_yon, miktar, stop,
                                {'stopPrice': stop, 'triggerPrice': stop, 'reduceOnly': True})
                        except Exception: pass

                    AKTIF_GRID_SISTEMLERI[symbol] = {
                        "yon": yon, "giris": giris, "stop": stop, "tp": tp, "miktar": miktar
                    }
                    hafizayi_kaydet()
                    print(f"🎯 {symbol} | {yon} | Puan:{s.get('puan')} | TP:{tp} SL:{stop}", flush=True)
                    telegram_mesaj_gonder(
                        f"🎯 *İŞLEM AÇILDI*\n"
                        f"📌 `{symbol}` | *{yon}*\n"
                        f"💰 Giriş: `{giris}`\n"
                        f"🎯 TP: `{tp}` | 🛑 SL: `{stop}`\n"
                        f"📊 Puan: `{s.get('puan', 0)}` | RSI: `{s['rsi']:.1f}` | ADX: `{s.get('adx', 0):.1f}`\n"
                        f"📚 Derinlik: `{s.get('derinlik', '?')}`"
                    )
                    break
                except Exception as e:
                    print(f"❌ İşlem hatası: {e}", flush=True)
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
    app_tg.add_handler(CommandHandler("puan", puan_komutu))
    app_tg.add_handler(CommandHandler("baslat", baslat_komutu))
    app_tg.add_handler(CommandHandler("durdur", durdur_komutu))
    app_tg.add_handler(CommandHandler("kapat", kapat_komutu))
    app_tg.add_handler(CommandHandler("backtest", backtest_komutu))
    app_tg.add_handler(CommandHandler("optimize", optimize_komutu))
    app_tg.add_handler(CommandHandler("params", params_komutu))
    app_tg.add_handler(CommandHandler("temizle", temizle_komutu))

    app_tg.run_polling(drop_pending_updates=True)

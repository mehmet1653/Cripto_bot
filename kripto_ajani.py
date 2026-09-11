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

MODLAR = {
    "puanli_kismi": {
        "aciklama": "🎯 Puanlı+Kısmi (8 faktör, eşik 85, TP1:%2/%50, TP2:%4/%50, Stop:%2)",
        "zaman_ana": "1h",
        "zaman_sinyal": "15m",
        # Puanlama eşiği
        "puan_esigi": 85,
        # Sinyal parametreleri
        "bollinger_period": 20, "bollinger_std": 1.7,
        "rsi_period": 14, "rsi_long": 38, "rsi_short": 62,
        "hacim_esik": 0.6,
        "adx_esik": 20,
        # Stop/TP
        "stop_pct": 0.020,
        "tp1_pct": 0.020, "tp1_oran": 0.50,
        "tp2_pct": 0.040, "tp2_oran": 1.00,
        # Risk
        "islem_riski_pct": 0.03, "kaldirac": 10, "maks_pozisyon": 3,
        "cooldown_dk": 10,
        "gunluk_max_kayip_pct": 0.10,
    },
}

BOT_CALISIYOR_MU = True
GUN_BASI_KASA = None
GUN_BASI_TARIH = None
BACKTEST_CALISIYOR = False
OPTIMIZE_CALISIYOR = False
AKTIF_MOD = "puanli_kismi"

# ==================== SUPABASE ====================
def hafizayi_yukle():
    varsayilan = {
        "aktif_sistemler": {},
        "analitik": {"basarili_islem_sayisi": 0, "basarisiz_islem_sayisi": 0},
        "cooldownlar": {}, "coin_params": {}, "aktif_mod": "puanli_kismi"
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
                "aktif_mod": v.get("aktif_mod", "puanli_kismi")
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
AKTIF_MOD = kalici.get("aktif_mod", "puanli_kismi")

def mod_al():
    return MODLAR.get(AKTIF_MOD, MODLAR["puanli_kismi"])

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

def bollinger_hesapla(close, period=20, std_mult=2.0):
    sma = close.rolling(period).mean()
    std = close.rolling(period).std()
    return sma, sma + (std * std_mult), sma - (std * std_mult)

def atr_hesapla(df, period=14):
    tr = pd.concat([
        df['high'] - df['low'],
        (df['high'] - df['close'].shift()).abs(),
        (df['low'] - df['close'].shift()).abs()
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean()

def ema_hesapla(close, period):
    return close.ewm(span=period, adjust=False).mean()

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

def hacim_orani(df, period=20):
    try:
        ort = df['volume'].rolling(period).mean().iloc[-1]
        son = df['volume'].iloc[-1]
        return float(son / ort) if ort > 0 else 1.0
    except Exception:
        return 1.0

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
        if bid_orani > 0.55: return "ALICI_BASKIN"
        elif bid_orani < 0.45: return "SATICI_BASKIN"
        return "DENGELI"
    except Exception:
        return "DENGELI"

# ==================== İYİLEŞTİRİLMİŞ PUANLAMA (8 FAKTÖR) ====================
def hesapla_puan(df_15m, df_1h, symbol, params):
    """8 faktörlü puanlama — eşik 85."""
    puan = 0
    detay = {}

    # ===== 1. TREND (0-30) =====
    ema7 = ema_hesapla(df_1h['close'], 7).iloc[-1]
    ema21 = ema_hesapla(df_1h['close'], 21).iloc[-1]
    if pd.isna(ema7) or pd.isna(ema21) or ema21 == 0:
        return 0, "EMA NaN", "LONG"
    fark_pct = abs(ema7 - ema21) / ema21 * 100
    trend_boga = ema7 > ema21
    yon = "LONG" if trend_boga else "SHORT"
    puan += 20
    if fark_pct > 1.5:
        puan += 10
    detay['trend'] = 20 + (10 if fark_pct > 1.5 else 0)

    # ===== 2. RSI (0-25) =====
    rsi = rsi_hesapla(df_15m['close'], 14).iloc[-1]
    if pd.isna(rsi):
        return 0, "RSI NaN", yon
    if rsi < 25 or rsi > 75: puan += 25
    elif rsi < 30 or rsi > 70: puan += 20
    elif rsi < 35 or rsi > 65: puan += 15
    elif 40 <= rsi <= 60: puan += 5
    detay['rsi'] = rsi

    # ===== 3. ADX (0-25) =====
    adx = adx_hesapla(df_15m, 14)
    if adx > 45: puan += 25
    elif adx > 35: puan += 20
    elif adx > 25: puan += 15
    elif adx > 20: puan += 10
    detay['adx'] = adx

    # ===== 4. BOLLINGER AŞIRILIK (0-15) =====
    sma, ust, alt = bollinger_hesapla(df_15m['close'], params['bollinger_period'], params['bollinger_std'])
    son_ust = ust.iloc[-1]; son_alt = alt.iloc[-1]; son_fiyat = df_15m['close'].iloc[-1]
    if not (pd.isna(son_ust) or pd.isna(son_alt)) and son_ust > son_alt:
        bb_poz = (son_fiyat - son_alt) / (son_ust - son_alt)
        if bb_poz < 0.05 or bb_poz > 0.95: puan += 15
        elif bb_poz < 0.15 or bb_poz > 0.85: puan += 10
        detay['bb'] = round(bb_poz, 2)

    # ===== 5. HACİM PATLAMASI (0-15) =====
    h_orani = hacim_orani(df_15m, 20)
    if h_orani > 2.0: puan += 15
    elif h_orani > 1.5: puan += 10
    elif h_orani > 1.0: puan += 5
    detay['hacim'] = round(h_orani, 2)

    # ===== 6. MUM YAPISI (0-10) =====
    govde = abs(df_15m['close'].iloc[-1] - df_15m['open'].iloc[-1])
    fitil = df_15m['high'].iloc[-1] - df_15m['low'].iloc[-1]
    if fitil > 0 and govde / fitil > 0.7:
        puan += 10
        detay['mum'] = "güçlü"
    else:
        detay['mum'] = "zayıf"

    # ===== 7. EMİR DEFTERİ (0-10) - Canlıda =====
    if symbol:
        derinlik = emir_defteri_derinlik_analizi(symbol)
        if (derinlik == "ALICI_BASKIN" and yon == "LONG") or \
           (derinlik == "SATICI_BASKIN" and yon == "SHORT"):
            puan += 10
        detay['derinlik'] = derinlik

    detay['toplam_puan'] = puan
    return puan, detay, yon

def sinyal_uret_puanli(df_15m, df_1h, symbol, params):
    """Puanlı sinyal — eşik kontrolü."""
    if len(df_15m) < 50 or len(df_1h) < 30:
        return None, "veri yetersiz", 0

    puan, detay, yon = hesapla_puan(df_15m, df_1h, symbol, params)
    if isinstance(detay, str):
        return None, detay, puan

    if puan < params['puan_esigi']:
        return None, f"puan {puan}<{params['puan_esigi']}", puan

    # Mum yönü kontrolü
    son_fiyat = df_15m['close'].iloc[-1]
    son_open = df_15m['open'].iloc[-1]
    if yon == "LONG" and son_fiyat <= son_open:
        return None, "mum yönü LONG değil", puan
    if yon == "SHORT" and son_fiyat >= son_open:
        return None, "mum yönü SHORT değil", puan

    # ATR kontrolü
    atr_pct = (atr_hesapla(df_15m, 14).iloc[-1] / son_fiyat) * 100
    if not (0.3 <= atr_pct <= 5.0):
        return None, f"ATR dışı", puan

    # Stop ve TP
    stop_pct = params['stop_pct']
    if yon == "LONG":
        stop = son_fiyat * (1 - stop_pct)
    else:
        stop = son_fiyat * (1 + stop_pct)

    return {
        "yon": yon, "giris": float(son_fiyat), "stop": float(stop),
        "stop_pct": stop_pct, "puan": puan, "detay": detay
    }, f"OK (puan {puan})", puan

# ==================== BACKTEST (KISMİ KÂR) ====================
def backtest_puanli_kismi(symbol, params, gun_sayisi=180):
    """Backtest: puanlama + kısmi kâr (backtest'te emir defteri yok)."""
    try:
        limit_1h = min(gun_sayisi * 24, 1000)
        ohlcv_1h = exchange.fetch_ohlcv(symbol, params['zaman_ana'], limit=limit_1h)
        ohlcv_15m = exchange.fetch_ohlcv(symbol, params['zaman_sinyal'], limit=1000)
        df_1h = pd.DataFrame(ohlcv_1h, columns=['timestamp','open','high','low','close','volume'])
        df_15m = pd.DataFrame(ohlcv_15m, columns=['timestamp','open','high','low','close','volume'])
        if len(df_1h) < 50 or len(df_15m) < 100:
            return None
        return _run_backtest(df_15m, df_1h, params)
    except Exception as e:
        return {"symbol": symbol, "islem": -1, "hata": str(e)[:40]}

def _run_backtest(df_15m, df_1h, params):
    """Ortak backtest motoru."""
    trades = []
    pozisyon = None
    for i in range(50, len(df_15m)):
        ts = df_15m['timestamp'].iloc[i]
        df_1h_s = df_1h[df_1h['timestamp'] <= ts]
        if len(df_1h_s) < 30:
            continue
        bar = df_15m.iloc[i]
        high = bar['high']; low = bar['low']

        if pozisyon is not None:
            kalan_oran = 1.0 - sum([k[1] for k in pozisyon['kapatilan']])
            if pozisyon['yon'] == 'LONG':
                # TP1
                if 1 not in [k[0] for k in pozisyon['kapatilan']]:
                    if high >= pozisyon['giris'] * (1 + params['tp1_pct']):
                        oran_kapat = min(params['tp1_oran'], kalan_oran)
                        pozisyon['kapatilan'].append((1, oran_kapat, pozisyon['giris'] * (1 + params['tp1_pct'])))
                        kalan_oran -= oran_kapat
                # TP2
                if 2 not in [k[0] for k in pozisyon['kapatilan']]:
                    if high >= pozisyon['giris'] * (1 + params['tp2_pct']):
                        pozisyon['kapatilan'].append((2, kalan_oran, pozisyon['giris'] * (1 + params['tp2_pct'])))
                        kalan_oran = 0
                # Stop
                if low <= pozisyon['stop']:
                    pozisyon['kapatilan'].append((0, kalan_oran, pozisyon['stop']))
                    trades.append(pozisyon); pozisyon = None
                elif kalan_oran <= 0.001:
                    trades.append(pozisyon); pozisyon = None
            else:
                if 1 not in [k[0] for k in pozisyon['kapatilan']]:
                    if low <= pozisyon['giris'] * (1 - params['tp1_pct']):
                        oran_kapat = min(params['tp1_oran'], kalan_oran)
                        pozisyon['kapatilan'].append((1, oran_kapat, pozisyon['giris'] * (1 - params['tp1_pct'])))
                        kalan_oran -= oran_kapat
                if 2 not in [k[0] for k in pozisyon['kapatilan']]:
                    if low <= pozisyon['giris'] * (1 - params['tp2_pct']):
                        pozisyon['kapatilan'].append((2, kalan_oran, pozisyon['giris'] * (1 - params['tp2_pct'])))
                        kalan_oran = 0
                if high >= pozisyon['stop']:
                    pozisyon['kapatilan'].append((0, kalan_oran, pozisyon['stop']))
                    trades.append(pozisyon); pozisyon = None
                elif kalan_oran <= 0.001:
                    trades.append(pozisyon); pozisyon = None
            continue

        # Yeni sinyal
        df_15m_s = df_15m.iloc[max(0, i-100):i+1].reset_index(drop=True)
        df_1h_r = df_1h_s.reset_index(drop=True)
        try:
            sig, _, _ = sinyal_uret_puanli(df_15m_s, df_1h_r, None, params)
        except Exception:
            continue
        if sig:
            pozisyon = {"yon": sig['yon'], "giris": sig['giris'], "stop": sig['stop'],
                        "kapatilan": []}

    if not trades:
        return {"symbol": "", "islem": 0, "win_rate": 0, "pf": 0, "max_dd": 0, "toplam": 0}
    kazanclar = []
    for t in trades:
        ort_pct = 0
        for (tp_level, oran, fiyat) in t['kapatilan']:
            if t['yon'] == 'LONG':
                pct = (fiyat - t['giris']) / t['giris']
            else:
                pct = (t['giris'] - fiyat) / t['giris']
            pct -= KOMISYON_ORANI
            ort_pct += pct * oran
        kazanclar.append(ort_pct)
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

# ==================== OPTİMİZASYON ====================
def optimize_coin(symbol, gun_sayisi=180):
    """Optimize: sinyal + puanlama + stop."""
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

    bstd_list = [1.5, 1.7, 2.0]
    rl_list = [32, 38, 42]
    esik_list = [80, 85, 90]
    stop_list = [0.015, 0.020, 0.025]

    en_iyi = None
    for bstd in bstd_list:
        for rl in rl_list:
            for esik in esik_list:
                for sp in stop_list:
                    params = dict(base_mod)
                    params['bollinger_std'] = bstd
                    params['rsi_long'] = rl
                    params['rsi_short'] = 100 - rl
                    params['puan_esigi'] = esik
                    params['stop_pct'] = sp
                    try:
                        r = _run_backtest(df_15m, df_1h, params)
                    except Exception:
                        continue
                    if r and r['islem'] >= 5:
                        if en_iyi is None or r['pf'] > en_iyi['pf']:
                            en_iyi = {"params": {
                                "bollinger_std": bstd,
                                "rsi_long": rl, "rsi_short": 100-rl,
                                "puan_esigi": esik,
                                "stop_pct": sp,
                                "tp1_pct": base_mod['tp1_pct'],
                                "tp1_oran": base_mod['tp1_oran'],
                                "tp2_pct": base_mod['tp2_pct'],
                                "tp2_oran": base_mod['tp2_oran'],
                                "bollinger_period": base_mod['bollinger_period'],
                                "rsi_period": base_mod['rsi_period'],
                                "hacim_esik": base_mod['hacim_esik'],
                                "adx_esik": base_mod['adx_esik'],
                            },
                            "pf": r['pf'], "win_rate": r['win_rate'],
                            "toplam": r['toplam'], "max_dd": r['max_dd'],
                            "islem": r['islem']}
    return en_iyi
    # ==================== FLASK ====================
@app.route('/')
def home():
    return f"Puanlı+Kısmi Bot | Mod: {AKTIF_MOD} | Poz: {len(AKTIF_GRID_SISTEMLERI)}"

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
            detay = "\n📋 *Aktif:*\n"
            for p in poslari:
                sym = p.get('symbol')
                kk = ""
                if sym in AKTIF_GRID_SISTEMLERI:
                    kismi = AKTIF_GRID_SISTEMLERI[sym].get('kismi_kapatmalar', [])
                    if kismi:
                        kk = f" | TP1✅"
                detay += f"• `{sym}` | {str(p.get('side','')).upper()} | `{float(p.get('unrealizedPnl',0)):+.2f}`{kk}\n"
        else:
            detay = "\n📋 *Pozisyon Yok*\n"
        opt = len(COIN_PARAMS)
        mesaj = (
            f"🎯 *PUANLI + KISMİ KÂR BOTU*\n\n"
            f"💰 Toplam: `{total:.2f}` USDT (Serbest: `{free:.2f}`)\n"
            f"{ikon} PnL: `{pnl:+.2f}` USDT (`%{pnl_pct:+.2f}`)\n"
            f"📌 Pozisyon: `{len(poslari)}/{mod['maks_pozisyon']}`\n"
            f"🧠 Optimize: `{opt}/6`\n"
            f"🎚️ Puan eşiği: `{mod['puan_esigi']}`\n"
            f"📐 Stop: `%{mod['stop_pct']*100}` | TP1: `%{mod['tp1_pct']*100}/%50` | TP2: `%{mod['tp2_pct']*100}`\n"
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
            puan, detay, yon = hesapla_puan(df_15m, df_1h, symbol, mod)
            if isinstance(detay, dict):
                satirlar.append(
                    f"`{symbol[:10]}` {yon} | Puan: `{puan}`\n"
                    f"  Trend:{detay.get('trend',0)} RSI:{detay.get('rsi',0):.0f} "
                    f"ADX:{detay.get('adx',0):.0f} BB:{detay.get('bb','-')} "
                    f"Hacim:{detay.get('hacim','-')} Derinlik:{detay.get('derinlik','-')}"
                )
            else:
                satirlar.append(f"`{symbol[:10]}` HATA: {detay}")
        except Exception as e:
            satirlar.append(f"`{symbol[:10]}` HATA")
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
    await update.message.reply_text("🔄 *Kapatılıyor...*", parse_mode='Markdown')
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
        await update.message.reply_text("✅ Kapatıldı.", parse_mode='Markdown')
    except Exception as e:
        AKTIF_GRID_SISTEMLERI.clear()
        hafizayi_kaydet()
        await update.message.reply_text(f"⚠️ {e}", parse_mode='Markdown')

async def temizle_komutu(update, context):
    global COIN_PARAMS
    COIN_PARAMS = {}
    hafizayi_kaydet()
    await update.message.reply_text("🗑️ Temizlendi.", parse_mode='Markdown')

async def optimize_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global OPTIMIZE_CALISIYOR, COIN_PARAMS
    if OPTIMIZE_CALISIYOR:
        await update.message.reply_text("⏳ Zaten çalışıyor.")
        return
    OPTIMIZE_CALISIYOR = True
    await update.message.reply_text(
        f"🧠 *Optimize başladı*\n"
        f"{len(TAKIP_EDILENLER)} coin × 81 kombinasyon\n"
        f"Tahmini: 20-30 dk.",
        parse_mode='Markdown')

    def run():
        global OPTIMIZE_CALISIYOR, COIN_PARAMS
        try:
            telegram_mesaj_gonder("🔬 *Optimize başladı...*")
            for c in TAKIP_EDILENLER:
                en_iyi = optimize_coin(c, gun_sayisi=180)
                if en_iyi is None:
                    telegram_mesaj_gonder(f"⚠️ `{c}` — yeterli sinyal yok.")
                    continue
                COIN_PARAMS[c] = en_iyi['params']
                hafizayi_kaydet()
                p = en_iyi['params']
                telegram_mesaj_gonder(
                    f"🏆 *{c}*\n"
                    f"STD:`{p['bollinger_std']}` RSI:`{p['rsi_long']}/{p['rsi_short']}` "
                    f"Eşik:`{p['puan_esigi']}` Stop:`%{p['stop_pct']*100}`\n"
                    f"📊 İŞL:`{en_iyi['islem']}` WIN:`{en_iyi['win_rate']}%` "
                    f"PF:`{en_iyi['pf']}` TOT:`{en_iyi['toplam']}%`"
                )
                time.sleep(1)
            telegram_mesaj_gonder(f"✅ *Optimize tamamlandı!* `/backtest` yaz.")
        except Exception as e:
            telegram_mesaj_gonder(f"❌ {e}")
        finally:
            OPTIMIZE_CALISIYOR = False

    threading.Thread(target=run, daemon=True).start()

async def backtest_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BACKTEST_CALISIYOR
    if BACKTEST_CALISIYOR:
        await update.message.reply_text("⏳ Zaten çalışıyor.")
        return
    BACKTEST_CALISIYOR = True
    mod = mod_al()
    await update.message.reply_text(
        f"⏳ *Backtest başladı*\n{mod['aciklama']}",
        parse_mode='Markdown')

    def run():
        global BACKTEST_CALISIYOR
        try:
            satirlar = [f"*📊 PUANLI+KISMİ BACKTEST*\n```"]
            satirlar.append(f"{'COIN':<14} {'İŞL':>4} {'WIN%':>6} {'PF':>6} {'DD%':>6} {'TOT%':>7}")
            satirlar.append("-" * 52)
            toplam = 0
            for c in TAKIP_EDILENLER:
                params = coin_parametre_al(c)
                r = backtest_puanli_kismi(c, params, gun_sayisi=180)
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
            satirlar.append(f"Ortalama: {ort:.2f}%")
            satirlar.append(f"Optimize: {len(COIN_PARAMS)}/6")
            satirlar.append("```")
            telegram_mesaj_gonder("\n".join(satirlar))
        except Exception as e:
            telegram_mesaj_gonder(f"❌ {e}")
        finally:
            BACKTEST_CALISIYOR = False

    threading.Thread(target=run, daemon=True).start()

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
            mod = mod_al()
            if gunluk_kayip_kontrol(mod):
                telegram_mesaj_gonder(f"🛑 Günlük limit!")
                BOT_CALISIYOR_MU = False
                time.sleep(3600); continue

            try:
                raw = exchange.fetch_positions()
                aktif_borsa = {p['symbol']: p for p in raw
                               if float(p.get('contracts', 0) or p.get('size', 0) or 0) > 0}
            except Exception:
                aktif_borsa = {}

            # ===== AÇIK POZİSYONLARI YÖNET (KISMİ KÂR) =====
            for sym, poz in list(AKTIF_GRID_SISTEMLERI.items()):
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
                    continue

                borsa_poz = aktif_borsa[sym]
                pnl_pct = float(borsa_poz.get('percentage', 0) or 0) / 100
                yon = poz.get('yon', 'LONG')
                kapat_yon = 'sell' if yon == 'LONG' else 'buy'
                toplam_miktar = poz.get('toplam_miktar', poz.get('miktar', 0))
                kalan_miktar = float(borsa_poz.get('contracts', 0) or 0)
                kismi = poz.get('kismi_kapatmalar', [])

                # TP1: %2'de %50 kapat
                if mod['tp1_pct'] not in kismi and pnl_pct >= mod['tp1_pct']:
                    kapat_miktar = toplam_miktar * mod['tp1_oran']
                    if 0 < kapat_miktar <= kalan_miktar:
                        try:
                            exchange.create_order(sym, 'market', kapat_yon, kapat_miktar, None, {'reduce_only': True})
                            kismi.append(mod['tp1_pct'])
                            poz['kismi_kapatmalar'] = kismi
                            AKTIF_GRID_SISTEMLERI[sym] = poz
                            hafizayi_kaydet()
                            telegram_mesaj_gonder(f"💰 *TP1* → `{sym}` %50 kapatıldı (+%{mod['tp1_pct']*100:.1f})")
                        except Exception: pass

                # TP2: %4'te kalanı kapat
                if mod['tp2_pct'] not in kismi and pnl_pct >= mod['tp2_pct']:
                    if kalan_miktar > 0:
                        try:
                            exchange.create_order(sym, 'market', kapat_yon, kalan_miktar, None, {'reduce_only': True})
                            telegram_mesaj_gonder(f"🎯 *TP2* → `{sym}` kalan kapatıldı (+%{mod['tp2_pct']*100:.1f})")
                        except Exception: pass

            # ===== YENİ SİNYAL =====
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
                        debug.append(f"{symbol.split('/')[0]}:✅{sig['yon']}(p{puan})")
                    else:
                        debug.append(f"{symbol.split('/')[0]}:p{puan}")
                except Exception:
                    continue

            dongu_sayaci += 1
            if dongu_sayaci % 20 == 0:
                print(f"🔍 #{dongu_sayaci} | {' | '.join(debug[:6])}", flush=True)

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
                        stop = giris * (1 - stop_pct); kapat_yon = 'sell'
                    else:
                        stop = giris * (1 + stop_pct); kapat_yon = 'buy'
                    stop = float(exchange.price_to_precision(symbol, stop))
                    try:
                        exchange.create_order(symbol, 'stop', kapat_yon, miktar, stop,
                            {'stopPrice': stop, 'triggerPrice': stop, 'reduceOnly': True})
                    except Exception:
                        try:
                            exchange.create_order(symbol, 'stop_market', kapat_yon, miktar, stop,
                                {'stopPrice': stop, 'triggerPrice': stop, 'reduceOnly': True})
                        except Exception: pass

                    AKTIF_GRID_SISTEMLERI[symbol] = {
                        "yon": yon, "giris": giris, "stop": stop,
                        "miktar": miktar, "toplam_miktar": miktar,
                        "kismi_kapatmalar": []
                    }
                    hafizayi_kaydet()
                    print(f"🎯 {symbol} | {yon} | Puan:{s.get('puan')} | {miktar}", flush=True)
                    telegram_mesaj_gonder(
                        f"🎯 *İŞLEM AÇILDI*\n"
                        f"📌 `{symbol}` | *{yon}*\n"
                        f"💰 Giriş: `{giris}` | SL: `{stop}`\n"
                        f"📊 Puan: `{s.get('puan')}` | RSI: `{s['detay'].get('rsi', 0):.0f}` "
                        f"ADX: `{s['detay'].get('adx', 0):.0f}`\n"
                        f"📐 TP1: %2 (½) | TP2: %4 | Stop: %2"
                    )
                    break
                except Exception as e:
                    print(f"❌ {e}", flush=True)
        except Exception as e:
            print(f"⚠️ {e}", flush=True)
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
    app_tg.add_handler(CommandHandler("temizle", temizle_komutu))
    app_tg.add_handler(CommandHandler("optimize", optimize_komutu))
    app_tg.add_handler(CommandHandler("backtest", backtest_komutu))

    app_tg.run_polling(drop_pending_updates=True)

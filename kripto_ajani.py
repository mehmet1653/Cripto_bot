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
    'SOL/USDT:USDT', 'XRP/USDT:USDT', 'BNB/USDT:USDT', 'DOGE/USDT:USDT'
]

KOMISYON_ORANI = 0.001

# ==================== AGRESİF PAKET ====================
MOD = {
    "aciklama": "🔥 Agresif Rejim Botu",
    # Zaman dilimleri
    "zaman_trend": "4h",
    "zaman_rejim": "15m",
    "zaman_sinyal": "1h",
    # Genel trend
    "trend_ema_hizli": 50,
    "trend_ema_yavas": 200,
    "trend_belirsiz_pct": 0.005,
    # Rejim
    "adx_trend_esik": 30,
    "adx_yatay_esik": 20,
    # Trend botu
    "ema_hizli": 9, "ema_orta": 21, "ema_yavas": 50,
    "rsi_period": 14,
    "rsi_long_min": 45, "rsi_long_max": 65,
    "rsi_short_min": 35, "rsi_short_max": 55,
    "hacim_esik_trend": 1.0,
    "atr_min": 0.5, "atr_max": 5.0,
    "atr_stop_mult": 1.5,
    "risk_reward": 2.0,
    "trailing_atr": 2.0,
    # Grid botu
    "grid_aralik_pct": 0.03,
    "grid_sayisi": 10,
    "grid_kar_pct": 0.006,
    # Hacim filtresi
    "min_hacim_usdt": 100_000_000,
    # Risk (AGRESİF)
    "islem_riski_pct": 0.03,
    "kaldirac": 10,
    "maks_pozisyon": 5,
    "gunluk_max_kayip_pct": 0.10,
    "gunluk_max_kar_pct": 0.20,
    "cooldown_dk": 5,
    # Trend pozisyon yönetimi
    "trailing_kullan": True,
    "breakeven_pct": 0.008,
}

BOT_CALISIYOR_MU = True
GUN_BASI_KASA = None
GUN_BASI_TARIH = None
AKTIF_POZISYONLAR = {}   # {symbol: pozisyon dict}
AKTIF_GRIDLER = {}       # {symbol: grid dict}
GLOBAL_TREND = {}        # {symbol: "LONG" veya "SHORT"}
BACKTEST_CALISIYOR = False
OPTIMIZE_CALISIYOR = False
COIN_PARAMS = {}

ANALITIK = {
    "basarili_islem_sayisi": 0,
    "basarisiz_islem_sayisi": 0,
    "toplam_kar": 0.0,
    "trend_long": 0,
    "trend_short": 0,
    "grid_kurulum": 0,
    "trend_kapatma": 0,
}

# ==================== SUPABASE ====================
def hafizayi_yukle():
    varsayilan = {
        "aktif_pozisyonlar": {},
        "aktif_gridler": {},
        "analitik": ANALITIK.copy(),
        "coin_params": {},
    }
    try:
        r = supabase.table("bot_hafiza").select("*").eq("id", 3).execute()
        if r.data:
            v = r.data[0]
            return {
                "aktif_pozisyonlar": v.get("aktif_pozisyonlar", {}),
                "aktif_gridler": v.get("aktif_gridler", {}),
                "analitik": v.get("analitik", ANALITIK.copy()),
                "coin_params": v.get("coin_params", {}),
            }
    except Exception:
        pass
    try:
        supabase.table("bot_hafiza").upsert({"id": 3, **varsayilan}).execute()
    except Exception:
        pass
    return varsayilan

def hafizayi_kaydet():
    try:
        supabase.table("bot_hafiza").upsert({
            "id": 3,
            "aktif_pozisyonlar": AKTIF_POZISYONLAR,
            "aktif_gridler": AKTIF_GRIDLER,
            "analitik": ANALITIK,
            "coin_params": COIN_PARAMS,
        }).execute()
    except Exception:
        pass

kalici = hafizayi_yukle()
AKTIF_POZISYONLAR = kalici["aktif_pozisyonlar"]
AKTIF_GRIDLER = kalici["aktif_gridler"]
ANALITIK = kalici["analitik"]
COIN_PARAMS = kalici.get("coin_params", {})

def coin_mod_al(symbol):
    base = dict(MOD)
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

# ==================== GÖSTERGELER ====================
def ema_hesapla(close, period):
    return close.ewm(span=period, adjust=False).mean()

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

# ==================== GENEL TREND TESPİTİ ====================
def genel_trend_tespit(symbol):
    """4h EMA50 vs EMA200 → LONG/SHORT/BELIRSIZ"""
    mod = coin_mod_al(symbol)
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, mod['zaman_trend'], limit=250)
        df = pd.DataFrame(ohlcv, columns=['timestamp','open','high','low','close','volume'])
        if len(df) < 200:
            return "BELIRSIZ"
        ema50 = ema_hesapla(df['close'], mod['trend_ema_hizli']).iloc[-1]
        ema200 = ema_hesapla(df['close'], mod['trend_ema_yavas']).iloc[-1]
        if pd.isna(ema50) or pd.isna(ema200):
            return "BELIRSIZ"
        fark = abs(ema50 - ema200) / ema200
        if fark < mod['trend_belirsiz_pct']:
            return "BELIRSIZ"
        return "LONG" if ema50 > ema200 else "SHORT"
    except Exception:
        return "BELIRSIZ"

# ==================== REJİM TESPİTİ ====================
def rejim_tespit(symbol):
    """15m ADX → TREND/YATAY/BELIRSIZ"""
    mod = coin_mod_al(symbol)
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, mod['zaman_rejim'], limit=50)
        df = pd.DataFrame(ohlcv, columns=['timestamp','open','high','low','close','volume'])
        if len(df) < 30:
            return "BELIRSIZ"
        adx = adx_hesapla(df, 14)
        if adx > mod['adx_trend_esik']:
            return "TREND"
        elif adx < mod['adx_yatay_esik']:
            return "YATAY"
        return "BELIRSIZ"
    except Exception:
        return "BELIRSIZ"

# ==================== HACİM FİLTRESİ ====================
def hacim_uygun_mu(symbol):
    mod = coin_mod_al(symbol)
    try:
        ticker = exchange.fetch_ticker(symbol)
        quote_volume = ticker.get('quoteVolume', 0) or 0
        return quote_volume >= mod['min_hacim_usdt'], quote_volume
    except Exception:
        return True, 0
# ==================== TREND BOTU SİNYALİ ====================
def trend_sinyal(symbol, genel_trend):
    """Genel trend yönünde sinyal üret. Ters yöne ASLA."""
    mod = coin_mod_al(symbol)
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, mod['zaman_sinyal'], limit=120)
        df = pd.DataFrame(ohlcv, columns=['timestamp','open','high','low','close','volume'])
        if len(df) < 60:
            return None, "veri yetersiz"

        close = df['close']
        fiyat = close.iloc[-1]
        ema9 = ema_hesapla(close, mod['ema_hizli']).iloc[-1]
        ema21 = ema_hesapla(close, mod['ema_orta']).iloc[-1]
        ema50 = ema_hesapla(close, mod['ema_yavas']).iloc[-1]
        rsi = rsi_hesapla(close, mod['rsi_period']).iloc[-1]
        atr = atr_hesapla(df, 14).iloc[-1]
        h_orani = hacim_orani(df, 20)
        if pd.isna(ema50) or pd.isna(rsi) or pd.isna(atr) or atr == 0:
            return None, "gösterge NaN"
        atr_pct = (atr / fiyat) * 100
        if not (mod['atr_min'] <= atr_pct <= mod['atr_max']):
            return None, f"ATR dışı"
        if h_orani < mod['hacim_esik_trend']:
            return None, "hacim düşük"

        # ===== LONG SİNYAL =====
        if genel_trend == "LONG":
            if ema9 > ema21 > ema50 and fiyat > ema21:
                if mod['rsi_long_min'] <= rsi <= mod['rsi_long_max']:
                    stop_pct = (atr_pct * mod['atr_stop_mult']) / 100.0
                    stop_pct = max(0.008, min(0.030, stop_pct))
                    stop = fiyat * (1 - stop_pct)
                    tp = fiyat * (1 + stop_pct * mod['risk_reward'])
                    return {"yon": "LONG", "giris": float(fiyat), "stop": float(stop),
                            "tp": float(tp), "stop_pct": stop_pct,
                            "tp_pct": stop_pct * mod['risk_reward'],
                            "atr": float(atr), "rsi": float(rsi)}, "OK"
        # ===== SHORT SİNYAL =====
        elif genel_trend == "SHORT":
            if ema9 < ema21 < ema50 and fiyat < ema21:
                if mod['rsi_short_min'] <= rsi <= mod['rsi_short_max']:
                    stop_pct = (atr_pct * mod['atr_stop_mult']) / 100.0
                    stop_pct = max(0.008, min(0.030, stop_pct))
                    stop = fiyat * (1 + stop_pct)
                    tp = fiyat * (1 - stop_pct * mod['risk_reward'])
                    return {"yon": "SHORT", "giris": float(fiyat), "stop": float(stop),
                            "tp": float(tp), "stop_pct": stop_pct,
                            "tp_pct": stop_pct * mod['risk_reward'],
                            "atr": float(atr), "rsi": float(rsi)}, "OK"

        return None, "koşullar uygun değil"
    except Exception as e:
        return None, str(e)[:30]

# ==================== GRID BOTU (TREND FİLTRELİ) ====================
def grid_olustur(symbol, genel_trend):
    """Yatay piyasada trend yönünde grid kur."""
    mod = coin_mod_al(symbol)
    try:
        uygun, hacim = hacim_uygun_mu(symbol)
        if not uygun:
            return None, f"hacim düşük"

        fiyat = exchange.fetch_ticker(symbol)['last']
        alt = fiyat * (1 - mod['grid_aralik_pct'])
        ust = fiyat * (1 + mod['grid_aralik_pct'])

        bal = exchange.fetch_balance()
        kasa = float(bal['total'].get('USDT', 0))
        if kasa < 10:
            return None, "yetersiz kasa"

        toplam_yatirim = kasa * mod['islem_riski_pct']
        adim = (ust - alt) / mod['grid_sayisi']
        miktar_per_grid = (toplam_yatirim / mod['grid_sayisi']) / fiyat

        if not set_leverage_and_margin_safely(symbol, mod['kaldirac']):
            return None, "kaldıraç hata"

        tum_emirleri_iptal_et(symbol)

        emirler = []
        # LONG trend → alt yarıya AL
        if genel_trend == "LONG":
            for i in range(mod['grid_sayisi']):
                seviye = alt + (adim * i)
                if seviye < fiyat * 0.998:
                    try:
                        miktar = float(exchange.amount_to_precision(symbol, miktar_per_grid))
                        if miktar <= 0: continue
                        emir = exchange.create_order(
                            symbol, 'limit', 'buy', miktar,
                            float(exchange.price_to_precision(symbol, seviye))
                        )
                        emirler.append({"tip": "AL", "fiyat": seviye, "miktar": miktar, "id": emir.get('id')})
                    except Exception: pass
        # SHORT trend → üst yarıya SAT (SHORT aç)
        elif genel_trend == "SHORT":
            for i in range(mod['grid_sayisi']):
                seviye = alt + (adim * (i + 1))
                if seviye > fiyat * 1.002:
                    try:
                        miktar = float(exchange.amount_to_precision(symbol, miktar_per_grid))
                        if miktar <= 0: continue
                        emir = exchange.create_order(
                            symbol, 'limit', 'sell', miktar,
                            float(exchange.price_to_precision(symbol, seviye))
                        )
                        emirler.append({"tip": "SAT", "fiyat": seviye, "miktar": miktar, "id": emir.get('id')})
                    except Exception: pass

        if len(emirler) == 0:
            return None, "0 emir"

        AKTIF_GRIDLER[symbol] = {
            "ust": ust, "alt": alt, "adim": adim,
            "genel_trend": genel_trend,
            "fiyat_baslangic": fiyat,
            "emir_sayisi": len(emirler),
            "acilis_zaman": int(time.time()*1000),
            "toplam_kar": 0.0
        }
        ANALITIK["grid_kurulum"] = ANALITIK.get("grid_kurulum", 0) + 1
        hafizayi_kaydet()

        telegram_mesaj_gonder(
            f"🎯 *GRID KURULDU* ({genel_trend})\n"
            f"📌 `{symbol[:12]}`\n"
            f"💰 `{fiyat}` | Aralık: `{alt:.4f}-{ust:.4f}`\n"
            f"📐 {len(emirler)} {emirler[0]['tip']} emri"
        )
        return True, "OK"
    except Exception as e:
        return None, str(e)[:50]

def grid_kapat(symbol, sebep):
    try:
        tum_emirleri_iptal_et(symbol)
        try:
            raw = exchange.fetch_positions([symbol])
            for p in raw:
                k = float(p.get('contracts', 0) or p.get('size', 0) or 0)
                if k > 0:
                    yon = str(p.get('side', '')).upper()
                    kapat = 'sell' if yon == 'LONG' else 'buy'
                    exchange.create_order(symbol, 'market', kapat, k, None, {'reduce_only': True})
        except Exception: pass

        if symbol in AKTIF_GRIDLER:
            del AKTIF_GRIDLER[symbol]
        hafizayi_kaydet()
        telegram_mesaj_gonder(f"🛑 *GRID KAPATILDI* → `{symbol[:12]}`\nSebep: {sebep}")
    except Exception as e:
        telegram_mesaj_gonder(f"⚠️ {e}")

# ==================== POZİSYON YÖNETİMİ (TRAILING + BREAKEVEN) ====================
def pozisyon_yonet(symbol, borsa_poz):
    """Açık pozisyonu takip et: trailing stop + breakeven."""
    mod = coin_mod_al(symbol)
    if symbol not in AKTIF_POZISYONLAR:
        return
    poz = AKTIF_POZISYONLAR[symbol]
    try:
        fiyat = exchange.fetch_ticker(symbol)['last']
        giris = poz['giris']
        yon = poz['yon']
        atr = poz.get('atr', 0)
        if atr <= 0:
            return

        # Kâr yüzdesi
        if yon == "LONG":
            pnl_pct = (fiyat - giris) / giris
        else:
            pnl_pct = (giris - fiyat) / giris

        # Breakeven
        if mod['breakeven_pct'] > 0 and not poz.get('breakeven', False):
            if pnl_pct >= mod['breakeven_pct']:
                yeni_stop = giris
                poz['stop'] = yeni_stop
                poz['breakeven'] = True
                AKTIF_POZISYONLAR[symbol] = poz
                # Emirleri güncelle
                tum_emirleri_iptal_et(symbol)
                miktar = poz.get('miktar', 0)
                if yon == "LONG":
                    try:
                        exchange.create_order(symbol, 'stop', 'sell', miktar, yeni_stop,
                            {'stopPrice': yeni_stop, 'triggerPrice': yeni_stop, 'reduceOnly': True})
                    except Exception: pass
                else:
                    try:
                        exchange.create_order(symbol, 'stop', 'buy', miktar, yeni_stop,
                            {'stopPrice': yeni_stop, 'triggerPrice': yeni_stop, 'reduceOnly': True})
                    except Exception: pass

        # Trailing stop
        if mod['trailing_kullan'] and mod['trailing_atr'] > 0:
            if yon == "LONG":
                yeni_stop = fiyat - atr * mod['trailing_atr']
                if yeni_stop > poz['stop']:
                    poz['stop'] = yeni_stop
                    AKTIF_POZISYONLAR[symbol] = poz
            else:
                yeni_stop = fiyat + atr * mod['trailing_atr']
                if yeni_stop < poz['stop']:
                    poz['stop'] = yeni_stop
                    AKTIF_POZISYONLAR[symbol] = poz
    except Exception:
        pass

# ==================== BACKTEST ====================
def backtest_trend(symbol, params, gun_sayisi=180):
    """Trend botu backtest — genel trend filtresi ile."""
    try:
        # 4h trend verisi
        limit_4h = min(gun_sayisi * 6, 1000)
        o4h = exchange.fetch_ohlcv(symbol, params['zaman_trend'], limit=limit_4h)
        df4h = pd.DataFrame(o4h, columns=['timestamp','open','high','low','close','volume'])
        # 1h sinyal
        o1h = exchange.fetch_ohlcv(symbol, params['zaman_sinyal'], limit=1000)
        df1h = pd.DataFrame(o1h, columns=['timestamp','open','high','low','close','volume'])
        if len(df4h) < 200 or len(df1h) < 200:
            return None

        trades = []
        poz = None
        for i in range(60, len(df1h)):
            ts = df1h['timestamp'].iloc[i]
            # 4h trend
            df4h_s = df4h[df4h['timestamp'] <= ts]
            if len(df4h_s) < 200:
                continue
            ema50 = ema_hesapla(df4h_s['close'], 50).iloc[-1]
            ema200 = ema_hesapla(df4h_s['close'], 200).iloc[-1]
            if pd.isna(ema50) or pd.isna(ema200):
                continue
            fark = abs(ema50 - ema200) / ema200
            if fark < params['trend_belirsiz_pct']:
                genel_trend = "BELIRSIZ"
            else:
                genel_trend = "LONG" if ema50 > ema200 else "SHORT"

            bar = df1h.iloc[i]
            high = bar['high']; low = bar['low']

            # Pozisyon yönetimi
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

            if genel_trend == "BELIRSIZ":
                continue

            # Sinyal
            df_slice = df1h.iloc[max(0, i-100):i+1].reset_index(drop=True)
            close = df_slice['close']
            fiyat = close.iloc[-1]
            ema9 = ema_hesapla(close, 9).iloc[-1]
            ema21 = ema_hesapla(close, 21).iloc[-1]
            ema50s = ema_hesapla(close, 50).iloc[-1]
            rsi = rsi_hesapla(close, 14).iloc[-1]
            atr = atr_hesapla(df_slice, 14).iloc[-1]
            if pd.isna(ema50s) or pd.isna(rsi) or pd.isna(atr) or atr == 0:
                continue
            atr_pct = (atr / fiyat) * 100
            if not (params['atr_min'] <= atr_pct <= params['atr_max']):
                continue

            if genel_trend == "LONG":
                if ema9 > ema21 > ema50s and fiyat > ema21 and params['rsi_long_min'] <= rsi <= params['rsi_long_max']:
                    stop_pct = (atr_pct * params['atr_stop_mult']) / 100.0
                    stop_pct = max(0.008, min(0.030, stop_pct))
                    stop = fiyat * (1 - stop_pct)
                    tp = fiyat * (1 + stop_pct * params['risk_reward'])
                    poz = {"yon": "LONG", "giris": float(fiyat), "stop": float(stop), "tp": float(tp)}
            elif genel_trend == "SHORT":
                if ema9 < ema21 < ema50s and fiyat < ema21 and params['rsi_short_min'] <= rsi <= params['rsi_short_max']:
                    stop_pct = (atr_pct * params['atr_stop_mult']) / 100.0
                    stop_pct = max(0.008, min(0.030, stop_pct))
                    stop = fiyat * (1 + stop_pct)
                    tp = fiyat * (1 - stop_pct * params['risk_reward'])
                    poz = {"yon": "SHORT", "giris": float(fiyat), "stop": float(stop), "tp": float(tp)}

        if not trades:
            return {"islem": 0, "win_rate": 0, "pf": 0, "max_dd": 0, "toplam": 0}
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
    except Exception as e:
        return {"islem": -1, "hata": str(e)[:40]}

def backtest_grid(symbol, params, gun_sayisi=180):
    """Grid backtest — basit simülasyon."""
    try:
        limit = min(gun_sayisi * 24 * 4, 1000)
        ohlcv = exchange.fetch_ohlcv(symbol, params['zaman_rejim'], limit=limit)
        df = pd.DataFrame(ohlcv, columns=['timestamp','open','high','low','close','volume'])
        if len(df) < 200:
            return None
        baslangic = 100
        fiyat_b = df['close'].iloc[baslangic]
        alt = fiyat_b * (1 - params['grid_aralik_pct'])
        ust = fiyat_b * (1 + params['grid_aralik_pct'])
        adim = (ust - alt) / params['grid_sayisi']
        seviyeler = [alt + adim * i for i in range(params['grid_sayisi'] + 1)]
        acik = []
        kazanclar = []
        for i in range(baslangic, len(df)):
            fiyat = df['close'].iloc[i]
            high = df['high'].iloc[i]
            low = df['low'].iloc[i]
            df_s = df.iloc[max(0, i-50):i+1]
            adx = adx_hesapla(df_s, 14)
            if adx > params['adx_trend_esik']:
                for (sf, mk) in acik:
                    kazanclar.append((fiyat - sf) / sf - KOMISYON_ORANI)
                acik = []
                fiyat_b = fiyat
                alt = fiyat_b * (1 - params['grid_aralik_pct'])
                ust = fiyat_b * (1 + params['grid_aralik_pct'])
                adim = (ust - alt) / params['grid_sayisi']
                seviyeler = [alt + adim * k for k in range(params['grid_sayisi'] + 1)]
                continue
            for sv in seviyeler:
                if low <= sv:
                    acik.append((sv, 1.0))
                    satis = sv * (1 + params['grid_kar_pct'])
                    if high >= satis:
                        kazanclar.append((satis - sv) / sv - KOMISYON_ORANI)
                        acik = [p for p in acik if p[0] != sv]
        if not kazanclar:
            return {"islem": 0, "win_rate": 0, "pf": 0, "max_dd": 0, "toplam": 0}
        k = np.array(kazanclar)
        kaz = k[k > 0]; kay = k[k < 0]
        win = len(kaz) / len(k) * 100 if len(k) else 0
        pf = abs(kaz.sum() / kay.sum()) if len(kay) and kay.sum() != 0 else 999
        equity = np.cumprod(1 + k)
        peak = np.maximum.accumulate(equity)
        dd = (equity - peak) / peak
        max_dd = dd.min() * 100 if len(dd) else 0
        return {"islem": len(kazanclar), "win_rate": round(win, 2),
                "pf": round(pf, 3) if pf != 999 else 999,
                "max_dd": round(max_dd, 2),
                "toplam": round((equity[-1] - 1) * 100, 2) if len(equity) else 0}
    except Exception:
        return None
# ==================== OPTİMİZASYON ====================
def optimize_coin(symbol, gun_sayisi=180):
    """Trend botu için optimize (grid optimize ayrı)."""
    base_mod = coin_mod_al(symbol)

    # Veri bir kez çek
    try:
        o4h = exchange.fetch_ohlcv(symbol, base_mod['zaman_trend'], limit=min(gun_sayisi*6, 1000))
        df4h = pd.DataFrame(o4h, columns=['timestamp','open','high','low','close','volume'])
        o1h = exchange.fetch_ohlcv(symbol, base_mod['zaman_sinyal'], limit=1000)
        df1h = pd.DataFrame(o1h, columns=['timestamp','open','high','low','close','volume'])
    except Exception:
        return None
    if len(df4h) < 200 or len(df1h) < 200:
        return None

    rl_long_list = [(40, 60), (45, 65), (50, 70)]
    rl_short_list = [(30, 50), (35, 55), (40, 60)]
    asm_list = [1.2, 1.5, 1.8]
    rr_list = [1.5, 2.0, 2.5]

    en_iyi = None
    for (rlm, rlx) in rl_long_list:
        for asm in asm_list:
            for rr in rr_list:
                params = dict(base_mod)
                params['rsi_long_min'] = rlm
                params['rsi_long_max'] = rlx
                params['atr_stop_mult'] = asm
                params['risk_reward'] = rr

                r = _backtest_trend_with_data(df4h, df1h, params)
                if r and r.get('islem', 0) >= 5:
                    if en_iyi is None or r['pf'] > en_iyi['pf']:
                        en_iyi = {
                            "params": {
                                "rsi_long_min": rlm, "rsi_long_max": rlx,
                                "rsi_short_min": 100-rlx, "rsi_short_max": 100-rlm,
                                "atr_stop_mult": asm,
                                "risk_reward": rr,
                                "zaman_trend": base_mod['zaman_trend'],
                                "zaman_rejim": base_mod['zaman_rejim'],
                                "zaman_sinyal": base_mod['zaman_sinyal'],
                                "trend_ema_hizli": base_mod['trend_ema_hizli'],
                                "trend_ema_yavas": base_mod['trend_ema_yavas'],
                                "trend_belirsiz_pct": base_mod['trend_belirsiz_pct'],
                                "adx_trend_esik": base_mod['adx_trend_esik'],
                                "adx_yatay_esik": base_mod['adx_yatay_esik'],
                                "ema_hizli": base_mod['ema_hizli'],
                                "ema_orta": base_mod['ema_orta'],
                                "ema_yavas": base_mod['ema_yavas'],
                                "rsi_period": base_mod['rsi_period'],
                                "hacim_esik_trend": base_mod['hacim_esik_trend'],
                                "atr_min": base_mod['atr_min'],
                                "atr_max": base_mod['atr_max'],
                                "trailing_atr": base_mod['trailing_atr'],
                                "grid_aralik_pct": base_mod['grid_aralik_pct'],
                                "grid_sayisi": base_mod['grid_sayisi'],
                                "grid_kar_pct": base_mod['grid_kar_pct'],
                                "min_hacim_usdt": base_mod['min_hacim_usdt'],
                                "islem_riski_pct": base_mod['islem_riski_pct'],
                                "kaldirac": base_mod['kaldirac'],
                                "maks_pozisyon": base_mod['maks_pozisyon'],
                                "gunluk_max_kayip_pct": base_mod['gunluk_max_kayip_pct'],
                                "gunluk_max_kar_pct": base_mod['gunluk_max_kar_pct'],
                                "cooldown_dk": base_mod['cooldown_dk'],
                                "trailing_kullan": base_mod['trailing_kullan'],
                                "breakeven_pct": base_mod['breakeven_pct'],
                                "aciklama": base_mod['aciklama'],
                            },
                            "pf": r['pf'], "win_rate": r['win_rate'],
                            "toplam": r['toplam'], "max_dd": r['max_dd'],
                            "islem": r['islem']
                        }
    return en_iyi

def _backtest_trend_with_data(df4h, df1h, params):
    """Veri hazır trend backtest — optimize için."""
    try:
        trades = []
        poz = None
        for i in range(60, len(df1h)):
            ts = df1h['timestamp'].iloc[i]
            df4h_s = df4h[df4h['timestamp'] <= ts]
            if len(df4h_s) < 200:
                continue
            ema50 = ema_hesapla(df4h_s['close'], 50).iloc[-1]
            ema200 = ema_hesapla(df4h_s['close'], 200).iloc[-1]
            if pd.isna(ema50) or pd.isna(ema200):
                continue
            fark = abs(ema50 - ema200) / ema200
            if fark < params['trend_belirsiz_pct']:
                genel_trend = "BELIRSIZ"
            else:
                genel_trend = "LONG" if ema50 > ema200 else "SHORT"

            bar = df1h.iloc[i]
            high = bar['high']; low = bar['low']

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

            if genel_trend == "BELIRSIZ":
                continue

            df_s = df1h.iloc[max(0, i-100):i+1].reset_index(drop=True)
            close = df_s['close']
            fiyat = close.iloc[-1]
            ema9 = ema_hesapla(close, params['ema_hizli']).iloc[-1]
            ema21 = ema_hesapla(close, params['ema_orta']).iloc[-1]
            ema50s = ema_hesapla(close, params['ema_yavas']).iloc[-1]
            rsi = rsi_hesapla(close, params['rsi_period']).iloc[-1]
            atr = atr_hesapla(df_s, 14).iloc[-1]
            if pd.isna(ema50s) or pd.isna(rsi) or pd.isna(atr) or atr == 0:
                continue
            atr_pct = (atr / fiyat) * 100
            if not (params['atr_min'] <= atr_pct <= params['atr_max']):
                continue

            if genel_trend == "LONG":
                if ema9 > ema21 > ema50s and fiyat > ema21 and params['rsi_long_min'] <= rsi <= params['rsi_long_max']:
                    stop_pct = max(0.008, min(0.030, (atr_pct * params['atr_stop_mult']) / 100.0))
                    poz = {"yon": "LONG", "giris": float(fiyat),
                           "stop": float(fiyat * (1 - stop_pct)),
                           "tp": float(fiyat * (1 + stop_pct * params['risk_reward']))}
            elif genel_trend == "SHORT":
                if ema9 < ema21 < ema50s and fiyat < ema21 and params['rsi_short_min'] <= rsi <= params['rsi_short_max']:
                    stop_pct = max(0.008, min(0.030, (atr_pct * params['atr_stop_mult']) / 100.0))
                    poz = {"yon": "SHORT", "giris": float(fiyat),
                           "stop": float(fiyat * (1 + stop_pct)),
                           "tp": float(fiyat * (1 - stop_pct * params['risk_reward']))}

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

# ==================== FLASK ====================
@app.route('/')
def home():
    return f"Rejim Botu | Poz: {len(AKTIF_POZISYONLAR)} | Grid: {len(AKTIF_GRIDLER)}"

# ==================== TELEGRAM ====================
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

        # Genel trend durumları
        trend_detay = "\n🌐 *Genel Trend:*\n"
        for sym in TAKIP_EDILENLER:
            gt = GLOBAL_TREND.get(sym, "?")
            rej = "?"
            if sym in AKTIF_GRIDLER:
                rej = "YATAY (Grid)"
            elif sym in AKTIF_POZISYONLAR:
                rej = "TREND"
            trend_detay += f"• `{sym[:10]}` → `{gt}` | {rej}\n"

        bs = ANALITIK.get("basarili_islem_sayisi", 0)
        bz = ANALITIK.get("basarisiz_islem_sayisi", 0)
        tot = bs + bz
        oran = (bs / tot * 100) if tot else 0

        mesaj = (
            f"🔥 *AGRESSIF REJİM BOTU*\n\n"
            f"💰 Toplam: `{total:.2f}` USDT (Serbest: `{free:.2f}`)\n"
            f"📈 PnL: `{pnl:+.2f}` USDT\n"
            f"📌 Pozisyon: `{len(poslari)}/{MOD['maks_pozisyon']}`\n"
            f"🎯 Grid: `{len(AKTIF_GRIDLER)}`\n"
            f"🧠 Optimize: `{len(COIN_PARAMS)}/4`\n"
            f"✅ TP: `{bs}` | ❌ Stop: `{bz}` | Başarı: `%{oran:.1f}`\n"
            f"{trend_detay}"
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
        AKTIF_GRIDLER.clear()
        hafizayi_kaydet()
        await update.message.reply_text("✅ *Kapatıldı.*", parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"⚠️ {e}", parse_mode='Markdown')

async def optimize_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global COIN_PARAMS, OPTIMIZE_CALISIYOR
    if OPTIMIZE_CALISIYOR:
        await update.message.reply_text("⏳ Zaten çalışıyor.")
        return
    OPTIMIZE_CALISIYOR = True
    await update.message.reply_text(
        f"🧠 *Optimize başladı*\n{len(TAKIP_EDILENLER)} coin × 27 kombinasyon\n20-30 dk",
        parse_mode='Markdown')

    def run():
        global COIN_PARAMS, OPTIMIZE_CALISIYOR
        try:
            telegram_mesaj_gonder("🔬 *Optimize başladı...*")
            for c in TAKIP_EDILENLER:
                en_iyi = optimize_coin(c, gun_sayisi=180)
                if en_iyi is None:
                    telegram_mesaj_gonder(f"⚠️ `{c[:12]}` — sinyal yok")
                    continue
                COIN_PARAMS[c] = en_iyi['params']
                hafizayi_kaydet()
                p = en_iyi['params']
                telegram_mesaj_gonder(
                    f"🏆 *{c[:12]}*\n"
                    f"RSI L:`{p['rsi_long_min']}-{p['rsi_long_max']}` | "
                    f"ATR×`{p['atr_stop_mult']}` | R/R:`{p['risk_reward']}`\n"
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
    await update.message.reply_text("⏳ *Backtest başladı* — 5-10 dk.", parse_mode='Markdown')

    def run():
        global BACKTEST_CALISIYOR
        try:
            satirlar = ["*📊 TREND BACKTEST*\n```"]
            satirlar.append(f"{'COIN':<14} {'İŞL':>4} {'WIN%':>6} {'PF':>6} {'DD%':>6} {'TOT%':>7}")
            satirlar.append("-" * 52)
            toplam = 0
            for c in TAKIP_EDILENLER:
                params = coin_mod_al(c)
                r = backtest_trend(c, params, gun_sayisi=180)
                if r is None:
                    satirlar.append(f"{c[:12]:<14} HATA")
                elif r.get('islem', -1) == -1:
                    satirlar.append(f"{c[:12]:<14} HATA")
                else:
                    satirlar.append(
                        f"{c[:12]:<14} {r['islem']:>4} {r['win_rate']:>6} {r['pf']:>6} {r['max_dd']:>6} {r['toplam']:>7}"
                    )
                    toplam += r['toplam']
            satirlar.append("-" * 52)
            ort = toplam / len(TAKIP_EDILENLER)
            satirlar.append(f"Ortalama: {ort:.2f}%")
            satirlar.append(f"Optimize: {len(COIN_PARAMS)}/4")
            satirlar.append("```")
            telegram_mesaj_gonder("\n".join(satirlar))
        except Exception as e:
            telegram_mesaj_gonder(f"❌ {e}")
        finally:
            BACKTEST_CALISIYOR = False

    threading.Thread(target=run, daemon=True).start()

# ==================== ANA DÖNGÜ ====================
def otomatik_arkaplan_tarayici():
    global BOT_CALISIYOR_MU, GLOBAL_TREND, ANALITIK
    print(f"🔥 [AGRESİF REJİM BOTU] Başladı", flush=True)
    try:
        exchange.load_markets()
    except Exception:
        pass

    dongu_sayaci = 0
    while True:
        try:
            if not BOT_CALISIYOR_MU:
                time.sleep(5); continue

            # Günlük limit
            gunluk = gunluk_kontrol()
            if gunluk is not None:
                if gunluk <= -MOD['gunluk_max_kayip_pct']:
                    telegram_mesaj_gonder(f"🛑 *Günlük zarar limiti!* (%{gunluk*100:.1f})")
                    BOT_CALISIYOR_MU = False
                    continue
                if gunluk >= MOD['gunluk_max_kar_pct']:
                    telegram_mesaj_gonder(f"✅ *Günlük kâr hedefi!* (%+{gunluk*100:.1f})")

            # Genel trendleri güncelle
            for symbol in TAKIP_EDILENLER:
                GLOBAL_TREND[symbol] = genel_trend_tespit(symbol)

            # Açık pozisyonları kontrol et
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
                    except Exception: pass
                    if basarili:
                        ANALITIK["basarili_islem_sayisi"] = ANALITIK.get("basarili_islem_sayisi", 0) + 1
                        telegram_mesaj_gonder(f"🎉 *Kâr* → `{sym[:12]}` 🟢")
                    else:
                        ANALITIK["basarisiz_islem_sayisi"] = ANALITIK.get("basarisiz_islem_sayisi", 0) + 1
                        telegram_mesaj_gonder(f"❌ *Stop* → `{sym[:12]}` 🔴")
                    hafizayi_kaydet()

            # Trailing + breakeven güncelle
            for sym, poz in list(AKTIF_POZISYONLAR.items()):
                if sym in aktif_borsa:
                    pozisyon_yonet(sym, aktif_borsa[sym])

            # Grid trend kontrolü (ADX > 25 ise grid kapat)
            for sym in list(AKTIF_GRIDLER.keys()):
                mod = coin_mod_al(sym)
                try:
                    ohlcv = exchange.fetch_ohlcv(sym, mod['zaman_rejim'], limit=50)
                    df = pd.DataFrame(ohlcv, columns=['timestamp','open','high','low','close','volume'])
                    adx = adx_hesapla(df, 14)
                    if adx > 25:
                        grid_kapat(sym, f"ADX {adx:.1f} > 25")
                        ANALITIK["trend_kapatma"] = ANALITIK.get("trend_kapatma", 0) + 1
                        continue
                except Exception:
                    continue
                # Genel trend değişirse grid kapat
                grid_gt = AKTIF_GRIDLER[sym].get("genel_trend", "LONG")
                if grid_gt != GLOBAL_TREND.get(sym, "BELIRSIZ"):
                    grid_kapat(sym, "Trend değişti")

            # Yeni pozisyon/grid aç
            for symbol in TAKIP_EDILENLER:
                if not BOT_CALISIYOR_MU:
                    break
                if len(aktif_borsa) >= MOD['maks_pozisyon']:
                    break
                if symbol in aktif_borsa:
                    continue
                # Hacim filtresi
                uygun, _ = hacim_uygun_mu(symbol)
                if not uygun:
                    continue

                genel_trend = GLOBAL_TREND.get(symbol, "BELIRSIZ")
                if genel_trend == "BELIRSIZ":
                    continue

                rejim = rejim_tespit(symbol)

                # TREND → trend botu
                if rejim == "TREND":
                    params = coin_mod_al(symbol)
                    sig, neden = trend_sinyal(symbol, genel_trend)
                    if sig:
                        # İşlem aç
                        bal = exchange.fetch_balance()
                        kasa = float(bal['total'].get('USDT', 0))
                        if kasa < 10:
                            continue
                        if not set_leverage_and_margin_safely(symbol, params['kaldirac']):
                            continue
                        risk_usdt = kasa * params['islem_riski_pct']
                        poz_degeri = risk_usdt / sig['stop_pct']
                        try:
                            market_info = exchange.market(symbol)
                            cs = float(market_info.get('contractSize', 1.0))
                            ham = poz_degeri / (sig['giris'] * cs)
                            miktar = float(exchange.amount_to_precision(symbol, max(ham, 0.001)))
                            if miktar <= 0:
                                continue
                        except Exception:
                            continue
                        try:
                            tum_emirleri_iptal_et(symbol)
                            emir = exchange.create_order(symbol, 'market',
                                                          'buy' if sig['yon'] == 'LONG' else 'sell',
                                                          miktar)
                            giris = float(emir.get('average') or emir.get('price') or sig['giris'])
                            time.sleep(0.5)
                            if sig['yon'] == 'LONG':
                                stop = giris * (1 - sig['stop_pct'])
                                tp = giris * (1 + sig['stop_pct'] * params['risk_reward'])
                                kapat_yon = 'sell'
                            else:
                                stop = giris * (1 + sig['stop_pct'])
                                tp = giris * (1 - sig['stop_pct'] * params['risk_reward'])
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
                                exchange.create_order(symbol, 'limit', kapat_yon, miktar, tp,
                                    {'reduceOnly': True})
                            except Exception: pass

                            AKTIF_POZISYONLAR[symbol] = {
                                "yon": sig['yon'], "giris": giris, "stop": stop,
                                "tp": tp, "miktar": miktar, "atr": sig['atr'],
                                "giris_zaman": int(time.time()*1000)
                            }
                            hafizayi_kaydet()
                            print(f"🎯 {symbol} | {sig['yon']} | Trend", flush=True)
                            telegram_mesaj_gonder(
                                f"🚀 *TREND İŞLEM* ({genel_trend})\n"
                                f"📌 `{symbol[:12]}` | *{sig['yon']}*\n"
                                f"💰 Giriş: `{giris}` | SL: `{stop}` | TP: `{tp}`\n"
                                f"📊 RSI: `{sig['rsi']:.0f}`"
                            )
                            break
                        except Exception as e:
                            print(f"❌ {e}", flush=True)

                # YATAY → grid
                elif rejim == "YATAY":
                    if symbol not in AKTIF_GRIDLER:
                        ok, mesaj = grid_olustur(symbol, genel_trend)
                        if ok:
                            print(f"🎯 Grid: {symbol}", flush=True)

            dongu_sayaci += 1
            if dongu_sayaci % 20 == 0:
                ozet = " | ".join([f"{s.split('/')[0]}:{GLOBAL_TREND.get(s,'?')}" for s in TAKIP_EDILENLER])
                print(f"🔍 #{dongu_sayaci} | {ozet} | Poz:{len(AKTIF_POZISYONLAR)} Grid:{len(AKTIF_GRIDLER)}", flush=True)

        except Exception as e:
            print(f"⚠️ {e}", flush=True)
        time.sleep(20)

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
    app_tg.add_handler(CommandHandler("optimize", optimize_komutu))
    app_tg.add_handler(CommandHandler("backtest", backtest_komutu))

    app_tg.run_polling(drop_pending_updates=True)

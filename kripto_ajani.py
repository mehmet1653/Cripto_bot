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

# ==================== 6 KANITLANMIŞ COİN ====================
TAKIP_EDILENLER = [
    'ETH/USDT:USDT', 'SOL/USDT:USDT', 'XRP/USDT:USDT',
    'ADA/USDT:USDT', 'DOGE/USDT:USDT', 'AVAX/USDT:USDT'
]

SEKTOR_MAP = {
    'ETH/USDT:USDT': 'ETH',
    'SOL/USDT:USDT': 'L1', 'ADA/USDT:USDT': 'L1', 'AVAX/USDT:USDT': 'L1',
    'XRP/USDT:USDT': 'PAYMENT',
    'DOGE/USDT:USDT': 'MEME'
}

KOMISYON_ORANI = 0.001

# ==================== MOD TANIMLARI ====================
MODLAR = {
    "muhafazakar": {
        "aciklama": "🟢 Muhafazakar - Az işlem, düşük risk",
        "zaman_dilimi": "1h",
        "bollinger_period": 20, "bollinger_std": 2.0,
        "rsi_period": 14, "rsi_long": 32, "rsi_short": 68,
        "atr_stop_mult": 2.0, "risk_reward": 2.0, "hacim_esik": 1.0,
        "islem_riski_pct": 0.01, "kaldirac": 5, "maks_pozisyon": 3,
        "cooldown_dk": 15, "gunluk_max_kayip_pct": 0.03,
    },
    "dengeli": {
        "aciklama": "🟡 Dengeli - Orta işlem, orta risk",
        "zaman_dilimi": "1h",
        "bollinger_period": 20, "bollinger_std": 1.8,
        "rsi_period": 14, "rsi_long": 38, "rsi_short": 62,
        "atr_stop_mult": 1.5, "risk_reward": 2.0, "hacim_esik": 0.7,
        "islem_riski_pct": 0.015, "kaldirac": 7, "maks_pozisyon": 4,
        "cooldown_dk": 8, "gunluk_max_kayip_pct": 0.05,
    },
    "agresif": {
        "aciklama": "🟠 Agresif - Çok işlem, yüksek risk",
        "zaman_dilimi": "1h",
        "bollinger_period": 20, "bollinger_std": 1.6,
        "rsi_period": 14, "rsi_long": 42, "rsi_short": 58,
        "atr_stop_mult": 1.3, "risk_reward": 2.0, "hacim_esik": 0.5,
        "islem_riski_pct": 0.02, "kaldirac": 10, "maks_pozisyon": 5,
        "cooldown_dk": 5, "gunluk_max_kayip_pct": 0.08,
    },
    "agresif100": {
        "aciklama": "🔥 Agresif100 - 6 coin, %3 risk, 10x kaldıraç",
        "zaman_dilimi": "1h",
        "bollinger_period": 20, "bollinger_std": 1.7,
        "rsi_period": 14, "rsi_long": 38, "rsi_short": 62,
        "atr_stop_mult": 1.5, "risk_reward": 2.0, "hacim_esik": 0.6,
        "islem_riski_pct": 0.03, "kaldirac": 10, "maks_pozisyon": 3,
        "cooldown_dk": 10, "gunluk_max_kayip_pct": 0.10,
    },
    "yildirim": {
        "aciklama": "🔴 Yıldırım - ÇILGIN mod (kumar)",
        "zaman_dilimi": "15m",
        "bollinger_period": 20, "bollinger_std": 1.4,
        "rsi_period": 14, "rsi_long": 45, "rsi_short": 55,
        "atr_stop_mult": 1.2, "risk_reward": 1.8, "hacim_esik": 0.3,
        "islem_riski_pct": 0.03, "kaldirac": 15, "maks_pozisyon": 6,
        "cooldown_dk": 3, "gunluk_max_kayip_pct": 0.12,
    },
}

BOT_CALISIYOR_MU = True
GUN_BASI_KASA = None
GUN_BASI_TARIH = None
BACKTEST_CALISIYOR = False
OPTIMIZE_CALISIYOR = False

# ==================== SUPABASE ====================
def hafizayi_yukle():
    varsayilan = {
        "aktif_sistemler": {},
        "analitik": {"basarili_islem_sayisi": 0, "basarisiz_islem_sayisi": 0},
        "cooldownlar": {}, "coin_params": {}, "aktif_mod": "agresif100"
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
                "aktif_mod": v.get("aktif_mod", "agresif100")
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
# Sadece bu coinler optimize edilmişse kabul et
COIN_PARAMS = {k: v for k, v in kalici.get("coin_params", {}).items() if k in TAKIP_EDILENLER}
AKTIF_MOD = kalici.get("aktif_mod", "agresif100")

def mod_al():
    return MODLAR.get(AKTIF_MOD, MODLAR["agresif100"])

def coin_parametre_al(symbol):
    base = dict(mod_al())
    if symbol in COIN_PARAMS:
        ov = COIN_PARAMS[symbol]
        base["bollinger_std"] = ov.get("bollinger_std", base["bollinger_std"])
        base["rsi_long"] = ov.get("rsi_long", base["rsi_long"])
        base["rsi_short"] = ov.get("rsi_short", base["rsi_short"])
        base["atr_stop_mult"] = ov.get("atr_stop_mult", base["atr_stop_mult"])
        base["risk_reward"] = ov.get("risk_reward", base["risk_reward"])
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

def aktif_sektorler():
    return set(SEKTOR_MAP.get(s, 'DIGER') for s in AKTIF_GRID_SISTEMLERI.keys())

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

def hacim_onay(df, period=20, esik=1.0):
    try:
        ort = df['volume'].rolling(period).mean().iloc[-1]
        return bool(df['volume'].iloc[-1] > ort * esik)
    except Exception:
        return False

# ==================== SAF MEAN REVERSION SİNYAL ====================
def sinyal_uret(df, params):
    """Saf Mean Reversion — trend yok, sadece aşırı uçlardan dönüş."""
    if len(df) < 50:
        return None, "veri yetersiz"

    close = df['close']; open_ = df['open']; high = df['high']; low = df['low']

    sma, ust, alt = bollinger_hesapla(close, params['bollinger_period'], params['bollinger_std'])
    rsi = rsi_hesapla(close, params['rsi_period'])
    atr = atr_hesapla(df, 14)

    son_fiyat = close.iloc[-1]
    son_open = open_.iloc[-1]
    son_ust = ust.iloc[-1]
    son_alt = alt.iloc[-1]
    son_rsi = rsi.iloc[-1]
    son_atr = atr.iloc[-1]

    if pd.isna(son_ust) or pd.isna(son_alt) or pd.isna(son_rsi) or pd.isna(son_atr) or son_atr == 0:
        return None, "gösterge NaN"

    atr_pct = (son_atr / son_fiyat) * 100
    if not (0.3 <= atr_pct <= 5.0):
        return None, f"ATR %{atr_pct:.2f} dışı"

    if not hacim_onay(df, 20, params['hacim_esik']):
        return None, "hacim yok"

    # LONG
    if son_fiyat <= son_alt and son_rsi < params['rsi_long'] and son_fiyat > son_open:
        stop_mesafe = son_atr * params['atr_stop_mult']
        stop = min(low.iloc[-1] - son_atr * 0.5, son_fiyat - stop_mesafe)
        stop_pct = max(0.008, min(0.035, (son_fiyat - stop) / son_fiyat))
        stop = son_fiyat * (1 - stop_pct)
        tp_pct = stop_pct * params['risk_reward'] + KOMISYON_ORANI
        tp = son_fiyat * (1 + tp_pct)
        return {"yon": "LONG", "giris": float(son_fiyat), "stop": float(stop), "tp": float(tp),
                "stop_pct": stop_pct, "tp_pct": tp_pct, "rsi": float(son_rsi), "atr_pct": atr_pct}, "OK"

    # SHORT
    if son_fiyat >= son_ust and son_rsi > params['rsi_short'] and son_fiyat < son_open:
        stop_mesafe = son_atr * params['atr_stop_mult']
        stop = max(high.iloc[-1] + son_atr * 0.5, son_fiyat + stop_mesafe)
        stop_pct = max(0.008, min(0.035, (stop - son_fiyat) / son_fiyat))
        stop = son_fiyat * (1 + stop_pct)
        tp_pct = stop_pct * params['risk_reward'] + KOMISYON_ORANI
        tp = son_fiyat * (1 - tp_pct)
        return {"yon": "SHORT", "giris": float(son_fiyat), "stop": float(stop), "tp": float(tp),
                "stop_pct": stop_pct, "tp_pct": tp_pct, "rsi": float(son_rsi), "atr_pct": atr_pct}, "OK"

    if son_fiyat > son_alt and son_fiyat < son_ust:
        return None, "fiyat bantlar arasında"
    if params['rsi_long'] <= son_rsi <= params['rsi_short']:
        return None, f"RSI {son_rsi:.1f} nötr"
    return None, "koşullar uygun değil"

# ==================== BACKTEST ====================
def backtest_coin_params(symbol, params, gun_sayisi=365):
    try:
        limit = min(gun_sayisi * 24, 1000) if params['zaman_dilimi'] == '1h' else 1000
        ohlcv = exchange.fetch_ohlcv(symbol, params['zaman_dilimi'], limit=limit)
        df = pd.DataFrame(ohlcv, columns=['timestamp','open','high','low','close','volume'])
        if len(df) < 100:
            return None
        trades = []
        pozisyon = None
        for i in range(50, len(df)):
            if pozisyon is not None:
                high = df['high'].iloc[i]; low = df['low'].iloc[i]
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
            df_slice = df.iloc[max(0, i-100):i+1].reset_index(drop=True)
            try:
                sig, _ = sinyal_uret(df_slice, params)
            except Exception:
                continue
            if sig:
                pozisyon = {**sig}
        if not trades:
            return {"symbol": symbol, "islem": 0, "win_rate": 0, "pf": 0, "ev": 0, "max_dd": 0, "toplam": 0}
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
                "pf": round(pf, 3) if pf != 999 else 999, "ev": round(k.mean() * 100, 4),
                "max_dd": round(max_dd, 2), "toplam": round((equity[-1] - 1) * 100, 2) if len(equity) else 0}
    except Exception as e:
        return {"symbol": symbol, "islem": -1, "hata": str(e)[:40]}

# ==================== OPTİMİZASYON ====================
def backtest_with_data(symbol, df, params):
    try:
        trades = []
        pozisyon = None
        for i in range(50, len(df)):
            if pozisyon is not None:
                high = df['high'].iloc[i]; low = df['low'].iloc[i]
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
            df_slice = df.iloc[max(0, i-100):i+1].reset_index(drop=True)
            try:
                sig, _ = sinyal_uret(df_slice, params)
            except Exception:
                continue
            if sig:
                pozisyon = {**sig}
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
                "pf": round(pf, 3) if pf != 999 else 999, "ev": round(k.mean() * 100, 4),
                "max_dd": round(max_dd, 2), "toplam": round((equity[-1] - 1) * 100, 2) if len(equity) else 0}
    except Exception:
        return None

def optimize_coin(symbol, gun_sayisi=365):
    base_mod = mod_al()
    bstd_list = [1.5, 2.0, 2.5]
    rl_list = [28, 32, 36]
    asm_list = [1.2, 1.5, 2.0]
    rr_list = [1.5, 2.0, 2.5]
    limit = min(gun_sayisi * 24, 1000) if base_mod['zaman_dilimi'] == '1h' else 1000
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, base_mod['zaman_dilimi'], limit=limit)
        df = pd.DataFrame(ohlcv, columns=['timestamp','open','high','low','close','volume'])
    except Exception:
        return None
    if len(df) < 100:
        return None
    en_iyi = None
    for bstd in bstd_list:
        for rl in rl_list:
            for asm in asm_list:
                for rr in rr_list:
                    params = dict(base_mod)
                    params['bollinger_std'] = bstd
                    params['rsi_long'] = rl
                    params['rsi_short'] = 100 - rl
                    params['atr_stop_mult'] = asm
                    params['risk_reward'] = rr
                    r = backtest_with_data(symbol, df, params)
                    if r and r['islem'] >= 5:
                        if en_iyi is None or r['pf'] > en_iyi['pf']:
                            en_iyi = {"params": {"bollinger_std": bstd, "rsi_long": rl,
                                                "rsi_short": 100 - rl, "atr_stop_mult": asm,
                                                "risk_reward": rr},
                                     "pf": r['pf'], "win_rate": r['win_rate'],
                                     "toplam": r['toplam'], "ev": r['ev'],
                                     "max_dd": r['max_dd'], "islem": r['islem']}
    return en_iyi

# ==================== FLASK ====================
@app.route('/')
def home():
    return f"Bot Aktif | Mod: {AKTIF_MOD} | Coin: {len(TAKIP_EDILENLER)} | Optimize: {len(COIN_PARAMS)} | Poz: {len(AKTIF_GRID_SISTEMLERI)}"

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
            f"⚙️ Zaman: `{mod['zaman_dilimi']}` | Kaldıraç: `{mod['kaldirac']}x`\n"
            f"{detay}\n"
            f"✅ TP: `{bs}` | ❌ Stop: `{bz}`\n"
            f"📈 Başarı: `%{oran:.1f}`\n"
        )
        await update.message.reply_text(mesaj, parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"Hata: {e}")

async def baslat_komutu(update, context):
    global BOT_CALISIYOR_MU
    BOT_CALISIYOR_MU = True
    await update.message.reply_text("🎯 *Bot Aktif!*", parse_mode='Markdown')

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
        await update.message.reply_text(f"⚠️ Hafıza temizlendi: {e}", parse_mode='Markdown')

async def mod_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global AKTIF_MOD
    args = context.args
    if not args:
        satirlar = ["🎚️ *Mod Seçenekleri:*\n"]
        for k, v in MODLAR.items():
            isaret = "▶️" if k == AKTIF_MOD else "  "
            satirlar.append(f"{isaret} `{k}` → {v['aciklama']}")
        satirlar.append("\nKullanım: `/mod agresif100`")
        await update.message.reply_text("\n".join(satirlar), parse_mode='Markdown')
        return
    yeni_mod = args[0].lower()
    if yeni_mod not in MODLAR:
        await update.message.reply_text(f"❌ Geçersiz mod. Seçenekler: {', '.join(MODLAR.keys())}")
        return
    AKTIF_MOD = yeni_mod
    hafizayi_kaydet()
    mod = MODLAR[yeni_mod]
    await update.message.reply_text(
        f"✅ Mod değiştirildi: *{mod['aciklama']}*\n\n"
        f"⚙️ Zaman: `{mod['zaman_dilimi']}`\n"
        f"🎚️ RSI: `{mod['rsi_long']}/{mod['rsi_short']}`\n"
        f"📊 STD: `{mod['bollinger_std']}` | ATR×`{mod['atr_stop_mult']}`\n"
        f"💪 Risk: `%{mod['islem_riski_pct']*100}` | Kaldıraç: `{mod['kaldirac']}x`\n"
        f"📌 Maks poz: `{mod['maks_pozisyon']}`\n"
        f"🧠 Optimize edilmiş coin: `{len(COIN_PARAMS)}/{len(TAKIP_EDILENLER)}`\n\n"
        f"_Bir sonraki döngüde devreye girecek._",
        parse_mode='Markdown')

async def backtest_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BACKTEST_CALISIYOR
    if BACKTEST_CALISIYOR or OPTIMIZE_CALISIYOR:
        await update.message.reply_text("⏳ Zaten çalışıyor.")
        return
    args = context.args
    test_modu = args[0].lower() if args else AKTIF_MOD
    if test_modu not in MODLAR:
        await update.message.reply_text(f"❌ Geçersiz mod. Seçenekler: {', '.join(MODLAR.keys())}")
        return
    BACKTEST_CALISIYOR = True
    mod = MODLAR[test_modu]
    await update.message.reply_text(
        f"⏳ *Backtest başlatıldı*\nMod: {mod['aciklama']}\n365 gün / {mod['zaman_dilimi']}\n{len(TAKIP_EDILENLER)} coin",
        parse_mode='Markdown')

    def run():
        global BACKTEST_CALISIYOR
        try:
            satirlar = [f"*📊 BACKTEST — {mod['aciklama']}*\n```"]
            satirlar.append(f"{'COIN':<14} {'İŞL':>4} {'WIN%':>6} {'PF':>6} {'EV%':>7} {'DD%':>6} {'TOT%':>7}")
            satirlar.append("-" * 58)
            toplam = 0
            for c in TAKIP_EDILENLER:
                params = coin_parametre_al(c)
                r = backtest_coin_params(c, params, gun_sayisi=365)
                if r is None:
                    satirlar.append(f"{c[:12]:<14} HATA")
                elif r.get('islem', -1) == -1:
                    satirlar.append(f"{c[:12]:<14} HATA: {r.get('hata','?')[:24]}")
                else:
                    satirlar.append(
                        f"{c[:12]:<14} {r['islem']:>4} {r['win_rate']:>6} {r['pf']:>6} {r['ev']:>7} {r['max_dd']:>6} {r['toplam']:>7}"
                    )
                    toplam += r['toplam']
            satirlar.append("-" * 58)
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
        f"{len(TAKIP_EDILENLER)} coin × 81 kombinasyon.\n"
        f"Aktif mod: `{AKTIF_MOD}`\n"
        f"Tahmini süre: 15-20 dakika.",
        parse_mode='Markdown')

    def run():
        global OPTIMIZE_CALISIYOR, COIN_PARAMS
        try:
            telegram_mesaj_gonder("🔬 *Optimizasyon başladı...*")
            for c in TAKIP_EDILENLER:
                en_iyi = optimize_coin(c, gun_sayisi=365)
                if en_iyi is None:
                    telegram_mesaj_gonder(f"⚠️ `{c}` — yeterli sinyal yok.")
                    continue
                COIN_PARAMS[c] = en_iyi['params']
                hafizayi_kaydet()
                p = en_iyi['params']
                mesaj = (
                    f"🏆 *{c}* — EN İYİ\n"
                    f"STD: `{p['bollinger_std']}` | RSI: `{p['rsi_long']}/{p['rsi_short']}` | "
                    f"ATR×`{p['atr_stop_mult']}` | R/R: `{p['risk_reward']}`\n"
                    f"📊 İŞL: `{en_iyi['islem']}` | WIN: `{en_iyi['win_rate']}%` | "
                    f"PF: `{en_iyi['pf']}` | TOT: `{en_iyi['toplam']}%`"
                )
                telegram_mesaj_gonder(mesaj)
                time.sleep(1)
            telegram_mesaj_gonder(
                f"✅ *Optimizasyon tamamlandı!*\n"
                f"Optimize edilen: `{len(COIN_PARAMS)}/{len(TAKIP_EDILENLER)}`\n"
                f"Şimdi `/backtest` yaz."
            )
        except Exception as e:
            telegram_mesaj_gonder(f"❌ Optimizasyon hatası: {e}")
        finally:
            OPTIMIZE_CALISIYOR = False

    threading.Thread(target=run, daemon=True).start()

async def params_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not COIN_PARAMS:
        await update.message.reply_text("Henüz optimize yok. `/optimize` yaz.", parse_mode='Markdown')
        return
    satirlar = ["🧠 *Optimize Edilmiş Parametreler:*\n"]
    for sym, p in COIN_PARAMS.items():
        satirlar.append(
            f"`{sym[:12]}`\n"
            f"  STD: {p['bollinger_std']} | RSI: {p['rsi_long']}/{p['rsi_short']} | "
            f"ATR×{p['atr_stop_mult']} | R/R: {p['risk_reward']}"
        )
    await update.message.reply_text("\n".join(satirlar), parse_mode='Markdown')

async def temizle_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global COIN_PARAMS
    COIN_PARAMS = {}
    hafizayi_kaydet()
    await update.message.reply_text("🗑️ Optimize parametreler silindi.", parse_mode='Markdown')

# ==================== ANA DÖNGÜ ====================
def otomatik_arkaplan_tarayici():
    global BOT_CALISIYOR_MU, ANALITIK_HAFIZA
    mod = mod_al()
    print(f"🎯 [BOT] Mod: {AKTIF_MOD} | Coin: {len(TAKIP_EDILENLER)} | Optimize: {len(COIN_PARAMS)}", flush=True)
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
            mod = mod_al()
            if gunluk_kayip_kontrol(mod):
                telegram_mesaj_gonder(f"🛑 *Günlük %{mod['gunluk_max_kayip_pct']*100:.0f} kayıp limiti aşıldı!*")
                BOT_CALISIYOR_MU = False
                time.sleep(3600)
                continue

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
                        gercek_pnl = 0.0
                        if closed:
                            son = sorted(closed, key=lambda x: x['timestamp'] or 0)[-2:]
                            for o in son:
                                gercek_pnl += float(o.get('info', {}).get('pnl', 0) or 0)
                        basarili = gercek_pnl > 0
                    except Exception:
                        pass
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
            mevcut_sektorler = aktif_sektorler()
            debug = []
            for symbol in TAKIP_EDILENLER:
                if not BOT_CALISIYOR_MU: break
                if symbol in aktif_borsa: continue
                if len(aktif_borsa) >= mod['maks_pozisyon']: break
                if su_an < COIN_COOLDOWNLAR.get(symbol, 0): continue
                if SEKTOR_MAP.get(symbol, 'DIGER') in mevcut_sektorler: continue
                try:
                    params = coin_parametre_al(symbol)
                    ohlcv = exchange.fetch_ohlcv(symbol, params['zaman_dilimi'], limit=120)
                    df = pd.DataFrame(ohlcv, columns=['timestamp','open','high','low','close','volume'])
                    sig, neden = sinyal_uret(df, params)
                    if sig:
                        sinyaller.append({"symbol": symbol, **sig})
                        debug.append(f"{symbol.split('/')[0]}:✅{sig['yon']}")
                    else:
                        debug.append(f"{symbol.split('/')[0]}:{neden[:12]}")
                except Exception:
                    continue

            dongu_sayaci += 1
            if dongu_sayaci % 40 == 0:
                ozet = " | ".join(debug[:6])
                print(f"🔍 #{dongu_sayaci} [{AKTIF_MOD}] opt:{len(COIN_PARAMS)} | {ozet}", flush=True)

            for s in sinyaller:
                if not BOT_CALISIYOR_MU: break
                if len(aktif_borsa) >= mod['maks_pozisyon']: break
                symbol = s["symbol"]; yon = s["yon"]; stop_pct = s["stop_pct"]
                try:
                    bal = exchange.fetch_balance()
                    kasa = float(bal['total'].get('USDT', 0))
                except Exception:
                    continue
                if kasa <= 0: continue
                if not set_leverage_and_margin_safely(symbol, mod['kaldirac']):
                    continue
                risk_usdt = kasa * mod['islem_riski_pct']
                poz_degeri = risk_usdt / stop_pct
                try:
                    market_info = exchange.market(symbol)
                    cs = float(market_info.get('contractSize', 1.0))
                    ham = poz_degeri / (s["giris"] * cs)
                    miktar = float(exchange.amount_to_precision(symbol, max(ham, 0.001)))
                    if miktar <= 0: continue
                except Exception:
                    continue
                try:
                    tum_emirleri_iptal_et(symbol)
                    emir = exchange.create_order(symbol, 'market', 'buy' if yon == 'LONG' else 'sell', miktar)
                    giris = float(emir.get('average') or emir.get('price') or s["giris"])
                    time.sleep(0.5)
                    if yon == 'LONG':
                        stop = giris * (1 - stop_pct)
                        tp = giris * (1 + s["tp_pct"])
                        kapat_yon = 'sell'
                    else:
                        stop = giris * (1 + stop_pct)
                        tp = giris * (1 - s["tp_pct"])
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
                    AKTIF_GRID_SISTEMLERI[symbol] = {
                        "yon": yon, "giris": giris, "stop": stop, "tp": tp, "miktar": miktar
                    }
                    hafizayi_kaydet()
                    print(f"🎯 {symbol} | {yon} | {miktar} | TP:{tp} | SL:{stop}", flush=True)
                    telegram_mesaj_gonder(
                        f"🎯 *İŞLEM AÇILDI* ({AKTIF_MOD})\n"
                        f"📌 `{symbol}` | *{yon}*\n"
                        f"💰 Giriş: `{giris}`\n"
                        f"🎯 TP: `{tp}` | 🛑 SL: `{stop}`\n"
                        f"📊 Risk: `{risk_usdt:.2f}` | RSI: `{s['rsi']:.1f}`"
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
    app_tg.add_handler(CommandHandler("baslat", baslat_komutu))
    app_tg.add_handler(CommandHandler("durdur", durdur_komutu))
    app_tg.add_handler(CommandHandler("kapat", kapat_komutu))
    app_tg.add_handler(CommandHandler("mod", mod_komutu))
    app_tg.add_handler(CommandHandler("backtest", backtest_komutu))
    app_tg.add_handler(CommandHandler("optimize", optimize_komutu))
    app_tg.add_handler(CommandHandler("params", params_komutu))
    app_tg.add_handler(CommandHandler("temizle", temizle_komutu))

    app_tg.run_polling(drop_pending_updates=True)

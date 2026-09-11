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

KOMISYON_ORANI = 0.001

# ==================== 6 COİN (En İyi Backtest) ====================
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

# ==================== RİSK ====================
RISK = {
    "islem_riski_pct": 0.03,      # %3 (Agresif100)
    "kaldirac": 10,               # 10x
    "maks_pozisyon": 3,
    "cooldown_dk": 10,
    "gunluk_max_kayip_pct": 0.10,
    "zaman_dilimi": "1h",
    "min_stop_pct": 0.008,
    "max_stop_pct": 0.030,
}

# ==================== MOD TANIMLARI ====================
MODLAR = {
    "muhafazakar": {
        "aciklama": "🟢 Muhafazakar",
        "islem_riski_pct": 0.01, "kaldirac": 5, "maks_pozisyon": 3,
        "cooldown_dk": 15, "gunluk_max_kayip_pct": 0.03,
    },
    "agresif100": {
        "aciklama": "🔥 Agresif100 (Backtest +%94)",
        "islem_riski_pct": 0.03, "kaldirac": 10, "maks_pozisyon": 3,
        "cooldown_dk": 10, "gunluk_max_kayip_pct": 0.10,
    },
}
AKTIF_MOD = "agresif100"

# ==================== DURUMLAR ====================
BOT_CALISIYOR_MU = True
GUN_BASI_KASA = None
GUN_BASI_TARIH = None
AKTIF_POZISYONLAR = {}
COIN_COOLDOWNLAR = {}
COIN_PARAMS = {}
ANALITIK = {
    "basarili_islem_sayisi": 0,
    "basarisiz_islem_sayisi": 0,
    "toplam_kar": 0.0,
}

def mod_al():
    return MODLAR.get(AKTIF_MOD, MODLAR["agresif100"])

# ==================== SUPABASE ====================
def hafizayi_yukle():
    varsayilan = {
        "aktif_pozisyonlar": {},
        "cooldownlar": {},
        "coin_params": {},
        "analitik": ANALITIK.copy(),
        "aktif_mod": "agresif100",
    }
    try:
        r = supabase.table("bot_hafiza").select("*").eq("id", 40).execute()
        if r.data:
            v = r.data[0]
            return {
                "aktif_pozisyonlar": v.get("aktif_pozisyonlar", {}),
                "cooldownlar": v.get("cooldownlar", {}),
                "coin_params": v.get("coin_params", {}),
                "analitik": v.get("analitik", ANALITIK.copy()),
                "aktif_mod": v.get("aktif_mod", "agresif100"),
            }
    except Exception:
        pass
    try:
        supabase.table("bot_hafiza").upsert({"id": 40, **varsayilan}).execute()
    except Exception:
        pass
    return varsayilan

def hafizayi_kaydet():
    try:
        supabase.table("bot_hafiza").upsert({
            "id": 40,
            "aktif_pozisyonlar": AKTIF_POZISYONLAR,
            "cooldownlar": COIN_COOLDOWNLAR,
            "coin_params": COIN_PARAMS,
            "analitik": ANALITIK,
            "aktif_mod": AKTIF_MOD,
        }).execute()
    except Exception:
        pass

kalici = hafizayi_yukle()
AKTIF_POZISYONLAR = kalici["aktif_pozisyonlar"]
COIN_COOLDOWNLAR = kalici["cooldownlar"]
COIN_PARAMS = kalici["coin_params"]
ANALITIK = kalici["analitik"]
AKTIF_MOD = kalici["aktif_mod"]

def coin_params_al(symbol):
    """Coin özel parametre varsa onu kullan, yoksa default."""
    default = {
        "bollinger_period": 20, "bollinger_std": 2.0,
        "rsi_period": 14, "rsi_long": 32, "rsi_short": 68,
        "atr_stop_mult": 1.5, "risk_reward": 2.0,
        "hacim_esik": 0.6,
    }
    if symbol in COIN_PARAMS:
        default.update(COIN_PARAMS[symbol])
    return default

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

def fetch_ohlcv_guvenli(symbol, timeframe, limit=200):
    for attempt in range(3):
        try:
            return exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
        except Exception:
            if attempt == 2:
                return None
            time.sleep(1)
    return None

def aktif_sektorler():
    return set(SEKTOR_MAP.get(s, 'DIGER') for s in AKTIF_POZISYONLAR.keys())

# ==================== GÖSTERGELER ====================
def rsi_hesapla(close, period=14):
    delta = close.diff()
    gain = delta.where(delta > 0, 0).rolling(period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))

def bollinger_hesapla(close, period=20, std=2.0):
    sma = close.rolling(period).mean()
    std_dev = close.rolling(period).std()
    return sma, sma + (std_dev * std), sma - (std_dev * std)

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

# ==================== SİNYAL ====================
def sinyal_uret(df, params):
    """Saf Mean Reversion."""
    if len(df) < 50:
        return None, "veri yetersiz"
    
    close = df['close']; open_ = df['open']
    high = df['high']; low = df['low']
    
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
        return None, "NaN"
    
    atr_pct = (son_atr / son_fiyat) * 100
    if not (0.3 <= atr_pct <= 5.0):
        return None, f"ATR dışı"
    
    if not hacim_onay(df, 20, params['hacim_esik']):
        return None, "hacim yok"
    
    # LONG
    if son_fiyat <= son_alt and son_rsi < params['rsi_long'] and son_fiyat > son_open:
        stop_pct = max(RISK['min_stop_pct'], min(RISK['max_stop_pct'],
                       (atr_pct * params['atr_stop_mult']) / 100.0))
        tp_pct = stop_pct * params['risk_reward'] + KOMISYON_ORANI
        return {
            "yon": "LONG", "giris": float(son_fiyat),
            "stop_pct": stop_pct, "tp_pct": tp_pct,
            "rsi": float(son_rsi), "atr_pct": atr_pct
        }, "OK"
    
    # SHORT
    if son_fiyat >= son_ust and son_rsi > params['rsi_short'] and son_fiyat < son_open:
        stop_pct = max(RISK['min_stop_pct'], min(RISK['max_stop_pct'],
                       (atr_pct * params['atr_stop_mult']) / 100.0))
        tp_pct = stop_pct * params['risk_reward'] + KOMISYON_ORANI
        return {
            "yon": "SHORT", "giris": float(son_fiyat),
            "stop_pct": stop_pct, "tp_pct": tp_pct,
            "rsi": float(son_rsi), "atr_pct": atr_pct
        }, "OK"
    
    return None, "koşullar"

# ==================== BACKTEST ====================
def backtest_coin(symbol, params):
    try:
        df_raw = fetch_ohlcv_guvenli(symbol, RISK['zaman_dilimi'], limit=1000)
        if df_raw is None or len(df_raw) < 200:
            return None
        df = pd.DataFrame(df_raw, columns=['timestamp','open','high','low','close','volume'])
        
        trades = []
        poz = None
        for i in range(60, len(df)):
            bar = df.iloc[i]
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
            
            df_slice = df.iloc[max(0, i-100):i+1].reset_index(drop=True)
            try:
                sig, _ = sinyal_uret(df_slice, params)
            except Exception:
                continue
            
            if sig:
                fiyat = sig['giris']
                if sig['yon'] == 'LONG':
                    stop = fiyat * (1 - sig['stop_pct'])
                    tp = fiyat * (1 + sig['tp_pct'])
                else:
                    stop = fiyat * (1 + sig['stop_pct'])
                    tp = fiyat * (1 - sig['tp_pct'])
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
            "islem": len(trades), "win_rate": round(win, 2),
            "pf": round(pf, 3) if pf != 999 else 999,
            "max_dd": round(max_dd, 2),
            "toplam": round((equity[-1] - 1) * 100, 2) if len(equity) else 0
        }
    except Exception:
        return None

# ==================== OPTİMİZASYON ====================
def optimize_coin(symbol, gun_sayisi=365):
    """81 kombinasyon grid search."""
    base_params = {
        "bollinger_period": 20, "rsi_period": 14,
        "hacim_esik": 0.6,
    }
    
    bstd_list = [1.5, 2.0, 2.5]
    rl_list = [28, 32, 36]
    asm_list = [1.2, 1.5, 2.0]
    rr_list = [1.5, 2.0, 2.5]
    
    try:
        df_raw = fetch_ohlcv_guvenli(symbol, RISK['zaman_dilimi'], limit=1000)
        if df_raw is None or len(df_raw) < 200:
            return None
        df = pd.DataFrame(df_raw, columns=['timestamp','open','high','low','close','volume'])
    except Exception:
        return None
    
    en_iyi = None
    for bstd in bstd_list:
        for rl in rl_list:
            for asm in asm_list:
                for rr in rr_list:
                    params = dict(base_params)
                    params['bollinger_std'] = bstd
                    params['rsi_long'] = rl
                    params['rsi_short'] = 100 - rl
                    params['atr_stop_mult'] = asm
                    params['risk_reward'] = rr
                    
                    # Hızlı backtest
                    trades = []
                    poz = None
                    for i in range(60, len(df)):
                        bar = df.iloc[i]
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
                        df_slice = df.iloc[max(0, i-100):i+1].reset_index(drop=True)
                        try:
                            sig, _ = sinyal_uret(df_slice, params)
                        except Exception:
                            continue
                        if sig:
                            fiyat = sig['giris']
                            if sig['yon'] == 'LONG':
                                stop = fiyat * (1 - sig['stop_pct']); tp = fiyat * (1 + sig['tp_pct'])
                            else:
                                stop = fiyat * (1 + sig['stop_pct']); tp = fiyat * (1 - sig['tp_pct'])
                            poz = {"yon": sig['yon'], "giris": float(fiyat), "stop": float(stop), "tp": float(tp)}
                    
                    if len(trades) < 5:
                        continue
                    
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
                    pf = abs(kaz.sum() / kay.sum()) if len(kay) and kay.sum() != 0 else 999
                    
                    if en_iyi is None or pf > en_iyi['pf']:
                        en_iyi = {
                            "params": {
                                "bollinger_std": bstd,
                                "rsi_long": rl,
                                "rsi_short": 100 - rl,
                                "atr_stop_mult": asm,
                                "risk_reward": rr,
                            },
                            "pf": round(pf, 3) if pf != 999 else 999,
                            "islem": len(trades),
                        }
    return en_iyi

# ==================== POZİSYON AÇMA ====================
def pozisyon_ac(symbol, sig):
    mod = mod_al()
    try:
        bal = exchange.fetch_balance()
        kasa = float(bal['total'].get('USDT', 0))
        if kasa < 10:
            return False
        if not set_leverage_and_margin_safely(symbol, mod['kaldirac']):
            return False

        risk_usdt = kasa * mod['islem_riski_pct']
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

        yon = sig['yon']
        tp_pct = sig['tp_pct']
        
        if yon == 'LONG':
            stop = giris * (1 - stop_pct); tp = giris * (1 + tp_pct); kapat_yon = 'sell'
        else:
            stop = giris * (1 + stop_pct); tp = giris * (1 - tp_pct); kapat_yon = 'buy'

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
            "miktar": miktar, "giris_zaman": int(time.time()*1000)
        }
        COIN_COOLDOWNLAR[symbol] = time.time() + mod['cooldown_dk'] * 60
        hafizayi_kaydet()

        telegram_mesaj_gonder(
            f"🎯 *İŞLEM AÇILDI* ({AKTIF_MOD})\n"
            f"📌 `{symbol[:12]}` | *{yon}*\n"
            f"💰 Giriş: `{giris}` | SL: `{stop}` | TP: `{tp}`\n"
            f"📊 RSI: `{sig['rsi']:.1f}` | Risk: `{risk_usdt:.2f}` USDT"
        )
        return True
    except Exception as e:
        print(f"❌ Pozisyon açma: {e}", flush=True)
        return False

# ==================== FLASK ====================
@app.route('/')
def home():
    return f"Klasik Bot | Mod: {AKTIF_MOD} | Poz: {len(AKTIF_POZISYONLAR)}"

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

        bs = ANALITIK.get("basarili_islem_sayisi", 0)
        bz = ANALITIK.get("basarisiz_islem_sayisi", 0)
        tot = bs + bz
        oran = (bs / tot * 100) if tot else 0

        detay = ""
        if poslari:
            detay = "\n📋 *Aktif:*\n"
            for p in poslari:
                detay += f"• `{p.get('symbol')[:12]}` | {str(p.get('side','')).upper()} | `{float(p.get('unrealizedPnl',0)):+.2f}`\n"

        opt = len(COIN_PARAMS)
        mesaj = (
            f"🎯 *KLASİK MEAN REVERSION*\n"
            f"_{MODLAR[AKTIF_MOD]['aciklama']}_\n\n"
            f"💰 Toplam: `{total:.2f}` USDT (Serbest: `{free:.2f}`)\n"
            f"📈 PnL: `{pnl:+.2f}` USDT\n"
            f"📌 Pozisyon: `{len(poslari)}/{mod_al()['maks_pozisyon']}`\n"
            f"🧠 Optimize: `{opt}/6`\n"
            f"⚙️ Zaman: `{RISK['zaman_dilimi']}` | Kaldıraç: `{mod_al()['kaldirac']}x`\n\n"
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

async def temizle_komutu(update, context):
    global AKTIF_POZISYONLAR, COIN_COOLDOWNLAR, COIN_PARAMS
    AKTIF_POZISYONLAR = {}
    COIN_COOLDOWNLAR = {}
    COIN_PARAMS = {}
    hafizayi_kaydet()
    await update.message.reply_text("🗑️ *Temizlendi.*", parse_mode='Markdown')

async def mod_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global AKTIF_MOD
    args = context.args
    if not args:
        satirlar = ["🎚️ *Modlar:*\n"]
        for k, v in MODLAR.items():
            isaret = "▶️" if k == AKTIF_MOD else "  "
            satirlar.append(f"{isaret} `{k}` → {v['aciklama']}")
        await update.message.reply_text("\n".join(satirlar), parse_mode='Markdown')
        return
    yeni = args[0].lower()
    if yeni not in MODLAR:
        await update.message.reply_text(f"❌ Geçersiz. Seçenekler: {', '.join(MODLAR.keys())}")
        return
    AKTIF_MOD = yeni
    hafizayi_kaydet()
    await update.message.reply_text(f"✅ *Mod: {MODLAR[yeni]['aciklama']}*", parse_mode='Markdown')

async def optimize_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global COIN_PARAMS
    await update.message.reply_text("🧠 *Optimize başladı* — 6 coin × 81 kombinasyon (~15 dk)", parse_mode='Markdown')
    
    def run():
        global COIN_PARAMS
        try:
            telegram_mesaj_gonder("🔬 *Optimize başladı...*")
            for c in TAKIP_EDILENLER:
                en_iyi = optimize_coin(c, gun_sayisi=180)
                if en_iyi:
                    COIN_PARAMS[c] = en_iyi['params']
                    hafizayi_kaydet()
                    p = en_iyi['params']
                    telegram_mesaj_gonder(
                        f"🏆 *{c[:12]}*\n"
                        f"STD:`{p['bollinger_std']}` RSI:`{p['rsi_long']}/{p['rsi_short']}` "
                        f"ATR×`{p['atr_stop_mult']}` R/R:`{p['risk_reward']}`\n"
                        f"PF:`{en_iyi['pf']}` İşl:`{en_iyi['islem']}`"
                    )
                time.sleep(1)
            telegram_mesaj_gonder(f"✅ *Optimize tamamlandı!* `/backtest` yaz.")
        except Exception as e:
            telegram_mesaj_gonder(f"❌ {e}")
    
    threading.Thread(target=run, daemon=True).start()

async def backtest_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ *Backtest başladı* — 30 sn", parse_mode='Markdown')
    
    def run():
        try:
            satirlar = [f"*📊 KLASİK BACKTEST (6 Coin)*\n```"]
            satirlar.append(f"{'COIN':<14} {'İŞL':>4} {'WIN%':>6} {'PF':>6} {'DD%':>6} {'TOT%':>7}")
            satirlar.append("-" * 56)
            toplam = 0
            for c in TAKIP_EDILENLER:
                params = coin_params_al(c)
                r = backtest_coin(c, params)
                if r is None:
                    satirlar.append(f"{c[:12]:<14} HATA")
                else:
                    satirlar.append(
                        f"{c[:12]:<14} {r['islem']:>4} {r['win_rate']:>6} {r['pf']:>6} {r['max_dd']:>6} {r['toplam']:>7}"
                    )
                    toplam += r['toplam']
            satirlar.append("-" * 56)
            ort = toplam / len(TAKIP_EDILENLER)
            satirlar.append(f"Ortalama: {ort:.2f}%")
            satirlar.append(f"Optimize: {len(COIN_PARAMS)}/6")
            satirlar.append("```")
            telegram_mesaj_gonder("\n".join(satirlar))
        except Exception as e:
            telegram_mesaj_gonder(f"❌ {e}")
    
    threading.Thread(target=run, daemon=True).start()

async def params_komutu(update, context):
    if not COIN_PARAMS:
        await update.message.reply_text("Henüz optimize yok.", parse_mode='Markdown')
        return
    satirlar = ["🧠 *Optimize Parametreleri:*\n"]
    for sym, p in COIN_PARAMS.items():
        satirlar.append(f"`{sym[:10]}` STD:{p['bollinger_std']} RSI:{p['rsi_long']}/{p['rsi_short']} ATR×{p['atr_stop_mult']} R/R:{p['risk_reward']}")
    await update.message.reply_text("\n".join(satirlar), parse_mode='Markdown')

# ==================== ANA DÖNGÜ ====================
def otomatik_arkaplan_tarayici():
    global BOT_CALISIYOR_MU, ANALITIK

    print(f"🎯 [KLASİK MEAN REVERSION] Başladı", flush=True)
    print(f"💰 {len(TAKIP_EDILENLER)} coin | Mod: {AKTIF_MOD}", flush=True)

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
            gunluk = gunluk_kontrol()
            if gunluk is not None and gunluk <= -mod['gunluk_max_kayip_pct']:
                telegram_mesaj_gonder(f"🛑 *Günlük zarar limiti!* (%{gunluk*100:.1f})")
                BOT_CALISIYOR_MU = False
                continue

            try:
                raw = exchange.fetch_positions()
                aktif_borsa = {p['symbol']: p for p in raw
                               if float(p.get('contracts', 0) or p.get('size', 0) or 0) > 0}
            except Exception:
                aktif_borsa = {}

            # Kapananları işle
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

            # Yeni sinyal
            su_an = time.time()
            sinyaller = []
            debug = []
            mevcut_sektorler = aktif_sektorler()
            
            for symbol in TAKIP_EDILENLER:
                if not BOT_CALISIYOR_MU: break
                if symbol in aktif_borsa: continue
                if len(aktif_borsa) >= mod['maks_pozisyon']: break
                if su_an < COIN_COOLDOWNLAR.get(symbol, 0): continue
                if SEKTOR_MAP.get(symbol, 'DIGER') in mevcut_sektorler: continue
                
                try:
                    params = coin_params_al(symbol)
                    df_raw = fetch_ohlcv_guvenli(symbol, RISK['zaman_dilimi'], limit=200)
                    if df_raw is None or len(df_raw) < 100:
                        continue
                    df = pd.DataFrame(df_raw, columns=['timestamp','open','high','low','close','volume'])
                    sig, neden = sinyal_uret(df, params)
                    if sig:
                        sinyaller.append({"symbol": symbol, **sig})
                        debug.append(f"{symbol.split('/')[0]}:✅{sig['yon']}")
                    else:
                        debug.append(f"{symbol.split('/')[0]}:{neden[:10]}")
                except Exception:
                    continue

            for s in sinyaller:
                if not BOT_CALISIYOR_MU: break
                if len(aktif_borsa) >= mod['maks_pozisyon']: break
                if pozisyon_ac(s['symbol'], s):
                    aktif_borsa[s['symbol']] = True

            dongu_sayaci += 1
            if dongu_sayaci % 40 == 0:
                ozet = " | ".join(debug[:6])
                print(f"🔍 #{dongu_sayaci} | Poz: {len(AKTIF_POZISYONLAR)} | {ozet}", flush=True)

        except Exception as e:
            print(f"⚠️ Döngü: {e}", flush=True)
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
    app_tg.add_handler(CommandHandler("mod", mod_komutu))
    app_tg.add_handler(CommandHandler("optimize", optimize_komutu))
    app_tg.add_handler(CommandHandler("backtest", backtest_komutu))
    app_tg.add_handler(CommandHandler("params", params_komutu))

    app_tg.run_polling(drop_pending_updates=True)

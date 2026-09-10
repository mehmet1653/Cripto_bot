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

# Sadece backtest'te kazanan 6 coin
TAKIP_EDILENLER = [
    'ETH/USDT:USDT', 'SOL/USDT:USDT', 'XRP/USDT:USDT',
    'ADA/USDT:USDT', 'DOGE/USDT:USDT', 'AVAX/USDT:USDT'
]

# ==================== RİSK PARAMETRELERİ ====================
ISLEM_BASI_RISK_PCT = 0.01
KALDIRAC = 5
MAKSIMUM_TOPLAM_POZISYON = 3
GUNLUK_MAX_KAYIP_PCT = 0.03
COOLDOWN_SURESI_SANIYE = 15 * 60

# ==================== MEAN REVERSION PARAMETRELERİ (DEFAULT) ====================
DEFAULT_PARAMS = {
    "bollinger_period": 20,
    "bollinger_std": 2.0,
    "rsi_period": 14,
    "rsi_long": 32,
    "rsi_short": 68,
    "atr_stop_mult": 1.5,
    "risk_reward": 2.0,
    "min_stop_pct": 0.008,
    "max_stop_pct": 0.030,
}

KOMISYON_ORANI = 0.001
ZAMAN_DILIMI = '1h'

SEKTOR_MAP = {
    'ETH/USDT:USDT': 'ETH',
    'SOL/USDT:USDT': 'L1', 'ADA/USDT:USDT': 'L1', 'AVAX/USDT:USDT': 'L1',
    'XRP/USDT:USDT': 'PAYMENT',
    'DOGE/USDT:USDT': 'MEME'
}

BOT_CALISIYOR_MU = True
GUN_BASI_KASA = None
GUN_BASI_TARIH = None
BACKTEST_CALISIYOR = False
OPTIMIZE_CALISIYOR = False

# Optimize edilen parametreler (coin bazlı)
COIN_PARAMS = {}  # {symbol: params_dict}

# ==================== SUPABASE ====================
def hafizayi_yukle():
    varsayilan = {
        "aktif_sistemler": {},
        "analitik": {
            "basarisiz_analizler": [],
            "basarili_islem_sayisi": 0,
            "basarisiz_islem_sayisi": 0,
            "gunluk_net_kar_usd": 0.0,
            "egitim_verileri": []
        },
        "cooldownlar": {},
        "coin_params": {}
    }
    try:
        r = supabase.table("bot_hafiza").select("*").eq("id", 1).execute()
        if r.data:
            v = r.data[0]
            return {
                "aktif_sistemler": v.get("aktif_sistemler", {}),
                "analitik": v.get("analitik", varsayilan["analitik"]),
                "cooldownlar": v.get("cooldownlar", {}),
                "coin_params": v.get("coin_params", {})
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
            "coin_params": COIN_PARAMS
        }).execute()
    except Exception:
        pass

kalici = hafizayi_yukle()
AKTIF_GRID_SISTEMLERI = kalici["aktif_sistemler"]
ANALITIK_HAFIZA = kalici["analitik"]
COIN_COOLDOWNLAR = kalici["cooldownlar"]
COIN_PARAMS = kalici.get("coin_params", {})

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
            try:
                exchange.cancel_order(e['id'], symbol)
            except Exception:
                pass
    except Exception:
        pass
    try:
        exchange.cancel_all_orders(symbol)
    except Exception:
        pass

def set_leverage_and_margin_safely(symbol, leverage):
    try:
        exchange.set_leverage(leverage, symbol)
        try:
            exchange.set_margin_mode('isolated', symbol)
        except Exception:
            pass
        return True
    except Exception:
        return False

def aktif_sektorler():
    return set(SEKTOR_MAP.get(s, 'DIGER') for s in AKTIF_GRID_SISTEMLERI.keys())

def gunluk_kayip_kontrol():
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
        return kayip_pct >= GUNLUK_MAX_KAYIP_PCT
    except Exception:
        return False

# ==================== GÖSTERGELER ====================
def rsi_hesapla(close, period=14):
    delta = close.diff()
    gain = delta.where(delta > 0, 0).rolling(period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi

def bollinger_hesapla(close, period=20, std_mult=2.0):
    sma = close.rolling(period).mean()
    std = close.rolling(period).std()
    ust = sma + (std * std_mult)
    alt = sma - (std * std_mult)
    return sma, ust, alt

def atr_hesapla(df, period=14):
    high = df['high']; low = df['low']; close = df['close']
    tr1 = high - low
    tr2 = (high - close.shift()).abs()
    tr3 = (low - close.shift()).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = tr.rolling(period).mean()
    return atr

def hacim_onay(df, period=20, esik=1.0):
    try:
        ort = df['volume'].rolling(period).mean().iloc[-1]
        return bool(df['volume'].iloc[-1] > ort * esik)
    except Exception:
        return False

# ==================== SİNYAL ÜRETİMİ (PARAMETRELİ) ====================
def sinyal_uret(df, params):
    """
    Mean Reversion sinyali. Parametreler dışarıdan geliyor.
    """
    if len(df) < 50:
        return None, "veri yetersiz"

    close = df['close']
    open_ = df['open']
    high = df['high']
    low = df['low']

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
        return None, f"ATR %{atr_pct:.2f} aralık dışı"

    if not hacim_onay(df, 20, 1.0):
        return None, "hacim yok"

    # LONG
    if son_fiyat <= son_alt and son_rsi < params['rsi_long'] and son_fiyat > son_open:
        stop_mesafe = son_atr * params['atr_stop_mult']
        stop = min(low.iloc[-1] - son_atr * 0.5, son_fiyat - stop_mesafe)
        stop_pct_hesap = (son_fiyat - stop) / son_fiyat
        stop_pct = max(params['min_stop_pct'], min(params['max_stop_pct'], stop_pct_hesap))
        stop = son_fiyat * (1 - stop_pct)
        tp_pct = stop_pct * params['risk_reward'] + KOMISYON_ORANI
        tp = son_fiyat * (1 + tp_pct)
        return {
            "yon": "LONG", "giris": float(son_fiyat),
            "stop": float(stop), "tp": float(tp),
            "stop_pct": stop_pct, "tp_pct": tp_pct,
            "rsi": float(son_rsi), "atr_pct": atr_pct
        }, "OK"

    # SHORT
    if son_fiyat >= son_ust and son_rsi > params['rsi_short'] and son_fiyat < son_open:
        stop_mesafe = son_atr * params['atr_stop_mult']
        stop = max(high.iloc[-1] + son_atr * 0.5, son_fiyat + stop_mesafe)
        stop_pct_hesap = (stop - son_fiyat) / son_fiyat
        stop_pct = max(params['min_stop_pct'], min(params['max_stop_pct'], stop_pct_hesap))
        stop = son_fiyat * (1 + stop_pct)
        tp_pct = stop_pct * params['risk_reward'] + KOMISYON_ORANI
        tp = son_fiyat * (1 - tp_pct)
        return {
            "yon": "SHORT", "giris": float(son_fiyat),
            "stop": float(stop), "tp": float(tp),
            "stop_pct": stop_pct, "tp_pct": tp_pct,
            "rsi": float(son_rsi), "atr_pct": atr_pct
        }, "OK"

    if son_fiyat > son_alt and son_fiyat < son_ust:
        return None, "fiyat bantlar arasında"
    if params['rsi_long'] <= son_rsi <= params['rsi_short']:
        return None, f"RSI {son_rsi:.1f} nötr"
    return None, "koşullar uygun değil"

def coin_parametre_al(symbol):
    """Optimize edilmiş parametre varsa onu, yoksa default'u döndür."""
    return COIN_PARAMS.get(symbol, DEFAULT_PARAMS)

# ==================== BACKTEST ====================
def tek_coin_backtest(symbol, params, gun_sayisi=365):
    try:
        limit = min(gun_sayisi * 24, 1000)
        ohlcv = exchange.fetch_ohlcv(symbol, ZAMAN_DILIMI, limit=limit)
        df = pd.DataFrame(ohlcv, columns=['timestamp','open','high','low','close','volume'])

        if len(df) < 100:
            return {"symbol": symbol, "islem": 0, "win_rate": 0, "pf": 0,
                    "ev": 0, "max_dd": 0, "toplam": 0}

        trades = []
        pozisyon = None
        baslangic = 50

        for i in range(baslangic, len(df)):
            ts = df['timestamp'].iloc[i]

            if pozisyon is not None:
                high = df['high'].iloc[i]
                low = df['low'].iloc[i]
                if pozisyon['yon'] == 'LONG':
                    if low <= pozisyon['stop']:
                        trades.append({**pozisyon, 'cikis': pozisyon['stop']})
                        pozisyon = None
                    elif high >= pozisyon['tp']:
                        trades.append({**pozisyon, 'cikis': pozisyon['tp']})
                        pozisyon = None
                else:
                    if high >= pozisyon['stop']:
                        trades.append({**pozisyon, 'cikis': pozisyon['stop']})
                        pozisyon = None
                    elif low <= pozisyon['tp']:
                        trades.append({**pozisyon, 'cikis': pozisyon['tp']})
                        pozisyon = None
                continue

            df_slice = df.iloc[max(0, i-100):i+1].reset_index(drop=True)
            try:
                sig, _ = sinyal_uret(df_slice, params)
            except Exception:
                continue

            if sig:
                pozisyon = {**sig, 'giris_zaman': ts}

        if not trades:
            return {"symbol": symbol, "islem": 0, "win_rate": 0, "pf": 0,
                    "ev": 0, "max_dd": 0, "toplam": 0}

        kazanclar = []
        for t in trades:
            if t['yon'] == 'LONG':
                pct = (t['cikis'] - t['giris']) / t['giris']
            else:
                pct = (t['giris'] - t['cikis']) / t['giris']
            pct -= KOMISYON_ORANI
            kazanclar.append(pct)

        k = np.array(kazanclar)
        kaz = k[k > 0]
        kay = k[k < 0]

        win = len(kaz) / len(k) * 100 if len(k) else 0
        pf = abs(kaz.sum() / kay.sum()) if len(kay) and kay.sum() != 0 else 999
        equity = np.cumprod(1 + k)
        peak = np.maximum.accumulate(equity)
        dd = (equity - peak) / peak
        max_dd = dd.min() * 100 if len(dd) else 0

        return {
            "symbol": symbol, "islem": len(trades),
            "win_rate": round(win, 2),
            "pf": round(pf, 3) if pf != 999 else 999,
            "ev": round(k.mean() * 100, 4),
            "max_dd": round(max_dd, 2),
            "toplam": round((equity[-1] - 1) * 100, 2) if len(equity) else 0
        }
    except Exception as e:
        return {"symbol": symbol, "islem": -1, "hata": str(e)[:40]}

# ==================== OPTİMİZASYON ====================
def optimize_coin(symbol, gun_sayisi=365):
    """Grid search: her kombinasyonu backtest eder, en iyi PF'i bulur."""
    # Denenecek parametre seti
    bollinger_std_list = [1.5, 2.0, 2.5]
    rsi_long_list = [28, 32, 36]
    atr_stop_mult_list = [1.2, 1.5, 2.0]
    risk_reward_list = [1.5, 2.0, 2.5]

    # Veriyi bir kez çek
    limit = min(gun_sayisi * 24, 1000)
    ohlcv = exchange.fetch_ohlcv(symbol, ZAMAN_DILIMI, limit=limit)
    df = pd.DataFrame(ohlcv, columns=['timestamp','open','high','low','close','volume'])

    if len(df) < 100:
        return None

    en_iyi = None
    toplam_kombinasyon = len(bollinger_std_list) * len(rsi_long_list) * len(atr_stop_mult_list) * len(risk_reward_list)
    sayac = 0

    for bstd in bollinger_std_list:
        for rl in rsi_long_list:
            for asm in atr_stop_mult_list:
                for rr in risk_reward_list:
                    params = {
                        "bollinger_period": 20,
                        "bollinger_std": bstd,
                        "rsi_period": 14,
                        "rsi_long": rl,
                        "rsi_short": 100 - rl,   # simetrik
                        "atr_stop_mult": asm,
                        "risk_reward": rr,
                        "min_stop_pct": 0.008,
                        "max_stop_pct": 0.030,
                    }
                    sayac += 1
                    r = backtest_with_data(symbol, df, params)
                    # Kriter: işlem sayısı >= 5 (istatistik), PF yüksek olsun
                    if r and r['islem'] >= 5:
                        if en_iyi is None or r['pf'] > en_iyi['pf']:
                            en_iyi = {
                                "params": params,
                                "pf": r['pf'],
                                "win_rate": r['win_rate'],
                                "toplam": r['toplam'],
                                "ev": r['ev'],
                                "max_dd": r['max_dd'],
                                "islem": r['islem']
                            }
    return en_iyi

def backtest_with_data(symbol, df, params):
    """Verilen df ve params ile backtest yapar (veri tekrar çekilmez)."""
    try:
        trades = []
        pozisyon = None

        for i in range(50, len(df)):
            if pozisyon is not None:
                high = df['high'].iloc[i]
                low = df['low'].iloc[i]
                if pozisyon['yon'] == 'LONG':
                    if low <= pozisyon['stop']:
                        trades.append({**pozisyon, 'cikis': pozisyon['stop']})
                        pozisyon = None
                    elif high >= pozisyon['tp']:
                        trades.append({**pozisyon, 'cikis': pozisyon['tp']})
                        pozisyon = None
                else:
                    if high >= pozisyon['stop']:
                        trades.append({**pozisyon, 'cikis': pozisyon['stop']})
                        pozisyon = None
                    elif low <= pozisyon['tp']:
                        trades.append({**pozisyon, 'cikis': pozisyon['tp']})
                        pozisyon = None
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
        kaz = k[k > 0]
        kay = k[k < 0]
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
            "ev": round(k.mean() * 100, 4),
            "max_dd": round(max_dd, 2),
            "toplam": round((equity[-1] - 1) * 100, 2) if len(equity) else 0
        }
    except Exception:
        return None

# ==================== FLASK ====================
@app.route('/')
def home():
    return f"Bot Aktif | Mean Reversion + Optimize | Pozisyon: {len(AKTIF_GRID_SISTEMLERI)}"

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
                detay += f"• `{p.get('symbol')}` | {str(p.get('side','')).upper()} | `{float(p.get('unrealizedPnl',0)):+.2f}` USDT\n"
        else:
            detay = "\n📋 *Aktif Pozisyon Yok*\n"

        opt_sayisi = len(COIN_PARAMS)
        mesaj = (
            f"🎯 *MEAN REVERSION BOT*\n\n"
            f"💰 Toplam: `{total:.2f}` USDT (Serbest: `{free:.2f}`)\n"
            f"{ikon} PnL: `{pnl:+.2f}` USDT (`%{pnl_pct:+.2f}`)\n"
            f"📌 Pozisyon: `{len(poslari)}/{MAKSIMUM_TOPLAM_POZISYON}`\n"
            f"🧠 Optimize edilmiş coin: `{opt_sayisi}/{len(TAKIP_EDILENLER)}`\n"
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

async def backtest_komutu(update, context):
    global BACKTEST_CALISIYOR
    if BACKTEST_CALISIYOR or OPTIMIZE_CALISIYOR:
        await update.message.reply_text("⏳ Zaten bir işlem çalışıyor, bitmesini bekle.")
        return
    BACKTEST_CALISIYOR = True
    await update.message.reply_text(
        "⏳ *Backtest başlatıldı...*\nSon 365 gün / 1h mum. 3-5 dakika.",
        parse_mode='Markdown'
    )

    def run():
        global BACKTEST_CALISIYOR
        try:
            satirlar = ["*📊 BACKTEST (365 gün, 1h)*\n", "```"]
            satirlar.append(f"{'COIN':<16} {'İŞL':>4} {'WIN%':>6} {'PF':>6} {'EV%':>7} {'DD%':>6} {'TOT%':>7}")
            satirlar.append("-" * 60)
            for c in TAKIP_EDILENLER:
                params = coin_parametre_al(c)
                r = tek_coin_backtest(c, params, gun_sayisi=365)
                if r.get('islem', -1) == -1:
                    satirlar.append(f"{c[:14]:<16} HATA: {r.get('hata','?')[:30]}")
                else:
                    satirlar.append(
                        f"{c[:14]:<16} {r['islem']:>4} {r['win_rate']:>6} {r['pf']:>6} {r['ev']:>7} {r['max_dd']:>6} {r['toplam']:>7}"
                    )
            satirlar.append("```")
            satirlar.append(f"\n🧠 Optimize edilmiş: {len(COIN_PARAMS)}/{len(TAKIP_EDILENLER)}")

            metin = "\n".join(satirlar)
            telegram_mesaj_gonder(metin)
        except Exception as e:
            telegram_mesaj_gonder(f"❌ Backtest hatası: {e}")
        finally:
            BACKTEST_CALISIYOR = False

    threading.Thread(target=run, daemon=True).start()

async def optimize_komutu(update, context):
    global OPTIMIZE_CALISIYOR
    if BACKTEST_CALISIYOR or OPTIMIZE_CALISIYOR:
        await update.message.reply_text("⏳ Zaten bir işlem çalışıyor, bitmesini bekle.")
        return
    OPTIMIZE_CALISIYOR = True
    await update.message.reply_text(
        "🧠 *Optimizasyon başlatıldı!*\n"
        f"Her coin için 81 kombinasyon denenecek ({len(TAKIP_EDILENLER)} coin).\n"
        "Tahmini süre: 8-12 dakika. Sabırlı ol.",
        parse_mode='Markdown'
    )

    def run():
        global OPTIMIZE_CALISIYOR, COIN_PARAMS
        try:
            telegram_mesaj_gonder("🔬 *Optimizasyon başladı...*\nHer coin için sırayla sonuç atacağım.")

            for c in TAKIP_EDILENLER:
                en_iyi = optimize_coin(c, gun_sayisi=365)
                if en_iyi is None:
                    telegram_mesaj_gonder(f"⚠️ `{c}` — yeterli sinyal yok, atlanıyor.")
                    continue

                COIN_PARAMS[c] = en_iyi['params']
                hafizayi_kaydet()

                p = en_iyi['params']
                mesaj = (
                    f"🏆 *{c}* — EN İYİ PARAMETRELER\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"Bollinger STD: `{p['bollinger_std']}`\n"
                    f"RSI Long/Short: `{p['rsi_long']}/{p['rsi_short']}`\n"
                    f"ATR Stop Mult: `{p['atr_stop_mult']}`\n"
                    f"Risk/Reward: `{p['risk_reward']}`\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"📊 İşlem: `{en_iyi['islem']}` | WIN: `{en_iyi['win_rate']}%`\n"
                    f"💎 PF: `{en_iyi['pf']}` | EV: `{en_iyi['ev']}%`\n"
                    f"📉 DD: `{en_iyi['max_dd']}%` | TOT: `{en_iyi['toplam']}%`"
                )
                telegram_mesaj_gonder(mesaj)
                time.sleep(2)

            telegram_mesaj_gonder(
                f"✅ *Optimizasyon tamamlandı!*\n"
                f"Toplam optimize edilen: `{len(COIN_PARAMS)}/{len(TAKIP_EDILENLER)}`\n"
                f"Şimdi `/backtest` yaz, yeni sonuçları gör."
            )
        except Exception as e:
            telegram_mesaj_gonder(f"❌ Optimizasyon hatası: {e}")
        finally:
            OPTIMIZE_CALISIYOR = False

    threading.Thread(target=run, daemon=True).start()

async def temizle_komutu(update, context):
    global COIN_PARAMS
    COIN_PARAMS = {}
    hafizayi_kaydet()
    await update.message.reply_text("🗑️ Optimize edilmiş parametreler silindi. Default'a dönüldü.", parse_mode='Markdown')

async def params_komutu(update, context):
    if not COIN_PARAMS:
        await update.message.reply_text("Henüz optimize edilmiş parametre yok. `/optimize` yaz.", parse_mode='Markdown')
        return
    satirlar = ["🧠 *Optimize Edilmiş Parametreler:*\n"]
    for sym, p in COIN_PARAMS.items():
        satirlar.append(
            f"`{sym[:12]}`\n"
            f"  STD: {p['bollinger_std']} | RSI: {p['rsi_long']}/{p['rsi_short']} | "
            f"ATR×{p['atr_stop_mult']} | R/R: {p['risk_reward']}"
        )
    await update.message.reply_text("\n".join(satirlar), parse_mode='Markdown')

# ==================== ANA DÖNGÜ ====================
def otomatik_arkaplan_tarayici():
    global BOT_CALISIYOR_MU, ANALITIK_HAFIZA
    print("🎯 [MEAN REVERSION + OPTIMIZE] Aktif.", flush=True)
    print(f"📊 {len(TAKIP_EDILENLER)} coin | {ZAMAN_DILIMI} | R/R default 1:2", flush=True)
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

            if gunluk_kayip_kontrol():
                telegram_mesaj_gonder("🛑 *Günlük %3 kayıp limiti aşıldı!*")
                BOT_CALISIYOR_MU = False
                time.sleep(3600)
                continue

            try:
                raw = exchange.fetch_positions()
                aktif_borsa = {p['symbol']: p for p in raw
                               if float(p.get('contracts', 0) or p.get('size', 0) or 0) > 0}
            except Exception:
                aktif_borsa = {}

            # Kapananları işle
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
                        telegram_mesaj_gonder(f"🎉 *Kâr Alındı* → `{sym}` 🟢")
                    else:
                        ANALITIK_HAFIZA["basarisiz_islem_sayisi"] = ANALITIK_HAFIZA.get("basarisiz_islem_sayisi", 0) + 1
                        COIN_COOLDOWNLAR[sym] = time.time() + COOLDOWN_SURESI_SANIYE
                        telegram_mesaj_gonder(f"❌ *Stop* → `{sym}` 🔴")
                    hafizayi_kaydet()

            su_an = time.time()
            sinyaller = []
            mevcut_sektorler = aktif_sektorler()
            debug_mesaj = []

            for symbol in TAKIP_EDILENLER:
                if not BOT_CALISIYOR_MU:
                    break
                if symbol in aktif_borsa:
                    continue
                if len(aktif_borsa) >= MAKSIMUM_TOPLAM_POZISYON:
                    break
                if su_an < COIN_COOLDOWNLAR.get(symbol, 0):
                    continue
                if SEKTOR_MAP.get(symbol, 'DIGER') in mevcut_sektorler:
                    continue

                try:
                    ohlcv = exchange.fetch_ohlcv(symbol, ZAMAN_DILIMI, limit=120)
                    df = pd.DataFrame(ohlcv, columns=['timestamp','open','high','low','close','volume'])
                    params = coin_parametre_al(symbol)
                    sig, neden = sinyal_uret(df, params)
                    if sig:
                        sinyaller.append({"symbol": symbol, **sig})
                        debug_mesaj.append(f"{symbol.split('/')[0]}:✅{sig['yon']}")
                    else:
                        debug_mesaj.append(f"{symbol.split('/')[0]}:{neden[:15]}")
                except Exception:
                    continue

            dongu_sayaci += 1
            if dongu_sayaci % 40 == 0:
                ozet = " | ".join(debug_mesaj[:6])
                print(f"🔍 #{dongu_sayaci} | {ozet}", flush=True)

            # İşlem aç
            for s in sinyaller:
                if not BOT_CALISIYOR_MU:
                    break
                if len(aktif_borsa) >= MAKSIMUM_TOPLAM_POZISYON:
                    break

                symbol = s["symbol"]
                yon = s["yon"]
                stop_pct = s["stop_pct"]

                try:
                    bal = exchange.fetch_balance()
                    kasa = float(bal['total'].get('USDT', 0))
                except Exception:
                    continue
                if kasa <= 0:
                    continue

                if not set_leverage_and_margin_safely(symbol, KALDIRAC):
                    continue

                risk_usdt = kasa * ISLEM_BASI_RISK_PCT
                pozisyon_degeri = risk_usdt / stop_pct

                try:
                    market_info = exchange.market(symbol)
                    contract_size = float(market_info.get('contractSize', 1.0))
                    ham_kontrat = pozisyon_degeri / (s["giris"] * contract_size)
                    miktar = float(exchange.amount_to_precision(symbol, max(ham_kontrat, 0.001)))
                    if miktar <= 0:
                        continue
                except Exception:
                    continue

                try:
                    tum_emirleri_iptal_et(symbol)
                    emir = exchange.create_order(
                        symbol, 'market',
                        'buy' if yon == 'LONG' else 'sell',
                        miktar
                    )
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
                        exchange.create_order(
                            symbol, 'stop', kapat_yon, miktar, stop,
                            {'stopPrice': stop, 'triggerPrice': stop, 'reduceOnly': True}
                        )
                    except Exception:
                        try:
                            exchange.create_order(
                                symbol, 'stop_market', kapat_yon, miktar, stop,
                                {'stopPrice': stop, 'triggerPrice': stop, 'reduceOnly': True}
                            )
                        except Exception:
                            pass

                    try:
                        exchange.create_order(
                            symbol, 'limit', kapat_yon, miktar, tp,
                            {'reduceOnly': True}
                        )
                    except Exception:
                        pass

                    AKTIF_GRID_SISTEMLERI[symbol] = {
                        "yon": yon, "giris": giris, "stop": stop, "tp": tp, "miktar": miktar
                    }
                    hafizayi_kaydet()

                    print(f"🎯 {symbol} | {yon} | {miktar} | TP:{tp} | SL:{stop}", flush=True)
                    telegram_mesaj_gonder(
                        f"🎯 *İŞLEM AÇILDI*\n"
                        f"📌 `{symbol}` | *{yon}*\n"
                        f"💰 Giriş: `{giris}`\n"
                        f"🎯 TP: `{tp}` | 🛑 SL: `{stop}`\n"
                        f"📊 Risk: `{risk_usdt:.2f}` USDT | RSI: `{s['rsi']:.1f}`"
                    )
                    break
                except Exception as e:
                    print(f"❌ İşlem hatası: {e}", flush=True)

        except Exception as e:
            print(f"⚠️ Döngü hatası: {e}", flush=True)
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
    app_tg.add_handler(CommandHandler("backtest", backtest_komutu))
    app_tg.add_handler(CommandHandler("optimize", optimize_komutu))
    app_tg.add_handler(CommandHandler("params", params_komutu))
    app_tg.add_handler(CommandHandler("temizle", temizle_komutu))

    app_tg.run_polling(drop_pending_updates=True)

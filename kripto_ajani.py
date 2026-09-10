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

# Top 10 likit coin
TAKIP_EDILENLER = [
    'BTC/USDT:USDT', 'ETH/USDT:USDT', 'SOL/USDT:USDT', 'BNB/USDT:USDT',
    'XRP/USDT:USDT', 'ADA/USDT:USDT', 'DOGE/USDT:USDT', 'AVAX/USDT:USDT',
    'LINK/USDT:USDT', 'DOT/USDT:USDT'
]

# ==================== RİSK PARAMETRELERİ ====================
ISLEM_BASI_RISK_PCT = 0.01        # Kasanın %1'i
KALDIRAC = 5
MAKSIMUM_TOPLAM_POZISYON = 3
GUNLUK_MAX_KAYIP_PCT = 0.03
COOLDOWN_SURESI_SANIYE = 15 * 60

# --- MEAN REVERSION PARAMETRELERİ ---
BOLLINGER_PERIOD = 20
BOLLINGER_STD = 2.0
RSI_PERIOD = 14
RSI_LONG_ESIK = 32              # RSI < 32 → LONG dönüş sinyali
RSI_SHORT_ESIK = 68             # RSI > 68 → SHORT dönüş sinyali
ATR_PERIOD = 14
ATR_STOP_MULT = 1.5
RISK_REWARD = 2.0
KOMISYON_ORANI = 0.001
MIN_STOP_PCT = 0.008
MAX_STOP_PCT = 0.030

# Zaman dilimi
ZAMAN_DILIMI = '1h'

SEKTOR_MAP = {
    'BTC/USDT:USDT': 'BTC', 'ETH/USDT:USDT': 'ETH',
    'SOL/USDT:USDT': 'L1', 'AVAX/USDT:USDT': 'L1', 'ADA/USDT:USDT': 'L1', 'DOT/USDT:USDT': 'L1',
    'BNB/USDT:USDT': 'EXCH', 'XRP/USDT:USDT': 'PAYMENT', 'LINK/USDT:USDT': 'ORACLE',
    'DOGE/USDT:USDT': 'MEME'
}

BOT_CALISIYOR_MU = True
GUN_BASI_KASA = None
GUN_BASI_TARIH = None
BACKTEST_CALISIYOR = False

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
        "cooldownlar": {}
    }
    try:
        r = supabase.table("bot_hafiza").select("*").eq("id", 1).execute()
        if r.data:
            v = r.data[0]
            return {
                "aktif_sistemler": v.get("aktif_sistemler", {}),
                "analitik": v.get("analitik", varsayilan["analitik"]),
                "cooldownlar": v.get("cooldownlar", {})
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
            "cooldownlar": COIN_COOLDOWNLAR
        }).execute()
    except Exception:
        pass

kalici = hafizayi_yukle()
AKTIF_GRID_SISTEMLERI = kalici["aktif_sistemler"]
ANALITIK_HAFIZA = kalici["analitik"]
COIN_COOLDOWNLAR = kalici["cooldownlar"]

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

def ema_hesapla(close, period):
    return close.ewm(span=period, adjust=False).mean()

def hacim_onay(df, period=20, esik=1.0):
    try:
        ort = df['volume'].rolling(period).mean().iloc[-1]
        return bool(df['volume'].iloc[-1] > ort * esik)
    except Exception:
        return False

# ==================== SİNYAL ÜRETİMİ ====================
def sinyal_uret_mean_reversion(df, btc_df=None):
    """
    Mean Reversion stratejisi:
    - Fiyat Bollinger alt bandının altına indi + RSI < 32 + son mum yeşil → LONG
    - Fiyat Bollinger üst bandının üstüne çıktı + RSI > 68 + son mum kırmızı → SHORT
    """
    if len(df) < 50:
        return None, "veri yetersiz"

    close = df['close']
    open_ = df['open']
    high = df['high']
    low = df['low']

    sma, ust, alt = bollinger_hesapla(close, BOLLINGER_PERIOD, BOLLINGER_STD)
    rsi = rsi_hesapla(close, RSI_PERIOD)
    atr = atr_hesapla(df, ATR_PERIOD)

    son_fiyat = close.iloc[-1]
    son_open = open_.iloc[-1]
    son_sma = sma.iloc[-1]
    son_ust = ust.iloc[-1]
    son_alt = alt.iloc[-1]
    son_rsi = rsi.iloc[-1]
    son_atr = atr.iloc[-1]

    if pd.isna(son_sma) or pd.isna(son_rsi) or pd.isna(son_atr) or son_atr == 0:
        return None, "gösterge hesaplanamadı"

    atr_pct = (son_atr / son_fiyat) * 100

    # ATR filtresi
    if not (0.3 <= atr_pct <= 5.0):
        return None, f"ATR %{atr_pct:.2f} aralık dışı"

    # Hacim onayı
    if not hacim_onay(df, 20, 1.0):
        return None, "hacim yok"

    # LONG sinyali
    if son_fiyat <= son_alt and son_rsi < RSI_LONG_ESIK and son_fiyat > son_open:
        stop_mesafe = son_atr * ATR_STOP_MULT
        stop = min(low.iloc[-1] - son_atr * 0.5, son_fiyat - stop_mesafe)
        stop_pct_hesap = (son_fiyat - stop) / son_fiyat
        stop_pct = max(MIN_STOP_PCT, min(MAX_STOP_PCT, stop_pct_hesap))
        stop = son_fiyat * (1 - stop_pct)
        tp_pct = stop_pct * RISK_REWARD + KOMISYON_ORANI
        tp = son_fiyat * (1 + tp_pct)
        return {
            "yon": "LONG",
            "giris": float(son_fiyat),
            "stop": float(stop),
            "tp": float(tp),
            "stop_pct": stop_pct,
            "tp_pct": tp_pct,
            "rsi": float(son_rsi),
            "atr_pct": atr_pct
        }, "OK"

    # SHORT sinyali
    if son_fiyat >= son_ust and son_rsi > RSI_SHORT_ESIK and son_fiyat < son_open:
        stop_mesafe = son_atr * ATR_STOP_MULT
        stop = max(high.iloc[-1] + son_atr * 0.5, son_fiyat + stop_mesafe)
        stop_pct_hesap = (stop - son_fiyat) / son_fiyat
        stop_pct = max(MIN_STOP_PCT, min(MAX_STOP_PCT, stop_pct_hesap))
        stop = son_fiyat * (1 + stop_pct)
        tp_pct = stop_pct * RISK_REWARD + KOMISYON_ORANI
        tp = son_fiyat * (1 - tp_pct)
        return {
            "yon": "SHORT",
            "giris": float(son_fiyat),
            "stop": float(stop),
            "tp": float(tp),
            "stop_pct": stop_pct,
            "tp_pct": tp_pct,
            "rsi": float(son_rsi),
            "atr_pct": atr_pct
        }, "OK"

    # Hangi filtre takıldı?
    if son_fiyat > son_alt and son_fiyat < son_ust:
        return None, "fiyat bantlar arasında"
    if son_rsi >= RSI_LONG_ESIK and son_rsi <= RSI_SHORT_ESIK:
        return None, f"RSI {son_rsi:.1f} nötr"
    return None, "koşullar uygun değil"

# ==================== BACKTEST ====================
def tek_coin_backtest(symbol, gun_sayisi=365):
    try:
        # 1h mum, kaç tane lazım?
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
                        trades.append({**pozisyon, 'cikis': pozisyon['stop'], 'sonuc': 'STOP'})
                        pozisyon = None
                    elif high >= pozisyon['tp']:
                        trades.append({**pozisyon, 'cikis': pozisyon['tp'], 'sonuc': 'TP'})
                        pozisyon = None
                else:
                    if high >= pozisyon['stop']:
                        trades.append({**pozisyon, 'cikis': pozisyon['stop'], 'sonuc': 'STOP'})
                        pozisyon = None
                    elif low <= pozisyon['tp']:
                        trades.append({**pozisyon, 'cikis': pozisyon['tp'], 'sonuc': 'TP'})
                        pozisyon = None
                continue

            df_slice = df.iloc[max(0, i-100):i+1].reset_index(drop=True)
            try:
                sig, _ = sinyal_uret_mean_reversion(df_slice)
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
            "symbol": symbol,
            "islem": len(trades),
            "win_rate": round(win, 2),
            "pf": round(pf, 3) if pf != 999 else 999,
            "ev": round(k.mean() * 100, 4),
            "max_dd": round(max_dd, 2),
            "toplam": round((equity[-1] - 1) * 100, 2) if len(equity) else 0
        }
    except Exception as e:
        return {"symbol": symbol, "islem": -1, "hata": str(e)[:40]}

# ==================== FLASK ====================
@app.route('/')
def home():
    return f"Bot Aktif | Strateji: Mean Reversion | Pozisyon: {len(AKTIF_GRID_SISTEMLERI)}"

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

        mesaj = (
            f"🎯 *MEAN REVERSION BOT*\n\n"
            f"💰 Toplam: `{total:.2f}` USDT (Serbest: `{free:.2f}`)\n"
            f"{ikon} PnL: `{pnl:+.2f}` USDT (`%{pnl_pct:+.2f}`)\n"
            f"📌 Pozisyon: `{len(poslari)}/{MAKSIMUM_TOPLAM_POZISYON}`\n"
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
    if BACKTEST_CALISIYOR:
        await update.message.reply_text("⏳ Zaten bir backtest çalışıyor, bitmesini bekle.")
        return
    BACKTEST_CALISIYOR = True
    await update.message.reply_text(
        "⏳ *Mean Reversion Backtest başlatıldı...*\nSon 365 gün / 1h mum taranıyor. 3-5 dakika sürebilir.",
        parse_mode='Markdown'
    )

    def run():
        global BACKTEST_CALISIYOR
        try:
            satirlar = ["*📊 MEAN REVERSION BACKTEST* (1h, 365 gün)\n", "```"]
            satirlar.append(f"{'COIN':<16} {'İŞL':>4} {'WIN%':>6} {'PF':>6} {'EV%':>7} {'DD%':>6} {'TOT%':>7}")
            satirlar.append("-" * 60)
            for c in TAKIP_EDILENLER:
                r = tek_coin_backtest(c, gun_sayisi=365)
                if r.get('islem', -1) == -1:
                    satirlar.append(f"{c[:14]:<16} HATA: {r.get('hata','?')[:30]}")
                else:
                    satirlar.append(
                        f"{c[:14]:<16} {r['islem']:>4} {r['win_rate']:>6} {r['pf']:>6} {r['ev']:>7} {r['max_dd']:>6} {r['toplam']:>7}"
                    )
            satirlar.append("```")
            satirlar.append("\n*Yorumlama:*")
            satirlar.append("• PF > 1.3 ✅ | PF < 1.1 ❌")
            satirlar.append("• EV% > 0 ✅ | EV% < 0 ❌")
            satirlar.append("• İŞL > 30 → güvenilir")

            metin = "\n".join(satirlar)
            if len(metin) > 4000:
                for i in range(0, len(metin), 3500):
                    telegram_mesaj_gonder(metin[i:i+3500])
            else:
                telegram_mesaj_gonder(metin)
        except Exception as e:
            telegram_mesaj_gonder(f"❌ Backtest hatası: {e}")
        finally:
            BACKTEST_CALISIYOR = False

    threading.Thread(target=run, daemon=True).start()

# ==================== ANA DÖNGÜ ====================
def otomatik_arkaplan_tarayici():
    global BOT_CALISIYOR_MU, ANALITIK_HAFIZA
    print("🎯 [MEAN REVERSION MODU] Bollinger + RSI stratejisi aktif.", flush=True)
    print(f"📊 Zaman: {ZAMAN_DILIMI} | R/R: 1:{RISK_REWARD} | RSI LONG<{RSI_LONG_ESIK} SHORT>{RSI_SHORT_ESIK}", flush=True)
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
                telegram_mesaj_gonder("🛑 *Günlük %3 kayıp limiti aşıldı!* Bot durdu.")
                BOT_CALISIYOR_MU = False
                time.sleep(3600)
                continue

            try:
                raw = exchange.fetch_positions()
                aktif_borsa = {p['symbol']: p for p in raw
                               if float(p.get('contracts', 0) or p.get('size', 0) or 0) > 0}
            except Exception:
                aktif_borsa = {}

            # Kapanan pozisyonları işle
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
                    debug_mesaj.append(f"{symbol.split('/')[0]}: pozisyon var")
                    continue
                if len(aktif_borsa) >= MAKSIMUM_TOPLAM_POZISYON:
                    break
                if su_an < COIN_COOLDOWNLAR.get(symbol, 0):
                    debug_mesaj.append(f"{symbol.split('/')[0]}: cooldown")
                    continue
                if SEKTOR_MAP.get(symbol, 'DIGER') in mevcut_sektorler:
                    debug_mesaj.append(f"{symbol.split('/')[0]}: sektör dolu")
                    continue

                try:
                    ohlcv = exchange.fetch_ohlcv(symbol, ZAMAN_DILIMI, limit=120)
                    df = pd.DataFrame(ohlcv, columns=['timestamp','open','high','low','close','volume'])

                    sig, neden = sinyal_uret_mean_reversion(df)
                    if sig:
                        sinyaller.append({"symbol": symbol, **sig})
                        debug_mesaj.append(f"{symbol.split('/')[0]}: ✅ {sig['yon']} (RSI:{sig['rsi']:.1f})")
                    else:
                        debug_mesaj.append(f"{symbol.split('/')[0]}: {neden}")
                except Exception:
                    debug_mesaj.append(f"{symbol.split('/')[0]}: hata")
                    continue

            dongu_sayaci += 1
            if dongu_sayaci % 40 == 0:
                ozet = " | ".join(debug_mesaj[:10])
                print(f"🔍 Tarama #{dongu_sayaci} | {ozet}", flush=True)

            # İşlem açma
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
                except Exception as e:
                    print(f"❌ Miktar hatası ({symbol}): {e}", flush=True)
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
                        except Exception as e:
                            print(f"⚠️ Stop emri başarısız: {e}", flush=True)

                    try:
                        exchange.create_order(
                            symbol, 'limit', kapat_yon, miktar, tp,
                            {'reduceOnly': True}
                        )
                    except Exception as e:
                        print(f"⚠️ TP emri başarısız: {e}", flush=True)

                    AKTIF_GRID_SISTEMLERI[symbol] = {
                        "yon": yon, "giris": giris, "stop": stop, "tp": tp, "miktar": miktar
                    }
                    hafizayi_kaydet()

                    print(f"🎯 İŞLEM: {symbol} | {yon} | Miktar: {miktar} | TP: {tp} | SL: {stop}", flush=True)
                    telegram_mesaj_gonder(
                        f"🎯 *İŞLEM AÇILDI* (Mean Reversion)\n"
                        f"📌 `{symbol}` | *{yon}*\n"
                        f"💰 Giriş: `{giris}`\n"
                        f"🎯 TP: `{tp}` | 🛑 SL: `{stop}`\n"
                        f"📊 Miktar: `{miktar}` | Risk: `{risk_usdt:.2f} USDT`\n"
                        f"📈 RSI: `{s['rsi']:.1f}`"
                    )
                    break
                except Exception as e:
                    print(f"❌ İşlem açma hatası: {e}", flush=True)

        except Exception as e:
            print(f"⚠️ Döngü hatası: {e}", flush=True)
        time.sleep(20)  # 1h mum için 20 sn yeter

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

    app_tg.run_polling(drop_pending_updates=True)

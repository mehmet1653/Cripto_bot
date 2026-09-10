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
    'SOL/USDT:USDT', 'AVAX/USDT:USDT', 'XRP/USDT:USDT',
    'DOGE/USDT:USDT', 'SUI/USDT:USDT', 'LINK/USDT:USDT', 'ADA/USDT:USDT'
]

# ==================== RİSK PARAMETRELERİ ====================
ISLEM_BASI_RISK_PCT = 0.01
KALDIRAC = 5
MAKSIMUM_TOPLAM_POZISYON = 3
GUNLUK_MAX_KAYIP_PCT = 0.03
COOLDOWN_SURESI_SANIYE = 10 * 60
MIN_STOP_PCT = 0.008
MAX_STOP_PCT = 0.030
RISK_REWARD = 2.0
KOMISYON_ORANI = 0.001
ATR_STOP_MULT = 1.5

SEKTOR_MAP = {
    'SOL/USDT:USDT': 'L1', 'AVAX/USDT:USDT': 'L1', 'SUI/USDT:USDT': 'L1',
    'ADA/USDT:USDT': 'L1', 'XRP/USDT:USDT': 'PAYMENT', 'LINK/USDT:USDT': 'ORACLE',
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

def btc_trend_al():
    try:
        ohlcv = exchange.fetch_ohlcv('BTC/USDT:USDT', '1h', limit=60)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        if len(df) < 50:
            return None
        ema20 = df['close'].ewm(span=20, adjust=False).mean().iloc[-1]
        ema50 = df['close'].ewm(span=50, adjust=False).mean().iloc[-1]
        return "UP" if ema20 > ema50 else "DOWN"
    except Exception:
        return None

def atr_hesapla(df, period=14):
    try:
        high = df['high']; low = df['low']; close = df['close']
        tr1 = high - low
        tr2 = (high - close.shift()).abs()
        tr3 = (low - close.shift()).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        atr = tr.rolling(period).mean().iloc[-1]
        return float((atr / close.iloc[-1]) * 100)
    except Exception:
        return 1.5

def rsi_hesapla(close, period=14):
    delta = close.diff()
    gain = delta.where(delta > 0, 0).rolling(period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return float(rsi.iloc[-1]) if not pd.isna(rsi.iloc[-1]) else 50.0

def ema_hesapla(close, period):
    return close.ewm(span=period, adjust=False).mean()

def hacim_onay(df, period=20):
    try:
        return bool(df['volume'].iloc[-1] > df['volume'].rolling(period).mean().iloc[-1])
    except Exception:
        return False

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
        plus_di = 100 * pd.Series(plus_dm).rolling(period).mean() / atr14
        minus_di = 100 * pd.Series(minus_dm).rolling(period).mean() / atr14
        dx = (abs(plus_di - minus_di) / (plus_di + minus_di).replace(0, np.nan)) * 100
        val = dx.rolling(period).mean().iloc[-1]
        return float(val) if not pd.isna(val) else 0.0
    except Exception:
        return 0.0

def hesapla_gostergeler(df15, df1h, df4h):
    out = {}
    if len(df4h) >= 50:
        ema50_4h = ema_hesapla(df4h['close'], 50).iloc[-1]
        out['trend_4h_yon'] = "LONG" if df4h['close'].iloc[-1] > ema50_4h else "SHORT"
    else:
        out['trend_4h_yon'] = None

    ema7_1h = ema_hesapla(df1h['close'], 7).iloc[-1]
    ema21_1h = ema_hesapla(df1h['close'], 21).iloc[-1]
    out['trend_1h_yon'] = "LONG" if ema7_1h > ema21_1h else "SHORT"

    out['rsi'] = rsi_hesapla(df15['close'], 14)
    out['atr_pct'] = atr_hesapla(df15, 14)
    out['hacim_onay'] = hacim_onay(df15, 20)
    out['adx'] = adx_hesapla(df15, 14)

    ema20_15 = ema_hesapla(df15['close'], 20).iloc[-1]
    out['fiyat_ema20_ustu'] = df15['close'].iloc[-1] > ema20_15
    out['fiyat'] = float(df15['close'].iloc[-1])
    return out

def sinyal_uret(g, btc_trend=None):
    """
    Filtreleri tek tek geçen sinyaller. Debug için hangi filtreden geçtiğini de döner.
    """
    if g['trend_4h_yon'] is None:
        return None, "4h trend hesaplanamadı"
    yon = g['trend_4h_yon']
    if g['trend_1h_yon'] != yon:
        return None, f"1h ({g['trend_1h_yon']}) 4h ({yon}) uyumsuz"
    if btc_trend is not None:
        if yon == "LONG" and btc_trend == "DOWN":
            return None, "BTC düşüyor, LONG engellendi"
        if yon == "SHORT" and btc_trend == "UP":
            return None, "BTC yükseliyor, SHORT engellendi"
    if not (0.4 <= g['atr_pct'] <= 4.0):
        return None, f"ATR %{g['atr_pct']:.2f} aralık dışı"
    if g['adx'] < 22:
        return None, f"ADX {g['adx']:.1f} < 22"
    if yon == "LONG" and not (30 <= g['rsi'] <= 55):
        return None, f"RSI {g['rsi']:.1f} LONG aralığında değil"
    if yon == "SHORT" and not (45 <= g['rsi'] <= 70):
        return None, f"RSI {g['rsi']:.1f} SHORT aralığında değil"
    if not g['hacim_onay']:
        return None, "Hacim onayı yok"
    if yon == "LONG" and not g['fiyat_ema20_ustu']:
        return None, "Fiyat EMA20 altında (LONG)"
    if yon == "SHORT" and g['fiyat_ema20_ustu']:
        return None, "Fiyat EMA20 üstünde (SHORT)"

    stop_pct = max(MIN_STOP_PCT, min(MAX_STOP_PCT, (g['atr_pct'] * ATR_STOP_MULT) / 100.0))
    tp_pct = stop_pct * RISK_REWARD + KOMISYON_ORANI

    giris = g['fiyat']
    if yon == "LONG":
        stop = giris * (1 - stop_pct)
        tp = giris * (1 + tp_pct)
    else:
        stop = giris * (1 + stop_pct)
        tp = giris * (1 - tp_pct)

    return {"yon": yon, "giris": giris, "stop": stop, "tp": tp,
            "stop_pct": stop_pct, "tp_pct": tp_pct}, "OK"

# ==================== BACKTEST FONKSİYONU ====================
def tek_coin_backtest(symbol, gun_sayisi=180):
    try:
        # BTC trend referansı
        btc_ohlcv = exchange.fetch_ohlcv('BTC/USDT:USDT', '1h', limit=min(gun_sayisi * 24, 1000))
        df_btc = pd.DataFrame(btc_ohlcv, columns=['timestamp','open','high','low','close','volume'])

        o15 = exchange.fetch_ohlcv(symbol, '15m', limit=1000)
        o1h = exchange.fetch_ohlcv(symbol, '1h', limit=1000)
        o4h = exchange.fetch_ohlcv(symbol, '4h', limit=500)

        df15 = pd.DataFrame(o15, columns=['timestamp','open','high','low','close','volume'])
        df1h = pd.DataFrame(o1h, columns=['timestamp','open','high','low','close','volume'])
        df4h = pd.DataFrame(o4h, columns=['timestamp','open','high','low','close','volume'])

        trades = []
        pozisyon = None
        baslangic = 100

        for i in range(baslangic, len(df15)):
            ts = df15['timestamp'].iloc[i]

            if pozisyon is not None:
                high = df15['high'].iloc[i]
                low = df15['low'].iloc[i]
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

            df1h_s = df1h[df1h['timestamp'] <= ts]
            df4h_s = df4h[df4h['timestamp'] <= ts]
            df_btc_s = df_btc[df_btc['timestamp'] <= ts]
            df15_s = df15.iloc[max(0, i-100):i+1].reset_index(drop=True)

            if len(df1h_s) < 30 or len(df4h_s) < 50:
                continue

            try:
                g = hesapla_gostergeler(df15_s, df1h_s, df4h_s)
                btc_t = None
                if len(df_btc_s) >= 50:
                    ema20 = df_btc_s['close'].ewm(span=20, adjust=False).mean().iloc[-1]
                    ema50 = df_btc_s['close'].ewm(span=50, adjust=False).mean().iloc[-1]
                    btc_t = "UP" if ema20 > ema50 else "DOWN"
                sig, _ = sinyal_uret(g, btc_t)
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
        return {"symbol": symbol, "islem": -1, "hata": str(e)}

# ==================== FLASK ====================
@app.route('/')
def home():
    return f"Bot Aktif | Pozisyon: {len(AKTIF_GRID_SISTEMLERI)}"

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
            f"🎯 *GÜVENLİ İŞLEM BOTU*\n\n"
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
    await update.message.reply_text("⏳ *Backtest başlatıldı...*\nSon 180 gün taranıyor. 2-3 dakika sürebilir. Sonuç gelince atacağım.", parse_mode='Markdown')

    def run():
        global BACKTEST_CALISIYOR
        try:
            satirlar = ["*📊 BACKTEST SONUÇLARI* (Son 180 gün)\n", "```"]
            satirlar.append(f"{'COIN':<16} {'İŞL':>4} {'WIN%':>6} {'PF':>6} {'EV%':>7} {'DD%':>6} {'TOT%':>7}")
            satirlar.append("-" * 60)
            for c in TAKIP_EDILENLER:
                r = tek_coin_backtest(c, gun_sayisi=180)
                if r.get('islem', -1) == -1:
                    satirlar.append(f"{c[:14]:<16} HATA: {r.get('hata','?')[:20]}")
                else:
                    satirlar.append(
                        f"{c[:14]:<16} {r['islem']:>4} {r['win_rate']:>6} {r['pf']:>6} {r['ev']:>7} {r['max_dd']:>6} {r['toplam']:>7}"
                    )
            satirlar.append("```")
            satirlar.append("\n*Yorumlama:*")
            satirlar.append("• PF > 1.3 ✅ | PF < 1.1 ❌")
            satirlar.append("• EV% > 0 ✅ | EV% < 0 ❌")
            satirlar.append("• İŞL > 30 → güvenilir sonuç")

            metin = "\n".join(satirlar)
            # Telegram 4096 karakter limiti
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
    print("🎯 [GÜVENLİ MOD] R/R 1:2, Komisyon dahil, BTC filtresi aktif.", flush=True)
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

            btc_trend = btc_trend_al()
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
                    o15 = exchange.fetch_ohlcv(symbol, '15m', limit=120)
                    o1h = exchange.fetch_ohlcv(symbol, '1h', limit=60)
                    o4h = exchange.fetch_ohlcv(symbol, '4h', limit=120)
                    df15 = pd.DataFrame(o15, columns=['timestamp','open','high','low','close','volume'])
                    df1h = pd.DataFrame(o1h, columns=['timestamp','open','high','low','close','volume'])
                    df4h = pd.DataFrame(o4h, columns=['timestamp','open','high','low','close','volume'])

                    g = hesapla_gostergeler(df15, df1h, df4h)
                    sig, neden = sinyal_uret(g, btc_trend)
                    if sig:
                        sinyaller.append({"symbol": symbol, **sig})
                        debug_mesaj.append(f"{symbol.split('/')[0]}: ✅ SİNYAL {sig['yon']}")
                    else:
                        debug_mesaj.append(f"{symbol.split('/')[0]}: {neden}")
                except Exception as e:
                    debug_mesaj.append(f"{symbol.split('/')[0]}: hata")
                    continue

            # Her 40 döngüde bir (yaklaşık 10 dk) log bas
            dongu_sayaci += 1
            if dongu_sayaci % 40 == 0:
                ozet = " | ".join(debug_mesaj[:7])
                print(f"🔍 Tarama #{dongu_sayaci} | BTC: {btc_trend} | {ozet}", flush=True)

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
                    ham

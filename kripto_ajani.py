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
ISLEM_BASI_RISK_PCT = 0.01        # Kasanın %1'i
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

# ==================== YARDIMCI FONKSİYONLAR ====================
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
    return bool(df['volume'].iloc[-1] > df['volume'].rolling(period).mean().iloc[-1])

def hesapla_gostergeler(df15, df1h, df4h):
    out = {}
    # 4h trend
    if len(df4h) >= 50:
        ema50_4h = ema_hesapla(df4h['close'], 50).iloc[-1]
        out['trend_4h_yon'] = "LONG" if df4h['close'].iloc[-1] > ema50_4h else "SHORT"
    else:
        out['trend_4h_yon'] = None

    # 1h trend
    ema7_1h = ema_hesapla(df1h['close'], 7).iloc[-1]
    ema21_1h = ema_hesapla(df1h['close'], 21).iloc[-1]
    out['trend_1h_yon'] = "LONG" if ema7_1h > ema21_1h else "SHORT"

    out['rsi'] = rsi_hesapla(df15['close'], 14)
    out['atr_pct'] = atr_hesapla(df15, 14)
    out['hacim_onay'] = hacim_onay(df15, 20)

    ema20_15 = ema_hesapla(df15['close'], 20).iloc[-1]
    out['fiyat_ema20_ustu'] = df15['close'].iloc[-1] > ema20_15

    # ADX basit versiyon (yönlü hareket)
    try:
        up = df15['high'].diff()
        down = -df15['low'].diff()
        plus_dm = np.where((up > down) & (up > 0), up, 0.0)
        minus_dm = np.where((down > up) & (down > 0), down, 0.0)
        tr = pd.concat([
            df15['high'] - df15['low'],
            (df15['high'] - df15['close'].shift()).abs(),
            (df15['low'] - df15['close'].shift()).abs()
        ], axis=1).max(axis=1)
        atr14 = tr.rolling(14).mean()
        plus_di = 100 * pd.Series(plus_dm).rolling(14).mean() / atr14
        minus_di = 100 * pd.Series(minus_dm).rolling(14).mean() / atr14
        dx = (abs(plus_di - minus_di) / (plus_di + minus_di).replace(0, np.nan)) * 100
        out['adx'] = float(dx.rolling(14).mean().iloc[-1]) if not pd.isna(dx.rolling(14).mean().iloc[-1]) else 0.0
    except Exception:
        out['adx'] = 0.0

    out['fiyat'] = float(df15['close'].iloc[-1])
    return out

def sinyal_uret(g, btc_trend=None):
    if g['trend_4h_yon'] is None:
        return None
    yon = g['trend_4h_yon']
    if g['trend_1h_yon'] != yon:
        return None
    if btc_trend is not None:
        if yon == "LONG" and btc_trend == "DOWN":
            return None
        if yon == "SHORT" and btc_trend == "UP":
            return None
    if not (0.4 <= g['atr_pct'] <= 4.0):
        return None
    if g['adx'] < 22:
        return None
    if yon == "LONG" and not (30 <= g['rsi'] <= 55):
        return None
    if yon == "SHORT" and not (45 <= g['rsi'] <= 70):
        return None
    if not g['hacim_onay']:
        return None
    if yon == "LONG" and not g['fiyat_ema20_ustu']:
        return None
    if yon == "SHORT" and g['fiyat_ema20_ustu']:
        return None

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
            "stop_pct": stop_pct, "tp_pct": tp_pct}

# ==================== FLASK ====================
@app.route('/')
def home():
    return f"Bot Aktif | Pozisyon: {len(AKTIF_GRID_SISTEMLERI)}"

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

# ==================== ANA DÖNGÜ ====================
def otomatik_arkaplan_tarayici():
    global BOT_CALISIYOR_MU, ANALITIK_HAFIZA
    print("🎯 [GÜVENLİ MOD] R/R 1:2, Komisyon dahil, BTC filtresi aktif.", flush=True)
    try:
        exchange.load_markets()
    except Exception:
        pass

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

            btc_trend = btc_trend_al()
            su_an = time.time()
            sinyaller = []
            mevcut_sektorler = aktif_sektorler()

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
                    o15 = exchange.fetch_ohlcv(symbol, '15m', limit=120)
                    o1h = exchange.fetch_ohlcv(symbol, '1h', limit=60)
                    o4h = exchange.fetch_ohlcv(symbol, '4h', limit=120)
                    df15 = pd.DataFrame(o15, columns=['timestamp','open','high','low','close','volume'])
                    df1h = pd.DataFrame(o1h, columns=['timestamp','open','high','low','close','volume'])
                    df4h = pd.DataFrame(o4h, columns=['timestamp','open','high','low','close','volume'])

                    g = hesapla_gostergeler(df15, df1h, df4h)
                    sig = sinyal_uret(g, btc_trend)
                    if sig:
                        sinyaller.append({"symbol": symbol, **sig})
                except Exception:
                    continue

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

                # Kasa × %1 = Kayıp edilecek USDT
                risk_usdt = kasa * ISLEM_BASI_RISK_PCT
                # Pozisyon değeri = Risk / Stop%  (kaldıraçtan bağımsız)
                pozisyon_degeri = risk_usdt / stop_pct
                # Marj = Pozisyon / Kaldıraç
                marj = pozisyon_degeri / KALDIRAC

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

                    # STOP (reduce-only market)
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

                    # TP (limit reduce-only)
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
                        f"🎯 *İŞLEM AÇILDI*\n"
                        f"📌 `{symbol}` | *{yon}*\n"
                        f"💰 Giriş: `{giris}`\n"
                        f"🎯 TP: `{tp}` | 🛑 SL: `{stop}`\n"
                        f"📊 Miktar: `{miktar}` | Risk: `{risk_usdt:.2f} USDT`"
                    )
                    break
                except Exception as e:
                    print(f"❌ İşlem açma hatası: {e}", flush=True)

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

    app_tg.run_polling()

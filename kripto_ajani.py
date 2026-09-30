import os
import sys

os.environ['PYTHONUNBUFFERED'] = '1'
try:
    sys.stdout = open(sys.stdout.fileno(), mode='w', encoding='utf-8', buffering=1)
except Exception:
    sys.stdout.reconfigure(line_buffering=True)

import time
import threading
import asyncio
import requests
import ccxt
import pandas as pd
import ta
import numpy as np
from dotenv import load_dotenv
from flask import Flask
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler
from supabase import create_client, Client

if os.path.exists('/etc/secrets/.env'):
    load_dotenv('/etc/secrets/.env', override=True)
    print("✅ .env yüklendi", flush=True)
else:
    load_dotenv(override=True)

app = Flask(__name__)

@app.route('/')
def home():
    return "Bot aktif!"

def run_web():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.environ.get("CHAT_ID", "").strip()
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "").strip()

if not SUPABASE_URL or not SUPABASE_KEY:
    print("❌ SUPABASE boş!", flush=True); sys.exit(1)
if not TELEGRAM_TOKEN or not CHAT_ID:
    print("❌ TELEGRAM boş!", flush=True); sys.exit(1)

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

exchange = ccxt.gate({
    'apiKey': os.environ.get("GATE_API_KEY", "").strip(),
    'secret': os.environ.get("GATE_SECRET", "").strip(),
    'enableRateLimit': True,
    'timeout': 30000,
    'options': {'defaultType': 'swap'}
})
exchange.set_sandbox_mode(True)

# ==================== AYARLAR ====================
HARIC_TUTULANLAR = ['BTC/USDT:USDT', 'ETH/USDT:USDT', 'AVAX/USDT:USDT']

YEDEK_LISTE = [
    'SOL/USDT:USDT', 'XRP/USDT:USDT', 'DOGE/USDT:USDT',
    'LTC/USDT:USDT', 'LINK/USDT:USDT',
    'ADA/USDT:USDT', 'DOT/USDT:USDT'
]

COIN_SAYISI = 10
MIN_HACIM_USD = 20_000_000  # 20M (daha likit)
CACHE_SURESI = 60 * 60      # 1 saat cache

KALDIRAC = 3                # Düşük kaldıraç
KASA_ORANI = 0.15           # Kasanın %15'i
MAKSIMUM_TOPLAM_POZISYON = 1  # Tek pozisyon
COOLDOWN_SURESI = 4 * 60 * 60  # 4 saat

KOMISYON_ORANI = 0.0008

# Trailing stop
TRAILING_BASABAS_ROE = 5.0   # +%5 RoE → SL başabaş
TRAILING_KAR_ROE = 10.0      # +%10 RoE → SL +%5

# Günlük kayıp limiti
GUNLUK_KAYIP_LIMIT = 5.0     # Kasa %5 düşerse dur

# ==================== DURUM ====================
BOT_CALISIYOR_MU = True
state_lock = threading.Lock()
tarayici_kilidi = threading.Lock()
liste_kilidi = threading.Lock()
SON_BTC_TREND = "YATAY"
TAKIP_EDILENLER = YEDEK_LISTE.copy()
SON_LISTE_GUNCELLE = 0
GUN_BASI_KASA = None
BUGUNUN_TARIHI = None

def volatil_coinleri_bul():
    global TAKIP_EDILENLER, SON_LISTE_GUNCELLE

    with liste_kilidi:
        if time.time() - SON_LISTE_GUNCELLE < CACHE_SURESI and TAKIP_EDILENLER:
            return TAKIP_EDILENLER

    print("🔍 [COİN SEÇİMİ] Volatil coinler taranıyor...", flush=True)

    try:
        tickers = exchange.fetch_tickers()
        skorlar = []
        for sym, t in tickers.items():
            if not sym.endswith(':USDT'):
                continue
            if sym in HARIC_TUTULANLAR:
                continue
            try:
                high = float(t.get('high') or 0)
                low = float(t.get('low') or 0)
                vol = float(t.get('quoteVolume') or 0)
                if high <= 0 or low <= 0 or vol < MIN_HACIM_USD:
                    continue
                range_orani = (high - low) / low
                skor = range_orani * (vol ** 0.5)
                skorlar.append((sym, skor, range_orani, vol))
            except:
                continue

        if not skorlar:
            return YEDEK_LISTE

        skorlar.sort(key=lambda x: x[1], reverse=True)
        secilenler = [s[0] for s in skorlar[:COIN_SAYISI]]

        print(f"📊 [COİN SEÇİMİ] İlk {len(secilenler)} coin:", flush=True)
        for i, s in enumerate(skorlar[:COIN_SAYISI], 1):
            print(f"   {i}. {s[0]} | Range:%{s[2]*100:.2f} | Hacim:{s[3]/1_000_000:.0f}M", flush=True)

        with liste_kilidi:
            TAKIP_EDILENLER = secilenler
            SON_LISTE_GUNCELLE = time.time()

        return secilenler
    except Exception as e:
        print(f"⚠️ Coin seçim hatası: {e}", flush=True)
        return YEDEK_LISTE

# ==================== HAFIZA ====================
def hafizayi_yukle():
    try:
        response = supabase.table("bot_hafiza").select("*").eq("id", 1).execute()
        if response.data and len(response.data) > 0:
            veri = response.data[0]
            return {
                "aktif_sistemler": veri.get("aktif_sistemler", {}),
                "analitik": veri.get("analitik", {"basarili_islem_sayisi": 0, "basarisiz_islem_sayisi": 0}),
                "cooldownlar": veri.get("cooldownlar", {})
            }
    except Exception as e:
        print(f"⚠️ Hafıza: {e}", flush=True)
    return {"aktif_sistemler": {}, "analitik": {"basarili_islem_sayisi": 0, "basarisiz_islem_sayisi": 0}, "cooldownlar": {}}

def hafizayi_kaydet():
    with state_lock:
        try:
            payload = {
                "basarili_islem_sayisi": int(ANALITIK.get("basarili_islem_sayisi", 0)),
                "basarisiz_islem_sayisi": int(ANALITIK.get("basarisiz_islem_sayisi", 0))
            }
            clean_cd = {}
            for k, v in COIN_COOLDOWN.items():
                if isinstance(v, dict):
                    clean_cd[k] = {"zaman": float(v.get("zaman", 0)), "son_yon": str(v.get("son_yon", ""))}
                else:
                    clean_cd[k] = {"zaman": float(v), "son_yon": ""}

            supabase.table("bot_hafiza").upsert({
                "id": 1, "aktif_sistemler": AKTIF_POZISYONLAR,
                "analitik": payload, "cooldownlar": clean_cd
            }).execute()
        except Exception as e:
            print(f"⚠️ Kayıt: {e}", flush=True)

kalici = hafizayi_yukle()
AKTIF_POZISYONLAR = kalici.get("aktif_sistemler", {})
ANALITIK = kalici.get("analitik", {"basarili_islem_sayisi": 0, "basarisiz_islem_sayisi": 0})
COIN_COOLDOWN = kalici.get("cooldownlar", {})

# ==================== BTC GÜNLÜK TREND ====================
def btc_gunluk_trend():
    global SON_BTC_TREND
    try:
        ohlcv = exchange.fetch_ohlcv('BTC/USDT:USDT', timeframe='1d', limit=250)
        df = pd.DataFrame(ohlcv, columns=['t', 'o', 'h', 'l', 'c', 'v'])

        ema20 = ta.trend.ema_indicator(df['c'], window=20).iloc[-1]
        ema50 = ta.trend.ema_indicator(df['c'], window=50).iloc[-1]
        ema200 = ta.trend.ema_indicator(df['c'], window=200).iloc[-1]
        fiyat = df['c'].iloc[-1]

        if ema20 > ema50 > ema200 and fiyat > ema20:
            trend = "LONG"
        elif ema20 < ema50 < ema200 and fiyat < ema20:
            trend = "SHORT"
        else:
            trend = "YATAY"

        SON_BTC_TREND = trend
        print(f"📊 [BTC GÜNLÜK] Trend: {trend} | EMA20:{ema20:.0f} EMA50:{ema50:.0f} EMA200:{ema200:.0f}", flush=True)
        return trend
    except Exception as e:
        print(f"⚠️ BTC trend hatası: {e}", flush=True)
        return "YATAY"

# ==================== COİN YAPISI (4h) ====================
def coin_yapisi(symbol, btc_trend):
    """Coin 4h yapısı BTC ile uyumlu mu?"""
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe='4h', limit=100)
        df = pd.DataFrame(ohlcv, columns=['t', 'o', 'h', 'l', 'c', 'v'])

        if len(df) < 50:
            return False, "yetersiz veri"

        ema50 = ta.trend.ema_indicator(df['c'], window=50).iloc[-1]
        ema200 = ta.trend.ema_indicator(df['c'], window=200).iloc[-1] if len(df) >= 200 else ema50
        fiyat = df['c'].iloc[-1]

        # Swing high/low analizi (son 30 mum)
        son_30 = df.iloc[-30:]
        highs = son_30['h'].values
        lows = son_30['l'].values

        # Higher High + Higher Low kontrolü
        ilk_yarim = slice(0, 15)
        ikinci_yarim = slice(15, 30)

        hh = max(highs[ikinci_yarim]) > max(highs[ilk_yarim])
        hl = min(lows[ikinci_yarim]) > min(lows[ilk_yarim])

        # Lower High + Lower Low
        lh = max(highs[ikinci_yarim]) < max(highs[ilk_yarim])
        ll = min(lows[ikinci_yarim]) < min(lows[ilk_yarim])

        if btc_trend == "LONG":
            if fiyat > ema50 and hh and hl:
                return True, f"LONG yapı (HH+HL, EMA50 üstü)"
            return False, "LONG yapı yok"
        elif btc_trend == "SHORT":
            if fiyat < ema50 and lh and ll:
                return True, f"SHORT yapı (LH+LL, EMA50 altı)"
            return False, "SHORT yapı yok"
        return False, "BTC trend yok"
    except Exception as e:
        return False, f"hata: {e}"

# ==================== GİRİŞ NOKTASI (1h) ====================
def giris_noktasi(symbol, yon):
    """1h grafikte pullback + hacim kontrolü"""
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe='1h', limit=100)
        df = pd.DataFrame(ohlcv, columns=['t', 'o', 'h', 'l', 'c', 'v'])

        if len(df) < 50:
            return False, "yetersiz veri", 0, 0, 0

        ema20 = ta.trend.ema_indicator(df['c'], window=20).iloc[-1]
        ema50 = ta.trend.ema_indicator(df['c'], window=50).iloc[-1]
        fiyat = df['c'].iloc[-1]

        # Hacim kontrolü
        son_hacim = float(df['v'].iloc[-1])
        ort_hacim = float(df['v'].iloc[-21:-1].mean())
        hacim_orani = son_hacim / ort_hacim if ort_hacim > 0 else 1

        # ATR (SL için)
        atr = ta.volatility.AverageTrueRange(df['h'], df['l'], df['c'], window=14).average_true_range().iloc[-1]

        # Pullback kontrolü: fiyat son 10 mumda EMA20'ye yaklaştı mı?
        son_10 = df.iloc[-10:]
        min_fiyat = son_10['l'].min()
        max_fiyat = son_10['h'].max()

        if yon == "LONG":
            # Fiyat EMA20 üstünde ve son 10 mumda EMA20'ye değdi mi?
            if fiyat > ema20 > ema50:
                # Son 3 mum yeşil (momentum)
                son_3 = df['c'].iloc[-3:].values
                momentum = son_3[0] < son_3[1] < son_3[2]
                # Hacim onay
                if momentum and hacim_orani > 1.2:
                    return True, f"LONG pullback + momentum + hacim x{hacim_orani:.1f}", atr, ema50, fiyat
            return False, "LONG pullback yok", 0, 0, 0
        elif yon == "SHORT":
            if fiyat < ema20 < ema50:
                son_3 = df['c'].iloc[-3:].values
                momentum = son_3[0] > son_3[1] > son_3[2]
                if momentum and hacim_orani > 1.2:
                    return True, f"SHORT pullback + momentum + hacim x{hacim_orani:.1f}", atr, ema50, fiyat
            return False, "SHORT pullback yok", 0, 0, 0

        return False, "yön belirsiz", 0, 0, 0
    except Exception as e:
        return False, f"hata: {e}", 0, 0, 0

# ==================== TP/SL HESABI ====================
def seviye_hesapla(fiyat, yon, atr):
    """ATR bazlı geniş TP/SL"""
    if yon == "LONG":
        sl = fiyat - (atr * 2.0)
        tp = fiyat + (atr * 4.0)  # R/R 2.0
        return tp, sl, 'sell'
    else:
        sl = fiyat + (atr * 2.0)
        tp = fiyat - (atr * 4.0)
        return tp, sl, 'buy'

# ==================== TELEGRAM ====================
def tg_gonder(mesaj):
    if not TELEGRAM_TOKEN or not CHAT_ID: return
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                      json={"chat_id": CHAT_ID, "text": mesaj, "parse_mode": "Markdown"}, timeout=5)
    except: pass

async def durum_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != int(CHAT_ID): return
    try:
        balance = await asyncio.to_thread(exchange.fetch_balance)
        total = float(balance['total'].get('USDT', 0))
        pos = [p for p in await asyncio.to_thread(exchange.fetch_positions) if float(p.get('contracts', 0) or p.get('size', 0) or 0) > 0]
        pnl = sum(float(p.get('unrealizedPnl', 0)) for p in pos)
        trend = await asyncio.to_thread(btc_gunluk_trend)

        bas = int(ANALITIK.get("basarili_islem_sayisi", 0))
        basz = int(ANALITIK.get("basarisiz_islem_sayisi", 0))
        top = bas + basz
        oran = (bas / top * 100) if top > 0 else 0

        pos_detay = ""
        for p in pos:
            sym = p['symbol']
            y = str(p.get('side', '')).upper() or "?"
            g = float(p.get('entryPrice', 0))
            k = int(p.get('leverage', 3))
            t = await asyncio.to_thread(exchange.fetch_ticker, sym)
            gf = float(t['last'])
            f = (gf - g) / g if y == "LONG" else (g - gf) / g
            roe = f * 100 * k
            pos_detay += f"\n• `{sym}` | {y} ({k}x)\n  Giriş: `{g}` | ROE: `%{roe:+.2f}`"

        liste = ", ".join([s.split('/')[0] for s in TAKIP_EDILENLER[:10]])

        mesaj = (
            f"📊 *DURUM* [Yapı Analizi]\n\n"
            f"🌐 BTC Günlük: `{trend}`\n"
            f"💰 Kasa: `{total:.2f} USDT` | PnL: `{pnl:+.2f}`\n"
            f"📌 Açık: `{len(pos)} / {MAKSIMUM_TOPLAM_POZISYON}`\n"
            f"🎯 Takip: `{liste}`"
            f"{pos_detay}\n\n"
            f"✅ TP: `{bas}` | ❌ SL: `{basz}`\n"
            f"📈 Başarı: `%{oran:.1f}`"
        )
        await update.message.reply_text(mesaj, parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"Hata: {e}")

async def baslat_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != int(CHAT_ID): return
    global BOT_CALISIYOR_MU
    BOT_CALISIYOR_MU = True
    await update.message.reply_text("🟢 Bot aktif! (Yapı analizi)")

async def durdur_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != int(CHAT_ID): return
    global BOT_CALISIYOR_MU
    BOT_CALISIYOR_MU = False
    await update.message.reply_text("⏸️ Bot durduruldu.")

async def kapat_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != int(CHAT_ID): return
    try:
        positions = await asyncio.to_thread(exchange.fetch_positions)
        for p in positions:
            k = float(p.get('contracts', 0) or p.get('size', 0) or 0)
            if k > 0:
                y = str(p.get('side', '')).upper() or "LONG"
                ky = 'sell' if y == 'LONG' else 'buy'
                try: exchange.cancel_all_orders(p['symbol'])
                except: pass
                exchange.create_order(p['symbol'], 'market', ky, k, None, {'reduceOnly': True})
        await update.message.reply_text("✅ Kapatıldı.")
    except Exception as e:
        await update.message.reply_text(f"Hata: {e}")

# ==================== ANA DÖNGÜ ====================
def tarayici():
    global BUGUNUN_TARIHI, GUN_BASI_KASA
    print("🚀 [BAŞLANGIÇ] Yapı analizi sistemi...", flush=True)
    try:
        exchange.load_markets()
    except: pass

    dongu = 0

    while True:
        if not tarayici_kilidi.acquire(blocking=False):
            time.sleep(2)
            continue

        try:
            if not BOT_CALISIYOR_MU:
                time.sleep(5)
                continue

            dongu += 1
            print(f"\n{'='*55}", flush=True)
            print(f"🔄 [DÖNGÜ #{dongu}] {time.strftime('%H:%M:%S')}", flush=True)

            # Günlük kayıp kontrolü
            bugun = time.strftime('%Y-%m-%d')
            try:
                balance = exchange.fetch_balance()
                kasa = float(balance['total'].get('USDT', 0))
                if BUGUNUN_TARIHI != bugun:
                    BUGUNUN_TARIHI = bugun
                    GUN_BASI_KASA = kasa
                    print(f"📅 Yeni gün: Kasa başı {kasa:.2f} USDT", flush=True)
                elif GUN_BASI_KASA and kasa < GUN_BASI_KASA * (1 - GUNLUK_KAYIP_LIMIT/100):
                    print(f"🛑 GÜNLÜK KAYIP LİMİTİ! Kasa: {kasa:.2f} < {GUN_BASI_KASA:.2f}", flush=True)
                    print(f"🛑 Bot bugün için durduruldu.", flush=True)
                    time.sleep(3600)
                    continue
            except Exception as e:
                print(f"⚠️ Kasa kontrol: {e}", flush=True)

            # Coin listesi
            volatil_coinleri_bul()

            # BTC günlük trend
            btc_trend = btc_gunluk_trend()

            if btc_trend == "YATAY":
                print(f"⏸️ BTC günlük trend yok — işlem açılmıyor", flush=True)

            # Mevcut pozisyonları al
            try:
                raw = exchange.fetch_positions()
                aktif_map = {}
                aktif_list = []
                for p in raw:
                    k = float(p.get('contracts', 0) or p.get('size', 0) or 0)
                    if k > 0:
                        s = p['symbol']
                        aktif_map[s] = p
                        aktif_list.append(s)
            except:
                raw = []
                aktif_map = {}
                aktif_list = []

            # ==================== POZİSYON KAPANIŞ ====================
            try:
                anlik = [p['symbol'] for p in raw if float(p.get('contracts', 0) or p.get('size', 0) or 0) > 0]
                for eski in list(AKTIF_POZISYONLAR.keys()):
                    if eski not in anlik:
                        bilgi = AKTIF_POZISYONLAR[eski]
                        g = bilgi.get("giris_fiyati", 0)
                        y = bilgi.get("yon", "LONG")
                        tpk = bilgi.get("tp_fiyat", g)
                        slk = bilgi.get("sl_fiyat", g)

                        karli = False
                        cikis = g
                        try:
                            t = exchange.fetch_ticker(eski)
                            cikis = float(t['last'])
                            karli = abs(cikis - tpk) < abs(cikis - slk)
                        except:
                            karli = cikis > g if y == "LONG" else cikis < g

                        with state_lock:
                            b = int(ANALITIK.get("basarili_islem_sayisi", 0))
                            bz = int(ANALITIK.get("basarisiz_islem_sayisi", 0))
                            if karli:
                                b += 1; tip = "✅ *KÂRLA KAPANDI*"
                            else:
                                bz += 1; tip = "❌ *ZARARLA KAPANDI*"
                            ANALITIK["basarili_islem_sayisi"] = b
                            ANALITIK["basarisiz_islem_sayisi"] = bz
                            COIN_COOLDOWN[eski] = {"zaman": float(time.time() + COOLDOWN_SURESI), "son_yon": y}
                            if eski in AKTIF_POZISYONLAR:
                                del AKTIF_POZISYONLAR[eski]

                        hafizayi_kaydet()
                        print(f"💰 [KAPANIŞ] {eski} | Çıkış: {cikis}", flush=True)
                        tg_gonder(f"{tip}\n📌 `{eski}` | Çıkış: `{cikis}`")
            except Exception as e:
                print(f"⚠️ Kapanış: {e}", flush=True)

            # ==================== TRAILING STOP ====================
            try:
                with state_lock:
                    aktif_p = list(AKTIF_POZISYONLAR.items())

                for sym, kayit in aktif_p:
                    if sym not in AKTIF_POZISYONLAR:
                        continue
                    y = kayit.get("yon", "LONG")
                    g = float(kayit.get("giris_fiyati", 0))
                    sl_k = float(kayit.get("sl_fiyat", 0))
                    kaldirac_v = int(kayit.get("kaldirac", KALDIRAC))
                    giris_zaman = float(kayit.get("giris_zamani", 0))

                    # İlk 5 dakika trailing yok
                    if time.time() - giris_zaman < 300:
                        continue

                    try:
                        t = exchange.fetch_ticker(sym)
                        anlik = float(t['last'])
                    except:
                        continue

                    if y == "LONG":
                        roe = (anlik - g) / g * 100 * kaldirac_v
                    else:
                        roe = (g - anlik) / g * 100 * kaldirac_v

                    yeni_sl = None

                    # Trailing seviyeleri
                    if roe >= TRAILING_KAR_ROE:
                        # +%10 RoE → SL +%5
                        if y == "LONG":
                            yeni_sl = g * (1 + 0.05 / kaldirac_v)
                        else:
                            yeni_sl = g * (1 - 0.05 / kaldirac_v)
                    elif roe >= TRAILING_BASABAS_ROE:
                        # +%5 RoE → SL başabaş
                        yeni_sl = g

                    if yeni_sl:
                        # SL iyileştir mi?
                        if y == "LONG" and yeni_sl > sl_k:
                            guncelle = True
                        elif y == "SHORT" and yeni_sl < sl_k:
                            guncelle = True
                        else:
                            guncelle = False

                        if guncelle:
                            try:
                                exchange.cancel_all_orders(sym)
                                # Yeni SL emri
                                miktar = None
                                for p in exchange.fetch_positions():
                                    if p['symbol'] == sym:
                                        miktar = float(p.get('contracts', 0) or p.get('size', 0) or 0)
                                        break
                                if miktar and miktar > 0:
                                    ky = 'sell' if y == "LONG" else 'buy'
                                    exchange.create_order(sym, 'stop', ky, miktar, yeni_sl, {'stopPrice': yeni_sl, 'reduceOnly': True})
                                    with state_lock:
                                        if sym in AKTIF_POZISYONLAR:
                                            AKTIF_POZISYONLAR[sym]["sl_fiyat"] = yeni_sl
                                    print(f"🔒 [TRAILING] {sym} SL güncellendi: {yeni_sl:.6f} (RoE:%{roe:.1f})", flush=True)
                            except Exception as e:
                                print(f"   ⚠️ Trailing hatası: {e}", flush=True)
            except Exception as e:
                print(f"⚠️ Trailing genel: {e}", flush=True)

            # ==================== YENİ İŞLEM ====================
            print(f"{'─'*55}", flush=True)
            print(f"🔍 [TARAMA] BTC:{btc_trend} | {len(TAKIP_EDILENLER)} coin", flush=True)
            print(f"{'─'*55}", flush=True)

            if btc_trend in ["LONG", "SHORT"]:
                for symbol in TAKIP_EDILENLER:
                    if not BOT_CALISIYOR_MU: break
                    if len(aktif_map) >= MAKSIMUM_TOPLAM_POZISYON: break
                    if symbol in aktif_list: continue

                    # Cooldown
                    with state_lock:
                        cd = COIN_COOLDOWN.get(symbol)
                        if cd:
                            z = cd.get("zaman", 0) if isinstance(cd, dict) else float(cd)
                            if z - time.time() > 0:
                                continue

                    # Yapı kontrolü (4h)
                    uygun, sebep = coin_yapisi(symbol, btc_trend)
                    if not uygun:
                        continue

                    print(f"✅ [{symbol}] Yapı uygun: {sebep}", flush=True)

                    # Giriş noktası (1h)
                    giris_uygun, giris_sebep, atr, ema50, fiyat = giris_noktasi(symbol, btc_trend)
                    if not giris_uygun:
                        continue

                    print(f"✅ [{symbol}] Giriş: {giris_sebep}", flush=True)

                    # TP/SL hesabı
                    tp, sl, kapat_y = seviye_hesapla(fiyat, btc_trend, atr)

                    # R/R
                    tp_m = abs(tp - fiyat)
                    sl_m = abs(sl - fiyat)
                    kom = fiyat * KOMISYON_ORANI
                    net_rr = (tp_m - kom) / (sl_m + kom) if (sl_m + kom) > 0 else 0

                    if net_rr < 1.5:
                        print(f"⏭️ [{symbol}] R/R düşük: {net_rr:.2f}", flush=True)
                        continue

                    # İşlem aç
                    try:
                        b = exchange.fetch_balance()
                        toplam = float(b['total'].get('USDT', 0))
                        serbest = float(b.get('free', {}).get('USDT', 0) or 0)

                        exchange.set_leverage(KALDIRAC, symbol)
                        market = exchange.market(symbol)

                        kullan = min(toplam * KASA_ORANI, serbest)
                        if kullan < 1: continue

                        miktar = float(exchange.amount_to_precision(
                            symbol,
                            max((kullan * KALDIRAC) / fiyat / float(market.get('contractSize', 1.0)),
                                float(market['limits']['amount']['min'] or 1.0))
                        ))

                        iy = 'buy' if btc_trend == "LONG" else 'sell'
                        exchange.create_order(symbol, 'market', iy, miktar)
                        time.sleep(0.5)

                        sl_ok = False
                        try:
                            exchange.create_order(symbol, 'limit', kapat_y, miktar, tp, {'reduceOnly': True})
                            exchange.create_order(symbol, 'stop', kapat_y, miktar, sl, {'stopPrice': sl, 'reduceOnly': True})
                            sl_ok = True
                        except Exception as e:
                            print(f"   ⚠️ TP/SL: {e}", flush=True)

                        if not sl_ok:
                            try:
                                exchange.create_order(symbol, 'market', kapat_y, miktar, None, {'reduceOnly': True})
                            except: pass
                            continue

                        with state_lock:
                            AKTIF_POZISYONLAR[symbol] = {
                                "giris_fiyati": fiyat, "yon": btc_trend,
                                "tp_fiyat": tp, "sl_fiyat": sl,
                                "giris_zamani": time.time(),
                                "kaldirac": KALDIRAC,
                                "atr": atr
                            }
                            aktif_map[symbol] = {"dummy": True}
                            aktif_list.append(symbol)

                        hafizayi_kaydet()
                        print(f"✅ [AÇILDI] {symbol} {btc_trend} @ {fiyat} ({KALDIRAC}x)", flush=True)

                        tg_gonder(
                            f"🎯 *İŞLEM* ({btc_trend} - {KALDIRAC}x)\n"
                            f"📌 `{symbol}`\n"
                            f"🎯 Giriş: `{fiyat}`\n"
                            f"💰 TP: `{tp}`\n"
                            f"🛑 SL: `{sl}`\n"
                            f"📊 Net R/R: `{net_rr:.2f}`\n"
                            f"📈 Yapı: {sebep}"
                        )
                        break
                    except Exception as e:
                        print(f"⚠️ Emir hatası: {e}", flush=True)
                        continue
            else:
                print(f"⏸️ BTC YATAY — işlem açılmıyor", flush=True)

        except Exception as e:
            print(f"⚠️ Ana döngü: {e}", flush=True)
        finally:
            try: tarayici_kilidi.release()
            except: pass

        time.sleep(60)  # 60 saniye (gürültüsüz)

async def main():
    web = threading.Thread(target=run_web, daemon=True)
    web.start()

    app_tg = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    try:
        requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/deleteWebhook?drop_pending_updates=True", timeout=5)
    except: pass

    app_tg.add_handler(CommandHandler("durum", durum_komutu))
    app_tg.add_handler(CommandHandler("baslat", baslat_komutu))
    app_tg.add_handler(CommandHandler("durdur", durdur_komutu))
    app_tg.add_handler(CommandHandler("kapat", kapat_komutu))

    await app_tg.initialize()
    await app_tg.start()
    await app_tg.updater.start_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

    t = threading.Thread(target=tarayici, daemon=True)
    t.start()

    stop = asyncio.Event()
    await stop.wait()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass

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

# ==================== .env ====================
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

TAKIP_EDILENLER = ['SOL/USDT:USDT', 'XRP/USDT:USDT', 'DOGE/USDT:USDT', 'LTC/USDT:USDT', 'LINK/USDT:USDT']

BOT_CALISIYOR_MU = True
state_lock = threading.Lock()
tarayici_kilidi = threading.Lock()

KALDIRAC = 5
MAKSIMUM_TOPLAM_POZISYON = 2
COOLDOWN_SURESI_SANIYE = 30 * 60

# 🎯 LİKİDİTE AVI AYARLARI
SWING_LOOKBACK = 50           # Swing high/low tarama aralığı
MIN_SWING_MESAFE = 0.005      # Min %0.5 swing mesafesi (gürültü filtresi)
SWEEP_TOLERANCE = 0.003       # Sweep toleransı (seviyeden %0.3 fazla kırılım)
SWEEP_MUM_SAYISI = 2          # Kaç mum içinde geri dönmeli
MIN_RR = 2.0                  # Minimum Risk/Reward

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

# ==================== LİKİDİTE SEVİYELERİ ====================
def swing_noktalari_bul(df, lookback=SWING_LOOKBACK):
    """Son N mumda swing high/low'ları bul"""
    highs = []
    lows = []
    baslangic = max(2, len(df) - lookback)

    for i in range(baslangic, len(df) - 2):
        h = df['high'].iloc[i]
        l = df['low'].iloc[i]

        # Swing high
        if (h > df['high'].iloc[i-1] and h > df['high'].iloc[i-2] and
            h > df['high'].iloc[i+1] and h > df['high'].iloc[i+2]):
            highs.append((i, h))

        # Swing low
        if (l < df['low'].iloc[i-1] and l < df['low'].iloc[i-2] and
            l < df['low'].iloc[i+1] and l < df['low'].iloc[i+2]):
            lows.append((i, l))

    return highs, lows

def likidite_seviyeleri(df, highs, lows, anlik_fiyat):
    """Sadece anlamlı likidite seviyeleri (min mesafe filtresi)"""
    # Yakın seviyeleri birleştir (cluster)
    def cluster(levels, tolerance=0.003):
        if not levels:
            return []
        sorted_l = sorted(levels, key=lambda x: x[1])
        result = [sorted_l[0][1]]
        for _, lvl in sorted_l[1:]:
            if abs(lvl - result[-1]) / result[-1] > tolerance:
                result.append(lvl)
        return result

    high_levels = cluster([h[1] for h in highs])
    low_levels = cluster([l[1] for l in lows])

    # Anlık fiyata yakın olanları filtrele (anlamlı olanlar)
    # Long için: fiyatın altındaki likidite
    # Short için: fiyatın üstündeki likidite
    destekler = [l for l in low_levels if l < anlik_fiyat]
    direncler = [h for h in high_levels if h > anlik_fiyat]

    # En yakın 3 tanesini al
    destekler = sorted(destekler, reverse=True)[:3]
    direncler = sorted(direncler)[:3]

    return destekler, direncler

def sweep_tespit_et(df, destekler, direncler, anlik_fiyat):
    """
    Likidite avı tespit et.
    LONG için: Fiyat desteği aşağı sıyırdı, geri döndü
    SHORT için: Fiyat direnci yukarı sıyırdı, geri döndü
    """
    if len(df) < 3:
        return None, None, None, None

    son_mum = df.iloc[-1]
    onceki = df.iloc[-2]
    iki_onceki = df.iloc[-3]

    # === BULLISH SWEEP (SHORT SL avı sonrası LONG) ===
    # Fiyat desteğin altına sarkıp geri döndü
    for destek in destekler:
        # Son 2-3 mumda destek kırıldı mı?
        kirildi = False
        en_dusuk = 999999

        if iki_onceki['low'] < destek:
            kirildi = True
            en_dusuk = min(iki_onceki['low'], en_dusuk)
        if onceki['low'] < destek:
            kirildi = True
            en_dusuk = min(onceki['low'], en_dusuk)

        # Geri döndü mü? Son mum destek üstünde kapandı mı?
        if kirildi and son_mum['close'] > destek and anlik_fiyat > destek:
            # Kırılım çok fazla mı? (gerçek trend vs fake sweep)
            kirilma_orani = (destek - en_dusuk) / destek
            if kirilma_orani < 0.02:  # %2'den fazla kırılmadıysa = sweep
                # Yönü LONG
                sl_fiyat = en_dusuk * (1 - 0.001)  # Sweep ucunun %0.1 altı
                return "LONG", destek, sl_fiyat, en_dusuk

    # === BEARISH SWEEP (LONG SL avı sonrası SHORT) ===
    # Fiyat direncin üstüne çıkıp geri döndü
    for direnc in direncler:
        kirildi = False
        en_yuksek = 0

        if iki_onceki['high'] > direnc:
            kirildi = True
            en_yuksek = max(iki_onceki['high'], en_yuksek)
        if onceki['high'] > direnc:
            kirildi = True
            en_yuksek = max(onceki['high'], en_yuksek)

        if kirildi and son_mum['close'] < direnc and anlik_fiyat < direnc:
            kirilma_orani = (en_yuksek - direnc) / direnc
            if kirilma_orani < 0.02:
                sl_fiyat = en_yuksek * (1 + 0.001)
                return "SHORT", direnc, sl_fiyat, en_yuksek

    return None, None, None, None

def tp_hesapla(yon, giris, sl, destekler, direncler, atr):
    """Sonraki likidite seviyesini TP yap"""
    if yon == "LONG":
        # Sonraki direnç = TP
        for d in direncler:
            if d > giris:
                tp = d
                # Min R/R kontrol
                rr = (tp - giris) / (giris - sl) if giris > sl else 0
                if rr >= MIN_RR:
                    return tp, rr
        # Alternatif: ATR × 3
        tp = giris + (atr * 3.0)
        rr = (tp - giris) / (giris - sl) if giris > sl else 0
        return tp, rr
    else:
        for d in sorted(destekler, reverse=True):
            if d < giris:
                tp = d
                rr = (giris - tp) / (sl - giris) if sl > giris else 0
                if rr >= MIN_RR:
                    return tp, rr
        tp = giris - (atr * 3.0)
        rr = (giris - tp) / (sl - giris) if sl > giris else 0
        return tp, rr

def tg_gonder(mesaj):
    if not TELEGRAM_TOKEN or not CHAT_ID: return
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                      json={"chat_id": CHAT_ID, "text": mesaj, "parse_mode": "Markdown"}, timeout=5)
    except: pass

# ==================== TELEGRAM KOMUTLARI ====================
async def durum_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != int(CHAT_ID): return
    try:
        balance = await asyncio.to_thread(exchange.fetch_balance)
        total = float(balance['total'].get('USDT', 0))
        pos = [p for p in await asyncio.to_thread(exchange.fetch_positions) if float(p.get('contracts', 0) or p.get('size', 0) or 0) > 0]
        pnl = sum(float(p.get('unrealizedPnl', 0)) for p in pos)
        bas = int(ANALITIK.get("basarili_islem_sayisi", 0))
        basz = int(ANALITIK.get("basarisiz_islem_sayisi", 0))
        top = bas + basz
        oran = (bas / top * 100) if top > 0 else 0

        pos_detay = ""
        for p in pos:
            sym = p['symbol']
            y = str(p.get('side', '')).upper() or "?"
            g = float(p.get('entryPrice', 0))
            k = int(p.get('leverage', KALDIRAC))
            t = await asyncio.to_thread(exchange.fetch_ticker, sym)
            gf = float(t['last'])
            f = (gf - g) / g if y == "LONG" else (g - gf) / g
            roe = f * 100 * k
            pos_detay += f"\n• `{sym}` | {y} ({k}x)\n  Giriş: `{g}` | ROE: `%{roe:+.2f}`"

        mesaj = (
            f"📊 *DURUM* [LİKİDİTE AVI]\n\n"
            f"💰 Kasa: `{total:.2f} USDT` | PnL: `{pnl:+.2f}`\n"
            f"📌 Açık: `{len(pos)} / {MAKSIMUM_TOPLAM_POZISYON}`"
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
    await update.message.reply_text("🟢 Likidite Avı Bot aktif!")

async def durdur_komutu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != int(CHAT_ID): return
    global BOT_CALISIYOR_MU
    BOT_CALISIYOR_MU = False
    await update.message.reply_text("⏸️ Durduruldu.")

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

# ==================== ANA TARAYICI ====================
def tarayici():
    print("🚀 [BAŞLANGIÇ] LİKİDİTE AVI Botu Aktif...", flush=True)
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

            # KAPANIŞ KONTROLÜ
            try:
                anlik = [p['symbol'] for p in raw if float(p.get('contracts', 0) or p.get('size', 0) or 0) > 0]
                for eski in list(AKTIF_POZISYONLAR.keys()):
                    if eski not in anlik:
                        bilgi = AKTIF_POZISYONLAR[eski]
                        g = bilgi.get("giris_fiyati", 0)
                        y = bilgi.get("yon", "LONG")
                        tp_k = bilgi.get("tp_fiyat", g)
                        sl_k = bilgi.get("sl_fiyat", g)

                        karli = False
                        cikis = g
                        try:
                            t = exchange.fetch_ticker(eski)
                            cikis = float(t['last'])
                            tp_uz = abs(cikis - tp_k)
                            sl_uz = abs(cikis - sl_k)
                            karli = tp_uz < sl_uz
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
                            COIN_COOLDOWN[eski] = {"zaman": float(time.time() + COOLDOWN_SURESI_SANIYE), "son_yon": y}
                            if eski in AKTIF_POZISYONLAR:
                                del AKTIF_POZISYONLAR[eski]

                        hafizayi_kaydet()
                        print(f"💰 [KAPANIŞ] {eski} | Çıkış: {cikis}", flush=True)
                        tg_gonder(f"{tip}\n📌 `{eski}` | Çıkış: `{cikis}`")
            except Exception as e:
                print(f"⚠️ Kapanış: {e}", flush=True)

            # SİNYAL TARAMA
            for symbol in TAKIP_EDILENLER:
                if not BOT_CALISIYOR_MU: break
                if len(aktif_map) >= MAKSIMUM_TOPLAM_POZISYON: break
                if symbol in aktif_list: continue

                with state_lock:
                    cd = COIN_COOLDOWN.get(symbol)
                    if cd:
                        z = cd.get("zaman", 0) if isinstance(cd, dict) else float(cd)
                        if z - time.time() > 0:
                            continue

                try:
                    # 15m veri
                    ohlcv = exchange.fetch_ohlcv(symbol, timeframe='15m', limit=SWING_LOOKBACK + 20)
                    df = pd.DataFrame(ohlcv, columns=['t', 'o', 'h', 'l', 'c', 'v'])

                    ticker = exchange.fetch_ticker(symbol)
                    anlik = float(ticker['last'])

                    # Swing noktaları
                    highs, lows = swing_noktalari_bul(df)

                    if len(highs) < 2 or len(lows) < 2:
                        continue

                    # Likidite seviyeleri
                    destekler, direncler = likidite_seviyeleri(df, highs, lows, anlik)

                    if not destekler and not direncler:
                        continue

                    # Sweep tespit
                    yon, likidite_lvl, sl_fiyat, sweep_uc = sweep_tespit_et(df, destekler, direncler, anlik)

                    if yon is None:
                        continue

                    print(f"🎯 [{symbol}] SWEEP! {yon} | Likidite: {likidite_lvl:.6f} | Sweep ucu: {sweep_uc:.6f}", flush=True)

                    # ATR
                    atr = ta.volatility.AverageTrueRange(df['h'], df['l'], df['c'], window=14).average_true_range().iloc[-1]

                    # TP hesabı
                    tp_fiyat, rr = tp_hesapla(yon, anlik, sl_fiyat, destekler, direncler, atr)

                    if rr < MIN_RR:
                        print(f"   ⏭️ R/R düşük: {rr:.2f}", flush=True)
                        continue

                    # İşlem aç
                    bakiye = exchange.fetch_balance()
                    toplam_b = float(bakiye['total'].get('USDT', 0))
                    serbest_b = float(bakiye.get('free', {}).get('USDT', 0) or 0)

                    exchange.set_leverage(KALDIRAC, symbol)
                    market = exchange.market(symbol)

                    kullan = min(toplam_b * 0.3, serbest_b)
                    if kullan < 1: continue

                    miktar = float(exchange.amount_to_precision(
                        symbol,
                        max((kullan * KALDIRAC) / anlik / float(market.get('contractSize', 1.0)),
                            float(market['limits']['amount']['min'] or 1.0))
                    ))

                    iy = 'buy' if yon == 'LONG' else 'sell'
                    kapat_y = 'sell' if yon == 'LONG' else 'buy'

                    exchange.create_order(symbol, 'market', iy, miktar)
                    time.sleep(0.5)

                    sl_ok = False
                    try:
                        exchange.create_order(symbol, 'limit', kapat_y, miktar, tp_fiyat, {'reduceOnly': True})
                        exchange.create_order(symbol, 'stop', kapat_y, miktar, sl_fiyat, {'stopPrice': sl_fiyat, 'reduceOnly': True})
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
                            "giris_fiyati": anlik, "yon": yon,
                            "tp_fiyat": tp_fiyat, "sl_fiyat": sl_fiyat,
                            "giris_zamani": time.time(),
                            "likidite_lvl": likidite_lvl,
                            "sweep_uc": sweep_uc
                        }
                        aktif_list.append(symbol)
                        aktif_map[symbol] = {"dummy": True}

                    hafizayi_kaydet()
                    print(f"✅ [AÇILDI] {symbol} {yon} @ {anlik} | SL: {sl_fiyat} | TP: {tp_fiyat} | R/R: {rr:.2f}", flush=True)

                    tg_gonder(
                        f"🎯 *LİKİDİTE AVI!*\n"
                        f"📌 `{symbol}` | {yon}\n"
                        f"💧 Likidite: `{likidite_lvl:.6f}`\n"
                        f"🌀 Sweep ucu: `{sweep_uc:.6f}`\n"
                        f"🎯 Giriş: `{anlik}`\n"
                        f"💰 TP: `{tp_fiyat}`\n"
                        f"🛑 SL: `{sl_fiyat}`\n"
                        f"📊 R/R: `{rr:.2f}`"
                    )
                except Exception as e:
                    print(f"⚠️ {symbol}: {e}", flush=True)
                    continue

        except Exception as e:
            print(f"⚠️ Ana döngü: {e}", flush=True)
        finally:
            try: tarayici_kilidi.release()
            except: pass

        time.sleep(10)  # 10 saniye

async def main():
    web_thread = threading.Thread(target=run_web, daemon=True)
    web_thread.start()

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
